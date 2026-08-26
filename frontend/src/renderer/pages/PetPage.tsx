import React, { useState } from 'react';
import { GlassCard } from '../components/GlassCard';
import Toggle from '../components/Toggle';
import PetAvatar, { type PetMood } from '../components/PetAvatar';
import { usePetEnabled } from '../hooks/usePetEnabled';

/**
 * 桌宠页（/pet）。
 * 默认关闭（轻量优先，见工程文档 §5.4 / §18.2 决策点5）。
 *  - 关闭态：展示「桌宠默认关闭」说明 + 开启开关（localStorage 持久化 cx-a.petEnabled）。
 *  - 开启态（浏览器预览）：页内渲染简化桌宠 —— 纯 CSS 二次元形象、呼吸缩放、
 *    说话口型两档开合、开心 / 平静表情切换。
 *  - Electron 环境下可另开独立透明悬浮窗（见 components/PetOverlay.tsx 接线说明）。
 */
export default function PetPage() {
  const { enabled, setEnabled } = usePetEnabled();
  const [mood, setMood] = useState<PetMood>('happy');
  const [talking, setTalking] = useState(false);

  return (
    <div className="flex h-full flex-col overflow-y-auto p-5">
      <div className="mb-4">
        <h1 className="text-xl font-bold text-gradient">桌宠</h1>
        <p className="text-sm text-[var(--text-secondary)]">让一个可爱的小家伙陪你工作生活</p>
      </div>

      <div className="flex flex-1 flex-col items-center gap-6">
        {enabled ? (
          /* ---------- 开启态：页内渲染简化桌宠 ---------- */
          <div className="animate-bubble-in flex w-full flex-col items-center gap-6">
            <PetAvatar mood={mood} talking={talking} size={230} />

            {/* 口型 / 表情演示控制（占位交互） */}
            <div className="flex flex-wrap items-center justify-center gap-3">
              <div className="flex items-center gap-1.5">
                <span className="mr-0.5 text-xs text-[var(--text-tertiary)]">表情</span>
                <MoodButton active={mood === 'happy'} onClick={() => setMood('happy')}>
                  开心
                </MoodButton>
                <MoodButton active={mood === 'calm'} onClick={() => setMood('calm')}>
                  平静
                </MoodButton>
              </div>
              <button
                type="button"
                onClick={() => setTalking((v) => !v)}
                className="h-8 rounded-full border border-[var(--glass-border)] bg-[var(--bg-secondary)] px-3 text-xs font-medium text-[var(--text-secondary)] shadow-sm transition hover:text-[var(--color-primary)] focus:outline-none focus:ring-2 focus:ring-[var(--color-accent)]"
              >
                {talking ? '安静一下' : '说句话试试'}
              </button>
            </div>

            {/* 悬浮窗提示 */}
            <GlassCard className="w-full max-w-md">
              <p className="text-xs leading-relaxed text-[var(--text-tertiary)]">
                这里看到的是页内预览版小家伙。在 <b>Electron</b>{' '}
                环境里，开启权限后还能把它放到独立的透明悬浮窗里，趴在桌面上陪你（接线说明见
                <span className="text-[var(--color-accent)]"> PetOverlay.tsx</span>
                ）。
              </p>
            </GlassCard>
          </div>
        ) : (
          /* ---------- 关闭态：桌宠默认关闭说明 ---------- */
          <div className="flex flex-col items-center gap-4 py-6">
            <div className="animate-bubble-in flex h-28 w-28 items-center justify-center rounded-[2rem] bg-gradient-to-br from-[var(--pink-300)] via-[var(--color-secondary)] to-[var(--color-accent)] text-5xl opacity-70 shadow-lg">
              🐾
            </div>
            <p className="text-sm font-medium text-[var(--text-secondary)]">桌宠默认关闭</p>
            <p className="max-w-sm text-center text-xs leading-relaxed text-[var(--text-tertiary)]">
              桌宠是个轻量的小伴生，默认关着好省资源。想让小家伙来陪着你了，把下面的开关打开就好——开启后它就会在这里直接出现。
            </p>
          </div>
        )}

        {/* 开关（两种状态均可见） */}
        <GlassCard className="w-full max-w-md">
          <div className="flex items-center justify-between p-4">
            <div>
              <p className="font-medium">启用桌宠</p>
              <p className="text-xs text-[var(--text-tertiary)]">
                默认关闭 · 开启后小家伙在页面里陪你（桌面悬浮窗需 Electron 环境生效）
              </p>
            </div>
            <Toggle checked={enabled} onChange={setEnabled} label="启用桌宠" />
          </div>
        </GlassCard>
      </div>
    </div>
  );
}

/** 表情切换小按钮 */
function MoodButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        'h-8 rounded-full px-3 text-xs font-medium transition focus:outline-none focus:ring-2 focus:ring-[var(--color-accent)]',
        active
          ? 'bg-gradient-to-r from-[var(--color-secondary)] to-[var(--color-primary)] text-white shadow-sm'
          : 'border border-[var(--glass-border)] bg-[var(--bg-secondary)] text-[var(--text-secondary)] hover:text-[var(--color-primary)]',
      ].join(' ')}
    >
      {children}
    </button>
  );
}