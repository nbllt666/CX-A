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