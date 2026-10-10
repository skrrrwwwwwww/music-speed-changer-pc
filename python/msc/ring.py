"""Кольцевой буфер для одноканальных float32-семплов."""

from __future__ import annotations

import numpy as np

EMPTY = np.empty(0, dtype=np.float32)


def _next_pow2(n: int) -> int:
    p = 1
    while p < n:
        p <<= 1
    return p


class RingFifo:
    """FIFO с произвольным доступом на чтение по порядку, без аллокаций при
    записи (кроме случая нехватки места — тогда вытесняется самое старое)."""

    __slots__ = ("_buf", "_cap", "_r", "_w", "_size")

    def __init__(self, capacity: int = 1 << 14) -> None:
        self._cap = _next_pow2(max(64, int(capacity)))
        self._buf = np.zeros(self._cap, dtype=np.float32)
        self._r = 0
        self._w = 0
        self._size = 0

    def __len__(self) -> int:
        return self._size

    @property
    def available(self) -> int:
        return self._size

    def clear(self) -> None:
        self._r = self._w = self._size = 0

    def write(self, x) -> None:
        if x is None:
            return
        x = np.asarray(x, dtype=np.float32)
        n = int(x.size)
        if n == 0:
            return
        if n >= self._cap:
            # Оставляем только хвост.
            x = x[-self._cap :]
            n = self._cap
            self._r = self._w
            self._size = 0
        free = self._cap - self._size
        if n > free:
            drop = n - free
            self._r = (self._r + drop) % self._cap
            self._size -= drop
        w = self._w
        first = min(n, self._cap - w)
        self._buf[w : w + first] = x[:first]
        if n > first:
            self._buf[: n - first] = x[first:]
        self._w = (w + n) % self._cap
        self._size += n

    def read(self, n: int) -> np.ndarray:
        """Возвращает до n семплов (меньше — только если буфер пуст)."""
        n = int(min(n, self._size))
        if n <= 0:
            return EMPTY
        r = self._r
        first = min(n, self._cap - r)
        if first == n:
            out = self._buf[r : r + n].copy()
        else:
            out = np.empty(n, dtype=np.float32)
            out[:first] = self._buf[r : r + first]
            out[first:] = self._buf[: n - first]
        self._r = (r + n) % self._cap
        self._size -= n
        return out

    def peek_all(self) -> np.ndarray:
        if self._size == 0:
            return EMPTY
        r = self._r
        first = min(self._size, self._cap - r)
        if first == self._size:
            return self._buf[r : r + first].copy()
        return np.concatenate((self._buf[r : r + first], self._buf[: self._size - first]))
