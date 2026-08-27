# -*- coding: utf-8 -*-
"""llama.cpp 内置运行时（Task C1）：LlamaRuntime 与 LlamaEmbeddingProvider。

承载两块 GGUF 模型（见工程文档 §6）：
1. qwen3-embedding:0.6b —— 记忆检索嵌入（内置）；
2. 本地小 LLM（建议 ~1.7B）——「是否回复」判定 + 断网兜底回复（可选下载，存 data/local_llm/）。

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
- 本模块不 import 任何未实现模块。
"""

import importlib
import os

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

#: prompt 长度估算比例：约 4 字符 ≈ 1 token
_CHARS_PER_TOKEN = 4

#: GPU 卸载层数——device="cpu" 且未显式配置 n_gpu_layers 时使用（0 = 全部层驻留 CPU）
GPU_LAYERS_CPU = 0

#: GPU 卸载层数——device="gpu" 且未显式配置 n_gpu_layers 时使用（-1 = 尽量全部层卸载）
GPU_LAYERS_ALL = -1

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


class LlamaRuntime:
    """内置 llama.cpp 运行时，管理 GGUF 模型加载与推理。

    职责：
    1. 懒加载 qwen3-embedding 嵌入模型与本地小 LLM；
    2. 提供嵌入（记忆检索用）、「是否回复」判定与断网兜底回复；
    3. 加载失败与依赖缺失时按降级策略处理，保证主进程不崩溃。
    """

    def __init__(self, config=None):
        """初始化运行时，读取配置意向但不加载任何模型。

        Args:
            config: 可选的配置来源，支持三种形态：
                - ``None``：使用默认配置意向；
                - ``dict``：形如 ``{"embedding": {...}, "local_llm": {...}}`` 的嵌套字典；
                - ``ConfigManager`` 实例（具备 ``get(section, key, default)`` 接口）。
        """
        #: 嵌入模型配置意向（标识/文件名，非已加载实例）
        self._emb_model_name = self._read_cfg(config, "embedding", "model", DEFAULT_EMBEDDING_MODEL)
        #: 本地小 LLM 是否启用
        self._llm_enabled = bool(self._read_cfg(config, "local_llm", "enabled", False))
        #: 本地小 LLM 模型文件路径
        self._llm_path = self._read_cfg(config, "local_llm", "model_path", "") or ""
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
        #: 已加载的本地小 LLM 实例（Llama），未加载为 None
        self._llm = None
        #: 本地小 LLM 是否就绪
        self._llm_ready = False
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
        """加载 qwen3-embedding 嵌入模型。

        Args:
            path: GGUF 模型文件绝对路径。
        Returns:
            bool: 成功加载返回 True；文件缺失 / 加载异常返回 False（进程不崩溃，
                置 ``_emb_ready=False`` 并在 ``warnings`` 记录降级提示）。
        Raises:
            RuntimeError: 当 llama-cpp-python 未安装（导入失败）时抛出，
                提示先执行 pip install llama-cpp-python。
        """
        if not os.path.exists(path):
            return self._record_load_failure("emb", f"嵌入模型文件不存在：{path}（配置意向：{self._emb_model_name}）")
        llama_cls = _import_llama()  # 导入失败抛 RuntimeError（提示安装）
        try:
            self._emb_model = llama_cls(
                model_path=path, embedding=True, n_gpu_layers=self._emb_n_gpu_layers
            )
        except Exception as exc:  # noqa: BLE001 - 加载异常按降级处理，不崩溃
            self._emb_model = None
            return self._record_load_failure("emb", f"嵌入模型加载失败：{exc}")
        self._emb_ready = True
        return True

    def load_local_llm(self, path) -> bool:
        """加载本地小 LLM（embedding=False，n_ctx 建议 2048）。

        Args:
            path: GGUF 模型文件绝对路径。
        Returns:
            bool: 成功加载返回 True；文件缺失 / 加载异常返回 False（不崩溃，
                置 ``_llm_ready=False`` 并在 ``warnings`` 记录降级提示）。
        Raises:
            RuntimeError: 当 llama-cpp-python 未安装（导入失败）时抛出。
        """
        if not os.path.exists(path):
            return self._record_load_failure("llm", f"本地小 LLM 文件不存在：{path}（配置意向：{self._llm_path}）")
        llama_cls = _import_llama()  # 导入失败抛 RuntimeError（提示安装）
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
    # 嵌入（EmbeddingProvider 语义）                                     #
    # ------------------------------------------------------------------ #

    def embed(self, texts: list) -> list:
        """批量文本嵌入（记忆检索用）。

        Args:
            texts: 文本列表（list[str]）。
        Returns:
            list[list[float]]: 与输入等长的向量列表，维度固定（由模型决定）。
        Raises:
            LlamaNotReady: 嵌入模型未就绪时抛出。
        """
        if not self._emb_ready or self._emb_model is None:
            raise LlamaNotReady("嵌入模型未就绪：请先调用 load_embedding_model 加载模型后再进行文本嵌入。")
        result = self._emb_model.create_embedding(input=list(texts))
        return self._extract_embeddings(result)

    def embed_texts(self, texts: list) -> list:
        """embed 的显式别名，便于调用方按语义命名。"""
        return self.embed(texts)

    # ------------------------------------------------------------------ #
    # 本地小 LLM：判定 + 离线兜底                                        #
    # ------------------------------------------------------------------ #

    def _max_prompt_chars(self, max_tokens):
        """按 4 chars≈1token 估算的 prompt 字符预算。

        预算公式：``(n_ctx - max_tokens - 64余量) * 4``；保证至少留出可用空间。
        """
        usable = int(self._n_ctx) - int(max_tokens) - N_CTX_RESERVE_TOKENS
        return max(int(usable), 1) * _CHARS_PER_TOKEN

    def _fit_prompt(self, prompt, max_tokens, messages=None):
        """投递前的 n_ctx 溢出防护（M11）。

        规则：
        - 按 ``4 chars ≈ 1 token`` 估算，预算 = ``(n_ctx - max_tokens - 64余量) * 4``；
        - 未超限原样返回；
        - 超限且提供 ``messages``：按**消息列表**从最旧侧删减重建
          （保底全部 system 行 + 最近一轮），重建后仍未落地则继续行级兜底；
        - 行级兜底：拆行后保护头部连续 ``system`` 行，从最旧侧删减、最近一行恒留，
          重算仍超限（单条/system 即爆）则硬截断 prompt 尾部到预算内。

        :param prompt: 待投递的提示词文本
        :param max_tokens: 本次生成的最大 token 数（用于预算计算）
        :param messages: 生成 ``prompt`` 的原始消息列表（可选）；提供时启用消息级删减
        :return: 裁剪后的提示词（长度 <= 预算）
        """
        prompt = str(prompt)
        limit = self._max_prompt_chars(max_tokens)
        if len(prompt) <= limit:
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
            if len(rebuilt) <= limit:
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
        while len(body) > 1 and len("\n".join(header + body)) > limit:
            body.pop(0)  # 从最旧侧删减；body[-1]（最近一轮）恒保留
        fitted = "\n".join(header + body)
        if len(fitted) > limit:
            fitted = fitted[:limit]  # 硬截断尾部兜底
        return fitted

    def judge_should_reply(self, user_text) -> bool:
        """本地小 LLM 判定：用户是否在对本助手说话、是否应当回复。

        使用中文提示词（见 ``JUDGE_PROMPT_TEMPLATE``）要求模型只答 是/否，
        解析结果首词/首字（是/yes/y/true）命中即判为 True。

        Args:
            user_text: 用户刚说的话。
        Returns:
            bool: True 表示应当回复；False 表示不回复（自言自语）。
        Raises:
            LlamaNotReady: 本地小 LLM 未就绪时抛出。
        """
        if not self._llm_ready or self._llm is None:
            raise LlamaNotReady("本地小 LLM 未就绪：请先调用 load_local_llm 加载模型后再进行「是否回复」判定。")
        prompt = JUDGE_PROMPT_TEMPLATE.format(user_text=str(user_text).strip() or "（空输入）")
        prompt = self._fit_prompt(prompt, JUDGE_MAX_TOKENS)
        result = self._llm(prompt, max_tokens=JUDGE_MAX_TOKENS, temperature=0.0)
        return self._parse_yes(self._extract_text(result))

    def offline_chat(self, messages) -> str:
        """断网兜底：本地小 LLM 按 system + 最近消息生成回复。

        Args:
            messages: list[dict]，形如 ``[{"role": "system", "content": ...},
                {"role": "user", "content": ...}]``。
        Returns:
            str: 本地小 LLM 生成的纯文本回复。
        Raises:
            LlamaNotReady: 本地小 LLM 未就绪时抛出。
        """
        if not self._llm_ready or self._llm is None:
            raise LlamaNotReady("本地小 LLM 未就绪：请先调用 load_local_llm 加载模型后再进行离线对话。")
        prompt = self._fit_prompt(
            self._format_messages(messages), OFFLINE_CHAT_MAX_TOKENS, messages=messages
        )
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