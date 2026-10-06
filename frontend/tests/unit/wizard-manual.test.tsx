import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react';

/**
 * 手动路径直达（Task 6）。
 *
 * 风格照抄 setupwizard.test.tsx：模块级部分 mock `../src/renderer/api`，
 * 断言用户可见行为。覆盖：
 *  1. 「我自己挑」手动配置三项（本地开关 / 显卡 / 档位）均有值 → 出现「完成并保存」，
 *     点击直达确认页（云端 / 线路 / 下载步骤被跳过），提交体不含 cloud 段、
 *     线路落默认 mirror、local_llm.enabled 按用户选择、apply_recommended=false；
 *  2. 手动关掉本地开关后直达 → 提交体 local_llm.enabled=false；
 *  3. 推荐路径回归：「就用推荐的」仍走线路步骤（不直达确认页）；
 *  4. 手动模式保留「下一步」（想配云端大脑的用户不受影响）。
 */
vi.mock('../../src/renderer/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/renderer/api')>();
  return {
    ...actual,
    fetchSetupStatus: vi.fn(),
    fetchSetupRecommend: vi.fn(),
    completeSetup: vi.fn(),
    startModelDownload: vi.fn(),
    fetchModelProgress: vi.fn(),
    cancelModelDownload: vi.fn(),
  };
});

import SetupWizard from '../../src/renderer/pages/SetupWizard';
import * as api from '../../src/renderer/api';

const fetchSetupStatusMock = vi.mocked(api.fetchSetupStatus);
const fetchSetupRecommendMock = vi.mocked(api.fetchSetupRecommend);
const completeSetupMock = vi.mocked(api.completeSetup);

const STATUS_REQUIRED: api.SetupStatusView = {
  completed: false,
  wizard_required: true,
  download: { channel: 'mirror' },
  local_llm_source: 'modelscope',
};

const RECOMMEND_OK: api.SetupRecommendResult = {
  profile: {
    cpu_cores: 8,
    ram_gb: 16,
    gpu_vendor: 'nvidia',
    vram_gb: 8,
    cuda_version: '12.4',
    disk_free_gb: 120,
    probe_notes: [],
  },
  recommendation: {
    use_local: true,
    device: 'cpu',
    tier: '1.7B',
    config_patch: { local_llm: { enabled: true } },
    model: {
      tier: '1.7B',
      repo: 'Qwen/Qwen1.5-1.8B-Chat-GGUF',
      filename: 'qwen1_5-1_8b-chat-q4_k_m.gguf',
      approximate_size_gb: 1.134,
    },
    reasons: ['内存够用，跑个小模型没问题'],
    probe_notes: [],
  },
  tiers: [
    {
      tier: '0.5B',
      repo: 'Qwen/Qwen2.5-0.5B-Instruct-GGUF',
      filename: 'qwen2.5-0.5b-instruct-q4_k_m.gguf',
      approximate_size_gb: 0.458,
      ram_requirement_gb: 4,
      vram_requirement_gb: 0,
      description: '最轻巧，几乎不占资源',
    },
    {
      tier: '1.7B',
      repo: 'Qwen/Qwen1.5-1.8B-Chat-GGUF',
      filename: 'qwen1_5-1_8b-chat-q4_k_m.gguf',
      approximate_size_gb: 1.134,
      ram_requirement_gb: 8,
      vram_requirement_gb: 0,
      description: '轻快又聪明，日常够用',
    },
  ],
  suggested_source: 'modelscope',
};

describe('向导手动路径直达完成（Task 6）', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    vi.spyOn(console, 'error').mockImplementation(() => {});
    fetchSetupStatusMock.mockResolvedValue(STATUS_REQUIRED);
    fetchSetupRecommendMock.mockResolvedValue(RECOMMEND_OK);
    completeSetupMock.mockResolvedValue({
      ok: true,
      applied: ['download.channel'],
      ignored: [],
      setup: { completed: true, completed_at: '2026-10-05T12:00:00Z' },
      config: {},
    });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('手动配置选完 →「完成并保存」直达确认页，提交体不含 cloud 且线路落默认 mirror', async () => {
    const onDone = vi.fn();
    render(<SetupWizard onDone={onDone} initialStatus={STATUS_REQUIRED} />);

    await screen.findByText(/跑得动本地小模型/);
    fireEvent.click(screen.getByRole('button', { name: '我自己挑' }));

    // 三项均有值（本地开关 / 显卡 / 档位，默认值已就位）→ 主按钮「完成并保存」出现
    const finishBtn = await screen.findByRole('button', { name: '完成并保存' });
    // 「下一步」保留给想配云端大脑的用户
    expect(screen.getByRole('button', { name: '下一步' })).toBeInTheDocument();

    fireEvent.click(finishBtn);

    // 直达确认页：云端 / 线路步骤标题均不出现
    expect(screen.getByText(/快好了，确认一下/)).toBeInTheDocument();
    expect(screen.queryByText('想让它用哪个云端大脑？')).not.toBeInTheDocument();
    expect(screen.queryByText('下载要用哪条线路？')).not.toBeInTheDocument();
    // 确认页如实展示「先不用云端」（手动路径本次提交不含云端）
    expect(screen.getByText(/先不用云端（本地就能聊，以后在设置里补）/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '开始聊天' }));

    await waitFor(() => expect(completeSetupMock).toHaveBeenCalledTimes(1));
    const payload = completeSetupMock.mock.calls[0][0];
    // 未选项按既有默认值补全：cloud 段整体省略、线路默认国内
    expect(payload).not.toHaveProperty('cloud');
    expect(payload.download.channel).toBe('mirror');
    // local_llm 按用户当前选择（默认开）落盘；模型仓库由线路在服务端派生
    expect(payload.local_llm.enabled).toBe(true);
    expect(payload.local_llm).not.toHaveProperty('source');
    // 手动路径不声明采纳推荐
    expect(payload.apply_recommended).toBe(false);
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));
  });

  it('手动关掉本地开关后直达 → 提交体 local_llm.enabled=false（按用户选择落盘）', async () => {
    render(<SetupWizard onDone={vi.fn()} initialStatus={STATUS_REQUIRED} />);

    await screen.findByText(/跑得动本地小模型/);
    fireEvent.click(screen.getByRole('button', { name: '我自己挑' }));
    // 关掉本地小模型开关
    fireEvent.click(screen.getByRole('switch', { name: '开启本地小模型' }));
    fireEvent.click(screen.getByRole('button', { name: '完成并保存' }));

    expect(screen.getByText(/快好了，确认一下/)).toBeInTheDocument();
    expect(screen.getByText('本地小模型：先关着')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '开始聊天' }));

    await waitFor(() => expect(completeSetupMock).toHaveBeenCalledTimes(1));
    const payload = completeSetupMock.mock.calls[0][0];
    expect(payload).not.toHaveProperty('cloud');
    expect(payload.local_llm.enabled).toBe(false);
  });

  it('手动模式可切显卡 / 档位后仍能直达；确认页展示所选本地开关状态', async () => {
    render(<SetupWizard onDone={vi.fn()} initialStatus={STATUS_REQUIRED} />);

    await screen.findByText(/跑得动本地小模型/);
    fireEvent.click(screen.getByRole('button', { name: '我自己挑' }));
    // 换选显卡与更轻档位（档位选择不进提交体，但界面可换）
    fireEvent.click(screen.getByRole('button', { name: /用显卡加速/ }));
    fireEvent.click(screen.getByRole('button', { name: /0.5B · 约 0.5GB/ }));
    fireEvent.click(screen.getByRole('button', { name: '完成并保存' }));

    expect(screen.getByText(/快好了，确认一下/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '开始聊天' }));

    await waitFor(() => expect(completeSetupMock).toHaveBeenCalledTimes(1));
    expect(completeSetupMock.mock.calls[0][0].local_llm.enabled).toBe(true);
  });

  it('推荐路径回归：「就用推荐的」仍进线路步骤（不直达确认页）', async () => {
    render(<SetupWizard onDone={vi.fn()} initialStatus={STATUS_REQUIRED} />);

    await screen.findByText(/跑得动本地小模型/);
    fireEvent.click(screen.getByRole('button', { name: '就用推荐的' }));

    // 行为不变：跳过云端直达线路步骤，而不是确认页
    expect(screen.getByText('下载要用哪条线路？')).toBeInTheDocument();
    expect(screen.queryByText(/快好了，确认一下/)).not.toBeInTheDocument();
  });

  it('手动模式保留「下一步」：点击后仍进云端步骤（想配云端大脑的用户不受影响）', async () => {
    render(<SetupWizard onDone={vi.fn()} initialStatus={STATUS_REQUIRED} />);

    await screen.findByText(/跑得动本地小模型/);
    fireEvent.click(screen.getByRole('button', { name: '我自己挑' }));
    fireEvent.click(screen.getByRole('button', { name: '下一步' }));

    expect(screen.getByText('想让它用哪个云端大脑？')).toBeInTheDocument();
  });
});
