#!/usr/bin/env bash
# Выкладка сайта на хостинг reg.ru (Gitea Actions, задача site). Без настроек хостинга ничего не делает.
# Переменные Gitea: REGRU_FTP_HOST, REGRU_FTP_USER, REGRU_SITE_DIR, SITE_URL; секрет REGRU_FTP_PASSWORD.
# BOT_TOKEN и BOT_USERNAME берутся из секрета DEPLOY_ENV_FILE (тот же .env, что у бота).
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -z "${REGRU_FTP_HOST:-}" ] || [ -z "${REGRU_FTP_PASSWORD:-}" ]; then
  echo "Хостинг reg.ru ещё не настроен (переменная REGRU_FTP_HOST и секрет REGRU_FTP_PASSWORD), пропускаю."
  exit 0
fi

if ! command -v python3 >/dev/null 2>&1; then
  (apt-get update -qq && apt-get install -y -qq python3 >/dev/null) || sudo bash -c "apt-get update -qq && apt-get install -y -qq python3 >/dev/null"
fi

env_file() {
  case ${DEPLOY_ENV_FILE:-} in
    base64:*) printf '%s' "${DEPLOY_ENV_FILE#base64:}" | base64 -d ;;
    *) printf '%s\n' "${DEPLOY_ENV_FILE:-}" ;;
  esac
}
env_value() { env_file | tr -d '\r' | sed -n "s/^$1=//p" | tail -n 1 | sed "s/^[\"']//; s/[\"']\$//"; }

BOT_TOKEN=$(env_value BOT_TOKEN)
BOT_USERNAME=${BOT_USERNAME:-$(env_value BOT_USERNAME)}
SITE_URL=${SITE_URL:-https://myapphub.tech/car}
export BOT_TOKEN BOT_USERNAME SITE_URL

python3 scripts/build_web.py
python3 scripts/deploy_regru.py
if [ "${INDEXNOW:-1}" = "1" ]; then
  python3 scripts/indexnow.py || echo "::warning::IndexNow не ответил, страницы просто проиндексируются позже"
fi
