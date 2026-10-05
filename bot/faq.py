"""Ответы на вопросы своими словами без ИИ.

Ключевые слова ведут к разделу гида или пояснению из texts.py, а упомянутая модель из списка
получает карточку с вердиктом по льготе и кнопкой расчёта. Работает всегда: и пока ключа ИИ нет,
и как запасной вариант, если ИИ недоступен или исчерпан дневной лимит.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .texts import EXPLAIN, GUIDE

BTN_CALC = ("🧮 Рассчитать под ключ", "go:calc")
BTN_CHECK = ("✅ Проверить льготу", "go:check")
BTN_VIN = ("🔎 Проверить VIN", "go:vin")
BTN_LEAD = ("🚗 Заявка на подбор", "go:lead")


@dataclass
class FaqAnswer:
    text: str
    buttons: list[tuple[str, str]] = field(default_factory=list)
    topic: str | None = None
    model_id: str | None = None


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().replace("ё", "е")).strip()


# Порядок важен: при равенстве совпадений побеждает тема выше. Общий «сколько стоит» стоит последним,
# чтобы «сколько стоит растаможка» ушло в пошлину, а не в общий расчёт.
TOPICS: list[tuple[str, list[str], str, list[tuple[str, str]]]] = [
    (
        "util",
        [r"утил", r"льгот", r"\b160\b", r"лошад", r"мощност", r"\bл\.? ?с\b"],
        GUIDE["util"],
        [BTN_CHECK, BTN_CALC],
    ),
    (
        "ev",
        [r"электр", r"гибрид", r"\b(erev|phev|ev|bev)\b", r"акциз", r"батаре", r"запас хода"],
        "Электромобили и гибриды\n\n"
        "Электромобиль и последовательный гибрид (EREV) считаются по отдельной схеме: пошлина от стоимости, "
        "затем акциз и НДС.\n"
        f"• Акциз. {EXPLAIN['excise']}\n"
        f"• НДС. {EXPLAIN['vat']}\n"
        "• Льготный утильсбор только при 30-минутной мощности не более 80 л.с. Пиковая мощность в рекламе "
        "обычно выше, смотрите документы.\n\n"
        "Параллельный гибрид (PHEV, DM-i) считается как бензиновая машина по суммарной мощности двигателя "
        "и электромотора: порог льготы 160 л.с.",
        [BTN_CALC, BTN_CHECK],
    ),
    (
        "duty",
        [r"пошлин", r"растамож", r"таможн", r"\b(3|три|трех|3-?х)\s*(год|лет)", r"старше", r"\b5 лет"],
        "Таможенная пошлина\n\n"
        f"{EXPLAIN['duty']}\n\n"
        "Если машине скоро исполнится 3 года, бот в расчёте сам покажет, сколько можно сэкономить, если подождать.",
        [BTN_CALC],
    ),
    (
        "docs",
        [r"документ", r"бумаг", r"\bэ?птс\b", r"сбктс", r"глонасс", r"\bучет", r"гибдд", r"регистрац"],
        GUIDE["docs"] + f"\n\nСБКТС и ЭРА-ГЛОНАСС. {EXPLAIN['sbkts_glonass']}",
        [("📋 Шаги перегона", "guide:steps"), ("🚚 Сроки", "guide:time")],
    ),
    (
        "time",
        [
            r"\bсрок", r"долго", r"сколько (ехать|идет|едет|везут|ждать)", r"маршрут", r"логист", r"доставк",
            r"суйфэн|суйфун", r"хоргос", r"забайкал", r"маньчжур", r"владивосток", r"уссурийск", r"\bморем",
            r"автовоз", r"недел",
        ],
        GUIDE["time"] + "\n\nСравнить стоимость маршрутов для своей машины можно сразу после расчёта.",
        [BTN_CALC],
    ),
    (
        "mistakes",
        [r"ошибк", r"обман", r"мошен", r"кинул", r"развод", r"риск", r"безопасн", r"подвох", r"на что обратить"],
        GUIDE["mistakes"],
        [BTN_VIN, BTN_LEAD],
    ),
    (
        "payment",
        [r"оплат", r"заплат", r"перев(од|ести) (ден|юан)", r"\bюан", r"платеж", r"\bswift\b", r"агент"],
        "Оплата в Китай\n\n"
        f"{EXPLAIN['payment_agent']}\n\n"
        "Никогда не платите на личную карту продавца: только через экспортную компанию или проверенного агента.",
        [BTN_LEAD],
    ),
    (
        "broker",
        [r"брокер", r"\bсвх\b", r"склад"],
        f"Брокер и СВХ\n\n• Брокер. {EXPLAIN['broker']}\n• СВХ. {EXPLAIN['svh']}",
        [BTN_CALC],
    ),
    (
        "steps",
        [
            r"как (пригнать|привезти|купить|заказать|ввезти)", r"с чего начать", r"\bэтап", r"\bшаг", r"процесс",
            r"пошагов",
        ],
        GUIDE["steps"],
        [("📄 Документы", "guide:docs"), ("🚚 Сроки", "guide:time")],
    ),
    (
        "vin",
        [r"\bvin\b", r"\bвин\b", r"вин-?код", r"истори", r"пробег", r"\bдтп\b", r"скрут"],
        "Проверка по VIN\n\n"
        "По VIN проверяю историю машины из Китая: пробег, ДТП, залог, работа в такси. "
        "Попросите у продавца VIN из 17 символов и фото документов ещё до предоплаты.",
        [BTN_VIN],
    ),
    (
        "lead",
        [r"подбор", r"подобрать", r"заявк", r"менеджер", r"помогите (купить|найти|пригнать)", r"под заказ", r"найти машин"],
        "Подбор машины\n\n"
        "Оставьте заявку: какая машина нужна, бюджет и город. Менеджер свяжется с вами и предложит варианты.",
        [BTN_LEAD],
    ),
    (
        "calc",
        [
            r"сколько (стоит|будет|выйдет|обойдется|получится)", r"\bцен", r"стоимост", r"посчита", r"рассчита",
            r"расчет", r"под ключ", r"во сколько",
        ],
        "Расчёт под ключ\n\n"
        "Посчитаю пошлину, утильсбор, логистику, СБКТС, ЭПТС и доставку до города. Нужны цена в юанях, год выпуска, "
        "объём, мощность и тип двигателя. Для популярных моделей характеристики уже есть в списке.",
        [BTN_CALC],
    ),
]

_TOPIC_RE = [(key, [re.compile(p) for p in pats], body, buttons) for key, pats, body, buttons in TOPICS]

# Как модели пишут в вопросах: латиница и кириллица. Пробел в шаблоне означает «пробел, дефис или ничего».
MODEL_ALIASES: dict[str, list[str]] = {
    "coolray": ["coolray", "кулрей", "кулрэй"],
    "emgrand": ["emgrand", "эмгранд"],
    "monjaro": ["monjaro", "монжаро", "манжаро"],
    "boyue_l": ["boyue", "atlas", "атлас", "боюэ"],
    "tiggo4": ["tiggo 4", "тигго 4", "тиго 4"],
    "omoda_c5": ["omoda", "омода"],
    "tiggo7": ["tiggo 7", "тигго 7", "тиго 7"],
    "tiggo8": ["tiggo 8", "тигго 8", "тиго 8"],
    "jolion": ["jolion", "джолион", "жолион"],
    "h6": ["haval h6", "хавал h6", "хавейл h6", "h6"],
    "dargo": ["dargo", "дарго"],
    "tank300": ["tank 300", "танк 300"],
    "cs75": ["cs 75", "цс 75"],
    "unik": ["uni k", "юни к", "юник"],
    "camry": ["camry", "камри"],
    "highlander": ["highlander", "хайлендер", "хайлэндер"],
    "crv": ["cr v", "срв"],
    "xtrail": ["x trail", "икс трейл", "х трейл"],
    "tiguan": ["tiguan", "тигуан"],
    "x3": ["bmw x3", "бмв х3", "бмв x3", "x3"],
    "li_l7": ["li l7", "lixiang l7", "лисян l7", "лисян л7", "ли л7", "l7"],
    "zeekr001": ["zeekr 001", "зикр 001", "001"],
    "byd_seal": ["byd seal", "бид сил", "seal"],
    "model_y": ["model y", "модель y", "модель у", "tesla", "тесла"],
}


def _alias_re(alias: str) -> re.Pattern[str]:
    parts = [re.escape(p) for p in alias.split(" ")]
    return re.compile(r"(?<![a-zа-я0-9])" + r"[\s\-]*".join(parts) + r"(?![a-zа-я0-9])")


_MODEL_RE = [(mid, [_alias_re(a) for a in aliases]) for mid, aliases in MODEL_ALIASES.items()]


def find_model(text: str) -> str | None:
    t = norm(text)
    for mid, pats in _MODEL_RE:
        if any(p.search(t) for p in pats):
            return mid
    return None


def find_topic(text: str) -> str | None:
    t = norm(text)
    best, best_score = None, 0
    for key, pats, _body, _buttons in _TOPIC_RE:
        score = sum(1 for p in pats if p.search(t))
        if score > best_score:
            best, best_score = key, score
    return best


def model_card(mdl: dict, pref: dict, fuel_labels: dict[str, str]) -> str:
    """Короткая справка по модели из списка и вердикт по льготному утильсбору по типовым данным."""
    ev = mdl["fuel"] in ("ev", "hybrid_seq")
    max_hp = pref["max_hp_ev"] if ev else pref["max_hp_ice"]
    cc = f"{mdl['cc']} см³, " if mdl.get("cc") else ""
    lines = [f"{mdl['name']}: {cc}{mdl['hp']} л.с., {fuel_labels.get(mdl['fuel'], mdl['fuel'])}."]
    if ev:
        lines.append(
            f"Для льготного утильсбора важна 30-минутная мощность не более {max_hp} л.с. "
            "Сверьте её по документам: в рекламе обычно указана пиковая."
        )
    elif mdl["hp"] <= max_hp and mdl["cc"] <= pref["max_cc"]:
        lines.append(f"По типовой мощности проходит под льготный утильсбор: до {max_hp} л.с.")
    else:
        lines.append(f"Мощность выше порога {max_hp} л.с.: льготного утильсбора не будет, утильсбор коммерческий.")
    if mdl.get("note"):
        lines.append(f"⚠️ {mdl['note']}.")
    return "\n".join(lines)


def answer(text: str, models_by_id: dict[str, dict], pref: dict, fuel_labels: dict[str, str]) -> FaqAnswer | None:
    """Ответ по ключевым словам или None, если вопрос не распознан."""
    topic = find_topic(text)
    mid = find_model(text)
    mdl = models_by_id.get(mid) if mid else None
    if not topic and not mdl:
        return None
    parts: list[str] = []
    buttons: list[tuple[str, str]] = []
    if mdl:
        parts.append(model_card(mdl, pref, fuel_labels))
        short = mdl["name"].split(" (")[0]
        buttons.append((f"🧮 Посчитать {short}"[:60], f"faq:model:{mdl['id']}"))
    if topic and not (mdl and topic == "calc"):
        _key, _pats, body, topic_buttons = next(t for t in _TOPIC_RE if t[0] == topic)
        parts.append(body)
        buttons.extend(b for b in topic_buttons if b not in buttons and not (mdl and b == BTN_CALC))
    parts.append("Нужен ответ человека: кнопка «Написать нам».")
    return FaqAnswer(text="\n\n".join(parts), buttons=buttons, topic=topic, model_id=mdl["id"] if mdl else None)
