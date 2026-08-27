import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
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
});
