"""Ответы без ИИ: темы по ключевым словам, модели из вопроса, встраивание в бота."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup

import bot.main as bm
from bot import ai as ai_mod
from bot import faq
from bot.db import DB


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize(
    "text, topic",
    [
        ("Сколько стоит растаможка?", "duty"),
        ("Машине почти 3 года, ждать или нет?", "duty"),
        ("Пройдёт ли по льготе 150 л.с.?", "util"),
        ("какой утильсбор на 200 лошадей", "util"),
        ("Как пригнать машину из Китая?", "steps"),
        ("Сколько ехать через Хоргос?", "time"),
        ("Электромобиль выгодно везти?", "ev"),
        ("Какие документы нужны для ЭПТС?", "docs"),
        ("Как не нарваться на мошенников", "mistakes"),
        ("Как оплатить машину в юанях", "payment"),
        ("Нужен ли брокер и сколько стоит СВХ", "broker"),
        ("Проверить пробег по VIN", "vin"),
        ("Помогите подобрать машину", "lead"),
        ("Сколько будет стоить под ключ", "calc"),
    ],
)
def test_topics(text, topic):
    assert faq.find_topic(text) == topic


def test_unknown_question():
    assert faq.find_topic("Можно ли ввезти вторую машину в этом году?") is None
    assert faq.answer("Привет, как дела?", bm.MODELS_BY_ID, bm.RULES["util"]["preferential"], bm.FUEL_LABELS) is None


@pytest.mark.parametrize(
    "text, mid",
    [
        ("Монжаро сколько будет?", "monjaro"),
        ("Haval H6 2023", "h6"),
        ("tiggo-7 pro или jolion", "tiggo7"),
        ("Honda CR-V из Китая", "crv"),
        ("x-trail гибрид", "xtrail"),
        ("Тесла модель у", "model_y"),
        ("Li L7 цена", "li_l7"),
    ],
)
def test_models(text, mid):
    assert faq.find_model(text) == mid


def test_no_false_model():
    assert faq.find_model("Цена 1001 тысяча юаней") is None
    assert faq.find_model("Сколько лошадиных сил разрешено") is None


def test_every_model_has_aliases():
    assert set(faq.MODEL_ALIASES) == set(bm.MODELS_BY_ID)


def _answer(text):
    return faq.answer(text, bm.MODELS_BY_ID, bm.RULES["util"]["preferential"], bm.FUEL_LABELS)


def test_model_with_price_question():
    a = _answer("сколько будет монжаро под ключ")
    assert a.model_id == "monjaro" and a.buttons[0] == ("🧮 Посчитать Geely Monjaro 2.0T", "faq:model:monjaro")
    assert "выше порога 160" in a.text
    assert faq.BTN_CALC not in a.buttons  # кнопка модели уже ведёт в расчёт
    assert "Расчёт под ключ" not in a.text


def test_model_verdicts():
    assert "проходит под льготный" in _answer("coolray").text
    ev = _answer("BYD Seal пройдёт по льготе?")
    assert "30-минутная мощность" in ev.text and ev.topic == "util"


def test_callback_data_fits_telegram_limit():
    for mid in bm.MODELS_BY_ID:
        a = _answer(faq.MODEL_ALIASES[mid][0])
        assert a.model_id == mid
        for _text, data in a.buttons:
            assert len(data.encode()) <= 64


# ---------- в боте ----------

@dataclass
class User:
    id: int = 1
    username: str | None = "tester"


@dataclass
class Msg:
    text: str | None = None
    from_user: User = field(default_factory=User)
    outbox: list[dict[str, Any]] = field(default_factory=list)

    async def answer(self, text: str, reply_markup: Any = None) -> None:
        self.outbox.append({"text": text, "kb": reply_markup})


@dataclass
class Cb:
    data: str
    message: Msg
    from_user: User = field(default_factory=User)

    async def answer(self) -> None:
        pass


class FakeAI:
    def __init__(self, enabled=True, allow=True, exc=None, reply="Ответ ИИ"):
        self.enabled, self._allow, self.exc, self.reply = enabled, allow, exc, reply
        self.model = "fake"

    def allow(self, uid):
        return self._allow

    async def answer(self, q, facts):
        if self.exc:
            raise self.exc
        return self.reply


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "db", DB(tmp_path / "t.sqlite3"))
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=0, chat_id=1, user_id=1))


def test_bot_answers_without_ai(state, monkeypatch):
    monkeypatch.setattr(bm, "ai", FakeAI(enabled=False))
    m = Msg(text="Сколько стоит растаможка?")
    run(bm.free_question(m))
    assert "Таможенная пошлина" in m.outbox[0]["text"]
    assert isinstance(m.outbox[0]["kb"], InlineKeyboardMarkup)


def test_bot_falls_back_when_ai_fails_or_limit(state, monkeypatch):
    monkeypatch.setattr(bm, "ai", FakeAI(exc=ai_mod.AIError("boom")))
    m = Msg(text="Какие документы нужны?")
    run(bm.free_question(m))
    assert "Документы, которые нужны" in m.outbox[0]["text"]

    monkeypatch.setattr(bm, "ai", FakeAI(allow=False))
    m2 = Msg(text="Можно ли ввезти вторую машину в этом году?")
    run(bm.free_question(m2))
    assert "лимит" in m2.outbox[0]["text"]

    m3 = Msg(text="Сколько ехать морем?")
    run(bm.free_question(m3))
    assert "Сроки и логистика" in m3.outbox[0]["text"]


def test_ai_still_first_when_enabled(state, monkeypatch):
    monkeypatch.setattr(bm, "ai", FakeAI(reply="Ответ по правилам"))
    m = Msg(text="Сколько стоит растаможка?")
    run(bm.free_question(m))
    assert "Ответ по правилам" in m.outbox[0]["text"]


def test_model_button_starts_calc(state):
    msg = Msg()
    run(bm.faq_model_calc(Cb(data="faq:model:coolray", message=msg), state))
    d = run(state.get_data())
    assert d["preset"] == "coolray" and d["power_hp"] == 147
    assert run(state.get_state()) == bm.Calc.price.state
    assert "Geely Coolray" in msg.outbox[0]["text"]


def test_check_button(state):
    msg = Msg()
    run(bm.check_start_cb(Cb(data="go:check", message=msg), state))
    assert run(state.get_state()) == bm.Check.fuel.state
