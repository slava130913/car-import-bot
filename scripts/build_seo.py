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
th,td{padding:8px 6px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}
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
<h2>Другие модели</h2><ul class="models">{other_links}</ul>
<footer>{esc(rules['disclaimer'])}</footer>
</div></body></html>
"""
    return f"{model['id']}.html", body


def main() -> None:
    rules = load_rules()
    models = json.loads((ROOT / "data" / "models.json").read_text(encoding="utf-8"))["models"]
    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*.html"):
        f.unlink()
    for m in models:
        fn, body = page(m, rules, models)
        (OUT / fn).write_text(body, encoding="utf-8")
    items = "".join(
        f"<li><a href='{m['id']}.html'>{esc(m['name'])}</a> <span class='muted'>· {m['cc']} см³, {m['hp']} л.с.</span></li>" for m in models)
    (OUT / "index.html").write_text(f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Сколько стоит пригнать машину из Китая: расчёт по моделям</title>
<meta name="description" content="Расчёт под ключ для популярных моделей из Китая: пошлина, утильсбор, логистика, СБКТС и ЭПТС.">
<link rel="canonical" href="{SITE}/auto/"><style>{CSS}</style></head><body><div class="wrap">
<p class="muted"><a href="../">Калькулятор</a></p>
<h1>Сколько стоит пригнать машину из Китая: по моделям</h1>
<p>Для каждой модели: проходит ли по льготному утильсбору, итог под ключ при разной цене в Китае и из чего он складывается.</p>
<ul class="lines">{items}</ul>
<div class="cta"><a class="p" href="https://t.me/{BOT}?start=calc">Посчитать свою машину в боте</a></div>
<footer>{esc(rules['disclaimer'])}</footer></div></body></html>
""", encoding="utf-8")
    today = date.today().isoformat()
    urls = [f"{SITE}/", f"{SITE}/partners.html", f"{SITE}/auto/"] + [f"{SITE}/auto/{m['id']}.html" for m in models]
    (ROOT / "web" / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "".join(f"  <url><loc>{u}</loc><lastmod>{today}</lastmod></url>\n" for u in urls) + "</urlset>\n", encoding="utf-8")
    (ROOT / "web" / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {SITE}/sitemap.xml\n", encoding="utf-8")
    print(f"SEO: {len(models)} страниц моделей, index, sitemap.xml, robots.txt")


if __name__ == "__main__":
    main()
