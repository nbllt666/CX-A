import React, { useCallback, useEffect, useState } from 'react';
import { ChevronLeft, Moon, Play, Send, Sun } from 'lucide-react';
import {
  fetchDreamtalkHealth,
  fetchDreamtalkRecalls,
  fetchDreamtalkSession,
  postDreamtalkMessage,
  startDreamtalkSession,
  wakeDreamtalkSession,
} from '../api';
import type {
  DreamtalkHealth,
  DreamtalkMode,
  DreamtalkRecall,
  DreamtalkTurn,
  DreamtalkView,
} from '../api';

/**
 * 梦境页（梦话双 LLM 梦境，20261127 迁移自 CX-O dreamtalk）。
 *
 * 五态状态机：`setup`（入睡）/ `dreaming`（回合流）/ `wake`（摘要）/
 * `dreamless`（无梦）/ `archive`（档案）。
 *
 * 边界（与后端契约一致）：
 * - 记忆素材只能来自 CX-A 本机记忆，页面**不提供手填素材入口**；
 * - 清醒侧只拿摘要，原始回合只在梦境档案里回看；
 * - 后端不可达时入睡面板仍渲染，仅给中文降级提示条（不白屏）。
 */

type Phase = 'setup' | 'dreaming' | 'wake' | 'dreamless' | 'archive';

const CARD =
  'rounded-[var(--radius-lg)] border border-[var(--glass-border)] bg-[var(--glass-bg)] shadow backdrop-blur-[var(--glass-blur)]';

const MODE_LABELS: Record<DreamtalkMode, string> = {
  memory: '记忆梦',
  wild: '天马行空',
  dreamless: '一夜无梦',
};

const MODE_HINTS: Record<DreamtalkMode | 'auto', string> = {
  auto: '按今夜素材与情绪自动判定',
  memory: '把白天记忆的残片织进梦里',
  wild: '允许时空跳跃、身份漂移与梦中梦',
  dreamless: '安静的一夜，不做梦',
};

/** 角色着色：导演偏紫、梦者偏青、醒者偏粉 */
const SPEAKER_STYLE: Record<DreamtalkTurn['speaker'], string> = {
  director: 'border-[var(--color-secondary)] bg-[rgba(157,124,255,0.10)]',
  dreamer: 'border-[var(--color-accent)] bg-[rgba(124,216,255,0.12)]',
  awakened: 'border-[var(--color-primary)] bg-[rgba(255,123,186,0.12)]',
};

const SPEAKER_LABELS: Record<DreamtalkTurn['speaker'], string> = {
  director: '潜意识导演',
  dreamer: '梦者',
  awakened: '醒者',
};

export default function DreamPage() {
  const [phase, setPhase] = useState<Phase>('setup');
  const [mode, setMode] = useState<DreamtalkMode | 'auto'>('auto');
  const [health, setHealth] = useState<DreamtalkHealth | null>(null);
  const [view, setView] = useState<DreamtalkView | null>(null);
  const [turns, setTurns] = useState<DreamtalkTurn[]>([]);
  const [recalls, setRecalls] = useState<DreamtalkRecall[]>([]);
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [offlineHint, setOfflineHint] = useState('');

  /** 拉取状态与历史摘要：失败只降级提示，不阻断页面渲染 */
  const refresh = useCallback(async () => {
    try {
      const [nextHealth, history] = await Promise.all([
        fetchDreamtalkHealth(),
        fetchDreamtalkRecalls(8),
      ]);
      setHealth(nextHealth);
      setRecalls(history.items);
      setOfflineHint('');
    } catch {
      setHealth(null);
      setOfflineHint('暂时读不到本机记忆，梦境可能无法醒来；可直接重试。');
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const enter = useCallback((next: DreamtalkView) => {
    setView(next);
    setTurns(next.turns);
    if (next.session.status === 'ended') {
      setPhase(next.session.mode === 'dreamless' ? 'dreamless' : 'wake');
    } else {
      setPhase('dreaming');
    }
  }, []);

  /** 统一的在途提交：禁止重复点击，失败给中文提示条 */
  const submit = useCallback(
    async (task: () => Promise<void>) => {
      if (busy) return;
      setBusy(true);
      setError('');
      try {
        await task();
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        setError(message ? `梦境暂时不可用：${message}` : '梦境暂时不可用');
      } finally {
        setBusy(false);
      }
    },
    [busy],
  );

  const handleStart = () =>
    submit(async () => {
      const next = await startDreamtalkSession(mode);
      enter(next);
      void refresh();
    });

  const handleContinue = (content: string) =>
    submit(async () => {
      if (!view) return;
      const next = await postDreamtalkMessage(view.session.id, content);
      enter(next);
      setDraft('');
    });

  const handleWake = () =>
    submit(async () => {
      if (!view) return;
      const next = await wakeDreamtalkSession(view.session.id);
      enter(next);
      void refresh();
    });

  const handleArchive = (sessionId: string) =>
    submit(async () => {
      const next = await fetchDreamtalkSession(sessionId);
      enter(next);
      setPhase('archive');
    });

  const handleBackToSetup = () => {
    setPhase('setup');
    setView(null);
    setTurns([]);
    setDraft('');
    setError('');
    void refresh();
  };

  return (
    <div className="h-full overflow-y-auto px-8 py-6" data-testid="dream-page">
      <header className="mb-5 flex items-center gap-3">
        <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-[var(--color-secondary)] to-[var(--color-accent)] text-[var(--color-accent-foreground)]">
          <Moon className="h-5 w-5" aria-hidden="true" />
        </span>
        <div>
          <h1 className="text-lg font-semibold text-[var(--text-primary)]">梦境</h1>
          <p className="text-xs text-[var(--text-secondary)]">
            两个 LLM 共演的一场梦：素材只来自本机记忆，醒来只带走摘要。
          </p>
        </div>
      </header>

      {offlineHint ? <TipStrip tone="warn" text={offlineHint} testId="dream-offline" /> : null}
      {error ? <TipStrip tone="error" text={error} testId="dream-error" /> : null}

      {phase === 'setup' ? (
        <div className="grid gap-5 lg:grid-cols-[1.4fr_1fr]">
          <section className={`${CARD} p-5`}>
            <h2 className="mb-1 text-sm font-semibold text-[var(--text-primary)]">入睡</h2>
            <p className="mb-4 text-xs text-[var(--text-secondary)]">
              素材由后端在入睡时从 CX-A 记忆库只读采集一次（近 7 天、正在淡去的碎片优先，
              永久与极高重要性记忆不入梦）。页面不提供手填素材入口。
            </p>

            <div className="mb-5 grid grid-cols-2 gap-2" role="radiogroup" aria-label="梦境模式">
              {(['auto', 'memory', 'wild', 'dreamless'] as const).map((item) => (
                <button
                  key={item}
                  type="button"
                  role="radio"
                  aria-checked={mode === item}
                  disabled={busy}
                  onClick={() => setMode(item)}
                  className={[
                    'rounded-xl border px-3 py-2.5 text-left text-sm transition-all',
                    mode === item
                      ? 'border-[var(--color-secondary)] bg-[rgba(157,124,255,0.12)] text-[var(--text-primary)]'
                      : 'border-[var(--glass-border)] text-[var(--text-secondary)] hover:bg-[rgba(255,255,255,0.35)]',
                  ].join(' ')}
                >
                  <span className="block font-medium">
                    {item === 'auto' ? '自动' : MODE_LABELS[item]}
                  </span>
                  <span className="block text-xs text-[var(--text-tertiary)]">
                    {MODE_HINTS[item]}
                  </span>
                </button>
              ))}
            </div>

            <div className="flex items-center gap-3">
              <button
                type="button"
                data-testid="dream-start"
                disabled={busy}
                onClick={handleStart}
                className="inline-flex items-center gap-2 rounded-xl bg-gradient-to-r from-[var(--color-secondary)] to-[var(--color-accent)] px-4 py-2 text-sm font-medium text-[var(--color-accent-foreground)] shadow disabled:opacity-50"
              >
                <Play className="h-4 w-4" aria-hidden="true" />
                {busy ? '正在入睡…' : '开始入睡'}
              </button>
              <span className="text-xs text-[var(--text-tertiary)]">
                {health?.status === 'disabled'
                  ? '梦境已在配置中关闭'
                  : health
                    ? `契约 ${health.contract_version}${health.memory_writeback ? ' · 醒来回写记忆' : ''}`
                    : '状态读取中'}
              </span>
            </div>
          </section>

          <section className={`${CARD} p-5`}>
            <h2 className="mb-3 text-sm font-semibold text-[var(--text-primary)]">今晚的梦</h2>
            {recalls.length === 0 ? (
              <p className="text-xs text-[var(--text-secondary)]">还没有醒来的梦。</p>
            ) : (
              <ul className="flex flex-col gap-2" data-testid="dream-history">
                {recalls.map((item) => (
                  <li key={item.session_id}>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => handleArchive(item.session_id)}
                      className="w-full rounded-xl border border-[var(--glass-border)] px-3 py-2 text-left transition-all hover:bg-[rgba(255,255,255,0.35)]"
                    >
                      <span className="block text-sm text-[var(--text-primary)]">
                        {item.title || '无名之梦'}
                      </span>
                      <span className="block truncate text-xs text-[var(--text-secondary)]">
                        {item.summary}
                      </span>
                      <span className="block text-[11px] text-[var(--text-tertiary)]">
                        {MODE_LABELS[item.mode]} · {formatTime(item.created_at)}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      ) : null}

      {phase === 'dreaming' && view ? (
        <section className={`${CARD} flex h-[calc(100%-2rem)] flex-col p-5`}>
          <div className="mb-3 flex flex-wrap items-center gap-2 text-xs text-[var(--text-secondary)]">
            <span className="rounded-full bg-[rgba(157,124,255,0.14)] px-2.5 py-1 text-[var(--text-primary)]">
              {MODE_LABELS[view.session.mode]}
            </span>
            <span>{view.session.mode_reason}</span>
            <span className="text-[var(--text-tertiary)]">
              {view.session.turn_count} 回合
            </span>
          </div>

          <TurnStream turns={turns} />

          <div className="mt-4 flex flex-col gap-2">
            <div className="flex items-end gap-2">
              <textarea
                data-testid="dream-input"
                value={draft}
                disabled={busy}
                onChange={(event) => setDraft(event.target.value)}
                placeholder="醒者的一句话（留空则继续做梦）"
                rows={2}
                className="min-h-[44px] flex-1 resize-none rounded-xl border border-[var(--glass-border)] bg-[var(--glass-bg-strong)] px-3 py-2 text-sm text-[var(--text-primary)] outline-none placeholder:text-[var(--text-tertiary)]"
              />
              <button
                type="button"
                data-testid="dream-continue"
                disabled={busy}
                onClick={() => handleContinue(draft.trim())}
                className="inline-flex items-center gap-1.5 rounded-xl border border-[var(--glass-border)] px-3 py-2 text-sm text-[var(--text-primary)] hover:bg-[rgba(255,255,255,0.35)] disabled:opacity-50"
              >
                <Send className="h-4 w-4" aria-hidden="true" />
                继续
              </button>
              <button
                type="button"
                data-testid="dream-wake"
                disabled={busy}
                onClick={handleWake}
                className="inline-flex items-center gap-1.5 rounded-xl bg-gradient-to-r from-[var(--color-secondary)] to-[var(--color-accent)] px-3 py-2 text-sm font-medium text-[var(--color-accent-foreground)] shadow disabled:opacity-50"
              >
                <Sun className="h-4 w-4" aria-hidden="true" />
                醒来
              </button>
            </div>
          </div>
        </section>
      ) : null}

      {phase === 'wake' && view ? (
        <section className={`${CARD} p-6`} data-testid="dream-wake-panel">
          <p className="text-xs uppercase tracking-wide text-[var(--text-tertiary)]">醒来</p>
          <h2 className="mt-1 text-xl font-semibold text-[var(--text-primary)]">
            {view.session.title || '无名之梦'}
          </h2>
          <p className="mt-3 text-sm leading-6 text-[var(--text-secondary)]">
            {view.session.summary}
          </p>
          <Residue residue={view.session.emotion_residue} />
          <p className="mt-4 text-xs text-[var(--text-tertiary)]">
            梦的原文只留在梦境档案；清醒侧只带走这段摘要。
          </p>
          <div className="mt-5 flex flex-wrap gap-2">
            <button
              type="button"
              data-testid="dream-open-archive"
              disabled={busy}
              onClick={() => handleArchive(view.session.id)}
              className="rounded-xl border border-[var(--glass-border)] px-3.5 py-2 text-sm text-[var(--text-primary)] hover:bg-[rgba(255,255,255,0.35)]"
            >
              查看梦境档案
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={handleBackToSetup}
              className="rounded-xl bg-gradient-to-r from-[var(--color-secondary)] to-[var(--color-accent)] px-3.5 py-2 text-sm font-medium text-[var(--color-accent-foreground)] shadow"
            >
              再做一场
            </button>
          </div>
        </section>
      ) : null}

      {phase === 'dreamless' && view ? (
        <section className={`${CARD} p-6`} data-testid="dream-empty">
          <h2 className="text-lg font-semibold text-[var(--text-primary)]">今夜无梦</h2>
          <p className="mt-2 text-sm text-[var(--text-secondary)]">{view.session.mode_reason}</p>
          <p className="mt-2 text-sm text-[var(--text-tertiary)]">{view.session.summary}</p>
          <button
            type="button"
            disabled={busy}
            onClick={handleBackToSetup}
            className="mt-5 rounded-xl bg-gradient-to-r from-[var(--color-secondary)] to-[var(--color-accent)] px-3.5 py-2 text-sm font-medium text-[var(--color-accent-foreground)] shadow"
          >
            再来一晚
          </button>
        </section>
      ) : null}

      {phase === 'archive' && view ? (
        <section className={`${CARD} p-5`} data-testid="dream-archive">
          <div className="mb-4 flex items-center gap-2">
            <button
              type="button"
              onClick={() => setPhase(view.recall ? 'wake' : 'setup')}
              className="inline-flex items-center gap-1 rounded-xl border border-[var(--glass-border)] px-2.5 py-1.5 text-xs text-[var(--text-secondary)] hover:bg-[rgba(255,255,255,0.35)]"
            >
              <ChevronLeft className="h-3.5 w-3.5" aria-hidden="true" />
              返回
            </button>
            <h2 className="text-sm font-semibold text-[var(--text-primary)]">
              {view.session.title || '梦境档案'}
            </h2>
            <span className="text-xs text-[var(--text-tertiary)]">
              {MODE_LABELS[view.session.mode]} · {view.turns.length} 回合
            </span>
          </div>
          <TurnStream turns={turns} />
        </section>
      ) : null}
    </div>
  );
}

function TurnStream({ turns }: { turns: DreamtalkTurn[] }) {
  if (turns.length === 0) {
    return <p className="text-sm text-[var(--text-secondary)]">梦还没有成形。</p>;
  }
  return (
    <ol className="flex flex-1 flex-col gap-2.5 overflow-y-auto pr-1" data-testid="dream-turns">
      {turns.map((turn) => (
        <li
          key={`${turn.id || turn.seq}`}
          className={`rounded-xl border px-3.5 py-2.5 ${SPEAKER_STYLE[turn.speaker]}`}
        >
          <span className="mb-1 block text-[11px] text-[var(--text-tertiary)]">
            {SPEAKER_LABELS[turn.speaker]} · {turn.seq}
            {turn.flagged ? ' · 已标记' : ''}
          </span>
          <p className="text-sm leading-6 text-[var(--text-primary)]">{turn.content}</p>
        </li>
      ))}
    </ol>
  );
}

function Residue({ residue }: { residue: { valence?: number; arousal?: number } }) {
  const valence = typeof residue?.valence === 'number' ? residue.valence : 0;
  const arousal = typeof residue?.arousal === 'number' ? residue.arousal : 0;
  return (
    <p className="mt-3 text-xs text-[var(--text-tertiary)]">
      情绪残留：效价 {valence.toFixed(2)} · 唤醒 {arousal.toFixed(2)}
    </p>
  );
}

function TipStrip({
  text,
  tone,
  testId,
}: {
  text: string;
  tone: 'warn' | 'error';
  testId: string;
}) {
  const palette =
    tone === 'error'
      ? 'border-[var(--color-error)] text-[var(--color-error)]'
      : 'border-[var(--color-warning)] text-[var(--text-secondary)]';
  return (
    <div
      data-testid={testId}
      className={`mb-4 rounded-xl border bg-[var(--glass-bg-strong)] px-3.5 py-2 text-xs ${palette}`}
    >
      {text}
    </div>
  );
}

/** 后端时间戳（ISO 或 ``YYYY-MM-DD HH:MM:SS.ffffff``）→ 简短本地时间 */
function formatTime(value: string): string {
  const normalized = String(value || '').replace(' ', 'T');
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return value;
  return `${date.getMonth() + 1}/${date.getDate()} ${String(date.getHours()).padStart(2, '0')}:${String(
    date.getMinutes(),
  ).padStart(2, '0')}`;
}
