"""Цепочка обработки: чтение -> смена темпа (фазовый вокодер) -> сдвиг тона.

    источник (sr_src)
        -> sinc(sr_src / sr_dev)        приведение к частоте карты
        -> phase vocoder (rate = speed/p)  темп меняется, тон сохраняется
        -> sinc(p)                      сдвиг тона на p = 2^(полутон/12)

Потребление исходника на один выходной семпл:

    n_src = speed * sr_src / sr_dev

поэтому позиция playhead считается аналитически и точно совпадает со звуком.

Знаки сдвига тона выбраны как в Music Speed Changer: положительные
полутона — тональность выше (сильнее голос/скрипка), отрицательные — ниже.
"""

from __future__ import annotations

import numpy as np

from .phasevocoder import PhaseVocoder
from .ring import EMPTY, RingFifo
from .sinc import SincResampler
from .sources import AudioSource

SPEED_MIN, SPEED_MAX = 0.25, 4.0
PITCH_MIN, PITCH_MAX = -12.0, 12.0
PV_N_FFT = 4096
PV_HOP = 256


def pitch_to_ratio(semitones: float) -> float:
    return float(2.0 ** (float(semitones) / 12.0))


def clamp_speed(v: float) -> float:
    return float(min(SPEED_MAX, max(SPEED_MIN, v)))


def clamp_pitch(v: float) -> float:
    return float(min(PITCH_MAX, max(PITCH_MIN, v)))


# --------------------------------------------------------------------------- io
class SourceFanout:
    """Читает источник блоками и раскладывает семплы по каналам в FIFO."""

    CHUNK = 4096

    def __init__(self, source: AudioSource, channels: int) -> None:
        self.src = source
        self.channels = int(channels)
        self.fifos = [RingFifo(1 << 16) for _ in range(self.channels)]
        self.eof = False
        self.drained = False

    def read_channel(self, ch: int, n: int) -> np.ndarray:
        """До n семплов канала; пустой результат означает конец файла."""
        fifo = self.fifos[ch]
        guard = 0
        while fifo.available < n and not self.eof and guard < 64:
            guard += 1
            block = self.src.read(self.CHUNK)
            if block.shape[1] == 0:
                self.eof = True
                self.drained = True
                break
            for c in range(self.channels):
                self.fifos[c].write(block[c])
        return fifo.read(n)

    def seek(self, frame: int) -> None:
        self.src.seek(frame)
        for fifo in self.fifos:
            fifo.clear()
        self.eof = False
        self.drained = False

    def clear(self) -> None:
        for fifo in self.fifos:
            fifo.clear()
        self.eof = False
        self.drained = False

    @property
    def position(self) -> int:
        return self.src.position


# ------------------------------------------------------------------------ stages
class _SincStage:
    """Ресемплинг с постоянным отношением (передискретизация / сдвиг тона)."""

    CHUNK = 4096

    def __init__(self, upstream, ratio: float) -> None:
        self.up = upstream
        self.rs = SincResampler()
        self.rs.set_ratio(ratio)
        self.ratio = float(ratio)
        self._pending = EMPTY
        self.finished = False

    @property
    def pending_out(self) -> int:
        return int(self._pending.size)

    @property
    def undelivered_in(self) -> float:
        """Входных (от upstream) семплов, принятых, но ещё не отданных дальше."""
        produced = float(self.pending_out) + float(self.rs.available)
        return produced / max(1e-6, self.ratio) + self.rs.input_ahead

    def pull(self, n: int) -> np.ndarray:
        n = int(n)
        if n <= 0:
            return EMPTY
        taps = self.rs.n_taps
        while self._pending.size < n and not self.finished:
            got = self.rs.read(self.rs.available)
            if got.size:
                self._pending = np.concatenate((self._pending, got))
            need = n - int(self._pending.size)
            want_in = int(need * self.ratio) + taps + 2
            x = self.up.pull(min(self.CHUNK, max(taps, want_in)))
            if x.size == 0:
                # Источник исчерпан: дочитываем всё, что осталось в ресемплере.
                self.finished = True
                got = self.rs.read(self.rs.available)
                if got.size:
                    self._pending = np.concatenate((self._pending, got))
                break
            self.rs.write(x)
        out = self._pending[:n]
        self._pending = self._pending[n:]
        return out

    def reset(self) -> None:
        self.rs.reset()
        self._pending = EMPTY
        self.finished = False


class _PvStage:
    """Фазовый вокодер: меняет темп, сохраняя тон."""

    def __init__(self, upstream, n_fft: int = PV_N_FFT, hop_syn: int = PV_HOP, phase_lock: bool = True) -> None:
        self.up = upstream
        self.pv = PhaseVocoder(n_fft=n_fft, hop_syn=hop_syn, phase_lock=phase_lock)
        self._pending = EMPTY
        self.finished = False

    @property
    def rate(self) -> float:
        return self.pv.rate

    @property
    def pending_out(self) -> int:
        """Уже синтезировано, но ещё не запрошено вниз по цепи."""
        return int(self._pending.size)

    def set_rate(self, rate: float) -> None:
        self.pv.set_rate(rate)

    def pull(self, n: int) -> np.ndarray:
        n = int(n)
        if n <= 0:
            return EMPTY
        need = self.pv.n_fft + 1
        while self._pending.size < n and not self.finished:
            x = self.up.pull(need)
            if x.size == 0:
                self.finished = True
                tail = self.pv.flush()
                if tail.size:
                    self._pending = np.concatenate((self._pending, tail))
                break
            y = self.pv.feed(x)
            if y.size:
                self._pending = np.concatenate((self._pending, y))
        out = self._pending[:n]
        self._pending = self._pending[n:]
        return out

    def reset(self) -> None:
        self.pv.reset()
        self._pending = EMPTY
        self.finished = False


# ----------------------------------------------------------------------- path
class StreamPath:
    """Готовая цепочка для всех каналов: read(n) -> (channels, n) float32."""

    def __init__(
        self,
        source: AudioSource,
        out_rate: int,
        speed: float = 1.0,
        pitch: float = 0.0,
        n_fft: int = PV_N_FFT,
        hop_syn: int = PV_HOP,
        phase_lock: bool = True,
    ) -> None:
        self.fanout = SourceFanout(source, source.channels)
        self.channels = source.channels
        self.out_rate = int(out_rate)
        self.sr_src = int(source.samplerate)
        self.rho = self.sr_src / float(self.out_rate)
        self.speed = clamp_speed(speed)
        self.pitch = clamp_pitch(pitch)
        self.gain = 1.0
        self._p = pitch_to_ratio(self.pitch)

        self._pre: list[_SincStage] = []
        self._pv: list[_PvStage] = []
        self._post: list[_SincStage] = []
        for ch in range(self.channels):
            pre = _SincStage(_ChannelView(self.fanout, ch), self.rho)
            pv = _PvStage(pre, n_fft=n_fft, hop_syn=hop_syn, phase_lock=phase_lock)
            post = _SincStage(pv, self._p)
            self._pre.append(pre)
            self._pv.append(pv)
            self._post.append(post)
        self._log_start = 0.0  # позиция источника (кадры) на момент последнего seek
        self._apply()

    @property
    def source_pos(self) -> float:
        """Позиция в исходнике (кадры), соответствующая следующему выходному
        семплу.  Считается по реально поглощённым кадрам вокодера, поэтому
        не зависит от того, насколько далеко вперёд прочитался источник."""
        if not self._pv:
            return self._log_start
        pv = self._pv[0].pv
        # Оба ступени «ниже» вокодера уже посчитали часть выхода — вычитаем
        # их упреждение, переведённый во входные семплы вокодера.
        lead = self._post[0].undelivered_in + self._pv[0].pending_out * pv.rate
        consumed = max(0.0, pv.consumed_in - lead)
        return self._log_start + consumed * self.rho

    # ------------------------------------------------------------- параметры
    def _apply(self) -> None:
        self._p = pitch_to_ratio(self.pitch)
        rate = self.speed / self._p
        for pv in self._pv:
            pv.set_rate(rate)
        for post in self._post:
            post.rs.set_ratio(self._p)

    def set_speed(self, speed: float) -> None:
        self.speed = clamp_speed(speed)
        self._apply()

    def set_pitch(self, pitch: float) -> None:
        self.pitch = clamp_pitch(pitch)
        self._apply()

    def set_gain(self, gain: float) -> None:
        self.gain = float(gain)

    def set_phase_lock(self, enabled: bool) -> None:
        for pv in self._pv:
            pv.pv.phase_lock = bool(enabled)

    # -------------------------------------------------------------- буферы
    def reset_buffers(self) -> None:
        for stage in (*self._pre, *self._pv, *self._post):
            stage.reset()
        self.fanout.clear()

    def seek(self, frame: int) -> None:
        frame = int(max(0, frame))
        self._log_start = float(frame)
        self.fanout.seek(frame)
        self.reset_buffers()

    @property
    def eof(self) -> bool:
        return all(st.finished for st in self._post)

    # --------------------------------------------------------------- чтение
    def read(self, n: int) -> np.ndarray:
        n = int(n)
        if n <= 0:
            return np.zeros((self.channels, 0), dtype=np.float32)
        outs = [st.pull(n) for st in self._post]
        k = min(o.size for o in outs)
        if k <= 0:
            return np.zeros((self.channels, 0), dtype=np.float32)
        if k < n:  # выравниваем каналы на общем минимуме
            outs = [o[:k] for o in outs]
        out = np.empty((self.channels, k), dtype=np.float32)
        for ch, o in enumerate(outs):
            out[ch] = o
        if self.gain != 1.0:
            out *= np.float32(self.gain)
        return out

    def flush(self) -> np.ndarray:
        """Хвост трека: допроизводит накопленное и сливает буферы."""
        parts = []
        while True:
            block = self.read(1 << 16)
            if block.shape[1] == 0:
                break
            parts.append(block.T)
        if not parts:
            return np.zeros((self.channels, 0), dtype=np.float32)
        return np.ascontiguousarray(np.concatenate(parts, axis=0))


class _ChannelView:
    """Адаптер: «источник» одного канала поверх общей fanout-раздачи."""

    __slots__ = ("fanout", "ch")

    def __init__(self, fanout: SourceFanout, ch: int) -> None:
        self.fanout = fanout
        self.ch = ch

    def read(self, n: int) -> np.ndarray:
        return self.fanout.read_channel(self.ch, n)

    pull = read
