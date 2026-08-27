import { describe, it, expect, beforeEach } from 'vitest';
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

  it('初始读取 localStorage：键值为 "true" 时读回 true', () => {
    window.localStorage.setItem(PET_ENABLED_KEY, 'true');
    const { result } = renderHook(() => usePetEnabled());
    expect(result.current.enabled).toBe(true);
  });

  it('setEnabled(true) 后状态翻转并写入 cx-a.petEnabled = "true"', () => {
    const { result } = renderHook(() => usePetEnabled());
    expect(result.current.enabled).toBe(false);

    act(() => {
      result.current.setEnabled(true);
    });

    expect(result.current.enabled).toBe(true);
    expect(window.localStorage.getItem(PET_ENABLED_KEY)).toBe('true');
  });

  it('setEnabled(false) 后写回 "false"（往返幂等）', () => {
    window.localStorage.setItem(PET_ENABLED_KEY, 'true');
    const { result } = renderHook(() => usePetEnabled());
    act(() => {
      result.current.setEnabled(false);
    });
    expect(result.current.enabled).toBe(false);
    expect(window.localStorage.getItem(PET_ENABLED_KEY)).toBe('false');
  });
});
