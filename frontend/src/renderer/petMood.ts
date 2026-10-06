/**
 * 桌宠表情档位（PetMood）——唯一真相源。
 *
 * 为什么单独成文件：该类型原先挂在 CSS 卡通形象组件 `PetAvatar.tsx` 上，
 * 而该组件已按设计对齐要求删除（见 .trae/documents/20260925_模块0_清理不合设计风格元素.md）。
 * 档位本身是**领域概念**（聊天后端会回传 mood、VRM 用它挑预设表情），
 * 不应依附于某个具体渲染实现，故独立为 `petMood.ts`，供 VrmAvatar / PetOverlay / PetPage 共用。
 *
 * 六档与后端情绪集的映射见 VrmAvatar 的 vrmExpressionForMood：
 * happy→happy、calm→neutral、sad→sad、surprised→surprised、shy→relaxed、sleepy→relaxed。
 * 后端另含 angry 档，前端无此档，一律回落 calm。
 */
export type PetMood = 'happy' | 'calm' | 'sad' | 'surprised' | 'shy' | 'sleepy';

/** 全部档位（渲染顺序无关；供白名单校验/遍历使用，避免各处重复硬编码数组） */
export const PET_MOODS: readonly PetMood[] = [
  'happy',
  'calm',
  'sad',
  'surprised',
  'shy',
  'sleepy',
];

// ---------------------------------------------------------------- 表情总线（跨窗口）
// 聊天页（主窗口）发布 LLM 情绪 → 桌宠悬浮窗（另一 BrowserWindow）消费。
// 通道与 petEnabled/petSize 同套路：localStorage + storage 事件。storage 事件
// 只在**其他**窗口触发——聊天页写、悬浮窗收，天然单向不回环。

/** 表情总线存储键（值 = JSON {mood, at}） */
export const PET_MOOD_KEY = 'cx-a.petMood';

/** 表情保鲜期（毫秒）：超窗不回放（表情是瞬时反应）；同值用于消费端自动回落 */
export const PET_MOOD_HOLD_MS = 8_000;

/** 后端情绪档 → 前端档白名单归一：angry 前端无对应档，回落 calm（见文件头注释） */
export function normalizeBackendMood(mood: string | null | undefined): PetMood {
  return (PET_MOODS as readonly string[]).includes(mood ?? '') ? (mood as PetMood) : 'calm';
}

/** 发布一次表情（聊天页调用）：带时间戳落 localStorage，供跨窗口与延迟消费 */
export function publishPetMood(mood: PetMood): void {
  try {
    window.localStorage.setItem(PET_MOOD_KEY, JSON.stringify({ mood, at: Date.now() }));
  } catch {
    /* 存储不可用（隐私模式等）时静默忽略 */
  }
}

/** 读最近一次表情；无记录或 JSON 非法返回 null */
export function readLatestPetMood(): { mood: PetMood; at: number } | null {
  try {
    const raw = window.localStorage.getItem(PET_MOOD_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { mood?: string; at?: number };
    if (typeof parsed.mood !== 'string' || typeof parsed.at !== 'number') return null;
    return { mood: normalizeBackendMood(parsed.mood), at: parsed.at };
  } catch {
    return null;
  }
}

/**
 * 订阅表情总线（跨窗口 storage 事件；同窗口写入不触发——聊天页写、悬浮窗收）。
 * 返回去订阅函数。
 */
export function onPetMood(callback: (mood: PetMood) => void): () => void {
  const handler = (e: StorageEvent) => {
    if (e.key !== PET_MOOD_KEY || !e.newValue) return;
    try {
      const parsed = JSON.parse(e.newValue) as { mood?: string };
      if (typeof parsed.mood === 'string') {
        callback(normalizeBackendMood(parsed.mood));
      }
    } catch {
      /* 非法 JSON 静默忽略 */
    }
  };
  window.addEventListener('storage', handler);
  return () => window.removeEventListener('storage', handler);
}