#!/usr/bin/env bash
# Тесты в CI (Gitea Actions). Нужны python3 и node; node уже есть в образе раннера, python ставим при необходимости.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v python3 >/dev/null 2>&1 || ! python3 -c "import venv" >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    (apt-get update -qq && apt-get install -y -qq python3 python3-venv >/dev/null) || sudo bash -c "apt-get update -qq && apt-get install -y -qq python3 python3-venv >/dev/null"
  else
    echo "::warning::python3 недоступен в раннере, тесты пропущены"
    exit 0
  fi
fi

# PHP нужен для проверки приёма заявок на сайте (site_api/, tests/test_site_php.py); без него тест пропустится.
if ! command -v php >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
  (apt-get install -y -qq php-cli >/dev/null 2>&1 || (apt-get update -qq && apt-get install -y -qq php-cli >/dev/null)) \
    || echo "::warning::php-cli не установился, проверка PHP пропущена"
fi

python3 -m venv .venv-ci
.venv-ci/bin/pip install -q -r requirements.txt
.venv-ci/bin/python -m pytest -q
