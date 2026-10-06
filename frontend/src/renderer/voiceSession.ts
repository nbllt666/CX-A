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
  transcribe: (audioBase64: string, sampleRate: number) => Promise<{ ok: boolean; text?: string }>;
  /** 发送一句话（POST /api/chat/message；后端持会话历史）。 */
  chat: (message: string) => Promise<{ clean_text?: string; mood?: string } | undefined>;
  /**
   * 流式合成并播放一段回复；返回句柄——abort() 停播停合成（打断用）、
   * done 在播放自然结束后 resolve（打断时 reject 或挂起均可，会话不再等待）。
   */
  speak: (text: string) => { abort: () => void; done: Promise<void> };
  /** 表情/聊天刷新回调（mood 来自 chat 回复；缺省可省）。 */
  onReplyMeta?: (mood: string | undefined) => void;
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

export class VoiceSession {
  private readonly deps: VoiceSessionDeps;
  private recorder: ContinuousRecordingSession | null = null;
  private state: VoiceSessionState = 'idle';
  /** 待发送句队列（duplex；vad 模式不经队列） */
  private queue: string[] = [];
  /** 队列泵忙标记（一句 chat 进行中） */
  private pumping = false;
  /** 播放中的句柄集（打断时统一 abort） */
  private readonly activeSpeaks = new Set<{ abort: () => void; done: Promise<void> }>();
  /** 打断能量累计（毫秒；连续超门限计时，低于门限清零） */
  private interruptAccumMs = 0;
  private readonly interruptThreshold: number;
  private readonly interruptGuardMs: number;
  private lastLevelTs = 0;
  private stopped = false;

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
  async start(): Promise<boolean> {
    if (this.recorder?.active) return true;
    try {
      this.recorder = await (this.deps.createRecorder ?? defaultCreateRecorder)(
        this.makeHandlers(),
      );
    } catch (err) {
      console.error('[VoiceSession] 麦克风不可用:', err);
      this.deps.onError?.(err instanceof Error ? err : new Error(String(err)));
      return false;
    }
    this.stopped = false;
    this.setState('listening');
    return true;
  }

  /** 关闭会话：停采集、停播、清队（幂等）。 */
  stop(): void {
    this.stopped = true;
    this.queue = [];
    this.pumping = false;
    for (const handle of this.activeSpeaks) handle.abort();
    this.activeSpeaks.clear();
    this.recorder?.stop();
    this.recorder = null;
    this.setState('idle');
  }

  /** 采集事件组装（每块能量 → 打断检测；断句 → 按模式处理）。 */
  private makeHandlers(): ContinuousRecordingHandlers {
    return {
      onLevel: (rms) => this.handleLevel(rms),
      onUtterance: (audio) => void this.handleUtterance(audio),
      onError: (err) => {
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
    if (this.deps.mode !== 'duplex' || this.state !== 'speaking') return;
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

  /** 一段语音结束（能量 VAD 断句产出）：识别 → 按模式处理。 */
  private async handleUtterance(audio: {
    audioBase64: string;
    sampleRate: number;
  }): Promise<void> {
    if (this.stopped) return;
    // vad 模式播放期间不检测打断（spec 冻结：单轮交互）——播放中到达的语音段丢弃
    if (this.deps.mode === 'vad' && this.state === 'speaking') return;
    this.setState('processing');
    let text = '';
    try {
      const result = await this.deps.transcribe(audio.audioBase64, audio.sampleRate);
      text = result?.ok === true ? (result.text ?? '').trim() : '';
    } catch (err) {
      console.error('[VoiceSession] 识别失败:', err);
    }
    if (this.stopped) return;
    if (!text) {
      this.settleIfIdle();
      return;
    }
    if (this.deps.mode === 'duplex') {
      this.queue.push(...splitSentences(text));
      this.pump();
      return;
    }
    // vad 模式：整段一句话处理
    await this.processSentence(text);
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
    void this.processSentence(sentence).finally(() => {
      this.pumping = false;
      this.pump();
    });
  }

  /** 处理一句话：发送 LLM → 回复入历史（后端）→ 流式合成播放。 */
  private async processSentence(sentence: string): Promise<void> {
    this.setState('processing');
    let reply = '';
    let mood: string | undefined;
    try {
      const data = await this.deps.chat(sentence);
      reply = typeof data?.clean_text === 'string' ? data.clean_text.trim() : '';
      mood = typeof data?.mood === 'string' ? data.mood : undefined;
    } catch (err) {
      console.error('[VoiceSession] 对话失败:', err);
    }
    if (this.stopped) return;
    this.deps.onReplyMeta?.(mood);
    if (!reply) {
      this.settleIfIdle();
      return;
    }
    this.setState('speaking');
    const handle = this.deps.speak(reply);
    this.activeSpeaks.add(handle);
    void handle.done
      .catch(() => undefined)
      .finally(() => {
        this.activeSpeaks.delete(handle);
        this.settleIfIdle();
      });
  }

  /** 播放中用户开口（duplex）：停播、清队、丢弃采集缓冲、回聆听。 */
  private interrupt(): void {
    if (this.stopped) return;
    this.queue = [];
    this.pumping = false;
    for (const handle of this.activeSpeaks) handle.abort();
    this.activeSpeaks.clear();
    // 丢弃采集缓冲：AI 扬声器残留混入的音频不得进入识别链路（spec 冻结段）
    this.recorder?.clearBuffer();
    this.setState('listening');
  }

  /** 队列空且无播放中句柄 → 回聆听态（会话持续待命）。 */
  private settleIfIdle(): void {
    if (this.stopped) return;
    if (this.queue.length === 0 && !this.pumping && this.activeSpeaks.size === 0) {
      this.setState('listening');
    }
  }
}

/** 默认采集工厂（生产路径）。 */
function defaultCreateRecorder(
  handlers: ContinuousRecordingHandlers,
): Promise<ContinuousRecordingSession> {
  return startContinuousRecording(handlers, { speechThreshold: 0.02, silenceTimeoutSec: 0.7 });
}
