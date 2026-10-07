import { test, expect, _electron, type ElectronApplication, type Page } from '@playwright/test';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';

/**
 * 打包态「全新安装」实跑（默认不参与门禁，需显式启用）。
 *
 * 与其余 E2E 的区别：本用例打的是**打包产物**，不是源码 + dist：
 *   `release/portable/`（安装程序载荷目录，与 CX-A-Setup 安装后的目录一致）
 *   → `<root>/CX-A.exe`（自带 runtime/backend/backend.exe 与 data/pet/cx-open.vrm，
 *   顶层无 config.json = 真·全新安装）。便携 zip 已移除（20261007 产品裁决：
 *   唯一交付物为安装程序），本用例改以载荷目录为载体。
 *
 * 为什么要单独一关：源码态 E2E 无法覆盖「首启门控 + 打包后的后端拉起 + 内置模型路径」三者
 * 叠加的真实路径——上一轮的教训正是「源码里对了、包里的旧产物不对」，用户看到的是包。
 *
 * 覆盖链路：首启向导出现（配置未完成）→ 向导阶段不出悬浮窗 → 走完 5 步并提交（POST
 * /api/setup/complete）→ 主界面 → 自动拉起透明悬浮窗 → VRM `data-vrm-state="ready"` +
 * canvas 286px → 载荷根 config.json 落盘 `completed: true`。
 *
 * 启用方式（默认 skip，避免无产物环境下误红）：
 *   $env:PW_PACKAGED=1; $env:CXA_PACKAGED_ROOT='<release/portable 目录>'
 *   npx playwright test e2e/packaged-install.e2e.spec.ts
 * 可选：CXA_PACKAGED_SHOT=<截图落盘目录>
 */

const PACKAGED_ROOT = process.env.CXA_PACKAGED_ROOT ?? '';
const SHOT_DIR = process.env.CXA_PACKAGED_SHOT ?? path.join('test-results', 'packaged-install');

/** 后端健康探测（打包态后端固定 8600）。 */
function probeHealth(): Promise<boolean> {
  return new Promise((resolve) => {
    const req = http.get('http://127.0.0.1:8600/api/health', { timeout: 1500 }, (res) => {
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

/** 采集当前向导状态：步骤号 + 可见标题/按钮文案（失败时用于定位卡在哪一步）。 */
async function dumpWizardState(win: Page): Promise<string> {
  const step = await win
    .locator('text=/第 \\d+ \\/ \\d+ 步/')
    .first()
    .textContent()
    .catch(() => null);
  const headings = await win.getByRole('heading').allTextContents().catch(() => []);
  const buttons = await win.getByRole('button').allTextContents().catch(() => []);
  return `step=${step ?? 'n/a'} headings=${JSON.stringify(headings)} buttons=${JSON.stringify(buttons)}`;
}

/** 点掉给定候选按钮里第一个可见的（向导不同分支的按钮文案不同，逐个探测）。 */
async function clickAny(win: Page, names: string[], timeoutMs = 60_000): Promise<string> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    for (const name of names) {
      const loc = win.getByRole('button', { name, exact: true });
      if ((await loc.count()) > 0 && (await loc.first().isVisible())) {
        await loc.first().click();
        return name;
      }
    }
    await win.waitForTimeout(300);
  }
  throw new Error(
    `未等到可点击按钮：${names.join(' / ')}；当前状态 ${await dumpWizardState(win)}`,
  );
}

test.describe.serial('打包态全新安装实跑', () => {
  /** 用例前置：包内有 exe 与内置模型（缺失即失败，不静默跳过）。 */
  function assertPrereqs(): void {
    const exe = path.join(PACKAGED_ROOT, 'CX-A.exe');
    expect(fs.existsSync(exe), `打包产物可执行文件不存在：${exe}`).toBe(true);
    expect(
      fs.existsSync(path.join(PACKAGED_ROOT, 'data', 'pet', 'cx-open.vrm')),
      '包内缺少内置模型 data/pet/cx-open.vrm',
    ).toBe(true);
  }

  /** 拉起便携包主程序（同一用例内可能多次拉起，故抽成 helper）。 */
  async function launchPackaged(): Promise<{ app: ElectronApplication; win: Page }> {
    const app: ElectronApplication = await _electron.launch({
      executablePath: path.join(PACKAGED_ROOT, 'CX-A.exe'),
    });
    const win = await app.firstWindow();
    await win.waitForLoadState('domcontentloaded');
    return { app, win };
  }

  test('向导中途退出再进入：重新从第 1 步开始（部分进度不持久化）', async () => {
    test.skip(
      process.env.PW_PACKAGED !== '1',
      '需显式启用：设置 PW_PACKAGED=1 与 CXA_PACKAGED_ROOT=<解压后的便携根>',
    );
    test.setTimeout(180_000);
    assertPrereqs();
    expect(await probeHealth(), '跑全新安装前 8600 必须是空闲的').toBe(false);

    // 第一轮：进向导 → 推进到第 2 步 → 直接关掉应用（模拟用户中途退出）
    const first = await launchPackaged();
    await expect(first.win.getByRole('heading', { name: '欢迎来到 CX-A' })).toBeVisible({
      timeout: 60_000,
    });
    await clickAny(first.win, ['就用推荐的', '跳过推荐，直接下一步']);
    // 分支无关：不写死步号（推荐为本地时快车道直达线路步骤），只断言已离开第 1 步
    await expect(first.win.getByText(/第 [2-5] \/ 5 步/)).toBeVisible({ timeout: 30_000 });
    await first.app.close();
    await waitUntil(async () => !(await probeHealth()), 20_000, '退出后 8600 释放');

    // 第二轮：再来一次——向导必须重新出现且回到第 1 步（中途进度不落盘），
    // 同时证明「向导不走完就不会被记成已完成」（上一轮修过的永久跳过缺陷的回归防线）
    const second = await launchPackaged();
    await expect(second.win.getByRole('heading', { name: '欢迎来到 CX-A' })).toBeVisible({
      timeout: 60_000,
    });
    await expect(second.win.getByText(/第 1 \/ 5 步/)).toBeVisible();
    expect(
      await second.app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length),
      '向导未完成时不应有悬浮窗',
    ).toBe(1);
    // config 仍未落「已完成」
    const configPath = path.join(PACKAGED_ROOT, 'config.json');
    if (fs.existsSync(configPath)) {
      const config = JSON.parse(fs.readFileSync(configPath, 'utf-8')) as {
        setup?: { completed?: boolean };
      };
      expect(config.setup?.completed ?? false, '中途退出后 config 不应记 completed=true').toBe(
        false,
      );
    }
    await second.app.close();
    await waitUntil(async () => !(await probeHealth()), 20_000, '退出后 8600 释放');
  });

  test('向导 → 主界面 → 悬浮窗 VRM ready（含 config 落盘校验）', async () => {
    test.skip(
      process.env.PW_PACKAGED !== '1',
      '需显式启用：设置 PW_PACKAGED=1 与 CXA_PACKAGED_ROOT=<解压后的便携根>',
    );
    test.setTimeout(240_000);
    assertPrereqs();
    // 全新安装前提：端口空闲（若被占用，说明有外部/残留后端，实跑结论不可信）
    expect(await probeHealth(), '跑全新安装前 8600 必须是空闲的').toBe(false);

    const { app, win } = await launchPackaged();

    // ── ① 首启向导出现（顶层无 config.json → 后端判 wizard_required） ──
    await expect(win.getByRole('heading', { name: '欢迎来到 CX-A' })).toBeVisible({
      timeout: 60_000,
    });
    await expect(win.getByText(/第 1 \/ 5 步/)).toBeVisible();
    // 向导阶段不挂桌宠生命周期：不应有第二个窗口
    expect(
      await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length),
      '向导阶段不应出现悬浮窗',
    ).toBe(1);

    // ── ② 走完 5 步（步 0 视体检结果有两种按钮，逐个探测） ──
    const first = await clickAny(win, ['就用推荐的', '跳过推荐，直接下一步']);
    console.log(`[packaged] 步骤0 点击：${first}；点击后状态 ${await dumpWizardState(win)}`);
    // 中间步推进改为条件驱动：快车道可能跳过云端步骤，打包态步数不写死
    // （本地快车道 2 步、云端主路径 3 步；上限 6 次防死循环；「跳过，先用本地」作兜底候选）
    for (let i = 0; i < 6; i += 1) {
      const confirmVisible = await win
        .getByRole('heading', { name: '快好了，确认一下' })
        .isVisible()
        .catch(() => false);
      if (confirmVisible) break;
      const clicked = await clickAny(win, ['下一步', '以后再说', '跳过，先用本地']);
      console.log(`[packaged] 第 ${i + 2} 步点击：${clicked}；状态 ${await dumpWizardState(win)}`);
    }
    await expect(win.getByRole('heading', { name: '快好了，确认一下' })).toBeVisible();
    // 最后一步用真实提交（POST /api/setup/complete），而不是「稍后再说」——
    // 这样同时验证提交链路与 config 落盘
    await clickAny(win, ['开始聊天']);

    // ── ③ 提交成功 → 主界面 ──
    await expect(win.getByRole('heading', { name: '聊天' })).toBeVisible({ timeout: 60_000 });
    await expect(win.getByRole('heading', { name: '欢迎来到 CX-A' })).toHaveCount(0);

    // ── ④ 主界面自动拉起悬浮窗（默认开启），且 VRM 真就位 ──
    //    历史 localStorage 可能残留 '0'（同一 userData 被其它用例写过），清掉后 reload 复原默认开启；
    //    同理清掉尺寸偏好（cx-a.petSize 与已安装副本共享 userData，残留会使默认档位断言失真）
    await win.evaluate(() => {
      localStorage.removeItem('cx-a.petEnabled');
      localStorage.removeItem('cx-a.petSize');
    });
    await win.reload();
    await win.waitForLoadState('domcontentloaded');
    await expect(win.getByRole('heading', { name: '聊天' })).toBeVisible({ timeout: 60_000 });

    await waitUntil(
      async () =>
        app.windows().some((p) => p.url().includes('pet-overlay.html')),
      60_000,
      '悬浮窗出现',
    );
    const overlay = app.windows().find((p) => p.url().includes('pet-overlay.html'));
    if (!overlay) throw new Error('悬浮窗未出现');
    await overlay.waitForLoadState('domcontentloaded');

    await expect(overlay.locator('[data-vrm-state="ready"]')).toBeAttached({ timeout: 90_000 });
    await expect(overlay.locator('.cx-vrm-unsupported')).toHaveCount(0);
    const box = await overlay.locator('.cx-vrm-host canvas').boundingBox();
    expect(Math.round(box?.width ?? 0), '悬浮窗内 canvas 显示宽度应为 286').toBe(286);

    // 截图留证（透明窗铺浅底便于目视）
    fs.mkdirSync(SHOT_DIR, { recursive: true });
    await overlay.evaluate(() => {
      document.body.style.background = '#f2f2f7';
    });
    await overlay.waitForTimeout(300);
    const shot = path.join(SHOT_DIR, 'packaged-overlay-ready.png');
    fs.writeFileSync(shot, await overlay.screenshot());
    console.log(`[packaged] 截图：${shot}`);

    // ── ⑤ 便携根 config.json 落盘 completed=true（提交链路真的写进包内配置） ──
    const configPath = path.join(PACKAGED_ROOT, 'config.json');
    await waitUntil(async () => fs.existsSync(configPath), 15_000, 'config.json 落盘');
    const config = JSON.parse(fs.readFileSync(configPath, 'utf-8')) as {
      setup?: { completed?: boolean };
    };
    expect(config.setup?.completed, '提交后 config.json 应记录 setup.completed=true').toBe(true);

    await app.close();

    // ── ⑥ 退出后端口释放（主进程应回收后端子进程） ──
    await waitUntil(async () => !(await probeHealth()), 20_000, '退出后 8600 释放');
  });
});