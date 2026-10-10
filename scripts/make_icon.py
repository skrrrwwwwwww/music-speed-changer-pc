#!/usr/bin/env python3
"""Генерация иконок приложения (PNG) без внешних зависимостей.

Рисует те же мотивы, что и интерфейс: тёмный фон, циферблат темпа
и стрелка-указатель. Пишет 512x512 (ico-совместимый master) и 256x256.
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

import numpy as np

BG_TOP = np.array([0x1B, 0x21, 0x33], dtype=np.float64)
BG_BOTTOM = np.array([0x10, 0x12, 0x19], dtype=np.float64)
ARC_FROM = np.array([0x4E, 0xA1, 0xFF], dtype=np.float64)
ARC_TO = np.array([0x7C, 0x5C, 0xFF], dtype=np.float64)
TRACK = np.array([0x26, 0x2C, 0x3A], dtype=np.float64)
KNOB = np.array([0x17, 0x1B, 0x26], dtype=np.float64)
WHITE = np.array([0xE8, 0xEC, 0xF5], dtype=np.float64)


def _blend(img: np.ndarray, color: np.ndarray, mask: np.ndarray) -> None:
    """Плавное наложение цвета по маске 0..1 (сглаживание antialias)."""
    a = mask[..., None]
    img *= 1.0 - a
    img += color * a


def _rounded_square(size: int, radius: int) -> np.ndarray:
    y, x = np.mgrid[0:size, 0:size].astype(np.float64)
    # расстояние до «скруглённого» прямоугольника
    dx = np.maximum(radius - x, x - (size - 1 - radius))
    dy = np.maximum(radius - y, y - (size - 1 - radius))
    dx = np.maximum(dx, 0.0)
    dy = np.maximum(dy, 0.0)
    dist = np.sqrt(dx * dx + dy * dy) - radius
    return np.clip(0.5 - dist, 0.0, 1.0)  # мягкий край


def _ring(size: int, cx: float, cy: float, radius: float, width: float) -> np.ndarray:
    y, x = np.mgrid[0:size, 0:size].astype(np.float64)
    d = np.sqrt((x - cx) ** 2 + (y - cy) ** 2)
    return np.clip(width / 2.0 - np.abs(d - radius) + 0.5, 0.0, 1.0)


def _disc(size: int, cx: float, cy: float, radius: float) -> np.ndarray:
    y, x = np.mgrid[0:size, 0:size].astype(np.float64)
    d = np.sqrt((x - cx) ** 2 + (y - cy) ** 2)
    return np.clip(radius - d + 0.5, 0.0, 1.0)


def _arc(size: int, cx: float, cy: float, radius: float, width: float, t: float) -> tuple:
    """Дуга от 225° до 225°+270°*t (как у ручки темпа)."""
    y, x = np.mgrid[0:size, 0:size].astype(np.float64)
    d = np.sqrt((x - cx) ** 2 + (y - cy) ** 2)
    band = np.clip(width / 2.0 - np.abs(d - radius) + 0.5, 0.0, 1.0)
    ang = np.degrees(np.arctan2(y - cy, x - cx)) % 360.0
    a0, a1 = 225.0, 225.0 + 270.0
    rel = (ang - a0) % 360.0
    span = 270.0
    on_arc = rel <= span
    # мягкие края дуги
    edge = np.minimum(rel, np.maximum(span - rel, 0.0))
    m = band * on_arc * np.clip(edge / 2.0, 0.0, 1.0)
    # цвет по углу: градиент от голубого к фиолетовому
    g = np.clip(rel / span, 0.0, 1.0)[..., None]
    color = ARC_FROM * (1 - g) + ARC_TO * g
    return m, color


def _needle(size: int, cx: float, cy: float, angle_deg: float, length: float, half_w: float) -> np.ndarray:
    y, x = np.mgrid[0:size, 0:size].astype(np.float64)
    a = np.radians(angle_deg)
    # поворот координат в систему указателя
    px, py = x - cx, y - cy
    rx = px * np.cos(a) + py * np.sin(a)
    ry = -px * np.sin(a) + py * np.cos(a)
    # треугольник: остриё наружу (по +X), основание ближе к центру
    tip = np.array([length, 0.0])
    bl = np.array([-length * 0.45, -half_w])
    br = np.array([-length * 0.45, half_w])

    def side(p0: np.ndarray, p1: np.ndarray) -> np.ndarray:
        """Знак ориентированной площади относительно ребра p0->p1."""
        return (p1[0] - p0[0]) * (ry - p0[1]) - (p1[1] - p0[1]) * (rx - p0[0])

    s1 = side(tip, br)
    s2 = side(br, bl)
    s3 = side(bl, tip)
    inside = ((s1 >= 0) & (s2 >= 0) & (s3 >= 0)) | ((s1 <= 0) & (s2 <= 0) & (s3 <= 0))
    return inside.astype(np.float64)


def render(size: int) -> np.ndarray:
    img = np.zeros((size, size, 3), dtype=np.float64)
    y = np.linspace(0.0, 1.0, size)[:, None, None]
    img += BG_TOP * (1 - y) + BG_BOTTOM * y

    # скруглённый квадрат — маска всей иконки
    sq = _rounded_square(size, size * 0.22)

    cx = cy = size / 2.0
    R = size * 0.32
    width = size * 0.075

    # тёмная дорожка шкалы
    _blend(img, TRACK, _ring(size, cx, cy, R, width) * sq)
    # заполненная часть дуги
    m, color = _arc(size, cx, cy, R, width, 0.62)
    _blend(img, color, m * sq)
    # центральный диск
    _blend(img, KNOB, _disc(size, cx, cy, R - width * 1.2) * sq)
    # указатель: треугольник от центра к текущему значению шкалы
    _blend(
        img,
        WHITE,
        _needle(size, cx, cy, 225.0 + 270.0 * 0.62, R * 0.62, max(1.0, size * 0.019)) * sq,
    )

    img *= sq[..., None]
    return np.clip(img, 0, 255).astype(np.uint8)


def write_png(path: Path, rgb: np.ndarray) -> None:
    h, w, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[i].tobytes() for i in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("assets")
    out.mkdir(parents=True, exist_ok=True)
    for size in (512, 256, 128, 64):
        write_png(out / f"icon-{size}.png", render(size))
        print(f"  {out / f'icon-{size}.png'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
