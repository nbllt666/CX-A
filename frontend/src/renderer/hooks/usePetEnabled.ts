import { useCallback, useEffect, useState } from 'react';
import { openPetOverlay, closePetOverlay } from '../bridge';

/**
 * 桌宠开关持久化 key：PetPage 与 SettingsPage 共享同一 local storage 键，
 * 一处开启 / 关闭后，另一处组件挂载时读到的状态保持一致。
 */
export const PET_ENABLED_KEY = 'cx-a.petEnabled';

/** 读取持久化开关（默认关闭）；存储不可用时静默回退为关闭。 */
function readStoredEnabled(): boolean {
  try {
    return window.localStorage.getItem(PET_ENABLED_KEY) === 'true';
  } catch {
    return false;
  }
}

/**
 * 桌宠开关 Hook（默认关闭，轻量优先）。
 *
 * 职责：
 * - 读取 / 写入 `cx-a.petEnabled` 并监听跨窗口 storage 事件保持同步；
 * - Electron 环境下开关联动透明悬浮窗生命周期：开启 → IPC 打开悬浮窗，
 *   关闭 → IPC 关闭悬浮窗（开后必有窗、关后必无窗）；纯浏览器预览下
 *   桥调用自动降级为 no-op，行为与旧版一致。
 */
export function usePetEnabled() {
  const [enabled, setEnabledState] = useState<boolean>(readStoredEnabled);

  const setEnabled = useCallback((next: boolean) => {
    setEnabledState(next);
    try {
      window.localStorage.setItem(PET_ENABLED_KEY, String(next));
    } catch {
      /* 存储不可用（隐私模式等）时静默忽略 */
    }
    // Electron 下经桥开关悬浮窗；桥内部已处理非 Electron 环境（返回 false）
    void (next ? openPetOverlay() : closePetOverlay()).catch(() => {
      /* IPC 异常时静默：下次操作自然重试 */
    });
  }, []);

  // 跨窗口同步：Electron 悬浮窗 / 多窗口修改该 key 时跟随刷新
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === PET_ENABLED_KEY) setEnabledState(e.newValue === 'true');
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, []);

  return { enabled, setEnabled };
}