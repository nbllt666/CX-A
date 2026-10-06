import React, { useState } from 'react';
import { Boxes, Search } from 'lucide-react';
import { search3d } from '../../api';
import type { MemoryRow } from '../../api';

/**
 * 3D 加权检索面板（Task 7）：问题输入 + 重要性/时间/相关性三条权重滑杆（0-1，
 * 默认均分 0.33）+ 检索结果列表。与顶部普通搜索并存，结果展示在本面板内。
 */

/** 三维权重默认均分 */
const W_DEFAULT = 0.33;

/** 权重滑杆行 */
function WeightSlider({
  label,
  value,
  onChange,
}: {
  label: string;
  value: number;
  onChange: (v: number) => void;
}) {
  return (
    <label className="flex items-center gap-2 text-xs text-[var(--text-secondary)]">
      <span className="w-14 shrink-0">{label}</span>
      <input
        type="range"
        min={0}
        max={1}
        step={0.05}
        value={value}
        aria-label={label}
        onChange={(e) => onChange(Number(e.target.value))}
        className="h-1.5 flex-1 accent-[var(--color-accent)]"
      />
      <span className="w-8 shrink-0 text-right tabular-nums text-[var(--text-tertiary)]">
        {value.toFixed(2)}
      </span>
    </label>
  );
}

export default function Search3DPanel() {
  const [query, setQuery] = useState('');
  const [wImp, setWImp] = useState(W_DEFAULT);
  const [wTime, setWTime] = useState(W_DEFAULT);
  const [wRel, setWRel] = useState(W_DEFAULT);
  const [results, setResults] = useState<MemoryRow[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState('');

  async function run() {
    const q = query.trim();
    if (!q || searching) return;
    setSearching(true);
    setError('');
    try {
      setResults(await search3d(q, { w_importance: wImp, w_time: wTime, w_rel: wRel }));
    } catch {
      // 检索失败：清空旧结果，给出中文提示（保留 Mock 降级由页面列表承担）
      setResults(null);
      setError('三维检索暂时不可用，请稍后再试');
    } finally {
      setSearching(false);
    }
  }

  return (
    <div className="glass-panel mb-4 rounded-2xl p-4">
      <div className="mb-3 flex items-center gap-2">
        <Boxes className="h-4 w-4 text-[var(--color-accent)]" />
        <h2 className="text-sm font-bold text-[var(--text-primary)]">三维检索</h2>
        <p className="text-xs text-[var(--text-tertiary)]">按重要性、时间与相关性加权排序回忆</p>
      </div>

      <div className="mb-3 flex gap-2">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.nativeEvent.isComposing) run();
          }}
          placeholder="想找点什么？"
          aria-label="三维检索问题"
          className="h-9 flex-1 rounded-xl border border-[var(--glass-border)] bg-[var(--glass-bg-strong)] px-3 text-sm text-[var(--text-primary)] outline-none transition focus:ring-2 focus:ring-[var(--color-accent)]"
        />
        <button
          type="button"
          onClick={run}
          disabled={searching || query.trim() === ''}
          className="flex items-center gap-1.5 rounded-xl bg-gradient-to-r from-[var(--color-secondary)] to-[var(--color-primary)] px-4 py-1.5 text-sm text-white transition disabled:cursor-not-allowed disabled:opacity-50"
        >
          <Search className="h-3.5 w-3.5" />
          {searching ? '检索中…' : '开始检索'}
        </button>
      </div>

      <div className="mb-3 space-y-2">
        <WeightSlider label="重要性权重" value={wImp} onChange={setWImp} />
        <WeightSlider label="时间权重" value={wTime} onChange={setWTime} />
        <WeightSlider label="相关性权重" value={wRel} onChange={setWRel} />
      </div>

      {error && (
        <p role="alert" className="mb-2 rounded-lg bg-[rgba(255,107,157,0.12)] px-3 py-2 text-xs text-[var(--color-error)]">
          {error}
        </p>
      )}

      {results !== null && (
        <div className="space-y-2">
          {results.length === 0 && (
            <p className="py-4 text-center text-sm text-[var(--text-tertiary)]">
              没有找到相关记忆，试试调整权重换个问法？
            </p>
          )}
          {results.map((r) => {
            const content = r.content ?? '';
            const firstLine = content.split('\n')[0] || `记忆 #${r.id}`;
            const date =
              typeof r.created_at === 'string' && r.created_at.length >= 10
                ? r.created_at.slice(0, 10)
                : '';
            return (
              <div
                key={r.id}
                className="flex items-start justify-between gap-3 rounded-xl bg-[var(--glass-bg-strong)] px-3 py-2"
              >
                <p className="selectable text-sm text-[var(--text-primary)]">{firstLine}</p>
                <span className="shrink-0 text-xs text-[var(--text-tertiary)]">{date}</span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
