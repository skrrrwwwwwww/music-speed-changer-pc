"""Точка входа для PyInstaller.

Запуск `msc-engine` эквивалентен `python -m msc`: тот же NDJSON-протокол
в stdin/stdout, тот же набор аргументов.
"""

import multiprocessing
import sys


def main() -> int:
    # В onefile-сборке multiprocessing требует freeze_support.
    multiprocessing.freeze_support()
    from msc.server import main as server_main

    return server_main()


if __name__ == "__main__":
    sys.exit(main())
