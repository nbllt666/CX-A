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
  /** 开始桌宠悬浮窗拖拽（主进程光标跟随循环） */
  dragPetOverlayStart: () => Promise<boolean>;
  /** 结束桌宠悬浮窗拖拽 */
  dragPetOverlayEnd: () => Promise<boolean>;
  /** 调整桌宠透明悬浮窗尺寸档位（六档，主进程映射为窗口宽高） */
  resizePetOverlay: (size: number) => Promise<boolean>;
  /** 桌宠尺寸上下限（按主屏分辨率推导，滑块量程用） */
  getPetOverlaySizeBounds: () => Promise<{ min: number; max: number }>;
  /** 打开系统目录选择器选音色文件夹；取消返回 null */
  pickVoiceFolder: () => Promise<string | null>;
  /** 打开系统文件选择器挑 VRM 模型文件（.vrm 过滤）；取消返回 null */
  pickVrmFile?: () => Promise<string | null>;
  /** 获取后端启动令牌（N1 鉴权：API 请求附带 X-Client-Token 头） */
  getBackendToken: () => Promise<string>;
  /** 显示并聚焦主窗口（悬浮窗菜单「打开主窗口」通道） */
  showMainWindow?: () => Promise<boolean>;
  /** 订阅托盘桌宠开关同步；返回去订阅函数（旧主进程无此方法时为 undefined） */
  onPetSetEnabled?: (callback: (enabled: boolean) => void) => () => void;
}

declare global {
  interface Window {
    cxaAPI?: CxaBridge;
  }
}

const MOCK_APP_INFO: AppInfo = {
  name: 'CX-A',
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
 * 开始桌宠悬浮窗拖拽（经 IPC pet-overlay:drag-start）。
 * 主进程进入光标跟随循环直至 drag-end；仅 Electron 环境且白名单方法存在时生效，
 * 否则安全降级返回 false（不抛异常）。
 */
export function dragPetOverlayStart(): Promise<boolean> {
  if (isElectron() && window.cxaAPI?.dragPetOverlayStart) {
    return window.cxaAPI.dragPetOverlayStart();
  }
  return Promise.resolve(false);
}

/**
 * 结束桌宠悬浮窗拖拽（经 IPC pet-overlay:drag-end）。
 * 语义同 dragPetOverlayStart 的降级口径。
 */
export function dragPetOverlayEnd(): Promise<boolean> {
  if (isElectron() && window.cxaAPI?.dragPetOverlayEnd) {
    return window.cxaAPI.dragPetOverlayEnd();
  }
  return Promise.resolve(false);
}

/**
 * 获取桌宠尺寸上下限（经 IPC pet-overlay:size-bounds，按主屏分辨率推导）。
 * 仅 Electron 环境且白名单方法存在时生效；否则回退保守默认 { min: 160, max: 640 }。
 */
export function getPetOverlaySizeBounds(): Promise<{ min: number; max: number }> {
  if (isElectron() && window.cxaAPI?.getPetOverlaySizeBounds) {
    return window.cxaAPI.getPetOverlaySizeBounds();
  }
  return Promise.resolve({ min: 160, max: 640 });
}

/**
 * 调整桌宠透明悬浮窗尺寸（经 IPC pet-overlay:resize）。
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

/**
 * 打开系统文件选择器挑 VRM 模型文件（经 IPC pick-vrm-file，.vrm 过滤）。
 * 仅 Electron 环境且白名单方法存在时生效；用户取消返回 null；
 * 非 Electron / 方法缺失安全降级返回 null（不抛异常）。
 */
export function pickVrmFile(): Promise<string | null> {
  if (isElectron() && window.cxaAPI?.pickVrmFile) {
    return window.cxaAPI.pickVrmFile();
  }
  return Promise.resolve(null);
}

/**
 * 显示并聚焦主窗口（经 IPC app:show-main；悬浮窗菜单「打开主窗口」通道）。
 * 仅 Electron 环境且白名单方法存在时生效；否则安全降级返回 false（不抛异常）。
 */
export function showMainWindow(): Promise<boolean> {
  if (isElectron() && window.cxaAPI?.showMainWindow) {
    return window.cxaAPI.showMainWindow();
  }
  return Promise.resolve(false);
}

/**
 * 订阅托盘桌宠开关同步（主进程直控窗口显隐后通知渲染层对齐 localStorage）。
 * 仅 Electron 环境且白名单方法存在时生效；非 Electron 环境返回 no-op 去订阅函数。
 */
export function onPetSetEnabled(callback: (enabled: boolean) => void): () => void {
  if (isElectron() && window.cxaAPI?.onPetSetEnabled) {
    return window.cxaAPI.onPetSetEnabled(callback);
  }
  return () => undefined;
}