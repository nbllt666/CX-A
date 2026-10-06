import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react';
import PetOverlay from '../../src/renderer/components/PetOverlay';

/**
 * 悬浮窗语音闭环组件级单测（20261004 悬浮窗语音/视觉/授权闭环）：
 *
 * 「说话」= 语音输入开关的确定性行为验证（真实录音依赖物理麦克风，e2e 不断言）：
 *   开启录音（聆听，aria-pressed=true）→ 再点停止 → 识别 → 直发 /api/chat/message
 *   → 表情 pushMood（总线 + 本窗口）→ 聊天刷新总线 tick → 流式合成朗读（口型）。
 *
 * VrmAvatar / bridge / API / 录音全部替身化：本套件只验证「页面 ↔ 模块 ↔ 接口」
 * 契约；VRM 渲染由 e2e pet-vrm 覆盖，录音采集逻辑由 audiorecorder.test 覆盖。
 */

const recorderMocks = vi.hoisted(() => ({
  start: vi.fn(),
  stop: vi.fn(),
  cancel: vi.fn(),
}));

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
  startRecording: recorderMocks.start,
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

    recorderMocks.start.mockResolvedValue({
      stop: recorderMocks.stop,
      cancel: recorderMocks.cancel,
      active: true,
    });
    recorderMocks.stop.mockResolvedValue({
      pcm: new Int16Array(1600),
      audioBase64: 'AAA=',
      sampleRate: 16000,
      durationSec: 1,
      sampleCount: 1600,
    });
    bridgeMocks.bounds.mockResolvedValue({ min: 160, max: 640 });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    vi.clearAllMocks();
    window.localStorage.clear();
  });

  it('六项菜单齐全：打开主窗口/说话/屏幕共享/操作授权/大小/关闭', async () => {
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

    for (const name of ['打开主窗口', '说话', '屏幕共享', '操作授权', '大小', '关闭']) {
      expect(await screen.findByRole('button', { name })).toBeInTheDocument();
    }
  });

  it('说话闭环：录音 → 识别 → 发送 → 表情/刷新总线 → 流式朗读（口型开）', async () => {
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

    // ① 开启录音：aria-pressed=true（聆听中），data-talking 仍 false（非朗读）
    fireEvent.click(await screen.findByRole('button', { name: '说话' }));
    await waitFor(() => {
      expect(recorderMocks.start).toHaveBeenCalledTimes(1);
      expect(screen.getByRole('button', { name: '说话' })).toHaveAttribute(
        'aria-pressed',
        'true',
      );
    });
    expect(screen.getByTestId('pet-overlay-root')).toHaveAttribute('data-talking', 'false');

    // ② 再点停止：识别 → 直发对话 → 表情 + 刷新总线 + 朗读
    fireEvent.click(screen.getByRole('button', { name: '说话' }));
    await waitFor(() => {
      expect(recorderMocks.stop).toHaveBeenCalledTimes(1);
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
  });

  it('识别为空/失败：不伪造对话，状态回到空闲', async () => {
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
    fireEvent.click(await screen.findByRole('button', { name: '说话' }));
    // 等聆听态就位（state 异步更新）再点停止，避免第二击落入空闲分支
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '说话' })).toHaveAttribute(
        'aria-pressed',
        'true',
      );
    });
    fireEvent.click(screen.getByRole('button', { name: '说话' }));

    await waitFor(() => {
      expect(recorderMocks.stop).toHaveBeenCalledTimes(1);
    });
    // 识别为空 → 不发送对话、不写刷新总线、不朗读
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '说话' })).toHaveAttribute(
        'aria-pressed',
        'false',
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
