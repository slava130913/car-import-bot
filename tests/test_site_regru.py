"""Сайт на хостинге reg.ru: выкладка по FTPS только изменённых файлов, заявки с формы и их забор ботом."""

from __future__ import annotations

import asyncio
import ftplib
import importlib.util
import io
from pathlib import Path

import aiohttp
from aiohttp import web

import bot.main as bm
from bot.site_leads import fetch_leads, site_token
from tests.test_tenants import PLATFORM_ADMIN, Cb, FakeBot, Msg, User, env  # noqa: F401  (env: фикстура)

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("deploy_regru", ROOT / "scripts" / "deploy_regru.py")
dr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dr)


class FakeFTP:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = set()
        self.stored: list[str] = []
        self.deleted: list[str] = []

    def retrbinary(self, cmd: str, cb) -> None:
        name = cmd.split(" ", 1)[1]
        if name not in self.files:
            raise ftplib.error_perm("550 No such file")
        cb(self.files[name])

    def storbinary(self, cmd: str, fh) -> None:
        name = cmd.split(" ", 1)[1]
        parent = name.rsplit("/", 1)[0] if "/" in name else ""
        assert not parent or parent in self.dirs, f"нет папки {parent}"
        self.files[name] = fh.read()
        self.stored.append(name)

    def mkd(self, d: str) -> None:
        if d in self.dirs:
            raise ftplib.error_perm("550 exists")
        self.dirs.add(d)

    def delete(self, name: str) -> None:
        self.files.pop(name)
        self.deleted.append(name)


def test_token_same_for_bot_and_deploy():
    t = "123456:" + "A" * 35
    assert site_token(t) == dr.site_token(t)
    assert len(site_token(t)) == 64 and site_token(t) != site_token(t + "x")


def test_plan_uploads_changes_and_removes_only_own_stale_files():
    old = {"index.html": "a", "old.html": "b", "api/data/leads.php": "c"}
    new = {"index.html": "a2", "privacy.html": "d"}
    up, rm = dr.plan(old, new)
    assert sorted(up) == ["index.html", "privacy.html"]
    assert rm == ["old.html"]


def test_stage_puts_api_and_config_with_token(tmp_path):
    dr.stage(tmp_path / "s", "123456:" + "B" * 35)
    s = tmp_path / "s"
    # .htaccess в корне снимает noindex технического домена myapphub.tech для /car/
    assert "unset X-Robots-Tag" in (s / ".htaccess").read_text(encoding="utf-8")
    assert ".htaccess" in dr.manifest_of(s)
    assert (s / "index.html").is_file() and (s / "privacy.html").is_file()
    assert (s / "api" / "lead.php").is_file() and (s / "api" / "leads.php").is_file()
    cfg = (s / "api" / "config.php").read_text(encoding="utf-8")
    assert site_token("123456:" + "B" * 35) in cfg
    assert not (s / "api" / "data").exists()


def test_sync_uploads_only_changes(tmp_path):
    base = tmp_path / "site"
    (base / "auto").mkdir(parents=True)
    (base / "index.html").write_text("v1", encoding="utf-8")
    (base / "auto" / "a.html").write_text("a", encoding="utf-8")
    (base / "gone.html").write_text("g", encoding="utf-8")
    ftp = FakeFTP()
    assert dr.sync(ftp, base) == (3, 0)
    assert ftp.stored[-2] == "index.html"  # главная страница последней, перед списком файлов
    ftp.stored.clear()
    assert dr.sync(ftp, base) == (0, 0)
    (base / "index.html").write_text("v2", encoding="utf-8")
    (base / "gone.html").unlink()
    assert dr.sync(ftp, base) == (1, 1)
    assert ftp.files["index.html"] == b"v2" and "gone.html" not in ftp.files


class DirFTP:
    """Только переходы по папкам: какие есть на хостинге и какие создал скрипт."""

    def __init__(self, dirs: set[str]) -> None:
        self.dirs, self.cur, self.made = set(dirs), "", []

    def _abs(self, d: str) -> str:
        return f"{self.cur}/{d}".strip("/") if self.cur else d

    def cwd(self, d: str) -> None:
        if self._abs(d) not in self.dirs:
            raise ftplib.error_perm("550 no such dir")
        self.cur = self._abs(d)

    def mkd(self, d: str) -> None:
        self.dirs.add(self._abs(d))
        self.made.append(self._abs(d))


def test_open_site_dir_creates_subfolder_only_inside_existing_site():
    ftp = DirFTP({"www", "www/myapphub.tech"})
    assert dr.open_site_dir(ftp, "www/myapphub.tech/car") and ftp.cur == "www/myapphub.tech/car"
    assert ftp.made == ["www/myapphub.tech/car"]
    # Новый сайт www/<домен> скрипт не создаёт: домен на такую папку смотреть не будет
    ftp = DirFTP({"www"})
    assert not dr.open_site_dir(ftp, "www/car.myapphub.tech") and ftp.made == []
    # Сайта-родителя нет: тоже ничего не создаём
    ftp = DirFTP({"www"})
    assert not dr.open_site_dir(ftp, "www/nosuch.tech/car") and ftp.made == []
    ftp = DirFTP({"www", "www/myapphub.tech", "www/myapphub.tech/car"})
    assert dr.open_site_dir(ftp, "www/myapphub.tech/car") and ftp.made == []


def test_deploy_skips_without_hosting_settings(monkeypatch):
    for k in ("REGRU_FTP_HOST", "REGRU_FTP_USER", "REGRU_FTP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    assert dr.main() == 0


def test_index_has_lead_form_and_privacy_link():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert 'id="leadForm"' in html and 'href="privacy.html"' in html and "leadUrl" in html
    assert "LEAD_FORM = Boolean(LEAD_URL) && !BRAND && !botParam" in html


# ---------- бот забирает заявки ----------

def _site_lead(i: int, **kw) -> dict:
    d = {"id": i, "name": "Иван", "phone": "+7 900 111-22-33", "city": "Тула", "when": "now",
         "model": "Haval Jolion", "calc": "Haval Jolion, под ключ 2 100 000 ₽", "total": 2100000}
    d.update(kw)
    return d


def test_ingest_site_leads_notifies_and_dedupes(env):
    bot = FakeBot()
    assert asyncio.run(bm.ingest_site_leads(bot, [_site_lead(1), _site_lead(2, when="", name="")])) == 2
    assert len(bot.sent) == 2
    card = bot.sent[0][1]
    assert bot.sent[0][0] == PLATFORM_ADMIN
    assert "🔥" in card and "+7 900 111-22-33 (Иван)" in card and "форма на сайте" in card and "Тула" in card
    assert "по расчёту 2 100 000 ₽" in card.replace("\xa0", " ")
    assert bot.sent[0][2] is not None  # кнопки статуса
    assert bm.db.get_meta("site_leads_after") == "2"
    # Повтор тех же заявок ничего не добавляет
    assert asyncio.run(bm.ingest_site_leads(bot, [_site_lead(1), _site_lead(2)])) == 0
    r = bm.db.list_leads(tenant_id=bm.MAIN)
    assert len(r) == 2 and r[0]["source"] == "site" and r[0]["user_id"] == 0


def test_site_lead_status_button_rerenders(env):
    bot = FakeBot()
    asyncio.run(bm.ingest_site_leads(bot, [_site_lead(5)]))
    lead_id = bm.db.list_leads(tenant_id=bm.MAIN)[0]["id"]
    msg = Msg()
    cb = Cb(data=f"ls:work:{lead_id}", message=msg, from_user=User(id=PLATFORM_ADMIN, username="boss"))
    asyncio.run(bm.lead_set_status(cb))
    assert "форма на сайте" in msg.outbox[-1]["text"] and "В работе" in msg.outbox[-1]["text"]


def test_fetch_leads_sends_token_and_reads_reply():
    token = site_token("123456:" + "C" * 35)
    seen = {}

    async def handler(request: web.Request) -> web.Response:
        seen["token"] = request.headers.get("X-Token")
        seen["after"] = request.query.get("after")
        if request.headers.get("X-Token") != token:
            return web.json_response({"ok": False}, status=403)
        return web.json_response({"ok": True, "leads": [_site_lead(4), {"id": "x"}, {"id": 0}], "last": 4, "epoch": "ab12"})

    async def scenario():
        app = web.Application()
        app.router.add_get("/api/leads.php", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        url = f"http://127.0.0.1:{port}/api/leads.php"
        try:
            async with aiohttp.ClientSession() as s:
                leads, last, epoch = await fetch_leads(s, url, token, 3)
                try:
                    await fetch_leads(s, url, "wrong", 3)
                    raise AssertionError("ожидали ошибку")
                except RuntimeError as e:
                    assert "403" in str(e)
        finally:
            await runner.cleanup()
        return leads, last, epoch

    leads, last, epoch = asyncio.run(scenario())
    assert [x["id"] for x in leads] == [4] and last == 4 and epoch == "ab12"
    assert seen["after"] == "3"


class FlakyBot(FakeBot):
    """Telegram недоступен: сообщения не уходят."""

    def __init__(self) -> None:
        super().__init__()
        self.fail = True

    async def send_message(self, chat_id, text, **kw):
        if self.fail:
            raise RuntimeError("telegram down")
        await super().send_message(chat_id, text, **kw)


def test_undelivered_site_lead_is_retried_without_duplicate_row(env):
    bot = FlakyBot()
    assert asyncio.run(bm.ingest_site_leads(bot, [_site_lead(1), _site_lead(2)], "e1")) == 0
    assert bm.db.get_meta("site_leads_after", "0") == "0"  # номер не сдвинулся: заявка придёт снова
    assert len(bm.db.list_leads(tenant_id=bm.MAIN)) == 1  # вторую не трогали, пока первая не доставлена
    bot.fail = False
    assert asyncio.run(bm.ingest_site_leads(bot, [_site_lead(1), _site_lead(2)], "e1")) == 2
    rows = bm.db.list_leads(tenant_id=bm.MAIN)
    assert len(rows) == 2 and {r["site_ref"] for r in rows} == {"e1:1", "e1:2"}
    assert len(bot.sent) == 2 and bm.db.get_meta("site_leads_after") == "2"


def test_replay_after_epoch_reset_does_not_duplicate(env):
    bot = FakeBot()
    asyncio.run(bm.ingest_site_leads(bot, [_site_lead(1), _site_lead(2)], "e1"))
    bm.db.set_meta("site_leads_after", "0")  # бот начал с нуля, сайт отдал те же заявки ещё раз
    asyncio.run(bm.ingest_site_leads(bot, [_site_lead(1), _site_lead(2)], "e1"))
    assert len(bm.db.list_leads(tenant_id=bm.MAIN)) == 2
    # Новый счётчик на сайте: номер 1 снова свободен, но это другая заявка
    bm.db.set_meta("site_leads_after", "0")
    asyncio.run(bm.ingest_site_leads(bot, [_site_lead(1, phone="+7 900 999-99-99")], "e2"))
    assert len(bm.db.list_leads(tenant_id=bm.MAIN)) == 3


def test_old_lead_contacts_are_erased_after_a_year(env):
    old = bm.db.add_lead(77, "ivan", "Jolion", "2 млн", "Тула", "+7 900 111-22-33")
    new = bm.db.add_lead(78, "petr", "Jolion", "2 млн", "Тула", "+7 900 222-33-44")
    bm.db.conn.execute("UPDATE leads SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?", (old,))
    bm.db.conn.commit()
    assert bm.db.anonymize_old_leads(days=365) == 1
    assert bm.db.anonymize_old_leads(days=365) == 0
    r = bm.db.get_lead(old, bm.MAIN)
    assert "900" not in r["contact"] and r["username"] is None and r["user_id"] == 0 and r["model"] == "Jolion"
    assert bm.db.get_lead(new, bm.MAIN)["contact"] == "+7 900 222-33-44"
