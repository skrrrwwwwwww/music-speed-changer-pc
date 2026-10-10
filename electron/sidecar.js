'use strict';

const { spawn } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');

/**
 * Управление python-сайдкаром: NDJSON по stdin/stdout.
 * Каждой команде соответствует ответ с тем же id, события приходят без id.
 */
class Sidecar {
  constructor({ onEvent, onLog, onExit } = {}) {
    this.onEvent = onEvent || (() => {});
    this.onLog = onLog || (() => {});
    this.onExit = onExit || (() => {});
    this.proc = null;
    this.pending = new Map();
    this.nextId = 1;
    this.ready = false;
    this.caps = null;
    this.exitInfo = null;
    this.stopping = false;
    this.deviceName = null;
  }

  /**
   * Каталог с ресурсами приложения (там лежит движок).
   * Не полагаемся только на app.isPackaged: в Flatpak-песочнице он может быть
   * false, хотя resources/bin/msc-engine рядом есть.
   */
  resourcesDir() {
    return process.resourcesPath || path.join(path.dirname(appPath()), 'resources');
  }

  /**
   * Команда запуска аудиодвижка.
   *
   * В собранном пакете (.deb/.AppImage/Flatpak) это один исполняемый файл
   * `resources/bin/msc-engine`, собранный PyInstaller'ом: системный Python
   * пользователю не нужен. В режиме разработки — интерпретатор из .venv.
   */
  resolvePython() {
    const candidates = [];
    if (process.env.MSC_PYTHON) {
      candidates.push(process.env.MSC_PYTHON);
    }
    // Собранный движок — в .deb, AppImage, Flatpak и любой другой упаковке.
    candidates.push(path.join(this.resourcesDir(), 'bin', binName('msc-engine')));
    if (!isBundled()) {
      const root = path.join(__dirname, '..');
      candidates.push(path.join(root, '.venv', 'bin', 'python'));
      candidates.push(path.join(root, '.venv', 'Scripts', 'python.exe'));
    }
    if (process.platform === 'win32') {
      candidates.push('python.exe', 'python');
    } else {
      candidates.push('/usr/bin/python3', 'python3');
    }
    for (const c of candidates) {
      if (isExecutable(c)) return c;
    }
    return process.platform === 'win32' ? 'python' : 'python3';
  }

  /** Собранный движок запускается сам, без `-m msc`. */
  isFrozenEngine() {
    return path.basename(this.resolvePython()).startsWith('msc-engine');
  }

  start(opts = {}) {
    const python = this.resolvePython();
    const frozen = this.isFrozenEngine();
    const args = frozen ? [] : ['-u', '-m', 'msc'];
    if (opts.silent) args.push('--silent');
    if (opts.device) args.push('--device', opts.device);
    this.deviceName = opts.device || null;
    this.stopping = false;
    const env = { ...process.env, PYTHONIOENCODING: 'utf-8' };
    if (!frozen) env.PYTHONPATH = this.resolvePythonPath();
    // Приложенный ffmpeg имеет приоритет над системным — версия с rubberband.
    const bundledFfmpeg = path.join(this.resourcesDir(), 'bin', 'ffmpeg');
    if (frozen && isExecutable(bundledFfmpeg)) env.MSC_FFMPEG = bundledFfmpeg;
    const ffDir = ffmpegBinDir(env.MSC_FFMPEG || bundledFfmpeg);
    if (ffDir) env.PATH = ffDir + path.delimiter + (env.PATH || '');

    this.onLog(`engine: ${python} ${args.join(' ')}`);
    this.proc = spawn(python, args, {
      cwd: this.resolveCwd(),
      env,
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    });

    // NDJSON разбираем сами: readline-обёртка ненадёжна на pipe из Electron.
    let buffer = '';
    this.proc.stdout.setEncoding('utf8');
    this.proc.stdout.on('data', (chunk) => {
      buffer += chunk;
      let idx;
      while ((idx = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 1);
        if (line.trim()) this._onLine(line);
      }
      if (buffer.length > 1 << 20) buffer = '';
    });

    this.proc.stderr.on('data', (d) => {
      const text = d.toString().trim();
      if (text) this.onLog(`[py] ${text}`);
    });
    this.proc.on('error', (err) => {
      this.onEvent({ ev: 'error', message: `Не удалось запустить Python: ${err.message}` });
    });
    this.proc.on('exit', (code, signal) => {
      const pending = [...this.pending.values()];
      this.pending.clear();
      const reason = this.stopping ? 'аудиодвижок остановлен' : 'python завершился';
      pending.forEach(({ reject }) => reject(new Error(reason)));
      this.exitInfo = { code, signal };
      this.ready = false;
      this.onLog(`python exited: code=${code} signal=${signal}`);
      this.onExit(this.exitInfo);
    });
  }

  resolvePythonPath() {
    // Нужно только для запуска из исходников (dev-режим).
    return path.join(__dirname, '..', 'python');
  }

  resolveCwd() {
    if (isBundled()) return path.dirname(this.resolvePython());
    return path.join(__dirname, '..');
  }

  _onLine(line) {
    if (!line || !line.trim()) return;
    let msg;
    try {
      msg = JSON.parse(line);
    } catch {
      this.onLog(`[py:bad] ${line.slice(0, 200)}`);
      return;
    }
    if (msg.ev) {
      if (msg.ev === 'ready') {
        this.ready = true;
        this.caps = msg;
      }
      this.onEvent(msg);
      return;
    }
    if (msg.id && this.pending.has(msg.id)) {
      const { resolve, reject } = this.pending.get(msg.id);
      this.pending.delete(msg.id);
      if (msg.ok) resolve(msg.result ?? null);
      else reject(new Error(msg.error || 'ошибка python'));
    }
  }

  send(cmd, args = {}) {
    if (!this.proc || this.proc.exitCode !== null) {
      return Promise.reject(new Error('Аудиодвижок не запущен'));
    }
    const id = this.nextId++;
    const payload = JSON.stringify({ id, cmd, ...args }) + '\n';
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.proc.stdin.write(payload, (err) => {
        if (err) {
          this.pending.delete(id);
          reject(err);
        }
      });
    });
  }

  async capabilities() {
    try {
      return await this.send('capabilities');
    } catch (err) {
      return { error: err.message };
    }
  }

  async devices() {
    try {
      const res = await this.send('devices');
      return Array.isArray(res) ? res : [];
    } catch {
      return [];
    }
  }

  /** Останавливает процесс. Промис резолвится, когда он действительно завершён. */
  stop() {
    if (!this.proc) return Promise.resolve();
    const proc = this.proc;
    this.stopping = true;
    try {
      proc.stdin.write(JSON.stringify({ cmd: 'quit' }) + '\n');
    } catch {
      /* ignore */
    }
    return new Promise((resolve) => {
      const done = () => {
        clearTimeout(timer);
        resolve();
      };
      const timer = setTimeout(() => {
        if (proc.exitCode === null) {
          try {
            proc.kill('SIGTERM');
          } catch {
            /* ignore */
          }
          setTimeout(() => {
            try {
              if (proc.exitCode === null) proc.kill('SIGKILL');
            } catch {
              /* ignore */
            }
            done();
          }, 400);
        } else {
          done();
        }
      }, 800);
      proc.once('exit', done);
    });
  }
}

/** Путь к исполняемому файлу Electron (там же лежат ресурсы). */
function appPath() {
  return process.execPath;
}

/**
 * Признак «собранного» приложения: .deb, AppImage, Flatpak.
 * Проверяем наличие движка, а не app.isPackaged — в Flatpak он не всегда true.
 */
function isBundled() {
  const engine = path.join(process.resourcesPath || '', 'bin', binName('msc-engine'));
  if (engine && isExecutable(engine)) return true;
  try {
    // eslint-disable-next-line global-require
    return !!require('electron').app?.isPackaged;
  } catch {
    return false;
  }
}

function binName(base) {
  return process.platform === 'win32' ? `${base}.exe` : base;
}

function isExecutable(file) {
  try {
    fs.accessSync(file, fs.constants.X_OK);
    return fs.statSync(file).isFile();
  } catch {
    return false;
  }
}

/** Каталог с ffmpeg/ffprobe — добавляем в PATH, чтобы ffprobe тоже нашёлся. */
function ffmpegBinDir(candidate) {
  if (!candidate) return '';
  const dir = path.dirname(candidate);
  try {
    return isExecutable(path.join(dir, 'ffprobe')) || isExecutable(path.join(dir, 'ffmpeg')) ? dir : '';
  } catch {
    return '';
  }
}

module.exports = { Sidecar };
