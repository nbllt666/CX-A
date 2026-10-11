import { describe, it, expect, vi, afterEach } from 'vitest';
import {
  VoiceSession,
  silenceTimeoutForMode,
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
  const finishNext = () => pendings.shift()?.();
  return { factory, spoken, aborted, pendings, finishAll, finishNext };
}

const sessions: VoiceSession[] = [];
afterEach(() => {
  sessions.splice(0).forEach((session) => session.stop());
  vi.useRealTimers();
});

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
  sessions.push(session);
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

describe('silenceTimeoutForMode：断句尾静音按模式区分（20261010 全双工节奏优化）', () => {
  it('duplex 0.45s（2 块 ≈0.51s，激进断句）；vad 0.7s（3 块，保守防误切）', () => {
    expect(silenceTimeoutForMode('duplex')).toBe(0.45);
    expect(silenceTimeoutForMode('vad')).toBe(0.7);
  });

  it('全双工激进断句落到 VAD 块粒度：0.45s → 2 块（0.256s/块）', () => {
    const vad = new EnergyVad({
      silenceTimeoutSec: silenceTimeoutForMode('duplex'),
      chunkDurationSec: 0.256,
    });
    vad.feed(0.5); // speech-start
    expect(vad.feed(0.001)).toBeNull(); // 静音块 1
    expect(vad.feed(0.001)).toBe('speech-end'); // 静音块 2 → 断句（vad 模式需 3 块）
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
    expect(speak.spoken).toEqual(['回:今天天气不错。']);
    speak.finishNext();
    await flush();
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
    expect(speak.spoken.length).toBe(1);
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

describe('VoiceSession：流式句级流水线（20261010 语音低延迟）', () => {
  /** 构造可控 fake chatStream：捕获 onDelta，测试分帧喂增量；done 时 resolve meta */
  function fakeChatStream() {
    const emitters: Array<(delta: string) => void> = [];
    type StreamMeta = NonNullable<Awaited<ReturnType<NonNullable<VoiceSessionDeps['chatStream']>>>>;
    const resolvers: Array<(meta: StreamMeta) => void> = [];
    const signals: AbortSignal[] = [];
    const impl = vi.fn(
      (
        _message: string,
        onDelta: (delta: string) => void,
        signal: AbortSignal,
      ): Promise<StreamMeta> => {
        emitters.push(onDelta);
        signals.push(signal);
        return new Promise((resolve, reject) => {
          resolvers.push(resolve);
          signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
        });
      },
    );
    const emit = (delta: string) => emitters.forEach((fn) => fn(delta));
    const finish = (meta: StreamMeta = { clean_text: '', mood: 'calm' }) =>
      resolvers.forEach((fn) => fn(meta));
    return { impl, emit, finish, signals };
  }

  it('首个完整句在 LLM 生成中途即送 TTS（不等完整回复）', async () => {
    const stream = fakeChatStream();
    const { session, recorder, speak } = makeSession({
      mode: 'vad',
      transcribe: vi.fn(async () => ({ ok: true, text: '你好' })),
      chatStream: stream.impl,
    });
    expect(await session.start()).toBe(true);
    recorder.captured[0].onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(stream.impl).toHaveBeenCalledWith('你好', expect.any(Function), expect.any(AbortSignal));

    // LLM 生成中：第一个句号出现 → 立即 speak 第一句（此时流未结束）
    stream.emit('[emotion:happy]好的，');
    await flush();
    expect(speak.spoken).toEqual(['好的，']);
    expect(session.getState()).toBe('speaking');

    // 第二句凑齐先入 FIFO，不取消/重叠第一句。
    stream.emit('我在呢。');
    await flush();
    expect(speak.spoken).toEqual(['好的，']);
    expect(speak.aborted).toEqual([]);
    speak.finishNext();
    await flush();
    expect(speak.spoken).toEqual(['好的，', '我在呢。']);

    // done：mood 权威值上报
    stream.finish({ clean_text: '好的，我在呢。', mood: 'happy' });
    await flush();
    expect(speak.spoken).toEqual(['好的，', '我在呢。']);
  });

  it('标签跨 chunk 撕裂不误读；flush 收口尾句；mood 提前上报', async () => {
    const stream = fakeChatStream();
    const moods: Array<string | undefined> = [];
    const { session, recorder, speak } = makeSession({
      mode: 'vad',
      transcribe: vi.fn(async () => ({ ok: true, text: '嗯' })),
      chatStream: stream.impl,
      onReplyMeta: (m) => moods.push(m),
    });
    await session.start();
    recorder.captured[0].onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();

    stream.emit('当然[emo'); // 悬挂：半截标签不下发（"当然"无句尾标点也不产出）
    await flush();
    expect(speak.spoken).toEqual([]);
    stream.emit('tion:sad]不行呀，'); // 标签闭合剥掉 + 逗号凑齐 → 立即朗读（含前段"当然"）；mood=sad 提前上报
    await flush();
    expect(speak.spoken).toEqual(['当然不行呀，']);
    expect(moods).toContain('sad');
    stream.finish({ clean_text: '当然不行呀，', mood: 'sad' });
    await flush();
    expect(speak.spoken).toEqual(['当然不行呀，']);
  });

  it('失败 done 保留已接收文本，但不上报完整成功', async () => {
    const stream = fakeChatStream();
    const onReplyMeta = vi.fn();
    const onError = vi.fn();
    const { session, recorder, speak } = makeSession({
      mode: 'vad',
      transcribe: vi.fn(async () => ({ ok: true, text: '你好' })),
      chatStream: stream.impl,
      onReplyMeta,
      onError,
    });
    await session.start();
    recorder.captured[0].onUtterance!({ audioBase64: 'AA==', sampleRate: 16000, durationSec: 1 });
    await flush();
    stream.emit('[emotion:happy]已收到的半截文本');
    expect(onReplyMeta).toHaveBeenLastCalledWith('happy', false);
    stream.finish({ ok: false, incomplete: true, error: 'connection lost', clean_text: '已收到的半截文本' });
    await flush();
    expect(speak.spoken).toEqual(['已收到的半截文本']);
    expect(onReplyMeta).toHaveBeenLastCalledWith('happy', false);
    expect(onError).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ message: 'connection lost' }));
    speak.finishAll();
    await flush();
    expect(session.getState()).toBe('listening');
  });

  it('语音先播完、流后结束时，processing 正确回到 listening', async () => {
    const stream = fakeChatStream();
    const onReplyMeta = vi.fn();
    const { session, recorder, speak } = makeSession({
      mode: 'vad',
      transcribe: vi.fn(async () => ({ ok: true, text: '你好' })),
      chatStream: stream.impl,
      onReplyMeta,
    });
    await session.start();
    recorder.captured[0].onUtterance!({ audioBase64: 'AA==', sampleRate: 16000, durationSec: 1 });
    await flush();
    stream.emit('完整回复。');
    speak.finishAll();
    await flush();
    expect(session.getState()).toBe('processing');
    stream.finish({ ok: true, clean_text: '完整回复。', mood: 'calm' });
    await flush();
    expect(onReplyMeta).toHaveBeenLastCalledWith('calm', true);
    expect(session.getState()).toBe('listening');
  });

  it('打断：在途流式请求同步 abort、已播句统一停', async () => {
    const stream = fakeChatStream();
    const { session, recorder, speak } = makeSession({
      mode: 'duplex',
      transcribe: vi.fn(async () => ({ ok: true, text: '你好' })),
      chatStream: stream.impl,
    });
    await session.start();
    const handlers = recorder.captured[0];
    handlers.onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    stream.emit('好的，我在呢。');
    await flush();
    expect(speak.spoken).toEqual(['好的，我在呢。']);

    // 播放期用户开口超门限达保护窗 → 打断：speak abort + 流 abort + 缓冲丢弃
    for (let i = 0; i < 12; i += 1) handlers.onLevel!(0.2);
    await flush();
    expect(speak.aborted.length).toBe(1);
    expect(stream.signals[0].aborted).toBe(true);
    expect(session.getState()).toBe('listening');
  });

  it('打断丢弃 FIFO 和未闭合尾句，AbortError 后不 flush，晚到 delta 也忽略', async () => {
    const stream = fakeChatStream();
    const { session, recorder, speak } = makeSession({
      interrupt: { guardMs: 30 },
      transcribe: vi.fn(async () => ({ ok: true, text: '你好。' })),
      chatStream: stream.impl,
    });
    await session.start();
    const handlers = recorder.captured[0];
    handlers.onUtterance!({ audioBase64: 'AA==', sampleRate: 16000, durationSec: 1 });
    await flush();
    stream.emit('第一句。');
    stream.emit('第二句。尚未闭合的尾句');
    expect(speak.spoken).toEqual(['第一句。']);
    handlers.onLevel!(0.2);
    await flush();
    stream.emit('迟到的句子。');
    stream.finish();
    speak.finishAll();
    await flush();
    expect(speak.spoken).toEqual(['第一句。']);
    expect(speak.aborted).toEqual(['第一句。']);
    expect(session.getState()).toBe('listening');
  });

  it('未注入 chatStream 时回退既有 chat 整段路径（兼容性）', async () => {
    const chat = vi.fn(async (message: string) => ({ clean_text: `回:${message}`, mood: 'calm' }));
    const { session, recorder, speak } = makeSession({
      mode: 'vad',
      chat,
      transcribe: vi.fn(async () => ({ ok: true, text: '你好' })),
    });
    await session.start();
    recorder.captured[0].onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(chat).toHaveBeenCalledWith('你好');
    expect(speak.spoken).toEqual(['回:你好']);
  });

  it('话轮判定：终端标点规则快筛直发（不调 turnCheck）；半截话 CONTINUE 挂起合并', async () => {
    const turnCheck = vi.fn();
    const { session, recorder, speak } = makeSession({
      mode: 'duplex',
      transcribe: vi.fn()
        .mockResolvedValueOnce({ ok: true, text: '今天我们' }) // 无终端标点 → 走判定
        .mockResolvedValueOnce({ ok: true, text: '去公园好不好？' }), // 标点结尾 → 规则直发
      turnCheck,
    });
    await session.start();
    const handlers = recorder.captured[0];

    // 第一段：无终端标点 → turnCheck 判 CONTINUE → 挂起不发送
    turnCheck.mockResolvedValueOnce('CONTINUE');
    handlers.onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(turnCheck).toHaveBeenCalledWith('今天我们');
    expect(speak.spoken).toEqual([]);

    // 第二段：标点结尾 → 规则快筛直发（不再调 turnCheck），且合并挂起的半截话
    handlers.onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(turnCheck).toHaveBeenCalledTimes(1);
    expect(speak.spoken).toEqual(['回:今天我们去公园好不好？']); // chat mock 带「回:」前缀
  });

  it('话轮判定 IGNORE 丢弃；挂起连续 2 次强制 SEND 防死循环', async () => {
    const turnCheck = vi.fn().mockResolvedValue('IGNORE');
    const { session, recorder, speak } = makeSession({
      mode: 'duplex',
      transcribe: vi.fn(async () => ({ ok: true, text: '嗯嗯' })),
      turnCheck,
    });
    await session.start();
    const handlers = recorder.captured[0];
    handlers.onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(speak.spoken).toEqual([]); // IGNORE → 丢弃

    // 挂起 2 次后第三句强制 SEND（turnCheck 返回 CONTINUE 也不再看）
    turnCheck.mockResolvedValue('CONTINUE');
    handlers.onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    handlers.onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    handlers.onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(speak.spoken.length).toBe(1); // 强制发送（三段合并）
  });
});

describe('VoiceSession：异步生命周期与半句话超时', () => {
  it('start 去重，stop 后迟到的 recorder 立即关闭且不会重启会话', async () => {
    let resolveRecorder!: (recorder: ContinuousRecordingSession) => void;
    const stop = vi.fn();
    const createRecorder = vi.fn(() => new Promise<ContinuousRecordingSession>((resolve) => {
      resolveRecorder = resolve;
    }));
    const { session } = makeSession({ createRecorder });
    const first = session.start();
    expect(session.start()).toBe(first);
    expect(createRecorder).toHaveBeenCalledTimes(1);
    session.stop();
    resolveRecorder({ active: true, stop, clearBuffer: vi.fn() });
    expect(await first).toBe(false);
    expect(stop).toHaveBeenCalledTimes(1);
    expect(session.getState()).toBe('idle');
  });

  it('stop/start 后旧转写结果及旧采集回调不能进入新会话', async () => {
    let resolveTranscript!: (value: { ok: boolean; text: string }) => void;
    const transcribe = vi.fn(() => new Promise<{ ok: boolean; text: string }>((resolve) => {
      resolveTranscript = resolve;
    }));
    const chat = vi.fn(async () => ({ clean_text: '不应朗读' }));
    const { session, recorder } = makeSession({ transcribe, chat });
    await session.start();
    const oldHandlers = recorder.captured[0];
    oldHandlers.onUtterance!({ audioBase64: 'AA==', sampleRate: 16000, durationSec: 1 });
    session.stop();
    await session.start();
    resolveTranscript({ ok: true, text: '旧结果。' });
    oldHandlers.onUtterance!({ audioBase64: 'AA==', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(transcribe).toHaveBeenCalledTimes(1);
    expect(chat).not.toHaveBeenCalled();
    expect(session.getState()).toBe('listening');
  });

  it('CONTINUE 后持续静音达到期限，半句话只发送一次', async () => {
    vi.useFakeTimers();
    const chat = vi.fn(async (message: string) => ({ clean_text: message }));
    const { session, recorder } = makeSession({
      chat,
      turnWaitMs: 100,
      turnCheck: vi.fn(async () => 'CONTINUE' as const),
      transcribe: vi.fn(async () => ({ ok: true, text: '今天我们' })),
    });
    await session.start();
    recorder.captured[0].onUtterance!({ audioBase64: 'AA==', sampleRate: 16000, durationSec: 1 });
    await flush();
    await vi.advanceTimersByTimeAsync(99);
    expect(chat).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(chat).toHaveBeenCalledExactlyOnceWith('今天我们');
    await vi.advanceTimersByTimeAsync(1000);
    expect(chat).toHaveBeenCalledTimes(1);
  });

  it('stop 清除半句话定时器，不在关闭后发送', async () => {
    vi.useFakeTimers();
    const chat = vi.fn(async () => ({ clean_text: '回复' }));
    const { session, recorder } = makeSession({
      chat,
      turnWaitMs: 100,
      turnCheck: vi.fn(async () => 'CONTINUE' as const),
      transcribe: vi.fn(async () => ({ ok: true, text: '半句话' })),
    });
    await session.start();
    recorder.captured[0].onUtterance!({ audioBase64: 'AA==', sampleRate: 16000, durationSec: 1 });
    await flush();
    session.stop();
    await vi.advanceTimersByTimeAsync(1000);
    expect(chat).not.toHaveBeenCalled();
  });
});

describe('VoiceSession：视觉语音融合（20261011 开口截图预填充）', () => {
  it('vad：开口瞬间 onSpeechStart 回调 + 首句 chat 携带 {visual:true}；次句恢复纯文本', async () => {
    const chat = vi.fn(async (message: string) => ({ clean_text: `回:${message}`, mood: 'happy' }));
    const onSpeechStart = vi.fn();
    const { session, recorder, speak } = makeSession({
      mode: 'vad',
      chat,
      onSpeechStart,
      transcribe: vi.fn(async () => ({ ok: true, text: '看看我的屏幕' })),
    });
    expect(await session.start()).toBe(true);
    const handlers = recorder.captured[0];

    // 开口瞬间：置视觉标记 + 触发预填充回调（fire-and-forget 由接线方负责）
    handlers.onSpeechStart!();
    expect(onSpeechStart).toHaveBeenCalledTimes(1);
    handlers.onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(chat).toHaveBeenCalledWith('看看我的屏幕', { visual: true });
    speak.finishAll();
    await flush();
    // 第二句（未再开口）：不带 opts，调用 arity 与旧版逐字一致
    handlers.onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(chat).toHaveBeenLastCalledWith('看看我的屏幕');
  });

  it('duplex：一次开口的多句切分仅首句带 visual（标记一次性消费）', async () => {
    const chat = vi.fn(async (message: string) => ({ clean_text: `回:${message}`, mood: 'happy' }));
    const { session, recorder, speak } = makeSession({
      mode: 'duplex',
      chat,
      transcribe: vi.fn(async () => ({ ok: true, text: '第一句。第二句。' })),
    });
    await session.start();
    const handlers = recorder.captured[0];
    handlers.onSpeechStart!();
    handlers.onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(chat).toHaveBeenCalledTimes(2);
    expect(chat).toHaveBeenNthCalledWith(1, '第一句。', { visual: true });
    expect(chat).toHaveBeenNthCalledWith(2, '第二句。');
    speak.finishAll();
  });

  it('chatStream 路径：visual 经第 4 参透传（{visual:true} / 缺省）', async () => {
    const streamCalls: Array<boolean | undefined> = [];
    const chatStream = vi.fn(
      async (
        _message: string,
        _onDelta: (delta: string) => void,
        _signal: AbortSignal,
        opts?: { visual?: boolean },
      ) => {
        streamCalls.push(opts?.visual);
        return { clean_text: '看到了', mood: 'happy' };
      },
    );
    const { session, recorder, speak } = makeSession({
      mode: 'vad',
      chatStream,
      transcribe: vi.fn(async () => ({ ok: true, text: '这是什么' })),
    });
    await session.start();
    const handlers = recorder.captured[0];
    handlers.onSpeechStart!();
    handlers.onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    speak.finishAll();
    await flush();
    handlers.onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(chatStream).toHaveBeenCalledTimes(2);
    expect(streamCalls).toEqual([true, undefined]);
    speak.finishAll();
  });

  it('stop 清空未消费的视觉标记：stop/start 后首句不带图', async () => {
    const chat = vi.fn(async (message: string) => ({ clean_text: message, mood: 'calm' }));
    const { session, recorder } = makeSession({
      mode: 'vad',
      chat,
      transcribe: vi.fn(async () => ({ ok: true, text: '还没说出口' })),
    });
    await session.start();
    const handlers = recorder.captured[0];
    handlers.onSpeechStart!(); // 开口但未说话即关闭会话
    session.stop();
    await session.start();
    recorder.captured[1].onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(chat).toHaveBeenLastCalledWith('还没说出口');
  });
});
