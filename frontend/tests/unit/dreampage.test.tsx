import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

/**
 * 梦境页五态与降级单测（20261127 迁移自 CX-O dreamtalk）。
 *
 * 覆盖：入睡 → 回合流 → 醒来摘要 → 档案；无梦视图；接口失败降级提示条；
 * 在途提交禁用重复点击。
 */

const apiMocks = vi.hoisted(() => ({
  fetchDreamtalkHealth: vi.fn(),
  fetchDreamtalkRecalls: vi.fn(),
  fetchDreamtalkSession: vi.fn(),
  postDreamtalkMessage: vi.fn(),
  startDreamtalkSession: vi.fn(),
  wakeDreamtalkSession: vi.fn(),
}));

vi.mock('../../src/renderer/api', () => apiMocks);

import DreamPage from '../../src/renderer/pages/DreamPage';

const health = {
  ok: true as const,
  status: 'idle' as const,
  enabled: true,
  contract_version: '1.0',
  sessions: 0,
  mode_weights: 'memory=0.5,wild=0.3,dreamless=0.2',
  memory_writeback: true,
};

function session(overrides: Record<string, unknown> = {}) {
  return {
    id: 's1',
    agent_id: 'default',
    created_at: '2026-11-27T22:00:00',
    mode: 'memory',
    mode_reason: '记忆的碎片被悄悄织进梦里',
    status: 'active',
    title: '',
    summary: '',
    emotion_residue: { valence: 0, arousal: 0 },
    ended_at: null,
    turn_count: 2,
    ...overrides,
  };
}

function turn(seq: number, speaker: string, content: string) {
  return {
    id: `s1:${seq}`,
    session_id: 's1',
    seq,
    speaker,
    content,
    flagged: false,
    created_at: '2026-11-27T22:00:00',
  };
}

const activeView = {
  ok: true as const,
  session: session(),
  turns: [turn(1, 'director', '雾把走廊的轮廓擦掉了。'), turn(2, 'dreamer', '我向前走。')],
  recall: null,
};

const endedView = {
  ok: true as const,
  session: session({
    status: 'ended',
    title: '倒挂的窗',
    summary: '潮水退去后，窗里长出一片森林。',
    emotion_residue: { valence: 0.2, arousal: 0.4 },
  }),
  turns: activeView.turns,
  recall: {
    session_id: 's1',
    created_at: '2026-11-27T22:00:00',
    mode: 'memory',
    title: '倒挂的窗',
    summary: '潮水退去后，窗里长出一片森林。',
    emotion_residue: { valence: 0.2, arousal: 0.4 },
  },
};

beforeEach(() => {
  Object.values(apiMocks).forEach((mock) => mock.mockReset());
  apiMocks.fetchDreamtalkHealth.mockResolvedValue(health);
  apiMocks.fetchDreamtalkRecalls.mockResolvedValue({ ok: true, items: [], total: 0 });
});

afterEach(() => {
  cleanup();
});

describe('DreamPage：主链路', () => {
  it('入睡 → 回合流 → 醒来摘要 → 查看档案', async () => {
    apiMocks.startDreamtalkSession.mockResolvedValue(activeView);
    apiMocks.wakeDreamtalkSession.mockResolvedValue(endedView);
    apiMocks.fetchDreamtalkSession.mockResolvedValue(endedView);

    render(<DreamPage />);
    await waitFor(() => expect(apiMocks.fetchDreamtalkHealth).toHaveBeenCalled());

    fireEvent.click(screen.getByTestId('dream-start'));
    await waitFor(() => expect(screen.getByTestId('dream-turns')).toBeTruthy());
    expect(screen.getByText('雾把走廊的轮廓擦掉了。')).toBeTruthy();
    expect(screen.getByText('潜意识导演 · 1')).toBeTruthy();
    expect(apiMocks.startDreamtalkSession).toHaveBeenCalledWith('auto');

    fireEvent.click(screen.getByTestId('dream-wake'));
    await waitFor(() => expect(screen.getByTestId('dream-wake-panel')).toBeTruthy());
    expect(screen.getByText('倒挂的窗')).toBeTruthy();
    expect(screen.getByText(/梦的原文只留在梦境档案/)).toBeTruthy();

    fireEvent.click(screen.getByTestId('dream-open-archive'));
    await waitFor(() => expect(screen.getByTestId('dream-archive')).toBeTruthy());
    expect(apiMocks.fetchDreamtalkSession).toHaveBeenCalledWith('s1');
    expect(screen.getByText('我向前走。')).toBeTruthy();
  });

  it('继续做梦只发送输入内容（留空即继续）', async () => {
    apiMocks.startDreamtalkSession.mockResolvedValue(activeView);
    apiMocks.postDreamtalkMessage.mockResolvedValue(activeView);

    render(<DreamPage />);
    await waitFor(() => expect(apiMocks.fetchDreamtalkHealth).toHaveBeenCalled());
    fireEvent.click(screen.getByTestId('dream-start'));
    await waitFor(() => expect(screen.getByTestId('dream-continue')).toBeTruthy());

    fireEvent.click(screen.getByTestId('dream-continue'));
    await waitFor(() => expect(apiMocks.postDreamtalkMessage).toHaveBeenCalledWith('s1', ''));
  });
});

describe('DreamPage：无梦与降级', () => {
  it('一夜无梦渲染无梦视图，不进入回合流', async () => {
    apiMocks.startDreamtalkSession.mockResolvedValue({
      ok: true,
      session: session({
        mode: 'dreamless',
        status: 'ended',
        mode_reason: '今夜没有记忆碎片，意识沉入安静的深水，今夜无梦',
        title: '一夜无梦',
        summary: '没有画面，也没有声音。',
      }),
      turns: [],
      recall: null,
    });

    render(<DreamPage />);
    await waitFor(() => expect(apiMocks.fetchDreamtalkHealth).toHaveBeenCalled());
    fireEvent.click(screen.getByTestId('dream-start'));

    await waitFor(() => expect(screen.getByTestId('dream-empty')).toBeTruthy());
    expect(screen.getByText('今夜无梦')).toBeTruthy();
    expect(screen.queryByTestId('dream-turns')).toBeNull();
  });

  it('后端不可达时给中文降级提示条且不白屏', async () => {
    apiMocks.fetchDreamtalkHealth.mockRejectedValue(new Error('backend down'));
    apiMocks.fetchDreamtalkRecalls.mockRejectedValue(new Error('backend down'));

    render(<DreamPage />);
    await waitFor(() => expect(screen.getByTestId('dream-offline')).toBeTruthy());
    expect(screen.getByText(/暂时读不到本机记忆/)).toBeTruthy();
    expect(screen.getByTestId('dream-start')).toBeTruthy();
    expect(screen.getByTestId('dream-page')).toBeTruthy();
  });

  it('入睡失败显示错误提示条', async () => {
    apiMocks.startDreamtalkSession.mockRejectedValue(new Error('后端请求失败: 503'));

    render(<DreamPage />);
    await waitFor(() => expect(apiMocks.fetchDreamtalkHealth).toHaveBeenCalled());
    fireEvent.click(screen.getByTestId('dream-start'));

    await waitFor(() => expect(screen.getByTestId('dream-error')).toBeTruthy());
    expect(screen.getByText(/梦境暂时不可用/)).toBeTruthy();
  });

  it('在途提交禁用重复点击', async () => {
    let resolveStart: (value: unknown) => void = () => undefined;
    apiMocks.startDreamtalkSession.mockImplementation(
      () => new Promise((resolve) => {
        resolveStart = resolve;
      }),
    );

    render(<DreamPage />);
    await waitFor(() => expect(apiMocks.fetchDreamtalkHealth).toHaveBeenCalled());
    const button = screen.getByTestId('dream-start') as HTMLButtonElement;
    fireEvent.click(button);
    await waitFor(() => expect(button.disabled).toBe(true));
    fireEvent.click(button);
    expect(apiMocks.startDreamtalkSession).toHaveBeenCalledTimes(1);

    resolveStart(activeView);
    await waitFor(() => expect(screen.getByTestId('dream-turns')).toBeTruthy());
  });
});
