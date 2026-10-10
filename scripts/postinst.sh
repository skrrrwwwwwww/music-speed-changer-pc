#!/bin/sh
# postinst: Electron требует SUID-песочницу (chrome-sandbox) с правами 4755.
# electron-builder не выставляет их автоматически, поэтому делаем это сами.
set -e

# Каталог установки — БЕЗ пробелов. Chromium местами режет путь по
# пробелам при запуске дочерних процессов (зигот, рендерер) и падает с
# "failed to execvp: /opt/Music". Имя в меню задаётся отдельно, в .desktop.
APP_DIR="/opt/MusicSpeedChanger"

SANDBOX="$APP_DIR/chrome-sandbox"

if [ -f "$SANDBOX" ]; then
    chown root:root "$SANDBOX" 2>/dev/null || true
    chmod 4755 "$SANDBOX" 2>/dev/null || true
fi

# Старый каталог с пробелом в имени мог остаться от прежних сборок.
if [ -d "/opt/Music Speed Changer" ]; then
    echo "postinst: удаляю прежний каталог /opt/Music Speed Changer" >&2
    rm -rf "/opt/Music Speed Changer" 2>/dev/null || true
fi

# Запуск из терминала: music-speed-changer.
# electron-builder создаёт ссылку через update-alternatives, но при
# переустановке пакета она не восстанавливается — делаем сами.
APP="$APP_DIR/music-speed-changer"
if [ -x "$APP" ]; then
    ln -sf "$APP" /usr/bin/music-speed-changer 2>/dev/null || true
fi

# Иконки в кэше рабочего стола (иначе после установки иконка не видна сразу).
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -f -t /usr/share/icons/hicolor 2>/dev/null || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications 2>/dev/null || true
fi

exit 0
