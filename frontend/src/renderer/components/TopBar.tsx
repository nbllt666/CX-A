import React from 'react';
import { useRouter } from '../App';

/**
 * 顶部栏：应用标识 + 版本徽标。
 * （管理面已收敛为纯后端 API，不再提供伴侣面⇄管理面模式切换。）
 */
export default function TopBar() {
  const { appInfo } = useRouter();

  return (
    <header className="absolute inset-x-0 top-0 z-20 flex h-14 items-center justify-between border-b border-[var(--glass-border)] bg-[var(--glass-bg-strong)] px-4 backdrop-blur-[var(--glass-blur)]">
      <div className="flex items-center gap-2.5">
        <span className="inline-block h-2.5 w-2.5 rounded-full bg-gradient-to-br from-[var(--color-primary)] via-[var(--color-secondary)] to-[var(--color-accent)] shadow" />
        <span className="text-base font-semibold tracking-wide">
          <span className="text-gradient">CX-A</span>
          <span className="ml-1.5 text-sm text-[var(--text-secondary)]">赛博伴侣</span>
        </span>
        {appInfo && (
          <span className="ml-2 hidden rounded-full bg-[rgba(124,216,255,0.14)] px-2 py-0.5 text-xs text-[var(--text-tertiary)] sm:inline">
            v{appInfo.version}
          </span>
        )}
      </div>

      <span className="rounded-full bg-[rgba(124,216,255,0.12)] px-2.5 py-1 text-xs text-[var(--text-tertiary)]">
        伴侣模式
      </span>
    </header>
  );
}