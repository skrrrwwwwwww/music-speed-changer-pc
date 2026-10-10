/**
 * Сквозной тест: настоящий python-сайдкар, настоящая звуковая карта.
 * Проверяет тот же путь, что и Electron: NDJSON-протокол -> Engine -> вывод.
 *
 *   node scripts/e2e.mjs
 */

import { spawn, execSync } from 'node:child_process';
import readline from 'node:readline';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const PY = fs.existsSync(`${ROOT}/.venv/bin/python`) ? `${ROOT}/.venv/bin/python` : 'python3';
const haveFfmpeg = (() => {
  try {
    execSync('ffmpeg -version', { stdio: 'ignore' });
    return true;
  } catch {
    return false;
  }
})();

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let fails = 0;
const check = (name, ok, detail = '') => {
  console.log(`${ok ? 'OK  ' : 'FAIL'}  ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) fails++;
};

/* ---------------------------------------------------------- тестовый трек */
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'msc-e2e-'));
if (!haveFfmpeg) {
  console.error('ffmpeg не найден — тест невозможен');
  process.exit(2);
}
const TRACKS = ['track1.mp3', 'track2.mp3', 'track3.mp3'];
for (const [i, f] of TRACKS.entries()) {
  execSync(
    `ffmpeg -v error -y -f lavfi -i "sine=frequency=${220 * (i + 1)}:duration=3" ` +
      `-ac 2 -ar 44100 -metadata title="E2E ${i + 1}" -metadata artist=Synth ` +
      `-metadata TBPM=${110 + i * 10} -c:a libmp3lame "${path.join(TMP, f)}"`,
    { stdio: 'ignore' },
  );
}

/* ---------------------------------------------------------------- сайдкар */
const proc = spawn(PY, ['-u', '-m', 'msc'], {
  cwd: ROOT,
  env: { ...process.env, PYTHONPATH: path.join(ROOT, 'python'), PYTHONIOENCODING: 'utf-8' },
  stdio: ['pipe', 'pipe', 'pipe'],
});
proc.stderr.on('data', (d) => {
  const text = d.toString().trim();
  if (text) console.log('  py> ' + text.split('\n')[0].slice(0, 160));
});

let nextId = 0;
const waiters = new Map();
const events = [];
readline.createInterface({ input: proc.stdout }).on('line', (line) => {
  const msg = JSON.parse(line);
  if (msg.ev) {
    events.push(msg);
    return;
  }
  const w = waiters.get(msg.id);
  if (w) {
    waiters.delete(msg.id);
    msg.ok ? w.res(msg.result) : w.rej(new Error(msg.error));
  }
});
const send = (cmd, args = {}) =>
  new Promise((res, rej) => {
    const id = ++nextId;
    waiters.set(id, { res, rej });
    proc.stdin.write(JSON.stringify({ id, cmd, ...args }) + '\n');
  });
const evs = (name) => events.filter((e) => e.ev === name);
const state = () => {
  const s = evs('state');
  return s.length ? s[s.length - 1].state : null;
};

/* ------------------------------------------------------------------ сценарий */
try {
  for (let i = 0; i < 60 && !evs('ready').length; i++) await sleep(100);
  const ready = evs('ready')[0];
  check('готовность движка', !!ready, ready ? `${ready.device?.name ?? '?'} @ ${ready.rate} Гц` : 'нет события');

  for (const f of TRACKS) {
    const info = await send('load', { path: path.join(TMP, f) });
    check(
      `загрузка ${f}`,
      info.file === f && Math.abs(info.duration - 3) < 0.1,
      `${info.name} | ${info.meta?.bpm ?? '?'} BPM | ${info.samplerate} Гц | ${info.channels} кан.`,
    );
    await sleep(400);
    const p = evs('peaks').slice(-1)[0];
    check(`  огибающая ${f}`, !!p, p ? `бакетов ${p.buckets}` : 'нет события peaks');
  }

  await send('load', { path: path.join(TMP, TRACKS[1]) });
  for (const [speed, pitch] of [
    [1, 0],
    [2, 0],
    [0.5, 0],
    [1, 7],
    [1, -7],
    [3.5, 12],
    [0.25, -12],
  ]) {
    await send('set', { param: 'speed', value: speed });
    await send('set', { param: 'pitch', value: pitch });
    await send('seek', { position: 0 });
    await send('play');
    await sleep(600);
    const st = state();
    check(
      `воспроизведение ${speed}× ${pitch} st`,
      st.playing && Math.abs(st.position - 0.6 * speed) < 0.25,
      `позиция ${st.position.toFixed(2)} с (ожидалось ~${(0.6 * speed).toFixed(2)})`,
    );
    await send('pause');
  }
  check('без потерь в очереди', state().underruns <= 6, `underruns=${state().underruns}`);

  await send('set', { param: 'speed', value: 1 });
  await send('seek', { position: 2.0 });
  await sleep(150);
  check('перемотка', Math.abs(state().position - 2.0) < 0.15, `позиция ${state().position.toFixed(3)} с`);

  await send('set', { param: 'loopRange', value: [0.5, 1.5] });
  await send('set', { param: 'loop', value: true });
  await send('play');
  await sleep(3000);
  const ls = state();
  check('петля A–B', ls.loop.enabled && ls.position >= 0.4 && ls.position <= 1.6, `позиция ${ls.position.toFixed(3)} с в [0.5; 1.5]`);
  await send('set', { param: 'loop', value: false });

  const devs = await send('devices');
  check('устройства вывода', Array.isArray(devs) && devs.some((d) => d.out > 0), `${devs.length} шт.`);

  const caps = await send('capabilities');
  check('rubberband доступен', caps.rubberband === true, caps.ffmpeg ? 'ffmpeg есть' : 'ffmpeg нет');

  for (const [mode, ext] of [
    ['rubberband', 'mp3'],
    ['engine', 'flac'],
  ]) {
    const out = path.join(TMP, `out-${mode}.${ext}`);
    const before = { ok: evs('exported').length, err: evs('exportFailed').length };
    await send('export', { path: out, speed: 1.5, pitch: 5, mode });
    for (let i = 0; i < 100 && evs('exported').length === before.ok && evs('exportFailed').length === before.err; i++) {
      await sleep(100);
    }
    const fail = evs('exportFailed').slice(-1)[0];
    if (fail) {
      check(`экспорт ${mode}`, false, fail.message);
      continue;
    }
    const dur = parseFloat(
      execSync(`ffprobe -v error -show_entries format=duration -of csv=p=0 "${out}"`).toString().trim(),
    );
    check(`экспорт ${mode}`, Math.abs(dur - 2.0) < 0.15, `${ext}: ${dur.toFixed(2)} с (3 с трек при 1.5×)`);
  }
} catch (err) {
  check('сценарий не упал', false, err.message);
} finally {
  try {
    proc.stdin.write('{"cmd":"quit"}\n');
  } catch {
    /* ignore */
  }
  await sleep(400);
  proc.kill();
  fs.rmSync(TMP, { recursive: true, force: true });
}

console.log(`\n${fails ? `ПРОВАЛЫ: ${fails}` : 'E2E OK'}  (событий state: ${evs('state').length}, пиков: ${evs('peaks').length})`);
process.exit(fails ? 1 : 0);
