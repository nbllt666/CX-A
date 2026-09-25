/**
 * check-vrm-render.js — 桌宠 VRM 渲染自查脚本（`npm run check:vrm`）。
 *
 * 用途：把「我看了一眼觉得对了」变成可复跑、可留证的检查。它用真实 Electron
 * 加载真实构建产物 `dist/pet-overlay.html`，等待 `data-vrm-state` 落定，然后：
 *   1. 打印状态与失败原因、canvas 尺寸、WebGL 渲染器名（显卡信息）；
 *   2. 截图落盘，供目视核对亮度 / 站姿 / 取景（E2E 断言替代不了「好不好看」）；
 *   3. 以退出码表达结论：ready → 0，unsupported / 超时 → 1。
 *
 * 与 e2e/pet-vrm.e2e.spec.ts 的分工：E2E 是阻断式断言（必须通过），
 * 本脚本是**目视迭代工具**（改光照 / 姿势参数后跑一次看截图，快且不阻塞）。
 *
 * 用法：
 *   npm run check:vrm                       # 自动拉起项目后端（先 npm run build）
 *   npm run check:vrm -- --no-backend       # 不拉后端：验证「暂时显示不了」提示路径
 *   npm run check:vrm -- --bg "#f2f2f7"     # 截图前铺一层背景色，便于判断是否偏暗
 *   npm run check:vrm -- --out <目录>        # 指定截图目录（默认 .trae/documents/test_reports/vrm_visual_<时间戳>）
 *   npm run check:vrm -- --token <令牌>      # 后端开启令牌校验时使用（注入 CXA_API_TOKEN）
 *   npm run check:vrm -- --timeout 90000    # 等待上限（默认 60s）
 */

'use strict';

const { app, BrowserWindow, ipcMain } = require('electron');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');

const FRONTEND_ROOT = path.resolve(__dirname, '..');
const PROJECT_ROOT = path.resolve(FRONTEND_ROOT, '..');
const PRELOAD = path.join(FRONTEND_ROOT, 'src', 'main', 'preload.js');
const OVERLAY_HTML = path.join(FRONTEND_ROOT, 'dist', 'pet-overlay.html');
const API_HEALTH = 'http://127.0.0.1:8600/api/health';

/** 解析命令行参数（`electron scripts/check-vrm-render.js -- --xxx`）。 */
function parseArgs(argv) {
  const opts = { backend: true, bg: '', out: '', token: '', timeout: 60000 };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--no-backend') opts.backend = false;
    else if (arg === '--bg') opts.bg = argv[++i] ?? '';
    else if (arg === '--out') opts.out = argv[++i] ?? '';
    else if (arg === '--token') opts.token = argv[++i] ?? '';
    else if (arg === '--timeout') opts.timeout = Number(argv[++i]) || opts.timeout;
  }
  return opts;
}

const OPTS = parseArgs(process.argv.slice(2));

/** 默认截图目录：与既有视觉迭代证据同处 `.trae/documents/test_reports/`。 */
function defaultOutDir() {
  const stamp = new Date()
    .toISOString()
    .replace(/[-:T]/g, '')
    .slice(0, 14);
  return path.join(PROJECT_ROOT, '.trae', 'documents', 'test_reports', `vrm_visual_${stamp}`);
}

function log(msg) {
  console.log(`[check-vrm] ${msg}`);
}

/** 后端健康探测（true = 200）。 */
function probeHealth() {
  return new Promise((resolve) => {
    const req = http.get(API_HEALTH, { timeout: 1500 }, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.on('error', () => resolve(false));
    req.on('timeout', () => {
      req.destroy();
      resolve(false);
    });
  });
}

let backend = null;

/** 拉起项目后端（与 Electron 主进程同一命令），返回子进程。 */
async function startBackend() {
  const env = { ...process.env };
  if (OPTS.token) env.CXA_API_TOKEN = OPTS.token;
  backend = spawn(
    process.platform === 'win32' ? 'python' : 'python3',
    ['-m', 'lite.server.api_server'],
    { cwd: PROJECT_ROOT, windowsHide: true, env },
  );
  backend.stdout?.on('data', (d) => log(`backend: ${String(d).trimEnd()}`));
  backend.stderr?.on('data', (d) => log(`backend(err): ${String(d).trimEnd()}`));

  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    if (await probeHealth()) {
      log('后端已就绪（127.0.0.1:8600）');
      return;
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  log('警告：后端 30s 内未就绪，继续执行（将走「显示不了」路径）');
}

/** 结束后端进程树（幂等）。 */
function stopBackend() {
  if (!backend?.pid) return;
  if (process.platform === 'win32') {
    spawn('taskkill', ['/pid', String(backend.pid), '/T', '/F'], { windowsHide: true });
  } else {
    backend.kill('SIGTERM');
  }
  backend = null;
}

/** 轮询页面内 data-vrm-state，直到 ready / unsupported / 超时。 */
async function waitForVrmState(win, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let last = 'unknown';
  while (Date.now() < deadline) {
    last = await win.webContents.executeJavaScript(
      `document.querySelector('[data-vrm-state]')?.getAttribute('data-vrm-state') || 'absent'`,
    );
    if (last === 'ready' || last === 'unsupported') return last;
    await new Promise((r) => setTimeout(r, 500));
  }
  return `${last}（超时）`;
}

/** 采集诊断信息：原因文案、canvas 尺寸、WebGL 渲染器名。 */
async function collectDiagnostics(win) {
  return win.webContents.executeJavaScript(`(() => {
    const root = document.querySelector('[data-vrm-state]');
    const canvas = document.querySelector('.cx-vrm-host canvas');
    const desc = document.querySelector('.cx-vrm-unsupported-desc');
    let gpu = 'n/a';
    try {
      const probe = document.createElement('canvas');
      const gl = probe.getContext('webgl');
      const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
      if (ext) gpu = gl.getParameter(ext.UNMASKED_RENDERER_WEBGL);
      else if (gl) gpu = gl.getParameter(gl.RENDERER);
    } catch (e) { gpu = 'probe-error: ' + e.message; }
    return {
      state: root ? root.getAttribute('data-vrm-state') : 'absent',
      reason: desc ? desc.textContent : null,
      dpr: window.devicePixelRatio,
      canvasCss: canvas
        ? { w: Math.round(canvas.getBoundingClientRect().width), h: Math.round(canvas.getBoundingClientRect().height) }
        : null,
      canvasBacking: canvas ? { w: canvas.width, h: canvas.height } : null,
      gpu,
    };
  })()`);
}

(async () => {
  if (!fs.existsSync(OVERLAY_HTML)) {
    log(`找不到构建产物：${OVERLAY_HTML}；请先执行 npm run build`);
    process.exit(2);
  }
  if (OPTS.backend) {
    if (await probeHealth()) {
      log('8600 已有后端在跑，直接复用（不重复拉起）');
    } else {
      await startBackend();
    }
  } else {
    log('--no-backend：不拉后端，验证「暂时显示不了 3D 桌宠」提示路径');
  }

  await app.whenReady();

  // 最小 IPC 桥：与 main.js 同名的通道，令牌回传空串（后端开放模式）或命令行给定值
  ipcMain.handle('backend:token', () => OPTS.token || '');
  ipcMain.handle('app:get-info', () => ({ name: 'check-vrm', version: '0.0.0', platform: process.platform }));
  ipcMain.handle('pet-overlay:open', () => true);
  ipcMain.handle('pet-overlay:close', () => {
    app.exit(0);
    return true;
  });

  const win = new BrowserWindow({
    width: 320,
    height: 360,
    show: true,
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    webPreferences: { preload: PRELOAD, contextIsolation: true, nodeIntegration: false },
  });
  await win.loadFile(OVERLAY_HTML);

  const state = await waitForVrmState(win, OPTS.timeout);
  // 截图前可选铺背景色：透明 PNG 在不同底色上观感差很多，判断「偏暗」需要一个已知底色
  if (OPTS.bg) {
    await win.webContents.executeJavaScript(
      `document.body.style.background = ${JSON.stringify(OPTS.bg)};`,
    );
    await new Promise((r) => setTimeout(r, 300));
  }

  const outDir = OPTS.out ? path.resolve(OPTS.out) : defaultOutDir();
  fs.mkdirSync(outDir, { recursive: true });
  const shotPath = path.join(outDir, `check-${String(state).replace(/[（）]/g, '')}.png`);
  const image = await win.webContents.capturePage();
  fs.writeFileSync(shotPath, image.toPNG());

  const diagnostics = await collectDiagnostics(win);
  const report = {
    timestamp: new Date().toISOString(),
    state,
    screenshot: shotPath,
    backend: OPTS.backend ? (OPTS.token ? 'token 模式' : '项目后端（开放模式）') : '未拉起（--no-backend）',
    diagnostics,
    verdict: state === 'ready' ? 'ready：3D 模型已渲染，请看截图核对亮度 / 站姿 / 取景' : '未就绪：见 diagnostics.reason',
  };
  console.log(JSON.stringify(report, null, 2));

  const code = state === 'ready' ? 0 : 1;
  stopBackend();
  win.destroy();
  app.exit(code);
})().catch((err) => {
  console.error(`[check-vrm] 执行失败：${err && err.stack ? err.stack : err}`);
  stopBackend();
  app.exit(2);
});