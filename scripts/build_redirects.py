"""Превращает HTML-страницы в web/ в переадресацию на основной сайт (хостинг reg.ru).

Запускается только в сборке GitHub Pages (.github/workflows/pages.yml) после build_web.py, меняет файлы
в рабочей копии CI, в репозиторий не коммитится. Кнопка меню бота и старые ссылки ведут на github.io:
страница сразу уходит на тот же путь на новом сайте и сохраняет ?bot=…&brand=…&fee=… и #tgWebAppData
(по ним калькулятор внутри Telegram узнаёт бот клиента). Для поисковиков canonical и meta refresh.

Запуск: REDIRECT_TO=https://myapphub.tech/car python scripts/build_redirects.py
"""

from __future__ import annotations

import html
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"

STUB = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Калькулятор переехал</title>
<link rel="canonical" href="{url}">
<meta http-equiv="refresh" content="0; url={url}">
<script>location.replace({url_js} + location.search + location.hash);</script>
<style>body{{font:16px/1.5 system-ui,sans-serif;margin:40px 16px;color:#15171a;background:#fff}}
@media (prefers-color-scheme: dark){{body{{color:#e8eaed;background:#0f1114}}a{{color:#ff4d63}}}}</style>
</head>
<body><p>Калькулятор переехал: <a href="{url}">{url}</a></p></body>
</html>
"""


def target_for(rel: str, base: str) -> str:
    """index.html → папка: /auto/index.html → /auto/, /index.html → /."""
    path = rel[: -len("index.html")] if rel.endswith("index.html") else rel
    return f"{base}/{path}"


def main() -> int:
    base = os.environ.get("REDIRECT_TO", "").strip().rstrip("/")
    if not base.startswith("https://"):
        print("REDIRECT_TO не задан (нужен https-адрес основного сайта), страницы остаются как есть.")
        return 0
    n = 0
    for page in sorted(WEB.rglob("*.html")):
        rel = page.relative_to(WEB).as_posix()
        url = target_for(rel, base)
        page.write_text(STUB.format(url=html.escape(url, quote=True), url_js=repr(url).replace("<", "\\x3c")),
                        encoding="utf-8")
        n += 1
    (WEB / "sitemap.xml").unlink(missing_ok=True)  # карта сайта только на основном адресе
    print(f"{n} страниц переадресуют на {base}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
