"""Telegram-бот: калькулятор «под ключ», заказ проверки по VIN, заявка «хочу пригнать».

Запуск: python -m bot.main  (переменные окружения см. в .env.example)
"""

from __future__ import annotations

import asyncio
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
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from calc.engine import AGE_LABELS, CarInput, age_category_from_date, calculate, load_rules, render_text
from calc.rates import get_rates

from .db import DB

try:  # .env необязателен
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

log = logging.getLogger("bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}
VIN_PRICE_STARS = int(os.environ.get("VIN_PRICE_STARS", "0"))
PAYMENT_INSTRUCTIONS = os.environ.get(
    "PAYMENT_INSTRUCTIONS",
    "Мы свяжемся с вами для оплаты и пришлём отчёт в течение 24 часов.",
)
DB_PATH = os.environ.get("DB_PATH", str(Path(__file__).resolve().parent.parent / "data" / "bot.sqlite3"))
WEB_URL = os.environ.get("WEB_URL", "")

RULES = load_rules()
db = DB(DB_PATH)
router = Router()

BTN_CALC = "🧮 Рассчитать под ключ"
BTN_VIN = "🔎 Проверить VIN"
BTN_LEAD = "🚗 Хочу пригнать"
BTN_HELP = "ℹ️ Как это работает"

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_CALC), KeyboardButton(text=BTN_VIN)],
              [KeyboardButton(text=BTN_LEAD), KeyboardButton(text=BTN_HELP)]],
    resize_keyboard=True,
)
CANCEL_KB = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отмена")]], resize_keyboard=True)

VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


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
AFTER_CALC_KB = _ikb([
    [("🔎 Проверить эту машину по VIN", "go:vin")],
    [("🚗 Оставить заявку на подбор", "go:lead")],
    [("🔁 Пересчитать", "go:calc")],
])


class Calc(StatesGroup):
    price = State()
    made = State()
    cc = State()
    hp = State()
    fuel = State()
    route = State()
    dest = State()


class Vin(StatesGroup):
    vin = State()
    contact = State()


class Lead(StatesGroup):
    model = State()
    budget = State()
    city = State()
    contact = State()


async def notify_admins(bot: Bot, text: str) -> None:
    for admin in ADMIN_IDS:
        try:
            await bot.send_message(admin, text)
        except Exception as e:  # noqa: BLE001
            log.warning("admin notify failed for %s: %s", admin, e)


# ---------- старт и меню ----------

@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext, command: CommandObject) -> None:
    await state.clear()
    db.touch_user(m.from_user.id, m.from_user.username)
    # deep links с сайта: t.me/<bot>?start=vin / ?start=lead
    if command.args == "vin":
        await vin_start(m, state)
        return
    if command.args == "lead":
        await lead_start(m, state)
        return
    text = (
        "Привет! Я помогаю посчитать, сколько реально стоит пригнать машину из Китая, "
        "и проверить её историю по VIN.\n\n"
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
        "Что умеет бот:\n\n"
        "1. Калькулятор «под ключ»: цена в Китае, перевод денег, расходы в Китае, логистика, пошлина, "
        "утильсбор, СБКТС, ЭПТС и доставка по России.\n\n"
        "2. Проверка по VIN: заказываем китайский отчёт (пробег, ДТП, сервисная история), переводим и "
        "присылаем на русском в течение 24 часов.\n\n"
        "3. Заявка на подбор: передаём проверенному агенту, он связывается с вами.\n\n"
        "Льготный утильсбор для физлица действует, если:\n• " + "\n• ".join(pref["conditions"]) +
        f"\n\n{RULES['disclaimer']}",
        reply_markup=MAIN_KB,
    )


# ---------- калькулятор ----------

@router.message(F.text == BTN_CALC)
@router.message(Command("calc"))
async def calc_start(m: Message, state: FSMContext) -> None:
    db.touch_user(m.from_user.id, m.from_user.username)
    await state.clear()
    await state.set_state(Calc.price)
    await m.answer("Цена машины в Китае в юанях, например 95000:", reply_markup=CANCEL_KB)


@router.callback_query(F.data == "go:calc")
async def calc_restart(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    await state.clear()
    await state.set_state(Calc.price)
    await c.message.answer("Цена машины в Китае в юанях:", reply_markup=CANCEL_KB)


def _parse_number(text: str) -> float | None:
    t = text.replace(" ", "").replace(",", ".").replace("¥", "").replace("k", "000").replace("к", "000")
    try:
        v = float(t)
    except ValueError:
        return None
    return v if v > 0 else None


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
    await state.update_data(age=age, made=made.isoformat())
    await state.set_state(Calc.cc)
    await m.answer(f"Возраст: {AGE_LABELS[age]}{note}.\nОбъём двигателя в см³ (для электромобиля 0):")


@router.message(Calc.cc)
async def calc_cc(m: Message, state: FSMContext) -> None:
    t = (m.text or "").replace(" ", "").replace(",", ".")
    try:
        v = float(t)
    except ValueError:
        await m.answer("Нужно число, например 1498. Если указано в литрах, напишите 1.5.")
        return
    cc = int(round(v * 1000)) if v < 20 else int(round(v))
    if cc < 0 or cc > 8000:
        await m.answer("Проверьте объём.")
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
    db.add_calc(c.from_user.id, {k: data[k] for k in ("price_cny", "age", "engine_cc", "power_hp", "fuel", "route", "destination")}, res.total_mid)
    await state.clear()
    text = render_text(res, car) + f"\n\nКурс: {rates_src}.\n{RULES['disclaimer']}"
    await c.message.answer(text, reply_markup=MAIN_KB)
    await c.message.answer("Что дальше?", reply_markup=AFTER_CALC_KB)


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
        f"Статус: {'ждём оплату' if VIN_PRICE_STARS > 0 else 'новый'}",
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


# ---------- админ ----------

def _is_admin(m: Message) -> bool:
    return m.from_user is not None and m.from_user.id in ADMIN_IDS


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


@router.message()
async def fallback(m: Message) -> None:
    await m.answer("Выберите действие на клавиатуре или отправьте /start.", reply_markup=MAIN_KB)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан. Скопируйте .env.example в .env и заполните.")
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    log.info("rules version %s, admins %s, vin price %s stars", RULES["version"], sorted(ADMIN_IDS), VIN_PRICE_STARS)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
