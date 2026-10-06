import type { MemoryRow } from '../../api';
import type { MemoryItem } from '../../mock';

/**
 * 记忆页共享视图模型与转换（Task 7：MemoriesPage 与 memory 子组件共用）。
 * 后端记录与 Mock 归一化后的形状；原始行保留在 raw 供编辑/删除回传。
 */

/** 页面统一使用的记忆视图模型（后端记录与 Mock 归一化后的形状） */
export interface ViewMemory {
  id: string;
  title: string;
  summary: string;
  tags: string[];
  /** yyyy-MM-dd */
  date: string;
  /** memory_type 值（四值 long_term/short_term/permanent/diary；旧数据可能是其他值，原样保留） */
  memoryType: string;
  /** 重要性 1-5（缺失/非法归 3） */
  importance: number;
  /** 归属 agent（缺失归 default） */
  agentId: string;
  /** 来源 manual/vision/distill（缺失归 manual） */
  source: string;
  /** 原始后端记录；Mock 示例项为 null（不可编辑/删除） */
  raw: MemoryRow | null;
}

/** memory_type 中文标签（四值之外的原样展示） */
export const TYPE_LABELS: Record<string, string> = {
  long_term: '长期',
  short_term: '短期',
  permanent: '永久',
  diary: '日记',
};

/** 来源中文标签 */
export const SOURCE_LABELS: Record<string, string> = {
  manual: '手动',
  vision: '视觉',
  distill: '蒸馏',
};

export function typeLabel(t: string): string {
  return TYPE_LABELS[t] ?? t;
}

export function sourceLabel(s: string): string {
  return SOURCE_LABELS[s] ?? s;
}

/** memory_type 合法四值集合（编辑弹窗提交前对齐后端值域） */
export const MEMORY_TYPE_VALUES = ['long_term', 'short_term', 'permanent', 'diary'] as const;

/** tags 字段解析：兼容 sqlite TEXT（JSON 数组串 / 逗号串）与真数组；返回前去重（F-9）。 */
export function parseTags(raw: unknown): string[] {
  if (Array.isArray(raw)) return Array.from(new Set(raw.map(String).filter(Boolean)));
  if (typeof raw === 'string') {
    const t = raw.trim();
    if (!t) return [];
    try {
      const arr = JSON.parse(t);
      if (Array.isArray(arr)) return Array.from(new Set(arr.map(String).filter(Boolean)));
    } catch {
      /* 非 JSON，走逗号切分 */
    }
    return Array.from(new Set(t.split(',').map((s) => s.trim()).filter(Boolean)));
  }
  return [];
}

/** 来源宽松解析：source / source_type 字段均可，缺失归 manual。 */
export function sourceOf(r: MemoryRow): string {
  const s = (r as { source?: unknown; source_type?: unknown }).source ??
    (r as { source_type?: unknown }).source_type;
  return typeof s === 'string' && s ? s : 'manual';
}

/** 重要性宽松解析：数字 clamp 1-5；数字字符串可解析；缺失/非法归 3。 */
function parseImportance(v: unknown): number {
  const n = typeof v === 'number' ? v : typeof v === 'string' && /^\d+(\.\d+)?$/.test(v) ? Number(v) : NaN;
  if (Number.isNaN(n)) return 3;
  return Math.min(5, Math.max(1, Math.round(n)));
}

/** 后端记录 -> 视图模型：content 首行作标题（超长截断 28），全文作摘要，日期取前 10 位。 */
export function rowToView(r: MemoryRow): ViewMemory {
  const content = r.content ?? '';
  const firstLine = content.split('\n')[0] || '';
  const title = firstLine.length > 28 ? `${firstLine.slice(0, 28)}…` : firstLine || `记忆 #${r.id}`;
  return {
    id: String(r.id),
    title,
    summary: content,
    tags: parseTags(r.tags),
    date: typeof r.created_at === 'string' && r.created_at.length >= 10 ? r.created_at.slice(0, 10) : '',
    memoryType: typeof r.type === 'string' && r.type ? r.type : 'long_term',
    importance: parseImportance(r.importance),
    agentId: typeof r.agent_id === 'string' && r.agent_id ? r.agent_id : 'default',
    source: sourceOf(r),
    raw: r,
  };
}

/** Mock 记录 -> 视图模型（保持既有展示形状；raw 为 null 表示不可写操作）。 */
export function mockToView(m: MemoryItem): ViewMemory {
  return {
    id: m.id,
    title: m.title,
    summary: m.summary,
    tags: m.tags,
    date: m.date,
    memoryType: 'long_term',
    importance: 3,
    agentId: 'default',
    source: 'manual',
    raw: null,
  };
}

/** 本地关键词过滤（离线 / keyword 为空兜底 / 搜索失败降级时使用） */
export function matchLocal(m: ViewMemory, keyword: string): boolean {
  return (
    m.title.includes(keyword) ||
    m.summary.includes(keyword) ||
    m.tags.some((t) => t.includes(keyword))
  );
}
