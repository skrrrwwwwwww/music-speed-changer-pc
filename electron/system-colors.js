'use strict';

/**
 * Системная палитра рабочего стола.
 *
 * Единого API «дай все цвета» в Linux нет, поэтому пробуем источники по
 * порядку: тема rice/DarkShadow (matugen → ~/.config/rice/palette.json),
 * палитры quickshell, KDE (kdeglobals), акцент GNOME, акцент
 * Windows/macOS. Первый валидный источник и выигрывает.
 *
 * Наружу отдаём нормализованную Material-палитру (surface, on_surface,
 * primary, tertiary, error…), а renderer уже раскладывает её по токенам
 * приложения и строит недостающую светлую/тёмную половину.
 */

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { nativeTheme, systemPreferences } = require('electron');

const HOME = os.homedir();
const XDG = process.env.XDG_CONFIG_HOME || path.join(HOME, '.config');

/* ------------------------------------------------------------- цветовой утиль */
function parseHex(value) {
  if (typeof value !== 'string') return null;
  let v = value.trim().toLowerCase().replace(/^#+/, '#');
  if (/^#[0-9a-f]{3}$/.test(v)) {
    v = `#${v[1]}${v[1]}${v[2]}${v[2]}${v[3]}${v[3]}`;
  }
  if (!/^#[0-9a-f]{6}$/.test(v)) return null;
  return { r: parseInt(v.slice(1, 3), 16), g: parseInt(v.slice(3, 5), 16), b: parseInt(v.slice(5, 7), 16) };
}

function hex(value) {
  const rgb = typeof value === 'string' ? parseHex(value) : value;
  if (!rgb) return null;
  const p = (n) => Math.max(0, Math.min(255, Math.round(n))).toString(16).padStart(2, '0');
  return `#${p(rgb.r)}${p(rgb.g)}${p(rgb.b)}`;
}

function mix(a, b, t) {
  const x = parseHex(a);
  const y = parseHex(b);
  if (!x || !y) return a;
  return hex({ r: x.r + (y.r - x.r) * t, g: x.g + (y.g - x.g) * t, b: x.b + (y.b - x.b) * t });
}

function luminance(color) {
  const c = parseHex(color);
  if (!c) return 0;
  const lin = (v) => {
    const s = v / 255;
    return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b);
}

function contrast(a, b) {
  const la = luminance(a);
  const lb = luminance(b);
  const hi = Math.max(la, lb);
  const lo = Math.min(la, lb);
  return (hi + 0.05) / (lo + 0.05);
}

function readableOn(color) {
  return contrast(color, '#ffffff') >= contrast(color, '#000000') ? '#ffffff' : '#000000';
}

/** Подмешивает чёрный/белый, пока контраст с фоном не станет достаточным. */
function ensureContrast(color, bg, ratio = 4.5) {
  if (contrast(color, bg) >= ratio) return color;
  const toward = contrast('#ffffff', bg) >= contrast('#000000', bg) ? '#ffffff' : '#000000';
  for (let i = 1; i <= 20; i += 1) {
    const c = mix(color, toward, i / 20);
    if (contrast(c, bg) >= ratio) return c;
  }
  return toward;
}

/* ---------------------------------------------------------- нормализация */
const ROLES = [
  'surface', 'surface_low', 'surface_container', 'surface_high', 'surface_highest',
  'on_surface', 'on_surface_variant', 'outline', 'outline_variant',
  'primary', 'on_primary', 'primary_container', 'on_primary_container',
  'secondary', 'secondary_container', 'tertiary', 'error',
];

function normalizePalette(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const surface = hex(raw.surface);
  const primary = hex(raw.primary) || hex(raw.accent);
  if (!surface || !primary) return null;

  const mode = luminance(surface) < 0.5 ? 'dark' : 'light';
  const onSurface =
    hex(raw.on_surface) || (mode === 'dark' ? ensureContrast('#e8ecf5', surface, 7) : ensureContrast('#1b2231', surface, 7));
  const p = {
    mode,
    surface,
    surface_low: hex(raw.surface_low) || mix(surface, onSurface, 0.03),
    surface_container: hex(raw.surface_container) || hex(raw.surface_low) || mix(surface, onSurface, 0.06),
    surface_high: hex(raw.surface_high) || mix(surface, onSurface, 0.1),
    surface_highest: hex(raw.surface_highest) || mix(surface, onSurface, 0.15),
    on_surface: onSurface,
    on_surface_variant: hex(raw.on_surface_variant) || mix(onSurface, surface, 0.35),
    outline: hex(raw.outline) || mix(onSurface, surface, 0.55),
    outline_variant: hex(raw.outline_variant) || mix(onSurface, surface, 0.75),
    primary,
    on_primary: hex(raw.on_primary) || readableOn(primary),
    primary_container: hex(raw.primary_container) || mix(primary, surface, 0.65),
    on_primary_container: hex(raw.on_primary_container) || ensureContrast(primary, mix(primary, surface, 0.65), 4.5),
    secondary: hex(raw.secondary) || mix(primary, onSurface, 0.3),
    secondary_container: hex(raw.secondary_container) || mix(primary, surface, 0.75),
    tertiary: hex(raw.tertiary) || mix(primary, onSurface, 0.2),
    error: hex(raw.error) || (mode === 'dark' ? '#ff6b6b' : '#d64545'),
  };
  for (const role of ROLES) {
    if (!hex(p[role])) return null;
  }
  return p;
}

function wrap(source, raw) {
  const palette = normalizePalette(raw);
  return palette ? { source, mode: palette.mode, palette } : null;
}

/* --------------------------------------------------------------- источники */
function readRice() {
  const file = path.join(XDG, 'rice', 'palette.json');
  try {
    return wrap('rice', JSON.parse(fs.readFileSync(file, 'utf-8')));
  } catch {
    return null;
  }
}

function readQuickshellQml() {
  // qml_color.json — Material-подобные имена другого варианта quickshell.
  const file = path.join(XDG, 'quickshell', 'qml_color.json');
  try {
    const j = JSON.parse(fs.readFileSync(file, 'utf-8'));
    return wrap('quickshell', {
      surface: j.windowBackground,
      surface_low: j.layerBackground1,
      surface_container: j.layerBackground2,
      surface_high: j.layerBackground3,
      on_surface: j.surfaceText || j.primaryText,
      on_surface_variant: j.secondaryText,
      outline: j.borderPrimary,
      outline_variant: j.borderSecondary,
      primary: j.accentPrimary,
      secondary: j.accentSecondary,
      tertiary: j.selectionBackground,
      error: j.error,
    });
  } catch {
    return null;
  }
}

function readQuickshellCache() {
  // ~/.cache/43pr/palette.json — плоская палитра темы quickshell.
  const file = path.join(HOME, '.cache', '43pr', 'palette.json');
  try {
    const j = JSON.parse(fs.readFileSync(file, 'utf-8'));
    return wrap('quickshell', {
      surface: j.bg,
      surface_low: j.bg_panel,
      surface_container: j.bg_card,
      surface_high: j.bg_hover || j.bg_card,
      surface_highest: j.bg_track,
      on_surface: j.fg,
      on_surface_variant: j.fg_dim || j.fg_muted,
      outline: j.border_strong || j.border,
      outline_variant: j.border,
      primary: j.accent,
      on_primary: j.cursor_text,
      secondary: j.highlight,
      tertiary: j.ok,
      error: j.danger,
    });
  } catch {
    return null;
  }
}

function colorValue(value) {
  if (typeof value !== 'string') return null;
  const v = value.trim();
  const m = /^(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})/.exec(v);
  if (m) return hex({ r: Number(m[1]), g: Number(m[2]), b: Number(m[3]) });
  return hex(v);
}

function parseIni(text) {
  const out = {};
  let section = '';
  for (const line of text.split(/\r?\n/)) {
    const s = line.match(/^\s*\[(.+)\]\s*$/);
    if (s) {
      section = s[1];
      out[section] = out[section] || {};
      continue;
    }
    const kv = line.match(/^\s*([^=;#]+?)\s*=\s*(.*)\s*$/);
    if (kv && section) out[section][kv[1]] = kv[2];
  }
  return out;
}

function readKde() {
  const file = path.join(XDG, 'kdeglobals');
  let ini;
  try {
    ini = parseIni(fs.readFileSync(file, 'utf-8'));
  } catch {
    return null;
  }
  const win = ini['Colors:Window'] || {};
  const view = ini['Colors:View'] || {};
  const button = ini['Colors:Button'] || {};
  const sel = ini['Colors:Selection'] || {};
  const general = ini.General || {};
  const negative = ini['Colors:Negative'] || {};

  const surface = colorValue(win.BackgroundNormal);
  const primary = colorValue(general.AccentColor) || colorValue(sel.BackgroundNormal);
  if (!surface || !primary) return null;

  return wrap('kde', {
    surface,
    surface_low: colorValue(view.BackgroundNormal),
    surface_container: colorValue(view.BackgroundNormal) || colorValue(button.BackgroundNormal),
    surface_high: colorValue(button.BackgroundNormal),
    on_surface: colorValue(win.ForegroundNormal) || colorValue(view.ForegroundNormal),
    on_surface_variant: colorValue(button.ForegroundNormal),
    outline: colorValue(win.ForegroundNormal),
    outline_variant: colorValue(win.BackgroundAlternate),
    primary,
    on_primary: colorValue(sel.ForegroundNormal),
    primary_container: colorValue(sel.BackgroundNormal),
    error: colorValue(negative.ForegroundNormal),
  });
}

const GNOME_ACCENTS = {
  blue: '#3584e4',
  teal: '#2190a4',
  green: '#3a944a',
  yellow: '#c88800',
  orange: '#ed5b00',
  red: '#e62d42',
  pink: '#d56199',
  purple: '#9141ac',
  slate: '#6f8396',
};

function gsettings(key) {
  try {
    const out = execFileSync('gsettings', ['get', 'org.gnome.desktop.interface', key], {
      encoding: 'utf-8',
      timeout: 1200,
      stdio: ['ignore', 'pipe', 'ignore'],
    }).trim();
    return out.replace(/^'|'$/g, '');
  } catch {
    return '';
  }
}

function readGnome() {
  const accentName = gsettings('accent-color');
  const accent = GNOME_ACCENTS[accentName];
  if (!accent) return null;
  const scheme = gsettings('color-scheme');
  const mode = scheme === 'prefer-light' ? 'light' : scheme === 'prefer-dark' ? 'dark' : nativeTheme.shouldUseDarkColors ? 'dark' : 'light';
  return wrap('gnome', standardPalette(accent, mode));
}

function standardPalette(accent, mode) {
  if (mode === 'dark') {
    return {
      surface: '#101219',
      surface_low: '#141824',
      surface_container: '#1b1f2b',
      surface_high: '#222736',
      surface_highest: '#2a3040',
      on_surface: '#e8ecf5',
      on_surface_variant: '#8b93a7',
      outline: '#6b7488',
      outline_variant: '#2a3040',
      primary: accent,
      on_primary: readableOn(accent),
      primary_container: mix(accent, '#101219', 0.65),
      on_primary_container: mix(accent, '#ffffff', 0.7),
      secondary: mix(accent, '#e8ecf5', 0.25),
      secondary_container: mix(accent, '#101219', 0.75),
      tertiary: mix(accent, '#ffffff', 0.3),
      error: '#ff6b6b',
    };
  }
  return {
    surface: '#f1f4f9',
    surface_low: '#f7f9fd',
    surface_container: '#ffffff',
    surface_high: '#eef2f8',
    surface_highest: '#e1e6ef',
    on_surface: '#1b2231',
    on_surface_variant: '#697285',
    outline: '#9aa4b5',
    outline_variant: '#d8dfea',
    primary: accent,
    on_primary: readableOn(accent),
    primary_container: mix(accent, '#ffffff', 0.78),
    on_primary_container: mix(accent, '#000000', 0.55),
    secondary: mix(accent, '#1b2231', 0.3),
    secondary_container: mix(accent, '#ffffff', 0.82),
    tertiary: mix(accent, '#000000', 0.15),
    error: '#d64545',
  };
}

function readSystemAccent() {
  let accent = '';
  try {
    accent = systemPreferences.getAccentColor() || '';
  } catch {
    return null;
  }
  // Windows отдаёт RGBA («aabbccdd»), macOS — тот же вид строки.
  const a = hex(`#${accent.slice(0, 6)}`);
  if (!a) return null;
  const mode = nativeTheme.shouldUseDarkColors ? 'dark' : 'light';
  return wrap('system', standardPalette(a, mode));
}

/* ------------------------------------------------------------------- API */
function readSystemPalette() {
  const sources = [readRice, readQuickshellQml, readQuickshellCache, readKde, readGnome, readSystemAccent];
  for (const read of sources) {
    try {
      const found = read();
      if (found) return found;
    } catch {
      /* источник сломан — пробуем следующий */
    }
  }
  return null;
}

/** Следит за файлами палитр; вызывает cb после изменений (с дебаунсом). */
function watchSystemPalette(cb) {
  const files = [
    path.join(XDG, 'rice', 'palette.json'),
    path.join(XDG, 'quickshell', 'qml_color.json'),
    path.join(HOME, '.cache', '43pr', 'palette.json'),
    path.join(XDG, 'kdeglobals'),
  ];
  let timer = null;
  const onChange = () => {
    clearTimeout(timer);
    timer = setTimeout(() => cb(), 250);
  };
  const watching = [];
  for (const file of files) {
    try {
      if (!fs.existsSync(file)) continue;
      fs.watchFile(file, { interval: 2000 }, onChange);
      watching.push(file);
    } catch {
      /* файл не поддался наблюдению */
    }
  }
  return () => {
    clearTimeout(timer);
    for (const file of watching) fs.unwatchFile(file, onChange);
  };
}

module.exports = { readSystemPalette, watchSystemPalette };
