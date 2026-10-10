#!/usr/bin/env bash
# Подготовка ресурсов для сборки пакета:
#   * иконки (assets/icon.png и набор размеров);
#   * статический ffmpeg/ffprobe рядом с движком (необязательно).
set -euo pipefail
cd "$(dirname "$0")/.."

OS=$(uname -s | tr '[:upper:]' '[:lower:]')
ARCH=$(uname -m)
case "$ARCH" in
  x86_64 | amd64) FFM_ARCH=amd64 ;;
  aarch64 | arm64) FFM_ARCH=arm64 ;;
  *) echo "!! Неизвестная архитектура $ARCH — ffmpeg не будет приложен" ;;
esac

echo "==> Иконки"
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3
"$PY" scripts/make_icon.py assets >/dev/null
# electron-builder ждёт assets/icon.png (512x512)
cp -f assets/icon-512.png assets/icon.png
echo "    assets/icon.png готов"

echo "==> ffmpeg (статическая сборка)"
if [ -n "${FFM_ARCH:-}" ]; then
  if [ ! -x "build/ffmpeg/$OS/ffmpeg" ]; then
    URL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-${FFM_ARCH}-static.tar.xz"
    TMP=$(mktemp -d)
    if curl -fsSL --max-time 300 -o "$TMP/ff.tar.xz" "$URL"; then
      tar -xf "$TMP/ff.tar.xz" -C "$TMP"
      mkdir -p "build/ffmpeg/$OS"
      cp -f "$TMP"/ffmpeg-*-static/ffmpeg "build/ffmpeg/$OS/ffmpeg"
      cp -f "$TMP"/ffmpeg-*-static/ffprobe "build/ffmpeg/$OS/ffprobe"
      chmod +x "build/ffmpeg/$OS/ffmpeg" "build/ffmpeg/$OS/ffprobe"
      echo "    $(du -h "build/ffmpeg/$OS/ffmpeg" | cut -f1) + ffprobe"
    else
      echo "    !! не удалось скачать ffmpeg — будет использован системный"
    fi
    rm -rf "$TMP"
  else
    echo "    уже собран"
  fi
else
  mkdir -p "build/ffmpeg/$OS"
  echo "    пропущено для архитектуры $ARCH"
fi

echo
echo "Ресурсы готовы. Сборка пакета: npm run dist"
