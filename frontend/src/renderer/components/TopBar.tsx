import React from 'react';
import { useRouter } from '../App';
import { MODE_LABEL, type Mode } from '../App';

/**
 * 顶部栏：应用标识 + 伴侣面⇄管理面模式切换（同进程切换视图，不重启窗口）。
 */
export default function TopBar() {
  const { mode, appInfo, switchMode } = useRouter();

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

      <ModeSwitch mode={mode} onSwitch={switchMode} />
    </header>
  );
}

function ModeSwitch({ mode, onSwitch }: { mode: Mode; onSwitch: (m: Mode) => void }) {
  const options: Mode[] = ['companion', 'management'];
  return (
    <div className="glass-panel-strong flex items-center gap-1 p-1 !rounded-full">
      {options.map((m) => (
        <button
          key={m}
          type="button"
          onClick={() => onSwitch(m)}
          className={[
            'rounded-full px-4 py-1.5 text-sm font-medium transition-all duration-200',
            mode === m
              ? 'bg-gradient-to-r from-[var(--color-secondary)] to-[var(--color-primary)] text-white shadow'
              : 'text-[var(--text-secondary)] hover:bg-[rgba(255,255,255,0.1)] hover:text-[var(--text-primary)]',
          ].join(' ')}
        >
          {MODE_LABEL[m]}
        </button>
      ))}
    </div>
  );
}