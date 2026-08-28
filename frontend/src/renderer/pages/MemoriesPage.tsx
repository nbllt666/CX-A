import React, { useEffect, useState } from 'react';
import { MOCK_MEMORIES, MOCK_SEARCH_SUGGESTIONS } from '../mock';
import type { MemoryItem } from '../mock';
import { GlassCard } from '../components/GlassCard';
import { IS_BACKEND_READY, fetchMemories, fetchSearch } from '../api';
import type { MemoryRow } from '../api';

/**
 * 记忆页（/memories）：记忆条目浏览。
 * IS_BACKEND_READY=true 时优先拉取 /api/memories 真实列表，搜索走 /api/memories/search；
 * 后端不可用（网络异常 / 非 2xx）时自动降级到本地 Mock，界面提示「离线示例数据」。
 */

/** 页面统一使用的记忆视图模型（后端记录与 Mock 归一化后的形状） */
interface ViewMemory {
  id: string;
  title: string;
  summary: string;
  tags: string[];
  /** yyyy-MM-dd */
  date: string;
}

/** Mock 记录 -> 视图模型（保持既有展示形状） */
function mockToView(m: MemoryItem): ViewMemory {
  return { id: m.id, title: m.title, summary: m.summary, tags: m.tags, date: m.date };
}

/** tags 字段解析：兼容 sqlite TEXT（可能是 JSON 数组字符串 / 逗号串）与真数组。
 * F-9（第三轮体检批次6）：返回前去重——后端 tags 存重复值（如 "音乐,音乐"）时
 * 渲染处 key 重复会触发 React 告警。 */
function parseTags(raw: unknown): string[] {
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

/** 后端记录 -> 视图模型：content 作摘要，首行作标题（超长截断），取日期前 10 位 */
function rowToView(r: MemoryRow): ViewMemory {
  const content = r.content ?? '';
  const firstLine = content.split('\n')[0] || '';
  const title = firstLine.length > 28 ? `${firstLine.slice(0, 28)}…` : firstLine || `记忆 #${r.id}`;
  return {
    id: String(r.id),
    title,
    summary: content,
    tags: parseTags(r.tags),
    date: typeof r.created_at === 'string' && r.created_at.length >= 10 ? r.created_at.slice(0, 10) : '',
  };
}

const OFFLINE_FALLBACK = MOCK_MEMORIES.map(mockToView);

/** 本地关键词过滤（离线 / keyword 为空 / 搜索失败时的兜底） */
function matchLocal(m: ViewMemory, keyword: string): boolean {
  return (
    m.title.includes(keyword) ||
    m.summary.includes(keyword) ||
    m.tags.some((t) => t.includes(keyword))
  );
}

export default function MemoriesPage() {
  const [keyword, setKeyword] = useState('');
  /** 数据源：online=后端真实数据，offline=降级到示例数据 */
  const [mode, setMode] = useState<'online' | 'offline'>(IS_BACKEND_READY ? 'online' : 'offline');
  const [loading, setLoading] = useState(IS_BACKEND_READY);
  /** 当前数据源的全量列表（keyword 为空时展示它） */
  const [data, setData] = useState<ViewMemory[]>(IS_BACKEND_READY ? [] : OFFLINE_FALLBACK);
  /** 在线搜索命中结果（keyword 非空且在线时使用），null 表示未搜索 / 走本地过滤 */
  const [searched, setSearched] = useState<ViewMemory[] | null>(null);

  // 初始化：拉取真实列表，失败则降级到示例数据
  useEffect(() => {
    if (!IS_BACKEND_READY) {
      setData(OFFLINE_FALLBACK);
      setMode('offline');
      return;
    }
    let alive = true;
    (async () => {
      try {
        const rows = await fetchMemories({ limit: 200 });
        if (!alive) return;
        setData(rows.map(rowToView));
        setMode('online');
      } catch {
        if (!alive) return;
        setData(OFFLINE_FALLBACK);
        setMode('offline');
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  // 在线搜索：keyword 非空且在线时调用 search 端点；关键词置空或离线时回到本地列表
  useEffect(() => {
    if (!IS_BACKEND_READY || mode !== 'online') {
      setSearched(null);
      return;
    }
    if (keyword === '') {
      setSearched(null);
      return;
    }
    let alive = true;
    // 300ms 防抖：连续输入只在停顿后触发一次检索，避免逐键打满单线程后端。
    // cleanup 同时清 timer 与 alive 标志，防止防抖窗口过期后的 stale 写入。
    const timer = setTimeout(() => {
      (async () => {
        try {
          const res = await fetchSearch(keyword, { top_k: 20 });
          if (!alive) return;
          setSearched(res.memories.map(rowToView));
        } catch {
          if (!alive) return;
          // 搜索失败：清空后端命中，退回到 data 上的本地关键词过滤
          setSearched(null);
        }
      })();
    }, 300);
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [keyword, mode]);

  // 最终渲染数据：后端语义搜索成功（searched 非 null）时直接采用其结果——
  // 语义命中不保证字面包含查询词，不得再用 matchLocal 字面匹配二次过滤（否则命中被静默清空）；
  // 仅当搜索失败 / 未搜索（searched 为 null）时回退本地兜底：keyword 为空展示全量，否则字面过滤
  const filtered =
    searched !== null
      ? searched
      : keyword === ''
        ? data
        : data.filter((m) => matchLocal(m, keyword));

  return (
    <div className="flex h-full flex-col p-5">
      <div className="mb-4">
        <h1 className="text-xl font-bold text-gradient">记忆</h1>
        <p className="text-sm text-[var(--text-secondary)]">
          这些都是我悄悄记住的你，随时可以回来翻看
        </p>
      </div>

      {mode === 'offline' && (
        <div className="mb-3 rounded-xl border border-[var(--glass-border)] bg-[rgba(124,216,255,0.08)] px-3 py-2 text-xs text-[var(--text-secondary)]">
          后端暂不可用，当前展示离线示例数据
        </div>
      )}

      <div className="mb-4">
        <input
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          placeholder="搜一搜我们之间的回忆…"
          className="h-10 w-full max-w-sm rounded-xl border border-[var(--glass-border)] bg-[var(--glass-bg-strong)] px-3 text-sm outline-none transition focus:ring-2 focus:ring-[var(--color-accent)]"
        />
        {keyword === '' && (
          <div className="mt-2 flex gap-1.5">
            {MOCK_SEARCH_SUGGESTIONS.map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => setKeyword(s)}
                className="rounded-full bg-[rgba(124,216,255,0.14)] px-2.5 py-0.5 text-xs text-[var(--text-secondary)] transition hover:bg-[rgba(124,216,255,0.24)]"
              >
                {s}
              </button>
            ))}
          </div>
        )}
      </div>

      {loading && (
        <p className="mb-2 text-xs text-[var(--text-tertiary)]">正在加载记忆…</p>
      )}

      <div className="grid flex-1 auto-rows-min grid-cols-1 gap-3 overflow-y-auto pb-2 sm:grid-cols-2">
        {filtered.map((r) => (
          <MemoryCard key={r.id} item={r} />
        ))}
        {filtered.length === 0 && (
          <p className="col-span-full py-16 text-center text-sm text-[var(--text-tertiary)]">
            没有找到相关的回忆，换个关键词试试？
          </p>
        )}
      </div>
    </div>
  );
}

function MemoryCard({ item }: { item: ViewMemory }) {
  return (
    <GlassCard hoverable className="animate-fade-up">
      <div className="p-4">
        <div className="mb-1 flex items-start justify-between gap-2">
          <h3 className="font-medium text-[var(--text-primary)]">{item.title}</h3>
          <span className="shrink-0 text-xs text-[var(--text-tertiary)]">{item.date}</span>
        </div>
        <p className="selectable text-sm leading-relaxed text-[var(--text-secondary)]">{item.summary}</p>
        <div className="mt-3 flex flex-wrap gap-1.5">
          {item.tags.map((t) => (
            <span
              key={t}
              className="rounded-full bg-[rgba(255,183,225,0.16)] px-2 py-0.5 text-xs text-[var(--text-secondary)]"
            >
              #{t}
            </span>
          ))}
        </div>
      </div>
    </GlassCard>
  );
}