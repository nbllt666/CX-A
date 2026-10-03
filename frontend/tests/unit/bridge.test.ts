import { afterEach, describe, it, expect, vi } from 'vitest';
import {
  isElectron,
  getAppInfo,
  openPetOverlay,
  closePetOverlay,
  movePetOverlay,
  resizePetOverlay,
  pickVoiceFolder,
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

/**
 * Test1b · bridge.ts Electron 环境包装验证（mock window.cxaAPI）。
 *
 * 覆盖本轮新增的三个白名单包装：movePetOverlay / resizePetOverlay / pickVoiceFolder。
 * 断言两层语义：①参数原样透传给 cxaAPI（不加工、不吞返回值）；
 * ②方法缺失（preload 旧版本未暴露）时安全降级，不抛异常。
 */
describe('bridge.ts Electron 环境（window.cxaAPI 已 mock）', () => {
  afterEach(() => {
    // 清理 mock 注入，避免污染其他用例（isElectron 全局读 window.cxaAPI）
    delete (window as { cxaAPI?: unknown }).cxaAPI;
  });

  it('movePetOverlay(dx, dy) 透传像素增量给 cxaAPI 并返回其结果', async () => {
    const movePetOverlayMock = vi.fn().mockResolvedValue(true);
    (window as { cxaAPI?: unknown }).cxaAPI = { movePetOverlay: movePetOverlayMock };

    await expect(movePetOverlay(12, -7)).resolves.toBe(true);
    expect(movePetOverlayMock).toHaveBeenCalledTimes(1);
    expect(movePetOverlayMock).toHaveBeenCalledWith(12, -7);
  });

  it('resizePetOverlay(size) 透传档位给 cxaAPI 并返回其结果', async () => {
    const resizePetOverlayMock = vi.fn().mockResolvedValue(true);
    (window as { cxaAPI?: unknown }).cxaAPI = { resizePetOverlay: resizePetOverlayMock };

    await expect(resizePetOverlay(220)).resolves.toBe(true);
    expect(resizePetOverlayMock).toHaveBeenCalledWith(220);
  });

  it('pickVoiceFolder() 透传调用并返回所选文件夹路径', async () => {
    const pickVoiceFolderMock = vi.fn().mockResolvedValue('D:\\voices');
    (window as { cxaAPI?: unknown }).cxaAPI = { pickVoiceFolder: pickVoiceFolderMock };

    await expect(pickVoiceFolder()).resolves.toBe('D:\\voices');
    expect(pickVoiceFolderMock).toHaveBeenCalledTimes(1);
  });

  it('pickVoiceFolder() 用户取消（cxaAPI 返回 null）时原样返回 null', async () => {
    (window as { cxaAPI?: unknown }).cxaAPI = { pickVoiceFolder: vi.fn().mockResolvedValue(null) };

    await expect(pickVoiceFolder()).resolves.toBeNull();
  });

  it('cxaAPI 存在但方法缺失时安全降级：move/resize 返回 false、pickVoiceFolder 返回 null', async () => {
    // 模拟 preload 旧版本：桥存在但未暴露新方法
    (window as { cxaAPI?: unknown }).cxaAPI = {};

    await expect(movePetOverlay(1, 2)).resolves.toBe(false);
    await expect(resizePetOverlay(360)).resolves.toBe(false);
    await expect(pickVoiceFolder()).resolves.toBeNull();
  });
});
