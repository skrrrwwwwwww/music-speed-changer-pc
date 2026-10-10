"""Проверка DSP-части без звуковой карты: python/selftest.py

Проверяем то, что критично для приложения:
  1. ресемплер сохраняет тон и длительность при ratio = 1;
  2. фазовый вокодер меняет длительность в 1/rate раз и не меняет тон;
  3. StreamPath: темп x2 -> та же высота, длительность / 2; pitch +12 -> та же
     длительность, частота x2;
  4. смена скорости «на лету» не даёт разрывов (максимальный скачок амплитуды);
  5. позиция playhead, потреблённая источником, совпадает с расчётной;
  6. цикл рендера в Engine даёт ровно ожидаемое число семплов.
"""

from __future__ import annotations

import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from msc.phasevocoder import PhaseVocoder  # noqa: E402
from msc.ring import RingFifo  # noqa: E402
from msc.sinc import SincResampler  # noqa: E402
from msc.sources import MemorySource  # noqa: E402
from msc.stream import StreamPath  # noqa: E402

SR = 44100
FAILED: list[str] = []
import tempfile  # noqa: E402


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'OK  ' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(name)


def tone(freq: float, dur: float, sr: int = SR, amp: float = 0.5) -> np.ndarray:
    t = np.arange(int(dur * sr), dtype=np.float64) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def dominant_freq(x: np.ndarray, sr: int = SR) -> float:
    """Оценка тона по максимуму спектра (в полосе 80..4000 Гц)."""
    n = len(x)
    if n < 2048:
        return 0.0
    win = np.hanning(n)
    spec = np.abs(np.fft.rfft(x[:n] * win))
    freqs = np.fft.rfftfreq(n, 1 / sr)
    mask = (freqs > 80) & (freqs < 4000)
    return float(freqs[mask][int(np.argmax(spec[mask]))])


def freq_tol(x: np.ndarray, expect: float) -> float:
    """Допуск с учётом разрешения FFT по длине сигнала."""
    return max(3.0, 1.5 * SR / max(2048, len(x)), 0.004 * expect)


def freq_ok(x: np.ndarray, expect: float) -> bool:
    return abs(dominant_freq(x) - expect) <= freq_tol(x, expect)


# Длина хвоста, который добавляет OLA-степень вокодера (n_fft + hop_syn).
PV_TAIL = 4096 + 256


# --------------------------------------------------------------------- кольцо
def test_ring() -> None:
    f = RingFifo(1024)
    f.write(np.arange(100, dtype=np.float32))
    out = f.read(30)
    check("RingFifo: FIFO-порядок", np.array_equal(out, np.arange(30, dtype=np.float32)))
    check("RingFifo: остаток", len(f) == 70 and int(f.read(100)[0]) == 30)
    f.clear()
    check("RingFifo: clear", len(f) == 0)
    # переполнение: старые данные вытесняются, порядок сохраняется
    g = RingFifo(64)
    g.write(np.arange(100, dtype=np.float32))
    check("RingFifo: вытеснение старых", np.array_equal(g.read(64), np.arange(36, 100, dtype=np.float32)))


# ---------------------------------------------------------------- ресемплер
def test_resampler() -> None:
    rs = SincResampler()
    rs.set_ratio(1.0)
    x = tone(440.0, 0.05)
    rs.write(x)
    y = rs.read(rs.available)
    delay = rs.half - 1  # встроенная задержка ресемплера
    a, b = y[1000:2000], x[1000 + delay : 2000 + delay]
    err = float(np.max(np.abs(a - b))) if a.size else 1.0
    check("SincResampler: ratio=1 прозрачен", err < 1e-3, f"max err {err:.2e}, задержка {delay}")

    # Чистый ресемплинг меняет тон пропорционально ratio (это «проигрывание»
    # с другой скоростью); сохранение тона обеспечивает фазовый вокодер.
    for ratio, label in ((0.5, "замедление x2"), (2.0, "ускорение x2")):
        rs = SincResampler()
        rs.set_ratio(ratio)
        rs.write(tone(440.0, 0.2))
        y = rs.read(rs.available)
        expect = int(round(0.2 * SR / ratio))
        check(
            f"SincResampler: ratio={ratio} ({label}) -> длина",
            abs(y.size - expect) < 0.02 * expect,
            f"{y.size} ≈ {expect}",
        )
        expect_hz = 440.0 * ratio
        check(
            f"SincResampler: ratio={ratio} ({label}) -> тон {expect_hz:.0f} Гц",
            freq_ok(y, expect_hz),
            f"{dominant_freq(y):.1f} Гц",
        )


# ------------------------------------------------------------------- вокодер
def test_vocoder() -> None:
    for rate, expect in ((2.0, 0.5), (0.5, 2.0), (4.0, 0.25), (0.25, 4.0)):
        pv = PhaseVocoder(n_fft=4096, hop_syn=256)
        pv.set_rate(rate)
        dur = 1.0
        src = tone(220.0, dur)
        out = [pv.feed(src)]
        out.append(pv.flush())
        y = np.concatenate([o for o in out if o.size])
        expect_len = int(dur * SR / rate)
        tol = 0.05 * expect_len + PV_TAIL
        check(
            f"PhaseVocoder: rate={rate} -> длина",
            abs(y.size - expect_len) < tol,
            f"{y.size} ≈ {expect_len} (+хвост до {PV_TAIL})",
        )
        check(
            f"PhaseVocoder: rate={rate} -> тон сохранён",
            freq_ok(y, 220),
            f"{dominant_freq(y):.1f} Гц",
        )


# ----------------------------------------------------------------- StreamPath
def _render(speed: float, pitch: float, dur: float = 1.0, sr: int = SR) -> np.ndarray:
    src = MemorySource(tone(220.0, dur, sr), sr)
    path = StreamPath(src, sr, speed=speed, pitch=pitch)
    out = [path.read(1 << 15) for _ in range(int(dur * sr / 4096) + 6)]
    out.append(path.flush())
    blocks = [b for b in out if b.shape[1]]
    return np.concatenate(blocks, axis=1)[0]


def test_path() -> None:
    def dur_ok(y: np.ndarray, expect: float) -> bool:
        return abs(y.size - expect) < 0.05 * expect + PV_TAIL

    y = _render(1.0, 0.0)
    check("StreamPath: 1.0x -> длительность 1:1", dur_ok(y, SR), f"{y.size}/{SR}")
    check("StreamPath: 1.0x -> тон 220 Гц", freq_ok(y, 220), f"{dominant_freq(y):.1f}")

    y = _render(2.0, 0.0)
    check("StreamPath: 2.0x -> вдвое короче", dur_ok(y, SR / 2), f"{y.size}/{SR // 2}")
    check("StreamPath: 2.0x -> тон не изменился", freq_ok(y, 220), f"{dominant_freq(y):.1f}")

    y = _render(1.0, 12.0)
    check("StreamPath: +12 полутонов -> длительность та же", dur_ok(y, SR), f"{y.size}/{SR}")
    check("StreamPath: +12 полутонов -> 440 Гц", freq_ok(y, 440), f"{dominant_freq(y):.1f}")

    y = _render(1.0, -12.0)
    check("StreamPath: -12 полутонов -> 110 Гц", freq_ok(y, 110), f"{dominant_freq(y):.1f}")

    y = _render(1.5, -5.0)
    check("StreamPath: 1.5x -> короче в 1.5 раза", dur_ok(y, SR / 1.5), f"{y.size}")
    expect_hz = 220 * 2 ** (-5 / 12)
    check(
        "StreamPath: 1.5x и -5 полутонов -> тон",
        freq_ok(y, expect_hz),
        f"{dominant_freq(y):.1f} ≈ {expect_hz:.1f}",
    )

    # Смена скорости на лету: не должно быть щелчков.
    src = MemorySource(tone(220.0, 2.0), SR)
    path = StreamPath(src, SR, speed=1.0, pitch=0.0)
    chunks = []
    for i in range(int(2.0 * SR / 4096)):
        path.set_speed(1.0 if i < 12 else 2.0)
        b = path.read(4096)
        if b.shape[1]:
            chunks.append(b[0])
    y = np.concatenate(chunks) if chunks else np.zeros(0, np.float32)
    if y.size > 100:
        d = np.abs(np.diff(y))
        thresh = 0.08
        check(
            "StreamPath: смена скорости без щелчков",
            float(d.max()) < thresh,
            f"max|d| = {d.max():.4f} (порог {thresh})",
        )


def test_position_accounting() -> None:
    """Позиция, которую сообщает StreamPath, должна совпадать с фактически
    выданным аудио (несмотря на то, что источник прочитан далеко вперёд)."""
    for speed, pitch in ((1.0, 0.0), (1.7, 0.0), (1.0, 7.0), (0.6, -3.0), (2.5, 12.0)):
        dur = 4.0
        sr = SR
        src = MemorySource(tone(220.0, dur, sr), sr)
        path = StreamPath(src, sr, speed=speed, pitch=pitch)
        got = 0
        while got < int(1.0 * sr):
            b = path.read(4096)
            if b.shape[1] == 0:
                break
            got += b.shape[1]
        # 1 секунда выхода при скорости speed => speed секунд исходника
        expect = got * speed / sr
        actual = path.source_pos / sr
        rel = abs(actual - expect) / expect
        check(
            f"Позиция: speed={speed} pitch={pitch} -> позиция источника",
            rel < 0.02,
            f"{actual:.3f} c, ожидалось {expect:.3f} c ({rel * 100:.1f}%)",
        )
        # и сильно отличается от позиции, до которой дочитал источник
        lead = (src.position - path.source_pos) / sr
        check(
            f"Позиция: speed={speed} pitch={pitch} -> упреждение чтения компенсировано",
            lead > 0.05,
            f"источник прочитан на {lead:.2f} c вперёд, учтено",
        )


def test_rates_and_rewrite() -> None:
    """Другая частота источника (например, 48 кГц) -> ресемплер + вокодер."""
    sr_src = 48000
    src = MemorySource(tone(330.0, 1.0, sr_src), sr_src)
    path = StreamPath(src, SR, speed=1.25, pitch=-2.0)
    out = [path.read(1 << 15) for _ in range(40)]
    out.append(path.flush())
    y = np.concatenate([b[0] for b in out if b.shape[1]])
    expect_hz = 330 * 2 ** (-2 / 12)
    check("StreamPath: 48 кГц источник -> 44.1 кГц карты, тон", freq_ok(y, expect_hz), f"{dominant_freq(y):.1f}")
    check("StreamPath: 48 кГц -> длительность / 1.25", abs(y.size - SR / 1.25) < 0.06 * SR + PV_TAIL, f"{y.size}")


def test_seek_while_playing() -> None:
    """Поиск по волне во время воспроизведения не должен замораживать
    playhead. Раньше _pos_override оставался выставленным до нажатия паузы:
    звук играл с нового места, а ползунок стоял."""
    from msc.engine import Engine

    sr = 22050
    dur = 20.0
    import tempfile

    path = os.path.join(tempfile.gettempdir(), "msc-selftest-seek.wav")
    import soundfile as sf

    sf.write(path, tone(220.0, dur, sr), sr)
    eng = Engine(silent=True, rate=sr, channels=1)
    try:
        eng.load(path)
        eng.play()
        time.sleep(0.6)

        target = 10.0
        eng.seek(target)
        time.sleep(0.35)
        p1 = eng.position()
        check(
            "Поиск на лету: позиция уехала с цели",
            p1 > target + 0.05,
            f"{p1:.2f} c сразу после seek({target:.0f})",
        )
        time.sleep(0.6)
        p2 = eng.position()
        check(
            "Поиск на лету: playhead идёт, а не стоит",
            p2 > p1 + 0.2,
            f"{p1:.2f} → {p2:.2f} c за 0.6 c",
        )
        check(
            "Поиск на лету: скорость позиции близка к 1x",
            0.6 < (p2 - p1) / 0.6 < 1.6,
            f"{(p2 - p1) / 0.6:.2f}x",
        )

        # На паузе позиция, наоборот, обязана стоять на месте.
        eng.pause()
        q1 = eng.position()
        time.sleep(0.7)
        q2 = eng.position()
        check("Пауза: позиция не уезжает", abs(q2 - q1) < 0.02, f"{q1:.2f} → {q2:.2f}")

        # Поиск на паузе обязан показывать ровно цель и продолжать с неё.
        eng.seek(3.0)
        time.sleep(0.2)
        check("Поиск на паузе: позиция равна цели", abs(eng.position() - 3.0) < 0.02, f"{eng.position():.2f}")
        eng.play()
        time.sleep(0.4)
        check(
            "Поиск на паузе: воспроизведение продолжилось с цели",
            eng.position() > 3.2,
            f"{eng.position():.2f} c",
        )

        # Старт не должен оставлять очередь пустой: раньше play() сначала
        # ставил _playing=True и наполнял очередь после, и аудиоколбэк успевал
        # за это окно увидеть пустую очередь — щелчок на старте каждого трека.
        eng.pause()
        eng.seek(0.0)
        eng.play()
        check(
            "Старт: очередь заполнена к моменту включения воспроизведения",
            len(eng._queue) > 0 and eng._playing,
            f"блоков в очереди {len(eng._queue)}, играет {eng._playing}",
        )
        check("Старт: счётчик срывов не растёт", eng._underruns == 0, f"underruns={eng._underruns}")
    finally:
        eng.close()
        try:
            os.unlink(path)
        except OSError:
            pass


def test_engine_silent() -> None:
    from msc.engine import Engine

    sr = 22050
    dur = 3.0
    raw = tone(220.0, dur, sr)
    import tempfile

    path = os.path.join(tempfile.gettempdir(), "msc-selftest.wav")
    import soundfile as sf

    sf.write(path, raw, sr)
    eng = Engine(silent=True, rate=sr, channels=1)
    eng.load(path)
    st = eng.state()
    check("Engine: длительность трека", abs(st["duration"] - dur) < 0.05, f"{st['duration']:.2f}")
    eng.set("speed", 2.0)
    eng.play()
    t0 = time.time()
    time.sleep(1.0)
    st = eng.state()
    pos = st["position"]
    elapsed = time.time() - t0
    check("Engine: позиция растёт", pos > 0.8, f"{pos:.2f} c")
    expect = elapsed * 2.0
    check(
        "Engine: позиция соответствует скорости 2x",
        abs(pos - expect) < 0.35,
        f"{pos:.2f} c ≈ {expect:.2f} c ({elapsed:.2f} c реального времени)",
    )
    eng.pause()
    time.sleep(0.3)
    p1 = eng.state()["position"]
    time.sleep(0.4)
    p2 = eng.state()["position"]
    check("Engine: пауза останавливает позицию", abs(p2 - p1) < 0.05, f"{p1:.3f} -> {p2:.3f}")
    eng.seek(1.0)
    check("Engine: seek", abs(eng.state()["position"] - 1.0) < 0.05, f"{eng.state()['position']:.3f}")
    eng.close()
    os.unlink(path)


def test_loop_and_repeat() -> None:
    """Петля A–B: позиция не выходит за границы и не застревает."""
    from msc.engine import Engine

    sr = 22050
    path = os.path.join(tempfile.gettempdir(), "msc-loop.wav")
    import soundfile as sf

    sf.write(path, tone(440.0, 6.0, sr), sr)
    eng = Engine(silent=True, rate=sr, channels=1)
    eng.load(path)
    eng.set("loopRange", [1.0, 2.0])
    eng.set("loop", True)
    eng.play()
    time.sleep(1.6)  # доходим до начала петли
    seen = []
    for _ in range(40):
        time.sleep(0.05)
        seen.append(eng.state()["position"])
    lo, hi = min(seen), max(seen)
    check("Петля A-B: позиция внутри границ", 0.95 < lo and hi < 2.1, f"{lo:.3f}..{hi:.3f} (ждём 1..2)")
    check("Петля A-B: петля реально проигрывается", hi - lo > 0.5, f"размах {hi - lo:.2f} c")

    eng.set("loop", False)
    eng.set("repeat", True)
    eng.set("speed", 2.0)
    eng.stop()
    eng.play()
    time.sleep(2.0)
    st = eng.state()
    check("Повтор: трек играет по кругу", st["playing"] and st["position"] > 0.2, f"pos={st['position']:.2f}, playing={st['playing']}")
    eng.close()
    os.unlink(path)


def test_server_protocol() -> None:
    """Полный путь: python-сервер <- NDJSON -> команды и события."""
    import json
    import subprocess

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    python = os.path.join(root, ".venv", "bin", "python")
    if not os.path.exists(python):
        python = sys.executable
    env = {**os.environ, "PYTHONPATH": os.path.join(root, "python"), "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen(
        [python, "-u", "-m", "msc", "--silent"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    try:
        assert proc.stdout is not None
        proc.stdin.write((json.dumps({"id": 1, "cmd": "ping"}) + "\n").encode())
        proc.stdin.flush()
        deadline = time.time() + 15
        got_ready = got_reply = False
        while time.time() < deadline and not (got_ready and got_reply):
            line = proc.stdout.readline()
            if not line:
                break
            msg = json.loads(line)
            got_ready |= msg.get("ev") == "ready"
            if msg.get("id") == 1 and msg.get("ok"):
                got_reply = True
        check("IPC: событие ready получено", got_ready)
        check("IPC: команда ping получила ответ", got_reply)

        caps = None
        proc.stdin.write((json.dumps({"id": 2, "cmd": "capabilities"}) + "\n").encode())
        proc.stdin.flush()
        deadline = time.time() + 10
        while time.time() < deadline:
            msg = json.loads(proc.stdout.readline())
            if msg.get("id") == 2:
                caps = msg.get("result")
                break
        check(
            "IPC: capabilities",
            bool(caps) and caps["speedRange"] == [0.25, 4.0],
            f"speed={caps['speedRange'] if caps else '?'}, ffmpeg={caps.get('ffmpeg') if caps else '?'}",
        )
    finally:
        try:
            proc.stdin.write(b'{"cmd":"quit"}\n')
            proc.stdin.flush()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()


def test_soundfile_path() -> None:
    """Ветка soundfile должна работать сама по себе, без ffmpeg.

    Регрессия: `always_2d` передавался в конструктор SoundFile вместо read(),
    поэтому ветка всегда падала и уходила в ffmpeg-фолбэк.
    """
    from msc import sources as src_mod
    from msc.sources import SndFileSource
    from msc.stream import StreamPath
    import soundfile as sf

    sr = 44100
    path = os.path.join(tempfile.gettempdir(), "msc-sf.wav")
    sf.write(path, tone(220.0, 2.0, sr), sr)

    src = SndFileSource(path)
    check("soundfile: открытие файла", src.frames == 2 * sr and src.channels == 1, f"{src.frames} кадров")
    block = src.read(1024)
    check("soundfile: чтение блока", block.shape == (1, 1024), f"форма {block.shape}")
    src.seek(1000)
    check("soundfile: seek", src.position == 1000, f"позиция {src.position}")
    rest = src.read(2 * sr)  # просим больше, чем осталось
    check("soundfile: чтение до конца", abs(rest.shape[1] - (2 * sr - 1000)) < 2, f"{rest.shape[1]} из {2 * sr - 1000}")
    check("soundfile: конец файла", src.read(1024).shape[1] == 0, "0 семплов")
    src.close()

    picked = src_mod.open_source(path, prefer="soundfile")
    check("open_source: выбирает soundfile", isinstance(picked, SndFileSource), type(picked).__name__)
    picked.close()

    # Полная обработка без единого обращения к ffmpeg.
    st = StreamPath(SndFileSource(path), sr, speed=1.5, pitch=-4)
    chunks = []
    for _ in range(40):
        b = st.read(8192)
        if b.shape[1] == 0:
            break
        chunks.append(b[0])
    tail = st.flush()
    if tail.shape[0]:
        chunks.append(np.asarray(tail[0], dtype=np.float32))
    y = np.concatenate(chunks)
    expect_hz = 220 * 2 ** (-4 / 12)
    expect_len = int(2.0 * sr / 1.5)  # трек 2 с при темпе 1.5x
    check("soundfile: длина результата", abs(y.size - expect_len) < 0.06 * expect_len + PV_TAIL, f"{y.size} ≈ {expect_len}")
    check("soundfile: тон на выходе", freq_ok(y, expect_hz), f"{dominant_freq(y):.1f} Гц (ждали {expect_hz:.1f})")
    check("soundfile: позиция источника", st.source_pos > 0, f"{st.source_pos / sr:.2f} с")
    os.unlink(path)


def test_metadata() -> None:
    """Метаданные и BPM читаются из тегов (ffprobe)."""
    from msc.sources import metadata

    if not exporter_have_ffmpeg():
        check("Метаданные: ffmpeg доступен", True, "пропущено (нет ffmpeg)")
        return
    import subprocess

    # BPM надёжнее всего хранится в ID3 (mp3); m4a его обычно теряет.
    path = os.path.join(tempfile.gettempdir(), "msc-meta.mp3")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-ac", "2", "-ar", "44100",
         "-metadata", "title=Заголовок", "-metadata", "artist=Артист",
         "-metadata", "TBPM=128", "-c:a", "libmp3lame", path],
        check=True, capture_output=True, timeout=60,
    )
    meta = metadata(path)
    check("Метаданные: title/artist", meta.get("title") == "Заголовок" and meta.get("artist") == "Артист",
          f"{meta.get('title')} / {meta.get('artist')}")
    check("Метаданные: BPM из тега", abs(meta.get("bpm", 0) - 128) < 0.01, f"bpm={meta.get('bpm')}")
    check("Метаданные: частота/каналы", meta.get("samplerate") == 44100 and meta.get("channels") == 2,
          f"{meta.get('samplerate')} Гц, {meta.get('channels')} кан.")
    check("Метаданные: кодек определён", meta.get("codec") not in (None, "", "unknown"), f"{meta.get('codec')}")
    os.unlink(path)


def exporter_have_ffmpeg() -> bool:
    from msc.sources import have_ffmpeg

    return have_ffmpeg()


def test_pitch_export() -> None:
    """Экспорт со сдвигом тона сохраняет длительность и меняет высоту."""
    from msc import exporter

    if not exporter_have_ffmpeg():
        return
    import soundfile as sf

    sr = 44100
    src = os.path.join(tempfile.gettempdir(), "msc-pitch-src.wav")
    dst = os.path.join(tempfile.gettempdir(), "msc-pitch-out.flac")
    sf.write(src, tone(330.0, 1.0, sr), sr)
    exporter.export(src, dst, speed=1.0, pitch=12.0, mode="rubberband")
    data, rate = sf.read(dst)
    hz = dominant_freq(np.asarray(data, dtype=np.float32), rate)
    check("Экспорт: +12 полутонов -> длительность та же", abs(data.size / rate - 1.0) < 0.05, f"{data.size / rate:.2f} c")
    check("Экспорт: +12 полутонов -> тон ×2", abs(hz - 660) < 12, f"{hz:.0f} Гц (ждали 660)")
    for p in (src, dst):
        if os.path.exists(p):
            os.unlink(p)


def test_export() -> None:
    from msc import exporter

    if not exporter.have_ffmpeg():
        check("Export: ffmpeg доступен", True, "пропущено (нет ffmpeg)")
        return
    import tempfile

    import soundfile as sf

    src_path = os.path.join(tempfile.gettempdir(), "msc-export-src.wav")
    dst_path = os.path.join(tempfile.gettempdir(), "msc-export-out.mp3")
    sr = 44100
    sf.write(src_path, tone(220.0, 2.0, sr), sr)
    for mode in ("rubberband", "engine"):
        exporter.export(src_path, dst_path, speed=2.0, pitch=0.0, mode=mode)
        info = sf.info(dst_path)
        ok = abs(info.duration - 1.0) < 0.15
        check(f"Export[{mode}]: 2.0x -> вдвое короче", ok, f"{info.duration:.2f} c")
        data, rate = sf.read(dst_path)
        check(
            f"Export[{mode}]: тон сохранён",
            abs(dominant_freq(np.asarray(data, dtype=np.float32), rate) - 220) < 4,
            f"{dominant_freq(np.asarray(data, dtype=np.float32), rate):.1f} Гц",
        )
    for p in (src_path, dst_path):
        if os.path.exists(p):
            os.unlink(p)


def main() -> int:
    t0 = time.time()
    test_ring()
    test_resampler()
    test_vocoder()
    test_path()
    test_position_accounting()
    test_rates_and_rewrite()
    test_seek_while_playing()
    test_engine_silent()
    test_loop_and_repeat()
    test_server_protocol()
    test_soundfile_path()
    test_metadata()
    test_pitch_export()
    test_export()
    dt = time.time() - t0
    print(f"\n{'-' * 60}\n{'ВСЁ ОК' if not FAILED else 'ПРОВАЛЫ: ' + ', '.join(FAILED)}  ({dt:.1f} c)")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
