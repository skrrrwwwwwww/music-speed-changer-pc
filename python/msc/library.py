"""Библиотека треков: обход музыкальных папок, фильтр мусора, кэш.

Сканирование идёт в фоне и не блокирует интерфейс: список приходит
в renderer событием, как и остальное состояние.

Задача модуля — не найти всё подряд, а показать осмысленный список:
«Загрузки» по умолчанию выключены (это свалка: голосовые сообщения,
внутренние ресурсы приложений), а из обхода вырезаются известные
мусорные места.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

from .sources import metadata

# Форматы для библиотеки. .mp4/.3gp убраны: это видео-контейнеры, и в
# «Загрузках» они давали десятки мусорных файлов. Открыть такой файл
# по-прежнему можно через «Открыть» или перетаскивание.
AUDIO_EXT = {
    ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".oga", ".opus",
    ".wma", ".aiff", ".aif", ".ape", ".wv", ".mka", ".webm", ".amr", ".m4b",
}

# Папки-кандидаты: (имя, включать ли по умолчанию).
# Музыка — да; «Загрузки» — нет: там лежит всё подряд, и без фильтра
# список превращается в кашу из голосовых сообщений и файлов приложений.
ROOT_PRESETS: tuple[tuple[str, bool], ...] = (
    ("Музыка", True),
    ("Music", True),
    ("Загрузки", False),
    ("Downloads", False),
)

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".cache", ".venv", "venv",
    "dist", "build", "$RECYCLE.BIN", "System Volume Information", ".Trash-1000",
    "vendor", "site-packages", "__MACOSX",
}

# Голосовые сообщения Telegram и прочие «разговорные» папки.
SKIP_DIR_NAMES = {
    "voice_messages", "voice-messages", "voice messages", "audio_messages",
    "video_files", "video_documents", "temp", "tmp",
}

# Признаки того, что мы залезли внутрь распакованного приложения.
JUNK_PATH_MARKERS = (
    "/resources/app/", "/node_modules/", "/.git/", "/__pycache__/",
    "/.venv/", "/site-packages/", "/.next/", "/.cache/",
)

CONFIG_DIR = Path(os.path.expanduser("~")) / ".config" / "Music Speed Changer"
CACHE_FILE = CONFIG_DIR / "library.json"
SETTINGS_FILE = CONFIG_DIR / "library-roots.json"

_NUM_RE = re.compile(r"(\d+)")


def natural_key(text: str) -> tuple:
    """Ключ сортировки, где числа сравниваются как числа.

    Обычная сортировка ставит «10. Введение» перед «2. Основы», потому что
    сравнивает строки. Здесь получается порядок как у человека.
    """
    out: list[tuple[int, int, str]] = []
    for part in _NUM_RE.split(str(text).lower()):
        if not part:
            continue
        if part.isdigit():
            out.append((0, int(part), ""))
        else:
            out.append((1, 0, part))
    return tuple(out)


# ------------------------------------------------------------------ корни

def _resolve(path: str) -> str:
    try:
        return str(Path(path).expanduser().resolve())
    except OSError:
        return str(Path(path).expanduser())


def _home_presets() -> list[dict]:
    """Папки-кандидаты из домашнего каталога, с флагом «включена»."""
    home = Path.home()
    seen: set[str] = set()
    out: list[dict] = []

    xdg = os.environ.get("XDG_MUSIC_DIR")
    candidates: list[tuple[Path, bool]] = []
    if xdg:
        candidates.append((Path(xdg), True))
    for name, enabled in ROOT_PRESETS:
        candidates.append((home / name, enabled))

    for path, enabled in candidates:
        resolved = _resolve(str(path))
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append({"path": resolved, "enabled": enabled, "exists": False, "preset": True})
    return out


def _load_saved() -> list[dict]:
    if not SETTINGS_FILE.exists():
        return []
    try:
        raw = json.loads(SETTINGS_FILE.read_text("utf-8"))
    except Exception:  # noqa: BLE001
        return []
    # Старый формат: просто список путей. Считаем их включёнными,
    # чтобы не выключить то, что пользователь сам добавил раньше.
    if isinstance(raw, list):
        return [{"path": _resolve(str(p)), "enabled": True} for p in raw if isinstance(p, str)]
    if isinstance(raw, dict):
        return [
            {"path": _resolve(str(r.get("path", ""))), "enabled": bool(r.get("enabled", True))}
            for r in raw.get("roots", [])
            if isinstance(r, dict) and r.get("path")
        ]
    return []


def _save_known(roots: list[dict]) -> None:
    payload = {"roots": [{"path": r["path"], "enabled": bool(r["enabled"])} for r in roots]}
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
    except OSError:
        pass


def known_roots() -> list[dict]:
    """Все известные папки: из домашнего каталога плюс добавленные вручную."""
    out = _home_presets()
    by_path = {r["path"]: r for r in out}
    for saved in _load_saved():
        path = saved["path"]
        if path in by_path:
            # Пользовательский выбор главнее значения по умолчанию.
            by_path[path]["enabled"] = saved["enabled"]
        else:
            out.append({**saved, "exists": False, "preset": False})
    for r in out:
        try:
            r["exists"] = Path(r["path"]).is_dir()
        except OSError:
            r["exists"] = False
        r["name"] = Path(r["path"]).name or r["path"]
    return out


def user_roots() -> list[Path]:
    """Папки, которые реально сканируем: включённые и существующие."""
    return [Path(r["path"]) for r in known_roots() if r["enabled"] and r["exists"]]


def add_root(path: str, enabled: bool = True) -> list[dict]:
    roots = known_roots()
    resolved = _resolve(path)
    for r in roots:
        if r["path"] == resolved:
            r["enabled"] = enabled
            _save_known(roots)
            return roots
    roots.append({"path": resolved, "enabled": enabled, "exists": False, "preset": False,
                  "name": Path(resolved).name or resolved})
    _save_known(roots)
    return known_roots()


def set_root_enabled(path: str, enabled: bool) -> list[dict]:
    return add_root(path, enabled=enabled)


def remove_root(path: str) -> list[dict]:
    """Убирает только папку, добавленную вручную. Предустановленные не трогаем:
    их состояние «выключено» хранится отдельно и не должно исчезать."""
    resolved = _resolve(path)
    roots = known_roots()
    kept: list[dict] = []
    for r in roots:
        if r["path"] == resolved and not r.get("preset"):
            continue
        if r["path"] == resolved and not r["enabled"]:
            continue  # предустановленная выключенная — просто оставляем выключенной
        kept.append(r)
    _save_known(kept)
    return known_roots()


# ------------------------------------------------------------------- обход

def _is_junk(path: Path) -> bool:
    s = str(path)
    return any(marker in s for marker in JUNK_PATH_MARKERS)


def _walk(root: Path, limit_per_dir: int = 4000) -> Iterable[Path]:
    """Обход дерева с защитой от symlink-циклов и пропуском мусорных папок."""
    seen_dirs: set[tuple] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=lambda _e: None):
        here = Path(dirpath)
        try:
            st = here.stat()
            key = (st.st_dev, st.st_ino)
        except OSError:
            key = None
        if key is not None:
            if key in seen_dirs:
                dirnames[:] = []
                continue
            seen_dirs.add(key)
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIRS and d not in SKIP_DIR_NAMES and not d.startswith(".")
        ]
        if _is_junk(here):
            dirnames[:] = []
            continue
        count = 0
        for name in sorted(filenames):
            p = here / name
            if p.suffix.lower() in AUDIO_EXT:
                yield p
                count += 1
                if count >= limit_per_dir:
                    break


def duration_of(path: str) -> Optional[float]:
    """Длительность трека в секундах или None, если не определилась.

    ffprobe стоит около 40 мс на файл, поэтому длительности не читаются при
    сканировании — их досчитывает отдельный проход в фоне (см. DurationWorker).
    """
    try:
        meta = metadata(path)
    except Exception:  # noqa: BLE001
        return None
    d = meta.get("duration")
    try:
        d = float(d)
    except (TypeError, ValueError):
        return None
    return d if d > 0 else None


class DurationWorker:
    """Досчитывает длительности в фоне, порциями, с кэшем на диске.

    Кэш ключуется по (размер, mtime), поэтому повторные запуски почти мгновенны,
    а изменённые файлы пересчитываются сами.
    """

    def __init__(self, on_batch: Callable[[dict, bool], None]) -> None:
        self.on_batch = on_batch
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, tracks: list[dict], cache: Optional[dict] = None) -> bool:
        with self._lock:
            if self.running:
                return False
            self._thread = threading.Thread(
                target=self._run, args=(tracks, cache or {}), daemon=True, name="msc-dur"
            )
            self._thread.start()
            return True

    def _run(self, tracks: list[dict], cache: dict) -> None:
        found: dict[str, float] = {}
        stale = 0
        pending: list[dict] = []
        for t in tracks:
            key = t.get("path", "")
            hit = cache.get(key)
            if isinstance(hit, (list, tuple)) and len(hit) == 3:
                dur, size, mtime = hit
                if size == t.get("size") and abs(float(mtime) - float(t.get("mtime", 0))) < 1:
                    found[key] = float(dur)
                    continue
            pending.append(t)

        # Сначала отдаём то, что уже было в кэше: список отрисуется сразу.
        if found:
            self.on_batch(found, False)
        for t in pending:
            d = duration_of(t.get("path", ""))
            if d is None:
                continue
            found[t["path"]] = d
            stale += 1
            # Отдаём порциями, чтобы список наполнялся по ходу дела.
            if stale % 25 == 0:
                self.on_batch(found, False)
        if found:
            self.on_batch(found, True)


def load_duration_cache() -> dict:
    data = load_cache()
    cache = data.get("durations")
    return cache if isinstance(cache, dict) else {}


def save_duration_cache(tracks: list[dict], durations: dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        payload = {}
        by_path = {t.get("path"): t for t in tracks}
        for path, dur in durations.items():
            t = by_path.get(path)
            if t is not None:
                payload[path] = [round(float(dur), 3), t.get("size"), t.get("mtime")]
        data = load_cache()
        data["durations"] = payload
        CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    except Exception:  # noqa: BLE001
        pass


def scan(roots: Optional[list[Path]] = None) -> list[dict]:
    """Список треков: путь, имя, папка, размер. Сортировка «человеческая»."""
    roots = roots if roots is not None else user_roots()
    items: list[dict] = []
    seen: set[str] = set()
    for root in roots:
        try:
            if not root.is_dir():
                continue
        except OSError:
            continue
        for path in _walk(root):
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            try:
                stat = path.stat()
            except OSError:
                continue
            items.append(
                {
                    "path": key,
                    "name": path.stem,
                    "dir": str(path.parent),
                    "ext": path.suffix.lower().lstrip("."),
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                }
            )
    items.sort(key=lambda t: (natural_key(t["dir"]), natural_key(t["name"])))
    return items


def enrich(track: dict) -> dict:
    """Добавляет теги (название/артист) из метаданных файла."""
    try:
        meta = metadata(track["path"])
    except Exception:  # noqa: BLE001
        return track
    if meta:
        track["title"] = meta.get("title") or None
        track["artist"] = meta.get("artist") or None
        track["album"] = meta.get("album") or None
        track["duration"] = meta.get("duration") or None
        track["bpm"] = meta.get("bpm") or None
    return track


# --------------------------------------------------------------------- кэш

def load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text("utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {}


def save_cache(roots: list[Path], tracks: list[dict]) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(
            json.dumps(
                {"roots": [str(r) for r in roots], "tracks": tracks, "at": time.time()},
                ensure_ascii=False,
            ),
            "utf-8",
        )
    except OSError:
        pass


def cached_tracks() -> list[dict]:
    return load_cache().get("tracks", [])


def is_fresh(max_age: float = 600.0) -> bool:
    cache = load_cache()
    at = cache.get("at", 0)
    return bool(cache.get("tracks")) and (time.time() - at) < max_age


class LibraryWorker:
    """Фоновое сканирование с защитой от повторного запуска."""

    def __init__(self, on_done: Callable[[dict], None]) -> None:
        self.on_done = on_done
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, *, force: bool = False, roots: Optional[list[Path]] = None) -> bool:
        with self._lock:
            if self.running:
                return False
            self._thread = threading.Thread(target=self._run, args=(force, roots), daemon=True, name="msc-lib")
            self._thread.start()
            return True

    def _result(self, tracks: list[dict], cached: bool, roots: list[Path]) -> dict:
        return {
            "tracks": tracks,
            "cached": cached,
            "roots": known_roots(),
            "scanned": [str(r) for r in roots],
            "skipped": [r for r in known_roots() if not r["enabled"] and r["exists"]],
            "home": str(Path.home()),
            "duration_cache": load_duration_cache(),
        }

    def _run(self, force: bool, roots: Optional[list[Path]]) -> None:
        try:
            use_roots = roots if roots is not None else user_roots()
            if not force and is_fresh():
                self.on_done(self._result(cached_tracks(), True, use_roots))
                return
            tracks = scan(use_roots)
            save_cache(use_roots, tracks)
            self.on_done(self._result(tracks, False, use_roots))
        except Exception as exc:  # noqa: BLE001
            self.on_done(
                {
                    "tracks": [],
                    "error": str(exc),
                    "roots": known_roots(),
                    "scanned": [],
                    "skipped": [],
                    "home": str(Path.home()),
                }
            )