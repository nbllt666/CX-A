/**
 * renderer 侧对 window.cxaAPI（preload 暴露）的轻量封装。
 * 开发期在纯浏览器内跑（未挂 electron）时自动降级为 mock，保证可独立渲染。
 */

export interface AppInfo {
  name: string;
  version: string;
  platform: string;
}

interface CxaBridge {
  getAppInfo: () => Promise<AppInfo>;
  onModeSwitch: (cb: (mode: string) => void) => () => void;
  /** 打开桌宠透明悬浮窗 */
  openPetOverlay: () => Promise<boolean>;
  /** 关闭桌宠透明悬浮窗 */
  closePetOverlay: () => Promise<boolean>;
}

declare global {
  interface Window {
    cxaAPI?: CxaBridge;
  }
}

const MOCK_APP_INFO: AppInfo = {
  name: 'CX-A 赛博伴侣',
  version: '0.1.0',
  platform: 'mock-dev',
};

export function isElectron(): boolean {
  return typeof window !== 'undefined' && !!window.cxaAPI;
}

export async function getAppInfo(): Promise<AppInfo> {
  if (isElectron() && window.cxaAPI) {
    return window.cxaAPI.getAppInfo();
  }
  return MOCK_APP_INFO;
}

/**
 * 订阅主进程发起的「视图面切换」。纯浏览器降级为 no-op。
 * @returns 取消订阅函数
 */
export function onModeSwitch(cb: (mode: 'companion' | 'management') => void): () => void {
  if (isElectron() && window.cxaAPI) {
    return window.cxaAPI.onModeSwitch((mode) => {
      if (mode === 'companion' || mode === 'management') cb(mode);
    });
  }
  return () => {};
}

/**
 * 打开桌宠透明悬浮窗（经 IPC pet-overlay:open，主进程幂等创建）。
 * 仅 Electron 环境生效；纯浏览器预览降级为 no-op 并返回 false。
 */
export function openPetOverlay(): Promise<boolean> {
  if (isElectron() && window.cxaAPI) {
    return window.cxaAPI.openPetOverlay();
  }
  return Promise.resolve(false);
}

/**
 * 关闭桌宠透明悬浮窗（经 IPC pet-overlay:close，窗口不存在时静默成功）。
 * 仅 Electron 环境生效；纯浏览器预览降级为 no-op 并返回 false。
 */
export function closePetOverlay(): Promise<boolean> {
  if (isElectron() && window.cxaAPI) {
    return window.cxaAPI.closePetOverlay();
  }
  return Promise.resolve(false);
}