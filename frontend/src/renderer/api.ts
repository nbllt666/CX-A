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
  /**
   * 桌宠 VRM 模型原始字节（GET，Content-Type: model/gltf-binary）。
   * 服务端默认模型为 <root>/data/pet/cx-open.vrm，用户可替换；前端只认接口，不关心路径。
   */
  petModel: `${API_BASE}/pet/model`,
  /**
   * 更换桌宠模型（POST {source_path} → {ok, message}）。
   * 后端校验 .vrm 后备份原模型为 .bak 再覆盖；失败 400 带中文 message。
   */
  petModelImport: `${API_BASE}/pet/model/import`,
  /** 恢复默认桌宠模型（POST → {ok}；无备份 404 {error:'pet_model_backup_missing'} 带中文 message） */
  petModelReset: `${API_BASE}/pet/model/reset`,
  chat: {
    /** 发起聊天（既有守卫端点，复数路径） */
    sendMessage: `${API_BASE}/chat/messages`,
    /** 表情聊天（Task H3）：走云端流式拼接 + 标签解析，返回 {clean_text, mood, raw} */
    message: `${API_BASE}/chat/message`,
    /** 聊天历史（20261004 持久化）：GET → {ok, messages:[{role, content, time}]} 最近 200 条 */
    history: `${API_BASE}/chat/history`,
  },
  voice: {
    /** 文本合成语音（POST {text, voice?} → {ok, audio_base64, mime:'audio/wav'}） */
    synthesize: `${API_BASE}/voice/synthesize`,
    /** 文本合成语音（流式，POST {text, voice?} → chunked NDJSON） */
    synthesizeStream: `${API_BASE}/voice/synthesize_stream`,
    /** 语音转文本（POST {audio_base64, sample_rate?} → {ok, text}） */
    transcribe: `${API_BASE}/voice/transcribe`,
  },
  voices: {
    /** 音色包列表（GET → {ok, voices:[{id, path, is_default, size, builtin}]}，首项恒为内置 cx-open） */
    list: `${API_BASE}/voices`,
    /** 导入音色包（POST {source_path, name?, overwrite?} → 成功 {ok, voice}；失败 400 {ok:false, error, message(中文)}） */
    import: `${API_BASE}/voices/import`,
  },
  memories: {
    /** 记忆列表（GET）；新建记忆也走该路径（POST，Task 7） */
    list: `${API_BASE}/memories`,
    /** 记忆检索 */
    search: `${API_BASE}/memories/search`,
    /** 整理记忆（既有蒸馏端点，注意单数 memory 路径；可能较慢） */
    distill: `${API_BASE}/memory/distill`,
    /** 批量删除（POST {ids:[]} → {deleted_count, skipped}；permanent 由后端跳过） */
    batchDelete: `${API_BASE}/memories/batch-delete`,
    /** 衰减统计（GET → {total, decaying, decayed_count, ...}，字段以后端为准） */
    decayStats: `${API_BASE}/memories/decay-stats`,
    /** 执行衰减整理（POST → {deleted_count}，低分记忆软删归档） */
    syncDecay: `${API_BASE}/memories/sync-decay`,
    /** 日记视图（GET ?date=YYYY-MM-DD → [{date, memories:[...]}]） */
    diary: `${API_BASE}/memories/diary`,
    /** 3D 加权检索（GET ?query=&w_importance=&w_time=&w_rel= → 记忆数组） */
    search3d: `${API_BASE}/memories/3d`,
  },
  settings: {
    /** 用户可读配置视图（GET，不含 API Key） */
    get: `${API_BASE}/settings`,
    /** 更新可热更配置（PUT，白名单键：cloud.provider / tts.voice / local_llm.enabled / vision.enabled） */
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
  setup: {
    /** 首启向导是否需要（门控）与当前下载线路取值（模型仓库由线路派生） */
    status: `${API_BASE}/setup/status`,
    /** 硬件体检结论与推荐（含候选档位） */
    recommend: `${API_BASE}/setup/recommend`,
    /** 提交向导选择并置「已完成初始化」 */
    complete: `${API_BASE}/setup/complete`,
    /** 启动本地小模型下载（后台执行，幂等） */
    modelDownload: `${API_BASE}/setup/model/download`,
    /** 查询下载进度（即时返回，不等待下载结束） */
    modelProgress: `${API_BASE}/setup/model/progress`,
    /** 取消进行中的下载（保留临时文件供续传） */
    modelCancel: `${API_BASE}/setup/model/cancel`,
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

/**
 * 桌宠模型请求超时（毫秒）：15MB 本地回环传输，给足余量避免弱机首次加载被误判超时
 * （比 requestJson 默认 300s 短，但远高于进度轮询的 10s）。
 */
const PET_MODEL_TIMEOUT_MS = 120_000;

/**
 * 拉取桌宠 VRM 模型原始字节（GET /api/pet/model，Content-Type: model/gltf-binary）。
 *
 * 为何不复用 requestJson：requestJson 末段固定做 `res.json()` 解析，而本端点返回的是
 * 二进制 GLB 字节流，JSON 解析必然失败；故这里另写一个只做 `arrayBuffer()` 的取体函数，
 * 但沿用 requestJson 的鉴权（X-Client-Token）、AbortController 超时与中文错误语义。
 *
 * 失败（404 / 无网络 / 后端未起 / 超时）统一抛中文错误，由 VrmAvatar 捕获后给出
 * 中文「暂时显示不了 3D 桌宠」提示（不回落卡通形象）。
 *
 * @param signal 可选外部取消信号：组件卸载时会 abort，联动内部超时控制器一起取消
 *   （15MB 下载不应在悬浮窗反复开关后继续占用连接）。
 */
export async function fetchPetModelBuffer(signal?: AbortSignal): Promise<ArrayBuffer> {
  // N1：持有启动令牌时自动附带 X-Client-Token 头（后端开启令牌校验后必需）
  const token = await ensureBackendToken();
  const headers = new Headers();
  if (token) {
    headers.set('X-Client-Token', token);
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), PET_MODEL_TIMEOUT_MS);
  // 外部取消（组件卸载）联动内部控制器：任一 abort 都终止本次取体
  const onExternalAbort = () => controller.abort();
  if (signal) {
    if (signal.aborted) {
      controller.abort();
    } else {
      signal.addEventListener('abort', onExternalAbort, { once: true });
    }
  }
  try {
    const res = await fetch(API_ENDPOINTS.petModel, { headers, signal: controller.signal });
    if (!res.ok) {
      throw new Error(`桌宠模型请求失败: ${res.status} ${res.statusText}`);
    }
    // 读体放在 try 内：让超时 / 外部取消同样覆盖 15MB 字节流读取阶段
    return await res.arrayBuffer();
  } catch (err) {
    if (signal?.aborted) {
      throw new Error('桌宠模型请求已取消');
    }
    if (controller.signal.aborted) {
      throw new Error(
        `桌宠模型请求超时（${Math.round(PET_MODEL_TIMEOUT_MS / 1000)} 秒）: ${API_ENDPOINTS.petModel}`,
      );
    }
    throw err;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', onExternalAbort);
  }
}

/* ==========================================================================
 * 桌宠模型管理（Task 8）：更换 / 恢复默认 / 跨窗口重载总线 / 文件选择桥封装。
 * 通道与 CHAT_TICK / petMood 总线同套路：localStorage + storage 事件。
 * ========================================================================== */

/**
 * 桌宠模型重载总线键（跨窗口）：导入/恢复默认成功后写 tick，桌宠渲染端
 * （PetPage / PetOverlay 的 VrmAvatar）据此作废旧缓存并重新拉取模型。
 */
export const PET_MODEL_RELOAD_KEY = 'cx-a.petModelReload';

/** 读当前模型代际 tick（无记录 / 非法 / 存储不可用返回 0）；消费端挂载时对齐用 */
export function readPetModelReloadTick(): number {
  try {
    const raw = window.localStorage.getItem(PET_MODEL_RELOAD_KEY);
    const n = raw === null ? NaN : Number(raw);
    return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
  } catch {
    return 0;
  }
}

/** 发布一次模型重载信号（导入/恢复成功后调用；其他窗口经 storage 事件到达） */
export function publishPetModelReload(): void {
  try {
    window.localStorage.setItem(PET_MODEL_RELOAD_KEY, String(Date.now()));
  } catch {
    /* 存储不可用（隐私模式等）时静默忽略 */
  }
}

/**
 * 订阅模型重载总线（跨窗口 storage 事件；同窗口写入不触发——发布方经
 * onReloaded 回调自行处理本窗口刷新）。返回去订阅函数。
 */
export function onPetModelReload(callback: () => void): () => void {
  const handler = (e: StorageEvent) => {
    if (e.key !== PET_MODEL_RELOAD_KEY || !e.newValue) return;
    callback();
  };
  window.addEventListener('storage', handler);
  return () => window.removeEventListener('storage', handler);
}

/**
 * 更换桌宠模型（POST /api/pet/model/import）。
 *
 * 不复用 requestJson 的通用抛错文案：失败响应体带后端中文 message
 * （如「文件不是有效的 VRM 模型」），调用方要把它直接展示给用户；
 * 故这里单独读体解析——失败抛 Error(message)，后端未给 message 时回落通用提示。
 *
 * @param sourcePath 待导入 .vrm 文件的绝对路径（pickVrmFile 所选）
 */
export async function importPetModel(sourcePath: string): Promise<{ ok?: boolean; message?: string }> {
  // N1：持有启动令牌时自动附带 X-Client-Token 头
  const token = await ensureBackendToken();
  const headers = new Headers({ 'Content-Type': 'application/json' });
  if (token) headers.set('X-Client-Token', token);
  let res: Response;
  try {
    res = await fetch(API_ENDPOINTS.petModelImport, {
      method: 'POST',
      headers,
      body: JSON.stringify({ source_path: sourcePath }),
    });
  } catch {
    throw new Error('更换桌宠模型失败：暂时连不上服务，请稍后再试');
  }
  if (!res.ok) {
    let message = '';
    try {
      const err = (await res.json()) as { message?: string };
      if (typeof err?.message === 'string') message = err.message;
    } catch {
      /* 响应体不可读（非 JSON 等）时跳过，走通用提示 */
    }
    throw new Error(message || '更换桌宠模型失败：这个文件可能不是有效的 VRM 模型');
  }
  return res.json() as Promise<{ ok?: boolean; message?: string }>;
}

/**
 * 恢复默认桌宠模型（POST /api/pet/model/reset）。
 * 无备份时后端 404（error: pet_model_backup_missing），抛中文错误由调用方展示。
 */
export async function resetPetModel(): Promise<{ ok?: boolean; message?: string }> {
  // N1：持有启动令牌时自动附带 X-Client-Token 头
  const token = await ensureBackendToken();
  const headers = new Headers({ 'Content-Type': 'application/json' });
  if (token) headers.set('X-Client-Token', token);
  let res: Response;
  try {
    res = await fetch(API_ENDPOINTS.petModelReset, { method: 'POST', headers });
  } catch {
    throw new Error('恢复默认模型失败：暂时连不上服务，请稍后再试');
  }
  if (!res.ok) {
    let message = '';
    try {
      const err = (await res.json()) as { message?: string };
      if (typeof err?.message === 'string') message = err.message;
    } catch {
      /* 响应体不可读时跳过，走通用提示 */
    }
    throw new Error(message || '恢复默认模型失败：没有找到可还原的备份');
  }
  return res.json() as Promise<{ ok?: boolean; message?: string }>;
}

/** Electron 桥上负责选 VRM 文件的最小形状（preload 契约，浏览器 dev 无此方法） */
type VrmFilePickerBridge = { pickVrmFile?: () => Promise<unknown> };

/**
 * 弹出系统文件选择框挑 VRM 模型（Electron preload 桥 window.cxaAPI.pickVrmFile）。
 * 非 Electron 环境（纯浏览器 dev / 单测）桥或方法不存在 → 返回 null；
 * 用户取消选择（桥返回 null/非字符串）→ 返回 null。调用方按「拿没拿到路径」分流。
 */
export async function pickVrmFile(): Promise<string | null> {
  const bridge = (typeof window !== 'undefined' ? window.cxaAPI : undefined) as
    | VrmFilePickerBridge
    | undefined;
  const picked = await bridge?.pickVrmFile?.();
  return typeof picked === 'string' && picked.length > 0 ? picked : null;
}

/**
 * 当前环境是否具备选 VRM 文件能力（Electron 桥上存在 pickVrmFile 方法）。
 * 非 Electron 环境（纯浏览器 dev / 单测）为 false，调用方据此给出「要在桌面应用里用」的引导提示。
 */
export function hasVrmFilePicker(): boolean {
  const bridge = (typeof window !== 'undefined' ? window.cxaAPI : undefined) as
    | VrmFilePickerBridge
    | undefined;
  return typeof bridge?.pickVrmFile === 'function';
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

/** 表情聊天请求体（Task H3） */
export interface ChatMessagePayload {
  message: string;
  /** 可选智能体 id：命中本地 Agent 时以其 persona 作为 system 人设 */
  agent_id?: string;
}

/** 表情聊天响应：clean_text 为后端解析后的干净文本（已识别情绪标签已剥离，
 *  未知/非法标签按 spec 口径原文保留）；mood 为表情档位（离线/无标签为 calm）；
 *  raw 为云端原始带标签文本。 */
export interface ChatMessageResult {
  ok: boolean;
  clean_text: string;
  mood: string;
  raw: string;
  /** 离线兜底标记：后端无 api_key / 云端不可达时为 true（clean_text 为固定友好文案） */
  offline?: boolean;
}

/**
 * 发送一条表情聊天消息到后端 POST /api/chat/message（Task H3）。
 * 后端走 CloudAdapter 流式拼接 + EmotionTagParser 解析；无 api_key / 云端
 * 不可达时不抛 5xx，返回 ok:true + 固定友好文案 + mood=calm（offline:true），
 * 前端把该文案作为AI气泡真实展示（后端真实回传，非本地伪造）。
 */
export async function sendChatMessage(payload: ChatMessagePayload): Promise<ChatMessageResult> {
  return requestJson<ChatMessageResult>(API_ENDPOINTS.chat.message, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

/** 一条对话历史记录（GET /api/chat/history 返回项；20261004 持久化契约）。 */
export interface ChatHistoryEntry {
  /** 'user' = 用户消息，'assistant' = AI回复 */
  role: 'user' | 'assistant' | string;
  content: string;
  /** 落盘时间戳（YYYY-MM-DD HH:MM:SS；旧记录缺失时为空串） */
  time?: string;
}

/**
 * 拉取最近对话历史（GET /api/chat/history）。
 *
 * 聊天页挂载加载 + 悬浮窗语音回复后经聊天刷新总线触发重载，两个窗口共享
 * 后端落盘的同一份对话真相。失败抛错由调用方降级（保持当前列表不动）。
 */
export async function fetchChatHistory(): Promise<ChatHistoryEntry[]> {
  // 历史读取是轻量本地文件，10s 短超时足够（失败快速暴露，不挂 UI）
  const data = await requestJson<{ ok?: boolean; messages?: ChatHistoryEntry[] }>(
    API_ENDPOINTS.chat.history,
    undefined,
    10_000,
  );
  return Array.isArray(data?.messages) ? data.messages : [];
}

/**
 * 聊天刷新总线键（跨窗口）：悬浮窗「说话」发送回复后写 tick，主窗口聊天页
 * 监听 storage 事件重载历史——桌宠语音对话与聊天页共享同一份真相
 * （通道与 petMood 总线同套路：localStorage + storage 事件，本窗口不自触发）。
 */
export const CHAT_TICK_KEY = 'cx-a.chatTick';

/** 发布一次聊天刷新信号（悬浮窗语音链路发送成功后调用；主窗口发送无需自刷）。 */
export function publishChatTick(): void {
  try {
    window.localStorage.setItem(CHAT_TICK_KEY, String(Date.now()));
  } catch {
    /* 存储不可用（隐私模式等）时静默忽略 */
  }
}

/** 语音合成响应（/api/voice/synthesize）。 */
export interface SpeechSynthesisResult {
  ok?: boolean;
  /** base64 编码的 wav 音频字节 */
  audio_base64?: string;
  mime?: string;
  error?: string;
  message?: string;
}

/**
 * 文本合成语音（AI回复朗读）。
 *
 * @param text 待合成文本（非空）
 * @param voice 可选音色标识；缺省由服务端使用默认音色（cx-open）
 */
export async function synthesizeSpeech(
  text: string,
  voice?: string,
): Promise<SpeechSynthesisResult> {
  return requestJson<SpeechSynthesisResult>(API_ENDPOINTS.voice.synthesize, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(voice ? { text, voice } : { text }),
  });
}

/** 流式合成单帧（/api/voice/synthesize_stream 的 NDJSON 行）。 */
export interface SpeechStreamChunk {
  seq?: number;
  text?: string;
  audio_base64?: string;
  error?: string;
  done?: boolean;
  total?: number;
}

/**
 * 流式文本合成语音（按标点切分 + chunked NDJSON）。
 *
 * 每收到一帧调用一次 ``onChunk``；收到 ``done`` 帧时调用 ``onDone``（可选）。
 * 支持通过 ``signal`` 中断（用于停止朗读时取消未完成的合成）——中断是预期控制流：
 * AbortError 在本层静默消化（函数正常返回、不调 onDone），不作为错误上抛；
 * 其余错误（HTTP 非 200 / 网络故障）照常抛出。
 *
 * @param text 待合成文本
 * @param onChunk 每帧回调（含音频 base64 或 error）
 * @param onDone 结束回调（传入 total 句数）
 * @param signal AbortSignal，用于中断
 * @param voice 可选音色标识
 */
export async function synthesizeSpeechStream(
  text: string,
  onChunk: (chunk: SpeechStreamChunk) => void,
  onDone?: (total: number) => void,
  signal?: AbortSignal,
  voice?: string,
): Promise<void> {
  const token = await ensureBackendToken();
  const headers = new Headers({ 'Content-Type': 'application/json' });
  if (token) headers.set('X-Client-Token', token);
  try {
    const res = await fetch(API_ENDPOINTS.voice.synthesizeStream, {
      method: 'POST',
      headers,
      body: JSON.stringify(voice ? { text, voice } : { text }),
      signal,
    });
    if (!res.ok || !res.body) {
      const msg = await res.text().catch(() => '');
      throw new Error(`流式合成失败（${res.status}）：${msg.slice(0, 200)}`);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let nl: number;
        while ((nl = buffer.indexOf('\n')) !== -1) {
          const line = buffer.slice(0, nl).trim();
          buffer = buffer.slice(nl + 1);
          if (!line) continue;
          let obj: SpeechStreamChunk;
          try {
            obj = JSON.parse(line) as SpeechStreamChunk;
          } catch {
            continue;
          }
          if (obj.done) {
            onDone?.(obj.total ?? 0);
            return;
          }
          onChunk(obj);
        }
      }
    } finally {
      try {
        reader.releaseLock();
      } catch {
        /* ignore */
      }
    }
  } catch (e) {
    // 中断（signal 触发）是预期控制流：静默结束，不作为错误上抛；其余异常原样上抛
    if ((e as DOMException | undefined)?.name === 'AbortError' || signal?.aborted) return;
    throw e;
  }
}

/** 语音识别响应（/api/voice/transcribe）。 */
export interface SpeechTranscriptionResult {
  ok?: boolean;
  text?: string;
  error?: string;
  message?: string;
}

/**
 * 语音转文本（麦克风输入）。
 *
 * @param audioBase64 裸 int16 PCM 的 base64（见 audioRecorder 采集口径）
 * @param sampleRate 输入采样率（Hz）；服务端据此重采样到识别器期望的 16kHz
 */
export async function transcribeAudio(
  audioBase64: string,
  sampleRate: number,
): Promise<SpeechTranscriptionResult> {
  return requestJson<SpeechTranscriptionResult>(API_ENDPOINTS.voice.transcribe, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ audio_base64: audioBase64, sample_rate: sampleRate }),
  });
}

/* ==========================================================================
 * 音色包管理：列表 / 导入（GET/POST /api/voices**）
 * ========================================================================== */

/** 一个音色包（对应 GET /api/voices 返回项；首项恒为内置 cx-open，builtin:true） */
export interface VoiceInfo {
  /** 音色标识（PUT settings tts.voice 的取值） */
  id: string;
  /** 音色包在磁盘上的路径 */
  path: string;
  /** 是否为默认音色 */
  is_default: boolean;
  /** 音色包体积（字节；用于下拉里的人性化大小展示） */
  size: number;
  /** 是否内置音色包（true = 随应用自带，false = data/voices/ 下的自定义包） */
  builtin: boolean;
}

/**
 * 拉取音色包列表（GET /api/voices）。
 * 非 2xx / 网络失败时抛错，由调用方降级（设置页回退演示选项，不弹错）。
 */
export async function fetchVoices(): Promise<VoiceInfo[]> {
  const data = await requestJson<{ ok?: boolean; voices?: VoiceInfo[] }>(API_ENDPOINTS.voices.list);
  return Array.isArray(data?.voices) ? data.voices : [];
}

/**
 * 导入音色包（POST /api/voices/import）。
 *
 * 不复用 requestJson 的通用抛错文案：后端失败响应体里带中文 message
 * （如「文件夹不是有效的音色包」），设置页要把它直接展示给用户；
 * 故这里单独读体解析——失败抛 Error(message)，后端未给 message 时
 * 回落通用中文提示；网络不可达同样抛中文错误。
 *
 * @param sourcePath 待导入音色包文件夹的绝对路径（Electron pickVoiceFolder 所选）
 * @param name 可选自定义音色名（缺省由后端按文件夹名派生）
 * @param overwrite 可选：同名音色已存在时是否覆盖
 */
export async function importVoice(
  sourcePath: string,
  name?: string,
  overwrite?: boolean,
): Promise<VoiceInfo> {
  // N1：持有启动令牌时自动附带 X-Client-Token 头
  const token = await ensureBackendToken();
  const headers = new Headers({ 'Content-Type': 'application/json' });
  if (token) headers.set('X-Client-Token', token);
  const payload: Record<string, unknown> = { source_path: sourcePath };
  if (name !== undefined) payload.name = name;
  if (overwrite !== undefined) payload.overwrite = overwrite;
  let res: Response;
  try {
    res = await fetch(API_ENDPOINTS.voices.import, {
      method: 'POST',
      headers,
      body: JSON.stringify(payload),
    });
  } catch {
    throw new Error('导入音色失败：暂时连不上服务，请稍后再试');
  }
  if (!res.ok) {
    let message = '';
    try {
      const err = (await res.json()) as { message?: string };
      if (typeof err?.message === 'string') message = err.message;
    } catch {
      /* 响应体不可读（非 JSON 等）时跳过，走通用提示 */
    }
    throw new Error(message || '导入音色失败：这个文件夹可能不是有效的音色包');
  }
  const data = (await res.json()) as { ok?: boolean; voice?: VoiceInfo };
  if (!data?.voice) {
    throw new Error('导入音色失败：服务没有返回新音色，请稍后再试');
  }
  return data.voice;
}

/** Electron 桥上负责选音色文件夹的最小形状（preload 契约已冻结，浏览器 dev 无此方法） */
type VoiceFolderPickerBridge = { pickVoiceFolder?: () => Promise<unknown> };

/**
 * 弹出系统文件夹选择框挑音色包（Electron preload 桥 window.cxaAPI.pickVoiceFolder）。
 *
 * 非 Electron 环境（纯浏览器 dev / 单测）桥或方法不存在 → 返回 null；
 * 用户取消选择（桥返回 null/非字符串）→ 返回 null。调用方按「拿没拿到路径」分流。
 */
export async function pickVoiceFolder(): Promise<string | null> {
  const bridge = (typeof window !== 'undefined' ? window.cxaAPI : undefined) as
    | VoiceFolderPickerBridge
    | undefined;
  const picked = await bridge?.pickVoiceFolder?.();
  return typeof picked === 'string' && picked.length > 0 ? picked : null;
}

/**
 * 当前环境是否具备选音色文件夹能力（Electron 桥上存在 pickVoiceFolder 方法）。
 * 非 Electron 环境（纯浏览器 dev / 单测）为 false，调用方据此给出「要在桌面应用里用」的引导提示。
 */
export function hasVoiceFolderPicker(): boolean {
  const bridge = (typeof window !== 'undefined' ? window.cxaAPI : undefined) as
    | VoiceFolderPickerBridge
    | undefined;
  return typeof bridge?.pickVoiceFolder === 'function';
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

/* ────────────────────────────────────────────────────────────────────────────
 * 记忆 CRUD / 批量 / 衰减 / 日记 / 3D 检索 / 蒸馏（Task 7：记忆页改造）。
 * 仅追加封装，端点契约以后端 api_server.py 为准；解析一律宽松，失败由调用方降级。
 * ──────────────────────────────────────────────────────────────────────────── */

/** 记忆类型四值（对齐 CX-O memory_type 值域；permanent 豁免衰减、批量删除被跳过） */
export type MemoryTypeValue = 'long_term' | 'short_term' | 'permanent' | 'diary';

/** 新建/编辑记忆请求体（后端经 jsonschema 校验后入库） */
export interface MemoryUpsertPayload {
  content: string;
  memory_type: MemoryTypeValue;
  /** 重要性 1-5 */
  importance: number;
  tags?: string[];
  agent_id?: string;
}

/** 新建记忆：POST /api/memories，返回落库后的记录。 */
export async function createMemory(payload: MemoryUpsertPayload): Promise<MemoryRow> {
  return requestJson<MemoryRow>(API_ENDPOINTS.memories.list, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

/** 编辑记忆：PUT /api/memories/{id}（同字段部分更新）；非法 id 后端 404。 */
export async function updateMemory(
  id: string | number,
  payload: Partial<MemoryUpsertPayload>,
): Promise<MemoryRow> {
  return requestJson<MemoryRow>(`${API_BASE}/memories/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

/** 删除单条记忆：DELETE /api/memories/{id}（后端软删）。 */
export async function deleteMemory(id: string | number): Promise<unknown> {
  return requestJson<unknown>(`${API_BASE}/memories/${id}`, { method: 'DELETE' });
}

/** 批量删除结果：deleted_count = 成功软删数；skipped = 被跳过的 id（permanent 保护等）。 */
export interface BatchDeleteResult {
  deleted_count?: number;
  skipped?: Array<string | number>;
}

/** 批量删除记忆：POST /api/memories/batch-delete（permanent 由后端跳过，前端也禁选）。 */
export async function batchDeleteMemories(
  ids: Array<string | number>,
): Promise<BatchDeleteResult> {
  return requestJson<BatchDeleteResult>(API_ENDPOINTS.memories.batchDelete, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids }),
  });
}

/** 衰减统计（字段以后端为准，前端宽松解析：total / decaying / decayed_count 等）。 */
export type DecayStats = Record<string, unknown>;

/** 拉取衰减统计：GET /api/memories/decay-stats。 */
export async function fetchDecayStats(): Promise<DecayStats> {
  return requestJson<DecayStats>(API_ENDPOINTS.memories.decayStats);
}

/** 执行衰减整理：POST /api/memories/sync-decay → {deleted_count}（低分记忆软删归档）。 */
export async function syncDecay(): Promise<{ deleted_count?: number }> {
  return requestJson<{ deleted_count?: number }>(API_ENDPOINTS.memories.syncDecay, {
    method: 'POST',
  });
}

/** 日记分组条目：{date, memories}（memories 为后端原始记录数组）。 */
export interface DiaryGroup {
  date: string;
  memories: MemoryRow[];
}

/**
 * 拉取日记视图：GET /api/memories/diary?date=YYYY-MM-DD。
 * 响应宽松解析：顶层数组或 {days:[...]} 均可；非对象项被过滤。
 */
export async function fetchDiary(date?: string): Promise<DiaryGroup[]> {
  const qs = new URLSearchParams();
  if (date) qs.set('date', date);
  const query = qs.toString();
  const url = `${API_ENDPOINTS.memories.diary}${query ? `?${query}` : ''}`;
  const data = await requestJson<unknown>(url);
  const groups: unknown[] = Array.isArray(data)
    ? data
    : ((data as { days?: unknown[] } | null)?.days ?? []);
  return groups.filter(
    (g): g is DiaryGroup => !!g && typeof g === 'object' && Array.isArray((g as DiaryGroup).memories),
  );
}

/**
 * 3D 加权检索：GET /api/memories/3d?query=&w_importance=&w_time=&w_rel=。
 * 权重 0-1（重要性/时间/相关性三维）；响应宽松解析：数组或 {memories:[...]} 均可。
 */
export async function search3d(
  query: string,
  weights: { w_importance: number; w_time: number; w_rel: number },
  opts?: { agent_id?: string; top_k?: number },
): Promise<MemoryRow[]> {
  const qs = new URLSearchParams({ query });
  qs.set('w_importance', String(weights.w_importance));
  qs.set('w_time', String(weights.w_time));
  qs.set('w_rel', String(weights.w_rel));
  if (opts?.agent_id) qs.set('agent_id', opts.agent_id);
  if (opts?.top_k != null) qs.set('top_k', String(opts.top_k));
  const data = await requestJson<unknown>(`${API_ENDPOINTS.memories.search3d}?${qs.toString()}`);
  if (Array.isArray(data)) return data as MemoryRow[];
  const inner = (data as { memories?: unknown } | null)?.memories;
  return Array.isArray(inner) ? (inner as MemoryRow[]) : [];
}

/**
 * 整理记忆：POST /api/memory/distill（既有蒸馏端点，云端蒸馏可能较慢）。
 * 回执字段未冻结，前端只判成功 / 失败，返回宽松对象。
 */
export async function distillMemory(): Promise<Record<string, unknown>> {
  return requestJson<Record<string, unknown>>(API_ENDPOINTS.memories.distill, {
    method: 'POST',
  });
}

/** 运行偏好：performance = 性能优先，eco = 省电优先（值域由后端冻结）。 */
export type AccelMode = 'performance' | 'eco';
/** TTS 加速后端值域（与后端 config 一致） */
export type TtsAccel = 'auto' | 'cpu' | 'cuda' | 'dml' | 'rocm' | 'off';
/** TTS 加速设备提示值域（DirectML 设备选择；''=自动） */
export type TtsAccelDevice = '' | 'igpu' | 'dgpu';

/**
 * 加速剖面（后端唯一真相源 accel_plan 产出）：
 * 模式默认 + 各组件落点 + 中文理由（reasons 含术语，仅作诊断，前端不直接展示）。
 */
export interface AccelProfile {
  mode: AccelMode;
  tts: { accel: TtsAccel; accel_device: TtsAccelDevice };
  asr: { device: string };
  local_llm: { device: string };
  embedding: { device: string };
  reasons: string[];
}

/** 语音桥重建结果（N-6）：needs_restart=true 时明确提示需重启应用（不静默）。 */
export interface VoiceBackendResult {
  rebuilt: boolean;
  needs_restart: boolean;
  message: string;
}

/** 用户可读配置视图（对应 GET /api/settings；api_key 已由后端脱敏，形如 sk-****尾4位）。 */
export interface SettingsView {
  /** api_key 为脱敏回显值（sk-****尾4位）；未配置时缺失 */
  cloud: { provider: string; base_url?: string; api_key?: string };
  tts: { voice: string; accel?: TtsAccel; accel_device?: TtsAccelDevice };
  /** 运行偏好（性能/节能双模式） */
  accel: { mode: AccelMode };
  /** 下载线路（mirror=国内魔塔 / official=海外 HuggingFace）；旧后端缺失时前端回落 mirror */
  download?: { channel: DownloadChannel };
  /**
   * ready = 本地小模型是否已下载就绪（设置页据此展示「本地大脑已就绪」徽标）；
   * model_path = 当前模型文件路径（设置页档位卡展示用；后端未给时前端隐藏该行）；
   * gpu_preference = LLM 显卡偏好（"" 自动 / igpu 核显 / dgpu 独显；20261006，旧后端缺失时前端按自动处理）
   */
  local_llm: { enabled: boolean; ready?: boolean; model_path?: string; gpu_preference?: string };
  acp: { enabled: boolean };
  remote: { enabled: boolean };
  /** 主动视觉开关（视图新增字段；旧后端缺失时前端按关闭处理） */
  vision?: { enabled: boolean };
  /** 聊天记忆注入开关（RAG 闭环，20261005；旧后端缺失时前端按开启处理） */
  memory?: { context_inject: boolean };
  /**
   * 语音交互模式（20261006 全双工降级版）：vad=传统自动断句 / duplex=全双工
   * （按标点切句逐句轮询 LLM）；旧后端缺失时前端按 vad 处理
   */
  voice?: { interaction_mode: 'vad' | 'duplex' };
}

/** PUT /api/settings 响应（配置视图 + 可选语音桥重建结果）。 */
export interface SettingsUpdateResult {
  config: SettingsView;
  voice_backend?: VoiceBackendResult;
}

/** 拉取配置视图（前端设置页首帧对齐后端默认值）。 */
export async function fetchSettings(): Promise<SettingsView> {
  return requestJson<SettingsView>(API_ENDPOINTS.settings.get);
}

/**
 * 更新可热更配置（PUT /api/settings）。
 *
 * 白名单键：``cloud.provider`` / ``cloud.api_key``（后端加密存储，GET 脱敏回显）/
 * ``download.channel``（后端据此派生 local_llm.source）/ ``tts.voice`` /
 * ``tts.accel`` / ``tts.accel_device`` / ``local_llm.enabled`` /
 * ``accel.mode``（运行偏好；保存后后端按新配置重建语音桥，结果见 voice_backend）。
 */
export async function updateSettings(patch: Record<string, unknown>): Promise<SettingsUpdateResult> {
  // N1：统一走 requestJson（自动附带 X-Client-Token），保留 config 缺失的显式抛错语义
  const data = await requestJson<{
    config?: SettingsView;
    voice_backend?: VoiceBackendResult;
    error?: string;
  }>(API_ENDPOINTS.settings.update, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
  if (!data.config) {
    throw new Error(`配置更新失败: ${data.error ?? '未知错误'}`);
  }
  return { config: data.config, voice_backend: data.voice_backend };
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

/** 角色人设条目（本地多 Agent 体系；data/agents.json，默认角色 id="default" 软软）。 */
export interface AgentInfo {
  id: string;
  name: string;
  persona: string;
  voice?: string | null;
  enabled: boolean;
}

/** 拉取角色（Agent）列表（GET /api/agents）。 */
export async function listAgents(): Promise<AgentInfo[]> {
  return requestJson<AgentInfo[]>(`${API_BASE}/api/agents`);
}

/**
 * 更新角色人设等字段（PUT /api/agents/{id}；后端白名单 name/persona/voice/enabled）。
 * 修改默认角色（id="default"）的 persona 即调整桌宠对话的 system 人设。
 */
export async function updateAgent(
  id: string,
  patch: { persona?: string; name?: string; voice?: string | null; enabled?: boolean },
): Promise<AgentInfo> {
  return requestJson<AgentInfo>(`${API_BASE}/api/agents/${encodeURIComponent(id)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
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

/* ==========================================================================
 * 首启向导（Task 8）：接口契约见 spec「首启向导后端接口」（路径 / 字段名已冻结）
 * ========================================================================== */

/** 下载线路：mirror = 国内（魔塔），official = 海外（HuggingFace） */
export type DownloadChannel = 'mirror' | 'official';
/** 本地模型仓库：modelscope = 魔塔（国内），huggingface = HuggingFace（海外）；由线路派生 */
export type LocalModelSource = 'modelscope' | 'huggingface';

/** 首启状态（GET /api/setup/status）：门控 + 当前选择取值 */
export interface SetupStatusView {
  completed: boolean;
  /** 是否需要在首帧展示向导 */
  wizard_required: boolean;
  /** 用户选择来源：前端只以它为准（唯一真相） */
  download: { channel: DownloadChannel };
  /**
   * 模型仓库（与 download.channel 语义一致：mirror↔modelscope、official↔huggingface）。
   * 仅作状态回显，前端不作为选择来源，也不据此覆盖 channel。
   */
  local_llm_source: LocalModelSource;
}

/** 硬件画像：探测能力缺失时对应字段为空（探测失败只降级，不抛错） */
export interface HardwareProfile {
  cpu_cores: number | null;
  ram_gb: number | null;
  gpu_vendor: string | null;
  vram_gb: number | null;
  cuda_version: string | null;
  disk_free_gb: number | null;
  probe_notes: string[];
  /** 是否含核显（GPU 清单枚举结论；探测失败/旧后端缺失 → undefined，前端保守显示全部选项） */
  has_igpu?: boolean;
  /** 首个已知独显厂商（nvidia/amd/intel；无独显 → null，字段缺失 → undefined） */
  dgpu_vendor?: string | null;
  /** GPU 清单（vendor/name/type/vram_hint）；探测失败降级时缺失 */
  gpus?: Array<{ vendor: string; name: string; type: string; vram_hint: string }>;
}

/** 一个候选模型档位（含体积与适用条件，用于「我自己挑」） */
export interface ModelTierInfo {
  tier: string;
  repo: string;
  filename: string;
  approximate_size_gb: number;
  ram_requirement_gb: number;
  vram_requirement_gb: number;
  description: string;
}

/** 推荐结论：走云端还是本地、设备与档位、中文理由 */
export interface HardwareRecommendation {
  use_local: boolean;
  device: 'cpu' | 'gpu';
  tier: string;
  config_patch: Record<string, unknown>;
  model: {
    tier: string;
    repo: string;
    filename: string;
    approximate_size_gb: number;
  } | null;
  reasons: string[];
  probe_notes: string[];
  /** 加速剖面（性能/节能双模式；模式默认按画像推导） */
  accel?: AccelProfile;
}

/** 硬件体检响应（GET /api/setup/recommend） */
export interface SetupRecommendResult {
  profile: HardwareProfile;
  recommendation: HardwareRecommendation;
  /** 加速剖面（与 recommendation.accel 同源，顶层直取便于向导渲染） */
  accel?: AccelProfile;
  tiers: ModelTierInfo[];
  /** 当前线路对应的模型仓库（恒等于线路派生值，前端无需自行推导） */
  suggested_source: string;
}

/** 下载进度（GET /api/setup/model/progress） */
export interface ModelProgress {
  state: 'idle' | 'downloading' | 'done' | 'failed' | 'canceled';
  downloaded: number;
  total: number;
  percent: number;
  file: string;
  error: string | null;
  model: Record<string, unknown> | null;
}

/** 启动下载的响应（POST /api/setup/model/download，幂等） */
export interface ModelDownloadResult {
  ok: boolean;
  state: string;
  /** 已有下载在跑时为 true（不会启动第二个下载） */
  already_running: boolean;
  model: Record<string, unknown> | null;
}

/** 向导提交请求体（POST /api/setup/complete） */
export interface SetupCompletePayload {
  /** 云端段可选：跳过云端（快车道采纳 / 「跳过，先用本地」）时整体省略 */
  cloud?: { provider: string; api_key?: string };
  /** 用户唯一选择：线路 */
  download: { channel: DownloadChannel };
  /** 模型仓库由下载线路在服务端派生，前端不再提交 source */
  local_llm: { enabled: boolean };
  /** 运行偏好（省电优先 / 性能优先）：服务端据此展开各组件落点 */
  accel?: { mode: AccelMode };
  /** 可选显式覆盖语音加速后端 / 设备提示（缺省由运行偏好推导） */
  tts?: { accel?: TtsAccel; accel_device?: TtsAccelDevice };
  /** 是否采纳硬件体检给出的推荐 */
  apply_recommended: boolean;
}

/** 向导提交响应：回显已应用 / 被忽略的键（非法取值不静默丢弃） */
export interface SetupCompleteResult {
  ok: boolean;
  applied: string[];
  ignored: string[];
  setup: { completed: boolean; completed_at: string };
  config: Record<string, unknown>;
}

/**
 * 启动下载的可选入参（缺省时后端取当前线路对应的仓库 / 推荐档位）。
 * 不再传 source：模型仓库由下载线路在服务端派生。
 */
export interface ModelDownloadPayload {
  tier?: string;
}

/** 查询首启状态（GET /api/setup/status）：App 首帧门控的唯一依据。 */
export async function fetchSetupStatus(): Promise<SetupStatusView> {
  return requestJson<SetupStatusView>(API_ENDPOINTS.setup.status);
}

/** 拉取硬件体检结论与推荐（GET /api/setup/recommend）。 */
export async function fetchSetupRecommend(): Promise<SetupRecommendResult> {
  return requestJson<SetupRecommendResult>(API_ENDPOINTS.setup.recommend);
}

/** 提交向导选择（POST /api/setup/complete），成功后后端置「已完成初始化」。 */
export async function completeSetup(payload: SetupCompletePayload): Promise<SetupCompleteResult> {
  return requestJson<SetupCompleteResult>(API_ENDPOINTS.setup.complete, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

/** 启动本地小模型下载（POST /api/setup/model/download），进行中重复调用幂等。 */
export async function startModelDownload(
  payload?: ModelDownloadPayload,
): Promise<ModelDownloadResult> {
  return requestJson<ModelDownloadResult>(API_ENDPOINTS.setup.modelDownload, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload ?? {}),
  });
}

/**
 * 进度查询的专用超时（毫秒）。
 *
 * 进度轮询必须「立刻拿到当前快照」，而 requestJson 默认超时 300s（对齐后端
 * api_timeout 契约，服务长任务）——用于轮询会让失败的后端把每 1s 一次的轮询
 * 挂成长达 5 分钟的悬空请求，堆积并拖慢界面。故这里显式传 10s 短超时。
 */
const PROGRESS_TIMEOUT_MS = 10_000;

/** 查询下载进度（GET /api/setup/model/progress）：短超时，失败由调用方保留上次进度。 */
export async function fetchModelProgress(): Promise<ModelProgress> {
  return requestJson<ModelProgress>(
    API_ENDPOINTS.setup.modelProgress,
    undefined,
    PROGRESS_TIMEOUT_MS,
  );
}

/** 取消进行中的下载（POST /api/setup/model/cancel），保留临时文件供续传。 */
export async function cancelModelDownload(): Promise<{ ok: boolean; state: string }> {
  return requestJson<{ ok: boolean; state: string }>(API_ENDPOINTS.setup.modelCancel, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  });
}

// ---------------------------------------------------------------- 管理面：CX-A 管理 CX-O（spec add-fleet-frontend-hidden）
//
// FleetPage 消费的 fleet 封装。与 requestJson 的差异：非 2xx 抛 FleetApiError
// {status, body}——治理界面需要把后端/远端实例的原始状态码与 ADMIN_* 错误码
// **原样呈现**给使用者（SB-2 裁决口径），而非拼接成消息字符串丢失结构。

/** fleet 实例（后端台账脱敏视图：绝不携带 token 明文，仅尾 4 位标识） */
export interface FleetInstance {
  id: string;
  name: string;
  base_url: string;
  role: string | null;
  source: 'manual' | 'registered' | string;
  registered_at: string | null;
  last_seen: string | null;
  has_token: boolean;
  token_suffix: string | null;
}

/** fleet 错误响应体（后端 400/404 与透传的 ADMIN_* 错误码均落在 error/message 字段） */
export interface FleetErrorBody {
  ok?: boolean;
  error?: string;
  message?: string;
  [key: string]: unknown;
}

/** fleet 请求错误：保留 HTTP 状态码与响应体（原样呈现用） */
export class FleetApiError extends Error {
  readonly status: number;
  readonly body: FleetErrorBody | null;

  constructor(status: number, body: FleetErrorBody | null, message: string) {
    super(message);
    this.name = 'FleetApiError';
    this.status = status;
    this.body = body;
  }
}

/** fleet 请求专用超时：管理操作短平快，30s 足够（远端实例超时由后端 504 兜底） */
const FLEET_TIMEOUT_MS = 30_000;

async function fleetRequest<T>(method: string, path: string, body?: unknown): Promise<T> {
  // N1：与其他后端调用一致，自动附带 X-Client-Token（register 之外全部过闸）
  const token = await ensureBackendToken();
  const headers = new Headers({ 'Content-Type': 'application/json' });
  if (token) {
    headers.set('X-Client-Token', token);
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FLEET_TIMEOUT_MS);
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/fleet${path}`, {
      method,
      headers,
      signal: controller.signal,
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    });
  } catch (err) {
    if (controller.signal.aborted) {
      throw new Error(`管理面请求超时（${FLEET_TIMEOUT_MS / 1000} 秒）`);
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
  if (!res.ok) {
    let parsed: FleetErrorBody | null = null;
    try {
      parsed = (await res.json()) as FleetErrorBody;
    } catch {
      parsed = null; // 响应体非 JSON 时保留 null，状态码仍在
    }
    throw new FleetApiError(res.status, parsed, `管理面请求失败: ${res.status}`);
  }
  return res.json() as Promise<T>;
}

/** 实例台账列表（GET /api/fleet/instances；脱敏视图，被动 last_seen 新鲜度） */
export async function listFleetInstances(): Promise<{ status: string; instances: FleetInstance[] }> {
  return fleetRequest('GET', '/instances');
}

/** 手动登记实例（POST /api/fleet/instances）；同地址无令牌的已注册实例由后端按补录处理 */
export async function addFleetInstance(payload: {
  name: string;
  base_url: string;
  token: string;
}): Promise<{ status: string; instance: FleetInstance }> {
  return fleetRequest('POST', '/instances', payload);
}

/** 注销实例（DELETE /api/fleet/instances/{id}；语义为移除记录，非阻止注册） */
export async function deleteFleetInstance(id: string): Promise<{ status: string }> {
  return fleetRequest('DELETE', `/instances/${encodeURIComponent(id)}`);
}

/** 查看实例能力清单（GET /api/fleet/instances/{id}/manifest，readonly 透传） */
export async function fetchFleetManifest(id: string): Promise<Record<string, unknown>> {
  return fleetRequest('GET', `/instances/${encodeURIComponent(id)}/manifest`);
}

/** 实例健康探测（GET /api/fleet/instances/{id}/health，转发不带 Bearer） */
export async function fetchFleetHealth(id: string): Promise<Record<string, unknown>> {
  return fleetRequest('GET', `/instances/${encodeURIComponent(id)}/health`);
}

/** 下发单条控制指令（POST /api/fleet/instances/{id}/control；缺 request_id 由后端自动补） */
export async function sendFleetControl(
  id: string,
  payload: { target: string; action: string; request_id?: string; params?: Record<string, unknown> },
): Promise<Record<string, unknown>> {
  return fleetRequest('POST', `/instances/${encodeURIComponent(id)}/control`, payload);
}