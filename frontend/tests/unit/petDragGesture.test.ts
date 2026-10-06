import { describe, it, expect } from 'vitest';
import {
  DRAG_THRESHOLD_PX,
  advanceDragGesture,
  createDragGestureState,
  isDragGestureClick,
} from '../../src/renderer/components/petDragGesture';

describe('petDragGesture（对齐 CX-O 拖拽手势语义）', () => {
  it('未过阈值：不判定拖拽，增量被吸收', () => {
    const state = createDragGestureState(100, 100);
    // 阈值内小幅位移（< 6px）
    const r1 = advanceDragGesture(state, 102, 101);
    expect(r1).toEqual({ dx: 0, dy: 0, moved: false });
    expect(state.dragging).toBe(false);
    expect(isDragGestureClick(state)).toBe(true);
  });

  it('越过阈值：判定拖拽并返回累计增量', () => {
    const state = createDragGestureState(100, 100);
    const r1 = advanceDragGesture(state, 110, 100); // 位移 10px ≥ 6px
    expect(r1.moved).toBe(true);
    expect(r1.dx).toBe(10);
    expect(r1.dy).toBe(0);
    expect(state.dragging).toBe(true);
    expect(isDragGestureClick(state)).toBe(false);
  });

  it('拖拽中：增量基于上次点累计（screenX/screenY 不变量语义）', () => {
    const state = createDragGestureState(100, 100);
    advanceDragGesture(state, 110, 100);
    const r = advanceDragGesture(state, 113, 104);
    expect(r).toEqual({ dx: 3, dy: 4, moved: true });
  });

  it('拖拽判定后指针回退仍算拖拽（增量可为负）', () => {
    const state = createDragGestureState(100, 100);
    advanceDragGesture(state, 120, 100);
    const r = advanceDragGesture(state, 118, 100);
    expect(r).toEqual({ dx: -2, dy: 0, moved: true });
    expect(isDragGestureClick(state)).toBe(false);
  });

  it('零位移帧：不误报 moved（节流协同）', () => {
    const state = createDragGestureState(100, 100);
    advanceDragGesture(state, 120, 100);
    const r = advanceDragGesture(state, 120, 100);
    expect(r).toEqual({ dx: 0, dy: 0, moved: false });
  });

  it('isDragGestureClick(null)：无手势状态视为非点击', () => {
    expect(isDragGestureClick(null)).toBe(false);
  });

  it('默认阈值导出且为 6px（与 CX-O 一致）', () => {
    expect(DRAG_THRESHOLD_PX).toBe(6);
    const state = createDragGestureState(0, 0);
    advanceDragGesture(state, 5, 3); // 距离 ≈5.83 < 6
    expect(state.dragging).toBe(false);
    advanceDragGesture(state, 6, 3); // 累计位移过阈值
    expect(state.dragging).toBe(true);
  });
});
