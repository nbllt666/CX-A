import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react';
import PetOverlay from '../../src/renderer/components/PetOverlay';

/**
 * 悬浮窗语音会话组件级单测（20261006 全双工降级版：麦克风按钮 = 会话总开关）：
 *
 * 「麦克风」= 语音会话开关（voice.interaction_mode 决定内部机制，默认 vad）：
 * 点按开启持续采集（能量 VAD 自动断句）→ 断句产出自动识别 → 直发 /api/chat/message
 * → 表情 pushMood（总线 + 本窗口）→ 聊天刷新总线 tick → 流式合成朗读（口型）。
 * 会话编排在 voiceSession.ts（其纯逻辑由 voicesession.test 覆盖）；本套件验证
 * 「组件 ↔ 会话 ↔ 接口」接线：按钮名/态、断句触发链路、关闭清理。
 *
 * VrmAvatar / 持续采集 / bridge / API 全部替身化：VRM 渲染由 e2e pet-vrm 覆盖。
 */

const continuousMocks = vi.hoisted(() => {
  type Handlers = {
    onUtterance: (audio: { audioBase64: string; sampleRate: number; durationSec: number }) => void;
    onLevel?: (rms: number) => void;
    onError?: (err: Error) => void;
  };
  const captured: Handlers[] = [];
  return {
    captured,
    /** 开始持续采集：捕获 handlers 供测试直断句；返回可控会话句柄 */
    start: vi.fn(async (handlers: Handlers) => {
      captured.push(handlers);
      return {
        get active() {
          return true;
        },
        stop: vi.fn(),
        clearBuffer: vi.fn(),
      };
    }),
  };
});

const bridgeMocks = vi.hoisted(() => ({
  close: vi.fn(async () => undefined),
  dragStart: vi.fn(async () => undefined),
  dragEnd: vi.fn(async () => undefined),
  bounds: vi.fn(async () => ({ min: 160, max: 640 })),
  resize: vi.fn(async () => undefined),
  showMain: vi.fn(async () => undefined),
}));

vi.mock('../../src/renderer/components/VrmAvatar', () => ({
  default: function FakeVrmAvatar() {
    return <div data-testid="fake-vrm" />;
  },
}));

vi.mock('../../src/renderer/audioRecorder', () => ({
  TARGET_SAMPLE_RATE: 16000,
  startContinuousRecording: continuousMocks.start,
}));

vi.mock('../../src/renderer/bridge', () => ({
  closePetOverlay: bridgeMocks.close,
  dragPetOverlayStart: bridgeMocks.dragStart,
  dragPetOverlayEnd: bridgeMocks.dragEnd,
  getPetOverlaySizeBounds: bridgeMocks.bounds,
  resizePetOverlay: bridgeMocks.resize,
  showMainWindow: bridgeMocks.showMain,
}));

/** 合成一条 NDJSON 流式合成响应（一帧音频 + done）。 */
function synthStreamResponse(): Response {
  const ndjson =
    JSON.stringify({ seq: 0, text: '我在呢', audio_base64: 'UklGRg==' }) +
    '\n' +
    JSON.stringify({ done: true, total: 1 }) +
    '\n';
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(ndjson));
      controller.close();
    },
  });
  return new Response(stream, { status: 200, headers: { 'Content-Type': 'application/x-ndjson' } });
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** 合成一条 NDJSON 流式对话响应（delta 增量帧 + done 权威收口帧）。 */
function chatStreamResponse(): Response {
  const ndjson =
    JSON.stringify({ delta: '[emotion:happy]' }) +
    '\n' +
    JSON.stringify({ delta: '我在呢' }) +
    '\n' +
    JSON.stringify({ done: true, ok: true, clean_text: '我在呢', mood: 'happy', raw: '[emotion:happy]我在呢' }) +
    '\n';
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(ndjson));
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { 'Content-Type': 'application/x-ndjson; charset=utf-8' },
  });
}

function openMenu() {
  fireEvent.pointerDown(screen.getByTestId('fake-vrm').parentElement!, {
    button: 0,
    isPrimary: true,
    pointerId: 1,
  });
  fireEvent.pointerUp(screen.getByTestId('fake-vrm').parentElement!, {
    button: 0,
    isPrimary: true,
    pointerId: 1,
  });
}

describe('PetOverlay：悬浮窗语音闭环', () => {
  let playMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.stubGlobal(
      'Audio',
      class {
        onended: (() => void) | null = null;
        onerror: (() => void) | null = null;
        pause = vi.fn();
        play = playMock;
      },
    );
    playMock = vi.fn(async () => undefined);
    vi.spyOn(console, 'error').mockImplementation(() => {});
    window.localStorage.clear();
    continuousMocks.captured.length = 0;
    continuousMocks.start.mockClear();
    bridgeMocks.bounds.mockResolvedValue({ min: 160, max: 640 });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    vi.clearAllMocks();
    window.localStorage.clear();
  });

  it('六项菜单齐全：打开主窗口/麦克风/屏幕共享/操作授权/大小/关闭', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/settings')) {
        return jsonResponse({ config: { vision: { enabled: false } } });
      }
      if (url.includes('/computer/status')) {
        return jsonResponse({ authorized: false, confirm_dangerous: true });
      }
      if (url.includes('/chat/history')) {
        return jsonResponse({ ok: true, messages: [] });
      }
      throw new Error(`unexpected url: ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<PetOverlay />);
    openMenu();

    for (const name of ['打开主窗口', '麦克风', '屏幕共享', '操作授权', '大小', '关闭']) {
      expect(await screen.findByRole('button', { name })).toBeInTheDocument();
    }
  });

  it('大小滑块定位在按钮坐标系内：窗口合成位置不越界（20261006 越界修复回归锁）', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/settings')) {
        return jsonResponse({ config: { vision: { enabled: false } } });
      }
      if (url.includes('/computer/status')) {
        return jsonResponse({ authorized: false, confirm_dangerous: true });
      }
      if (url.includes('/chat/history')) {
        return jsonResponse({ ok: true, messages: [] });
      }
      throw new Error(`unexpected url: ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<PetOverlay />);
    openMenu();

    const sizeBtn = await screen.findByRole('button', { name: '大小' });
    // aria-label 在 range input 上，定位 style 在其父级胶囊 span（.pet-overlay-slider）
    const sliderHost = (await screen.findByLabelText('大小滑块')).parentElement as HTMLElement;
    const btnLeft = parseFloat(sizeBtn.style.left);
    const btnTop = parseFloat(sizeBtn.style.top);
    const btnSize = parseFloat(sizeBtn.style.height);
    const sliderLeft = parseFloat(sliderHost.style.left);
    const sliderTop = parseFloat(sliderHost.style.top);

    // 相对按钮系断言：垂直 = 按钮高 + 6（正下方）；水平为 clamp 换算后的有限偏移
    // （回归锁：修复前 left/top 用的是窗口坐标值，叠加按钮位置后飞出窗口）
    expect(sliderTop).toBeCloseTo(btnSize + 6, 5);
    expect(Math.abs(sliderLeft)).toBeLessThan(220);

    // 窗口合成位置（按钮位置 + 相对偏移）完整落在窗口内
    const sliderWinLeft = btnLeft + sliderLeft;
    const sliderWinTop = btnTop + sliderTop;
    expect(sliderWinLeft).toBeGreaterThanOrEqual(0);
    expect(sliderWinLeft + 176).toBeLessThanOrEqual(window.innerWidth);
    expect(sliderWinTop).toBeGreaterThanOrEqual(0);
    expect(sliderWinTop).toBeLessThanOrEqual(window.innerHeight);
  });

  it('麦克风会话闭环（VAD 模式）：断句→识别→发送→表情/刷新总线→流式朗读；再点关闭', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/settings')) {
        return jsonResponse({ config: { vision: { enabled: false } } });
      }
      if (url.includes('/computer/status')) {
        return jsonResponse({ authorized: false, confirm_dangerous: true });
      }
      if (url.includes('/chat/history')) {
        return jsonResponse({ ok: true, messages: [] });
      }
      if (url.includes('/voice/transcribe')) {
        return jsonResponse({ ok: true, text: '在吗' });
      }
      // 流式对话端点（20261010 语音低延迟）：必须先于 /chat/message 判断
      // （'/chat/message_stream'.includes('/chat/message') 为真，顺序反了会错路由）
      if (url.includes('/chat/message_stream')) {
        return chatStreamResponse();
      }
      if (url.includes('/chat/message')) {
        return jsonResponse({
          ok: true,
          clean_text: '我在呢',
          mood: 'happy',
          raw: '[emotion:happy]我在呢',
        });
      }
      if (url.includes('/voice/synthesize_stream')) {
        return synthStreamResponse();
      }
      throw new Error(`unexpected url: ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<PetOverlay />);
    openMenu();

    // ① 点麦克风：会话开启（持续采集拉起，aria-pressed=true），data-talking 仍 false
    fireEvent.click(await screen.findByRole('button', { name: '麦克风' }));
    await waitFor(() => {
      expect(continuousMocks.start).toHaveBeenCalledTimes(1);
      expect(screen.getByRole('button', { name: '麦克风' })).toHaveAttribute(
        'aria-pressed',
        'true',
      );
    });
    expect(screen.getByTestId('pet-overlay-root')).toHaveAttribute('data-talking', 'false');

    // ② VAD 断句产出一段语音 → 自动识别 → 发送 → 表情 + 刷新总线 + 朗读
    continuousMocks.captured[0].onUtterance({
      audioBase64: 'aGk=',
      sampleRate: 16000,
      durationSec: 1,
    });
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/voice/transcribe'),
        expect.objectContaining({ method: 'POST' }),
      );
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/chat/message'),
        expect.objectContaining({ method: 'POST' }),
      );
    });
    // 表情总线（跨窗口）+ 本窗口立即生效：happy 落 localStorage
    expect(JSON.parse(window.localStorage.getItem('cx-a.petMood') ?? '{}')).toMatchObject({
      mood: 'happy',
    });
    // 聊天刷新总线：主窗口聊天页据此重载历史
    expect(window.localStorage.getItem('cx-a.chatTick')).toBeTruthy();
    // 流式合成 + 播放（口型由 data-talking 驱动）
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/voice/synthesize_stream'),
        expect.objectContaining({ method: 'POST' }),
      );
      expect(playMock).toHaveBeenCalledTimes(1);
    });
    expect(screen.getByTestId('pet-overlay-root')).toHaveAttribute('data-talking', 'true');

    // ③ 再点麦克风：关闭会话（aria-pressed=false）
    fireEvent.click(screen.getByRole('button', { name: '麦克风' }));
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '麦克风' })).toHaveAttribute(
        'aria-pressed',
        'false',
      );
    });
  });

  it('识别为空：会话保持聆听（不关闭）、不伪造对话', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/settings')) {
        return jsonResponse({ config: { vision: { enabled: false } } });
      }
      if (url.includes('/computer/status')) {
        return jsonResponse({ authorized: false, confirm_dangerous: true });
      }
      if (url.includes('/chat/history')) {
        return jsonResponse({ ok: true, messages: [] });
      }
      if (url.includes('/voice/transcribe')) {
        return jsonResponse({ ok: true, text: '' });
      }
      throw new Error(`unexpected url: ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<PetOverlay />);
    openMenu();
    fireEvent.click(await screen.findByRole('button', { name: '麦克风' }));
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '麦克风' })).toHaveAttribute(
        'aria-pressed',
        'true',
      );
    });

    // VAD 断句产出 → 识别为空 → 回聆听（会话仍开），不发送对话、不写刷新总线
    continuousMocks.captured[0].onUtterance({
      audioBase64: 'aGk=',
      sampleRate: 16000,
      durationSec: 1,
    });
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/voice/transcribe'),
        expect.objectContaining({ method: 'POST' }),
      );
    });
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '麦克风' })).toHaveAttribute(
        'aria-pressed',
        'true',
      );
    });
    expect(fetchMock).not.toHaveBeenCalledWith(
      expect.stringContaining('/chat/message'),
      expect.anything(),
    );
    expect(window.localStorage.getItem('cx-a.chatTick')).toBeNull();
  });

  it('屏幕共享开关：点击热更新 vision.enabled（乐观更新 + 服务端回执）', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes('/settings') && (init?.method ?? 'GET') === 'PUT') {
        return jsonResponse({ config: { vision: { enabled: true } } });
      }
      if (url.includes('/settings')) {
        return jsonResponse({ config: { vision: { enabled: false } } });
      }
      if (url.includes('/computer/status')) {
        return jsonResponse({ authorized: false, confirm_dangerous: true });
      }
      if (url.includes('/chat/history')) {
        return jsonResponse({ ok: true, messages: [] });
      }
      throw new Error(`unexpected url: ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<PetOverlay />);
    openMenu();
    const button = await screen.findByRole('button', { name: '屏幕共享' });
    expect(button).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(button);
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/settings'),
        expect.objectContaining({ method: 'PUT' }),
      );
      expect(screen.getByRole('button', { name: '屏幕共享' })).toHaveAttribute(
        'aria-pressed',
        'true',
      );
    });
  });

  it('操作授权：未授权点击弹确认 → 确认后 POST authorize 开启', async () => {
    const confirmMock = vi.fn(() => true);
    vi.stubGlobal('confirm', confirmMock);
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes('/computer/status')) {
        return jsonResponse({ authorized: false, confirm_dangerous: true });
      }
      if (url.includes('/computer/authorize')) {
        return jsonResponse({ authorized: true, confirm_dangerous: true });
      }
      if (url.includes('/settings')) {
        return jsonResponse({ config: { vision: { enabled: false } } });
      }
      if (url.includes('/chat/history')) {
        return jsonResponse({ ok: true, messages: [] });
      }
      throw new Error(`unexpected url: ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<PetOverlay />);
    openMenu();
    fireEvent.click(await screen.findByRole('button', { name: '操作授权' }));

    await waitFor(() => {
      expect(confirmMock).toHaveBeenCalledTimes(1);
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/computer/authorize'),
        expect.objectContaining({ method: 'POST' }),
      );
      expect(screen.getByRole('button', { name: '操作授权' })).toHaveAttribute(
        'aria-pressed',
        'true',
      );
    });
  });

  it('操作授权：确认弹窗取消 → 不发起授权请求', async () => {
    const confirmMock = vi.fn(() => false);
    vi.stubGlobal('confirm', confirmMock);
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/computer/status')) {
        return jsonResponse({ authorized: false, confirm_dangerous: true });
      }
      if (url.includes('/settings')) {
        return jsonResponse({ config: { vision: { enabled: false } } });
      }
      if (url.includes('/chat/history')) {
        return jsonResponse({ ok: true, messages: [] });
      }
      throw new Error(`unexpected url: ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<PetOverlay />);
    openMenu();
    fireEvent.click(await screen.findByRole('button', { name: '操作授权' }));

    await waitFor(() => {
      expect(confirmMock).toHaveBeenCalledTimes(1);
    });
    expect(fetchMock).not.toHaveBeenCalledWith(
      expect.stringContaining('/computer/authorize'),
      expect.anything(),
    );
  });
});

describe('PetOverlay：大小滑块跟手（rAF 合并 + 记忆防抖，20261010 跟手性回归锁）', () => {
  /** 渲染 + 打开菜单 + 等挂载校准的 resize 落地后清基线（只计滑块触发的调用）。 */
  async function renderAndClearBaseline() {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/settings')) {
        return jsonResponse({ config: { vision: { enabled: false } } });
      }
      if (url.includes('/computer/status')) {
        return jsonResponse({ authorized: false, confirm_dangerous: true });
      }
      if (url.includes('/chat/history')) {
        return jsonResponse({ ok: true, messages: [] });
      }
      throw new Error(`unexpected url: ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<PetOverlay />);
    openMenu();
    const slider = await screen.findByLabelText('大小滑块');
    // 挂载校准（幂等 resize + bounds 收紧）会直发 resize：等其稳定后清基线，
    // 后续断言只统计滑块 onChange 触发的调用
    await waitFor(() => expect(bridgeMocks.resize).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 50));
    bridgeMocks.resize.mockClear();
    return slider;
  }

  it('同帧多次 onChange 合并为一次窗口 resize，发送的是最后一个值', async () => {
    const slider = await renderAndClearBaseline();

    fireEvent.change(slider, { target: { value: '300' } });
    fireEvent.change(slider, { target: { value: '320' } });
    await waitFor(() => expect(bridgeMocks.resize).toHaveBeenCalledTimes(1));
    expect(bridgeMocks.resize).toHaveBeenLastCalledWith(320);

    // rAF 句柄已归还：下一帧再次拖动仍能触发新一轮 resize
    fireEvent.change(slider, { target: { value: '340' } });
    await waitFor(() => expect(bridgeMocks.resize).toHaveBeenCalledTimes(2));
    expect(bridgeMocks.resize).toHaveBeenLastCalledWith(340);
  });

  it('尺寸记忆防抖落盘：拖动中不写 localStorage，停手后写入最终值', async () => {
    const slider = await renderAndClearBaseline();

    fireEvent.change(slider, { target: { value: '300' } });
    // 防抖窗口（300ms）内不落盘——拖动中不做同步存储阻塞
    expect(window.localStorage.getItem('cx-a.petSize')).not.toBe('300');
    await waitFor(
      () => expect(window.localStorage.getItem('cx-a.petSize')).toBe('300'),
      { timeout: 1500 },
    );
  });
});
