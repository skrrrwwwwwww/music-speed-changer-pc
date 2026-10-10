"""Расчёт огибающей волны для отрисовки в UI."""

from __future__ import annotations

import base64

import numpy as np

from .sources import AudioSource


def compute_peaks(source: AudioSource, buckets: int = 1600) -> dict:
    """min/max (и RMS) на каждый бакет, по каждому каналу.

    Возвращает payload для отправки в Electron: base64 от int16
    (buckets, 2, channels) — это ~10 КБ на трек вместо ~200 КБ JSON.
    """
    src = source
    frames = max(1, int(src.frames))
    buckets = int(max(16, min(4096, buckets)))
    ch = src.channels

    lo = np.zeros((buckets, ch), dtype=np.float32)
    hi = np.zeros((buckets, ch), dtype=np.float32)
    rms = np.zeros((buckets, ch), dtype=np.float32)

    # Границы бакетов в кадрах.
    edges = np.linspace(0, frames, buckets + 1).astype(np.int64)
    pos = src.position
    src.seek(0)
    try:
        for i in range(buckets):
            start, end = int(edges[i]), int(edges[i + 1])
            if end <= start:
                continue
            need = end - start
            chunks = []
            got = 0
            while got < need:
                block = src.read(min(1 << 16, need - got))
                if block.shape[1] == 0:
                    break
                chunks.append(block)
                got += block.shape[1]
            if not chunks:
                break
            data = np.concatenate(chunks, axis=1)[:, :need]  # (ch, need)
            lo[i] = data.min(axis=1)
            hi[i] = data.max(axis=1)
            rms[i] = np.sqrt(np.mean(np.square(data, dtype=np.float64), axis=1))
    finally:
        src.seek(pos)

    packed = np.empty((buckets, 3, ch), dtype=np.int16)
    packed[:, 0, :] = np.clip(hi * 32767.0, -32768, 32767).astype(np.int16)  # max
    packed[:, 1, :] = np.clip(lo * 32767.0, -32768, 32767).astype(np.int16)  # min
    packed[:, 2, :] = np.clip(np.sqrt(rms) * 32767.0, 0, 32767).astype(np.int16)  # rms
    return {
        "buckets": buckets,
        "channels": ch,
        "rate": src.samplerate,
        "frames": int(frames),
        "data": base64.b64encode(packed.tobytes()).decode("ascii"),
    }
