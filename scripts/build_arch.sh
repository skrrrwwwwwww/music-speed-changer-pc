#!/usr/bin/env bash
# Сборка пакета для Arch Linux (.pkg.tar.zst).
#
#   ./scripts/build_arch.sh
#
# На Ubuntu ставится .deb (scripts/build_deb.sh), на Arch — этот пакет.
# Раскладка та же, что в .deb: /opt/MusicSpeedChanger без пробелов в пути.
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION=$(.venv/bin/python -c "import json;print(json.load(open('package.json'))['version'])")
APP=build/arch/stage/opt/MusicSpeedChanger
STAGE=build/arch/stage

echo "==> Версия $VERSION"

echo "==> Аудиодвижок (PyInstaller)"
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

echo "==> Иконки"
.venv/bin/python scripts/make_icon.py assets >/dev/null

echo "==> Раскладка"
rm -rf "$STAGE"
mkdir -p "$APP/resources/bin" "$APP/resources/app"
cp -a node_modules/electron/dist/. "$APP/"
rm -rf "$APP/resources"
mkdir -p "$APP/resources/bin" "$APP/resources/app"
# Бинарь Electron переименовываем: так его видно в списке процессов и в меню.
mv "$APP/electron" "$APP/music-speed-changer"

# Приложение (те же файлы, что electron-builder кладёт в app.asar).
cp -a electron renderer package.json "$APP/resources/app/"
install -m 755 "$ENGINE" "$APP/resources/bin/msc-engine"

# Иконки в тему hicolor.
for size in 64 128 256 512; do
  install -Dm 644 "assets/icon-${size}.png" \
    "$STAGE/usr/share/icons/hicolor/${size}x${size}/apps/music-speed-changer.png"
done

install -Dm 644 LICENSE "$STAGE/usr/share/licenses/music-speed-changer/LICENSE" 2>/dev/null || true

echo "==> .desktop"
mkdir -p "$STAGE/usr/share/applications"
cat > "$STAGE/usr/share/applications/music-speed-changer.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=Music Speed Changer
Name[ru]=Music Speed Changer
GenericName=Audio Speed and Pitch Changer
GenericName[ru]=Изменение темпа и тона
Comment=Меняйте темп и тон аудио в реальном времени
Comment[ru]=Меняйте темп и тон аудио в реальном времени
Exec=/opt/MusicSpeedChanger/music-speed-changer %U
Icon=music-speed-changer
Terminal=false
StartupNotify=true
Categories=AudioVideo;Audio;Player;
Keywords=audio;speed;tempo;pitch;music;player;
Keywords[ru]=аудио;темп;тон;скорость;музыка;плеер;
MimeType=audio/mpeg;audio/mp4;audio/x-m4a;audio/aac;audio/ogg;audio/opus;audio/flac;audio/wav;audio/x-wav;audio/webm;
DESKTOP

echo "==> PKGBUILD"
mkdir -p build/arch/pkg
cat > build/arch/pkg/PKGBUILD <<PKGBUILD
pkgname=music-speed-changer
pkgver=$VERSION
pkgrel=1
pkgdesc="Изменение темпа и тона аудио в реальном времени"
arch=('x86_64')
url="https://github.com/skrrrwwwwwww/music-speed-changer-pc"
license=('MIT')
depends=('gtk3' 'nss' 'nspr' 'alsa-lib' 'libpulse' 'libcups' 'libdrm' 'mesa' 'libxkbcommon'
         'libx11' 'libxcomposite' 'libxdamage' 'libxext' 'libxfixes' 'libxrandr' 'libxcb'
         'at-spi2-core' 'dbus' 'expat' 'glib2' 'glibc' 'pango' 'cairo' 'systemd-libs'
         'ffmpeg' 'portaudio' 'hicolor-icon-theme')
options=(!strip)
install=music-speed-changer.install

package() {
  cp -a "\$startdir/../stage/." "\$pkgdir/"
  install -d "\$pkgdir/usr/bin"
  ln -s /opt/MusicSpeedChanger/music-speed-changer "\$pkgdir/usr/bin/music-speed-changer"
}
PKGBUILD

cat > build/arch/pkg/music-speed-changer.install <<'INSTALL'
post_install() {
  # Electron требует SUID-песочницу, иначе падает с "No usable sandbox".
  chown root:root /opt/MusicSpeedChanger/chrome-sandbox 2>/dev/null || true
  chmod 4755 /opt/MusicSpeedChanger/chrome-sandbox 2>/dev/null || true
  gtk-update-icon-cache -q -t /usr/share/icons/hicolor 2>/dev/null || true
  update-desktop-database -q /usr/share/applications 2>/dev/null || true
}

post_upgrade() {
  post_install
}
INSTALL

echo "==> makepkg"
cd build/arch/pkg
rm -f ./*.pkg.tar.zst
makepkg -f --noconfirm >/dev/null
PKG=$(ls -t ./*.pkg.tar.zst | head -1)
mkdir -p ../../../dist
cp -f "$PKG" "../../../dist/"
cd ../../..
echo "==> Готово: dist/$(basename "$PKG")"
ls -lh "dist/$(basename "$PKG")" | awk '{print $5, $9}'
