/**
 * CX-A 赛博伴侣 — Electron 主进程
 *
 * 职责：
 *  - 创建主窗口（伴侣面 / 管理面主视图）
 *  - 预留创建「桌宠透明悬浮窗」的独立窗口函数（后续伴生宠物使用）
 *  - 注册 renderer 所需的最小 IPC（app:get-info、mode:switch）
 */
const { app, BrowserWindow, ipcMain } = require('electron');
const path = require('path');

const VITE_DEV_SERVER_URL = process.env.VITE_DEV_SERVER_URL;

/** 主窗口引用，避免被 GC 回收 */
let mainWindow = null;

/**
 * 通用窗口创建函数。
 * 支持透明悬浮窗参数（transparent/frame/resizable/alwaysOnTop/skipTaskbar）——
 * 为后续「桌宠悬浮窗」预留独立调用入口。
 * @param {BrowserWindowConstructorOptions} options
 * @returns {BrowserWindow}
 */
function createWindow(options = {}) {
  const {
    width = 1000,
    height = 720,
    transparent = false,
    resizable = true,
    ...rest
  } = options;

  const win = new BrowserWindow({
    width,
    height,
    transparent,
    resizable,
    backgroundColor: transparent ? '#00000000' : '#fafafc',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
    ...rest,
  });

  // renderer 加载：开发期走 Vite dev server，生产期走构建产物
  if (VITE_DEV_SERVER_URL) {
    win.loadURL(VITE_DEV_SERVER_URL);
  } else {
    win.loadFile(path.join(__dirname, '..', '..', 'dist', 'index.html'));
  }

  win.on('closed', () => {
    if (mainWindow === win) mainWindow = null;
  });

  return win;
}

/**
 * 桌宠透明悬浮窗（独立窗口，后续任务接入真实桌宠渲染逻辑）。
 * 由伴生宠物功能器调用，随主窗口手动创建/关闭。
 */
function createPetOverlayWindow() {
  return createWindow({
    transparent: true,
    frame: false,
    resizable: false,
    movable: true,
    minimizable: false,
    maximizable: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    hasShadow: false,
    width: 320,
    height: 360,
  });
}

// ---------- IPC：renderer 最小桥接 ----------

ipcMain.handle('app:get-info', () => ({
  name: 'CX-A 赛博伴侣',
  version: app.getVersion(),
  platform: process.platform,
}));

// 预留：主进程可主动通知 renderer 切换视图面（如托盘事件），renderer 通过 preload.onModeSwitch 订阅
ipcMain.handle('mode:switch', (_event, mode) => {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('mode:switch', mode);
    return true;
  }
  return false;
});

// ---------- 应用生命周期 ----------

app.whenReady().then(() => {
  mainWindow = createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      mainWindow = createWindow();
    }
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});