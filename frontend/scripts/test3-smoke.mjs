/**
 * Test3 · 运行时 Mock 回归 / GUI 存活冒烟（s0402 三重闸第三关）。
 *
 * 口径：与既有「6 秒存活冒烟」等价 —— _electron.launch 直拉
 * node_modules/electron/dist/electron.exe（实际安装二进制），等待应用就绪，
 * 主窗口渲染成功后截图，再存活满 5 秒，正常关闭且进程退出即 PASS。
 *
 * 用法：node scripts/test3-smoke.mjs <evidence_output_dir>
 */
import { _electron } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND_ROOT = path.resolve(__dirname, '..');
const ELECTRON_EXE = path.join(FRONTEND_ROOT, 'node_modules', 'electron', 'dist', 'electron.exe');
const ALIVE_MS = 5_000;

const OUT_DIR = process.argv[2] || path.join(FRONTEND_ROOT, 'test-results', 'smoke');
fs.mkdirSync(OUT_DIR, { recursive: true });
const LOG_FILE = path.join(OUT_DIR, 'test3_smoke_runtime.log');

function log(line) {
  const ts = new Date().toISOString();
  const full = `[${ts}] ${line}`;
  console.log(full);
  fs.appendFileSync(LOG_FILE, full + '\n');
}

async function main() {
  log('=== Test3 运行时冒烟开始 ===');
  if (!fs.existsSync(ELECTRON_EXE)) {
    throw new Error(`electron.exe 不存在: ${ELECTRON_EXE}`);
  }
  log(`electron.exe 存在: ${ELECTRON_EXE}`);

  // 1. 拉起 Electron 应用（launch 内部等待应用 ready）
  const t0 = Date.now();
  const app = await _electron.launch({ executablePath: ELECTRON_EXE, args: ['.'], cwd: FRONTEND_ROOT });
  log(`STEP1 launch 完成（含 ready）耗时 ${Date.now() - t0}ms`);

  // 2. 主窗口就绪 + 渲染验证
  const win = await app.firstWindow();
  await win.waitForLoadState('domcontentloaded');
  await win.locator('#root').waitFor({ state: 'visible', timeout: 20_000 });
  const title = await win.title();
  log(`STEP2 主窗口就绪 title="${title}" #root 可见`);
  if (title !== 'CX-A 赛博伴侣') throw new Error(`title 不符: ${title}`);

  // 3. 截图留证
  const shotPath = path.join(OUT_DIR, 'test3_smoke_main_window.png');
  await win.screenshot({ path: shotPath });
  log(`STEP3 截图落盘: ${shotPath} (${fs.statSync(shotPath).size} bytes)`);

  // 4. 存活观察期：ALIVE_MS 后确认窗口仍在
  await new Promise((r) => setTimeout(r, ALIVE_MS));
  const aliveCount = await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length);
  log(`STEP4 存活 ${ALIVE_MS}ms 后主进程窗口数=${aliveCount}`);
  if (aliveCount < 1) throw new Error('存活期内窗口丢失');

  // 5. 正常关闭（before-quit 会顺带销毁桌宠悬浮窗钩子）
  const tClose = Date.now();
  await app.close();
  log(`STEP5 close 正常返回，耗时 ${Date.now() - tClose}ms`);

  const totalMs = Date.now() - t0;
  log(`RESULT=PASS 总耗时 ${totalMs}ms（拉起到关闭全链路正常退出）`);
  log('=== Test3 运行时冒烟结束 ===');
}

main().catch((err) => {
  log(`RESULT=FAIL ${err && err.stack ? err.stack : String(err)}`);
  process.exit(1);
});
