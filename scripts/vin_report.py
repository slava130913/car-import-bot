"""Переводит китайский отчёт об истории автомобиля и собирает HTML-отчёт для клиента.

Использование:
    python scripts/vin_report.py <VIN> <файл_с_китайским_отчётом.txt> [--model claude-opus-5]

Результат: reports/<VIN>.html, его можно отправить клиенту через бота:
    в чате с ботом прикрепить файл и подписать "/send <номер заказа>".

Нужен ANTHROPIC_API_KEY в .env или в окружении (либо профиль `ant auth login`).
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

import anthropic

SYSTEM = """Ты переводчик и аналитик отчётов об истории автомобилей с китайского рынка.
Тебе дают сырой текст отчёта (车辆历史报告, 车况查询, 4S店维修记录, 出险记录 и т.п.).
Верни строго JSON без пояснений по схеме:
{
  "summary": "2-4 предложения: общий вывод о машине простым языком",
  "verdict": "ok" | "caution" | "avoid",
  "verdict_reason": "одна фраза, почему такой вердикт",
  "mileage_km": число или null,
  "mileage_note": "есть ли признаки скрутки пробега",
  "owners": число или null,
  "accidents": [{"date": "ГГГГ-ММ-ДД или ГГГГ-ММ", "description": "что произошло", "amount_cny": число или null}],
  "service": [{"date": "...", "mileage_km": число или null, "description": "что делали"}],
  "flags": ["список рисков: такси, залог, затопление, замена ключевых узлов, несовпадение VIN и т.д."],
  "raw_terms": [{"zh": "термин", "ru": "перевод"}]
}
Правила: даты, пробеги и суммы сохраняй точно; 4S店 = дилерский центр; 出险 = страховой случай;
过户 = смена владельца; 泡水 = затопление; 营运 = коммерческое использование (такси/каршеринг);
抵押 = залог. Если данных нет, ставь null или пустой список, не выдумывай."""

MODEL_DEFAULT = "claude-opus-5"


def translate(raw_text: str, model: str) -> dict:
    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=model,
        max_tokens=16000,
        system=SYSTEM,
        betas=["server-side-fallback-2026-06-01"],
        fallbacks=[{"model": "claude-opus-4-8"}],
        messages=[{"role": "user", "content": f"Отчёт:\n\n{raw_text}"}],
    )
    if response.stop_reason == "refusal":
        raise SystemExit("Модель отказалась обрабатывать текст. Проверьте, что это отчёт об автомобиле.")
    text = "".join(b.text for b in response.content if b.type == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise SystemExit(f"Не удалось найти JSON в ответе модели:\n{text[:500]}")
    return json.loads(m.group(0))


VERDICT_LABEL = {"ok": "Можно рассматривать", "caution": "Есть вопросы, торгуйтесь или проверьте очно", "avoid": "Не рекомендуем"}
VERDICT_COLOR = {"ok": "#1a7f37", "caution": "#b26a00", "avoid": "#c8102e"}


def render(vin: str, data: dict, generated: date) -> str:
    e = html.escape
    v = data.get("verdict", "caution")
    accidents = data.get("accidents") or []
    service = data.get("service") or []
    flags = data.get("flags") or []
    terms = data.get("raw_terms") or []

    def rows(items, cols):
        if not items:
            return "<tr><td colspan='%d' class='muted'>записей нет</td></tr>" % len(cols)
        out = []
        for it in items:
            out.append("<tr>" + "".join(f"<td>{e(str(it.get(c) if it.get(c) is not None else '—'))}</td>" for c in cols) + "</tr>")
        return "".join(out)

    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Отчёт по VIN {e(vin)}</title>
<style>
body{{font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#15171a;background:#fff;margin:0;padding:24px 16px;max-width:760px;margin:0 auto}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:17px;margin:28px 0 8px}}
.muted{{color:#5c6670;font-size:13px}}
.verdict{{display:inline-block;padding:6px 12px;border-radius:8px;color:#fff;font-weight:600;background:{VERDICT_COLOR.get(v, '#b26a00')}}}
table{{width:100%;border-collapse:collapse;font-size:14px}} td,th{{padding:6px 4px;border-bottom:1px solid #dde2e7;text-align:left;vertical-align:top}}
.flags li{{margin-bottom:4px}}
.box{{background:#f4f6f8;border-radius:10px;padding:12px 14px;margin-top:12px}}
</style></head><body>
<h1>Отчёт по VIN {e(vin)}</h1>
<div class="muted">Сформирован {generated.isoformat()} по данным китайских баз. Перевод и анализ автоматические, проверены оператором.</div>
<div class="box"><span class="verdict">{e(VERDICT_LABEL.get(v, v))}</span><div style="margin-top:8px">{e(data.get('verdict_reason') or '')}</div></div>
<h2>Кратко</h2><p>{e(data.get('summary') or '')}</p>
<h2>Пробег и владельцы</h2>
<table><tr><th>Пробег по последней записи</th><td>{e(str(data.get('mileage_km') or '—'))} км</td></tr>
<tr><th>Признаки скрутки</th><td>{e(data.get('mileage_note') or '—')}</td></tr>
<tr><th>Владельцев</th><td>{e(str(data.get('owners') or '—'))}</td></tr></table>
<h2>Страховые случаи и ДТП</h2>
<table><tr><th>Дата</th><th>Описание</th><th>Сумма, ¥</th></tr>{rows(accidents, ['date', 'description', 'amount_cny'])}</table>
<h2>Сервисная история</h2>
<table><tr><th>Дата</th><th>Пробег, км</th><th>Работы</th></tr>{rows(service, ['date', 'mileage_km', 'description'])}</table>
<h2>Риски</h2>
<ul class="flags">{''.join(f'<li>{e(f)}</li>' for f in flags) or '<li class="muted">не выявлены</li>'}</ul>
<h2>Термины из оригинала</h2>
<table>{''.join(f"<tr><td>{e(t.get('zh',''))}</td><td>{e(t.get('ru',''))}</td></tr>" for t in terms) or '<tr><td class="muted">—</td></tr>'}</table>
<p class="muted" style="margin-top:28px">Отчёт носит информационный характер. Перед покупкой рекомендуем очный осмотр.</p>
</body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("vin")
    ap.add_argument("source", help="текстовый файл с китайским отчётом")
    ap.add_argument("--model", default=MODEL_DEFAULT)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    vin = args.vin.strip().upper()
    raw = Path(args.source).read_text(encoding="utf-8")
    if not raw.strip():
        sys.exit("Файл с отчётом пустой.")
    print(f"Перевожу отчёт для {vin} моделью {args.model}…")
    data = translate(raw, args.model)
    out = Path(args.out) if args.out else ROOT / "reports" / f"{vin}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(vin, data, date.today()), encoding="utf-8")
    (out.with_suffix(".json")).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Готово: {out}\nПроверьте глазами, затем отправьте клиенту: прикрепите файл в чат с ботом и подпишите /send <номер заказа>")


if __name__ == "__main__":
    main()
