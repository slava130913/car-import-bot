"""ИИ-функции без реального API: подставной клиент и подставной ИИ в боте."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import bot.ai as ai_mod
import bot.main as bm
from bot.db import DB


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------- модуль ai ----------

class FakeMessages:
    def __init__(self, text: str = "", stop_reason: str = "end_turn", exc: Exception | None = None):
        self.text, self.stop_reason, self.exc = text, stop_reason, exc
        self.calls: list[dict] = []

    async def create(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return SimpleNamespace(stop_reason=self.stop_reason, content=[SimpleNamespace(type="text", text=self.text)])


def fake_client(**kw):
    msgs = FakeMessages(**kw)
    return SimpleNamespace(beta=SimpleNamespace(messages=msgs)), msgs


GOOD = {"is_car_listing": True, "model_name": "Geely Monjaro 2.0T", "price_cny": 145800, "year": 2023, "month": 5,
        "engine_cc": 1998, "power_hp": 238, "fuel": "ice", "mileage_km": 30000, "note": ""}


def test_parse_listing_sends_image_schema_and_fallback():
    client, msgs = fake_client(text=json.dumps(GOOD))
    a = ai_mod.AI(client=client)
    out = run(a.parse_listing(b"\x89PNGdata", "image/png"))
    assert out["price_cny"] == 145800.0 and out["power_hp"] == 238 and out["fuel"] == "ice"
    call = msgs.calls[0]
    assert call["model"] == ai_mod.AI_MODEL and call["fallbacks"] == "default" and ai_mod.FALLBACK_BETA in call["betas"]
    assert call["output_config"]["format"]["type"] == "json_schema"
    img = call["messages"][0]["content"][0]
    assert img["type"] == "image" and img["source"]["media_type"] == "image/png"


def test_refusal_and_network_errors_become_aierror():
    client, _ = fake_client(text="{}", stop_reason="refusal")
    with pytest.raises(ai_mod.AIError):
        run(ai_mod.AI(client=client).parse_listing(b"x"))
    client, _ = fake_client(exc=RuntimeError("timeout"))
    with pytest.raises(ai_mod.AIError):
        run(ai_mod.AI(client=client).answer("вопрос", "факты"))
    client, _ = fake_client(text="не json")
    with pytest.raises(ai_mod.AIError):
        run(ai_mod.AI(client=client).parse_listing(b"x"))


def test_disabled_without_key():
    a = ai_mod.AI(api_key="")
    assert not a.enabled
    with pytest.raises(ai_mod.AIError):
        run(a.answer("q", "f"))


def test_clean_listing_drops_implausible_values():
    bad = dict(GOOD, price_cny=12, year=1800, month=13, engine_cc=99999, power_hp=0, fuel="diesel", model_name="  ")
    out = ai_mod.clean_listing(bad)
    assert out["price_cny"] is None and out["year"] is None and out["month"] is None
    assert out["engine_cc"] is None and out["power_hp"] is None and out["fuel"] is None and out["model_name"] is None
    ev = ai_mod.clean_listing(dict(GOOD, fuel="ev", engine_cc=None))
    assert ev["engine_cc"] == 0


def test_daily_limit():
    a = ai_mod.AI(client=object(), daily_limit=2)
    assert a.allow(1) and a.allow(1) and not a.allow(1)
    assert a.allow(2)


def test_facts_grounded_in_rules():
    facts = ai_mod.build_facts(bm.RULES, bm.GUIDE, bm.EXPLAIN, bm.EXPLAIN_TITLES)
    assert "3400" in facts and "160 л.с." in facts and "Суйфэньхэ" in facts


# ---------- бот ----------

@dataclass
class User:
    id: int = 1
    username: str | None = "tester"


@dataclass
class Msg:
    text: str | None = None
    photo: list | None = None
    document: Any = None
    from_user: User = field(default_factory=User)
    outbox: list[dict[str, Any]] = field(default_factory=list)

    async def answer(self, text: str, reply_markup: Any = None) -> None:
        self.outbox.append({"text": text, "kb": reply_markup})


class FakeBot:
    async def download(self, file_id, destination):
        destination.write(b"jpegbytes")


class FakeAI:
    def __init__(self, enabled=True, info=None, reply="Ответ", exc=None):
        self.enabled, self.info, self.reply, self.exc = enabled, info, reply, exc
        self.model = "fake"

    def allow(self, uid):
        return True

    async def parse_listing(self, data, media):
        assert data == b"jpegbytes" and media == "image/jpeg"
        if self.exc:
            raise self.exc
        return self.info

    async def answer(self, q, facts):
        return self.reply


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "db", DB(tmp_path / "t.sqlite3"))
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=0, chat_id=1, user_id=1))
    return state


def texts(m: Msg) -> str:
    return "\n".join(o["text"] for o in m.outbox)


def photo_msg():
    return Msg(photo=[SimpleNamespace(file_id="small"), SimpleNamespace(file_id="big")])


def test_screenshot_full_info_goes_straight_to_route(env, monkeypatch):
    monkeypatch.setattr(bm, "ai", FakeAI(info=ai_mod.clean_listing(GOOD)))
    m = photo_msg()
    run(bm.calc_from_screenshot(m, env, FakeBot()))
    out = texts(m)
    assert "Geely Monjaro 2.0T" in out and "145 800 ¥" in out and "238 л.с." in out
    assert run(env.get_state()) == bm.Calc.route.state
    d = run(env.get_data())
    assert d["price_cny"] == 145800 and d["age"] in ("new", "3-5") and d["ai_model"] == "Geely Monjaro 2.0T"


def test_screenshot_missing_year_and_power_asks_for_them(env, monkeypatch):
    info = ai_mod.clean_listing(dict(GOOD, year=None, month=None, power_hp=None))
    monkeypatch.setattr(bm, "ai", FakeAI(info=info))
    m = photo_msg()
    run(bm.calc_from_screenshot(m, env, FakeBot()))
    assert run(env.get_state()) == bm.Calc.made.state
    run(bm.calc_made(Msg(text="2024-02", outbox=m.outbox), env))
    assert run(env.get_state()) == bm.Calc.hp.state  # объём уже известен, спрашиваем мощность
    run(bm.calc_hp(Msg(text="150", outbox=m.outbox), env))
    assert run(env.get_state()) == bm.Calc.route.state  # тип двигателя тоже известен


def test_screenshot_when_ai_off_or_fails(env, monkeypatch):
    monkeypatch.setattr(bm, "ai", FakeAI(enabled=False))
    m = photo_msg()
    run(bm.calc_from_screenshot(m, env, FakeBot()))
    assert "скоро включим" in texts(m) and run(env.get_state()) == bm.Calc.price.state

    monkeypatch.setattr(bm, "ai", FakeAI(exc=ai_mod.AIError("boom")))
    m2 = photo_msg()
    run(bm.calc_from_screenshot(m2, env, FakeBot()))
    assert "Не получилось прочитать" in texts(m2) and run(env.get_state()) == bm.Calc.price.state

    monkeypatch.setattr(bm, "ai", FakeAI(info=ai_mod.clean_listing(dict(GOOD, is_car_listing=False))))
    m3 = photo_msg()
    run(bm.calc_from_screenshot(m3, env, FakeBot()))
    assert "не похоже на объявление" in texts(m3)


def test_free_question(env, monkeypatch):
    monkeypatch.setattr(bm, "ai", FakeAI(reply="Льгота действует для одной машины в год."))
    m = Msg(text="Можно ли ввезти вторую машину в этом году?")
    run(bm.free_question(m))
    assert "Льгота действует" in texts(m) and "автоматически" in texts(m)

    monkeypatch.setattr(bm, "ai", FakeAI(enabled=False))
    m2 = Msg(text="Можно ли ввезти вторую машину в этом году?")
    run(bm.free_question(m2))
    assert "Написать нам" in texts(m2)


def test_photo_handler_not_in_support_state():
    """Фото в режиме «Написать нам» должно уйти человеку, а не в распознавание."""
    from aiogram.filters import StateFilter

    handler = next(h for h in bm.router.message.handlers if h.callback is bm.calc_from_screenshot)
    sf = next(f.callback for f in handler.filters if isinstance(f.callback, StateFilter))
    assert bm.Support.msg.state not in [getattr(s, "state", s) for s in sf.states]
