import { useRef } from 'react';
import { useRouter } from '../App';

/** 连点次数阈值（3 秒窗口内点满 5 次解锁） */
const TAP_THRESHOLD = 5;
/** 连点时间窗口（毫秒）：超窗自动重置计数 */
const TAP_WINDOW_MS = 3_000;

/**
 * 隐藏入口 hook（spec add-fleet-frontend-hidden）：3 秒窗口内连点品牌区 5 次
 * 进入管理面（#/fleet）。
 *
 * Sidebar / TopBar 品牌区共用；前 4 次零视觉反馈、窗口过期自动重置——
 * 普通用户不可能误触，界面上也无任何可见提示。
 *
 * 返回可直接挂到品牌区容器的 onClick 处理器。
 */
export function useHiddenFleetEntrance(): () => void {
  const { navigate } = useRouter();
  const tapCountRef = useRef(0);
  const lastTapAtRef = useRef(0);

  const handleBrandTap = () => {
    const now = Date.now();
    if (now - lastTapAtRef.current > TAP_WINDOW_MS) {
      tapCountRef.current = 0; // 窗口过期：重置计数
    }
    lastTapAtRef.current = now;
    tapCountRef.current += 1;
    if (tapCountRef.current >= TAP_THRESHOLD) {
      tapCountRef.current = 0;
      navigate('fleet');
    }
  };

  return handleBrandTap;
}
