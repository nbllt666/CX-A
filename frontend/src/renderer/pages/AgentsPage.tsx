import React, { useEffect, useState } from 'react';
import { GlassCard } from '../components/GlassCard';
import Toggle from '../components/Toggle';
import { IS_BACKEND_READY, fetchAgents, API_ENDPOINTS } from '../api';
import type { AgentRow } from '../api';

/**
 * 管理面结构化占位页模板：所有管理能力以后端 API 提供。
 * 供 RemotePage / StatusPage 等非全量实现页面复用。
 */
interface ManagementSection {
  key: string;
  icon: string;
  title: string;
  desc: string;
  /** 能力接入状态，如「接口未接」「局域网待接」「API 待接」 */
  status: string;
}

interface ManagementPlaceholderProps {
  title: string;
  desc: string;
  endpoint: string;
  icon: string;
  /** 该页规划的可操作区块 */
  sections?: ManagementSection[];
}

export function ManagementPlaceholder({
  title,
  desc,
  endpoint,
  icon,
  sections = [],
}: ManagementPlaceholderProps) {
  return (
    <div className="flex h-full flex-col gap-4 overflow-y-auto p-5">
      <GlassCard hoverable className="w-full shrink-0 !bg-[var(--glass-bg-strong)]">
        <div className="flex items-center gap-4 p-5">
          <span className="text-4xl">{icon}</span>
          <div className="min-w-0 flex-1">
            <h2 className="text-lg font-bold text-gradient">{title}</h2>
            <p className="mt-1 text-sm leading-relaxed text-[var(--text-secondary)]">{desc}</p>
          </div>
          <div className="shrink-0 text-right">
            <p className="inline-block rounded-full bg-[rgba(124,216,255,0.14)] px-3 py-1 text-xs text-[var(--text-tertiary)]">
              管理能力以 API 提供
            </p>
            <p className="mt-1 text-xs text-[var(--text-tertiary)]">接入端点：{endpoint}</p>
          </div>
        </div>
      </GlassCard>

      {sections.length > 0 && (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {sections.map((s) => (
            <GlassCard key={s.key} className="!bg-[var(--glass-bg)]">
              <div className="flex items-start gap-3 p-4">
                <span className="text-2xl leading-none">{s.icon}</span>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <h3 className="text-sm font-semibold">{s.title}</h3>
                    <span className="shrink-0 rounded-full bg-[rgba(124,216,255,0.12)] px-2 py-0.5 text-[10px] text-[var(--text-tertiary)]">
                      {s.status}
                    </span>
                  </div>
                  <p className="mt-1 text-xs leading-relaxed text-[var(--text-secondary)]">
                    {s.desc}
                  </p>
                </div>
              </div>
            </GlassCard>
          ))}
        </div>
      )}

      {!IS_BACKEND_READY && (
        <p className="shrink-0 text-xs text-[var(--text-tertiary)]">
          后端接口就绪后此页面自动点亮
        </p>
      )}
    </div>
  );
}

/**
 * 智能体页（/agents）：本地多 Agent 人设管理。
 * IS_BACKEND_READY=true 时，优先 fetch GET /api/agents 渲染真实人设卡片列表；
 * 后端不可用（网络异常 / 非 2xx）时降级为占位文案。
 */
export default function AgentsPage() {
  /** 数据源：online=后端真实数据，offline=占位文案 */
  const [mode, setMode] = useState<'online' | 'offline'>(IS_BACKEND_READY ? 'online' : 'offline');
  const [loading, setLoading] = useState(IS_BACKEND_READY);
  const [agents, setAgents] = useState<AgentRow[]>([]);

  // 初始化：拉取真实 Agent 列表，失败则降级到占位文案
  useEffect(() => {
    if (!IS_BACKEND_READY) {
      setMode('offline');
      setLoading(false);
      return;
    }
    let alive = true;
    (async () => {
      try {
        const rows = await fetchAgents();
        if (!alive) return;
        setAgents(rows);
        setMode('online');
      } catch {
        if (!alive) return;
        setMode('offline');
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  return (
    <div className="flex h-full flex-col p-5">
      <div className="mb-4">
        <h1 className="text-xl font-bold text-gradient">智能体</h1>
        <p className="text-sm text-[var(--text-secondary)]">
          本地多 Agent 人设管理：集中维护智能体伙伴与人设配置
        </p>
      </div>

      {mode === 'offline' && (
        <GlassCard className="!bg-[var(--glass-bg)]">
          <div className="flex items-center gap-3 p-5">
            <span className="text-3xl">🤖</span>
            <div>
              <p className="text-sm font-semibold">智能体列表暂不可用</p>
              <p className="mt-1 text-xs leading-relaxed text-[var(--text-secondary)]">
                后端 API（{API_ENDPOINTS.management.agents}）当前不可达，端口 8600 服务就绪后自动点亮。
              </p>
            </div>
          </div>
        </GlassCard>
      )}

      {loading && <p className="mb-2 text-xs text-[var(--text-tertiary)]">正在加载智能体…</p>}

      {mode === 'online' && (
        <div className="grid flex-1 auto-rows-min grid-cols-1 gap-3 overflow-y-auto pb-2 sm:grid-cols-2">
          {agents.map((a) => (
            <AgentCard key={a.id} agent={a} />
          ))}
          {agents.length === 0 && (
            <p className="col-span-full py-16 text-center text-sm text-[var(--text-tertiary)]">
              还没有本地智能体，先从后端创建一位吧
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function AgentCard({ agent }: { agent: AgentRow }) {
  return (
    <GlassCard hoverable className="animate-fade-up !bg-[var(--glass-bg)]">
      <div className="p-4">
        <div className="mb-1 flex items-start justify-between gap-2">
          <div className="flex min-w-0 items-center gap-2">
            <span className="text-2xl leading-none">🤖</span>
            <div className="min-w-0">
              <h3 className="truncate font-medium text-[var(--text-primary)]">{agent.name}</h3>
              <p className="text-[10px] text-[var(--text-tertiary)]">
                {agent.id}
                {agent.voice && agent.voice !== 'cx-open' ? ` · 音色 ${agent.voice}` : ''}
              </p>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <span
              className={
                agent.enabled
                  ? 'rounded-full bg-[rgba(124,216,255,0.14)] px-2 py-0.5 text-[10px] text-[var(--text-secondary)]'
                  : 'rounded-full bg-[rgba(255,80,80,0.12)] px-2 py-0.5 text-[10px] text-[var(--text-tertiary)]'
              }
            >
              {agent.enabled ? '已启用' : '已停用'}
            </span>
            <Toggle checked={agent.enabled} onChange={() => {}} disabled label="启用状态" />
          </div>
        </div>
        <p className="mt-2 text-sm leading-relaxed text-[var(--text-secondary)]">{agent.persona}</p>
      </div>
    </GlassCard>
  );
}