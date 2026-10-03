#!/usr/bin/env bash
# Установка бота на Ubuntu/Debian VPS как systemd-сервиса.
# Использование (на сервере, из корня репозитория): sudo bash deploy/install.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE=car-import-bot

if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "Создан $APP_DIR/.env. Заполните BOT_TOKEN, ADMIN_IDS, BOT_USERNAME и запустите скрипт снова."
  exit 1
fi

apt-get update -qq && apt-get install -y -qq python3 python3-venv >/dev/null
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
"$APP_DIR/.venv/bin/python" "$APP_DIR/scripts/build_web.py"

cat > /etc/systemd/system/$SERVICE.service <<EOF
[Unit]
Description=Car import Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/python -m bot.main
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now $SERVICE
sleep 2
systemctl --no-pager status $SERVICE | head -5
echo
echo "Логи: journalctl -u $SERVICE -f"
echo "Обновление: git pull && systemctl restart $SERVICE"
