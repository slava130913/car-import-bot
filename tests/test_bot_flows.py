"""Прогон диалогов бота без Telegram: хендлеры вызываются напрямую с фейковыми Message/CallbackQuery."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import bot.main as bm
from bot.db import DB

ADMIN_ID = 4242


@dataclass
class User:
    id: int = 1
    username: str | None = "tester"


@dataclass
class Contact:
    phone_number: str


@dataclass
class Msg:
    text: str | None = None
    from_user: User = field(default_factory=User)
    contact: Contact | None = None
    outbox: list[dict[str, Any]] = field(default_factory=list)

    async def answer(self, text: str, reply_markup: Any = None) -> None:
        self.outbox.append({"type": "text", "text": text, "kb": reply_markup})

    async def answer_invoice(self, **kw: Any) -> None:
        self.outbox.append({"type": "invoice", **kw})

    async def edit_reply_markup(self, reply_markup: Any = None) -> None:
        self.outbox.append({"type": "edit_kb"})


@dataclass
class Cb:
    data: str
    message: Msg
    from_user: User = field(default_factory=User)

    async def answer(self) -> None:
        pass


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "db", DB(tmp_path / "t.sqlite3"))
    monkeypatch.setattr(bm, "ADMIN_IDS", {ADMIN_ID})
    monkeypatch.setattr(bm, "VIN_PRICE_STARS", 0)

    async def fake_rates(rules):
        fb = rules["fallback_rates"]
        return {"CNY": fb["CNY"], "EUR": fb["EUR"], "USD": fb["USD"]}, "тестовый курс"

    monkeypatch.setattr(bm, "get_rates", fake_rates)
    storage = MemoryStorage()
    state = FSMContext(storage=storage, key=StorageKey(bot_id=0, chat_id=1, user_id=1))
    return state, FakeBot()


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def texts(msg: Msg) -> str:
    return "\n".join(o["text"] for o in msg.outbox if o["type"] == "text")


def test_calc_flow_end_to_end(env):
    state, fbot = env

    async def flow():
        m = Msg(text=bm.BTN_CALC)
        await bm.calc_start(m, state)
        await bm.calc_price(Msg(text="abc", outbox=m.outbox), state)
        assert "число" in m.outbox[-1]["text"]
        await bm.calc_price(Msg(text="95 000", outbox=m.outbox), state)
        await bm.calc_made(Msg(text="2025-01", outbox=m.outbox), state)
        assert "до 3 лет" in m.outbox[-1]["text"]
        await bm.calc_cc(Msg(text="1.5", outbox=m.outbox), state)
        assert (await state.get_data())["engine_cc"] == 1500
        await bm.calc_hp(Msg(text="147", outbox=m.outbox), state)
        await bm.calc_fuel(Cb("fuel:ice", m), state)
        await bm.calc_route(Cb("route:sea", m), state)
        await bm.calc_dest(Cb("dest:tula", m), state, fbot)
        return m

    m = run(flow())
    out = texts(m)
    assert "ИТОГО ориентировочно" in out
    assert "Тула" in out and "Морем" in out
    assert "льготная ставка" in out
    assert "тестовый курс" in out
    assert bm.db.stats()["calcs"] == 1
    assert run(state.get_state()) is None


def test_calc_flow_warns_over_160hp(env):
    state, fbot = env

    async def flow():
        m = Msg(text="x")
        await bm.calc_start(m, state)
        await bm.calc_price(Msg(text="180000", outbox=m.outbox), state)
        await bm.calc_made(Msg(text="2024", outbox=m.outbox), state)
        assert "месяц не указан" in m.outbox[-1]["text"]
        await bm.calc_cc(Msg(text="1998", outbox=m.outbox), state)
        await bm.calc_hp(Msg(text="238", outbox=m.outbox), state)
        await bm.calc_fuel(Cb("fuel:ice", m), state)
        await bm.calc_route(Cb("route:suifenhe", m), state)
        await bm.calc_dest(Cb("dest:moscow", m), state, fbot)
        return m

    out = texts(run(flow()))
    assert "выше порога 160" in out
    assert "коммерческая ставка" in out


def test_vin_flow_without_stars(env):
    state, fbot = env

    async def flow():
        m = Msg(text=bm.BTN_VIN)
        await bm.vin_start(m, state)
        await bm.vin_input(Msg(text="123", outbox=m.outbox), state)
        assert "17 символов" in m.outbox[-1]["text"]
        await bm.vin_input(Msg(text="lvvdb21b5md123456", outbox=m.outbox), state)
        await bm.vin_contact(Msg(contact=Contact("+79990000000"), outbox=m.outbox), state, fbot)
        return m

    m = run(flow())
    assert "Заказ #1 принят" in texts(m)
    assert not any(o["type"] == "invoice" for o in m.outbox)
    assert fbot.sent and fbot.sent[0][0] == ADMIN_ID and "LVVDB21B5MD123456" in fbot.sent[0][1]
    assert bm.db.get_order(1)["status"] == "new"


def test_vin_flow_with_stars_sends_invoice(env, monkeypatch):
    state, fbot = env
    monkeypatch.setattr(bm, "VIN_PRICE_STARS", 500)

    async def flow():
        m = Msg(text=bm.BTN_VIN)
        await bm.vin_start(m, state)
        await bm.vin_input(Msg(text="LVVDB21B5MD123456", outbox=m.outbox), state)
        await bm.vin_contact(Msg(text="@buyer", outbox=m.outbox), state, fbot)
        return m

    m = run(flow())
    inv = next(o for o in m.outbox if o["type"] == "invoice")
    assert inv["currency"] == "XTR" and inv["payload"] == "vin:1"
    assert inv["prices"][0].amount == 500
    assert bm.db.get_order(1)["status"] == "awaiting_payment"


def test_lead_flow_and_admin_commands(env):
    state, fbot = env

    async def flow():
        m = Msg(text=bm.BTN_LEAD)
        await bm.lead_start(m, state)
        await bm.lead_model(Msg(text="Zeekr 001 2024", outbox=m.outbox), state)
        await bm.lead_budget(Msg(text="4.5 млн", outbox=m.outbox), state)
        await bm.lead_city(Msg(text="Тула", outbox=m.outbox), state)
        await bm.lead_contact(Msg(text="@buyer", outbox=m.outbox), state, fbot)
        admin = Msg(text="/leads", from_user=User(id=ADMIN_ID, username="admin"))
        await bm.admin_leads(admin)
        stats = Msg(text="/stats", from_user=User(id=ADMIN_ID))
        await bm.admin_stats(stats)
        stranger = Msg(text="/stats", from_user=User(id=777))
        await bm.admin_stats(stranger)
        return m, admin, stats, stranger

    m, admin, stats, stranger = run(flow())
    assert "Заявка #1 принята" in texts(m)
    assert "Zeekr 001" in fbot.sent[0][1] and "Тула" in fbot.sent[0][1]
    assert "Zeekr 001" in texts(admin)
    assert "Заявок: 1" in texts(stats)
    assert stranger.outbox == []


def test_start_deep_links(env):
    state, fbot = env

    async def flow():
        m = Msg(text="/start vin")
        await bm.cmd_start(m, state, CommandObject(prefix="/", command="start", args="vin"))
        s1 = await state.get_state()
        m2 = Msg(text="/start")
        await bm.cmd_start(m2, state, CommandObject(prefix="/", command="start", args=None))
        s2 = await state.get_state()
        return m, s1, m2, s2

    m, s1, m2, s2 = run(flow())
    assert s1 == bm.Vin.vin.state
    assert "Введите VIN" in texts(m)
    assert s2 is None and "Привет" in texts(m2)


def test_done_notifies_user(env):
    state, fbot = env
    oid = bm.db.add_order(55, "u", "LVVDB21B5MD123456", "@u", 0)

    async def flow():
        admin = Msg(text=f"/done {oid}", from_user=User(id=ADMIN_ID))
        await bm.admin_done(admin, fbot)
        return admin

    admin = run(flow())
    assert "закрыт" in texts(admin)
    assert bm.db.get_order(oid)["status"] == "done"
    assert any(chat == 55 for chat, _ in fbot.sent)


def test_calc_flow_ev_and_recalc_button(env):
    state, fbot = env

    async def flow():
        m = Msg(text=bm.BTN_CALC)
        await bm.calc_start(m, state)
        await bm.calc_price(Msg(text="150000", outbox=m.outbox), state)
        await bm.calc_made(Msg(text="2025-06", outbox=m.outbox), state)
        await bm.calc_cc(Msg(text="0", outbox=m.outbox), state)
        await bm.calc_hp(Msg(text="204", outbox=m.outbox), state)
        await bm.calc_fuel(Cb("fuel:ev", m), state)
        await bm.calc_route(Cb("route:suifenhe", m), state)
        await bm.calc_dest(Cb("dest:moscow", m), state, fbot)
        out = texts(m)
        assert "Пошлина 15%" in out and "НДС 22%" in out and "Акциз" in out
        assert "выше порога 80" in out
        # кнопка «Пересчитать» снова просит цену
        await bm.calc_restart(Cb("go:calc", m), state)
        assert await state.get_state() == bm.Calc.price.state
        return m

    run(flow())


def test_calc_rejects_bad_year_and_huge_cc(env):
    state, fbot = env

    async def flow():
        m = Msg(text=bm.BTN_CALC)
        await bm.calc_start(m, state)
        await bm.calc_price(Msg(text="100000", outbox=m.outbox), state)
        await bm.calc_made(Msg(text="2099", outbox=m.outbox), state)
        assert "Проверьте год" in m.outbox[-1]["text"]
        await bm.calc_made(Msg(text="2022-02", outbox=m.outbox), state)
        assert "от 3 до 5 лет" in m.outbox[-1]["text"]
        await bm.calc_cc(Msg(text="99999", outbox=m.outbox), state)
        assert "Проверьте объём" in m.outbox[-1]["text"]
        return m

    run(flow())


def test_start_deep_link_lead(env):
    state, fbot = env

    async def flow():
        m = Msg(text="/start lead")
        await bm.cmd_start(m, state, CommandObject(prefix="/", command="start", args="lead"))
        return m, await state.get_state()

    m, s = run(flow())
    assert s == bm.Lead.model.state and "Какую машину" in texts(m)
