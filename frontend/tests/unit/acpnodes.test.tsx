import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import AcpNodePage from '../../src/renderer/pages/AcpNodePage';

/**
 * ACP 节点互联页单测（20261017 CX-O 全量迁移批次 6）：
 * 本机节点卡 + 节点列表渲染 + 手动添加节点流程（fetch mock 全覆盖）。
 */

const LOCAL_AGENT = {
  id: 'cxa-agent-001', name: 'CX-A', host: '127.0.0.1', port: 0,
  status: 'online', version: '1.0.0', capabilities: ['chat'],
  last_seen: '2026-10-17T00:00:00', metadata: { source: 'cxa_main' },
};

function stubFetch(handlers: Array<(url: string, init?: RequestInit) => { status?: number; body: unknown } | undefined>) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    for (const handler of handlers) {
      const result = handler(url, init);
      if (result) {
        return new Response(JSON.stringify(result.body ?? {}), {
          status: result.status ?? 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
    }
    return new Response(JSON.stringify({}), { status: 404 });
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('AcpNodePage：节点互联视图', () => {
  it('渲染本机节点卡与空节点提示；统计行展示', async () => {
    stubFetch([
      (url) => {
        if (url.includes('/api/acp/agents')) return { body: { status: 'success', agents: [LOCAL_AGENT], total: 1 } };
        if (url.includes('/api/acp/stats')) {
          return { body: { status: 'success', statistics: { total_agents: 1, online_agents: 1, total_connections: 0, total_messages: 0, local_agent_id: 'cxa-agent-001', local_agent_name: 'CX-A' } } };
        }
        if (url.includes('/api/cxfc/remote/nodes')) return { body: { nodes: [] } };
        return undefined;
      },
    ]);
    render(<AcpNodePage />);
    await waitFor(() => expect(screen.getByText(/本机节点：CX-A/)).toBeTruthy());
    expect(screen.getByText('还没有其他节点——双方都开启互联后，点「扫描局域网」即可互相发现。')).toBeTruthy();
  });

  it('手动添加节点：填写 host/port → POST /api/acp/agents → 列表刷新', async () => {
    const added = {
      id: 'manual_192.168.1.8_8001', name: '192.168.1.8:8001', host: '192.168.1.8', port: 8001,
      status: 'online', version: '1.0.0', capabilities: [], last_seen: '2026-10-17T01:00:00',
      metadata: { source: 'manual' },
    };
    const fetchMock = stubFetch([
      (url, init) => {
        if (url.includes('/api/acp/agents') && (init?.method ?? 'GET') === 'POST') {
          return { body: { status: 'success', agent: added, message: '节点已添加' } };
        }
        if (url.includes('/api/acp/agents')) {
          // 添加前列表只有本机；添加后（POST 发生过）返回两节点——以 fetch 调用序判断
          const posted = fetchMock.mock.calls.some(
            ([u, i]) => String(u).includes('/api/acp/agents') && (i?.method ?? '') === 'POST',
          );
          return { body: { status: 'success', agents: posted ? [LOCAL_AGENT, added] : [LOCAL_AGENT], total: posted ? 2 : 1 } };
        }
        if (url.includes('/api/acp/stats')) {
          return { body: { status: 'success', statistics: { total_agents: 1, online_agents: 1, total_connections: 0, total_messages: 0, local_agent_id: 'x', local_agent_name: 'CX-A' } } };
        }
        if (url.includes('/api/cxfc/remote/nodes')) return { body: { nodes: [] } };
        return undefined;
      },
    ]);

    render(<AcpNodePage />);
    await waitFor(() => expect(screen.getByText(/本机节点：CX-A/)).toBeTruthy());

    fireEvent.change(screen.getByLabelText('手动添加节点主机地址'), { target: { value: '192.168.1.8' } });
    fireEvent.change(screen.getByLabelText('手动添加节点端口'), { target: { value: '8001' } });
    fireEvent.click(screen.getByRole('button', { name: /添加节点/ }));

    await waitFor(() => expect(screen.getByText('节点已添加')).toBeTruthy());
    // 节点名（缺省=host:port）与地址栏两处同文案 → getAllByText 断言至少一处
    await waitFor(() => expect(screen.getAllByText('192.168.1.8:8001').length).toBeGreaterThan(0));
    expect(fetchMock.mock.calls.some(([u, i]) => String(u).includes('/api/acp/agents') && (i?.method ?? '') === 'POST')).toBe(true);
  });

  it('非法端口：前端拦截不发起请求', async () => {
    const fetchMock = stubFetch([
      (url) => {
        if (url.includes('/api/acp/agents')) return { body: { status: 'success', agents: [LOCAL_AGENT], total: 1 } };
        if (url.includes('/api/acp/stats')) return { body: { statistics: {} } };
        if (url.includes('/api/cxfc/remote/nodes')) return { body: { nodes: [] } };
        return undefined;
      },
    ]);
    render(<AcpNodePage />);
    await waitFor(() => expect(screen.getByText(/本机节点：CX-A/)).toBeTruthy());
    fireEvent.change(screen.getByLabelText('手动添加节点主机地址'), { target: { value: 'h' } });
    fireEvent.change(screen.getByLabelText('手动添加节点端口'), { target: { value: '70000' } });
    fireEvent.click(screen.getByRole('button', { name: /添加节点/ }));
    expect(screen.getByText('请填写有效的主机地址与端口（1~65535）')).toBeTruthy();
    expect(fetchMock.mock.calls.some(([u, i]) => String(u).includes('/api/acp/agents') && (i?.method ?? '') === 'POST')).toBe(false);
  });
});
