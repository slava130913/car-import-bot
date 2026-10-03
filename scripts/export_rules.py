"""Копирует data/rules.json в web/rules.json, чтобы веб-калькулятор использовал те же ставки, что и бот."""

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
src = ROOT / "data" / "rules.json"
dst = ROOT / "web" / "rules.json"
shutil.copyfile(src, dst)
print(f"{src} -> {dst}")
