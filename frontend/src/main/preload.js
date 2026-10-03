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

  /** 打开桌宠透明悬浮窗（主进程幂等创建；已存在则置前显示） */
  openPetOverlay: () => ipcRenderer.invoke('pet-overlay:open'),

  /** 关闭桌宠透明悬浮窗（不存在时静默成功） */
  closePetOverlay: () => ipcRenderer.invoke('pet-overlay:close'),

  /** 平移桌宠透明悬浮窗（dx/dy 为像素增量；非法参数 / 窗口不存在时主进程静默处理） */
  movePetOverlay: (dx, dy) => ipcRenderer.invoke('pet-overlay:move', dx, dy),

  /** 调整桌宠透明悬浮窗尺寸档位（size 为画面宽档位 220/286/360，主进程映射为窗口宽高） */
  resizePetOverlay: (size) => ipcRenderer.invoke('pet-overlay:resize', size),

  /**
   * 打开系统目录选择器（供设置页音色文件夹导入用，本通道只负责选目录）。
   * 用户取消返回 null，否则返回所选文件夹绝对路径。
   */
  pickVoiceFolder: () => ipcRenderer.invoke('voice:pick-folder'),

  /**
   * 获取后端启动令牌（N1 鉴权）：renderer 请求后端 API 时附带 X-Client-Token 头。
   * 非 Electron 环境（纯浏览器 dev）不会被调用。
   */
  getBackendToken: () => ipcRenderer.invoke('backend:token'),

  // ---------- 后续任务扩展点（占位，保留签名） ----------
  // 后端 API / 系统通知等能力将在此处增量暴露，
  // 契约由对应任务（A10 起）补充，本期保持最小集。
});