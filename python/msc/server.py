"""IPC-мост Electron <-> Python: newline-delimited JSON по stdin/stdout.

Команда:  {"id": 1, "cmd": "set", "param": "speed", "value": 1.5}
Ответ:    {"id": 1, "ok": true}
Событие:  {"ev": "state", ...}

Важно: в stdout пишется ТОЛЬКО протокол, поэтому sys.stdout перенаправляется
в stderr на уровне модуля, а протоколный writer держит отдельный дескриптор.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from . import exporter
from . import library as library_mod
from .engine import Engine
from .sources import have_ffmpeg

STATE_INTERVAL = 0.05


def _drain() -> None:
    """Никакого вывода в stdout, кроме протокола."""
    sys.stdout = sys.stderr
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001
        pass


class Writer:
    """Пишет NDJSON в исходный stdout (sys.stdout уже перенаправлен в stderr)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.stream = sys.__stdout__

    def send(self, obj: dict) -> None:
        line = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            try:
                self.stream.write(line)
                self.stream.flush()
            except (BrokenPipeError, ValueError):
                pass


class Server:
    def __init__(self, silent: bool = False, device: Optional[str] = None) -> None:
        self.writer = Writer()
        self.commands: "queue.Queue[dict]" = queue.Queue()
        self.engine = Engine(silent=silent, device=device)
        self._last_state: Optional[dict] = None
        self._export_thread: Optional[threading.Thread] = None
        self._export_lock = threading.Lock()
        self._library: dict = {"tracks": [], "roots": [], "scanning": False}
        self.lib_worker = library_mod.LibraryWorker(self._on_library)
        self.running = True

    # ------------------------------------------------------------- библиотека
    def _on_library(self, result: dict) -> None:
        # Держим все поля результата (tracks, roots, home, skipped…): раньше
        # здесь брали только три, и интерфейс не получал, например, home —
        # из-за чего в списке вместо «~/Музыка» показывался полный путь.
        self._library.update(result)
        self._library["scanning"] = False
        self.emit("library", **self._library)

    def library_state(self) -> dict:
        self._library.setdefault("home", str(Path.home()))
        if not self._library["tracks"] and not self.lib_worker.running:
            self.lib_worker.start(force=False)
            self._library["scanning"] = True
        return dict(self._library)

    # ----------------------------------------------------------------- вход
    def _reader(self) -> None:
        stream = sys.__stdin__
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict):
                self.commands.put(msg)
        self.commands.put({"cmd": "__eof__"})

    def emit(self, ev: str, **payload: Any) -> None:
        self.writer.send({"ev": ev, **payload})

    # --------------------------------------------------------------- команды
    def dispatch(self, msg: dict) -> None:
        cmd = msg.get("cmd", "")
        mid = msg.get("id")
        try:
            result = self.handle(cmd, msg)
        except Exception as exc:  # noqa: BLE001
            self.reply(mid, ok=False, error=f"{type(exc).__name__}: {exc}")
            return
        if mid is not None:
            self.reply(mid, ok=True, result=result)

    def reply(self, mid: Any, ok: bool, **payload: Any) -> None:
        self.writer.send({"id": mid, "ok": ok, **payload})

    def handle(self, cmd: str, msg: dict):
        eng = self.engine
        if cmd == "ping":
            return {"pong": time.time()}
        if cmd == "load":
            path = msg.get("path", "")
            info = eng.load(path)
            # Автозапуск: файл пришёл из проводника, и пользователь хочет,
            # чтобы он сразу заиграл. Делаем здесь, а не в UI — тогда старт
            # воспроизведения не зависит от того, успел ли отрисоваться UI.
            if msg.get("autoplay"):
                eng.play()
                info = dict(info, autoplay=True)
            return info
        if cmd == "play":
            eng.play()
            return None
        if cmd == "pause":
            eng.pause()
            return None
        if cmd == "stop":
            eng.stop()
            return None
        if cmd == "seek":
            eng.seek(float(msg.get("position", 0.0)))
            return None
        if cmd == "set":
            eng.set(str(msg.get("param", "")), msg.get("value"))
            return None
        if cmd == "toggleLoop":
            return {"enabled": eng.toggle_loop()}
        if cmd == "state":
            return eng.state()
        if cmd == "devices":
            return list_devices()
        if cmd == "library":
            return self.library_state()
        if cmd == "libraryScan":
            force = bool(msg.get("force"))
            started = self.lib_worker.start(force=force)
            return {"started": started, "running": self.lib_worker.running}
        if cmd == "libraryAddRoot":
            roots = library_mod.add_root(str(msg.get("path", "")))
            self.lib_worker.start(force=True)
            return {"roots": roots}
        if cmd == "libraryRemoveRoot":
            roots = library_mod.remove_root(str(msg.get("path", "")))
            self.lib_worker.start(force=True)
            return {"roots": roots}
        if cmd == "librarySetRoot":
            roots = library_mod.set_root_enabled(
                str(msg.get("path", "")), bool(msg.get("enabled", True))
            )
            self.lib_worker.start(force=True)
            return {"roots": roots}
        if cmd == "libraryInfo":
            return {"info": library_mod.enrich(dict(msg.get("track") or {}))}
        if cmd == "selectDevice":
            # Устройство применяется при перезапуске процесса (main.js перезапускает).
            return {"device": msg.get("name", ""), "restart": True}
        if cmd == "capabilities":
            return {
                "speedRange": [0.25, 4.0],
                "pitchRange": [-12.0, 12.0],
                "ffmpeg": have_ffmpeg(),
                "rubberband": exporter.rub_known_filters(),
                "device": eng.device_info,
                "silent": eng.silent,
                "rate": eng.rate,
                "channels": eng.channels,
            }
        if cmd == "export":
            return self.start_export(msg)
        if cmd == "quit":
            self.running = False
            return None
        raise ValueError(f"неизвестная команда: {cmd}")

    def start_export(self, msg: dict) -> dict:
        with self._export_lock:
            if self._export_thread and self._export_thread.is_alive():
                raise RuntimeError("экспорт уже выполняется")
            args = {
                "src_path": msg.get("src") or self.engine.info.get("path"),
                "dst_path": msg.get("path"),
                "speed": float(msg.get("speed", 1.0)),
                "pitch": float(msg.get("pitch", 0.0)),
                "mode": str(msg.get("mode", "rubberband")),
                "start": msg.get("start"),
                "duration": msg.get("duration"),
                "out_rate": self.engine.rate,
            }
            if not args["src_path"]:
                raise RuntimeError("не загружен трек")
            if not args["dst_path"]:
                raise RuntimeError("не указан путь сохранения")

            def worker() -> None:
                def progress(value: float) -> None:
                    self.emit("exportProgress", value=round(float(value), 3))

                try:
                    self.emit("exportStarted", path=args["dst_path"])
                    info = exporter.export(on_progress=progress, **args)
                except Exception as exc:  # noqa: BLE001
                    self.emit("exportFailed", message=f"{type(exc).__name__}: {exc}")
                else:
                    self.emit("exported", **info)

            self._export_thread = threading.Thread(target=worker, name="msc-export", daemon=True)
            self._export_thread.start()
        return {"started": True, "path": args["dst_path"]}

    # -------------------------------------------------------------- main loop
    def run(self) -> None:
        threading.Thread(target=self._reader, name="msc-stdin", daemon=True).start()
        self.emit(
            "ready",
            pid=os.getpid(),
            rate=self.engine.rate,
            channels=self.engine.channels,
            silent=self.engine.silent,
            device=self.engine.device_info,
            ffmpeg=have_ffmpeg(),
            rubberband=exporter.rub_known_filters(),
        )
        while self.running:
            try:
                msg = self.commands.get(timeout=STATE_INTERVAL)
            except queue.Empty:
                msg = None
            if msg is not None:
                if msg.get("cmd") == "__eof__":
                    break
                self.dispatch(msg)
            self.tick()
        self.engine.close()

    def tick(self) -> None:
        state = self.engine.state()
        peaks = self.engine.take_peaks()
        if peaks is not None:
            self.emit("peaks", **peaks)
        if state != self._last_state:
            self._last_state = state
            self.emit("state", state=state)


def list_devices() -> list[dict]:
    """Устройства вывода PortAudio для выбора в настройках."""
    try:
        import sounddevice as sd
    except Exception:  # noqa: BLE001
        return []
    out = []
    try:
        for i, d in enumerate(sd.query_devices()):
            out.append(
                {
                    "index": i,
                    "name": d.get("name", ""),
                    "out": int(d.get("max_output_channels", 0)),
                    "in": int(d.get("max_input_channels", 0)),
                    "rate": float(d.get("default_samplerate", 0.0)),
                    "hostapi": d.get("hostapi", 0),
                    "default": bool(d.get("default_samplerate", 0)),
                }
            )
    except Exception:  # noqa: BLE001
        return []
    return out


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    silent = "--silent" in argv
    device = None
    if "--device" in argv:
        i = argv.index("--device")
        if i + 1 < len(argv):
            device = argv[i + 1]
    _drain()
    server = Server(silent=silent, device=device)
    try:
        server.run()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    return 0
