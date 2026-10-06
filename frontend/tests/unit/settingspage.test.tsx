import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, act } from '@testing-library/react';
import SettingsPage from '../../src/renderer/pages/SettingsPage';

/**
 * Test1 · SettingsPage PUT 失败的内联错误交互。
 *
 * 后端不可用（GET/PUT 均 fail）场景：
 *  1. 首帧 GET 失败 → 区块级降级提示条出现（非空白崩溃）；
 *  2. 切换本地模式触发 PUT 失败 catch → 内联红色错误小字出现；
 *  3. 再次操作（切音色）→ 同步清除旧提示，随后出现新的内联提示。
 *
 * Test2（第二个 describe）· 音色列表 / 导入音色包 / 主动视觉 / 本地就绪徽标：
 *  1. 后端 GET /api/voices 列表驱动下拉（内置 CX-OPEN（默认），自定义 id + 人性化大小）；
 *  2. 导入成功全链路（mock pickVoiceFolder + import → 列表刷新 + PUT tts.voice）；
 *  3. 非 Electron（桥缺失）点导入 → 显式提示「要在桌面应用里才能用」；
 *  4. 主动视觉开关渲染初始值，切换 PUT {vision:{enabled}}；
 *  5. 本地模式就绪徽标两态（ready / 未 ready），未开启时不显示。
 */

const LOCAL_MODE_ERROR = '本地模式开关没保存上…待会儿再拨一次就好啦';
const VOICE_ERROR = '音色设置没保存上…待会儿再选一次就好啦';

/** 按 URL + method 分派的 fetch mock：全部请求失败（后端未起）。 */
function makeFailFetch(): ReturnType<typeof vi.fn> {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    return new Response(JSON.stringify({ error: `backend down for ${url}` }), {
      status: 503,
      statusText: 'Service Unavailable',
      headers: { 'Content-Type': 'application/json' },
    });
  });
}

describe('SettingsPage：PUT 失败 → 内联错误提示', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', makeFailFetch());
    vi.spyOn(console, 'error').mockImplementation(() => {});
    window.localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('首帧加载失败显示降级提示条；本地模式 PUT 失败出现内联红字', async () => {
    render(<SettingsPage />);

    // 首帧 GET /api/settings 与 GET /api/computer/status 均失败 → 区块级降级提示
    const degraded = await screen.findByText(/设置加载失败啦/);
    expect(degraded).toBeInTheDocument();

    // 页面没有空白崩溃：设置区块标题与各配置卡可见
    expect(screen.getByText('云端提供商')).toBeInTheDocument();
    expect(screen.getByText('电脑控制授权')).toBeInTheDocument();

    // 切换本地模式开关 → PUT /api/settings 失败 → 内联红字
    const localModeSwitch = screen.getByRole('switch', { name: '本地模式' });
    fireEvent.click(localModeSwitch);

    const inlineError = await screen.findByText(LOCAL_MODE_ERROR);
    expect(inlineError).toBeInTheDocument();
    expect(inlineError.className).toContain('text-[var(--color-error)]');
  });

  it('再次操作时旧提示先被同步清除，随后展示新操作的新提示', async () => {
    render(<SettingsPage />);
    await screen.findByText(/设置加载失败啦/); // 等首帧 effect 完成

    // 第一次操作：本地模式 PUT 失败 → 错误 A 出现
    fireEvent.click(screen.getByRole('switch', { name: '本地模式' }));
    await screen.findByText(LOCAL_MODE_ERROR);

    // 第二次操作：切音色 → handleVoiceChange 开头同步 setSaveError(null)
    // （页面 label 未与 select 做 htmlFor 关联，故按 DOM 顺序取第二个 combobox：音色）
    const combos = screen.getAllByRole('combobox');
    expect(combos.length).toBeGreaterThanOrEqual(2); // [0]=云端提供商, [1]=音色
    fireEvent.change(combos[1], { target: { value: 'ling' } });

    // 旧的「本地模式」提示已被清除（同步阶段）
    expect(screen.queryByText(LOCAL_MODE_ERROR)).not.toBeInTheDocument();

    // 新的「音色」PUT 又失败 → 新内联提示出现
    await screen.findByText(VOICE_ERROR);
    expect(screen.queryByText(LOCAL_MODE_ERROR)).not.toBeInTheDocument();
  });

  it('旧操作迟到失败被序号守卫丢弃，不覆盖新操作状态', async () => {
    // PUT 请求挂起、由测试手动决定失败时序；GET 全部失败（首帧走降级分支）
    const pending: Array<(v: Response) => void> = [];
    const seqFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if ((init?.method ?? 'GET').toUpperCase() === 'PUT') {
        return new Promise<Response>((resolve) => pending.push(resolve));
      }
      return new Response(JSON.stringify({ error: `backend down for ${String(input)}` }), {
        status: 503,
        statusText: 'Service Unavailable',
        headers: { 'Content-Type': 'application/json' },
      });
    });
    vi.stubGlobal('fetch', seqFetch);

    render(<SettingsPage />);
    await screen.findByText(/设置加载失败啦/); // 等首帧 effect 完成

    // 操作1：本地模式开关 → PUT 挂起（序号1）
    fireEvent.click(screen.getByRole('switch', { name: '本地模式' }));
    // 操作2：切音色 → 同步清错 + PUT 挂起（序号2，成为最新）
    // （Task 9 后新增语音加速两个下拉，音色不再是最后一个 combobox；固定取第 2 个 = 音色）
    const combos = screen.getAllByRole('combobox');
    fireEvent.change(combos[1], { target: { value: 'ling' } });
    // requestJson 内部有 await，PUT 实际发出在微任务中：先冲刷再断言挂起数量
    await act(async () => {});
    expect(pending.length).toBe(2);

    // 操作1 的迟到失败先到达：序号已非最新 → 不得出现「本地模式」错误提示
    pending[0](
      new Response(JSON.stringify({ error: 'stale request' }), {
        status: 503,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    await act(async () => {}); // 冲刷微任务，让操作1 的 .catch 执行
    expect(screen.queryByText(LOCAL_MODE_ERROR)).not.toBeInTheDocument();

    // 操作2 的失败随后到达：序号最新 → 音色错误提示正常出现
    pending[1](
      new Response(JSON.stringify({ error: 'fresh request' }), {
        status: 503,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    await screen.findByText(VOICE_ERROR);
    expect(screen.queryByText(LOCAL_MODE_ERROR)).not.toBeInTheDocument();
  });

  it('localStorage 键族迁移：预置旧 cx.* 键时降级读取回落旧值并静默写入新 cx-a.* 键', async () => {
    // 预置旧版键族状态（新键缺失）：模拟既有用户升级场景
    window.localStorage.setItem('cx.computer.authorized', '1');

    render(<SettingsPage />);
    await screen.findByText(/设置加载失败啦/); // 全部请求失败 → 走本地记忆分支

    // computer/status 请求失败后 readLsBool 触发迁移：旧键值读回并写入新键
    await vi.waitFor(() => {
      expect(window.localStorage.getItem('cx-a.computer.authorized')).toBe('1');
    });
  });

  it('授权切换与配置保存分桶计数：配置保存失败不得吞掉授权失败分支（D1）', async () => {
    // 修复1回归：此前共用单一 saveSeqRef，配置保存（音色）在授权 POST 在途期间
    // 抢占最新序号 → 授权迟到失败被判「非最新」静默丢弃，离线降级分支被跳过。
    const pendingAuth: Array<(v: Response) => void> = [];
    const bucketFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? 'GET').toUpperCase();
      if (url.includes('/computer/authorize') && method === 'POST') {
        return new Promise<Response>((resolve) => pendingAuth.push(resolve));
      }
      if (method === 'PUT') {
        // 配置保存：立即失败（settings 桶内最新序号 → 音色错误提示出现）
        return new Response(JSON.stringify({ error: `settings down for ${url}` }), {
          status: 503,
          statusText: 'Service Unavailable',
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.includes('/computer/status')) {
        // 授权状态在线装载：authorized=false
        return new Response(JSON.stringify({ authorized: false, confirm_dangerous: true }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      // 其余 GET（/api/settings 首帧）：失败走降级默认值
      return new Response(JSON.stringify({ error: `down for ${url}` }), {
        status: 503,
        statusText: 'Service Unavailable',
        headers: { 'Content-Type': 'application/json' },
      });
    });
    vi.stubGlobal('fetch', bucketFetch);

    render(<SettingsPage />);
    // 等待电脑控制状态装载成功：status GET 成功后 writeLsBool(false) 先写入 '0'
    await vi.waitFor(() => {
      expect(window.localStorage.getItem('cx-a.computer.authorized')).toBe('0');
    });
    expect(screen.getByText(/权限很敏感，谨慎开关/)).toBeInTheDocument();

    // 操作1：授权切换 → POST /api/computer/authorize 挂起（auth 桶序号1）
    fireEvent.click(screen.getByRole('switch', { name: '电脑控制授权' }));

    // 操作2：切音色 → PUT 立即失败（settings 桶序号1，与授权桶无关）
    // （Task 9 后新增语音加速两个下拉，音色不再是最后一个 combobox；固定取第 2 个 = 音色）
    const combos = screen.getAllByRole('combobox');
    fireEvent.change(combos[1], { target: { value: 'ling' } });
    await screen.findByText(VOICE_ERROR); // 配置桶失败提示正常出现

    // 授权 POST 迟到失败：auth 桶内序号仍最新 → 失败分支必须执行（修复前被静默跳过）
    pendingAuth[0](
      new Response(JSON.stringify({ error: 'auth down' }), {
        status: 503,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    await vi.waitFor(() => {
      // 失败分支写回本地记忆（开关为 true → '1'）
      expect(window.localStorage.getItem('cx-a.computer.authorized')).toBe('1');
    });
    // 降级离线：授权卡描述切到离线文案（computerOnline=false 的直接证据）
    expect(screen.getByText(/还没连上 TA/)).toBeInTheDocument();
    // 配置桶的音色错误提示不受授权失败影响
    expect(screen.getByText(VOICE_ERROR)).toBeInTheDocument();
  });

  it('切换运行偏好 → PUT accel.mode；需重启时明确提示（不静默）', async () => {
    const settingsView = {
      cloud: { provider: 'deepseek', base_url: '' },
      tts: { voice: 'cx-open', accel: 'cuda', accel_device: '' },
      accel: { mode: 'performance' },
      local_llm: { enabled: false },
      acp: { enabled: false },
      remote: { enabled: false },
    };
    const putBodies: Array<Record<string, unknown>> = [];
    const accelFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? 'GET').toUpperCase();
      if (url.includes('/api/settings') && method === 'PUT') {
        const body = JSON.parse(String(init?.body ?? '{}'));
        putBodies.push(body);
        return new Response(
          JSON.stringify({
            ok: true,
            applied: ['accel.mode', 'tts.accel', 'tts.accel_device'],
            ignored: [],
            config: { ...settingsView, accel: { mode: body.accel?.mode } },
            voice_backend: {
              rebuilt: false,
              needs_restart: true,
              message: '运行偏好已保存，但语音加速切换失败（RuntimeError）：需重启应用后生效',
            },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      }
      if (url.includes('/api/settings')) {
        return new Response(JSON.stringify(settingsView), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.includes('/api/computer/status')) {
        return new Response(JSON.stringify({ authorized: false, confirm_dangerous: true }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } });
    });
    vi.stubGlobal('fetch', accelFetch);

    render(<SettingsPage />);
    // 首帧配置装载后出现运行偏好区块（两个选项）
    await screen.findByText('运行偏好');
    fireEvent.click(screen.getByRole('button', { name: /省电优先/ }));

    await vi.waitFor(() => expect(putBodies.length).toBe(1));
    expect(putBodies[0]).toEqual({ accel: { mode: 'eco' } });
    // 需重启时的明确提示出现（不静默）
    expect(await screen.findByText(/需重启应用后生效/)).toBeInTheDocument();
  });
});

/* ==========================================================================
 * Test2 · 音色列表 / 导入音色包 / 主动视觉 / 本地就绪徽标（后端可用场景）
 * ========================================================================== */

/** 音色包列表项的测试桩形状（与 api.ts VoiceInfo 一致） */
type VoiceStub = { id: string; path: string; is_default: boolean; size: number; builtin: boolean };

/** 200 响应快捷构造 */
function okJson(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** 标准 GET /api/settings 配置视图桩（新字段 local_llm.ready / vision.enabled 可覆盖） */
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

/** 全链路可用型 fetch stub：GET settings / voices / computer 正常，PUT 回显合并后的 config */
function makeOkFetch(opts?: { settings?: Record<string, unknown>; voices?: VoiceStub[] }) {
  const settings = opts?.settings ?? makeSettingsView();
  const voices: VoiceStub[] = opts?.voices ?? [
    { id: 'cx-open', path: 'data/pet/cx-open', is_default: true, size: 0, builtin: true },
  ];
  const putBodies: Array<Record<string, unknown>> = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? 'GET').toUpperCase();
    if (url.includes('/api/settings') && method === 'PUT') {
      const body = JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>;
      putBodies.push(body);
      // 一层浅合并（patch 形如 {tts:{voice}} / {vision:{enabled}}，够用）
      const merged = { ...settings } as Record<string, unknown>;
      for (const [k, v] of Object.entries(body)) {
        if (v && typeof v === 'object') {
          merged[k] = { ...((merged[k] as object) ?? {}), ...(v as object) };
        } else {
          merged[k] = v;
        }
      }
      return okJson({ ok: true, applied: Object.keys(body), ignored: [], config: merged });
    }
    if (url.includes('/api/settings')) return okJson(settings);
    if (url.includes('/api/voices')) return okJson({ ok: true, voices });
    if (url.includes('/api/computer/status')) {
      return okJson({ authorized: false, confirm_dangerous: true });
    }
    return okJson({});
  });
  return { fetchMock, putBodies };
}

describe('SettingsPage：音色列表 / 导入 / 主动视觉 / 本地就绪徽标', () => {
  beforeEach(() => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    window.localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    delete (window as unknown as { cxaAPI?: unknown }).cxaAPI;
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('后端音色列表驱动下拉：内置显「CX-OPEN（默认）」，自定义显 id 与人性化大小；写死演示项不再出现', async () => {
    const { fetchMock } = makeOkFetch({
      voices: [
        { id: 'cx-open', path: 'data/pet/cx-open', is_default: true, size: 0, builtin: true },
        { id: 'star-pack', path: 'C:\\voices\\star', is_default: false, size: 125829120, builtin: false },
      ],
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsPage />);
    expect(await screen.findByRole('option', { name: 'CX-OPEN（默认）' })).toBeInTheDocument();
    // 125829120 字节 = 120MB
    expect(screen.getByRole('option', { name: 'star-pack（约 120MB）' })).toBeInTheDocument();
    // 写死的 ling/gulu/momo 无真实模型支撑，后端列表可用时不再出现
    expect(screen.queryByRole('option', { name: /咕噜/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /灵灵/ })).not.toBeInTheDocument();
  });

  it('导入音色包成功：列表刷新、自动选中新音色并 PUT tts.voice', async () => {
    const voiceList: VoiceStub[] = [
      { id: 'cx-open', path: 'data/pet/cx-open', is_default: true, size: 0, builtin: true },
    ];
    const putBodies: Array<Record<string, unknown>> = [];
    const importBodies: Array<Record<string, unknown>> = [];
    let voicesGetCount = 0;
    const settings = makeSettingsView();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? 'GET').toUpperCase();
      if (url.includes('/api/voices/import') && method === 'POST') {
        importBodies.push(JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>);
        const voice: VoiceStub = {
          id: 'newpack',
          path: 'C:\\voices\\newpack',
          is_default: false,
          size: 52428800,
          builtin: false,
        };
        voiceList.push(voice);
        return okJson({ ok: true, voice });
      }
      if (url.includes('/api/voices') && method === 'GET') {
        voicesGetCount += 1;
        return okJson({ ok: true, voices: [...voiceList] });
      }
      if (url.includes('/api/settings') && method === 'PUT') {
        const body = JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>;
        putBodies.push(body);
        return okJson({
          ok: true,
          applied: Object.keys(body),
          ignored: [],
          config: { ...settings, tts: { voice: 'newpack' } },
        });
      }
      if (url.includes('/api/settings')) return okJson(settings);
      if (url.includes('/api/computer/status')) {
        return okJson({ authorized: false, confirm_dangerous: true });
      }
      return okJson({});
    });
    vi.stubGlobal('fetch', fetchMock);
    // 模拟 Electron 桥：pickVoiceFolder 返回所选文件夹绝对路径
    (window as unknown as { cxaAPI?: unknown }).cxaAPI = {
      pickVoiceFolder: vi.fn(async () => 'C:\\voices\\newpack'),
    };

    render(<SettingsPage />);
    await screen.findByRole('option', { name: 'CX-OPEN（默认）' }); // 首帧列表装载完成

    fireEvent.click(screen.getByRole('button', { name: '导入音色包' }));

    // 成功轻量提示出现
    expect(await screen.findByText('音色导入成功，已经帮你换上啦')).toBeInTheDocument();
    // 列表已刷新（第二次 GET /api/voices）且新音色在下拉中、被自动选中
    await vi.waitFor(() => expect(voicesGetCount).toBe(2));
    expect(screen.getByRole('option', { name: 'newpack（约 50MB）' })).toBeInTheDocument();
    const combos = screen.getAllByRole('combobox');
    expect(combos[1]).toHaveValue('newpack');
    // 导入请求体携带所选路径；随后自动 PUT tts.voice 到新音色
    expect(importBodies[0]).toEqual({ source_path: 'C:\\voices\\newpack' });
    await vi.waitFor(() => expect(putBodies.length).toBe(1));
    expect(putBodies[0]).toEqual({ tts: { voice: 'newpack' } });
    // 导入结束后按钮恢复可用（loading 态退出）
    expect(screen.getByRole('button', { name: '导入音色包' })).not.toBeDisabled();
  });

  it('非 Electron 环境（桥缺失）点导入：显式提示要在桌面应用里才能用，不发导入请求', async () => {
    const { fetchMock, putBodies } = makeOkFetch();
    vi.stubGlobal('fetch', fetchMock);
    // 不设置 window.cxaAPI → pickVoiceFolder 方法不存在（浏览器 dev / 测试环境）

    render(<SettingsPage />);
    await screen.findByRole('option', { name: 'CX-OPEN（默认）' });

    fireEvent.click(screen.getByRole('button', { name: '导入音色包' }));

    expect(await screen.findByText('导入音色要在桌面应用里才能用哦')).toBeInTheDocument();
    // 未发起导入，也未触发任何 PUT
    const calls = fetchMock.mock.calls.filter(
      ([input, init]) =>
        String(input).includes('/api/voices/import') ||
        (init?.method ?? 'GET').toUpperCase() === 'PUT',
    );
    expect(calls.length).toBe(0);
    expect(putBodies.length).toBe(0);
  });

  it('主动视觉：开关渲染初始值（GET vision.enabled），切换后 PUT {vision:{enabled}}', async () => {
    const { fetchMock, putBodies } = makeOkFetch({
      settings: makeSettingsView({ vision: { enabled: false } }),
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<SettingsPage />);
    const visionSwitch = await screen.findByRole('switch', { name: '主动视觉' });
    expect(visionSwitch).toHaveAttribute('aria-checked', 'false');

    fireEvent.click(visionSwitch);
    await vi.waitFor(() => expect(putBodies.length).toBe(1));
    expect(putBodies[0]).toEqual({ vision: { enabled: true } });
    expect(screen.getByRole('switch', { name: '主动视觉' })).toHaveAttribute('aria-checked', 'true');
  });

  it('本地模式徽标两态：ready 显「已就绪」、未 ready 显「还没准备好」；未开启时不显示徽标', async () => {
    // 状态一：开启且 ready → 绿/蓝调就绪徽标；同时回归验证新 desc 文案
    const ready = makeOkFetch({ settings: makeSettingsView({ local_llm: { enabled: true, ready: true } }) });
    vi.stubGlobal('fetch', ready.fetchMock);
    render(<SettingsPage />);
    expect(await screen.findByText('本地大脑已就绪')).toBeInTheDocument();
    expect(screen.getByText('开启后聊天都在这台电脑上完成，不联网也能陪你说')).toBeInTheDocument();
    cleanup();

    // 状态二：开启但未 ready → 中性调提示徽标（指引去新手引导）
    const notReady = makeOkFetch({ settings: makeSettingsView({ local_llm: { enabled: true, ready: false } }) });
    vi.stubGlobal('fetch', notReady.fetchMock);
    render(<SettingsPage />);
    expect(await screen.findByText('本地大脑还没准备好——新手引导里可以下载')).toBeInTheDocument();
    expect(screen.queryByText('本地大脑已就绪')).not.toBeInTheDocument();
    cleanup();

    // 状态三：未开启 → 不显示任何徽标
    const off = makeOkFetch({ settings: makeSettingsView({ local_llm: { enabled: false, ready: true } }) });
    vi.stubGlobal('fetch', off.fetchMock);
    render(<SettingsPage />);
    await act(async () => {}); // 冲刷首帧 effect（settings GET 装载完成）
    expect(screen.queryByText('本地大脑已就绪')).not.toBeInTheDocument();
    expect(screen.queryByText(/本地大脑还没准备好/)).not.toBeInTheDocument();
  });
});
