import { describe, it, expect, vi } from 'vitest';
import {
  VoiceSession,
  type VoiceSessionDeps,
  type VoiceSessionState,
} from '../../src/renderer/voiceSession';
import { EnergyVad, type ContinuousRecordingSession } from '../../src/renderer/audioRecorder';

/**
 * VoiceSession 状态机单测（20261006 全双工语音降级版）：
 * 采集/识别/LLM/播放全部依赖注入——只验证状态机、队列轮询、上下文串行、
 * 打断（停播+清队+缓冲丢弃）与门限防误触的纯逻辑。
 */

/** 可控假采集器：测试直接操作捕获到的 handlers */
function fakeRecorderFactory() {
  const captured: Array<Parameters<NonNullable<VoiceSessionDeps['createRecorder']>>[0]> = [];
  const stopped: boolean[] = [];
  const cleared: number[] = [];
  const factory = async (
    handlers: Parameters<NonNullable<VoiceSessionDeps['createRecorder']>>[0],
  ): Promise<ContinuousRecordingSession> => {
    captured.push(handlers);
    return {
      get active() {
        return true;
      },
      stop(): void {
        stopped.push(true);
      },
      clearBuffer(): void {
        cleared.push(1);
      },
    };
  };
  return { factory, captured, stopped, cleared };
}

/** 可控假 speak：记录文本、返回可 abort/done 控制的句柄 */
function fakeSpeak() {
  const spoken: string[] = [];
  const aborted: string[] = [];
  const pendings: Array<() => void> = [];
  const factory = (text: string) => {
    spoken.push(text);
    const handle = {
      abort: () => aborted.push(text),
      done: new Promise<void>((resolve) => pendings.push(resolve)),
    };
    return handle;
  };
  const finishAll = () => {
    while (pendings.length) pendings.shift()!();
  };
  return { factory, spoken, aborted, pendings, finishAll };
}

function makeSession(
  overrides?: Partial<VoiceSessionDeps>,
): { session: VoiceSession; recorder: ReturnType<typeof fakeRecorderFactory>; speak: ReturnType<typeof fakeSpeak>; states: VoiceSessionState[] } {
  const recorder = fakeRecorderFactory();
  const speak = fakeSpeak();
  const states: VoiceSessionState[] = [];
  const session = new VoiceSession({
    mode: 'duplex',
    transcribe: vi.fn(async () => ({ ok: true, text: '' })),
    chat: vi.fn(async (message: string) => ({ clean_text: `回:${message}`, mood: 'happy' })),
    speak: speak.factory,
    createRecorder: recorder.factory,
    onStateChange: (s) => states.push(s),
    ...overrides,
  });
  return { session, recorder, speak, states };
}

/** 等待微任务队列清空（多轮，让 async 链跑完） */
const flush = async (): Promise<void> => {
  for (let i = 0; i < 10; i += 1) await Promise.resolve();
};

describe('EnergyVad：能量断句状态机', () => {
  it('超门限 speech-start；连续静音达限 speech-end；短停顿滞回不断句', () => {
    // chunkDurationSec=0.256、silenceTimeoutSec=0.5 → 静音 2 块断句
    const vad = new EnergyVad({ speechThreshold: 0.02, silenceTimeoutSec: 0.5, chunkDurationSec: 0.256 });
    expect(vad.feed(0.5)).toBe('speech-start');
    expect(vad.feed(0.1)).toBeNull();
    expect(vad.feed(0.001)).toBeNull(); // 静音块 1（未达限）
    expect(vad.feed(0.3)).toBeNull(); // 又说话 → 计数重置
    expect(vad.feed(0.001)).toBeNull();
    expect(vad.feed(0.001)).toBe('speech-end'); // 静音块 2 → 断句
    expect(vad.inSpeech).toBe(false);
  });

  it('reset 归零状态（打断清缓冲复用）', () => {
    const vad = new EnergyVad({ speechThreshold: 0.02 });
    vad.feed(0.5);
    expect(vad.inSpeech).toBe(true);
    vad.reset();
    expect(vad.inSpeech).toBe(false);
  });
});

describe('VoiceSession：vad 模式（传统自动断句）', () => {
  it('整段识别→发送→播放→播放完回聆听；播放期间到达的语音段丢弃', async () => {
    const chat = vi.fn(async (message: string) => ({ clean_text: `回:${message}`, mood: 'happy' }));
    const { session, recorder, speak, states } = makeSession({
      mode: 'vad',
      chat,
      transcribe: vi.fn(async () => ({ ok: true, text: '你好呀' })),
    });
    expect(await session.start()).toBe(true);
    const handlers = recorder.captured[0];

    handlers.onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(chat).toHaveBeenCalledWith('你好呀');
    expect(speak.spoken).toEqual(['回:你好呀']);
    expect(session.getState()).toBe('speaking');

    // 播放期间到达的语音段：vad 模式直接丢弃（不触发第二次 chat）
    handlers.onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(chat).toHaveBeenCalledTimes(1);

    speak.finishAll();
    await flush();
    expect(session.getState()).toBe('listening');
    expect(states[states.length - 1]).toBe('listening');
  });
});

describe('VoiceSession：duplex 模式（按标点切句逐句轮询）', () => {
  it('识别文本按标点切句入队，串行逐句 chat（句 2 在句 1 完成后才发）', async () => {
    const chatCalls: string[] = [];
    const chat = vi.fn(async (message: string) => {
      chatCalls.push(message);
      // 句 1 的 chat 未完成前阻塞：验证串行语义
      await new Promise((r) => setTimeout(r, message === '今天天气不错。' ? 5 : 0));
      return { clean_text: `回:${message}`, mood: 'calm' };
    });
    const { session, recorder, speak } = makeSession({
      mode: 'duplex',
      chat,
      transcribe: vi.fn(async () => ({ ok: true, text: '今天天气不错。我们去公园吧？' })),
    });
    expect(await session.start()).toBe(true);
    const handlers = recorder.captured[0];

    handlers.onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    // 句 1 的 chat 带真实 5ms 定时器：轮询等待串行泵推进到句 2
    await vi.waitFor(() => expect(chatCalls).toEqual(['今天天气不错。', '我们去公园吧？']), {
      timeout: 1000,
    });
    expect(speak.spoken).toEqual(['回:今天天气不错。', '回:我们去公园吧？']);
    speak.finishAll();
    await flush();
    expect(session.getState()).toBe('listening');
  });

  it('打断：speaking 期连续高能量达保护窗 → abort + 清队 + 清缓冲 + 回聆听', async () => {
    const { session, recorder, speak, states } = makeSession({
      mode: 'duplex',
      interrupt: { threshold: 0.09, guardMs: 30 }, // 测试用短保护窗（逻辑与生产一致）
      transcribe: vi.fn(async () => ({ ok: true, text: '第一句。第二句。第三句。' })),
    });
    expect(await session.start()).toBe(true);
    const handlers = recorder.captured[0];
    handlers.onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(speak.spoken.length).toBe(3);
    expect(session.getState()).toBe('speaking');

    // 播放期高能量连续多块（模拟 ~400ms 持续超门限，跨过 300ms 保护窗）
    for (let i = 0; i < 14; i += 1) {
      handlers.onLevel!(0.2);
      await new Promise((r) => setTimeout(r, 5));
    }
    await flush();

    expect(speak.aborted.length).toBeGreaterThanOrEqual(1);
    expect(recorder.cleared.length).toBe(1); // 采集缓冲被丢弃（AI 残留不进识别）
    expect(session.getState()).toBe('listening');
    expect(states).toContain('listening');
  });

  it('防误触：播放期低能量（AI 回采低于门限）不触发打断', async () => {
    const { session, recorder } = makeSession({
      mode: 'duplex',
      transcribe: vi.fn(async () => ({ ok: true, text: '长回复。' })),
    });
    expect(await session.start()).toBe(true);
    const handlers = recorder.captured[0];
    handlers.onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(session.getState()).toBe('speaking');

    for (let i = 0; i < 10; i += 1) handlers.onLevel!(0.03); // 低于打断门限 0.09
    expect(session.getState()).toBe('speaking'); // 未被打断
  });

  it('stop：停采集、清队列、abort 播放，状态回 idle', async () => {
    const { session, recorder, speak } = makeSession({
      mode: 'duplex',
      transcribe: vi.fn(async () => ({ ok: true, text: '一句话。' })),
    });
    expect(await session.start()).toBe(true);
    recorder.captured[0].onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(speak.spoken.length).toBe(1);

    session.stop();
    expect(recorder.stopped.length).toBe(1);
    expect(speak.aborted.length).toBe(1);
    expect(session.getState()).toBe('idle');
  });
});
