#!/bin/sh
# Пишет config.js из переменной BOT_USERNAME при старте контейнера (nginx выполняет скрипты из /docker-entrypoint.d).
name=$(printf '%s' "${BOT_USERNAME:-}" | sed 's/^@//')
[ -n "$name" ] || name=YOUR_BOT_USERNAME
printf '// generated at container start\nwindow.APP_CONFIG = {"botUsername": "%s"};\n' "$name" > /usr/share/nginx/html/config.js
echo "web: config.js written for bot @$name"
