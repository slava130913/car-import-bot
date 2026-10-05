"""Сообщает Яндексу и Bing о страницах сайта через IndexNow (без аккаунта, ключ лежит файлом в web/).

Запуск после выкладки сайта: python scripts/indexnow.py
"""

import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = "https://slava130913.github.io/car-import-bot"
HOST = "slava130913.github.io"

keys = [p for p in (ROOT / "web").glob("*.txt") if re.fullmatch(r"[0-9a-f]{32}\.txt", p.name)]
if not keys:
    sys.exit("Нет файла ключа IndexNow в web/")
key = keys[0].stem
urls = re.findall(r"<loc>([^<]+)</loc>", (ROOT / "web" / "sitemap.xml").read_text(encoding="utf-8"))
body = json.dumps({"host": HOST, "key": key, "keyLocation": f"{SITE}/{key}.txt", "urlList": urls}).encode()
for endpoint in ("https://yandex.com/indexnow", "https://api.indexnow.org/indexnow"):
    req = urllib.request.Request(endpoint, data=body, method="POST", headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(endpoint, r.status, len(urls), "URL")
    except urllib.error.HTTPError as e:
        print(endpoint, "HTTP", e.code, e.read()[:200])
    except Exception as e:  # noqa: BLE001
        print(endpoint, "ошибка:", e)
