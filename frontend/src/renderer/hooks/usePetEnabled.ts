import { useCallback, useEffect, useState } from 'react';

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
 * 读取 / 写入 `cx-a.petEnabled`，并监听跨窗口 storage 事件保持同步。
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