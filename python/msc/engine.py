"""Движок транспорта: поток рендера + аудио-колбэк + состояние.

Поток рендера всегда опережает звуковую карту на несколько блоков, поэтому
переключение скорости/тона слышно сразу.  Аудио-колбэк (real-time) делает
только memcpy из очереди — вся DSP-работа выполняется вне него.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Optional

import numpy as np

from . import sources as src_mod
from .peaks import compute_peaks
from .sources import AudioSource
from .stream import StreamPath, clamp_pitch, clamp_speed

BLOCK = 1024
QUEUE_BLOCKS = 8
LOW_WATER = 3
FADE_SAMPLES = 256  # ~6 мс: сглаживает склейку на границе петли


class QueuedBlock:
    __slots__ = ("audio", "src_pos", "rate")

    def __init__(self, audio: np.ndarray, src_pos: float, rate: float) -> None:
        self.audio = audio
        self.src_pos = src_pos  # позиция в исходнике на начало блока, сек
        self.rate = rate  # сек исходника на 1 сек выхода


def _edge_fade(block: np.ndarray, fade_in: bool, fade_out: bool) -> np.ndarray:
    n = block.shape[0]
    k = min(FADE_SAMPLES, n)
    if k <= 1:
        return block
    ramp = (np.linspace(0.0, 1.0, k, dtype=np.float32) ** 0.6)[:, None]
    if fade_in:
        block[:k] *= ramp
    if fade_out:
        block[n - k :] *= ramp[::-1]
    return block


class Engine:
    def __init__(
        self,
        silent: bool = False,
        device: Optional[str] = None,
        phase_lock: bool = True,
        rate: Optional[int] = None,
        channels: Optional[int] = None,
    ) -> None:
        self.silent = bool(silent)
        self.device_name = device
        self.phase_lock = bool(phase_lock)
        self.device_info: dict = {}
        self._forced_rate = int(rate) if rate else None
        self._forced_channels = int(channels) if channels else None

        self.lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()

        self.source: Optional[AudioSource] = None
        self.info: dict = {}
        self._peaks_payload: Optional[dict] = None

        self.rate = 44100
        self.channels = 2
        self._stream: Optional[StreamPath] = None
        self._stream_pos = 0.0
        self._queue: deque[QueuedBlock] = deque()
        self._playing = False
        self._paused = True
        self._eof = False
        self._pos_override: Optional[float] = None
        self._heard: Optional[tuple] = None  # (src_pos, rate)
        self._heard_at = 0.0
        self._underruns = 0
        self._ended = threading.Event()

        self.speed = 1.0
        self.pitch = 0.0
        self.volume = 0.9
        self.loop_enabled = False
        self.loop_start = 0.0
        self.loop_end = 0.0
        self.repeat = False

        self._stream_thread: Optional[threading.Thread] = None
        self._clock_thread: Optional[threading.Thread] = None
        self._audio_out: Optional[object] = None
        self.peaks_ready = threading.Event()
        self.peaks_error: Optional[str] = None

        self._detect_device()
        self._start_workers()

    # ------------------------------------------------------------- устройство
    def _detect_device(self) -> None:
        if self.silent:
            self.rate = self._forced_rate or 44100
            self.channels = self._forced_channels or 2
            self.device_info = {"name": "(без звука)", "samplerate": float(self.rate), "channels": self.channels}
            return
        try:
            import sounddevice as sd

            dev = None
            if self.device_name:
                for d in sd.query_devices():
                    if d.get("name") == self.device_name:
                        dev = d
                        break
            if dev is None:
                dev = sd.query_devices(kind="output")
            self.rate = int(round(float(dev["default_samplerate"])))
            self.channels = 2 if int(dev["max_output_channels"]) >= 2 else 1
            self.device_info = {
                "name": dev.get("name", ""),
                "channels": int(dev["max_output_channels"]),
                "samplerate": float(dev["default_samplerate"]),
            }
        except Exception as exc:  # noqa: BLE001
            self.silent = True
            self.rate = self._forced_rate or 44100
            self.channels = self._forced_channels or 2
            self.device_info = {"error": str(exc)}

    def _start_workers(self) -> None:
        if not self.silent:
            try:
                self._open_stream()
            except Exception as exc:  # noqa: BLE001
                self.silent = True
                self.device_info = {"error": str(exc)}
        self._stream_thread = threading.Thread(target=self._render_loop, name="msc-render", daemon=True)
        self._stream_thread.start()
        if self.silent:
            self._clock_thread = threading.Thread(target=self._silent_clock, name="msc-clock", daemon=True)
            self._clock_thread.start()

    def _open_stream(self) -> None:
        import sounddevice as sd

        def cb(outdata, frames, time_info, status):  # noqa: ANN001
            self._fill(outdata)

        self._audio_out = sd.OutputStream(
            samplerate=self.rate,
            channels=self.channels,
            dtype="float32",
            blocksize=BLOCK,
            callback=cb,
        )
        self._audio_out.start()

    # ------------------------------------------------------------------ load
    def load(self, path: str) -> dict:
        with self.lock:
            self._close_locked()
            source = src_mod.open_source(path)
            meta = src_mod.metadata(path)
            self.source = source
            self.speed, self.pitch = 1.0, 0.0
            self.loop_enabled = False
            self.loop_start = self.loop_end = 0.0
            self._pos_override = 0.0
            self._stream = StreamPath(source, self.rate, phase_lock=self.phase_lock)
            self._stream.set_gain(self.volume)
            self._stream_pos = 0.0
            self._heard = (0.0, self.speed)
            self._heard_at = time.monotonic()
            self.info = {
                "path": path,
                "name": meta.get("title") or os.path.basename(path),
                "file": os.path.basename(path),
                "duration": source.duration,
                "samplerate": source.samplerate,
                "channels": source.channels,
                "meta": meta,
            }
        self.peaks_ready.clear()
        self.peaks_error = None
        threading.Thread(target=self._peaks_worker, args=(path,), name="msc-peaks", daemon=True).start()
        return self.info

    def _peaks_worker(self, path: str) -> None:
        payload = None
        try:
            source = src_mod.open_source(path)
            try:
                payload = compute_peaks(source, buckets=1600)
            finally:
                source.close()
        except Exception as exc:  # noqa: BLE001
            self.peaks_error = str(exc)
        else:
            with self.lock:
                if self.info.get("path") == path:
                    self._peaks_payload = payload
        self.peaks_ready.set()

    def take_peaks(self) -> Optional[dict]:
        if not self.peaks_ready.is_set():
            return None
        self.peaks_ready.clear()
        return self._peaks_payload

    def _close_locked(self) -> None:
        self._playing = False
        self._paused = True
        self._queue.clear()
        self._heard = None
        self._eof = False
        self._stream = None
        if self.source is not None:
            self.source.close()
            self.source = None

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        with self.lock:
            self._close_locked()
        if self._audio_out is not None:
            try:
                self._audio_out.stop()
                self._audio_out.close()
            except Exception:  # noqa: BLE001
                pass
            self._audio_out = None

    # -------------------------------------------------------------- транспорт
    def play(self) -> None:
        with self.lock:
            if self._stream is None:
                return
            if self._pos_override is not None:
                self._seek_locked(self._pos_override)
                self._pos_override = None
            self._paused = False
            self._playing = True
            self._ended.clear()
            self._prime_locked()
        self._wake.set()

    def _prime_locked(self) -> None:
        """Предзаполняет очередь, чтобы не было щелчка тишины на старте."""
        while len(self._queue) < QUEUE_BLOCKS and self._stream is not None:
            blocks = self._render_blocks(BLOCK)
            if not blocks:
                break
            for block in blocks:
                self._queue.append(block)

    def pause(self) -> None:
        with self.lock:
            if not self._playing:
                self._paused = True
                return
            self._pos_override = self._position_locked()
            self._paused = True
            self._playing = False
        self._wake.set()

    def stop(self) -> None:
        with self.lock:
            self._pos_override = 0.0
            self._paused = True
            self._playing = False
            self._seek_locked(0.0)
        self._wake.set()

    def seek(self, seconds: float) -> None:
        with self.lock:
            self._pos_override = float(max(0.0, seconds))
            self._seek_locked(self._pos_override)
            self._paused = not self._playing
            if self._playing:
                self._prime_locked()
        self._wake.set()

    def _seek_locked(self, seconds: float) -> None:
        if self._stream is None or self.source is None:
            return
        dur = self.source.duration
        seconds = float(max(0.0, min(seconds, max(0.0, dur - 0.005))))
        self._stream.seek(int(seconds * self.source.samplerate))
        self._stream_pos = seconds
        self._queue.clear()
        self._eof = False
        self._heard = (seconds, self.speed)
        self._heard_at = time.monotonic()

    def set(self, param: str, value) -> None:
        with self.lock:
            if param == "speed":
                self.speed = clamp_speed(float(value))
                if self._stream:
                    self._stream.set_speed(self.speed)
            elif param == "pitch":
                self.pitch = clamp_pitch(float(value))
                if self._stream:
                    self._stream.set_pitch(self.pitch)
            elif param == "volume":
                self.volume = float(max(0.0, min(2.0, value)))
                if self._stream:
                    self._stream.set_gain(self.volume)
            elif param == "loop":
                self.loop_enabled = bool(value)
            elif param == "loopRange":
                a, b = (list(value) + [0.0, 0.0])[:2]
                self.loop_start = float(max(0.0, a))
                self.loop_end = float(max(0.0, b))
            elif param == "repeat":
                self.repeat = bool(value)
            elif param == "phaseLock":
                self.phase_lock = bool(value)
                if self._stream:
                    self._stream.set_phase_lock(self.phase_lock)
        self._wake.set()

    def toggle_loop(self) -> bool:
        with self.lock:
            if not self.loop_enabled and self.loop_end <= self.loop_start:
                dur = self.source.duration if self.source else 0.0
                self.loop_start = max(0.0, dur - 8.0)
                self.loop_end = dur
            self.loop_enabled = not self.loop_enabled
            return self.loop_enabled

    # -------------------------------------------------------------- позиция
    def _position_locked(self) -> float:
        if self._pos_override is not None:
            return self._pos_override
        if self._heard is None:
            return self._stream_pos
        src_pos, rate = self._heard
        dt = time.monotonic() - self._heard_at
        dt = max(0.0, min(dt, 4.0 * BLOCK / self.rate))
        return src_pos + dt * rate

    def position(self) -> float:
        with self.lock:
            return self._position_locked()

    def state(self) -> dict:
        with self.lock:
            dur = self.source.duration if self.source else 0.0
            pos = self._position_locked()
            return {
                "playing": self._playing and not self._paused,
                "position": pos,
                "duration": dur,
                "speed": self.speed,
                "pitch": self.pitch,
                "volume": self.volume,
                "channels": self.channels,
                "rate": self.rate,
                "silent": self.silent,
                "buffering": len(self._queue) >= QUEUE_BLOCKS,
                "underruns": self._underruns,
                "ended": self._ended.is_set(),
                "repeat": self.repeat,
                "phaseLock": self.phase_lock,
                "loop": {
                    "enabled": self.loop_enabled,
                    "start": min(self.loop_start, dur) if dur else self.loop_start,
                    "end": min(self.loop_end, dur) if dur else self.loop_end,
                },
                "hasTrack": self._stream is not None,
            }

    # -------------------------------------------------------- поток рендера
    def _render_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(0.05)
            self._wake.clear()
            while not self._stop.is_set() and len(self._queue) < QUEUE_BLOCKS:
                with self.lock:
                    if self._stream is None or self._paused or not self._playing:
                        break
                    blocks = self._render_blocks(BLOCK)
                for block in blocks:
                    self._queue.append(block)
                if not blocks:
                    break
                # Даём карте догнать темп рендера, если очередь опустела.
                if len(self._queue) < LOW_WATER:
                    time.sleep(0.002)
            with self.lock:
                if self._eof and not self._queue and self._playing and not self._paused:
                    self._on_track_end()

    def _render_blocks(self, n: int) -> list[QueuedBlock]:
        """Рендерит до n выходных семплов с учётом петли и конца трека."""
        st, source = self._stream, self.source
        if st is None or source is None:
            return []
        out: list[QueuedBlock] = []
        produced = 0
        sr_src = float(source.samplerate)
        dev_rate = float(self.rate)
        rho = sr_src / dev_rate
        looping = self.loop_enabled and self.loop_end > self.loop_start + 1e-3
        while produced < n:
            want = n - produced
            at_loop_edge = False
            if looping:
                # Сколько ВЫХОДНЫХ сэмплов осталось до конца петли: за sek
                # исходника выходит sek * speed секунд аудио.
                remaining_out = (self.loop_end - self._stream_pos) * self.speed
                if remaining_out <= 1e-3:
                    st.seek(int(self.loop_start * sr_src))
                    self._stream_pos = self.loop_start
                    continue
                frames_left = int(remaining_out * dev_rate)
                if frames_left < want:
                    want = frames_left
                    at_loop_edge = True
            start_pos = st.source_pos
            block = st.read(want)
            made = int(block.shape[1])
            if made < want:  # дошли до конца трека
                tail = st.flush()
                if tail.shape[0]:
                    block = np.concatenate((block, tail), axis=1)
                    made = int(block.shape[1])
                self._eof = True
            if made == 0:
                self._eof = True
                break
            if block.shape[0] < self.channels:  # моно-источник -> стерео-карта
                block = np.repeat(block, self.channels, axis=0)
            elif block.shape[0] > self.channels:
                block = block[: self.channels]
            frames_ch = np.ascontiguousarray(block.T)
            if at_loop_edge:
                _edge_fade(frames_ch, True, True)
            end_pos = st.source_pos
            seconds = made / dev_rate
            rate = (end_pos - start_pos) / sr_src / seconds if seconds > 0 else self.speed
            out.append(QueuedBlock(frames_ch, start_pos / sr_src, rate))
            self._stream_pos = end_pos / sr_src
            produced += made
            if at_loop_edge:
                # Возвращаемся к началу петли на следующей итерации.
                st.seek(int(self.loop_start * sr_src))
                self._stream_pos = self.loop_start
            if self._eof:
                break
        return out

    def _on_track_end(self) -> None:
        self._ended.set()
        with self.lock:
            dur = self.source.duration if self.source else 0.0
            if self.repeat:
                self._seek_locked(0.0)
                self._paused = False
                self._playing = True
                self._ended.clear()
                self._prime_locked()
            else:
                self._pos_override = max(0.0, dur - 0.01)
                self._playing = False
                self._paused = True
        self._wake.set()

    # ---------------------------------------------------------- аудио-колбэк
    def _fill(self, outdata: np.ndarray) -> None:
        """Только чтение из очереди: без блокировок и тяжёлых операций."""
        frames = int(outdata.shape[0])
        filled = 0
        first = True
        while filled < frames:
            if not self._queue:
                if filled < frames:
                    outdata[filled:] = 0.0
                    if self._playing:
                        self._underruns += 1
                self._heard = None
                # Будим рендерер: иначе он спит до конца таймаута и будет тишина.
                self._wake.set()
                return
            item = self._queue.popleft()
            take = min(frames - filled, item.audio.shape[0])
            outdata[filled : filled + take] = item.audio[:take]
            if take < item.audio.shape[0]:
                self._queue.appendleft(
                    QueuedBlock(item.audio[take:], item.src_pos + take / self.rate * item.rate, item.rate)
                )
            if len(self._queue) <= LOW_WATER:
                # Буфер на исходе — просим рендерер добавить ещё.
                self._wake.set()
            if first:
                # В момент вызова колбэка предыдущий буфер доигран —
                # значит звук на колонках ровно на границе нового блока.
                self._heard = (item.src_pos, item.rate)
                self._heard_at = time.monotonic()
                first = False
            filled += take

    def _silent_clock(self) -> None:
        """Виртуальные часы: очередь расходуется в реальном времени без карты."""
        step = BLOCK / float(self.rate)
        while not self._stop.is_set():
            time.sleep(step)
            with self.lock:
                running = self._playing and not self._paused
            if not running or not self._queue:
                continue
            item = self._queue.popleft()
            self._heard = (item.src_pos, item.rate)
            self._heard_at = time.monotonic()
            with self.lock:
                if self._eof and not self._queue:
                    self._on_track_end()
