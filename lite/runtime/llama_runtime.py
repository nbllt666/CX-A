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
            self._emb_model = llama_cls(model_path=path, embedding=True)
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
            self._llm = llama_cls(model_path=path, embedding=False, n_ctx=DEFAULT_LLM_N_CTX)
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
        result = self._llm(prompt, max_tokens=8, temperature=0.0)
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
        prompt = self._format_messages(messages)
        result = self._llm(prompt, max_tokens=128)
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