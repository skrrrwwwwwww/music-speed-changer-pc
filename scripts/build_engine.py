# -*- mode: python ; coding: utf-8 -*-
"""Сборка python-движка в один исполняемый файл (PyInstaller).

Движок не должен зависеть от системного Python: в .deb попадает только
исполняемый файл, а зависимости (portaudio, ffmpeg) объявляются в пакете.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ROOT / "python" / "msc_entry.py"
DIST = ROOT / "build" / "pyinstaller"
WORK = ROOT / "build" / "pyinstaller-work"

sys.path.insert(0, str(ROOT / "python"))

# Модули, которые PyInstaller не должен искать статически.
EXCLUDES = [
    "tkinter",
    "matplotlib",
    "scipy",
    "pandas",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "IPython",
    "pytest",
    "setuptools",
    "pip",
    "wheel",
    "sqlite3",
    "xmlrpc",
    "pydoc_data",
    "unittest",
    "doctest",
    "lib2to3",
]

a = None
try:
    import PyInstaller.building.makespec as ms  # noqa: F401
except Exception:
    pass

# Собираем onefile: одна папка resources/app/bin/msc-engine
if not ENTRY.exists():
    raise SystemExit(f"нет точки входа: {ENTRY}")

from PyInstaller.__main__ import run  # noqa: E402

DIST.mkdir(parents=True, exist_ok=True)

args = [
    str(ENTRY),
    "--name",
    "msc-engine",
    "--onefile",
    "--noconfirm",
    "--clean",
    "--log-level",
    "WARN",
    "--distpath",
    str(DIST),
    "--workpath",
    str(WORK),
    "--specpath",
    str(WORK),
    # Наш пакет
    "--paths",
    str(ROOT / "python"),
    "--hidden-import",
    "msc",
    "--hidden-import",
    "msc.server",
    "--hidden-import",
    "msc.engine",
    "--hidden-import",
    "msc.stream",
    "--hidden-import",
    "msc.phasevocoder",
    "--hidden-import",
    "msc.sinc",
    "--hidden-import",
    "msc.sources",
    "--hidden-import",
    "msc.peaks",
    "--hidden-import",
    "msc.exporter",
    "--hidden-import",
    "msc.ring",
    # numpy / soundfile / sounddevice
    "--collect-submodules",
    "numpy",
    "--collect-submodules",
    "soundfile",
    "--collect-submodules",
    "sounddevice",
    "--collect-all",
    "_soundfile_data",
    # Ресурсы
    "--add-data",
    f"{ROOT / 'python' / 'msc'}:msc",
]
for mod in EXCLUDES:
    args += ["--exclude-module", mod]

# Не тянем оконные библиотеки GUI.
args += ["--exclude-module", "libtk"]
args += ["--exclude-module", "PIL"]

if __name__ == "__main__":
    run(args)
