import { test, expect, _electron, type ElectronApplication, type Page } from '@playwright/test';
import { spawn, type ChildProcess } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';

/**
 * VRM 桌宠渲染链路 E2E（s0402 三重闸第二关的专项补充）。
 *
 * 与 app.e2e.spec.ts 的分工：后者验证「窗口层」行为（悬浮窗开/关、窗口数），
 * 本文件验证「画面层」行为——悬浮窗里**到底渲染出了什么**：
 *
 * 路径 A（后端可用）→ 真·3D 模型就位：data-vrm-state="ready" + canvas 尺寸正确；
 *   路径 B（后端不可用）→ 明确的中文「暂时显示不了 3D 桌宠」提示，
 *                        且页面**不出现任何替代形象**（不留空 canvas）。
 *
 * 为什么要写死这两条路径：用户三次反馈「这根本不是 VRM」。旧版失败时回落 CSS 卡通，
 * 视觉上无法与「根本没做 VRM」区分；本用例把「失败必须明说、不许伪装」钉成可复跑断言。
 * （该 CSS 卡通形象组件已于 2026-09-25 按设计对齐要求整体删除，回归防线由"不得出现
 * canvas / 必须出现提示卡"承接。）
 *
 * 运行前提：
 *   - 已完成 `npm run build`（Electron 生产模式加载 dist/*.html）；
 *   - 本机可用 `python -m lite.server.api_server`（路径 A 由本用例自行起停后端）；
 *   - 路径 A 需要开发态模型 `<项目根>/data/pet/cx-open.vrm`（data/ 不随仓库分发，缺失则跳过）。
 *
 * 端口纪律：路径 A 用 8600，afterAll 必须停后端并等到端口真正释放，否则会污染
 * 其余「后端不可用」用例（app.e2e.spec.ts 的五个场景均以后端未起为前提）。
 */

const FRONTEND_ROOT = path.resolve(__dirname, '..');
const PROJECT_ROOT = path.resolve(FRONTEND_ROOT, '..');
const ELECTRON_EXE = path.join(FRONTEND_ROOT, 'node_modules', 'electron', 'dist', 'electron.exe');
const MODEL_PATH = path.join(PROJECT_ROOT, 'data', 'pet', 'cx-open.vrm');
const API_BASE = 'http://127.0.0.1:8600/api';

/** 后端健康探测：true = /api/health 返回 200。 */
function probeHealth(): Promise<boolean> {
  return new Promise((resolve) => {
    const req = http.get(`${API_BASE}/health`, { timeout: 1500 }, (res) => {
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

/** 轮询等待条件成立，超时抛错（带可读 label）。 */
async function waitUntil(
  predicate: () => Promise<boolean>,
  timeoutMs: number,
  label: string,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await new Promise((r) => setTimeout(r, 400));
  }
  throw new Error(`等待超时（${timeoutMs}ms）：${label}`);
}

let backend: ChildProcess | null = null;
let backendExitCode: number | null = null;

/**
 * 拉起真实后端（本用例自持）。
 *
 * 为什么用临时 config 而不是项目根 config.json：向导门控读 `setup.completed`，
 * 开发态根 config 里没有 setup 段 → 后端一就绪应用就会弹首启向导、不挂载桌宠生命周期，
 * 路径 A 无从断言。直接改根 config 会污染用户数据，故复制一份到临时目录并置
 * `setup.completed = true`，让应用直接进主界面。模型路径走 app_root()，与 config 位置无关。
 */
async function startBackend(): Promise<void> {
  const rootConfig = path.join(PROJECT_ROOT, 'config.json');
  const base = fs.existsSync(rootConfig)
    ? (JSON.parse(fs.readFileSync(rootConfig, 'utf-8')) as Record<string, unknown>)
    : {};
  base.setup = { ...((base.setup as Record<string, unknown>) ?? {}), completed: true };
  // 嵌入禁止降级（20261005）：后端无嵌入模型会启动失败——显式指向 bundled 真
  // 嵌入模型（装机同款文件），e2e 走真实嵌入链路（llama-server 本机就绪）
  base.embedding = {
    ...((base.embedding as Record<string, unknown>) ?? {}),
    model_path: path.join(
      PROJECT_ROOT, 'installer', 'bundled', 'embedding_model', 'Qwen3-Embedding-0.6B-Q8_0.gguf',
    ),
  };
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'cxa-pet-e2e-'));
  const configPath = path.join(tmpDir, 'config.json');
  fs.writeFileSync(configPath, JSON.stringify(base, null, 2), 'utf-8');

  backendExitCode = null;
  backend = spawn(
    process.platform === 'win32' ? 'python' : 'python3',
    ['-m', 'lite.server.api_server', '--config', configPath],
    { cwd: PROJECT_ROOT, windowsHide: true },
  );
  backend.stdout?.on('data', (d) => console.log(`[backend] ${String(d).trimEnd()}`));
  backend.stderr?.on('data', (d) => console.warn(`[backend] ${String(d).trimEnd()}`));
  backend.on('exit', (code) => {
    backendExitCode = code;
    backend = null;
  });

  await waitUntil(probeHealth, 30_000, '后端 /api/health 就绪');
}

/** 停后端并等到端口真正释放（幂等）。 */
async function stopBackend(): Promise<void> {
  const proc = backend;
  if (proc?.pid) {
    if (process.platform === 'win32') {
      // Windows 下 python 会派生子进程，需按进程树结束
      await new Promise<void>((resolve) => {
        const killer = spawn('taskkill', ['/pid', String(proc.pid), '/T', '/F'], { windowsHide: true });
        killer.on('exit', () => resolve());
        killer.on('error', () => resolve());
      });
    } else {
      proc.kill('SIGTERM');
    }
  }
  backend = null;
  await waitUntil(async () => !(await probeHealth()), 15_000, '后端端口释放（/api/health 不再 200）');
}

/** 拉起 Electron（与 app.e2e.spec.ts 同一入口，生产模式加载 dist）。 */
async function launchApp(): Promise<{ app: ElectronApplication; win: Page }> {
  const app = await _electron.launch({
    executablePath: ELECTRON_EXE,
    args: ['.'],
    cwd: FRONTEND_ROOT,
  });
  const win = await app.firstWindow();
  await win.waitForLoadState('domcontentloaded');
  await expect(win.locator('#root')).toBeVisible();
  return { app, win };
}

/** 悬浮窗页面（URL 含 pet-overlay.html）——未出现时返回 null。 */
function findOverlay(app: ElectronApplication): Page | null {
  return app.windows().find((p) => p.url().includes('pet-overlay.html')) ?? null;
}

/**
 * 确保悬浮窗已出现并取其页面。
 *
 * 先清掉 `cx-a.petEnabled` 再 reload：用例之间共享同一 Electron userData，
 * 既有 app.e2e 场景 3 结尾会写入 '0'（关闭态）；不清则本次启动不会自动拉起悬浮窗。
 * 同理清掉 `cx-a.petSize`（尺寸记忆档位）：上次运行若在大小切换段中途失败，
 * 残留的非中档档位会让「canvas 宽 286」基线断言失真，一并清零保证用例自愈。
 */
async function openOverlay(app: ElectronApplication, win: Page): Promise<Page> {
  await win.evaluate(() => {
    localStorage.removeItem('cx-a.petEnabled');
    localStorage.removeItem('cx-a.petSize');
  });
  await win.reload();
  await win.waitForLoadState('domcontentloaded');
  await waitUntil(async () => findOverlay(app) !== null, 30_000, '桌宠悬浮窗出现');
  const overlay = findOverlay(app);
  if (!overlay) throw new Error('桌宠悬浮窗未出现');
  await overlay.waitForLoadState('domcontentloaded');
  return overlay;
}

test.describe.serial('VRM 桌宠渲染链路 E2E', () => {
  test.describe('路径 A：后端可用 → 真·3D 模型就位', () => {
    test.beforeAll(async () => {
      await startBackend();
    });

    test.afterAll(async () => {
      await stopBackend();
    });

    test('悬浮窗内 VRM 渲染为 ready，且模型接口返回与文件一致的字节数', async () => {
      // 前置资产缺失属「环境不满足」——不用 test.skip（静默跳过会让断言假绿），直接失败并说明原因
      expect(
        fs.existsSync(MODEL_PATH),
        `开发态模型缺失：${MODEL_PATH}（data/ 不随仓库分发，请先放置 cx-open.vrm）`,
      ).toBe(true);
      expect(
        fs.existsSync(path.join(FRONTEND_ROOT, 'dist', 'pet-overlay.html')),
        'dist 未构建：请先执行 npm run build',
      ).toBe(true);

      // 模型接口：200 + model/gltf-binary + 字节数与磁盘文件一致
      const res = await fetch(`${API_BASE}/pet/model`);
      expect(res.status).toBe(200);
      expect(res.headers.get('content-type')).toContain('model/gltf-binary');
      const bytes = (await res.arrayBuffer()).byteLength;
      expect(bytes).toBe(fs.statSync(MODEL_PATH).size);
      expect(bytes).toBeGreaterThan(1_000_000);

      const { app, win } = await launchApp();
      const overlay = await openOverlay(app, win);

      // 画面层断言：状态位 ready（模型加载 + 解析 + 首帧渲染完成才置位）
      await expect(overlay.locator('[data-vrm-state="ready"]')).toBeAttached({ timeout: 60_000 });
      // 不出提示卡、不出卡通形象（本用例的核心：失败不许伪装成别的东西）
      // 早期 CSS 卡通形象（PetAvatar）已按设计对齐要求整体删除，不再是任何失败态的兜底
      await expect(overlay.locator('.cx-vrm-unsupported')).toHaveCount(0);
      await expect(overlay.locator('.cx-vrm-loading')).toHaveCount(0);

      // canvas 真实存在且按 286px 宽取景（悬浮窗内的显示尺寸，与 dpr 无关）
      const canvas = overlay.locator('.cx-vrm-host canvas');
      await expect(canvas).toHaveCount(1);
      // 建窗/量程校准存在异步窗口尺寸过渡，轮询等待取景稳定（一次性测量会偶发拿到 0）
      await expect
        .poll(
          async () => {
            const box = await canvas.boundingBox();
            return Math.round(box?.width ?? 0);
          },
          { timeout: 15_000, intervals: [250, 500, 1_000] },
        )
        .toBe(286);

      // ---- 应用内桌宠页同样要真·3D（两个承载点都渲染 VRM，330px） ----
      await win.getByRole('button', { name: /桌宠/ }).first().click();
      await expect(win.getByRole('heading', { name: '桌宠' })).toBeVisible({ timeout: 20_000 });
      await expect(win.locator('[data-vrm-state="ready"]')).toBeAttached({ timeout: 60_000 });
      // 页内 canvas 就位即证明渲染的是真 3D（早期 CSS 卡通形象组件已整体删除）
      const pageCanvas = win.locator('.cx-vrm-host canvas');
      await expect(pageCanvas).toHaveCount(1);
      const pageBox = await pageCanvas.boundingBox();
      expect(Math.round(pageBox?.width ?? 0), '桌宠页 canvas 显示宽度应为 330').toBe(330);

      // ---- 悬浮窗交互闭环（六项菜单）：默认收起 → 点本体弹出 → 六个功能入口 ----
      // 交互改版后无常驻按钮排：菜单默认收起（挂载后不可见），点击桌宠本体弹出
      await expect(overlay.locator('.pet-overlay-menu')).toHaveCount(0);
      await overlay.locator('.pet-overlay-stage').click();
      await expect(overlay.locator('.pet-overlay-menu')).toBeVisible();

      // 打开主窗口（对齐 CX-O 应用控制语义）：主窗口保持可见，悬浮窗本体不受影响
      await overlay.getByRole('button', { name: '打开主窗口' }).click();
      await expect(win.locator('#root')).toBeVisible();
      // 表情按钮已按 CX-O 语义移除：data-mood 固定 calm（VRM 常态表情）
      await expect(overlay.locator('.pet-overlay-stage')).toHaveAttribute('data-mood', 'calm');
      await expect(overlay.getByRole('button', { name: '开心' })).toHaveCount(0);
      await expect(overlay.getByRole('button', { name: '平静' })).toHaveCount(0);

      // 说话 = 语音输入开关（20261004 语义改版，不再是口型动画）。真实录音
      // 依赖物理麦克风（环境相关），这里不断言按下态——语音闭环的确定性行为
      // （录音→识别→发送→朗读→表情/历史同步）由组件级单测 petoverlay-voice 覆盖。
      await expect(overlay.locator('.pet-overlay')).toHaveAttribute('data-talking', 'false');

      // 六项菜单齐全（打开主窗口/说话/屏幕共享/操作授权/大小/关闭）
      for (const name of ['说话', '屏幕共享', '操作授权', '大小', '关闭']) {
        await expect(overlay.getByRole('button', { name })).toBeVisible();
      }

      // 大小：滑块连续调节（React 受控 range，需原生 setter + input 事件）
      // canvas 宽度实时跟随；窗口尺寸由主进程按公式同步（画布+28 / ×1.05+28）
      // （VrmAvatar 以 key={size} 重挂载，模型字节走模块级缓存，无 loading 间隙）
      const sizeSlider = overlay.locator('.pet-overlay-slider input');
      const setSize = (value: number) =>
        sizeSlider.evaluate((el, v) => {
          const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')
            ?.set;
          setter?.call(el, String(v));
          el.dispatchEvent(new Event('input', { bubbles: true }));
        }, value);
      await setSize(360);
      await expect(overlay.locator('.cx-vrm')).toHaveAttribute('style', /width:\s*360px/);
      await expect(overlay.locator('.cx-vrm-host canvas')).toBeVisible();
      const bigBox = await overlay.locator('.cx-vrm-host canvas').boundingBox();
      expect(Math.round(bigBox?.width ?? 0), '滑块 360 时 canvas 显示宽度应为 360').toBe(360);
      // 回默认中档：宽度回到 286px（applySize 同步写 localStorage『cx-a.petSize』）
      await setSize(286);
      await expect(overlay.locator('.cx-vrm')).toHaveAttribute('style', /width:\s*286px/);
      await expect(overlay.locator('.cx-vrm-host canvas')).toBeVisible();
      const midBox = await overlay.locator('.cx-vrm-host canvas').boundingBox();
      expect(Math.round(midBox?.width ?? 0), '滑块 286 时 canvas 显示宽度应为 286').toBe(286);

      // ready 态全程保持（交互 + 档位重挂载不得把渲染打回失败/加载）
      await expect(overlay.locator('[data-vrm-state="ready"]')).toBeAttached();

      // 关闭按钮：窗口从 2 收到 1，并把开关持久化为 '0'（尊重用户关闭选择）
      // 注意：点击本身会立刻销毁被点击的那个窗口，Playwright 的 click 因而可能以
      // 「Target page has been closed」结束——这是**预期现象**而非失败，吞掉它，
      // 用「窗口数回落 + localStorage 落 '0'」两个副作用断言真正发生了关闭。
      await overlay
        .getByRole('button', { name: '关闭' })
        .click({ timeout: 5_000 })
        .catch(() => undefined);
      await waitUntil(
        async () =>
          (await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length)) === 1,
        20_000,
        '悬浮窗关闭后窗口数回落为 1',
      );
      await waitUntil(
        async () =>
          (await win.evaluate(() => localStorage.getItem('cx-a.petEnabled'))) === '0',
        10_000,
        "关闭后 localStorage 'cx-a.petEnabled' 落 '0'",
      );

      await app.close();
    });
  });

  test.describe('路径 B：后端不可用 → 明确提示，不回落卡通', () => {
    test('悬浮窗显示「暂时显示不了 3D 桌宠」且无卡通节点', async () => {
      // 8600 被占用 → 直接失败：既可能是外部后端，也可能暴露路径 A 的端口泄漏（skip 会掩盖泄漏）
      const upBefore = await probeHealth();
      expect(
        upBefore,
        '8600 仍被后端占用（路径 A 未释放端口，或本机有外部后端在跑），无法构造「后端不可用」场景',
      ).toBe(false);
      expect(
        fs.existsSync(path.join(FRONTEND_ROOT, 'dist', 'pet-overlay.html')),
        'dist 未构建：请先执行 npm run build',
      ).toBe(true);

      const { app, win } = await launchApp();
      const overlay = await openOverlay(app, win);

      // 取模型带 6 次 × 1.5s 退避重试，全部失败后才落 unsupported
      await expect(overlay.locator('[data-vrm-state="unsupported"]')).toBeAttached({
        timeout: 60_000,
      });
      await expect(overlay.getByText('暂时显示不了 3D 桌宠')).toBeVisible();
      // 原因可读：Electron 内 WebGL 正常 → 原因应指向模型取不到（或环境不支持，二者均为合法失败态）
      const reason = (await overlay.locator('.cx-vrm-unsupported-desc').textContent()) ?? '';
      expect(reason.trim().length).toBeGreaterThan(0);
      expect(reason).toMatch(/没取到桌宠模型文件|显卡/);

      // 关键断言：不留空 canvas 冒充「加载中」；页面也不出现任何替代形象
      // （早期 CSS 卡通形象组件已整体删除，故不再断言其类名——那会是恒真断言）
      await expect(overlay.locator('canvas')).toHaveCount(0);

      await app.close();
    });
  });
});