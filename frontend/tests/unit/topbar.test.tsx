import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import TopBar from '../../src/renderer/components/TopBar';

/**
 * TopBar 隐藏入口单测（spec add-fleet-frontend-hidden 加固轮）：
 * 顶栏品牌区与侧栏共用 useHiddenFleetEntrance——连点 5 次进入管理面，
 * 界面上无任何可见提示（版本徽标为既有元素，与入口无关）。
 */

const appMocks = vi.hoisted(() => ({
  navigate: vi.fn(),
  view: 'chat',
  appInfo: { name: 'CX-A', version: '0.1.0', platform: 'mock-dev' },
}));

vi.mock('../../src/renderer/App', () => ({ useRouter: () => appMocks }));

function clickBrand(times: number) {
  const brand = screen.getByTestId('topbar-brand');
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

describe('TopBar：连点品牌区解锁管理面（与侧栏同 hook）', () => {
  it('窗口内连点 5 次 → navigate("fleet")', () => {
    render(<TopBar />);
    clickBrand(5);
    expect(appMocks.navigate).toHaveBeenCalledTimes(1);
    expect(appMocks.navigate).toHaveBeenCalledWith('fleet');
  });

  it('前 4 次不导航', () => {
    render(<TopBar />);
    clickBrand(4);
    expect(appMocks.navigate).not.toHaveBeenCalled();
  });
});
