import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react';

/**
 * Test1 · 首启向导（Task 8）。
 *
 * 风格照抄 settingspage.test.tsx：模块级 mock `../src/renderer/api`（此处用
 * importOriginal 做「部分 mock」，其余导出保持真实），断言用户可见行为。
 *
 * 覆盖：
 *  1. App 门控：wizard_required=true → 向导覆盖主界面；
 *  2. App 门控降级：status 请求失败 → 放行主界面 + 本地记「已跳过」；
 *  3. 推荐加载成功 → 展示中文推荐理由与档位；
 *  4. 推荐请求失败 → 可跳过继续；
 *  5. status 取不到时向导不卡死（用内置默认值继续）；
 *  6. 走到最后提交 → completeSetup 入参含 provider / channel / enabled / apply_recommended，不含模型来源；
 *  7. 下载步骤：进度文本出现 + 取消路径 + 卸载清理定时器。
 *
 * 追加（本轮「线路合一」变更）：第 3 步只有一个选择问题、首帧只认线路、
 * 下载入参只带 tier、完成页汇总为单行线路文案。
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
import App from '../../src/renderer/App';
import * as api from '../../src/renderer/api';

const fetchSetupStatusMock = vi.mocked(api.fetchSetupStatus);
const fetchSetupRecommendMock = vi.mocked(api.fetchSetupRecommend);
const completeSetupMock = vi.mocked(api.completeSetup);
const startModelDownloadMock = vi.mocked(api.startModelDownload);
const fetchModelProgressMock = vi.mocked(api.fetchModelProgress);
const cancelModelDownloadMock = vi.mocked(api.cancelModelDownload);

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

const DOWNLOADING_PROGRESS: api.ModelProgress = {
  state: 'downloading',
  downloaded: 420 * 1024 * 1024,
  total: 1000 * 1024 * 1024,
  percent: 42,
  file: 'b.gguf',
  error: null,
  model: null,
};

/** 从向导第 1 步一路点到第 4 步（采纳推荐 → 两步「下一步」） */
function advanceToDownloadStep() {
  fireEvent.click(screen.getByRole('button', { name: '就用推荐的' }));
  fireEvent.click(screen.getByRole('button', { name: '下一步' }));
  fireEvent.click(screen.getByRole('button', { name: '下一步' }));
}

/** 从向导第 1 步点到第 3 步（下载线路那一问） */
function advanceToChannelStep() {
  fireEvent.click(screen.getByRole('button', { name: '就用推荐的' }));
  fireEvent.click(screen.getByRole('button', { name: '下一步' }));
}

describe('首启向导（SetupWizard / App 门控）', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    vi.spyOn(console, 'error').mockImplementation(() => {});
    // 默认：状态为「需要初始化」，推荐可用
    fetchSetupStatusMock.mockResolvedValue(STATUS_REQUIRED);
    fetchSetupRecommendMock.mockResolvedValue(RECOMMEND_OK);
    completeSetupMock.mockResolvedValue({
      ok: true,
      applied: ['cloud.provider'],
      ignored: [],
      setup: { completed: true, completed_at: '2026-09-19T12:00:00Z' },
      config: {},
    });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('App 门控：wizard_required=true → 向导覆盖主界面（主界面文案不出现）', async () => {
    render(<App />);

    // 向导独有文案（欢迎标题）
    expect(await screen.findByText('欢迎来到 CX-A')).toBeInTheDocument();
    // 主界面（聊天页空态）不得出现
    expect(screen.queryByText(/还没有聊天记录/)).not.toBeInTheDocument();
  });

  it('App 门控降级：status 请求失败 → 本次会话放行主界面，且下次启动仍会重新询问', async () => {
    fetchSetupStatusMock.mockRejectedValue(new Error('ECONNREFUSED: backend not running'));
    // 主界面 ChatPage 不主动联网；仍兜底 stub fetch，避免其它组件触发未 mock 网络
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')));

    render(<App />);

    // 放行主界面：聊天页空态可见（无空白页 / 无永久 loading）
    expect(await screen.findByText(/还没有聊天记录/)).toBeInTheDocument();
    expect(screen.queryByText('欢迎来到 CX-A')).not.toBeInTheDocument();
    // 降级只作用于本次会话：不得持久化「已跳过」标记（否则后端慢一拍会让向导永久消失）
    expect(window.localStorage.getItem('cx-a.setup.skipped')).toBeNull();
    // 且下次挂载仍会重新询问后端（后端恢复后向导照常出现）
    expect(fetchSetupStatusMock).toHaveBeenCalledTimes(1);
    cleanup();
    fetchSetupStatusMock.mockResolvedValue(STATUS_REQUIRED);
    render(<App />);
    expect(await screen.findByText('欢迎来到 CX-A')).toBeInTheDocument();
  });

  it('推荐加载成功 → 展示中文推荐结论与理由，并可自己挑档位', async () => {
    render(<SetupWizard onDone={vi.fn()} initialStatus={STATUS_REQUIRED} />);

    expect(await screen.findByText(/跑得动本地小模型/)).toBeInTheDocument();
    expect(screen.getByText('内存够用，跑个小模型没问题')).toBeInTheDocument();

    // 「我自己挑」→ 展示档位（含体积与说明）
    fireEvent.click(screen.getByRole('button', { name: '我自己挑' }));
    expect(screen.getByText('1.7B · 约 1.1GB')).toBeInTheDocument();
    expect(screen.getByText('轻快又聪明，日常够用')).toBeInTheDocument();
  });

  it('推荐请求失败 → 降级为「跳过推荐，直接下一步」，可继续走到第二步', async () => {
    fetchSetupRecommendMock.mockRejectedValue(new Error('probe down'));
    render(<SetupWizard onDone={vi.fn()} initialStatus={STATUS_REQUIRED} />);

    expect(await screen.findByText(/硬件体检没跑起来/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '跳过推荐，直接下一步' }));

    // 第二步：云端服务商与钥匙
    expect(screen.getByText(/云端大脑/)).toBeInTheDocument();
    expect(screen.getByLabelText('云端服务商')).toBeInTheDocument();
  });

  it('status 取不到时向导不卡死：用内置默认值继续渲染（不因请求失败而空白）', async () => {
    fetchSetupStatusMock.mockRejectedValue(new Error('status down'));
    render(<SetupWizard onDone={vi.fn()} />);

    expect(await screen.findByText(/跑得动本地小模型/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '就用推荐的' })).toBeInTheDocument();
  });

  it('走到最后提交：completeSetup 入参含 channel / enabled / apply_recommended，且不含模型来源', async () => {
    const onDone = vi.fn();
    render(<SetupWizard onDone={onDone} initialStatus={STATUS_REQUIRED} />);

    await screen.findByText(/跑得动本地小模型/);
    advanceToDownloadStep();
    fireEvent.click(screen.getByRole('button', { name: '以后再说' }));
    fireEvent.click(screen.getByRole('button', { name: '开始聊天' }));

    await waitFor(() => expect(completeSetupMock).toHaveBeenCalledTimes(1));
    const payload = completeSetupMock.mock.calls[0][0];
    expect(payload.cloud.provider).toBe('deepseek');
    expect(payload.download.channel).toBe('mirror');
    expect(payload.local_llm.enabled).toBe(true);
    // 模型仓库由线路在服务端派生：提交体不得再出现 source
    expect(payload.local_llm).not.toHaveProperty('source');
    expect(payload.apply_recommended).toBe(true);
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));
  });

  it('下载步骤：展示进度百分比 / 已下载与总大小，取消走 cancelModelDownload', async () => {
    startModelDownloadMock.mockResolvedValue({
      ok: true,
      state: 'downloading',
      already_running: false,
      model: null,
    });
    fetchModelProgressMock.mockResolvedValue(DOWNLOADING_PROGRESS);
    cancelModelDownloadMock.mockResolvedValue({ ok: true, state: 'canceled' });

    render(<SetupWizard onDone={vi.fn()} initialStatus={STATUS_REQUIRED} />);
    await screen.findByText(/跑得动本地小模型/);
    advanceToDownloadStep();

    fireEvent.click(screen.getByRole('button', { name: '现在下载' }));

    // 启动入参只带档位：模型站点由线路在服务端决定，不再传 source
    await waitFor(() => expect(startModelDownloadMock).toHaveBeenCalledTimes(1));
    expect(startModelDownloadMock.mock.calls[0][0]).toEqual({ tier: '1.7B' });
    expect(startModelDownloadMock.mock.calls[0][0]).not.toHaveProperty('source');

    // 进度文本：42% + 人类可读体积
    expect(await screen.findByText(/42%/)).toBeInTheDocument();
    expect(screen.getByText(/420 MB \/ 1000 MB/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '取消下载' }));
    await waitFor(() => expect(cancelModelDownloadMock).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/已经停下啦/)).toBeInTheDocument();
  });

  it('卸载时清掉进度轮询定时器（不泄漏定时器 / 不再继续请求）', async () => {
    startModelDownloadMock.mockResolvedValue({
      ok: true,
      state: 'downloading',
      already_running: false,
      model: null,
    });
    fetchModelProgressMock.mockResolvedValue(DOWNLOADING_PROGRESS);
    const clearSpy = vi.spyOn(globalThis, 'clearInterval');

    const { unmount } = render(<SetupWizard onDone={vi.fn()} initialStatus={STATUS_REQUIRED} />);
    await screen.findByText(/跑得动本地小模型/);
    advanceToDownloadStep();
    fireEvent.click(screen.getByRole('button', { name: '现在下载' }));
    await screen.findByText(/42%/);

    clearSpy.mockClear();
    unmount();

    // 卸载清理必须调用 clearInterval（清掉轮询定时器）
    expect(clearSpy).toHaveBeenCalled();
  });

  it('提交失败不把用户卡死：展示中文提示 + 「稍后再说，先进去用」可放行', async () => {
    completeSetupMock.mockRejectedValue(new Error('save failed'));
    const onDone = vi.fn();
    render(<SetupWizard onDone={onDone} initialStatus={STATUS_REQUIRED} />);

    await screen.findByText(/跑得动本地小模型/);
    advanceToDownloadStep();
    fireEvent.click(screen.getByRole('button', { name: '以后再说' }));
    fireEvent.click(screen.getByRole('button', { name: '开始聊天' }));

    expect(await screen.findByText(/没能保存上/)).toBeInTheDocument();
    // 仍可先进去用（降级放行）
    fireEvent.click(screen.getByRole('button', { name: '稍后再说，先进去用' }));
    expect(onDone).toHaveBeenCalledTimes(1);
  });

  it('第 3 步只有一个选择：两条线路选项，旧的「模型从哪下」问题已消失', async () => {
    render(<SetupWizard onDone={vi.fn()} initialStatus={STATUS_REQUIRED} />);
    await screen.findByText(/跑得动本地小模型/);
    advanceToChannelStep();

    // 唯一问题的标题 + 两个线路选项（含口语化副说明）
    expect(screen.getByText('走哪条线路？')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /国内线路（魔塔，推荐）/ })).toBeInTheDocument();
    expect(screen.getByText('下载更快更稳，模型从国内的魔塔拿')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /海外线路（HuggingFace）/ })).toBeInTheDocument();
    expect(screen.getByText('直连海外站点，模型从 HuggingFace 拿')).toBeInTheDocument();

    // 本步的选项按钮恰为两个（不存在第二组选择）
    const optionButtons = screen
      .getAllByRole('button')
      .filter((b) => /线路/.test(b.textContent ?? ''));
    expect(optionButtons).toHaveLength(2);

    // 旧语义（第二个问题与旧文案）不再出现
    expect(screen.queryByText('模型从哪下？')).not.toBeInTheDocument();
    expect(screen.queryByText(/下载走哪条路/)).not.toBeInTheDocument();
    expect(screen.queryByText(/官方直连/)).not.toBeInTheDocument();
    expect(screen.queryByText(/魔搭/)).not.toBeInTheDocument();
  });

  it('首帧只认线路：后端模型来源与线路不一致时，也不据此覆盖线路', async () => {
    const inconsistent: api.SetupStatusView = {
      ...STATUS_REQUIRED,
      download: { channel: 'mirror' },
      local_llm_source: 'huggingface', // 故意与 channel 不一致
    };
    render(<SetupWizard onDone={vi.fn()} initialStatus={inconsistent} />);
    await screen.findByText(/跑得动本地小模型/);
    advanceToDownloadStep();
    fireEvent.click(screen.getByRole('button', { name: '以后再说' }));
    fireEvent.click(screen.getByRole('button', { name: '开始聊天' }));

    await waitFor(() => expect(completeSetupMock).toHaveBeenCalledTimes(1));
    expect(completeSetupMock.mock.calls[0][0].download.channel).toBe('mirror');
  });

  it('选择海外线路 → 提交体 channel=official（线路是唯一选择来源）', async () => {
    render(<SetupWizard onDone={vi.fn()} initialStatus={STATUS_REQUIRED} />);
    await screen.findByText(/跑得动本地小模型/);
    advanceToChannelStep();

    fireEvent.click(screen.getByRole('button', { name: /海外线路（HuggingFace）/ }));
    fireEvent.click(screen.getByRole('button', { name: '下一步' }));
    fireEvent.click(screen.getByRole('button', { name: '以后再说' }));
    fireEvent.click(screen.getByRole('button', { name: '开始聊天' }));

    await waitFor(() => expect(completeSetupMock).toHaveBeenCalledTimes(1));
    const payload = completeSetupMock.mock.calls[0][0];
    expect(payload.download.channel).toBe('official');
    expect(payload.local_llm).not.toHaveProperty('source');
  });

  it('完成页汇总为单行线路文案（不再分开写「下载走」与「模型来自」）', async () => {
    render(<SetupWizard onDone={vi.fn()} initialStatus={STATUS_REQUIRED} />);
    await screen.findByText(/跑得动本地小模型/);
    advanceToDownloadStep();
    fireEvent.click(screen.getByRole('button', { name: '以后再说' }));

    expect(screen.getByText(/下载线路：国内线路（魔塔）/)).toBeInTheDocument();
    expect(screen.queryByText(/模型来自/)).not.toBeInTheDocument();
    expect(screen.queryByText(/下载走：/)).not.toBeInTheDocument();
  });
});
