"""Генерирует страницы «Сколько стоит пригнать {модель} из Китая» для поиска: web/auto/*.html, sitemap.xml, robots.txt.

Цены машин в Китае не выдумываем: на странице таблица «цена в Китае → итог под ключ» для нескольких цен,
и разбор утильсбора именно для объёма и мощности этой модели. Все числа считает calc.engine по data/rules.json.

Запуск: python scripts/build_seo.py   (вызывается и из scripts/build_web.py)
"""

from __future__ import annotations

import html
import json
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from calc.engine import FUEL_LABELS, CarInput, calculate, format_rub, load_rules, util_fee  # noqa: E402

SITE = os.environ.get("SITE_URL", "https://slava130913.github.io/car-import-bot").rstrip("/")
BOT = os.environ.get("BOT_USERNAME", "china_car_calc_bot").lstrip("@") or "china_car_calc_bot"
PRICES = [80_000, 120_000, 160_000, 200_000, 260_000]
OUT = ROOT / "web" / "auto"

CSS = """
:root{--bg:#faf7f6;--surface:#fff;--ink:#1d1517;--muted:#6b5d60;--line:#e9dfdf;--red:#b8121f;--gold:#f6c33a;--gold-ink:#3a2a00;--ok:#1f7a3d;--warn:#8a5a00}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--bg:#151012;--surface:#1f181a;--ink:#f4ecec;--muted:#b4a5a8;--line:#3a2e31;--red:#ff5a63;--ok:#6fd08f;--warn:#f1c46a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:860px;margin:0 auto;padding:28px 16px 64px}
a{color:var(--red)}h1{font-size:clamp(24px,4.6vw,34px);line-height:1.2;margin:0 0 10px;text-wrap:balance}
h2{font-size:20px;margin:30px 0 10px}p{max-width:68ch}.muted{color:var(--muted);font-size:14px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:16px}
.verdict{border-left:4px solid var(--c);padding:12px 14px;background:var(--surface);border-radius:8px;margin:14px 0}
.tbl{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:15px;font-variant-numeric:tabular-nums}
th,td{padding:8px 6px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left;white-space:normal;min-width:9em}
th{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.cta{display:flex;flex-wrap:wrap;gap:10px;margin:18px 0}.cta a{padding:12px 18px;border-radius:10px;text-decoration:none;font-weight:600}
.cta a.p{background:var(--gold);color:var(--gold-ink)}.cta a.s{border:1px solid var(--red)}
ul.lines{padding-left:18px}ul.lines li{margin-bottom:4px}.models{columns:2;gap:24px;padding-left:18px}@media(max-width:560px){.models{columns:1}}
footer{margin-top:40px;color:var(--muted);font-size:14px}
"""


def esc(s: object) -> str:
    return html.escape(str(s))


def rub_short(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v / 1_000_000:.2f}".replace(".", ",") + " млн ₽"


def page(model: dict, rules: dict, others: list[dict]) -> tuple[str, str]:
    fb = rules["fallback_rates"]
    pref = rules["util"]["preferential"]
    name, cc, hp, fuel = model["name"], model["cc"], model["hp"], model["fuel"]
    ev = fuel in ("ev", "hybrid_seq")
    base = dict(engine_cc=cc, power_hp=hp, fuel=fuel, route="suifenhe", destination="moscow", cny_rub=fb["CNY"], eur_rub=fb["EUR"])

    u_new, _, unv_new = util_fee(rules, CarInput(price_cny=100_000, age="new", **base))
    u_old, _, _ = util_fee(rules, CarInput(price_cny=100_000, age="3-5", **base))
    limit = pref["max_hp_ev"] if ev else pref["max_hp_ice"]
    preferential = u_new == pref["new_rub"]
    if preferential:
        verdict = (f"Проходит по льготному утильсбору: {format_rub(u_new)} за машину до 3 лет и {format_rub(u_old)} старше 3 лет, "
                   f"если ввозите для себя. Мощность {hp} л.с. не выше порога {limit} л.с.")
        color = "var(--ok)"
    else:
        amt = (f"{format_rub(u_new)} за машину до 3 лет и {format_rub(u_old)} старше 3 лет" if u_new is not None
               else "сумма для этой комбинации объёма и мощности в открытых источниках не подтверждена")
        if unv_new and u_new is not None:
            amt += " (для такой мощности ставка в источниках не подтверждена, показана ближайшая известная, реальная может быть выше)"
        verdict = (f"Льготный утильсбор не действует: мощность {hp} л.с. выше порога {limit} л.с. "
                   f"Коммерческий утильсбор: {amt}. Ищите версию до {limit} л.с.: разница может быть больше миллиона.")
        color = "var(--warn)"
    ev_note = ("<p class='muted'>Для электромобилей и последовательных гибридов порог считается по 30-минутной мощности электромоторов, "
               "она обычно ниже пиковой из рекламы. Сверьте по документам: от этого зависит, будет ли льгота.</p>") if ev else ""
    if model.get("note"):
        ev_note += f"<p class='muted'>Характеристики: {esc(model['note'])}.</p>"

    rows = []
    mid = None
    for p in PRICES:
        r_new = calculate(CarInput(price_cny=p, age="new", **base), rules)
        r_old = calculate(CarInput(price_cny=p, age="3-5", **base), rules)
        if p == 160_000:
            mid = r_new
        rows.append(f"<tr><td>{p:,} ¥".replace(",", " ") + f"</td><td>{rub_short(p * fb['CNY'])}</td>"
                    f"<td>{rub_short(r_new.total_mid)}</td><td>{rub_short(r_old.total_mid)}</td></tr>")
    lines = "".join(f"<li>{esc(ln.label)}: {format_rub(ln.mid)}</li>" for ln in mid.lines) if mid else ""

    title = f"Сколько стоит пригнать {name} из Китая в {date.today().year} году: расчёт под ключ"
    desc = (f"{name}: пошлина, утильсбор ({'льготный' if preferential else 'коммерческий'}), логистика, СБКТС и ЭПТС. "
            f"Итог под ключ до Москвы для цен от 80 до 260 тысяч юаней.")
    faq = [
        (f"Проходит ли {name} по льготному утильсбору?", verdict),
        (f"Сколько стоит пригнать {name} из Китая под ключ?",
         f"При цене в Китае 160 000 ¥ новая машина выйдет примерно в {rub_short(mid.total_mid if mid else None)} до Москвы через Суйфэньхэ. "
         "Точный расчёт под вашу цену, год и город в Telegram-боте."),
    ]
    ld = {"@context": "https://schema.org", "@type": "FAQPage",
          "mainEntity": [{"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in faq]}
    other_links = "".join(f"<li><a href='{o['id']}.html'>{esc(o['name'])}</a></li>" for o in others if o["id"] != model["id"])

    body = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title><meta name="description" content="{esc(desc)}">
<link rel="canonical" href="{SITE}/auto/{model['id']}.html">
<meta property="og:title" content="{esc(title)}"><meta property="og:description" content="{esc(desc)}"><meta property="og:image" content="{SITE}/avatar.png">
<style>{CSS}</style>
<script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script>
</head><body><div class="wrap">
<p class="muted"><a href="index.html">Все модели</a> · <a href="../">Калькулятор</a></p>
<h1>Сколько стоит пригнать {esc(name)} из Китая</h1>
<p>Типовые характеристики для китайского рынка: {cc} см³, {hp} л.с., {esc(FUEL_LABELS[fuel])}. Расчёт до Москвы через Суйфэньхэ по правилам на {esc(rules['version'])}, курс ¥ {fb['CNY']:.2f} ₽ и € {fb['EUR']:.2f} ₽.</p>
<div class="verdict" style="--c:{color}">{esc(verdict)}</div>
{ev_note}
<div class="cta"><a class="p" href="https://t.me/{BOT}?start=calc">Посчитать свою машину в боте</a><a class="s" href="../#model-{model['id']}">Открыть калькулятор</a></div>
<h2>Итог под ключ при разной цене в Китае</h2>
<div class="tbl card"><table><thead><tr><th>Цена в Китае</th><th>В рублях</th><th>Под ключ, до 3 лет</th><th>Под ключ, 3–5 лет</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<p class="muted">Машине от 3 до 5 лет пошлина считается за см³, а не от цены, поэтому дорогие версии после трёхлетия часто выходят дешевле. Расчёт ориентировочный: вилка по логистике и оформлению 100–200 тысяч.</p>
<h2>Из чего складывается цена при 160 000 ¥, новая машина</h2>
<ul class="lines">{lines}</ul>
<p><b>Итого ориентировочно: {format_rub(mid.total_mid if mid else None)}.</b></p>
<div class="cta"><a class="p" href="https://t.me/{BOT}?start=calc">Рассчитать под свою цену и город</a><a class="s" href="https://t.me/{BOT}?start=lead">Подобрать и привезти</a></div>
<p>Читайте также: <a href="../guide/utilsbor.html">утильсбор</a>, <a href="../guide/rastamozhka.html">растаможка</a>, <a href="../guide/marshruty.html">маршруты</a>, расчёт <a href="../city/moscow.html">в Москву</a> и <a href="../city/tula.html">в Тулу</a>.</p>
<h2>Другие модели</h2><ul class="models">{other_links}</ul>
<footer>{esc(rules['disclaimer'])}</footer>
</div></body></html>
"""
    return f"{model['id']}.html", body


# ---------- тематические и городские страницы ----------

# (ключ доставки в rules.json, имя файла, город, «куда», «где»)
CITIES = [
    ("moscow", "moscow", "Москва", "в Москву", "в Москве"),
    ("tula", "tula", "Тула", "в Тулу", "в Туле"),
    ("novosibirsk", "novosibirsk", "Новосибирск", "в Новосибирск", "в Новосибирске"),
    ("khabarovsk", "khabarovsk", "Хабаровск", "в Хабаровск", "в Хабаровске"),
    ("far_east", "vladivostok", "Владивосток", "во Владивосток", "во Владивостоке"),
]
CITY_PRICE = 120_000
UTIL_EXAMPLES = [
    ("1,5 л, 147 л.с.", 1498, 147, "ice"),
    ("1,5 л, 177 л.с.", 1498, 177, "ice"),
    ("2,0 л, 150 л.с.", 1998, 150, "ice"),
    ("2,0 л, 190 л.с.", 1998, 190, "ice"),
    ("2,0 л, 238 л.с.", 1969, 238, "ice"),
    ("Электромобиль, 80 л.с. по 30-минутной мощности", 0, 80, "ev"),
    ("Электромобиль, 204 л.с. по 30-минутной мощности", 0, 204, "ev"),
]
GUIDES = [
    ("utilsbor", "Утильсбор на авто из Китая: льгота до 160 л.с. и коммерческие ставки"),
    ("rastamozhka", "Растаможка авто из Китая: пошлина до 3 лет и старше"),
    ("elektromobil", "Электромобиль из Китая: растаможка, акциз и утильсбор"),
    ("marshruty", "Доставка авто из Китая: маршруты, сроки и цены"),
]


def money(v: float | None) -> str:
    return format_rub(v) if v is not None else "не подтверждена"


def car(rules: dict, price: float, age: str, cc: int, hp: int, fuel: str, route: str = "suifenhe", dest: str = "moscow"):
    fb = rules["fallback_rates"]
    return CarInput(price_cny=price, age=age, engine_cc=cc, power_hp=hp, fuel=fuel, route=route, destination=dest,
                    cny_rub=fb["CNY"], eur_rub=fb["EUR"])


def shell(title: str, desc: str, path: str, crumbs: str, content: str, rules: dict, faq_pairs: list | None = None) -> str:
    ld = ""
    if faq_pairs:
        data = {"@context": "https://schema.org", "@type": "FAQPage",
                "mainEntity": [{"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in faq_pairs]}
        ld = f'<script type="application/ld+json">{json.dumps(data, ensure_ascii=False)}</script>'
    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title><meta name="description" content="{esc(desc)}">
<link rel="canonical" href="{SITE}/{path}">
<meta property="og:title" content="{esc(title)}"><meta property="og:description" content="{esc(desc)}"><meta property="og:image" content="{SITE}/avatar.png">
<style>{CSS}</style>{ld}
</head><body><div class="wrap">
<p class="muted">{crumbs}</p>
{content}
<footer>Правила расчёта на {esc(rules['version'])}, курс ¥ {rules['fallback_rates']['CNY']:.2f} ₽ и € {rules['fallback_rates']['EUR']:.2f} ₽. {esc(rules['disclaimer'])}</footer>
</div></body></html>
"""


def cta(start: str = "calc", text: str = "Посчитать свою машину в боте", second: tuple[str, str] | None = ("../", "Открыть калькулятор")) -> str:
    s = f'<a class="s" href="{second[0]}">{esc(second[1])}</a>' if second else ""
    return f'<div class="cta"><a class="p" href="https://t.me/{BOT}?start={start}">{esc(text)}</a>{s}</div>'


CRUMBS = '<a href="../">Калькулятор</a> · <a href="../guide/">Гид и города</a> · <a href="../auto/">Модели</a>'


def guide_utilsbor(rules: dict, models: list[dict]) -> str:
    pref = rules["util"]["preferential"]
    rows, star = [], False
    for label, cc, hp, fuel in UTIL_EXAMPLES:
        cells = []
        for age in ("new", "3-5"):
            amt, _note, unv = util_fee(rules, car(rules, 100_000, age, cc, hp, fuel))
            mark = "*" if unv and amt is not None else ""
            star = star or bool(mark)
            cells.append(money(amt) + mark)
        rows.append(f"<tr><td>{esc(label)}</td><td>{cells[0]}</td><td>{cells[1]}</td></tr>")
    ok, no = [], []
    for m in models:
        amt, _n, _u = util_fee(rules, car(rules, 100_000, "new", m["cc"], m["hp"], m["fuel"]))
        (ok if amt == pref["new_rub"] else no).append(f"<li><a href='../auto/{m['id']}.html'>{esc(m['name'])}</a></li>")
    conditions = "".join(f"<li>{esc(c)}</li>" for c in pref["conditions"])
    star_note = "<p class='muted'>* Для такой мощности ставка в открытых источниках не подтверждена, показана ближайшая известная. Реальная может быть выше.</p>" if star else ""
    content = f"""<h1>Утильсбор на авто из Китая в {date.today().year} году</h1>
<p>Утильсбор чаще всего решает, есть ли смысл везти машину. Льготный стоит {format_rub(pref['new_rub'])} за машину до 3 лет и {format_rub(pref['old_rub'])} старше 3 лет. Коммерческий начинается от сотен тысяч рублей и может превышать миллион.</p>
<h2>Когда действует льгота</h2><p>Нужны все условия сразу:</p><ul class="lines">{conditions}</ul>
<h2>Сколько платить: примеры</h2>
<div class="tbl card"><table><thead><tr><th>Машина</th><th>До 3 лет</th><th>Старше 3 лет</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
{star_note}
<p>Мощность проверяйте по документам, а не по рекламе. У электромобилей и последовательных гибридов считается 30-минутная мощность моторов, она обычно ниже пиковой. Параллельный гибрид считается как бензиновая машина по суммарной мощности.</p>
{cta()}
<h2>Популярные модели: проходят по льготе</h2><ul class="models">{''.join(ok)}</ul>
<h2>Популярные модели: льготы нет</h2><ul class="models">{''.join(no)}</ul>"""
    faq_pairs = [
        ("Сколько стоит льготный утильсбор на машину из Китая?",
         f"{format_rub(pref['new_rub'])} за машину до 3 лет и {format_rub(pref['old_rub'])} старше 3 лет при ввозе для себя, мощности до {pref['max_hp_ice']} л.с. и объёме до {pref['max_cc']} см³."),
        ("Какой порог мощности для льготного утильсбора у электромобиля?",
         f"Не более {pref['max_hp_ev']} л.с. по 30-минутной мощности электромоторов."),
    ]
    return shell(f"Утильсбор на авто из Китая в {date.today().year} году: льгота до 160 л.с. и коммерческие ставки",
                 "Когда действует льготный утильсбор 3 400 ₽, сколько платить при мощности выше 160 л.с. и какие популярные модели проходят по льготе.",
                 "guide/utilsbor.html", CRUMBS, content, rules, faq_pairs)


def _customs(res) -> float:
    return sum(ln.mid or 0 for ln in res.lines if ln.key in ("duty", "excise", "vat", "customs_fee"))


def guide_rastamozhka(rules: dict, models: list[dict]) -> str:
    fb = rules["fallback_rates"]
    rows = []
    for p in PRICES:
        cells = []
        for cc, hp in ((1498, 147), (1998, 150)):
            for age in ("new", "3-5"):
                cells.append(rub_short(_customs(calculate(car(rules, p, age, cc, hp, "ice"), rules))))
        rows.append(f"<tr><td>{p:,} ¥".replace(",", " ") + f"</td><td>{rub_short(p * fb['CNY'])}</td>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    content = f"""<h1>Растаможка авто из Китая в {date.today().year} году</h1>
<p>При ввозе для себя физлицо платит таможенные платежи по единым ставкам. Для машины до 3 лет пошлина считается от стоимости с минимумом за каждый кубический сантиметр. Для машины от 3 до 5 лет только за объём двигателя. Поэтому дорогие версии выгоднее ввозить после трёхлетия.</p>
<h2>Таможенные платежи при разной цене</h2>
<p class="muted">Пошлина и сбор за оформление, без утильсбора. Объём 1,5 л и 2,0 л, мощность до 160 л.с.</p>
<div class="tbl card"><table><thead><tr><th>Цена в Китае</th><th>В рублях</th><th>1,5 л до 3 лет</th><th>1,5 л 3–5 лет</th><th>2,0 л до 3 лет</th><th>2,0 л 3–5 лет</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<p>Утильсбор платится отдельно и зависит от мощности: подробно на странице <a href="utilsbor.html">про утильсбор</a>. Если машине скоро исполнится 3 года, бот в расчёте сам покажет, сколько можно сэкономить, если подождать.</p>
{cta()}
<h2>Что ещё входит в цену под ключ</h2>
<p>Платёжный агент, расходы в Китае, логистика до границы, брокер, склад временного хранения, СБКТС с ЭРА-ГЛОНАСС, ЭПТС и доставка до города. Полный расчёт по моделям: <a href="../auto/">все модели</a>.</p>"""
    mid = calculate(car(rules, 160_000, "new", 1998, 150, "ice"), rules)
    mid_old = calculate(car(rules, 160_000, "3-5", 1998, 150, "ice"), rules)
    faq_pairs = [
        ("Сколько стоит растаможка машины из Китая?",
         f"Для машины 2,0 л за 160 000 ¥ таможенные платежи около {rub_short(_customs(mid))} до 3 лет и {rub_short(_customs(mid_old))} в возрасте 3–5 лет, без утильсбора."),
        ("Почему машины старше 3 лет растаможивать дешевле?",
         "Для машин от 3 до 5 лет пошлина считается только за объём двигателя, а не от стоимости."),
    ]
    return shell(f"Растаможка авто из Китая в {date.today().year} году: пошлина до 3 лет и старше",
                 "Таможенные платежи за машину из Китая по единым ставкам: таблица для 1,5 и 2,0 л при цене от 80 до 260 тысяч юаней.",
                 "guide/rastamozhka.html", CRUMBS, content, rules, faq_pairs)


def guide_elektromobil(rules: dict, models: list[dict]) -> str:
    pref = rules["util"]["preferential"]
    rows = []
    for p in PRICES:
        a = calculate(car(rules, p, "new", 0, 80, "ev"), rules)
        b = calculate(car(rules, p, "new", 0, 204, "ev"), rules)
        rows.append(f"<tr><td>{p:,} ¥".replace(",", " ") + f"</td><td>{rub_short(a.total_mid)}</td><td>{rub_short(b.total_mid)}</td></tr>")
    br = calculate(car(rules, 150_000, "new", 0, 204, "ev"), rules)
    lines = "".join(f"<li>{esc(ln.label)}: {money(ln.mid)}</li>" for ln in br.lines if ln.key in ("price", "duty", "excise", "vat", "util"))
    ev_models = "".join(f"<li><a href='../auto/{m['id']}.html'>{esc(m['name'])}</a></li>" for m in models if m["fuel"] in ("ev", "hybrid_seq"))
    content = f"""<h1>Электромобиль из Китая в {date.today().year} году</h1>
<p>Электромобили и последовательные гибриды (EREV) растаможивают по отдельной схеме: пошлина от стоимости, затем акциз и НДС. Льготный утильсбор только при 30-минутной мощности не более {pref['max_hp_ev']} л.с. Большинство современных электромобилей мощнее, поэтому итог выходит заметно дороже бензиновой машины той же цены.</p>
<h2>Итог под ключ до Москвы, новая машина</h2>
<div class="tbl card"><table><thead><tr><th>Цена в Китае</th><th>80 л.с. по 30 мин</th><th>204 л.с. по 30 мин</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<h2>Из чего складывается цена: 150 000 ¥, 204 л.с.</h2>
<ul class="lines">{lines}</ul>
<p>30-минутная мощность указана в документах и обычно ниже пиковой из рекламы. Сверьте её до покупки: от неё зависит и акциз, и утильсбор.</p>
{cta()}
<h2>Электромобили и EREV в списке моделей</h2><ul class="models">{ev_models}</ul>"""
    faq_pairs = [
        ("Выгодно ли везти электромобиль из Китая?",
         f"Только если 30-минутная мощность не выше {pref['max_hp_ev']} л.с. Иначе утильсбор коммерческий, и вместе с акцизом и НДС итог заметно выше."),
    ]
    return shell(f"Электромобиль из Китая в {date.today().year} году: растаможка, акциз и утильсбор",
                 "Сколько стоит растаможить электромобиль из Китая: пошлина, акциз, НДС и утильсбор, итог под ключ при разной цене.",
                 "guide/elektromobil.html", CRUMBS, content, rules, faq_pairs)


def guide_marshruty(rules: dict, models: list[dict]) -> str:
    lg = rules["costs"]["logistics"]
    ref = next((m for m in models if m["id"] == "coolray"), models[0])
    rows = []
    for key, v in lg.items():
        total = calculate(car(rules, 100_000, "new", ref["cc"], ref["hp"], ref["fuel"], route=key), rules).total_mid
        rows.append(f"<tr><td>{esc(v['label'])}</td><td>{esc(v['days'])} дней</td><td>{format_rub(v['low'])} – {format_rub(v['high'])}</td><td>{rub_short(total)}</td></tr>")
    dm = rules["costs"]["domestic"]
    drows = "".join(f"<tr><td>{esc(v['label'])}</td><td>{format_rub(v['low'])} – {format_rub(v['high'])}</td></tr>"
                    for k, v in dm.items() if isinstance(v, dict) and "low" in v and k != "far_east")
    content = f"""<h1>Доставка авто из Китая: маршруты, сроки и цены</h1>
<p>Машину везут через сухопутный переход или морем. От маршрута зависят срок, цена логистики и то, где машина пройдёт таможню.</p>
<h2>Маршруты до границы</h2>
<div class="tbl card"><table><thead><tr><th>Маршрут</th><th>Срок</th><th>Логистика</th><th>Итог для {esc(ref['name'])} за 100 000 ¥</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<p class="muted">Итог под ключ до Москвы, новая машина. Срок с учётом очереди на границе.</p>
<h2>Доставка по России от Уссурийска или Владивостока</h2>
<div class="tbl card"><table><thead><tr><th>Город</th><th>Автовоз</th></tr></thead><tbody>{drows}</tbody></table></div>
<p>До Москвы автовоз идёт 10–14 дней. Весь путь от покупки до учёта обычно 4–7 недель.</p>
{cta(text="Сравнить маршруты для своей машины")}"""
    return shell("Доставка авто из Китая в Россию: маршруты, сроки и цены",
                 "Суйфэньхэ, Забайкальск, Хоргос или море: сроки, стоимость логистики и итог под ключ для каждого маршрута.",
                 "guide/marshruty.html", CRUMBS, content, rules)


def city_page(rules: dict, models: list[dict], dest: str, slug: str, city: str, to: str, where: str) -> str:
    pref = rules["util"]["preferential"]
    dm = rules["costs"]["domestic"][dest]
    rows, star = [], False
    for m in models:
        r = calculate(car(rules, CITY_PRICE, "new", m["cc"], m["hp"], m["fuel"], dest=dest), rules)
        util = next(ln.mid for ln in r.lines if ln.key == "util")
        lgota = "да" if util == pref["new_rub"] else "нет"
        mark = "*" if r.unverified else ""
        star = star or bool(mark)
        rows.append(f"<tr><td><a href='../auto/{m['id']}.html'>{esc(m['name'])}</a></td><td>{lgota}</td><td>{rub_short(r.total_mid)}{mark}</td></tr>")
    star_note = ("<p class='muted'>* Ставка утильсбора для такой мощности в открытых источниках не подтверждена. "
                 "Итог может быть выше.</p>") if star else ""
    price = f"{CITY_PRICE:,}".replace(",", " ")
    if dm["mid"]:
        delivery = f"<p>Доставка автовозом от Уссурийска или Владивостока {to}: {format_rub(dm['low'])} – {format_rub(dm['high'])}. Она уже включена в итог ниже.</p>"
    else:
        delivery = "<p>Машину получаете во Владивостоке или Уссурийске, доставка по России не нужна.</p>"
    if dest == "moscow":
        delivery += "<p>Автовоз до Москвы идёт 10–14 дней. Весь путь от покупки до учёта обычно 4–7 недель.</p>"
    content = f"""<h1>Пригнать авто из Китая {esc(to)} в {date.today().year} году</h1>
<p>Сколько стоит машина из Китая под ключ с доставкой {esc(to)}: пошлина, утильсбор, логистика, СБКТС, ЭПТС и автовоз до города.</p>
{delivery}
<h2>Итог под ключ {esc(where)} при цене в Китае {price} ¥</h2>
<p class="muted">Новая машина, маршрут через Суйфэньхэ. Цены машин в Китае разные: для своей цены посчитайте в боте или калькуляторе.</p>
<div class="tbl card"><table><thead><tr><th>Модель</th><th>Льгота</th><th>Под ключ</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
{star_note}
{cta(text=f"Посчитать с доставкой {to}")}
<h2>Нужна помощь с подбором?</h2>
<p>Оставьте заявку в боте: модель, бюджет и город. Менеджер свяжется с вами и предложит варианты.</p>
{cta(start="lead", text="Оставить заявку на подбор", second=None)}"""
    faq_pairs = [
        (f"Сколько стоит доставка машины из Китая {to}?",
         (f"Автовоз от Уссурийска или Владивостока {to}: {format_rub(dm['low'])} – {format_rub(dm['high'])}." if dm["mid"]
          else "Доставка по России не нужна: машину получают во Владивостоке или Уссурийске.")),
    ]
    return shell(f"Пригнать авто из Китая {to} в {date.today().year} году: цена под ключ",
                 f"Сколько стоит машина из Китая под ключ {where}: пошлина, утильсбор, логистика и доставка {to} для популярных моделей.",
                 f"city/{slug}.html", CRUMBS, content, rules, faq_pairs)


def guide_index(rules: dict) -> str:
    g = "".join(f"<li><a href='{k}.html'>{esc(t)}</a></li>" for k, t in GUIDES)
    c = "".join(f"<li><a href='../city/{slug}.html'>Авто из Китая {esc(to)}</a></li>" for _d, slug, _c, to, _w in CITIES)
    content = f"""<h1>Авто из Китая: гид и расчёт по городам</h1>
<h2>Гид</h2><ul class="lines">{g}</ul>
<h2>Города</h2><ul class="lines">{c}</ul>
<h2>Модели</h2><p><a href="../auto/">Расчёт под ключ для 24 популярных моделей</a></p>
{cta()}"""
    return shell("Авто из Китая: утильсбор, растаможка, доставка и цены по городам",
                 "Гид по ввозу машины из Китая: утильсбор, растаможка, электромобили, маршруты и итог под ключ для Москвы, Тулы и других городов.",
                 "guide/", '<a href="../">Калькулятор</a> · <a href="../auto/">Модели</a>', content, rules)


def extra_pages(rules: dict, models: list[dict]) -> dict[str, str]:
    """Путь от web/ → HTML тематических и городских страниц."""
    pages = {"guide/index.html": guide_index(rules)}
    builders = {"utilsbor": guide_utilsbor, "rastamozhka": guide_rastamozhka, "elektromobil": guide_elektromobil, "marshruty": guide_marshruty}
    for key, _t in GUIDES:
        pages[f"guide/{key}.html"] = builders[key](rules, models)
    for dest, slug, city, to, where in CITIES:
        pages[f"city/{slug}.html"] = city_page(rules, models, dest, slug, city, to, where)
    return pages


def main() -> None:
    rules = load_rules()
    models = json.loads((ROOT / "data" / "models.json").read_text(encoding="utf-8"))["models"]
    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*.html"):
        f.unlink()
    for m in models:
        fn, body = page(m, rules, models)
        (OUT / fn).write_text(body, encoding="utf-8")
    extra = extra_pages(rules, models)
    for sub in ("guide", "city"):
        d = ROOT / "web" / sub
        d.mkdir(parents=True, exist_ok=True)
        for f in d.glob("*.html"):
            f.unlink()
    for rel, body in extra.items():
        (ROOT / "web" / rel).write_text(body, encoding="utf-8")
    items = "".join(
        f"<li><a href='{m['id']}.html'>{esc(m['name'])}</a> <span class='muted'>· {m['cc']} см³, {m['hp']} л.с.</span></li>" for m in models)
    (OUT / "index.html").write_text(f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Сколько стоит пригнать машину из Китая: расчёт по моделям</title>
<meta name="description" content="Расчёт под ключ для популярных моделей из Китая: пошлина, утильсбор, логистика, СБКТС и ЭПТС.">
<link rel="canonical" href="{SITE}/auto/"><style>{CSS}</style></head><body><div class="wrap">
<p class="muted"><a href="../">Калькулятор</a> · <a href="../guide/">Гид и города</a></p>
<h1>Сколько стоит пригнать машину из Китая: по моделям</h1>
<p>Для каждой модели: проходит ли по льготному утильсбору, итог под ключ при разной цене в Китае и из чего он складывается.</p>
<ul class="lines">{items}</ul>
<div class="cta"><a class="p" href="https://t.me/{BOT}?start=calc">Посчитать свою машину в боте</a></div>
<footer>{esc(rules['disclaimer'])}</footer></div></body></html>
""", encoding="utf-8")
    today = date.today().isoformat()
    urls = ([f"{SITE}/", f"{SITE}/partners.html", f"{SITE}/auto/"] + [f"{SITE}/auto/{m['id']}.html" for m in models]
            + [f"{SITE}/{rel[:-len('index.html')] if rel.endswith('index.html') else rel}" for rel in extra])
    (ROOT / "web" / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "".join(f"  <url><loc>{u}</loc><lastmod>{today}</lastmod></url>\n" for u in urls) + "</urlset>\n", encoding="utf-8")
    (ROOT / "web" / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {SITE}/sitemap.xml\n", encoding="utf-8")
    print(f"SEO: {len(models)} страниц моделей, {len(extra)} страниц гида и городов, index, sitemap.xml, robots.txt")


if __name__ == "__main__":
    main()
