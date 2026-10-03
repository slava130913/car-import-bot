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

python3 -m venv .venv-ci
.venv-ci/bin/pip install -q -r requirements.txt
.venv-ci/bin/python -m pytest -q
