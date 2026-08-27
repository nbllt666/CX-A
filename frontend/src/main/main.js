/**
 * CX-A 赛博伴侣 — Electron 主进程
 *
 * 职责：
 *  - 拉起 Python 后端服务（127.0.0.1:8600）并等待 /api/health 就绪（打包链路）
 *  - 创建主窗口（伴侣面 / 管理面主视图）
 *  - 创建「桌宠透明悬浮窗」的独立窗口（createPetOverlayWindow / pet-overlay:open IPC）
 *  - 注册 renderer 所需的最小 IPC（app:get-info、mode:switch、pet-overlay:open/close）
 *
 * 后端生命周期：
 *  - 开发态（VITE_DEV_SERVER_URL 存在）：spawn `python -m lite.server.api_server`，cwd=项目根；
 *  - 生产态（便携包）：spawn `<exe目录>/runtime/backend/backend.exe`（PyInstaller 产物）；
 *  - 后端启动失败仅告警不阻断窗口（前端侧有 Mock 降级路径）；
 *  - before-quit / process.exit 兜底回收子进程，防止残留进程占用 8600。
 *
 * 安全基线：contextIsolation:true、nodeIntegration:false、renderer sandbox 走
 * Electron 默认（开启）；WebContents 导航守卫 will-navigate 只放行本地来源，
 * setWindowOpenHandler 一律拒绝 window.open。
 */
const { app, BrowserWindow, ipcMain } = require('electron');
const { spawn } = require('child_process');
const http = require('http');
const path = require('path');

const VITE_DEV_SERVER_URL = process.env.VITE_DEV_SERVER_URL;

/** 后端服务固定地址（与 lite/server/api_server.py、renderer api.ts 约定一致） */
const BACKEND_HOST = '127.0.0.1';
const BACKEND_PORT = 8600;
/** 健康等待参数：500ms 轮询，30s 上限 */
const HEALTH_INTERVAL_MS = 500;
const HEALTH_TIMEOUT_MS = 30000;

/** 主窗口引用，避免被 GC 回收 */
let mainWindow = null;
/** 桌宠悬浮窗引用；closed 时置 null，全程最多存在一个实例 */
let petWindow = null;
/** 后端子进程引用（仅主进程持有，退出时回收） */
let backendProcess = null;

/**
 * 推导后端启动命令（命令 + 参数 + cwd）。
 * @returns {{command: string, args: string[], cwd: string}}
 */
function resolveBackendCommand() {
  if (VITE_DEV_SERVER_URL) {
    // 开发态：项目根 = frontend/src/main 上溯三级
    const projectRoot = path.join(__dirname, '..', '..', '..');
    return {
      command: process.platform === 'win32' ? 'python' : 'python3',
      args: ['-m', 'lite.server.api_server'],
      cwd: projectRoot,
    };
  }
  // 生产态：PyInstaller 后端位于 <exe目录>/runtime/backend/backend.exe
  const backendExe = path.join(
    path.dirname(app.getPath('exe')), 'runtime', 'backend',
    process.platform === 'win32' ? 'backend.exe' : 'backend'
  );
  return { command: backendExe, args: [], cwd: path.dirname(backendExe) };
}

/**
 * 拉起后端子进程；stdout/stderr 转发至主进程日志（带 [backend] 前缀）。
 * 失败只告警不抛错——后端缺席时前端走降级路径。
 */
function startBackend() {
  if (backendProcess) return;
  const { command, args, cwd } = resolveBackendCommand();
  try {
    backendProcess = spawn(command, args, { cwd, windowsHide: true });
  } catch (err) {
    console.warn(`[backend] 启动失败（${err.message}）；前端将走降级路径`);
    backendProcess = null;
    return;
  }
  console.log(`[backend] 已启动：${command} ${args.join(' ')} (cwd=${cwd})`);
  backendProcess.stdout?.on('data', (d) => console.log(`[backend] ${String(d).trimEnd()}`));
  backendProcess.stderr?.on('data', (d) => console.warn(`[backend] ${String(d).trimEnd()}`));
  backendProcess.on('error', (err) => {
    console.warn(`[backend] 进程错误：${err.message}`);
  });
  backendProcess.on('exit', (code) => {
    console.warn(`[backend] 进程退出：code=${code}`);
    backendProcess = null;
  });
}

/**
 * 轮询 /api/health 直到就绪或超时。
 * @returns {Promise<boolean>} 就绪返回 true；超时返回 false（不抛错）
 */
function waitForBackendHealth() {
  const url = `http://${BACKEND_HOST}:${BACKEND_PORT}/api/health`;
  const startedAt = Date.now();
  return new Promise((resolve) => {
    const probe = () => {
      const req = http.get(url, { timeout: 2000 }, (res) => {
        res.resume();
        if (res.statusCode === 200) {
          resolve(true);
        } else {
          schedule(); // 后端已监听但未就绪：继续轮询
        }
      });
      req.on('error', () => schedule());
      req.on('timeout', () => {
        req.destroy();
        schedule();
      });
    };
    const schedule = () => {
      if (Date.now() - startedAt >= HEALTH_TIMEOUT_MS) {
        resolve(false);
        return;
      }
      setTimeout(probe, HEALTH_INTERVAL_MS);
    };
    probe();
  });
}

/** 回收后端子进程（幂等）。 */
function stopBackend() {
  if (!backendProcess) return;
  try {
    backendProcess.kill();
  } catch {
    /* 进程已退出时忽略 */
  }
  backendProcess = null;
}

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
  // 拉起后端：不 await——窗口先开，健康探测并行进行，超时仅告警
  startBackend();
  waitForBackendHealth().then((ready) => {
    if (ready) {
      console.log('[backend] /api/health 就绪（127.0.0.1:8600）');
    } else {
      console.warn('[backend] 健康等待超时：后端可能未就绪，前端走降级路径');
    }
  });

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

// 应用退出前销毁桌宠悬浮窗并回收后端子进程，防止残留孤儿窗口/进程
app.on('before-quit', () => {
  closePetOverlayWindow();
  stopBackend();
});

// 兜底：任何路径退出都尝试回收后端，防止 8600 被残留进程占用
process.on('exit', () => {
  stopBackend();
});
