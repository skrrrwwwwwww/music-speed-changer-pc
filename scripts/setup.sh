#!/usr/bin/env bash
# Первоначальная настройка: venv + зависимости + electron.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=${PYTHON:-python3}

if [ ! -d .venv ]; then
  echo "==> Создаём venv (.venv)"
  "$PY" -m venv .venv
fi

echo "==> Ставим Python-зависимости"
.venv/bin/pip install --upgrade pip >/dev/null
.venv/bin/pip install -r python/requirements.txt

if [ ! -d node_modules ]; then
  echo "==> Ставим npm-зависимости"
  npm install
fi

if ! command -v ffmpeg >/dev/null; then
  echo "!! ffmpeg не найден: экспорт и импорт нестандартных форматов будут недоступны"
  echo "   Debian/Ubuntu: sudo apt install ffmpeg"
fi

echo
echo "Готово. Запуск:  npm start"
