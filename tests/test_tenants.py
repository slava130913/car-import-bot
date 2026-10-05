"""Боты клиентов (white label): изоляция данных, бренд и комиссия, самостоятельный запуск, статусы заявок, платформа."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import pytest
from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import bot.main as bm
from bot import tenants as tn
from bot.db import DB
from calc.engine import CarInput, calculate, load_rules

PLATFORM_ADMIN = 4242
CLIENT_ADMIN = 5151


def run(coro):
    return asyncio.run(coro)


@dataclass
class User:
    id: int = 1
    username: str | None = "tester"
    full_name: str = "Tester"


@dataclass
class Msg:
    text: str | None = None
    from_user: User = field(default_factory=User)
    contact: Any = None
    message_id: int = 100
    chat: Any = None
    reply_to_message: Any = None
    outbox: list[dict[str, Any]] = field(default_factory=list)
    deleted: bool = False

    async def answer(self, text: str, reply_markup: Any = None) -> None:
        self.outbox.append({"text": text, "kb": reply_markup})

    async def edit_reply_markup(self, reply_markup: Any = None) -> None:
        self.outbox.append({"text": "", "kb": None})

    async def edit_text(self, text: str, reply_markup: Any = None) -> None:
        self.outbox.append({"text": text, "kb": reply_markup, "edited": True})

    async def delete(self) -> None:
        self.deleted = True


@dataclass
class Cb:
    data: str
    message: Msg
    from_user: User = field(default_factory=User)
    alerts: list[str] = field(default_factory=list)

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        if text:
            self.alerts.append(text)


class FakeBot:
    def __init__(self, bot_id: int = 1) -> None:
        self.id = bot_id
        self.sent: list[tuple[int, str, Any]] = []

    async def send_message(self, chat_id: int, text: str, **kw: Any) -> None:
        self.sent.append((chat_id, text, kw.get("reply_markup")))


def texts(m: Msg) -> str:
    return "\n".join(o["text"] for o in m.outbox)


CLIENT = tn.Tenant(id="client_bot", brand="АвтоМост", manager="@most_manager", admin_ids=frozenset({CLIENT_ADMIN}),
                   web_url="https://x.test/?bot=client_bot", service_fee=60000, is_main=False, username="client_bot",
                   status="trial", payment_text="Менеджер свяжется.")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "db", DB(tmp_path / "t.sqlite3"))
    monkeypatch.setattr(bm, "ADMIN_IDS", {PLATFORM_ADMIN})
    monkeypatch.setattr(bm, "BRAND_NAME", "")
    monkeypatch.setattr(bm, "RUNNER", None)
    monkeypatch.setattr(bm, "BOT_TOKEN", "999999:" + "M" * 35)

    async def fake_rates(rules):
        return {"CNY": 12.0, "EUR": 95.0}, "тест"

    monkeypatch.setattr(bm, "get_rates", fake_rates)
    tn.BY_BOT.clear()
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=0, chat_id=1, user_id=1))
    yield state
    tn.BY_BOT.clear()


# ---------- расчёт с услугами компании ----------

def test_engine_extras_add_line_and_total():
    rules = load_rules()
    car = CarInput(price_cny=100_000, age="new", engine_cc=1498, power_hp=147, fuel="ice")
    base = calculate(car, rules)
    withfee = calculate(car, rules, [("Услуги АвтоМост", 60000)])
    assert withfee.lines[-1].label == "Услуги АвтоМост" and withfee.lines[-1].mid == 60000
    assert withfee.total_mid == base.total_mid + 60000
    assert withfee.total_low == base.total_low + 60000 and withfee.total_high == base.total_high + 60000
    assert calculate(car, rules, [("ноль", 0)]).total_mid == base.total_mid


def test_tenant_extras_and_web_url():
    assert CLIENT.extras() == [("Услуги АвтоМост", 60000.0)]
    assert tn.Tenant(id="main").extras() == []
    url = tn.tenant_web_url("https://site.test/calc/", CLIENT)
    assert url.startswith("https://site.test/calc/?bot=client_bot&brand=") and "fee=60000" in url
    assert tn.tenant_web_url("", CLIENT) == ""


# ---------- контекст арендатора ----------

def test_current_defaults_to_main_and_switches(env):
    assert tn.current().is_main and tn.current().admin_ids == frozenset({PLATFORM_ADMIN})
    with tn.use(CLIENT):
        assert tn.current().id == "client_bot"
    assert tn.current().is_main


def test_middleware_picks_tenant_by_bot_id(env):
    tn.BY_BOT[777] = CLIENT
    seen = {}

    async def handler(event, data):
        seen["t"] = tn.current().id

    run(tn.tenant_middleware(handler, object(), {"bot": FakeBot(777)}))
    assert seen["t"] == "client_bot"
    run(tn.tenant_middleware(handler, object(), {"bot": FakeBot(1)}))
    assert seen["t"] == "main"


def test_client_bot_brand_and_partner_link_ignored(env):
    state = env

    async def flow():
        with tn.use(CLIENT):
            m = Msg(text="/start partner", from_user=User(id=CLIENT_ADMIN))
            await bm.cmd_start(m, state, CommandObject(prefix="/", command="start", args="partner"))
            return m, await state.get_state()

    m, st = run(flow())
    assert "Это бот АвтоМост" in texts(m) and "Вы администратор" in texts(m)
    assert st is None  # у бота клиента нет входа для партнёров платформы


def test_calc_in_client_bot_includes_fee(env):
    state = env

    async def flow():
        with tn.use(CLIENT):
            await state.set_state(bm.Calc.dest)
            await state.set_data({"price_cny": 100000, "age": "new", "engine_cc": 1498, "power_hp": 147,
                                  "fuel": "ice", "route": "suifenhe", "made": "2025-01-01", "made_exact": True})
            msg = Msg()
            await bm.calc_dest(Cb(data="dest:moscow", message=msg), state, FakeBot())
            return msg

    msg = run(flow())
    assert "Услуги АвтоМост: 60 000 ₽" in texts(msg)
    assert bm.db.last_calc(1, "client_bot") is not None and bm.db.last_calc(1) is None


def test_lead_isolated_per_tenant_with_status_buttons(env):
    state = env
    client_bot, main_bot = FakeBot(), FakeBot()

    async def lead(bot, tenant):
        with tn.use(tenant):
            m = Msg(text="Хочу пригнать")
            await bm.lead_start(m, state)
            await bm.lead_model(Msg(text="Haval Jolion", outbox=m.outbox), state)
            await bm.lead_budget(Msg(text="2.5 млн", outbox=m.outbox), state)
            await bm.lead_city(Msg(text="Тула", outbox=m.outbox), state)
            await bm.lead_timeline(Cb(data="lt:soon", message=Msg(outbox=m.outbox)), state)
            await bm.lead_contact(Msg(text="@buyer", outbox=m.outbox), state, bot)
            return m

    m = run(lead(client_bot, CLIENT))
    assert "Менеджер АвтоМост свяжется" in texts(m)
    assert [s[0] for s in client_bot.sent] == [CLIENT_ADMIN]
    card, kb = client_bot.sent[0][1], client_bot.sent[0][2]
    assert "В ближайшие 3 месяца" in card and "🆕 Новая" in card
    assert [b.callback_data for b in kb.inline_keyboard[0]] == ["ls:work:1", "ls:won:1", "ls:lost:1"]

    run(lead(main_bot, None))
    assert [s[0] for s in main_bot.sent] == [PLATFORM_ADMIN]
    assert len(bm.db.list_leads(tenant_id="client_bot")) == 1 and len(bm.db.list_leads()) == 1

    # менеджер клиента ставит статус; чужой и платформенный админ не могут
    async def status(user_id, tenant, data="ls:won:1"):
        with tn.use(tenant):
            c = Cb(data=data, message=Msg(), from_user=User(id=user_id, username="boss"))
            await bm.lead_set_status(c)
            return c

    c = run(status(CLIENT_ADMIN, CLIENT))
    assert c.alerts == ["✅ Сделка"] and "✅ Сделка (@boss)" in texts(c.message)
    assert bm.db.get_lead(1, "client_bot")["status"] == "won"
    assert bm.db.get_lead(1, "main") is None  # номер заявки клиента из основного бота не виден
    assert bm.db.get_lead(2, "main")["status"] == "new"
    denied = run(status(PLATFORM_ADMIN, CLIENT, "ls:lost:1"))
    assert denied.alerts == ["Только для менеджеров."] and bm.db.get_lead(1, "client_bot")["status"] == "won"


def test_lead_card_has_last_calc(env):
    state = env
    bm.db.add_calc(1, {"preset": "coolray", "price_cny": 100000}, 2264282, "client_bot")
    bot = FakeBot()

    async def flow():
        with tn.use(CLIENT):
            await state.set_state(bm.Lead.contact)
            await state.set_data({"model": "Coolray", "budget": "2.3 млн", "city": "Тула", "timeline": "now"})
            await bm.lead_contact(Msg(text="+79990000000"), state, bot)

    run(flow())
    card = bot.sent[0][1]
    assert card.startswith("🔥") and "Последний расчёт: Geely Coolray 1.5T, цена в Китае 100 000 ¥, под ключ 2 264 282 ₽" in card


def test_report_counts(env):
    db = bm.db
    for i, st in enumerate(["new", "work", "won", "won"]):
        lid = db.add_lead(1, "u", "m", "b", "c", "x", tenant_id="client_bot", timeline="now" if i == 0 else "later")
        if st != "new":
            db.set_lead_status(lid, "client_bot", st, "@a")

    async def flow():
        with tn.use(CLIENT):
            m = Msg(text="/report", from_user=User(id=CLIENT_ADMIN))
            await bm.admin_report(m)
            return m

    out = texts(run(flow()))
    assert "Заявок: 4, из них горячих 🔥 1" in out and "сделки: 2" in out and "Конверсия в сделку: 50%" in out


# ---------- самостоятельный запуск ----------

def test_white_label_onboarding(env, monkeypatch):
    state = env

    async def fake_validate(token):
        return (123456, "most_auto_bot") if token.startswith("123456:") else None

    monkeypatch.setattr(bm, "validate_token", fake_validate)
    main_bot = FakeBot()
    token = "123456:" + "A" * 35
    owner = User(id=CLIENT_ADMIN, username="owner")

    async def flow():
        m = Msg(text="/mybot", from_user=owner)
        await bm.wl_start(m, state)
        bad = Msg(text="hello", from_user=owner, outbox=m.outbox)
        await bm.wl_token(bad, state)
        assert "не похоже на токен" in m.outbox[-1]["text"]
        wrong = Msg(text="654321:" + "B" * 35, from_user=owner, outbox=m.outbox)
        await bm.wl_token(wrong, state)
        assert "не принял" in m.outbox[-1]["text"]
        tok = Msg(text=token, from_user=owner, outbox=m.outbox)
        await bm.wl_token(tok, state)
        assert tok.deleted
        await bm.wl_brand(Msg(text="АвтоМост", from_user=owner, outbox=m.outbox), state)
        await bm.wl_manager(Msg(text="@most_manager", from_user=owner, outbox=m.outbox), state)
        await bm.wl_fee(Msg(text="60 000", from_user=owner, outbox=m.outbox), state)
        assert "Пробный период 7 дней" in m.outbox[-1]["text"]
        cb = Cb(data="wl:go", message=Msg(outbox=m.outbox), from_user=owner)
        await bm.wl_confirm(cb, state, main_bot)
        return m

    m = run(flow())
    row = bm.db.get_tenant("most_auto_bot")
    assert row is not None and row["brand"] == "АвтоМост" and row["service_fee"] == 60000 and row["status"] == "trial"
    assert row["admin_ids"] == str(CLIENT_ADMIN) and row["token"] == token
    assert "Готово! Бот @most_auto_bot работает" in texts(m) and "нажмите «Старт»" in texts(m)
    assert tn.BY_BOT[123456].brand == "АвтоМост" and tn.BY_BOT[123456].service_fee == 60000
    assert "most_auto_bot" in tn.BY_BOT[123456].web_url or bm.WEB_URL == ""
    assert main_bot.sent and main_bot.sent[0][0] == PLATFORM_ADMIN and "Новый бот клиента @most_auto_bot" in main_bot.sent[0][1]
    assert token not in texts(m)

    # повторно тот же бот подключить нельзя
    async def again():
        m2 = Msg(text="/mybot", from_user=owner)
        await bm.wl_start(m2, state)
        await bm.wl_token(Msg(text=token, from_user=owner, outbox=m2.outbox), state)
        return m2

    assert "уже подключён" in texts(run(again()))


def test_onboarding_only_in_main_bot(env):
    state = env

    async def flow():
        with tn.use(CLIENT):
            m = Msg(text="/mybot")
            await bm.wl_start(m, state)
            return m, await state.get_state()

    m, st = run(flow())
    assert m.outbox == [] and st is None


# ---------- настройки клиента и платформа ----------

def _add_client(db: DB) -> None:
    db.add_tenant(tenant_id="client_bot", bot_id=777, token="777:" + "C" * 35, username="client_bot", brand="АвтоМост",
                  manager="@m", service_fee=0, owner_id=CLIENT_ADMIN)


def test_client_settings(env, monkeypatch):
    _add_client(bm.db)

    async def no_configure(bot, t, commands):
        return None

    monkeypatch.setattr(tn, "configure_bot", no_configure)
    t = bm._tenant_from_row(bm.db.get_tenant("client_bot"))

    async def cmd(name, arg, user=CLIENT_ADMIN):
        with tn.use(tn.BY_BOT.get(777, t)):
            m = Msg(text=f"/{name} {arg}".strip(), from_user=User(id=user))
            await bm.tenant_settings(m, CommandObject(prefix="/", command=name, args=arg or None), FakeBot(777))
            return m

    assert "Настройки бота @client_bot" in texts(run(cmd("settings", "")))
    assert "Сохранено" in texts(run(cmd("setfee", "75000")))
    assert bm.db.get_tenant("client_bot")["service_fee"] == 75000 and tn.BY_BOT[777].service_fee == 75000
    run(cmd("addadmin", "8888"))
    assert bm.db.get_tenant("client_bot")["admin_ids"] == f"{CLIENT_ADMIN},8888"
    assert run(cmd("setfee", "1", user=999)).outbox == []  # не админ
    assert "Нужна сумма" in texts(run(cmd("setfee", "много")))


def test_platform_commands(env):
    _add_client(bm.db)

    async def cmd(name, arg, user=PLATFORM_ADMIN, tenant=None):
        with tn.use(tenant):
            m = Msg(text=f"/{name} {arg}".strip(), from_user=User(id=user))
            if name == "tenants":
                await bm.platform_tenants(m)
            else:
                await bm.platform_tenant_switch(m, CommandObject(prefix="/", command=name, args=arg or None), FakeBot())
            return m

    assert "client_bot @client_bot «АвтоМост»: trial" in texts(run(cmd("tenants", "")))
    assert run(cmd("tenants", "", user=CLIENT_ADMIN)).outbox == []
    assert run(cmd("tenants", "", tenant=CLIENT)).outbox == []  # в боте клиента команды платформы молчат
    assert "оплачен до" in texts(run(cmd("tenant_on", "client_bot 30")))
    assert bm.db.get_tenant("client_bot")["status"] == "active" and tn.BY_BOT[777].status == "active"
    assert "приостановлен" in texts(run(cmd("tenant_off", "client_bot")))
    assert bm.db.get_tenant("client_bot")["status"] == "paused" and 777 not in tn.BY_BOT


def test_trial_expiry_query(env):
    db = bm.db
    db.add_tenant(tenant_id="old_bot", bot_id=1, token="1:" + "D" * 35, username="old_bot", brand="Old", manager="",
                  service_fee=0, owner_id=1, trial_days=-1)
    _add_client(db)
    assert [r["id"] for r in db.tenants_trial_expired()] == ["old_bot"]
    db.update_tenant("old_bot", expiry_notified=1)
    assert db.tenants_trial_expired() == []


# ---------- база: миграция старой схемы ----------

def test_migrates_old_database(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE leads (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, username TEXT, model TEXT,
        budget TEXT, city TEXT, contact TEXT, status TEXT NOT NULL DEFAULT 'new', created_at TEXT NOT NULL);
    CREATE TABLE calcs (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, input_json TEXT NOT NULL,
        total REAL, created_at TEXT NOT NULL);
    INSERT INTO leads(user_id, username, model, budget, city, contact, created_at) VALUES (1,'u','Monjaro','4 млн','Тула','@u','2026-10-01');
    INSERT INTO calcs(user_id, input_json, total, created_at) VALUES (1,'{"price_cny": 1}', 100, '2026-10-01');
    """)
    conn.commit()
    conn.close()
    db = DB(path)
    leads = db.list_leads()
    assert len(leads) == 1 and leads[0]["model"] == "Monjaro" and leads[0]["tenant_id"] == "main"
    assert db.last_calc(1) == {"price_cny": 1}
    DB(path)  # повторное открытие не ломается


# ---------- опрос ботов ----------

def test_runner_feeds_updates_and_handles_revoked_token():
    from aiogram.exceptions import TelegramUnauthorizedError
    from aiogram.methods import GetUpdates

    class FakeDp:
        def __init__(self):
            self.fed = []

        def resolve_used_update_types(self):
            return ["message"]

        async def feed_update(self, bot, update):
            self.fed.append((bot.id, update.update_id))

    @dataclass
    class Upd:
        update_id: int

    class PollBot:
        def __init__(self):
            self.id = 55
            self.calls = 0
            self.session = self

        async def delete_webhook(self, **kw):
            return True

        async def get_updates(self, **kw):
            self.calls += 1
            if self.calls == 1:
                return [Upd(10), Upd(11)]
            raise TelegramUnauthorizedError(method=GetUpdates(), message="Unauthorized")

        async def close(self):
            pass

    async def flow():
        dp = FakeDp()
        runner = tn.Runner(dp)
        revoked = []

        async def on_unauth(bot):
            revoked.append(bot.id)

        runner.on_unauthorized = on_unauth
        bot = PollBot()
        await runner.start(bot)
        await asyncio.wait_for(runner.tasks[55], timeout=2)
        await asyncio.sleep(0)
        return dp, revoked, runner

    dp, revoked, runner = run(flow())
    assert dp.fed == [(55, 10), (55, 11)] and revoked == [55] and not runner.running(55)
