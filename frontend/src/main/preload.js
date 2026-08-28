/**
 * CX-A 赛博伴侣 — 预加载脚本
 *
 * 通过 contextBridge 向 renderer 暴露最小、窄面的 API（window.cxaAPI）。
 * 原则：contextIsolation 开启，renderer 不接触 Node 能力，仅消费这里声明的白名单方法。
 */
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('cxaAPI', {
  /** 获取应用基础信息（名称 / 版本 / 平台） */
  getAppInfo: () => ipcRenderer.invoke('app:get-info'),

  /** 订阅主进程发起的「视图面切换」事件，返回取消订阅函数 */
  onModeSwitch: (callback) => {
    const listener = (_event, mode) => {
      if (typeof callback === 'function') callback(mode);
    };
    ipcRenderer.on('mode:switch', listener);
    return () => ipcRenderer.removeListener('mode:switch', listener);
  },

  /** 打开桌宠透明悬浮窗（主进程幂等创建；已存在则置前显示） */
  openPetOverlay: () => ipcRenderer.invoke('pet-overlay:open'),

  /** 关闭桌宠透明悬浮窗（不存在时静默成功） */
  closePetOverlay: () => ipcRenderer.invoke('pet-overlay:close'),

  /**
   * 获取后端启动令牌（N1 鉴权）：renderer 请求后端 API 时附带 X-Client-Token 头。
   * 非 Electron 环境（纯浏览器 dev）不会被调用。
   */
  getBackendToken: () => ipcRenderer.invoke('backend:token'),

  // ---------- 后续任务扩展点（占位，保留签名） ----------
  // 后端 API / 系统通知等能力将在此处增量暴露，
  // 契约由对应任务（A10 起）补充，本期保持最小集。
});