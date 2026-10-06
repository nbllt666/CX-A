/**
 * 麦克风录音采集（语音输入）：采集 PCM 并按后端识别口径编码。
 *
 * 采集口径（与后端 /api/voice/transcribe 对齐）：
 * - 单声道、16-bit PCM（int16，小端）；
 * - 目标采样率 16000 Hz——AudioContext 优先直接以 16k 采集；
 *   设备不支持时按实际采样率采集并线性降采样；
 * - 裸 PCM 编码为 base64（无容器头），便于 JSON 传输。
 *
 * 实现走 Web Audio API（AudioContext + ScriptProcessorNode）：
 * 不产出 webm/opus 等压缩容器，避免后端解码依赖；Electron/Chromium 可用。
 * 采集链经 0 增益节点接地（满足 Chromium 的触发要求且不回放麦克风原声）。
 */

/** 采集目标采样率（Hz）。 */
export const TARGET_SAMPLE_RATE = 16000;

/** 一次录音的采集结果。 */
export interface RecordedAudio {
  /** int16 PCM（小端）样本 */
  pcm: Int16Array;
  /** 与 pcm 同源的 base64 编码（无容器头） */
  audioBase64: string;
  /** 实际采样率（重采样后即目标值） */
  sampleRate: number;
  /** 时长（秒） */
  durationSec: number;
  /** 有效样本数 */
  sampleCount: number;
}

/** 录音会话句柄。 */
export interface RecordingSession {
  /** 停止录音并返回采集结果；无有效音频时抛错。 */
  stop(): Promise<RecordedAudio>;
  /** 取消录音（丢弃已采集数据，不产生结果）。 */
  cancel(): void;
  /** 是否仍在采集。 */
  readonly active: boolean;
}

// ------------------------------------------------------------------ //
// 纯函数（可单测，不依赖浏览器环境）                                    //
// ------------------------------------------------------------------ //

/**
 * 把浮点波形降采样到目标采样率（线性插值）。
 *
 * @param samples 输入波形（[-1, 1] 浮点）
 * @param sourceRate 输入采样率（Hz）
 * @param targetRate 目标采样率（Hz）
 * @returns 目标采样率下的波形；等率或非法参数时原样返回输入
 */
export function downsample(
  samples: Float32Array,
  sourceRate: number,
  targetRate: number,
): Float32Array {
  if (
    sourceRate <= 0 ||
    targetRate <= 0 ||
    sourceRate === targetRate ||
    samples.length === 0
  ) {
    return samples;
  }
  const count = Math.max(1, Math.round((samples.length * targetRate) / sourceRate));
  const out = new Float32Array(count);
  const step = (samples.length - 1) / Math.max(1, count - 1);
  for (let i = 0; i < count; i += 1) {
    const pos = i * step;
    const left = Math.floor(pos);
    const right = Math.min(samples.length - 1, left + 1);
    const frac = pos - left;
    out[i] = samples[left] * (1 - frac) + samples[right] * frac;
  }
  return out;
}

/**
 * 把浮点波形（[-1, 1]）编码为 int16 PCM（乘 32767 后钳制，防越界回绕爆音）。
 *
 * @param samples 输入波形
 * @returns int16 样本（与输入等长）
 */
export function toPcm16(samples: Float32Array): Int16Array {
  const out = new Int16Array(samples.length);
  for (let i = 0; i < samples.length; i += 1) {
    const value = Math.round(samples[i] * 32767);
    out[i] = Math.max(-32768, Math.min(32767, value));
  }
  return out;
}

/**
 * int16 PCM → base64（分块拼接，避免超长参数触发调用栈溢出）。
 *
 * @param pcm int16 样本
 * @returns base64 字符串（无容器头）
 */
export function pcm16ToBase64(pcm: Int16Array): string {
  const bytes = new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength);
  const CHUNK = 0x8000;
  let binary = '';
  for (let offset = 0; offset < bytes.length; offset += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + CHUNK));
  }
  return btoa(binary);
}

/** 合并多个浮点块为单段波形。 */
export function concatFloat32(chunks: Float32Array[]): Float32Array {
  const total = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
  const merged = new Float32Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    merged.set(chunk, offset);
    offset += chunk.length;
  }
  return merged;
}

// ------------------------------------------------------------------ //
// 录音会话                                                             //
// ------------------------------------------------------------------ //

/** 采集处理器缓冲区大小（4096 ≈ 256ms @16k，兼顾实时性与开销）。 */
const PROCESSOR_BUFFER_SIZE = 4096;

/** 每段有效音频的最短时长（秒）：低于该值视为误触，不产生结果。 */
export const MIN_DURATION_SEC = 0.2;

type AudioContextCtor = new (options?: AudioContextOptions) => AudioContext;

/** 取得可用的 AudioContext 构造器（兼容 webkit 前缀）。 */
function resolveAudioContextCtor(): AudioContextCtor | null {
  const scope = window as unknown as {
    AudioContext?: AudioContextCtor;
    webkitAudioContext?: AudioContextCtor;
  };
  return scope.AudioContext ?? scope.webkitAudioContext ?? null;
}

/**
 * 开始一次录音。
 *
 * @returns 录音会话；调用方需在结束时 stop() 或 cancel()
 * @throws Error 麦克风权限被拒 / 环境不支持 Web Audio 时
 */
export async function startRecording(): Promise<RecordingSession> {
  const Ctor = resolveAudioContextCtor();
  if (!Ctor) {
    throw new Error('当前环境不支持麦克风录音');
  }
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
  });
  const context = new Ctor({ sampleRate: TARGET_SAMPLE_RATE });
  const source = context.createMediaStreamSource(stream);
  const processor = context.createScriptProcessor(PROCESSOR_BUFFER_SIZE, 1, 1);
  const mute = context.createGain();
  mute.gain.value = 0; // 0 增益接地：Chromium 需接地才触发采集，且不回放原声
  const chunks: Float32Array[] = [];
  let active = true;

  processor.onaudioprocess = (event: AudioProcessingEvent) => {
    if (!active) return;
    chunks.push(new Float32Array(event.inputBuffer.getChannelData(0)));
  };
  source.connect(processor);
  processor.connect(mute);
  mute.connect(context.destination);

  /** 释放采集资源（幂等）：断开节点、停轨道、关闭上下文。 */
  function teardown(): void {
    active = false;
    try {
      processor.onaudioprocess = null;
    } catch {
      /* 忽略：环境不支持置空时无碍 */
    }
    for (const node of [processor, mute, source]) {
      try {
        node.disconnect();
      } catch {
        /* 忽略重复断开 */
      }
    }
    for (const track of stream.getTracks()) {
      track.stop();
    }
    void context.close().catch(() => undefined);
  }

  return {
    get active() {
      return active;
    },
    async stop(): Promise<RecordedAudio> {
      if (!active) {
        throw new Error('录音已结束');
      }
      const sourceRate = context.sampleRate;
      teardown();
      const merged = concatFloat32(chunks);
      const resampled = downsample(merged, sourceRate, TARGET_SAMPLE_RATE);
      const durationSec = resampled.length / TARGET_SAMPLE_RATE;
      if (resampled.length === 0 || durationSec < MIN_DURATION_SEC) {
        throw new Error('没有录到声音，请再说一次');
      }
      const pcm = toPcm16(resampled);
      return {
        pcm,
        audioBase64: pcm16ToBase64(pcm),
        sampleRate: TARGET_SAMPLE_RATE,
        durationSec,
        sampleCount: pcm.length,
      };
    },
    cancel(): void {
      teardown();
      chunks.length = 0;
    },
  };
}

// ------------------------------------------------------------------ //
// 持续采集 + 能量 VAD（20261006 全双工语音降级版）                      //
// ------------------------------------------------------------------ //

/** 计算一段波形的 RMS 能量（0~1 口径，与浮点样本幅度一致）。 */
export function computeRms(samples: Float32Array): number {
  if (samples.length === 0) return 0;
  let sum = 0;
  for (let i = 0; i < samples.length; i += 1) {
    sum += samples[i] * samples[i];
  }
  return Math.sqrt(sum / samples.length);
}

/** 能量 VAD 选项 */
export interface EnergyVadOptions {
  /** 有声门限（RMS 0~1）：超过视为语音开始。默认 0.02 */
  speechThreshold?: number;
  /** 静音断句时长（秒）：连续静音超过该值视为一句话结束。默认 0.7 */
  silenceTimeoutSec?: number;
  /** 采集块时长（秒）：feed 的每块对应时长（用于把静音时长折算成块数）。默认 0.256 */
  chunkDurationSec?: number;
}

export type VadEvent = 'speech-start' | 'speech-end' | null;

/**
 * 能量 VAD 状态机（纯逻辑，可单测）：silent ↔ speech 两态 + 滞回。
 *
 * - silent 态：RMS 超门限 → speech-start（进入 speech 态）；
 * - speech 态：连续静音块数折算时长超过 silenceTimeoutSec → speech-end（回 silent）；
 *   期间任何超门限块重置静音计数（滞回：短停顿不断句）。
 * 门限/超时的具体数值由调用方按场景给定（普通聆听与 AI 播放期防自触发用不同门限，
 * 见 voiceSession）。
 */
export class EnergyVad {
  private readonly threshold: number;
  private readonly silenceChunksLimit: number;
  private silentChunks = 0;
  private speaking = false;

  constructor(options?: EnergyVadOptions) {
    this.threshold = options?.speechThreshold ?? 0.02;
    const silenceSec = options?.silenceTimeoutSec ?? 0.7;
    const chunkSec = options?.chunkDurationSec ?? 0.256;
    this.silenceChunksLimit = Math.max(1, Math.round(silenceSec / chunkSec));
  }

  /** 是否处于语音态。 */
  get inSpeech(): boolean {
    return this.speaking;
  }

  /**
   * 喂入一块音频的能量。
   *
   * @param rms 该块 RMS（0~1）
   * @returns 状态跃迁事件（speech-start / speech-end / null）
   */
  feed(rms: number): VadEvent {
    if (rms >= this.threshold) {
      this.silentChunks = 0;
      if (!this.speaking) {
        this.speaking = true;
        return 'speech-start';
      }
      return null;
    }
    if (!this.speaking) return null;
    this.silentChunks += 1;
    if (this.silentChunks >= this.silenceChunksLimit) {
      this.speaking = false;
      this.silentChunks = 0;
      return 'speech-end';
    }
    return null;
  }

  /** 重置状态（打断后丢弃缓冲复用）。 */
  reset(): void {
    this.silentChunks = 0;
    this.speaking = false;
  }
}

/** 持续采集会话句柄。 */
export interface ContinuousRecordingSession {
  /** 停止采集并释放资源（幂等）。 */
  stop(): void;
  /** 是否仍在采集。 */
  readonly active: boolean;
  /**
   * 丢弃进行中的语音缓冲并重置 VAD（打断场景专用）：AI 播放残留混入的
   * 音频不得进入识别链路，打断后调用方用本方法清缓冲再重新聆听。
   */
  clearBuffer(): void;
}

/** 持续采集事件回调集。 */
export interface ContinuousRecordingHandlers {
  /** 一句话采集完成（能量 VAD 断句产出）。 */
  onUtterance: (audio: { audioBase64: string; sampleRate: number; durationSec: number }) => void;
  /** 每块能量回调（RMS 0~1；打断检测/可视化用）。 */
  onLevel?: (rms: number) => void;
  /** 采集错误（权限被拒等）。 */
  onError?: (err: Error) => void;
}

/** 持续采集选项 */
export interface ContinuousRecordingOptions {
  /** 有声门限（RMS）。默认 0.02 */
  speechThreshold?: number;
  /** 静音断句时长（秒）。默认 0.7 */
  silenceTimeoutSec?: number;
}

/**
 * 持续采集麦克风并按能量 VAD 自动断句（全双工/传统 VAD 会话的采集层）。
 *
 * 与 :func:`startRecording` 的一次性录音不同：本函数持续运行，逐块计算 RMS
 * 交给内部 EnergyVad，语音段结束（静音断句）时把整段波形编码回调 onUtterance。
 * 调用方经 session.stop() 停止。
 *
 * @throws Error 麦克风权限被拒 / 环境不支持 Web Audio 时
 */
export async function startContinuousRecording(
  handlers: ContinuousRecordingHandlers,
  options?: ContinuousRecordingOptions,
): Promise<ContinuousRecordingSession> {
  const Ctor = resolveAudioContextCtor();
  if (!Ctor) {
    throw new Error('当前环境不支持麦克风录音');
  }
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
  });
  const context = new Ctor({ sampleRate: TARGET_SAMPLE_RATE });
  const source = context.createMediaStreamSource(stream);
  const processor = context.createScriptProcessor(PROCESSOR_BUFFER_SIZE, 1, 1);
  const mute = context.createGain();
  mute.gain.value = 0;
  const vad = new EnergyVad({
    speechThreshold: options?.speechThreshold,
    silenceTimeoutSec: options?.silenceTimeoutSec,
    chunkDurationSec: PROCESSOR_BUFFER_SIZE / context.sampleRate,
  });
  let speechChunks: Float32Array[] = [];
  let active = true;

  processor.onaudioprocess = (event: AudioProcessingEvent) => {
    if (!active) return;
    const chunk = new Float32Array(event.inputBuffer.getChannelData(0));
    const rms = computeRms(chunk);
    handlers.onLevel?.(rms);
    const event0 = vad.feed(rms);
    if (event0 === 'speech-start') {
      speechChunks = [chunk];
      return;
    }
    if (vad.inSpeech) {
      speechChunks.push(chunk);
    }
    if (event0 === 'speech-end') {
      const merged = concatFloat32(speechChunks);
      speechChunks = [];
      const resampled = downsample(merged, context.sampleRate, TARGET_SAMPLE_RATE);
      const durationSec = resampled.length / TARGET_SAMPLE_RATE;
      if (durationSec < MIN_DURATION_SEC) return; // 误触（关门声等瞬态）丢弃
      const pcm = toPcm16(resampled);
      handlers.onUtterance({
        audioBase64: pcm16ToBase64(pcm),
        sampleRate: TARGET_SAMPLE_RATE,
        durationSec,
      });
    }
  };
  source.connect(processor);
  processor.connect(mute);
  mute.connect(context.destination);

  /** 释放采集资源（幂等）。 */
  function teardown(): void {
    active = false;
    try {
      processor.onaudioprocess = null;
    } catch {
      /* 忽略 */
    }
    for (const node of [processor, mute, source]) {
      try {
        node.disconnect();
      } catch {
        /* 忽略重复断开 */
      }
    }
    for (const track of stream.getTracks()) {
      track.stop();
    }
    void context.close().catch(() => undefined);
  }

  return {
    get active() {
      return active;
    },
    stop(): void {
      teardown();
      speechChunks = [];
    },
    clearBuffer(): void {
      // 丢弃进行中的语音缓冲 + 重置 VAD 状态（含 speech 态归零：打断后重新聆听）
      speechChunks = [];
      vad.reset();
    },
  };
}