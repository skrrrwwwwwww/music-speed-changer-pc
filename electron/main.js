'use strict';

const { app, BrowserWindow, ipcMain, dialog, shell, Menu } = require('electron');
const path = require('node:path');
const fs = require('node:fs');
const { Sidecar } = require('./sidecar');

const isDev = !app.isPackaged;

/* ------------------------------------------------------------------ логи */
const LOG_DIR = path.join(app.getPath('userData'), 'logs');
let logFile = null;
try {
  fs.mkdirSync(LOG_DIR, { recursive: true });
  logFile = path.join(LOG_DIR, 'main.log');
  // Ротация: не даём файлу расти бесконечно.
  if (fs.existsSync(logFile) && fs.statSync(logFile).size > 2 * 1024 * 1024) {
    fs.renameSync(logFile, logFile + '.1');
  }
} catch {
  logFile = null;
}

/** Пишет в файл логов и в stdout (если есть терминал). */
function log(...args) {
  const line = args
    .map((a) => (typeof a === 'string' ? a : safeJson(a)))
    .join(' ');
  const stamp = new Date().toISOString();
  if (logFile) {
    try {
      fs.appendFileSync(logFile, `${stamp} ${line}\n`);
    } catch {
      /* лог недоступен — молча продолжаем */
    }
  }
  if (process.stdout.isTTY || isDev) {
    console.log(`[msc] ${line}`);
  }
}

function safeJson(value) {
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

/* --------------------------------------------------------------- настройки */
/* Живут в файле, а не в localStorage: открыть файл из проводника можно и
   когда окно ещё не создано, и настройка нужна до готовности renderer. */
const DEFAULTS = { autoplayExternal: true };
const SETTINGS_FILE = path.join(app.getPath('userData'), 'settings.json');
let settings = { ...DEFAULTS };

function readSettings() {
  try {
    if (fs.existsSync(SETTINGS_FILE)) {
      const raw = JSON.parse(fs.readFileSync(SETTINGS_FILE, 'utf-8'));
      settings = { ...DEFAULTS, ...(raw && typeof raw === 'object' ? raw : {}) };
    }
  } catch (err) {
    log('не читаются настройки, беру значения по умолчанию:', err && err.message);
    settings = { ...DEFAULTS };
  }
  return settings;
}

function writeSettings() {
  try {
    fs.writeFileSync(SETTINGS_FILE, JSON.stringify(settings, null, 1), 'utf-8');
  } catch (err) {
    log('не сохранились настройки:', err && err.message);
  }
}

function setSetting(key, value) {
  if (!(key in DEFAULTS)) throw new Error(`неизвестная настройка: ${key}`);
  settings[key] = value;
  writeSettings();
  log(`настройка ${key} = ${JSON.stringify(value)}`);
  return settings;
}

readSettings();

/** Автозапуск при открытии файла из проводника.
 *  --autoplay / --no-autoplay в командной строке важнее сохранённого. */
function autoplayForExternalFile() {
  if (process.argv.includes('--autoplay')) return true;
  if (process.argv.includes('--no-autoplay')) return false;
  return settings.autoplayExternal !== false;
}

log(`=== запуск v${app.getVersion()}, electron ${process.versions.electron}, packaged=${app.isPackaged}`);
log(`argv: ${process.argv.slice(1).join(' ')}`);
log(`resourcesPath: ${process.resourcesPath}`);
log(`лог: ${logFile || 'недоступен'}`);

/** Необработанные ошибки — в лог, иначе о них никто не узнает. */
process.on('uncaughtException', (err) => {
  log('!!! uncaughtException:', err && err.stack ? err.stack : String(err));
});
process.on('unhandledRejection', (err) => {
  log('!!! unhandledRejection:', err && err.stack ? err.stack : String(err));
});

/** Флаги нашего отладочного интерфейса — их не считаем именем файла. */
const DEV_FLAGS = new Set(['--smoke', '--shot', '--silent', '--device', '--debug-ui']);

const AUDIO_EXT = new Set([
  '.mp3', '.wav', '.flac', '.ogg', '.oga', '.opus', '.m4a', '.mp4', '.aac',
  '.wma', '.aif', '.aiff', '.ape', '.wv', '.mka', '.webm', '.caf', '.amr', '.3gp',
]);

/**
 * Приводит аргумент запуска к пути файла.
 *
 * Файловый менеджер по схеме .desktop (`Exec=... %U`) передаёт не путь, а
 * URI: file:///home/user/Музыка/песня.mp3. Раньше мы ждали только путь,
 * statSync на строке с file:// падал, и файл просто не открывался.
 * Некоторые менеджеры (и «Открыть с помощью») передают обычный путь —
 * поэтому поддерживаем оба вида, плюс percent-экранирование пробелов.
 */
function filePathFromArg(arg) {
  if (!arg) return null;
  if (!/^file:\/\//i.test(arg)) return arg;
  let url;
  try {
    url = new URL(arg);
  } catch {
    return null;
  }
  // Удалённый файл (file://host/path) нам не открыть.
  if (url.hostname && url.hostname !== 'localhost') return null;
  try {
    return decodeURIComponent(url.pathname);
  } catch {
    return url.pathname;
  }
}

/**
 * Ищет аудиофайл среди аргументов командной строки.
 * Нужно для «Открыть с помощью» и запуска по клику на файл в менеджере:
 * Electron на Linux/Lab не передаёт такие файлы иначе.
 */
function audioFileFromArgv(argv = process.argv.slice(1)) {
  const tried = [];
  for (const arg of argv) {
    if (DEV_FLAGS.has(arg) || arg.startsWith('--')) continue;
    const file = filePathFromArg(arg);
    if (!file) continue;
    if (!file.includes(path.sep) && !file.includes('.')) continue;
    const ext = path.extname(file).toLowerCase();
    if (!AUDIO_EXT.has(ext)) continue;
    try {
      if (fs.statSync(file).isFile()) return path.resolve(file);
    } catch {
      tried.push(`${arg} (не открывается)`);
    }
  }
  // Полезно в логе: покажет, что файл был, но не нашёлся или не тот.
  if (tried.length) log('аргументы с аудио-расширением, но файла нет:', tried.join(', '));
  return null;
}

/** @type {BrowserWindow|null} */
let win = null;
/** @type {Sidecar|null} */
let sidecar = null;
let quitting = false;

// Автотест интерфейса: загрузить файл, снять скриншот и выйти (только в dev).
const smokeFile = process.argv.includes('--smoke') ? process.argv[process.argv.indexOf('--smoke') + 1] : null;
const smokeShot = process.argv.includes('--shot') ? process.argv[process.argv.indexOf('--shot') + 1] : null;

ipcMain.handle('msc:devices', async () => (sidecar ? sidecar.devices() : []));

/** Выбор папки с музыкой для библиотеки. */
ipcMain.handle('msc:pickDirectory', async () => {
  const res = await dialog.showOpenDialog(win, {
    title: 'Выберите папку с музыкой',
    properties: ['openDirectory', 'multiSelections'],
    defaultPath: app.getPath('music'),
  });
  if (res.canceled || !res.filePaths.length) return null;
  return res.filePaths[0];
});

/** Перезапуск аудиодвижка с другим устройством вывода. */
ipcMain.handle('msc:selectDevice', async (_e, { name }) => {
  if (!sidecar) throw new Error('Аудиодвижок не запущен');
  const res = await sidecar.send('selectDevice', { name });
  if (sidecar.stop()) {
    await new Promise((r) => setTimeout(r, 500));
    startSidecar({ device: name });
  }
  return res;
});

function createWindow() {
  win = new BrowserWindow({
    width: 1180,
    height: 800,
    minWidth: 900,
    minHeight: 640,
    backgroundColor: '#12141a',
    title: 'Music Speed Changer',
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  win.webContents.on('console-message', (...args) => {
    const [a, b, c, d, e] = args;
    // Electron <35: (event, level, message, line, sourceId); >=35: (event, details)
    if (b && typeof b === 'object') {
      if (b.level === 'error' || b.level === 'warning') log(`[renderer:${b.level}] ${b.message}`);
    } else if (typeof a === 'number' || typeof b === 'number') {
      const level = typeof a === 'number' ? a : b;
      if (level >= 2) log(`[renderer:${level}] ${c}  (${e}:${d})`);
    }
  });
  win.webContents.on('render-process-gone', (_e, details) => {
    log('[renderer] процесс отрисовки упал:', JSON.stringify(details));
  });
  win.webContents.on('did-fail-load', (_e, code, desc, url) => {
    log(`[renderer] не загрузилась страница ${url}: ${code} ${desc}`);
  });
  win.webContents.on('preload-error', (_e, file, err) => {
    log(`[renderer] ошибка preload ${file}:`, err && err.message);
  });

  const query = process.argv.includes('--debug-ui') ? { search: 'debug=1' } : undefined;
  win.loadFile(path.join(__dirname, '..', 'renderer', 'index.html'), query);
  win.once('ready-to-show', () => {
    win.show();
    if (win) win.focus();
    log('окно показано, visible =', win.isVisible());
    // Автотесты UI: MSC_EVAL — JS, выполняемый в renderer после загрузки.
    if (process.env.MSC_EVAL) {
      setTimeout(async () => {
        try {
          const res = await win.webContents.executeJavaScript(process.env.MSC_EVAL, true);
          console.log(`[eval] ${safeJson(res)}`);
        } catch (err) {
          console.log(`[eval-error] ${err.message}`);
        }
        if (process.env.MSC_EVAL_EXIT !== '0') setTimeout(() => app.quit(), 400);
      }, Number(process.env.MSC_EVAL_DELAY || 1500));
    }
    // Файл из командной строки (клик по файлу в менеджере, «Открыть с помощью»).
    if (pendingFile) {
      const file = pendingFile;
      pendingFile = null;
      setTimeout(() => loadIntoUi(file, { autoplay: autoplayForExternalFile() }), 400);
    }
    if (smokeFile) {
      setTimeout(async () => {
        try {
          const info = await sidecar.send('load', { path: smokeFile });
          send('msc:event', { ev: 'smokeLoaded', info });
          await sidecar.send('set', { param: 'speed', value: 1.5 });
          await sidecar.send('play');
        } catch (err) {
          send('msc:event', { ev: 'error', message: String(err) });
        }
      }, 900);
    }
    if (smokeShot) {
      setTimeout(async () => {
        try {
          if (process.env.MSC_SMOKE_SETUP) {
            send('msc:event', { ev: 'smokeSetup', ...JSON.parse(process.env.MSC_SMOKE_SETUP) });
            await new Promise((r) => setTimeout(r, 500));
          }
          const img = await win.webContents.capturePage();
          fs.writeFileSync(smokeShot, img.toPNG());
          send('msc:event', { ev: 'smokeShot', path: smokeShot });
        } catch (err) {
          send('msc:event', { ev: 'error', message: String(err) });
        }
        if (!smokeFile) app.quit();
        else setTimeout(() => app.quit(), 400);
      }, Number(process.env.MSC_SHOT_DELAY || 2500));
    }
  });
  win.on('closed', () => {
    win = null;
  });

  // Внешние ссылки открываем в системном браузере.
  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });
}

function send(channel, payload) {
  if (win && !win.isDestroyed()) win.webContents.send(channel, payload);
}

/** Файл, переданный аргументом или через open-file; ждёт готовности окна. */
let pendingFile = null;

/** Загружает файл в UI так же, как это делает диалог открытия.
 *  autoplay=true — файл пришёл из проводника и должен сразу заиграть. */
async function loadIntoUi(file, { autoplay = false } = {}) {
  try {
    log(`открываю ${file}, автозапуск=${autoplay}`);
    const info = await sidecar.send('load', { path: file, autoplay });
    send('msc:event', { ev: 'openFile', info, autoplay });
    if (autoplay) {
      // Проверяем факт воспроизведения: флаг мог уйти, а звук не пошёл.
      setTimeout(async () => {
        try {
          const st = await sidecar.send('state');
          log(`проверка автозапуска: играет=${st.playing} позиция=${Number(st.position).toFixed(2)} с`);
        } catch {
          /* движок занят — не страшно */
        }
      }, 900);
    }
  } catch (err) {
    send('msc:event', { ev: 'error', message: `Не удалось открыть ${path.basename(file)}: ${err.message || err}` });
  }
}

function startSidecar(opts = {}) {
  sidecar = new Sidecar({
    onEvent: (ev) => {
      if (ev.ev === 'error' || ev.ev === 'crash') log(`событие ${ev.ev}:`, ev);
      send('msc:event', ev);
    },
    onLog: (line) => {
      log(line);
      send('msc:log', line);
    },
    onExit: (info) => {
      log('аудиодвижок завершился:', info);
      if (!quitting && !sidecar?.stopping) send('msc:event', { ev: 'crash', ...info });
    },
  });
  try {
    sidecar.start(opts);
  } catch (err) {
    log('!!! не удалось запустить аудиодвижок:', err && err.stack ? err.stack : String(err));
    send('msc:event', { ev: 'error', message: `Не удалось запустить аудиодвижок: ${err.message || err}` });
  }
}

/* ------------------------------------------------------------------ IPC */
ipcMain.handle('msc:cmd', async (_e, cmd, args = {}) => {
  if (!sidecar) throw new Error('Аудиодвижок не запущен');
  return sidecar.send(cmd, args);
});

/* --------------------------------------------------------------- настройки */
ipcMain.handle('msc:getSettings', () => settings);

ipcMain.handle('msc:setSetting', (_e, key, value) => setSetting(key, value));

ipcMain.handle('msc:openFile', async () => {
  const res = await dialog.showOpenDialog(win, {
    title: 'Открыть аудиофайл',
    properties: ['openFile'],
    filters: [
      { name: 'Аудио', extensions: ['mp3', 'wav', 'flac', 'ogg', 'm4a', 'aac', 'opus', 'wma', 'aiff', 'ape', 'mp4'] },
      { name: 'Все файлы', extensions: ['*'] },
    ],
  });
  if (res.canceled || !res.filePaths.length) return null;
  return res.filePaths[0];
});

ipcMain.handle('msc:loadPath', async (_e, filePath) => {
  if (!filePath) return null;
  if (!fs.existsSync(filePath)) throw new Error(`Файл не найден: ${filePath}`);
  return sidecar.send('load', { path: filePath });
});

ipcMain.handle('msc:saveFile', async (_e, { defaultName, exts }) => {
  const res = await dialog.showSaveDialog(win, {
    title: 'Сохранить обработанный трек',
    defaultPath: defaultName,
    filters: [
      { name: 'MP3', extensions: ['mp3'] },
      { name: 'M4A (AAC)', extensions: ['m4a'] },
      { name: 'FLAC', extensions: ['flac'] },
      { name: 'OGG Vorbis', extensions: ['ogg'] },
      { name: 'Opus', extensions: ['opus'] },
      { name: 'WAV', extensions: ['wav'] },
    ],
  });
  if (res.canceled || !res.filePath) return null;
  return res.filePath;
});

ipcMain.handle('msc:dragPath', async (_e, filePath) => {
  // Electron отдаёт абсолютный путь файла при drop.
  if (!filePath) return null;
  return fs.promises
    .realpath(filePath)
    .then((p) => ({ path: p, size: fs.statSync(p).size }))
    .catch(() => null);
});

ipcMain.handle('msc:appInfo', async () => ({
  version: app.getVersion(),
  electron: process.versions.electron,
  chrome: process.versions.chrome,
  node: process.versions.node,
  platform: process.platform,
  dev: isDev,
  resources: isDev ? path.join(__dirname, '..') : process.resourcesPath,
}));

ipcMain.handle('msc:restartEngine', async () => {
  if (sidecar) await sidecar.stop();
  const device = sidecar?.deviceName;
  startSidecar({ device });
  return true;
});

/* ----------------------------------------------------------------- меню */
function buildMenu() {
  const template = [
    ...(isDev ? [{ role: 'reload' }, { role: 'toggleDevTools' }] : []),
    {
      label: 'Файл',
      submenu: [
        {
          label: 'Открыть…',
          accelerator: 'CmdOrCtrl+O',
          click: () => send('msc:menu', { action: 'open' }),
        },
        { type: 'separator' },
        { role: 'quit', label: 'Выход', accelerator: 'CmdOrCtrl+Q' },
      ],
    },
    {
      label: 'Воспроизведение',
      submenu: [
        { label: 'Играть / Пауза', accelerator: 'Space', click: () => send('msc:menu', { action: 'toggle' }) },
        { label: 'Стоп', accelerator: 'CmdOrCtrl+.', click: () => send('msc:menu', { action: 'stop' }) },
        { type: 'separator' },
        { label: 'Начало', accelerator: 'Home', click: () => send('msc:menu', { action: 'seekStart' }) },
        { label: 'Петля A-B', accelerator: 'L', click: () => send('msc:menu', { action: 'loop' }) },
        { type: 'separator' },
        { label: 'Сбросить скорость', accelerator: 'CmdOrCtrl+0', click: () => send('msc:menu', { action: 'resetSpeed' }) },
        { label: 'Сбросить тон', accelerator: 'CmdOrCtrl+Shift+0', click: () => send('msc:menu', { action: 'resetPitch' }) },
      ],
    },
    {
      label: 'Вид',
      submenu: [{ role: 'togglefullscreen' }, { role: 'zoomIn' }, { role: 'zoomOut' }, { type: 'separator' }, { role: 'resetZoom' }],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

/* ------------------------------------------------------------------ app */

// Один экземпляр: повторный запуск с файлом отдаёт файл уже открытому окну.
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  log('ДРУГОЙ ЭКЗЕМПЛЯР УЖЕ ЗАПУЩЕН — передаём ему файл и выходим');
  app.quit();
} else {
  log('получена single-instance блокировка');
  app.on('second-instance', (_e, argv) => {
    const file = audioFileFromArgv(argv);
    log('повторный запуск: экземпляр уже работает, поднимаем окно');
    if (win && !win.isDestroyed()) {
      // На Wayland focus() сам по себе не работает — помогает moveTop/show.
      if (win.isMinimized()) win.restore();
      if (!win.isVisible()) win.show();
      win.setSkipTaskbar(false);
      win.moveTop();
      win.focus();
      app.focus({ steal: true });
    } else {
      // Окно потерялось, но процесс жив — пересоздаём.
      pendingFile = file;
      createWindow();
    }
    if (file) loadIntoUi(file, { autoplay: autoplayForExternalFile() });
  });

  // macOS: двойной клик по файлу.
  app.on('open-file', (e, file) => {
    e.preventDefault();
    if (win) loadIntoUi(file, { autoplay: autoplayForExternalFile() });
    else {
      pendingFile = file;
      if (app.isReady()) createWindow();
    }
  });

  app.whenReady().then(() => {
    log('app ready');
    buildMenu();
    const silent = process.argv.includes('--silent');
    const deviceIdx = process.argv.indexOf('--device');
    const device = deviceIdx >= 0 ? process.argv[deviceIdx + 1] : null;
    pendingFile = audioFileFromArgv();
    log('файл из аргументов:', pendingFile || 'нет');
    startSidecar({ silent, device });
    createWindow();
    log('окно создано');

    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });

  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit();
  });

  app.on('before-quit', () => {
    quitting = true;
    if (sidecar) sidecar.stop();
  });
}
