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
   * 话轮判定（20261010 主模型判定模式，对齐 CX-O agent_interrupt_user 语义）：
   * 判断 ASR 文本是完整一句（SEND）/ 还在说（CONTINUE，挂起合并下一句）/
   * 无意义（IGNORE，丢弃）。注入后 duplex 模式对**无终端标点**的识别文本
   * 先判定再发送；未注入或判定异常一律 SEND（宁误发不吞话）。
   */
  turnCheck?: (text: string) => Promise<'CONTINUE' | 'SEND' | 'IGNORE'>;
  /** CONTINUE 后没有新语音时，最多等待的毫秒数。默认 2000。 */
  turnWaitMs?: number;
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
  /** 队列泵忙标记（一句 chat 进行中） */
  private pumping = false;
  /** 播放中的句柄集（打断时统一 abort） */
  private readonly activeSpeaks = new Set<{ abort: () => void; done: Promise<void> }>();
  private speechQueue: string[] = [];
  /** 流式对话的取消控制器集（打断时统一 abort 中止 LLM 流） */
  private readonly activeStreams = new Set<AbortController>();
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
   * 每块能量：duplex + speaking 态下做打断检测——能量连续超过播放期门限
   * 达到保护窗（guardMs）即打断（无 AEC 的自触发缓解：高门限 + 保护窗）。
   */
  private handleLevel(rms: number): void {
    if (this.pendingTurnText && rms >= (this.deps.vad?.speechThreshold ?? 0.02)) {
      this.schedulePendingTurn();
    }
    if (this.deps.mode !== 'duplex' || this.activeSpeaks.size === 0) return;
    const now = Date.now();
    const elapsed = this.lastLevelTs ? now - this.lastLevelTs : 0;
    this.lastLevelTs = now;
    if (rms >= this.interruptThreshold) {
      this.interruptAccumMs += Math.min(elapsed || 32, 120);
      if (this.interruptAccumMs >= this.interruptGuardMs) {
        this.interruptAccumMs = 0;
        this.interrupt();
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
    // vad 模式播放期间不检测打断（spec 冻结：单轮交互）——播放中到达的语音段丢弃
    if (this.deps.mode === 'vad' && this.state === 'speaking') return;
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
      // 话轮判定（20261010 主模型判定模式）：CONTINUE 挂起合并下一句，不发送
      const verdict = await this.resolveTurn(text);
      if (this.stopped || generation !== this.turnGeneration) return;
      if (verdict === 'IGNORE') {
        this.schedulePendingTurn();
        this.settleIfIdle();
        return;
      }
      if (verdict === 'CONTINUE') {
        this.pendingTurnText = this.pendingTurnText
          ? `${this.pendingTurnText}${text}`
          : text;
        this.pendingTurnCount += 1;
        this.schedulePendingTurn();
        this.settleIfIdle();
        return;
      }
      // SEND：带上挂起的半截话一起发（可能为空）
      const merged = this.pendingTurnText ? `${this.pendingTurnText}${text}` : text;
      this.pendingTurnText = '';
      this.pendingTurnCount = 0;
      this.clearPendingTurnTimer();
      this.queue.push(...splitSentences(merged));
      this.pump();
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

  /**
   * 话轮判定（duplex 专用）：**规则快筛优先**——识别文本以终端标点结尾即视为
   * 完整句（零成本，多数句命中）；无终端标点且注入了 turnCheck 才走主模型判定
   * （挂起连续 2 次强制 SEND 防死循环；判定异常/未注入一律 SEND，宁误发不吞话）。
   */
  private async resolveTurn(
    text: string,
  ): Promise<'SEND' | 'CONTINUE' | 'IGNORE'> {
    if (/[。！？；…]["」』）】]?\s*$/.test(text)) return 'SEND';
    if (this.pendingTurnCount >= 2) return 'SEND';
    if (!this.deps.turnCheck) return 'SEND';
    try {
      const verdict = await this.deps.turnCheck(text);
      if (verdict === 'CONTINUE' || verdict === 'IGNORE') return verdict;
      return 'SEND';
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

  /** 播放中用户开口（duplex）：停播、中止流式请求、清队、丢弃采集缓冲、回聆听。 */
  private interrupt(): void {
    if (this.stopped) return;
    this.turnGeneration += 1;
    this.queue = [];
    this.speechQueue = [];
    this.pumping = false;
    this.pendingTurnText = '';
    this.pendingTurnCount = 0;
    this.clearPendingTurnTimer();
    this.interruptAccumMs = 0;
    this.lastLevelTs = 0;
    for (const handle of this.activeSpeaks) handle.abort();
    this.activeSpeaks.clear();
    // 中止在途流式对话请求（LLM 生成同步停止；后端检测断开不落盘该轮历史）
    for (const controller of this.activeStreams) controller.abort();
    this.activeStreams.clear();
    // 丢弃采集缓冲：AI 扬声器残留混入的音频不得进入识别链路（spec 冻结段）
    this.recorder?.clearBuffer();
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
