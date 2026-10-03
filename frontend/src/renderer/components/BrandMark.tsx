import React from 'react';
import { Sparkles } from 'lucide-react';

/**
 * BrandMark — CX-A 品牌徽章（唯一品牌标识实现）。
 *
 * 视觉语言与首启向导一致：135° 樱花粉 → 梦境紫 → 星海青渐变的圆角方玻璃徽章，
 * 中心为白色线性星光图标（与全应用 lucide 图标语言同一套，不使用 emoji），
 * 顶部高光描边 + 柔和投影沿用 --glass-shadow 的玻璃质感口径。
 *
 * 使用点：TopBar（应用标识）、SetupWizard 欢迎头部（首启品牌入口）、
 * Sidebar 迷你品牌区。尺寸经 size 属性收敛，禁止各处手写等价样式。
 */
export default function BrandMark({ size = 28 }: { size?: number }) {
  return (
    <span
      aria-hidden="true"
      className="inline-flex shrink-0 items-center justify-center rounded-[30%] shadow-md"
      style={{
        width: size,
        height: size,
        background:
          'linear-gradient(135deg, var(--color-primary), var(--color-secondary), var(--color-accent))',
        boxShadow:
          'inset 0 1px 0 rgba(255, 255, 255, 0.6), inset 0 -2px 6px rgba(255, 255, 255, 0.25), 0 4px 14px rgba(157, 124, 255, 0.35)',
      }}
    >
      <Sparkles
        style={{ width: Math.round(size * 0.52), height: Math.round(size * 0.52) }}
        className="text-white"
        strokeWidth={2.2}
      />
    </span>
  );
}
