"""Telegram-бот: калькулятор «под ключ», проверка льготы, гид, заказ отчёта по VIN, заявка на подбор, связь с нами.

Запуск: python -m bot.main  (переменные окружения см. в .env.example)
"""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
)

from calc.engine import AGE_LABELS, FUEL_LABELS, CarInput, age_category_from_date, calculate, format_rub, load_rules, render_text, util_fee
from calc.rates import get_rates

from . import faq
from . import tenants as tn
from .site_leads import fetch_leads, site_token
from .ai import AI, AIError, build_facts
from .db import DB, LEAD_STATUSES, MAIN
from .texts import EXPLAIN, EXPLAIN_TITLES, GUIDE, GUIDE_TITLES

try:  # .env необязателен
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

log = logging.getLogger("bot")

ROOT = Path(__file__).resolve().parent.parent
def fix_mojibake(s: str) -> str:
    """Чинит UTF-8, прочитанный как cp1251 (так портит кириллицу запись .env без кодировки на Windows)."""
    if not s or not re.search(r"[РС][Ѐ-ӿ‘-›\u0080-ÿ]", s):
        return s
    try:
        fixed = s.encode("cp1251").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s
    return fixed if re.search(r"[а-яё]", fixed, re.I) else s


BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}
VIN_PRICE_STARS = int(os.environ.get("VIN_PRICE_STARS", "0"))
PAYMENT_INSTRUCTIONS = fix_mojibake(os.environ.get(
    "PAYMENT_INSTRUCTIONS",
    "Мы свяжемся с вами для оплаты и пришлём отчёт в течение 24 часов.",
))
DB_PATH = os.environ.get("DB_PATH", str(ROOT / "data" / "bot.sqlite3"))
WEB_URL = os.environ.get("WEB_URL", "")
# Брендирование под клиента (white label): название компании и прямой контакт менеджера
BRAND_NAME = fix_mojibake(os.environ.get("BRAND_NAME", "").strip())
MANAGER_CONTACT = fix_mojibake(os.environ.get("MANAGER_CONTACT", "").strip())

TRIAL_DAYS = int(os.environ.get("TRIAL_DAYS", "7"))
# Заявки с формы на сайте (хостинг reg.ru): бот забирает их отсюда раз в минуту. Пустое значение выключает.
SITE_LEADS_URL = os.environ.get("SITE_LEADS_URL", "https://car.myapphub.tech/api/leads.php").strip()
SITE_LEADS_INTERVAL = 60
# Контакт платформы для клиентов: куда писать об оплате и вопросах по их боту
# Режим платформы: самостоятельный запуск ботов клиентов и команды /tenants. В отдельном экземпляре для клиента
# (BRAND_NAME задан) по умолчанию выключен, чтобы покупатели клиента не видели «Запустить свой бот».
PLATFORM_MODE = os.environ.get("PLATFORM_MODE", "0" if os.environ.get("BRAND_NAME", "").strip() else "1") == "1"
PLATFORM_CONTACT = os.environ.get("PLATFORM_CONTACT", "").strip() or (
    f"t.me/{os.environ.get('BOT_USERNAME', '').lstrip('@')}?start=partner" if os.environ.get("BOT_USERNAME") else "")

RULES = load_rules()
with open(ROOT / "data" / "models.json", encoding="utf-8") as _f:
    MODELS: list[dict] = json.load(_f)["models"]
MODELS_BY_ID = {m["id"]: m for m in MODELS}
db = DB(DB_PATH)
ai = AI()
AI_FACTS = build_facts(RULES, GUIDE, EXPLAIN, EXPLAIN_TITLES)
router = Router()


def _main_tenant() -> tn.Tenant:
    """Основной бот как арендатор: настройки из .env (тесты подменяют глобальные переменные модуля)."""
    return tn.Tenant(
        id=MAIN, brand=BRAND_NAME, manager=MANAGER_CONTACT, admin_ids=frozenset(ADMIN_IDS), web_url=WEB_URL,
        vin_price=VIN_PRICE_STARS, payment_text=PAYMENT_INSTRUCTIONS, is_main=True,
    )


tn.set_main_factory(_main_tenant)
T = tn.current
RUNNER: tn.Runner | None = None

BTN_CALC = "🧮 Рассчитать под ключ"
BTN_VIN = "🔎 Проверить VIN"
BTN_LEAD = "🚗 Хочу пригнать"
BTN_CHECK = "✅ Проверить льготу"
BTN_GUIDE = "📖 Гид по перегону"
BTN_SUPPORT = "✍️ Написать нам"
BTN_HELP = "ℹ️ Как это работает"

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_CALC), KeyboardButton(text=BTN_VIN)],
        [KeyboardButton(text=BTN_LEAD), KeyboardButton(text=BTN_CHECK)],
        [KeyboardButton(text=BTN_GUIDE), KeyboardButton(text=BTN_SUPPORT)],
    ],
    resize_keyboard=True,
)
CANCEL_KB = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отмена")]], resize_keyboard=True)

VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
ROUTE_NAMES = {k: v["label"] for k, v in RULES["costs"]["logistics"].items()}


def _ikb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


FUEL_KB = _ikb([
    [("Бензин / дизель", "fuel:ice")],
    [("Параллельный гибрид (PHEV, DM-i)", "fuel:hybrid_par")],
    [("Последовательный гибрид (EREV)", "fuel:hybrid_seq")],
    [("Электромобиль", "fuel:ev")],
])
ROUTE_KB = _ikb([
    [("Суйфэньхэ → Уссурийск", "route:suifenhe")],
    [("Маньчжурия → Забайкальск", "route:zabaikalsk")],
    [("Хоргос → Казахстан", "route:khorgos")],
    [("Морем до Владивостока", "route:sea")],
])
DEST_KB = _ikb([
    [("Москва", "dest:moscow"), ("Тула", "dest:tula")],
    [("Новосибирск", "dest:novosibirsk"), ("Хабаровск", "dest:khabarovsk")],
    [("Оставить во Владивостоке", "dest:far_east")],
])
START_MODE_KB = _ikb([
    [("📋 Выбрать модель из списка", "mode:list")],
    [("✍️ Ввести характеристики вручную", "mode:manual")],
])
AFTER_CALC_KB = _ikb([
    [("🚚 Сравнить маршруты", "go:routes"), ("ℹ️ Что это за статьи", "go:explain")],
    [("🔎 Проверить эту машину по VIN", "go:vin")],
    [("🚗 Оставить заявку на подбор", "go:lead")],
    [("🔁 Пересчитать", "go:calc")],
])
GUIDE_KB = _ikb([[(GUIDE_TITLES[k], f"guide:{k}")] for k in GUIDE] + [[("ℹ️ Что умеет бот", "guide:help")]])


def models_kb() -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    row: list[tuple[str, str]] = []
    for m in MODELS:
        row.append((m["name"], f"model:{m['id']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([("✍️ Моей модели нет, введу вручную", "mode:manual")])
    return _ikb(rows)


class Calc(StatesGroup):
    mode = State()
    model = State()
    price = State()
    made = State()
    cc = State()
    hp = State()
    fuel = State()
    route = State()
    dest = State()


class Check(StatesGroup):
    fuel = State()
    hp = State()
    cc = State()


class Vin(StatesGroup):
    vin = State()
    contact = State()


class Lead(StatesGroup):
    model = State()
    budget = State()
    city = State()
    timeline = State()
    contact = State()


class WL(StatesGroup):
    """Самостоятельный запуск бота клиента (white label)."""
    token = State()
    brand = State()
    manager = State()
    fee = State()
    confirm = State()


TIMELINES = {"now": "🔥 В этом месяце", "soon": "В ближайшие 3 месяца", "later": "Пока присматриваюсь"}
TIMELINE_KB = _ikb([[(v, f"lt:{k}")] for k, v in TIMELINES.items()])


def lead_status_kb(lead_id: int) -> InlineKeyboardMarkup:
    return _ikb([[("🟡 В работе", f"ls:work:{lead_id}"), ("✅ Сделка", f"ls:won:{lead_id}"), ("❌ Отказ", f"ls:lost:{lead_id}")]])


class Support(StatesGroup):
    msg = State()


class Broadcast(StatesGroup):
    confirm = State()


async def notify_admins(bot: Bot, text: str, reply_markup: InlineKeyboardMarkup | None = None,
                        admins: frozenset[int] | set[int] | None = None) -> int:
    """Пишет админам текущего бота (основного или бота клиента). Возвращает число доставленных."""
    sent = 0
    for admin in (T().admin_ids if admins is None else admins):
        try:
            if reply_markup is None:
                await bot.send_message(admin, text)
            else:
                await bot.send_message(admin, text, reply_markup=reply_markup)
            sent += 1
        except Exception as e:  # noqa: BLE001
            log.warning("admin notify failed for %s: %s", admin, e)
    return sent


def _is_admin(m: Message | CallbackQuery) -> bool:
    return m.from_user is not None and m.from_user.id in T().admin_ids


def _platform() -> bool:
    """Функции платформы работают только в основном боте и только в режиме платформы."""
    return PLATFORM_MODE and T().is_main


def _is_platform_admin(m: Message | CallbackQuery) -> bool:
    """Владелец платформы: админ основного бота, команды доступны только в основном боте."""
    return _platform() and m.from_user is not None and m.from_user.id in ADMIN_IDS


def _parse_number(text: str) -> float | None:
    t = text.replace(" ", "").replace(",", ".").replace("¥", "").replace("k", "000").replace("к", "000")
    try:
        v = float(t)
    except ValueError:
        return None
    return v if v > 0 else None


def _parse_cc(text: str) -> int | None:
    t = (text or "").replace(" ", "").replace(",", ".")
    try:
        v = float(t)
    except ValueError:
        return None
    cc = int(round(v * 1000)) if v < 20 else int(round(v))
    return cc if 0 <= cc <= 8000 else None


# Вход /start partner: сюда ведёт ссылка из писем партнёрам, поэтому здесь все условия.
PARTNER_TEXT = (
    "Партнёрство: заявки на машины из Китая\n\n"
    "Бот считает машину из Китая под ключ и собирает заявки на подбор: модель, бюджет, город и контакт. "
    "Мы передаём их компаниям, которые возят машины.\n\n"
    "Форматы:\n"
    "1. Заявки за долю. Первая заявка на пробу без оплаты, дальше 15% от вашей комиссии и только с закрытых сделок. "
    "Абонентской платы нет. Каждая заявка уходит одному партнёру.\n"
    "2. Свой бот под вашим брендом: ваше название, контакт и комиссия в расчёте, все заявки только вам, "
    "со статусами и отчётом за месяц. Запускается за 5 минут прямо здесь, кнопка ниже. "
    "Первая неделя без оплаты, дальше 15 000 ₽ за подключение и 3 000 ₽ в месяц.\n"
    "3. Для блогеров: бот под брендом канала, заявки уходят проверенным импортёрам, вознаграждение за закрытые сделки делим с вами пополам.\n\n"
    "Как бот считает, можно проверить прямо сейчас: /calc.\n\n"
    "Чтобы подключиться, напишите одним сообщением: компания или канал, город, сколько машин в месяц возите "
    "и как с вами связаться. Ответим в течение дня."
)

def help_text() -> str:
    t = T()
    lead = ("передаём проверенному агенту, он связывается с вами" if t.is_main
            else f"менеджер {t.brand} свяжется с вами")
    vin = ("китайский отчёт (пробег, ДТП, сервисная история) с переводом на русский за 24 часа" if t.is_main
           else "история машины из китайских баз, стоимость и сроки уточнит менеджер")
    return (
        "Что умеет бот:\n\n"
        "1. Калькулятор «под ключ»: цена в Китае, перевод денег, расходы в Китае, логистика, пошлина, "
        "утильсбор, СБКТС, ЭПТС и доставка по России. Можно выбрать модель из списка, сравнить маршруты, "
        "узнать, что изменится, когда машине исполнится 3 года.\n\n"
        "2. Проверка льготы: за 10 секунд понять, попадает ли машина под льготный утильсбор.\n\n"
        "3. Гид по перегону: шаги, документы, сроки, типичные ошибки.\n\n"
        f"4. Проверка по VIN: {vin}.\n\n"
        f"5. Заявка на подбор: {lead}.\n\n"
        "6. Написать нам: вопрос уходит человеку, ответ придёт сюда же.\n\n"
        "Можно задать вопрос своими словами: про утильсбор, пошлину, сроки, документы или конкретную модель."
        + (
            " Или прислать скриншот объявления из Китая: бот прочитает модель, цену, год и мощность и посчитает под ключ."
            if ai.enabled else ""
        )
    )


PARTNER_KB = _ikb([[("🚀 Запустить свой бот за 5 минут", "wl:start")], [("🧮 Посмотреть, как бот считает", "go:calc")]])


# ---------- старт и меню ----------

@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext, command: CommandObject) -> None:
    await state.clear()
    db.touch_user(m.from_user.id, m.from_user.username, T().id)
    if command.args == "vin":
        await vin_start(m, state)
        return
    if command.args == "lead":
        await lead_start(m, state)
        return
    if command.args == "calc":
        await calc_start(m, state)
        return
    if command.args == "partner" and _platform():
        await state.set_state(Support.msg)
        await state.update_data(kind="partner")
        await m.answer(PARTNER_TEXT, reply_markup=CANCEL_KB)
        await m.answer("Можно не ждать ответа и запустить свой бот прямо сейчас: пробная неделя без оплаты.", reply_markup=PARTNER_KB)
        return
    t = T()
    text = (
        (f"Привет! Это бот {t.brand}. Я помогаю посчитать" if t.brand else "Привет! Я помогаю посчитать")
        + ", сколько реально стоит пригнать машину из Китая, "
        "проверить, пройдёт ли она по льготному утильсбору, и проверить её историю по VIN.\n\n"
        f"Правила расчёта актуальны на {RULES['version']}.\n"
        "Выберите действие:"
    )
    if t.web_url:
        text += f"\n\nВеб-версия калькулятора: {t.web_url}"
    if not t.is_main and m.from_user.id in t.admin_ids:
        text += "\n\nВы администратор этого бота: заявки приходят сюда. Команды: /leads, /report, /settings, /stats, /export."
    await m.answer(text, reply_markup=MAIN_KB)


@router.message(F.text == "Отмена")
@router.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext) -> None:
    await state.clear()
    await m.answer("Ок, отменил.", reply_markup=MAIN_KB)


@router.message(F.text == BTN_HELP)
@router.message(Command("help"))
async def cmd_help(m: Message) -> None:
    pref = RULES["util"]["preferential"]
    await m.answer(
        help_text() + (f"\n\nМенеджер: {T().manager}" if T().manager else "") +
        "\n\nЛьготный утильсбор для физлица действует, если:\n• " + "\n• ".join(pref["conditions"]) +
        f"\n\n{RULES['disclaimer']}",
        reply_markup=MAIN_KB,
    )


# ---------- гид ----------

@router.message(F.text == BTN_GUIDE)
@router.message(Command("guide"))
async def cmd_guide(m: Message, state: FSMContext) -> None:
    db.touch_user(m.from_user.id, m.from_user.username, T().id)
    await state.clear()
    await m.answer("Гид по перегону машины из Китая. Что показать?", reply_markup=GUIDE_KB)


@router.callback_query(F.data.startswith("guide:"))
async def guide_section(c: CallbackQuery) -> None:
    await c.answer()
    key = c.data.split(":")[1]
    text = help_text() if key == "help" else GUIDE.get(key, "Раздел не найден.")
    await c.message.answer(text, reply_markup=GUIDE_KB)


# ---------- калькулятор ----------

@router.message(F.text == BTN_CALC)
@router.message(Command("calc"))
async def calc_start(m: Message, state: FSMContext) -> None:
    db.touch_user(m.from_user.id, m.from_user.username, T().id)
    await state.clear()
    await state.set_state(Calc.mode)
    await m.answer("Как считаем?", reply_markup=START_MODE_KB)


@router.callback_query(F.data == "go:calc")
async def calc_restart(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    await state.clear()
    await state.set_state(Calc.mode)
    await c.message.answer("Как считаем?", reply_markup=START_MODE_KB)


@router.callback_query(F.data == "mode:list")
async def calc_mode_list(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    await state.set_state(Calc.model)
    await c.message.answer(
        "Популярные модели. Характеристики типовые, мощность сверьте с документами:",
        reply_markup=models_kb(),
    )


async def _next_step(m: Message, state: FSMContext, prefix: str = "") -> None:
    """Спрашивает первое недостающее поле расчёта. Порядок: цена, дата выпуска, объём, мощность, тип, маршрут.

    Так одинаково работают ручной ввод, модель из списка и скриншот объявления: заполненное пропускается.
    """
    d = await state.get_data()
    if d.get("price_cny") is None:
        await state.set_state(Calc.price)
        await m.answer(prefix + "Цена машины в Китае в юанях, например 95000:", reply_markup=CANCEL_KB)
    elif not d.get("made"):
        await state.set_state(Calc.made)
        await m.answer(prefix + "Год и месяц выпуска, например 2024-03 (можно только год):", reply_markup=CANCEL_KB)
    elif d.get("engine_cc") is None:
        await state.set_state(Calc.cc)
        await m.answer(prefix + "Объём двигателя в см³ (для электромобиля 0):", reply_markup=CANCEL_KB)
    elif d.get("power_hp") is None:
        await state.set_state(Calc.hp)
        await m.answer(prefix + "Мощность в л.с. (для гибрида и электромобиля смотрите подсказку после выбора типа):", reply_markup=CANCEL_KB)
    elif not d.get("fuel"):
        await state.set_state(Calc.fuel)
        await m.answer(prefix + "Тип двигателя:", reply_markup=FUEL_KB)
    else:
        await state.set_state(Calc.route)
        await m.answer(prefix + "Маршрут доставки:", reply_markup=ROUTE_KB)


@router.callback_query(F.data == "mode:manual")
async def calc_mode_manual(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    await state.set_data({"preset": None})
    await _next_step(c.message, state)


@router.callback_query(Calc.model, F.data.startswith("model:"))
async def calc_model_pick(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    mdl = MODELS_BY_ID.get(c.data.split(":")[1])
    if not mdl:
        await state.set_data({"preset": None})
        await _next_step(c.message, state, "Модель не найдена, введите характеристики вручную.\n\n")
        return
    await state.update_data(preset=mdl["id"], engine_cc=mdl["cc"], power_hp=mdl["hp"], fuel=mdl["fuel"])
    note = f"\n⚠️ {mdl['note']}." if mdl.get("note") else ""
    await _next_step(c.message, state, f"{mdl['name']}: {mdl['cc']} см³, {mdl['hp']} л.с., {FUEL_LABELS[mdl['fuel']]}.{note}\n\n")


@router.message(Calc.price)
async def calc_price(m: Message, state: FSMContext) -> None:
    v = _parse_number(m.text or "")
    if v is None or v > 10_000_000:
        await m.answer("Нужно число в юанях, например 95000.")
        return
    await state.update_data(price_cny=v)
    await _next_step(m, state)


def _made_fields(year: int, month: int | None) -> tuple[dict, str]:
    made = date(year, month or 7, 1)
    age = age_category_from_date(made)
    note = "" if month else " (месяц не указан, взял середину года, на границе 3 и 5 лет это важно)"
    return {"age": age, "made": made.isoformat(), "made_exact": bool(month)}, f"Возраст: {AGE_LABELS[age]}{note}.\n"


@router.message(Calc.made)
async def calc_made(m: Message, state: FSMContext) -> None:
    t = (m.text or "").strip()
    mt = re.match(r"^(\d{4})(?:[-./ ](\d{1,2}))?$", t)
    if not mt:
        await m.answer("Формат: 2024-03 или просто 2024.")
        return
    year = int(mt.group(1))
    month = int(mt.group(2)) if mt.group(2) else None
    if not (1990 <= year <= date.today().year) or not (month is None or 1 <= month <= 12):
        await m.answer("Проверьте год и месяц.")
        return
    fields, prefix = _made_fields(year, month)
    await state.update_data(**fields)
    await _next_step(m, state, prefix)


@router.message(Calc.cc)
async def calc_cc(m: Message, state: FSMContext) -> None:
    cc = _parse_cc(m.text or "")
    if cc is None:
        await m.answer("Нужно число, например 1498. Если указано в литрах, напишите 1.5. Для электромобиля 0.")
        return
    await state.update_data(engine_cc=cc)
    await _next_step(m, state)


@router.message(Calc.hp)
async def calc_hp(m: Message, state: FSMContext) -> None:
    v = _parse_number(m.text or "")
    if v is None or v > 2000:
        await m.answer("Нужно число, например 147.")
        return
    await state.update_data(power_hp=int(round(v)))
    await _next_step(m, state)


@router.callback_query(Calc.fuel, F.data.startswith("fuel:"))
async def calc_fuel(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    await state.update_data(fuel=c.data.split(":")[1])
    await c.message.edit_reply_markup(reply_markup=None)
    await _next_step(c.message, state)


# ---------- скриншот объявления (ИИ) ----------

AI_OFF_TEXT = (
    "Распознавание скриншотов объявлений скоро включим. "
    "Пока посчитаем по характеристикам: это займёт минуту."
)


async def _download_image(m: Message, bot: Bot) -> tuple[bytes, str] | None:
    if m.photo:
        file_id, media = m.photo[-1].file_id, "image/jpeg"
    elif m.document and (m.document.mime_type or "").startswith("image/"):
        file_id, media = m.document.file_id, m.document.mime_type
    else:
        return None
    buf = io.BytesIO()
    await bot.download(file_id, destination=buf)
    return buf.getvalue(), media


@router.message(StateFilter(None, Calc.mode, Calc.model, Calc.price), F.photo | F.document.mime_type.startswith("image/"))
async def calc_from_screenshot(m: Message, state: FSMContext, bot: Bot) -> None:
    db.touch_user(m.from_user.id, m.from_user.username, T().id)
    await state.set_data({"preset": None})
    if not ai.enabled:
        await _next_step(m, state, AI_OFF_TEXT + "\n\n")
        return
    if not ai.allow(m.from_user.id):
        await _next_step(m, state, "На сегодня лимит распознаваний исчерпан, посчитаем вручную.\n\n")
        return
    await m.answer("Читаю объявление, это займёт до 30 секунд…")
    try:
        img = await _download_image(m, bot)
        if img is None:
            raise AIError("не изображение")
        info = await ai.parse_listing(*img)
    except AIError:
        await _next_step(m, state, "Не получилось прочитать скриншот. Посчитаем по характеристикам.\n\n")
        return
    if not info["is_car_listing"]:
        await _next_step(m, state, "На картинке не похоже на объявление о машине. Посчитаем по характеристикам.\n\n")
        return
    upd: dict = {"preset": None, "ai_model": info["model_name"]}
    for k in ("price_cny", "engine_cc", "power_hp", "fuel"):
        if info[k] is not None:
            upd[k] = info[k]
    found = []
    if info["model_name"]:
        found.append(info["model_name"])
    if info["price_cny"] is not None:
        found.append(f"{info['price_cny']:,.0f} ¥".replace(",", " "))
    age_prefix = ""
    if info["year"]:
        fields, age_prefix = _made_fields(info["year"], info["month"])
        upd.update(fields)
        found.append(f"{info['year']}" + (f"-{info['month']:02d}" if info["month"] else ""))
    if info["engine_cc"] is not None:
        found.append(f"{info['engine_cc']} см³")
    if info["power_hp"] is not None:
        found.append(f"{info['power_hp']} л.с.")
    if info["fuel"]:
        found.append(FUEL_LABELS[info["fuel"]])
    if info["mileage_km"]:
        found.append(f"пробег {info['mileage_km']:,} км".replace(",", " "))
    await state.set_data(upd)
    head = "Нашёл в объявлении: " + (", ".join(found) if found else "почти ничего не удалось прочитать") + "."
    if info["note"]:
        head += f"\n⚠️ {info['note']}"
    await m.answer(head + "\nЕсли что-то неверно, нажмите «Ввести вручную».", reply_markup=_ikb([[("✍️ Ввести вручную", "mode:manual")]]))
    await _next_step(m, state, age_prefix)


@router.callback_query(Calc.route, F.data.startswith("route:"))
async def calc_route(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    await state.update_data(route=c.data.split(":")[1])
    await state.set_state(Calc.dest)
    await c.message.edit_reply_markup(reply_markup=None)
    await c.message.answer("Куда везём:", reply_markup=DEST_KB)


def _three_year_advice(data: dict, car: CarInput, res_total: float | None) -> str:
    """Подсказка: что изменится, когда машине исполнится 3 года (пошлина считается иначе)."""
    if car.age != "new" or not data.get("made"):
        return ""
    made = date.fromisoformat(data["made"])
    try:
        turn3 = made.replace(year=made.year + 3)
    except ValueError:  # 29 февраля
        turn3 = made.replace(year=made.year + 3, day=28)
    days = (turn3 - date.today()).days
    if days <= 0 or days > 400:
        return ""
    alt = calculate(CarInput(**{**car.__dict__, "age": "3-5"}), RULES, T().extras())
    if alt.total_mid is None or res_total is None or alt.total_mid >= res_total:
        return ""
    saving = res_total - alt.total_mid
    approx = "" if data.get("made_exact") else " (дата выпуска взята приблизительно)"
    return (
        f"\n\n⏳ Через {days} дн., с {turn3.strftime('%d.%m.%Y')}, машине исполнится 3 года{approx}. "
        f"Пошлина будет считаться за см³, а не от цены: итог примерно {format_rub(alt.total_mid)} "
        f"вместо {format_rub(res_total)}, экономия около {format_rub(saving)}. "
        "Возраст считается на дату подачи декларации."
    )


@router.callback_query(Calc.dest, F.data.startswith("dest:"))
async def calc_dest(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await c.answer()
    await c.message.edit_reply_markup(reply_markup=None)
    data = await state.get_data()
    data["destination"] = c.data.split(":")[1]
    rates, rates_src = await get_rates(RULES)
    car = CarInput(
        price_cny=data["price_cny"], age=data["age"], engine_cc=data["engine_cc"], power_hp=data["power_hp"],
        fuel=data["fuel"], route=data["route"], destination=data["destination"],
        cny_rub=rates["CNY"], eur_rub=rates["EUR"],
    )
    res = calculate(car, RULES, T().extras())
    saved = {k: data.get(k) for k in ("price_cny", "age", "engine_cc", "power_hp", "fuel", "route", "destination", "made", "made_exact", "preset")}
    db.add_calc(c.from_user.id, saved, res.total_mid, T().id)
    await state.clear()
    head = ""
    if data.get("preset") and data["preset"] in MODELS_BY_ID:
        head = f"{MODELS_BY_ID[data['preset']]['name']}\n"
    elif data.get("ai_model"):
        head = f"{data['ai_model']} (по скриншоту)\n"
    text = head + render_text(res, car) + _three_year_advice(data, car, res.total_mid)
    text += f"\n\nКурс: {rates_src}.\n{RULES['disclaimer']}"
    await c.message.answer(text, reply_markup=MAIN_KB)
    await c.message.answer("Что дальше?", reply_markup=AFTER_CALC_KB)


@router.callback_query(F.data == "go:routes")
async def compare_routes(c: CallbackQuery) -> None:
    await c.answer()
    last = db.last_calc(c.from_user.id, T().id)
    if not last:
        await c.message.answer("Сначала сделайте расчёт.", reply_markup=MAIN_KB)
        return
    rates, rates_src = await get_rates(RULES)
    lines = []
    for route, info in RULES["costs"]["logistics"].items():
        car = CarInput(
            price_cny=last["price_cny"], age=last["age"], engine_cc=last["engine_cc"], power_hp=last["power_hp"],
            fuel=last["fuel"], route=route, destination=last["destination"], cny_rub=rates["CNY"], eur_rub=rates["EUR"],
        )
        r = calculate(car, RULES, T().extras())
        mark = " ← ваш выбор" if route == last["route"] else ""
        lines.append(f"• {info['label']}: {format_rub(r.total_mid)}, {info['days']} дней{mark}")
    dest = RULES["costs"]["domestic"].get(last["destination"], {}).get("label", "")
    await c.message.answer(
        f"Та же машина, все маршруты (доставка по России: {dest}):\n\n" + "\n".join(lines) +
        "\n\nРазница в основном в логистике. Сроки зависят от очереди на границе и расписания судов.",
        reply_markup=MAIN_KB,
    )


@router.callback_query(F.data == "go:explain")
async def explain_lines(c: CallbackQuery) -> None:
    await c.answer()
    text = "Что это за статьи расчёта:\n\n" + "\n\n".join(f"• {EXPLAIN_TITLES[k]}. {EXPLAIN[k]}" for k in EXPLAIN)
    await c.message.answer(text, reply_markup=MAIN_KB)


# ---------- проверка льготы ----------

@router.message(F.text == BTN_CHECK)
@router.message(Command("check"))
async def check_start(m: Message, state: FSMContext) -> None:
    db.touch_user(m.from_user.id, m.from_user.username, T().id)
    await state.clear()
    await state.set_state(Check.fuel)
    await m.answer("Проверим, пройдёт ли машина по льготному утильсбору. Тип двигателя:", reply_markup=FUEL_KB)


@router.callback_query(Check.fuel, F.data.startswith("fuel:"))
async def check_fuel(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    fuel = c.data.split(":")[1]
    await state.update_data(fuel=fuel)
    await c.message.edit_reply_markup(reply_markup=None)
    if fuel in ("ev", "hybrid_seq"):
        await state.update_data(engine_cc=0)
        await state.set_state(Check.hp)
        await c.message.answer("Суммарная 30-минутная мощность электромоторов в л.с. (из ЭПТС или документов; пиковая обычно выше):", reply_markup=CANCEL_KB)
    else:
        await state.set_state(Check.cc)
        await c.message.answer("Объём двигателя в см³:", reply_markup=CANCEL_KB)


@router.message(Check.cc)
async def check_cc(m: Message, state: FSMContext) -> None:
    cc = _parse_cc(m.text or "")
    if cc is None:
        await m.answer("Нужно число, например 1998.")
        return
    await state.update_data(engine_cc=cc)
    await state.set_state(Check.hp)
    await m.answer("Мощность в л.с. (для параллельного гибрида суммарная):")


@router.message(Check.hp)
async def check_hp(m: Message, state: FSMContext) -> None:
    v = _parse_number(m.text or "")
    if v is None or v > 2000:
        await m.answer("Нужно число, например 147.")
        return
    d = await state.get_data()
    await state.clear()
    hp = int(round(v))
    base = dict(price_cny=1, engine_cc=d["engine_cc"], power_hp=hp, fuel=d["fuel"])
    new_amt, new_note, _ = util_fee(RULES, CarInput(age="new", **base))
    old_amt, _, _ = util_fee(RULES, CarInput(age="5+", **base))
    pref = RULES["util"]["preferential"]
    ev = d["fuel"] in ("ev", "hybrid_seq")
    ok = new_amt == pref["new_rub"]
    if ok:
        verdict = "✅ Да, льгота действует"
        why = (f"мощность {hp} л.с. не выше {pref['max_hp_ev'] if ev else pref['max_hp_ice']} л.с."
               + ("" if ev else f", объём {d['engine_cc']} см³ не выше {pref['max_cc']} см³"))
        amounts = f"Утильсбор: {format_rub(new_amt)} за машину до 3 лет, {format_rub(old_amt)} старше 3 лет."
        tail = "Остальные условия: ввоз для себя, одна машина в год, без продажи 12 месяцев."
    else:
        verdict = "❌ Нет, льготы не будет"
        limit = pref["max_hp_ev"] if ev else pref["max_hp_ice"]
        reasons = []
        if hp > limit:
            reasons.append(f"мощность {hp} л.с. выше порога {limit} л.с.")
        if not ev and d["engine_cc"] > pref["max_cc"]:
            reasons.append(f"объём {d['engine_cc']} см³ больше {pref['max_cc']} см³")
        why = "; ".join(reasons) or new_note
        amounts = (f"Коммерческий утильсбор: {format_rub(new_amt)} за машину до 3 лет, {format_rub(old_amt)} старше 3 лет."
                   if new_amt is not None else "Ставка для этой комбинации объёма и мощности не подтверждена, уточните у брокера.")
        tail = f"Совет: ищите версию той же модели с мощностью до {limit} л.с. или считайте полную стоимость в калькуляторе."
    await m.answer(f"{verdict}: {why.rstrip('.')}.\n{amounts}\n{tail}", reply_markup=MAIN_KB)


# ---------- проверка по VIN ----------

@router.message(F.text == BTN_VIN)
@router.message(Command("vin"))
async def vin_start(m: Message, state: FSMContext) -> None:
    db.touch_user(m.from_user.id, m.from_user.username, T().id)
    await state.clear()
    await _vin_intro(m)
    await state.set_state(Vin.vin)


@router.callback_query(F.data == "go:vin")
async def vin_start_cb(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    await state.clear()
    await _vin_intro(c.message)
    await state.set_state(Vin.vin)


async def _vin_intro(m: Message) -> None:
    t = T()
    if not t.is_main:
        await m.answer(
            f"Проверка по VIN: менеджер {t.brand} подскажет, как проверить историю машины, и назовёт стоимость и сроки.\n\n"
            "Введите VIN (17 символов):",
            reply_markup=CANCEL_KB,
        )
        return
    price = f"{t.vin_price} ⭐" if t.vin_price > 0 else "по договорённости"
    await m.answer(
        "Проверка по VIN: китайский отчёт об истории машины (пробег, ДТП, страховые случаи, "
        f"сервисные записи), переведённый на русский. Срок до 24 часов. Стоимость: {price}.\n\n"
        "Введите VIN (17 символов):",
        reply_markup=CANCEL_KB,
    )


@router.message(Vin.vin)
async def vin_input(m: Message, state: FSMContext) -> None:
    vin = (m.text or "").strip().upper().replace(" ", "")
    if not VIN_RE.match(vin):
        await m.answer("VIN должен быть из 17 символов без букв I, O, Q. Проверьте и введите снова.")
        return
    await state.update_data(vin=vin)
    await state.set_state(Vin.contact)
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Отправить мой номер", request_contact=True)], [KeyboardButton(text="Отмена")]],
        resize_keyboard=True,
    )
    await m.answer("Как с вами связаться? Отправьте номер телефона или напишите @username:", reply_markup=kb)


@router.message(Vin.contact)
async def vin_contact(m: Message, state: FSMContext, bot: Bot) -> None:
    contact = m.contact.phone_number if m.contact else (m.text or "").strip()
    if not contact:
        await m.answer("Напишите телефон или @username.")
        return
    data = await state.get_data()
    await state.clear()
    order_id = db.add_order(m.from_user.id, m.from_user.username, data["vin"], contact, T().vin_price, T().id)
    await notify_admins(
        bot,
        f"🔎 Заказ VIN #{order_id}\nVIN: {data['vin']}\nКонтакт: {contact}\n"
        f"От: @{m.from_user.username or '-'} (id {m.from_user.id})\n"
        f"Статус: {'ждём оплату' if T().vin_price > 0 else 'новый'}\n"
        f"Готовый отчёт: прикрепите файл с подписью /send {order_id}",
    )
    if T().vin_price > 0:
        await m.answer(f"Заказ #{order_id} создан. Оплатите, и мы начнём проверку.", reply_markup=MAIN_KB)
        await m.answer_invoice(
            title=f"Отчёт по VIN {data['vin']}",
            description="История автомобиля из китайских баз с переводом на русский. Срок до 24 часов.",
            payload=f"vin:{order_id}",
            currency="XTR",
            prices=[LabeledPrice(label="Отчёт по VIN", amount=T().vin_price)],
        )
    else:
        await m.answer(f"Заказ #{order_id} принят. {T().payment_text}", reply_markup=MAIN_KB)


def _payable_order(payload: str, currency: str, amount: int, user_id: int):
    """Заказ VIN этого бота и этого покупателя, который ждёт оплаты ровно на эту сумму в звёздах, иначе None.

    Без этой проверки владелец бота клиента мог бы выставить себе счёт с чужим номером заказа и увидеть
    VIN и контакт чужого покупателя.
    """
    if not payload.startswith("vin:") or currency != "XTR":
        return None
    try:
        order_id = int(payload.split(":", 1)[1])
    except ValueError:
        return None
    order = db.get_order(order_id, T().id)
    if (order is None or order["status"] != "awaiting_payment" or int(order["price_stars"] or 0) != amount
            or int(order["user_id"]) != user_id):
        return None
    return order


@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery) -> None:
    ok = _payable_order(q.invoice_payload or "", q.currency, q.total_amount, q.from_user.id) is not None
    if ok:
        await q.answer(ok=True)
    else:
        await q.answer(ok=False, error_message="Заказ не найден или уже оплачен. Оформите проверку VIN заново.")


@router.message(F.successful_payment)
async def paid(m: Message, bot: Bot) -> None:
    sp = m.successful_payment
    order = _payable_order(sp.invoice_payload or "", sp.currency, sp.total_amount, m.from_user.id)
    if order is None or not db.mark_paid(order["id"], sp.telegram_payment_charge_id, T().id):
        log.warning("payment not matched to an order: payload=%r amount=%s", sp.invoice_payload, sp.total_amount)
        await notify_admins(bot, f"⚠️ Оплата {sp.total_amount} ⭐ не привязана к заказу (payload {sp.invoice_payload!r}). "
                                 f"Проверьте и при необходимости верните звёзды: id платежа {sp.telegram_payment_charge_id}")
        return
    await m.answer(f"Оплата получена, заказ #{order['id']} в работе. Отчёт пришлём сюда в течение 24 часов.", reply_markup=MAIN_KB)
    await notify_admins(bot, f"💰 Оплачен заказ VIN #{order['id']}: {order['vin']}, контакт {order['contact']}")


# ---------- заявка на подбор ----------

@router.message(F.text == BTN_LEAD)
@router.message(Command("lead"))
async def lead_start(m: Message, state: FSMContext) -> None:
    db.touch_user(m.from_user.id, m.from_user.username, T().id)
    await state.clear()
    await state.set_state(Lead.model)
    await m.answer("Какую машину хотите? Марка, модель, год, пожелания:", reply_markup=CANCEL_KB)


@router.callback_query(F.data == "go:lead")
async def lead_start_cb(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    await state.clear()
    await state.set_state(Lead.model)
    await c.message.answer("Какую машину хотите? Марка, модель, год, пожелания:", reply_markup=CANCEL_KB)


@router.message(Lead.model)
async def lead_model(m: Message, state: FSMContext) -> None:
    await state.update_data(model=(m.text or "").strip()[:300])
    await state.set_state(Lead.budget)
    await m.answer("Бюджет под ключ в рублях:")


@router.message(Lead.budget)
async def lead_budget(m: Message, state: FSMContext) -> None:
    await state.update_data(budget=(m.text or "").strip()[:100])
    await state.set_state(Lead.city)
    await m.answer("Город получения:")


@router.message(Lead.city)
async def lead_city(m: Message, state: FSMContext) -> None:
    await state.update_data(city=(m.text or "").strip()[:100])
    await state.set_state(Lead.timeline)
    await m.answer("Когда планируете покупку?", reply_markup=TIMELINE_KB)


async def _ask_lead_contact(m: Message, state: FSMContext) -> None:
    await state.set_state(Lead.contact)
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Отправить мой номер", request_contact=True)], [KeyboardButton(text="Отмена")]],
        resize_keyboard=True,
    )
    t = T()
    consent = ("Оставляя контакт, вы соглашаетесь на передачу его агенту по подбору" if t.is_main
               else f"Оставляя контакт, вы соглашаетесь, что {t.brand} свяжется с вами по этой заявке")
    await m.answer(f"Телефон или @username для связи. {consent}:", reply_markup=kb)


@router.callback_query(Lead.timeline, F.data.startswith("lt:"))
async def lead_timeline(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    key = c.data.split(":", 1)[1]
    await state.update_data(timeline=key if key in TIMELINES else None)
    await c.message.edit_reply_markup(reply_markup=None)
    await _ask_lead_contact(c.message, state)


@router.message(Lead.timeline)
async def lead_timeline_text(m: Message, state: FSMContext) -> None:
    """Ответ текстом вместо кнопки: принимаем как есть и идём дальше."""
    await state.update_data(timeline=None, timeline_text=(m.text or "").strip()[:60])
    await _ask_lead_contact(m, state)


def _calc_summary(user_id: int) -> str | None:
    """Последний расчёт клиента для менеджера: что считал и на какую сумму вышло."""
    last = db.last_calc_row(user_id, T().id)
    if not last or last.get("total") is None:
        return None
    name = MODELS_BY_ID[last["preset"]]["name"] if last.get("preset") in MODELS_BY_ID else (
        f"{last.get('engine_cc')} см³, {last.get('power_hp')} л.с.")
    price = f"{last['price_cny']:,.0f} ¥".replace(",", " ") if last.get("price_cny") else "?"
    return f"{name}, цена в Китае {price}, под ключ {format_rub(last['total'])}"


def _lead_card(lead_id: int, d: dict, contact: str, username: str | None, user_id: int, summary: str | None,
               status: str = "new", by: str | None = None) -> str:
    when = TIMELINES.get(d.get("timeline") or "", d.get("timeline_text") or "не указано")
    hot = "🔥 " if d.get("timeline") == "now" else ""
    lines = [
        f"{hot}🚗 Заявка #{lead_id}",
        f"Машина: {d.get('model')}",
        f"Бюджет: {d.get('budget')}",
        f"Город: {d.get('city')}",
        f"Когда покупка: {when}",
        f"Контакт: {contact}",
        f"От: @{username or '-'} (id {user_id})" if user_id else "От: форма на сайте (без Telegram), позвоните клиенту",
    ]
    if summary:
        lines.append(f"Последний расчёт: {summary}")
    lines.append(f"Статус: {LEAD_STATUSES.get(status, status)}" + (f" ({by})" if by else ""))
    return "\n".join(lines)


@router.message(Lead.contact)
async def lead_contact(m: Message, state: FSMContext, bot: Bot) -> None:
    contact = m.contact.phone_number if m.contact else (m.text or "").strip()
    if not contact:
        await m.answer("Напишите телефон или @username.")
        return
    d = await state.get_data()
    await state.clear()
    summary = _calc_summary(m.from_user.id)
    lead_id = db.add_lead(m.from_user.id, m.from_user.username, d["model"], d["budget"], d["city"], contact,
                          tenant_id=T().id, timeline=d.get("timeline") or d.get("timeline_text"), calc_summary=summary)
    await notify_admins(
        bot, _lead_card(lead_id, d, contact, m.from_user.username, m.from_user.id, summary),
        reply_markup=lead_status_kb(lead_id),
    )
    t = T()
    who = "Агент" if t.is_main else f"Менеджер {t.brand}"
    await m.answer(f"Заявка #{lead_id} принята. {who} свяжется с вами в течение рабочего дня.", reply_markup=MAIN_KB)


@router.callback_query(F.data.startswith("ls:"))
async def lead_set_status(c: CallbackQuery) -> None:
    """Кнопки под заявкой у менеджера: в работе, сделка, отказ."""
    if not _is_admin(c):
        await c.answer("Только для менеджеров.", show_alert=True)
        return
    try:
        _, status, lead_id_s = c.data.split(":")
        lead_id = int(lead_id_s)
    except ValueError:
        await c.answer()
        return
    who = f"@{c.from_user.username}" if c.from_user.username else c.from_user.full_name
    if status not in LEAD_STATUSES or not db.set_lead_status(lead_id, T().id, status, who):
        await c.answer("Заявка не найдена.", show_alert=True)
        return
    await c.answer(LEAD_STATUSES[status])
    r = db.get_lead(lead_id, T().id)
    d = {"model": r["model"], "budget": r["budget"], "city": r["city"],
         "timeline": r["timeline"] if r["timeline"] in TIMELINES else None, "timeline_text": r["timeline"]}
    try:
        await c.message.edit_text(
            _lead_card(lead_id, d, r["contact"], r["username"], r["user_id"], r["calc_summary"], status, who),
            reply_markup=lead_status_kb(lead_id),
        )
    except Exception as e:  # noqa: BLE001
        log.info("edit lead card: %s", e)


# ---------- связь с нами ----------

@router.message(F.text == BTN_SUPPORT)
@router.message(Command("support"))
async def support_start(m: Message, state: FSMContext) -> None:
    db.touch_user(m.from_user.id, m.from_user.username, T().id)
    await state.clear()
    await state.set_state(Support.msg)
    direct = f"\nИли напишите менеджеру напрямую: {T().manager}" if T().manager else ""
    await m.answer(f"Напишите вопрос одним сообщением, можно с фото. Ответ придёт сюда же.{direct}", reply_markup=CANCEL_KB)


@router.message(Support.msg, ~F.text.startswith("/"))
async def support_message(m: Message, state: FSMContext, bot: Bot) -> None:
    kind = (await state.get_data()).get("kind")
    await state.clear()
    title = "🤝 Заявка на партнёрство" if kind == "partner" else "✍️ Вопрос"
    delivered = 0
    for admin in T().admin_ids:
        try:
            await bot.send_message(admin, f"{title} от @{m.from_user.username or '-'} (id {m.from_user.id}). Ответьте реплаем на следующее сообщение.")
            fwd = await bot.forward_message(admin, m.chat.id, m.message_id)
            db.add_support(admin, fwd.message_id, m.from_user.id, T().id)
            delivered += 1
        except Exception as e:  # noqa: BLE001
            log.warning("support forward failed for %s: %s", admin, e)
    if delivered:
        await m.answer("Передал. Обычно отвечаем в течение дня.", reply_markup=MAIN_KB)
    else:
        await m.answer("Не удалось передать сообщение, попробуйте позже.", reply_markup=MAIN_KB)


async def _is_support_reply(m: Message) -> bool:
    # async: синхронные фильтры aiogram выполняет в отдельном потоке, а соединение SQLite привязано к основному
    return (
        _is_admin(m)
        and m.reply_to_message is not None
        and db.support_user(m.from_user.id, m.reply_to_message.message_id, T().id) is not None
    )


@router.message(_is_support_reply)
async def support_reply(m: Message, bot: Bot) -> None:
    user_id = db.support_user(m.from_user.id, m.reply_to_message.message_id, T().id)
    try:
        if m.text:
            await bot.send_message(user_id, f"Ответ от нас:\n\n{m.text}")
        else:
            await bot.copy_message(user_id, m.chat.id, m.message_id)
        await m.answer("Отправлено пользователю.")
    except Exception as e:  # noqa: BLE001
        await m.answer(f"Не удалось отправить: {e}")


# ---------- админ ----------

@router.message(Command("orders"))
async def admin_orders(m: Message) -> None:
    if not _is_admin(m):
        return
    rows = db.list_orders(tenant_id=T().id)
    if not rows:
        await m.answer("Открытых заказов нет.")
        return
    await m.answer("\n".join(f"#{r['id']} {r['status']} {r['vin']} {r['contact']} {r['created_at'][:16]}" for r in rows))


@router.message(Command("leads"))
async def admin_leads(m: Message) -> None:
    if not _is_admin(m):
        return
    rows = db.list_leads(tenant_id=T().id)
    if not rows:
        await m.answer("Заявок пока нет.")
        return
    def fmt(r) -> str:
        hot = "🔥 " if r["timeline"] == "now" else ""
        return (f"{hot}#{r['id']} {LEAD_STATUSES.get(r['status'], r['status'])}\n{r['model']} | {r['budget']} | {r['city']} | {r['contact']}")
    await m.answer("Последние заявки:\n\n" + "\n\n".join(fmt(r) for r in rows))


@router.message(Command("report"))
async def admin_report(m: Message) -> None:
    if not _is_admin(m):
        return
    r = db.lead_report(T().id, days=30)
    s = db.stats(T().id)
    conv = f"{r['won'] * 100 // r['total']}%" if r["total"] else "—"
    await m.answer(
        "Отчёт за 30 дней\n\n"
        f"Заявок: {r['total']}, из них горячих 🔥 {r['hot']}\n"
        f"Новые: {r['new']}, в работе: {r['work']}, сделки: {r['won']}, отказы: {r['lost']}\n"
        f"Конверсия в сделку: {conv}\n\n"
        f"Всего пользователей бота: {s['users']}, расчётов: {s['calcs']} (за 7 дней {s['calcs_7d']})"
    )


# ---------- настройки бота клиента ----------

def _settings_text(t: tn.Tenant) -> str:
    row = db.get_tenant(t.id)
    status = {"trial": "пробный период", "active": "оплачен", "paused": "приостановлен"}.get(t.status, t.status)
    until = ""
    if row is not None:
        until = (row["trial_until"] if t.status == "trial" else row["paid_until"]) or ""
        until = f" до {until[:10]}" if until else ""
    fee = format_rub(t.service_fee) if t.service_fee else "не указаны"
    return (
        f"Настройки бота @{t.username}\n\n"
        f"Название: {t.brand}\n"
        f"Контакт менеджера: {t.manager or 'не указан'}\n"
        f"Ваши услуги в расчёте: {fee}\n"
        f"Статус: {status}{until}\n\n"
        "Изменить:\n/setbrand Новое название\n/setmanager @username или телефон («нет», чтобы убрать)\n/setfee 60000 (0, чтобы убрать)\n\n"
        "Сотрудники, которые получают заявки (добавляет и удаляет только владелец):\n"
        "/admins — список\n/addadmin 123456789 — добавить (id узнать у @userinfobot)\n/deladmin 123456789 — убрать"
    )


async def _reload_tenant(bot: Bot, tenant_id: str) -> tn.Tenant | None:
    row = db.get_tenant(tenant_id)
    if row is None:
        return None
    t = _tenant_from_row(row)
    tn.BY_BOT[row["bot_id"]] = t
    await tn.configure_bot(bot, t, COMMANDS)
    return t


@router.message(Command("settings", "setbrand", "setmanager", "setfee", "addadmin", "deladmin", "admins"))
async def tenant_settings(m: Message, command: CommandObject, bot: Bot) -> None:
    t = T()
    if not _is_admin(m):
        return
    if t.is_main:
        await m.answer("Это основной бот: его настройки задаются в .env на сервере.")
        return
    arg = (command.args or "").strip()
    cmd = command.command
    row = db.get_tenant(t.id)
    owner_id = int(row["owner_id"]) if row is not None else None
    if cmd == "settings":
        await m.answer(_settings_text(t))
        return
    if cmd == "admins":
        lines = [f"{i}{' (владелец)' if i == owner_id else ''}" for i in sorted(t.admin_ids)]
        await m.answer("Получают заявки:\n" + "\n".join(lines) + "\n\nДобавить: /addadmin id, убрать: /deladmin id")
        return
    if cmd in ("addadmin", "deladmin") and m.from_user.id != owner_id:
        await m.answer("Добавлять и убирать сотрудников может только владелец бота.")
        return
    if not arg:
        await m.answer("Укажите значение после команды, например /setfee 60000.")
        return
    if cmd == "setbrand":
        if not (2 <= len(arg) <= 60):
            await m.answer("Название от 2 до 60 символов.")
            return
        db.update_tenant(t.id, brand=arg)
    elif cmd == "setmanager":
        db.update_tenant(t.id, manager="" if arg.lower() in ("нет", "-", "no", "0") else arg[:60])
    elif cmd == "setfee":
        fee = _parse_number(arg) if arg not in ("0", "нет") else 0
        if fee is None or fee > 5_000_000:
            await m.answer("Нужна сумма в рублях, например 60000, или 0.")
            return
        db.update_tenant(t.id, service_fee=int(fee))
    elif cmd == "addadmin":
        if not arg.isdigit():
            await m.answer("Нужен числовой id сотрудника. Узнать его можно у @userinfobot.")
            return
        ids = set(t.admin_ids) | {int(arg)}
        db.update_tenant(t.id, admin_ids=",".join(str(i) for i in sorted(ids)))
    elif cmd == "deladmin":
        if not arg.isdigit():
            await m.answer("Нужен числовой id сотрудника, список: /admins.")
            return
        if int(arg) == owner_id:
            await m.answer("Владельца убрать нельзя.")
            return
        ids = set(t.admin_ids) - {int(arg)}
        db.update_tenant(t.id, admin_ids=",".join(str(i) for i in sorted(ids)))
    new = await _reload_tenant(bot, t.id)
    await m.answer("Сохранено.\n\n" + _settings_text(new or t))


@router.message(Command("done"))
async def admin_done(m: Message, bot: Bot) -> None:
    if not _is_admin(m):
        return
    parts = (m.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await m.answer("Использование: /done <номер заказа>")
        return
    order = db.get_order(int(parts[1]), T().id)
    if not order or not db.mark_done(order["id"], T().id):
        await m.answer("Заказ не найден.")
        return
    await m.answer(f"Заказ #{order['id']} закрыт.")
    try:
        await bot.send_message(order["user_id"], f"Заказ #{order['id']} выполнен. Если отчёт ещё не пришёл, напишите нам.")
    except Exception as e:  # noqa: BLE001
        log.warning("notify user failed: %s", e)


@router.message(F.document, F.caption.regexp(r"^/send\s+\d+"))
async def admin_send_report(m: Message, bot: Bot) -> None:
    """Админ прикрепляет файл отчёта с подписью «/send <номер заказа>»: бот пересылает клиенту и закрывает заказ."""
    if not _is_admin(m):
        return
    order_id = int(m.caption.split()[1])
    order = db.get_order(order_id, T().id)
    if not order:
        await m.answer("Заказ не найден.")
        return
    try:
        await bot.send_document(
            order["user_id"], m.document.file_id,
            caption=f"Отчёт по VIN {order['vin']} готов. Если есть вопросы по отчёту, напишите нам здесь.",
        )
    except Exception as e:  # noqa: BLE001
        await m.answer(f"Не удалось отправить клиенту: {e}")
        return
    db.mark_done(order_id, T().id)
    await m.answer(f"Отчёт отправлен, заказ #{order_id} закрыт.")


@router.message(Command("stats"))
async def admin_stats(m: Message) -> None:
    if not _is_admin(m):
        return
    s = db.stats(T().id)
    await m.answer(
        f"Пользователей: {s['users']}\nРасчётов: {s['calcs']} (за 7 дней {s['calcs_7d']})\n"
        f"Заказов VIN: {s['orders']} (оплачено {s['orders_paid']})\nЗаявок: {s['leads']}"
    )


@router.message(Command("export"))
async def admin_export(m: Message) -> None:
    if not _is_admin(m):
        return
    data = db.export_csv(T().id)
    await m.answer_document(
        BufferedInputFile(data, filename=f"car-import-{date.today().isoformat()}.csv"),
        caption="Заказы, заявки и расчёты. Разделитель «;», открывается в Excel.",
    )


@router.message(Command("broadcast"))
async def admin_broadcast(m: Message, state: FSMContext, command: CommandObject) -> None:
    if not _is_admin(m):
        return
    text = (command.args or "").strip()
    if not text:
        await m.answer("Использование: /broadcast текст сообщения. Перед отправкой попрошу подтверждение.")
        return
    n = len(db.all_user_ids(T().id))
    await state.set_state(Broadcast.confirm)
    await state.update_data(text=text)
    await m.answer(
        f"Получателей: {n}. Текст:\n\n{text}",
        reply_markup=_ikb([[("📣 Отправить всем", "bc:yes"), ("Отмена", "bc:no")]]),
    )


@router.callback_query(Broadcast.confirm, F.data.startswith("bc:"))
async def admin_broadcast_confirm(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await c.answer()
    d = await state.get_data()
    await state.clear()
    await c.message.edit_reply_markup(reply_markup=None)
    if c.data != "bc:yes":
        await c.message.answer("Рассылка отменена.")
        return
    sent = failed = 0
    for uid in db.all_user_ids(T().id):
        try:
            await bot.send_message(uid, d["text"])
            sent += 1
        except Exception:  # noqa: BLE001
            failed += 1
        await asyncio.sleep(0.05)
    await c.message.answer(f"Разослано: {sent}, не доставлено: {failed}.")


# ---------- боты клиентов: самостоятельный запуск ----------

TOKEN_RE = re.compile(r"^\d{5,12}:[A-Za-z0-9_-]{30,}$")


def _tenant_from_row(row) -> tn.Tenant:
    base = tn.from_row(row, web_url="", payment_text="Менеджер свяжется с вами, расскажет о стоимости и сроках.")
    return dataclasses.replace(base, web_url=tn.tenant_web_url(WEB_URL, base))


async def validate_token(token: str) -> tuple[int, str] | None:
    """Проверяет токен у Telegram: (bot_id, username) или None. Подменяется в тестах."""
    b = Bot(token)
    try:
        me = await b.get_me()
        return me.id, me.username or ""
    except Exception as e:  # noqa: BLE001
        log.info("token check failed: %s", e)
        return None
    finally:
        await b.session.close()


async def start_tenant_bot(t: tn.Tenant, token: str) -> None:
    """Подключает бота клиента к общему опросу. Без запущенного Runner (тесты) только регистрирует."""
    bot_id = int(token.split(":")[0])
    tn.BY_BOT[bot_id] = t
    if RUNNER is None or RUNNER.running(bot_id):
        return
    b = Bot(token)
    if not await RUNNER.start(b):  # уже запущен параллельным вызовом
        await b.session.close()
        return
    await tn.configure_bot(b, t, COMMANDS)


@router.callback_query(F.data == "wl:start")
@router.message(Command("mybot"))
async def wl_start(event: Message | CallbackQuery, state: FSMContext) -> None:
    if not _platform():
        return
    m = event.message if isinstance(event, CallbackQuery) else event
    if isinstance(event, CallbackQuery):
        await event.answer()
    await state.clear()
    await state.set_state(WL.token)
    await m.answer(
        "Запустим ваш бот за 5 минут.\n\n"
        "1. Откройте @BotFather и отправьте ему /newbot.\n"
        "2. Придумайте имя, например «Авто из Китая — ВашаКомпания», и адрес, который заканчивается на bot.\n"
        "3. BotFather пришлёт токен вида 123456789:AA… Скопируйте его и пришлите сюда.\n\n"
        "Сообщение с токеном я сразу удалю из чата. Токен нужен, чтобы бот работал на нашем сервере.",
        reply_markup=CANCEL_KB,
    )


@router.message(WL.token)
async def wl_token(m: Message, state: FSMContext) -> None:
    token = (m.text or "").strip()
    try:
        await m.delete()
    except Exception:  # noqa: BLE001
        pass
    if not TOKEN_RE.match(token):
        await m.answer("Это не похоже на токен. Он выглядит так: 123456789:AAH… Пришлите его целиком.")
        return
    if token == BOT_TOKEN:
        await m.answer("Это токен нашего бота. Нужен токен вашего нового бота от @BotFather.")
        return
    checked = await validate_token(token)
    if checked is None:
        await m.answer("Telegram не принял этот токен. Проверьте, что скопировали его полностью, и пришлите снова.")
        return
    bot_id, username = checked
    existing = db.tenant_by_bot(bot_id)
    if existing is not None:
        await state.clear()
        admins = {int(x) for x in str(existing["admin_ids"]).split(",") if x}
        if m.from_user.id != int(existing["owner_id"]) and m.from_user.id not in admins:
            await m.answer(f"Бот @{username} уже подключён другим владельцем.")
            return
        if token == existing["token"]:
            await m.answer(f"Бот @{username} уже подключён. Откройте его и напишите /settings.")
            return
        # Владелец перевыпустил токен у @BotFather: обновляем и запускаем заново
        fields: dict = {"token": token}
        if existing["status"] == "revoked":
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            fields["status"] = "active" if (existing["paid_until"] or "") > now else "trial"
        db.update_tenant(existing["id"], **fields)
        row = db.get_tenant(existing["id"])
        if row["status"] == "paused":
            await m.answer(f"Токен бота @{username} обновлён. Бот приостановлен, чтобы возобновить, напишите нам: {PLATFORM_CONTACT or '/support'}.")
            return
        if RUNNER is not None:
            await RUNNER.stop(bot_id)
        await start_tenant_bot(_tenant_from_row(row), token)
        await m.answer(f"Токен обновлён, бот @{username} снова работает.", reply_markup=MAIN_KB)
        return
    await state.update_data(token=token, bot_id=bot_id, username=username)
    await state.set_state(WL.brand)
    await m.answer(f"Бот @{username} найден. Как называется ваша компания? Так бот будет представляться клиентам.")


@router.message(WL.brand)
async def wl_brand(m: Message, state: FSMContext) -> None:
    brand = " ".join((m.text or "").split())
    if not (2 <= len(brand) <= 60):
        await m.answer("Название от 2 до 60 символов.")
        return
    await state.update_data(brand=brand)
    await state.set_state(WL.manager)
    await m.answer("Контакт менеджера, который бот покажет клиентам: @username или телефон. Если не нужен, отправьте «нет».")


@router.message(WL.manager)
async def wl_manager(m: Message, state: FSMContext) -> None:
    v = (m.text or "").strip()
    await state.update_data(manager="" if v.lower() in ("нет", "-", "no") else v[:60])
    await state.set_state(WL.fee)
    await m.answer(
        "Ваша комиссия или стоимость услуг в рублях. Бот добавит её отдельной строкой, и клиент увидит вашу цену под ключ. "
        "Например 60000. Если не нужно, отправьте 0."
    )


@router.message(WL.fee)
async def wl_fee(m: Message, state: FSMContext) -> None:
    v = (m.text or "").strip()
    fee = 0 if v in ("0", "нет", "-") else _parse_number(v)
    if fee is None or fee > 5_000_000:
        await m.answer("Нужна сумма в рублях, например 60000, или 0.")
        return
    await state.update_data(fee=int(fee))
    d = await state.get_data()
    await state.set_state(WL.confirm)
    await m.answer(
        "Проверьте:\n\n"
        f"Бот: @{d['username']}\nКомпания: {d['brand']}\nМенеджер: {d['manager'] or 'не указан'}\n"
        f"Ваши услуги в расчёте: {format_rub(d['fee']) if d['fee'] else 'нет'}\n\n"
        f"Пробный период {TRIAL_DAYS} дней без оплаты. Потом подключение 15 000 ₽ и 3 000 ₽ в месяц, "
        "напомним заранее. Настройки можно поменять в любой момент.",
        reply_markup=_ikb([[("🚀 Запустить", "wl:go"), ("Отмена", "wl:no")]]),
    )


@router.callback_query(WL.confirm, F.data.in_({"wl:go", "wl:no"}))
async def wl_confirm(c: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await c.answer()
    d = await state.get_data()
    await state.clear()
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
    if c.data != "wl:go":
        await c.message.answer("Отменил. Запустить можно в любой момент: /mybot.", reply_markup=MAIN_KB)
        return
    if db.tenant_by_bot(d["bot_id"]) is not None:
        await c.message.answer("Этот бот уже подключён.", reply_markup=MAIN_KB)
        return
    tenant_id = (d["username"] or str(d["bot_id"])).lower()
    if db.get_tenant(tenant_id) is not None:  # тот же username у удалённого и пересозданного бота
        tenant_id = f"{tenant_id}_{d['bot_id']}"
    try:
        db.add_tenant(tenant_id=tenant_id, bot_id=d["bot_id"], token=d["token"], username=d["username"], brand=d["brand"],
                      manager=d["manager"], service_fee=d["fee"], owner_id=c.from_user.id, trial_days=TRIAL_DAYS)
    except Exception:  # noqa: BLE001
        log.exception("add tenant failed")
        await c.message.answer("Не получилось подключить бота. Попробуйте ещё раз: /mybot, или напишите нам: /support.", reply_markup=MAIN_KB)
        return
    t = _tenant_from_row(db.get_tenant(tenant_id))
    await start_tenant_bot(t, d["token"])
    row = db.get_tenant(tenant_id)
    await c.message.answer(
        f"Готово! Бот @{d['username']} работает под брендом «{d['brand']}».\n\n"
        f"Важно: откройте t.me/{d['username']} и нажмите «Старт». Без этого Telegram не даст боту присылать вам заявки.\n\n"
        "Что дальше:\n"
        "• Поставьте ссылку на бота на сайт, в описание канала и в ответы клиентам.\n"
        "• Заявки придут в чат с ботом, с кнопками «В работе», «Сделка», «Отказ».\n"
        "• В боте вам доступны /leads, /report, /settings, /stats, /export.\n\n"
        f"Пробный период до {row['trial_until'][:10]}.",
        reply_markup=MAIN_KB,
    )
    await notify_admins(
        bot,
        f"🆕 Новый бот клиента @{d['username']}: «{d['brand']}», владелец @{c.from_user.username or '-'} (id {c.from_user.id}), "
        f"услуги {d['fee']} ₽, пробный период до {row['trial_until'][:10]}.",
        admins=ADMIN_IDS,
    )


# ---------- боты клиентов: управление для владельца платформы ----------

def _chunks(lines: list[str], limit: int = 3500) -> list[str]:
    """Режет список строк на сообщения не длиннее лимита Telegram."""
    out, cur = [], ""
    for ln in lines:
        if cur and len(cur) + len(ln) + 1 > limit:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{ln}" if cur else ln
    if cur:
        out.append(cur)
    return out


def _admins_of(row) -> frozenset[int]:
    return frozenset(int(x) for x in str(row["admin_ids"]).split(",") if x)


HEALTH = {"ok": "работает", "conflict": "⚠️ конфликт опроса (чужой вебхук или второй процесс)", "revoked": "⚠️ токен отозван"}


@router.message(Command("tenants"))
async def platform_tenants(m: Message) -> None:
    if not _is_platform_admin(m):
        return
    rows = db.list_tenants()
    if not rows:
        await m.answer("Ботов клиентов пока нет. Ссылка для клиентов: t.me/<ваш бот>?start=partner, кнопка «Запустить свой бот».")
        return
    out = []
    for r in rows:
        cnt = db.tenant_counts(r["id"])
        until = r["trial_until"] if r["status"] == "trial" else r["paid_until"]
        if RUNNER is not None and RUNNER.running(r["bot_id"]):
            running = HEALTH.get(RUNNER.health.get(r["bot_id"], "ok"), "работает")
        else:
            running = "не запущен"
        out.append(f"{r['id']} @{r['username']} «{r['brand']}»: {r['status']} до {(until or '-')[:10]}, {running}\n"
                   f"   пользователей {cnt['users']}, расчётов {cnt['calcs']}, заявок {cnt['leads']}")
    lines = [f"Боты клиентов ({len(rows)}):", ""] + out + [
        "", "/tenant_on <id> <дней> — оплачен, /tenant_off <id> — пауза, /tenant_owner <id> <user_id> — сменить владельца"]
    for part in _chunks(lines):
        await m.answer(part)


@router.message(Command("tenant_on", "tenant_off", "tenant_owner"))
async def platform_tenant_switch(m: Message, command: CommandObject, bot: Bot) -> None:
    if not _is_platform_admin(m):
        return
    parts = (command.args or "").split()
    if not parts:
        await m.answer("Использование: /tenant_on <id> <дней>, /tenant_off <id>, /tenant_owner <id> <user_id>")
        return
    row = db.get_tenant(parts[0].lower().lstrip("@"))
    if row is None:
        await m.answer("Нет такого бота. Список: /tenants")
        return
    if command.command == "tenant_owner":
        if len(parts) < 2 or not parts[1].isdigit():
            await m.answer("Использование: /tenant_owner <id> <user_id нового владельца>")
            return
        new_owner = int(parts[1])
        db.update_tenant(row["id"], owner_id=new_owner, admin_ids=str(new_owner))
        if row["bot_id"] in tn.BY_BOT:
            tn.BY_BOT[row["bot_id"]] = _tenant_from_row(db.get_tenant(row["id"]))
        await m.answer(f"Владелец @{row['username']}: {new_owner}. Остальные сотрудники убраны, владелец добавит их заново.")
        return
    if command.command == "tenant_off":
        db.update_tenant(row["id"], status="paused")
        tn.BY_BOT.pop(row["bot_id"], None)
        if RUNNER is not None:
            await RUNNER.stop(row["bot_id"])
        await m.answer(f"@{row['username']} приостановлен.")
        return
    days = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 30
    now = datetime.now(timezone.utc)
    start = now
    if row["paid_until"]:
        try:
            start = max(now, datetime.fromisoformat(row["paid_until"]))
        except ValueError:
            pass
    paid_until = (start + timedelta(days=days)).isoformat(timespec="seconds")
    db.update_tenant(row["id"], status="active", paid_until=paid_until, expiry_notified=0)
    t = _tenant_from_row(db.get_tenant(row["id"]))
    await start_tenant_bot(t, row["token"])
    tn.BY_BOT[row["bot_id"]] = t
    await m.answer(f"@{row['username']} оплачен до {paid_until[:10]}.")
    if RUNNER is not None and row["bot_id"] in RUNNER.bots:
        await notify_admins(RUNNER.bots[row["bot_id"]], f"Спасибо! Бот оплачен до {paid_until[:10]}.", admins=_admins_of(row))


async def _notify_tenant(row, text: str) -> None:
    """Сообщение админам бота клиента от имени его же бота (если бот запущен)."""
    tbot = RUNNER.bots.get(row["bot_id"]) if RUNNER is not None else None
    if tbot is not None:
        await notify_admins(tbot, text, admins=_admins_of(row))


async def check_billing(bot: Bot) -> None:
    """Напоминания: за 2 дня до конца пробного периода, после его окончания и после окончания оплаты.
    Бот клиента при этом не отключается, решение за владельцем платформы."""
    contact = PLATFORM_CONTACT or "кнопка «Написать нам» в нашем боте"
    for row in db.tenants_trial_ending(days=2):
        db.update_tenant(row["id"], reminder_sent=1)
        await _notify_tenant(row, (
            f"Пробный период бота @{row['username']} закончится {row['trial_until'][:10]}. Чтобы бот продолжил работать: "
            f"подключение 15 000 ₽ и 3 000 ₽ в месяц. Написать нам: {contact}."))
    for row in db.tenants_trial_expired():
        db.update_tenant(row["id"], expiry_notified=1)
        await _notify_tenant(row, (
            f"Пробный период бота @{row['username']} закончился. Бот пока работает. "
            f"Чтобы оставить его за вами: подключение 15 000 ₽ и 3 000 ₽ в месяц. Написать нам: {contact}."))
        await notify_admins(bot, f"⏰ Закончился пробный период у @{row['username']} «{row['brand']}». "
                                 f"Оплатили: /tenant_on {row['id']} 30, отключить: /tenant_off {row['id']}", admins=ADMIN_IDS)
    for row in db.tenants_paid_expired():
        db.update_tenant(row["id"], expiry_notified=1)
        await _notify_tenant(row, f"Оплаченный период бота @{row['username']} закончился. Продлить: 3 000 ₽ в месяц. Написать нам: {contact}.")
        await notify_admins(bot, f"💳 Закончилась оплата у @{row['username']} «{row['brand']}». "
                                 f"Продлили: /tenant_on {row['id']} 30, отключить: /tenant_off {row['id']}", admins=ADMIN_IDS)


async def ingest_site_leads(bot: Bot, leads: list[dict]) -> int:
    """Новые заявки с сайта: в базу основного бота и менеджерам с кнопками статуса. Возвращает число новых."""
    after = int(db.get_meta("site_leads_after", "0") or 0)
    added = 0
    for sl in sorted(leads, key=lambda x: x["id"]):
        if sl["id"] <= after:
            continue
        name = str(sl.get("name") or "").strip()[:80]
        phone = str(sl.get("phone") or "").strip()[:40]
        contact = f"{phone} ({name})" if name else phone
        model = str(sl.get("model") or "").strip()[:80] or "не выбрана"
        total = sl.get("total")
        budget = f"по расчёту {format_rub(total)}" if isinstance(total, int) and total > 0 else "не указан"
        city = str(sl.get("city") or "").strip()[:60] or "не указан"
        when = sl.get("when") if sl.get("when") in TIMELINES else None
        summary = str(sl.get("calc") or "").strip()[:400] or None
        with tn.use(_main_tenant()):
            lead_id = db.add_lead(0, None, model, budget, city, contact, tenant_id=MAIN, timeline=when,
                                  calc_summary=summary, source="site")
            await notify_admins(bot, _lead_card(lead_id, {"model": model, "budget": budget, "city": city, "timeline": when},
                                                contact, None, 0, summary), reply_markup=lead_status_kb(lead_id))
        after = sl["id"]
        db.set_meta("site_leads_after", str(after))
        added += 1
    return added


async def site_leads_watch(bot: Bot) -> None:
    """Раз в минуту забирает заявки с сайта. Ошибки (сайт ещё не выложен, нет сети) пишет в журнал не чаще раза в час."""
    if not SITE_LEADS_URL or not BOT_TOKEN:
        return
    token = site_token(BOT_TOKEN)
    warned_at = 0.0
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        while True:
            try:
                after = int(db.get_meta("site_leads_after", "0") or 0)
                leads, last = await fetch_leads(session, SITE_LEADS_URL, token, after)
                if last < after:
                    log.warning("site leads: на сайте последняя заявка #%d, у бота #%d; файл заявок начат заново", last, after)
                    db.set_meta("site_leads_after", "0")
                    continue
                n = await ingest_site_leads(bot, leads)
                if n:
                    log.info("site leads: %d new", n)
                    if len(leads) >= 100:
                        continue
            except Exception as e:  # noqa: BLE001
                if time.monotonic() - warned_at > 3600:
                    log.warning("site leads: %s", e)
                    warned_at = time.monotonic()
            await asyncio.sleep(SITE_LEADS_INTERVAL)


async def trial_watch(bot: Bot) -> None:
    while True:
        try:
            await check_billing(bot)
        except Exception:  # noqa: BLE001
            log.exception("billing check failed")
        await asyncio.sleep(3600)


# Токен бота, присланный вне сценария запуска (например, после перезапуска сервера состояние потерялось):
# сразу удаляем сообщение, чтобы токен не остался в чате и не ушёл в ответы на вопросы.
@router.message(F.text.regexp(TOKEN_RE.pattern))
async def stray_token(m: Message, state: FSMContext) -> None:
    if _platform():
        await state.set_state(WL.token)
        await wl_token(m, state)
        return
    try:
        await m.delete()
    except Exception:  # noqa: BLE001
        pass
    await m.answer("Похоже на токен бота, я удалил это сообщение. Не отправляйте токены в чаты.", reply_markup=MAIN_KB)


def _faq_reply(text: str) -> faq.FaqAnswer | None:
    return faq.answer(text, MODELS_BY_ID, RULES["util"]["preferential"], FUEL_LABELS)


@router.message(StateFilter(None), F.text)
async def free_question(m: Message) -> None:
    """Вопрос своими словами.

    Сначала ИИ по нашим ставкам и гиду, если он включён и лимит не исчерпан. Иначе или при ошибке ИИ
    отвечаем по ключевым словам из гида. Если и так не распознали, подсказываем меню.
    """
    text = (m.text or "").strip()
    if text.startswith("/"):
        await m.answer("Выберите действие на клавиатуре или отправьте /start. Вопрос человеку можно задать через «Написать нам».", reply_markup=MAIN_KB)
        return
    db.touch_user(m.from_user.id, m.from_user.username, T().id)
    limit_hit = False
    if ai.enabled and len(text) >= 6:
        if ai.allow(m.from_user.id):
            try:
                reply = await ai.answer(text, AI_FACTS)
            except AIError:
                reply = None
            if reply:
                await m.answer(reply + "\n\nОтвет сформирован автоматически по правилам бота. Точный расчёт: «Рассчитать под ключ».", reply_markup=MAIN_KB)
                return
        else:
            limit_hit = True
    fa = _faq_reply(text)
    if fa:
        kb = _ikb([[b] for b in fa.buttons]) if fa.buttons else MAIN_KB
        await m.answer(fa.text, reply_markup=kb)
        return
    if limit_hit:
        await m.answer("На сегодня лимит вопросов исчерпан. Задайте вопрос человеку через «Написать нам».", reply_markup=MAIN_KB)
        return
    await m.answer(
        "Не нашёл ответа на этот вопрос. Выберите действие на клавиатуре, откройте «Гид по перегону» "
        "или задайте вопрос человеку через «Написать нам».",
        reply_markup=MAIN_KB,
    )


@router.callback_query(F.data == "go:check")
async def check_start_cb(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    await state.clear()
    await state.set_state(Check.fuel)
    await c.message.answer("Проверим, пройдёт ли машина по льготному утильсбору. Тип двигателя:", reply_markup=FUEL_KB)


@router.callback_query(F.data.startswith("faq:model:"))
async def faq_model_calc(c: CallbackQuery, state: FSMContext) -> None:
    """Кнопка «Посчитать модель» из ответа на вопрос: сразу расчёт с характеристиками модели из списка."""
    await c.answer()
    mdl = MODELS_BY_ID.get(c.data.split(":", 2)[2])
    await state.clear()
    if not mdl:
        await state.set_state(Calc.mode)
        await c.message.answer("Как считаем?", reply_markup=START_MODE_KB)
        return
    await state.set_data({"preset": mdl["id"], "engine_cc": mdl["cc"], "power_hp": mdl["hp"], "fuel": mdl["fuel"]})
    note = f"\n⚠️ {mdl['note']}." if mdl.get("note") else ""
    await _next_step(c.message, state, f"{mdl['name']}: {mdl['cc']} см³, {mdl['hp']} л.с., {FUEL_LABELS[mdl['fuel']]}.{note}\n\n")


@router.message()
async def fallback(m: Message) -> None:
    await m.answer("Выберите действие на клавиатуре или отправьте /start.", reply_markup=MAIN_KB)


COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="calc", description="Рассчитать стоимость под ключ"),
    BotCommand(command="check", description="Проверить льготный утильсбор"),
    BotCommand(command="guide", description="Гид по перегону"),
    BotCommand(command="vin", description="Проверить машину по VIN"),
    BotCommand(command="lead", description="Оставить заявку на подбор"),
    BotCommand(command="support", description="Написать нам"),
    BotCommand(command="help", description="Как это работает"),
]


def _main_bot() -> Bot | None:
    if RUNNER is None or not BOT_TOKEN:
        return None
    return RUNNER.bots.get(int(BOT_TOKEN.split(":")[0]))


async def _on_unauthorized(bot: Bot) -> None:
    """Клиент отозвал токен у @BotFather: бот на паузу «revoked», владельцу и платформе сообщаем, как вернуть."""
    tn.BY_BOT.pop(bot.id, None)
    row = db.tenant_by_bot(bot.id)
    if row is None:
        log.error("main bot token rejected by Telegram")
        return
    db.update_tenant(row["id"], status="revoked")
    log.warning("tenant bot %s stopped: token revoked", bot.id)
    main_bot = _main_bot()
    if main_bot is None:
        return
    await notify_admins(main_bot, (
        f"Бот @{row['username']} остановлен: токен отозван у @BotFather. Чтобы он снова заработал, "
        "пришлите новый токен здесь командой /mybot."), admins=frozenset({int(row["owner_id"])}))
    await notify_admins(main_bot, f"⚠️ Бот клиента @{row['username']} остановлен: токен отозван у @BotFather.", admins=ADMIN_IDS)


async def _on_conflict(bot: Bot, error: str) -> None:
    row = db.tenant_by_bot(bot.id)
    main_bot = _main_bot()
    if main_bot is None:
        return
    name = f"@{row['username']}" if row is not None else "основного бота"
    await notify_admins(main_bot, (
        f"⚠️ Опрос {name} конфликтует: {error[:200]}. Обычно это чужой вебхук или второй процесс с тем же токеном. "
        "Заявки из этого бота сейчас не приходят."), admins=ADMIN_IDS)


async def main() -> None:
    global RUNNER
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not BOT_TOKEN:
        while True:
            log.error("BOT_TOKEN не задан. Впишите токен от @BotFather в .env (на сервере: секрет DEPLOY_ENV_FILE) и перезапустите.")
            await asyncio.sleep(3600)
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.update.outer_middleware(tn.tenant_middleware)
    dp.include_router(router)
    log.info("rules version %s, models %d, admins %s, vin price %s stars, ai %s (%s)", RULES["version"], len(MODELS), sorted(ADMIN_IDS), VIN_PRICE_STARS, "on" if ai.enabled else "off", ai.model)
    try:
        await bot.set_my_commands(COMMANDS)
    except Exception as e:  # noqa: BLE001
        log.warning("set_my_commands failed: %s", e)
    RUNNER = tn.Runner(dp)
    RUNNER.on_unauthorized = _on_unauthorized
    RUNNER.on_conflict = _on_conflict
    tn.MAIN_BOT_ID = bot.id
    await RUNNER.start(bot)
    started = 0
    for row in db.list_tenants(running_only=True):
        try:
            await start_tenant_bot(_tenant_from_row(row), row["token"])
            started += 1
        except Exception:  # noqa: BLE001
            log.exception("tenant %s failed to start", row["id"])
    log.info("tenant bots started: %d", started)
    watch = asyncio.create_task(trial_watch(bot))  # ссылки держат задачи живыми
    site_watch = asyncio.create_task(site_leads_watch(bot))
    await RUNNER.wait()
    watch.cancel()
    site_watch.cancel()


if __name__ == "__main__":
    asyncio.run(main())
