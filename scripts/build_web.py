"""Собирает папку web/: копирует data/rules.json и пишет web/config.js из переменных окружения.

Запуск: python scripts/build_web.py
Переменные: BOT_USERNAME (без @), берутся из .env, если есть python-dotenv.
Для хостинга reg.ru (ci/site.sh): REGRU_FTP_HOST включает форму заявки, OPERATOR_NAME и CONTACT_EMAIL идут в политику.
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
shutil.copyfile(ROOT / "data" / "models.json", ROOT / "web" / "models.json")
# Форма заявки без Telegram работает только на хостинге с PHP (reg.ru): там рядом лежит api/lead.php.
# На GitHub Pages PHP нет, поэтому форма включается только при выкладке на reg.ru (ci/site.sh задаёт REGRU_FTP_HOST).
config = {
    "botUsername": bot_username,
    "leadUrl": "api/lead.php" if os.environ.get("REGRU_FTP_HOST") else "",
    "operator": os.environ.get("OPERATOR_NAME", "").strip(),
    "contactEmail": os.environ.get("CONTACT_EMAIL", "").strip(),
}
(ROOT / "web" / "config.js").write_text(
    "// Генерируется scripts/build_web.py, не редактировать руками\n"
    f"window.APP_CONFIG = {json.dumps(config, ensure_ascii=False)};\n",
    encoding="utf-8",
)
print(f"web/rules.json и web/models.json обновлены, web/config.js: бот @{bot_username}")

# Страницы моделей для поиска, sitemap.xml и robots.txt
import subprocess, sys as _sys
subprocess.run([_sys.executable, str(ROOT / "scripts" / "build_seo.py")], check=True)
