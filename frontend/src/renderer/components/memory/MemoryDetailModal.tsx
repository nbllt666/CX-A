import React, { useState } from 'react';
import { Pencil, Star, Trash2, X } from 'lucide-react';
import { deleteMemory } from '../../api';
import { sourceLabel, typeLabel } from './view';
import type { ViewMemory } from './view';

/**
 * 记忆详情弹窗（Task 7）：查看全文 + 编辑入口 + 单条删除（删除前 window.confirm 确认）。
 * 离线示例项（raw 为 null）的编辑/删除按钮禁用，附引导提示。
 */
export default function MemoryDetailModal({
  item,
  onClose,
  onEdit,
  onDeleted,
}: {
  item: ViewMemory;
  onClose: () => void;
  onEdit: (item: ViewMemory) => void;
  onDeleted: () => void;
}) {
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState('');
  const writable = item.raw !== null;

  async function remove() {
    if (!writable || deleting || !item.raw) return;
    if (!window.confirm('确定要删除这条记忆吗？')) return;
    setDeleting(true);
    setError('');
    try {
      await deleteMemory(item.raw.id);
      onDeleted();
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除失败，请稍后再试');
      setDeleting(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      role="dialog"
      aria-modal="true"
      aria-label="记忆详情"
    >
      <div className="glass-panel max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-2xl p-5">
        <div className="mb-3 flex items-start justify-between gap-2">
          <h2 className="text-base font-bold text-[var(--text-primary)]">{item.title}</h2>
          <button
            type="button"
            aria-label="关闭"
            onClick={onClose}
            className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-[var(--text-tertiary)] transition hover:bg-[var(--glass-bg-strong)] hover:text-[var(--text-primary)]"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="mb-3 flex flex-wrap items-center gap-2 text-xs text-[var(--text-secondary)]">
          <span className="rounded-full bg-[rgba(124,216,255,0.14)] px-2 py-0.5">
            {typeLabel(item.memoryType)}
          </span>
          <span className="rounded-full bg-[rgba(255,183,225,0.16)] px-2 py-0.5">
            {sourceLabel(item.source)}
          </span>
          <span className="rounded-full bg-[var(--glass-bg-strong)] px-2 py-0.5">
            {item.agentId}
          </span>
          <span className="flex items-center gap-0.5 text-amber-400" aria-label={`重要性${item.importance}星`}>
            {Array.from({ length: item.importance }, (_, i) => (
              <Star key={i} className="h-3 w-3 fill-current" />
            ))}
          </span>
          <span className="text-[var(--text-tertiary)]">{item.date}</span>
        </div>

        <p className="selectable mb-4 whitespace-pre-wrap text-sm leading-relaxed text-[var(--text-secondary)]">
          {item.summary}
        </p>

        {item.tags.length > 0 && (
          <div className="mb-4 flex flex-wrap gap-1.5">
            {item.tags.map((t) => (
              <span
                key={t}
                className="rounded-full bg-[rgba(255,183,225,0.16)] px-2 py-0.5 text-xs text-[var(--text-secondary)]"
              >
                #{t}
              </span>
            ))}
          </div>
        )}

        {!writable && (
          <p className="mb-3 text-xs text-[var(--text-tertiary)]">这是离线示例数据，暂不支持编辑与删除</p>
        )}
        {error && (
          <p role="alert" className="mb-3 rounded-lg bg-[rgba(255,107,157,0.12)] px-3 py-2 text-xs text-[var(--color-error)]">
            {error}
          </p>
        )}

        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={() => onEdit(item)}
            disabled={!writable}
            className="flex items-center gap-1.5 rounded-xl border border-[var(--glass-border)] px-4 py-1.5 text-sm text-[var(--text-secondary)] transition hover:bg-[var(--glass-bg-strong)] disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Pencil className="h-3.5 w-3.5" />
            编辑
          </button>
          <button
            type="button"
            onClick={remove}
            disabled={!writable || deleting}
            className="flex items-center gap-1.5 rounded-xl bg-[rgba(255,107,157,0.16)] px-4 py-1.5 text-sm text-[var(--color-error)] transition hover:bg-[rgba(255,107,157,0.26)] disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Trash2 className="h-3.5 w-3.5" />
            {deleting ? '删除中…' : '删除'}
          </button>
        </div>
      </div>
    </div>
  );
}
