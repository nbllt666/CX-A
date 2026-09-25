import React from 'react';
import { Brain, Cat, MessageSquareText, Settings, type LucideIcon } from 'lucide-react';
import { useRouter } from '../App';
import type { View } from '../App';

/**
 * 侧边导航：伴侣面（聊天 / 桌宠 / 记忆 / 设置）。
 *
 * 管理面已收敛为纯后端 API（/api/agents、/api/remote/*、/api/status），
 * 不再进入前端导航，由另一 Agent / 管理工具调用。
 *
 * 图标统一走 lucide-react（与设计参考项目同一套图标语言），**不使用 emoji 当图标**：
 * emoji 的字形由系统字体决定（跨机器观感不一致、基线不齐），且高饱和色与
 * 玻璃拟态/扁平设计冲突。详见 .trae/documents/20260925_模块0_清理不合设计风格元素.md。
 */
export default function Sidebar() {
  const { view, navigate } = useRouter();

  const companionItems: { view: View; label: string; icon: LucideIcon }[] = [
    { view: 'chat', label: '聊天', icon: MessageSquareText },
    { view: 'pet', label: '桌宠', icon: Cat },
    { view: 'memories', label: '记忆', icon: Brain },
    { view: 'settings', label: '设置', icon: Settings },
  ];

  return (
    <aside className="absolute bottom-0 left-0 top-14 z-10 w-56 border-r border-[var(--glass-border)] bg-[var(--glass-bg)] p-4 backdrop-blur-[var(--glass-blur)]">
      <nav className="flex h-full flex-col gap-1">
        <p className="px-3 pb-1 text-xs font-medium tracking-wider text-[var(--text-tertiary)]">
          伴侣
        </p>
        {companionItems.map((item) => (
          <NavItem
            key={item.view}
            label={item.label}
            icon={item.icon}
            active={view === item.view}
            onClick={() => navigate(item.view)}
          />
        ))}
      </nav>
    </aside>
  );
}

function NavItem({
  label,
  icon: Icon,
  active,
  onClick,
}: {
  label: string;
  icon: LucideIcon;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        'group flex w-full items-center gap-2.5 rounded-xl px-3 py-2.5 text-sm transition-all duration-200',
        active
          ? 'bg-gradient-to-r from-[var(--color-secondary)] to-[var(--color-accent)] text-[var(--color-accent-foreground)] shadow'
          : 'text-[var(--text-secondary)] hover:bg-[rgba(255,255,255,0.1)] hover:text-[var(--text-primary)]',
      ].join(' ')}
    >
      <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
      <span className="flex-1 text-left">{label}</span>
    </button>
  );
}