import { describe, it, expect } from 'vitest';
import {
  concatFloat32,
  downsample,
  EnergyVad,
  pcm16ToBase64,
  startContinuousRecording,
  startRecording,
  TARGET_SAMPLE_RATE,
  toPcm16,
  type VadEvent,
} from '../../src/renderer/audioRecorder';

/**
 * 语音输入采集模块的单测（纯函数为主，不依赖浏览器音频设备）。
 *
 * 覆盖：
 * - downsample：等率直通 / 44.1k→16k 长度与线性插值边界 / 空输入与非法参数；
 * - toPcm16：[-1,1] 映射与钳制（防越界回绕）；
 * - pcm16ToBase64：已知样本 → 已知 base64（小端）；
 * - concatFloat32：多块按序合并；
 * - startRecording：无 Web Audio 环境（jsdom）时给出明确中文错误；
 * - EnergyVad：silenceSec 实时静音时长（跨态连续，慢速 VAD 判停确认通道）
 *   与 feed 事件序列回归；
 * - startContinuousRecording：getSilenceSec 透传与 clearBuffer 归零
 *   （最小假 Web Audio 环境手动投喂驱动，仍不依赖真实音频设备）。
 */

describe('audioRecorder 纯函数', () => {
  it('downsample：等率输入原样返回', () => {
    const input = new Float32Array([0.1, 0.2, 0.3]);
    expect(downsample(input, TARGET_SAMPLE_RATE, TARGET_SAMPLE_RATE)).toBe(input);
  });

  it('downsample：44.1k → 16k 长度按比例收缩', () => {
    const input = new Float32Array(44100);
    const output = downsample(input, 44100, TARGET_SAMPLE_RATE);
    expect(Math.abs(output.length - 16000)).toBeLessThanOrEqual(1);
  });

  it('downsample：线性插值保留端点值', () => {
    const input = new Float32Array([0, 1, 0, -1]);
    const output = downsample(input, 4, 2);
    expect(output.length).toBe(2);
    expect(output[0]).toBeCloseTo(0, 5);
    expect(output[output.length - 1]).toBeCloseTo(-1, 5);
  });

  it('downsample：空输入与非法参数原样返回', () => {
    const empty = new Float32Array(0);
    expect(downsample(empty, 44100, 16000)).toBe(empty);
    const input = new Float32Array([0.5]);
    expect(downsample(input, 0, 16000)).toBe(input);
    expect(downsample(input, 44100, 0)).toBe(input);
  });

  it('toPcm16：[-1,1] 映射到 int16 并钳制', () => {
    const pcm = toPcm16(new Float32Array([0, 1, -1, 2, -2]));
    expect(Array.from(pcm)).toEqual([0, 32767, -32767, 32767, -32768]);
  });

  it('pcm16ToBase64：小端样本 → 已知 base64', () => {
    // 0x0100 = 256（小端 bytes: 00 01）；-1 = 0xFFFF（bytes: FF FF）
    const pcm = new Int16Array([256, -1]);
    expect(pcm16ToBase64(pcm)).toBe(btoa('\u0000\u0001\u00ff\u00ff'));
  });

  it('concatFloat32：多块按序合并', () => {
    const merged = concatFloat32([
      new Float32Array([1, 2]),
      new Float32Array([3]),
      new Float32Array([4, 5]),
    ]);
    expect(Array.from(merged)).toEqual([1, 2, 3, 4, 5]);
  });

  it('startRecording：无 Web Audio 环境给出明确中文错误', async () => {
    await expect(startRecording()).rejects.toThrow(/不支持麦克风录音/);
  });
});

// ------------------------------------------------------------------ //
// EnergyVad 静音时长实时查询（并行慢速 VAD 判停确认通道）              //
// ------------------------------------------------------------------ //

describe('EnergyVad：silenceSec 实时静音时长', () => {
  it('语音态 silenceSec 恒 0（含静音块未达断句限期间）', () => {
    const vad = new EnergyVad({ silenceTimeoutSec: 0.7, chunkDurationSec: 0.256 });
    vad.feed(0.5); // speech-start
    expect(vad.silenceSec).toBe(0);
    vad.feed(0.001); // 静音块 1（未达限，仍在语音态）
    expect(vad.silenceSec).toBe(0);
    vad.feed(0.3); // 超门限恢复，静音计数重置
    expect(vad.silenceSec).toBe(0);
  });

  it('speech-end 后静音持续累计（跨态连续，切段不归零）', () => {
    const vad = new EnergyVad({ silenceTimeoutSec: 0.7, chunkDurationSec: 0.256 });
    vad.feed(0.5); // speech-start
    expect(vad.feed(0.001)).toBeNull(); // 静音块 1
    expect(vad.feed(0.001)).toBeNull(); // 静音块 2
    expect(vad.feed(0.001)).toBe('speech-end'); // 静音块 3 → 断句
    expect(vad.silenceSec).toBeCloseTo(3 * 0.256, 5); // 断句后不归零
    expect(vad.feed(0.001)).toBeNull(); // 继续静音
    expect(vad.silenceSec).toBeCloseTo(4 * 0.256, 5); // 跨态连续增长
  });

  it('reset() 归零静音累计与语音态', () => {
    const vad = new EnergyVad({ silenceTimeoutSec: 0.7, chunkDurationSec: 0.256 });
    vad.feed(0.5);
    vad.feed(0.001);
    vad.feed(0.001);
    vad.feed(0.001); // speech-end，静音累计 3 块
    vad.reset();
    expect(vad.silenceSec).toBe(0);
    expect(vad.inSpeech).toBe(false);
    vad.feed(0.001); // 重置后从 0 重新起算
    expect(vad.silenceSec).toBeCloseTo(0.256, 5);
  });

  it('feed 事件序列回归：start/end/滞回与现状逐字一致', () => {
    const vad = new EnergyVad({
      speechThreshold: 0.02,
      silenceTimeoutSec: 0.7,
      chunkDurationSec: 0.256,
    });
    const events: VadEvent[] = [];
    events.push(vad.feed(0.5)); // 超门限 → speech-start
    events.push(vad.feed(0.001)); // 短静音（未达限）
    events.push(vad.feed(0.001));
    events.push(vad.feed(0.5)); // 恢复超门限 → 无重复 start
    events.push(vad.feed(0.001));
    events.push(vad.feed(0.001));
    events.push(vad.feed(0.001)); // 静音达限 → speech-end
    events.push(vad.feed(0.001)); // 静音态继续 → 无重复 end
    expect(events).toEqual(['speech-start', null, null, null, null, null, 'speech-end', null]);
  });

  it('精度口径：chunkDurationSec=0.256 喂 4 个静音块 → silenceSec≈1.024', () => {
    const vad = new EnergyVad({ chunkDurationSec: 0.256 });
    for (let i = 0; i < 4; i += 1) vad.feed(0.001);
    expect(vad.silenceSec).toBeCloseTo(1.024, 5);
  });
});

/**
 * 构造最小 Web Audio 假环境（jsdom 无 AudioContext / getUserMedia）：
 * startContinuousRecording 以 16k 建链后，用 feed 系列手动逐块投喂样本
 * （块长 4096 样本 = 0.256s，与 PROCESSOR_BUFFER_SIZE 对齐）。
 */
function installFakeAudio() {
  let onProcess:
    | ((event: { inputBuffer: { getChannelData: (ch: number) => Float32Array } }) => void)
    | null = null;
  const processor: Record<string, unknown> = {
    connect: () => undefined,
    disconnect: () => undefined,
  };
  Object.defineProperty(processor, 'onaudioprocess', {
    get: () => onProcess,
    set: (fn: typeof onProcess) => {
      onProcess = fn;
    },
  });
  class FakeAudioContext {
    sampleRate = TARGET_SAMPLE_RATE;
    destination = {};
    createMediaStreamSource() {
      return { connect: () => undefined, disconnect: () => undefined };
    }
    createScriptProcessor() {
      return processor;
    }
    createGain() {
      return { gain: { value: 0 }, connect: () => undefined, disconnect: () => undefined };
    }
    close() {
      return Promise.resolve();
    }
  }
  (globalThis as unknown as { AudioContext?: unknown }).AudioContext = FakeAudioContext;
  Object.defineProperty(globalThis.navigator, 'mediaDevices', {
    value: { getUserMedia: async () => ({ getTracks: () => [] }) },
    configurable: true,
  });
  /** 投喂一块恒定幅度样本（RMS = 幅度）。 */
  const feed = (amplitude: number) => {
    onProcess?.({
      inputBuffer: {
        getChannelData: () => new Float32Array(4096).fill(amplitude),
      },
    });
  };
  return {
    /** 超门限块（RMS=0.5 > 0.02）。 */
    feedSpeech: () => feed(0.5),
    /** 静音块（RMS=0）。 */
    feedSilence: () => feed(0),
  };
}

describe('ContinuousRecordingSession：getSilenceSec 透传（假 Web Audio 环境）', () => {
  it('getSilenceSec 返回实时静音（语音态 0、跨态连续）；clearBuffer 后归零', async () => {
    const env = installFakeAudio();
    const utteranceDurations: number[] = [];
    const session = await startContinuousRecording({
      onUtterance: (audio) => utteranceDurations.push(audio.durationSec),
    });
    env.feedSpeech(); // speech-start
    env.feedSilence(); // 静音块 1
    env.feedSilence(); // 静音块 2
    expect(session.getSilenceSec()).toBe(0); // 语音态恒 0
    env.feedSilence(); // 静音块 3 → speech-end（切段）
    expect(session.getSilenceSec()).toBeCloseTo(3 * 0.256, 5); // 切段不归零
    env.feedSilence(); // 继续静音
    expect(session.getSilenceSec()).toBeCloseTo(4 * 0.256, 5); // ≥1s 可供确认闸门判定
    expect(utteranceDurations).toEqual([0.768]); // 语音段回归：1 超门限块 + 2 静音块
    session.clearBuffer(); // 静音累计一并归零
    expect(session.getSilenceSec()).toBe(0);
    session.stop();
  });
});