import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { usePetEnabled, PET_ENABLED_KEY } from '../../src/renderer/hooks/usePetEnabled';

/**
 * Test1 · usePetEnabled hook 持久化行为。
 * 非 Electron 环境（jsdom 无 cxaAPI）：桥调用自动降级 no-op，不影响本地状态与存储。
 */
describe('usePetEnabled', () => {
  beforeEach(() => {
    window.localStorage.clear();
    delete (window as { cxaAPI?: unknown }).cxaAPI;
  });

  it('初始读取 localStorage：无记录时默认 false', () => {
    const { result } = renderHook(() => usePetEnabled());
    expect(result.current.enabled).toBe(false);
  });

  it('初始读取 localStorage：键值为 "1" 时读回 true（新约定，D7）', () => {
    window.localStorage.setItem(PET_ENABLED_KEY, '1');
    const { result } = renderHook(() => usePetEnabled());
    expect(result.current.enabled).toBe(true);
  });

  it('初始读取 localStorage：旧值 "true" 兼容读取为 true（D7 迁移兼容）', () => {
    window.localStorage.setItem(PET_ENABLED_KEY, 'true');
    const { result } = renderHook(() => usePetEnabled());
    expect(result.current.enabled).toBe(true);
  });

  it('旧值 "false" 兼容读取为 false（PetOverlay 关闭按钮写入路径）', () => {
    window.localStorage.setItem(PET_ENABLED_KEY, 'false');
    const { result } = renderHook(() => usePetEnabled());
    expect(result.current.enabled).toBe(false);
  });

  it('setEnabled(true) 后状态翻转并写入 cx-a.petEnabled = "1"（新约定）', () => {
    const { result } = renderHook(() => usePetEnabled());
    expect(result.current.enabled).toBe(false);

    act(() => {
      result.current.setEnabled(true);
    });

    expect(result.current.enabled).toBe(true);
    expect(window.localStorage.getItem(PET_ENABLED_KEY)).toBe('1');
  });

  it('setEnabled(false) 后写回 "0"（往返幂等）', () => {
    window.localStorage.setItem(PET_ENABLED_KEY, '1');
    const { result } = renderHook(() => usePetEnabled());
    act(() => {
      result.current.setEnabled(false);
    });
    expect(result.current.enabled).toBe(false);
    expect(window.localStorage.getItem(PET_ENABLED_KEY)).toBe('0');
  });

  it('挂载时 enabled=true 且 Electron 环境下主动调 openPetOverlay 恢复悬浮窗', () => {
    const openPetOverlay = vi.fn().mockResolvedValue(true);
    const closePetOverlay = vi.fn().mockResolvedValue(true);
    (window as unknown as { cxaAPI: unknown }).cxaAPI = { openPetOverlay, closePetOverlay };
    window.localStorage.setItem(PET_ENABLED_KEY, 'true');

    renderHook(() => usePetEnabled());

    // 重启恢复：挂载即拉起悬浮窗一次，且不误触关闭
    expect(openPetOverlay).toHaveBeenCalledTimes(1);
    expect(closePetOverlay).not.toHaveBeenCalled();
  });

  it('挂载时 enabled=false 不调用 openPetOverlay（Electron 环境）', () => {
    const openPetOverlay = vi.fn().mockResolvedValue(false);
    (window as unknown as { cxaAPI: unknown }).cxaAPI = { openPetOverlay };

    renderHook(() => usePetEnabled());

    expect(openPetOverlay).not.toHaveBeenCalled();
  });

  it('挂载时 enabled=true 但非 Electron 环境：桥降级 no-op，不抛错', () => {
    // 无 cxaAPI：bridge.openPetOverlay 内部降级为 Promise.resolve(false)，静默跳过
    window.localStorage.setItem(PET_ENABLED_KEY, 'true');
    expect(() => renderHook(() => usePetEnabled())).not.toThrow();
  });
});
