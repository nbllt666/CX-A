import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import Sidebar from '../../src/renderer/components/Sidebar';

/**
 * Sidebar 隐藏入口单测（spec add-fleet-frontend-hidden / GN-004 D-2）。
 *
 * 覆盖连点解锁的三条边界（fake timers 驱动 Date.now）：
 *  1. 3 秒窗口内连点 5 次 → navigate('fleet') 恰好触发一次；
 *  2. 前 4 次永不导航（零视觉反馈的语义保证）；
 *  3. 窗口过期重置：4 次后超过 3 秒，计数归零，第 5 次点击不触发。
 */

const appMocks = vi.hoisted(() => ({
  navigate: vi.fn(),
  view: 'chat',
  appInfo: null,
}));

vi.mock('../../src/renderer/App', () => ({
  useRouter: () => appMocks,
}));

function clickBrand(times: number) {
  const brand = screen.getByTestId('sidebar-brand');
  for (let i = 0; i < times; i += 1) {
    fireEvent.click(brand);
  }
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(1_000_000);
  appMocks.navigate.mockClear();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('Sidebar：连点 logo 解锁管理面', () => {
  it('3 秒窗口内连点 5 次 → navigate("fleet") 触发一次', () => {
    render(<Sidebar />);
    clickBrand(5);
    expect(appMocks.navigate).toHaveBeenCalledTimes(1);
    expect(appMocks.navigate).toHaveBeenCalledWith('fleet');
  });

  it('前 4 次点击永不导航（普通用户无感）', () => {
    render(<Sidebar />);
    clickBrand(4);
    expect(appMocks.navigate).not.toHaveBeenCalled();
  });

  it('窗口过期重置：4 次后隔 4 秒再点，计数归零不触发', () => {
    render(<Sidebar />);
    clickBrand(4);
    vi.advanceTimersByTime(4_000);
    clickBrand(1); // 窗口已过期：这 1 次是新一轮的第 1 次
    expect(appMocks.navigate).not.toHaveBeenCalled();
  });

  it('窗口内跨 3 秒内的间隔点击仍累计（4 次 + 1 秒 + 第 5 次触发）', () => {
    render(<Sidebar />);
    clickBrand(4);
    vi.advanceTimersByTime(1_000);
    clickBrand(1);
    expect(appMocks.navigate).toHaveBeenCalledTimes(1);
    expect(appMocks.navigate).toHaveBeenCalledWith('fleet');
  });
});
