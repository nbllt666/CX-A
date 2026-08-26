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

  // ---------- 后续任务扩展点（占位，保留签名） ----------
  // 后端 API / 桌宠悬浮窗控制 / 系统通知等能力将在此处增量暴露，
  // 契约由对应任务（A10 起）补充，本期保持最小集。
});