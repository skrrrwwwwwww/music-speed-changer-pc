"""Открытие аудиофайлов: soundfile (быстрый, с seek) и ffmpeg (любой формат)."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Optional

import numpy as np

FFMPEG = os.environ.get("MSC_FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = os.environ.get("MSC_FFPROBE") or shutil.which("ffprobe") or "ffprobe"


def have_ffmpeg() -> bool:
    return shutil.which(FFMPEG) is not None or os.path.isfile(FFMPEG)


class AudioSource:
    """Общий интерфейс: чтение кадрами (n, channels) float32 + seek."""

    frames: int
    samplerate: int
    channels: int
    path: str

    def read(self, n: int) -> np.ndarray:  # pragma: no cover - интерфейс
        raise NotImplementedError

    def seek(self, frame: int) -> None:  # pragma: no cover - интерфейс
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - интерфейс
        pass

    @property
    def position(self) -> int:  # pragma: no cover - интерфейс
        raise NotImplementedError

    @property
    def duration(self) -> float:
        return self.frames / float(self.samplerate)


class SndFileSource(AudioSource):
    """Чтение через libsndfile.  Файл не декодируется целиком, seek нативный."""

    def __init__(self, path: str) -> None:
        import soundfile as sf  # локальный импорт: опциональная зависимость

        # always_2d — параметр read(), а не конструктора.
        self._f = sf.SoundFile(path, mode="r")
        self.path = path
        self.frames = int(self._f.frames)
        self.samplerate = int(self._f.samplerate)
        self.channels = int(self._f.channels)

    def read(self, n: int) -> np.ndarray:
        n = max(0, min(int(n), self.frames - self._f.tell()))
        if n <= 0:
            return np.zeros((self.channels, 0), dtype=np.float32)
        data = self._f.read(n, dtype="float32", always_2d=True)
        return np.ascontiguousarray(data.T)  # (channels, n)

    def seek(self, frame: int) -> None:
        self._f.seek(int(max(0, min(frame, self.frames))))

    @property
    def position(self) -> int:
        return int(self._f.tell())

    def close(self) -> None:
        try:
            self._f.close()
        except Exception:
            pass


class FFmpegSource(AudioSource):
    """Декодирование ffmpeg'ом во временный raw-файл + memmap.

    Даёт мгновенный и точный seek (важно для петли A-B) при любом формате.
    """

    def __init__(self, path: str, timeout: float = 120.0) -> None:
        info = _ffprobe(path)
        if info is None:
            raise RuntimeError("ffprobe не смог прочитать файл")
        self.samplerate = int(info["sample_rate"])
        self.channels = int(info["channels"])
        expected = int(round(info["duration"] * self.samplerate)) if info.get("duration") else 0

        fd, self._tmp = tempfile.mkstemp(prefix="msc-", suffix=".f32")
        os.close(fd)
        cmd = [
            FFMPEG, "-nostdin", "-v", "error", "-y", "-i", path,
            "-map", "0:a:0", "-f", "f32le", "-acodec", "pcm_f32le", "-",
        ]
        try:
            with open(self._tmp, "wb") as fh:
                subprocess.run(cmd, stdout=fh, stderr=subprocess.PIPE, check=True, timeout=timeout)
        except subprocess.CalledProcessError as exc:  # pragma: no cover
            detail = (exc.stderr or b"").decode("utf-8", "replace").strip()
            raise RuntimeError(f"ffmpeg: {detail or 'ошибка декодирования'}")
        except BaseException:
            self._unlink()
            raise

        self.frames = os.path.getsize(self._tmp) // (4 * self.channels)
        if expected and abs(self.frames - expected) > self.samplerate:  # расхождение длительности
            self.frames = min(self.frames, expected)
        self.path = path
        self._mm = np.memmap(self._tmp, dtype=np.float32, mode="r").reshape(-1, self.channels)
        self._pos = 0

    def read(self, n: int) -> np.ndarray:
        n = max(0, min(int(n), self.frames - self._pos))
        if n <= 0:
            return np.zeros((self.channels, 0), dtype=np.float32)
        block = np.asarray(self._mm[self._pos : self._pos + n])
        self._pos += n
        return np.ascontiguousarray(block.T)  # (channels, n)

    def seek(self, frame: int) -> None:
        self._pos = int(max(0, min(frame, self.frames)))

    @property
    def position(self) -> int:
        return self._pos

    def close(self) -> None:
        self._mm = None
        self._unlink()

    def _unlink(self) -> None:
        try:
            if getattr(self, "_tmp", None) and os.path.exists(self._tmp):
                os.unlink(self._tmp)
        except OSError:
            pass


class MemorySource(AudioSource):
    """Источник в памяти — для тестов и рендера."""

    def __init__(self, data: np.ndarray, samplerate: int) -> None:
        data = np.asarray(data, dtype=np.float32)
        if data.ndim == 1:
            data = data[None, :]
        self._data = data
        self.samplerate = int(samplerate)
        self.channels = int(data.shape[0])
        self.frames = int(data.shape[1])
        self.path = "<memory>"
        self._pos = 0

    def read(self, n: int) -> np.ndarray:
        n = max(0, min(int(n), self.frames - self._pos))
        if n <= 0:
            return np.zeros((self.channels, 0), dtype=np.float32)
        out = self._data[:, self._pos : self._pos + n]
        self._pos += n
        return out

    def seek(self, frame: int) -> None:
        self._pos = int(max(0, min(frame, self.frames)))

    @property
    def position(self) -> int:
        return self._pos


def _ffprobe(path: str) -> Optional[dict]:
    if not (shutil.which(FFPROBE) or os.path.isfile(FFPROBE)):
        return None
    cmd = [
        FFPROBE, "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=sample_rate,channels,duration,codec_name:format=duration",
        "-of", "default=noprint_wrappers=1:nokey=0", path,
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, check=True, timeout=30).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None
    info: dict = {}
    for line in out.decode("utf-8", "replace").splitlines():
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if key == "sample_rate":
            info["sample_rate"] = int(val)
        elif key == "channels":
            info["channels"] = int(val)
        elif key == "duration":
            try:
                info["duration"] = float(val)
            except ValueError:
                pass
        elif key == "codec_name":
            info["codec"] = val
    if "sample_rate" not in info or "channels" not in info:
        return None
    return info


def open_source(path: str, prefer: str = "auto") -> AudioSource:
    """Открывает файл: сначала soundfile, при неудаче — ffmpeg."""
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    order = ("soundfile", "ffmpeg") if prefer == "auto" else (prefer,)
    errors = []
    for kind in order:
        try:
            if kind == "soundfile":
                src = SndFileSource(path)
            else:
                src = FFmpegSource(path)
            if src.frames <= 0 or src.samplerate <= 0:
                src.close()
                raise RuntimeError("пустое аудио")
            return src
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{kind}: {exc}")
    raise RuntimeError("не удалось открыть файл (" + "; ".join(errors) + ")")


def metadata(path: str) -> dict:
    """Метаданные без декодирования (теги, битрейт, кодек)."""
    info = _ffprobe_meta(path)
    if info:
        return info
    try:
        import soundfile as sf

        with sf.SoundFile(path) as f:
            return {
                "samplerate": int(f.samplerate),
                "channels": int(f.channels),
                "duration": f.frames / float(f.samplerate),
                "codec": (f.format or "?") + "/" + (f.subtype or "?"),
            }
    except Exception:  # noqa: BLE001
        return {}


def _ffprobe_meta(path: str) -> dict:
    if not (shutil.which(FFPROBE) or os.path.isfile(FFPROBE)):
        return {}
    cmd = [
        FFPROBE, "-v", "error", "-select_streams", "a:0",
        "-show_entries",
        "stream=sample_rate,channels,duration,bit_rate,codec_name:stream_tags:"
        "format=duration,bit_rate,format_name:format_tags",
        "-of", "json", path,
    ]
    try:
        import json

        data = json.loads(subprocess.run(cmd, capture_output=True, check=True, timeout=30).stdout)
    except Exception:  # noqa: BLE001
        return {}
    streams = data.get("streams") or [{}]
    fmt = data.get("format") or {}
    st = streams[0]
    out = {
        "samplerate": int(st.get("sample_rate") or 0),
        "channels": int(st.get("channels") or 0),
        "duration": float(fmt.get("duration") or st.get("duration") or 0.0),
        "codec": st.get("codec_name") or "unknown",
        "container": fmt.get("format_name") or "",
        "bitrate": int(fmt.get("bit_rate") or st.get("bit_rate") or 0),
    }
    tags: dict = {}
    for block in (st.get("tags"), fmt.get("tags")):
        if isinstance(block, dict):
            tags.update({k.lower(): v for k, v in block.items()})
    for key in ("title", "artist", "album", "date", "genre"):
        if key in tags:
            out[key] = tags[key]
    # BPM встречается как TBPM (ID3) или bpm (Vorbis/FFmpeg-теги).
    for key in ("tbpm", "bpm"):
        if key in tags:
            try:
                bpm = float(str(tags[key]).replace(",", ".").split("/")[0])
            except (TypeError, ValueError):
                continue
            if 20.0 < bpm < 500.0:
                out["bpm"] = bpm
                break
    return out
