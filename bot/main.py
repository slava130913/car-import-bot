"""Telegram-бот: калькулятор «под ключ», проверка льготы, гид, заказ отчёта по VIN, заявка на подбор, связь с нами.

Запуск: python -m bot.main  (переменные окружения см. в .env.example)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import date
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
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

from .db import DB
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

RULES = load_rules()
with open(ROOT / "data" / "models.json", encoding="utf-8") as _f:
    MODELS: list[dict] = json.load(_f)["models"]
MODELS_BY_ID = {m["id"]: m for m in MODELS}
db = DB(DB_PATH)
router = Router()

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
    contact = State()


class Support(StatesGroup):
    msg = State()


class Broadcast(StatesGroup):
    confirm = State()


async def notify_admins(bot: Bot, text: str) -> None:
    for admin in ADMIN_IDS:
        try:
            await bot.send_message(admin, text)
        except Exception as e:  # noqa: BLE001
            log.warning("admin notify failed for %s: %s", admin, e)


def _is_admin(m: Message) -> bool:
    return m.from_user is not None and m.from_user.id in ADMIN_IDS


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


HELP_TEXT = (
    "Что умеет бот:\n\n"
    "1. Калькулятор «под ключ»: цена в Китае, перевод денег, расходы в Китае, логистика, пошлина, "
    "утильсбор, СБКТС, ЭПТС и доставка по России. Можно выбрать модель из списка, сравнить маршруты, "
    "узнать, что изменится, когда машине исполнится 3 года.\n\n"
    "2. Проверка льготы: за 10 секунд понять, попадает ли машина под льготный утильсбор.\n\n"
    "3. Гид по перегону: шаги, документы, сроки, типичные ошибки.\n\n"
    "4. Проверка по VIN: китайский отчёт (пробег, ДТП, сервисная история) с переводом на русский за 24 часа.\n\n"
    "5. Заявка на подбор: передаём проверенному агенту, он связывается с вами.\n\n"
    "6. Написать нам: вопрос уходит человеку, ответ придёт сюда же."
)


# ---------- старт и меню ----------

@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext, command: CommandObject) -> None:
    await state.clear()
    db.touch_user(m.from_user.id, m.from_user.username)
    if command.args == "vin":
        await vin_start(m, state)
        return
    if command.args == "lead":
        await lead_start(m, state)
        return
    if command.args == "calc":
        await calc_start(m, state)
        return
    text = (
        (f"Привет! Это бот {BRAND_NAME}. Я помогаю посчитать" if BRAND_NAME else "Привет! Я помогаю посчитать")
        + ", сколько реально стоит пригнать машину из Китая, "
        "проверить, пройдёт ли она по льготному утильсбору, и проверить её историю по VIN.\n\n"
        f"Правила расчёта актуальны на {RULES['version']}.\n"
        "Выберите действие:"
    )
    if WEB_URL:
        text += f"\n\nВеб-версия калькулятора: {WEB_URL}"
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
        HELP_TEXT + (f"\n\nМенеджер: {MANAGER_CONTACT}" if MANAGER_CONTACT else "") +
        "\n\nЛьготный утильсбор для физлица действует, если:\n• " + "\n• ".join(pref["conditions"]) +
        f"\n\n{RULES['disclaimer']}",
        reply_markup=MAIN_KB,
    )


# ---------- гид ----------

@router.message(F.text == BTN_GUIDE)
@router.message(Command("guide"))
async def cmd_guide(m: Message, state: FSMContext) -> None:
    db.touch_user(m.from_user.id, m.from_user.username)
    await state.clear()
    await m.answer("Гид по перегону машины из Китая. Что показать?", reply_markup=GUIDE_KB)


@router.callback_query(F.data.startswith("guide:"))
async def guide_section(c: CallbackQuery) -> None:
    await c.answer()
    key = c.data.split(":")[1]
    text = HELP_TEXT if key == "help" else GUIDE.get(key, "Раздел не найден.")
    await c.message.answer(text, reply_markup=GUIDE_KB)


# ---------- калькулятор ----------

@router.message(F.text == BTN_CALC)
@router.message(Command("calc"))
async def calc_start(m: Message, state: FSMContext) -> None:
    db.touch_user(m.from_user.id, m.from_user.username)
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


@router.callback_query(F.data == "mode:manual")
async def calc_mode_manual(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    await state.update_data(preset=None)
    await state.set_state(Calc.price)
    await c.message.answer("Цена машины в Китае в юанях, например 95000:", reply_markup=CANCEL_KB)


@router.callback_query(Calc.model, F.data.startswith("model:"))
async def calc_model_pick(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    mdl = MODELS_BY_ID.get(c.data.split(":")[1])
    if not mdl:
        await c.message.answer("Модель не найдена, введите характеристики вручную.", reply_markup=CANCEL_KB)
        await state.set_state(Calc.price)
        return
    await state.update_data(preset=mdl["id"], engine_cc=mdl["cc"], power_hp=mdl["hp"], fuel=mdl["fuel"])
    await state.set_state(Calc.price)
    note = f"\n⚠️ {mdl['note']}." if mdl.get("note") else ""
    await c.message.answer(
        f"{mdl['name']}: {mdl['cc']} см³, {mdl['hp']} л.с., {FUEL_LABELS[mdl['fuel']]}.{note}\n\n"
        "Цена машины в Китае в юанях, например 95000:",
        reply_markup=CANCEL_KB,
    )


@router.message(Calc.price)
async def calc_price(m: Message, state: FSMContext) -> None:
    v = _parse_number(m.text or "")
    if v is None or v > 10_000_000:
        await m.answer("Нужно число в юанях, например 95000.")
        return
    await state.update_data(price_cny=v)
    await state.set_state(Calc.made)
    await m.answer("Год и месяц выпуска, например 2024-03 (можно только год):")


@router.message(Calc.made)
async def calc_made(m: Message, state: FSMContext) -> None:
    t = (m.text or "").strip()
    mt = re.match(r"^(\d{4})(?:[-./ ](\d{1,2}))?$", t)
    if not mt:
        await m.answer("Формат: 2024-03 или просто 2024.")
        return
    year = int(mt.group(1))
    month = int(mt.group(2)) if mt.group(2) else 7
    if not (1990 <= year <= date.today().year) or not (1 <= month <= 12):
        await m.answer("Проверьте год и месяц.")
        return
    made = date(year, month, 1)
    age = age_category_from_date(made)
    note = "" if mt.group(2) else " (месяц не указан, взял середину года, на границе 3 и 5 лет это важно)"
    await state.update_data(age=age, made=made.isoformat(), made_exact=bool(mt.group(2)))
    data = await state.get_data()
    if data.get("preset"):
        await state.set_state(Calc.route)
        await m.answer(f"Возраст: {AGE_LABELS[age]}{note}.\nМаршрут доставки:", reply_markup=ROUTE_KB)
        return
    await state.set_state(Calc.cc)
    await m.answer(f"Возраст: {AGE_LABELS[age]}{note}.\nОбъём двигателя в см³ (для электромобиля 0):")


@router.message(Calc.cc)
async def calc_cc(m: Message, state: FSMContext) -> None:
    cc = _parse_cc(m.text or "")
    if cc is None:
        await m.answer("Нужно число, например 1498. Если указано в литрах, напишите 1.5. Для электромобиля 0.")
        return
    await state.update_data(engine_cc=cc)
    await state.set_state(Calc.hp)
    await m.answer("Мощность в л.с. (для гибрида и электромобиля смотрите подсказку после выбора типа):")


@router.message(Calc.hp)
async def calc_hp(m: Message, state: FSMContext) -> None:
    v = _parse_number(m.text or "")
    if v is None or v > 2000:
        await m.answer("Нужно число, например 147.")
        return
    await state.update_data(power_hp=int(round(v)))
    await state.set_state(Calc.fuel)
    await m.answer("Тип двигателя:", reply_markup=FUEL_KB)


@router.callback_query(Calc.fuel, F.data.startswith("fuel:"))
async def calc_fuel(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    await state.update_data(fuel=c.data.split(":")[1])
    await state.set_state(Calc.route)
    await c.message.edit_reply_markup(reply_markup=None)
    await c.message.answer("Маршрут доставки:", reply_markup=ROUTE_KB)


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
    alt = calculate(CarInput(**{**car.__dict__, "age": "3-5"}), RULES)
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
    res = calculate(car, RULES)
    saved = {k: data.get(k) for k in ("price_cny", "age", "engine_cc", "power_hp", "fuel", "route", "destination", "made", "made_exact", "preset")}
    db.add_calc(c.from_user.id, saved, res.total_mid)
    await state.clear()
    head = ""
    if data.get("preset") and data["preset"] in MODELS_BY_ID:
        head = f"{MODELS_BY_ID[data['preset']]['name']}\n"
    text = head + render_text(res, car) + _three_year_advice(data, car, res.total_mid)
    text += f"\n\nКурс: {rates_src}.\n{RULES['disclaimer']}"
    await c.message.answer(text, reply_markup=MAIN_KB)
    await c.message.answer("Что дальше?", reply_markup=AFTER_CALC_KB)


@router.callback_query(F.data == "go:routes")
async def compare_routes(c: CallbackQuery) -> None:
    await c.answer()
    last = db.last_calc(c.from_user.id)
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
        r = calculate(car, RULES)
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
    db.touch_user(m.from_user.id, m.from_user.username)
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
    db.touch_user(m.from_user.id, m.from_user.username)
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
    price = f"{VIN_PRICE_STARS} ⭐" if VIN_PRICE_STARS > 0 else "по договорённости"
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
    order_id = db.add_order(m.from_user.id, m.from_user.username, data["vin"], contact, VIN_PRICE_STARS)
    await notify_admins(
        bot,
        f"🔎 Заказ VIN #{order_id}\nVIN: {data['vin']}\nКонтакт: {contact}\n"
        f"От: @{m.from_user.username or '-'} (id {m.from_user.id})\n"
        f"Статус: {'ждём оплату' if VIN_PRICE_STARS > 0 else 'новый'}\n"
        f"Готовый отчёт: прикрепите файл с подписью /send {order_id}",
    )
    if VIN_PRICE_STARS > 0:
        await m.answer(f"Заказ #{order_id} создан. Оплатите, и мы начнём проверку.", reply_markup=MAIN_KB)
        await m.answer_invoice(
            title=f"Отчёт по VIN {data['vin']}",
            description="История автомобиля из китайских баз с переводом на русский. Срок до 24 часов.",
            payload=f"vin:{order_id}",
            currency="XTR",
            prices=[LabeledPrice(label="Отчёт по VIN", amount=VIN_PRICE_STARS)],
        )
    else:
        await m.answer(f"Заказ #{order_id} принят. {PAYMENT_INSTRUCTIONS}", reply_markup=MAIN_KB)


@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery) -> None:
    await q.answer(ok=True)


@router.message(F.successful_payment)
async def paid(m: Message, bot: Bot) -> None:
    sp = m.successful_payment
    payload = sp.invoice_payload or ""
    if payload.startswith("vin:"):
        order_id = int(payload.split(":")[1])
        db.mark_paid(order_id, sp.telegram_payment_charge_id)
        order = db.get_order(order_id)
        await m.answer(f"Оплата получена, заказ #{order_id} в работе. Отчёт пришлём сюда в течение 24 часов.", reply_markup=MAIN_KB)
        await notify_admins(bot, f"💰 Оплачен заказ VIN #{order_id}: {order['vin']}, контакт {order['contact']}")


# ---------- заявка на подбор ----------

@router.message(F.text == BTN_LEAD)
@router.message(Command("lead"))
async def lead_start(m: Message, state: FSMContext) -> None:
    db.touch_user(m.from_user.id, m.from_user.username)
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
    await state.set_state(Lead.contact)
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Отправить мой номер", request_contact=True)], [KeyboardButton(text="Отмена")]],
        resize_keyboard=True,
    )
    await m.answer("Телефон или @username для связи. Оставляя контакт, вы соглашаетесь на передачу его агенту по подбору:", reply_markup=kb)


@router.message(Lead.contact)
async def lead_contact(m: Message, state: FSMContext, bot: Bot) -> None:
    contact = m.contact.phone_number if m.contact else (m.text or "").strip()
    if not contact:
        await m.answer("Напишите телефон или @username.")
        return
    d = await state.get_data()
    await state.clear()
    lead_id = db.add_lead(m.from_user.id, m.from_user.username, d["model"], d["budget"], d["city"], contact)
    await notify_admins(
        bot,
        f"🚗 Заявка #{lead_id}\nМашина: {d['model']}\nБюджет: {d['budget']}\nГород: {d['city']}\n"
        f"Контакт: {contact}\nОт: @{m.from_user.username or '-'} (id {m.from_user.id})",
    )
    await m.answer(f"Заявка #{lead_id} принята. Агент свяжется с вами в течение рабочего дня.", reply_markup=MAIN_KB)


# ---------- связь с нами ----------

@router.message(F.text == BTN_SUPPORT)
@router.message(Command("support"))
async def support_start(m: Message, state: FSMContext) -> None:
    db.touch_user(m.from_user.id, m.from_user.username)
    await state.clear()
    await state.set_state(Support.msg)
    direct = f"\nИли напишите менеджеру напрямую: {MANAGER_CONTACT}" if MANAGER_CONTACT else ""
    await m.answer(f"Напишите вопрос одним сообщением, можно с фото. Ответ придёт сюда же.{direct}", reply_markup=CANCEL_KB)


@router.message(Support.msg)
async def support_message(m: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    delivered = 0
    for admin in ADMIN_IDS:
        try:
            await bot.send_message(admin, f"✍️ Вопрос от @{m.from_user.username or '-'} (id {m.from_user.id}). Ответьте реплаем на следующее сообщение.")
            fwd = await bot.forward_message(admin, m.chat.id, m.message_id)
            db.add_support(admin, fwd.message_id, m.from_user.id)
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
        and db.support_user(m.from_user.id, m.reply_to_message.message_id) is not None
    )


@router.message(_is_support_reply)
async def support_reply(m: Message, bot: Bot) -> None:
    user_id = db.support_user(m.from_user.id, m.reply_to_message.message_id)
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
    rows = db.list_orders()
    if not rows:
        await m.answer("Открытых заказов нет.")
        return
    await m.answer("\n".join(f"#{r['id']} {r['status']} {r['vin']} {r['contact']} {r['created_at'][:16]}" for r in rows))


@router.message(Command("leads"))
async def admin_leads(m: Message) -> None:
    if not _is_admin(m):
        return
    rows = db.list_leads()
    if not rows:
        await m.answer("Заявок нет.")
        return
    await m.answer("\n\n".join(f"#{r['id']} {r['model']} | {r['budget']} | {r['city']} | {r['contact']}" for r in rows))


@router.message(Command("done"))
async def admin_done(m: Message, bot: Bot) -> None:
    if not _is_admin(m):
        return
    parts = (m.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await m.answer("Использование: /done <номер заказа>")
        return
    order = db.get_order(int(parts[1]))
    if not order or not db.mark_done(order["id"]):
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
    order = db.get_order(order_id)
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
    db.mark_done(order_id)
    await m.answer(f"Отчёт отправлен, заказ #{order_id} закрыт.")


@router.message(Command("stats"))
async def admin_stats(m: Message) -> None:
    if not _is_admin(m):
        return
    s = db.stats()
    await m.answer(
        f"Пользователей: {s['users']}\nРасчётов: {s['calcs']} (за 7 дней {s['calcs_7d']})\n"
        f"Заказов VIN: {s['orders']} (оплачено {s['orders_paid']})\nЗаявок: {s['leads']}"
    )


@router.message(Command("export"))
async def admin_export(m: Message) -> None:
    if not _is_admin(m):
        return
    data = db.export_csv()
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
    n = len(db.all_user_ids())
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
    for uid in db.all_user_ids():
        try:
            await bot.send_message(uid, d["text"])
            sent += 1
        except Exception:  # noqa: BLE001
            failed += 1
        await asyncio.sleep(0.05)
    await c.message.answer(f"Разослано: {sent}, не доставлено: {failed}.")


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


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not BOT_TOKEN:
        while True:
            log.error("BOT_TOKEN не задан. Впишите токен от @BotFather в .env (на сервере: секрет DEPLOY_ENV_FILE) и перезапустите.")
            await asyncio.sleep(3600)
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    log.info("rules version %s, models %d, admins %s, vin price %s stars", RULES["version"], len(MODELS), sorted(ADMIN_IDS), VIN_PRICE_STARS)
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await bot.set_my_commands(COMMANDS)
    except Exception as e:  # noqa: BLE001
        log.warning("set_my_commands failed: %s", e)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
