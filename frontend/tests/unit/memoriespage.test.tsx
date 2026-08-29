import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import MemoriesPage from '../../src/renderer/pages/MemoriesPage';

/**
 * Test1 · MemoriesPage 200 条上限提示（D9 修复回归）。
 *
 * 后端 /api/memories?limit=200 返回条数达上限时页脚显示中文提示；
 * 未达上限时不显示。fetch 按真实 URL 链路 mock，不伪造组件内部状态。
 */

/** 构造 n 条后端记忆原始记录（lite/memory SQLite memories 表形状）
 *  content 用两行：首行作标题（h3），全文作摘要（p），避免 findByText 多重匹配。 */
function makeRows(n: number) {
  return Array.from({ length: n }, (_, i) => ({
    id: i + 1,
    type: 'fact',
    content: `记忆标题第${i + 1}条\n这是第${i + 1}条记忆的摘要内容`,
    tags: '[]',
    created_at: '2026-01-01T00:00:00Z',
  }));
}

describe('MemoriesPage：200 条上限提示（D9）', () => {
  beforeEach(() => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('后端返回 200 条（恰达上限）→ 页脚显示「仅显示最近 200 条记忆」', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(JSON.stringify(makeRows(200)), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    );
    render(<MemoriesPage />);

    expect(await screen.findByText('仅显示最近 200 条记忆')).toBeInTheDocument();
    // 列表本体正常渲染（标题为首行，精确匹配唯一 h3）
    expect(await screen.findByText('记忆标题第1条')).toBeInTheDocument();
  });

  it('后端返回不足 200 条 → 不显示上限提示', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(JSON.stringify(makeRows(3)), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    );
    render(<MemoriesPage />);

    await screen.findByText('记忆标题第1条');
    expect(screen.queryByText(/仅显示最近/)).not.toBeInTheDocument();
  });
});
