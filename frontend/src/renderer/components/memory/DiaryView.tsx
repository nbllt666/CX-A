import React, { useEffect, useState } from 'react';
import { CalendarDays } from 'lucide-react';
import { fetchDiary } from '../../api';
import type { DiaryGroup } from '../../api';
import { rowToView } from './view';
import type { ViewMemory } from './view';

/**
 * 日记视图（Task 7）：日期选择器 + 按日期分组的浏览模式，与卡片/列表视图并列。
 * 在线时走 GET /api/memories/diary?date=；接口失败或离线时降级为
 * 本地按 created_at 日期过滤（沿用页面列表数据）。
 */

/** 本地时区今天（toISOString 是 UTC，东八区会差一天，故本地拼装） */
export function todayStr(): string {
  const d = new Date();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${d.getFullYear()}-${m}-${day}`;
}

/** 单个日期分组的记忆列表（视图模型行） */
function GroupList({ title, items }: { title: string; items: ViewMemory[] }) {
  return (
    <section className="mb-4">
      <h3 className="mb-2 flex items-center gap-1.5 text-sm font-bold text-[var(--text-primary)]">
        <CalendarDays className="h-4 w-4 text-[var(--color-accent)]" />
        {title}
      </h3>
      <div className="space-y-2">
        {items.map((v) => (
          <div key={v.id} className="rounded-xl bg-[var(--glass-bg-strong)] px-3 py-2">
            <p className="text-sm font-medium text-[var(--text-primary)]">{v.title}</p>
            <p className="selectable mt-0.5 text-xs leading-relaxed text-[var(--text-secondary)]">
              {v.summary}
            </p>
          </div>
        ))}
      </div>
    </section>
  );
}

export default function DiaryView({
  data,
  online,
}: {
  /** 页面当前全量列表（降级过滤用） */
  data: ViewMemory[];
  /** 页面是否在线（在线才请求 diary 端点） */
  online: boolean;
}) {
  const [date, setDate] = useState(todayStr());
  const [groups, setGroups] = useState<DiaryGroup[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!online) {
      setGroups(null);
      setFailed(false);
      return;
    }
    let alive = true;
    setLoading(true);
    setFailed(false);
    fetchDiary(date)
      .then((g) => {
        if (!alive) return;
        setGroups(g);
        setLoading(false);
      })
      .catch(() => {
        if (!alive) return;
        // 接口失败：降级本地按日期过滤
        setGroups(null);
        setFailed(true);
        setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [date, online]);

  const localItems = data.filter((v) => v.date === date);

  return (
    <div className="flex-1 overflow-y-auto pb-2">
      <label className="mb-4 flex items-center gap-2 text-xs text-[var(--text-secondary)]">
        <CalendarDays className="h-4 w-4 text-[var(--color-accent)]" />
        选择日期
        <input
          type="date"
          value={date}
          onChange={(e) => setDate(e.target.value)}
          aria-label="日记日期"
          className="rounded-xl border border-[var(--glass-border)] bg-[var(--glass-bg-strong)] px-3 py-1.5 text-sm text-[var(--text-primary)] outline-none transition focus:ring-2 focus:ring-[var(--color-accent)]"
        />
      </label>

      {loading && <p className="py-8 text-center text-sm text-[var(--text-tertiary)]">正在翻这一天的记忆…</p>}

      {!loading && groups !== null && (
        <>
          {groups.length === 0 && (
            <p className="py-16 text-center text-sm text-[var(--text-tertiary)]">
              这一天还没有留下记忆
            </p>
          )}
          {groups.map((g) => (
            <GroupList key={g.date} title={g.date} items={g.memories.map(rowToView)} />
          ))}
        </>
      )}

      {!loading && (groups === null) && (
        <>
          {failed && (
            <p className="mb-3 text-xs text-[var(--text-tertiary)]">
              日记数据暂时拿不到，先按本地记录展示
            </p>
          )}
          {localItems.length === 0 ? (
            <p className="py-16 text-center text-sm text-[var(--text-tertiary)]">
              这一天还没有留下记忆
            </p>
          ) : (
            <GroupList title={date} items={localItems} />
          )}
        </>
      )}
    </div>
  );
}
