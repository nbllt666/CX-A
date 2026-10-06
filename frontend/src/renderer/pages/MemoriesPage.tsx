import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Bot,
  Boxes,
  CalendarDays,
  CheckSquare,
  Hourglass,
  LayoutGrid,
  List,
  Plus,
  Sparkles,
  Trash2,
  X,
} from 'lucide-react';
import { MOCK_MEMORIES, MOCK_SEARCH_SUGGESTIONS } from '../mock';
import { GlassCard } from '../components/GlassCard';
import { IS_BACKEND_READY, batchDeleteMemories, distillMemory, fetchMemories, fetchSearch } from '../api';
import MemoryEditorModal from '../components/memory/MemoryEditorModal';
import MemoryDetailModal from '../components/memory/MemoryDetailModal';
import MemoryAssistantModal from '../components/memory/MemoryAssistantModal';
import DecayPanel from '../components/memory/DecayPanel';
import Search3DPanel from '../components/memory/Search3DPanel';
import DiaryView from '../components/memory/DiaryView';
import { matchLocal, mockToView, rowToView, sourceLabel, typeLabel, TYPE_LABELS, SOURCE_LABELS } from '../components/memory/view';
import type { ViewMemory } from '../components/memory/view';

/**
 * 记忆页（/memories，Task 7 改造）：
 * - 三维过滤：类型（四值）/ 来源（手动/视觉/蒸馏）/ 归属智能体
 * - 三视图：卡片 / 列表 / 日记（按日期分组）
 * - 批量模式：全选（permanent 禁选）→ 批量删除（confirm 确认）→ 刷新
 * - 新建/编辑弹窗（内容/类型/重要性/标签/agent）+ 详情弹窗（查看/编辑/删除）
 * - 遗忘衰减面板（统计 + 手动同步）与「整理记忆」（既有蒸馏端点）
 * - 三维加权检索面板（问题 + 三权重滑杆 + 结果）
 * - 记忆管理助手对话入口（agent_id='memory-agent'，复用聊天链路）
 *
 * IS_BACKEND_READY=true 时优先拉取 /api/memories 真实列表，搜索走 /api/memories/search；
 * 后端不可用（网络异常 / 非 2xx）时自动降级到本地 Mock，界面提示「离线示例数据」，
 * 写操作（新建/批量/整理）在离线态禁用。
 */

/** 页面视图模式：卡片 / 列表 / 日记 */
type ViewMode = 'cards' | 'list' | 'diary';

const OFFLINE_FALLBACK = MOCK_MEMORIES.map(mockToView);

/** 单次拉取的记忆条数上限（与后端 /api/memories?limit= 约定一致，D9） */
const MEMORY_LIMIT = 200;

export default function MemoriesPage() {
  const [keyword, setKeyword] = useState('');
  /** 数据源：online=后端真实数据，offline=降级到示例数据 */
  const [mode, setMode] = useState<'online' | 'offline'>(IS_BACKEND_READY ? 'online' : 'offline');
  const [loading, setLoading] = useState(IS_BACKEND_READY);
  /** 当前数据源的全量列表（keyword 为空时展示它） */
  const [data, setData] = useState<ViewMemory[]>(IS_BACKEND_READY ? [] : OFFLINE_FALLBACK);
  /** 在线搜索命中结果（keyword 非空且在线时使用），null 表示未搜索 / 走本地过滤 */
  const [searched, setSearched] = useState<ViewMemory[] | null>(null);

  // 三维过滤：''= 全部
  const [typeFilter, setTypeFilter] = useState('');
  const [sourceFilter, setSourceFilter] = useState('');
  const [agentFilter, setAgentFilter] = useState('');

  // 视图与批量
  const [view, setView] = useState<ViewMode>('cards');
  const [batchMode, setBatchMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [batchDeleting, setBatchDeleting] = useState(false);

  // 弹窗
  const [editorOpen, setEditorOpen] = useState(false);
  /** 编辑目标：null=新建，ViewMemory=编辑既有项 */
  const [editorTarget, setEditorTarget] = useState<ViewMemory | null>(null);
  const [detailItem, setDetailItem] = useState<ViewMemory | null>(null);
  const [assistantOpen, setAssistantOpen] = useState(false);

  // 折叠面板
  const [decayOpen, setDecayOpen] = useState(false);
  const [panel3dOpen, setPanel3dOpen] = useState(false);

  // 整理记忆（蒸馏可能较慢，禁重复点击）
  const [distilling, setDistilling] = useState(false);

  // 轻提示（成功类，蓝色）与错误提示（红色），均可手动关闭
  const [notice, setNotice] = useState('');
  const [pageError, setPageError] = useState('');

  /** 从后端重拉列表；失败降级离线示例（静默刷新，不打 loading 态） */
  const reload = useCallback(async () => {
    if (!IS_BACKEND_READY) return;
    try {
      const rows = await fetchMemories({ limit: MEMORY_LIMIT });
      setData(rows.map(rowToView));
      setMode('online');
    } catch {
      setData(OFFLINE_FALLBACK);
      setMode('offline');
    }
  }, []);

  // 初始化：拉取真实列表，失败则降级到示例数据
  useEffect(() => {
    if (!IS_BACKEND_READY) {
      setData(OFFLINE_FALLBACK);
      setMode('offline');
      setLoading(false);
      return;
    }
    let alive = true;
    (async () => {
      try {
        const rows = await fetchMemories({ limit: MEMORY_LIMIT });
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

  /** 智能体下拉选项：从当前数据收集（缺失归 default，见 rowToView） */
  const agentOptions = useMemo(() => {
    const set = new Set<string>();
    data.forEach((v) => set.add(v.agentId));
    return Array.from(set).sort();
  }, [data]);

  // 最终渲染数据：搜索结果（或全量/本地关键词过滤）之上叠加三过滤——
  // 语义命中不保证字面包含查询词，不得再用 matchLocal 二次过滤 searched；
  const base =
    searched !== null
      ? searched
      : keyword === ''
        ? data
        : data.filter((m) => matchLocal(m, keyword));
  const filtered = base.filter(
    (v) =>
      (typeFilter === '' || v.memoryType === typeFilter) &&
      (sourceFilter === '' || v.source === sourceFilter) &&
      (agentFilter === '' || v.agentId === agentFilter),
  );

  /** 可勾选项：permanent 类型禁选（后端也会跳过） */
  const selectable = filtered.filter((v) => v.memoryType !== 'permanent');
  const allSelected = selectable.length > 0 && selectable.every((v) => selectedIds.has(v.id));

  function toggleBatchMode() {
    setBatchMode((b) => !b);
    setSelectedIds(new Set());
  }

  function toggleAll() {
    setSelectedIds(allSelected ? new Set() : new Set(selectable.map((v) => v.id)));
  }

  function toggleOne(id: string) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  /** 批量删除：confirm 确认 → batchDeleteMemories → 清选择 + 刷新；skipped 提示 permanent 保护 */
  async function confirmBatchDelete() {
    const ids = Array.from(selectedIds);
    if (ids.length === 0 || batchDeleting) return;
    if (!window.confirm(`确定要删除选中的 ${ids.length} 条记忆吗？`)) return;
    setBatchDeleting(true);
    setPageError('');
    try {
      const res = await batchDeleteMemories(ids);
      const skipped = res.skipped?.length ?? 0;
      setSelectedIds(new Set());
      setNotice(
        `已删除 ${res.deleted_count ?? ids.length} 条记忆${skipped > 0 ? `，${skipped} 条永久记忆被跳过` : ''}`,
      );
      await reload();
    } catch (err) {
      setPageError(err instanceof Error ? err.message : '批量删除失败，请稍后再试');
    } finally {
      setBatchDeleting(false);
    }
  }

  /** 整理记忆（POST /api/memory/distill）：可能较慢，进行中禁重复点击 */
  async function runDistill() {
    if (distilling || mode !== 'online') return;
    setDistilling(true);
    setNotice('');
    setPageError('');
    try {
      await distillMemory();
      setNotice('整理完成，记忆已经焕然一新');
      await reload();
    } catch (err) {
      setPageError(err instanceof Error ? err.message : '整理失败，请稍后再试');
    } finally {
      setDistilling(false);
    }
  }

  /** 打开详情（批量模式下点击卡片为切换勾选，不打开详情） */
  function openDetail(item: ViewMemory) {
    if (batchMode) return;
    setDetailItem(item);
  }

  const writable = mode === 'online';

  return (
    <div className="flex h-full flex-col p-5">
      {/* 页头：标题 + 记忆管理助手 / 整理记忆 */}
      <div className="mb-4 flex items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold text-gradient">记忆</h1>
          <p className="text-sm text-[var(--text-secondary)]">
            这些都是我悄悄记住的你，随时可以回来翻看
          </p>
        </div>
        <div className="flex shrink-0 gap-2">
          <button
            type="button"
            onClick={() => setAssistantOpen(true)}
            className="flex items-center gap-1.5 rounded-xl border border-[var(--glass-border)] bg-[var(--glass-bg-strong)] px-3 py-1.5 text-sm text-[var(--text-primary)] transition hover:bg-[rgba(124,216,255,0.16)]"
          >
            <Bot className="h-4 w-4 text-[var(--color-accent)]" />
            记忆管理助手
          </button>
          <button
            type="button"
            onClick={runDistill}
            disabled={distilling || !writable}
            title={writable ? undefined : '离线示例数据暂不支持整理'}
            className="flex items-center gap-1.5 rounded-xl bg-gradient-to-r from-[var(--color-secondary)] to-[var(--color-primary)] px-3 py-1.5 text-sm text-white transition disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Sparkles className={`h-4 w-4 ${distilling ? 'animate-pulse' : ''}`} />
            {distilling ? '整理中…' : '整理记忆'}
          </button>
        </div>
      </div>

      {mode === 'offline' && (
        <div className="mb-3 rounded-xl border border-[var(--glass-border)] bg-[rgba(124,216,255,0.08)] px-3 py-2 text-xs text-[var(--text-secondary)]">
          还没连上 TA，先给你看些示例回忆
        </div>
      )}
      {notice && (
        <div className="mb-3 flex items-center justify-between rounded-xl bg-[rgba(124,216,255,0.12)] px-3 py-2 text-xs text-[var(--text-secondary)]">
          <span>{notice}</span>
          <button type="button" aria-label="关闭提示" onClick={() => setNotice('')} className="text-[var(--text-tertiary)] hover:text-[var(--text-primary)]">
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      )}
      {pageError && (
        <div role="alert" className="mb-3 flex items-center justify-between rounded-xl bg-[rgba(255,107,157,0.12)] px-3 py-2 text-xs text-[var(--color-error)]">
          <span>{pageError}</span>
          <button type="button" aria-label="关闭错误提示" onClick={() => setPageError('')} className="text-[var(--text-tertiary)] hover:text-[var(--text-primary)]">
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      )}

      {/* 工具行 1：搜索 + 三过滤 + 视图切换 */}
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <input
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          placeholder="搜一搜我们之间的回忆…"
          className="h-10 w-full max-w-xs rounded-xl border border-[var(--glass-border)] bg-[var(--glass-bg-strong)] px-3 text-sm outline-none transition focus:ring-2 focus:ring-[var(--color-accent)]"
        />
        <select
          value={typeFilter}
          onChange={(e) => setTypeFilter(e.target.value)}
          aria-label="类型筛选"
          className="h-10 rounded-xl border border-[var(--glass-border)] bg-[var(--glass-bg-strong)] px-2 text-sm text-[var(--text-primary)] outline-none transition focus:ring-2 focus:ring-[var(--color-accent)]"
        >
          <option value="">全部类型</option>
          {Object.entries(TYPE_LABELS).map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
        <select
          value={sourceFilter}
          onChange={(e) => setSourceFilter(e.target.value)}
          aria-label="来源筛选"
          className="h-10 rounded-xl border border-[var(--glass-border)] bg-[var(--glass-bg-strong)] px-2 text-sm text-[var(--text-primary)] outline-none transition focus:ring-2 focus:ring-[var(--color-accent)]"
        >
          <option value="">全部来源</option>
          {Object.entries(SOURCE_LABELS).map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
        <select
          value={agentFilter}
          onChange={(e) => setAgentFilter(e.target.value)}
          aria-label="智能体筛选"
          className="h-10 rounded-xl border border-[var(--glass-border)] bg-[var(--glass-bg-strong)] px-2 text-sm text-[var(--text-primary)] outline-none transition focus:ring-2 focus:ring-[var(--color-accent)]"
        >
          <option value="">全部智能体</option>
          {agentOptions.map((a) => (
            <option key={a} value={a}>
              {a}
            </option>
          ))}
        </select>
        <div
          role="group"
          aria-label="视图切换"
          className="ml-auto flex overflow-hidden rounded-xl border border-[var(--glass-border)]"
        >
          {(
            [
              { key: 'cards', label: '卡片', Icon: LayoutGrid },
              { key: 'list', label: '列表', Icon: List },
              { key: 'diary', label: '日记', Icon: CalendarDays },
            ] as const
          ).map(({ key, label, Icon }) => (
            <button
              key={key}
              type="button"
              aria-pressed={view === key}
              onClick={() => setView(key)}
              className={`flex items-center gap-1.5 px-3 py-2 text-sm transition ${
                view === key
                  ? 'bg-[rgba(124,216,255,0.16)] text-[var(--color-accent)]'
                  : 'text-[var(--text-secondary)] hover:bg-[var(--glass-bg-strong)]'
              }`}
            >
              <Icon className="h-4 w-4" />
              {label}
            </button>
          ))}
        </div>
      </div>

      {/* 工具行 2：新建 / 批量 / 衰减 / 三维检索 */}
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => {
            setEditorTarget(null);
            setEditorOpen(true);
          }}
          disabled={!writable}
          title={writable ? undefined : '离线示例数据暂不支持新建'}
          className="flex items-center gap-1.5 rounded-xl border border-[var(--glass-border)] bg-[var(--glass-bg-strong)] px-3 py-1.5 text-sm text-[var(--text-primary)] transition hover:bg-[rgba(124,216,255,0.16)] disabled:cursor-not-allowed disabled:opacity-50"
        >
          <Plus className="h-4 w-4" />
          新建记忆
        </button>

        {view !== 'diary' &&
          (batchMode ? (
            <>
              <label className="flex items-center gap-1.5 text-sm text-[var(--text-secondary)]">
                <input
                  type="checkbox"
                  aria-label="全选"
                  checked={allSelected}
                  onChange={toggleAll}
                  disabled={selectable.length === 0}
                  className="h-4 w-4 accent-[var(--color-accent)]"
                />
                全选
              </label>
              <button
                type="button"
                onClick={confirmBatchDelete}
                disabled={selectedIds.size === 0 || batchDeleting}
                className="flex items-center gap-1.5 rounded-xl bg-[rgba(255,107,157,0.16)] px-3 py-1.5 text-sm text-[var(--color-error)] transition hover:bg-[rgba(255,107,157,0.26)] disabled:cursor-not-allowed disabled:opacity-50"
              >
                <Trash2 className="h-4 w-4" />
                {batchDeleting ? '删除中…' : `批量删除${selectedIds.size > 0 ? `（${selectedIds.size}）` : ''}`}
              </button>
              <button
                type="button"
                onClick={toggleBatchMode}
                className="rounded-xl border border-[var(--glass-border)] px-3 py-1.5 text-sm text-[var(--text-secondary)] transition hover:bg-[var(--glass-bg-strong)]"
              >
                退出批量
              </button>
            </>
          ) : (
            <button
              type="button"
              onClick={toggleBatchMode}
              disabled={!writable}
              title={writable ? undefined : '离线示例数据暂不支持批量管理'}
              className="flex items-center gap-1.5 rounded-xl border border-[var(--glass-border)] bg-[var(--glass-bg-strong)] px-3 py-1.5 text-sm text-[var(--text-primary)] transition hover:bg-[rgba(124,216,255,0.16)] disabled:cursor-not-allowed disabled:opacity-50"
            >
              <CheckSquare className="h-4 w-4" />
              批量管理
            </button>
          ))}

        <button
          type="button"
          aria-expanded={decayOpen}
          onClick={() => setDecayOpen((o) => !o)}
          className={`ml-auto flex items-center gap-1.5 rounded-xl border border-[var(--glass-border)] px-3 py-1.5 text-sm transition ${
            decayOpen
              ? 'bg-[rgba(124,216,255,0.16)] text-[var(--color-accent)]'
              : 'bg-[var(--glass-bg-strong)] text-[var(--text-primary)] hover:bg-[rgba(124,216,255,0.16)]'
          }`}
        >
          <Hourglass className="h-4 w-4" />
          遗忘衰减
        </button>
        <button
          type="button"
          aria-expanded={panel3dOpen}
          onClick={() => setPanel3dOpen((o) => !o)}
          className={`flex items-center gap-1.5 rounded-xl border border-[var(--glass-border)] px-3 py-1.5 text-sm transition ${
            panel3dOpen
              ? 'bg-[rgba(124,216,255,0.16)] text-[var(--color-accent)]'
              : 'bg-[var(--glass-bg-strong)] text-[var(--text-primary)] hover:bg-[rgba(124,216,255,0.16)]'
          }`}
        >
          <Boxes className="h-4 w-4" />
          三维检索
        </button>
      </div>

      {/* 折叠面板：遗忘衰减 / 三维检索 */}
      {decayOpen && <DecayPanel onChanged={reload} />}
      {panel3dOpen && <Search3DPanel />}

      {/* 主体：日记视图 / 卡片 / 列表 */}
      {view === 'diary' ? (
        <DiaryView data={data} online={mode === 'online'} />
      ) : (
        <>
          {loading && <p className="mb-2 text-xs text-[var(--text-tertiary)]">正在加载记忆…</p>}
          <div
            className={
              view === 'cards'
                ? 'grid flex-1 auto-rows-min grid-cols-1 gap-3 overflow-y-auto pb-2 sm:grid-cols-2'
                : 'flex-1 space-y-2 overflow-y-auto pb-2'
            }
          >
            {filtered.map((r) =>
              view === 'cards' ? (
                <MemoryCard
                  key={r.id}
                  item={r}
                  batchMode={batchMode}
                  selected={selectedIds.has(r.id)}
                  onToggle={() => toggleOne(r.id)}
                  onOpen={() => openDetail(r)}
                />
              ) : (
                <MemoryRowItem
                  key={r.id}
                  item={r}
                  batchMode={batchMode}
                  selected={selectedIds.has(r.id)}
                  onToggle={() => toggleOne(r.id)}
                  onOpen={() => openDetail(r)}
                />
              ),
            )}
            {filtered.length === 0 && (
              <p className="col-span-full py-16 text-center text-sm text-[var(--text-tertiary)]">
                没有找到相关的回忆，换个条件试试？
              </p>
            )}
          </div>
        </>
      )}

      {/* D9 修复：拉取数达上限时显式告知，避免「硬截断无提示」 */}
      {mode === 'online' && data.length >= MEMORY_LIMIT && (
        <p className="pt-1 text-center text-xs text-[var(--text-tertiary)]">
          仅显示最近 {MEMORY_LIMIT} 条记忆
        </p>
      )}

      {/* 弹窗：新建/编辑 / 详情 / 记忆管理助手 */}
      {editorOpen && (
        <MemoryEditorModal
          item={editorTarget}
          onClose={() => setEditorOpen(false)}
          onSaved={() => {
            setEditorOpen(false);
            void reload();
          }}
        />
      )}
      {detailItem && (
        <MemoryDetailModal
          item={detailItem}
          onClose={() => setDetailItem(null)}
          onEdit={(item) => {
            setDetailItem(null);
            setEditorTarget(item);
            setEditorOpen(true);
          }}
          onDeleted={() => {
            setDetailItem(null);
            setNotice('记忆已删除');
            void reload();
          }}
        />
      )}
      {assistantOpen && <MemoryAssistantModal onClose={() => setAssistantOpen(false)} />}
    </div>
  );
}

/** 卡片/列表行公共交互 props：批量模式点击=切换勾选，浏览模式点击=打开详情 */
interface MemoryItemProps {
  item: ViewMemory;
  batchMode: boolean;
  selected: boolean;
  onToggle: () => void;
  onOpen: () => void;
}

/** 记忆卡片：批量模式头部出现勾选框（permanent 锁定禁选），浏览模式点击打开详情 */
function MemoryCard({ item, batchMode, selected, onToggle, onOpen }: MemoryItemProps) {
  const locked = item.memoryType === 'permanent';
  return (
    <GlassCard
      hoverable={!batchMode}
      className="animate-fade-up cursor-pointer"
      onClick={() => (batchMode ? (locked ? undefined : onToggle()) : onOpen())}
    >
      <div className="p-4">
        <div className="mb-1 flex items-start justify-between gap-2">
          <div className="flex items-start gap-2">
            {batchMode && (
              <input
                type="checkbox"
                aria-label={`选择 ${item.title}`}
                checked={selected}
                disabled={locked}
                onChange={() => onToggle()}
                onClick={(e) => e.stopPropagation()}
                title={locked ? '永久记忆不可批量删除' : undefined}
                className="mt-1 h-4 w-4 accent-[var(--color-accent)]"
              />
            )}
            <h3 className="font-medium text-[var(--text-primary)]">{item.title}</h3>
          </div>
          <span className="shrink-0 text-xs text-[var(--text-tertiary)]">{item.date}</span>
        </div>
        <p className="selectable text-sm leading-relaxed text-[var(--text-secondary)]">{item.summary}</p>
        <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
          <span className="rounded-full bg-[rgba(124,216,255,0.14)] px-2 py-0.5 text-xs text-[var(--text-secondary)]">
            {typeLabel(item.memoryType)}
          </span>
          <span className="rounded-full bg-[var(--glass-bg-strong)] px-2 py-0.5 text-xs text-[var(--text-tertiary)]">
            {sourceLabel(item.source)}
          </span>
          {item.tags.map((t) => (
            <span
              key={t}
              className="rounded-full bg-[rgba(255,183,225,0.16)] px-2 py-0.5 text-xs text-[var(--text-secondary)]"
            >
              #{t}
            </span>
          ))}
        </div>
        {batchMode && locked && (
          <p className="mt-1.5 text-xs text-[var(--text-tertiary)]">永久记忆不可批量删除</p>
        )}
      </div>
    </GlassCard>
  );
}

/** 列表行：单列紧凑展示（标题 + 摘要截断 + 日期 + 类型徽章） */
function MemoryRowItem({ item, batchMode, selected, onToggle, onOpen }: MemoryItemProps) {
  const locked = item.memoryType === 'permanent';
  return (
    <GlassCard
      className="animate-fade-up cursor-pointer"
      onClick={() => (batchMode ? (locked ? undefined : onToggle()) : onOpen())}
    >
      <div className="flex items-center gap-3 px-4 py-3">
        {batchMode && (
          <input
            type="checkbox"
            aria-label={`选择 ${item.title}`}
            checked={selected}
            disabled={locked}
            onChange={() => onToggle()}
            onClick={(e) => e.stopPropagation()}
            title={locked ? '永久记忆不可批量删除' : undefined}
            className="h-4 w-4 shrink-0 accent-[var(--color-accent)]"
          />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex items-center justify-between gap-2">
            <h3 className="truncate text-sm font-medium text-[var(--text-primary)]">{item.title}</h3>
            <span className="shrink-0 text-xs text-[var(--text-tertiary)]">{item.date}</span>
          </div>
          <p className="truncate text-xs text-[var(--text-secondary)]">{item.summary}</p>
        </div>
        <span className="shrink-0 rounded-full bg-[rgba(124,216,255,0.14)] px-2 py-0.5 text-xs text-[var(--text-secondary)]">
          {typeLabel(item.memoryType)}
        </span>
      </div>
    </GlassCard>
  );
}
