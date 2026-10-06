/**
 * CX-A — 预加载脚本
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

  /** 开始桌宠悬浮窗拖拽（主进程启动光标跟随循环，直至 drag-end） */
  dragPetOverlayStart: () => ipcRenderer.invoke('pet-overlay:drag-start'),

  /** 结束桌宠悬浮窗拖拽（停掉跟随循环） */
  dragPetOverlayEnd: () => ipcRenderer.invoke('pet-overlay:drag-end'),

  /** 调整桌宠透明悬浮窗尺寸档位（size 为画面宽档位，主进程按档位表映射为窗口宽高） */
  resizePetOverlay: (size) => ipcRenderer.invoke('pet-overlay:resize', size),

  /** 桌宠尺寸上下限（按主屏分辨率推导，滑块量程用） */
  getPetOverlaySizeBounds: () => ipcRenderer.invoke('pet-overlay:size-bounds'),

  /** 显示并聚焦主窗口（悬浮窗菜单「打开主窗口」通道） */
  showMainWindow: () => ipcRenderer.invoke('app:show-main'),

  /**
   * 订阅托盘桌宠开关同步（20261004_模块0_托盘常驻）：托盘直控窗口显隐后，
   * 主进程经本通道通知渲染层对齐 localStorage 开关状态（单一真相源仍为渲染层）。
   * 返回去订阅函数。
   */
  onPetSetEnabled: (callback) => {
    const listener = (_event, enabled) => callback(enabled);
    ipcRenderer.on('pet:set-enabled', listener);
    return () => ipcRenderer.removeListener('pet:set-enabled', listener);
  },

  /**
   * 打开系统目录选择器（供设置页音色文件夹导入用，本通道只负责选目录）。
   * 用户取消返回 null，否则返回所选文件夹绝对路径。
   */
  pickVoiceFolder: () => ipcRenderer.invoke('voice:pick-folder'),

  /**
   * 打开系统文件选择器挑 VRM 模型文件（供桌宠「更换桌宠模型」用，.vrm 过滤）。
   * 用户取消返回 null，否则返回所选文件绝对路径。
   */
  pickVrmFile: () => ipcRenderer.invoke('pick-vrm-file'),

  /**
   * 获取后端启动令牌（N1 鉴权）：renderer 请求后端 API 时附带 X-Client-Token 头。
   * 非 Electron 环境（纯浏览器 dev）不会被调用。
   */
  getBackendToken: () => ipcRenderer.invoke('backend:token'),

  // ---------- 后续任务扩展点（占位，保留签名） ----------
  // 后端 API / 系统通知等能力将在此处增量暴露，
  // 契约由对应任务（A10 起）补充，本期保持最小集。
});