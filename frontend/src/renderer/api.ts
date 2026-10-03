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
  chat: {
    /** 发起聊天（既有守卫端点，复数路径） */
    sendMessage: `${API_BASE}/chat/messages`,
    /** 表情聊天（Task H3）：走云端流式拼接 + 标签解析，返回 {clean_text, mood, raw} */
    message: `${API_BASE}/chat/message`,
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
    /** 记忆列表 */
    list: `${API_BASE}/memories`,
    /** 记忆检索 */
    search: `${API_BASE}/memories/search`,
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
 * 前端把该文案作为伴侣气泡真实展示（后端真实回传，非本地伪造）。
 */
export async function sendChatMessage(payload: ChatMessagePayload): Promise<ChatMessageResult> {
  return requestJson<ChatMessageResult>(API_ENDPOINTS.chat.message, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
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
 * 文本合成语音（伴侣回复朗读）。
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
 * 支持通过 ``signal`` 中断（用于停止朗读时取消未完成的合成）。
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

/** 用户可读配置视图（对应 GET /api/settings，不含 API Key）。 */
export interface SettingsView {
  cloud: { provider: string; base_url?: string };
  tts: { voice: string; accel?: TtsAccel; accel_device?: TtsAccelDevice };
  /** 运行偏好（性能/节能双模式） */
  accel: { mode: AccelMode };
  /** ready = 本地小模型是否已下载就绪（设置页据此展示「本地大脑已就绪」徽标） */
  local_llm: { enabled: boolean; ready?: boolean };
  acp: { enabled: boolean };
  remote: { enabled: boolean };
  /** 主动视觉开关（视图新增字段；旧后端缺失时前端按关闭处理） */
  vision?: { enabled: boolean };
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
 * 白名单键：``cloud.provider`` / ``tts.voice`` / ``local_llm.enabled`` /
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