/**
 * petDragGesture — 桌宠拖拽手势纯逻辑（对齐 CX-O src/pages/pet/dragGesture.ts）。
 *
 * 为什么用屏幕坐标（screenX/screenY）而不是 clientX/clientY：
 * 拖拽中窗口本身在被移动，clientX（相对窗口）会随窗口位移回跳，把真实指针位移
 * 部分抵消——表现就是"拖动不顺畅、窗口追不上手"。screenX/screenY 以屏幕为参照，
 * 是窗口移动的不变量，增量即真实指针位移。
 */

/** 位移超过该阈值（px）判定为拖拽而非点击 */
export const DRAG_THRESHOLD_PX = 6;

export interface DragGestureState {
  originX: number;
  originY: number;
  lastX: number;
  lastY: number;
  dragging: boolean;
}

export function createDragGestureState(x: number, y: number): DragGestureState {
  return { originX: x, originY: y, lastX: x, lastY: y, dragging: false };
}

/**
 * 推进一次拖拽状态：未过阈值前不算拖拽（位移被吸收）；过后返回本轮增量。
 * 增量取整数像素（窗口 setPosition 只接受整数坐标意义下的对齐）。
 */
export function advanceDragGesture(
  state: DragGestureState,
  x: number,
  y: number,
  threshold: number = DRAG_THRESHOLD_PX,
): { dx: number; dy: number; moved: boolean } {
  if (!state.dragging && Math.hypot(x - state.originX, y - state.originY) < threshold) {
    return { dx: 0, dy: 0, moved: false };
  }
  state.dragging = true;
  const dx = Math.round(x - state.lastX);
  const dy = Math.round(y - state.lastY);
  if (dx !== 0 || dy !== 0) {
    state.lastX = x;
    state.lastY = y;
  }
  return { dx, dy, moved: dx !== 0 || dy !== 0 };
}

/** 本次手势是否为点击（未越过拖拽阈值）：用于"点本体弹菜单" */
export function isDragGestureClick(state: DragGestureState | null): boolean {
  return !!state && !state.dragging;
}
