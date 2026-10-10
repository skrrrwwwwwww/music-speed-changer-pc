"""Экспорт обработанного трека в файл.

Два режима:
  * ``rubberband`` — ffmpeg с фильтром rubberband (высокое качество, offline);
  * ``engine``     — наш собственный движок (гарантированно 1:1 то, что слышно).
"""

from __future__ import annotations

import os
import subprocess
from typing import Callable, Optional

import numpy as np

from .sources import FFMPEG, have_ffmpeg
from .stream import StreamPath
from .sources import open_source

Progress = Callable[[float], None]

CODECS = {
    "mp3": ["-c:a", "libmp3lame", "-b:a", "256k"],
    "m4a": ["-c:a", "aac", "-b:a", "256k"],
    "aac": ["-c:a", "aac", "-b:a", "256k"],
    "opus": ["-c:a", "libopus", "-b:a", "192k"],
    "ogg": ["-c:a", "libvorbis", "-q:a", "5"],
    "flac": ["-c:a", "flac", "-compression_level", "5"],
    "wav": ["-c:a", "pcm_s16le"],
    "opus.ogg": ["-c:a", "libopus", "-b:a", "192k"],
}

# Диапазон tempo в фильтре rubberband зависит от версии ffmpeg.
RUBBERBAND_TEMPO_MIN = 0.25
RUBBERBAND_TEMPO_MAX = 4.0


def rub_known_filters() -> bool:
    if not have_ffmpeg():
        return False
    try:
        out = subprocess.run(
            [FFMPEG, "-hide_banner", "-filters"], capture_output=True, timeout=20
        ).stdout.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return False
    return " rubberband " in out


def _guess_format(path: str) -> str:
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    return ext or "mp3"


def export(
    src_path: str,
    dst_path: str,
    speed: float = 1.0,
    pitch: float = 0.0,
    mode: str = "rubberband",
    start: Optional[float] = None,
    duration: Optional[float] = None,
    on_progress: Optional[Progress] = None,
    out_rate: int = 44100,
    formant: bool = False,
) -> dict:
    """Обрабатывает трек и сохраняет результат. Возвращает info результата."""
    if not have_ffmpeg():
        raise RuntimeError("ffmpeg не найден — экспорт невозможен")
    if mode == "rubberband" and rub_known_filters():
        return _export_ffmpeg(src_path, dst_path, speed, pitch, start, duration, on_progress, formant)
    return _export_engine(src_path, dst_path, speed, pitch, start, duration, on_progress, out_rate)


def _export_ffmpeg(
    src_path: str,
    dst_path: str,
    speed: float,
    pitch: float,
    start: Optional[float],
    duration: Optional[float],
    on_progress: Optional[Progress],
    formant: bool = False,
) -> dict:
    # В фильтре rubberband tempo и pitch — коэффициенты (1.0 = без изменений).
    tempo = float(max(0.01, min(100.0, speed)))
    pitch_f = float(2.0 ** (pitch / 12.0))
    filters = [
        f"rubberband=tempo={tempo:.6f}:pitch={pitch_f:.6f}"
        f":transients=crisp:smoothing=on:formant={'preserved' if formant else 'shifted'}"
    ]
    filters.append("aresample=resampler=soxr")

    cmd = [FFMPEG, "-nostdin", "-v", "error", "-y"]
    if start is not None:
        cmd += ["-ss", f"{max(0.0, start):.3f}"]
    cmd += ["-i", src_path]
    if duration:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-af", ",".join(filters)]
    cmd += CODECS.get(_guess_format(dst_path), CODECS["mp3"])
    cmd += [dst_path]
    _run_ffmpeg(cmd, on_progress)
    return {"mode": "rubberband", "path": dst_path}


def _export_engine(
    src_path: str,
    dst_path: str,
    speed: float,
    pitch: float,
    start: Optional[float],
    duration: Optional[float],
    on_progress: Optional[Progress],
    out_rate: int,
) -> dict:
    """Рендер нашим движком, кодирование ffmpeg'ом через stdin."""
    source = open_source(src_path)
    try:
        path = StreamPath(source, out_rate, speed=speed, pitch=pitch)
        if start:
            path.seek(int(start * source.samplerate))
        total = duration or max(0.0, source.duration - (start or 0.0))
        frames_target = int(total * out_rate / max(1e-6, speed))
        proc = subprocess.Popen(
            [FFMPEG, "-nostdin", "-v", "error", "-y",
             "-f", "f32le", "-ar", str(out_rate), "-ac", str(source.channels), "-i", "pipe:0"]
            + CODECS.get(_guess_format(dst_path), CODECS["mp3"])
            + [dst_path],
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        chunk = 1 << 15
        written = 0
        try:
            while written < frames_target:
                block = path.read(min(chunk, frames_target - written))
                if block.shape[1] == 0:
                    break
                proc.stdin.write(np.ascontiguousarray(block.T, dtype=np.float32).tobytes())
                written += block.shape[1]
                if on_progress and total:
                    on_progress(min(0.99, (written / out_rate) / max(1e-6, total / speed)))
            tail = path.flush()
            if tail.shape[0]:
                proc.stdin.write(np.ascontiguousarray(tail, dtype=np.float32).tobytes())
        finally:
            try:
                proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            err = proc.stderr.read() if proc.stderr else b""
            code = proc.wait()
        if code != 0:
            raise RuntimeError(f"ffmpeg: {err.decode('utf-8', 'replace').strip() or 'ошибка кодирования'}")
        if on_progress:
            on_progress(1.0)
        return {"mode": "engine", "path": dst_path, "frames": written}
    finally:
        source.close()


def _run_ffmpeg(cmd: list[str], on_progress: Optional[Progress]) -> None:
    proc = subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    # Точный прогресс из ffmpeg не требуется: шлём «занято» и сбрасываем в конце.
    if on_progress:
        on_progress(0.1)
    err = proc.communicate()[1]
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg: {(err or b'').decode('utf-8', 'replace').strip() or 'ошибка'}")
    if on_progress:
        on_progress(1.0)
