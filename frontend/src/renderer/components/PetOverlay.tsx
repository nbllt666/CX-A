import React, { useState } from 'react';
import PetAvatar, { type PetMood } from './PetAvatar';
import { PET_ENABLED_KEY } from '../hooks/usePetEnabled';
import { closePetOverlay } from '../bridge';

/**
 * PetOverlay — Electron 桌宠透明悬浮窗的独立根组件（简化版）。
 *
 * 复用 PetPage 的 CSS 二次元形象（PetAvatar），提供透明背景、可拖拽区域与
 * 口型 / 表情演示，不含 VRM 等重物理逻辑。
 *
 * ======================== 接线说明（Electron 环境生效） ========================
 * 1. main.js 的 createPetOverlayWindow() 创建透明、无边框、置顶、跳过任务栏的
 *    悬浮窗（320×360，transparent:true），由 IPC『pet-overlay:open』触发创建，
 *    『pet-overlay:close』关闭；本组件经独立入口 pet-overlay.html 挂载。
 * 2. 透明开启：BrowserWindow 以 show:false 创建，ready-to-show 后再 show()，
 *    避免部分 Linux 上直接 show 会丢透明。
 * 3. 关闭链路：点击「关闭」→ bridge.closePetOverlay()（IPC）让主进程关窗；
 *    同时写 localStorage 记录关闭状态，主窗口内 usePetEnabled 经 storage
 *    事件同步收敛开关显示。开后必有窗、关后必无窗，断链不再出现。
 * 4. 拖拽：本组件底部 .pet-overlay-drag 区域设 -webkit-app-region: drag，
 *    关闭按钮设 no-drag，保证既能拖动又能点击。
 * ======================== 鼠标穿透说明 ========================
 * 本占位未做整窗镂空穿透。要「区域外点击穿透到桌面」时，可：
 *   - 交互区（拖拽把手 / 关闭按钮）保留 pointer-events:auto；
 *   - 非交互展示区设 pointer-events:none；
 *   - 并在 BrowserWindow 侧配合 setIgnoreMouseEvents(true, { forward: true })。
 * 当前简单起见整窗保留可拖拽，穿透作为后续扩展点。
 */
export default function PetOverlay() {
  const [mood, setMood] = useState<PetMood>('happy');
  const [talking, setTalking] = useState(false);

  // 关闭按钮：优先经 IPC 桥让主进程关闭悬浮窗；localStorage 写入保留作状态记录
  const handleClose = () => {
    try {
      window.localStorage.setItem(PET_ENABLED_KEY, 'false');
    } catch {
      /* no-op */
    }
    setTalking(false);
    void closePetOverlay().catch(() => {
      /* 主进程不可达时静默：窗口自身状态仍由本次写入收敛 */
    });
  };

  return (
    <div className="pet-overlay" data-talking={talking ? 'true' : 'false'}>
      <style>{PET_OVERLAY_CSS}</style>

      <div className="pet-overlay-drag" data-mood={mood}>
        <PetAvatar mood={mood} talking={talking} size={150} />
      </div>

      <div className="pet-overlay-tools">
        <button
          type="button"
          className="pet-overlay-btn"
          onClick={() => setMood((m) => (m === 'happy' ? 'calm' : 'happy'))}
        >
          {mood === 'happy' ? '平静' : '开心'}
        </button>
        <button
          type="button"
          className="pet-overlay-btn"
          onClick={() => setTalking((v) => !v)}
        >
          {talking ? '安静' : '说话'}
        </button>
        <button type="button" className="pet-overlay-btn pet-overlay-close" onClick={handleClose}>
          关闭
        </button>
      </div>

      <p className="pet-overlay-note">拖动移动 · 这里可做鼠标穿透扩展</p>
    </div>
  );
}

const PET_OVERLAY_CSS = `
.pet-overlay {
  position: fixed;
  inset: 0;
  background: transparent;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
  padding: 12px;
  user-select: none;
  font-family: 'HarmonyOS Sans SC', 'PingFang SC', 'Microsoft YaHei', system-ui, sans-serif;
}
.pet-overlay-drag {
  -webkit-app-region: drag;
  display: flex;
  align-items: flex-end;
  justify-content: center;
}
.pet-overlay-tools {
  -webkit-app-region: no-drag;
  display: flex;
  gap: 8px;
}
.pet-overlay-btn {
  -webkit-app-region: no-drag;
  pointer-events: auto;
  border: 1px solid rgba(255, 255, 255, 0.7);
  background: rgba(255, 255, 255, 0.55);
  backdrop-filter: blur(12px) saturate(1.4);
  color: #5c5c70;
  font-size: 11px;
  line-height: 1;
  padding: 6px 10px;
  border-radius: 999px;
  cursor: pointer;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.8), 0 4px 12px rgba(255, 145, 210, 0.25);
  transition: transform 150ms ease-out;
}
.pet-overlay-btn:hover {
  transform: translateY(-1px);
}
.pet-overlay-close {
  background: rgba(240, 120, 170, 0.65);
  color: #fff;
}
.pet-overlay-note {
  -webkit-app-region: no-drag;
  margin: 0;
  font-size: 10px;
  color: rgba(124, 124, 150, 0.75);
  text-align: center;
}
`;