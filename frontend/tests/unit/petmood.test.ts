import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { fireEvent } from '@testing-library/react';
import {
  PET_MOOD_HOLD_MS,
  PET_MOOD_KEY,
  normalizeBackendMood,
  publishPetMood,
  readLatestPetMood,
  onPetMood,
} from '../../src/renderer/petMood';
import { usePetMoodFeed } from '../../src/renderer/hooks/usePetMoodFeed';

/**
 * 表情总线单测（20261004_模块0_桌宠表情LLM标签驱动）：
 * LLM 情绪经 localStorage 总线跨窗口（聊天页 → 桌宠悬浮窗）驱动 VRM 表情。
 * storage 事件在真实浏览器中只在其他窗口触发——测试用 dispatchEvent 模拟
 * 悬浮窗侧收到的跨窗口事件。
 */

function dispatchStorage(key: string, newValue: string | null) {
  fireEvent(window, new StorageEvent('storage', { key, newValue }));
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(1_000_000);
  window.localStorage.clear();
});

afterEach(() => {
  cleanupHook();
  vi.useRealTimers();
  window.localStorage.clear();
});

let cleanupHook: () => void = () => undefined;

describe('normalizeBackendMood：后端情绪档白名单归一', () => {
  it('白名单内原样通过', () => {
    expect(normalizeBackendMood('happy')).toBe('happy');
    expect(normalizeBackendMood('shy')).toBe('shy');
    expect(normalizeBackendMood('sleepy')).toBe('sleepy');
  });

  it('angry 前端无对应档 → calm；非法/空 → calm', () => {
    expect(normalizeBackendMood('angry')).toBe('calm');
    expect(normalizeBackendMood('bogus')).toBe('calm');
    expect(normalizeBackendMood('')).toBe('calm');
    expect(normalizeBackendMood(null)).toBe('calm');
    expect(normalizeBackendMood(undefined)).toBe('calm');
  });
});

describe('表情总线：publish / read / onPetMood', () => {
  it('publish 后 readLatest 读回（含时间戳）', () => {
    publishPetMood('happy');
    expect(readLatestPetMood()).toEqual({ mood: 'happy', at: 1_000_000 });
  });

  it('onPetMood 收到跨窗口 storage 事件并做白名单归一', () => {
    const seen: string[] = [];
    const off = onPetMood((mood) => seen.push(mood));
    dispatchStorage(PET_MOOD_KEY, JSON.stringify({ mood: 'sad', at: 1 }));
    dispatchStorage(PET_MOOD_KEY, JSON.stringify({ mood: 'angry', at: 2 })); // 归一 calm
    dispatchStorage('cx-a.petEnabled', '0'); // 非表情键：忽略
    expect(seen).toEqual(['sad', 'calm']);
    off();
    dispatchStorage(PET_MOOD_KEY, JSON.stringify({ mood: 'happy', at: 3 }));
    expect(seen).toEqual(['sad', 'calm']); // 去订阅后不再收到
  });

  it('非法 JSON 静默忽略', () => {
    const seen: string[] = [];
    const off = onPetMood((mood) => seen.push(mood));
    dispatchStorage(PET_MOOD_KEY, 'not-json');
    expect(seen).toEqual([]);
    off();
  });
});

describe('usePetMoodFeed：表情消费 hook', () => {
  it('保鲜期内启动回放最近情绪', () => {
    publishPetMood('happy');
    const { result } = renderHook(() => usePetMoodFeed());
    expect(result.current.mood).toBe('happy');
  });

  it('超保鲜期（8 秒）不回放，保持 calm', () => {
    publishPetMood('happy');
    vi.advanceTimersByTime(PET_MOOD_HOLD_MS + 1);
    const { result } = renderHook(() => usePetMoodFeed());
    expect(result.current.mood).toBe('calm');
  });

  it('实时跟进总线事件，非 calm 档 8 秒后自动回落 calm', () => {
    const { result } = renderHook(() => usePetMoodFeed());
    expect(result.current.mood).toBe('calm');
    act(() => {
      dispatchStorage(PET_MOOD_KEY, JSON.stringify({ mood: 'surprised', at: 1_001_000 }));
    });
    expect(result.current.mood).toBe('surprised');
    act(() => {
      vi.advanceTimersByTime(PET_MOOD_HOLD_MS);
    });
    expect(result.current.mood).toBe('calm');
  });

  it('回落前新情绪到达：重置回落计时', () => {
    const { result } = renderHook(() => usePetMoodFeed());
    act(() => {
      dispatchStorage(PET_MOOD_KEY, JSON.stringify({ mood: 'sad', at: 1_001_000 }));
    });
    act(() => {
      vi.advanceTimersByTime(PET_MOOD_HOLD_MS - 1);
    });
    act(() => {
      dispatchStorage(PET_MOOD_KEY, JSON.stringify({ mood: 'happy', at: 1_002_000 }));
    });
    expect(result.current.mood).toBe('happy');
    act(() => {
      vi.advanceTimersByTime(PET_MOOD_HOLD_MS - 1);
    });
    expect(result.current.mood).toBe('happy'); // 新情绪的重置计时尚未到点
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(result.current.mood).toBe('calm');
  });

  it('pushMood：发布总线（跨窗口可达）且本窗口立即生效', () => {
    const { result } = renderHook(() => usePetMoodFeed());
    act(() => {
      result.current.pushMood('shy');
    });
    // 本窗口立即生效（storage 事件不回环本窗口，靠 pushMood 内部 apply）
    expect(result.current.mood).toBe('shy');
    // 总线已发布：其他窗口可读回（含时间戳）
    expect(readLatestPetMood()?.mood).toBe('shy');
    // 非 calm 档按同口径 8 秒回落
    act(() => {
      vi.advanceTimersByTime(PET_MOOD_HOLD_MS);
    });
    expect(result.current.mood).toBe('calm');
  });
});
