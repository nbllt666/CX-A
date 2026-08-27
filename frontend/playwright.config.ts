import { defineConfig } from '@playwright/test';

/**
 * Test2 E2E 配置（s0402 三重闸第二关）。
 *
 * Electron 链路经 _electron.launch 直接拉起 node_modules/electron/dist/electron.exe，
 * 不使用浏览器二进制（无需 npx playwright install）。
 */
export default defineConfig({
  testDir: './e2e',
  // Electron 冷启动 + 后端不可达超时都较慢，整体放宽
  timeout: 120_000,
  expect: { timeout: 15_000 },
  // 单实例 GUI 应用：禁并行，规避窗口数断言互扰
  fullyParallel: false,
  workers: 1,
  // 环境性 flaky 容忍：每用例至多重试 2 次（s0402 口径），仍红则记 FAIL
  retries: 2,
  // 失败截图自动落盘 test-results/
  screenshot: 'only-on-failure',
  outputDir: 'test-results',
  reporter: [['list']],
});
