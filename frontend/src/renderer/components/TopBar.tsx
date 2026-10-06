import React from 'react';
import { useRouter } from '../App';
import BrandMark from './BrandMark';
import { useHiddenFleetEntrance } from '../hooks/useHiddenFleetEntrance';

/**
 * 顶部栏：品牌徽章 + 应用标识 + 版本徽标。
 * （管理面已收敛为纯后端 API。）
 *
 * 品牌区沿用首启向导的视觉语言（渐变玻璃 + 胶囊），logo 统一走
 * BrandMark（渐变玻璃徽章），不再使用早期的小圆点占位。
 */
export default function TopBar() {
  const { appInfo } = useRouter();
  // 隐藏入口（spec add-fleet-frontend-hidden）：顶栏品牌区与侧栏共用连点解锁，
  // 双入口都能进管理面；界面上依旧无任何可见提示。
  const handleBrandTap = useHiddenFleetEntrance();

  return (
    <header className="absolute inset-x-0 top-0 z-20 flex h-14 items-center justify-between border-b border-[var(--glass-border)] bg-[var(--glass-bg-strong)] px-4 backdrop-blur-[var(--glass-blur)]">
      <div
        className="flex items-center gap-3"
        onClick={handleBrandTap}
        data-testid="topbar-brand"
      >
        <BrandMark size={30} />
        <span className="text-base font-semibold tracking-wide">
          <span className="text-gradient">CX-A</span>
        </span>
        {appInfo && (
          <span className="ml-2 hidden rounded-full border border-[var(--glass-border)] bg-[rgba(124,216,255,0.14)] px-2 py-0.5 text-xs text-[var(--text-tertiary)] sm:inline">
            v{appInfo.version}
          </span>
        )}
      </div>
    </header>
  );
}