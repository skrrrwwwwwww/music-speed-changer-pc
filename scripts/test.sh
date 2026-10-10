#!/usr/bin/env bash
# Проверки DSP-части и протокола без запуска Electron.
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python python/selftest.py "$@"
