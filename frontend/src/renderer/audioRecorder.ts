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