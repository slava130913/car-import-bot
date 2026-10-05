"""Новые сценарии: пресеты моделей, сравнение маршрутов, подсказка про 3 года, проверка льготы, гид, поддержка, экспорт, рассылка."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, timedelta
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
class Chat:
    id: int = 1


@dataclass
class Msg:
    text: str | None = None
    from_user: User = field(default_factory=User)
    contact: Any = None
    message_id: int = 100
    chat: Chat = field(default_factory=Chat)
    reply_to_message: Any = None
    outbox: list[dict[str, Any]] = field(default_factory=list)

    async def answer(self, text: str, reply_markup: Any = None) -> None:
        self.outbox.append({"type": "text", "text": text, "kb": reply_markup})

    async def answer_document(self, doc: Any, caption: str = "") -> None:
        self.outbox.append({"type": "doc", "doc": doc, "caption": caption})

    async def edit_reply_markup(self, reply_markup: Any = None) -> None:
        self.outbox.append({"type": "edit_kb"})


@dataclass
class Cb:
    data: str
    message: Msg
    from_user: User = field(default_factory=User)

    async def answer(self) -> None:
        pass


@dataclass
class Fwd:
    message_id: int


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.forwarded: list[tuple[int, int, int]] = []
        self.copied: list[tuple[int, int, int]] = []
        self._next_id = 500

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))

    async def forward_message(self, chat_id: int, from_chat_id: int, message_id: int) -> Fwd:
        self._next_id += 1
        self.forwarded.append((chat_id, from_chat_id, message_id))
        return Fwd(self._next_id)

    async def copy_message(self, chat_id: int, from_chat_id: int, message_id: int) -> None:
        self.copied.append((chat_id, from_chat_id, message_id))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "db", DB(tmp_path / "t.sqlite3"))
    monkeypatch.setattr(bm, "ADMIN_IDS", {ADMIN_ID})

    async def fake_rates(rules):
        fb = rules["fallback_rates"]
        return {"CNY": fb["CNY"], "EUR": fb["EUR"], "USD": fb["USD"]}, "тестовый курс"

    monkeypatch.setattr(bm, "get_rates", fake_rates)
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=0, chat_id=1, user_id=1))
    return state, FakeBot()


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def texts(msg: Msg) -> str:
    return "\n".join(o["text"] for o in msg.outbox if o["type"] == "text")


def test_preset_flow_skips_specs_and_offers_routes(env):
    state, fbot = env

    async def flow():
        m = Msg(text=bm.BTN_CALC)
        await bm.calc_start(m, state)
        assert await state.get_state() == bm.Calc.mode.state
        await bm.calc_mode_list(Cb("mode:list", m), state)
        await bm.calc_model_pick(Cb("model:coolray", m), state)
        assert "Geely Coolray" in m.outbox[-1]["text"] and "147 л.с." in m.outbox[-1]["text"]
        await bm.calc_price(Msg(text="91000", outbox=m.outbox), state)
        await bm.calc_made(Msg(text="2025-03", outbox=m.outbox), state)
        assert await state.get_state() == bm.Calc.route.state  # объём/мощность/тип пропущены
        await bm.calc_route(Cb("route:suifenhe", m), state)
        await bm.calc_dest(Cb("dest:moscow", m), state, fbot)
        out = texts(m)
        assert out.count("Geely Coolray 1.5T") >= 2 and "ИТОГО" in out
        await bm.compare_routes(Cb("go:routes", m))
        return m

    m = run(flow())
    out = m.outbox[-1]["text"]
    assert "все маршруты" in out and "← ваш выбор" in out
    assert out.count("•") == 4


def test_three_year_advice_appears_near_boundary(env):
    state, fbot = env
    made = date.today() - timedelta(days=365 * 3 - 120)  # три года исполнится примерно через 120 дней

    async def flow():
        m = Msg(text=bm.BTN_CALC)
        await bm.calc_start(m, state)
        await bm.calc_mode_manual(Cb("mode:manual", m), state)
        await bm.calc_price(Msg(text="300000", outbox=m.outbox), state)  # дорогая машина: 48% от цены > ставка за см³
        await bm.calc_made(Msg(text=made.strftime("%Y-%m"), outbox=m.outbox), state)
        await bm.calc_cc(Msg(text="1998", outbox=m.outbox), state)
        await bm.calc_hp(Msg(text="150", outbox=m.outbox), state)
        await bm.calc_fuel(Cb("fuel:ice", m), state)
        await bm.calc_route(Cb("route:suifenhe", m), state)
        await bm.calc_dest(Cb("dest:moscow", m), state, fbot)
        return m

    out = texts(run(flow()))
    assert "исполнится 3 года" in out and "экономия" in out


def test_check_preferential_yes_and_no(env):
    state, fbot = env

    async def flow():
        m = Msg(text=bm.BTN_CHECK)
        await bm.check_start(m, state)
        await bm.check_fuel(Cb("fuel:ice", m), state)
        await bm.check_cc(Msg(text="1.5", outbox=m.outbox), state)
        await bm.check_hp(Msg(text="147", outbox=m.outbox), state)
        yes = m.outbox[-1]["text"]
        m2 = Msg(text=bm.BTN_CHECK)
        await bm.check_start(m2, state)
        await bm.check_fuel(Cb("fuel:ev", m2), state)
        assert await state.get_state() == bm.Check.hp.state  # объём у электромобиля не спрашиваем
        await bm.check_hp(Msg(text="204", outbox=m2.outbox), state)
        return yes, m2.outbox[-1]["text"]

    yes, no = run(flow())
    assert yes.startswith("✅") and "3 400 ₽" in yes
    assert no.startswith("❌") and "выше порога 80" in no and "2 193 600 ₽" in no


def test_guide_sections_and_explain(env):
    state, fbot = env

    async def flow():
        m = Msg(text=bm.BTN_GUIDE)
        await bm.cmd_guide(m, state)
        for key in bm.GUIDE:
            await bm.guide_section(Cb(f"guide:{key}", m))
        await bm.guide_section(Cb("guide:help", m))
        await bm.explain_lines(Cb("go:explain", m))
        return m

    out = texts(run(flow()))
    assert "по шагам" in out and "Документы, которые нужны" in out and "Типичные ошибки" in out
    assert "Что умеет бот" in out and "Что это за статьи расчёта" in out


def test_support_bridge_both_directions(env):
    state, fbot = env

    async def flow():
        m = Msg(text=bm.BTN_SUPPORT)
        await bm.support_start(m, state)
        q = Msg(text="Сколько ждать машину?", message_id=777, outbox=m.outbox)
        await bm.support_message(q, state, fbot)
        assert fbot.forwarded == [(ADMIN_ID, 1, 777)]
        fwd_id = fbot._next_id
        reply = Msg(text="Около месяца", from_user=User(id=ADMIN_ID), chat=Chat(id=ADMIN_ID), reply_to_message=Fwd(fwd_id))
        assert bm._is_support_reply(reply)
        await bm.support_reply(reply, fbot)
        stranger = Msg(text="x", from_user=User(id=9), reply_to_message=Fwd(fwd_id))
        assert not bm._is_support_reply(stranger)
        return m, reply

    m, reply = run(flow())
    assert "Передал" in texts(m)
    assert (1, "Ответ от нас:\n\nОколо месяца") in fbot.sent
    assert "Отправлено" in texts(reply)


def test_export_and_broadcast(env):
    state, fbot = env
    bm.db.touch_user(1, "a")
    bm.db.touch_user(2, "b")
    bm.db.add_lead(1, "a", "Zeekr", "4 млн", "Тула", "@a")

    async def flow():
        exp = Msg(text="/export", from_user=User(id=ADMIN_ID))
        await bm.admin_export(exp)
        assert exp.outbox[-1]["type"] == "doc"
        csv_text = exp.outbox[-1]["doc"].data.decode("utf-8")
        assert "Zeekr" in csv_text and "заявка" in csv_text

        bc = Msg(text="/broadcast Привет всем", from_user=User(id=ADMIN_ID))
        await bm.admin_broadcast(bc, state, CommandObject(prefix="/", command="broadcast", args="Привет всем"))
        assert "Получателей: 2" in bc.outbox[-1]["text"]
        await bm.admin_broadcast_confirm(Cb("bc:yes", bc, from_user=User(id=ADMIN_ID)), state, fbot)
        return bc

    bc = run(flow())
    assert "Разослано: 2" in texts(bc)
    assert (1, "Привет всем") in fbot.sent and (2, "Привет всем") in fbot.sent


def test_non_admin_cannot_export_or_broadcast(env):
    state, fbot = env

    async def flow():
        exp = Msg(text="/export", from_user=User(id=5))
        await bm.admin_export(exp)
        bc = Msg(text="/broadcast hi", from_user=User(id=5))
        await bm.admin_broadcast(bc, state, CommandObject(prefix="/", command="broadcast", args="hi"))
        return exp, bc

    exp, bc = run(flow())
    assert exp.outbox == [] and bc.outbox == []


def test_check_texts_have_no_double_dot_and_ev_limit_in_advice(env):
    state, fbot = env

    async def flow():
        m = Msg(text=bm.BTN_CHECK)
        await bm.check_start(m, state)
        await bm.check_fuel(Cb("fuel:ev", m), state)
        await bm.check_hp(Msg(text="120", outbox=m.outbox), state)
        return m.outbox[-1]["text"]

    out = run(flow())
    assert ".." not in out and "до 80 л.с." in out


def test_explain_has_titles(env):
    state, fbot = env
    m = Msg()
    run(bm.explain_lines(Cb("go:explain", m)))
    out = texts(m)
    assert "• Утилизационный сбор. " in out and "• СВХ. " in out


def test_fix_mojibake_repairs_cp1251_garbled_utf8_and_keeps_normal_text():
    good = "Мы свяжемся с вами для оплаты и пришлём отчёт в течение 24 часов."
    garbled = good.encode("utf-8").decode("cp1251", errors="replace")
    assert bm.fix_mojibake(garbled) == good
    assert bm.fix_mojibake(good) == good
    assert bm.fix_mojibake("Pay via SBP") == "Pay via SBP"
    assert bm.fix_mojibake("") == ""
