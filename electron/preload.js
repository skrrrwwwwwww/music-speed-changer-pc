'use strict';

const { contextBridge, ipcRenderer, webUtils } = require('electron');

/**
 * Мост renderer -> main. Никаких прямых вызовов node в странице.
 */
contextBridge.exposeInMainWorld('msc', {
  /** Команда аудиодвижку. */
  cmd: (cmd, args) => ipcRenderer.invoke('msc:cmd', cmd, args),

  /** Диалог открытия файла. */
  openFile: () => ipcRenderer.invoke('msc:openFile'),

  /** Загрузка файла по пути (в т.ч. из drag&drop). */
  loadPath: (path) => ipcRenderer.invoke('msc:loadPath', path),

  /** Диалог сохранения. */
  saveFile: (defaultName) => ipcRenderer.invoke('msc:saveFile', { defaultName }),

  /** Устройства вывода и переключение на другое. */
  devices: () => ipcRenderer.invoke('msc:devices'),
  selectDevice: (name) => ipcRenderer.invoke('msc:selectDevice', { name }),

  /** Выбор папки с музыкой. */
  pickDirectory: () => ipcRenderer.invoke('msc:pickDirectory'),

  /** Настройки приложения (автозапуск из проводника и т.п.). */
  getSettings: () => ipcRenderer.invoke('msc:getSettings'),
  setSetting: (key, value) => ipcRenderer.invoke('msc:setSetting', key, value),

  /** Абсолютный путь файла из DataTransfer (drop). */
  pathForFile: (file) => {
    try {
      return webUtils.getPathForFile(file);
    } catch {
      return file?.path || null;
    }
  },

  appInfo: () => ipcRenderer.invoke('msc:appInfo'),
  restartEngine: () => ipcRenderer.invoke('msc:restartEngine'),

  /** Подписка на события движка. Возвращает функцию отписки. */
  onEvent: (cb) => {
    const handler = (_e, payload) => cb(payload);
    ipcRenderer.on('msc:event', handler);
    return () => ipcRenderer.removeListener('msc:event', handler);
  },

  /** Лог из python-процесса. */
  onLog: (cb) => {
    const handler = (_e, line) => cb(line);
    ipcRenderer.on('msc:log', handler);
    return () => ipcRenderer.removeListener('msc:log', handler);
  },

  /** Команды из меню приложения. */
  onMenu: (cb) => {
    const handler = (_e, payload) => cb(payload);
    ipcRenderer.on('msc:menu', handler);
    return () => ipcRenderer.removeListener('msc:menu', handler);
  },
});
