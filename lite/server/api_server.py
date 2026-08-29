# -*- coding: utf-8 -*-
"""轻量后端 REST API 服务（A9）——基于标准库 http.server 实现，无第三方重型框架依赖。

面向前端 MemoriesPage 提供记忆浏览与管理接口：
    GET    /api/health              健康检查 -> {"status": "ok"}
    GET    /api/memories            记忆列表（?type= &agent_id= &limit=）
    GET    /api/memories/search     记忆检索（?q= &agent_id= &top_k=，走 MemoryRetrievalPipeline）
    DELETE /api/memories/{id}       软删除一条记忆

电脑控制（Task D3，走 ToolBridge 全链路）：
    GET    /api/computer/status     授权状态（authorized / confirm_dangerous）
    POST   /api/computer/authorize  开启/撤销授权（body {enabled: bool}）
    POST   /api/computer/call       执行工具调用（body {tool, arguments}；未授权 403）

配置与管理 API（记录在 `.trae/documents/20260826_模块0_差异审查登记与处理计划.md`）：
    GET    /api/status              轻量系统状态（app / version / uptime）
    GET    /api/settings            用户可读配置视图（不含 API Key）
    PUT    /api/settings            更新可热更配置（白名单键；body 为补丁）
    POST   /api/chat/messages       聊天发送（未启用守卫：明确提示走前端 Mock）
    GET    /api/chat/history        聊天历史（未启用守卫：返回空列表 + 提示）

生产装配接线 API（批次E，记录在 `.trae/documents/20260828_模块0_生产装配接线.md`）：
    GET    /api/tools               内置工具清单（含 usage 端点用法自述）
    POST   /api/tools/call          调用内置工具（body {name, arguments}；未授权 403 / 未知工具 404）
    POST   /api/memory/distill      记忆蒸馏（body {messages, agent_id?}；未配置云端 400 / 云端离线 503）
    POST   /api/voice/synthesize    文本合成语音（body {text, voice?}；后端异常 503）
    POST   /api/voice/transcribe    语音转文本（body {audio_base64, sample_rate?}；后端异常 503）

> 管理面已收敛为纯 API：前端不再路由 Agents/Remote/Status，管理能力以上述端点
> + /api/agents、/api/remote/* 外露，供另一 Agent 或管理工具调用。

端口默认 8600（与前端 frontend/src/renderer/api.ts 的 API_PORT 约定一致）。
支持命令行参数：-h/--host、-p/--port；Ctrl+C 优雅退出。
所有 JSON 响应以 UTF-8 编码且 ensure_ascii=False，中文不做转义。

线程安全说明：本服务使用单线程 HTTPServer（一次只处理一个连接/请求），
MemoryStore 的 sqlite3 连接与 MemoryRetrievalPipeline 均在主处理线程内串行使用，
不引入并发读写，故无需加锁。若要切换到并发服务器，需另行处理存储连接竞争。
"""

import argparse
import base64
import hmac
import json
import logging
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

# 原生日志记录器（低-5：内部异常完整消息仅写日志，不外泄到响应体）
LOGGER = logging.getLogger(__name__)

# 聊天服务未启用守卫错误码（前端本期走 Mock 演示，端点存在但明确提示）
CHAT_SERVICE_DISABLED = "chat_service_disabled"

# ------------------------------------------------------------------ 启动令牌鉴权（N1）
# 环境变量 CXA_API_TOKEN 非空时，除 OPTIONS 预检与 GET /api/health 外的所有请求
# 必须携带匹配的 X-Client-Token 头（常量时间比较），否则回 403 unauthorized_client。
# Electron 生产态由 main.js 启动时生成随机令牌经 spawn env 注入本进程；env 未设置
# （纯浏览器 dev / 测试态）保持开放模式并告警一次。
_API_TOKEN = os.environ.get("CXA_API_TOKEN", "").strip()
#: 开放模式告警是否已发出（仅告警一次，避免刷屏）
_TOKEN_OPEN_MODE_WARNED = False

# 请求体大小上限（N6）：1MB，超出直接 413，防超大 body 阻塞单线程服务
_MAX_BODY_BYTES = 1048576

# 单次蒸馏请求的 messages 条数上限（H-5，第三轮体检批次2）：
# 超限 400——防单请求串行发起数百次云端 LLM 调用阻塞单线程服务
_MAX_DISTILL_MESSAGES = 200

# 语音合成文本长度上限（字符）（M-5）：超限 400——防超长文本分钟级合成阻塞服务
_MAX_SYNTH_TEXT_CHARS = 5000

# 记忆列表 limit / 检索 top_k 的允许上限（M-6）：负数 400、超上限钳制——
# 防 LIMIT -1 全表返回与超大整数触发 sqlite OverflowError
_MAX_LIST_LIMIT = 1000

# agent_id 最大长度（L-5）：入库字段限长，防任意长字符串写库
_MAX_AGENT_ID_CHARS = 100

# 记忆 id 合法范围（低-6，第四轮体检批次B）：SQLite INTEGER 为 64 位有符号，
# 范围外的 id 直接入库会触发 sqlite OverflowError → 500，边界处显式 400
_INT64_MIN = -(2 ** 63)
_INT64_MAX = 2 ** 63 - 1

# 回环监听地址集合（中-4 启动安全闸判定口径，第四轮体检批次B）
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _is_loopback_host(host) -> bool:
    """判断监听地址是否为本机回环地址（127.0.0.1 / localhost / ::1）。

    兼容 IPv6 字面量的方括号形态（[::1]）与大小写混写。
    """
    return str(host or "").strip().strip("[]").lower() in _LOOPBACK_HOSTS


def _env_api_token() -> str:
    """实时读取启动令牌 env（main 安全闸判定用，不依赖 import 期快照）。"""
    return os.environ.get("CXA_API_TOKEN", "").strip()


class _BodyTooLarge(Exception):
    """请求体超过 ``_MAX_BODY_BYTES`` 的内部信号（413 响应已由 _read_body_json 发出）。"""


# settings PUT 已知只读顶层键：GET 视图可见但不在 PUT 白名单的 section
# （收到时收集进 ignored 数组回显，消除静默丢弃——L2 收口）
_SETTINGS_READONLY_TOP_KEYS = ("acp", "remote", "vector")

# CSRF/CORS 加固：受信任的跨源 Origin 白名单。
# "null" 是 Electron 生产态（file:// 页面）发出的 Origin 字面量；
# 不使用通配符 *，未命中白名单的一律不返回 CORS 头（浏览器侧即被同源策略拦截）。
_ALLOWED_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173", "null")

# 防 DNS rebinding：Host 必须指向服务自身。生产默认端口 8600 精确列入白名单。
# 另放行"主机名为本机回环名、端口任意"的形态：测试起服绑定临时端口
# （HTTPServer(("127.0.0.1", 0))），urllib 自动发送 Host: 127.0.0.1:<随机端口>，
# 严格两值白名单会误伤；外部恶意域名（DNS rebinding 的真正攻击面）仍被拒绝。
_ALLOWED_HOSTS = ("127.0.0.1:8600", "localhost:8600")
_ALLOWED_HOST_NAMES = ("127.0.0.1", "localhost")

# 批次E：GET /api/tools 响应附带的端点用法自述（openapi 风格，供管理 Agent 自发现）
_TOOLS_USAGE = {
    "POST /api/tools/call": {
        "body": {"name": "工具 id（见 tools[].id）", "arguments": "工具参数 dict（可省略）"},
        "result": "200 {ok:true, result}；未授权/类别禁用 403 not_authorized；未知工具 404",
    },
    "POST /api/memory/distill": {
        "body": {"messages": "非空 [{role, content}, ...] 列表", "agent_id": "可选，默认 default"},
        "result": "200 {ok:true, sessions}；未配置云端 400 cloud_not_configured；云端离线 503 cloud_offline",
    },
    "POST /api/voice/synthesize": {
        "body": {"text": "待合成文本（非空）", "voice": "可选音色，默认 cx-open"},
        "result": "200 {ok:true, audio_base64, mime:'audio/wav'}；后端异常 503 voice_backend_unavailable",
    },
    "POST /api/voice/transcribe": {
        "body": {"audio_base64": "PCM 音频的 base64 编码", "sample_rate": "可选采样率（默认 16000，当前透传忽略）"},
        "result": "200 {ok:true, text}；解码失败 400；后端异常 503 voice_backend_unavailable",
    },
}

# ------------------------------------------------------------------ 路径推导
# lite/server/api_server.py -> lite/server -> lite -> 项目根目录（逐级上溯 3 次）
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_LITE_DIR = os.path.dirname(_THIS_DIR)
_PROJECT_ROOT = os.path.dirname(_LITE_DIR)

# 直接以脚本方式运行时，脚本目录会被加入 sys.path 而非项目根，故需手动补入项目根
# 才能以包路径 import lite.memory 下的既有模块（tests 侧以包方式 import 时幂等）
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from lite.memory.embedding import LiteEmbeddingProvider  # noqa: E402
from lite.memory.pipeline import MemoryRetrievalPipeline  # noqa: E402
from lite.memory.storage import MemoryStore  # noqa: E402
from lite.memory.vector_store import InMemoryVectorStore  # noqa: E402
from lite import __version__ as LITE_VERSION  # noqa: E402
from lite.management.local_agents import AgentManager, AgentNotFound  # noqa: E402
from lite.management.remote import (  # noqa: E402
    RemoteController,
    RemoteDisabled,
    RemoteError,
    RemoteUnreachable,
)
from lite.computer_control.control import (  # noqa: E402
    ComputerControl,
    NotAuthorizedError,
    PluginError,
)
from lite.computer_control.security import ControlAuthorizer  # noqa: E402
from lite.computer_control.tool_bridge import ToolBridge  # noqa: E402
from lite.config.config_manager import DEFAULTS, ConfigManager  # noqa: E402
from lite.cloud.adapter import PROVIDER_BASE_URLS  # noqa: E402
from lite.cloud.adapter import CloudAdapter, CloudConfigError  # noqa: E402
from lite.memory.distillation import DistillationPaused, MemoryDistiller  # noqa: E402
from lite.tools.builtin_registry import BuiltinToolRegistry  # noqa: E402
from lite.audio import LiteVoicePipeline, build_default_pipeline  # noqa: E402

# 云端 provider 白名单（L-8：从 adapter.PROVIDER_BASE_URLS 派生，单一真相源，
# 新增 provider 无需再同步本文件；置于 lite 包 import 之后——派生依赖其符号）
CLOUD_PROVIDER_ALLOWLIST = tuple(PROVIDER_BASE_URLS.keys())

# 默认数据目录：项目根目录下 data/（与 storage._default_db_path 的 data/memories.db 一致）
DEFAULT_DATA_DIR = os.path.join(_PROJECT_ROOT, "data")
# 默认监听端口（与前端 API_PORT 一致）
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8600


def _resolve_data_dir(data_dir=None) -> str:
    """归一化数据目录：未显式提供时回落项目根 data/。"""
    return data_dir or DEFAULT_DATA_DIR


def build_deps(data_dir=None, config_path=None):
    """组装服务依赖。

    Args:
        data_dir: 数据目录（None 用项目根 data/）。
        config_path: 配置文件路径（H-3，第三轮体检批次4；None 用
            ``data_dir/config.json``）。生产链由 backend_entry 显式传
            ``<root>/config.json``，与安装链（bootstrap/first_run）的
            用户配置落点统一为同一真相源。

    Returns:
        tuple[MemoryStore, MemoryRetrievalPipeline, AgentManager, RemoteController]:
            存储实例、检索管线、本地 Agent 管理器与远端遥控控制器，四者共享同一
            data_dir。遥控控制器由配置的 remote 段驱动
            （默认 enabled=false），测试时各自注入 mock transport。
    """
    data_dir = _resolve_data_dir(data_dir)
    os.makedirs(data_dir, exist_ok=True)
    # M5 配置接线：读取 memory 段注入检索管线（缺省值与 DEFAULTS["memory"] 一致），
    # pipeline 内部会把 dedup/permanent_threshold 透传给其持有的 MemoryManager
    config = ConfigManager(config_path=config_path or os.path.join(data_dir, "config.json"))
    store = MemoryStore(db_path=os.path.join(data_dir, "memories.db"))
    embed = LiteEmbeddingProvider(dim=64)
    # 批次E（降级透明化，最小面）：读取 vector.backend 配置——InMemoryVectorStore
    # 为当前装配兜底不变；配置指定 lancedb 但当前环境缺失该依赖（frozen 产物
    # excludes lancedb 的常规形态）时中文告警，消除静默降级。探测用
    # find_spec 无副作用（不触发 lancedb 真实导入/连接）；lancedb 实际接线
    # 属后续装配升级范畴（64 维桩嵌入下切换 LanceDB 会改变检索排序语义）。
    vector_backend = str(
        config.get("vector", "backend", DEFAULTS["vector"]["backend"]) or ""
    ).strip().lower()
    if vector_backend == "lancedb":
        import importlib.util

        if importlib.util.find_spec("lancedb") is None:
            import logging

            logging.getLogger(__name__).warning(
                "配置指定 LanceDB 但当前环境不可用，已降级为内存向量库"
            )
    vector_store = InMemoryVectorStore()
    pipeline = MemoryRetrievalPipeline(
        store=store,
        vector_store=vector_store,
        embed=embed,
        max_memories=int(config.get("memory", "max_memories", DEFAULTS["memory"]["max_memories"])),
        dedup_threshold=float(config.get("memory", "dedup", DEFAULTS["memory"]["dedup"])),
        permanent_threshold=float(
            config.get("memory", "permanent_threshold", DEFAULTS["memory"]["permanent_threshold"])
        ),
    )
    manager = AgentManager(path=os.path.join(data_dir, "agents.json"))
    #: 遥控控制器：config 驱动（remote.enabled 默认 false），不主动发起真实网络
    remote = RemoteController(config=config)
    return store, pipeline, manager, remote


def build_computer_deps(data_dir=None):
    """组装电脑控制依赖：authorizer + computer + bridge（authorizer 单例复用）。

    Args:
        data_dir: 数据目录（None 用项目根 data/）。授权状态与审计日志落盘于此。

    Returns:
        tuple[ComputerControl, ControlAuthorizer, ToolBridge]:
            实际执行端、安全总控与接线层三者共享同一 data_dir。authorizer 默认
            安全关闭（authorized=False），computer 初始同步该状态，桥接层完成
            授权校验 → 高危确认 → 执行 → 审计 → 回填的完整链路。
    """
    data_dir = _resolve_data_dir(data_dir)
    os.makedirs(data_dir, exist_ok=True)
    authorizer = ControlAuthorizer(data_dir=data_dir)
    computer = ComputerControl(authorized=authorizer.is_authorized())
    bridge = ToolBridge(computer=computer, authorizer=authorizer)
    return computer, authorizer, bridge


def build_runtime_deps(data_dir=None, config=None, store=None, pipeline=None, computer_deps=None, config_path=None):
    """装配批次E生产运行时依赖：语音编排 / 内置工具注册表 / 记忆蒸馏器。

    三个引擎此前仅有能力实现、无生产构造点（20260828_模块0_生产装配接线），
    本函数为其提供统一装配入口：

    - voice：``build_default_pipeline(config)`` 装配三件套（funasr / melotts 缺席时
      自动回退 Mock 后端，装配零失败），再包装为 ``LiteVoicePipeline``（cloud 置
      None，保持纯本地离线形态）；
    - registry：``BuiltinToolRegistry``，电脑控制三件复用 ``computer_deps`` 产物
      （authorizer 单例同源），记忆读写复用 store / pipeline / pipeline.manager；
    - distiller：``MemoryDistiller``，云端适配器由同一 config 构造（CloudAdapter
      构造期零失败，CloudConfigError 延迟到 is_online / chat 时才抛出）。

    Args:
        data_dir: 数据目录（None 用项目根 data/）。
        config: 可选 ConfigManager；缺省按 data_dir 下 config.json 新建。
        store: 可选 MemoryStore；缺省随 pipeline 一起经 build_deps 补建。
        pipeline: 可选 MemoryRetrievalPipeline；缺省经 build_deps 补建。
        computer_deps: 可选 (computer, authorizer, bridge) 三元组；缺省经
            build_computer_deps 补建。

    Returns:
        tuple[LiteVoicePipeline, BuiltinToolRegistry, MemoryDistiller]:
            三者共享同一 config / store / pipeline 上下文。
    """
    data_dir = _resolve_data_dir(data_dir)
    os.makedirs(data_dir, exist_ok=True)
    if store is None or pipeline is None:
        built_store, built_pipeline, _manager, _remote = build_deps(data_dir, config_path=config_path)
        store = store or built_store
        pipeline = pipeline or built_pipeline
    if config is None:
        config = ConfigManager(config_path=config_path or os.path.join(data_dir, "config.json"))
    if computer_deps is None:
        computer_deps = build_computer_deps(data_dir)
    computer, authorizer, bridge = computer_deps

    # 语音全链路：Mock 兜底装配（缺依赖仅告警不失败），离线形态 cloud=None
    components = build_default_pipeline(config)
    voice = LiteVoicePipeline(
        vad=components["vad"],
        asr=components["asr"],
        tts=components["tts"],
        cloud=None,
        judge=components["judge"],
    )
    registry = BuiltinToolRegistry(
        computer=computer,
        computer_bridge=bridge,
        authorizer=authorizer,
        memory_store=store,
        pipeline=pipeline,
        manager=getattr(pipeline, "manager", None),
        config=config,
    )
    distiller = MemoryDistiller(
        cloud=CloudAdapter(config),
        store=store,
        manager=getattr(pipeline, "manager", None),
    )
    return voice, registry, distiller


def make_handler(
    store, pipeline, manager=None, remote=None,
    computer=None, authorizer=None, bridge=None, config=None,
    registry=None, distiller=None, voice=None,
):
    """基于指定依赖构建处理器类（闭包绑定 store / pipeline / manager / remote / computer，便于测试隔离）。

    Args:
        config: 可选 ConfigManager 实例（提供 /api/settings 读写；缺省新建，
            默认读写项目根 data/config.json）。
        registry: 可选内置工具注册表（批次E；供 /api/tools* 端点使用）。
        distiller: 可选记忆蒸馏器（批次E；供 /api/memory/distill 使用）。
        voice: 可选语音全链路编排器（批次E；供 /api/voice/* 端点使用）。
            registry / distiller / voice 遵循 N8 注入语义：仅对显式为 None 的
            依赖回落默认构建（voice 默认构建经 build_default_pipeline 的 Mock
            兜底零失败；registry / distiller 默认构建复用下方已解析的
            computer / authorizer / bridge 与 config / store / pipeline 上下文）。
    """
    if manager is None:
        manager = AgentManager()
    if remote is None:
        remote = RemoteController()
    # N8 注入语义修正：仅对显式为 None 的电脑控制依赖逐项回落默认，不再无条件
    # 覆盖调用方注入的 computer / authorizer；默认构建的 data_dir 优先复用已注入
    # 依赖携带的 data_dir 属性（ComputerControl.data_dir / ControlAuthorizer._data_dir），
    # 取不到再落 DEFAULT_DATA_DIR。create_app 全量注入路径行为不变。
    default_data_dir = DEFAULT_DATA_DIR
    for _dep in (computer, authorizer, bridge):
        reused = getattr(_dep, "data_dir", None) or getattr(_dep, "_data_dir", None)
        if reused:
            default_data_dir = reused
            break
    if computer is None or authorizer is None or bridge is None:
        built_computer, built_authorizer, built_bridge = build_computer_deps(default_data_dir)
        if computer is None:
            computer = built_computer
        if authorizer is None:
            authorizer = built_authorizer
        if bridge is None:
            bridge = built_bridge
    if config is None:
        config = ConfigManager(config_path=os.path.join(default_data_dir, "config.json"))
    # 批次E（N8 语义延续）：仅对显式为 None 的运行时依赖回落默认构建。
    # voice 默认构建走 build_default_pipeline（funasr/melotts 缺席自动回退 Mock，零失败）；
    # registry / distiller 默认构建复用上方已解析的 computer/authorizer/bridge 与
    # config/store/pipeline 上下文，保证与既有注入依赖同源。
    if voice is None:
        _components = build_default_pipeline(config)
        voice = LiteVoicePipeline(
            vad=_components["vad"],
            asr=_components["asr"],
            tts=_components["tts"],
            cloud=None,
            judge=_components["judge"],
        )
    if registry is None:
        registry = BuiltinToolRegistry(
            computer=computer,
            computer_bridge=bridge,
            authorizer=authorizer,
            memory_store=store,
            pipeline=pipeline,
            manager=getattr(pipeline, "manager", None),
            config=config,
        )
    if distiller is None:
        distiller = MemoryDistiller(
            cloud=CloudAdapter(config),
            store=store,
            manager=getattr(pipeline, "manager", None),
        )

    class ApiHandler(BaseHTTPRequestHandler):
        """REST 请求处理器。单线程 HTTPServer 内串行执行，无共享状态竞争。"""

        server_version = "CXLiteAPI/0.1"
        #: socket 读写超时（秒）（H-4 配套，第三轮体检批次2）：防慢客户端
        # （slowloris / 声明超大 body 缓慢发送）把单线程服务永久阻塞在 read 上；
        # 超时抛 socket.timeout（OSError 子类），由调用方捕获后正常回错
        timeout = 30
        #: 服务进程启动时刻（/api/status 的 uptime 基准）
        _STARTED_AT = time.monotonic()
        _store = store
        _pipeline = pipeline
        _manager = manager
        _remote = remote
        _computer = computer
        _authorizer = authorizer
        _bridge = bridge
        _config = config
        _registry = registry
        _distiller = distiller
        _voice = voice

        # ------------------------------------------------------------ 底层工具
        def log_message(self, fmt, *args):
            """精简访问日志（避免默认 stderr 冗长输出，服务启动信息仍打印）。"""

        def _check_host(self):
            """校验 Host 头是否指向本服务自身（防 DNS rebinding）。

            规则：Host 精确命中 _ALLOWED_HOSTS 放行；否则拆出主机名部分，
            仅当主机名为本机回环名（127.0.0.1 / localhost，端口任意，兼容
            测试态临时端口与本机自定义端口部署）时放行。缺失 Host 头一律拒绝。
            """
            host = (self.headers.get("Host") or "").strip().lower()
            if not host:
                return False
            if host in {h.lower() for h in _ALLOWED_HOSTS}:
                return True
            # IPv6 字面量形态 [::1]:port 与常规 host:port 分别拆出主机名
            name = host
            if name.startswith("["):
                name = name[1:].split("]", 1)[0]
            elif ":" in name:
                name = name.rsplit(":", 1)[0]
            return name in _ALLOWED_HOST_NAMES

        def _check_token(self):
            """校验启动令牌 X-Client-Token（N1：根治 CSRF→RCE 链）。

            规则：
            - 服务端令牌为空（env CXA_API_TOKEN 未设置）：开放模式放行，仅首次
              调用告警一次（提示当前无令牌保护，仅限开发 / 测试环境）；
            - 令牌非空：请求头 X-Client-Token 必须与令牌常量时间比较一致，
              否则回 403 ``unauthorized_client``。

            :return: True 放行；False 表示已发送 403，调用方直接 return。
            """
            global _TOKEN_OPEN_MODE_WARNED
            if not _API_TOKEN:
                if not _TOKEN_OPEN_MODE_WARNED:
                    _TOKEN_OPEN_MODE_WARNED = True
                    print(
                        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [WARNING] "
                        "CXA_API_TOKEN 未设置：API 服务运行于开放模式（无令牌校验），仅限开发/测试环境"
                    )
                return True
            supplied = (self.headers.get("X-Client-Token") or "").encode("utf-8", errors="replace")
            if hmac.compare_digest(supplied, _API_TOKEN.encode("utf-8")):
                return True
            self._send_json({"ok": False, "error": "unauthorized_client"}, 403)
            return False

        def _cors_headers(self):
            """按请求 Origin 计算应附带的 CORS 响应头。

            Origin 命中 _ALLOWED_ORIGINS 时返回放行头集合；未命中返回空 dict
            （不返回任何 Access-Control 头，更不放通配符 *），由浏览器同源策略兜底。
            """
            origin = self.headers.get("Origin")
            if origin in _ALLOWED_ORIGINS:
                return {
                    "Access-Control-Allow-Origin": origin,
                    "Vary": "Origin",
                    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
                    # 中-3（第四轮体检批次B）：预检允许头补 X-Client-Token——
                    # 令牌模式下浏览器跨源请求必须携带该自定义头，缺了即与令牌闸矛盾
                    "Access-Control-Allow-Headers": "Content-Type, X-Client-Token",
                }
            return {}

        def _deny_bad_host(self):
            """Host 校验失败的统一 403 响应。"""
            self._send_json({"ok": False, "error": "forbidden", "message": "Host 校验失败：拒绝访问"}, 403)

        def _guard_internal_error(self, exc):
            """全局异常兜底：回结构化 500，确保畸形输入不致连接中断。

            低-5（第四轮体检批次B）：对外只回错误码与异常类别摘要（类名），
            完整异常消息（可能含内部路径/实现细节）仅经 LOGGER.exception 写
            服务端日志，不再外泄到响应体。
            """
            LOGGER.exception("接口处理异常 path=%s", getattr(self, "path", "?"))
            self._send_json(
                {"ok": False, "error": "internal error", "detail": exc.__class__.__name__}, 500
            )

        def _reject_bad_json(self):
            """malformed / 非 dict 请求体的统一 400（L2 收口：与空 body 明确区分）。"""
            self._send_json(
                {
                    "ok": False,
                    "error": "bad_json",
                    "message": "请求体必须是合法 JSON 对象且 Content-Type 为 application/json",
                },
                400,
            )

        def _send_json(self, payload, status=200):
            """以 UTF-8 编码、ensure_ascii=False 输出 JSON 响应（中文不转义）。"""
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for key, value in self._cors_headers().items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            """处理预检请求：直接 204 + CORS 头 + 空 body（无路由分发、无副作用）。"""
            if not self._check_host():
                self._deny_bad_host()
                return
            self.send_response(204)
            for key, value in self._cors_headers().items():
                self.send_header(key, value)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _parse_query(self):
            """解析查询串为 dict[str, str|None]（首个值优先，空串归一为 None）。"""
            qs = parse_qs(urlparse(self.path).query)
            out = {}
            for key in ("type", "agent_id", "limit", "q", "top_k", "enabled"):
                vals = qs.get(key)
                if vals:
                    out[key] = vals[0] or None
            return out

        # ------------------------------------------------------------ 状态 / 设置 / 聊天守卫接口
        def _handle_status(self):
            """GET /api/status：轻量系统状态（供管理 API 健康探测 / 状态页占位）。"""
            self._send_json(
                {
                    "status": "ok",
                    "app": "CX-A/CX-Lite",
                    "version": LITE_VERSION,
                    "uptime_seconds": round(time.monotonic() - self._STARTED_AT, 2),
                    "companion": True,
                }
            )

        def _settings_view(self):
            """组装用户可读配置视图（**不含 API Key**，避免敏感信息外泄）。"""
            return {
                "cloud": {
                    "provider": self._config.get("cloud", "provider", "deepseek"),
                    "base_url": self._config.get("cloud", "base_url", ""),
                },
                "tts": {"voice": self._config.get("tts", "voice", "cx-open")},
                "local_llm": {"enabled": bool(self._config.get("local_llm", "enabled", False))},
                "acp": {"enabled": bool(self._config.get("acp", "enabled", False))},
                "remote": {"enabled": bool(self._config.get("remote", "enabled", False))},
            }

        def _handle_settings_get(self):
            """GET /api/settings：返回脱敏配置视图（供前端设置页首帧对齐默认值）。"""
            self._send_json(self._settings_view())

        def _handle_settings_update(self):
            """PUT /api/settings：应用白名单补丁并热更新落盘。

            支持键：``cloud.provider``（须在 provider 白名单内）、``tts.voice``、
            ``local_llm.enabled``。其余键被忽略并列入 ``ignored`` 返回，供调用方校正。
            段（cloud/tts/local_llm）存在但非 dict 时回 400，避免 AttributeError 冒泡。

            L2 收口语义：
            - malformed / 非 dict JSON body → 400 ``bad_json``；
            - 空 body（``{}``）→ 400 ``empty_body``（与 malformed 明确区分）；
            - GET 视图可见但白名单只读的已知键（acp/remote/vector section 及
              cloud.base_url）收集进 ``ignored`` 数组随响应回显，消除静默丢弃。
            """
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            if not body:
                self._send_json(
                    {"ok": False, "error": "empty_body", "message": "PUT /api/settings 要求非空配置补丁"}, 400
                )
                return
            # 先做段类型校验——段存在但非 dict 一律 400，不做 .get() 取值
            invalid_sections = [
                name for name in ("cloud", "tts", "local_llm")
                if name in body and not isinstance(body[name], dict)
            ]
            if invalid_sections:
                self._send_json(
                    {"ok": False, "error": f"invalid section type: {', '.join(invalid_sections)}"}, 400
                )
                return
            ignored = []
            applied = []

            # 已知只读键回显（GET 视图可见但不在 PUT 白名单）：不再静默丢弃
            for key in _SETTINGS_READONLY_TOP_KEYS:
                if key in body:
                    ignored.append(f"{key}（GET 视图可见但白名单只读，未应用）")
            cloud_section = body.get("cloud")
            if isinstance(cloud_section, dict) and "base_url" in cloud_section:
                ignored.append("cloud.base_url（GET 视图可见但白名单只读，未应用）")

            provider = body.get("cloud", {}).get("provider")
            if provider is not None:
                if provider in CLOUD_PROVIDER_ALLOWLIST:
                    self._config.set("cloud", "provider", provider)
                    applied.append("cloud.provider")
                else:
                    ignored.append(f"cloud.provider={provider!r}（不在白名单 {CLOUD_PROVIDER_ALLOWLIST}）")

            # H-6（第三轮体检批次4）：api_key 写入白名单——生产装配下唯一可用的
            # 云端 Key 配置入口（管理 API 供另一 Agent 调用，不进前端 UI）。
            # 走 ConfigManager 既有 Fernet 加密链路落盘（save 时统一加密）；
            # GET 视图继续不含 api_key，applied 只回键名不回显值。
            api_key = body.get("cloud", {}).get("api_key")
            if api_key is not None:
                if isinstance(api_key, str) and api_key.strip():
                    self._config.set("cloud", "api_key", api_key.strip())
                    applied.append("cloud.api_key")
                else:
                    ignored.append("cloud.api_key（必须为非空字符串）")

            voice = body.get("tts", {}).get("voice")
            if voice is not None:
                if isinstance(voice, str) and voice.strip():
                    self._config.set("tts", "voice", voice.strip())
                    applied.append("tts.voice")
                else:
                    ignored.append("tts.voice（必须为非空字符串）")

            local_enabled = body.get("local_llm", {}).get("enabled")
            if local_enabled is not None:
                if isinstance(local_enabled, bool):
                    self._config.set("local_llm", "enabled", local_enabled)
                    applied.append("local_llm.enabled")
                else:
                    ignored.append("local_llm.enabled（必须为布尔）")

            if applied:
                try:
                    self._config.save()
                except OSError as exc:  # noqa: BLE001 - 落盘失败不影响内存生效
                    self._send_json(
                        {"ok": False, "error": "config_save_failed", "message": str(exc),
                         "applied": applied, "ignored": ignored, "config": self._settings_view()},
                        status=500,
                    )
                    return

            self._send_json({"ok": True, "applied": applied, "ignored": ignored, "config": self._settings_view()})

        def _handle_chat_send_guard(self):
            """POST /api/chat/messages：聊天服务未启用守卫（避免直连误 404）。"""
            self._send_json(
                {
                    "ok": False,
                    "error": CHAT_SERVICE_DISABLED,
                    "message": "聊天服务未启用（本期前端走 Mock 演示），请接入 lite/cloud 云端适配后启用",
                }
            )

        def _handle_chat_history_guard(self):
            """GET /api/chat/history：聊天历史守卫（同样返回未启用提示 + 空列表）。"""
            self._send_json(
                {
                    "ok": False,
                    "error": CHAT_SERVICE_DISABLED,
                    "message": "聊天服务未启用（本期前端走 Mock 演示）",
                    "messages": [],
                }
            )

        # ------------------------------------------------------------ 路由
        def do_GET(self):
            """处理 GET：health/status/settings/chat 守卫、记忆/Agent/远端/电脑状态、/api/tools。

            GET /api/health 豁免令牌校验（供 main.js 健康探测与运维探针）。
            """
            if not self._check_host():
                self._deny_bad_host()
                return
            path = urlparse(self.path).path
            # N1：健康检查豁免令牌，其余 GET 一律先过启动令牌闸
            if path != "/api/health" and not self._check_token():
                return
            try:
                query = self._parse_query()

                if path == "/api/health":
                    self._send_json({"status": "ok"})
                    return

                if path == "/api/status":
                    self._handle_status()
                    return

                if path == "/api/settings":
                    self._handle_settings_get()
                    return

                if path == "/api/chat/history":
                    self._handle_chat_history_guard()
                    return

                if path == "/api/remote/status":
                    self._handle_remote_status()
                    return

                if path == "/api/memories":
                    self._handle_list(query)
                    return

                if path == "/api/memories/search":
                    self._handle_search(query)
                    return

                if path == "/api/agents":
                    self._handle_agents_list(query)
                    return

                if path == "/api/computer/status":
                    self._handle_computer_status()
                    return

                if path == "/api/tools":
                    self._handle_tools_list()
                    return

                self._send_json({"ok": False, "error": "not_found", "message": f"未找到接口 {path}"}, 404)
            except Exception as exc:  # noqa: BLE001 - 兜底：任何畸形输入都得到结构化 500 而非连接中断
                # SystemExit / KeyboardInterrupt 继承 BaseException，不会被此处捕获
                self._guard_internal_error(exc)

        def do_POST(self):
            """处理 POST：chat 守卫、Agent/远端/电脑控制、tools/call、memory/distill、voice/*。"""
            if not self._check_host():
                self._deny_bad_host()
                return
            try:
                path = urlparse(self.path).path
                # N1：POST 无豁免端点，统一在 path 解析后过启动令牌闸（与 do_GET 次序对齐）
                if not self._check_token():
                    return
                if path == "/api/chat/messages":
                    self._handle_chat_send_guard()
                    return
                if path == "/api/agents":
                    self._handle_agents_create()
                    return
                if path == "/api/remote/control":
                    self._handle_remote_control()
                    return
                if path == "/api/remote/push_config":
                    self._handle_remote_push_config()
                    return
                if path == "/api/computer/authorize":
                    self._handle_computer_authorize()
                    return
                if path == "/api/computer/call":
                    self._handle_computer_call()
                    return
                if path == "/api/tools/call":
                    self._handle_tools_call()
                    return
                if path == "/api/memory/distill":
                    self._handle_memory_distill()
                    return
                if path == "/api/voice/synthesize":
                    self._handle_voice_synthesize()
                    return
                if path == "/api/voice/transcribe":
                    self._handle_voice_transcribe()
                    return
                self._send_json({"ok": False, "error": "not_found", "message": f"未找到接口 {path}"}, 404)
            except _BodyTooLarge:
                # N6：413 响应已由 _read_body_json 发出，此处直接返回
                return
            except Exception as exc:  # noqa: BLE001 - 兜底：结构化 500 而非连接中断
                self._guard_internal_error(exc)

        def do_PUT(self):
            """处理 PUT：/api/settings 更新配置；/api/agents/{id} 更新指定 Agent。"""
            if not self._check_host():
                self._deny_bad_host()
                return
            try:
                path = urlparse(self.path).path
                # N1：PUT 无豁免端点，统一在 path 解析后过启动令牌闸（与 do_GET 次序对齐）
                if not self._check_token():
                    return
                if path == "/api/settings":
                    self._handle_settings_update()
                    return
                agent_id = self._extract_agents_id(path)
                if agent_id is None:
                    self._send_json({"ok": False, "error": "not_found", "message": f"未找到接口 {path}"}, 404)
                    return
                self._handle_agents_update(agent_id)
            except _BodyTooLarge:
                # N6：413 响应已由 _read_body_json 发出，此处直接返回
                return
            except Exception as exc:  # noqa: BLE001 - 兜底：结构化 500 而非连接中断
                self._guard_internal_error(exc)

        def do_DELETE(self):
            """处理 DELETE：/api/memories/{id} 软删除 / /api/agents/{id} 删除 Agent。"""
            if not self._check_host():
                self._deny_bad_host()
                return
            try:
                path = urlparse(self.path).path
                # N1：DELETE 无豁免端点，统一在 path 解析后过启动令牌闸（与 do_GET 次序对齐）
                if not self._check_token():
                    return
                prefix = "/api/memories/"
                if path.startswith(prefix):
                    self._delete_memory(path[len(prefix):])
                    return
                agent_id = self._extract_agents_id(path)
                if agent_id is not None:
                    self._handle_agents_delete(agent_id)
                    return
                self._send_json({"ok": False, "error": "not_found", "message": f"未找到接口 {path}"}, 404)
            except Exception as exc:  # noqa: BLE001 - 兜底：超大 int 触发 sqlite OverflowError 等畸形输入均结构化响应
                self._guard_internal_error(exc)

        @staticmethod
        def _extract_agents_id(path):
            """从 /api/agents/{id} 提取 id；非该前缀或含额外斜杠返回 None。"""
            prefix = "/api/agents/"
            if not path.startswith(prefix):
                return None
            raw = path[len(prefix):]
            if not raw or "/" in raw:
                return None
            return raw

        def _delete_memory(self, raw_id):
            """软删除单条记忆（原先 do_DELETE 的记忆分支）。"""
            if not raw_id or "/" in raw_id:
                self._send_json({"ok": False, "error": "bad_request", "message": "非法记忆 id"}, 400)
                return
            try:
                memory_id = int(raw_id)
            except ValueError:
                self._send_json({"ok": False, "error": "bad_request", "message": "id 必须是整数"}, 400)
                return
            # 低-6（第四轮体检批次B）：超出 64 位有符号整数范围的 id 直接入库会触发
            # sqlite OverflowError → 500，边界处显式 400
            if not (_INT64_MIN <= memory_id <= _INT64_MAX):
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": "id 超出有效范围（64 位整数）"}, 400
                )
                return

            # M-10（第三轮体检批次3）：优先走 manager.soft_delete（软删 + 同步
            # 清理向量库孤儿向量）；manager 缺席时回落 store 直删保持旧行为
            manager = getattr(self._pipeline, "manager", None)
            if manager is not None:
                deleted = manager.soft_delete(memory_id)
            else:
                deleted = self._store.soft_delete(memory_id)
            if deleted:
                self._send_json({"ok": True, "id": memory_id})
            else:
                self._send_json({"ok": False, "error": "not_found", "message": f"记忆 {memory_id} 不存在"}, 404)

        # ------------------------------------------------------------ 各接口实现
        def _parse_limit_param(self, raw, name):
            """解析并校验 limit/top_k 类参数（M-6，第三轮体检批次2）。

            非整数回 400；负数回 400（LIMIT -1 在 SQLite 语义为无限制，与
            限制条数意图相反）；超上限钳制到 _MAX_LIST_LIMIT（防 sqlite
            OverflowError 与全量拉取）。

            :param raw: 原始字符串参数
            :param name: 参数名（用于错误消息）
            :return: 校验后的整数；校验失败时已发送 400 并返回 None
            """
            try:
                value = int(raw)
            except ValueError:
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": f"{name} 必须是整数"}, 400
                )
                return None
            if value < 0:
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": f"{name} 不能为负数"}, 400
                )
                return None
            return min(value, _MAX_LIST_LIMIT)

        def _sanitize_agent_id(self, raw):
            """规范化 agent_id（L-5）：strip + 限长，空值回落 default。"""
            return (str(raw or "").strip()[:_MAX_AGENT_ID_CHARS]) or "default"

        def _handle_list(self, query):
            """记忆列表：支持 type / agent_id / limit 过滤，默认仅返回未软删除记录。"""
            limit = None
            if query.get("limit") is not None:
                limit = self._parse_limit_param(query["limit"], "limit")
                if limit is None:
                    return
            agent_id = self._sanitize_agent_id(query.get("agent_id")) if query.get("agent_id") else None
            try:
                rows = self._store.list(
                    type=query.get("type"),
                    agent_id=agent_id,
                    limit=limit,
                    include_deleted=False,
                )
            except ValueError as exc:
                self._send_json({"ok": False, "error": "bad_request", "message": str(exc)}, 400)
                return
            self._send_json(rows)

        def _handle_search(self, query):
            """记忆检索：走 MemoryRetrievalPipeline.retrieve，返回命中的记忆与 context_text。"""
            q = query.get("q") or ""
            if not q.strip():
                self._send_json({"memories": [], "context_text": "【回忆】"})
                return

            top_k = None
            if query.get("top_k") is not None:
                top_k = self._parse_limit_param(query["top_k"], "top_k")
                if top_k is None:
                    return
            agent_id = self._sanitize_agent_id(query.get("agent_id"))
            try:
                result = self._pipeline.retrieve(q, agent_id=agent_id, top_k=top_k)
            except ValueError as exc:
                self._send_json({"ok": False, "error": "bad_request", "message": str(exc)}, 400)
                return
            self._send_json(result)

        # ------------------------------------------------------------ 远端遥控接口
        def _map_remote_error(self, exc):
            """把远端遥控异常映射为可发送的 (payload, status)。

            Args:
                exc: 捕获的远端异常（RemoteDisabled / RemoteUnreachable /
                    RemoteError / ValueError）。

            Returns:
                tuple[dict, int]: (JSON 载荷, HTTP 状态码)。
            """
            if isinstance(exc, RemoteDisabled):
                return {"ok": False, "error": "remote_disabled", "message": str(exc)}, 503
            if isinstance(exc, RemoteUnreachable):
                return {"ok": False, "error": "remote_unreachable", "message": str(exc)}, 504
            if isinstance(exc, ValueError):
                return {"ok": False, "error": "bad_request", "message": str(exc)}, 400
            return {"ok": False, "error": "remote_error", "message": str(exc)}, 502

        def _handle_remote_status(self):
            """GET /api/remote/status：转发 get_status，异常按语义映射状态码。"""
            try:
                data = self._remote.get_status()
            except (RemoteDisabled, RemoteUnreachable, RemoteError) as exc:
                payload, status = self._map_remote_error(exc)
                self._send_json(payload, status)
                return
            self._send_json(data)

        def _handle_remote_control(self):
            """POST /api/remote/control：读取 action/agent_id 并转发 control。"""
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            action = body.get("action")
            if action not in self._remote.ACTIONS:
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": f"action 必须为 {'/'.join(self._remote.ACTIONS)}"},
                    400,
                )
                return
            agent_id = body.get("agent_id")
            try:
                data = self._remote.control(action, agent_id=agent_id)
            except (RemoteDisabled, RemoteUnreachable, RemoteError) as exc:
                payload, status = self._map_remote_error(exc)
                self._send_json(payload, status)
                return
            self._send_json(data)

        def _handle_remote_push_config(self):
            """POST /api/remote/push_config：读取非空 JSON 补丁并转发 push_config。"""
            patch = self._read_body_json()
            if patch is None:
                self._reject_bad_json()
                return
            if not isinstance(patch, dict) or not patch:
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": "patch 必须为非空 JSON 对象"}, 400
                )
                return
            try:
                data = self._remote.push_config(patch)
            except (RemoteDisabled, RemoteUnreachable, RemoteError) as exc:
                payload, status = self._map_remote_error(exc)
                self._send_json(payload, status)
                return
            self._send_json(data)

        # ------------------------------------------------------------ 电脑控制接口
        def _handle_computer_status(self):
            """GET /api/computer/status：返回授权状态与高危确认开关。"""
            self._send_json(
                {
                    "authorized": self._authorizer.is_authorized(),
                    "confirm_dangerous": bool(self._authorizer.confirm_dangerous),
                }
            )

        def _handle_computer_authorize(self):
            """POST /api/computer/authorize：body {enabled: bool}，开启/撤销授权。

            同步 authorizer 与 computer 两端授权状态，返回最新状态（ok:true + 状态字段）。
            """
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            enabled = body.get("enabled")
            if not isinstance(enabled, bool):
                self._send_json({"ok": False, "error": "bad_request", "message": "enabled 必须为布尔值"}, 400)
                return
            if enabled:
                self._authorizer.authorize()
            else:
                self._authorizer.revoke()
            self._computer.set_authorized(self._authorizer.is_authorized())
            self._send_json(
                {
                    "ok": True,
                    "authorized": self._authorizer.is_authorized(),
                    "confirm_dangerous": bool(self._authorizer.confirm_dangerous),
                }
            )

        def _handle_computer_call(self):
            """POST /api/computer/call：body {tool, arguments}，走 ToolBridge 执行。

            未授权（NotAuthorizedError）映射 403 并标注 authorized:false；
            其余 PluginError 按各自错误码映射状态码，**不误标 authorized 字段**（L3：
            仅授权类错误才携带 authorized:false，避免语义失真）。
            """
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            tool = body.get("tool")
            if not tool:
                self._send_json({"ok": False, "error": "bad_request", "message": "tool 为必填字段"}, 400)
                return
            # L-3：arguments 存在且非 dict 时显式 400（不再静默替换为 {}）
            raw_args = body.get("arguments")
            if raw_args is not None and not isinstance(raw_args, dict):
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": "arguments 必须为对象"}, 400
                )
                return
            arguments = raw_args or {}
            try:
                payload = self._bridge.execute(str(tool), arguments)
            except NotAuthorizedError as exc:
                self._send_json(
                    {
                        "ok": False,
                        "authorized": False,
                        "error": "需要先授权",
                        "error_code": exc.error_code,
                    },
                    403,
                )
                return
            except PluginError as exc:
                # N7：原 isinstance(exc, NotAuthorizedError) 分支恒 False（该异常已由
                # 上一个 except 捕获），删除死分支；非授权类 PluginError 不携带 authorized 键
                self._send_json(
                    {"ok": False, "error": exc.message, "error_code": exc.error_code},
                    exc.http_status,
                )
                return
            self._send_json(payload)

        # ------------------------------------------------------------ 内置工具 / 记忆蒸馏 / 语音（批次E）
        def _handle_tools_list(self):
            """GET /api/tools：内置工具清单 + 端点用法自述（供管理 Agent 自发现）。

            按 BuiltinToolRegistry.list_tools() 实际提供的数据结构如实映射
            （id/name/description/source/category/enabled），不虚构参数 schema。
            """
            tools = [
                {
                    "id": t["id"],
                    "name": t["name"],
                    "description": t["description"],
                    "source": t["source"],
                    "category": t["category"],
                    "enabled": t["enabled"],
                }
                for t in self._registry.list_tools()
            ]
            self._send_json({"ok": True, "tools": tools, "usage": _TOOLS_USAGE})

        def _handle_tools_call(self):
            """POST /api/tools/call：body {name, arguments}，调用内置工具注册表。

            注册表 ``call`` 不抛异常、统一返回 ``{success, tool, result|error,
            authorized}`` 外壳，本端点按外壳判定映射：
            - 未知工具（list_tools 无该 id）→ 404 not_found；
            - success=False 且 authorized=False（NotAuthorizedError 包装 /
              类别开关禁用，电脑三件套已在注册表内走 授权→高危确认→审计 链）
              → 403 not_authorized；
            - 其余 success=False（执行失败）→ 400；
            - 成功 → 200 {ok:true, result}。
            """
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            name = str(body.get("name") or "").strip()
            if not name:
                self._send_json({"ok": False, "error": "bad_request", "message": "name 为必填字段"}, 400)
                return
            # L-3：arguments 存在且非 dict 时显式 400（不再静默替换为 {}）
            raw_args = body.get("arguments")
            if raw_args is not None and not isinstance(raw_args, dict):
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": "arguments 必须为对象"}, 400
                )
                return
            arguments = raw_args or {}
            known_ids = {t["id"] for t in self._registry.list_tools()}
            if name not in known_ids:
                self._send_json(
                    {"ok": False, "error": "not_found", "message": f"未知内置工具：{name!r}"}, 404
                )
                return
            outcome = self._registry.call(name, arguments)
            if not outcome.get("success"):
                if outcome.get("authorized") is False:
                    self._send_json(
                        {"ok": False, "error": "not_authorized", "message": outcome.get("error")}, 403
                    )
                else:
                    # 低-5（第四轮体检批次B）：工具执行失败的 error 可能含内部异常
                    # 文本（registry 侧包装 str(exc)），对外只回固定类别摘要，
                    # 完整错误写日志；结构化 error_code（若有）非自由文本可透传
                    LOGGER.warning("内置工具 %s 执行失败：%s", name, outcome.get("error"))
                    payload = {"ok": False, "error": "tool_failed", "message": "工具执行失败"}
                    if outcome.get("error_code"):
                        payload["error_code"] = outcome.get("error_code")
                    self._send_json(payload, 400)
                return
            self._send_json({"ok": True, "result": outcome.get("result")})

        def _handle_memory_distill(self):
            """POST /api/memory/distill：body {messages, agent_id?}，触发云端记忆蒸馏。

            - messages 必须为非空列表且每项含 role/content，否则 400；
            - CloudConfigError（未配置 api_key / 未知 provider）→ 400 cloud_not_configured；
            - DistillationPaused（云端离线）→ 503 cloud_offline；
            - 成功 → 200 {ok:true, sessions}（distill_with_sessions 完整会话明细）。
            """
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            messages = body.get("messages")
            if not isinstance(messages, list) or not messages:
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": "messages 必须为非空列表"}, 400
                )
                return
            if not all(isinstance(m, dict) and "role" in m and "content" in m for m in messages):
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": "messages 每项必须是含 role/content 的对象"},
                    400,
                )
                return
            # H-5：条数上限——防单请求串行发起数百次云端 LLM 调用阻塞单线程服务
            if len(messages) > _MAX_DISTILL_MESSAGES:
                self._send_json(
                    {
                        "ok": False,
                        "error": "bad_request",
                        "message": f"messages 条数超过上限 {_MAX_DISTILL_MESSAGES}（收到 {len(messages)}）",
                    },
                    400,
                )
                return
            agent_id = self._sanitize_agent_id(body.get("agent_id"))
            try:
                sessions = self._distiller.distill_with_sessions(messages, agent_id=agent_id)
            except CloudConfigError as exc:
                self._send_json({"ok": False, "error": "cloud_not_configured", "message": str(exc)}, 400)
                return
            except DistillationPaused as exc:
                self._send_json({"ok": False, "error": "cloud_offline", "message": str(exc)}, 503)
                return
            self._send_json({"ok": True, "sessions": sessions})

        def _handle_voice_synthesize(self):
            """POST /api/voice/synthesize：body {text, voice?}，TTS 合成并以 base64 返回。

            调 voice.tts.synthesize(text, voice) 得 wav/pcm 字节；后端任何异常
            （含引擎未就绪）统一 503 voice_backend_unavailable，不中断服务。
            """
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            text = str(body.get("text") or "").strip()
            if not text:
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": "text 为必填字段且不能为空"}, 400
                )
                return
            # M-5：文本长度上限——防超长文本分钟级合成阻塞单线程服务
            if len(text) > _MAX_SYNTH_TEXT_CHARS:
                self._send_json(
                    {
                        "ok": False,
                        "error": "bad_request",
                        "message": f"text 长度超过上限 {_MAX_SYNTH_TEXT_CHARS} 字符（收到 {len(text)}）",
                    },
                    400,
                )
                return
            voice = body.get("voice") or None
            try:
                audio = self._voice.tts.synthesize(text, voice)
            except Exception as exc:  # noqa: BLE001 - 语音后端故障统一 503 兜底
                self._send_json(
                    {"ok": False, "error": "voice_backend_unavailable", "message": str(exc)[:200]}, 503
                )
                return
            if not audio:
                self._send_json(
                    {"ok": False, "error": "voice_backend_unavailable", "message": "合成返回空音频"}, 503
                )
                return
            self._send_json(
                {
                    "ok": True,
                    "audio_base64": base64.b64encode(bytes(audio)).decode("ascii"),
                    "mime": "audio/wav",
                }
            )

        def _handle_voice_transcribe(self):
            """POST /api/voice/transcribe：body {audio_base64, sample_rate?}，ASR 转写。

            base64 解码后送 voice.asr.transcribe；解码失败 400；后端异常 503
            voice_backend_unavailable。sample_rate 为可选元数据（当前 ASR 门面
            不消费，透传忽略，保留字段兼容未来采样率感知后端）。
            """
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            raw = body.get("audio_base64")
            if not isinstance(raw, str) or not raw.strip():
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": "audio_base64 为必填字段"}, 400
                )
                return
            try:
                audio = base64.b64decode(raw, validate=True)
            except (ValueError, TypeError):
                # binascii.Error 是 ValueError 子类，统一按非法 base64 处理
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": "audio_base64 不是合法的 base64 编码"}, 400
                )
                return
            try:
                result = self._voice.asr.transcribe(audio) or {}
            except Exception as exc:  # noqa: BLE001 - 语音后端故障统一 503 兜底
                self._send_json(
                    {"ok": False, "error": "voice_backend_unavailable", "message": str(exc)[:200]}, 503
                )
                return
            self._send_json({"ok": True, "text": str(result.get("text", ""))})

        # ------------------------------------------------------------ Agent 接口
        def _read_body_json(self):
            """读取请求体并解析为 dict。

            返回值语义（L2 收口：malformed 与空 body 明确区分）：
            - 无 body（Content-Length<=0）：返回 ``{}``（空 body 语义，向后兼容）；
            - 带 body 但 Content-Type 非 application/json 开头：返回 None；
            - 非法 JSON（malformed）：返回 None，调用方统一回 400 ``bad_json``；
            - 合法 JSON 但非 dict（数组/字符串/数字等）：返回 None（结构不符契约，
              视为坏请求）。
            调用方必须将 None 视为坏请求回 400。
            """
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0:
                return {}
            if length > _MAX_BODY_BYTES:
                # N6 + H-4（第三轮体检批次2）：超大 body 直接 413，防止阻塞单线程
                # 服务（本机 DoS 面）。丢弃读取有界（最多 1MB——保证诚实客户端的
                # 真实大 body 被读完、能收到 413；恶意声明 10GB 也最多丢 1MB），
                # 随后置 close_connection 强制断开；配合 ApiHandler.timeout=30 的
                # socket 超时，慢客户端（slowloris）最坏阻塞 30s 而非永久。
                # 抛内部信号由 do_METHOD 捕获终止分发（响应已发出）
                try:
                    self.rfile.read(min(length, _MAX_BODY_BYTES))
                except OSError:
                    pass
                self.close_connection = True
                self._send_json({"ok": False, "error": "payload_too_large"}, 413)
                raise _BodyTooLarge()
            content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if not content_type.startswith("application/json"):
                return None
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except (OSError, ValueError):
                return None
            return body if isinstance(body, dict) else None

        def _handle_agents_list(self, query):
            """Agent 列表：支持 enabled 过滤（true/false），默认返回全部。"""
            enabled = None
            if query.get("enabled") is not None:
                raw = query["enabled"].strip().lower()
                if raw in ("true", "1"):
                    enabled = True
                elif raw in ("false", "0"):
                    enabled = False
                else:
                    self._send_json({"ok": False, "error": "bad_request", "message": "enabled 必须为 true/false"}, 400)
                    return
            agents = self._manager.list(enabled=enabled)
            self._send_json([a.to_dict() for a in agents])

        def _handle_agents_create(self):
            """创建 Agent：body 必须含 name 与 persona，voice 可选。"""
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            name = (body.get("name") or "").strip()
            persona = (body.get("persona") or "").strip()
            if not name or not persona:
                self._send_json(
                    {"ok": False, "error": "bad_request", "message": "name 与 persona 为必填字段"}, 400
                )
                return
            voice = body.get("voice") or None
            agent = self._manager.create(name=name, persona=persona, voice=voice)
            self._send_json(agent.to_dict(), 201)

        def _handle_agents_update(self, agent_id):
            """更新 Agent：body 为可更新字段（name/persona/voice/enabled）。

            L-4（第三轮体检批次2）：API 层先做白名单键过滤再展开——即使底层
            AgentManager.update 的白名单未来放开，接口面仍只接受这四个字段。
            """
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            allowed_keys = ("name", "persona", "voice", "enabled")
            patch = {k: body[k] for k in allowed_keys if k in body}
            try:
                agent = self._manager.update(agent_id, **patch)
            except AgentNotFound as exc:
                self._send_json({"ok": False, "error": "not_found", "message": str(exc)}, 404)
                return
            self._send_json(agent.to_dict())

        def _handle_agents_delete(self, agent_id):
            """删除 Agent：不存在时返回 404。"""
            try:
                self._manager.delete(agent_id)
            except AgentNotFound as exc:
                self._send_json({"ok": False, "error": "not_found", "message": str(exc)}, 404)
                return
            self._send_json({"ok": True, "id": agent_id})

    return ApiHandler


def create_app(data_dir=None, config_path=None):
    """创建完整应用依赖并返回 (store, pipeline, handler_class)。

    Args:
        data_dir: 数据目录（None 用项目根 data/）。
        config_path: 配置文件路径（H-3；None 用 data_dir/config.json；
            生产链传 <root>/config.json 与安装链统一真相源）。

    Returns:
        tuple: (store, pipeline, handler) -> (MemoryStore, MemoryRetrievalPipeline, ApiHandler)。
    """
    data_dir = _resolve_data_dir(data_dir)
    store, pipeline, manager, remote = build_deps(data_dir, config_path=config_path)
    computer, authorizer, bridge = build_computer_deps(data_dir)
    #: 配置实例与 remote 同源（同一 config_path），供 /api/settings 读写，
    #: 保证测试隔离（不触碰项目根运行时配置）。
    config = ConfigManager(config_path=config_path or os.path.join(data_dir, "config.json"))
    #: 批次E生产装配：语音编排 / 内置工具注册表 / 记忆蒸馏器，全量注入 handler
    voice, registry, distiller = build_runtime_deps(
        data_dir,
        config=config,
        store=store,
        pipeline=pipeline,
        computer_deps=(computer, authorizer, bridge),
    )
    handler = make_handler(
        store, pipeline, manager, remote,
        computer=computer, authorizer=authorizer, bridge=bridge, config=config,
        registry=registry, distiller=distiller, voice=voice,
    )
    return store, pipeline, handler


def create_server(host=DEFAULT_HOST, port=DEFAULT_PORT, data_dir=None, config_path=None):
    """构建并返回配置好的 HTTPServer（单线程串行处理）。"""
    _store, _pipeline, handler = create_app(data_dir, config_path=config_path)
    return HTTPServer((host, port), handler)


def main(argv=None):
    """命令行入口：解析 -h/--host、-p/--port 并启动服务，支持 Ctrl+C 优雅退出。"""
    parser = argparse.ArgumentParser(
        prog="api_server",
        description="CXLite 轻量记忆浏览与管理 REST 服务",
        add_help=False,  # 关闭 argparse 默认 -h（帮助），腾出 -h 给 host
    )
    parser.add_argument("-h", "--host", default=DEFAULT_HOST, help=f"监听主机（默认 {DEFAULT_HOST}）")
    parser.add_argument("-p", "--port", type=int, default=DEFAULT_PORT, help=f"监听端口（默认 {DEFAULT_PORT}）")
    parser.add_argument("--data-dir", default=None, help="数据目录（默认项目根 data/）")
    parser.add_argument(
        "--config",
        default=None,
        help="配置文件路径（H-3：生产链传 <root>/config.json 与安装链统一；默认 data_dir/config.json）",
    )
    args = parser.parse_args(argv)

    host, port = args.host, args.port
    # 中-4（第四轮体检批次B）启动安全闸：非回环监听 + 未配置 CXA_API_TOKEN 的组合
    # 意味着 LAN 内任意主机可无令牌调用 /api/computer/authorize 等端点（远程控制面
    # 完全暴露）。默认拒绝启动；CXA_ALLOW_UNSAFE=1 显式放行并打印醒目风险横幅。
    if not _is_loopback_host(host) and not _env_api_token():
        if os.environ.get("CXA_ALLOW_UNSAFE", "").strip() == "1":
            print("=" * 68)
            print("[安全警告] CXA_ALLOW_UNSAFE=1：API 服务将以【无令牌】模式绑定非回环地址")
            print(f"[安全警告] 监听 {host}:{port} —— LAN 内任意主机可调用电脑控制/授权端点！")
            print("[安全警告] 仅限临时调试使用，生产环境必须设置 CXA_API_TOKEN。")
            print("=" * 68)
        else:
            print(
                f"[ERROR] 拒绝启动：监听地址 {host} 为非回环地址且未设置 CXA_API_TOKEN，"
                "LAN 内任意主机可无令牌调用电脑控制等端点。"
            )
            print(
                "[ERROR] 请设置环境变量 CXA_API_TOKEN 启用令牌校验后重启；"
                "确有需要可临时设置 CXA_ALLOW_UNSAFE=1 强行放行（风险自负）。"
            )
            sys.exit(1)
    server = create_server(host=host, port=port, data_dir=args.data_dir, config_path=args.config)
    print(f"[INFO] 记忆 API 服务已启动: http://{host}:{port}/api/health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[INFO] 收到 Ctrl+C，正在优雅退出…")
    finally:
        server.server_close()
        print("[INFO] 服务已关闭")


if __name__ == "__main__":
    main()