import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import SettingsPage, {
  hardwareKnown,
  filterTtsAccelOptions,
  filterTtsAccelDeviceOptions,
  shouldShowAccelModeCard,
  filterLlmGpuOptions,
} from '../../src/renderer/pages/SettingsPage';
import type { HardwareProfile } from '../../src/renderer/api';

/**
 * 设置页补齐向导选项（Task 9）。
 *
 * 路由式 fetch stub（照 settingspage.test.tsx Test2 口径）：GET/PUT /api/settings、
 * GET /api/setup/recommend、POST /api/setup/model/download、GET progress、
 * POST cancel 全部可控。覆盖：
 *  1. API Key 脱敏回显（占位符展示 sk-****尾4位）+ 保存请求体 {cloud:{api_key}} + 成功提示；
 *  2. 下载线路单选：初始值来自 GET，切换 PUT {download:{channel}} + 成功提示；
 *  3. 本地模型档位卡：当前模型路径展示（不可得隐藏）、档位单选、
 *     下载请求只带 tier、进度条、取消、「设为本地默认大脑」开关走既有 local_llm.enabled 链路；
 *  4. 语音加速两下拉：初始值 + PUT {tts:{accel}} / {tts:{accel_device}}；
 *  5. 桌宠模型按钮（更换 / 恢复默认）在设置页可见。
 */

/** 标准配置视图桩（可覆盖新键） */
function makeSettingsView(overrides?: Record<string, unknown>): Record<string, unknown> {
  return {
    cloud: { provider: 'deepseek', base_url: '' },
    tts: { voice: 'cx-open', accel: 'auto', accel_device: '' },
    accel: { mode: 'performance' },
    local_llm: { enabled: false, ready: false },
    acp: { enabled: false },
    remote: { enabled: false },
    vision: { enabled: false },
    ...overrides,
  };
}

const RECOMMEND_VIEW = {
  profile: {
    cpu_cores: 8,
    ram_gb: 16,
    gpu_vendor: null,
    vram_gb: null,
    cuda_version: null,
    disk_free_gb: 120,
    probe_notes: [],
  },
  recommendation: {
    use_local: true,
    device: 'cpu',
    tier: '1.7B',
    config_patch: {},
    model: null,
    reasons: [],
    probe_notes: [],
  },
  tiers: [
    {
      tier: '0.5B',
      repo: 'Qwen/Qwen2.5-0.5B-Instruct-GGUF',
      filename: 'a.gguf',
      approximate_size_gb: 0.458,
      ram_requirement_gb: 4,
      vram_requirement_gb: 0,
      description: '最轻巧，几乎不占资源',
    },
    {
      tier: '1.7B',
      repo: 'Qwen/Qwen1.5-1.8B-Chat-GGUF',
      filename: 'b.gguf',
      approximate_size_gb: 1.134,
      ram_requirement_gb: 8,
      vram_requirement_gb: 0,
      description: '轻快又聪明，日常够用',
    },
  ],
  suggested_source: 'modelscope',
};

interface RouteFetchOptions {
  settings?: Record<string, unknown>;
  /** 下载进度状态（测试中可直接改写该对象字段驱动轮询） */
  progress?: { state: string; downloaded: number; total: number; percent: number; error: string | null };
  /** 覆盖 /api/setup/recommend 响应（设备画像裁剪用例注入 has_igpu / dgpu_vendor） */
  recommend?: Record<string, unknown>;
}

/** 路由式 fetch stub：返回 fetchMock（断言调用）与捕获到的请求体列表 */
function makeRouteFetch(opts?: RouteFetchOptions) {
  const settings = opts?.settings ?? makeSettingsView();
  const progress = opts?.progress ?? null;
  const recommend = opts?.recommend ?? RECOMMEND_VIEW;
  const putBodies: Array<Record<string, unknown>> = [];
  const downloadBodies: Array<Record<string, unknown>> = [];
  let cancelCalled = 0;
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? 'GET').toUpperCase();
    const ok = (body: unknown) =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    if (url.includes('/api/settings') && method === 'PUT') {
      const body = JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>;
      putBodies.push(body);
      // 一层浅合并 + 模拟后端对 api_key 的脱敏回显
      const merged = { ...settings } as Record<string, unknown>;
      for (const [k, v] of Object.entries(body)) {
        if (v && typeof v === 'object') {
          merged[k] = { ...((merged[k] as object) ?? {}), ...(v as object) };
        } else {
          merged[k] = v;
        }
      }
      const cloud = merged.cloud as Record<string, unknown> | undefined;
      if (cloud && typeof cloud.api_key === 'string') {
        merged.cloud = { ...cloud, api_key: `sk-****${cloud.api_key.slice(-4)}` };
      }
      return ok({ ok: true, applied: Object.keys(body), ignored: [], config: merged });
    }
    if (url.includes('/api/settings')) return ok(settings);
    if (url.includes('/api/setup/recommend')) return ok(recommend);
    if (url.includes('/api/setup/model/download') && method === 'POST') {
      downloadBodies.push(JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>);
      return ok({ ok: true, state: 'downloading', already_running: false, model: null });
    }
    if (url.includes('/api/setup/model/progress')) {
      return ok(
        progress ?? {
          state: 'idle',
          downloaded: 0,
          total: 0,
          percent: 0,
          file: '',
          error: null,
          model: null,
        },
      );
    }
    if (url.includes('/api/setup/model/cancel')) {
      cancelCalled += 1;
      return ok({ ok: true, state: 'canceled' });
    }
    if (url.includes('/api/voices')) {
      return ok({
        ok: true,
        voices: [{ id: 'cx-open', path: 'data/voices/cx-open', is_default: true, size: 0, builtin: true }],
      });
    }
    if (url.includes('/api/computer/status')) {
      return ok({ authorized: false, confirm_dangerous: true });
    }
    return ok({});
  });
  return { fetchMock, putBodies, downloadBodies, getCancelCalls: () => cancelCalled };
}

describe('设置页补齐向导选项（Task 9）', () => {
  beforeEach(() => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    window.localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('API Key：脱敏回显占位（sk-****尾4位），保存请求体 {cloud:{api_key}} 且占位符换新脱敏值', async () => {
    const { fetchMock, putBodies } = makeRouteFetch({
      settings: makeSettingsView({ cloud: { provider: 'deepseek', api_key: 'sk-****abcd' } }),
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsPage />);
    // 首帧 GET 完成（云端提供商区块出现）
    const keyInput = await screen.findByLabelText('云端钥匙（API Key）');
    // 脱敏回显以占位符展示，不回填明文
    expect(keyInput).toHaveAttribute('placeholder', expect.stringContaining('sk-****abcd'));
    expect(keyInput).toHaveValue('');

    // 输入新钥匙保存
    fireEvent.change(keyInput, { target: { value: 'sk-live-key-9999' } });
    fireEvent.click(screen.getByRole('button', { name: '保存钥匙' }));

    expect(await screen.findByText('API Key 已保存')).toBeInTheDocument();
    await vi.waitFor(() => expect(putBodies.length).toBe(1));
    expect(putBodies[0]).toEqual({ cloud: { api_key: 'sk-live-key-9999' } });
    // 保存后输入框清空，占位符更新为新钥匙的脱敏值（后端回显）
    expect(await screen.findByLabelText('云端钥匙（API Key）')).toHaveValue('');
    expect(screen.getByLabelText('云端钥匙（API Key）')).toHaveAttribute(
      'placeholder',
      expect.stringContaining('sk-****9999'),
    );
  });

  it('下载线路：初始值来自 GET（official 选中），切国内 PUT {download:{channel:"mirror"}} + 提示', async () => {
    const { fetchMock, putBodies } = makeRouteFetch({
      settings: makeSettingsView({ download: { channel: 'official' } }),
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsPage />);
    const overseas = await screen.findByRole('button', { name: /海外线路（HuggingFace）/ });
    const domestic = screen.getByRole('button', { name: /国内线路（魔塔，推荐）/ });
    expect(overseas).toHaveAttribute('aria-pressed', 'true');
    expect(domestic).toHaveAttribute('aria-pressed', 'false');

    fireEvent.click(domestic);
    expect(await screen.findByText(/下载线路已保存/)).toBeInTheDocument();
    await vi.waitFor(() => expect(putBodies.length).toBe(1));
    expect(putBodies[0]).toEqual({ download: { channel: 'mirror' } });
    expect(domestic).toHaveAttribute('aria-pressed', 'true');
  });

  it('档位卡：展示当前模型路径与四档（渲染两档），下载请求只带 tier，进度条与取消链路可用', async () => {
    const progress = {
      state: 'downloading',
      downloaded: 420 * 1024 * 1024,
      total: 1000 * 1024 * 1024,
      percent: 42,
      error: null as string | null,
    };
    const { fetchMock, downloadBodies, getCancelCalls } = makeRouteFetch({
      settings: makeSettingsView({ local_llm: { enabled: false, model_path: 'C:\\models\\cx-open.vrm' } }),
      progress,
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsPage />);

    // 当前模型路径可得 → 展示该行
    expect(await screen.findByText(/当前模型：C:\\models\\cx-open\.vrm/)).toBeInTheDocument();
    // 档位选项（两档，含体积与描述）
    const tierLight = await screen.findByRole('button', { name: /0\.5B · 约 0\.5GB/ });
    const tierBig = screen.getByRole('button', { name: /1\.7B · 约 1\.1GB/ });
    expect(tierBig).toHaveAttribute('aria-pressed', 'true'); // 默认选中推荐档
    expect(tierLight).toHaveAttribute('aria-pressed', 'false');

    // 选轻档 → 下载请求只带所选 tier（不带 source）
    fireEvent.click(tierLight);
    expect(tierLight).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(screen.getByRole('button', { name: '下载所选档位' }));

    await vi.waitFor(() => expect(downloadBodies.length).toBe(1));
    expect(downloadBodies[0]).toEqual({ tier: '0.5B' });
    expect(downloadBodies[0]).not.toHaveProperty('source');

    // 进度条立即可见（首次拉取不等待轮询间隔）
    expect(await screen.findByText(/42%/)).toBeInTheDocument();
    expect(screen.getByText(/420 MB \/ 1000 MB/)).toBeInTheDocument();
    // 下载中：出现取消按钮
    fireEvent.click(screen.getByRole('button', { name: '取消下载' }));
    await vi.waitFor(() => expect(getCancelCalls()).toBe(1));
    expect(await screen.findByText(/已经停下啦/)).toBeInTheDocument();
    // 取消后回到可重新开始态
    expect(screen.getByRole('button', { name: '重新开始下载' })).toBeInTheDocument();
  });

  it('档位卡：下载到终态 done → 停表并提示完成；模型路径不可得时隐藏该行', async () => {
    const progress = {
      state: 'downloading',
      downloaded: 900 * 1024 * 1024,
      total: 1000 * 1024 * 1024,
      percent: 90,
      error: null as string | null,
    };
    const { fetchMock } = makeRouteFetch({
      settings: makeSettingsView(), // 无 local_llm.model_path
      progress,
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsPage />);
    await screen.findByRole('button', { name: /1\.7B · 约 1\.1GB/ });
    // 模型路径不可得 → 该行隐藏
    expect(screen.queryByText(/当前模型：/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '下载所选档位' }));
    expect(await screen.findByText(/90%/)).toBeInTheDocument();

    // 推进到终态 done：下一次 1s 轮询拿到 → 停表并提示完成
    progress.state = 'done';
    await vi.waitFor(() => expect(screen.getByText(/下载完成/)).toBeInTheDocument(), {
      timeout: 3000,
    });
    // 完成后不再提供「下载所选档位」入口（终态语义正确）
    expect(screen.queryByRole('button', { name: '下载所选档位' })).not.toBeInTheDocument();
  });

  it('「设为本地默认大脑」开关沿用既有 local_llm.enabled 链路（PUT {local_llm:{enabled}}）', async () => {
    const { fetchMock, putBodies } = makeRouteFetch();
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsPage />);
    const cardSwitch = await screen.findByRole('switch', { name: '设为本地默认大脑' });
    expect(cardSwitch).toHaveAttribute('aria-checked', 'false');

    fireEvent.click(cardSwitch);
    await vi.waitFor(() => expect(putBodies.length).toBe(1));
    expect(putBodies[0]).toEqual({ local_llm: { enabled: true } });
  });

  it('语音加速：两个下拉初始值来自 GET，分别 PUT {tts:{accel}} 与 {tts:{accel_device}}', async () => {
    const { fetchMock, putBodies } = makeRouteFetch({
      settings: makeSettingsView({ tts: { voice: 'cx-open', accel: 'cuda', accel_device: 'dgpu' } }),
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsPage />);
    const accelSelect = await screen.findByLabelText('加速方式');
    const deviceSelect = screen.getByLabelText('加速设备');
    expect(accelSelect).toHaveValue('cuda');
    expect(deviceSelect).toHaveValue('dgpu');

    fireEvent.change(accelSelect, { target: { value: 'off' } });
    await vi.waitFor(() => expect(putBodies.length).toBe(1));
    expect(putBodies[0]).toEqual({ tts: { accel: 'off' } });
    expect(await screen.findByText('语音加速已保存')).toBeInTheDocument();

    fireEvent.change(deviceSelect, { target: { value: 'igpu' } });
    await vi.waitFor(() => expect(putBodies.length).toBe(2));
    expect(putBodies[1]).toEqual({ tts: { accel_device: 'igpu' } });
  });

  it('桌宠模型卡：设置页可见「更换桌宠模型 / 恢复默认」按钮', async () => {
    const { fetchMock } = makeRouteFetch();
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsPage />);
    expect(await screen.findByRole('button', { name: '更换桌宠模型' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '恢复默认' })).toBeInTheDocument();
  });
});

/**
 * 20261006 设置页按设备与模式裁剪显示：
 * 无核显不显示核显选项、纯 CPU 隐藏运行偏好、本地模式隐藏云端配置、视觉文案不再说「要联网」。
 */

/** 画像桩：字段齐备（hardwareKnown=true）的最小构造 */
function makeProfile(overrides?: Partial<HardwareProfile>): HardwareProfile {
  return {
    cpu_cores: 8,
    ram_gb: 16,
    gpu_vendor: null,
    vram_gb: null,
    cuda_version: null,
    disk_free_gb: 120,
    probe_notes: [],
    has_igpu: false,
    dgpu_vendor: null,
    ...overrides,
  };
}

describe('设备画像裁剪：纯函数分支（20261006）', () => {
  it('hardwareKnown：字段齐备才 true；null / 探测失败降级画像（字段缺失）→ false', () => {
    expect(hardwareKnown(null)).toBe(false);
    expect(hardwareKnown({ cpu_cores: 8, ram_gb: null, gpu_vendor: 'cpu', vram_gb: null, cuda_version: null, disk_free_gb: null, probe_notes: [] })).toBe(false);
    expect(hardwareKnown(makeProfile())).toBe(true);
    // dgpu_vendor 键存在（值为 null）也算齐备——「明确无独显」是有效结论
    expect(hardwareKnown(makeProfile({ dgpu_vendor: null }))).toBe(true);
  });

  it('纯 CPU（无核显无独显）：cuda/rocm/dml 与核显/独显全隐藏，运行偏好卡隐藏', () => {
    const p = makeProfile();
    const accel = filterTtsAccelOptions(p).map((o) => o.value);
    expect(accel).toEqual(['auto', 'cpu', 'off']);
    const device = filterTtsAccelDeviceOptions(p).map((o) => o.value);
    expect(device).toEqual(['']);
    expect(shouldShowAccelModeCard(p)).toBe(false);
  });

  it('仅核显：igpu 显示、dgpu 隐藏；cuda/rocm 隐藏、dml 显示；运行偏好卡显示', () => {
    const p = makeProfile({ has_igpu: true });
    expect(filterTtsAccelOptions(p).map((o) => o.value)).toEqual(['auto', 'cpu', 'dml', 'off']);
    expect(filterTtsAccelDeviceOptions(p).map((o) => o.value)).toEqual(['', 'igpu']);
    expect(shouldShowAccelModeCard(p)).toBe(true);
  });

  it('仅 N 卡独显：cuda/dgpu/dml 显示、igpu/rocm 隐藏；gpu_vendor=nvidia 亦命中 cuda', () => {
    const p = makeProfile({ dgpu_vendor: 'nvidia' });
    expect(filterTtsAccelOptions(p).map((o) => o.value)).toEqual(['auto', 'cpu', 'cuda', 'dml', 'off']);
    expect(filterTtsAccelDeviceOptions(p).map((o) => o.value)).toEqual(['', 'dgpu']);
    expect(shouldShowAccelModeCard(p)).toBe(true);
    // gpu_vendor 兜底口径（nvidia-smi 探测成功但清单枚举未归类）
    const p2 = makeProfile({ gpu_vendor: 'nvidia', dgpu_vendor: null });
    expect(filterTtsAccelOptions(p2).map((o) => o.value)).toContain('cuda');
  });

  it('AMD 独显：rocm 显示；核显+N 卡齐全时设备选项全量（rocm 仍仅限 AMD）', () => {
    const p = makeProfile({ dgpu_vendor: 'amd' });
    expect(filterTtsAccelOptions(p).map((o) => o.value)).toContain('rocm');
    expect(filterTtsAccelOptions(p).map((o) => o.value)).not.toContain('cuda');
    // 现实机器不会同时插 N 卡与 A 卡：核显 + N 卡 → rocm 仍不显示
    const both = makeProfile({ has_igpu: true, dgpu_vendor: 'nvidia' });
    expect(filterTtsAccelOptions(both).map((o) => o.value)).toEqual([
      'auto', 'cpu', 'cuda', 'dml', 'off',
    ]);
    expect(filterTtsAccelDeviceOptions(both).map((o) => o.value)).toEqual(['', 'igpu', 'dgpu']);
  });

  it('画像未知（null / 降级）：全部选项与运行偏好卡保守显示（一次失败不藏功能）', () => {
    expect(filterTtsAccelOptions(null)).toHaveLength(6);
    expect(filterTtsAccelDeviceOptions(null)).toHaveLength(3);
    expect(shouldShowAccelModeCard(null)).toBe(true);
  });

  it('LLM 显卡选项（20261006）：核显+独显齐全 → 全量；仅核显 / 仅 N 卡 → 按需裁剪', () => {
    // 核显 + N 卡：自动 + NVIDIA 独显 + 核显（无 AMD/Intel 独显选项）
    const both = filterLlmGpuOptions(makeProfile({ has_igpu: true, dgpu_vendor: 'nvidia' })).map((o) => o.label);
    expect(both).toEqual(['自动（推荐）', 'NVIDIA 独显', '核显']);
    // 仅 AMD 独显：自动 + AMD/Intel 独显（无 NVIDIA、无核显）
    const amdOnly = filterLlmGpuOptions(makeProfile({ has_igpu: false, dgpu_vendor: 'amd' })).map((o) => o.label);
    expect(amdOnly).toEqual(['自动（推荐）', '独立显卡（AMD/Intel）']);
    // 仅核显：自动 + 核显
    const igpuOnly = filterLlmGpuOptions(makeProfile({ has_igpu: true, dgpu_vendor: null })).map((o) => o.label);
    expect(igpuOnly).toEqual(['自动（推荐）', '核显']);
    // 纯 CPU：只剩自动（卡片本身也不渲染，但选项函数仍返回自动兜底）
    const none = filterLlmGpuOptions(makeProfile()).map((o) => o.label);
    expect(none).toEqual(['自动（推荐）']);
    // 画像未知：全量（保守显示）
    expect(filterLlmGpuOptions(null)).toHaveLength(4);
  });
});

describe('设备画像裁剪：渲染条件（20261006）', () => {
  beforeEach(() => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    window.localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('无核显无独显设备：核显/独显选项与「省电优先」运行偏好卡不出现', async () => {
    const { fetchMock } = makeRouteFetch({
      recommend: {
        ...RECOMMEND_VIEW,
        profile: makeProfile(),
      },
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsPage />);
    await screen.findByLabelText('加速方式');
    // 画像请求异步落定 → 裁剪生效
    await vi.waitFor(() => {
      expect(screen.queryByRole('button', { name: /省电优先/ })).not.toBeInTheDocument();
    });
    const deviceSelect = screen.getByLabelText('加速设备');
    expect(deviceSelect).toContainHTML('自动选设备');
    expect(deviceSelect).not.toContainHTML('核显（更省电）');
    expect(deviceSelect).not.toContainHTML('独立显卡（更快）');
    expect(screen.getByLabelText('加速方式')).not.toContainHTML('NVIDIA 显卡加速');
  });

  it('本地模式开启：云端提供商/API Key 配置隐藏，原位渲染「先关掉本地模式」指引', async () => {
    const { fetchMock } = makeRouteFetch({
      settings: makeSettingsView({ local_llm: { enabled: true, ready: true } }),
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsPage />);
    expect(await screen.findByText(/本地模式开着，聊天都在这台电脑上完成/)).toBeInTheDocument();
    expect(screen.queryByLabelText('云端钥匙（API Key）')).not.toBeInTheDocument();
    expect(screen.queryByText('云端提供商')).not.toBeInTheDocument();
    // 本地模式开关与就绪徽标仍在
    expect(screen.getByRole('switch', { name: '本地模式' })).toHaveAttribute('aria-checked', 'true');
  });

  it('主动视觉文案如实描述本地理解（不再声称「需要联网」）', async () => {
    const { fetchMock } = makeRouteFetch();
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsPage />);
    expect(await screen.findByText(/画面只在这台电脑上理解、不会上传/)).toBeInTheDocument();
    expect(screen.queryByText(/理解画面时需要联网/)).not.toBeInTheDocument();
  });

  it('语音交互模式（20261006）：渲染默认传统，切换全双工 PUT {voice:{interaction_mode}}', async () => {
    const { fetchMock, putBodies } = makeRouteFetch();
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsPage />);
    // 默认传统选中
    const vadBtn = await screen.findByRole('button', { name: /传统 · 自动断句/ });
    expect(vadBtn).toHaveAttribute('aria-pressed', 'true');
    const duplexBtn = screen.getByRole('button', { name: /全双工 · 边说边聊/ });
    expect(duplexBtn).toHaveAttribute('aria-pressed', 'false');

    // 切全双工 → PUT 请求体 {voice:{interaction_mode:'duplex'}} + 成功提示
    fireEvent.click(duplexBtn);
    await vi.waitFor(() => expect(putBodies.length).toBe(1));
    expect(putBodies[0]).toEqual({ voice: { interaction_mode: 'duplex' } });
    expect(await screen.findByText(/全双工模式建议戴耳机/)).toBeInTheDocument();
    expect(duplexBtn).toHaveAttribute('aria-pressed', 'true');
    expect(vadBtn).toHaveAttribute('aria-pressed', 'false');
  });

  it('语音交互模式回显：GET 返回 duplex 时全双工选中', async () => {
    const { fetchMock } = makeRouteFetch({
      settings: makeSettingsView({ voice: { interaction_mode: 'duplex' } }),
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsPage />);
    const duplexBtn = await screen.findByRole('button', { name: /全双工 · 边说边聊/ });
    await vi.waitFor(() => expect(duplexBtn).toHaveAttribute('aria-pressed', 'true'));
  });
});
