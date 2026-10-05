"""Логика расчёта. Чистые функции, без сети и без состояния.

Все ставки берутся из data/rules.json. Любое число, которого нет в rules.json,
здесь появляться не должно.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

RULES_PATH = Path(__file__).resolve().parent.parent / "data" / "rules.json"

AgeCategory = Literal["new", "3-5", "5+"]
Fuel = Literal["ice", "hybrid_par", "hybrid_seq", "ev"]

FUEL_LABELS = {
    "ice": "бензин / дизель",
    "hybrid_par": "параллельный гибрид (считается как ДВС)",
    "hybrid_seq": "последовательный гибрид (считается как электромобиль)",
    "ev": "электромобиль",
}

AGE_LABELS = {"new": "до 3 лет", "3-5": "от 3 до 5 лет", "5+": "старше 5 лет"}


def load_rules(path: Path | str = RULES_PATH) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def age_category_from_date(made: date, today: date | None = None) -> AgeCategory:
    """Категория возраста по дате выпуска. Границы: ровно 3 года и ровно 5 лет."""
    today = today or date.today()
    years = today.year - made.year - ((today.month, today.day) < (made.month, made.day))
    if years < 3:
        return "new"
    if years < 5:
        return "3-5"
    return "5+"


@dataclass
class CarInput:
    price_cny: float
    age: AgeCategory
    engine_cc: int
    power_hp: int
    fuel: Fuel = "ice"
    route: str = "suifenhe"
    destination: str = "moscow"
    cny_rub: float | None = None
    eur_rub: float | None = None


@dataclass
class Line:
    key: str
    label: str
    mid: float | None
    low: float | None = None
    high: float | None = None
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "mid": self.mid,
            "low": self.low,
            "high": self.high,
            "note": self.note,
        }


@dataclass
class Result:
    lines: list[Line] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unverified: bool = False
    total_mid: float | None = None
    total_low: float | None = None
    total_high: float | None = None
    rates: dict[str, float] = field(default_factory=dict)
    rules_version: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "lines": [ln.as_dict() for ln in self.lines],
            "warnings": self.warnings,
            "unverified": self.unverified,
            "total_mid": self.total_mid,
            "total_low": self.total_low,
            "total_high": self.total_high,
            "rates": self.rates,
            "rules_version": self.rules_version,
        }


def _bracket(brackets: list[list[Any]], value: float) -> list[Any]:
    """Первый bracket, у которого первый элемент (верхняя граница, включительно) >= value."""
    for b in brackets:
        if value <= b[0]:
            return b
    return brackets[-1]


def _volume_key(cc: int) -> str:
    if cc <= 1000:
        return "0-1000"
    if cc <= 2000:
        return "1000-2000"
    if cc <= 3000:
        return "2000-3000"
    if cc <= 3500:
        return "3000-3500"
    return "3500+"


def _is_ev_scheme(fuel: str) -> bool:
    return fuel in ("ev", "hybrid_seq")


# ---------- утильсбор ----------

def util_fee(rules: dict[str, Any], car: CarInput) -> tuple[float | None, str, bool]:
    """Возвращает (сумма, пояснение, unverified)."""
    u = rules["util"]
    pref = u["preferential"]
    ev = _is_ev_scheme(car.fuel)
    max_hp = pref["max_hp_ev"] if ev else pref["max_hp_ice"]
    cc_ok = ev or car.engine_cc <= pref["max_cc"]
    is_new = car.age == "new"

    if car.power_hp <= max_hp and cc_ok:
        amount = pref["new_rub"] if is_new else pref["old_rub"]
        return float(amount), "льготная ставка для физлица", False

    if ev:
        table = u["commercial"]["ev"]["brackets"]
    else:
        vol = u["commercial"]["ice"][_volume_key(car.engine_cc)]
        table = vol.get("brackets")
        if table is None:
            return None, vol.get("note", "ставка не подтверждена"), True

    b = _bracket(table, car.power_hp)
    amount = b[1] if is_new else b[2]
    if amount is None:
        return None, "для этой мощности ставка не подтверждена, уточните у брокера", True
    if car.power_hp > table[-1][0]:
        return float(amount), "коммерческая ставка, верхняя граница таблицы", True
    return float(amount), "коммерческая ставка (мощность выше льготного порога)", False


# ---------- таможенные платежи ----------

def customs_payments(rules: dict[str, Any], car: CarInput, price_rub: float, eur_rub: float) -> list[Line]:
    lines: list[Line] = []
    if _is_ev_scheme(car.fuel):
        s = rules["ev_scheme"]
        duty = price_rub * s["duty_pct"] / 100
        per_hp = _bracket(s["excise_rub_per_hp"]["brackets"], car.power_hp)[1]
        excise = per_hp * car.power_hp
        vat = (price_rub + duty + excise) * s["vat_pct"] / 100
        lines.append(Line("duty", f"Пошлина {s['duty_pct']}%", round(duty)))
        lines.append(Line("excise", f"Акциз ({per_hp} ₽ за л.с.)", round(excise)))
        lines.append(Line("vat", f"НДС {s['vat_pct']}%", round(vat)))
        return lines

    d = rules["customs_duty_individual"]
    price_eur = price_rub / eur_rub
    if car.age == "new":
        b = _bracket(d["new_under_3y"]["brackets"], price_eur)
        by_pct = price_eur * b[1] / 100
        by_cc = car.engine_cc * b[2]
        duty_eur = max(by_pct, by_cc)
        how = f"{b[1]:g}% от стоимости, но не менее {b[2]:g} € за см³"
    else:
        key = "age_3_to_5" if car.age == "3-5" else "age_over_5"
        b = _bracket(d[key]["brackets"], car.engine_cc)
        duty_eur = car.engine_cc * b[1]
        how = f"{b[1]:g} € за см³"
    lines.append(Line("duty", "Таможенная пошлина (единая ставка для физлиц)", round(duty_eur * eur_rub), note=how))
    return lines


def customs_fee(rules: dict[str, Any], price_rub: float) -> float:
    return float(_bracket(rules["customs_fee"]["brackets"], price_rub)[1])


# ---------- основной расчёт ----------

def calculate(car: CarInput, rules: dict[str, Any] | None = None, extras: list[tuple[str, float]] | None = None) -> Result:
    """Полный расчёт под ключ.

    extras: дополнительные строки компании, которая считает для клиента (например, её комиссия или услуги),
    пары (название, сумма в рублях). Добавляются в конец и входят в итог.
    """
    rules = rules or load_rules()
    fb = rules["fallback_rates"]
    cny_rub = car.cny_rub or fb["CNY"]
    eur_rub = car.eur_rub or fb["EUR"]

    res = Result(rates={"CNY": cny_rub, "EUR": eur_rub}, rules_version=rules["version"])
    costs = rules["costs"]

    price_rub = car.price_cny * cny_rub
    res.lines.append(Line("price", f"Цена автомобиля ({car.price_cny:,.0f} ¥ × {cny_rub:.2f})", round(price_rub)))

    pa = costs["payment_agent_pct"]
    res.lines.append(Line("payment_agent", pa["label"],
                          round(price_rub * pa["mid"] / 100),
                          round(price_rub * pa["low"] / 100),
                          round(price_rub * pa["high"] / 100)))

    cs = costs["china_side"]
    res.lines.append(Line("china_side", cs["label"], cs["mid"], cs["low"], cs["high"]))

    lg = costs["logistics"].get(car.route) or costs["logistics"]["suifenhe"]
    res.lines.append(Line("logistics", lg["label"], lg["mid"], lg["low"], lg["high"], note=f"{lg['days']} дней"))

    res.lines.extend(customs_payments(rules, car, price_rub, eur_rub))
    res.lines.append(Line("customs_fee", "Таможенный сбор за оформление", customs_fee(rules, price_rub)))

    util, util_note, unverified = util_fee(rules, car)
    res.lines.append(Line("util", "Утилизационный сбор", util, note=util_note))
    res.unverified = res.unverified or unverified

    for key in ("broker", "svh", "sbkts_glonass", "epts_registration"):
        c = costs[key]
        res.lines.append(Line(key, c["label"], c["mid"], c["low"], c["high"]))

    dm = costs["domestic"].get(car.destination) or costs["domestic"]["moscow"]
    res.lines.append(Line("domestic", f"Доставка по России: {dm['label']}", dm["mid"], dm["low"], dm["high"]))
    for i, (label, amount) in enumerate(extras or []):
        if amount:
            res.lines.append(Line(f"extra{i}", str(label), round(float(amount))))

    # предупреждения
    pref = rules["util"]["preferential"]
    ev = _is_ev_scheme(car.fuel)
    max_hp = pref["max_hp_ev"] if ev else pref["max_hp_ice"]
    if car.power_hp > max_hp:
        res.warnings.append(
            f"Мощность {car.power_hp} л.с. выше порога {max_hp} л.с.: льготный утильсбор не действует, "
            "применяется коммерческая ставка. Это главная статья расходов, перепроверьте мощность по документам."
        )
    if not ev and car.engine_cc > pref["max_cc"]:
        res.warnings.append(f"Объём {car.engine_cc} см³ больше {pref['max_cc']} см³: льгота по утильсбору не действует.")
    if car.power_hp <= max_hp and (ev or car.engine_cc <= pref["max_cc"]):
        res.warnings.append("Льгота действует только при ввозе одного автомобиля в год для себя и без продажи 12 месяцев.")
    if ev:
        res.warnings.append("Для электромобилей и последовательных гибридов мощность считается по 30-минутной мощности электромоторов.")
    if car.fuel == "hybrid_par":
        res.warnings.append("Параллельный гибрид считается как ДВС, мощность берётся суммарная (ДВС + электромотор).")
    if res.unverified:
        res.warnings.append("В расчёте есть неподтверждённая ставка. Итог без неё неполный.")

    def _sum(attr: str) -> float | None:
        total = 0.0
        for ln in res.lines:
            v = getattr(ln, attr)
            if v is None:
                v = ln.mid
            if v is None:
                continue
            total += v
        return round(total)

    res.total_mid = _sum("mid")
    res.total_low = _sum("low")
    res.total_high = _sum("high")
    return res


def format_rub(v: float | None) -> str:
    if v is None:
        return "не подтверждено"
    return f"{v:,.0f} ₽".replace(",", " ")


def render_text(res: Result, car: CarInput) -> str:
    """Текстовое представление для бота."""
    head = (
        f"Расчёт «под ключ», правила от {res.rules_version}\n"
        f"Возраст: {AGE_LABELS[car.age]}, {car.engine_cc} см³, {car.power_hp} л.с., {FUEL_LABELS[car.fuel]}\n"
        f"Курс: ¥ {res.rates['CNY']:.2f} ₽, € {res.rates['EUR']:.2f} ₽\n"
    )
    body = []
    for ln in res.lines:
        s = f"• {ln.label}: {format_rub(ln.mid)}"
        if ln.low is not None and ln.high is not None and ln.low != ln.high:
            s += f" (вилка {format_rub(ln.low)} – {format_rub(ln.high)})"
        if ln.note:
            s += f" — {ln.note}"
        body.append(s)
    total = f"\nИТОГО ориентировочно: {format_rub(res.total_mid)}"
    if res.total_low != res.total_high:
        total += f"\nВилка: {format_rub(res.total_low)} – {format_rub(res.total_high)}"
    warn = ""
    if res.warnings:
        warn = "\n\n⚠️ " + "\n⚠️ ".join(res.warnings)
    return head + "\n" + "\n".join(body) + total + warn
