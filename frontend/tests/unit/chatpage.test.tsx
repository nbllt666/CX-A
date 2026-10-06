import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import ChatPage from '../../src/renderer/pages/ChatPage';
import { API_ENDPOINTS } from '../../src/renderer/api';

/**
 * 录音模块替身（vi.mock 工厂提升，故经 vi.hoisted 提供可注入的会话行为）。
 *
 * 语音接线测试只验证「页面 ↔ 模块 ↔ 接口」的调用契约；
 * 真实采集逻辑由 tests/unit/audiorecorder.test.ts 覆盖。
 */
const recorderMocks = vi.hoisted(() => ({
  start: vi.fn(),
  stop: vi.fn(),
  cancel: vi.fn(),
}));

vi.mock('../../src/renderer/audioRecorder', () => ({
  TARGET_SAMPLE_RATE: 16000,
  startRecording: recorderMocks.start,
}));

/**
 * Test1 · ChatPage 发送消息后 fetch 失败的真实链路降级。
 *
 * 断言（mock 网络层为 rejected fetch，全程无伪造AI回复）：
 *  1. 用户气泡出现且带「未送达」标记；
 *  2. 连接失败提示条可见，且**不含技术栈字样**（「后端」「通道」等，对齐文案口径）；
 *  3. 不出现任何AI回复气泡。
 *
 * Test2 · H3 表情端点成功：渲染 clean_text AI气泡 + 气泡头像为 Bot 图标。
 *
 * 视觉口径（对齐设计参考项目）：气泡头像是 32px 圆角方形 + lucide 图标
 * （用户 User / AI Bot），**不是 emoji 圆圈**；页头不挂任何卡通形象。
 */

/** 取气泡头像节点（lucide 图标自带 lucide-xxx 类名，便于断言语义化图标存在）。 */
function bubbleIcons(): { user: number; bot: number } {
  return {
    user: document.querySelectorAll('svg.lucide-user').length,
    bot: document.querySelectorAll('svg.lucide-bot').length,
  };
}

/**
 * 构造 URL 感知的 fetch 替身（20261004 对话持久化）：挂载时的 GET /api/chat/history
 * 恒回空历史（不计入发送断言），其余请求交给各用例的 behavior。
 */
function makeFetch(
  behavior: (url: string, init?: RequestInit) => Promise<Response>,
): ReturnType<typeof vi.fn> {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.includes('/chat/history')) {
      return new Response(JSON.stringify({ ok: true, messages: [] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    return behavior(url, init);
  });
}

describe('ChatPage：发送消息后 fetch 失败', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    // mock 网络层：后端不可达（reject），模拟真实「8600 未起」场景
    // （/chat/history 恒回空历史：挂载加载不属于本组用例的被测行为）
    fetchMock = makeFetch(async () => {
      throw new Error('ECONNREFUSED: backend not running');
    });
    vi.stubGlobal('fetch', fetchMock);
    vi.spyOn(console, 'error').mockImplementation(() => {});
    window.localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('未发过消息时不显示连接提示条（unknown 态不做断言，避免误导本地用户）', () => {
    render(<ChatPage />);

    // 空态引导可见
    expect(screen.getByText(/还没有聊天记录/)).toBeInTheDocument();
    // unknown 态：提示条不渲染（本地优先用户没配云端也能正常聊）
    expect(screen.queryByText(/消息暂时送不到/)).not.toBeInTheDocument();
  });

  it('用户气泡带未送达标记、提示条出现、无伪造AI回复', async () => {
    render(<ChatPage />);

    // 空态引导可见
    expect(screen.getByText(/还没有聊天记录/)).toBeInTheDocument();

    // 输入并发送
    const input = screen.getByPlaceholderText('跟你的AI说点什么吧…');
    fireEvent.change(input, { target: { value: '你好呀' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    // 网络层被真实调用：POST /api/chat/message（H3 表情聊天端点；挂载另有历史 GET）
    const [url, init] = await vi.waitFor(() => {
      const call = fetchMock.mock.calls.find(
        ([u]) => String(u) === API_ENDPOINTS.chat.message,
      );
      expect(call).toBeTruthy();
      return Promise.resolve(call as [string, RequestInit]);
    });
    expect(init.method).toBe('POST');
    expect(String(init.body)).toContain('你好呀');

    // 用户气泡出现且内容正确
    const myBubble = await screen.findByText('你好呀');
    expect(myBubble).toBeInTheDocument();

    // 带「未送达」失败标记（气泡元信息行内的红色小字）
    const failedMark = await screen.findByText('未送达');
    expect(failedMark).toBeInTheDocument();

    // 发送失败后提示条出现（unavailable 态），且文案不含技术栈字样
    const banner = await screen.findByText(/消息暂时送不到/);
    expect(banner).toBeInTheDocument();
    expect(banner.textContent).not.toMatch(/后端|通道|接口/);

    // 全程无伪造AI回复：AI气泡头像（Bot）不存在
    expect(bubbleIcons().bot).toBe(0);
    // 用户侧头像为 lucide User 图标（不是 emoji）
    expect(bubbleIcons().user).toBe(1);

    // 消息总数仍为 1 条（只有用户气泡）
    const bubbles = document.querySelectorAll('.rounded-2xl.px-3\\.5');
    expect(bubbles.length).toBe(1);

    // 失败后按钮恢复可点（sending 态复位）
    expect(screen.getByRole('button', { name: '发送' })).not.toBeDisabled();
  });

  it('页头不挂卡通形象，输入区为图标按钮', () => {
    render(<ChatPage />);
    // 语音输入为 lucide 图标按钮（不再是 emoji）
    const mic = screen.getByRole('button', { name: '语音输入' });
    expect(mic.querySelector('svg')).not.toBeNull();
    // 页头只有标题与副标题（无形象挂载点）：正文首个元素即标题
    expect(screen.getByRole('heading', { name: '聊天' })).toBeInTheDocument();
  });

  it('占位响应（2xx 但无回复字段）同样标未送达、不伪造回复', async () => {
    fetchMock = makeFetch(async () =>
      new Response(JSON.stringify({ error: 'chat service disabled' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);
    render(<ChatPage />);
    fireEvent.change(screen.getByPlaceholderText('跟你的AI说点什么吧…'), {
      target: { value: '在吗' },
    });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await screen.findByText('未送达');
    expect(bubbleIcons().bot).toBe(0);
    expect(screen.getByText(/消息暂时送不到/)).toBeInTheDocument();
  });

  it('ok:false 守卫响应不渲染AI回复（故障说明文本不得当回复展示）', async () => {
    // 守卫端点真实形状：200 + ok:false + error + message（失败说明文本）
    fetchMock = makeFetch(async () =>
      new Response(
        JSON.stringify({
          ok: false,
          error: 'chat_service_disabled',
          message: '聊天服务未启用，请先启动后端',
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );
    vi.stubGlobal('fetch', fetchMock);
    render(<ChatPage />);
    fireEvent.change(screen.getByPlaceholderText('跟你的AI说点什么吧…'), {
      target: { value: '在吗' },
    });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    // 走 unavailable + markFailed 路径：用户气泡带未送达标记
    await screen.findByText('未送达');
    // 故障说明文本绝不渲染成AI气泡
    expect(screen.queryByText(/聊天服务未启用/)).not.toBeInTheDocument();
    expect(screen.queryByText(/chat_service_disabled/)).not.toBeInTheDocument();
    // 无AI气泡头像
    expect(bubbleIcons().bot).toBe(0);
    // 提示条常显
    expect(screen.getByText(/消息暂时送不到/)).toBeInTheDocument();
    // 只有用户自己的一条气泡
    const bubbles = document.querySelectorAll('.rounded-2xl.px-3\\.5');
    expect(bubbles.length).toBe(1);
  });

  it('H3 表情端点成功：渲染 clean_text AI气泡 + Bot 图标 + 提示条隐藏', async () => {
    // 后端表情端点真实形状：200 + {ok, clean_text, mood, raw}（标签已由后端解析剥离）
    fetchMock = makeFetch(async () =>
      new Response(
        JSON.stringify({
          ok: true,
          clean_text: '今天真开心',
          mood: 'happy',
          raw: '[emotion:happy]今天真开心',
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );
    vi.stubGlobal('fetch', fetchMock);
    render(<ChatPage />);
    fireEvent.change(screen.getByPlaceholderText('跟你的AI说点什么吧…'), {
      target: { value: '你好呀' },
    });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    // AI气泡渲染 clean_text（已识别标签已由后端剥离）
    expect(await screen.findByText('今天真开心')).toBeInTheDocument();
    // 原始带标签文本不得直接展示
    expect(screen.queryByText('[emotion:happy]今天真开心')).not.toBeInTheDocument();
    // 两侧头像：用户 User + AI Bot（图标语义化，非 emoji）
    const icons = bubbleIcons();
    expect(icons.user).toBe(1);
    expect(icons.bot).toBe(1);
    // 确认连通后提示条隐藏（channel === 'connected'）
    expect(screen.queryByText(/消息暂时送不到/)).not.toBeInTheDocument();
    // 表情总线：LLM 情绪发布给桌宠悬浮窗（mood 白名单内原样发布）
    const moodRecord = JSON.parse(
      window.localStorage.getItem('cx-a.petMood') ?? '{}',
    ) as { mood?: string };
    expect(moodRecord.mood).toBe('happy');
    // 聊天刷新总线：发送成功后写 tick（其他窗口重载历史）
    expect(window.localStorage.getItem('cx-a.chatTick')).toBeTruthy();
  });
});

/* ────────────────────────────────────────────────────────────────────────────
 * 追加（语音接线）：朗读回复与麦克风输入的交互契约。
 * 既有用例与断言零修改，本段为纯追加。
 * ──────────────────────────────────────────────────────────────────────────── */

describe('ChatPage：语音接线（朗读 + 麦克风）', () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  let playMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    playMock = vi.fn(async () => undefined);
    class FakeAudio {
      onended: (() => void) | null = null;
      onerror: (() => void) | null = null;
      pause = vi.fn();
      play = playMock;
    }
    vi.stubGlobal('Audio', FakeAudio);
    vi.spyOn(console, 'error').mockImplementation(() => {});
    recorderMocks.stop.mockResolvedValue({
      pcm: new Int16Array(0),
      audioBase64: 'AAA=',
      sampleRate: 16000,
      durationSec: 1,
      sampleCount: 0,
    });
    recorderMocks.start.mockResolvedValue({
      stop: recorderMocks.stop,
      cancel: recorderMocks.cancel,
      active: true,
    });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    vi.clearAllMocks();
  });

  /** 构造「聊天回复 + 流式合成音频」双端点替身 fetch（/chat/history 恒回空历史）。 */
  function stubChatAndSynth(): ReturnType<typeof vi.fn> {
    const mock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/chat/history')) {
        return new Response(JSON.stringify({ ok: true, messages: [] }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.includes('/chat/message')) {
        return new Response(JSON.stringify({ ok: true, clean_text: '我在呢' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.includes('/voice/synthesize_stream')) {
        const ndjson =
          JSON.stringify({ seq: 0, text: '我在呢', audio_base64: 'UklGRg==' }) + '\n' +
          JSON.stringify({ done: true, total: 1 }) + '\n';
        const stream = new ReadableStream({
          start(controller) {
            controller.enqueue(new TextEncoder().encode(ndjson));
            controller.close();
          },
        });
        return new Response(stream, {
          status: 200,
          headers: { 'Content-Type': 'application/x-ndjson' },
        });
      }
      throw new Error(`unexpected url: ${url}`);
    });
    vi.stubGlobal('fetch', mock);
    return mock;
  }

  it('AI回复带朗读按钮：点击请求流式合成端点并播放', async () => {
    fetchMock = stubChatAndSynth();

    render(<ChatPage />);
    fireEvent.change(screen.getByPlaceholderText('跟你的AI说点什么吧…'), {
      target: { value: '在吗' },
    });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    expect(await screen.findByText('我在呢')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '朗读' }));
    await vi.waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        API_ENDPOINTS.voice.synthesizeStream,
        expect.objectContaining({ method: 'POST' }),
      );
      expect(playMock).toHaveBeenCalledTimes(1);
    });
  });

  it('朗读开关打开时新回复自动朗读（无需点击气泡按钮）', async () => {
    stubChatAndSynth();

    render(<ChatPage />);
    fireEvent.click(screen.getByRole('switch', { name: '朗读回复' }));
    fireEvent.change(screen.getByPlaceholderText('跟你的AI说点什么吧…'), {
      target: { value: '在吗' },
    });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    expect(await screen.findByText('我在呢')).toBeInTheDocument();

    await vi.waitFor(() => {
      expect(playMock).toHaveBeenCalledTimes(1);
    });
  });

  it('TTS 前剔除：送入合成的文本是剔除标签后的 clean_text（raw 带标签也不进 TTS）', async () => {
    // 后端契约：clean_text 已剥离已识别标签；raw 保留原始带标签全文。
    // 前端朗读必须送 clean_text——本用例把 raw 塞回响应以验证前端不会误用。
    const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes('/chat/history')) {
        return new Response(JSON.stringify({ ok: true, messages: [] }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.includes('/chat/message')) {
        return new Response(
          JSON.stringify({ ok: true, clean_text: '我在呢', raw: '[emotion:happy]我在呢' }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      }
      if (url.includes('/voice/synthesize_stream')) {
        const ndjson =
          JSON.stringify({ seq: 0, text: '我在呢', audio_base64: 'UklGRg==' }) + '\n' +
          JSON.stringify({ done: true, total: 1 }) + '\n';
        const stream = new ReadableStream({
          start(controller) {
            controller.enqueue(new TextEncoder().encode(ndjson));
            controller.close();
          },
        });
        return new Response(stream, {
          status: 200,
          headers: { 'Content-Type': 'application/x-ndjson' },
        });
      }
      throw new Error(`unexpected url: ${url}`);
    });
    vi.stubGlobal('fetch', mock);

    render(<ChatPage />);
    fireEvent.click(screen.getByRole('switch', { name: '朗读回复' }));
    fireEvent.change(screen.getByPlaceholderText('跟你的AI说点什么吧…'), {
      target: { value: '在吗' },
    });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    expect(await screen.findByText('我在呢')).toBeInTheDocument();

    await vi.waitFor(() => expect(playMock).toHaveBeenCalledTimes(1));
    const synthCall = mock.mock.calls.find(([, reqInit]) =>
      String(reqInit?.body ?? '').includes('我在呢'),
    );
    expect(synthCall).toBeTruthy();
    const synthBody = JSON.parse(String(synthCall?.[1]?.body ?? '{}')) as { text?: string };
    expect(synthBody.text).toBe('我在呢');
    expect(synthBody.text).not.toContain('[emotion:');
  });

  it('麦克风按钮：开始录音 → 停止后识别文本填入输入框（不自动发送）', async () => {
    fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/chat/history')) {
        return new Response(JSON.stringify({ ok: true, messages: [] }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.includes('/voice/transcribe')) {
        return new Response(JSON.stringify({ ok: true, text: '语音转写结果' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      throw new Error(`unexpected url: ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<ChatPage />);
    fireEvent.click(screen.getByRole('button', { name: '语音输入' }));
    await vi.waitFor(() => {
      expect(recorderMocks.start).toHaveBeenCalledTimes(1);
    });

    // 录音态是异步 state 更新：等待「停止」按钮渲染完成后再点击
    const stopButton = await screen.findByRole('button', { name: '停止语音输入' });
    fireEvent.click(stopButton);
    await vi.waitFor(() => {
      expect(recorderMocks.stop).toHaveBeenCalledTimes(1);
      expect(fetchMock).toHaveBeenCalledWith(
        API_ENDPOINTS.voice.transcribe,
        expect.objectContaining({ method: 'POST' }),
      );
    });
    await vi.waitFor(() => {
      expect(screen.getByPlaceholderText('跟你的AI说点什么吧…')).toHaveValue('语音转写结果');
    });
  });
});