"""Потоковый фазовый вокодер (time-stretch с сохранением тона).

Особенности:
  * фиксированный синтезирующий хоп (hop_syn) и переменный анализирующий
    хоп = round(hop_syn * rate).  Благодаря этому COLA-нормализация постоянна,
    а смена скорости не требует пересборки внутренних буферов;
  * оценка истинной частоты по отклонению фазы (стандартный phase-vocoder);
  * identity phase locking — фазы в окрестности пика спектра «приклеиваются»
    к пику, что заметно снижает размазывание на транзиентах;
  * потоковый интерфейс feed()/flush() — обработка идёт блоками в реальном
    времени, без предварительного рендера всего файла.
"""

from __future__ import annotations

import numpy as np

TWO_PI = 2.0 * np.pi


class PhaseVocoder:
    def __init__(self, n_fft: int = 4096, hop_syn: int = 256, phase_lock: bool = True) -> None:
        if n_fft % hop_syn:
            n_fft = (n_fft // hop_syn) * hop_syn
        self.n_fft = int(n_fft)
        self.hs = int(hop_syn)
        self.phase_lock = bool(phase_lock)

        n = np.arange(self.n_fft, dtype=np.float64)
        # Периодическое Ханна — даёт точное COLA при хопе n_fft / hop_syn.
        self.win = (0.5 - 0.5 * np.cos(TWO_PI * n / self.n_fft)).astype(np.float32)

        # Нормализация перекрывающегося сложения: берём участок, где
        # перекрываются все n_fft/hop_syn + 1 окон (иначе сумма не постоянна).
        acc = np.zeros(2 * self.n_fft + self.hs, dtype=np.float64)
        w2 = self.win.astype(np.float64) ** 2
        for k in range(self.n_fft // self.hs + 1):
            acc[k * self.hs : k * self.hs + self.n_fft] += w2
        self._norm = float(np.mean(acc[self.n_fft : self.n_fft + self.hs]))
        self._inv_norm = np.float32(1.0 / self._norm)

        self.k = np.arange(self.n_fft // 2 + 1, dtype=np.float64)
        self.omega = TWO_PI * self.k / self.n_fft
        self._ha = self.hs
        self.reset()

    # ------------------------------------------------------------------- api
    def set_rate(self, rate: float) -> None:
        rate = max(1e-3, float(rate))
        self._ha = max(1, int(round(self.hs * rate)))

    @property
    def rate(self) -> float:
        return self._ha / self.hs

    @property
    def input_backlog(self) -> int:
        """Входных семплов в буфере (меньше n_fft — окно уже почти заполнено)."""
        return int(self._x.size)

    def reset(self) -> None:
        self._x = np.zeros(0, dtype=np.float32)
        self._acc = np.zeros(self.n_fft + self.hs, dtype=np.float32)
        self._prev = None
        self._syn = None
        self.consumed_in = 0  # входных семплов поглощено с последнего сброса
        self.emitted_out = 0  # выходных семплов выдано с последнего сброса

    def feed(self, x) -> np.ndarray:
        """Скормить входные семплы, получить (возможно, пустой) выход."""
        if x is not None and len(x):
            self._x = np.concatenate((self._x, np.asarray(x, dtype=np.float32)))
        out = []
        while self._x.size >= self.n_fft:
            frame = self._x[: self.n_fft]
            self._acc[: self.n_fft] += self._synthesize(frame)
            out.append(self._acc[: self.hs])
            self._acc = np.concatenate((self._acc[self.hs :], np.zeros(self.hs, dtype=np.float32)))
            self._x = self._x[self._ha :]
            self.consumed_in += self._ha
            self.emitted_out += self.hs
        if not out:
            return np.empty(0, dtype=np.float32)
        return np.concatenate(out) * self._inv_norm

    def flush(self) -> np.ndarray:
        """Хвост трека: дорендериваем неполное окно и сливаем накопитель.

        Остаток накопителя затухает сам (покрытие окон уменьшается), поэтому
        дополнительного фейда не требуется.
        """
        parts = []
        if self._x.size:
            # Остаток входа доклеиваем одним нулёвым окном.
            y = self.feed(np.zeros(self.n_fft - int(self._x.size), dtype=np.float32))
            if y.size:
                parts.append(y)
        rest = self._acc * self._inv_norm
        self._acc = np.zeros(self.n_fft + self.hs, dtype=np.float32)
        self._prev = None
        self._syn = None
        self._x = np.zeros(0, dtype=np.float32)
        if rest.size:
            parts.append(rest)
        return np.concatenate(parts) if parts else np.empty(0, dtype=np.float32)

    # ---------------------------------------------------------------- internal
    def _synthesize(self, frame: np.ndarray) -> np.ndarray:
        spec = np.fft.rfft(frame * self.win)
        mag = np.abs(spec)
        ph = np.angle(spec)

        if self._prev is None:
            syn = ph
        else:
            dev = ph - self._prev - self.omega * self._ha
            dev -= TWO_PI * np.rint(dev / TWO_PI)
            true_omega = self.omega + dev / self._ha
            syn = self._syn + true_omega * self.hs
            syn = np.mod(syn + np.pi, TWO_PI) - np.pi
            if self.phase_lock:
                syn = self._lock(syn, ph, mag)
        self._prev = ph
        self._syn = syn
        y = np.fft.irfft(mag * np.exp(1j * syn), self.n_fft)
        return (y * self.win).astype(np.float32)

    def _lock(self, syn: np.ndarray, ph: np.ndarray, mag: np.ndarray) -> np.ndarray:
        """Identity phase locking: сохраняем относительные фазы внутри пика."""
        nb = mag.size
        if nb < 3:
            return syn
        mid = mag[1:-1]
        peaks = np.flatnonzero((mid > mag[:-2]) & (mid > mag[2:]) & (mid > 0.0)) + 1
        if peaks.size == 0:
            return syn
        idx = np.arange(nb)
        owner = np.searchsorted(peaks, idx, side="right") - 1
        np.maximum(owner, 0, out=owner)
        pk = peaks[owner]
        return syn[pk] + (ph - ph[pk])
