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
      // 类型补齐（20261106 并行 VAD 闸门）：采集层新接口；本假采集器不消费该方法
      //（闸门直通与否由 VoiceSessionDeps.getSilenceSec 注入决定）
      getSilenceSec(): number {
        return 0;
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

  it('出声阻断（20261107）：speaking 期高能量不触发打断；AI 回采切段被丢弃不进识别', async () => {
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

    // 播放期高能量连续多块：说完比撤回好——不触发打断（AI 把话说完）
    for (let i = 0; i < 14; i += 1) {
      handlers.onLevel!(0.2);
      await new Promise((r) => setTimeout(r, 5));
    }
    await flush();
    expect(speak.aborted).toEqual([]); // 播放未被中止
    expect(recorder.cleared.length).toBe(0); // 不清采集缓冲
    expect(session.getState()).toBe('speaking');

    // AI 回采被 VAD 切段产出 utterance：speaking 态一律丢弃（不进识别、不发送）
    handlers.onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(session.getState()).toBe('speaking');
    expect(states.filter((s) => s === 'processing')).toHaveLength(1); // 未进入处理
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

  it('出声阻断：已出声后高能量不再撤回（流不被 abort、播放继续）', async () => {
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

    // 出声后高能量（含 AI 回采口径）：说完比撤回好——流与播放均不受影响
    for (let i = 0; i < 12; i += 1) handlers.onLevel!(0.2);
    await flush();
    expect(speak.aborted.length).toBe(0);
    expect(stream.signals[0].aborted).toBe(false);
    expect(session.getState()).toBe('speaking');
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
    resolveRecorder({ active: true, stop, clearBuffer: vi.fn(), getSilenceSec: () => 0 });
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

  it('CONTINUE 挂起后持续静音不发送（20261107 不兜底）；下一段 SEND 合并发出', async () => {
    vi.useFakeTimers();
    const chat = vi.fn(async (message: string) => ({ clean_text: message }));
    const { session, recorder } = makeSession({
      chat,
      turnWaitMs: 100,
      turnCheck: vi.fn(async () => 'CONTINUE' as const),
      transcribe: vi.fn()
        .mockResolvedValueOnce({ ok: true, text: '今天我们' }) // 无标点 → turnCheck CONTINUE
        .mockResolvedValueOnce({ ok: true, text: '说完了。' }), // 句号 → 快筛 SEND
    });
    await session.start();
    recorder.captured[0].onUtterance!({ audioBase64: 'AA==', sampleRate: 16000, durationSec: 1 });
    await flush();
    // 挂起后长时间静音：不兜底强发（尊重 LLM 判定语义）
    await vi.advanceTimersByTimeAsync(3000);
    expect(chat).not.toHaveBeenCalled();

    // 用户下一段语音以句号结尾（快筛 SEND）→ 与挂起文本合并发出
    recorder.captured[0].onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(chat).toHaveBeenCalledExactlyOnceWith('今天我们说完了。');
  });

  it('stop 清除挂起文本与定时器，不在关闭后发送', async () => {
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

describe('VoiceSession：SEND 确认闸门与补足定时器（20261106 并行慢速 VAD 判停）', () => {
  it('换气停顿不误切：SEND 后静音未达标挂起，补足定时器到点发送', async () => {
    vi.useFakeTimers();
    const chat = vi.fn(async (message: string) => ({ clean_text: message }));
    const { session, recorder } = makeSession({
      chat,
      getSilenceSec: () => 0.5, // 换气停顿 0.5s（未达阈值 1.0s，且不随时间更新）
      confirmSilenceSec: 1.0,
      transcribe: vi.fn(async () => ({ ok: true, text: '挺好的。' })),
    });
    await session.start();
    recorder.captured[0].onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(chat).not.toHaveBeenCalled(); // 不立即发送（现状行为会直接发）
    // 补足定时器 = max(1.0 - 0.5, 0.2) = 0.5s；getSilenceSec 恒返 0.5 也到点无条件发送
    await vi.advanceTimersByTimeAsync(499);
    expect(chat).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(201);
    expect(chat).toHaveBeenCalledExactlyOnceWith('挺好的。');
  });

  it('真说完立即发送：实时静音 ≥ 阈值直通（不前进时钟）', async () => {
    vi.useFakeTimers();
    const chat = vi.fn(async (message: string) => ({ clean_text: message }));
    const { session, recorder } = makeSession({
      chat,
      getSilenceSec: () => 1.2,
      confirmSilenceSec: 1.0,
      transcribe: vi.fn(async () => ({ ok: true, text: '说完了。' })),
    });
    await session.start();
    recorder.captured[0].onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(chat).toHaveBeenCalledExactlyOnceWith('说完了。'); // 零额外等待
    await vi.advanceTimersByTimeAsync(2000);
    expect(chat).toHaveBeenCalledTimes(1); // 无补足定时器重复发送
  });

  it('开口即清：挂起后能量超门限清补足定时器且不重启 pendingTurn，新段合并发送', async () => {
    vi.useFakeTimers();
    const chat = vi.fn(async (message: string) => ({ clean_text: message }));
    const { session, recorder } = makeSession({
      chat,
      getSilenceSec: () => 0.5,
      confirmSilenceSec: 1.0,
      transcribe: vi.fn()
        .mockResolvedValueOnce({ ok: true, text: '挺好的' })
        .mockResolvedValueOnce({ ok: true, text: '那我们走吧' }),
    });
    await session.start();
    const handlers = recorder.captured[0];
    handlers.onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(chat).not.toHaveBeenCalled(); // SEND → 挂起（补足定时器 0.5s）

    handlers.onLevel!(0.05); // 超聆听门限 0.02 → 开口即清 confirmTimer（不等 0.45s 断句）
    await vi.advanceTimersByTimeAsync(2000);
    expect(chat).not.toHaveBeenCalled(); // confirmTimer 已清且未重启 pendingTurn（2s 仍不发）

    handlers.onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    await vi.advanceTimersByTimeAsync(500); // 合并后再次过闸门仍挂起（0.5<1.0）→ 补足到点
    expect(chat).toHaveBeenCalledExactlyOnceWith('挺好的那我们走吧'); // 挂起文本与新段合并一次发送
  });

  it('判定期间已开口：实时静音 0 → 降级挂起不发送（补足定时器兜底不沉默）', async () => {
    vi.useFakeTimers();
    const chat = vi.fn(async (message: string) => ({ clean_text: message }));
    const { session, recorder } = makeSession({
      chat,
      getSilenceSec: () => 0, // turnCheck 耗时期间用户已再次开口
      confirmSilenceSec: 1.0,
      transcribe: vi.fn(async () => ({ ok: true, text: '接着说。' })),
    });
    await session.start();
    recorder.captured[0].onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(chat).not.toHaveBeenCalled(); // 降级挂起
    await vi.advanceTimersByTimeAsync(1000); // 补足 max(1.0 - 0, 0.2) = 1s
    expect(chat).toHaveBeenCalledExactlyOnceWith('接着说。'); // 到点无条件发送，无永久沉默
  });

  it('补足期间新语音段到达：清定时器，新段与挂起文本合并一次发送', async () => {
    vi.useFakeTimers();
    const chat = vi.fn(async (message: string) => ({ clean_text: message }));
    const { session, recorder } = makeSession({
      chat,
      getSilenceSec: () => 0.4,
      confirmSilenceSec: 1.0,
      transcribe: vi.fn()
        .mockResolvedValueOnce({ ok: true, text: '今天我们' })
        .mockResolvedValueOnce({ ok: true, text: '去公园好不好' }),
    });
    await session.start();
    const handlers = recorder.captured[0];
    handlers.onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(chat).not.toHaveBeenCalled(); // 挂起（补足 0.6s）

    handlers.onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 }); // 定时器到点前新段到达
    await flush();
    expect(chat).not.toHaveBeenCalled(); // 旧定时器已清，合并后再过闸门仍挂起
    await vi.advanceTimersByTimeAsync(600);
    expect(chat).toHaveBeenCalledExactlyOnceWith('今天我们去公园好不好'); // 合并一次发送
  });

  it('confirmSilenceSec ≤0 禁用闸门直通', async () => {
    vi.useFakeTimers();
    const chat = vi.fn(async (message: string) => ({ clean_text: message }));
    const { session, recorder } = makeSession({
      chat,
      getSilenceSec: () => 0,
      confirmSilenceSec: 0, // ≤0 视为禁用
      transcribe: vi.fn(async () => ({ ok: true, text: '直接说。' })),
    });
    await session.start();
    recorder.captured[0].onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(chat).toHaveBeenCalledExactlyOnceWith('直接说。'); // 立即发送
  });

  it('confirmSilenceSec >10 clamp 至 10：静音 5s 挂起补足 5s 后发送', async () => {
    vi.useFakeTimers();
    const chat = vi.fn(async (message: string) => ({ clean_text: message }));
    const { session, recorder } = makeSession({
      chat,
      getSilenceSec: () => 5,
      confirmSilenceSec: 15, // clamp → 10
      transcribe: vi.fn(async () => ({ ok: true, text: '好的。' })),
    });
    await session.start();
    recorder.captured[0].onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(chat).not.toHaveBeenCalled(); // 5 < 10 → 挂起
    await vi.advanceTimersByTimeAsync(4999);
    expect(chat).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1); // 补足 max(10 - 5, 0.2) = 5s 到点
    expect(chat).toHaveBeenCalledExactlyOnceWith('好的。');
  });

  it('未注入 getSilenceSec：闸门直通（降级环境与现状逐字一致）', async () => {
    vi.useFakeTimers();
    const chat = vi.fn(async (message: string) => ({ clean_text: message }));
    const { session, recorder } = makeSession({
      chat,
      confirmSilenceSec: 1.0, // 阈值注入但查询函数缺失 → 直通
      transcribe: vi.fn(async () => ({ ok: true, text: '降级直通。' })),
    });
    await session.start();
    recorder.captured[0].onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(chat).toHaveBeenCalledExactlyOnceWith('降级直通。');
  });

  it('vad 模式不经确认闸门：getSilenceSec 返回 0 也整段直发', async () => {
    const chat = vi.fn(async (message: string) => ({ clean_text: message }));
    const { session, recorder } = makeSession({
      mode: 'vad',
      chat,
      getSilenceSec: () => 0,
      confirmSilenceSec: 1.0,
      transcribe: vi.fn(async () => ({ ok: true, text: '你好呀' })),
    });
    expect(await session.start()).toBe(true);
    recorder.captured[0].onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(chat).toHaveBeenCalledWith('你好呀'); // 整段路径不经过闸门
  });

  it('CONTINUE 挂起不兜底强发（20261107，禁用长静音兜底）：等后续语音 SEND 合并或 count≥2 强制 SEND', async () => {
    vi.useFakeTimers();
    const chat = vi.fn(async (message: string) => ({ clean_text: message }));
    const turnCheck = vi.fn();
    const { session, recorder } = makeSession({
      chat,
      getSilenceSec: () => 5, // 静音充分：强制 SEND 过闸门直通
      confirmSilenceSec: 1.0,
      continueSilenceSec: 0, // 禁用长静音兜底（本用例专测禁用语义）
      turnCheck,
      transcribe: vi.fn(async () => ({ ok: true, text: '今天我们' })),
    });
    await session.start();
    turnCheck.mockResolvedValueOnce('CONTINUE'); // 第 1 段：挂起
    recorder.captured[0].onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(chat).not.toHaveBeenCalled();
    // LLM 判定 CONTINUE（话轮被打断/未说完）：挂起后不兜底强发
    await vi.advanceTimersByTimeAsync(5000);
    expect(chat).not.toHaveBeenCalled(); // 无定时强发——尊重判定语义

    // 出口 1：用户后续语音段（无标点 → turnCheck 第 2 次 CONTINUE → count=2 仍挂起）
    turnCheck.mockResolvedValueOnce('CONTINUE');
    recorder.captured[0].onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(chat).not.toHaveBeenCalled(); // 合并池 '今天我们今天我们'，count=2

    // 出口 2：第 3 段语音 → count≥2 强制 SEND（合并全部，死循环保护）
    turnCheck.mockResolvedValueOnce('CONTINUE');
    recorder.captured[0].onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(turnCheck).toHaveBeenCalledTimes(2); // 第 3 段不再调 turnCheck（强制 SEND）
    expect(chat).toHaveBeenCalledExactlyOnceWith('今天我们今天我们今天我们');
  });

  it('CONTINUE 长静音 VAD 兜底（20261107）：静音达长窗照发挂起池（解决悬挂）', async () => {
    vi.useFakeTimers();
    const chat = vi.fn(async (message: string) => ({ clean_text: message }));
    const turnCheck = vi.fn();
    const { session, recorder } = makeSession({
      chat,
      getSilenceSec: () => 0.5, // 挂起时刻静音 0.5s（duplex 断句窗刚达成）
      confirmSilenceSec: 1.0,
      continueSilenceSec: 3.0, // 长窗 3s
      turnCheck,
      transcribe: vi.fn(async () => ({ ok: true, text: '今天我们' })),
    });
    await session.start();
    turnCheck.mockResolvedValueOnce('CONTINUE');
    recorder.captured[0].onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(chat).not.toHaveBeenCalled();
    // 补足长窗：3s - 0.5s = 2.5s 后照发（VAD 证据驱动，非盲等）
    await vi.advanceTimersByTimeAsync(2499);
    expect(chat).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(chat).toHaveBeenCalledExactlyOnceWith('今天我们');
  });

  it('CONTINUE 兜底 timer 被新语音清掉重判（用户有下文则不照发）', async () => {
    vi.useFakeTimers();
    const chat = vi.fn(async (message: string) => ({ clean_text: message }));
    const turnCheck = vi.fn();
    const { session, recorder } = makeSession({
      chat,
      getSilenceSec: () => 0.5,
      confirmSilenceSec: 1.0,
      continueSilenceSec: 3.0,
      turnCheck,
      transcribe: vi.fn(async () => ({ ok: true, text: '今天我们' })),
    });
    await session.start();
    turnCheck.mockResolvedValueOnce('CONTINUE');
    recorder.captured[0].onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    // 长窗到期前用户新语音段到达：timer 清掉重判（仍 CONTINUE → 重新调度）
    await vi.advanceTimersByTimeAsync(1000);
    turnCheck.mockResolvedValueOnce('CONTINUE');
    recorder.captured[0].onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    await vi.advanceTimersByTimeAsync(2000); // 原 timer 到期时刻已过
    expect(chat).not.toHaveBeenCalled(); // 旧 timer 已清（重判后重新调度，未到期）
    // 新调度补足：3s - 0.5s = 2.5s（从第二次挂起起算）
    await vi.advanceTimersByTimeAsync(500);
    expect(chat).toHaveBeenCalledExactlyOnceWith('今天我们今天我们');
  });

  it('continueSilenceSec ≤0 禁用兜底：CONTINUE 挂起保持悬挂等后续语音解锁', async () => {
    vi.useFakeTimers();
    const chat = vi.fn(async (message: string) => ({ clean_text: message }));
    const turnCheck = vi.fn();
    const { session, recorder } = makeSession({
      chat,
      getSilenceSec: () => 5, // 静音充分：SEND 过闸门直通（本用例专测 CONTINUE 兜底禁用）
      confirmSilenceSec: 1.0,
      continueSilenceSec: 0, // 禁用长静音兜底
      turnCheck,
      transcribe: vi.fn()
        .mockResolvedValueOnce({ ok: true, text: '今天我们' }) // 无标点 → turnCheck CONTINUE
        .mockResolvedValueOnce({ ok: true, text: '说完了。' }), // 句号 → 快筛 SEND 解锁
    });
    await session.start();
    turnCheck.mockResolvedValueOnce('CONTINUE');
    recorder.captured[0].onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    await vi.advanceTimersByTimeAsync(10000);
    expect(chat).not.toHaveBeenCalled(); // 无兜底（禁用语义）
    // 用户下一段 SEND 解锁
    recorder.captured[0].onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(chat).toHaveBeenCalledExactlyOnceWith('今天我们说完了。');
  });

  it('INTERRUPT（LLM 打断人）：挂起文本立即发送触发插话', async () => {
    vi.useFakeTimers();
    const chat = vi.fn(async (message: string) => ({ clean_text: message }));
    const turnCheck = vi.fn();
    const { session, recorder } = makeSession({
      chat,
      getSilenceSec: () => 5, // 静音充分：发送路径过闸门直通
      confirmSilenceSec: 1.0,
      turnCheck,
      transcribe: vi.fn(async () => ({ ok: true, text: '我今天就是想' })),
    });
    await session.start();
    // 第 1 段 CONTINUE 挂起
    turnCheck.mockResolvedValueOnce('CONTINUE');
    recorder.captured[0].onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(chat).not.toHaveBeenCalled();
    // 第 2 段 LLM 判定 INTERRUPT：AI 插话——挂起文本立即发送
    turnCheck.mockResolvedValueOnce('INTERRUPT');
    recorder.captured[0].onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(chat).toHaveBeenCalledExactlyOnceWith('我今天就是想我今天就是想');
  });

  it('SILENT（说完不回复）：本段沉默但保留挂起池（20261107 精修）；后续 SEND 合并发出', async () => {
    vi.useFakeTimers();
    const chat = vi.fn(async (message: string) => ({ clean_text: message }));
    const turnCheck = vi.fn();
    const { session, recorder } = makeSession({
      chat,
      getSilenceSec: () => 5,
      confirmSilenceSec: 1.0,
      turnCheck,
      transcribe: vi.fn()
        .mockResolvedValueOnce({ ok: true, text: '嗯好的就这样' }) // 无标点 → turnCheck CONTINUE
        .mockResolvedValueOnce({ ok: true, text: '对了还有' }) // 无标点 → turnCheck SILENT
        .mockResolvedValueOnce({ ok: true, text: '聊这个。' }), // 句号 → 快筛 SEND
    });
    await session.start();
    // 第 1 段 CONTINUE 挂起
    turnCheck.mockResolvedValueOnce('CONTINUE');
    recorder.captured[0].onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    // 第 2 段 SILENT：本段沉默——挂起池保留（不连坐丢弃）
    turnCheck.mockResolvedValueOnce('SILENT');
    recorder.captured[0].onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    await vi.advanceTimersByTimeAsync(5000);
    expect(chat).not.toHaveBeenCalled(); // 无兜底、无强发——AI 对本段沉默

    // 后续语音 SEND：与保留的挂起池合并发出（上下文不丢）
    recorder.captured[0].onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(chat).toHaveBeenCalledExactlyOnceWith('嗯好的就这样聊这个。');
  });

  it('IGNORE（无意义音节）：丢弃本段且池子保留挂起不兜底（与 CONTINUE 语义一致）', async () => {
    vi.useFakeTimers();
    const chat = vi.fn(async (message: string) => ({ clean_text: message }));
    const turnCheck = vi.fn();
    const { session, recorder } = makeSession({
      chat,
      getSilenceSec: () => 5,
      confirmSilenceSec: 1.0,
      turnCheck,
      transcribe: vi.fn()
        .mockResolvedValueOnce({ ok: true, text: '今天我们' })
        .mockResolvedValueOnce({ ok: true, text: '咳咳' })
        .mockResolvedValueOnce({ ok: true, text: '聊这个。' }),
    });
    await session.start();
    // 第 1 段 CONTINUE 挂起
    turnCheck.mockResolvedValueOnce('CONTINUE');
    recorder.captured[0].onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    // 第 2 段 IGNORE：本段丢弃，池子保留——不兜底强发
    turnCheck.mockResolvedValueOnce('IGNORE');
    recorder.captured[0].onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    await vi.advanceTimersByTimeAsync(5000);
    expect(chat).not.toHaveBeenCalled();

    // 第 3 段 SEND：与保留的池子合并发出
    recorder.captured[0].onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(chat).toHaveBeenCalledExactlyOnceWith('今天我们聊这个。');
  });

  it('stop 清除补足确认定时器与挂起文本，关闭后不发送', async () => {
    vi.useFakeTimers();
    const chat = vi.fn(async (message: string) => ({ clean_text: message }));
    const { session, recorder } = makeSession({
      chat,
      getSilenceSec: () => 0.5,
      confirmSilenceSec: 1.0,
      transcribe: vi.fn(async () => ({ ok: true, text: '挂起句。' })),
    });
    await session.start();
    recorder.captured[0].onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(chat).not.toHaveBeenCalled(); // 已挂起
    session.stop();
    await vi.advanceTimersByTimeAsync(2000);
    expect(chat).not.toHaveBeenCalled(); // 定时器随 stop 清除，不迟到发送
  });

  it('闸门降级不增 pendingTurnCount：两次 SEND 挂起后 turnCheck 仍被正常咨询', async () => {
    vi.useFakeTimers();
    const chat = vi.fn(async (message: string) => ({ clean_text: message }));
    const turnCheck = vi.fn();
    const { session, recorder } = makeSession({
      chat,
      getSilenceSec: () => 0.5,
      confirmSilenceSec: 1.0,
      turnCheck,
      transcribe: vi.fn(async () => ({ ok: true, text: '还没说完' })), // 无标点 → 走 turnCheck
    });
    await session.start();
    const handlers = recorder.captured[0];
    turnCheck.mockResolvedValueOnce('SEND');
    handlers.onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(chat).not.toHaveBeenCalled(); // SEND → 闸门挂起（第一次）

    turnCheck.mockResolvedValueOnce('SEND');
    handlers.onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(chat).not.toHaveBeenCalled(); // 第二次挂起（合并文本）

    // 若闸门误增计数，此处 pendingTurnCount 已达 2 → 第三句强制 SEND 不再调 turnCheck
    turnCheck.mockResolvedValueOnce('CONTINUE');
    handlers.onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(turnCheck).toHaveBeenCalledTimes(3); // 计数未涨 → 第三句仍走 turnCheck
    expect(chat).not.toHaveBeenCalled(); // CONTINUE → 挂起不发送
  });
});

describe('VoiceSession：撤回窗口延伸至 TTS 出声（20261106）', () => {
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

  it('出声前开口撤回：abort 在途流（不落盘）+ 用户句回池 + 新语音合并重发 + 不清采集缓冲', async () => {
    const stream = fakeChatStream();
    const transcribe = vi.fn()
      .mockResolvedValueOnce({ ok: true, text: '一句话。' })
      .mockResolvedValueOnce({ ok: true, text: '补充。' });
    const { session, recorder, speak } = makeSession({
      mode: 'duplex',
      interrupt: { threshold: 0.09, guardMs: 30 },
      transcribe,
      chatStream: stream.impl,
    });
    expect(await session.start()).toBe(true);
    const handlers = recorder.captured[0];
    handlers.onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(stream.impl).toHaveBeenCalledTimes(1);
    expect(stream.signals[0].aborted).toBe(false);
    expect(speak.spoken).toEqual([]); // 流未产出任何 TTS（未出声）

    // LLM 计算中（未出声）：用户开口——能量 0.05 低于播放期高门限 0.09，
    // 但撤回期用正常聆听门限 0.02 即可触发；连续跨过保护窗
    for (let i = 0; i < 14; i += 1) {
      handlers.onLevel!(0.05);
      await new Promise((r) => setTimeout(r, 5));
    }
    await flush();

    expect(stream.signals[0].aborted).toBe(true); // LLM 流被撤回（后端断开不落盘）
    expect(speak.spoken).toEqual([]); // 未出过声，无播放可停
    expect(recorder.cleared.length).toBe(0); // 无残留声源：不清采集缓冲（保用户正在说的话）
    expect(session.getState()).toBe('listening');

    // 用户句回池：新语音段到达 → 与撤回文本合并（'一句话。补充。'）重判
    // SEND → 按句切分逐句轮询：chatStream 第 2 次收 merged 首句，第 3 次收下半句
    handlers.onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(stream.impl).toHaveBeenCalledTimes(2);
    expect(stream.impl).toHaveBeenLastCalledWith('一句话。', expect.any(Function), expect.any(AbortSignal));
    stream.finish({ clean_text: '好的', mood: 'calm' }); // 首句流完成 → 泵继续下一句
    await flush();
    expect(stream.impl).toHaveBeenCalledTimes(3);
    expect(stream.impl).toHaveBeenLastCalledWith('补充。', expect.any(Function), expect.any(AbortSignal));
  });

  it('撤回后用户不再继续：pendingTurn 2s 兜底把回池文本照发（不沉默）', async () => {
    vi.useFakeTimers();
    const stream = fakeChatStream();
    const { session, recorder } = makeSession({
      mode: 'duplex',
      interrupt: { threshold: 0.09, guardMs: 30 },
      transcribe: vi.fn(async () => ({ ok: true, text: '一句话。' })),
      chatStream: stream.impl,
    });
    await session.start();
    const handlers = recorder.captured[0];
    handlers.onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    // 撤回（同步能量循环：elapsed=0 → 每块计 32ms，14 块 ≥ 保护窗 30ms）
    for (let i = 0; i < 14; i += 1) handlers.onLevel!(0.05);
    expect(stream.signals[0].aborted).toBe(true);

    await vi.advanceTimersByTimeAsync(2100);
    await flush();
    expect(stream.impl).toHaveBeenCalledTimes(2); // 兜底发送
    expect(stream.impl).toHaveBeenLastCalledWith('一句话。', expect.any(Function), expect.any(AbortSignal));
  });

  it('门限区分：撤回期低能量即触发；播放期须高门限（防 AI 回采自触发）', async () => {
    vi.useFakeTimers();
    const stream = fakeChatStream();
    const { session, recorder, speak } = makeSession({
      mode: 'duplex',
      interrupt: { threshold: 0.09, guardMs: 30 },
      transcribe: vi.fn(async () => ({ ok: true, text: '一句话。' })),
      chatStream: stream.impl,
    });
    await session.start();
    const handlers = recorder.captured[0];
    // 撤回期：0.05（>0.02 聆听门限、<0.09 播放门限）即可触发
    handlers.onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    for (let i = 0; i < 14; i += 1) handlers.onLevel!(0.05);
    expect(stream.signals[0].aborted).toBe(true);

    // 兜底重发第二次流并出声
    await vi.advanceTimersByTimeAsync(2100);
    await flush();
    expect(stream.impl).toHaveBeenCalledTimes(2);
    stream.emit('回答，');
    await flush();
    expect(speak.spoken).toEqual(['回答，']);
    expect(session.getState()).toBe('speaking');
    // 播放期：0.05 低于 0.09 高门限 → 不触发打断
    for (let i = 0; i < 20; i += 1) handlers.onLevel!(0.05);
    expect(speak.aborted).toEqual([]); // 播放期低能量不触发
    expect(session.getState()).toBe('speaking');
  });

  it('出声后阻断（20261107）：播放期高能量不再停播清缓冲（说完比撤回好）', async () => {
    const stream = fakeChatStream();
    const { session, recorder, speak } = makeSession({
      mode: 'duplex',
      interrupt: { threshold: 0.09, guardMs: 30 },
      transcribe: vi.fn(async () => ({ ok: true, text: '一句话。' })),
      chatStream: stream.impl,
    });
    await session.start();
    const handlers = recorder.captured[0];
    handlers.onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    stream.emit('回答，'); // 出声
    await flush();
    expect(speak.spoken).toEqual(['回答，']);
    expect(session.getState()).toBe('speaking');

    for (let i = 0; i < 14; i += 1) {
      handlers.onLevel!(0.2); // 播放期高能量（含回采口径）——阻断
      await new Promise((r) => setTimeout(r, 5));
    }
    await flush();
    expect(speak.aborted).toEqual([]); // 播放继续不被中止
    expect(recorder.cleared.length).toBe(0); // 不清采集缓冲
    expect(session.getState()).toBe('speaking');
  });

  it('INTERRUPT 300ms 人类不可打断窗（20261107）：窗内不撤回，窗后恢复', async () => {
    vi.useFakeTimers();
    const stream = fakeChatStream();
    const turnCheck = vi.fn();
    const { session, recorder } = makeSession({
      getSilenceSec: () => 5, // 静音充分：发送路径过闸门直通
      confirmSilenceSec: 1.0,
      turnCheck,
      interrupt: { threshold: 0.09, guardMs: 30 },
      transcribe: vi.fn(async () => ({ ok: true, text: '我今天就是想' })),
      chatStream: stream.impl,
    });
    await session.start();
    const handlers = recorder.captured[0];
    // 第 1 段 CONTINUE 挂起
    turnCheck.mockResolvedValueOnce('CONTINUE');
    handlers.onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    // 第 2 段 INTERRUPT：AI 插话发送（挂起文本立即发）
    turnCheck.mockResolvedValueOnce('INTERRUPT');
    handlers.onUtterance!({ audioBase64: 'aGk=', sampleRate: 16000, durationSec: 1 });
    await flush();
    expect(stream.impl).toHaveBeenCalledTimes(1);

    // 窗内（<300ms）用户还在说（高能量跨保护窗）：撤回被抑制——流不被 abort
    for (let i = 0; i < 14; i += 1) handlers.onLevel!(0.5);
    expect(stream.signals[0].aborted).toBe(false);

    // 窗后（推进 300ms+）继续高能量：撤回生效（用户持续说话=否决插话）
    await vi.advanceTimersByTimeAsync(350);
    for (let i = 0; i < 14; i += 1) handlers.onLevel!(0.5);
    expect(stream.signals[0].aborted).toBe(true);
  });
});
