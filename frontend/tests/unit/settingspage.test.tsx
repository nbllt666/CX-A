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
    const combos = screen.getAllByRole('combobox');
    fireEvent.change(combos[combos.length - 1], { target: { value: 'ling' } });
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
    const combos = screen.getAllByRole('combobox');
    fireEvent.change(combos[combos.length - 1], { target: { value: 'ling' } });
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
    expect(screen.getByText(/后端还没连上/)).toBeInTheDocument();
    // 配置桶的音色错误提示不受授权失败影响
    expect(screen.getByText(VOICE_ERROR)).toBeInTheDocument();
  });
});
