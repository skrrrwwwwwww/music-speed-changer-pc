#!/usr/bin/env bash
# Сборка .deb для Ubuntu.
#
#   ./scripts/build_deb.sh              — обычный пакет (~150 МБ), ffmpeg из системы
#   MSC_BUNDLE_FFMPEG=1 ./scripts/...  — автономный пакет (~230 МБ) со вложенным ffmpeg
#
# Требуется: dpkg, fakeroot (для подписи) и зависимости из scripts/setup.sh.
set -euo pipefail
cd "$(dirname "$0")/.."

BUNDLE_FFMPEG="${MSC_BUNDLE_FFMPEG:-0}"
VERSION=$(node -p "require('./package.json').version")
ARCH=$(node -p "process.arch === 'x64' ? 'amd64' : process.arch")
OS=$(node -p "process.platform === 'darwin' ? 'mac' : process.platform === 'win32' ? 'win' : 'linux'")

echo "==> Ресурсы"
./scripts/prepare_assets.sh

echo "==> Аудиодвижок (PyInstaller)"
# Пересобираем, если бинаря нет ИЛИ хоть один исходник python новее него.
# Раньше проверяли только существование файла, и правки в python/msc/*.py
# молча попадали в пакет в старом виде.
ENGINE=build/pyinstaller/msc-engine
if [ ! -x "$ENGINE" ]; then
  .venv/bin/python scripts/build_engine.py
else
  NEWEST=$(find python/msc scripts/build_engine.py -name '*.py' -newer "$ENGINE" -print -quit 2>/dev/null || true)
  if [ -n "$NEWEST" ]; then
    echo "    исходник новее движка: $NEWEST"
    .venv/bin/python scripts/build_engine.py
  else
    echo "    уже собран: $(du -h "$ENGINE" | cut -f1)"
  fi
fi
chmod +x build/pyinstaller/msc-engine

# Конфиг electron-builder: при необходимости добавляем ffmpeg в resources.
CFG="build/electron-builder.deb.yml"
node -e '
const fs = require("fs");
const pkg = require("./package.json");
const cfg = JSON.parse(JSON.stringify(pkg.build));
const bundle = process.env.MSC_BUNDLE_FFMPEG === "1";
if (bundle) {
  cfg.extraResources.push(
    { from: "build/ffmpeg/${os}/ffmpeg", to: "bin/ffmpeg" },
    { from: "build/ffmpeg/${os}/ffprobe", to: "bin/ffprobe" }
  );
  cfg.linux.deb.recommends = (cfg.linux.deb.recommends || []).filter((d) => d !== "ffmpeg");
}
fs.writeFileSync(process.argv[1], JSON.stringify(cfg, null, 2));
console.log(bundle ? "    ffmpeg будет вложен в пакет" : "    ffmpeg берётся из системы (Recommends)");
' "$CFG"

echo "==> Сборка .deb"
npx electron-builder --linux deb --config "$CFG" "$@"

DEB=$(ls -t dist/*.deb 2>/dev/null | head -1 || true)
if [ -z "$DEB" ]; then
  echo "!! Пакет не собран" >&2
  exit 1
fi

echo
echo "==> Готово: $DEB"
ls -lh "$DEB"
echo
echo "Проверка метаданных:"
dpkg-deb -I "$DEB" | sed -n '2,14p'
echo
echo "Установка:"
echo "  sudo apt install \"$DEB\"      # из локального файла"
echo "  sudo dpkg -i \"$DEB\"        # только dpkg"
