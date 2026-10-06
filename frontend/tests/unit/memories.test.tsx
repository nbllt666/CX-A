import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import MemoriesPage from '../../src/renderer/pages/MemoriesPage';
import { API_ENDPOINTS } from '../../src/renderer/api';
import type { MemoryRow } from '../../src/renderer/api';

/**
 * Test（Task 7）· 记忆页改造：三过滤 / 视图切换 / 批量删除确认流 / 新建与编辑弹窗 /
 * 3D 面板调权请求参数 / 衰减面板同步 / 记忆管理助手 agent_id。
 *
 * 全部走 URL 感知的 fetch 替身（不伪造组件内部状态），断言「页面 → 接口」调用契约
 * 与接口结果到 UI 的回显；写操作一律 window.confirm 确认后触发。
 */

/* ── 数据构造 ─────────────────────────────────────────────────────────────── */

/** 构造一条后端记忆原始记录（lite/memory SQLite memories 表形状） */
function makeRow(overrides: Partial<MemoryRow> & { id: number }): MemoryRow {
  return {
    type: 'long_term',
    content: `记忆标题第${overrides.id}条\n这是第${overrides.id}条记忆的摘要内容`,
    tags: '[]',
    created_at: '2026-01-01T00:00:00Z',
    importance: 3,
    agent_id: 'default',
    ...overrides,
  };
}

/** 四条覆盖类型/来源/agent 组合的标准数据：3 号 permanent 用于批量禁选断言 */
const MEMORIES: MemoryRow[] = [
  makeRow({ id: 1, type: 'long_term' }),
  makeRow({ id: 2, type: 'short_term', source: 'vision', agent_id: 'alice', created_at: '2026-02-02T00:00:00Z' }),
  makeRow({ id: 3, type: 'permanent' }),
  makeRow({ id: 4, type: 'diary', source: 'distill', agent_id: 'bob' }),
];

function jsonRes(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/* ── URL 判别（后端端点契约） ─────────────────────────────────────────────── */

const isList = (u: string) => u.endsWith('/api/memories') || u.includes('/api/memories?');
const isSearch = (u: string) => u.includes('/memories/search');
const is3d = (u: string) => u.includes('/memories/3d');
const isDiary = (u: string) => u.includes('/memories/diary');
const isBatch = (u: string) => u.includes('/memories/batch-delete');
const isDecayStats = (u: string) => u.includes('/memories/decay-stats');
const isSyncDecay = (u: string) => u.includes('/memories/sync-decay');
const isDistill = (u: string) => u.includes('/memory/distill');
const isChatMessage = (u: string) => u.includes('/chat/message');

/** 列表默认路由：挂载与写后 reload 都回 MEMORIES */
const listRoute = { match: isList, reply: () => jsonRes(MEMORIES) };

/** 组装 URL 感知 fetch 替身：按序匹配路由（match 可看 url 与 init），未命中直接抛错（暴露意料外请求） */
function makeFetch(
  routes: Array<{
    match: (u: string, init?: RequestInit) => boolean;
    reply: (u: string, init?: RequestInit) => Response;
  }>,
): ReturnType<typeof vi.fn> {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    for (const r of routes) {
      if (r.match(url)) return r.reply(url, init);
    }
    throw new Error(`unexpected url: ${url}`);
  });
}

/** 等待并找到第一个匹配谓词的调用（url + init）。
 *  必须异步：requestJson 内部先 await ensureBackendToken 再发 fetch，
 *  fireEvent 同步返回时请求往往还在微任务队列里，同步断言会假阴性。 */
async function findCall(
  mock: ReturnType<typeof vi.fn>,
  pred: (u: string, init?: RequestInit) => boolean,
): Promise<[string, RequestInit | undefined]> {
  return vi.waitFor(() => {
    const call = mock.mock.calls.find(([u, i]) => pred(String(u), i as RequestInit | undefined));
    expect(call, `未找到匹配的 fetch 调用`).toBeTruthy();
    return call as [string, RequestInit | undefined];
  });
}

describe('MemoriesPage：三维过滤（类型/来源/智能体）', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = makeFetch([listRoute]);
    vi.stubGlobal('fetch', fetchMock);
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('类型下拉选「长期」→ 只剩 long_term 记忆', async () => {
    render(<MemoriesPage />);
    expect(await screen.findByText('记忆标题第1条')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('类型筛选'), { target: { value: 'long_term' } });

    expect(screen.getByText('记忆标题第1条')).toBeInTheDocument();
    expect(screen.queryByText('记忆标题第2条')).not.toBeInTheDocument();
    expect(screen.queryByText('记忆标题第3条')).not.toBeInTheDocument();
    expect(screen.queryByText('记忆标题第4条')).not.toBeInTheDocument();
  });

  it('来源下拉选「视觉」→ 只剩 vision 来源记忆（2 号）', async () => {
    render(<MemoriesPage />);
    expect(await screen.findByText('记忆标题第1条')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('来源筛选'), { target: { value: 'vision' } });

    expect(screen.getByText('记忆标题第2条')).toBeInTheDocument();
    expect(screen.queryByText('记忆标题第1条')).not.toBeInTheDocument();
    expect(screen.queryByText('记忆标题第3条')).not.toBeInTheDocument();
  });

  it('智能体下拉选 alice → 只剩该 agent 记忆', async () => {
    render(<MemoriesPage />);
    expect(await screen.findByText('记忆标题第1条')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('智能体筛选'), { target: { value: 'alice' } });

    expect(screen.getByText('记忆标题第2条')).toBeInTheDocument();
    expect(screen.queryByText('记忆标题第1条')).not.toBeInTheDocument();
    expect(screen.queryByText('记忆标题第4条')).not.toBeInTheDocument();
  });
});

describe('MemoriesPage：视图切换（卡片/列表/日记）', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = makeFetch([
      { match: isDiary, reply: () => jsonRes([{ date: '2026-02-01', memories: [makeRow({ id: 9 })] }]) },
      listRoute,
    ]);
    vi.stubGlobal('fetch', fetchMock);
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('切到列表视图 → 内容仍渲染；切到日记视图 → 拉取 diary 端点并渲染分组', async () => {
    render(<MemoriesPage />);
    expect(await screen.findByText('记忆标题第1条')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '列表' }));
    expect(screen.getByText('记忆标题第1条')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '日记' }));
    expect(await screen.findByText('2026-02-01')).toBeInTheDocument();
    expect(screen.getByText('记忆标题第9条')).toBeInTheDocument();

    const diaryCall = await findCall(fetchMock, (u) => isDiary(u));
    expect(diaryCall[0]).toContain('date=');
  });

  it('日记端点返回空 → 显示空态提示', async () => {
    fetchMock = makeFetch([
      { match: isDiary, reply: () => jsonRes([]) },
      listRoute,
    ]);
    vi.stubGlobal('fetch', fetchMock);
    render(<MemoriesPage />);
    await screen.findByText('记忆标题第1条');

    fireEvent.click(screen.getByRole('button', { name: '日记' }));
    expect(await screen.findByText('这一天还没有留下记忆')).toBeInTheDocument();
  });
});

describe('MemoriesPage：批量删除确认流', () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  let confirmSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    fetchMock = makeFetch([
      { match: isBatch, reply: () => jsonRes({ deleted_count: 3, skipped: [] }) },
      listRoute,
    ]);
    vi.stubGlobal('fetch', fetchMock);
    confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('进入批量 → permanent 勾选框禁选 → 全选只选可选 3 条 → 确认后调 batch-delete 并刷新', async () => {
    render(<MemoriesPage />);
    expect(await screen.findByText('记忆标题第1条')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '批量管理' }));

    // permanent（3 号）勾选框禁用
    const lockedBox = screen.getByLabelText('选择 记忆标题第3条') as HTMLInputElement;
    expect(lockedBox).toBeDisabled();

    // 全选：只勾中可选的 1/2/4
    fireEvent.click(screen.getByLabelText('全选'));
    expect(screen.getByLabelText('选择 记忆标题第1条')).toBeChecked();
    expect(screen.getByLabelText('选择 记忆标题第2条')).toBeChecked();
    expect(screen.getByLabelText('选择 记忆标题第4条')).toBeChecked();

    fireEvent.click(screen.getByRole('button', { name: '批量删除（3）' }));

    // confirm 被真实调用
    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(String(confirmSpy.mock.calls[0][0])).toContain('3 条记忆');

    // batch-delete 端点被调用，body.ids 为 3 条
    const [url, init] = await findCall(
      fetchMock,
      (u, i) => isBatch(u) && i?.method === 'POST',
    );
    expect(url).toBe(API_ENDPOINTS.memories.batchDelete);
    const body = JSON.parse(String(init?.body)) as { ids: string[] };
    expect(body.ids.sort()).toEqual(['1', '2', '4']);

    // 删除后列表刷新（第二次列表拉取）
    await vi.waitFor(() => {
      const listCalls = fetchMock.mock.calls.filter(([u]) => isList(String(u)));
      expect(listCalls.length).toBeGreaterThanOrEqual(2);
    });
    expect(await screen.findByText('记忆标题第1条')).toBeInTheDocument();
  });

  it('confirm 取消 → 不调 batch-delete', async () => {
    confirmSpy.mockReturnValue(false);
    render(<MemoriesPage />);
    await screen.findByText('记忆标题第1条');

    fireEvent.click(screen.getByRole('button', { name: '批量管理' }));
    fireEvent.click(screen.getByLabelText('选择 记忆标题第1条'));
    fireEvent.click(screen.getByRole('button', { name: '批量删除（1）' }));

    expect(fetchMock.mock.calls.some(([u]) => isBatch(String(u)))).toBe(false);
  });
});

describe('MemoriesPage：新建记忆弹窗', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = makeFetch([
      { match: (u, i) => isList(u) && i?.method === 'POST', reply: () => jsonRes(makeRow({ id: 20 })) },
      listRoute,
    ]);
    vi.stubGlobal('fetch', fetchMock);
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('新建 → 填内容/类型/重要性 → 提交 POST /api/memories 且 body 完整', async () => {
    render(<MemoriesPage />);
    expect(await screen.findByText('记忆标题第1条')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '新建记忆' }));
    const dialog = await screen.findByRole('dialog', { name: '新建记忆' });
    expect(dialog).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('记忆内容'), {
      target: { value: '下周三一起去海边看日落' },
    });
    fireEvent.click(screen.getByLabelText('永久'));
    fireEvent.click(screen.getByLabelText('4星'));
    fireEvent.change(screen.getByLabelText('标签（逗号分隔）'), { target: { value: '旅行, 海边' } });

    fireEvent.click(screen.getByRole('button', { name: '创建记忆' }));

    const [url, init] = await findCall(
      fetchMock,
      (u, i) => String(u) === API_ENDPOINTS.memories.list && i?.method === 'POST',
    );
    const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
    expect(body.content).toBe('下周三一起去海边看日落');
    expect(body.memory_type).toBe('permanent');
    expect(body.importance).toBe(4);
    expect(body.tags).toEqual(['旅行', '海边']);
    expect(body.agent_id).toBe('default');

    // 保存成功后弹窗关闭并回列表
    await vi.waitFor(() => {
      expect(screen.queryByRole('dialog', { name: '新建记忆' })).not.toBeInTheDocument();
    });
  });
});

describe('MemoriesPage：详情查看与编辑', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = makeFetch([
      { match: (u, i) => /\/api\/memories\/\d+$/.test(u) && i?.method === 'PUT', reply: () => jsonRes(makeRow({ id: 1 })) },
      listRoute,
    ]);
    vi.stubGlobal('fetch', fetchMock);
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('点卡片开详情 → 编辑 → 修改内容 → PUT /api/memories/{id}', async () => {
    render(<MemoriesPage />);
    expect(await screen.findByText('记忆标题第1条')).toBeInTheDocument();

    fireEvent.click(screen.getByText('记忆标题第1条'));
    const detail = await screen.findByRole('dialog', { name: '记忆详情' });
    expect(detail).toHaveTextContent('这是第1条记忆的摘要内容');

    fireEvent.click(screen.getByRole('button', { name: '编辑' }));
    await screen.findByRole('dialog', { name: '编辑记忆' });

    fireEvent.change(screen.getByLabelText('记忆内容'), {
      target: { value: '改过的记忆内容' },
    });
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }));

    const [url, init] = await findCall(
      fetchMock,
      (u, i) => /\/api\/memories\/\d+$/.test(u) && i?.method === 'PUT',
    );
    expect(url).toBe(`${API_ENDPOINTS.memories.list}/1`);
    const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
    expect(body.content).toBe('改过的记忆内容');
  });
});

describe('MemoriesPage：三维检索面板', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = makeFetch([
      { match: is3d, reply: () => jsonRes([makeRow({ id: 11, importance: 5 })]) },
      listRoute,
    ]);
    vi.stubGlobal('fetch', fetchMock);
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('输入问题 + 调重要性权重滑杆 → 请求 URL 携带三维权重参数 → 渲染结果', async () => {
    render(<MemoriesPage />);
    expect(await screen.findByText('记忆标题第1条')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '三维检索' }));
    fireEvent.change(screen.getByLabelText('三维检索问题'), {
      target: { value: '项目计划' },
    });
    // 重要性权重调到 0.8（时间/相关性保持默认均分 0.33）
    fireEvent.change(screen.getByLabelText('重要性权重'), { target: { value: '0.8' } });
    fireEvent.click(screen.getByRole('button', { name: '开始检索' }));

    const [url] = await findCall(fetchMock, (u) => is3d(u));
    expect(url).toContain('query=');
    expect(decodeURIComponent(url)).toContain('项目计划');
    expect(url).toContain('w_importance=0.8');
    expect(url).toContain('w_time=0.33');
    expect(url).toContain('w_rel=0.33');

    expect(await screen.findByText('记忆标题第11条')).toBeInTheDocument();
  });
});

describe('MemoriesPage：遗忘衰减面板', () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  let confirmSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    fetchMock = makeFetch([
      { match: isSyncDecay, reply: () => jsonRes({ deleted_count: 2 }) },
      { match: isDecayStats, reply: () => jsonRes({ total: 10, decaying: [1, 2], decayed_count: 3 }) },
      listRoute,
    ]);
    vi.stubGlobal('fetch', fetchMock);
    confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('展开面板 → 统计展示（宽松解析）→ 执行衰减 → sync-decay 调用 + 完成提示 + 列表刷新', async () => {
    render(<MemoriesPage />);
    expect(await screen.findByText('记忆标题第1条')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '遗忘衰减' }));

    expect(await screen.findByLabelText('记忆总数')).toHaveTextContent('10');
    expect(screen.getByLabelText('即将遗忘')).toHaveTextContent('2');
    expect(screen.getByLabelText('已衰减归档')).toHaveTextContent('3');

    fireEvent.click(screen.getByRole('button', { name: '执行衰减整理' }));

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    await findCall(fetchMock, (u, i) => isSyncDecay(u) && i?.method === 'POST');

    expect(await screen.findByText(/衰减整理完成，已归档 2 条记忆/)).toBeInTheDocument();
    await vi.waitFor(() => {
      const listCalls = fetchMock.mock.calls.filter(([u]) => isList(String(u)));
      expect(listCalls.length).toBeGreaterThanOrEqual(2);
    });
  });
});

describe('MemoriesPage：整理记忆', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = makeFetch([
      { match: isDistill, reply: () => jsonRes({ ok: true }) },
      listRoute,
    ]);
    vi.stubGlobal('fetch', fetchMock);
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('点击整理记忆 → POST /api/memory/distill → 完成提示 + 列表刷新', async () => {
    render(<MemoriesPage />);
    expect(await screen.findByText('记忆标题第1条')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '整理记忆' }));

    await findCall(fetchMock, (u, i) => isDistill(u) && i?.method === 'POST');
    expect(await screen.findByText('整理完成，记忆已经焕然一新')).toBeInTheDocument();
    await vi.waitFor(() => {
      const listCalls = fetchMock.mock.calls.filter(([u]) => isList(String(u)));
      expect(listCalls.length).toBeGreaterThanOrEqual(2);
    });
  });
});

describe('MemoriesPage：记忆管理助手', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = makeFetch([
      { match: isChatMessage, reply: () => jsonRes({ ok: true, clean_text: '已为你找到 2 条关于项目计划的记忆', mood: 'calm', raw: '' }) },
      listRoute,
    ]);
    vi.stubGlobal('fetch', fetchMock);
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('顶部入口打开面板 → 发送消息 → POST /api/chat/message 带 agent_id=memory-agent → 渲染 clean_text', async () => {
    render(<MemoriesPage />);
    expect(await screen.findByText('记忆标题第1条')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '记忆管理助手' }));
    expect(await screen.findByRole('dialog', { name: '记忆管理助手' })).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('助手输入框'), {
      target: { value: '帮我找关于项目计划的记忆' },
    });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    const [url, init] = await findCall(
      fetchMock,
      (u, i) => isChatMessage(u) && i?.method === 'POST',
    );
    expect(url).toBe(API_ENDPOINTS.chat.message);
    const body = JSON.parse(String(init?.body)) as { message: string; agent_id: string };
    expect(body.message).toBe('帮我找关于项目计划的记忆');
    expect(body.agent_id).toBe('memory-agent');

    // 气泡渲染后端 clean_text（指令标签由后端剥离，前端原样展示）
    expect(await screen.findByText('已为你找到 2 条关于项目计划的记忆')).toBeInTheDocument();
    // 用户气泡也在
    expect(screen.getByText('帮我找关于项目计划的记忆')).toBeInTheDocument();
  });

  it('发送失败 → 用户气泡标「未送达」，不伪造助手回复', async () => {
    fetchMock = makeFetch([
      {
        match: isChatMessage,
        reply: () => {
          throw new Error('ECONNREFUSED: backend not running');
        },
      },
      listRoute,
    ]);
    vi.stubGlobal('fetch', fetchMock);
    vi.spyOn(console, 'error').mockImplementation(() => {});

    render(<MemoriesPage />);
    await screen.findByText('记忆标题第1条');

    fireEvent.click(screen.getByRole('button', { name: '记忆管理助手' }));
    await screen.findByRole('dialog', { name: '记忆管理助手' });
    fireEvent.change(screen.getByLabelText('助手输入框'), { target: { value: '在吗' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(await screen.findByText('未送达')).toBeInTheDocument();
    expect(await screen.findByText(/助手暂时无法回应/)).toBeInTheDocument();
    // 全程只有用户一条气泡（无伪造 bot 回复）
    const bubbles = document.querySelectorAll('.rounded-2xl.px-3\\.5');
    expect(bubbles.length).toBe(1);
  });
});
