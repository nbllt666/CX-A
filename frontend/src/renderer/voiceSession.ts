/**
 * 语音会话状态机（20261006 全双工语音降级版）。
 *
 * 两种模式共用同一会话框架（麦克风按钮 = 会话总开关）：
 * - **vad（传统·自动断句）**：能量 VAD 断句 → 整段识别 → 发送 → 播放回复；
 *   播放期间不做打断检测（单轮交互，说完即听），播放期间到达的语音段丢弃。
 * - **duplex（全双工降级）**：断句识别后按标点符号切句（splitSentences），
 *   句子入 FIFO 队列**逐句轮询 LLM**（串行发送：回复到手即视为已入上下文，
 *   下一句即可发出；TTS 播放与下一句请求重叠）；**AI 播放期间能量检测持续，
 *   用户开口（高门限 + 300ms 保护窗）→ 打断**：停播、清队、丢弃采集缓冲
 *   （AI 扬声器残留不得进入识别链路，无 AEC 的缓解见 spec）。
 *   SEND 确认闸门（20261106 并行慢速 VAD 判停）：话轮判定 SEND 后查采集层
 *   实时静音（getSilenceSec），未达确认阈值（confirmSilenceSec，默认 1.0s）则
 *   挂起并启动补足确认定时器（开口即清 / 新语音段合并 / stop 清除）——排除
 *   换气、犹豫停顿被快 VAD + 标点快筛误判成句的抢话。
 *
 * 上下文延续：/api/chat/message 的会话历史由后端持有，逐句串行发送即自然
 * 延续上下文（前端不重复维护 messages 数组）。
 *
 * 可测性：transcribe / chat / speak / 采集工厂全部依赖注入，状态机纯逻辑
 * 单测覆盖（见 tests/unit/voicesession.test.ts）。
 */

import {
  startContinuousRecording,
  type ContinuousRecordingHandlers,
  type ContinuousRecordingSession,
} from './audioRecorder';
import { splitSentences } from './sentences';
import { StreamTextPipeline } from './streamText';

export type VoiceInteractionMode = 'vad' | 'duplex';

/**
 * 会话状态：idle 未开启 / listening 聆听 / processing 识别或 LLM 处理中 /
 * speaking 播放回复中。初始恒为 idle（start 成功 → listening 触发通知）。
 */
export type VoiceSessionState = 'idle' | 'listening' | 'processing' | 'speaking';

/** 打断检测参数（duplex 播放期防自触发）。 */
export interface InterruptOptions {
  /** 播放期打断能量门限（RMS；应显著高于普通聆听门限，防扬声器回采误触）。默认 0.09 */
  threshold?: number;
  /** 保护窗（毫秒）：能量需连续超门限达到该时长才触发打断。默认 300 */
  guardMs?: number;
}

export interface VoiceSessionDeps {
  /** 语音交互模式（会话启动时冻结；运行中切模式经 stop/start 生效）。 */
  mode: VoiceInteractionMode;
  /** 识别一段语音（POST /api/voice/transcribe）。 */
  transcribe: (audioBase64: string, sampleRate: number) => Promise<{ ok?: boolean; text?: string }>;
  /** 发送一句话（POST /api/chat/message；后端持会话历史）。 */
  chat: (message: string, opts?: { visual?: boolean }) => Promise<{ clean_text?: string; mood?: string } | undefined>;
  /**
   * 流式发送一句话（POST /api/chat/message_stream；20261010 语音低延迟）：
   * onDelta 逐帧回调 raw 增量（含 [emotion:x] 标签原文），Promise 在 done 帧
   * resolve（权威 clean_text/mood）。注入后 processSentence 走流式句级流水线
   * ——LLM 生成出首个完整句即送 TTS，不等完整回复；缺省回退 chat 旧路径。
   * signal 为打断取消信号（打断时同步中止流式请求）。
   * opts.visual（20261011 视觉语音融合）：本句携带开口瞬间截图（后端消费
   * pending 图，命中预填充 KV 前缀）。
   */
  chatStream?: (
    message: string,
    onDelta: (delta: string) => void,
    signal: AbortSignal,
    opts?: { visual?: boolean },
  ) => Promise<{ clean_text?: string; mood?: string; offline?: boolean; ok?: boolean; incomplete?: boolean; error?: string } | undefined>;
  /**
   * 用户开口瞬间（20261011 视觉语音融合）：会话层触发截图预填充
   * （fire-and-forget POST /api/voice/visual_prefill），正式请求命中图像 KV。
   */
  onSpeechStart?: () => void;
  /**
   * 话轮判定（20261010 主模型判定模式，对齐 CX-O agent_interrupt_user 语义；
   * 20261107 扩展五枚举）：判断 ASR 文本——SEND 完整一句（发送回复）/
   * CONTINUE 还在说（挂起合并）/ INTERRUPT 该 AI 插话打断用户（立即发送）/
   * IGNORE 无意义（丢弃本段，池子保留）/ SILENT 说完了但无需回复（整轮作废）。
   * 注入后 duplex 模式对**无终端标点**的识别文本先判定再发送；
   * 未注入或判定异常一律 SEND（宁误发不吞话）。
   */
  turnCheck?: (
    text: string,
  ) => Promise<'CONTINUE' | 'SEND' | 'IGNORE' | 'INTERRUPT' | 'SILENT'>;
  /** CONTINUE 后没有新语音时，最多等待的毫秒数。默认 2000。 */
  turnWaitMs?: number;
  /**
   * 实时连续静音秒数查询（20261106 并行慢速 VAD 判停，duplex SEND 确认闸门用）：
   * 采集层 ContinuousRecordingSession.getSilenceSec 透传注入（语音态=0，静音态=
   * 自最近超门限块起算的连续静音秒数）。未注入时闸门直通（现状行为，测试/降级
   * 环境兼容）。
   */
  getSilenceSec?: () => number;
  /**
   * SEND 确认阈值（秒）。默认 1.0；≤0 视为禁用闸门直通；>10 clamp 至 10
   * （防配置错误永久沉默）。不进 config/设置页（最小实现，调参留后续）。
   */
  confirmSilenceSec?: number;
  /**
   * CONTINUE 长静音兜底窗口（秒，20261107）。默认 3.0；≤0 视为禁用兜底；
   * >30 clamp 至 30。CONTINUE 挂起后静音持续达此值照发挂起池（VAD 证据驱动，
   * 解决 LLM 误判 CONTINUE 的悬挂：用户说完 AI 不应答）。不进 config/设置页。
   */
  continueSilenceSec?: number;
  /**
   * 流式合成并播放一段回复；返回句柄——abort() 停播停合成（打断用）、
   * done 在播放自然结束后 resolve（打断时 reject 或挂起均可，会话不再等待）。
   */
  speak: (text: string) => { abort: () => void; done: Promise<void> };
  /** 表情/聊天刷新回调（mood 来自 chat 回复；缺省可省）。 */
  onReplyMeta?: (mood: string | undefined, complete?: boolean) => void;
  /** 状态变化回调（UI 按钮/口型接线用）。 */
  onStateChange?: (state: VoiceSessionState) => void;
  /** 采集错误回调（权限被拒等；缺省仅 console）。 */
  onError?: (err: Error) => void;
  /** 采集工厂（默认 startContinuousRecording；测试注入 fake）。 */
  createRecorder?: (
    handlers: ContinuousRecordingHandlers,
  ) => Promise<ContinuousRecordingSession>;
  /** 普通聆听门限/断句参数（透传采集层；默认 0.02 / 0.7s）。 */
  vad?: { speechThreshold?: number; silenceTimeoutSec?: number };
  /** 播放期打断检测参数（仅 duplex 生效）。 */
  interrupt?: InterruptOptions;
}

/** 打断默认参数 */
const INTERRUPT_THRESHOLD_DEFAULT = 0.09;
const INTERRUPT_GUARD_MS_DEFAULT = 300;

/** SEND 确认闸门默认阈值（秒）与 clamp 上限（20261106 并行慢速 VAD 判停） */
const CONFIRM_SILENCE_DEFAULT = 1.0;
const CONFIRM_SILENCE_MAX = 10;
/** 补足确认定时器时长下限（毫秒）：静音仅差毫厘时不做 0~数 ms 的抖动定时 */
const CONFIRM_TIMER_MIN_MS = 200;
/** INTERRUPT 插话后的人类不可打断窗（毫秒，20261107）：给用户反应停止说话的时间 */
const INTERRUPT_HUMAN_GUARD_MS = 300;
/** CONTINUE 长静音兜底窗口（秒，20261107）：静音持续达此值照发挂起池（VAD 证据驱动） */
const CONTINUE_SILENCE_DEFAULT = 3.0;
/** 长静音兜底窗口 clamp 上限（秒）：防配置错误导致超长沉默 */
const CONTINUE_SILENCE_MAX = 30;

/**
 * 断句尾静音时长按交互模式区分（20261010_模块0_全双工节奏优化）：
 *
 * - **duplex（全双工）0.45s → 实际 2 块 ≈ 0.51s**：全双工本就「说完一句就发」，
 *   误切的容忍度高——碎片句由识别文本的标点切分再校准、AI 说话期有独立打断
 *   机制（0.3s 保护窗），断句激进一档换来每句首响快约 0.25s；
 * - **vad（传统）0.7s → 3 块 ≈ 0.77s**：单轮交互无兜底，保守断句防把用户的
 *   换气/思考停顿误切成两半句。
 */
export function silenceTimeoutForMode(mode: VoiceInteractionMode): number {
  return mode === 'duplex' ? 0.45 : 0.7;
}

export class VoiceSession {
  private readonly deps: VoiceSessionDeps;
  private recorder: ContinuousRecordingSession | null = null;
  private state: VoiceSessionState = 'idle';
  /** 待发送句队列（duplex；vad 模式不经队列） */
  private queue: string[] = [];
  /** 话轮判定 CONTINUE 挂起的半截话（duplex；下一句到手拼接后再判/强制发送） */
  private pendingTurnText = '';
  /** 半截话连续挂起次数（>=2 强制 SEND——防「永不发送」死循环） */
  private pendingTurnCount = 0;
  private pendingTurnTimer: ReturnType<typeof setTimeout> | null = null;
  /** SEND 确认阈值（构造时 clamp；0 = 闸门禁用直通） */
  private readonly confirmSilenceSec: number;
  /** SEND 挂起的补足确认定时器（独立于 pendingTurnTimer，两定时器互不重入） */
  private confirmTimer: ReturnType<typeof setTimeout> | null = null;
  /** 队列泵忙标记（一句 chat 进行中） */
  private pumping = false;
  /** 播放中的句柄集（打断时统一 abort） */
  private readonly activeSpeaks = new Set<{ abort: () => void; done: Promise<void> }>();
  private speechQueue: string[] = [];
  /** 流式对话的取消控制器集（打断时统一 abort 中止 LLM 流） */
  private readonly activeStreams = new Set<AbortController>();
  /** 正在处理中的用户句文本（撤回窗口用：interrupt 时回合并池，防止用户已说文本丢失） */
  private inflightSentence: string | null = null;
  /**
   * INTERRUPT 插话的人类不可打断窗截止时刻（20261107）：插话发送后 300ms 内
   * 撤回窗口抑制——给人反应停止说话的时间；窗后恢复撤回（持续说话=否决插话）。
   */
  private interruptGuardUntilTs = 0;
  /** CONTINUE 长静音兜底定时器（与 confirmTimer/pendingTurnTimer 互不重入） */
  private continueTimer: ReturnType<typeof setTimeout> | null = null;
  /** 长静音兜底窗口（秒，构造期 clamp：≤0 禁用存 0；>30 clamp 30） */
  private readonly continueSilenceSec: number;
  /**
   * 待消费的视觉标记（20261011 视觉语音融合）：开口瞬间置 true，随下一句
   * 发送透传 visual（后端一次性消费 pending 截图），发出后即复位——duplex
   * 一次开口的多句轮询仅首句带图。
   */
  private pendingVisual = false;
  /** 打断能量累计（毫秒；连续超门限计时，低于门限清零） */
  private interruptAccumMs = 0;
  private readonly interruptThreshold: number;
  private readonly interruptGuardMs: number;
  private lastLevelTs = 0;
  private stopped = true;
  private lifecycle = 0;
  private turnGeneration = 0;
  private starting: Promise<boolean> | null = null;

  constructor(deps: VoiceSessionDeps) {
    this.deps = deps;
    this.interruptThreshold = deps.interrupt?.threshold ?? INTERRUPT_THRESHOLD_DEFAULT;
    this.interruptGuardMs = deps.interrupt?.guardMs ?? INTERRUPT_GUARD_MS_DEFAULT;
    const confirmSilenceRaw = deps.confirmSilenceSec ?? CONFIRM_SILENCE_DEFAULT;
    // ≤0 视为禁用（存 0 → 闸门直通）；>10 clamp 至 10（防配置错误永久沉默）
    this.confirmSilenceSec = confirmSilenceRaw <= 0 ? 0 : Math.min(confirmSilenceRaw, CONFIRM_SILENCE_MAX);
    const continueSilenceRaw = deps.continueSilenceSec ?? CONTINUE_SILENCE_DEFAULT;
    // ≤0 视为禁用兜底（存 0 → 不调度）；>30 clamp 至 30（防配置错误超长沉默）
    this.continueSilenceSec = continueSilenceRaw <= 0 ? 0 : Math.min(continueSilenceRaw, CONTINUE_SILENCE_MAX);
  }

  /** 当前会话状态。 */
  getState(): VoiceSessionState {
    return this.state;
  }

  private setState(next: VoiceSessionState): void {
    if (this.state === next) return;
    this.state = next;
    this.deps.onStateChange?.(next);
  }

  /** 开启会话：拉起持续采集（幂等保护）。返回 false 表示采集不可用。 */
  start(): Promise<boolean> {
    if (this.recorder?.active) return Promise.resolve(true);
    if (this.starting) return this.starting;
    const lifecycle = ++this.lifecycle;
    this.turnGeneration += 1;
    this.stopped = false;
    const mode = this.deps.mode;
    const starting = (async () => {
      try {
        const recorder = await (this.deps.createRecorder ?? defaultCreateRecorder)(
          this.makeHandlers(lifecycle),
          { speechThreshold: this.deps.vad?.speechThreshold, silenceTimeoutSec: this.deps.vad?.silenceTimeoutSec ?? silenceTimeoutForMode(mode) },
        );
        if (this.stopped || this.lifecycle !== lifecycle) {
          recorder.stop();
          return false;
        }
        this.recorder = recorder;
        this.setState('listening');
        return true;
      } catch (err) {
        if (this.stopped || this.lifecycle !== lifecycle) return false;
        this.stopped = true;
        console.error('[VoiceSession] 麦克风不可用:', err);
        this.deps.onError?.(err instanceof Error ? err : new Error(String(err)));
        return false;
      }
    })();
    this.starting = starting;
    void starting.finally(() => {
      if (this.starting === starting) this.starting = null;
    });
    return starting;
  }

  /** 关闭会话：停采集、停播、中止流式请求、清队、清挂起半截话（幂等）。 */
  stop(): void {
    this.stopped = true;
    this.lifecycle += 1;
    this.turnGeneration += 1;
    this.starting = null;
    this.queue = [];
    this.speechQueue = [];
    this.pumping = false;
    this.pendingTurnText = '';
    this.pendingTurnCount = 0;
    this.pendingVisual = false;
    this.clearPendingTurnTimer();
    this.clearConfirmTimer();
    this.clearContinueTimer();
    this.interruptAccumMs = 0;
    this.lastLevelTs = 0;
    for (const handle of this.activeSpeaks) handle.abort();
    this.activeSpeaks.clear();
    for (const controller of this.activeStreams) controller.abort();
    this.activeStreams.clear();
    this.recorder?.stop();
    this.recorder = null;
    this.setState('idle');
  }

  /** 采集事件组装（每块能量 → 打断检测；断句 → 按模式处理）。 */
  private makeHandlers(lifecycle: number): ContinuousRecordingHandlers {
    return {
      onLevel: (rms) => {
        if (this.lifecycle === lifecycle && !this.stopped) this.handleLevel(rms);
      },
      onSpeechStart: () => {
        if (this.lifecycle !== lifecycle || this.stopped) return;
        // 开口瞬间（20261011 视觉语音融合）：置视觉标记 + 触发截图预填充
        // （fire-and-forget，失败静默不阻断语音）
        this.pendingVisual = true;
        this.deps.onSpeechStart?.();
      },
      onUtterance: (audio) => {
        if (this.lifecycle === lifecycle && !this.stopped) void this.handleUtterance(audio);
      },
      onError: (err) => {
        if (this.lifecycle !== lifecycle || this.stopped) return;
        console.error('[VoiceSession] 采集错误:', err);
        this.stop();
      },
    };
  }

  /**
   * 每块能量：duplex 下仅在「未出声撤回窗口」做打断/撤回检测（20261107 出声阻断）：
   * - TTS 出声后（activeSpeaks>0）**直接阻断**——说完比撤回好：AI 回采不再可能
   *   误触发打断/撤回（20261006 播放期打断语义由本裁决推翻）；
   * - 未出声在途期（SEND 后 LLM 计算/TTS 合成中）：AI 无声源，用正常聆听门限 →
   *   interrupt 撤回模式（abort 流式请求不落盘 + 用户句文本回合并池继续）。
   */
  private handleLevel(rms: number): void {
    if (this.pendingTurnText && rms >= (this.deps.vad?.speechThreshold ?? 0.02)) {
      // SEND 确认闸门挂起（confirmTimer 活跃）：开口即清补足定时器（不等 0.45s
      // 断句）——挂起文本保留，等新 utterance 合并。CONTINUE 挂起（confirmTimer
      // 不活跃）什么都不做：LLM 判定未完不兜底强发（20261107），等 VAD 断句
      // 产出新 utterance 走判定；撤回回池的 2s 兜底 timer 也不被重置
      if (this.confirmTimer !== null) this.clearConfirmTimer();
    }
    if (this.deps.mode !== 'duplex' || this.activeSpeaks.size > 0) return;
    // INTERRUPT 插话的人类不可打断窗（20261107）：300ms 内能量块忽略——给用户
    // 反应停止说话的时间，窗后恢复撤回（持续说话=否决插话）
    if (Date.now() < this.interruptGuardUntilTs) return;
    // 未出声在途期才有撤回对象；纯 listening（无在途工作）无事可做
    const pendingWork =
      this.activeStreams.size > 0 ||
      this.queue.length > 0 ||
      this.pumping ||
      this.inflightSentence !== null;
    if (!pendingWork) return;
    const gate = this.deps.vad?.speechThreshold ?? 0.02;
    const now = Date.now();
    const elapsed = this.lastLevelTs ? now - this.lastLevelTs : 0;
    this.lastLevelTs = now;
    if (rms >= gate) {
      this.interruptAccumMs += Math.min(elapsed || 32, 120);
      if (this.interruptAccumMs >= this.interruptGuardMs) {
        this.interruptAccumMs = 0;
        this.interrupt();
        return;
      }
    } else {
      this.interruptAccumMs = 0;
    }
  }

  /** 一段语音结束（能量 VAD 断句产出）：识别 → 话轮判定 → 按模式处理。 */
  private async handleUtterance(audio: {
    audioBase64: string;
    sampleRate: number;
  }): Promise<void> {
    if (this.stopped) return;
    const generation = this.turnGeneration;
    this.clearPendingTurnTimer();
    // 新语音段到达（20261106 SEND 确认闸门）：清补足确认定时器，挂起文本走
    // 既有合并/判定流程（新判定 SEND 时与挂起文本合并后再过闸门）
    this.clearConfirmTimer();
    // 新语音段到达（20261107 CONTINUE 长静音兜底）：清长静音 timer——用户有
    // 下文，重新走判定（再 CONTINUE 则重新调度兜底）
    this.clearContinueTimer();
    // 出声阻断（20261107）：speaking 态到达的语音段一律丢弃——AI 扬声器回采
    // 不得进入识别链路（vad/duplex 同则；说完比撤回好，播放期不再打断）
    if (this.state === 'speaking') return;
    if (this.activeSpeaks.size === 0) this.setState('processing');
    let text = '';
    try {
      const result = await this.deps.transcribe(audio.audioBase64, audio.sampleRate);
      text = result?.ok === true ? (result.text ?? '').trim() : '';
    } catch (err) {
      console.error('[VoiceSession] 识别失败:', err);
    }
    if (this.stopped || generation !== this.turnGeneration) return;
    if (!text) {
      this.schedulePendingTurn();
      this.settleIfIdle();
      return;
    }
    if (this.deps.mode === 'duplex') {
      // 话轮判定（20261010 主模型判定模式；20261107 五枚举）
      const verdict = await this.resolveTurn(text);
      if (this.stopped || generation !== this.turnGeneration) return;
      if (verdict === 'IGNORE') {
        // 无意义音节（20261107 语义修正）：丢弃本段，此前挂起的半截话保留
        // 挂起等后续（不兜底强发，与 CONTINUE 语义一致）
        this.settleIfIdle();
        return;
      }
      if (verdict === 'SILENT') {
        // 说完了但无需回复（如「好的就这样」）：仅对本段沉默（20261107 精修：
        // 此前挂起的半截话**保留**——与 IGNORE 同构，后续语音 SEND 时合并发出）
        this.settleIfIdle();
        return;
      }
      if (verdict === 'CONTINUE') {
        // LLM 判定话轮被打断/未说完（20261107）：挂起合并但**不固定强发**——
        // 出口 = 用户后续语音段（SEND 合并发出）、count≥2 强制 SEND（死循环
        // 保护）或**长静音 VAD 兜底**（静音持续达长窗照发，20261107 用户三裁决：
        // VAD 证据驱动，解决 LLM 误判 CONTINUE 的悬挂）
        this.pendingTurnText = this.pendingTurnText
          ? `${this.pendingTurnText}${text}`
          : text;
        this.pendingTurnCount += 1;
        this.scheduleContinueFallback();
        this.settleIfIdle();
        return;
      }
      // SEND / INTERRUPT（该 AI 插话打断用户）：带上挂起的半截话一起发（可能为
      // 空）；确认闸门未放行时挂起，由补足确认定时器承载发送（不叠加 2s 兜底）。
      // INTERRUPT 与 SEND 同发送路径——撤回窗口照常（300ms 人类不可打断窗除外）
      if (verdict === 'INTERRUPT') {
        this.interruptGuardUntilTs = Date.now() + INTERRUPT_HUMAN_GUARD_MS;
      }
      const merged = this.pendingTurnText ? `${this.pendingTurnText}${text}` : text;
      this.pendingTurnText = '';
      this.pendingTurnCount = 0;
      this.clearPendingTurnTimer();
      if (this.passSendConfirmGate(merged)) {
        this.queue.push(...splitSentences(merged));
        this.pump();
      }
      return;
    }
    // vad 模式：整段一句话处理
    await this.processSentence(text);
  }

  private clearPendingTurnTimer(): void {
    if (this.pendingTurnTimer !== null) clearTimeout(this.pendingTurnTimer);
    this.pendingTurnTimer = null;
  }

  private schedulePendingTurn(): void {
    this.clearPendingTurnTimer();
    if (this.stopped || !this.pendingTurnText) return;
    const generation = this.turnGeneration;
    this.pendingTurnTimer = setTimeout(() => {
      this.pendingTurnTimer = null;
      if (this.stopped || generation !== this.turnGeneration) return;
      const text = this.pendingTurnText;
      this.pendingTurnText = '';
      this.pendingTurnCount = 0;
      this.queue.push(...splitSentences(text));
      this.pump();
    }, Math.max(1, this.deps.turnWaitMs ?? 2000));
  }

  /** 清除 SEND 挂起的补足确认定时器（幂等；stop/打断/新 utterance/开口能量均先行清除）。 */
  private clearConfirmTimer(): void {
    if (this.confirmTimer !== null) clearTimeout(this.confirmTimer);
    this.confirmTimer = null;
  }

  /** 实时静音秒数（查询异常按静音充分处理——宁误发不吞话，与 turnCheck 异常一律 SEND 同则）。 */
  private currentSilenceSec(): number {
    try {
      return this.deps.getSilenceSec?.() ?? 0;
    } catch {
      return Number.MAX_SAFE_INTEGER;
    }
  }

  /**
   * SEND 确认闸门（20261106 并行慢速 VAD 判停，duplex 专用）：话轮判定 SEND 后
   * 查询采集层实时静音——≥ 确认阈值立即放行（turnCheck 耗时通常已吸收大半确认窗，
   * 多数场景零额外延迟）；未达标挂起进合并池（复用 pendingTurnText，闸门降级
   * **不计** pendingTurnCount——该计数仅服务 turnCheck CONTINUE 死循环保护），并
   * 启动补足确认定时器（时长 = 阈值 − 当前静音，下限 0.2s）到点无条件发送。
   * 降级环境（未注入 getSilenceSec / clamp 后阈值 ≤0）直通，行为与闸门前逐字一致。
   *
   * @param merged 本轮 SEND 待发的合并文本
   * @returns true=已放行（调用方入队发送）；false=已挂起（补足定时器承载发送）
   */
  private passSendConfirmGate(merged: string): boolean {
    if (!(this.confirmSilenceSec > 0 && typeof this.deps.getSilenceSec === 'function')) return true;
    const silence = this.currentSilenceSec();
    if (silence >= this.confirmSilenceSec) return true;
    this.pendingTurnText = merged;
    this.scheduleConfirmTimer(silence);
    this.settleIfIdle();
    return false;
  }

  /**
   * 补足确认定时器（SEND 挂起路径**唯一**发送承载，不叠加 turnWaitMs 兜底）：
   * 时长 = clamp 后阈值 − 挂起时刻静音（下限 0.2s），到点无条件发送挂起文本
   * （splitSentences 入队 → 泵）；正常路径一次性定时无重试循环，不存在永久沉默。
   */
  private scheduleConfirmTimer(silenceSec: number): void {
    this.clearConfirmTimer();
    if (this.stopped || !this.pendingTurnText) return;
    const generation = this.turnGeneration;
    const waitMs = Math.max(
      CONFIRM_TIMER_MIN_MS,
      Math.round((this.confirmSilenceSec - silenceSec) * 1000),
    );
    this.confirmTimer = setTimeout(() => {
      this.confirmTimer = null;
      if (this.stopped || generation !== this.turnGeneration) return;
      const text = this.pendingTurnText;
      this.pendingTurnText = '';
      this.pendingTurnCount = 0;
      this.queue.push(...splitSentences(text));
      this.pump();
    }, waitMs);
  }

  /** 清除 CONTINUE 长静音兜底定时器（幂等；stop/打断/新 utterance 均先行清除）。 */
  private clearContinueTimer(): void {
    if (this.continueTimer !== null) clearTimeout(this.continueTimer);
    this.continueTimer = null;
  }

  /**
   * CONTINUE 长静音 VAD 兜底（20261107 用户三裁决）：挂起池在静音持续达长窗
   * （默认 3s，远大于确认闸门 1.0s）时照发——VAD 证据驱动的条件兜底，与已移除
   * 的「固定 2s 盲等」本质不同：用户继续说则新 utterance 清 timer 重判，不违背
   * LLM「未说完」判定；仅解决 LLM 误判 CONTINUE 的悬挂。未注入 getSilenceSec
   * 或 ≤0 禁用时不调度（保持悬挂，等后续语音解锁）。静音 3s 远超确认闸门 1.0s，
   * 兜底发送直接入队（静音证据充分，不再过闸门）。
   */
  private scheduleContinueFallback(): void {
    this.clearContinueTimer();
    if (this.stopped || !this.pendingTurnText) return;
    if (!(this.continueSilenceSec > 0 && typeof this.deps.getSilenceSec === 'function')) return;
    const silence = this.currentSilenceSec();
    const waitMs = Math.max(
      CONFIRM_TIMER_MIN_MS,
      Math.round((this.continueSilenceSec - silence) * 1000),
    );
    const generation = this.turnGeneration;
    this.continueTimer = setTimeout(() => {
      this.continueTimer = null;
      if (this.stopped || generation !== this.turnGeneration) return;
      const text = this.pendingTurnText;
      this.pendingTurnText = '';
      this.pendingTurnCount = 0;
      this.queue.push(...splitSentences(text));
      this.pump();
    }, waitMs);
  }

  /**
   * 话轮判定（duplex 专用）：**规则快筛优先**——识别文本以终端标点结尾即视为
   * 完整句（零成本，多数句命中）；无终端标点且注入了 turnCheck 才走主模型判定
   * （挂起连续 2 次强制 SEND 防死循环；判定异常/未注入一律 SEND，宁误发不吞话）。
   */
  private async resolveTurn(
    text: string,
  ): Promise<'SEND' | 'CONTINUE' | 'IGNORE' | 'INTERRUPT' | 'SILENT'> {
    if (/[。！？；…]["」』）】]?\s*$/.test(text)) return 'SEND';
    if (this.pendingTurnCount >= 2) return 'SEND';
    if (!this.deps.turnCheck) return 'SEND';
    try {
      return await this.deps.turnCheck(text);
    } catch {
      return 'SEND';
    }
  }

  /**
   * 队列泵（duplex）：串行逐句 chat——回复到手即完成该句（历史由后端持有，
   * 下一句自然延续上下文），播放与下一句请求重叠（不等播放完毕）。
   */
  private pump(): void {
    if (this.stopped || this.pumping) return;
    const sentence = this.queue.shift();
    if (!sentence) {
      this.settleIfIdle();
      return;
    }
    this.pumping = true;
    const generation = this.turnGeneration;
    void this.processSentence(sentence).finally(() => {
      if (this.stopped || generation !== this.turnGeneration) return;
      this.pumping = false;
      this.pump();
    });
  }

  /** 处理一句话：发送 LLM → 回复入历史（后端）→ 流式合成播放。 */
  private async processSentence(sentence: string): Promise<void> {
    if (this.activeSpeaks.size === 0) this.setState('processing');
    // 撤回窗口记录（20261106）：interrupt 在 TTS 出声前触发时按序回合并池
    this.inflightSentence = sentence;
    try {
      return await this.processSentenceInner(sentence);
    } finally {
      if (this.inflightSentence === sentence) this.inflightSentence = null;
    }
  }

  private async processSentenceInner(sentence: string): Promise<void> {
    const generation = this.turnGeneration;
    if (this.activeSpeaks.size === 0) this.setState('processing');
    // 视觉标记一次性消费：开口后首句带图（duplex 多句轮询仅首句 visual=true）。
    // 仅 visual=true 才透传 opts——非视觉路径调用签名与旧版逐字一致（兼容既有调用方）。
    const visual = this.pendingVisual;
    this.pendingVisual = false;
    // 流式路径（20261010 语音低延迟）：LLM 增量 → 增量剥标签/切句 → 首个完整句
    // 即送 TTS，不等完整回复；未注入 chatStream 时回退既有整段路径
    if (this.deps.chatStream) {
      await this.processSentenceStreamed(sentence, visual);
      return;
    }
    let reply = '';
    let mood: string | undefined;
    try {
      // 条件展开保持调用 arity：非视觉路径与旧版逐字一致（单测对调用签名的精确断言兼容）
      const data = await this.deps.chat(
        sentence,
        ...(visual ? [{ visual: true }] : []),
      );
      reply = typeof data?.clean_text === 'string' ? data.clean_text.trim() : '';
      mood = typeof data?.mood === 'string' ? data.mood : undefined;
    } catch (err) {
      console.error('[VoiceSession] 对话失败:', err);
    }
    if (this.stopped || generation !== this.turnGeneration) return;
    this.deps.onReplyMeta?.(mood, true);
    if (!reply) {
      this.settleIfIdle();
      return;
    }
    this.enqueueSpeech(reply);
  }

  private enqueueSpeech(text: string): void {
    if (this.stopped || !text.trim()) return;
    this.speechQueue.push(text);
    this.playNextSpeech();
  }

  private playNextSpeech(): void {
    if (this.stopped || this.activeSpeaks.size > 0) return;
    const text = this.speechQueue.shift();
    if (!text) {
      this.settleIfIdle();
      return;
    }
    const generation = this.turnGeneration;
    this.setState('speaking');
    const handle = this.deps.speak(text);
    this.activeSpeaks.add(handle);
    void handle.done
      .catch(() => undefined)
      .finally(() => {
        this.activeSpeaks.delete(handle);
        if (this.stopped || generation !== this.turnGeneration) return;
        this.playNextSpeech();
      });
  }

  /**
   * 流式句级流水线（20261010_模块0_语音交互延迟优化）：单句 chat 的回复内部
   * 再按句边界提前朗读——「LLM 生成中」与「TTS 播放」重叠，首响延迟从
   * 「完整回复生成完」压缩到「首个完整句生成完」。
   *
   * - StreamTextPipeline 增量剥 [emotion:x] 标签（跨 chunk 悬挂缓冲）+ 句边界
   *   切分；每凑齐完整句进入播放 FIFO，LLM 继续生成而音频不重叠；
   * - mood 首个已识别标签到达即上报（表情提前驱动），done 帧权威值兜底再报；
   * - 打断：流式请求经 activeStreams 统一 abort（interrupt() 扩展），已收残句
   *   不再 flush（signal / 代次短路）。
   */
  private async processSentenceStreamed(sentence: string, visual = false): Promise<void> {
    const chatStream = this.deps.chatStream;
    if (!chatStream) return;
    const pipeline = new StreamTextPipeline();
    const generation = this.turnGeneration;
    const controller = new AbortController();
    this.activeStreams.add(controller);
    let spokeAny = false;
    let moodReported = false;

    const speakPiece = (piece: string): void => {
      const text = piece.trim();
      if (!text) return;
      spokeAny = true;
      this.enqueueSpeech(text);
    };

    let meta: Awaited<ReturnType<NonNullable<VoiceSessionDeps['chatStream']>>>;
    let streamError: Error | undefined;
    try {
      meta = await chatStream(
        sentence,
        (delta) => {
          if (this.stopped || controller.signal.aborted || generation !== this.turnGeneration) return;
          for (const piece of pipeline.push(delta)) speakPiece(piece);
          // 首个已识别标签到达即驱动表情（不等完整回复）
          if (!moodReported) {
            const early = pipeline.getMood();
            if (early) {
              moodReported = true;
              this.deps.onReplyMeta?.(early, false);
            }
          }
        },
        controller.signal,
        ...(visual ? [{ visual: true }] : []),
      );
    } catch (err) {
      // 打断（signal.aborted）静默；真实失败记日志（已收文本可能已部分播放）
      if (!controller.signal.aborted) {
        streamError = err instanceof Error ? err : new Error(String(err));
        console.error('[VoiceSession] 对话失败:', err);
      }
    } finally {
      this.activeStreams.delete(controller);
    }
    if (this.stopped || controller.signal.aborted || generation !== this.turnGeneration) return;
    // 流结束：残余强制收口（未闭合标签原文保留口径），照常朗读
    for (const piece of pipeline.flush()) speakPiece(piece);
    if (!spokeAny && meta?.clean_text?.trim()) speakPiece(meta.clean_text);
    // 权威 mood 兜底（流中未出现标签时 done 帧给 calm 等）
    const mood = meta?.mood ?? pipeline.getMood();
    const complete = meta !== undefined && meta.ok !== false && meta.incomplete !== true && !meta.error && !streamError;
    this.deps.onReplyMeta?.(mood, complete);
    if (!complete) this.deps.onError?.(streamError ?? new Error(meta?.error ?? '回复未完整接收'));
    this.settleIfIdle();
  }

  /**
   * 撤回（duplex，20261106 撤回窗口 + 20261107 出声阻断）：仅在 **TTS 出声前**
   * 可达（handleLevel 播放期已阻断）——abort 在途流式请求（后端断开不落盘=
   * 撤回该轮判定），正在处理/排队中的**用户句文本按序回合并池**（不清采集
   * 缓冲：无残留声源，保住用户正在说的话），回聆听等新语音段合并重判再发
   * （pendingTurn 2s 兜底防沉默）。
   */
  private interrupt(): void {
    if (this.stopped) return;
    this.turnGeneration += 1;
    // 撤回集合：排队中剩余句（先入先拼）+ 正在处理中的句子文本
    const reclaim: string[] = [...this.queue];
    if (this.inflightSentence) reclaim.unshift(this.inflightSentence);
    this.queue = [];
    this.speechQueue = [];
    this.pumping = false;
    this.pendingTurnText = '';
    this.pendingTurnCount = 0;
    this.clearPendingTurnTimer();
    this.clearConfirmTimer();
    this.clearContinueTimer();
    this.interruptAccumMs = 0;
    this.lastLevelTs = 0;
    // 中止在途流式对话请求（LLM 生成同步停止；后端检测断开不落盘该轮历史）
    for (const controller of this.activeStreams) controller.abort();
    this.activeStreams.clear();
    this.inflightSentence = null;
    // 用户已说文本回合并池（新语音段合并重判；2s 兜底防沉默）
    this.pendingTurnText = reclaim.join('');
    if (this.pendingTurnText) this.schedulePendingTurn();
    this.setState('listening');
  }

  /** 队列空且无播放中句柄 → 回聆听态（会话持续待命）。 */
  private settleIfIdle(): void {
    if (this.stopped) return;
    if (this.activeSpeaks.size > 0 || this.speechQueue.length > 0) {
      this.setState('speaking');
    } else if (this.queue.length === 0 && !this.pumping && this.activeStreams.size === 0) {
      this.setState('listening');
    } else {
      this.setState('processing');
    }
  }
}

/** 默认采集工厂（生产路径）：按会话模式/显式配置传 VAD 参数（断句尾静音见 silenceTimeoutForMode）。 */
function defaultCreateRecorder(
  handlers: ContinuousRecordingHandlers,
  options?: { speechThreshold?: number; silenceTimeoutSec?: number },
): Promise<ContinuousRecordingSession> {
  return startContinuousRecording(handlers, {
    speechThreshold: options?.speechThreshold,
    silenceTimeoutSec: options?.silenceTimeoutSec,
  });
}
