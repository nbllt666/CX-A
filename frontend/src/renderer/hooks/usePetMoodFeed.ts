import { useCallback, useEffect, useRef, useState } from 'react';
import {
  PET_MOOD_HOLD_MS,
  onPetMood,
  publishPetMood,
  readLatestPetMood,
  type PetMood,
} from '../petMood';

/**
 * 桌宠表情订阅 hook（消费端）：跨窗口接收 LLM 情绪并驱动 VRM 表情。
 *
 * 行为：
 * - 挂载时回放**保鲜期内**（8 秒）的最近一次表情——覆盖「回复先到、桌宠后开」
 *   的时序（悬浮窗未必在回复瞬间开着）；更早的不回放（表情过期属预期）；
 * - 订阅表情总线实时跟进（storage 事件只在其他窗口触发：聊天页写、悬浮窗收）；
 * - 非 calm 档 **8 秒后自动回落 calm**——表情是瞬时反应而非持久状态；期间新
 *   情绪到达则重置计时。
 *
 * pushMood（20261004 悬浮窗语音闭环）：发布 + 本窗口立即生效二合一——
 * ``publishPetMood`` 走 localStorage 总线（跨窗口 storage 事件送达，本窗口
 * 不自触发），再经 applyRef 让本窗口状态立即跟上。悬浮窗「说话」链路回复
 * 到达时调用，桌宠即时做出表情；主窗口聊天页发送链路同样可用。
 */
export function usePetMoodFeed(): { mood: PetMood; pushMood: (mood: PetMood) => void } {
  const [mood, setMood] = useState<PetMood>('calm');
  /** 最新 apply 的引用：pushMood 经它让本窗口立即生效（避免闭包过期） */
  const applyRef = useRef<(next: PetMood) => void>(() => undefined);

  useEffect(() => {
    let timer: number | undefined;

    const apply = (next: PetMood) => {
      setMood(next);
      if (timer !== undefined) window.clearTimeout(timer);
      if (next !== 'calm') {
        timer = window.setTimeout(() => setMood('calm'), PET_MOOD_HOLD_MS);
      }
    };
    applyRef.current = apply;

    // 启动回放：仅保鲜期内有效
    const latest = readLatestPetMood();
    if (latest && Date.now() - latest.at < PET_MOOD_HOLD_MS) {
      apply(latest.mood);
    }

    const off = onPetMood(apply);
    return () => {
      off();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, []);

  const pushMood = useCallback((next: PetMood) => {
    publishPetMood(next); // 跨窗口总线（其他窗口经 storage 事件到达）
    applyRef.current(next); // 本窗口立即生效（storage 不回环本窗口）
  }, []);

  return { mood, pushMood };
}
