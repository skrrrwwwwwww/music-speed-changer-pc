/*
 * Тест долгой работы: гоняет движок N минут и следит за инвариантами.
 *
 *   node scripts/soak.mjs            # 10 минут
 *   node scripts/soak.mjs 30         # 30 минут
 *
 * Проверяет то, чего не ловит короткий e2e: щелчки, зависание playhead,
 * рост памяти, уход позиции, сборы протокола. Именно эти вещи вылезают
 * через час-два живой игры, а не за минуту.
 */
import { spawn, execSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const MINUTES = Number(process.argv[2] || 10);
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'msc-soak-'));
const PY = fs.existsSync(`${ROOT}/.venv/bin/python`) ? `${ROOT}/.venv/bin/python` : 'python3';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let failures = 0;
const check = (name, ok, detail = '') => {
  if (!ok) failures++;
  console.log(`  ${ok ? 'OK  ' : 'FAIL'} ${name}${detail ? ` — ${detail}` : ''}`);
};

// ----------------------------------------------------------- тестовые треки
function makeTracks(count) {
  const names = [];
  for (let i = 0; i < count; i++) {
    const f = path.join(TMP, `soak${i}.wav`);
    execSync(
      `ffmpeg -v error -y -f lavfi -i "sine=frequency=${180 + i * 37}:duration=${20 + i * 3}" ` +
        `-filter:a "tremolo=f=${3 + i}:d=0.7,volume=0.5" -ac 2 -c:a pcm_s16le "${f}"`,
      { stdio: 'ignore' },
    );
    names.push(f);
  }
  return names;
}

// ------------------------------------------------------------------- запуск
const tracks = makeTracks(4);
const proc = spawn(PY, ['-u', '-m', 'msc'], {
  cwd: ROOT,
  env: { ...process.env, PYTHONPATH: path.join(ROOT, 'python'), PYTHONIOENCODING: 'utf-8' },
  stdio: ['pipe', 'pipe', 'pipe'],
});

let seq = 1;
const pending = new Map();
let lastState = null;
let events = 0;

proc.stdout.on('data', (buf) => {
  for (const line of buf.toString().split('\n')) {
    if (!line.trim()) continue;
    let m;
    try {
      m = JSON.parse(line);
    } catch {
      continue;
    }
    events++;
    if (m.ev === 'state') lastState = m.state;
    if (m.id && pending.has(m.id)) {
      const { resolve, reject } = pending.get(m.id);
      pending.delete(m.id);
      m.ok ? resolve(m.result) : reject(new Error(m.error));
    }
  }
});
let stderrText = '';
proc.stderr.on('data', (b) => {
  stderrText += b.toString();
});

function send(cmd, args = {}, timeoutMs = 30000) {
  const id = seq++;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    proc.stdin.write(`${JSON.stringify({ id, cmd, ...args })}\n`);
    setTimeout(() => {
      if (pending.has(id)) {
        pending.delete(id);
        reject(new Error(`таймаут на ${cmd}`));
      }
    }, timeoutMs);
  });
}

function pssMb(pid) {
  try {
    const txt = fs.readFileSync(`/proc/${pid}/smaps_rollup`, 'utf8');
    const m = txt.match(/^Pss:\s+(\d+)/m);
    return m ? Number(m[1]) / 1024 : null;
  } catch {
    return null;
  }
}

function enginePid() {
  for (const entry of fs.readdirSync('/proc')) {
    if (!/^\d+$/.test(entry)) continue;
    try {
      const cmd = fs.readFileSync(`/proc/${entry}/cmdline`, 'utf8').replace(/\0/g, ' ');
      if (cmd.includes('-m msc') || cmd.includes('msc_entry')) return Number(entry);
    } catch {
      /* пропускаем чужие процессы */
    }
  }
  return null;
}

// ---------------------------------------------------------------- сценарий
const SPEEDS = [1, 1.25, 1.5, 2, 0.75, 0.5, 1.75];
const PITCHES = [0, 3, -5, 7, -7, 12, -12];

console.log(`Тест долгой работы: ${MINUTES} мин, движок pid будет определён по ходу`);
console.log(`Треки: ${tracks.length}, tmp: ${TMP}\n`);

const t0 = Date.now();
let cycles = 0;
let lastUnderruns = 0;
let memStart = null;
let memPeak = 0;
let stalls = 0;
let biggestStallMs = 0;
let loadPeak = 0;
let loadSlow = 0;

try {
  for (let i = 0; i < 200 && !lastState; i++) await sleep(100);
  if (!lastState) throw new Error('движок не прислал состояние');

  const pid = enginePid();
  memStart = pssMb(pid);
  console.log(`старт: pid=${pid}, PSS=${memStart?.toFixed(1)} МБ\n`);

  while ((Date.now() - t0) / 60000 < MINUTES) {
    const file = tracks[cycles % tracks.length];
    const speed = SPEEDS[cycles % SPEEDS.length];
    const pitch = PITCHES[cycles % PITCHES.length];

    const tl = Date.now();
    await send('load', { path: file });
    const loadMs = Date.now() - tl;
    loadPeak = Math.max(loadPeak, loadMs);
    if (loadMs > 1500) loadSlow++;

    await send('set', { param: 'speed', value: speed });
    await send('set', { param: 'pitch', value: pitch });
    await send('seek', { position: Math.random() * 5 });
    await send('play');

    // Играем и следим: позиция обязана идти, щелчков быть не должно.
    let prev = lastState?.position ?? 0;
    const started = Date.now();
    while (Date.now() - started < 2500) {
      await sleep(250);
      const st = lastState;
      if (!st) continue;
      const gap = st.position - prev;
      if (gap < 0.02 && speed > 0) {
        stalls++;
        biggestStallMs = Math.max(biggestStallMs, Date.now() - started);
      }
      prev = st.position;
    }
    await send('pause');

    const st = lastState;
    if (st && st.underruns > lastUnderruns) lastUnderruns = st.underruns;
    const mem = pssMb(pid);
    if (mem) memPeak = Math.max(memPeak, mem);
    cycles++;

    if (cycles % 10 === 0) {
      console.log(
        `  цикл ${cycles}: ${((Date.now() - t0) / 60000).toFixed(1)} мин, ` +
          `underruns=${st?.underruns}, PSS=${mem?.toFixed(1)} МБ, загрузка ${loadMs} мс`,
      );
    }
  }

  const memEnd = pssMb(pid);
  const growth = memEnd - memStart;
  console.log('\n=== ИТОГИ ===');
  console.log(`циклов пройдено:        ${cycles}`);
  console.log(`событий от движка:      ${events}`);
  console.log(`щелчков (underruns):    ${lastUnderruns}`);
  console.log(`зависаний playhead:     ${stalls} (худшее ${biggestStallMs} мс)`);
  console.log(`загрузка трека: худшая ${loadPeak} мс, медленных ${loadSlow}`);
  console.log(`память движка: ${memStart?.toFixed(1)} → ${memEnd?.toFixed(1)} МБ ` +
    `(пик ${memPeak.toFixed(1)}, рост ${growth >= 0 ? '+' : ''}${growth.toFixed(1)} МБ)`);

  check('щелчков нет за весь прогон', lastUnderruns === 0, `underruns=${lastUnderruns}`);
  check('playhead не зависал', stalls === 0, `зависаний ${stalls}`);
  check('память не растёт', growth < 30, `рост ${growth.toFixed(1)} МБ`);
  check('загрузка трека быстрая', loadSlow === 0, `медленных загрузок ${loadSlow}`);
  check('движок не падал', proc.exitCode === null, `код ${proc.exitCode}`);
  check('нет ошибок в stderr', !/Traceback|Error/.test(stderrText), stderrText.slice(0, 200));
} catch (err) {
  console.error('\nСБОЙ ТЕСТА:', err.message);
  failures++;
} finally {
  try {
    proc.stdin.write(`${JSON.stringify({ cmd: 'quit' })}\n`);
    await sleep(600);
  } catch {
    /* уже закрыт */
  }
  proc.kill();
  fs.rmSync(TMP, { recursive: true, force: true });
}

console.log(`\n${failures === 0 ? 'SOAK OK' : `SOAK: ПРОВАЛОВ ${failures}`}`);
process.exit(failures ? 1 : 0);