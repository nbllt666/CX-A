import React, { useEffect, useRef, useState } from 'react';
import { GlassCard } from '../components/GlassCard';
import Toggle from '../components/Toggle';
import BrandMark from '../components/BrandMark';
import {
  cancelModelDownload,
  completeSetup,
  fetchModelProgress,
  fetchSetupRecommend,
  fetchSetupStatus,
  startModelDownload,
} from '../api';
import type {
  AccelMode,
  AccelProfile,
  DownloadChannel,
  ModelProgress,
  SetupRecommendResult,
  SetupStatusView,
} from '../api';

/**
 * 首启向导（`#/setup` / 首次启动覆盖层）。
 *
 * 五步（单页组件内 `step` 状态机，不引入路由库；导航随推荐自适应）：
 *   ① 欢迎 + 硬件体检（可一键采纳推荐 / 自己挑 / 直接选云端大脑，推荐不可用则跳过；
 *      推荐为本地可跑时「就用推荐的」跳过云端步骤直达线路）
 *   ② 云端服务商与钥匙（本地开关已开时可「跳过，先用本地」；钥匙可留空，提示以后在设置里补）
 *   ③ 下载线路（国内 / 海外，一个问题定全部；模型仓库由线路决定）
 *   ④ 可选下载（进度 / 取消 / 失败重试 / 以后再说）
 *   ⑤ 完成（提交选择；失败不把用户卡死，可先进去用）
 *
 * 手动路径直达（Task 6）：「我自己挑」在第一步把本地开关 / 显卡 / 档位选完后，
 * 提供「完成并保存」跳过②③④直达⑤确认页，未选项落既有默认值；
 * 「就用推荐的」与「我想用云端大脑」路径不受影响。
 *
 * 文案原则：口语化、不出现技术术语；视觉沿用既有玻璃质感（GlassCard / Toggle / tokens）。
 */

/** 步骤总数（①~⑤） */
const TOTAL_STEPS = 5;
/** 进度轮询间隔（毫秒） */
const PROGRESS_POLL_MS = 1000;

/** 云端服务商（与后端白名单一致，展示中文名） */
const CLOUD_PROVIDERS: Array<{ value: string; label: string }> = [
  { value: 'deepseek', label: 'DeepSeek' },
  { value: 'tongyi', label: '通义' },
  { value: 'openai', label: 'OpenAI' },
  { value: 'moonshot', label: 'Moonshot' },
];

/**
 * 下载线路（口语化，不出现「镜像源」这类词）。
 *
 * 一个问题定全部：线路既决定下载从哪取，也决定模型从哪个站点拿
 * （国内 → 魔塔；海外 → HuggingFace），模型站点不再单独让用户选。
 */
const CHANNEL_OPTIONS: Array<{ value: DownloadChannel; label: string; desc: string }> = [
  { value: 'mirror', label: '国内线路（魔塔，推荐）', desc: '下载更快更稳，模型从国内的魔塔拿' },
  { value: 'official', label: '海外线路（HuggingFace）', desc: '直连海外站点，模型从 HuggingFace 拿' },
];

/**
 * 运行偏好（口语化，零术语）：省电优先 / 性能优先。
 * 默认选中由后端按硬件画像推导（`accel.mode`），用户可改。
 */
const ACCEL_MODE_OPTIONS: Array<{ value: AccelMode; label: string; desc: string }> = [
  { value: 'eco', label: '省电优先', desc: '日常更省电更安静，够用就好' },
  { value: 'performance', label: '性能优先', desc: '需要时火力全开，反应更快' },
];

/** 模式取值 → 口语化标签 */
function accelModeLabel(mode: AccelMode): string {
  return mode === 'eco' ? '省电优先' : '性能优先';
}

/**
 * 由加速剖面生成口语化结论（零术语，禁止出现 EP / ORT / DirectML / CUDA 等词）。
 * 后端 `accel.reasons` 含技术术语，仅作诊断，界面不直接展示。
 */
function accelSummary(accel: AccelProfile | null | undefined): string {
  if (!accel) return '会根据你的电脑自动挑一个合适的跑法';
  const backend = accel.tts?.accel;
  const device = accel.tts?.accel_device;
  if (backend === 'cuda') return '检测到独立显卡，已为语音合成开启显卡加速';
  if (backend === 'dml' && device === 'dgpu') return '检测到独立显卡，已为语音合成开启显卡加速';
  if (backend === 'dml' && device === 'igpu') return '检测到核显，已为语音合成开启省电加速';
  if (backend === 'dml') return '检测到可用显卡，已为语音合成开启显卡加速';
  if (backend === 'off') return '已关闭额外加速，语音合成用电脑本身运行';
  return '没有可用的独立显卡，语音合成先用电脑本身运行（更稳更省电）';
}

const PRIMARY_BTN =
  'inline-flex items-center justify-center rounded-full bg-gradient-to-r from-[var(--color-secondary)] to-[var(--color-primary)] px-5 py-2 text-sm font-medium text-[var(--color-primary-foreground)] transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50';
const GHOST_BTN =
  'inline-flex items-center justify-center rounded-full border border-[var(--glass-border)] px-5 py-2 text-sm text-[var(--text-secondary)] transition hover:text-[var(--text-primary)] disabled:cursor-not-allowed disabled:opacity-50';

/** 选项按钮：选中态用主色描边 + 淡底 */
function optionClass(active: boolean): string {
  return [
    'flex w-full flex-col gap-0.5 rounded-xl border px-3 py-2 text-left text-sm transition',
    active
      ? 'border-[var(--color-primary)] bg-[rgba(255,183,225,0.12)]'
      : 'border-[var(--glass-border)] hover:border-[var(--color-accent)]',
  ].join(' ');
}

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

/**
 * 档位体积展示：后端给的是实测字节数折算值（如 1.134），界面按一位小数展示（约 1.1GB）。
 * 展示与校验共用同一数据源，仅展示层做四舍五入，避免「界面数字」与「校验基准」分叉。
 */
function formatTierGb(sizeGb: number | null | undefined): string {
  if (sizeGb == null || !Number.isFinite(sizeGb)) return '?';
  return sizeGb.toFixed(1);
}

interface SetupWizardProps {
  /** 向导完成（或用户选择「先进去用」）后的回调 */
  onDone: () => void;
  /** 首帧门控时已拿到的状态；未提供时向导自行读取（读不到就用内置默认值） */
  initialStatus?: SetupStatusView | null;
}

export default function SetupWizard({ onDone, initialStatus = null }: SetupWizardProps) {
  const [step, setStep] = useState(0);

  // ① 硬件体检
  const [rec, setRec] = useState<SetupRecommendResult | null>(null);
  const [recState, setRecState] = useState<'loading' | 'ok' | 'failed'>('loading');
  const [applyRecommended, setApplyRecommended] = useState(false);
  const [manual, setManual] = useState(false);
  // 本次提交不含云端：快车道采纳推荐 / 云端步骤「跳过，先用本地」时置真；
  // 云端步骤正常「下一步」推进时清除（覆盖「快车道后回云端补钥匙」的组合）
  const [cloudSkipped, setCloudSkipped] = useState(false);

  // 选择项
  const [provider, setProvider] = useState('deepseek');
  const [apiKey, setApiKey] = useState('');
  // 线路是唯一选择来源：模型仓库由线路在服务端派生，前端不再单独持有
  const [channel, setChannel] = useState<DownloadChannel>('mirror');
  const [localEnabled, setLocalEnabled] = useState(false);
  const [device, setDevice] = useState<'cpu' | 'gpu'>('cpu');
  const [tier, setTier] = useState('');
  // 运行偏好（省电优先 / 性能优先）：默认由后端按画像推导，用户可改
  const [accelMode, setAccelMode] = useState<AccelMode>('performance');

  // ④ 下载
  const [progress, setProgress] = useState<ModelProgress | null>(null);
  const [starting, setStarting] = useState(false);
  const [dlError, setDlError] = useState<string | null>(null);
  const [canceled, setCanceled] = useState(false);

  // ⑤ 提交
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const aliveRef = useRef(true);
  const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // 卸载清理：定时器必须清掉（防测试泄漏与内存泄漏）
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

  // 首帧：对齐当前选择（默认值）+ 拉硬件体检结论
  useEffect(() => {
    let alive = true;
    (async () => {
      // 默认值：外部已给当前选择就直接用，避免重复请求
      let st: SetupStatusView | null = initialStatus ?? null;
      if (!st) {
        try {
          st = await fetchSetupStatus();
        } catch {
          st = null; // 读不到就用内置默认值，绝不阻断向导
        }
      }
      if (!alive) return;
      if (st) {
        // 只以 download.channel 为唯一选择来源；后端 local_llm_source 与通道语义一致，
        // 不作为独立选择依据（即便不一致也不据此覆盖线路）。
        if (st.download?.channel === 'official') setChannel('official');
      }

      // 硬件体检：失败只降级为「跳过推荐」，不阻断后续步骤
      try {
        const r = await fetchSetupRecommend();
        if (!alive) return;
        setRec(r ?? null);
        setRecState('ok');
        if (r?.recommendation) {
          setLocalEnabled(Boolean(r.recommendation.use_local));
          setDevice(r.recommendation.device === 'gpu' ? 'gpu' : 'cpu');
          setTier(r.recommendation.tier || r.tiers?.[0]?.tier || '');
          // 运行偏好默认值：以后端画像推导为准（顶层 accel 优先，兼容 recommendation.accel）
          const defaultMode = r.accel?.mode ?? r.recommendation.accel?.mode;
          if (defaultMode === 'eco' || defaultMode === 'performance') setAccelMode(defaultMode);
        }
      } catch {
        if (alive) setRecState('failed');
      }
    })();
    return () => {
      alive = false;
    };
  }, [initialStatus]);

  const tiers = rec?.tiers ?? [];
  const recommendation = rec?.recommendation ?? null;
  const recommendedSize = recommendation?.model?.approximate_size_gb ?? null;
  // 加速剖面（顶层优先，兼容 recommendation.accel）：用于口语化结论展示
  const accelProfile: AccelProfile | null = rec?.accel ?? recommendation?.accel ?? null;

  const stopPolling = () => {
    if (pollTimerRef.current !== null) {
      clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  };

  /** 拉一次进度：完成 / 失败 / 取消时停表；失败保留上次进度，下个周期再试 */
  const refreshProgress = async () => {
    try {
      const p = await fetchModelProgress();
      if (!aliveRef.current) return;
      setProgress(p ?? null);
      if (p && (p.state === 'done' || p.state === 'failed' || p.state === 'canceled')) {
        stopPolling();
      }
    } catch {
      /* 单次查询失败不影响整体：保留上次进度，等下一次轮询 */
    }
  };

  const handleStartDownload = async () => {
    setDlError(null);
    setCanceled(false);
    setStarting(true);
    try {
      // 只传档位：模型从哪个站点拿由所选线路在服务端决定
      await startModelDownload({ tier: tier || undefined });
      if (!aliveRef.current) return;
      setStarting(false);
    } catch {
      if (!aliveRef.current) return;
      setStarting(false);
      setDlError('下载没能启动…待会儿再试一次吧');
      return;
    }
    stopPolling();
    void refreshProgress(); // 先立即拉一次，进度马上可见
    pollTimerRef.current = setInterval(() => {
      void refreshProgress();
    }, PROGRESS_POLL_MS);
  };

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

  const handleFinish = async () => {
    setSubmitting(true);
    setSubmitError(null);
    try {
      await completeSetup({
        // 本次提交不含云端（快车道 / 「跳过，先用本地」）→ cloud 段整体省略（后端契约已支持）
        ...(cloudSkipped
          ? {}
          : { cloud: { provider, ...(apiKey ? { api_key: apiKey } : {}) } }),
        download: { channel },
        // 不提交模型仓库：由线路在服务端派生
        local_llm: { enabled: localEnabled },
        // 运行偏好：服务端据此展开各组件落点（省电优先 / 性能优先）
        accel: { mode: accelMode },
        apply_recommended: applyRecommended,
      });
      if (!aliveRef.current) return;
      setSubmitting(false);
      onDone();
    } catch {
      if (!aliveRef.current) return;
      setSubmitting(false);
      setSubmitError('没能保存上…再试一次就好啦');
    }
  };

  const goNext = () => setStep((s) => Math.min(s + 1, TOTAL_STEPS - 1));
  const goBack = () => setStep((s) => Math.max(s - 1, 0));

  const adoptRecommendation = () => {
    setApplyRecommended(true);
    setManual(false);
    if (recommendation) {
      setLocalEnabled(Boolean(recommendation.use_local));
      setDevice(recommendation.device === 'gpu' ? 'gpu' : 'cpu');
      setTier(recommendation.tier || tiers[0]?.tier || '');
    }
    if (recommendation?.use_local) {
      // 快车道：推荐本地可跑 → 跳过云端步骤直达线路步骤，本次提交不含云端
      setCloudSkipped(true);
      setStep(2);
    } else {
      goNext();
    }
  };

  /**
   * 手动路径直达完成：本地配置（本地开关 / 显卡 / 档位）选完即可跳过云端 / 线路 /
   * 下载三步，直达确认页一键提交。未选项按既有默认值补全（线路默认国内、
   * 模型仓库由线路在服务端派生）；本次提交不含云端（钥匙可在设置里补），
   * 与「跳过，先用本地」同口径。
   */
  const finishManual = () => {
    setCloudSkipped(true);
    setStep(4);
  };

  const pct = progress ? progressPercent(progress) : 0;

  return (
    <div className="app-surface flex h-full w-full items-center justify-center overflow-y-auto p-6">
      <div className="w-full max-w-2xl">
        <div className="mb-4 flex flex-col gap-2">
          <div className="flex items-center gap-2.5">
            <BrandMark size={34} />
            <span className="text-sm font-medium text-[var(--text-secondary)]">CX-A</span>
          </div>
          <h1 className="text-xl font-bold text-gradient">欢迎来到 CX-A</h1>
          <p className="text-sm text-[var(--text-secondary)]">
            花一分钟把它调成你的样子，之后随时能在设置里改
          </p>
        </div>

        {/* 步骤指示器 */}
        <div className="mb-4 flex items-center gap-2" aria-label="向导进度">
          {Array.from({ length: TOTAL_STEPS }, (_, i) => (
            <span
              key={i}
              data-active={i === step ? 'true' : 'false'}
              className={[
                'h-2 rounded-full transition-all',
                i === step
                  ? 'w-6 bg-gradient-to-r from-[var(--color-secondary)] to-[var(--color-primary)]'
                  : 'w-2 bg-[var(--gray-200)]',
              ].join(' ')}
            />
          ))}
          <span className="ml-1 text-xs text-[var(--text-tertiary)]">
            第 {step + 1} / {TOTAL_STEPS} 步
          </span>
        </div>

        <GlassCard>
          <div className="flex flex-col gap-4 p-6">
            {/* ── ① 欢迎 + 硬件体检 ── */}
            {step === 0 && (
              <>
                <StepTitle title="先看看你的电脑" desc="看它能跑得动多大的本地小模型" />
                {recState === 'loading' && (
                  <p className="text-sm text-[var(--text-secondary)]">正在看看你的电脑怎么样…</p>
                )}
                {recState === 'failed' && (
                  <>
                    <p className="text-sm text-[var(--text-secondary)]">
                      硬件体检没跑起来～可以先跳过，回头在设置里再慢慢挑
                    </p>
                    <div className="flex flex-wrap gap-2">
                      <button
                        type="button"
                        className={PRIMARY_BTN}
                        onClick={() => {
                          setApplyRecommended(false);
                          goNext();
                        }}
                      >
                        跳过推荐，直接下一步
                      </button>
                    </div>
                  </>
                )}
                {recState === 'ok' && (
                  <>
                    <p className="text-base font-medium">
                      {recommendation?.use_local
                        ? `你的电脑跑得动本地小模型，推荐下载约 ${formatTierGb(recommendedSize)}GB 的那款`
                        : '你的电脑更适合把思考交给云端，不用下载大文件'}
                    </p>
                    {recommendation && recommendation.reasons.length > 0 && (
                      <div className="flex flex-col gap-1">
                        <p className="text-xs text-[var(--text-tertiary)]">为什么这么推荐？</p>
                        <ul className="list-inside list-disc text-sm text-[var(--text-secondary)]">
                          {recommendation.reasons.map((reason, i) => (
                            <li key={i}>{reason}</li>
                          ))}
                        </ul>
                      </div>
                    )}

                    {/* 加速结论（口语化、零术语） */}
                    <p className="text-sm text-[var(--text-secondary)]">{accelSummary(accelProfile)}</p>

                    {/* 运行偏好询问（省电优先 / 性能优先）：默认按画像选中 */}
                    <div className="flex flex-col gap-1.5">
                      <p className="text-sm font-medium">平时更看重哪一点？</p>
                      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                        {ACCEL_MODE_OPTIONS.map((opt) => (
                          <button
                            key={opt.value}
                            type="button"
                            className={optionClass(accelMode === opt.value)}
                            onClick={() => setAccelMode(opt.value)}
                          >
                            <span>{opt.label}</span>
                            <span className="text-xs text-[var(--text-tertiary)]">{opt.desc}</span>
                          </button>
                        ))}
                      </div>
                    </div>

                    {!manual && (
                      <div className="flex flex-wrap gap-2">
                        <button type="button" className={PRIMARY_BTN} onClick={adoptRecommendation}>
                          就用推荐的
                        </button>
                        <button
                          type="button"
                          className={GHOST_BTN}
                          onClick={() => {
                            setApplyRecommended(false);
                            setManual(true);
                          }}
                        >
                          我自己挑
                        </button>
                        <button
                          type="button"
                          className={GHOST_BTN}
                          onClick={() => {
                            setCloudSkipped(false);
                            goNext();
                          }}
                        >
                          我想用云端大脑
                        </button>
                      </div>
                    )}

                    {manual && (
                      <div className="flex flex-col gap-3">
                        <div className="flex items-center justify-between gap-4 rounded-xl border border-[var(--glass-border)] p-3">
                          <div>
                            <p className="text-sm font-medium">开启本地小模型</p>
                            <p className="text-xs text-[var(--text-tertiary)]">
                              开着它就能离线聊；关掉则都交给云端
                            </p>
                          </div>
                          <Toggle
                            checked={localEnabled}
                            onChange={setLocalEnabled}
                            label="开启本地小模型"
                          />
                        </div>

                        <div className="flex flex-col gap-1.5">
                          <p className="text-sm font-medium">要不要用显卡帮忙？</p>
                          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                            <button
                              type="button"
                              className={optionClass(device === 'gpu')}
                              onClick={() => setDevice('gpu')}
                            >
                              <span>用显卡加速</span>
                              <span className="text-xs text-[var(--text-tertiary)]">
                                有独立显卡时更快
                              </span>
                            </button>
                            <button
                              type="button"
                              className={optionClass(device === 'cpu')}
                              onClick={() => setDevice('cpu')}
                            >
                              <span>不用显卡</span>
                              <span className="text-xs text-[var(--text-tertiary)]">
                                稳一点，不吃显存
                              </span>
                            </button>
                          </div>
                        </div>

                        {tiers.length > 0 && (
                          <div className="flex flex-col gap-1.5">
                            <p className="text-sm font-medium">选哪一档？</p>
                            <div className="flex flex-col gap-2">
                              {tiers.map((t) => (
                                <button
                                  key={t.tier}
                                  type="button"
                                  className={optionClass(tier === t.tier)}
                                  onClick={() => setTier(t.tier)}
                                >
                                  <span>
                                    {t.tier} · 约 {formatTierGb(t.approximate_size_gb)}GB
                                  </span>
                                  <span className="text-xs text-[var(--text-tertiary)]">
                                    {t.description}
                                  </span>
                                </button>
                              ))}
                            </div>
                          </div>
                        )}

                        <div className="flex flex-wrap gap-2">
                          {tier !== '' ? (
                            /* 手动配置三项（本地开关 / 显卡 / 档位）均有值 → 可直达完成；
                               「下一步」保留给想配云端大脑的用户 */
                            <>
                              <button type="button" className={PRIMARY_BTN} onClick={finishManual}>
                                完成并保存
                              </button>
                              <button type="button" className={GHOST_BTN} onClick={goNext}>
                                下一步
                              </button>
                            </>
                          ) : (
                            /* 档位尚未就绪（体检未回 / 无候选）：维持原「下一步」主路径 */
                            <button type="button" className={PRIMARY_BTN} onClick={goNext}>
                              下一步
                            </button>
                          )}
                        </div>
                      </div>
                    )}
                  </>
                )}
              </>
            )}

            {/* ── ② 云端服务商与钥匙 ── */}
            {step === 1 && (
              <>
                <StepTitle
                  title="想让它用哪个云端大脑？"
                  desc="选一个你信任的，随时能换；也可以先跳过，用本地就够"
                />
                <select
                  value={provider}
                  onChange={(e) => setProvider(e.target.value)}
                  aria-label="云端服务商"
                  className="h-9 rounded-lg border border-[var(--glass-border)] bg-[var(--bg-secondary)] px-2 text-sm outline-none transition focus:ring-2 focus:ring-[var(--color-accent)]"
                >
                  {CLOUD_PROVIDERS.map((p) => (
                    <option key={p.value} value={p.value}>
                      {p.label}
                    </option>
                  ))}
                </select>
                <div className="flex flex-col gap-1.5">
                  <label className="text-sm font-medium" htmlFor="setup-api-key">
                    把钥匙粘在这里
                  </label>
                  <input
                    id="setup-api-key"
                    type="password"
                    value={apiKey}
                    onChange={(e) => setApiKey(e.target.value)}
                    placeholder="可以留空"
                    className="h-9 rounded-lg border border-[var(--glass-border)] bg-[var(--bg-secondary)] px-2 text-sm outline-none transition focus:ring-2 focus:ring-[var(--color-accent)]"
                  />
                  <p className="text-xs text-[var(--text-tertiary)]">
                    留空也没关系，以后可以在设置里补上
                  </p>
                </div>
                <NavRow
                  onBack={goBack}
                  onNext={() => {
                    // 云端步骤正常推进 → 清除「本次不含云端」（覆盖快车道后回补钥匙的组合）
                    setCloudSkipped(false);
                    goNext();
                  }}
                />
                {localEnabled && (
                  <div className="flex flex-wrap gap-2">
                    <button
                      type="button"
                      className={GHOST_BTN}
                      onClick={() => {
                        setCloudSkipped(true);
                        setStep(2);
                      }}
                    >
                      跳过，先用本地
                    </button>
                  </div>
                )}
              </>
            )}

            {/* ── ③ 下载线路（一个问题定全部） ── */}
            {step === 2 && (
              <>
                <StepTitle
                  title="下载要用哪条线路？"
                  desc="选一条顺的，国内更快更稳"
                />
                <div className="flex flex-col gap-2">
                  {CHANNEL_OPTIONS.map((opt) => (
                    <button
                      key={opt.value}
                      type="button"
                      className={optionClass(channel === opt.value)}
                      onClick={() => setChannel(opt.value)}
                    >
                      <span>{opt.label}</span>
                      <span className="text-xs text-[var(--text-tertiary)]">{opt.desc}</span>
                    </button>
                  ))}
                </div>
                <NavRow onBack={goBack} onNext={goNext} />
              </>
            )}

            {/* ── ④ 可选下载 ── */}
            {step === 3 && (
              <>
                <StepTitle
                  title="要不要现在就把本地小模型请回家？"
                  desc="不下载也能用，只是聊的时候都得连云端"
                />

                {applyRecommended ? (
                  // 已采纳推荐：档位已按推荐预设，不再让用户重复挑
                  <p className="text-sm text-[var(--text-secondary)]">
                    就用推荐的那款
                    {recommendedSize != null ? ` · 约 ${formatTierGb(recommendedSize)}GB` : ''}
                    ，不用再挑啦
                  </p>
                ) : tiers.length > 0 ? (
                  <div className="flex flex-col gap-2">
                    {tiers.map((t) => (
                      <button
                        key={t.tier}
                        type="button"
                        className={optionClass(tier === t.tier)}
                        onClick={() => setTier(t.tier)}
                      >
                        <span>
                          {t.tier} · 约 {formatTierGb(t.approximate_size_gb)}GB
                        </span>
                        <span className="text-xs text-[var(--text-tertiary)]">{t.description}</span>
                      </button>
                    ))}
                  </div>
                ) : (
                  <p className="text-sm text-[var(--text-secondary)]">
                    档位信息没拿到，先跳过这一步吧，回头在设置里也能下
                  </p>
                )}

                {dlError && <p className="text-xs font-medium text-[var(--color-error)]">{dlError}</p>}

                {progress?.state === 'downloading' && (
                  <div className="flex flex-col gap-1.5">
                    <div className="h-2 w-full overflow-hidden rounded-full bg-[var(--bg-tertiary)]">
                      <div
                        className="h-full rounded-full bg-gradient-to-r from-[var(--color-secondary)] to-[var(--color-primary)] transition-all"
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                    <p className="text-xs text-[var(--text-secondary)]">
                      {Math.round(pct)}% · {formatSize(progress.downloaded)} / {formatSize(progress.total)}
                    </p>
                    <div>
                      <button type="button" className={GHOST_BTN} onClick={() => void handleCancelDownload()}>
                        取消下载
                      </button>
                    </div>
                  </div>
                )}

                {progress?.state === 'done' && (
                  <p className="text-sm text-[var(--color-success)]">下载好啦～可以放心开聊了</p>
                )}

                {progress?.state === 'failed' && (
                  <div className="flex flex-col gap-2">
                    <p className="text-sm text-[var(--color-error)]">
                      下载没成功：{progress.error || '网络好像不太顺'}
                    </p>
                    <div>
                      <button type="button" className={GHOST_BTN} onClick={() => void handleStartDownload()}>
                        再试一次
                      </button>
                    </div>
                  </div>
                )}

                {canceled && (
                  <p className="text-sm text-[var(--text-secondary)]">
                    已经停下啦，随时可以重新开始下载
                  </p>
                )}

                <div className="flex flex-wrap gap-2">
                  {progress?.state === 'done' ? (
                    // 下载完成：正确动作是去确认页，不再显示「以后再说」（避免完成态语义错位）
                    <button type="button" className={PRIMARY_BTN} onClick={goNext}>
                      下一步
                    </button>
                  ) : (
                    (progress?.state !== 'downloading') && (
                      <button
                        type="button"
                        className={PRIMARY_BTN}
                        disabled={starting || tiers.length === 0}
                        onClick={() => void handleStartDownload()}
                      >
                        {starting ? '正在开始…' : canceled ? '重新开始下载' : '现在下载'}
                      </button>
                    )
                  )}
                  {progress?.state !== 'done' && (
                    <button type="button" className={GHOST_BTN} onClick={goNext}>
                      以后再说
                    </button>
                  )}
                  <button type="button" className={GHOST_BTN} onClick={goBack}>
                    上一步
                  </button>
                </div>
              </>
            )}

            {/* ── ⑤ 完成 ── */}
            {step === 4 && (
              <>
                <StepTitle title="快好了，确认一下" desc="点下去就按这些设置开始用" />
                <ul className="flex flex-col gap-1 text-sm text-[var(--text-secondary)]">
                  <li>
                    云端大脑：
                    {cloudSkipped
                      ? '先不用云端（本地就能聊，以后在设置里补）'
                      : CLOUD_PROVIDERS.find((p) => p.value === provider)?.label ?? provider}
                  </li>
                  <li>钥匙：{apiKey ? '已经填好' : '先空着，回头在设置里补'}</li>
                  <li>
                    下载线路：{channel === 'mirror' ? '国内线路（魔塔）' : '海外线路（HuggingFace）'}
                  </li>
                  <li>本地小模型：{localEnabled ? '开着' : '先关着'}</li>
                  <li>运行偏好：{accelModeLabel(accelMode)}</li>
                </ul>

                {submitError && (
                  <p className="text-xs font-medium text-[var(--color-error)]">{submitError}</p>
                )}

                <div className="flex flex-wrap gap-2">
                  <button
                    type="button"
                    className={PRIMARY_BTN}
                    disabled={submitting}
                    onClick={() => void handleFinish()}
                  >
                    {submitting ? '正在保存…' : '开始聊天'}
                  </button>
                  <button type="button" className={GHOST_BTN} onClick={onDone}>
                    稍后再说，先进去用
                  </button>
                  <button type="button" className={GHOST_BTN} onClick={goBack} disabled={submitting}>
                    上一步
                  </button>
                </div>
              </>
            )}
          </div>
        </GlassCard>
      </div>
    </div>
  );
}

function StepTitle({ title, desc }: { title: string; desc: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <h2 className="text-base font-semibold">{title}</h2>
      <p className="text-xs text-[var(--text-tertiary)]">{desc}</p>
    </div>
  );
}

function NavRow({ onBack, onNext }: { onBack: () => void; onNext: () => void }) {
  return (
    <div className="flex flex-wrap gap-2">
      <button type="button" className={PRIMARY_BTN} onClick={onNext}>
        下一步
      </button>
      <button type="button" className={GHOST_BTN} onClick={onBack}>
        上一步
      </button>
    </div>
  );
}
