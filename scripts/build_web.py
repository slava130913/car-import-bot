"""Собирает папку web/: копирует data/rules.json и пишет web/config.js из переменных окружения.

Запуск: python scripts/build_web.py
Переменные: BOT_USERNAME (без @), берутся из .env, если есть python-dotenv.
"""

import json
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

bot_username = os.environ.get("BOT_USERNAME", "").lstrip("@").strip()
if not bot_username:
    print("ВНИМАНИЕ: BOT_USERNAME не задан в .env, кнопки на сайте будут вести на заглушку.")
    bot_username = "YOUR_BOT_USERNAME"

shutil.copyfile(ROOT / "data" / "rules.json", ROOT / "web" / "rules.json")
(ROOT / "web" / "config.js").write_text(
    "// Генерируется scripts/build_web.py, не редактировать руками\n"
    f"window.APP_CONFIG = {json.dumps({'botUsername': bot_username}, ensure_ascii=False)};\n",
    encoding="utf-8",
)
print(f"web/rules.json обновлён, web/config.js: бот @{bot_username}")
