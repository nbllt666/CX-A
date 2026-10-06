# -*- coding: utf-8 -*-
"""llama.cpp 内置运行时（Task C1）：LlamaRuntime 与 LlamaEmbeddingProvider。

承载两块 GGUF 模型（见工程文档 §6）：
1. qwen3-embedding:0.6b —— 记忆检索嵌入（内置）；
2. 本地小 LLM（20261004 全换 Gemma 4 多模态，默认档 E2B-Q4）——「是否回复」判定 +
   断网兜底回复 + 本地视觉理解（可选下载，存 data/local_llm/）。

设计要点（对齐 Task C1 必做清单）：
- **真实调用路径 + 无库降级双轨**：``load_embedding_model`` / ``load_local_llm``
  通过 ``importlib.import_module("llama_cpp")`` 导入 ``Llama``；真实环境若已安装
  llama-cpp-python 则走真实推理；未安装时导入抛 ``ImportError`` 并转为
  ``RuntimeError``（提示 pip install llama-cpp-python）。测试通过向 ``sys.modules``
  注入 fake llama_cpp 即可模拟真实行为，无需真装包。
- **模型文件缺失 / 加载失败 → 返回 False + warning，进程不崩溃**（对齐 spec 场景
  "模型加载失败"）：仅在 llama_cpp 模块缺失（导入失败）时才抛 ``RuntimeError``；
  其余加载异常一律捕获置未就绪并降级。
- **懒加载**：``__init__`` 仅读取配置意向，不立即加载任何模型。
- **与 A5 检索管线衔接**：``LlamaEmbeddingProvider`` 包装 ``LlamaRuntime.embed``，
  语义与 ``EmbeddingProvider`` 一致，可直接替换 ``LiteEmbeddingProvider`` 桩。
- **嵌入外部路径**（20260926_模块0_真实嵌入与向量持久化）：``load_embedding_model``
  在 llama-cpp-python 缺席时回落 ``llama-server.exe`` 常驻子进程（
  ``lite.runtime.llama_server.LlamaServerEmbedder``），与 chat 的外部路径同构；
  模型路径解析统一走 ``resolve_embedding_model_path``（配置 → 约定目录）。
- 本模块不 import 任何未实现模块。
"""

import importlib
import os
import re

from lite.memory.embedding import EmbeddingProvider

#: 嵌入模型默认标识（对齐 config DEFAULTS 与工程文档 §6.2）
DEFAULT_EMBEDDING_MODEL = "qwen3-embedding:0.6b"

#: 本地小 LLM 生成时的上下文窗口建议值
DEFAULT_LLM_N_CTX = 2048

#: 离线对话时拼接进提示词的最大历史消息条数
DEFAULT_CHAT_WINDOW = 6

#: offline_chat 单次生成的最大 token 数
OFFLINE_CHAT_MAX_TOKENS = 128

#: 「是否回复」判定单次生成的最大 token 数
JUDGE_MAX_TOKENS = 8

#: n_ctx 预算中为响应/安全保留的余量 token 数（M11 溢出防护）
N_CTX_RESERVE_TOKENS = 64

#: prompt 长度估算比例：非 CJK 字符约 4 字符 ≈ 1 token（历史口径，仅对 ASCII 成立）
_CHARS_PER_TOKEN = 4

#: CJK 字符 token 估算：每字符 ≈ 0.75 token（约 1.3 字符/token，第四轮体检批次C：
#: 原统一 4 字符/token 口径对中文低估 3-4 倍，n_ctx 溢出防护失效）
_TOKENS_PER_CJK_CHAR = 0.75

#: CJK 表意字符判定（含扩展 A 区与兼容表意区）
_CJK_CHAR_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

#: llama.cpp 后端构建合法值集（20261002 批 A）：其余值一律归一 ""（默认构建）。
_LLAMA_BACKENDS = ("cuda", "vulkan")

#: 嵌入桥接后端档（20261002 批 B）：``embedding.backend == "onnx"`` 时嵌入经
#: 语音桥 embed op（sidecar 以 ORT 加载 Qwen3-Embedding ONNX，last-token
#: pooling + L2 归一）；桥路径不可用（sidecar 缺席 / embed 资产缺失）→ 中文
#: 告警回退 llama.cpp 既有路径。与 :data:`_LLAMA_BACKENDS`（llama.cpp 构建目录
#: 选择语义）互斥使用——``onnx`` 不是构建目录意图，不参与 resolve_llama_dir。
EMBED_BRIDGE_BACKEND = "onnx"


class VoiceBridgeEmbedder:
    """经语音桥 embed op 的嵌入客户端（与 :class:`LlamaServerEmbedder` 同接口）。

    ``embedding.backend == "onnx"`` 时由 :class:`LlamaRuntime` 装配：sidecar
    以 ORT 加载 ``<root>/runtime/voice_bridge/embed_onnx/`` 下的 Qwen3-Embedding
    ONNX（last-token pooling + L2 归一，见 voice_bridge.py 的 embed op 协议）。
    接口对齐：``ensure_started`` / ``embed`` / ``dim`` / ``close``。

    失败语义：sidecar 缺席 / 桥进程不可用 / embed 资产缺失 / ok=false 均抛
    中文 ``RuntimeError``（含桥客户端的 :class:`VoiceBridgeError`——其本身即
    RuntimeError 子类），由 :meth:`LlamaRuntime.load_embedding_model` 按
    "桥路径不可用"回退 llama.cpp。
    """

    def __init__(self, root=None, device="cpu", timeout=None):
        """构造独立桥客户端（不启动进程；``ensure_started`` 时探针）。

        :param root: 便携根；缺省经 ``app_root()`` 推导。
        :param device: 桥设备意图（embed 固定 CPU EP 推理，此参数仅透传桥进程）。
        :param timeout: 单请求超时秒数；缺省 :data:`EXTERNAL_EMBED_REQUEST_TIMEOUT_S`。
        """
        # 函数内延迟导入：仅 onnx 后端真正走到本类时才进入桥客户端导入链
        from lite.audio.voice_bridge_client import VoiceBridgeClient

        self._client = VoiceBridgeClient(
            root=root, device=device,
            timeout=int(timeout or EXTERNAL_EMBED_REQUEST_TIMEOUT_S),
        )
        self._dim = None

    def ensure_started(self):
        """探针就绪：ping 桥进程 + embed 单条探针确定维度。

        :raises RuntimeError: sidecar 缺席 / 进程不可用 / embed 资产缺失
            （sidecar ok=false）时抛出（中文）。
        """
        self._client.request({"op": "ping"}, timeout=30)
        self._dim = self._probe_dim()

    def _probe_dim(self, probe_text="ping"):
        """embed 单条探针：返回向量维度（缓存）。"""
        vectors = self.embed([str(probe_text)])
        if not vectors or not vectors[0]:
            raise RuntimeError("embed 探针未返回向量（sidecar 响应异常）")
        return len(vectors[0])

    def embed(self, texts):
        """批量文本嵌入：``list[str]`` → ``list[list[float]]``（L2 归一 float32）。

        :raises RuntimeError: texts 非列表/为空（中文）；桥交互失败或 sidecar
            返回 ok=false（VoiceBridgeError，中文）时抛出。
        """
        if not isinstance(texts, list):
            raise RuntimeError("embed 的 texts 必须为 list。")
        if not texts:
            raise RuntimeError("embed 的 texts 不能为空列表。")
        header, _payload = self._client.request({"op": "embed", "texts": [str(t) for t in texts]})
        vectors = header.get("vectors")
        if not isinstance(vectors, list):
            raise RuntimeError("embed 响应缺少 vectors 字段（sidecar 响应异常）")
        return vectors

    def dim(self, probe_text="ping"):
        """嵌入向量维度（首次探针后缓存）。"""
        if self._dim is None:
            self._dim = self._probe_dim(probe_text)
        return self._dim

    def close(self):
        """显式收尾桥进程（幂等、不抛错；与 LlamaRuntime.close 的调用约定一致）。"""
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 - 回收失败不抛（进程随系统回收）
            pass


def _normalize_backend(value) -> str:
    """归一 llama.cpp 后端意图（``"cuda"`` / ``"vulkan"`` / ``""``）。

    缺键 / None / 空串 / 非法值（含 ``"auto"`` 等未定义档）一律归一 ``""``，
    与历史行为（默认 CUDA/CPU 构建目录）逐字等价。

    :param value: 配置读出的原始值（任意类型先 str 化）。
    :return: 合法后端标识（小写、去首尾空白）；非法回 ``""``。
    """
    normalized = str(value or "").strip().lower()
    return normalized if normalized in _LLAMA_BACKENDS else ""


#: 嵌入后端合法值集（20261002 批 B 扩展）：cuda / vulkan（llama.cpp 构建目录
#: 选择）+ ``onnx``（语音桥 ORT 嵌入档，见 :data:`EMBED_BRIDGE_BACKEND`）。
_EMBED_BACKENDS = _LLAMA_BACKENDS + (EMBED_BRIDGE_BACKEND,)


def _normalize_embed_backend(value) -> str:
    """归一嵌入后端意图（``"cuda"`` / ``"vulkan"`` / ``"onnx"`` / ``""``）。

    与 :func:`_normalize_backend` 的差异：嵌入侧放行 ``"onnx"`` 档（显式配置
    嵌入走语音桥 embed op）；``local_llm.backend`` 无此语义，仍由
    :func:`_normalize_backend` 归一（非法回 ``""``，批 A 行为不变）。

    :param value: 配置读出的原始值（任意类型先 str 化）。
    :return: 合法后端标识（小写、去首尾空白）；非法回 ``""``。
    """
    normalized = str(value or "").strip().lower()
    return normalized if normalized in _EMBED_BACKENDS else ""

#: GPU 卸载层数——device="cpu" 且未显式配置 n_gpu_layers 时使用（0 = 全部层驻留 CPU）
GPU_LAYERS_CPU = 0

#: GPU 卸载层数——device="gpu" 且未显式配置 n_gpu_layers 时使用（-1 = 尽量全部层卸载）
GPU_LAYERS_ALL = -1

#: 外部路径：llama.cpp 预编译二进制相对便携根的落点（规划 §四-15 替代路线——
#: llama-cpp-python 因长路径墙装不上，改经 ``llama-cli.exe`` 子进程推理）。
EXTERNAL_LLAMA_CLI_REL = ("runtime", "llama", "llama-cli.exe")

#: 外部路径单次请求超时（秒）——含模型冷加载；与前端 300s 请求口径一致。
EXTERNAL_CHAT_TIMEOUT_S = 300

# ------------------------------------------------------------------ #
# 外部嵌入路径（20260926_模块0_真实嵌入与向量持久化）                  #
# ------------------------------------------------------------------ #

#: （20261002 批 A 起废弃）原 llama-server 固定落点常量已由
#: ``lite.runtime.llama_server.resolve_llama_dir`` 接管——按 backend 意图在
#: ``runtime/llama``（默认 CUDA/CPU 构建）与 ``runtime/llama_vulkan``
#: （Vulkan 构建）间选择；缺省行为与历史落点逐字等价。

#: 嵌入模型约定落点（安装器 [Files] 直落目标，与 manifest install_target 一致）：
#: ``<root>/data/local_llm/qwen3-embedding-0.6b/*.gguf``。
EMBEDDING_MODEL_DIR_REL = ("data", "local_llm", "qwen3-embedding-0.6b")

#: 嵌入服务上下文窗口（token）：记忆文本远短于该值；固定值避免配置面膨胀。
EMBEDDING_SERVER_N_CTX = 2048

#: 外部嵌入服务就绪等待上限（秒）——0.6B Q8 实测冷加载 2.5~3.5s，放宽覆盖慢盘。
EXTERNAL_EMBED_READY_TIMEOUT_S = 120

#: 外部嵌入单次请求超时（秒）——含批量 32 条（实测 1.55s@CPU）。
EXTERNAL_EMBED_REQUEST_TIMEOUT_S = 120

#: 离线对话经外部路径生成时的采样温度（与 2026-09-25 实测口径一致）。
OFFLINE_CHAT_TEMPERATURE = 0.7

#: 常驻 chat 服务的固定随机种子（20260930）：与语音桥 llama-cli 路径 ``-s 42``
#: 同口径。实测（20260930 全链探针）：同请求可稳定复现，但首轮与后续轮存在
#: 服务端序列状态差异（首轮回复与后续不同）——回复多样性是否放开另议。
CHAT_DEFAULT_SEED = 42

#: 思维链关闭后缀（Qwen3 软开关，20260930）：追加在提示词末尾后，模型直接给出
#: 回复而不产出 ``[Start thinking]…[End thinking]`` 思考块。本链路是 raw 补全
#: （llama-cli -p，无 chat template），``--chat-template-kwargs`` 不生效，
#: 软开关是唯一可行路径。用力点：思考块既拖慢生成（实测约 168 token ≈ 1.9s），
#: 又会混入回复文本被 TTS 当作正文朗读（实测整段合成放大到 25s 级首音延迟）。
NO_THINK_SUFFIX = " /no_think"

#: 「是否回复」判定提示词（中文，要求只答 是/否）
JUDGE_PROMPT_TEMPLATE = (
    "你是本助手的「是否应当回复」判定器。\n"
    "请判断用户刚说的这句话，是否是在对本智能助手说话、是否需要回复。\n"
    "只回答一个字：是 或 否。\n"
    "用户说：{user_text}\n"
    "是否回复："
)


class LlamaNotReady(Exception):
    """模型未就绪异常。

    当所请求的模型（嵌入模型或本地小 LLM）尚未成功加载、或调用时不可使用时抛出，
    用于提示调用方先调用 ``load_embedding_model`` / ``load_local_llm`` 完成加载。
    """


def resolve_embedding_model_path(config=None, root=None) -> str:
    """解析嵌入模型 GGUF 路径（单一真相源：配置优先 → 约定目录扫描）。

    顺序（20260926_模块0_真实嵌入与向量持久化）：
    1) ``embedding.model_path`` 非空：绝对路径原样返回；相对路径按 ``root`` 拼接；
    2) 约定目录 ``<root>/data/local_llm/qwen3-embedding-0.6b/`` 下的 ``*.gguf``
       （按文件名排序取首个；存在含 ``q8_0`` 的文件时优先取它）；
    3) 都没有 → 返回空串（调用方按"嵌入不可用"降级为桩嵌入）。

    :param config: 配置来源（None / dict / ConfigManager，语义同 ``_read_cfg``）。
    :param root: 应用根覆盖（None 经 frozen-aware 的 ``app_root()`` 推导）。
    :return: str 模型文件绝对路径；未找到返回 ""。
    """
    if root is None:
        from lite.config.paths import app_root

        root = app_root()
    configured = str(
        LlamaRuntime._read_cfg(config, "embedding", "model_path", "") or ""
    ).strip()
    if configured:
        if os.path.isabs(configured):
            return configured
        # 相对路径按根拼接并归一化分隔符（配置里写正斜杠也得到规范 Windows 路径）
        return os.path.normpath(os.path.join(root, configured))
    model_dir = os.path.join(root, *EMBEDDING_MODEL_DIR_REL)
    try:
        candidates = sorted(
            name for name in os.listdir(model_dir) if name.lower().endswith(".gguf")
        )
    except OSError:
        return ""
    if not candidates:
        return ""
    preferred = [name for name in candidates if "q8_0" in name.lower()]
    return os.path.join(model_dir, (preferred or candidates)[0])


def _import_llama():
    """导入 llama_cpp 并返回 ``Llama`` 类。

    未安装 llama-cpp-python（或 ``llama_cpp`` 模块不可用）时抛 ``RuntimeError``，
    附带 pip install 安装指引。本函数返回后说明模块可用，可安全构造模型实例。
    """
    try:
        module = importlib.import_module("llama_cpp")
    except ImportError as exc:
        raise RuntimeError(
            "llama-cpp-python 未安装：请先执行 pip install llama-cpp-python "
            "（源码 https://github.com/abetlen/llama-cpp-python）后再启用本地运行时。"
        ) from exc
    llama_cls = getattr(module, "Llama", None)
    if llama_cls is None:
        raise RuntimeError(
            "llama_cpp 模块中不存在 Llama 类，请确认 llama-cpp-python 安装完整。"
        )
    return llama_cls


def _estimate_prompt_tokens(text) -> float:
    """按 CJK 字符占比动态折算 prompt 的 token 估算值（第四轮体检批次C）。

    原口径 ``len(prompt) / 4`` 对中文低估 3-4 倍（中文约 1.3 字符/token），
    长中文 prompt 的 n_ctx 溢出防护失效。现按字符构成折算：

    ``tokens ≈ (n - c) / 4 + c * 0.75``

    其中 n 为总字符数、c 为 CJK 表意字符数（即非 CJK 按 4 字符/token、
    CJK 按 0.75 token/字符）。估算值随文本长度单调不减。

    :param text: 待估算文本（str；非 str 先 str() 化）
    :return: token 估算值（float）
    """
    text = str(text or "")
    total = len(text)
    if total == 0:
        return 0.0
    cjk = len(_CJK_CHAR_RE.findall(text))
    return (total - cjk) / _CHARS_PER_TOKEN + cjk * _TOKENS_PER_CJK_CHAR


def _clip_text_to_token_budget(text, budget_tokens) -> str:
    """把文本硬截断到 token 估算预算内（尾部截断）。

    估算随长度单调，先按比例一次截到位；若前缀构成与整体不同导致仍超预算，
    逐字符回退兜底，保证返回值估算不超 ``budget_tokens``。

    :param text: 待截断文本
    :param budget_tokens: token 估算预算上限
    :return: 截断后的文本（估算值 <= 预算）
    """
    est = _estimate_prompt_tokens(text)
    if est <= budget_tokens:
        return text
    cut = max(1, int(len(text) * budget_tokens / est))
    while cut > 1 and _estimate_prompt_tokens(text[:cut]) > budget_tokens:
        cut -= 1
    return text[:cut]


class LlamaRuntime:
    """内置 llama.cpp 运行时，管理 GGUF 模型加载与推理。

    职责：
    1. 懒加载 qwen3-embedding 嵌入模型与本地小 LLM；
    2. 提供嵌入（记忆检索用）、「是否回复」判定与断网兜底回复；
    3. 加载失败与依赖缺失时按降级策略处理，保证主进程不崩溃。
    """

    def __init__(self, config=None, root=None):
        """初始化运行时，读取配置意向但不加载任何模型。

        Args:
            config: 可选的配置来源，支持三种形态：
                - ``None``：使用默认配置意向；
                - ``dict``：形如 ``{"embedding": {...}, "local_llm": {...}}`` 的嵌套字典；
                - ``ConfigManager`` 实例（具备 ``get(section, key, default)`` 接口）。
            root: 便携根覆盖（外部 llama-cli 路径推导用）；缺省经
                ``lite.config.paths.app_root()`` 推导（frozen-aware 单一真相源）。
        """
        #: 便携根覆盖（None 时按 app_root() 推导；测试可显式注入）
        self._root_override = root
        #: 嵌入模型配置意向（标识/文件名，非已加载实例）
        self._emb_model_name = self._read_cfg(config, "embedding", "model", DEFAULT_EMBEDDING_MODEL)
        #: 本地小 LLM 是否启用
        self._llm_enabled = bool(self._read_cfg(config, "local_llm", "enabled", False))
        #: 本地小 LLM 模型文件路径
        self._llm_path = self._read_cfg(config, "local_llm", "model_path", "") or ""
        #: 本地小 LLM 设备意图串（cpu/gpu/auto；外部桥客户端透传用）
        self._llm_device = str(self._read_cfg(config, "local_llm", "device", "cpu") or "cpu")
        #: llama.cpp 后端构建意图（"cuda"/"vulkan"/""；20261002 批 A：非法归一 ""，
        #: 决定 llama-server.exe 取 runtime/llama_vulkan 还是默认 runtime/llama 目录）
        self._llm_backend = _normalize_backend(self._read_cfg(config, "local_llm", "backend", ""))
        #: 嵌入服务后端意图（20261002 批 B 扩展："cuda"/"vulkan"/"onnx"；onnx =
        #: 语音桥 ORT 嵌入档，cuda/vulkan 决定 llama.cpp 构建目录，非法归一 ""）
        self._emb_backend = _normalize_embed_backend(self._read_cfg(config, "embedding", "backend", ""))
        #: 本地小 LLM 上下文窗口（可选 n_ctx 覆盖键，缺省 DEFAULT_LLM_N_CTX；非法值回退默认）
        raw_n_ctx = self._read_cfg(config, "local_llm", "n_ctx", None)
        try:
            parsed_n_ctx = int(raw_n_ctx)
        except (TypeError, ValueError):
            parsed_n_ctx = DEFAULT_LLM_N_CTX
        self._n_ctx = parsed_n_ctx if parsed_n_ctx > 0 else DEFAULT_LLM_N_CTX
        #: GPU 卸载层数意向：嵌入模型 / 本地小 LLM 各自独立解析（默认 0 = 纯 CPU）
        self._emb_n_gpu_layers = self._read_gpu_layers(config, "embedding")
        self._llm_n_gpu_layers = self._read_gpu_layers(config, "local_llm")

        #: 已加载的嵌入模型实例（Llama），未加载为 None
        self._emb_model = None
        #: 嵌入模型是否就绪
        self._emb_ready = False
        #: 外部嵌入服务（llama-server 常驻子进程客户端），未启用为 None
        #: （20260926_模块0_真实嵌入与向量持久化：冻结态 llama-cpp-python 缺席时的真实路径）
        self._external_emb = None
        #: 嵌入向量维度（外部路径启动时探针确定；未确定为 None）
        self.emb_dim = None
        #: 已加载的本地小 LLM 实例（Llama），未加载为 None
        self._llm = None
        #: 本地小 LLM 是否就绪
        self._llm_ready = False
        #: 外部路径：语音桥客户端（llama-cli 子进程推理），未启用为 None
        self._external_client = None
        #: 外部路径：常驻 chat 服务客户端（llama-server /v1/chat/completions，
        #: 20260930_模块0_本地LLM常驻推理改造；优先于桥路径），未启用为 None
        self._external_chat = None
        #: 外部路径的 GGUF 模型路径（逐次请求透传给桥）
        self._external_model_path = ""
        #: 加载过程中的降级提示（与 ConfigManager.warnings 语义一致）
        self.warnings = []
        #: 离线对话可拼接的最大历史消息条数
        self._chat_window = DEFAULT_CHAT_WINDOW

    # ------------------------------------------------------------------ #
    # 内部：配置读取 / 输出解析                                           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _read_cfg(config, section, key, default):
        """从多种 config 形态中读取 ``(section, key)``，缺失返回 default。"""
        if config is None:
            return default
        # ConfigManager 形态（具备 get(section, key, default)）
        if hasattr(config, "get") and not isinstance(config, dict):
            try:
                return config.get(section, key, default)
            except TypeError:
                pass
        # dict 形态（嵌套字典）
        sec = config.get(section) if isinstance(config, dict) else None
        if isinstance(sec, dict) and key in sec:
            return sec.get(key, default)
        return default

    @staticmethod
    def _read_gpu_layers(config, section) -> int:
        """解析指定配置段的 GPU 卸载层数意向。

        规则：
        - 显式 ``{section}.n_gpu_layers``（可转 int）优先于 device 推导；
        - 缺失 / None / 非法时按 ``{section}.device`` 推导：
          ``"gpu" -> -1``（全层卸载），其余回 ``0``（纯 CPU）；
        - 默认 cpu，与历史行为一致。

        Args:
            config: 配置来源（None / dict / ConfigManager），语义同 ``_read_cfg``。
            section: 配置段名（"embedding" 或 "local_llm"）。
        Returns:
            int: 传给 ``Llama(n_gpu_layers=...)`` 的层数。
        """
        raw_layers = LlamaRuntime._read_cfg(config, section, "n_gpu_layers", None)
        if raw_layers is not None:
            try:
                return int(raw_layers)
            except (TypeError, ValueError):
                pass  # 非法覆盖值 → 按 device 推导
        device = str(LlamaRuntime._read_cfg(config, section, "device", "cpu") or "cpu")
        return GPU_LAYERS_ALL if device.strip().lower() == "gpu" else GPU_LAYERS_CPU

    @staticmethod
    def _extract_text(result):
        """从模型输出中提取纯文本。

        兼容两种形态：
        - 真实 llama-cpp-python：``LlamaOutput`` 为 dict 子类，取 ``["choices"][0]["text"]``
          （或 chat 形态的 ``["message"]["content"]``）；
        - fake / 直接返回纯字符串。
        """
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            choices = result.get("choices")
            if isinstance(choices, list) and choices:
                first = choices[0]
                if isinstance(first, dict):
                    return first.get("text") or first.get("message", {}).get("content", "")
        return str(result)

    @staticmethod
    def _extract_embeddings(result):
        """从 create_embedding 输出中提取向量列表。

        兼容：dict 形态（``{"data": [{"embedding": [...]}, ...]}`）与纯列表形态。
        """
        if isinstance(result, dict):
            data = result.get("data")
            if isinstance(data, list):
                out = []
                for item in data:
                    if isinstance(item, dict):
                        out.append(item.get("embedding"))
                    else:
                        out.append(item)
                return out
        if isinstance(result, list):
            return list(result)
        raise TypeError(f"无法解析的嵌入输出类型：{type(result)!r}")

    @staticmethod
    def _parse_yes(text):
        """解析「是否回复」判定结果：首字/首词命中 是/yes/Y/True 等视为 True。"""
        t = str(text).strip()
        if not t:
            return False
        first_char = t[0]
        first_word = t.split()[0].lower() if t.split() else t.lower()
        for yes_token in ("是", "yes", "y", "true", "对", "要"):
            if first_char == yes_token or first_word.startswith(yes_token):
                return True
        return False

    def _format_messages(self, messages):
        """把消息列表格式化为适用于本地小 LLM 的拼接提示词。

        Args:
            messages: list[dict]，元素含 ``role`` / ``content`` 字段，
                形如 ``[{"role": "system", "content": ...}, {"role": "user", "content": ...}]``。
        Returns:
            str: 按 role: content 拼接的文本（保留 system 前置，仅取最近 N 条）。
        """
        if not messages:
            return "（无消息）"
        recent = list(messages)[-self._chat_window:]
        lines = []
        for msg in recent:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if content:
                lines.append(f"{role}: {content}")
        return "\n".join(lines) if lines else "（无消息）"

    # ------------------------------------------------------------------ #
    # 模型加载                                                            #
    # ------------------------------------------------------------------ #

    def load_embedding_model(self, path) -> bool:
        """加载 qwen3-embedding 嵌入模型（in-process 优先，缺席回落外部 llama-server）。

        双路径（20260926_模块0_真实嵌入与向量持久化，与 chat 外部路径同构）：
        - **in-process**：llama-cpp-python 可用时按原口径构造 ``Llama`` 实例；
        - **外部**：llama-cpp-python 缺席（冻结产物常态）时拉起
          ``<root>/runtime/llama/llama-server.exe`` 常驻子进程（就绪 + dim 探针），
          就绪后 ``_external_emb`` 非空。

        Args:
            path: GGUF 模型文件绝对路径。
        Returns:
            bool: 成功加载返回 True；文件缺失 / 加载异常返回 False（进程不崩溃，
                置 ``_emb_ready=False`` 并在 ``warnings`` 记录降级提示）。
        Raises:
            RuntimeError: 当 llama-cpp-python 未安装**且**外部 llama-server 路径不可用时抛出。
        """
        if not os.path.exists(path):
            return self._record_load_failure("emb", f"嵌入模型文件不存在：{path}（配置意向：{self._emb_model_name}）")
        # 20261002 批 B：backend="onnx" → 嵌入优先走语音桥 embed op（sidecar 以
        # ORT 加载 Qwen3-Embedding ONNX）；桥路径不可用（sidecar 缺席 / embed
        # 资产缺失）→ 中文告警后回落 llama.cpp 既有双路径，行为不中断。
        if self._emb_backend == EMBED_BRIDGE_BACKEND and self._try_load_bridge_embedding():
            return True
        try:
            llama_cls = _import_llama()  # 导入失败抛 RuntimeError（提示安装）
        except RuntimeError as exc:
            # llama-cpp-python 缺席：改走外部 llama-server 路径（冻结态主路径）
            if self._try_load_external_embedding(path):
                return True
            raise RuntimeError(
                f"{exc}；外部 llama-server 嵌入路径亦不可用"
                f"（需 <root>/runtime/llama/llama-server.exe 与嵌入模型 GGUF 同时就位）。"
            ) from exc
        try:
            self._emb_model = llama_cls(
                model_path=path, embedding=True, n_gpu_layers=self._emb_n_gpu_layers
            )
        except Exception as exc:  # noqa: BLE001 - 加载异常按降级处理，不崩溃
            self._emb_model = None
            return self._record_load_failure("emb", f"嵌入模型加载失败：{exc}")
        self._emb_ready = True
        return True

    def _try_load_bridge_embedding(self) -> bool:
        """尝试经语音桥 embed op 的嵌入路径（``backend=="onnx"`` 专用，20261002 批 B）。

        就位条件：sidecar 解释器与 bridge 脚本就位，且 embed 资产就绪
        （``<root>/runtime/voice_bridge/embed_onnx/`` + 探针成功）。任一环节失败
        仅记录 ``warnings``（中文）并返回 False，由调用方回落 llama.cpp 既有路径。

        :return: 就绪返回 True（置 ``_external_emb`` / ``_emb_ready`` / ``emb_dim``）；
            否则 False（不改就绪状态）。
        """
        try:
            embedder = VoiceBridgeEmbedder(root=self._app_root())
            embedder.ensure_started()
        except Exception as exc:  # noqa: BLE001 - 桥路径不可用仅告警，不改动内嵌状态
            self.warnings.append(f"嵌入 ONNX 桥路径不可用：{exc}")
            return False
        self._external_emb = embedder
        self._emb_ready = True
        self.emb_dim = int(embedder.dim())
        return True

    def _try_load_external_embedding(self, path) -> bool:
        """尝试外部 llama-server 嵌入路径（预编译二进制 + 本机回环 HTTP）。

        就位条件：``<root>/runtime/llama/llama-server.exe`` 存在且服务就绪
        （``ensure_started`` 轮询 /health 至 ok）+ dim 探针成功。任一环节失败仅记录
        ``warnings`` 并返回 False，由调用方决定抛错或降级。

        :param path: 嵌入模型 GGUF 路径。
        :return: 就绪返回 True（置 ``_external_emb`` / ``_emb_ready`` / ``emb_dim``）；
            否则 False（不改就绪状态）。
        """
        root = self._app_root()
        # 20261002 批 A：按嵌入后端意图选目录（vulkan → runtime/llama_vulkan，
        # 缺失回退默认目录；cuda/"" → runtime/llama 既有行为）
        from lite.runtime.llama_server import LLAMA_SERVER_EXE_NAME, resolve_llama_dir

        exe = os.path.join(resolve_llama_dir(root, self._emb_backend), LLAMA_SERVER_EXE_NAME)
        if not os.path.isfile(exe):
            self.warnings.append(f"外部嵌入路径不可用：llama-server 不存在（{exe}）")
            return False
        try:
            # 函数内延迟导入：外部嵌入客户端模块仅在实际走该路径时进入导入链
            from lite.runtime.llama_server import LlamaServerEmbedder

            embedder = LlamaServerEmbedder(
                exe,
                str(path),
                n_gpu_layers=int(self._emb_n_gpu_layers),
                n_ctx=EMBEDDING_SERVER_N_CTX,
                ready_timeout=EXTERNAL_EMBED_READY_TIMEOUT_S,
                request_timeout=EXTERNAL_EMBED_REQUEST_TIMEOUT_S,
            )
            embedder.ensure_started()  # 未就绪/进程夭折/超时均抛 RuntimeError
            self.emb_dim = int(embedder.dim())
        except Exception as exc:  # noqa: BLE001 - 外部路径失败仅告警，不改动内嵌状态
            self.warnings.append(f"外部 llama-server 嵌入路径不可用：{exc}")
            return False
        self._external_emb = embedder
        self._emb_ready = True
        return True

    def load_local_llm(self, path) -> bool:
        """加载本地小 LLM（embedding=False，n_ctx 建议 2048）。

        双路径（规划 §四-15）：
        - **in-process**：llama-cpp-python 可用时按原口径构造 ``Llama`` 实例；
        - **外部**：llama-cpp-python 缺席（不可安装）时改试外部预编译二进制
          （``<root>/runtime/llama/llama-cli.exe`` + 语音桥 sidecar，见
          :meth:`_try_load_external_llm`）——就绪后 ``_external_client`` 非空。
        两条路径都不可用才抛 RuntimeError（提示安装指引）。

        Args:
            path: GGUF 模型文件绝对路径。
        Returns:
            bool: 成功加载返回 True；文件缺失 / 加载异常返回 False（不崩溃，
                置 ``_llm_ready=False`` 并在 ``warnings`` 记录降级提示）。
        Raises:
            RuntimeError: 当 llama-cpp-python 缺失**且**外部 llama-cli 路径不可用时抛出。
        """
        if not os.path.exists(path):
            return self._record_load_failure("llm", f"本地小 LLM 文件不存在：{path}（配置意向：{self._llm_path}）")
        try:
            llama_cls = _import_llama()  # 导入失败抛 RuntimeError（提示安装）
        except RuntimeError as exc:
            # llama-cpp-python 缺席：改走外部预编译二进制路径（规划 §四-15）
            if self._try_load_external_llm(path):
                return True
            raise RuntimeError(
                f"{exc}；外部 llama-cli 路径亦不可用（需 <root>/runtime/llama/llama-cli.exe "
                f"与语音 sidecar 同时就位）。"
            ) from exc
        try:
            self._llm = llama_cls(
                model_path=path,
                embedding=False,
                n_ctx=self._n_ctx,
                n_gpu_layers=self._llm_n_gpu_layers,
            )
        except Exception as exc:  # noqa: BLE001 - 加载异常按降级处理，不崩溃
            self._llm = None
            return self._record_load_failure("llm", f"本地小 LLM 加载失败：{exc}")
        self._llm_ready = True
        return True

    def _record_load_failure(self, kind, message):
        """统一记录加载失败并置未就绪，返回 False。"""
        self.warnings.append(message)
        if kind == "emb":
            self._emb_ready = False
            self._emb_model = None
        else:
            self._llm_ready = False
            self._llm = None
        return False

    # ------------------------------------------------------------------ #
    # 外部路径（llama.cpp 预编译二进制 + 语音桥，规划 §四-15）            #
    # ------------------------------------------------------------------ #

    def _app_root(self):
        """便携根（root 覆盖优先；否则经 frozen-aware 的 app_root()）。"""
        if self._root_override:
            return self._root_override
        from lite.config.paths import app_root

        return app_root()

    def _try_load_external_llm(self, path) -> bool:
        """尝试外部路径（优先级：常驻 llama-server → 语音桥 llama-cli）。

        常驻路径（20260930_模块0_本地LLM常驻推理改造）：``llama-server.exe`` 就位时
        改走 ``/v1/chat/completions`` 常驻服务——消除 llama-cli **每次请求重载模型**
        （实测单次全流程 6.4~7.4s → 常驻 0.41~0.43s）；
        回落路径：维持既有语音桥 llama-cli 单次推理（``NO_THINK_SUFFIX`` 软开关
        即该路径关闭思考链的手段）。
        两条都不就位返回 False，由调用方按既有口径处理。

        :param path: GGUF 模型文件路径
        :return: 就绪返回 True 并置 ``_llm_ready``；否则 False（不改状态）
        """
        if self._try_load_chat_server(path):
            return True
        return self._try_load_bridge_llm(path)

    def _try_load_chat_server(self, path) -> bool:
        """尝试常驻 chat 服务路径（llama-server ``/v1/chat/completions``）。

        就位条件：``<root>/runtime/llama/llama-server.exe`` 存在。进程**懒启动**
        （此处置好客户端，首次 chat 或 :meth:`warm_local_llm` 预热时按需拉起）。
        构造失败仅记录 ``warnings`` 并返回 False（由调用方回落桥路径）。

        :param path: 本地小 LLM GGUF 路径
        :return: 就绪返回 True（置 ``_external_chat`` / ``_llm_ready``）；否则 False
        """
        root = self._app_root()
        # 20261002 批 A：按本地 LLM 后端意图选目录（口径同嵌入路径，见
        # resolve_llama_dir；缺省/非法值与历史 runtime/llama 推导逐字等价）
        from lite.runtime.llama_server import (
            CHAT_SERVER_N_CTX,
            LLAMA_SERVER_EXE_NAME,
            resolve_llama_dir,
        )

        exe = os.path.join(resolve_llama_dir(root, self._llm_backend), LLAMA_SERVER_EXE_NAME)
        if not os.path.isfile(exe):
            self.warnings.append(f"常驻 chat 路径不可用：llama-server 不存在（{exe}）")
            return False
        try:
            # 函数内延迟导入：chat 服务客户端模块仅在实际走该路径时进入导入链
            from lite.runtime.llama_server import LlamaServerChat

            chat_server = LlamaServerChat(
                exe,
                str(path),
                n_gpu_layers=int(self._llm_n_gpu_layers),
                # 20261004 Gemma 4：chat 服务上下文固定 8192（KV cache 显式封顶 +
                # 覆盖多模态图片 token），不再沿用嵌入口径的 self._n_ctx（2048）
                n_ctx=int(CHAT_SERVER_N_CTX),
            )
        except Exception as exc:  # noqa: BLE001 - 构造失败按外部不可用处理
            self.warnings.append(f"常驻 chat 路径不可用：{exc}")
            return False
        self._external_chat = chat_server
        self._llm_ready = True
        return True

    def warm_local_llm(self) -> bool:
        """预热常驻 chat 服务（幂等；失败返回 False 不抛错）。

        用例：装配方在启动后台线程调用，规避"应用启动后第一句话"再吃一次模型
        冷加载（实测 8B Q4 GPU 约 5.3~5.5s）。in-process / 桥路径为 no-op（True）。

        :return: 常驻服务已就绪（或本路径无需预热）返回 True；预热失败 False
        """
        chat_server = self._external_chat
        if chat_server is None:
            return True
        try:
            chat_server.ensure_started()
            return True
        except Exception:  # noqa: BLE001 - 预热失败静默（首次真实请求再兜底拉起）
            return False

    def _try_load_bridge_llm(self, path) -> bool:
        """尝试外部 llama-cli 路径（预编译二进制 + 语音桥 sidecar）。

        就位条件：``<root>/runtime/llama/llama-cli.exe`` 存在，且语音桥可用
        （sidecar 解释器与分发落点 ``runtime/voice_bridge/bridge.py`` 同时就位）。
        缺一即返回 False，由调用方回落既有路径（llama-cpp-python 缺席则维持
        RuntimeError）。

        :param path: GGUF 模型文件路径（逐次请求透传给桥）
        :return: 就绪返回 True 并置 ``_llm_ready``；否则 False（不改状态）
        """
        root = self._app_root()
        cli = os.path.join(root, *EXTERNAL_LLAMA_CLI_REL)
        if not os.path.isfile(cli):
            return False
        try:
            from lite.audio.voice_bridge_client import VoiceBridgeClient
        except Exception:  # noqa: BLE001 - 桥模块不可用按外部不可用处理
            return False
        client = VoiceBridgeClient(
            root=root, device=self._llm_device, timeout=EXTERNAL_CHAT_TIMEOUT_S
        )
        if not client.available():
            return False
        self._external_client = client
        self._external_model_path = str(path)
        self._llm_ready = True
        return True

    def _llm_available(self) -> bool:
        """本地小 LLM 是否可用（in-process / 常驻 chat 服务 / 外部桥 三者之一就绪）。"""
        return bool(
            self._llm_ready
            and (
                self._external_chat is not None
                or self._external_client is not None
                or self._llm is not None
            )
        )

    def _external_chat_generate(self, messages, max_tokens, temperature) -> str:
        """经常驻 chat 服务（llama-server /v1/chat/completions）生成一次文本。

        :raises LlamaNotReady: 常驻路径未就绪（防御性）
        :raises RuntimeError: 服务侧失败（重启重试后仍失败等中文错误）向上抛出，
            由调用方的降级出口（OfflineFallbackManager / judge 统一出口）兜底。
        """
        if self._external_chat is None:
            raise LlamaNotReady("常驻 chat 服务路径未就绪。")
        return self._external_chat.chat(
            messages,
            max_tokens=int(max_tokens),
            temperature=float(temperature),
            seed=CHAT_DEFAULT_SEED,
        ).strip()

    def _fit_messages(self, messages, max_tokens):
        """messages 版的 n_ctx 溢出防护（常驻 chat 服务路径，20260930）。

        规则：按 :func:`_estimate_prompt_tokens` 估算全部消息文本，超预算
        ``n_ctx - max_tokens - 64余量`` 时从最旧侧删减**非 system** 消息
        （最近一条恒留）；仍超限则把最后一条内容硬截断到剩余预算内。
        与 raw 提示词路径的 :meth:`_fit_prompt` 同哲学（保底 system + 最近一轮）。

        :param messages: OpenAI 兼容消息列表
        :param max_tokens: 本次生成最大 token 数（预算扣减）
        :return: 裁剪后的消息列表（浅拷贝，元素为 dict）
        """
        budget = self._token_budget(max_tokens)
        msgs = [
            dict(m) if isinstance(m, dict) else {"role": "user", "content": str(m)}
            for m in (messages or [])
        ]
        if not msgs:
            return msgs

        # 20261004 Gemma 4 多模态：任一消息 content 为数组（text + image_url）
        # 时整体跳过裁剪——base64 数据 URL 不参与 token 估算，硬截断会损坏
        # 数据 URL（服务端 400）；上下文预算由常驻服务 -c（CHAT_SERVER_N_CTX）兜底
        if any(not isinstance(m.get("content"), str) for m in msgs):
            return msgs

        def _est(items):
            """消息列表的 token 估算（各条内容按行拼接后估算）。"""
            return _estimate_prompt_tokens(
                "\n".join(str(m.get("content", "")) for m in items)
            )

        # 1) 从最旧侧删减非 system 消息（最近一条恒留）
        while len(msgs) > 1 and _est(msgs) > budget:
            for idx, m in enumerate(msgs[:-1]):
                if m.get("role") != "system":
                    msgs.pop(idx)
                    break
            else:
                break  # 已无非 system 可删（只剩 system + 最近一条）

        def _clip_at(index):
            """把第 ``index`` 条内容截到「预算 − 其余各条估算」内（整表兜底用）。"""
            others = _est([m for pos, m in enumerate(msgs) if pos != index])
            remain = max(1.0, budget - others)
            msgs[index]["content"] = _clip_text_to_token_budget(
                str(msgs[index].get("content", "")), remain
            )

        # 2) 仍超限：先截最近一条内容；再超限则截 system（整表硬截断兜底，
        #    与 raw 提示词路径 _fit_prompt 的尾部截断同哲学；必然收敛到预算内）
        if _est(msgs) > budget:
            _clip_at(len(msgs) - 1)
        if _est(msgs) > budget:
            _clip_at(0)
        return msgs

    def _external_generate(self, prompt, max_tokens, temperature) -> str:
        """经外部桥（llama-cli）生成一次文本。

        :raises LlamaNotReady: 外部路径未就绪（防御性）
        :raises VoiceBridgeError: 桥侧失败（超时 / 解析失败等中文错误）向上抛出，
            由调用方的降级出口（OfflineFallbackManager / judge 统一出口）兜底。
        """
        if self._external_client is None:
            raise LlamaNotReady("外部 llama-cli 路径未就绪。")
        header, _payload = self._external_client.request(
            {
                "op": "chat",
                "model": self._external_model_path,
                "prompt": str(prompt),
                "max_tokens": int(max_tokens),
                "n_ctx": int(self._n_ctx),
                "n_gpu_layers": int(self._llm_n_gpu_layers),
                "temperature": float(temperature),
            },
            timeout=EXTERNAL_CHAT_TIMEOUT_S,
        )
        return str(header.get("text") or "")

    # ------------------------------------------------------------------ #
    # 嵌入（EmbeddingProvider 语义）                                     #
    # ------------------------------------------------------------------ #

    def embed(self, texts: list) -> list:
        """批量文本嵌入（记忆检索用，in-process 与外部 llama-server 同接口）。

        Args:
            texts: 文本列表（list[str]）。
        Returns:
            list[list[float]]: 与输入等长的向量列表，维度固定（由模型决定）。
        Raises:
            LlamaNotReady: 嵌入模型未就绪时抛出。
            RuntimeError: 外部路径服务失败（重启重试后仍失败）时抛出（中文）。
        """
        if self._external_emb is not None:
            return self._external_emb.embed(list(texts))
        if not self._emb_ready or self._emb_model is None:
            raise LlamaNotReady("嵌入模型未就绪：请先调用 load_embedding_model 加载模型后再进行文本嵌入。")
        result = self._emb_model.create_embedding(input=list(texts))
        return self._extract_embeddings(result)

    def close(self):
        """回收外部服务进程（幂等；in-process 模型无需显式回收）。

        覆盖两块常驻子进程：嵌入服务（``_external_emb``）与常驻 chat 服务
        （``_external_chat``，20260930）。回收失败不抛（进程随系统回收）。
        """
        embedder = self._external_emb
        self._external_emb = None
        if embedder is not None:
            try:
                embedder.close()
            except Exception:  # noqa: BLE001 - 回收失败不抛（进程随系统回收）
                pass
        chat_server = self._external_chat
        self._external_chat = None
        if chat_server is not None:
            try:
                chat_server.close()
            except Exception:  # noqa: BLE001 - 回收失败不抛（进程随系统回收）
                pass

    def embed_texts(self, texts: list) -> list:
        """embed 的显式别名，便于调用方按语义命名。"""
        return self.embed(texts)

    # ------------------------------------------------------------------ #
    # 本地小 LLM：判定 + 离线兜底                                        #
    # ------------------------------------------------------------------ #

    def _token_budget(self, max_tokens):
        """本次生成的可用 prompt token 预算（M11 + 第四轮体检批次C）。

        预算公式：``n_ctx - max_tokens - 64余量``（token 数）；保证至少留出
        可用空间。预算判断统一使用 :func:`_estimate_prompt_tokens` 的 CJK
        占比动态折算估算，不再使用对中文失真的字符数近似。
        """
        return max(int(self._n_ctx) - int(max_tokens) - N_CTX_RESERVE_TOKENS, 1)

    def _fit_prompt(self, prompt, max_tokens, messages=None):
        """投递前的 n_ctx 溢出防护（M11 + 第四轮体检批次C CJK 折算）。

        规则：
        - token 估算按 CJK 字符占比动态折算：
          ``tokens ≈ (n-c)/4 + c*0.75``（CJK 约 1.3 字符/token，纯 ASCII 约
          4 字符/token），预算 = ``n_ctx - max_tokens - 64余量``（token 数）；
        - 未超限原样返回；
        - 超限且提供 ``messages``：按**消息列表**从最旧侧删减重建
          （保底全部 system 行 + 最近一轮），重建后仍未落地则继续行级兜底；
        - 行级兜底：拆行后保护头部连续 ``system`` 行，从最旧侧删减、最近一行恒留，
          重算仍超限（单条/system 即爆）则硬截断 prompt 尾部到 token 预算内。

        :param prompt: 待投递的提示词文本
        :param max_tokens: 本次生成的最大 token 数（用于预算计算）
        :param messages: 生成 ``prompt`` 的原始消息列表（可选）；提供时启用消息级删减
        :return: 裁剪后的提示词（token 估算 <= 预算）
        """
        prompt = str(prompt)
        budget = self._token_budget(max_tokens)
        if _estimate_prompt_tokens(prompt) <= budget:
            return prompt

        if messages:
            # 消息级删减重建：保底 system 行 + 最近一轮（绕开 chat_window 造成的截断）
            system_msgs = [
                m for m in messages
                if isinstance(m, dict) and m.get("role") == "system"
            ]
            recent = messages[-1] if messages else None
            if isinstance(recent, dict) and all(recent is not m for m in system_msgs):
                system_msgs.append(recent)
            rebuilt = self._format_messages(system_msgs)
            if _estimate_prompt_tokens(rebuilt) <= budget:
                return rebuilt
            prompt = rebuilt  # 仍超限 -> 继续行级兜底

        lines = prompt.split("\n")
        # 保护头部连续 system 行（_format_messages / 判定模板均以 system 开头）
        header = []
        idx = 0
        while idx < len(lines) - 1 and lines[idx].startswith("system"):
            header.append(lines[idx])
            idx += 1
        body = lines[idx:]
        while len(body) > 1 and _estimate_prompt_tokens("\n".join(header + body)) > budget:
            body.pop(0)  # 从最旧侧删减；body[-1]（最近一轮）恒保留
        fitted = "\n".join(header + body)
        # 硬截断尾部兜底：按 token 估算截到预算内
        return _clip_text_to_token_budget(fitted, budget)

    def judge_should_reply(self, user_text) -> bool:
        """本地小 LLM 判定：用户是否在对本助手说话、是否应当回复。

        使用中文提示词（见 ``JUDGE_PROMPT_TEMPLATE``）要求模型只答 是/否，
        解析结果首词/首字（是/yes/y/true）命中即判为 True。三条路径
        （in-process / 常驻 chat 服务 / llama-cli 桥）共用同一提示词与解析口径：
        常驻服务路径以「单条 user 消息」投递（走 chat template + 原生关思考），
        其余两条走 raw 提示词 + ``NO_THINK_SUFFIX``。

        Args:
            user_text: 用户刚说的话。
        Returns:
            bool: True 表示应当回复；False 表示不回复（自言自语）。
        Raises:
            LlamaNotReady: 本地小 LLM 未就绪（三条路径均不可用）时抛出。
        """
        if not self._llm_available():
            raise LlamaNotReady("本地小 LLM 未就绪：请先调用 load_local_llm 加载模型后再进行「是否回复」判定。")
        prompt = JUDGE_PROMPT_TEMPLATE.format(user_text=str(user_text).strip() or "（空输入）")
        # 常驻 chat 服务路径：走 chat template（enable_thinking=false 原生关思考）
        if self._external_chat is not None:
            messages = self._fit_messages(
                [{"role": "user", "content": prompt}], JUDGE_MAX_TOKENS
            )
            return self._parse_yes(
                self._external_chat_generate(messages, JUDGE_MAX_TOKENS, 0.0)
            )
        # 关闭思维链（20260930）：判定要求一字作答，思考块会致首字解析错位
        prompt = self._fit_prompt(prompt, JUDGE_MAX_TOKENS) + NO_THINK_SUFFIX
        if self._external_client is not None:
            return self._parse_yes(self._external_generate(prompt, JUDGE_MAX_TOKENS, 0.0))
        result = self._llm(prompt, max_tokens=JUDGE_MAX_TOKENS, temperature=0.0)
        return self._parse_yes(self._extract_text(result))

    def offline_chat(self, messages) -> str:
        """断网兜底：本地小 LLM 按 system + 最近消息生成回复。

        Args:
            messages: list[dict]，形如 ``[{"role": "system", "content": ...},
                {"role": "user", "content": ...}]``。
        Returns:
            str: 本地小 LLM 生成的纯文本回复（in-process / 常驻 chat 服务 / 外部桥）。
        Raises:
            LlamaNotReady: 本地小 LLM 未就绪（三条路径均不可用）时抛出。
        """
        if not self._llm_available():
            raise LlamaNotReady("本地小 LLM 未就绪：请先调用 load_local_llm 加载模型后再进行离线对话。")
        # 常驻 chat 服务路径（20260930）：直接投递 messages（走 chat template +
        # enable_thinking=false 原生关思考），n_ctx 预算由 _fit_messages 防护
        if self._external_chat is not None:
            fitted = self._fit_messages(messages, OFFLINE_CHAT_MAX_TOKENS)
            return self._external_chat_generate(
                fitted, OFFLINE_CHAT_MAX_TOKENS, OFFLINE_CHAT_TEMPERATURE
            )
        prompt = self._fit_prompt(
            self._format_messages(messages), OFFLINE_CHAT_MAX_TOKENS, messages=messages
        )
        # 关闭思维链（20260930）：思考块混入回复会被 TTS 当作正文朗读（见 NO_THINK_SUFFIX）
        prompt = prompt + NO_THINK_SUFFIX
        if self._external_client is not None:
            return self._external_generate(
                prompt, OFFLINE_CHAT_MAX_TOKENS, OFFLINE_CHAT_TEMPERATURE
            ).strip()
        result = self._llm(prompt, max_tokens=OFFLINE_CHAT_MAX_TOKENS)
        return self._extract_text(result).strip()


class LlamaEmbeddingProvider(EmbeddingProvider):
    """llama.cpp 嵌入提供者（Task C1），包装 ``LlamaRuntime.embed``。

    实现 ``EmbeddingProvider`` 抽象，供 A5 记忆检索管线直接替换
    ``LiteEmbeddingProvider`` 桩使用，调用方（pipeline / vector_store）无需改动。
    """

    def __init__(self, runtime: LlamaRuntime):
        """初始化嵌入提供者。

        Args:
            runtime: 已持有 LlamaRuntime 实例（通常已调用 load_embedding_model）。
        """
        if runtime is None:
            raise TypeError("runtime 不能为 None，请传入 LlamaRuntime 实例。")
        self._runtime = runtime

    @property
    def runtime(self):
        """返回被包装的 LlamaRuntime 实例。"""
        return self._runtime

    def embed(self, texts: list) -> list:
        """批量文本嵌入，委托给包装的 LlamaRuntime.embed。

        Args:
            texts: 文本列表（list[str]）。
        Returns:
            list[list[float]]: 与输入等长的向量列表。
        Raises:
            LlamaNotReady: 底层嵌入模型未就绪时抛出。
        """
        return self._runtime.embed(texts)