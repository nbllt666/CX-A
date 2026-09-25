import { test, expect, _electron, type ElectronApplication, type Page } from '@playwright/test';
import path from 'node:path';

/**
 * Test2 E2E（s0402 三重闸第二关）—— Electron 真实拉起四场景。
 *
 * 直接使用 node_modules/electron/dist/electron.exe 启动（无需浏览器二进制），
 * 主入口经 package.json "main" 解析到 src/main/main.js，生产模式 loadFile dist/index.html。
 * 后端 8600 未起属预期：聊天/设置页应走降级链路而非崩溃。
 */

const FRONTEND_ROOT = path.resolve(__dirname, '..');
const ELECTRON_EXE = path.join(FRONTEND_ROOT, 'node_modules', 'electron', 'dist', 'electron.exe');

async function launchApp(): Promise<{ app: ElectronApplication; win: Page }> {
  const app = await _electron.launch({
    // Windows 下直接指向 dist 下 electron.exe
    executablePath: ELECTRON_EXE,
    args: ['.'],
    cwd: FRONTEND_ROOT,
  });
  const win = await app.firstWindow();
  await win.waitForLoadState('domcontentloaded');
  return { app, win };
}

/** 主进程真实窗口数（BrowserWindow.getAllWindows().length）。 */
function windowCount(app: ElectronApplication): () => Promise<number> {
  return () => app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length);
}

test.describe.serial('CX-A Electron 主窗口 E2E', () => {
  test('场景1：主窗口启动，App 渲染成功且标志性文案可见', async () => {
    const { app, win } = await launchApp();

    // 根元素可见（React 挂载成功）
    await expect(win.locator('#root')).toBeVisible();
    // title 匹配 index.html <title>
    await expect(win).toHaveTitle('CX-A 赛博伴侣');
    // 标志性文案：ChatPage 首屏 heading「聊天」与空态引导
    await expect(win.getByRole('heading', { name: '聊天' })).toBeVisible({ timeout: 20_000 });
    await expect(win.getByText(/还没有聊天记录/)).toBeVisible();

    await app.close();
  });

  test('场景2：发送消息后端不可达 → 未送达标记 + 提示条，无伪造回复气泡', async () => {
    const { app, win } = await launchApp();

    const input = win.getByPlaceholder('跟你的伴侣说点什么吧…');
    await input.waitFor({ state: 'visible', timeout: 20_000 });
    await input.fill('E2E 测试消息');

    // 提示条在首帧即常显（channel 初始 unknown ≠ connected）
    await expect(win.getByText(/消息暂时送不到/)).toBeVisible();

    await win.getByRole('button', { name: '发送', exact: true }).click();

    // 后端未起属预期 → fetch 失败 → 气泡带「未送达」标记
    const failedMark = win.getByText('未送达', { exact: true });
    await expect(failedMark).toBeVisible({ timeout: 30_000 });

    // 连接失败提示条仍在（文案不含技术栈字样）
    await expect(win.getByText(/消息暂时送不到/)).toBeVisible();

    // 无伪造伴侣回复：不存在伴侣气泡头像（lucide Bot 图标）；用户内容气泡保持唯一
    await expect(win.locator('svg.lucide-bot')).toHaveCount(0);
    const userBubbles = win.getByText('E2E 测试消息');
    await expect(userBubbles).toHaveCount(1);

    await app.close();
  });

  test('场景3：桌宠默认开启 → 进入桌宠页即出现透明悬浮窗，关闭后窗口数回落', async () => {
    const { app, win } = await launchApp();

    // 清掉历史持久化并重载，验证「无记录即默认开启」语义
    // （用例自身也会写入 '0'，此举避免跨次运行残留状态串扰）
    await win.evaluate(() => localStorage.removeItem('cx-a.petEnabled'));
    await win.reload();
    await win.waitForLoadState('domcontentloaded');

    // 经侧栏进入桌宠页，点击真实 UI 开关（aria-label=启用桌宠）
    await win.getByRole('button', { name: /桌宠/ }).first().click();
    const petSwitch = win.getByRole('switch', { name: '启用桌宠' });
    await petSwitch.waitFor({ state: 'visible', timeout: 20_000 });

    // 默认开启：开关初始即为 on（无持久化记录）
    await expect(petSwitch).toHaveAttribute('aria-checked', 'true');

    // 挂载恢复：进入桌宠页即自动拉起悬浮窗 → 主进程窗口列表增长为 2
    await expect
      .poll(windowCount(app), { timeout: 20_000, intervals: [250, 500, 1_000] })
      .toBe(2);

    // 第二窗口具备悬浮窗特征：置顶 + 跳过任务栏
    const traits = await app.evaluate(({ BrowserWindow }) =>
      BrowserWindow.getAllWindows().map((w) => ({
        alwaysOnTop: w.isAlwaysOnTop(),
        skipTaskbar: w.isSkipTaskbar?.() ?? null,
      })),
    );
    expect(traits.length).toBe(2);
    expect(traits.some((t) => t.alwaysOnTop === true)).toBeTruthy();

    // 关闭开关 → 窗口数回落到 1
    await petSwitch.click();
    await expect(petSwitch).toHaveAttribute('aria-checked', 'false');
    await expect
      .poll(windowCount(app), { timeout: 20_000, intervals: [250, 500, 1_000] })
      .toBe(1);

    // localStorage 持久化回落 '0'（关闭后干净退出，不污染其他用例）
    await expect
      .poll(() => win.evaluate(() => localStorage.getItem('cx-a.petEnabled')), {
        timeout: 10_000,
      })
      .toBe('0');

    await app.close();
  });

  test('场景4：设置页渲染 → 区块标题可见且有降级提示条而非空白崩溃', async () => {
    const { app, win } = await launchApp();

    await win.getByRole('button', { name: /设置/ }).first().click();
    await expect(win.getByRole('heading', { name: '设置' })).toBeVisible({ timeout: 20_000 });

    // 后端未起 → 首帧加载失败降级提示条（非空白、非崩溃）
    await expect(win.getByText(/设置加载失败啦/)).toBeVisible({ timeout: 30_000 });

    // 设置区块卡可见
    for (const block of ['云端提供商', '本地模式', '电脑控制授权']) {
      await expect(win.getByText(block).first()).toBeVisible();
    }

    await app.close();
  });

  test('场景5：手输非法 hash → 地址栏归一回写 #/chat 且聊天页可见', async () => {
    const { app, win } = await launchApp();

    // 模拟地址栏手输非法 hash（不在伴侣面路由表内）
    await win.evaluate(() => {
      window.location.hash = '#/bogus';
    });

    // App.tsx hashchange 归一逻辑：回落视图的同时回写地址栏，消除 URL 与视图失配
    await expect
      .poll(() => win.evaluate(() => window.location.hash), {
        timeout: 10_000,
        intervals: [100, 250, 500],
      })
      .toBe('#/chat');

    // 视图落在聊天页而非空白崩溃
    await expect(win.getByRole('heading', { name: '聊天' })).toBeVisible({ timeout: 20_000 });

    await app.close();
  });

  test('场景6：语音接线 UI — 朗读开关可切换、麦克风为图标按钮、后端不可达不崩溃', async () => {
    const { app, win } = await launchApp();

    // 朗读开关（页头右侧，默认关闭）：点击可双向切换
    const speakSwitch = win.getByRole('switch', { name: '朗读回复' });
    await expect(speakSwitch).toBeVisible({ timeout: 20_000 });
    await expect(speakSwitch).toHaveAttribute('aria-checked', 'false');
    await speakSwitch.click();
    await expect(speakSwitch).toHaveAttribute('aria-checked', 'true');
    await speakSwitch.click();
    await expect(speakSwitch).toHaveAttribute('aria-checked', 'false');

    // 麦克风按钮：输入区左侧图标按钮（lucide Mic，非 emoji），初始为「语音输入」态
    const mic = win.getByRole('button', { name: '语音输入' });
    await expect(mic).toBeVisible();
    await expect(mic.locator('svg')).toHaveCount(1);

    // 后端未起时点击麦克风：采集失败应被页面兜底（不崩溃、不弹未捕获错误），
    // 输入区与聊天页保持可用（错误分支只写 console，不改 UI 结构）
    await mic.click();
    await expect(win.getByRole('heading', { name: '聊天' })).toBeVisible();
    await expect(win.getByPlaceholder('跟你的伴侣说点什么吧…')).toBeVisible();

    await app.close();
  });
});
