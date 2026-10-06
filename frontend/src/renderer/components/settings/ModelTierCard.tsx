import React, { useEffect, useRef, useState } from 'react';
import { GlassCard } from '../GlassCard';
import Toggle from '../Toggle';
import {
  cancelModelDownload,
  fetchModelProgress,
  fetchSetupRecommend,
  startModelDownload,
} from '../../api';
import type { ModelProgress, ModelTierInfo } from '../../api';

/**
 * ModelTierCard — 本地模型档位管理卡（设置页，Task 9.3）。
 *
 * 能力（复用向导同款下载链路 /api/setup/model/*）：
 *  - 当前模型路径展示（后端视图 model_path 可得则展示，不可得隐藏该行）；
 *  - 四档单选（GET /api/setup/recommend 的 tiers：描述 + 约 X.XGB）；
 *  - 「下载所选档位」（POST download，body 只带 tier）+ 1s 轮询进度条；
 *  - done / failed / canceled 终态停表；下载中可取消；
 *  - 「设为本地默认大脑」开关沿用既有 local_llm.enabled 保存链路（经 props 接入）。
 */
interface ModelTierCardProps {
  /** 当前本地模型文件路径（后端视图可得则展示；空 / 缺省隐藏该行） */
  modelPath?: string;
  /** 本地默认大脑开关当前值（local_llm.enabled；未提供时不渲染开关行） */
  localEnabled?: boolean;
  /** 开关回调（沿用设置页既有 handleLocalModeChange 保存链路） */
  onLocalEnabledChange?: (next: boolean) => void;
}

/** 进度轮询间隔（毫秒）：与向导下载步骤同口径 */
const PROGRESS_POLL_MS = 1000;

/** 字节 → 人类可读体积（GB / MB） */
function formatSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 MB';
  const gb = bytes / 1024 ** 3;
  if (gb >= 1) return `${gb.toFixed(2)} GB`;
  return `${Math.max(1, Math.round(bytes / 1024 ** 2))} MB`;
}

/** 进度百分比：优先按已下载 / 总量算，总量未知时用后端给的百分比 */
function progressPercent(p: ModelProgress): number {
  if (p.total > 0) return Math.max(0, Math.min(100, (p.downloaded / p.total) * 100));
  return Math.max(0, Math.min(100, p.percent));
}

/** 档位体积展示：一位小数（约 X.XGB），与向导同口径 */
function formatTierGb(sizeGb: number | null | undefined): string {
  if (sizeGb == null || !Number.isFinite(sizeGb)) return '?';
  return sizeGb.toFixed(1);
}

/** 档位选项按钮样式：选中态主色描边 + 淡底（与向导选项同视觉） */
function tierOptionClass(active: boolean): string {
  return [
    'flex w-full flex-col gap-0.5 rounded-xl border px-3 py-2 text-left text-sm transition',
    active
      ? 'border-[var(--color-primary)] bg-[rgba(255,183,225,0.12)]'
      : 'border-[var(--glass-border)] hover:border-[var(--color-accent)]',
  ].join(' ');
}

export default function ModelTierCard({ modelPath, localEnabled, onLocalEnabledChange }: ModelTierCardProps) {
  /** 档位列表：null = 体检结论没拿到（降级提示，不阻断本卡其余部分） */
  const [tiers, setTiers] = useState<ModelTierInfo[] | null>(null);
  const [selectedTier, setSelectedTier] = useState('');
  const [progress, setProgress] = useState<ModelProgress | null>(null);
  const [starting, setStarting] = useState(false);
  const [canceled, setCanceled] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hint, setHint] = useState<string | null>(null);

  const aliveRef = useRef(true);
  const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // 卸载清理：轮询定时器必须清掉（防测试泄漏与内存泄漏）
  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
      if (pollTimerRef.current !== null) {
        clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    };
  }, []);

  // 首帧：拉体检结论拿候选档位；失败降级为「没拿到档位」提示（不阻断整卡）
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const r = await fetchSetupRecommend();
        if (!alive) return;
        const list = r?.tiers ?? [];
        setTiers(list);
        setSelectedTier(r?.recommendation?.tier || list[0]?.tier || '');
      } catch {
        if (alive) setTiers(null);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  const stopPolling = () => {
    if (pollTimerRef.current !== null) {
      clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  };

  /** 拉一次进度：done / failed / canceled 终态停表；失败保留上次进度，下个周期再试 */
  const refreshProgress = async () => {
    try {
      const p = await fetchModelProgress();
      if (!aliveRef.current) return;
      setProgress(p ?? null);
      if (p?.state === 'done') {
        stopPolling();
        setHint('下载完成，可以放心开启本地模式聊起来了');
      } else if (p && (p.state === 'failed' || p.state === 'canceled')) {
        stopPolling();
      }
    } catch {
      /* 单次查询失败不影响整体：保留上次进度，等下一次轮询 */
    }
  };

  /** 下载所选档位：入参只带 tier（模型仓库由线路在服务端派生，与向导同口径） */
  const handleStartDownload = async () => {
    if (starting) return;
    setError(null);
    setHint(null);
    setCanceled(false);
    setStarting(true);
    try {
      await startModelDownload({ tier: selectedTier || undefined });
      if (!aliveRef.current) return;
      setStarting(false);
    } catch {
      if (!aliveRef.current) return;
      setStarting(false);
      setError('下载没能启动…待会儿再试一次吧');
      return;
    }
    stopPolling();
    void refreshProgress(); // 先立即拉一次，进度马上可见
    pollTimerRef.current = setInterval(() => {
      void refreshProgress();
    }, PROGRESS_POLL_MS);
  };

  /** 取消进行中的下载（保留临时文件供续传） */
  const handleCancelDownload = async () => {
    stopPolling();
    try {
      await cancelModelDownload();
      if (!aliveRef.current) return;
      setProgress(null);
      setCanceled(true);
    } catch {
      /* 取消失败：保持现状，允许再点一次 */
    }
  };

  const pct = progress ? progressPercent(progress) : 0;
  const downloading = progress?.state === 'downloading';

  return (
    <GlassCard>
      <div className="flex flex-col gap-2 p-4">
        <p className="font-medium">本地模型档位</p>
        <p className="text-xs text-[var(--text-tertiary)]">
          挑一档下载到这台电脑，开了本地模式就能离线聊；以后随时能换
        </p>

        {/* 当前模型路径：后端视图可得才展示（不可得隐藏该行） */}
        {modelPath ? (
          <p className="break-all text-xs text-[var(--text-secondary)]">当前模型：{modelPath}</p>
        ) : null}

        {/* 四档单选 */}
        {tiers === null ? (
          <p className="text-xs text-[var(--text-tertiary)]">
            档位信息没拿到，先跳过这一项吧，待会儿再试试
          </p>
        ) : tiers.length === 0 ? (
          <p className="text-xs text-[var(--text-tertiary)]">暂时没有可下载的档位</p>
        ) : (
          <div className="flex flex-col gap-2">
            {tiers.map((t) => (
              <button
                key={t.tier}
                type="button"
                aria-pressed={selectedTier === t.tier}
                className={tierOptionClass(selectedTier === t.tier)}
                onClick={() => setSelectedTier(t.tier)}
              >
                <span>
                  {t.tier} · 约 {formatTierGb(t.approximate_size_gb)}GB
                </span>
                <span className="text-xs text-[var(--text-tertiary)]">{t.description}</span>
              </button>
            ))}
          </div>
        )}

        {/* 下载进度（1s 轮询） */}
        {downloading && (
          <div className="flex flex-col gap-1.5">
            <div className="h-2 w-full overflow-hidden rounded-full bg-[var(--bg-tertiary)]">
              <div
                className="h-full rounded-full bg-gradient-to-r from-[var(--color-secondary)] to-[var(--color-primary)] transition-all"
                style={{ width: `${pct}%` }}
              />
            </div>
            <p className="text-xs text-[var(--text-secondary)]">
              {Math.round(pct)}% · {formatSize(progress?.downloaded ?? 0)} /{' '}
              {formatSize(progress?.total ?? 0)}
            </p>
          </div>
        )}

        {progress?.state === 'failed' && (
          <p className="text-xs font-medium text-[var(--color-error)]">
            下载没成功：{progress.error || '网络好像不太顺'}
          </p>
        )}
        {canceled && (
          <p className="text-xs text-[var(--text-secondary)]">已经停下啦，随时可以重新开始下载</p>
        )}
        {error && <p className="text-xs font-medium text-[var(--color-error)]">{error}</p>}
        {hint && <p className="text-xs text-[var(--color-success)]">{hint}</p>}

        {/* 操作按钮：下载 / 取消 / 失败重试 */}
        <div className="flex flex-wrap gap-2">
          {!downloading && progress?.state !== 'done' && (
            <button
              type="button"
              onClick={() => {
                void handleStartDownload();
              }}
              disabled={starting || !selectedTier}
              className="rounded-full border border-[var(--glass-border)] px-4 py-1.5 text-sm text-[var(--text-secondary)] transition hover:text-[var(--text-primary)] disabled:cursor-not-allowed disabled:opacity-50"
            >
              {starting ? '正在开始…' : canceled ? '重新开始下载' : '下载所选档位'}
            </button>
          )}
          {downloading && (
            <button
              type="button"
              onClick={() => {
                void handleCancelDownload();
              }}
              className="rounded-full border border-[var(--glass-border)] px-4 py-1.5 text-sm text-[var(--text-secondary)] transition hover:text-[var(--text-primary)]"
            >
              取消下载
            </button>
          )}
          {progress?.state === 'failed' && (
            <button
              type="button"
              onClick={() => {
                void handleStartDownload();
              }}
              className="rounded-full border border-[var(--glass-border)] px-4 py-1.5 text-sm text-[var(--text-secondary)] transition hover:text-[var(--text-primary)]"
            >
              再试一次
            </button>
          )}
        </div>

        {/* 设为本地默认大脑：沿用既有 local_llm.enabled 状态与保存链路（props 接入） */}
        {onLocalEnabledChange && (
          <div className="mt-1 flex items-center justify-between gap-4 rounded-xl border border-[var(--glass-border)] p-3">
            <div>
              <p className="text-sm font-medium">设为本地默认大脑</p>
              <p className="text-xs text-[var(--text-tertiary)]">
                开着它就能离线聊；关掉则都交给云端
              </p>
            </div>
            <Toggle
              checked={Boolean(localEnabled)}
              onChange={onLocalEnabledChange}
              label="设为本地默认大脑"
            />
          </div>
        )}
      </div>
    </GlassCard>
  );
}
