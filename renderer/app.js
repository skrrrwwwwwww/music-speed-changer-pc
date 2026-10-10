/**
 * UI Music Speed Changer.
 *
 * Renderer ничего не знает про DSP: он шлёт команды аудиодвижку (python) и
 * рисует состояние, которое тот присылает событиями.
 */

const SPEED_MIN = 0.25;
const SPEED_MAX = 4.0;
const PITCH_MIN = -12;
const PITCH_MAX = 12;

/* ------------------------------------------------------------------ state */
const state = {
  track: null,
  peaks: null, // {buckets, channels, data: Int16Array (buckets*3*ch)}
  playing: false,
  position: 0,
  duration: 0,
  speed: 1,
  pitch: 0,
  volume: 0.9,
  loop: { enabled: false, start: 0, end: 0 },
  hasTrack: false,
  silent: false,
  silentMark: 0, // позиция A/B, взятая пользователем
  silentMarkB: null,
  dragging: null,
};

const el = {};
function byId(id) {
  return document.getElementById(id);
}

function cacheDom() {
  [
    'engine-status', 'track-title', 'track-sub', 'btn-open', 'btn-export',
    'wave', 'wave-hint', 'time-cur', 'time-total', 'loop-label',
    'knob', 'speed-value', 'speed-presets', 'pitch', 'pitch-value', 'pitch-presets',
    'btn-play', 'btn-stop', 'btn-prev', 'btn-next',
    'loop-toggle', 'btn-loop-mark', 'btn-loop-mark-b', 'btn-loop-clear',
    'device', 'bpm-row', 'bpm', 'bpm-apply', 'opt-autoplay',
    'btn-lib', 'library', 'lib-search', 'lib-refresh', 'lib-add', 'lib-close',
    'lib-list', 'lib-roots', 'lib-status', 'library-backdrop',
    'volume', 'volume-value', 'repeat-toggle', 'status-audio', 'status-engine', 'status-ffmpeg',
    'status-msg', 'export-overlay', 'export-bar', 'export-text',
  ].forEach((id) => {
    el[id] = byId(id);
  });
}

/* ------------------------------------------------------------------ utils */
function fmtTime(sec) {
  if (!isFinite(sec) || sec < 0) sec = 0;
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  const cs = Math.floor((sec * 100) % 100);
  return `${m}:${String(s).padStart(2, '0')}.${String(cs).padStart(2, '0')}`;
}

function setStatus(text, kind = '') {
  el['status-msg'].textContent = text || '';
  el['status-msg'].style.color = kind === 'bad' ? 'var(--bad)' : kind === 'good' ? 'var(--good)' : '';
}

let statusTimer = null;
function flash(text, kind = '') {
  setStatus(text, kind);
  clearTimeout(statusTimer);
  statusTimer = setTimeout(() => setStatus(''), 4000);
}

function send(cmd, args = {}) {
  return window.msc.cmd(cmd, args).catch((err) => {
    flash(err.message || String(err), 'bad');
    throw err;
  });
}

/* ------------------------------------------------------------------ knob */
const knob = {
  a0: Math.PI * 0.75, // начало дуги (225°)
  span: Math.PI * 1.5,
  dragging: false,
  startY: 0,
  startSpeed: 1,
};

function speedFromAngle(t) {
  // t: 0..1 по дуге ручки
  const v = SPEED_MIN * Math.pow(SPEED_MAX / SPEED_MIN, t);
  return Math.min(SPEED_MAX, Math.max(SPEED_MIN, v));
}

function angleToT(angle) {
  let a = angle;
  const a0 = knob.a0;
  const span = knob.span;
  while (a < a0) a += Math.PI * 2;
  return Math.min(1, Math.max(0, (a - a0) / span));
}

function drawKnob() {
  const c = el.knob;
  const dpr = window.devicePixelRatio || 1;
  const size = Math.round(c.getBoundingClientRect().width) || 200;
  if (c.width !== size * dpr) {
    c.width = size * dpr;
    c.height = size * dpr;
  }
  const ctx = c.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, size, size);

  const cx = size / 2;
  const cy = size / 2;
  const R = size * 0.42;
  const t = angleToTInv(state.speed);

  // дуга
  ctx.lineWidth = 12;
  ctx.lineCap = 'round';
  ctx.strokeStyle = '#262c3a';
  ctx.beginPath();
  ctx.arc(cx, cy, R, knob.a0, knob.a0 + knob.span);
  ctx.stroke();

  const grad = ctx.createLinearGradient(0, 0, size, size);
  grad.addColorStop(0, '#4ea1ff');
  grad.addColorStop(1, '#7c5cff');
  ctx.strokeStyle = grad;
  ctx.beginPath();
  ctx.arc(cx, cy, R, knob.a0, knob.a0 + knob.span * t);
  ctx.stroke();

  // деления
  ctx.strokeStyle = '#39415a';
  ctx.lineWidth = 2;
  for (let i = 0; i <= 8; i++) {
    const a = knob.a0 + (knob.span * i) / 8;
    const r1 = R - 11;
    const r2 = R - (i % 4 === 0 ? 21 : 17);
    ctx.beginPath();
    ctx.moveTo(cx + Math.cos(a) * r1, cy + Math.sin(a) * r1);
    ctx.lineTo(cx + Math.cos(a) * r2, cy + Math.sin(a) * r2);
    ctx.stroke();
  }

  // указатель
  const a = knob.a0 + knob.span * t;
  ctx.save();
  ctx.translate(cx, cy);
  ctx.rotate(a);
  ctx.fillStyle = '#e8ecf5';
  ctx.beginPath();
  ctx.moveTo(R - 3, 0);
  ctx.lineTo(R - 23, -5.5);
  ctx.lineTo(R - 23, 5.5);
  ctx.closePath();
  ctx.fill();
  ctx.restore();

  // подложка круга
  ctx.fillStyle = '#171b26';
  ctx.beginPath();
  ctx.arc(cx, cy, R - 30, 0, Math.PI * 2);
  ctx.fill();
}

/** Скорость -> положение на дуге ручки (логарифмическая шкала). */
function angleToTInv(speed) {
  const lo = Math.log(SPEED_MIN);
  const hi = Math.log(SPEED_MAX);
  return Math.min(1, Math.max(0, (Math.log(Math.max(SPEED_MIN, speed)) - lo) / (hi - lo)));
}

function setSpeed(value, { silentUi = false } = {}) {
  const v = Math.min(SPEED_MAX, Math.max(SPEED_MIN, Math.round(value * 100) / 100));
  state.speed = v;
  // Не затираем значение, пока пользователь его набирает.
  if (document.activeElement !== el['speed-value']) {
    el['speed-value'].value = fmt2(v);
  }
  drawKnob();
  el['speed-presets'].querySelectorAll('.chip').forEach((chip) => {
    chip.classList.toggle('is-active', Math.abs(Number(chip.dataset.speed) - v) < 0.005);
  });
  if (!silentUi) send('set', { param: 'speed', value: v });
}

function setupKnob() {
  const c = el.knob;
  const angleFromEvent = (e) => {
    const r = c.getBoundingClientRect();
    return Math.atan2(e.clientY - (r.top + r.height / 2), e.clientX - (r.left + r.width / 2));
  };

  c.addEventListener('pointerdown', (e) => {
    // Клик по самой цифре — это ввод, а не вращение ручки.
    if (e.target !== c) return;
    c.setPointerCapture(e.pointerId);
    knob.dragging = true;
    knob.startY = e.clientY;
    knob.startSpeed = state.speed;
    c.style.cursor = 'ns-resize';
  });

  c.addEventListener('pointermove', (e) => {
    if (!knob.dragging) return;
    const dy = knob.startY - e.clientY;
    const fine = e.shiftKey ? 0.25 : 1;
    const travel = c.getBoundingClientRect().height || 200;
    // Полный диапазон шкалы — за `travel` пикселей.
    const t = angleToTInv(knob.startSpeed) + (dy / travel) * fine;
    const v = SPEED_MIN * Math.pow(SPEED_MAX / SPEED_MIN, Math.min(1, Math.max(0, t)));
    setSpeed(Math.round(v * 1000) / 1000);
  });

  const stop = (e) => {
    if (!knob.dragging) return;
    knob.dragging = false;
    try {
      c.releasePointerCapture(e.pointerId);
    } catch {
      /* ignore */
    }
  };
  c.addEventListener('pointerup', stop);
  c.addEventListener('pointercancel', stop);

  // Щелчок по дуге — переход к значению под курсором.
  c.addEventListener('dblclick', () => setSpeed(1));

  // Ввод скорости с клавиатуры прямо на цифре ручки.
  setupNumberField(el['speed-value'], {
    get: () => state.speed,
    set: (v) => setSpeed(v),
    min: SPEED_MIN,
    max: SPEED_MAX,
    bigStep: 0.1,
  });

  c.addEventListener(
    'wheel',
    (e) => {
      e.preventDefault();
      const step = e.shiftKey ? 0.005 : 0.02;
      const mult = e.deltaY > 0 ? 1 - step : 1 + step;
      setSpeed(Math.round(state.speed * mult * 1000) / 1000);
    },
    { passive: false },
  );

  c.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowUp') setSpeed(state.speed + 0.05);
    else if (e.key === 'ArrowDown') setSpeed(state.speed - 0.05);
    else return;
    e.preventDefault();
  });
  c.tabIndex = 0;
}

/* ------------------------------------------------------------------ wave */
const wave = {
  ctx: null,
  w: 0,
  h: 0,
  dpr: 1,
  dragMode: null, // 'seek' | 'move' | 'a' | 'b'
  dragOffset: 0,
};

function setupWave() {
  const c = el.wave;
  wave.ctx = c.getContext('2d');
  const resize = () => {
    const r = c.getBoundingClientRect();
    wave.dpr = window.devicePixelRatio || 1;
    wave.w = Math.max(1, Math.floor(r.width));
    wave.h = Math.max(1, Math.floor(r.height));
    c.width = wave.w * wave.dpr;
    c.height = wave.h * wave.dpr;
    drawWave();
  };
  window.addEventListener('resize', resize);
  new ResizeObserver(resize).observe(c);

  const timeAt = (clientX) => {
    const r = c.getBoundingClientRect();
    const x = Math.min(r.width - 1, Math.max(0, clientX - r.left));
    return (x / r.width) * state.duration;
  };
  const nearHandle = (clientX) => {
    const r = c.getBoundingClientRect();
    const toX = (t) => (t / Math.max(0.001, state.duration)) * r.width + r.left;
    if (Math.abs(clientX - toX(state.loop.start)) < 6) return 'a';
    if (Math.abs(clientX - toX(state.loop.end)) < 6) return 'b';
    return null;
  };
  const inLoop = (clientX) => {
    const t = timeAt(clientX);
    return t >= state.loop.start && t <= state.loop.end && state.loop.end > state.loop.start;
  };

  c.addEventListener('pointerdown', (e) => {
    if (!state.hasTrack) return;
    c.setPointerCapture(e.pointerId);
    const handle = nearHandle(e.clientX);
    if (handle) {
      wave.dragMode = handle;
    } else if (inLoop(e.clientX)) {
      wave.dragMode = 'move';
      wave.dragOffset = timeAt(e.clientX) - state.loop.start;
    } else {
      wave.dragMode = 'seek';
      send('seek', { position: timeAt(e.clientX) });
    }
  });

  c.addEventListener('pointermove', (e) => {
    if (!state.hasTrack) return;
    if (!wave.dragMode) {
      c.style.cursor = nearHandle(e.clientX) || inLoop(e.clientX) ? 'ew-resize' : 'pointer';
      return;
    }
    const t = timeAt(e.clientX);
    if (wave.dragMode === 'seek') {
      send('seek', { position: t });
    } else if (wave.dragMode === 'a') {
      applyLoop(Math.min(t, state.loop.end - 0.2), state.loop.end);
    } else if (wave.dragMode === 'b') {
      applyLoop(state.loop.start, Math.max(t, state.loop.start + 0.2));
    } else if (wave.dragMode === 'move') {
      const len = state.loop.end - state.loop.start;
      let s = Math.min(Math.max(0, t - wave.dragOffset), Math.max(0, state.duration - len));
      applyLoop(s, s + len);
    }
  });

  const stopDrag = (e) => {
    if (!wave.dragMode) return;
    wave.dragMode = null;
    try {
      c.releasePointerCapture(e.pointerId);
    } catch {
      /* ignore */
    }
  };
  c.addEventListener('pointerup', stopDrag);
  c.addEventListener('pointercancel', stopDrag);
  c.addEventListener('dblclick', () => {
    if (state.hasTrack) send('seek', { position: 0 });
  });
}

function applyLoop(a, b) {
  state.loop.start = Math.max(0, Math.min(a, state.duration));
  state.loop.end = Math.max(0, Math.min(b, state.duration));
  send('set', { param: 'loopRange', value: [state.loop.start, state.loop.end] });
  drawWave();
}

function drawWave() {
  const ctx = wave.ctx;
  if (!ctx) return;
  const { w, h, dpr } = wave;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  // фон и центральная линия
  ctx.fillStyle = '#141824';
  ctx.fillRect(0, 0, w, h);
  ctx.strokeStyle = '#232a3a';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(0, h / 2);
  ctx.lineTo(w, h / 2);
  ctx.stroke();

  // сетка каждые 10 секунд
  if (state.duration > 0) {
    ctx.strokeStyle = 'rgba(255,255,255,0.045)';
    const stepSec = state.duration > 600 ? 60 : state.duration > 120 ? 10 : 5;
    for (let t = stepSec; t < state.duration; t += stepSec) {
      const x = Math.round((t / state.duration) * w) + 0.5;
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, h);
      ctx.stroke();
    }
  }

  const peaks = state.peaks;
  if (peaks) {
    const ch = peaks.channels;
    const mid = h / 2;
    const half = h / 2 - 6;
    const playedTo = state.duration > 0 ? (state.position / state.duration) * w : 0;
    for (let c = 0; c < ch; c++) {
      const rows = ch === 1 ? [mid] : [mid - half / 2, mid + half / 2];
      const amp = ch === 1 ? half : half / 2;
      for (let pass = 0; pass < 2; pass++) {
        // сначала рисуем «непрозвучавшую» часть, затем прозвучавшую поверх
        ctx.beginPath();
        for (let i = 0; i < peaks.buckets; i++) {
          const x0 = (i / peaks.buckets) * w;
          const x1 = ((i + 1) / peaks.buckets) * w;
          const mx = peaks.data[i * 3 * ch + 0 * ch + c] / 32768;
          const mn = peaks.data[i * 3 * ch + 1 * ch + c] / 32768;
          const yTop = rows[c] - mx * amp;
          const yBot = rows[c] - mn * amp;
          if (pass === 0) {
            if (x0 >= playedTo) {
              ctx.rect(x0, yTop, Math.max(1, x1 - x0), Math.max(1, yBot - yTop));
            }
          } else if (x1 <= playedTo) {
            ctx.rect(x0, yTop, Math.max(1, x1 - x0), Math.max(1, yBot - yTop));
          } else if (x0 < playedTo) {
            const mid2 = ((playedTo - x0) / (x1 - x0)) * (x1 - x0);
            ctx.rect(x0, yTop, mid2, Math.max(1, yBot - yTop));
          }
        }
        ctx.fillStyle = pass === 0 ? '#3d4660' : '#4ea1ff';
        ctx.globalAlpha = pass === 0 ? 0.55 : 0.95;
        ctx.fill();
      }
    }
    ctx.globalAlpha = 1;
  }

  // петля
  if (state.loop.end > state.loop.start) {
    const x0 = (state.loop.start / state.duration) * w;
    const x1 = (state.loop.end / state.duration) * w;
    ctx.fillStyle = state.loop.enabled ? 'rgba(255,180,84,0.14)' : 'rgba(255,255,255,0.06)';
    ctx.fillRect(x0, 0, x1 - x0, h);
    ctx.strokeStyle = state.loop.enabled ? 'rgba(255,180,84,0.85)' : 'rgba(255,255,255,0.25)';
    ctx.lineWidth = 1.5;
    for (const x of [x0, x1]) {
      ctx.beginPath();
      ctx.moveTo(x + 0.5, 0);
      ctx.lineTo(x + 0.5, h);
      ctx.stroke();
    }
  }

  // playhead
  if (state.duration > 0) {
    const x = Math.round((state.position / state.duration) * w) + 0.5;
    ctx.strokeStyle = '#ffffff';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, h);
    ctx.stroke();
    ctx.fillStyle = '#ffffff';
    ctx.beginPath();
    ctx.moveTo(x - 5, 0);
    ctx.lineTo(x + 5, 0);
    ctx.lineTo(x, 8);
    ctx.closePath();
    ctx.fill();
  }
}

/* --------------------------------------------------------------- peaks b64 */
function decodePeaks(payload) {
  const bin = atob(payload.data);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return {
    buckets: payload.buckets,
    channels: payload.channels,
    data: new Int16Array(bytes.buffer),
  };
}

/* -------------------------------------------------------------- события */
function applyState(s) {
  state.playing = s.playing;
  state.position = s.position;
  state.duration = s.duration;
  state.volume = s.volume;
  state.hasTrack = s.hasTrack;
  state.loop = s.loop;
  state.silent = s.silent;
  if (Math.abs(s.speed - state.speed) > 1e-6) setSpeed(s.speed, { silentUi: true });
  if (Math.abs(s.pitch - state.pitch) > 1e-6) setPitch(s.pitch, { silentUi: true });
  el['time-cur'].textContent = fmtTime(s.position);
  el['time-total'].textContent = fmtTime(s.duration);
  el['btn-play'].textContent = s.playing ? '❚❚' : '▶';
  el['btn-play'].classList.toggle('playing', s.playing);
  el['loop-toggle'].checked = s.loop.enabled;
  el['repeat-toggle'].checked = s.repeat;
  el['loop-label'].textContent = s.loop.end > s.loop.start
    ? `A–B петля: ${fmtTime(s.loop.start)} – ${fmtTime(s.loop.end)}`
    : 'A–B петля: выключена';
  el['loop-label'].classList.toggle('on', s.loop.enabled);
  el['btn-export'].disabled = !s.hasTrack;
  drawWave();
}

/** Форматирует число до сотых без хвостовых нулей: 1 → "1", 6.7 → "6.7". */
function fmt2(v) {
  return (Math.round(v * 100) / 100).toFixed(2).replace(/\.?0+$/, '') || '0';
}

/**
 * Разбирает то, что пользователь набирает в поле.
 * Промежуточные состояния («-», «6.», «6,7») не считаем ошибкой — это нормальный
 * набор. Возвращает null, если число ещё не набрано.
 */
function parseTyped(text) {
  const raw = String(text).trim().replace(',', '.').replace(/\s/g, '');
  if (raw === '' || raw === '-' || raw === '.' || raw === '-.') return null;
  const v = Number.parseFloat(raw);
  return Number.isFinite(v) ? v : null;
}

/**
 * Общий обработчик числового поля: Enter — применить, Esc — отмена,
 * стрелки и колесо — шаг 0.01 (с Shift — 1), потеря фокуса — применить.
 */
function setupNumberField(input, { get, set, min, max, bigStep = 1 }) {
  const commit = () => {
    const v = parseTyped(input.value);
    if (v === null) {
      input.value = fmt2(get());
      return;
    }
    set(v);
    input.value = fmt2(get());
  };

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      commit();
      input.blur();
      return;
    }
    if (e.key === 'Escape') {
      input.value = fmt2(get());
      input.blur();
      return;
    }
    if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
      e.preventDefault();
      const step = e.shiftKey ? bigStep : 0.01;
      const cur = parseTyped(input.value);
      set((cur === null ? get() : cur) + (e.key === 'ArrowUp' ? step : -step));
      input.value = fmt2(get());
    }
  });

  input.addEventListener('blur', commit);

  // Клик выделяет содержимое, чтобы можно было сразу печатать.
  input.addEventListener('click', () => input.select());

  // Колесо мыши над полем.
  input.addEventListener(
    'wheel',
    (e) => {
      e.preventDefault();
      const step = e.shiftKey ? 0.01 : bigStep / 20;
      set(get() + (e.deltaY > 0 ? -step : step));
      input.value = fmt2(get());
    },
    { passive: false },
  );

  return { commit };
}

function setPitch(v, { silentUi = false } = {}) {
  const val = Math.min(PITCH_MAX, Math.max(PITCH_MIN, Math.round(v * 100) / 100));
  state.pitch = val;
  el.pitch.value = String(val);
  // Не затираем то, что пользователь прямо сейчас набирает.
  if (document.activeElement !== el['pitch-value']) {
    el['pitch-value'].value = fmt2(val);
  }
  el['pitch-presets'].querySelectorAll('.chip').forEach((chip) => {
    chip.classList.toggle('is-active', Math.abs(Number(chip.dataset.pitch) - val) < 0.005);
  });
  if (!silentUi) send('set', { param: 'pitch', value: val });
}

/** Панель статуса: устройство вывода, частота, наличие ffmpeg. */
function showEngineInfo(info) {
  if (!info) return;
  const dev = info.device || {};
  el['status-audio'].textContent = info.silent
    ? 'аудио: нет (режим --silent)'
    : dev.name
      ? `аудио: ${dev.name}`
      : 'аудио: по умолчанию';
  el['status-engine'].textContent = `частота: ${info.rate} Гц · каналов: ${info.channels}`;
  el['status-ffmpeg'].textContent = `ffmpeg: ${
    info.ffmpeg ? (info.rubberband ? 'есть, rubberband' : 'есть, без rubberband') : 'нет (экспорт недоступен)'
  }`;
  if (info.silent) el['engine-status'].textContent = 'движок готов · без звуковой карты';
}

function onEngineEvent(ev) {
  if (window.__MSC_DEBUG) console.log(`[ev] ${ev.ev}`);
  switch (ev.ev) {
    case 'ready': {
      el['engine-status'].textContent = `движок готов · pid ${ev.pid}`;
      showEngineInfo(ev);
      break;
    }
    case 'state':
      applyState(ev.state);
      break;
    case 'peaks':
      state.peaks = decodePeaks(ev);
      el['wave-hint'].classList.add('hidden');
      drawWave();
      break;
    case 'library':
      library.tracks = ev.tracks || [];
      library.roots = ev.roots || [];
      library.home = ev.home || library.home || '';
      library.scanning = false;
      if (library.open) renderLibrary();
      else if (library.tracks.length) {
        el['btn-lib'].textContent = `Библиотека (${library.tracks.length})`;
      }
      break;
    case 'exportStarted':
      el['export-overlay'].classList.remove('hidden');
      el['export-bar'].style.width = '0%';
      el['export-text'].textContent = 'Обработка…';
      break;
    case 'exportProgress':
      el['export-bar'].style.width = `${Math.round(ev.value * 100)}%`;
      break;
    case 'exported':
      el['export-bar'].style.width = '100%';
      el['export-text'].textContent = `Готово: ${ev.path}`;
      setTimeout(() => el['export-overlay'].classList.add('hidden'), 900);
      flash('Файл сохранён', 'good');
      break;
    case 'exportFailed':
      el['export-overlay'].classList.add('hidden');
      flash(`Ошибка экспорта: ${ev.message}`, 'bad');
      break;
    case 'error':
      flash(ev.message, 'bad');
      break;
    case 'smokeSetup': {
      // Тестовый сценарий для скриншота.
      if (ev.speed) setSpeed(ev.speed);
      if (ev.pitch) setPitch(ev.pitch);
      if (ev.volume) {
        el.volume.value = String(ev.volume);
        el['volume-value'].textContent = `${Math.round(ev.volume * 100)}%`;
        send('set', { param: 'volume', value: ev.volume });
      }
      if (ev.loop) {
        state.loop = { start: ev.loop[0], end: ev.loop[1], enabled: true };
        el['loop-toggle'].checked = true;
        el['loop-label'].textContent = `A–B петля: ${fmtTime(ev.loop[0])} – ${fmtTime(ev.loop[1])}`;
        el['loop-label'].classList.add('on');
        send('set', { param: 'loopRange', value: ev.loop });
        send('set', { param: 'loop', value: true });
        drawWave();
      }
      if (ev.bpm) {
        el['bpm-row'].classList.remove('hidden');
        el.bpm.value = String(ev.bpm);
      }
      break;
    }
    case 'openFile':
      // Файл открыт из файлового менеджера или передан аргументом.
      showTrack(ev.info);
      if (ev.autoplay) flash(`Играет: ${ev.info.file || ev.info.name}`, 'good');
      break;
    case 'smokeLoaded': {
      // Автотест: подставляем трек в UI так же, как это делает диалог открытия.
      showTrack(ev.info);
      break;
    }
    case 'crash':
      el['engine-status'].textContent = 'аудиодвижок упал';
      flash(`Аудиодвижок завершился (код ${ev.code}). Перезапуск…`, 'bad');
      setTimeout(() => window.msc.restartEngine().catch(() => {}), 1200);
      break;
    default:
      break;
  }
}

/* ------------------------------------------------------------- библиотека */
const library = {
  tracks: [],
  roots: [],
  filter: '',
  open: false,
  current: null,
  scanning: false,
  collapsed: new Set(), // папки, свёрнутые пользователем
  home: '', // домашний каталог — приходит из движка, нужен для «~/…»
};

const NUM_RE = /(\d+)/g;

/** Сортировка, где числа идут по величине: «2» раньше «10». */
function naturalKey(text) {
  const out = [];
  for (const part of String(text ?? '').toLowerCase().split(NUM_RE)) {
    if (!part) continue;
    out.push(/^\d+$/.test(part) ? [0, Number(part), ''] : [1, 0, part]);
  }
  return out;
}

function cmpNatural(a, b) {
  const ka = naturalKey(a);
  const kb = naturalKey(b);
  const n = Math.min(ka.length, kb.length);
  for (let i = 0; i < n; i++) {
    if (ka[i][0] !== kb[i][0]) return ka[i][0] - kb[i][0];
    if (ka[i][1] !== kb[i][1]) return ka[i][1] - kb[i][1];
    if (ka[i][2] !== kb[i][2]) return ka[i][2] < kb[i][2] ? -1 : 1;
  }
  return ka.length - kb.length;
}

/** Переключатели папок: что сканируется, что нет, что вообще есть. */
function renderRoots() {
  const box = el['lib-roots'];
  box.replaceChildren();
  const list = (library.roots || []).filter((r) => r && r.path);

  for (const root of list) {
    const row = document.createElement('label');
    row.className = 'root-row';
    row.title = root.exists ? root.path : `папки нет: ${root.path}`;

    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = !!root.enabled;
    cb.disabled = !root.exists;
    cb.addEventListener('change', async () => {
      cb.disabled = true;
      try {
        const res = await send('librarySetRoot', { path: root.path, enabled: cb.checked });
        library.roots = res.roots || library.roots;
        library.scanning = true;
        renderLibrary();
      } catch (err) {
        flash(`Не удалось переключить папку: ${err.message || err}`, 'bad');
        cb.checked = !cb.checked;
        cb.disabled = false;
      }
    });

    const name = document.createElement('span');
    name.className = 'root-name';
    name.textContent = root.name || root.path;

    const n = document.createElement('span');
    n.className = 'root-count';
    n.textContent = root.enabled ? 'сканируется' : root.exists ? 'выключена' : 'нет папки';

    row.append(cb, name, n);
    if (!root.exists) row.classList.add('is-missing');
    box.append(row);
  }
}

/** Группировка по папке: треки внутри папки тоже по-человечески. */
function groupTracks(tracks) {
  const groups = new Map();
  for (const t of tracks) {
    if (!groups.has(t.dir)) groups.set(t.dir, []);
    groups.get(t.dir).push(t);
  }
  const dirs = [...groups.keys()].sort(cmpNatural);
  for (const tracksInDir of groups.values()) tracksInDir.sort((a, b) => cmpNatural(a.name, b.name));
  return dirs.map((dir) => ({ dir, tracks: groups.get(dir) }));
}

function trackRow(t) {
  const row = document.createElement('div');
  row.className = 'lib-item' + (t.path === library.current ? ' is-current' : '');
  row.title = t.path;

  const title = document.createElement('div');
  title.className = 't';
  title.textContent = t.artist ? `${t.artist} — ${t.name}` : t.name;

  const meta = document.createElement('div');
  meta.className = 'd';
  meta.textContent = t.ext ? t.ext.toUpperCase() : '';

  row.append(title, meta);
  row.addEventListener('click', () => loadFile(t.path));
  row.addEventListener('dblclick', async () => {
    await loadFile(t.path);
    await send('play');
  });
  return row;
}

function renderLibrary() {
  const list = el['lib-list'];
  renderRoots();

  if (library.scanning) {
    el['lib-status'].textContent = 'Сканирую библиотеку…';
    list.replaceChildren();
    return;
  }

  const q = library.filter.trim().toLowerCase();
  const items = q
    ? library.tracks.filter(
        (t) =>
          (t.name || '').toLowerCase().includes(q) ||
          (t.artist || '').toLowerCase().includes(q) ||
          (t.album || '').toLowerCase().includes(q) ||
          (t.dir || '').toLowerCase().includes(q),
      )
    : library.tracks;

  const enabled = (library.roots || []).filter((r) => r.enabled);
  const offButExists = (library.roots || []).filter((r) => !r.enabled && r.exists);

  list.replaceChildren();

  if (!items.length) {
    const empty = document.createElement('div');
    empty.className = 'lib-empty';
    if (q) {
      empty.textContent = 'Ничего не найдено';
    } else if (!enabled.length) {
      empty.append('Включите папку выше, чтобы начать сканирование.');
    } else if (!library.tracks.length && offButExists.length) {
      // Самая частая ситуация: музыка лежит в «Загрузках», а они выключены.
      empty.append(
        'В выбранных папках аудио не найдено. ',
        'Если музыка лежит в «Загрузках» — включите её:',
      );
      const btn = document.createElement('button');
      btn.className = 'lib-inline-btn';
      btn.textContent = offButExists.map((r) => r.name).join(', ');
      btn.addEventListener('click', () => toggleRoot(offButExists[0].path, true));
      empty.append(btn);
    } else {
      empty.textContent = 'В выбранных папках аудио не найдено';
    }
    list.append(empty);
    el['lib-status'].textContent = '';
    return;
  }

  const frag = document.createDocumentFragment();
  let shown = 0;
  for (const group of groupTracks(items)) {
    if (shown >= 3000) break;
    const collapsed = !q && library.collapsed.has(group.dir);

    const head = document.createElement('button');
    head.className = 'lib-group';
    head.title = group.dir;
    const caret = document.createElement('span');
    caret.className = 'caret' + (collapsed ? '' : ' open');
    const label = document.createElement('span');
    label.className = 'g-name';
    label.textContent = shortPath(group.dir);
    const count = document.createElement('span');
    count.className = 'g-count';
    count.textContent = String(group.tracks.length);
    head.append(caret, label, count);
    head.addEventListener('click', () => {
      if (library.collapsed.has(group.dir)) library.collapsed.delete(group.dir);
      else library.collapsed.add(group.dir);
      renderLibrary();
    });
    frag.append(head);

    if (!collapsed) {
      const box = document.createElement('div');
      box.className = 'lib-group-items';
      const take = Math.min(group.tracks.length, 3000 - shown);
      box.append(...group.tracks.slice(0, take).map(trackRow));
      frag.append(box);
      shown += take;
    }
  }
  list.append(frag);

  el['lib-status'].textContent =
    items.length > shown
      ? `Найдено: ${items.length} (показано ${shown})`
      : `Найдено: ${items.length} в ${new Set(items.map((t) => t.dir)).size} папках`;
}

/** Включает/выключает папку и пересканирует. */
async function toggleRoot(path, enabled) {
  const res = await send('librarySetRoot', { path, enabled });
  library.roots = res.roots || library.roots;
  library.scanning = true;
  renderLibrary();
}

/** «~/Музыка/Рок/песня.mp3» -> «Музыка/Рок» */
function shortPath(p) {
  const home = library.home || '';
  const s = String(p || '');
  if (home && s.startsWith(home)) return '~' + s.slice(home.length);
  return s;
}

function setLibraryOpen(open) {
  library.open = open;
  el.library.classList.toggle('hidden', !open);
  el['library-backdrop'].classList.toggle('hidden', !open);
  el['btn-lib'].classList.toggle('is-active', open);
  if (open) {
    renderLibrary();
    el['lib-search'].focus();
    // Первый раз подгружаем список с диска.
    if (!library.tracks.length && !library.scanning) {
      library.scanning = true;
      renderLibrary();
      send('library').then((st) => {
        if (st) {
          library.tracks = st.tracks || [];
          library.roots = st.roots || [];
          library.scanning = !!st.scanning;
          renderLibrary();
        }
      }).catch(() => {
        library.scanning = false;
        renderLibrary();
      });
    }
  }
}

async function pickFolder() {
  const dir = await window.msc.pickDirectory();
  if (!dir) return;
  const res = await send('libraryAddRoot', { path: dir });
  library.roots = res.roots || [];
  renderLibrary();
  el['lib-status'].textContent = 'Сканирую…';
}

/** Следующий/предыдущий трек в отфильтрованном списке. */
async function stepTrack(delta) {
  if (!library.tracks.length) return;
  const q = library.filter.trim().toLowerCase();
  const items = q
    ? library.tracks.filter((t) => (t.name || '').toLowerCase().includes(q) || (t.dir || '').toLowerCase().includes(q))
    : library.tracks;
  if (!items.length) return;
  const cur = items.findIndex((t) => t.path === library.current);
  const next = items[((cur + delta) % items.length + items.length) % items.length];
  await loadFile(next.path);
  renderLibrary();
}

/* ------------------------------------------------------------- команды UI */

/** Показывает загруженный трек в шапке и сбрасывает состояние под него. */
function showTrack(info, { notify = true } = {}) {
  if (!info) return;
  state.track = info;
  state.peaks = null;
  state.position = 0;
  state.loop = { enabled: false, start: 0, end: 0 };
  state.silentMark = 0;
  state.silentMarkB = null;

  el['track-title'].textContent = info.name || info.file;
  const m = info.meta || {};
  el['track-sub'].textContent = [
    m.artist,
    m.album,
    `${info.samplerate} Гц`,
    `${info.channels} кан.`,
    fmtTime(info.duration),
    m.codec,
  ]
    .filter(Boolean)
    .join(' · ');

  el['wave-hint'].classList.remove('hidden');
  el['wave-hint'].textContent = 'считаем волну…';
  setSpeed(1, { silentUi: true });
  setPitch(0, { silentUi: true });
  library.current = info.path || null;
  if (library.open) renderLibrary();

  const bpm = Number(m.bpm);
  if (Number.isFinite(bpm) && bpm > 0) {
    el.bpm.value = String(bpm);
    el['bpm-row'].classList.remove('hidden');
  } else {
    el['bpm-row'].classList.add('hidden');
  }
  drawWave();
  if (notify) flash(`Загружено: ${info.file}`, 'good');
}

async function loadFile(path) {
  if (!path) return;
  try {
    showTrack(await window.msc.loadPath(path));
  } catch (err) {
    flash(`Не удалось открыть файл: ${err.message || err}`, 'bad');
  }
}

async function togglePlay() {
  if (!state.hasTrack) {
    await openFile();
    return;
  }
  await send(state.playing ? 'pause' : 'play');
}

async function openFile() {
  const p = await window.msc.openFile();
  if (p) await loadFile(p);
}

async function doExport() {
  if (!state.hasTrack) return;
  const base = (state.track?.file || 'track').replace(/\.[^.]+$/, '');
  const suffix = `${state.speed.toFixed(2)}x${state.pitch >= 0 ? '+' : ''}${state.pitch.toFixed(1)}st`;
  const dst = await window.msc.saveFile(`${base}_${suffix}.mp3`);
  if (!dst) return;
  await send('export', {
    path: dst,
    speed: state.speed,
    pitch: state.pitch,
    mode: 'rubberband',
  });
}

window.addEventListener('error', (e) => {
  console.error('[renderer error]', e.message, e.filename, e.lineno);
});
window.addEventListener('unhandledrejection', (e) => {
  console.error('[renderer rejection]', e.reason);
});

function setupUi() {
  el['btn-open'].addEventListener('click', openFile);
  el['btn-lib'].addEventListener('click', () => setLibraryOpen(!library.open));
  el['lib-close'].addEventListener('click', () => setLibraryOpen(false));
  el['library-backdrop'].addEventListener('click', () => setLibraryOpen(false));
  el['lib-refresh']?.addEventListener?.('contextmenu', (e) => e.preventDefault());
  el['lib-search'].addEventListener('input', (e) => {
    library.filter = e.target.value;
    renderLibrary();
  });
  el['lib-refresh'].addEventListener('click', async () => {
    el['lib-status'].textContent = 'Сканирую…';
    await send('libraryScan', { force: true });
  });
  el['lib-add'].addEventListener('click', pickFolder);
  library.current = null;

  // Автозапуск файлов, открытых двойным кликом в проводнике.
  el['opt-autoplay'].addEventListener('change', async (e) => {
    const on = e.target.checked;
    try {
      await window.msc.setSetting('autoplayExternal', on);
      flash(on ? 'Файлы из проводника будут запускаться сразу' : 'Файлы из проводника будут только загружаться');
    } catch (err) {
      e.target.checked = !on; // не сохранилось — возвращаем галку
      flash(`Не сохранилась настройка: ${err.message || err}`, 'bad');
    }
  });
  // Подгонка темпа под заданный BPM (исходный BPM берётся из метаданных, если есть).
  el['bpm-apply'].addEventListener('click', () => {
    const target = Number(el.bpm.value);
    const src = Number(state.track?.meta?.bpm);
    if (!Number.isFinite(target) || target <= 0) {
      flash('Укажите корректный BPM', 'bad');
      return;
    }
    if (!Number.isFinite(src) || src <= 0) {
      flash('Исходный BPM неизвестен — укажите скорость вручную', 'bad');
      return;
    }
    setSpeed(Math.round((target / src) * 1000) / 1000);
    flash(`Темп: ${src} → ${target} BPM (${(target / src).toFixed(3)}×)`);
  });
  el['btn-export'].addEventListener('click', doExport);
  el['btn-play'].addEventListener('click', togglePlay);
  el['btn-stop'].addEventListener('click', () => send('stop'));
  el['btn-prev'].addEventListener('click', () => stepTrack(-1));
  el['btn-next'].addEventListener('click', () => stepTrack(1));

  el.pitch.addEventListener('input', (e) => setPitch(Number(e.target.value)));
  el['pitch-presets'].addEventListener('click', (e) => {
    const chip = e.target.closest('.chip');
    if (chip) setPitch(Number(chip.dataset.pitch));
  });

  // Ввод тона с клавиатуры: применяем по Enter или при потере фокуса.
  setupNumberField(el['pitch-value'], {
    get: () => state.pitch,
    set: (v) => setPitch(v),
    min: PITCH_MIN,
    max: PITCH_MAX,
    bigStep: 1,
  });
  el['speed-presets'].addEventListener('click', (e) => {
    const chip = e.target.closest('.chip');
    if (chip) setSpeed(Number(chip.dataset.speed));
  });

  el.volume.addEventListener('input', (e) => {
    const v = Number(e.target.value);
    el['volume-value'].textContent = `${Math.round(v * 100)}%`;
    send('set', { param: 'volume', value: v });
  });
  el.volume.value = String(state.volume);
  el['volume-value'].textContent = `${Math.round(state.volume * 100)}%`;

  el['loop-toggle'].addEventListener('change', (e) => {
    if (e.target.checked && state.loop.end <= state.loop.start) {
      const dur = state.duration || 1;
      applyLoop(Math.max(0, dur - 8), dur);
    }
    send('set', { param: 'loop', value: e.target.checked });
  });
  el['repeat-toggle'].addEventListener('change', (e) =>
    send('set', { param: 'repeat', value: e.target.checked }),
  );

  // Смена звуковой карты требует перезапуска аудиодвижка.
  el.device.addEventListener('change', async (e) => {
    const name = e.target.value;
    try {
      localStorage.setItem('msc.device', name);
      await window.msc.cmd('selectDevice', { name });
      flash(`Аудиодвижок перезапущен для «${name}»`, 'good');
    } catch (err) {
      flash(`Не удалось сменить устройство: ${err.message || err}`, 'bad');
    }
  });

  (async () => {
    try {
      const list = await window.msc.devices();
      const saved = localStorage.getItem('msc.device');
      const outs = (list || []).filter((d) => d.out > 0);
      if (!outs.length) {
        el.device.innerHTML = '<option value="">(не найдено)</option>';
        el.device.disabled = true;
        return;
      }
      el.device.innerHTML = outs
        .map((d, i) => `<option value="${d.name}">${d.name} · ${Math.round(d.rate)} Гц</option>`)
        .join('');
      el.device.disabled = false;
      if (saved && outs.some((d) => d.name === saved)) {
        el.device.value = saved;
        await window.msc.cmd('selectDevice', { name: saved });
      } else {
        el.device.value = outs[0].name;
      }
    } catch {
      el.device.innerHTML = '<option value="">(неизвестно)</option>';
    }
  })();

  el['btn-loop-mark'].addEventListener('click', () => {
    state.silentMark = state.position;
    const b = state.silentMarkB && state.silentMarkB > state.silentMark + 0.2 ? state.silentMarkB : state.position + 5;
    state.silentMarkB = b;
    applyLoop(state.silentMark, b);
    flash(`A = ${fmtTime(state.silentMark)}, B = ${fmtTime(b)}`);
  });
  el['btn-loop-mark-b'].addEventListener('click', () => {
    state.silentMarkB = state.position;
    const a = Math.min(state.silentMark, state.silentMarkB - 0.2);
    applyLoop(Math.max(0, a), state.silentMarkB);
    flash(`A = ${fmtTime(Math.max(0, a))}, B = ${fmtTime(state.silentMarkB)}`);
  });
  el['btn-loop-clear'].addEventListener('click', () => {
    state.silentMark = 0;
    state.silentMarkB = null;
    applyLoop(0, 0);
    send('set', { param: 'loop', value: false });
  });

  // drag & drop
  const area = document.querySelector('.wave-area');
  ['dragenter', 'dragover'].forEach((type) =>
    area.addEventListener(type, (e) => {
      e.preventDefault();
      area.style.outline = '2px dashed var(--accent)';
    }),
  );
  ['dragleave', 'drop'].forEach((type) =>
    area.addEventListener(type, (e) => {
      e.preventDefault();
      area.style.outline = 'none';
    }),
  );
  area.addEventListener('drop', async (e) => {
    e.preventDefault();
    const file = e.dataTransfer?.files?.[0];
    if (!file) return;
    const path = window.msc.pathForFile(file);
    if (path) await loadFile(path);
  });

  // клавиатура
  window.addEventListener('keydown', (e) => {
    // Escape закрывает библиотеку. В полях ввода Escape уже занят под отмену
    // правки, поэтому там не перехватываем.
    if (e.key === 'Escape' && library.open && e.target.tagName !== 'INPUT') {
      e.preventDefault();
      setLibraryOpen(false);
      return;
    }
    if (e.target.tagName === 'INPUT') return;
    switch (e.key) {
      case ' ':
        e.preventDefault();
        togglePlay();
        break;
      case 'ArrowRight':
        e.preventDefault();
        send('seek', { position: Math.min(state.duration, state.position + (e.shiftKey ? 10 : 3)) });
        break;
      case 'ArrowLeft':
        e.preventDefault();
        send('seek', { position: Math.max(0, state.position - (e.shiftKey ? 10 : 3)) });
        break;
      case 'ArrowUp':
        e.preventDefault();
        setSpeed(state.speed + (e.shiftKey ? 0.01 : 0.05));
        break;
      case 'ArrowDown':
        e.preventDefault();
        setSpeed(state.speed - (e.shiftKey ? 0.01 : 0.05));
        break;
      case '[':
        setPitch(state.pitch - (e.shiftKey ? 0.1 : 1));
        break;
      case ']':
        setPitch(state.pitch + (e.shiftKey ? 0.1 : 1));
        break;
      case 'l':
      case 'L':
      case 'д':
      case 'Д':
        el['loop-toggle'].click();
        break;
      case 'b':
      case 'B':
      case 'и':
      case 'И':
        setLibraryOpen(!library.open);
        break;
      case '0':
        if (e.ctrlKey || e.metaKey) {
          e.preventDefault();
          setSpeed(1);
        }
        break;
      default:
        break;
    }
  });

  window.msc.onMenu(async ({ action }) => {
    switch (action) {
      case 'open':
        await openFile();
        break;
      case 'toggle':
        await togglePlay();
        break;
      case 'stop':
        await send('stop');
        break;
      case 'seekStart':
        await send('seek', { position: 0 });
        break;
      case 'loop':
        el['loop-toggle'].click();
        break;
      case 'resetSpeed':
        setSpeed(1);
        break;
      case 'resetPitch':
        setPitch(0);
        break;
      default:
        break;
    }
  });
}

/* ------------------------------------------------------------------ init */
function init() {
  cacheDom();
  setupWave();
  setupKnob();
  setupUi();
  setSpeed(1, { silentUi: true });
  setPitch(0, { silentUi: true });
  drawKnob();
  drawWave();

  window.__MSC_DEBUG = new URLSearchParams(location.search).has('debug');
  window.msc.onEvent((ev) => {
    if (window.__MSC_DEBUG) console.log(`[ev] ${ev.ev}`, JSON.stringify(ev).slice(0, 400));
    onEngineEvent(ev);
  });
  window.msc.onLog((line) => console.debug(line));

  // Движок мог послать 'ready'/'state' до загрузки окна — забираем состояние сами.
  (async () => {
    try {
      showEngineInfo(await window.msc.cmd('capabilities'));
      applyState(await window.msc.cmd('state'));
      setStatus('Готов. Откройте аудиофайл (Ctrl+O).');
    } catch (err) {
      el['engine-status'].textContent = 'аудиодвижок недоступен';
      flash(`Аудиодвижок не отвечает: ${err.message || err}`, 'bad');
    }
  })();

  window.msc.appInfo().then((info) => {
    el['engine-status'].textContent = `v${info.version} · Electron ${info.electron}`;
  });

  // Сохранённая настройка автозапуска из проводника.
  window.msc
    .getSettings()
    .then((s) => {
      el['opt-autoplay'].checked = s.autoplayExternal !== false;
    })
    .catch(() => {
      el['opt-autoplay'].checked = true;
    });
}

document.addEventListener('DOMContentLoaded', init);
