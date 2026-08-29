import { describe, it, expect } from 'vitest';
import {
  isElectron,
  getAppInfo,
  openPetOverlay,
  closePetOverlay,
} from '../../src/renderer/bridge';

/**
 * Test1 · bridge.ts 非 Electron 环境降级验证。
 *
 * jsdom 下 window.cxaAPI 天然缺失（preload 只在 Electron 中注入），
 * 断言桥函数全部安全降级为 no-op / mock 值，不抛任何异常。
 */
describe('bridge.ts 非 Electron 环境（window.cxaAPI 缺失）', () => {
  it('isElectron() 返回 false 且不抛异常', () => {
    expect(window.cxaAPI).toBeUndefined();
    expect(() => isElectron()).not.toThrow();
    expect(isElectron()).toBe(false);
  });

  it('getAppInfo() 降级返回 mock AppInfo，不抛异常', async () => {
    await expect(getAppInfo()).resolves.toEqual({
      name: 'CX-A 赛博伴侣',
      version: '0.1.0',
      platform: 'mock-dev',
    });
  });

  it('openPetOverlay() 降级 no-op，resolve false 不抛异常', async () => {
    await expect(openPetOverlay()).resolves.toBe(false);
  });

  it('closePetOverlay() 降级 no-op，resolve false 不抛异常', async () => {
    await expect(closePetOverlay()).resolves.toBe(false);
  });
});
