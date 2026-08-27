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
    const failedMark = win.getByText(/未送达 · 聊天通道尚未接入/);
    await expect(failedMark).toBeVisible({ timeout: 30_000 });

    // 「聊天通道尚未接入」提示条仍在
    await expect(win.getByText(/消息暂时送不到/)).toBeVisible();

    // 无伪造伴侣回复：不存在 🐱 伴侣头像节点；用户内容气泡保持唯一
    await expect(win.getByText('🐱')).toHaveCount(0);
    const userBubbles = win.getByText('E2E 测试消息');
    await expect(userBubbles).toHaveCount(1);

    await app.close();
  });

  test('场景3：桌宠开关 → 第二个透明悬浮窗出现，关闭后窗口数回落', async () => {
    const { app, win } = await launchApp();

    // 经侧栏进入桌宠页，点击真实 UI 开关（aria-label=启用桌宠）
    await win.getByRole('button', { name: /桌宠/ }).first().click();
    const petSwitch = win.getByRole('switch', { name: '启用桌宠' });
    await petSwitch.waitFor({ state: 'visible', timeout: 20_000 });
    await expect(petSwitch).toHaveAttribute('aria-checked', 'false');

    const countBefore = await windowCount(app)();
    expect(countBefore).toBe(1);

    await petSwitch.click();

    // 断言主进程窗口列表增长为 2（透明悬浮窗 BrowserWindow 已创建）
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
    await expect(petSwitch).toHaveAttribute('aria-checked', 'true');
    await petSwitch.click();
    await expect
      .poll(windowCount(app), { timeout: 20_000, intervals: [250, 500, 1_000] })
      .toBe(1);

    // localStorage 持久化回落 false（关闭后干净退出，不污染其他用例）
    await expect
      .poll(() => win.evaluate(() => localStorage.getItem('cx-a.petEnabled')), {
        timeout: 10_000,
      })
      .toBe('false');

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
});
