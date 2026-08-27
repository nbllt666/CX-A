/**
 * CX-A 赛博伴侣 — Electron 主进程
 *
 * 职责：
 *  - 创建主窗口（伴侣面 / 管理面主视图）
 *  - 创建「桌宠透明悬浮窗」的独立窗口（createPetOverlayWindow / pet-overlay:open IPC）
 *  - 注册 renderer 所需的最小 IPC（app:get-info、mode:switch、pet-overlay:open/close）
 *
 * 安全基线：contextIsolation:true、nodeIntegration:false、renderer sandbox 走
 * Electron 默认（开启）；WebContents 导航守卫 will-navigate 只放行本地来源，
 * setWindowOpenHandler 一律拒绝 window.open。
 */
const { app, BrowserWindow, ipcMain } = require('electron');
const path = require('path');

const VITE_DEV_SERVER_URL = process.env.VITE_DEV_SERVER_URL;

/** 主窗口引用，避免被 GC 回收 */
let mainWindow = null;
/** 桌宠悬浮窗引用；closed 时置 null，全程最多存在一个实例 */
let petWindow = null;

/**
 * renderer 入口加载：开发期走 Vite dev server，生产期走构建产物。
 * @param {Electron.BrowserWindow} win 目标窗口
 * @param {string} entryHtml 构建产物入口 html 文件名（dist/ 下）
 */
function loadRenderer(win, entryHtml) {
  if (VITE_DEV_SERVER_URL) {
    win.loadURL(`${VITE_DEV_SERVER_URL}/${entryHtml}`);
  } else {
    win.loadFile(path.join(__dirname, '..', '..', 'dist', entryHtml));
  }
}

/**
 * 导航与弹窗守卫：
 *  - will-navigate 仅放行 file:// 与本地 dev server（localhost:5173），其余一律阻止；
 *  - setWindowOpenHandler 拒绝一切 window.open 新窗口请求。
 */
function attachNavigationGuards(win) {
  const isLocalDev = (url) => {
    if (!VITE_DEV_SERVER_URL) return false;
    return url === VITE_DEV_SERVER_URL || url.startsWith(`${VITE_DEV_SERVER_URL}/`);
  };
  const isLocalhostDevPort = (url) =>
    url.startsWith('http://localhost:5173') || url.startsWith('http://127.0.0.1:5173');

  win.webContents.on('will-navigate', (event, url) => {
    if (url.startsWith('file://') || isLocalDev(url) || isLocalhostDevPort(url)) {
      return;
    }
    event.preventDefault();
  });
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
}

/**
 * 主窗口创建函数。
 * @param {Partial<Electron.BrowserWindowConstructorOptions>} options
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
    },
    ...rest,
  });

  loadRenderer(win, 'index.html');
  attachNavigationGuards(win);

  win.on('closed', () => {
    if (mainWindow === win) mainWindow = null;
  });

  return win;
}

/**
 * 桌宠透明悬浮窗（320×360，置顶、无边框、跳过任务栏）。
 * 通过 IPC『pet-overlay:open』由 renderer 触发创建；幂等——已存在则直接复用。
 * 透明窗口先 show:false，ready-to-show 后再 show()，避免部分平台丢透明。
 */
function createPetOverlayWindow() {
  if (petWindow && !petWindow.isDestroyed()) {
    petWindow.show();
    return petWindow;
  }

  petWindow = new BrowserWindow({
    width: 320,
    height: 360,
    show: false,
    transparent: true,
    frame: false,
    resizable: false,
    movable: true,
    minimizable: false,
    maximizable: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    hasShadow: false,
    backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  loadRenderer(petWindow, 'pet-overlay.html');
  attachNavigationGuards(petWindow);

  petWindow.once('ready-to-show', () => {
    if (petWindow && !petWindow.isDestroyed()) petWindow.show();
  });

  petWindow.on('closed', () => {
    petWindow = null;
  });

  return petWindow;
}

/** 关闭桌宠悬浮窗（不存在时静默返回 true）。 */
function closePetOverlayWindow() {
  if (petWindow && !petWindow.isDestroyed()) {
    petWindow.close();
  }
  petWindow = null;
  return true;
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

// 桌宠悬浮窗开关（对应 preload 的 openPetOverlay / closePetOverlay 白名单方法）
ipcMain.handle('pet-overlay:open', () => {
  createPetOverlayWindow();
  return true;
});
ipcMain.handle('pet-overlay:close', () => closePetOverlayWindow());

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

// 应用退出前销毁桌宠悬浮窗，防止残留孤儿窗口阻塞退出或滞留桌面
app.on('before-quit', () => {
  closePetOverlayWindow();
});
