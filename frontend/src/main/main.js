/**
 * CX-A — Electron 主进程
 *
 * 职责：
 *  - 拉起 Python 后端服务（127.0.0.1:8600）并等待 /api/health 就绪（打包链路）
 *  - 创建主窗口（主视图）
 *  - 创建「桌宠透明悬浮窗」的独立窗口（createPetOverlayWindow / pet-overlay:open IPC）
 *  - 注册 renderer 所需的最小 IPC（app:get-info、pet-overlay:open/close、backend:token）
 *
 * 后端生命周期：
 *  - 开发态（VITE_DEV_SERVER_URL 存在）：spawn `python -m lite.server.api_server`，cwd=项目根；
 *  - 生产态（便携包）：spawn `<exe目录>/runtime/backend/backend.exe`（PyInstaller 产物）；
 *  - 后端启动失败仅告警不阻断窗口（前端侧有 Mock 降级路径）；
 *  - before-quit / process.exit 兜底回收子进程，防止残留进程占用 8600。
 *
 * 安全基线：contextIsolation:true、nodeIntegration:false、renderer sandbox 走
 * Electron 默认（开启）；单实例锁防双开令牌失配（D3）；WebContents 导航守卫
 * will-navigate 按 URL 解析精确放行（自身 dist 产物 / dev server origin，D2），
 * setWindowOpenHandler 一律拒绝 window.open。
 */
const { app, BrowserWindow, ipcMain, dialog, screen, Tray, Menu } = require('electron');
const { spawn } = require('child_process');
const crypto = require('crypto');
const http = require('http');
const path = require('path');
const { fileURLToPath } = require('url');

const VITE_DEV_SERVER_URL = process.env.VITE_DEV_SERVER_URL;

/**
 * 后端启动令牌（N1 防 CSRF）：每次应用启动随机生成，经 spawn env CXA_API_TOKEN
 * 注入后端进程，并经 IPC backend:token 交给 renderer 附带到 X-Client-Token 请求头。
 */
const BACKEND_TOKEN = crypto.randomBytes(24).toString('hex');

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

/** 系统托盘引用（20261004_模块0_托盘常驻：关窗不退出，常驻后台） */
let tray = null;
/** 是否处于退出流程：true 时主窗口 close 不再拦截（托盘「退出」/ app.quit 路径） */
let appIsQuitting = false;

/**
 * 应用图标路径（build/icon.ico，与前端 BrandMark 同一视觉）。
 * 开发态与打包态均为 `frontend(或 app 根)/build/icon.ico`（__dirname 上溯两级）；
 * 打包由 files 清单携带 build/icon.ico。缺失时返回 undefined（Electron 用默认图标）。
 * @returns {string | undefined}
 */
function appIconPath() {
  const iconPath = path.resolve(__dirname, '..', '..', 'build', 'icon.ico');
  try {
    // eslint-disable-next-line global-require
    require('fs').accessSync(iconPath);
    return iconPath;
  } catch {
    return undefined;
  }
}

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
    // N1：随机令牌经环境变量注入后端进程，后端据此开启 X-Client-Token 强制校验
    backendProcess = spawn(command, args, {
      cwd,
      windowsHide: true,
      env: { ...process.env, CXA_API_TOKEN: BACKEND_TOKEN },
    });
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
      // N1：健康探测附带令牌头——陌生进程占用 8600 时探测永不 200，超时告警更明确
      const req = http.get(
        url,
        { timeout: 2000, headers: { 'X-Client-Token': BACKEND_TOKEN } },
        (res) => {
          res.resume();
          if (res.statusCode === 200) {
            resolve(true);
          } else {
            schedule(); // 后端已监听但未就绪：继续轮询
          }
        }
      );
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

/**
 * 回收后端子进程及其后代进程树（幂等）。
 *
 * Windows：用 `taskkill /T /F` 结束整棵进程树——后端会拉起常驻
 * llama-server（记忆嵌入服务，约 600 MB 内存），Node 的 kill() 只终止直接
 * 子进程，会把它留成孤儿进程（持续占用内存）。
 * 非 Windows：回落 kill()。
 */
function stopBackend() {
  if (!backendProcess) return;
  try {
    if (process.platform === 'win32' && backendProcess.pid) {
      spawn('taskkill', ['/pid', String(backendProcess.pid), '/T', '/F'], {
        windowsHide: true,
      });
    } else {
      backendProcess.kill();
    }
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
 * 导航与弹窗守卫（D2 修复）：
 *  - 一律先 `new URL(url)` 解析，再做精确比对，杜绝前缀匹配绕过
 *    （如 `http://localhost:5173.evil.com/`）与任意本地文件放行；
 *  - file:// 仅放行应用自身 dist 目录的归一化路径前缀；
 *  - http(s) 仅在 VITE_DEV_SERVER_URL 存在（dev 态）时放行与其 origin 精确相等的
 *    来源——dev 放行统一收口到 dev server 存在性之下，生产态无该 env 即全部拒绝；
 *  - will-navigate 与 setWindowOpenHandler 共用同一判据（open-handler 仍拒绝一切新窗口）。
 */
function attachNavigationGuards(win) {
  // dev 放行来源：仅 dev server 本身的 origin（无 VITE_DEV_SERVER_URL 即为 null，永不放行）
  const devOrigin = (() => {
    if (!VITE_DEV_SERVER_URL) return null;
    try {
      return new URL(VITE_DEV_SERVER_URL).origin;
    } catch {
      return null;
    }
  })();

  // 应用自身 dist 产物目录（生产态 loadFile 目标）
  const distDir = path.resolve(__dirname, '..', '..', 'dist');
  const isOwnDistFile = (parsedUrl) => {
    if (parsedUrl.protocol !== 'file:') return false;
    try {
      const filePath = path.normalize(fileURLToPath(parsedUrl));
      const base = process.platform === 'win32' ? distDir.toLowerCase() : distDir;
      const target = process.platform === 'win32' ? filePath.toLowerCase() : filePath;
      // 前缀必须以路径分隔符收尾（或恰为目录本身），防 `dist-evil` 这类同前缀目录误放行
      return target === base || target.startsWith(`${base}${path.sep}`);
    } catch {
      return false;
    }
  };

  const isAllowedNavigation = (url) => {
    try {
      const parsed = new URL(url);
      if (parsed.protocol === 'file:') return isOwnDistFile(parsed);
      // http(s)/其它协议：仅放行 dev server origin 精确匹配（dev 态限定）
      return devOrigin !== null && parsed.origin === devOrigin;
    } catch {
      return false;
    }
  };

  win.webContents.on('will-navigate', (event, url) => {
    if (!isAllowedNavigation(url)) {
      event.preventDefault();
    }
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
    icon: appIconPath(),
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

  // 托盘常驻（20261004_模块0_托盘常驻）：主窗口点 X = 藏入托盘而非退出；
  // 真退出走托盘「退出」或 app.quit（before-quit 先置 appIsQuitting 放行 close）。
  win.on('close', (event) => {
    if (!appIsQuitting) {
      event.preventDefault();
      win.hide();
    }
  });

  win.on('closed', () => {
    if (mainWindow === win) mainWindow = null;
  });

  return win;
}

/**
 * 桌宠悬浮窗窗口尺寸（20261003_模块0_桌宠大小滑块化公式化）：
 * 与 renderer PetOverlay CSS 对应（滑块连续值，非查表）：
 *  - 画面（VRM 容器）宽 = 传入画布值，高 = round(画布值 × 1.05)（VrmAvatar 的取景比例）；
 *  - 窗口宽 = 画面宽 + 根容器左右 padding 14×2 = 画布宽 + 28；
 *  - 窗口高 = 画面高 + 上下 padding 28 + 底部菜单浮层呼吸余量。
 * @param {number} canvas 画布宽（px）
 * @returns {{width: number, height: number}}
 */
function petWindowSizeFor(canvas) {
  return {
    width: canvas + 28,
    height: Math.round(canvas * 1.05) + 28,
  };
}

/** 默认画布宽（建窗初始尺寸与 renderer 缺省 size 保持一致） */
const PET_OVERLAY_DEFAULT_SIZE = 286;

/**
 * 把窗口左上角坐标 clamp 到虚拟屏（全部显示器联合范围）内：
 * 至少保留 60px 可抓取区域在屏内，防止换档后窗口被移出屏幕过远找不回来。
 * 仅做简单 clamp，不做智能吸附。必须在 app ready 后调用（依赖 screen 模块）。
 */
function clampPointToVirtualScreen(x, y, width, height) {
  const keep = 60;
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const display of screen.getAllDisplays()) {
    const b = display.bounds;
    minX = Math.min(minX, b.x);
    minY = Math.min(minY, b.y);
    maxX = Math.max(maxX, b.x + b.width);
    maxY = Math.max(maxY, b.y + b.height);
  }
  return [
    Math.min(Math.max(x, minX - width + keep), maxX - keep),
    Math.min(Math.max(y, minY - height + keep), maxY - keep),
  ];
}

/**
 * 桌宠透明悬浮窗（中档预设尺寸，置顶、无边框、跳过任务栏）。
 * 通过 IPC『pet-overlay:open』由 renderer 触发创建；幂等——已存在则直接复用。
 * 初始尺寸取中档预设（renderer 缺省档位 286），非中档记忆档位由 renderer
 * 挂载后经『pet-overlay:resize』幂等校准，保证「初始 size 与窗口大小一致」。
 * 透明窗口先 show:false，ready-to-show 后再 show()，避免部分平台丢透明。
 */
function createPetOverlayWindow() {
  if (petWindow && !petWindow.isDestroyed()) {
    petWindow.show();
    return petWindow;
  }

  const preset = petWindowSizeFor(PET_OVERLAY_DEFAULT_SIZE);

  petWindow = new BrowserWindow({
    width: preset.width,
    height: preset.height,
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
    icon: appIconPath(),
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

// ---------- 托盘常驻（20261004_模块0_托盘常驻与悬浮窗按钮对齐CXO） ----------

/** 显示并聚焦主窗口（托盘菜单 / 悬浮窗「打开主窗口」共用）；主窗口已销毁时重建。 */
function showMainWindow() {
  if (mainWindow && !mainWindow.isDestroyed()) {
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
    return;
  }
  mainWindow = createWindow();
}

/** 按桌宠悬浮窗当前存在性重建托盘菜单（菜单项「桌宠：显示/隐藏」动态标注）。 */
function updateTrayMenu() {
  if (!tray) return;
  const petOpen = Boolean(petWindow && !petWindow.isDestroyed());
  const menu = Menu.buildFromTemplate([
    { label: '显示主窗口', click: () => showMainWindow() },
    {
      label: petOpen ? '隐藏桌宠' : '显示桌宠',
      click: () => {
        togglePetFromTray();
      },
    },
    { type: 'separator' },
    {
      label: '退出',
      click: () => {
        appIsQuitting = true;
        app.quit();
      },
    },
  ]);
  tray.setContextMenu(menu);
}

/** 托盘桌宠开关：主进程直控窗口显隐，同时通知渲染层同步 localStorage（单一真相源不变）。 */
function togglePetFromTray() {
  const open = !(petWindow && !petWindow.isDestroyed());
  if (open) {
    createPetOverlayWindow();
  } else {
    closePetOverlayWindow();
  }
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('pet:set-enabled', open);
  }
  updateTrayMenu();
}

/** 创建系统托盘（图标缺失时跳过并告警，不影响应用主流程）。 */
function createTray() {
  const iconPath = appIconPath();
  if (!iconPath) {
    console.warn('[tray] 未找到应用图标（build/icon.ico），托盘不创建');
    return;
  }
  tray = new Tray(iconPath);
  tray.setToolTip('CX-A');
  updateTrayMenu();
  // Windows 惯例：左键单击 = 呼出主窗口
  tray.on('click', () => showMainWindow());
}

// ---------- IPC：renderer 最小桥接 ----------

ipcMain.handle('app:get-info', () => ({
  name: 'CX-A',
  version: app.getVersion(),
  platform: process.platform,
}));

// 桌宠悬浮窗开关（对应 preload 的 openPetOverlay / closePetOverlay 白名单方法）
ipcMain.handle('pet-overlay:open', () => {
  createPetOverlayWindow();
  updateTrayMenu(); // 托盘「桌宠：显示/隐藏」标注跟随
  return true;
});
ipcMain.handle('pet-overlay:close', () => {
  closePetOverlayWindow();
  updateTrayMenu();
  return true;
});

// 显示主窗口（悬浮窗菜单「打开主窗口」通道；托盘左键走主进程内 showMainWindow）
ipcMain.handle('app:show-main', () => {
  showMainWindow();
  return true;
});

// 拖拽主进程化（20261003_模块0_桌宠拖拽主进程化与六档尺寸）：
// 渲染层只在 pointerdown/up 发 start/end，移动循环全在主进程——
// 16ms 轮询光标 → setPosition(光标 - 抓取偏移)，消除渲染层→IPC 往返延迟（拖拽跟手关键）。
// 窗口销毁循环自灭；drag-end 显式停表；循环内 clamp 虚拟屏防拖丢。
let petDragLoopTimer = null;

function stopPetDragLoop() {
  if (petDragLoopTimer !== null) {
    clearInterval(petDragLoopTimer);
    petDragLoopTimer = null;
  }
}

ipcMain.handle('pet-overlay:drag-start', () => {
  stopPetDragLoop();
  if (!petWindow || petWindow.isDestroyed()) return false;
  const cursor = screen.getCursorScreenPoint();
  const [winX, winY] = petWindow.getPosition();
  const offsetX = cursor.x - winX;
  const offsetY = cursor.y - winY;
  const step = () => {
    if (!petWindow || petWindow.isDestroyed()) {
      stopPetDragLoop();
      return;
    }
    const c = screen.getCursorScreenPoint();
    const [nx, ny] = clampPointToVirtualScreen(
      Math.round(c.x - offsetX),
      Math.round(c.y - offsetY),
      petWindow.getBounds().width,
      petWindow.getBounds().height,
    );
    petWindow.setPosition(nx, ny);
  };
  step();
  petDragLoopTimer = setInterval(step, 16);
  return true;
});

ipcMain.handle('pet-overlay:drag-end', () => {
  stopPetDragLoop();
  return true;
});

// 换档悬浮窗尺寸（20261003_模块0_桌宠大小滑块化）：滑块连续可调，
// 窗口尺寸按公式同步（画布宽 clamp [160,640]；宽=+28、高=round(×1.05)+28，
// 与 renderer VrmAvatar 取景比例和根容器 padding 对应）。
// 保持窗口中心不变，并 clamp 到虚拟屏范围内防止窗口被移出屏幕过远。
/**
 * 桌宠尺寸上下限按主屏分辨率推导（20261003_模块0_桌宠大小滑块与重出安装程序 追加回合）：
 *  - 上限：窗口高（画布×1.05 + 28）不超过主屏工作区高的 62%，再钳制 [320, 1440]；
 *  - 下限：随工作区高缩放（12%），钳制 [120, 280]（太小没法点、太大失去"宠物"感）。
 * renderer 滑块与窗口 resize 都以此为准（单一真相在主进程）。
 * @param {number} workAreaHeight 主屏工作区高（px）
 * @returns {{min: number, max: number}}
 */
function petSizeBoundsFor(workAreaHeight) {
  const max = Math.max(320, Math.min(1440, Math.round((workAreaHeight * 0.62) / 1.05) - 28));
  const min = Math.max(120, Math.min(280, Math.round(workAreaHeight * 0.12)));
  return { min, max };
}

// 注意：Electron Display 对象没有 workAreaHeight 快捷属性，必须取 workArea.height
// （写成 display.workAreaHeight 会得到 undefined → 全链 NaN → 画布归零）。
ipcMain.handle('pet-overlay:size-bounds', () =>
  petSizeBoundsFor(screen.getPrimaryDisplay().workArea.height),
);

ipcMain.handle('pet-overlay:resize', (_event, size) => {
  if (!petWindow || petWindow.isDestroyed()) return true;
  // typeof gate 之后按数字校验：非有限值直接忽略，防止 setPosition 写入 NaN
  if (typeof size !== 'number' || !Number.isFinite(size)) return false;
  const { min, max } = petSizeBoundsFor(screen.getPrimaryDisplay().workArea.height);
  const canvas = Math.max(min, Math.min(max, Math.round(size)));
  const width = canvas + 28;
  const height = Math.round(canvas * 1.05) + 28;
  const oldBounds = petWindow.getBounds();
  const centerX = oldBounds.x + oldBounds.width / 2;
  const centerY = oldBounds.y + oldBounds.height / 2;
  const [nx, ny] = clampPointToVirtualScreen(
    Math.round(centerX - width / 2),
    Math.round(centerY - height / 2),
    width,
    height,
  );
  petWindow.setBounds({ x: nx, y: ny, width, height });
  return true;
});

// 系统目录选择器（音色文件夹导入通道）：取消或未选返回 null，否则返回绝对路径
ipcMain.handle('voice:pick-folder', async () => {
  const result = await dialog.showOpenDialog({
    title: '选择音色文件夹',
    properties: ['openDirectory'],
  });
  if (result.canceled || result.filePaths.length === 0) return null;
  return result.filePaths[0];
});

// 系统文件选择器（桌宠 VRM 模型更换通道）：仅放行 .vrm 后缀；取消或未选返回 null
ipcMain.handle('pick-vrm-file', async () => {
  const result = await dialog.showOpenDialog({
    title: '选择 VRM 模型文件',
    properties: ['openFile'],
    filters: [{ name: 'VRM 模型', extensions: ['vrm'] }],
  });
  if (result.canceled || result.filePaths.length === 0) return null;
  return result.filePaths[0];
});

// 后端启动令牌（N1）：renderer 经 preload 白名单方法获取后，附带在 API 请求头
ipcMain.handle('backend:token', () => BACKEND_TOKEN);

// ---------- 应用生命周期 ----------

// 单实例锁（D3 修复）：双开会导致第二实例生成不同的 CXA_API_TOKEN，
// 对后端全部 API 401 静默降级——未获锁实例直接退出；获锁实例在
// second-instance 事件中聚焦既有主窗口。e2e 冒烟为串行 launch+close，不受影响。
const gotSingleInstanceLock = app.requestSingleInstanceLock();
if (!gotSingleInstanceLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });
}

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
  createTray();

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
  appIsQuitting = true; // 放行主窗口 close（藏托盘拦截只在非退出流程生效）
  closePetOverlayWindow();
  stopBackend();
});

// 兜底：任何路径退出都尝试回收后端，防止 8600 被残留进程占用
process.on('exit', () => {
  stopBackend();
});
