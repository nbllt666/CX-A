# -*- coding: utf-8 -*-
"""llama-server.exe 常驻子进程 + /v1/embeddings HTTP 客户端（主进程 stdlib-only）。

背景（2026-09-26）：主后端是 PyInstaller 冻结产物，只能跑标准库（无 numpy、
无 llama-cpp-python）。因此记忆检索的真实语义嵌入改走随包分发的预编译二进制
``<root>/runtime/llama/llama-server.exe``——主进程拉起其常驻子进程，经本机 HTTP
``/v1/embeddings`` 取回 1024 维向量。本轮实测口径：冷启动约 3s、单条嵌入 0.14s、
批量 32 条 1.55s。

设计要点：
- **纯标准库**：仅依赖 ``urllib.request`` / ``subprocess`` / ``socket`` / ``json``
  / ``os`` / ``sys`` / ``time`` / ``threading``，冻结后端可直接导入。
- **常驻 + 幂等**：模型冷加载昂贵，故子进程常驻复用；``ensure_started`` 幂等，
  进程存活时直接返回。
- **可注入替身**：``popen_factory`` / ``http_factory`` 为测试注入点，单测无需
  拉起真实进程、亦无需触网即可覆盖全部失败路径。
- **长驻进程必须 DEVNULL**：子进程 stdout / stderr 走 ``subprocess.DEVNULL``，
  不得用 PIPE——管道写满会阻塞服务器。
- **并发安全**：``ensure_started`` / ``embed`` / ``close`` 由类内互斥锁串行化。
- **输入截断与失败分流**（20260926 深挖 + 四审加固）：超长文本按 token 预算（
  :data:`MAX_INPUT_TOKENS`）**首尾各取一半**截断后送入，且预算收敛以服务端
  ``POST /tokenize`` **真实分词**为权威口径（逐次复核，严格 <= 预算；端点不可用时回退
  字符启发式近似）——既不再触发服务端 400 与"整批预热连锁失败"，也不会像纯启发式那样
  低估重复/生僻字符而漏截；HTTP 4xx 业务性错误不重启服务，仅传输级失败 / 5xx 才重启并重试一次。

接口契约（主线接线按此调用，不得改名/改签名）::

    LlamaServerEmbedder(exe_path, model_path, n_gpu_layers=0, n_ctx=2048,
                        host="127.0.0.1", ready_timeout=120.0, request_timeout=120.0,
                        popen_factory=None, http_factory=None)
        .ensure_started() -> None
        .embed(texts: list) -> list[list[float]]
        .dim(probe_text="ping") -> int
        .running -> bool
        .close() -> None

    LlamaServerChat(exe_path, model_path, n_gpu_layers=0, n_ctx=2048,
                    host="127.0.0.1", ready_timeout=300.0, request_timeout=120.0,
                    popen_factory=None, http_factory=None)
        .ensure_started() -> None
        .chat(messages: list, max_tokens=128, temperature=0.7, seed=None, timeout=None) -> str
        .running -> bool
        .close() -> None
"""

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

# 原生日志记录器（20260926 深挖：超长输入截断需留痕，避免"静默改变送入语义"）
LOGGER = logging.getLogger(__name__)

#: 就绪轮询间隔（秒）——/health 未就绪时按此节奏重试
READY_POLL_INTERVAL_S = 0.25

#: terminate 后等待子进程退出的上限（秒），超时再兜底 kill
TERMINATE_WAIT_S = 5.0

#: HTTP 错误响应片段在异常消息中的最大保留字数（超出尾部以「…」省略）
SNIPPET_LIMIT = 200

#: 单条文本送入嵌入服务的 token 估算上限（服务端上下文默认 2048，留安全余量）。
#: 20260926 深挖实测：7624 字中文（约 4582 token）触发服务端 HTTP 400
#: ``exceed_context_size_error``——该条记忆静默失去向量，且会连锁波及整批预热
#: （一批 32 条全失败 → 预热中断 → 数以百计的记忆零向量）。故超限文本按预算
#: 截断后再送入（仅影响该条向量的语义覆盖范围，优于"整条无向量"）。
MAX_INPUT_TOKENS = 1800

#: 每字符 token 折算（**仅作回退的近似口径**；权威口径为服务端 ``/tokenize`` 真实分词）：
#: 参考量级（真实 Qwen3 分词器实测样本）：自然中文 ≈0.6~0.65 / ASCII 自然文本 ≈0.21 /
#: 重复字·生僻字 CJK 1.0~2.0 / 随机可打印 ASCII ≈0.76——字符启发式无法给出可靠上界，
#: 故本表为近似取值：emoji 取 3.0（保守高估；实测约 1.0~1.4）；CJK 扩展 B~H 取 3.0
#: （实测约 3.0~4.0，扩展 G 更高、存在低估）——仅当 ``/tokenize`` 不可用时回退使用本表（近似、非保证）。
_TOKENS_PER_CJK_CHAR = 0.75
_TOKENS_PER_ASCII_CHAR = 0.4
_TOKENS_PER_WIDE_CHAR = 1.0
_TOKENS_PER_OTHER_CHAR = 3.0

#: 截断片段间的分隔标记（首尾采样用；其自身成本计入预算）
CLIP_SEPARATOR = "\n…\n"

#: ``/tokenize`` 探测单次超时（秒）——本机回环极轻调用（毫秒级）；失败即回退启发式
TOKENIZE_TIMEOUT_S = 30

#: chat 服务就绪等待上限（秒）——8B Q4 GPU 实测冷加载 5.3~5.5s，放宽覆盖慢盘 / CPU 加载
CHAT_READY_TIMEOUT_S = 300

#: chat 单次请求超时（秒）——常驻服务已加载模型，仅需覆盖生成本身（慢机留足余量）
CHAT_REQUEST_TIMEOUT_S = 120

# ------------------------------------------------------------------ #
# 后端目录解析（20261002 批 A：Vulkan 构建路径选择）                    #
# ------------------------------------------------------------------ #

#: 默认 llama.cpp 预编译二进制目录（相对便携根）：CUDA 构建与 CPU 兜底共用。
DEFAULT_LLAMA_DIR_REL = ("runtime", "llama")

#: Vulkan 后端 llama.cpp 预编译二进制目录（相对便携根）：AMD/Intel 独显与核显
#: 机器走该构建（accel_plan ``local_llm.backend`` / ``embedding.backend`` =
#: ``"vulkan"`` 时经 :func:`resolve_llama_dir` 选用）。
VULKAN_LLAMA_DIR_REL = ("runtime", "llama_vulkan")

#: llama-server 可执行文件名（两目录同名，仅构建后端不同）。
LLAMA_SERVER_EXE_NAME = "llama-server.exe"


def resolve_llama_dir(root, backend="") -> str:
    """按后端意图解析 llama.cpp 预编译二进制目录（stdlib-only，唯一选择口径）。

    规则（20261002 批 A；同日补充 N 卡 Vulkan 兜底）：

    - ``backend == "vulkan"`` → ``<root>/runtime/llama_vulkan``；该目录下
      ``llama-server.exe`` **不存在**时回退默认目录 ``<root>/runtime/llama``
      并输出中文日志告警（Vulkan 组件未随包分发时的兜底，保持可用性优先）；
    - ``backend == "cuda"`` → 默认目录 ``<root>/runtime/llama``；该目录下
      ``llama-server.exe`` **不存在**时回退 Vulkan 目录（Vulkan 为跨厂商后端、
      NVIDIA 亦可运行——CUDA 构建缺失时的 GPU 兜底，优于直接落 CPU）并输出
      中文日志告警；Vulkan 目录同样缺失时返回默认目录（交由上层报中文错误）；
    - 其余（``""`` / 未知值）→ 默认目录 ``<root>/runtime/llama``
      （与历史行为逐字等价，不做兜底探测）。

    :param root: 便携根绝对路径。
    :param backend: 后端意图（``"cuda"`` / ``"vulkan"`` / ``""``；大小写与
        首尾空白不敏感，非法值按默认目录处理）。
    :return: llama.cpp 二进制目录绝对路径（str）。
    """
    root_str = str(root or "")
    base_dir = os.path.join(root_str, *DEFAULT_LLAMA_DIR_REL)
    normalized = str(backend or "").strip().lower()
    if normalized == "cuda":
        # Vulkan 为跨厂商后端（NVIDIA 亦可运行）：CUDA 构建缺失时优先兜底
        # Vulkan 目录（GPU 路径），两者皆缺才返回默认目录（上层报中文错误）
        if os.path.isfile(os.path.join(base_dir, LLAMA_SERVER_EXE_NAME)):
            return base_dir
        vulkan_dir = os.path.join(root_str, *VULKAN_LLAMA_DIR_REL)
        if os.path.isfile(os.path.join(vulkan_dir, LLAMA_SERVER_EXE_NAME)):
            LOGGER.warning(
                "默认 llama.cpp 目录缺失 %s（期望：%s），回退 Vulkan 构建（NVIDIA 亦可运行）：%s",
                LLAMA_SERVER_EXE_NAME, base_dir, vulkan_dir,
            )
            return vulkan_dir
        return base_dir
    if normalized != "vulkan":
        return base_dir
    vulkan_dir = os.path.join(root_str, *VULKAN_LLAMA_DIR_REL)
    if os.path.isfile(os.path.join(vulkan_dir, LLAMA_SERVER_EXE_NAME)):
        return vulkan_dir
    LOGGER.warning(
        "Vulkan 运行时目录缺失或不含 %s（期望：%s），回退默认 llama.cpp 目录：%s",
        LLAMA_SERVER_EXE_NAME, vulkan_dir, base_dir,
    )
    return base_dir


def _char_cost(ch):
    """单字符 token 折算（**回退近似**口径，见上方常量说明）。"""
    code = ord(ch)
    if code < 128:
        return _TOKENS_PER_ASCII_CHAR
    if (
        (0x4E00 <= code <= 0x9FFF)        # CJK 统一表意（基本区）
        or (0x3400 <= code <= 0x4DBF)     # 扩展 A
        or (0xF900 <= code <= 0xFAFF)     # 兼容表意
    ):
        return _TOKENS_PER_CJK_CHAR
    if (
        (0x3000 <= code <= 0x303F)        # CJK 标点
        or (0xFF00 <= code <= 0xFFEF)     # 全角/半角形式
        or (0x3040 <= code <= 0x30FF)     # 日文假名（平假名 / 片假名）
        or (0x1100 <= code <= 0x11FF)     # 谚文字母
        or (0xAC00 <= code <= 0xD7AF)     # 谚文音节
        or (0x3130 <= code <= 0x318F)     # 谚文兼容字母
    ):
        return _TOKENS_PER_WIDE_CHAR
    return _TOKENS_PER_OTHER_CHAR


def _estimate_tokens(text):
    """文本 token 估算（逐字符折算求和；仅用于预算截断，不追求精确）。

    :param text: 待估算文本（非 str 先 ``str()`` 化）
    :return: token 估算值（float）
    """
    return sum(_char_cost(ch) for ch in str(text or ""))


def _clip_head(text, budget):
    """从头部按预算截取片段（返回片段本身的估算 <= budget）。"""
    cost = 0.0
    for index, ch in enumerate(text):
        cost += _char_cost(ch)
        if cost > budget:
            return text[:index]
    return text


def _clip_tail(text, budget):
    """从尾部按预算截取片段（GN-004 W-2：截断需保留文末信息）。"""
    cost = 0.0
    for index in range(len(text) - 1, -1, -1):
        cost += _char_cost(text[index])
        if cost > budget:
            return text[index + 1:]
    return text


def _clip_to_token_budget(text, budget=MAX_INPUT_TOKENS):
    """把文本截断到 token 预算内（**首尾各取一半**，保留文末关键信息）。

    GN-004 复审 W-2：旧实现仅保留头部，尾部关键信息（如文末"关键锚点"）不进向量；
    现改为 head + 分隔标记 + tail 采样，两段各占约一半预算（分隔标记成本计入预算）。
    极端混排导致首尾片段重叠/合计超预算时，回退为头部截断兜底。

    :param text: 待截断文本
    :param budget: token 估算预算上限
    :return: 截断后文本（估算值 <= 预算；未超限原样返回）
    """
    text = str(text or "")
    if _estimate_tokens(text) <= budget:
        return text
    separator_cost = _estimate_tokens(CLIP_SEPARATOR)
    half = max(1.0, (budget - separator_cost) / 2.0)
    head = _clip_head(text, half)
    tail = _clip_tail(text, half)
    clipped = head + CLIP_SEPARATOR + tail
    if _estimate_tokens(clipped) > budget:
        # 极端混排（首尾片段重叠且合计超预算）：头部截断兜底（严格 <= budget）
        return _clip_head(text, budget)
    return clipped


def _clip_text(text, limit=SNIPPET_LIMIT):
    """把文本截断到 ``limit`` 字以内，超出部分以「…」提示省略。

    :param text: 任意对象（先 ``str()`` 化）
    :param limit: 保留的最大字数
    :return: 截断后的字符串（长度 <= limit + 1）
    """
    text = str(text)
    return text if len(text) <= limit else text[:limit] + "…"


def _clip_bytes(body, limit=SNIPPET_LIMIT):
    """把响应体（bytes）解码为可读文本片段并截断（解码失败回退 repr）。"""
    if isinstance(body, (bytes, bytearray)):
        try:
            text = bytes(body).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 极端输入解码失败按 repr 兜底
            text = repr(body)
    else:
        text = str(body)
    return _clip_text(text, limit)


def _free_port(host="127.0.0.1"):
    """经 ``socket.bind(("127.0.0.1", 0))`` 取一个当前空闲端口后关闭套接字。

    仅用于探测可用端口（不监听、不触网），随后把端口号透传给 llama-server。

    :param host: 绑定地址（与本机回环一致，默认 127.0.0.1）
    :return: 空闲端口号（int）
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _default_popen(argv):
    """默认子进程工厂：DEVNULL 吞掉 stdout / stderr（长驻进程不得用 PIPE）。

    :param argv: 启动命令行参数列表
    :return: ``subprocess.Popen`` 实例
    """
    creationflags = 0x08000000 if sys.platform == "win32" else 0
    return subprocess.Popen(
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )


def _default_http(url, payload, timeout):
    """默认 HTTP 客户端：基于 ``urllib.request``，返回 ``(状态码, 响应体)``。

    - ``payload`` 为 None 时发 GET，否则发 POST（body 已编码为 bytes）；
    - 非 2xx 由 ``urllib.error.HTTPError`` 承载——此处**不抛出**，转为返回
      其 code 与 body，交由调用方按业务判定；
    - 连接类错误（``URLError`` / ``OSError`` / 超时）**直接抛出**，由调用方
      归为失败（用于触发重启重试或就绪轮询中的失败判定）。

    :param url: 完整请求 URL
    :param payload: 已编码的请求体 bytes（GET 传 None）
    :param timeout: 超时秒数
    :return: ``(status_code: int, body_bytes: bytes)``
    """
    headers = {}
    method = "GET"
    if payload is not None:
        method = "POST"
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            status = getattr(resp, "status", None)
            if status is None:
                status = resp.getcode()
            return int(status), resp.read()
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except Exception:  # noqa: BLE001 - 错误体读取失败按空处理
            body = b""
        return int(exc.code), body


class _ServerRequestError(RuntimeError):
    """llama-server 请求失败（携带"是否值得重启服务重试"标记；嵌入/chat 共用）。

    ``retriable=False`` 表示业务性错误（4xx：输入非法 / 超上下文等）——重启
    llama-server 无益。20260926 深挖实测：一条超长文本触发的 HTTP 400 曾导致
    健康服务被无谓重启（约 3~5s 抖动 + 整批预热失败），故按错误类别分流。
    """

    def __init__(self, message, retriable=True):
        """构造异常；``retriable`` 缺省 True（传输级/服务端级失败）。"""
        super().__init__(message)
        self.retriable = bool(retriable)


class _LlamaServerProcess:
    """llama-server.exe 常驻子进程的公共生命周期（嵌入器 / chat 客户端共用）。

    流程（两处一致，仅启动 argv 不同——由子类 :meth:`_build_argv` 提供）：
    校验 exe / model 文件存在 → 取空闲端口 → 拉起子进程（DEVNULL 吞输出）→
    轮询 ``GET /health``（间隔 0.25s，上限 ``ready_timeout``）直至 200 且
    body JSON ``status == "ok"``。失败均清理子进程引用并抛中文 RuntimeError。

    注入点（``popen_factory`` / ``http_factory``）与生命周期语义见
    :class:`LlamaServerEmbedder` 的 Args 说明（两子类同口径）。
    """

    def __init__(self, exe_path, model_path, n_gpu_layers=0, n_ctx=2048,
                 host="127.0.0.1", ready_timeout=120.0, request_timeout=120.0,
                 popen_factory=None, http_factory=None):
        """初始化（不启动任何进程，懒启动由 ``ensure_started`` 触发）。"""
        #: llama-server.exe 绝对路径
        self._exe_path = str(exe_path)
        #: 模型 GGUF 绝对路径
        self._model_path = str(model_path)
        #: GPU 卸载层数
        self._n_gpu_layers = int(n_gpu_layers)
        #: 上下文窗口
        self._n_ctx = int(n_ctx)
        #: 监听地址
        self._host = str(host)
        #: 就绪轮询上限（秒）
        self._ready_timeout = float(ready_timeout)
        #: 单次请求超时（秒）
        self._request_timeout = float(request_timeout)
        #: 子进程工厂（注入点）
        self._popen_factory = popen_factory or _default_popen
        #: HTTP 客户端（注入点）
        self._http_factory = http_factory or _default_http
        #: 并发互斥锁（串行化启动 / 请求 / 关闭）
        self._lock = threading.Lock()
        #: 常驻子进程句柄（未启动为 None）
        self._proc = None
        #: 当前子进程监听端口（未启动为 None）
        self._port = None
        #: 子进程是否已完成 /health 就绪确认
        self._ready = False

    def _build_argv(self, port):
        """构造启动 argv（子类实现；``port`` 为已探测的空闲端口）。"""
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    # 生命周期：启动 / 关闭                                                #
    # ------------------------------------------------------------------ #

    def ensure_started(self):
        """确保常驻子进程已就绪（幂等：进程存活且已就绪直接返回）。

        Raises:
            RuntimeError: exe / model 文件缺失；子进程启动过程中退出（附
                returncode）；就绪超时。失败均会清理子进程引用。
        """
        with self._lock:
            self._ensure_started_locked()

    def _ensure_started_locked(self):
        """``ensure_started`` 的无锁实现（供持有互斥锁的调用方复用）。"""
        if self._ready and self._proc is not None and self._proc.poll() is None:
            return  # 幂等：进程存活直接返回

        if not os.path.isfile(self._exe_path):
            raise RuntimeError(f"llama-server 可执行文件不存在：{self._exe_path}")
        if not os.path.isfile(self._model_path):
            raise RuntimeError(f"模型文件不存在：{self._model_path}")

        if self._proc is not None:
            self._stop_process()  # 清理残留（已退或未就绪）引用

        port = _free_port(self._host)
        argv = self._build_argv(port)
        try:
            proc = self._popen_factory(argv)
        except Exception as exc:  # noqa: BLE001 - 拉起失败转为中文错误
            raise RuntimeError(f"启动 llama-server 子进程失败：{exc}") from exc

        self._proc = proc
        self._port = port
        self._ready = False

        deadline = time.time() + self._ready_timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                code = proc.returncode
                self._stop_process()
                raise RuntimeError(f"llama-server 启动过程中退出（returncode={code}）。")
            if self._health_ok():
                self._ready = True
                return
            time.sleep(READY_POLL_INTERVAL_S)

        self._stop_process()
        raise RuntimeError(
            f"llama-server 就绪超时（{self._ready_timeout}s 内 /health 未返回 ok）。"
        )

    def _health_ok(self):
        """请求 ``GET /health``，200 且 body JSON ``status == "ok"`` 判定为就绪。

        任何连接异常 / 非 200 / 非法 JSON / 状态非 ok 均返回 False（不抛出）。
        """
        url = f"http://{self._host}:{self._port}/health"
        try:
            status, body = self._http_factory(url, None, self._request_timeout)
        except Exception:  # noqa: BLE001 - 连接未通属正常未就绪态
            return False
        if int(status) != 200:
            return False
        try:
            data = json.loads(_decode_body(body))
        except Exception:  # noqa: BLE001 - 非法 JSON 视为未就绪
            return False
        return isinstance(data, dict) and data.get("status") == "ok"

    def _stop_process(self):
        """终止常驻子进程并清空引用（``close`` 复用）。

        terminate → ``wait(5)`` → 兜底 kill；进程已退则仅清理引用。全程不抛错。
        """
        proc = self._proc
        self._proc = None
        self._port = None
        self._ready = False
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=TERMINATE_WAIT_S)
                except Exception:  # noqa: BLE001 - 未按时退出则兜底 kill
                    try:
                        proc.kill()
                        proc.wait(timeout=TERMINATE_WAIT_S)
                    except Exception:  # noqa: BLE001 - 清引用后不再追究
                        pass
        except Exception:  # noqa: BLE001 - 重入 / 句柄异常一律吞掉
            pass

    def close(self):
        """关闭常驻子进程（幂等：未启动或已退出均安全，不抛错）。"""
        with self._lock:
            self._stop_process()

    @property
    def running(self):
        """常驻子进程当前是否存活（bool）。"""
        with self._lock:
            proc = self._proc
            return proc is not None and proc.poll() is None


class LlamaServerEmbedder(_LlamaServerProcess):
    """llama-server.exe 常驻子进程 + /v1/embeddings HTTP 客户端（主进程 stdlib-only）。

    实测口径（2026-09-26）：``-m <gguf> --embeddings --pooling last --host 127.0.0.1
    --port <随机空闲端口> -c 2048 -ngl 0 --no-webui``；``/health`` 返回
    ``{"status":"ok"}`` 即就绪；``POST /v1/embeddings {"input":[...]}`` 返回
    ``{"data":[{"embedding":[...]},...]}``。

    Args:
        exe_path: llama-server.exe 绝对路径（随包分发于 ``runtime/llama/``）。
        model_path: 嵌入模型 GGUF 绝对路径。
        n_gpu_layers: GPU 卸载层数（默认 0 = 纯 CPU）。
        n_ctx: 上下文窗口大小（默认 2048）。
        host: 本机回环地址（默认 127.0.0.1）。
        ready_timeout: 冷启动就绪轮询上限（秒，默认 120）。
        request_timeout: 单次 HTTP 请求超时（秒，默认 120）。
        popen_factory: 子进程工厂注入点 ``(argv) -> proc``；默认
            :func:`_default_popen`。替身需具备 ``.poll()`` / ``.terminate()``
            / ``.kill()`` / ``.wait(timeout=...)`` / ``.returncode``。
        http_factory: HTTP 注入点 ``(url, payload_bytes_or_None, timeout)
            -> (status_code, body_bytes)``；默认 :func:`_default_http`。
    """

    def __init__(self, exe_path, model_path, n_gpu_layers=0, n_ctx=2048,
                 host="127.0.0.1", ready_timeout=120.0, request_timeout=120.0,
                 popen_factory=None, http_factory=None):
        """初始化嵌入器（不启动任何进程，懒启动由 ``ensure_started`` 触发）。"""
        super().__init__(
            exe_path, model_path, n_gpu_layers=n_gpu_layers, n_ctx=n_ctx,
            host=host, ready_timeout=ready_timeout, request_timeout=request_timeout,
            popen_factory=popen_factory, http_factory=http_factory,
        )
        #: 超长输入截断告警去重标记（首次截断时告警一次，避免逐请求刷屏）
        self._clip_warned = False

    def _build_argv(self, port):
        """嵌入服务启动 argv（``--embeddings --pooling last`` 为嵌入专用开关）。"""
        return [
            self._exe_path, "-m", self._model_path, "--embeddings",
            "--pooling", "last", "--host", self._host, "--port", str(port),
            "-c", str(self._n_ctx), "-ngl", str(self._n_gpu_layers), "--no-webui",
        ]

    # ------------------------------------------------------------------ #
    # 嵌入                                                                #
    # ------------------------------------------------------------------ #

    def embed(self, texts: list) -> list:
        """批量文本嵌入（记忆检索用）。

        送入前按 :data:`MAX_INPUT_TOKENS` 逐条截断（超长文本不再触发服务端 400）；
        失败分流（20260926 深挖）：
        - **业务性错误**（HTTP 4xx：输入非法 / 超上下文等）→ 直接抛中文 ``RuntimeError``
          （重启服务无益，不再抖动健康服务）；
        - **传输级 / 服务端级失败**（连接异常 / 超时 / 5xx / 响应非法）→ ``_stop_process()``
          + ``ensure_started()`` 重启并**重试一次**；仍失败抛中文 ``RuntimeError``。

        Args:
            texts: 非空文本列表（list[str]）。
        Returns:
            list[list[float]]: 与输入等长的向量列表（逐条转为 float）。
        Raises:
            RuntimeError: 输入非法、业务性错误、或重启重试后仍失败。
        """
        if not isinstance(texts, list):
            raise RuntimeError("embed 的 texts 必须为 list。")
        if not texts:
            raise RuntimeError("embed 的 texts 不能为空列表。")

        # 送入前预算收敛：优先真实分词（/tokenize 权威口径），不可用时回退启发式近似
        fitted = []
        used_real_tokenizer = False
        for text in texts:
            out, real = self._fit_text(text)
            fitted.append(out)
            used_real_tokenizer = used_real_tokenizer or real
        if not self._clip_warned and any(
            len(fitted[i]) != len(str(texts[i])) for i in range(len(texts))
        ):
            self._clip_warned = True
            LOGGER.warning(
                "存在超长文本（超 %d token 预算），已按%s截断后嵌入"
                "（保留首尾、仅影响该条向量的语义覆盖范围，不会整条失去向量）",
                MAX_INPUT_TOKENS,
                "真实分词（/tokenize）" if used_real_tokenizer else "启发式估算（分词端点不可用）",
            )

        with self._lock:
            self._ensure_started_locked()
            # 注意：不能沿用 ``except ... as first_exc`` 后直接引用——PEP 3110 规定
            # except 变量在离开处理块时即被删除（UnboundLocalError）。故显式回填。
            first_exc = None
            try:
                return self._embed_once(fitted)
            except _ServerRequestError as exc:
                if not exc.retriable:
                    # 业务性错误：重启 llama-server 无益，直接上抛（不产生服务抖动）
                    raise RuntimeError(str(exc)) from exc
                first_exc = exc
            except Exception as exc:  # noqa: BLE001 - 未知异常按可重试处理
                first_exc = exc
            # 传输级 / 服务端级失败：重启进程并重试一次
            self._stop_process()
            try:
                self._ensure_started_locked()
                return self._embed_once(fitted)
            except Exception as second_exc:  # noqa: BLE001 - 重试仍失败
                self._stop_process()
                raise RuntimeError(
                    "嵌入请求失败且已重启重试仍失败："
                    f"{_clip_text(second_exc)}（首次错误：{_clip_text(first_exc)}）"
                ) from second_exc

    def _token_count(self, text):
        """经 ``POST /tokenize`` 取真实 token 数；端点不可用 / 异常返回 None。

        20260926 四审 W-1~W-3：字符启发式无法给出可靠上界（重复/生僻 CJK 实测 1.0~2.0
        token/字符、随机 ASCII 0.76、扩展 B~H 达 3.0），故预算收敛以服务端真实分词为
        **权威口径**；本方法失败时调用方回退启发式近似。
        """
        url = f"http://{self._host}:{self._port}/tokenize"
        payload = json.dumps({"content": str(text)}).encode("utf-8")
        try:
            status, body = self._http_factory(url, payload, TOKENIZE_TIMEOUT_S)
            if int(status) != 200:
                return None
            data = json.loads(_decode_body(body))
        except Exception:  # noqa: BLE001 - 探测失败按"不可用"处理（回退启发式）
            return None
        tokens = data.get("tokens") if isinstance(data, dict) else None
        return len(tokens) if isinstance(tokens, list) else None

    def _take_prefix(self, text, want_tokens):
        """二分求"真实 token 数 <= want"的最长前缀；tokenize 不可用返回 None。"""
        low, high, best = 0, len(text), ""
        while low <= high:
            mid = (low + high) // 2
            count = self._token_count(text[:mid])
            if count is None:
                return None
            if count <= want_tokens:
                best = text[:mid]
                low = mid + 1
            else:
                high = mid - 1
        return best

    def _take_suffix(self, text, want_tokens):
        """二分求"真实 token 数 <= want"的最长后缀；tokenize 不可用返回 None。"""
        low, high, best = 0, len(text), ""
        while low <= high:
            mid = (low + high) // 2
            chunk = text[len(text) - mid:] if mid else ""
            count = self._token_count(chunk) if mid else 0
            if count is None:
                return None
            if count <= want_tokens:
                best = chunk
                low = mid + 1
            else:
                high = mid - 1
        return best

    def _fit_text(self, text):
        """把单条文本收敛到 token 预算内：优先真实分词，失败回退启发式近似。

        真实口径下（返回 ``(text, True)``）：首尾采样后的**真实 token 数必然 <= 预算**
        （每次拼接后都用 /tokenize 复核；迭代 4 轮仍超则仅保留头部兜底）。
        回退口径（返回 ``(text, False)``）：使用字符启发式近似（非保证，见常量注释）。

        :return: ``(送入文本, 是否使用真实分词口径)``
        """
        raw = str(text or "")
        count = self._token_count(raw)
        if count is None:
            return _clip_to_token_budget(raw), False
        if count <= MAX_INPUT_TOKENS:
            return raw, True
        sep_tokens = self._token_count(CLIP_SEPARATOR)
        if sep_tokens is None:
            return _clip_to_token_budget(raw), False
        # 两侧配额之和 = 预算 - 分隔标记成本（首轮）；超出时按超出量收缩两侧之和（收敛）
        total_want = max(2, MAX_INPUT_TOKENS - sep_tokens)
        for _ in range(4):
            head_want = max(1, total_want // 2)
            tail_want = max(1, total_want - head_want)
            head = self._take_prefix(raw, head_want)
            tail = self._take_suffix(raw, tail_want)
            if head is None or tail is None:
                return _clip_to_token_budget(raw), False
            clipped = head + CLIP_SEPARATOR + tail
            total = self._token_count(clipped)
            if total is None:
                return _clip_to_token_budget(raw), False
            if total <= MAX_INPUT_TOKENS:
                return clipped, True
            total_want = max(2, total_want - max(1, total - MAX_INPUT_TOKENS))
        # 收敛兜底：真实口径仅保留头部（严格 <= 预算）
        head = self._take_prefix(raw, MAX_INPUT_TOKENS)
        if head is None:
            return _clip_to_token_budget(raw), False
        return head, True

    def _embed_once(self, texts):
        """单次嵌入请求（不做重试）；失败抛 ``_ServerRequestError`` 供调用方分流。

        ``retriable`` 标记口径：HTTP 4xx（输入非法 / 超上下文等业务性错误）→ False；
        HTTP 5xx / 408 / 429 与响应结构异常 → True（重启服务可能恢复）。

        :param texts: 非空文本列表（调用方已完成 token 预算截断）
        :return: list[list[float]]，与输入等长
        """
        url = f"http://{self._host}:{self._port}/v1/embeddings"
        payload = json.dumps({"input": list(texts)}).encode("utf-8")
        status, body = self._http_factory(url, payload, self._request_timeout)
        if int(status) != 200:
            code = int(status)
            retriable = code >= 500 or code in (408, 429)
            raise _ServerRequestError(
                f"嵌入接口返回 HTTP {code}：{_clip_bytes(body)}", retriable=retriable
            )
        try:
            data = json.loads(_decode_body(body))
        except Exception as exc:  # noqa: BLE001 - 响应非 JSON 归为失败
            raise _ServerRequestError(f"嵌入响应非合法 JSON：{_clip_bytes(body)}") from exc

        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list) or not items:
            raise _ServerRequestError(f"嵌入响应缺少 data 条目：{_clip_bytes(body)}")

        parsed = []
        for pos, item in enumerate(items):
            if not isinstance(item, dict):
                raise _ServerRequestError(f"嵌入响应条目格式非法：{_clip_text(item)}")
            vector = item.get("embedding")
            if not isinstance(vector, list) or not vector:
                raise _ServerRequestError(f"嵌入响应存在空条目：{_clip_text(item)}")
            parsed.append((item.get("index"), [float(v) for v in vector]))

        # 各项均带 index 时按 index 排序保序；否则维持响应原顺序
        if all(index is not None for index, _vec in parsed):
            parsed.sort(key=lambda pair: pair[0])
        result = [vector for _index, vector in parsed]

        if len(result) != len(texts):
            raise _ServerRequestError(
                f"嵌入返回条数（{len(result)}）与输入条数（{len(texts)}）不一致。"
            )
        return result

    def dim(self, probe_text="ping") -> int:
        """经探针文本实测嵌入维度。

        :param probe_text: 探针文本（默认 "ping"）
        :return: 向量维度（int，实测为 1024）
        """
        return len(self.embed([probe_text])[0])


class LlamaServerChat(_LlamaServerProcess):
    """llama-server.exe 常驻子进程 + /v1/chat/completions 客户端（主进程 stdlib-only）。

    背景（20260930_模块0_本地LLM常驻推理改造）：本地小 LLM 原经语音桥 llama-cli
    单次子进程推理，**每次请求重载 5GB GGUF**（实测单次全流程 6.4~7.4s，其中生成
    仅 ~0.2~1s）。改为常驻 llama-server 后单次请求实测 **0.41~0.43s**。

    两个关键口径（均由 20260930 变体矩阵实测定案，脚本
    ``.trae/documents/test_reports/voice_no_think_20260930/server_variant_probe.py``）：
    - 请求走 ``/v1/chat/completions``（应用模型内嵌 chat template）；**同口径
      raw ``/completion`` 在 server 侧实测出现"复读提示词"异常，故本类只走 chat 端点**；
    - 以 ``chat_template_kwargs={"enable_thinking": false}`` 原生关闭 Qwen3 思考
      （实测：开启思考时 128 token 预算被思考吞掉、content 为空；关闭后直接作答）。

    Args:
        exe_path: llama-server.exe 绝对路径（随包分发于 ``runtime/llama/``）。
        model_path: 本地小 LLM GGUF 绝对路径。
        n_gpu_layers: GPU 卸载层数（默认 0 = 纯 CPU）。
        n_ctx: 上下文窗口大小（默认 2048）。
        host: 本机回环地址（默认 127.0.0.1）。
        ready_timeout: 冷启动就绪轮询上限（秒，默认 300——8B Q4 GPU 实测 5.3~5.5s，
            放宽覆盖慢盘 / CPU 加载）。
        request_timeout: 单次 HTTP 请求超时（秒，默认 120——常驻服务已加载模型，
            仅覆盖生成时间）。
        popen_factory / http_factory: 注入点（口径同 :class:`LlamaServerEmbedder`）。
    """

    def __init__(self, exe_path, model_path, n_gpu_layers=0, n_ctx=2048,
                 host="127.0.0.1", ready_timeout=CHAT_READY_TIMEOUT_S,
                 request_timeout=CHAT_REQUEST_TIMEOUT_S,
                 popen_factory=None, http_factory=None):
        """初始化 chat 客户端（不启动任何进程，懒启动由 ``ensure_started`` 触发）。"""
        super().__init__(
            exe_path, model_path, n_gpu_layers=n_gpu_layers, n_ctx=n_ctx,
            host=host, ready_timeout=ready_timeout, request_timeout=request_timeout,
            popen_factory=popen_factory, http_factory=http_factory,
        )

    def _build_argv(self, port):
        """chat 服务启动 argv（无嵌入专用开关；其余同嵌入服务口径）。"""
        return [
            self._exe_path, "-m", self._model_path,
            "--host", self._host, "--port", str(port),
            "-c", str(self._n_ctx), "-ngl", str(self._n_gpu_layers), "--no-webui",
        ]

    # ------------------------------------------------------------------ #
    # chat 补全                                                            #
    # ------------------------------------------------------------------ #

    def chat(self, messages, max_tokens=128, temperature=0.7, seed=None, timeout=None):
        """一次 chat 补全，返回助手回复文本。

        失败分流（与 :meth:`LlamaServerEmbedder.embed` 同口径）：
        - **业务性错误**（HTTP 4xx：消息非法 / 超上下文等）→ 直接抛中文
          ``RuntimeError``（重启服务无益）；
        - **传输级 / 服务端级失败**（连接异常 / 超时 / 5xx / 响应非法）→
          ``_stop_process()`` + ``ensure_started()`` 重启并**重试一次**。

        Args:
            messages: OpenAI 兼容消息列表（``[{"role", "content"}, ...]``，非空）。
            max_tokens: 生成长度上限（默认 128）。
            temperature: 采样温度（默认 0.7）。
            seed: 随机种子（None = 不传，由服务端默认；传入则固定复现）。
            timeout: 本次请求超时（秒）；缺省用构造时的 ``request_timeout``。
        Returns:
            str: 助手回复纯文本（可能为空串——由调用方按业务判定）。
        Raises:
            RuntimeError: 输入非法、业务性错误、或重启重试后仍失败（中文消息）。
        """
        if not isinstance(messages, list) or not messages:
            raise RuntimeError("chat 的 messages 必须为非空列表。")
        effective_timeout = self._request_timeout if timeout is None else timeout
        args = (list(messages), max_tokens, temperature, seed, effective_timeout)
        with self._lock:
            self._ensure_started_locked()
            # 注意：不能沿用 ``except ... as first_exc`` 后直接引用——PEP 3110 规定
            # except 变量在离开处理块时即被删除（UnboundLocalError）。故显式回填。
            first_exc = None
            try:
                return self._chat_once(*args)
            except _ServerRequestError as exc:
                if not exc.retriable:
                    # 业务性错误：重启 llama-server 无益，直接上抛（不产生服务抖动）
                    raise RuntimeError(str(exc)) from exc
                first_exc = exc
            except Exception as exc:  # noqa: BLE001 - 未知异常按可重试处理
                first_exc = exc
            # 传输级 / 服务端级失败：重启进程并重试一次
            self._stop_process()
            try:
                self._ensure_started_locked()
                return self._chat_once(*args)
            except Exception as second_exc:  # noqa: BLE001 - 重试仍失败
                self._stop_process()
                raise RuntimeError(
                    "chat 请求失败且已重启重试仍失败："
                    f"{_clip_text(second_exc)}（首次错误：{_clip_text(first_exc)}）"
                ) from second_exc

    def _chat_once(self, messages, max_tokens, temperature, seed, timeout):
        """单次 chat 补全请求（不做重试）；失败抛 ``_ServerRequestError`` 供调用方分流。

        ``retriable`` 标记口径：HTTP 4xx（业务性错误）→ False；
        HTTP 5xx / 408 / 429 与响应结构异常 → True（重启服务可能恢复）。

        :return: str 助手回复文本
        """
        url = f"http://{self._host}:{self._port}/v1/chat/completions"
        payload = {
            "messages": list(messages),
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
            # 原生关闭思考链（见类 docstring 实测依据）
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if seed is not None:
            payload["seed"] = int(seed)
        status, body = self._http_factory(
            url, json.dumps(payload).encode("utf-8"), timeout
        )
        if int(status) != 200:
            code = int(status)
            retriable = code >= 500 or code in (408, 429)
            raise _ServerRequestError(
                f"chat 接口返回 HTTP {code}：{_clip_bytes(body)}", retriable=retriable
            )
        try:
            data = json.loads(_decode_body(body))
        except Exception as exc:  # noqa: BLE001 - 响应非 JSON 归为失败
            raise _ServerRequestError(f"chat 响应非合法 JSON：{_clip_bytes(body)}") from exc
        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices:
            raise _ServerRequestError(f"chat 响应缺少 choices 条目：{_clip_bytes(body)}")
        first = choices[0]
        message = first.get("message") if isinstance(first, dict) else None
        if not isinstance(message, dict) or "content" not in message:
            raise _ServerRequestError(
                f"chat 响应缺少 choices[0].message.content：{_clip_bytes(body)}"
            )
        return str(message.get("content") or "")


def _decode_body(body):
    """把响应体 bytes 解码为 str（已为 str 则原样返回）。"""
    if isinstance(body, (bytes, bytearray)):
        return bytes(body).decode("utf-8")
    return str(body)