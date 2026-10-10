"""Ресемплер на основе оконной sinc-функции (Kaiser).

Нужен для двух задач:
  * приведение частоты дискретизации источника к частоте звуковой карты;
  * сдвиг высоты тона (pitch shift) «на лету», с мгновенной реакцией на ручку.

Реализация полностью векторизована: за раз обрабатывается блок позиций,
интерполяция — линейная между табличными сдвигами окна (kernel window
interpolation), фаза квантуется на `phases` позиций, каждая строка таблицы
нормирована на единицу, поэтому при ratio == 1.0 сигнал проходит без
изменения уровня.
"""

from __future__ import annotations

import numpy as np

from .ring import EMPTY, RingFifo


class SincResampler:
    """Переменный (и постоянный) ресемплинг с сохранением длины тона.

    ratio = sr_источника / sr_выхода.  ratio > 1 -> сигнал читается быстрее
    (тот же эффект, что уменьшение длительности без смены тона).
    """

    def __init__(self, n_taps: int = 32, phases: int = 64, beta: float = 8.6, chunk: int = 512) -> None:
        if n_taps % 2:
            n_taps += 1
        self.n_taps = int(n_taps)
        self.half = self.n_taps // 2
        self.phases = int(phases)
        self.chunk = int(chunk)
        self.ratio = 1.0
        self._offs = np.arange(-self.half + 1, self.half + 1, dtype=np.int64)
        self._tab = self._build_table(beta)
        self._x = np.zeros(0, dtype=np.float32)
        self._pos = float(self.half - 1)  # дробная позиция чтения
        self._out = RingFifo(1 << 15)

    # ------------------------------------------------------------------ table
    def _build_table(self, beta: float) -> np.ndarray:
        p = np.arange(self.phases, dtype=np.float64) / self.phases
        u = np.arange(self.n_taps, dtype=np.float64)[None, :] - (self.half - 1) - p[:, None]
        win_arg = np.maximum(0.0, 1.0 - (2.0 * u / (self.n_taps - 1)) ** 2)
        win = np.i0(beta * np.sqrt(win_arg)) / np.i0(beta)
        tab = np.sinc(u) * win
        tab /= tab.sum(axis=1, keepdims=True)  # ровно единичный выход на любой фазе
        return tab.astype(np.float32)

    # ------------------------------------------------------------------- api
    def set_ratio(self, ratio: float) -> None:
        if ratio > 1e-6:
            self.ratio = float(ratio)

    def reset(self) -> None:
        self._x = np.zeros(0, dtype=np.float32)
        self._pos = float(self.half - 1)
        self._out.clear()

    def clear(self) -> None:
        self.reset()

    @property
    def available(self) -> int:
        return self._out.available

    @property
    def input_ahead(self) -> float:
        """Входных семплов, прочитанных впереди текущей позиции чтения."""
        return max(0.0, float(self._x.size) - self._pos)

    def write(self, x) -> None:
        if x is not None and len(x):
            self._x = np.concatenate((self._x, np.asarray(x, dtype=np.float32)))
            self._process()

    def read(self, n: int) -> np.ndarray:
        return self._out.read(n)

    # ---------------------------------------------------------------- internal
    def _process(self) -> None:
        ratio = self.ratio
        half = self.half
        x = self._x
        while True:
            max_i0 = x.size - 1 - half
            if self._pos > max_i0:
                break
            n = int((max_i0 - self._pos) / ratio) + 1
            if n <= 0:
                break
            n = min(n, self.chunk)
            i0f = self._pos + ratio * np.arange(n, dtype=np.float64)
            base = np.floor(i0f).astype(np.int64)
            tap = ((i0f - base) * self.phases).astype(np.int64)
            np.minimum(tap, self.phases - 1, out=tap)
            idx = base[:, None] + self._offs[None, :]
            kernel = self._tab[tap]
            seg = x[idx]
            self._out.write(np.einsum("ij,ij->i", kernel, seg, dtype=np.float32))
            self._pos = float(i0f[-1]) + ratio
            # Подрезаем съеденную историю, оставляя окно фильтра.
            keep = int(np.floor(self._pos)) - half + 1
            if keep > 8192:
                x = self._x = x[keep:]
                self._pos -= keep
