import React, { useState } from 'react';
import { PawPrint } from 'lucide-react';
import { GlassCard } from '../components/GlassCard';
import Toggle from '../components/Toggle';
import VrmAvatar from '../components/VrmAvatar';
import { type PetMood } from '../petMood';
import { usePetEnabled } from '../hooks/usePetEnabled';

/**
 * 桌宠页（/pet）。
 * 默认开启：开箱即见桌宠（localStorage 持久化 cx-a.petEnabled；用户关闭后尊重其选择）。
 *  - 开启态（浏览器预览）：页内渲染 VRM 桌宠（VrmAvatar）——真实 3D 模型、呼吸起伏、
 *    心情表情、说话口型、随机眨眼；模型接口不可达 / 无 WebGL 时给出中文「暂时显示不了
 *    3D 桌宠」提示（不回落卡通形象，避免被误读为「没做 VRM」）。
 *  - 关闭态：展示「桌宠已关闭」说明 + 开启开关（localStorage 持久化 cx-a.petEnabled）。
 *  - Electron 环境下可另开独立透明悬浮窗（见 components/PetOverlay.tsx 接线说明）。
 */
export default function PetPage() {
  const { enabled, setEnabled } = usePetEnabled();
  // 默认「平静」：与悬浮窗一致，睁眼常态作为首眼观感
  const [mood, setMood] = useState<PetMood>('calm');
  const [talking, setTalking] = useState(false);

  return (
    <div className="flex h-full flex-col overflow-y-auto p-5">
      <div className="mb-4">
        <h1 className="text-xl font-bold text-gradient">桌宠</h1>
        <p className="text-sm text-[var(--text-secondary)]">让一个可爱的小家伙陪你工作生活</p>
      </div>

      <div className="flex flex-1 flex-col items-center gap-6">
        {enabled ? (
          /* ---------- 开启态：页内渲染 VRM 桌宠（失败给中文提示，不回落卡通） ---------- */
          <div className="animate-bubble-in flex w-full flex-col items-center gap-6">
            <VrmAvatar mood={mood} talking={talking} size={330} />

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

            {/* 桌宠说明（用户口径：不出现技术栈与实现细节） */}
            <GlassCard className="w-full max-w-md">
              <p className="text-xs leading-relaxed text-[var(--text-tertiary)]">
                这是页内预览版的 3D 小宠物（默认模型随包内置，也能换成你自己的）。桌面上的透明悬浮窗
                里也有一个它；万一这边显示不出来，会直说原因，不会悄悄换成别的形象。
              </p>
            </GlassCard>
          </div>
        ) : (
          /* ---------- 关闭态：用户已关闭桌宠说明 ---------- */
          <div className="flex flex-col items-center gap-4 py-6">
            <div className="animate-bubble-in flex h-28 w-28 items-center justify-center rounded-[2rem] bg-gradient-to-br from-[var(--pink-300)] via-[var(--color-secondary)] to-[var(--color-accent)] opacity-70 shadow-lg">
              <PawPrint className="h-10 w-10 text-white/85" aria-hidden="true" />
            </div>
            <p className="text-sm font-medium text-[var(--text-secondary)]">桌宠已关闭</p>
            <p className="max-w-sm text-center text-xs leading-relaxed text-[var(--text-tertiary)]">
              桌宠默认是开着的，你把它关掉了。想让小家伙回来陪你了，把下面的开关打开就好——开启后它就会在这里直接出现。
            </p>
          </div>
        )}

        {/* 开关（两种状态均可见） */}
        <GlassCard className="w-full max-w-md">
          <div className="flex items-center justify-between p-4">
            <div>
              <p className="font-medium">启用桌宠</p>
              <p className="text-xs text-[var(--text-tertiary)]">
                默认开启 · 关闭后小家伙就不再出现（桌面悬浮窗需 Electron 环境生效）
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