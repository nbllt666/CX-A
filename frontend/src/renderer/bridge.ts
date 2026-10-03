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
  /** 打开桌宠透明悬浮窗 */
  openPetOverlay: () => Promise<boolean>;
  /** 关闭桌宠透明悬浮窗 */
  closePetOverlay: () => Promise<boolean>;
  /** 平移桌宠透明悬浮窗（dx/dy 像素增量，拖拽手势用） */
  movePetOverlay: (dx: number, dy: number) => Promise<boolean>;
  /** 调整桌宠透明悬浮窗尺寸档位（220/286/360，主进程映射为窗口宽高） */
  resizePetOverlay: (size: number) => Promise<boolean>;
  /** 打开系统目录选择器选音色文件夹；取消返回 null */
  pickVoiceFolder: () => Promise<string | null>;
  /** 获取后端启动令牌（N1 鉴权：API 请求附带 X-Client-Token 头） */
  getBackendToken: () => Promise<string>;
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

/**
 * 平移桌宠透明悬浮窗（经 IPC pet-overlay:move，dx/dy 为像素增量）。
 * 仅 Electron 环境且白名单方法存在时生效；否则安全降级返回 false（不抛异常）。
 */
export function movePetOverlay(dx: number, dy: number): Promise<boolean> {
  if (isElectron() && window.cxaAPI?.movePetOverlay) {
    return window.cxaAPI.movePetOverlay(dx, dy);
  }
  return Promise.resolve(false);
}

/**
 * 调整桌宠透明悬浮窗尺寸档位（经 IPC pet-overlay:resize，档位 220/286/360）。
 * 仅 Electron 环境且白名单方法存在时生效；否则安全降级返回 false（不抛异常）。
 */
export function resizePetOverlay(size: number): Promise<boolean> {
  if (isElectron() && window.cxaAPI?.resizePetOverlay) {
    return window.cxaAPI.resizePetOverlay(size);
  }
  return Promise.resolve(false);
}

/**
 * 打开系统目录选择器选音色文件夹（经 IPC voice:pick-folder）。
 * 仅 Electron 环境且白名单方法存在时生效；用户取消返回 null；
 * 非 Electron / 方法缺失安全降级返回 null（不抛异常）。
 */
export function pickVoiceFolder(): Promise<string | null> {
  if (isElectron() && window.cxaAPI?.pickVoiceFolder) {
    return window.cxaAPI.pickVoiceFolder();
  }
  return Promise.resolve(null);
}