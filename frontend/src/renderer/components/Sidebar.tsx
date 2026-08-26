import React from 'react';
import { useRouter, MODE_ROUTES } from '../App';
import type { View } from '../App';

/**
 * 侧边导航：伴侣面（聊天 / 桌宠 / 记忆 / 设置）+ 管理面分组。
 * 管理面项标注「以 API 提供」占位。
 */
export default function Sidebar() {
  const { mode, view, navigate } = useRouter();

  const companionItems: { view: View; label: string; icon: string }[] = [
    { view: 'chat', label: '聊天', icon: '💬' },
    { view: 'pet', label: '桌宠', icon: '🐾' },
    { view: 'memories', label: '记忆', icon: '💫' },
    { view: 'settings', label: '设置', icon: '⚙️' },
  ];

  const managementItems: { view: View; label: string }[] = [
    { view: 'agents', label: '智能体' },
    { view: 'remote', label: '远程' },
    { view: 'status', label: '状态' },
  ];

  return (
    <aside className="absolute bottom-0 left-0 top-14 z-10 w-56 border-r border-[var(--glass-border)] bg-[var(--glass-bg)] p-4 backdrop-blur-[var(--glass-blur)]">
      <nav className="flex h-full flex-col gap-1">
        <p className="px-3 pb-1 text-xs font-medium tracking-wider text-[var(--text-tertiary)]">
          伴侣面
        </p>
        {companionItems.map((item) => (
          <NavItem
            key={item.view}
            label={item.label}
            icon={item.icon}
            active={mode === 'companion' && view === item.view}
            disabled={mode !== 'companion'}
            onClick={() => navigate(item.view)}
          />
        ))}

        <div className="my-3 border-t border-[var(--glass-border)]" />

        <p className="px-3 pb-1 text-xs font-medium tracking-wider text-[var(--text-tertiary)]">
          管理面
        </p>
        {managementItems.map((item) => (
          <NavItem
            key={item.view}
            label={item.label}
            icon="🛠️"
            hint="以 API 提供"
            active={mode === 'management' && view === item.view}
            disabled={mode !== 'management'}
            onClick={() => navigate(item.view)}
          />
        ))}
      </nav>
    </aside>
  );
}

function NavItem({
  label,
  icon,
  active,
  disabled,
  hint,
  onClick,
}: {
  label: string;
  icon: string;
  active: boolean;
  disabled?: boolean;
  hint?: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={[
        'group flex w-full items-center gap-2.5 rounded-xl px-3 py-2.5 text-sm transition-all duration-200',
        active
          ? 'bg-gradient-to-r from-[var(--color-secondary)] to-[var(--color-accent)] text-[var(--color-accent-foreground)] shadow'
          : 'text-[var(--text-secondary)] hover:bg-[rgba(255,255,255,0.1)] hover:text-[var(--text-primary)]',
        disabled ? 'opacity-40' : '',
      ].join(' ')}
    >
      <span className="text-base leading-none">{icon}</span>
      <span className="flex-1 text-left">{label}</span>
      {hint && (
        <span className="text-[10px] leading-tight text-[var(--text-tertiary)]">{hint}</span>
      )}
    </button>
  );
}