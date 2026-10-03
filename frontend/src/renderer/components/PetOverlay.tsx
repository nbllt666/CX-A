import React, { useEffect, useRef, useState } from 'react';
import VrmAvatar from './VrmAvatar';
import { type PetMood } from '../petMood';
import { PET_ENABLED_KEY } from '../hooks/usePetEnabled';
import { closePetOverlay, movePetOverlay, resizePetOverlay } from '../bridge';

/**
 * PetOverlay — Electron 桌宠透明悬浮窗的独立根组件。
 *
 * 渲染真实 VRM 模型（VrmAvatar，three + @pixiv/three-vrm，透明背景 canvas）；
 * 当无 WebGL / 模型接口不可达 / 解析失败时，在窗口内给出「暂时显示不了 3D 桌宠」
 * 的中文提示（含可读原因），**不回落卡通形象**——回落会让人误以为「根本没做 VRM」
 * （见 VrmAvatar 的兜底逻辑与 data-vrm-state 状态位）。
 *
 * ======================== 接线说明（Electron 环境生效） ========================
 * 1. main.js 的 createPetOverlayWindow() 创建透明、无边框、置顶、跳过任务栏的
 *    悬浮窗（初始尺寸 = 中档预设 314×366，transparent:true），由 IPC
 *    『pet-overlay:open』触发创建，『pet-overlay:close』关闭；本组件经独立入口
 *    pet-overlay.html 挂载。
 * 2. 透明开启：BrowserWindow 以 show:false 创建，ready-to-show 后再 show()，
 *    避免部分 Linux 上直接 show 会丢透明；VRM canvas 亦以 alpha+setClearAlpha(0) 保证透明。
 * 3. 关闭链路：点菜单「关闭」→ bridge.closePetOverlay()（IPC）让主进程关窗；
 *    同时写 localStorage 记录关闭状态，主窗口内 usePetEnabled 经 storage
 *    事件同步收敛开关显示。开后必有窗、关后必无窗，断链不再出现。
 * 4. 拖拽（GN-004 F1 改版）：**不再使用 CSS 拖拽区域标记**——旧标记会吞掉
 *    mousedown/click，导致「点击弹菜单」无法实现。改为纯 JS 手势：在桌宠本体
 *    （.pet-overlay-stage）上 pointerdown 记起点、pointermove 累计位移（>3px 判定
 *    拖拽，约 16ms 节流调 movePetOverlay(dx, dy) 像素增量、随后把累计基准重置为
 *    当前点）、pointerup 未拖拽则视为点击弹/收菜单。
 * 5. 弹出菜单：点击本体弹出玻璃拟态菜单（心情两枚 / 说话 toggle / 大小三档 /
 *    关闭）；点击菜单外（根元素 pointerdown 且目标不在菜单内、也不在本体上）收起。
 * 6. 尺寸档位：220 / 286 / 360 三档（画面宽）。点击档位 → 写 localStorage
 *    『cx-a.petSize』→ resizePetOverlay(档位)（主进程按映射表换窗口宽高、保持
 *    中心不变）→ setSize + VrmAvatar key={size} 重挂载（模型字节走模块级缓存，
 *    零请求零等待无感切换）。挂载时读记忆档位并幂等校准窗口尺寸。
 * ======================== 鼠标穿透说明 ========================
 * 本组件未做整窗镂空穿透。要「区域外点击穿透到桌面」时，可：
 *   - 交互区（本体 / 菜单）保留 pointer-events:auto；
 *   - 非交互展示区设 pointer-events:none；
 *   - 并在 BrowserWindow 侧配合 setIgnoreMouseEvents(true, { forward: true })。
 * 当前简单起见整窗保留可交互，穿透作为后续扩展点。
 */

/** 尺寸档位（画面宽 px）——与 main.js PET_OVERLAY_SIZE_PRESETS 的键一一对应 */
const PET_SIZES = [220, 286, 360] as const;
type PetSize = (typeof PET_SIZES)[number];

/** 尺寸记忆的本地存储键与菜单文案 */
const PET_SIZE_KEY = 'cx-a.petSize';
const DEFAULT_PET_SIZE: PetSize = 286;
const PET_SIZE_LABEL: Record<PetSize, string> = { 220: '小', 286: '中', 360: '大' };

/** 位移超过该阈值（px）判定为拖拽而非点击 */
const DRAG_THRESHOLD_PX = 3;
/** 拖拽增量 IPC 的节流间隔（约一帧，避免高频 IPC 洪泛主进程） */
const MOVE_THROTTLE_MS = 16;

/** 读取记忆档位；非法 / 缺失回落中档 286 */
function readStoredPetSize(): PetSize {
  try {
    const raw = window.localStorage.getItem(PET_SIZE_KEY);
    const value = raw === null ? NaN : Number(raw);
    return (PET_SIZES as readonly number[]).includes(value)
      ? (value as PetSize)
      : DEFAULT_PET_SIZE;
  } catch {
    return DEFAULT_PET_SIZE;
  }
}

/** 一次拖拽手势的瞬时状态（ref 而非 state：pointermove 高频更新不应触发渲染） */
interface DragGestureState {
  pointerId: number;
  origin: { x: number; y: number };
  /** 增量基准：上次发 IPC 时的指针位置，每次发送后重置为当前点 */
  last: { x: number; y: number };
  moved: boolean;
  lastSentAt: number;
}

export default function PetOverlay() {
  // 默认「平静」：neutral 表情为睁眼常态，避免直接进 happy 的笑眼被误读成「眼睛没睁开」
  const [mood, setMood] = useState<PetMood>('calm');
  const [talking, setTalking] = useState(false);
  const [size, setSize] = useState<PetSize>(() => readStoredPetSize());
  const [menuOpen, setMenuOpen] = useState(false);

  const menuRef = useRef<HTMLDivElement | null>(null);
  const stageRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<DragGestureState | null>(null);

  // 挂载即校准窗口尺寸（幂等）：主进程建窗固定为中档预设，若记忆档位非中档，
  // 这里立即把窗口对齐到记忆档位，保证「初始 size 与窗口大小一致」
  useEffect(() => {
    void resizePetOverlay(size).catch(() => {
      /* 主进程不可达时静默：档位下次交互仍可再校准 */
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 换档：写记忆 → 同步 renderer 画面（key={size} 重挂载 VrmAvatar，模型字节走缓存）
  // → 主进程换窗口宽高（保持中心不变）
  const applySize = (next: PetSize) => {
    try {
      window.localStorage.setItem(PET_SIZE_KEY, String(next));
    } catch {
      /* 存储不可用（隐私模式等）时静默：本会话内档位仍生效 */
    }
    setSize(next);
    void resizePetOverlay(next).catch(() => {
      /* 主进程不可达时静默 */
    });
  };

  // 关闭：优先经 IPC 桥让主进程关闭悬浮窗；localStorage 写入保留作状态记录
  const handleClose = () => {
    try {
      window.localStorage.setItem(PET_ENABLED_KEY, '0');
    } catch {
      /* no-op */
    }
    setTalking(false);
    void closePetOverlay().catch(() => {
      /* 主进程不可达时静默：窗口自身状态仍由本次写入收敛 */
    });
  };

  // ---- 桌宠本体手势：拖拽增量移动 + 点击弹菜单 ----
  const handleStagePointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!e.isPrimary) return;
    const point = { x: e.clientX, y: e.clientY };
    dragRef.current = { pointerId: e.pointerId, origin: point, last: point, moved: false, lastSentAt: 0 };
    // 捕获指针：移出本体（甚至窗口）仍能继续收到 move/up，拖拽不断手
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      /* capture 失败不影响后续手势（部分环境对已释放指针会抛错） */
    }
  };

  const handleStagePointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const gesture = dragRef.current;
    if (!gesture || gesture.pointerId !== e.pointerId) return;
    if (!gesture.moved) {
      const dxTotal = e.clientX - gesture.origin.x;
      const dyTotal = e.clientY - gesture.origin.y;
      if (Math.hypot(dxTotal, dyTotal) <= DRAG_THRESHOLD_PX) return;
      gesture.moved = true; // 超过阈值：本次手势判定为拖拽而非点击
    }
    const now = performance.now();
    if (now - gesture.lastSentAt < MOVE_THROTTLE_MS) return; // 节流：约一帧发一次增量
    gesture.lastSentAt = now;
    const dx = e.clientX - gesture.last.x;
    const dy = e.clientY - gesture.last.y;
    if (dx !== 0 || dy !== 0) {
      void movePetOverlay(dx, dy).catch(() => {
        /* 主进程不可达时静默：窗口就地不动，不中断手势 */
      });
    }
    gesture.last = { x: e.clientX, y: e.clientY }; // 累计基准重置为当前点
  };

  const handleStagePointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    const gesture = dragRef.current;
    if (!gesture || gesture.pointerId !== e.pointerId) return;
    dragRef.current = null;
    if (!gesture.moved) setMenuOpen((v) => !v); // 未拖拽 → 视为点击：弹/收菜单
  };

  const handleStagePointerCancel = () => {
    dragRef.current = null; // 手势被打断：丢弃状态，不触发菜单 toggle
  };

  // ---- 点菜单外收起：根元素 pointerdown 且目标不在菜单内 → 收起 ----
  // 本体（stage）除外：本体的 pointerup toggle 自管开关——若这里也收起，
  // 「pointerdown 关 + pointerup 开」两次更新相抵消，表现为点本体永远收不起菜单。
  const handleRootPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    const target = e.target as Node;
    if (menuRef.current?.contains(target)) return; // 菜单内点击：按钮 onClick 自行处理
    if (stageRef.current?.contains(target)) return; // 本体点击：由 toggle 接管
    setMenuOpen(false);
  };

  return (
    <div className="pet-overlay" data-talking={talking ? 'true' : 'false'} onPointerDown={handleRootPointerDown}>
      <style>{PET_OVERLAY_CSS}</style>

      <div
        ref={stageRef}
        className="pet-overlay-stage"
        data-mood={mood}
        onPointerDown={handleStagePointerDown}
        onPointerMove={handleStagePointerMove}
        onPointerUp={handleStagePointerUp}
        onPointerCancel={handleStagePointerCancel}
      >
        <VrmAvatar key={size} mood={mood} talking={talking} size={size} />
      </div>

      {menuOpen ? (
        <div ref={menuRef} className="pet-overlay-menu" role="menu" aria-label="桌宠菜单">
          <div className="pet-overlay-menu-row">
            <button
              type="button"
              className="pet-overlay-menu-btn"
              data-active={mood === 'happy'}
              onClick={() => setMood('happy')}
            >
              开心
            </button>
            <button
              type="button"
              className="pet-overlay-menu-btn"
              data-active={mood === 'calm'}
              onClick={() => setMood('calm')}
            >
              平静
            </button>
            <button
              type="button"
              className="pet-overlay-menu-btn"
              onClick={() => setTalking((v) => !v)}
            >
              {talking ? '安静' : '说话'}
            </button>
            <button
              type="button"
              className="pet-overlay-menu-btn pet-overlay-close"
              onClick={handleClose}
            >
              关闭
            </button>
          </div>
          <div className="pet-overlay-menu-row">
            {PET_SIZES.map((s) => (
              <button
                key={s}
                type="button"
                className="pet-overlay-menu-btn"
                data-active={size === s}
                onClick={() => applySize(s)}
              >
                {PET_SIZE_LABEL[s]}
              </button>
            ))}
          </div>
        </div>
      ) : null}
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
  align-items: center;
  justify-content: center;
  padding: 14px;
  user-select: none;
  font-family: 'HarmonyOS Sans SC', 'PingFang SC', 'Microsoft YaHei', system-ui, sans-serif;
}
/* 桌宠本体：交互手势承载层（拖拽 / 点击弹菜单）。
   不用 CSS 拖拽区域标记（会吞 mousedown/click），纯 pointer 事件实现。 */
.pet-overlay-stage {
  pointer-events: auto;
  cursor: grab;
  /* 交给 pointer 事件处理，避免浏览器把按住滑动当触摸滚动吞掉 move 序列 */
  touch-action: none;
  display: flex;
  align-items: flex-end;
  justify-content: center;
}
.pet-overlay-stage:active {
  cursor: grabbing;
}
/* 弹出菜单：玻璃拟态，与既有按钮同一视觉语言；absolute 悬于本体下方居中，不占布局 */
.pet-overlay-menu {
  position: absolute;
  left: 50%;
  bottom: 12px;
  transform: translateX(-50%);
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px;
  min-width: 148px;
  border: 1px solid rgba(255, 255, 255, 0.7);
  background: rgba(255, 255, 255, 0.55);
  backdrop-filter: blur(12px) saturate(1.4);
  border-radius: 16px;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.8), 0 8px 24px rgba(255, 145, 210, 0.3);
  animation: pet-overlay-menu-in 150ms ease-out;
}
@keyframes pet-overlay-menu-in {
  from { opacity: 0; transform: translateX(-50%) translateY(4px); }
  to { opacity: 1; transform: translateX(-50%) translateY(0); }
}
.pet-overlay-menu-row {
  display: flex;
  gap: 6px;
}
.pet-overlay-menu-btn {
  pointer-events: auto;
  flex: 1;
  border: 1px solid rgba(255, 255, 255, 0.7);
  background: rgba(255, 255, 255, 0.55);
  backdrop-filter: blur(12px) saturate(1.4);
  color: #5c5c70;
  font-size: 11px;
  line-height: 1;
  padding: 7px 10px;
  border-radius: 999px;
  cursor: pointer;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.8), 0 4px 12px rgba(255, 145, 210, 0.25);
  transition: transform 150ms ease-out, background 150ms ease-out, color 150ms ease-out;
  white-space: nowrap;
}
.pet-overlay-menu-btn:hover {
  transform: translateY(-1px);
}
/* 选中态高亮：当前心情 / 当前尺寸档位 */
.pet-overlay-menu-btn[data-active='true'] {
  background: rgba(255, 145, 210, 0.65);
  border-color: rgba(255, 145, 210, 0.8);
  color: #fff;
}
.pet-overlay-menu-btn.pet-overlay-close {
  background: rgba(240, 120, 170, 0.65);
  border-color: rgba(240, 120, 170, 0.8);
  color: #fff;
}
`;
