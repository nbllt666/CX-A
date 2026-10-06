import React, { useState } from 'react';
import { Star, X } from 'lucide-react';
import { createMemory, updateMemory } from '../../api';
import type { MemoryTypeValue } from '../../api';
import { MEMORY_TYPE_VALUES, parseTags } from './view';
import type { ViewMemory } from './view';

/**
 * 新建/编辑记忆弹窗（Task 7）：内容 + 类型四选 + 重要性 1-5 星级 + 标签 + 归属 agent。
 * 提交走 createMemory / updateMemory，失败显示中文错误不关闭弹窗。
 * item.raw 为 null（离线示例项）时不渲染编辑态——页面层保证只传可编辑项或 null。
 */

/** 类型四选选项（值域对齐 CX-O；permanent 附豁免说明） */
const TYPE_OPTIONS: Array<{ value: MemoryTypeValue; label: string }> = [
  { value: 'long_term', label: '长期' },
  { value: 'short_term', label: '短期' },
  { value: 'permanent', label: '永久' },
  { value: 'diary', label: '日记' },
];

export default function MemoryEditorModal({
  item,
  onClose,
  onSaved,
}: {
  /** 编辑目标视图模型；null = 新建 */
  item: ViewMemory | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const editing = item !== null;
  // 编辑时若旧值不在四值内（旧数据），回落 long_term，保证提交值合法
  const [memoryType, setMemoryType] = useState<string>(
    item && MEMORY_TYPE_VALUES.includes(item.memoryType as MemoryTypeValue)
      ? item.memoryType
      : 'long_term',
  );
  const [content, setContent] = useState(item?.summary ?? '');
  const [importance, setImportance] = useState(item?.importance ?? 3);
  const [tagsInput, setTagsInput] = useState(item?.tags.join(',') ?? '');
  const [agentId, setAgentId] = useState(item?.agentId ?? 'default');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const canSubmit = content.trim().length > 0 && !saving;

  async function submit() {
    if (!canSubmit) return;
    setSaving(true);
    setError('');
    try {
      const payload = {
        content: content.trim(),
        memory_type: memoryType as MemoryTypeValue,
        importance,
        tags: parseTags(tagsInput),
        agent_id: agentId.trim() || 'default',
      };
      if (editing && item?.raw) await updateMemory(item.raw.id, payload);
      else await createMemory(payload);
      onSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存失败，请稍后再试');
      setSaving(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      role="dialog"
      aria-modal="true"
      aria-label={editing ? '编辑记忆' : '新建记忆'}
    >
      <div className="glass-panel max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-2xl p-5">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-base font-bold text-[var(--text-primary)]">
            {editing ? '编辑记忆' : '新建记忆'}
          </h2>
          <button
            type="button"
            aria-label="关闭"
            onClick={onClose}
            className="flex h-7 w-7 items-center justify-center rounded-full text-[var(--text-tertiary)] transition hover:bg-[var(--glass-bg-strong)] hover:text-[var(--text-primary)]"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <textarea
          value={content}
          onChange={(e) => setContent(e.target.value)}
          rows={5}
          placeholder="写下想记住的事…"
          aria-label="记忆内容"
          className="mb-4 w-full resize-y rounded-xl border border-[var(--glass-border)] bg-[var(--glass-bg-strong)] px-3 py-2 text-sm text-[var(--text-primary)] outline-none transition focus:ring-2 focus:ring-[var(--color-accent)]"
        />

        <fieldset className="mb-4">
          <legend className="mb-1.5 text-xs text-[var(--text-secondary)]">记忆类型</legend>
          <div className="flex gap-2">
            {TYPE_OPTIONS.map((opt) => (
              <label
                key={opt.value}
                className="flex cursor-pointer items-center gap-1.5 rounded-full border border-[var(--glass-border)] px-3 py-1 text-xs text-[var(--text-secondary)] transition has-[:checked]:border-[var(--color-accent)] has-[:checked]:bg-[rgba(124,216,255,0.14)]"
              >
                <input
                  type="radio"
                  name="memory-type"
                  value={opt.value}
                  checked={memoryType === opt.value}
                  onChange={() => setMemoryType(opt.value)}
                  className="h-3 w-3 accent-[var(--color-accent)]"
                />
                {opt.label}
              </label>
            ))}
          </div>
          {memoryType === 'permanent' && (
            <p className="mt-1.5 text-xs text-[var(--text-tertiary)]">
              永久记忆不参与遗忘衰减，批量删除时会被自动跳过
            </p>
          )}
        </fieldset>

        <div className="mb-4">
          <span className="mb-1.5 block text-xs text-[var(--text-secondary)]">重要性（1-5）</span>
          <div className="flex gap-1">
            {[1, 2, 3, 4, 5].map((n) => (
              <button
                key={n}
                type="button"
                aria-label={`${n}星`}
                aria-pressed={importance === n}
                onClick={() => setImportance(n)}
                className={`transition ${n <= importance ? 'text-amber-400' : 'text-[var(--text-tertiary)] hover:text-amber-300'}`}
              >
                <Star className={`h-5 w-5 ${n <= importance ? 'fill-current' : ''}`} />
              </button>
            ))}
          </div>
        </div>

        <label className="mb-4 block">
          <span className="mb-1.5 block text-xs text-[var(--text-secondary)]">标签（逗号分隔）</span>
          <input
            value={tagsInput}
            onChange={(e) => setTagsInput(e.target.value)}
            placeholder="例如：旅行, 美食"
            aria-label="标签（逗号分隔）"
            className="h-9 w-full rounded-xl border border-[var(--glass-border)] bg-[var(--glass-bg-strong)] px-3 text-sm text-[var(--text-primary)] outline-none transition focus:ring-2 focus:ring-[var(--color-accent)]"
          />
        </label>

        <label className="mb-4 block">
          <span className="mb-1.5 block text-xs text-[var(--text-secondary)]">归属智能体</span>
          <input
            value={agentId}
            onChange={(e) => setAgentId(e.target.value)}
            placeholder="default"
            aria-label="归属智能体"
            className="h-9 w-full rounded-xl border border-[var(--glass-border)] bg-[var(--glass-bg-strong)] px-3 text-sm text-[var(--text-primary)] outline-none transition focus:ring-2 focus:ring-[var(--color-accent)]"
          />
        </label>

        {error && (
          <p role="alert" className="mb-3 rounded-lg bg-[rgba(255,107,157,0.12)] px-3 py-2 text-xs text-[var(--color-error)]">
            {error}
          </p>
        )}

        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-xl border border-[var(--glass-border)] px-4 py-1.5 text-sm text-[var(--text-secondary)] transition hover:bg-[var(--glass-bg-strong)]"
          >
            取消
          </button>
          <button
            type="button"
            onClick={submit}
            disabled={!canSubmit}
            className="rounded-xl bg-gradient-to-r from-[var(--color-secondary)] to-[var(--color-primary)] px-4 py-1.5 text-sm text-white transition disabled:cursor-not-allowed disabled:opacity-50"
          >
            {saving ? '保存中…' : editing ? '保存修改' : '创建记忆'}
          </button>
        </div>
      </div>
    </div>
  );
}
