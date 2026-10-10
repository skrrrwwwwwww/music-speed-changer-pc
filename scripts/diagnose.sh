#!/usr/bin/env bash
# Диагностика запуска: собирает факты о том, как приложение стартует.
set -uo pipefail

echo "=== 1. Установлен ли пакет ==="
dpkg -l music-speed-changer 2>/dev/null | tail -1 || echo "не установлен (dpkg)"
flatpak list --app 2>/dev/null | grep -i "speed" || echo "flatpak: приложение не найдено"

echo
echo "=== 2. Есть ли исполняемый файл ==="
ls -la /opt/MusicSpeedChanger/music-speed-changer 2>&1 | head -1
ls -la /opt/MusicSpeedChanger/resources/bin/msc-engine 2>&1 | head -1

echo
echo "=== 3. Прямой запуск движка (5 секунд) ==="
ENG=/opt/MusicSpeedChanger/resources/bin/msc-engine
if [ -x "$ENG" ]; then
  printf '{"id":1,"cmd":"capabilities"}\n{"cmd":"quit"}\n' | timeout 12 "$ENG" --silent 2>&1 | head -3
else
  echo "движок не найден или не исполняем: $ENG"
fi

echo
echo "=== 4. Запуск приложения (в фоне, 10 c) ==="
APP=/opt/MusicSpeedChanger/music-speed-changer
"$APP" >/tmp/msc-diag.log 2>&1 &
APP_PID=$!
sleep 8

echo
echo "=== 5. Живой ли процесс и есть ли окно? ==="
if kill -0 "$APP_PID" 2>/dev/null; then
  echo "  процесс жив (pid $APP_PID) — приложение РАБОТАЕТ"
else
  echo "  процесс ЗАВЕРШИЛСЯ — смотрим лог ниже"
fi
DISPLAY=${DISPLAY:-:0}
xprop -root _NET_CLIENT_LIST 2>/dev/null | tr ',' '\n' | grep -oE '0x[0-9a-f]+' |
  while read -r w; do
    n=$(xprop -id "$w" WM_NAME 2>/dev/null | head -1)
    [ -n "$n" ] && echo "  $w $n"
  done | grep -i "music speed" || echo "  окно не найдено"

echo
echo "--- вывод приложения (без шума темы/VSync) ---"
grep -viE "fontconfig|vsyncparameters|gl_surface" /tmp/msc-diag.log 2>/dev/null | head -20
echo "--- конец вывода ---"

kill "$APP_PID" 2>/dev/null
sleep 1

echo
echo "=== 6. Среда (важно для Electron) ==="
echo "  XDG_SESSION_TYPE=${XDG_SESSION_TYPE:-?}"
echo "  WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-нет}"
echo "  DISPLAY=${DISPLAY:-нет}"
sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null |
  sed 's/^/  apparmor_restrict_unprivileged_userns=/' || true
echo "  ffmpeg: $(command -v ffmpeg || echo 'нет в PATH')"

echo
echo "=== 7. Доступ к звуку ==="
ls -la /dev/snd 2>&1 | head -4

echo
echo "=== 8. Лог приложения ==="
LOG="$HOME/.config/Music Speed Changer/logs/main.log"
LOG2="$HOME/.config/music-speed-changer/logs/main.log"
for f in "$LOG" "$LOG2"; do
  if [ -f "$f" ]; then
    echo "  найден: $f"
    tail -40 "$f"
    break
  fi
done
[ -f "$LOG" ] || [ -f "$LOG2" ] || echo "  лог не создан — приложение не дошло до main.js (см. пункт 4)"

echo
echo "=== 9. Есть ли незакрытые процессы (мешают single-instance)? ==="
pgrep -af "Music Speed Changer|msc-engine" | grep -v pgrep || echo "  процессов нет"
