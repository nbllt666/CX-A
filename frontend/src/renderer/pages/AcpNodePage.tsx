/**
 * ACP 节点互联页（20261017 CX-O 全量迁移批次 6）。
 *
 * 形态（用户裁决）：CX-A 本机单角色，本页展示「节点互联」而非本机多角色管理——
 * - 统计行：节点总数 / 在线数 / 消息总数；
 * - 本机节点卡：agent_id / 名称（ACP 开启时展示，关闭时给设置页指引）；
 * - 节点列表：局域网发现 + 手动添加（host/port）；行操作：连接、移除；
 * - 消息互通：选中节点后查看历史、发消息（对端自动回复）；
 * - CXFC 远端节点区：添加局域网 CX-O 主机 → 拉取插件清单（跨机调用前置）。
 *
 * 入口：管理面（#/fleet）内「节点互联」按钮，或深链 `#/nodes`（与 FleetPage
 * 同为隐藏管理能力，普通用户界面无入口）。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { Network, Plus, RefreshCw, Send, Trash2 } from 'lucide-react';
import {
  addAcpNode,
  addCxfcRemoteNode,
  connectAcpAgent,
  discoverAcpNodes,
  fetchAcpNodeMessages,
  fetchAcpStats,
  fetchCxfcRemotePlugins,
  listAcpAgents,
  listCxfcRemoteNodes,
  removeAcpAgent,
  removeCxfcRemoteNode,
  sendAcpNodeMessage,
  type AcpAgent,
  type AcpNodeMessage,
  type AcpNodeStats,
  type CxfcRemoteNode,
} from '../api';

/** 从 ACP 消息 content 提取可读文本（兼容 string / {text} / 其他对象） */
function extractText(content: AcpNodeMessage['content']): string {
  if (typeof content === 'string') return content;
  const text = (content as { text?: unknown })?.text;
  if (typeof text === 'string') return text;
  return JSON.stringify(content);
}

export default function AcpNodePage() {
  const [stats, setStats] = useState<AcpNodeStats | null>(null);
  const [agents, setAgents] = useState<AcpAgent[]>([]);
  const [selected, setSelected] = useState<AcpAgent | null>(null);
  const [messages, setMessages] = useState<AcpNodeMessage[]>([]);
  const [draft, setDraft] = useState('');
  const [addHost, setAddHost] = useState('');
  const [addPort, setAddPort] = useState('');
  const [scanning, setScanning] = useState(false);
  const [hint, setHint] = useState<string | null>(null);
  const [remoteNodes, setRemoteNodes] = useState<CxfcRemoteNode[]>([]);
  const [remoteHost, setRemoteHost] = useState('');
  const [remotePort, setRemotePort] = useState('');
  const [remoteBusy, setRemoteBusy] = useState(false);
  const msgSeq = useRef(0);

  /** 节点列表 + 统计刷新 */
  const reload = useCallback(async () => {
    const [agentList, stat] = await Promise.all([
      listAcpAgents().catch(() => []),
      fetchAcpStats(),
    ]);
    setAgents(agentList);
    setStats(stat);
    setSelected((current) => {
      if (current && agentList.some((a) => a.id === current.id)) return current;
      // 默认选中第一个非本机节点
      return agentList.find((a) => a.metadata?.source !== 'cxa_main') ?? null;
    });
  }, []);

  /** 消息历史刷新（单调序号防快速切换旧响应覆盖） */
  const reloadMessages = useCallback(async (agentId: string) => {
    const seq = ++msgSeq.current;
    const list = await fetchAcpNodeMessages(agentId).catch(() => []);
    if (seq === msgSeq.current) setMessages(list);
  }, []);

  useEffect(() => {
    void reload();
    void listCxfcRemoteNodes()
      .then(setRemoteNodes)
      .catch(() => undefined);
  }, [reload]);

  useEffect(() => {
    if (!selected) return;
    void reloadMessages(selected.id);
  }, [selected, reloadMessages]);

  /** 主动发现：广播催促 + 刷新列表 */
  const handleScan = async () => {
    setScanning(true);
    setHint(null);
    try {
      await discoverAcpNodes();
      await reload();
      setHint('已广播本机存在，局域网节点会陆续出现（对方也需开启互联）');
    } catch (err) {
      setHint(err instanceof Error ? err.message : '发现失败，请确认已开启节点互联');
    } finally {
      setScanning(false);
    }
  };

  /** 手动添加节点 */
  const handleAdd = async () => {
    const port = Number(addPort);
    if (!addHost.trim() || !Number.isInteger(port) || port < 1 || port > 65535) {
      setHint('请填写有效的主机地址与端口（1~65535）');
      return;
    }
    try {
      await addAcpNode({ host: addHost.trim(), port, name: '' });
      setAddHost('');
      setAddPort('');
      await reload();
      setHint('节点已添加');
    } catch (err) {
      setHint(err instanceof Error ? err.message : '添加失败');
    }
  };

  /** 行操作：连接 / 移除 */
  const handleConnect = async (agent: AcpAgent) => {
    try {
      await connectAcpAgent(agent.id);
      setHint(`已建立与 ${agent.name || agent.id} 的连接`);
    } catch (err) {
      setHint(err instanceof Error ? err.message : '连接失败');
    }
  };

  const handleRemove = async (agent: AcpAgent) => {
    if (!window.confirm(`移除节点 ${agent.name || agent.id}？`)) return;
    try {
      await removeAcpAgent(agent.id);
      setSelected(null);
      await reload();
    } catch (err) {
      setHint(err instanceof Error ? err.message : '移除失败');
    }
  };

  /** 发送消息（对端自动回复） */
  const handleSend = async () => {
    if (!selected || !draft.trim()) return;
    const text = draft.trim();
    setDraft('');
    try {
      await sendAcpNodeMessage(selected.id, text);
      await reloadMessages(selected.id);
    } catch (err) {
      setHint(err instanceof Error ? err.message : '发送失败');
    }
  };

  /** CXFC：添加远端 CX-O 主机 + 拉插件清单 */
  const handleAddRemote = async () => {
    const port = Number(remotePort);
    if (!remoteHost.trim() || !Number.isInteger(port) || port < 1 || port > 65535) {
      setHint('请填写有效的主机地址与端口');
      return;
    }
    setRemoteBusy(true);
    try {
      const node = await addCxfcRemoteNode({ host: remoteHost.trim(), port });
      await listCxfcRemoteNodes().then(setRemoteNodes).catch(() => undefined);
      setRemoteHost('');
      setRemotePort('');
      setHint(`远端节点已添加：${node.name}`);
    } catch (err) {
      setHint(err instanceof Error ? err.message : '添加失败');
    } finally {
      setRemoteBusy(false);
    }
  };

  const handleFetchPlugins = async (node: CxfcRemoteNode) => {
    setRemoteBusy(true);
    try {
      const updated = await fetchCxfcRemotePlugins(node.id);
      setRemoteNodes((current) => current.map((n) => (n.id === updated.id ? updated : n)));
      setHint(`已拉取 ${updated.plugins.length} 个插件`);
    } catch (err) {
      setHint(err instanceof Error ? err.message : '拉取失败（对方不在线？）');
    } finally {
      setRemoteBusy(false);
    }
  };

  const handleRemoveRemote = async (node: CxfcRemoteNode) => {
    try {
      await removeCxfcRemoteNode(node.id);
      setRemoteNodes((current) => current.filter((n) => n.id !== node.id));
    } catch (err) {
      setHint(err instanceof Error ? err.message : '移除失败');
    }
  };

  const localAgent = agents.find((a) => a.metadata?.source === 'cxa_main') ?? null;
  const remoteAgents = agents.filter((a) => a.metadata?.source !== 'cxa_main');

  return (
    <div className="mx-auto w-full max-w-4xl px-6 py-6" data-testid="acp-node-page">
      <header className="mb-4 flex items-center gap-3">
        <Network className="h-6 w-6 text-[var(--color-primary)]" aria-hidden="true" />
        <div>
          <h1 className="text-lg font-semibold text-[var(--text-primary)]">节点互联</h1>
          <p className="text-xs text-[var(--text-secondary)]">
            和局域网里的其他 CX-O / CX-A 互相发现、聊天、调用对方的插件
          </p>
        </div>
      </header>

      {/* 统计行 */}
      <div className="mb-4 grid grid-cols-3 gap-3">
        {[
          { label: '节点总数', value: stats?.total_agents ?? agents.length },
          { label: '在线节点', value: stats?.online_agents ?? 0 },
          { label: '消息总数', value: stats?.total_messages ?? 0 },
        ].map((item) => (
          <div key={item.label} className="rounded-2xl border border-[var(--stroke)] bg-[var(--glass)] p-3 text-center">
            <div className="text-xl font-semibold text-[var(--text-primary)]">{item.value}</div>
            <div className="text-xs text-[var(--text-secondary)]">{item.label}</div>
          </div>
        ))}
      </div>

      {hint ? (
        <p className="mb-3 rounded-xl bg-[var(--glass)] px-3 py-2 text-xs text-[var(--text-secondary)]" role="status">
          {hint}
        </p>
      ) : null}

      {/* 本机节点卡 */}
      {localAgent ? (
        <div className="mb-4 rounded-2xl border border-[var(--stroke)] bg-[var(--glass)] p-4">
          <div className="text-sm font-medium text-[var(--text-primary)]">
            本机节点：{localAgent.name || 'CX-A'}
          </div>
          <div className="text-xs text-[var(--text-secondary)]">
            标识 {localAgent.id} · 在线 · 与局域网内开启互联的节点互通
          </div>
        </div>
      ) : (
        <div className="mb-4 rounded-2xl border border-[var(--stroke)] bg-[var(--glass)] p-4 text-sm text-[var(--text-secondary)]">
          节点互联未开启——请到 设置 → ACP 节点互联 打开开关。
        </div>
      )}

      {/* 节点列表 */}
      <section className="mb-4">
        <div className="mb-2 flex items-center gap-2">
          <h2 className="text-sm font-medium text-[var(--text-primary)]">节点列表</h2>
          <button
            type="button"
            onClick={handleScan}
            disabled={scanning}
            className="inline-flex items-center gap-1 rounded-full border border-[var(--stroke)] px-3 py-1 text-xs text-[var(--text-primary)] hover:border-[var(--pink-300)] disabled:opacity-50"
          >
            <RefreshCw className={`h-3.5 w-3.5${scanning ? ' animate-spin' : ''}`} aria-hidden="true" />
            {scanning ? '发现中…' : '扫描局域网'}
          </button>
        </div>
        <div className="mb-2 flex gap-2">
          <input
            value={addHost}
            onChange={(e) => setAddHost(e.target.value)}
            placeholder="主机地址，如 192.168.1.8"
            aria-label="手动添加节点主机地址"
            className="flex-1 rounded-xl border border-[var(--stroke)] bg-[var(--glass)] px-3 py-1.5 text-sm text-[var(--text-primary)] outline-none focus:border-[var(--pink-300)]"
          />
          <input
            value={addPort}
            onChange={(e) => setAddPort(e.target.value)}
            placeholder="端口"
            aria-label="手动添加节点端口"
            className="w-24 rounded-xl border border-[var(--stroke)] bg-[var(--glass)] px-3 py-1.5 text-sm text-[var(--text-primary)] outline-none focus:border-[var(--pink-300)]"
          />
          <button
            type="button"
            onClick={handleAdd}
            className="inline-flex items-center gap-1 rounded-xl border border-[var(--stroke)] px-3 py-1.5 text-xs text-[var(--text-primary)] hover:border-[var(--pink-300)]"
          >
            <Plus className="h-3.5 w-3.5" aria-hidden="true" /> 添加节点
          </button>
        </div>
        {remoteAgents.length === 0 ? (
          <p className="rounded-xl bg-[var(--glass)] px-3 py-2 text-xs text-[var(--text-secondary)]">
            还没有其他节点——双方都开启互联后，点「扫描局域网」即可互相发现。
          </p>
        ) : (
          <ul className="space-y-2">
            {remoteAgents.map((agent) => (
              <li
                key={agent.id}
                className={`flex items-center justify-between rounded-2xl border px-3 py-2 ${
                  selected?.id === agent.id
                    ? 'border-[var(--pink-300)] bg-[var(--glass)]'
                    : 'border-[var(--stroke)] bg-[var(--glass)]'
                }`}
              >
                <button
                  type="button"
                  className="flex-1 text-left"
                  onClick={() => setSelected(agent)}
                  aria-label={`选中节点 ${agent.name || agent.id}`}
                >
                  <span className="text-sm text-[var(--text-primary)]">
                    {agent.name || agent.id}
                  </span>
                  <span
                    className={`ml-2 inline-block rounded-full px-2 py-0.5 text-[10px] ${
                      agent.status === 'online'
                        ? 'bg-green-100 text-green-700'
                        : 'bg-gray-200 text-gray-500'
                    }`}
                  >
                    {agent.status === 'online' ? '在线' : '离线'}
                  </span>
                  <span className="ml-2 text-xs text-[var(--text-secondary)]">
                    {agent.host}:{agent.port}
                  </span>
                </button>
                <div className="flex items-center gap-1">
                  <button
                    type="button"
                    onClick={() => handleConnect(agent)}
                    className="rounded-full border border-[var(--stroke)] px-2 py-1 text-[10px] text-[var(--text-primary)] hover:border-[var(--pink-300)]"
                    aria-label={`连接 ${agent.name || agent.id}`}
                  >
                    连接
                  </button>
                  <button
                    type="button"
                    onClick={() => handleRemove(agent)}
                    className="rounded-full border border-[var(--stroke)] p-1 text-[var(--text-secondary)] hover:border-red-300"
                    aria-label={`移除 ${agent.name || agent.id}`}
                  >
                    <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* 消息互通 */}
      {selected ? (
        <section className="mb-4 rounded-2xl border border-[var(--stroke)] bg-[var(--glass)] p-4">
          <h2 className="mb-2 text-sm font-medium text-[var(--text-primary)]">
            与 {selected.name || selected.id} 聊天
          </h2>
          <div className="mb-2 max-h-64 space-y-2 overflow-y-auto" data-testid="acp-message-list">
            {messages.length === 0 ? (
              <p className="text-xs text-[var(--text-secondary)]">还没有消息——发一句打个招呼吧。</p>
            ) : (
              messages.map((msg) => (
                <div
                  key={msg.id}
                  className={`max-w-[80%] rounded-2xl px-3 py-2 text-sm ${
                    msg.is_sent
                      ? 'ml-auto bg-[var(--pink-100,#ffe3f1)] text-[var(--text-primary)]'
                      : 'bg-white/70 text-[var(--text-primary)]'
                  }`}
                >
                  {!msg.is_sent ? (
                    <div className="text-[10px] text-[var(--text-secondary)]">
                      {msg.from_agent_name || msg.from_agent_id}
                    </div>
                  ) : null}
                  {extractText(msg.content)}
                </div>
              ))
            )}
          </div>
          <div className="flex gap-2">
            <input
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.nativeEvent.isComposing) void handleSend();
              }}
              placeholder="给对方发条消息…"
              aria-label="节点消息输入框"
              className="flex-1 rounded-xl border border-[var(--stroke)] bg-white/70 px-3 py-1.5 text-sm text-[var(--text-primary)] outline-none focus:border-[var(--pink-300)]"
            />
            <button
              type="button"
              onClick={handleSend}
              className="inline-flex items-center gap-1 rounded-xl bg-[var(--color-primary)] px-3 py-1.5 text-xs text-white"
              aria-label="发送节点消息"
            >
              <Send className="h-3.5 w-3.5" aria-hidden="true" /> 发送
            </button>
          </div>
        </section>
      ) : null}

      {/* CXFC 远端节点（跨机插件调用） */}
      <section>
        <h2 className="mb-2 text-sm font-medium text-[var(--text-primary)]">远端插件节点（CX-O）</h2>
        <div className="mb-2 flex gap-2">
          <input
            value={remoteHost}
            onChange={(e) => setRemoteHost(e.target.value)}
            placeholder="CX-O 主机地址"
            aria-label="远端插件节点主机地址"
            className="flex-1 rounded-xl border border-[var(--stroke)] bg-[var(--glass)] px-3 py-1.5 text-sm text-[var(--text-primary)] outline-none focus:border-[var(--pink-300)]"
          />
          <input
            value={remotePort}
            onChange={(e) => setRemotePort(e.target.value)}
            placeholder="端口"
            aria-label="远端插件节点端口"
            className="w-24 rounded-xl border border-[var(--stroke)] bg-[var(--glass)] px-3 py-1.5 text-sm text-[var(--text-primary)] outline-none focus:border-[var(--pink-300)]"
          />
          <button
            type="button"
            onClick={handleAddRemote}
            disabled={remoteBusy}
            className="rounded-xl border border-[var(--stroke)] px-3 py-1.5 text-xs text-[var(--text-primary)] hover:border-[var(--pink-300)] disabled:opacity-50"
          >
            添加
          </button>
        </div>
        {remoteNodes.length === 0 ? (
          <p className="rounded-xl bg-[var(--glass)] px-3 py-2 text-xs text-[var(--text-secondary)]">
            添加局域网里的 CX-O 主机后，可拉取它注册的插件清单（跨机调用需在设置里配置管理密钥）。
          </p>
        ) : (
          <ul className="space-y-2">
            {remoteNodes.map((node) => (
              <li
                key={node.id}
                className="flex items-center justify-between rounded-2xl border border-[var(--stroke)] bg-[var(--glass)] px-3 py-2"
              >
                <div>
                  <div className="text-sm text-[var(--text-primary)]">{node.name}</div>
                  <div className="text-xs text-[var(--text-secondary)]">
                    {node.host}:{node.port} · {node.plugins.length} 个插件
                    {node.plugins_fetched_at ? ` · 拉取于 ${node.plugins_fetched_at.slice(11, 19)}` : ''}
                  </div>
                </div>
                <div className="flex items-center gap-1">
                  <button
                    type="button"
                    onClick={() => handleFetchPlugins(node)}
                    disabled={remoteBusy}
                    className="rounded-full border border-[var(--stroke)] px-2 py-1 text-[10px] text-[var(--text-primary)] hover:border-[var(--pink-300)] disabled:opacity-50"
                    aria-label={`拉取 ${node.name} 插件清单`}
                  >
                    拉取插件
                  </button>
                  <button
                    type="button"
                    onClick={() => handleRemoveRemote(node)}
                    className="rounded-full border border-[var(--stroke)] p-1 text-[var(--text-secondary)] hover:border-red-300"
                    aria-label={`移除 ${node.name}`}
                  >
                    <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
