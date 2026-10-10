#!/usr/bin/env bash
# Запуск версии из исходников (без установленного .deb).
#
# Флаг --no-sandbox обязателен: Electron из node_modules лежит в пользовательских
# каталогах, и Chromium отказывается стартовать, считая такой путь небезопасным
# для SUID-песочницы. В установленном пакете песочница настроена через postinst,
# поэтому там флаг не нужен.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
  echo "venv не найден — выполните ./scripts/setup.sh"
  exit 1
fi

exec npm start "$@"
