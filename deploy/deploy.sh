#!/usr/bin/env bash
# Обновление на сервере: подтянуть код, пересобрать сайт, перезапустить бота.
# Использование: bash /opt/car-import-bot/deploy/deploy.sh
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"
git pull --ff-only
.venv/bin/pip install -q -r requirements.txt
.venv/bin/python scripts/build_web.py
systemctl restart car-import-bot
systemctl --no-pager status car-import-bot | head -3
