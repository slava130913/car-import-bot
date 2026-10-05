"""Приём заявок на PHP (site_api/): проверка встроенным сервером PHP.

Нужен php в PATH (в CI его ставит ci/test.sh). Без php тест пропускается.
Можно проверить и уже запущенный сервер: PHP_SITE_URL=http://127.0.0.1:8089 PHP_SITE_DIR=<папка сайта>.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from bot.site_leads import site_token

ROOT = Path(__file__).resolve().parent.parent
BOT_TOKEN = "123456:" + "P" * 35


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    if os.environ.get("PHP_SITE_URL"):
        yield os.environ["PHP_SITE_URL"].rstrip("/"), Path(os.environ["PHP_SITE_DIR"])
        return
    if not shutil.which("php"):
        pytest.skip("php не установлен")
    import importlib.util

    spec = importlib.util.spec_from_file_location("deploy_regru", ROOT / "scripts" / "deploy_regru.py")
    dr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dr)
    base = tmp_path_factory.mktemp("php") / "site"
    dr.stage(base, BOT_TOKEN)
    port = _free_port()
    proc = subprocess.Popen(["php", "-S", f"127.0.0.1:{port}", "-t", str(base)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield url, base
    proc.terminate()
    proc.wait(timeout=5)


def call(url: str, data: dict | None = None, headers: dict | None = None) -> tuple[int, dict | str]:
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", **(headers or {})},
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            code, raw = r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        code, raw = e.code, e.read().decode()
    try:
        return code, json.loads(raw)
    except ValueError:
        return code, raw


LEAD = {"name": "Иван", "phone": "+7 900 111-22-33", "city": "Тула", "when": "now", "model": "Haval Jolion",
        "calc": "Haval Jolion, под ключ 2 100 000 ₽", "total": 2100000, "consent": True, "website": "", "elapsed": 9000}


def test_php_lead_flow(site):
    url, base = site
    assert call(f"{url}/api/lead.php")[0] == 405
    code, d = call(f"{url}/api/lead.php", LEAD)
    assert code == 200 and d == {"ok": True, "id": 1}
    assert call(f"{url}/api/lead.php", {**LEAD, "phone": "123"}) == (422, {"ok": False, "error": "phone"})
    assert call(f"{url}/api/lead.php", {**LEAD, "consent": False}) == (422, {"ok": False, "error": "consent"})
    # Ловушки для ботов: «принято», но не сохраняется
    assert call(f"{url}/api/lead.php", {**LEAD, "website": "http://spam"}) == (200, {"ok": True, "id": 0})
    # Слишком быстро после открытия страницы: не «принято», а просьба повторить (страница повторит сама)
    assert call(f"{url}/api/lead.php", {**LEAD, "elapsed": 500}) == (422, {"ok": False, "error": "too_fast"})
    code, d = call(f"{url}/api/lead.php", {**LEAD, "name": "x" * 500, "when": "bad", "total": -5, "city": "a\x00b"})
    assert code == 200 and d["id"] == 2

    # Бот забирает заявки только с правильным токеном
    assert call(f"{url}/api/leads.php")[0] == 403
    assert call(f"{url}/api/leads.php", headers={"X-Token": "wrong"})[0] == 403
    tok = {"X-Token": site_token(BOT_TOKEN)}
    code, d = call(f"{url}/api/leads.php?after=0", headers=tok)
    assert code == 200 and d["last"] == 2 and [x["id"] for x in d["leads"]] == [1, 2]
    epoch = d["epoch"]
    assert len(epoch) == 16
    first, second = d["leads"]
    assert first["phone"] == "+7 900 111-22-33" and first["when"] == "now" and first["total"] == 2100000
    assert len(second["name"]) == 80 and second["when"] == "" and second["total"] == 0 and "\x00" not in second["city"]
    assert call(f"{url}/api/leads.php?after=2", headers=tok)[1] == {"ok": True, "leads": [], "last": 2, "epoch": epoch}

    # Файлы данных не отдаются: защитная строка PHP обрывает вывод, служебные файлы ничего не печатают
    for path in ("api/data/leads.php", "api/data/rate.php", "api/config.php", "api/_lib.php"):
        code, raw = call(f"{url}/{path}")
        assert "900" not in str(raw) and site_token(BOT_TOKEN) not in str(raw), path
    stored = (base / "api" / "data" / "leads.php").read_text(encoding="utf-8")
    assert stored.startswith("<?php exit; ?>") and "+7 900 111-22-33" in stored

    # Удалили последнюю заявку руками (просьба клиента): номер 2 не выдаётся повторно
    leads_file = base / "api" / "data" / "leads.php"
    leads_file.write_text("".join(ln for ln in leads_file.read_text(encoding="utf-8").splitlines(True) if '"id":2,' not in ln),
                          encoding="utf-8")
    code, d = call(f"{url}/api/lead.php", LEAD)
    assert code == 200 and d["id"] == 3
    code, d = call(f"{url}/api/leads.php?after=2", headers=tok)
    assert [x["id"] for x in d["leads"]] == [3] and d["last"] == 3 and d["epoch"] == epoch

    # Не больше 5 заявок в час с одного адреса (3 уже приняты)
    codes = [call(f"{url}/api/lead.php", LEAD)[0] for _ in range(3)]
    assert codes == [200, 200, 429]

    # Старые записи о частоте удаляются при опросе ботом, даже без новых заявок
    rate_file = base / "api" / "data" / "rate.php"
    rate_file.write_text('<?php exit; ?>\n{"k":"old","t":1}\n', encoding="utf-8")
    call(f"{url}/api/leads.php?after=0", headers=tok)
    assert '"old"' not in rate_file.read_text(encoding="utf-8")

    # Заявки старше года удаляются с хостинга
    leads_file.write_text(leads_file.read_text(encoding="utf-8").replace('"id":1,"ts":"20', '"id":1,"ts":"19'), encoding="utf-8")
    code, d = call(f"{url}/api/leads.php?after=0", headers=tok)
    assert 1 not in [x["id"] for x in d["leads"]] and '"id":1,' not in leads_file.read_text(encoding="utf-8")
