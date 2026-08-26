/**
 * 后端 API 接入约定（集中常量，供后续任务统一替换）。
 *
 * 约定：后端 Python 服务托管于 http://127.0.0.1:<端口>/api/** 。
 * 端口由后续任务（A10 起）决定，本期先以常量占位；对接时只需改动此处。
 */

/** 后端服务监听端口占位（待 A10 定案） */
export const API_PORT = 8600;

export const API_BASE = `http://127.0.0.1:${API_PORT}/api`;

/**
 * 是否已接后端：true 时优先拉取真实接口；接口不可用时页面自动降级到 Mock。
 * （后端 lite/server/api_server.py 监听 8600 端口，见 API_PORT。）
 */
export const IS_BACKEND_READY = true;

export const API_ENDPOINTS = {
  chat: {
    /** 发起聊天 */
    sendMessage: `${API_BASE}/chat/messages`,
    /** 拉取历史消息 */
    history: `${API_BASE}/chat/history`,
  },
  memories: {
    /** 记忆列表 */
    list: `${API_BASE}/memories`,
    /** 记忆检索 */
    search: `${API_BASE}/memories/search`,
    /** 软删除单条记忆（按 id） */
    delete: (id: string | number) => `${API_BASE}/memories/${id}`,
  },
  settings: {
    /** 用户可读配置视图（GET，不含 API Key） */
    get: `${API_BASE}/settings`,
    /** 更新可热更配置（PUT，白名单键：cloud.provider / tts.voice / local_llm.enabled） */
    update: `${API_BASE}/settings`,
  },
  management: {
    /** 管理面已收敛为纯 API：前端不再路由这些端点；供另一 Agent / 管理工具调用 */
    agents: `${API_BASE}/agents`,
    remote: `${API_BASE}/remote/status`,
    status: `${API_BASE}/status`,
  },
  chatGuard: {
    /** 聊天服务未启用守卫（避免直连误 404；本期前端走 Mock） */
    send: `${API_BASE}/chat/messages`,
    history: `${API_BASE}/chat/history`,
  },
  computer: {
    /** 电脑控制授权状态 */
    status: `${API_BASE}/computer/status`,
    /** 开启/撤销授权 */
    authorize: `${API_BASE}/computer/authorize`,
    /** 执行一次工具调用（屏幕 / 键盘 / 指令） */
    call: `${API_BASE}/computer/call`,
  },
};

/** 一条记忆的后端原始记录（对应 lite/memory SQLite memories 表字段） */
export interface MemoryRow {
  id: number;
  type?: string;
  content?: string;
  tags?: string[] | string | null;
  importance?: number;
  importance_score?: number;
  created_at?: string;
  updated_at?: string;
  is_deleted?: number;
  agent_id?: string;
  [key: string]: unknown;
}

/** 通用 JSON 请求封装：非 2xx 视为失败并抛错（供上层 try/catch 降级）。 */
export async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) {
    throw new Error(`后端请求失败: ${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

/** 拉取记忆列表（可附带 type / agent_id / limit 过滤）。 */
export async function fetchMemories(params?: {
  type?: string;
  agent_id?: string;
  limit?: number;
}): Promise<MemoryRow[]> {
  const qs = new URLSearchParams();
  if (params?.type) qs.set('type', params.type);
  if (params?.agent_id) qs.set('agent_id', params.agent_id);
  if (params?.limit != null) qs.set('limit', String(params.limit));
  const query = qs.toString();
  const url = `${API_ENDPOINTS.memories.list}${query ? `?${query}` : ''}`;
  return requestJson<MemoryRow[]>(url);
}

/** 检索记忆：返回命中的记忆与拼接后的注入上下文。 */
export async function fetchSearch(
  q: string,
  opts?: { agent_id?: string; top_k?: number },
): Promise<{ memories: MemoryRow[]; context_text: string }> {
  const qs = new URLSearchParams({ q });
  if (opts?.agent_id) qs.set('agent_id', opts.agent_id);
  if (opts?.top_k != null) qs.set('top_k', String(opts.top_k));
  return requestJson(`${API_ENDPOINTS.memories.search}?${qs.toString()}`);
}

/** 一条 Agent 的后端原始记录（对应 lite/management/local_agents.py 的 Agent.to_dict()） */
export interface AgentRow {
  id: string;
  name: string;
  persona: string;
  voice: string;
  enabled: boolean;
  created_at?: string;
  updated_at?: string;
  [key: string]: unknown;
}

/** 拉取本地 Agent 列表（附带 enabled 过滤可选）。 */
export async function fetchAgents(params?: { enabled?: boolean }): Promise<AgentRow[]> {
  const qs = new URLSearchParams();
  if (params?.enabled != null) qs.set('enabled', String(params.enabled));
  const query = qs.toString();
  const url = `${API_ENDPOINTS.management.agents}${query ? `?${query}` : ''}`;
  return requestJson<AgentRow[]>(url);
}

/** 软删除单条记忆。 */
export async function deleteMemory(id: string | number): Promise<{ ok: boolean }> {
  return requestJson<{ ok: boolean }>(API_ENDPOINTS.memories.delete(id), { method: 'DELETE' });
}

/** 用户可读配置视图（对应 GET /api/settings，不含 API Key）。 */
export interface SettingsView {
  cloud: { provider: string; base_url?: string };
  tts: { voice: string };
  local_llm: { enabled: boolean };
  acp: { enabled: boolean };
  remote: { enabled: boolean };
}

/** 拉取配置视图（前端设置页首帧对齐后端默认值）。 */
export async function fetchSettings(): Promise<SettingsView> {
  return requestJson<SettingsView>(API_ENDPOINTS.settings.get);
}

/** 更新可热更配置（PUT /api/settings，白名单键：cloud.provider / tts.voice / local_llm.enabled）。 */
export async function updateSettings(patch: Record<string, unknown>): Promise<SettingsView> {
  const res = await fetch(API_ENDPOINTS.settings.update, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
  if (!res.ok) {
    throw new Error(`配置更新失败: ${res.status} ${res.statusText}`);
  }
  const data = (await res.json()) as { config?: SettingsView; error?: string };
  if (!data.config) {
    throw new Error(`配置更新失败: ${data.error ?? '未知错误'}`);
  }
  return data.config;
}

/** 电脑控制授权状态（对应当前 /api/computer/status 与 authorize 的返回）。 */
export interface ComputerStatus {
  authorized: boolean;
  confirm_dangerous: boolean;
}

/** 工具调用结果（ToolBridge 回填：含 result / authorized / tool / error_code）。 */
export interface ComputerCallResult {
  success: boolean;
  tool: string;
  result: unknown;
  authorized: boolean;
  error_code: string | null;
  error?: string | null;
  [key: string]: unknown;
}

/** 拉取电脑控制授权状态（GET /api/computer/status）。 */
export async function fetchComputerStatus(): Promise<ComputerStatus> {
  return requestJson<ComputerStatus>(API_ENDPOINTS.computer.status);
}

/** 开启 / 撤销电脑控制授权（POST /api/computer/authorize），返回最新状态。 */
export async function setComputerAuthorized(enabled: boolean): Promise<ComputerStatus> {
  const res = await fetch(API_ENDPOINTS.computer.authorize, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
  });
  if (!res.ok) {
    throw new Error(`授权请求失败: ${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<ComputerStatus>;
}

/** 发起一次电脑控制工具调用（POST /api/computer/call）。未授权时后端返回 403 抛错。 */
export async function callComputerTool(
  tool: string,
  arguments_: Record<string, unknown>,
): Promise<ComputerCallResult> {
  return requestJson<ComputerCallResult>(API_ENDPOINTS.computer.call, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tool, arguments: arguments_ }),
  });
}