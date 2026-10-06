import React, { useEffect, useState } from 'react';
import { Hourglass, RefreshCw } from 'lucide-react';
import { fetchDecayStats, syncDecay } from '../../api';
import type { DecayStats } from '../../api';

/**
 * 遗忘衰减面板（Task 7）：衰减统计展示（宽松解析）+ 手动「执行衰减整理」。
 * 统计字段以后端为准：total / decaying（数组或数量）/ decayed_count 均宽松读取；
 * 同步完成后回调整页刷新列表，并重拉统计。
 */

/** 统计数字宽松读取：数字直取、数组取长度、整数字符串可解析、其余归 0 */
export function numOf(v: unknown): number {
  if (typeof v === 'number' && Number.isFinite(v)) return v;
  if (Array.isArray(v)) return v.length;
  if (typeof v === 'string' && /^\d+$/.test(v)) return Number(v);
  return 0;
}

export default function DecayPanel({
  onChanged,
}: {
  /** 衰减整理完成后通知页面刷新记忆列表 */
  onChanged: () => void;
}) {
  const [stats, setStats] = useState<DecayStats | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [syncMsg, setSyncMsg] = useState('');
  const [error, setError] = useState('');

  useEffect(() => {
    let alive = true;
    fetchDecayStats()
      .then((s) => {
        if (alive) setStats(s);
      })
      .catch(() => {
        if (alive) setLoadError(true);
      });
    return () => {
      alive = false;
    };
  }, []);

  async function runSync() {
    if (syncing || loadError) return;
    if (!window.confirm('执行衰减整理会把低重要性记忆归档，永久记忆不受影响。继续吗？')) return;
    setSyncing(true);
    setError('');
    setSyncMsg('');
    try {
      const res = await syncDecay();
      setSyncMsg(`衰减整理完成，已归档 ${numOf(res.deleted_count)} 条记忆`);
      onChanged();
      const s = await fetchDecayStats();
      setStats(s);
      setLoadError(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : '衰减整理失败，请稍后再试');
    } finally {
      setSyncing(false);
    }
  }

  return (
    <div className="glass-panel mb-4 rounded-2xl p-4">
      <div className="mb-3 flex items-center gap-2">
        <Hourglass className="h-4 w-4 text-[var(--color-accent)]" />
        <h2 className="text-sm font-bold text-[var(--text-primary)]">遗忘衰减</h2>
        <p className="text-xs text-[var(--text-tertiary)]">低重要性记忆会随时间淡去，永久记忆豁免</p>
      </div>

      {loadError ? (
        <p className="mb-3 text-xs text-[var(--text-secondary)]">衰减统计暂时拿不到，稍后再试</p>
      ) : (
        <div className="mb-3 grid grid-cols-3 gap-2 text-center">
          <div className="rounded-xl bg-[var(--glass-bg-strong)] px-2 py-2">
            <p className="text-lg font-bold text-[var(--text-primary)]" aria-label="记忆总数">
              {stats ? numOf(stats.total) : '—'}
            </p>
            <p className="text-xs text-[var(--text-tertiary)]">记忆总数</p>
          </div>
          <div className="rounded-xl bg-[var(--glass-bg-strong)] px-2 py-2">
            <p className="text-lg font-bold text-amber-400" aria-label="即将遗忘">
              {stats ? numOf(stats.decaying) : '—'}
            </p>
            <p className="text-xs text-[var(--text-tertiary)]">即将遗忘</p>
          </div>
          <div className="rounded-xl bg-[var(--glass-bg-strong)] px-2 py-2">
            <p className="text-lg font-bold text-[var(--text-secondary)]" aria-label="已衰减归档">
              {stats ? numOf(stats.decayed_count) : '—'}
            </p>
            <p className="text-xs text-[var(--text-tertiary)]">已衰减归档</p>
          </div>
        </div>
      )}

      {syncMsg && (
        <p className="mb-2 rounded-lg bg-[rgba(124,216,255,0.12)] px-3 py-2 text-xs text-[var(--text-secondary)]">
          {syncMsg}
        </p>
      )}
      {error && (
        <p role="alert" className="mb-2 rounded-lg bg-[rgba(255,107,157,0.12)] px-3 py-2 text-xs text-[var(--color-error)]">
          {error}
        </p>
      )}

      <button
        type="button"
        onClick={runSync}
        disabled={syncing || loadError}
        className="flex items-center gap-1.5 rounded-xl bg-gradient-to-r from-[var(--color-secondary)] to-[var(--color-primary)] px-4 py-1.5 text-sm text-white transition disabled:cursor-not-allowed disabled:opacity-50"
      >
        <RefreshCw className={`h-3.5 w-3.5 ${syncing ? 'animate-spin' : ''}`} />
        {syncing ? '整理中…' : '执行衰减整理'}
      </button>
    </div>
  );
}
