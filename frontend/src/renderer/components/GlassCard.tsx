import React from 'react';
import type { HTMLAttributes } from 'react';

interface GlassCardProps extends HTMLAttributes<HTMLDivElement> {
  /** 是否带 hover 微动效 */
  hoverable?: boolean;
}

/**
 * 液态玻璃卡片容器，统一视觉的半透明面板。
 */
export const GlassCard = React.forwardRef<HTMLDivElement, GlassCardProps>(function GlassCard(
  { className, hoverable = false, children, ...props },
  ref,
) {
  const classes = [
    'glass-panel overflow-hidden text-[var(--text-primary)]',
    hoverable ? 'transition-all duration-250 hover:-translate-y-0.5 hover:shadow-lg' : '',
    className ?? '',
  ].join(' ');
  return (
    <div ref={ref} className={classes} {...props}>
      {children}
    </div>
  );
});