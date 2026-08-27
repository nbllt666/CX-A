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
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

# 聊天服务未启用守卫错误码（前端本期走 Mock 演示，端点存在但明确提示）
CHAT_SERVICE_DISABLED = "chat_service_disabled"

# 云端 provider 白名单（与 lite/cloud/adapter.py 支持的 provider 对齐）
CLOUD_PROVIDER_ALLOWLIST = ("deepseek", "tongyi", "openai", "moonshot")

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

# 默认数据目录：项目根目录下 data/（与 storage._default_db_path 的 data/memories.db 一致）
DEFAULT_DATA_DIR = os.path.join(_PROJECT_ROOT, "data")
# 默认监听端口（与前端 API_PORT 一致）
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8600


def _resolve_data_dir(data_dir=None) -> str:
    """归一化数据目录：未显式提供时回落项目根 data/。"""
    return data_dir or DEFAULT_DATA_DIR


def build_deps(data_dir=None):
    """组装服务依赖。

    Args:
        data_dir: 数据目录（None 用项目根 data/）。

    Returns:
        tuple[MemoryStore, MemoryRetrievalPipeline, AgentManager, RemoteController]:
            存储实例、检索管线、本地 Agent 管理器与远端遥控控制器，四者共享同一
            data_dir。遥控控制器由 data_dir 下 config.json 的 remote 段驱动
            （默认 enabled=false），测试时各自注入 mock transport。
    """
    data_dir = _resolve_data_dir(data_dir)
    os.makedirs(data_dir, exist_ok=True)
    # M5 配置接线：读取 memory 段注入检索管线（缺省值与 DEFAULTS["memory"] 一致），
    # pipeline 内部会把 dedup/permanent_threshold 透传给其持有的 MemoryManager
    config = ConfigManager(config_path=os.path.join(data_dir, "config.json"))
    store = MemoryStore(db_path=os.path.join(data_dir, "memories.db"))
    embed = LiteEmbeddingProvider(dim=64)
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


def make_handler(store, pipeline, manager=None, remote=None, computer=None, authorizer=None, bridge=None, config=None):
    """基于指定依赖构建处理器类（闭包绑定 store / pipeline / manager / remote / computer，便于测试隔离）。

    Args:
        config: 可选 ConfigManager 实例（提供 /api/settings 读写；缺省新建，
            默认读写项目根 data/config.json）。
    """
    if manager is None:
        manager = AgentManager()
    if remote is None:
        remote = RemoteController()
    if bridge is None:
        computer, authorizer, bridge = build_computer_deps(DEFAULT_DATA_DIR)
    if config is None:
        config = ConfigManager(config_path=os.path.join(DEFAULT_DATA_DIR, "config.json"))

    class ApiHandler(BaseHTTPRequestHandler):
        """REST 请求处理器。单线程 HTTPServer 内串行执行，无共享状态竞争。"""

        server_version = "CXLiteAPI/0.1"
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
                    "Access-Control-Allow-Headers": "Content-Type",
                }
            return {}

        def _deny_bad_host(self):
            """Host 校验失败的统一 403 响应。"""
            self._send_json({"ok": False, "error": "forbidden", "message": "Host 校验失败：拒绝访问"}, 403)

        def _guard_internal_error(self, exc):
            """全局异常兜底：回结构化 500，确保畸形输入不致连接中断。"""
            self._send_json({"ok": False, "error": "internal error", "detail": str(exc)[:200]}, 500)

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
                    "version": "0.1.0",
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
            """处理 GET：/api/health、/api/status、/api/settings、/api/chat/history、记忆/Agent/远端接口。"""
            if not self._check_host():
                self._deny_bad_host()
                return
            try:
                path = urlparse(self.path).path
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

                self._send_json({"ok": False, "error": "not_found", "message": f"未找到接口 {path}"}, 404)
            except Exception as exc:  # noqa: BLE001 - 兜底：任何畸形输入都得到结构化 500 而非连接中断
                # SystemExit / KeyboardInterrupt 继承 BaseException，不会被此处捕获
                self._guard_internal_error(exc)

        def do_POST(self):
            """处理 POST：/api/chat/messages、Agent/远端/电脑控制接口。"""
            if not self._check_host():
                self._deny_bad_host()
                return
            try:
                path = urlparse(self.path).path
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
                self._send_json({"ok": False, "error": "not_found", "message": f"未找到接口 {path}"}, 404)
            except Exception as exc:  # noqa: BLE001 - 兜底：结构化 500 而非连接中断
                self._guard_internal_error(exc)

        def do_PUT(self):
            """处理 PUT：/api/settings 更新配置；/api/agents/{id} 更新指定 Agent。"""
            if not self._check_host():
                self._deny_bad_host()
                return
            try:
                path = urlparse(self.path).path
                if path == "/api/settings":
                    self._handle_settings_update()
                    return
                agent_id = self._extract_agents_id(path)
                if agent_id is None:
                    self._send_json({"ok": False, "error": "not_found", "message": f"未找到接口 {path}"}, 404)
                    return
                self._handle_agents_update(agent_id)
            except Exception as exc:  # noqa: BLE001 - 兜底：结构化 500 而非连接中断
                self._guard_internal_error(exc)

        def do_DELETE(self):
            """处理 DELETE：/api/memories/{id} 软删除 / /api/agents/{id} 删除 Agent。"""
            if not self._check_host():
                self._deny_bad_host()
                return
            try:
                path = urlparse(self.path).path
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

            deleted = self._store.soft_delete(memory_id)
            if deleted:
                self._send_json({"ok": True, "id": memory_id})
            else:
                self._send_json({"ok": False, "error": "not_found", "message": f"记忆 {memory_id} 不存在"}, 404)

        # ------------------------------------------------------------ 各接口实现
        def _handle_list(self, query):
            """记忆列表：支持 type / agent_id / limit 过滤，默认仅返回未软删除记录。"""
            limit = None
            if query.get("limit") is not None:
                try:
                    limit = int(query["limit"])
                except ValueError:
                    self._send_json({"ok": False, "error": "bad_request", "message": "limit 必须是整数"}, 400)
                    return
            try:
                rows = self._store.list(
                    type=query.get("type"),
                    agent_id=query.get("agent_id"),
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
                try:
                    top_k = int(query["top_k"])
                except ValueError:
                    self._send_json({"ok": False, "error": "bad_request", "message": "top_k 必须是整数"}, 400)
                    return
            agent_id = query.get("agent_id") or "default"
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
            arguments = body.get("arguments") if isinstance(body.get("arguments"), dict) else {}
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
                payload = {"ok": False, "error": exc.message, "error_code": exc.error_code}
                # L3：仅授权类错误才标注 authorized:false；其他 PluginError 省略该键
                if isinstance(exc, NotAuthorizedError):
                    payload["authorized"] = False
                self._send_json(payload, exc.http_status)
                return
            self._send_json(payload)

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
            """更新 Agent：body 为任意可更新字段（name/persona/voice/enabled）。"""
            body = self._read_body_json()
            if body is None:
                self._reject_bad_json()
                return
            try:
                agent = self._manager.update(agent_id, **body)
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


def create_app(data_dir=None):
    """创建完整应用依赖并返回 (store, pipeline, handler_class)。

    Args:
        data_dir: 数据目录（None 用项目根 data/）。

    Returns:
        tuple: (store, pipeline, handler) -> (MemoryStore, MemoryRetrievalPipeline, ApiHandler)。
    """
    data_dir = _resolve_data_dir(data_dir)
    store, pipeline, manager, remote = build_deps(data_dir)
    computer, authorizer, bridge = build_computer_deps(data_dir)
    #: 配置实例与 remote 同源（同一 data_dir 下 config.json），供 /api/settings 读写，
    #: 保证测试隔离（不触碰项目根运行时配置）。
    config = ConfigManager(config_path=os.path.join(data_dir, "config.json"))
    handler = make_handler(
        store, pipeline, manager, remote,
        computer=computer, authorizer=authorizer, bridge=bridge, config=config,
    )
    return store, pipeline, handler


def create_server(host=DEFAULT_HOST, port=DEFAULT_PORT, data_dir=None):
    """构建并返回配置好的 HTTPServer（单线程串行处理）。"""
    _store, _pipeline, handler = create_app(data_dir)
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
    args = parser.parse_args(argv)

    host, port = args.host, args.port
    server = create_server(host=host, port=port, data_dir=args.data_dir)
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