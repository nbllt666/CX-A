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
  },
  memories: {
    /** 记忆列表 */
    list: `${API_BASE}/memories`,
    /** 记忆检索 */
    search: `${API_BASE}/memories/search`,
  },
  settings: {
    /** 用户可读配置视图（GET，不含 API Key） */
    get: `${API_BASE}/settings`,
    /** 更新可热更配置（PUT，白名单键：cloud.provider / tts.voice / local_llm.enabled） */
    update: `${API_BASE}/settings`,
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

/**
 * 后端启动令牌缓存（N1 鉴权）：Electron 环境经 preload IPC 惰性获取并缓存；
 * 非 Electron 环境（纯浏览器 dev / 测试）为 null，请求不附带令牌头。
 */
let backendToken: string | null | undefined;

/** 惰性获取并缓存后端启动令牌；失败或非 Electron 环境返回 null。 */
async function ensureBackendToken(): Promise<string | null> {
  if (backendToken === undefined) {
    try {
      backendToken = (await window.cxaAPI?.getBackendToken?.()) ?? null;
    } catch {
      backendToken = null;
    }
  }
  return backendToken;
}

/** 请求默认超时（毫秒）：对后端 api_timeout=300s 契约；超时 abort 防 UI 永久锁死（F-5）。 */
const DEFAULT_TIMEOUT_MS = 300_000;

/**
 * 通用 JSON 请求封装：非 2xx 视为失败并抛错（供上层 try/catch 降级）。
 *
 * F-5（第三轮体检批次6）：内置 AbortController 超时——后端为单线程服务，
 * 被长任务阻塞时无超时的 fetch 会永久挂起、UI 锁死；超时后 abort 并抛出
 * 明确的中文超时错误，调用方 catch 后正常降级。
 *
 * @param url 请求地址
 * @param init 可选 fetch 初始化（method/headers/body 等）
 * @param timeoutMs 超时毫秒数，默认 300_000（300s）
 */
export async function requestJson<T>(
  url: string,
  init?: RequestInit,
  timeoutMs: number = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  // N1：持有启动令牌时自动附带 X-Client-Token 头（后端开启令牌校验后必需）
  const token = await ensureBackendToken();
  const headers = new Headers(init?.headers);
  if (token) {
    headers.set('X-Client-Token', token);
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let res: Response;
  try {
    res = await fetch(url, { ...init, headers, signal: controller.signal });
  } catch (err) {
    if (controller.signal.aborted) {
      throw new Error(`后端请求超时（${Math.round(timeoutMs / 1000)} 秒）: ${url}`);
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
  if (!res.ok) {
    // D5 修复：非 2xx 时读取响应体片段（截断 200 字符）拼入错误信息，
    // 让后端错误码 / 错误说明对调用方可见；响应体不可读时静默跳过。
    let detail = '';
    try {
      const text = await res.text();
      if (text) detail = ` ${text.slice(0, 200)}`;
    } catch {
      /* 响应体不可读（流已消费等）时跳过 */
    }
    throw new Error(`后端请求失败: ${res.status} ${res.statusText}${detail}`);
  }
  return res.json() as Promise<T>;
}

/** 聊天发送请求体 */
export interface ChatSendPayload {
  content: string;
}

/**
 * 发送一条聊天消息到后端 POST /api/chat/messages。
 * 当前该端点为「未启用守卫」占位实现或可能不可达：
 * - 网络失败 / 非 2xx → 抛错，由调用方 catch 后降级为「未送达」提示；
 * - 守卫端点返回占位 JSON → 上层校验响应形状决定展示，绝不伪造回复文本。
 */
export async function sendMessage(payload: ChatSendPayload): Promise<unknown> {
  return requestJson<unknown>(API_ENDPOINTS.chat.sendMessage, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
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
  // N1：统一走 requestJson（自动附带 X-Client-Token），保留 config 缺失的显式抛错语义
  const data = await requestJson<{ config?: SettingsView; error?: string }>(
    API_ENDPOINTS.settings.update,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    },
  );
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
  // N1：统一走 requestJson（自动附带 X-Client-Token）
  return requestJson<ComputerStatus>(API_ENDPOINTS.computer.authorize, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
  });
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