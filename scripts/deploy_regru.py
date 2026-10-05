"""Выкладывает сайт (папка web/) и приём заявок (site_api/ → api/) на обычный хостинг reg.ru по FTPS.

Запуск после scripts/build_web.py:  python scripts/deploy_regru.py
Переменные окружения (в Gitea: Settings → Actions → Variables / Secrets):
  REGRU_FTP_HOST      сервер хостинга, например server298.hosting.reg.ru
  REGRU_FTP_USER      логин FTP (u3639514)
  REGRU_FTP_PASSWORD  пароль FTP (секрет)
  REGRU_SITE_DIR      папка сайта от корня FTP, например www/myapphub.tech/car
  BOT_TOKEN           из него считается токен, по которому бот забирает заявки (тот же считает бот)
  SITE_URL            адрес сайта для проверки после выкладки
  REGRU_FTP_INSECURE  1, если сертификат FTP-сервера не проходит проверку
Без REGRU_FTP_HOST/USER/PASSWORD скрипт ничего не делает и завершается успешно.

Загружаются только изменённые файлы: список с хешами лежит на хостинге в .deploy-manifest.json.
Файлы, которые раньше выкладывал скрипт и которых больше нет, удаляются. Папку api/data (заявки) скрипт не трогает.
"""

from __future__ import annotations

import ftplib
import hashlib
import hmac
import io
import json
import os
import shutil
import ssl
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ".deploy-manifest.json"
SKIP_PARTS = {"__pycache__", ".git", "data"}


def site_token(bot_token: str) -> str:
    """Токен выдачи заявок боту. bot/site_leads.py считает его так же."""
    return hmac.new(bot_token.encode(), b"site-leads", hashlib.sha256).hexdigest()


def stage(dest: Path, bot_token: str) -> None:
    """Собирает то, что поедет на хостинг: web/ в корень, site_api/ в api/, config.php с токеном."""
    shutil.copytree(ROOT / "web", dest, dirs_exist_ok=True)
    shutil.copytree(ROOT / "site_api", dest / "api", dirs_exist_ok=True)
    token = site_token(bot_token) if bot_token else ""
    salt = hashlib.sha256(f"salt|{token}".encode()).hexdigest()[:32] if token else "car-leads"
    (dest / "api" / "config.php").write_text(
        "<?php\n// Пишется при выкладке (scripts/deploy_regru.py), в репозиторий не попадает.\n"
        f"return ['token' => '{token}', 'salt' => '{salt}'];\n",
        encoding="utf-8",
    )


def manifest_of(base: Path) -> dict[str, str]:
    out = {}
    for p in sorted(base.rglob("*")):
        rel = p.relative_to(base)
        if p.is_file() and not SKIP_PARTS.intersection(rel.parts):
            out[rel.as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def plan(old: dict[str, str], new: dict[str, str]) -> tuple[list[str], list[str]]:
    """Что загрузить (новое или изменённое) и что удалить (было выложено раньше, теперь нет)."""
    upload = [k for k, v in new.items() if old.get(k) != v]
    remove = [k for k in old if k not in new and not k.startswith("api/data/")]
    return upload, remove


def read_remote_manifest(ftp: ftplib.FTP) -> dict[str, str]:
    buf = io.BytesIO()
    try:
        ftp.retrbinary(f"RETR {MANIFEST}", buf.write)
    except ftplib.error_perm:
        return {}
    try:
        data = json.loads(buf.getvalue().decode("utf-8"))
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except ValueError:
        return {}


def ensure_dirs(ftp: ftplib.FTP, rel: str, made: set[str]) -> None:
    parts = rel.split("/")[:-1]
    for i in range(1, len(parts) + 1):
        d = "/".join(parts[:i])
        if d in made:
            continue
        try:
            ftp.mkd(d)
        except ftplib.error_perm:
            pass  # уже есть
        made.add(d)


def sync(ftp: ftplib.FTP, base: Path) -> tuple[int, int]:
    old = read_remote_manifest(ftp)
    new = manifest_of(base)
    upload, remove = plan(old, new)
    made: set[str] = set()
    # Сначала всё, кроме index.html, потом index.html: страница не сошлётся на ещё не загруженные файлы.
    for rel in sorted(upload, key=lambda r: (r.endswith("index.html"), r)):
        ensure_dirs(ftp, rel, made)
        with open(base / rel, "rb") as fh:
            ftp.storbinary(f"STOR {rel}", fh)
    for rel in remove:
        try:
            ftp.delete(rel)
        except ftplib.error_perm:
            pass
    ftp.storbinary(f"STOR {MANIFEST}", io.BytesIO(json.dumps(new, indent=0, sort_keys=True).encode()))
    return len(upload), len(remove)


class SessionReuseFTP(ftplib.FTP_TLS):
    """FTPS, который переиспользует TLS-сессию в канале данных: ProFTPD и vsftpd без этого отказывают (522)."""

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size


CHAIN = ROOT / "scripts" / "certs" / "regru-ftp-chain.pem"


def connect(host: str, user: str, password: str) -> ftplib.FTP_TLS:
    ctx = ssl.create_default_context()
    # Сервер reg.ru не отдаёт промежуточный сертификат: добавляем его сами, проверка остаётся полной
    ctx.load_verify_locations(cafile=str(CHAIN))
    if os.environ.get("REGRU_FTP_INSECURE") == "1":
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    ftp = SessionReuseFTP(host, timeout=60, context=ctx)
    ftp.login(user, password)
    ftp.prot_p()
    return ftp


def open_site_dir(ftp: ftplib.FTP, site_dir: str) -> bool:
    """Переходит в папку сайта. Подпапку внутри существующего сайта (www/myapphub.tech/car) создаёт сама,
    новый сайт (www/<домен>) не создаёт: его заводят в панели, иначе домен не будет на него смотреть."""
    try:
        ftp.cwd(site_dir)
        return True
    except ftplib.error_perm:
        pass
    parent, _, leaf = site_dir.rpartition("/")
    if parent.count("/") < 1:  # родитель www или корень: это был бы новый сайт
        return False
    try:
        ftp.cwd(parent)
    except ftplib.error_perm:
        return False
    try:
        ftp.mkd(leaf)
        ftp.cwd(leaf)
    except ftplib.error_perm:
        return False
    print(f"Создана папка {site_dir}")
    return True


def check_site(url: str) -> None:
    """После выкладки: страница открывается, PHP работает (GET на приём заявок отвечает 405)."""
    url = url.rstrip("/")
    try:
        with urllib.request.urlopen(f"{url}/", timeout=20) as r:
            ok = "Сколько стоит" in r.read().decode("utf-8", "replace")
        print(f"{url}/ открывается" + ("" if ok else ", но это не наша страница (старый кеш или чужой сайт?)"))
    except Exception as e:  # noqa: BLE001
        print(f"::warning::{url}/ не открылся: {e}. Проверьте A-запись домена и SSL в панели reg.ru.")
        return
    try:
        urllib.request.urlopen(f"{url}/api/lead.php", timeout=20)
        print("::warning::api/lead.php ответил не 405: PHP на сайте, похоже, выключен")
    except urllib.error.HTTPError as e:
        print("api/lead.php: PHP работает" if e.code == 405 else f"::warning::api/lead.php ответил {e.code}")
    except Exception as e:  # noqa: BLE001
        print(f"::warning::api/lead.php недоступен: {e}")


def main() -> int:
    host = os.environ.get("REGRU_FTP_HOST", "").strip()
    user = os.environ.get("REGRU_FTP_USER", "").strip()
    password = os.environ.get("REGRU_FTP_PASSWORD", "")
    site_dir = os.environ.get("REGRU_SITE_DIR", "").strip().strip("/")
    if not (host and user and password):
        print("Хостинг reg.ru не настроен (нет REGRU_FTP_HOST, REGRU_FTP_USER или секрета REGRU_FTP_PASSWORD), сайт не выкладываю.")
        return 0
    if not site_dir:
        print("::error::Не задана REGRU_SITE_DIR, например www/myapphub.tech/car")
        return 1
    bot_token = os.environ.get("BOT_TOKEN", "").strip()
    if not bot_token:
        print("::warning::BOT_TOKEN не передан: заявки с сайта будут копиться на хостинге, но бот их не заберёт.")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "site"
        stage(base, bot_token)
        try:
            ftp = connect(host, user, password)
        except ssl.SSLError as e:
            print(f"::error::TLS с {host} не установился: {e}. Если reg.ru сменил сертификат, обновите scripts/certs/regru-ftp-chain.pem "
                  "(промежуточный сертификат из цепочки сервера).")
            return 1
        except ftplib.error_perm as e:
            print(f"::error::FTP отказал во входе: {e}. Проверьте логин и пароль FTP в панели reg.ru.")
            return 1
        except OSError as e:
            print(f"::error::Нет связи с {host}:21: {e}. Проверьте имя сервера и не закрыт ли FTP по IP в панели reg.ru.")
            return 1
        with ftp:
            if not open_site_dir(ftp, site_dir):
                print(f"::error::На хостинге нет папки {site_dir}. Создайте сайт в панели reg.ru (ispmanager → Сайты).")
                return 1
            up, rm = sync(ftp, base)
        print(f"reg.ru: загружено {up}, удалено {rm}, папка {site_dir}")
    site_url = os.environ.get("SITE_URL", "").strip()
    if site_url:
        check_site(site_url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
