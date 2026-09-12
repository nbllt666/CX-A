# -*- coding: utf-8 -*-
"""极简 CXFC 实现（Task G2 基线 + Task H1 relay 扩展）。

Task G2 基线：对齐 `.trae/documents/CX-A 补充文档 · ACP 与 CXFC.md` §3——砍掉
``direct`` 传输（插件自带 HTTP 服务）与 UDP 发现 / 心跳；保留 ``embedded``
进程内注册（一体两面注册内置工具用）。

Task H1 扩展（对齐 :file:`C:\\CX-O\\docs\\CXFC开发文档.md` §2.2 relay 协议）：
- ``register_relay``：登记 relay 目标（plugin_id + 工具描述列表 + 可注入 dispatcher）；
- ``call``：命中 relay 工具时投递 ``{type:"cxfc_relay_call", plugin_id, tool,
  arguments, request_id}`` 并在超时窗口内等待回报；
- 稳定错误码以返回值 ``error_code`` 表达（对齐 CX-O 返回外壳，**不抛异常**）：
  ``RELAY_UNREACHABLE``（目标未注册 / dispatcher 缺失 / 投递失败）、
  ``RELAY_TIMEOUT``（投递后窗口内无回报）、``REPLAY_DETECTED``（同 request_id
  在防重放窗口内重复作为调用键）；
- ``fulfill_relay``：被回报接口（api_server ``POST /api/cxfc/relay/result`` 调用）；
- ``pending_relay_calls``：取走待执行调用（api_server ``GET /api/cxfc/relay/pending`` 用）。

与 CX-O ``CXFCManager`` 的对照（见 :file:`C:\\CX-O\\CX-O-SERVER\\server\\core\\cxfc\\manager.py`）：
- CX-O 的 ``register_embedded_plugin`` 需 plugin_id + 工具/技能注册表、持久化 + 心跳任务
  （``_check_heartbeats_loop``）；本实现只维护进程内注册表 dict，无网络 / 心跳 / 发现；
- CX-O 的 relay 投递由 WebSocketManager 广播承载（``enable_relay_ws_dispatch``）；
  本实现以可注入 dispatcher 抽象同一投递点（生产由 api_server 注入 pending 队列
  投递——即本模块 ``pending_dispatcher()``，测试用 mock）；
- CX-O 的 ``call_tool`` 按 transport 分派到 relay / embedded / direct HTTP；本实现
  embedded 走进程内 ``handler(arguments)``，relay 走投递-等待-回报链路，direct 砍掉；
- CX-O 的 ``discovery.py``（UDP 广播 / 扫描）在本实现中被整体砍掉，无任何套接字创建。

线程语义：``call`` 的 relay 路径在**调用线程**内阻塞等待回报（threading.Event 带超时）；
``fulfill_relay`` / ``pending_relay_calls`` 可在**任意线程**调用（api_server 单线程
HTTP 线程 / 测试线程），内部以 ``threading.Lock`` 保护共享结构、以 per-call
``threading.Event`` 一对一唤醒等待方，支持多个 relay 调用并发等待、各自独立回填。

路径规范：本模块不写入磁盘文件；若需路径一律基于
``os.path.dirname(os.path.abspath(__file__))`` 推导，禁止相对路径。

本模块只使用标准库，可独立加载、无三方运行依赖。
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections import deque
from typing import Any, Callable, Dict, List, Optional

#: 本模块所在目录（供路径推导；本实现无磁盘 IO，仅声明规范）
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

#: 传输方式常量
TRANSPORT_EMBEDDED = "embedded"
TRANSPORT_RELAY = "relay"

#: relay 稳定错误码（以返回值 error_code 表达，对齐 CX-O 返回外壳，不抛异常）
RELAY_UNREACHABLE = "RELAY_UNREACHABLE"
RELAY_TIMEOUT = "RELAY_TIMEOUT"
REPLAY_DETECTED = "REPLAY_DETECTED"

#: relay 投递 dict 的 type 字段（对齐 CX-O WS 广播消息类型）
RELAY_CALL_TYPE = "cxfc_relay_call"

#: relay 默认超时窗口（秒）：投递后等待回报的上限（Task H1，参数可配）
DEFAULT_RELAY_TIMEOUT_S = 10.0
#: 防重放默认时间窗（秒）：同 request_id 在窗内重复作为调用键即拒绝
DEFAULT_REPLAY_WINDOW_S = 60.0

#: relay pending 队列上限（有界 deque，溢出丢最旧，防无界增长）
_MAX_PENDING_CALLS = 256
#: 已消费 request_id 集合硬上限（有界清理兜底，防极端量级下内存无界）
_MAX_CONSUMED_REQUEST_IDS = 4096
#: pending_relay_calls 单次默认取走条数
DEFAULT_PENDING_LIMIT = 50

#: 原生日志记录器
LOGGER = logging.getLogger(__name__)

__all__ = [
    "LiteCXFC",
    "CxfDisabled",
    "CxfToolNotFound",
    "TRANSPORT_EMBEDDED",
    "TRANSPORT_RELAY",
    "RELAY_UNREACHABLE",
    "RELAY_TIMEOUT",
    "REPLAY_DETECTED",
    "RELAY_CALL_TYPE",
    "DEFAULT_RELAY_TIMEOUT_S",
    "DEFAULT_REPLAY_WINDOW_S",
]


class CxfDisabled(Exception):
    """CXFC 总开关关闭（``enabled=False``）时，``register_embedded`` / ``call`` 抛出的自定义异常。

    含义：极简版 CXFC 默认关闭，需显式开启后才可注册 / 调用工具。
    Task H1 起 ``register_relay`` 同样受总开关约束（关闭时抛本异常）。
    """


class CxfToolNotFound(Exception):
    """调用未注册的工具时抛出的自定义异常。

    含义：调用方试图调用一个既不在进程内 embedded 注册表、也不在 relay
    工具索引中登记的工具 id。
    """


class LiteCXFC:
    """极简 CXFC：``embedded`` 进程内注册 + ``relay`` 投递-等待-回报传输。

    设计要点（与 CX-O 对照）：
    - **进程内注册表**：内部用 ``dict`` 保存 ``tool_id -> 描述``，不落盘、不跨进程；
    - **砍掉网络机制**：无 ``direct`` HTTP / 无 UDP 发现 / 无心跳 / 无独立插件进程；
      relay 投递以可注入 dispatcher 抽象（生产注入 pending 队列，测试 mock）；
    - **禁用即抛**：``enabled=False`` 时 ``register_embedded`` / ``register_relay`` /
      ``call`` 抛 :class:`CxfDisabled`；
    - **未注册即抛**：``call`` 传入未注册工具抛 :class:`CxfToolNotFound`；
    - **异常包装**：embedded handler 抛出任何异常时不向上抛，包装返回
      ``{success:false, error}``；relay 路径全程不抛，错误以 ``error_code`` 返回；
    - **防重放**：同 ``request_id`` 在 :attr:`replay_window_s` 窗口内重复作为调用键
      返回 ``REPLAY_DETECTED``；已消费 request_id 集合有界清理。

    :param enabled: CXFC 总开关；默认 False（轻量版默认关，用内置工具系统）
    :param embedded_only: 是否仅允许 ``embedded`` 传输；默认 True（True 时 relay
        注册被禁止，与既有语义一致）
    :param config: 配置（ConfigManager / 裸 dict / None）；非 None 时以 ``cxfc`` 段
        覆写上两开关及 ``relay_timeout_s`` / ``replay_window_s``
    :param relay_timeout_s: relay 调用回报等待窗口（秒），默认 10
    :param replay_window_s: 防重放时间窗（秒），默认 60
    """

    def __init__(
        self,
        enabled: bool = False,
        embedded_only: bool = True,
        config: Any = None,
        relay_timeout_s: float = DEFAULT_RELAY_TIMEOUT_S,
        replay_window_s: float = DEFAULT_REPLAY_WINDOW_S,
    ) -> None:
        #: 进程内注册表：tool_id -> 工具描述 dict（embedded）
        self._registry: Dict[str, Dict[str, Any]] = {}
        self.enabled = bool(enabled)
        self.embedded_only = bool(embedded_only)
        #: relay 调用回报等待窗口（秒）
        self.relay_timeout_s = float(relay_timeout_s)
        #: 防重放时间窗（秒）
        self.replay_window_s = float(replay_window_s)

        # 配置段覆盖（缺省段 / 键时回退构造参数携入的默认值）
        if config is not None:
            self.enabled = bool(self._get_config(config, "cxfc", "enabled", self.enabled))
            self.embedded_only = bool(
                self._get_config(config, "cxfc", "embedded_only", self.embedded_only)
            )
            self.relay_timeout_s = float(
                self._get_config(config, "cxfc", "relay_timeout_s", self.relay_timeout_s)
            )
            self.replay_window_s = float(
                self._get_config(config, "cxfc", "replay_window_s", self.replay_window_s)
            )

        # ---------------- relay 状态（Task H1，统一由 self._lock 保护） ----------------
        #: 共享锁：保护下方全部 relay 共享结构（目标表 / 工具索引 / 等待表 / 队列 / 已消费键）
        self._lock = threading.Lock()
        #: relay 目标注册表：plugin_id -> {plugin_id, dispatcher, tool_names, tools}
        self._relay_targets: Dict[str, Dict[str, Any]] = {}
        #: relay 工具名索引：tool name -> plugin_id（先注册者优先，供 call 分派）
        self._relay_tool_index: Dict[str, str] = {}
        #: 待执行 relay 调用队列（有界 deque）：元素为
        #: ``{type, plugin_id, tool, arguments, request_id}``
        self._pending_relay_calls: deque = deque(maxlen=_MAX_PENDING_CALLS)
        #: 等待回报表：request_id -> {event, payload, tool, request_id}
        self._relay_waiters: Dict[str, Dict[str, Any]] = {}
        #: 已消费 request_id：request_id -> 消费时刻（time.monotonic()），窗口外有界清理
        self._consumed_request_ids: Dict[str, float] = {}

    @staticmethod
    def _get_config(config: Any, section: str, key: str, default: Any) -> Any:
        """从 ConfigManager 或裸 dict 读取配置项；段/键缺失时返回 default。"""
        if isinstance(config, dict):
            sec = config.get(section)
            if isinstance(sec, dict):
                return sec.get(key, default)
            return default
        # 兼容 ConfigManager（get(section, key, default)）
        try:
            return config.get(section, key, default)
        except (TypeError, AttributeError):
            return default

    # ------------------------------------------------------------------ #
    # embedded 注册                                                      #
    # ------------------------------------------------------------------ #

    def register_embedded(
        self,
        tool_id: str,
        name: str,
        handler: Callable,
        description: str = "",
        parameters: Optional[Dict[str, Any]] = None,
        transport: str = TRANSPORT_EMBEDDED,
        returns: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """进程内注册一个工具（Callable handler），不走任何网络。

        - ``(tool_id, name, handler)`` 为必填；缺失抛 :class:`ValueError`；
        - 重复 ``tool_id`` 覆盖旧注册并打印 warning（覆盖语义）；
        - ``embedded_only=True`` 时 ``transport`` 仅允许 ``"embedded"``，
          传入其它值抛 :class:`ValueError`（禁止网络传输参数）。

        :param tool_id: 工具稳定标识（唯一键）
        :param name: 工具名
        :param handler: 进程内可调用对象，签名 ``handler(arguments: dict) -> Any``
        :param description: 工具描述（缺省空串）
        :param parameters: 参数 schema（缺省空 dict）
        :param transport: 传输方式，仅允许 ``"embedded"``
        :param returns: 返回结构描述（JSON Schema 片段，缺省空 dict；
            Task H1 补齐对齐 CX-O ``/tools`` 端点字段）
        :return: 注册成功返回 True
        :raises CxfDisabled: CXFC 总开关关闭
        :raises ValueError: 必填缺失，或 embedded_only 下 transport 非法
        """
        if not self.enabled:
            raise CxfDisabled("CXFC 未启用（enabled=False），禁止注册工具")
        if tool_id is None or not str(tool_id).strip():
            raise ValueError("tool_id 为必填参数")
        if name is None or not str(name).strip():
            raise ValueError("name 为必填参数")
        if not callable(handler):
            raise ValueError("handler 必须为可调用对象（Callable）")
        if self.embedded_only and transport != TRANSPORT_EMBEDDED:
            raise ValueError(
                f"embedded_only 模式仅允许 transport='{TRANSPORT_EMBEDDED}'，收到 {transport!r}；"
                f"embedded_only=True 时禁止 {TRANSPORT_RELAY} 等网络传输（relay 请以 "
                f"embedded_only=False 构造并走 register_relay）"
            )

        if tool_id in self._registry:
            LOGGER.warning(
                "重复注册工具 %r（name=%r），将以新 handler 覆盖旧注册", tool_id, name
            )

        self._registry[tool_id] = {
            "tool_id": tool_id,
            "name": str(name),
            "handler": handler,
            "description": description or "",
            "parameters": parameters or {},
            "transport": TRANSPORT_EMBEDDED,
            "returns": returns if isinstance(returns, dict) else {},
        }
        return True

    # ------------------------------------------------------------------ #
    # relay 注册与投递（Task H1）                                        #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _normalize_relay_tool(raw: Any) -> Dict[str, Any]:
        """把工具描述归一化为 ``{name, description, parameters, returns}``。

        对齐 CX-O ``/tools`` 端点（CXFC开发文档.md §3.2）字段结构；
        缺失字段以空值补齐，``name`` 缺失视为非法抛 :class:`ValueError`。
        """
        if not isinstance(raw, dict):
            raise ValueError("relay 工具描述必须为 dict（含 name 字段）")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ValueError("relay 工具描述必须含非空 name 字段")
        parameters = raw.get("parameters")
        returns = raw.get("returns")
        return {
            "name": name,
            "description": str(raw.get("description") or ""),
            "parameters": parameters if isinstance(parameters, dict) else {},
            "returns": returns if isinstance(returns, dict) else {},
        }

    def register_relay(
        self,
        plugin_id: str,
        tools: List[Dict[str, Any]],
        dispatcher: Optional[Callable] = None,
    ) -> bool:
        """登记一个 relay 传输目标（插件）及其工具描述列表。

        - ``enabled=False`` 抛 :class:`CxfDisabled`（与 embedded 注册同约束）；
        - ``embedded_only=True``（默认）抛 :class:`ValueError`（与既有语义一致，
          relay 传输需以 ``embedded_only=False`` 显式开启）；
        - 重复 ``plugin_id`` 覆盖旧注册并打印 warning（覆盖语义）；
        - ``dispatcher`` 抽象 = 可调用 ``dispatch(call_dict) -> None``：生产由
          api_server 注入 ``pending_dispatcher()``（投递进 pending 队列供前端
          ``GET /api/cxfc/relay/pending`` 拉取），测试注入 mock。缺失时 ``call``
          返回 ``RELAY_UNREACHABLE``（通道不可达）。

        :param plugin_id: relay 目标稳定标识（唯一键）
        :param tools: 工具描述列表，每项 ``{name, description?, parameters?, returns?}``
        :param dispatcher: 投递函数 ``dispatch(call_dict) -> None``；可为 None
        :return: 注册成功返回 True
        :raises CxfDisabled: CXFC 总开关关闭
        :raises ValueError: embedded_only 模式 / 必填缺失 / tools 形态非法
        """
        if not self.enabled:
            raise CxfDisabled("CXFC 未启用（enabled=False），禁止注册 relay 目标")
        if self.embedded_only:
            raise ValueError(
                "embedded_only 模式禁止 relay 传输：register_relay 被拒绝"
                "（需以 embedded_only=False 构造 LiteCXFC，与既有 embedded-only 语义一致）"
            )
        if plugin_id is None or not str(plugin_id).strip():
            raise ValueError("plugin_id 为必填参数")
        if tools is None or not isinstance(tools, (list, tuple)):
            raise ValueError("tools 必须为工具描述列表（list/tuple）")
        if dispatcher is not None and not callable(dispatcher):
            raise ValueError("dispatcher 必须为可调用对象（dispatch(call_dict)->None）或 None")

        pid = str(plugin_id).strip()
        normalized = [self._normalize_relay_tool(raw) for raw in tools]

        with self._lock:
            if pid in self._relay_targets:
                LOGGER.warning("重复注册 relay 目标 %r，将以新工具列表覆盖旧注册", pid)
                # 清理旧索引中指向该目标的工具名，避免覆盖后残留悬空映射
                for stale in [n for n, p in self._relay_tool_index.items() if p == pid]:
                    del self._relay_tool_index[stale]
            tools_map: Dict[str, Dict[str, Any]] = {}
            for desc in normalized:
                tool_name = desc["name"]
                if tool_name in tools_map:
                    LOGGER.warning("relay 目标 %r 内重复工具名 %r，保留首个", pid, tool_name)
                    continue
                tools_map[tool_name] = desc
                # 先注册者优先：同名工具已被其它目标占用时不抢占
                self._relay_tool_index.setdefault(tool_name, pid)
            self._relay_targets[pid] = {
                "plugin_id": pid,
                "dispatcher": dispatcher,
                "tool_names": list(tools_map.keys()),
                "tools": tools_map,
            }
        return True

    def pending_dispatcher(self) -> Callable:
        """返回内置 pending 队列投递器（``dispatch(call_dict) -> None``）。

        供 api_server 组装时注入 ``register_relay`` 的 ``dispatcher``：
        投递即把调用 dict 追加进有界 pending 队列，前端经
        ``GET /api/cxfc/relay/pending`` 取走执行后回报。
        """
        return self._enqueue_pending_relay_call

    def _enqueue_pending_relay_call(self, call_dict: Dict[str, Any]) -> None:
        """把 relay 调用 dict 投入 pending 队列（有界，溢出丢最旧并告警）。"""
        with self._lock:
            self._pending_relay_calls.append(call_dict)
        if len(self._pending_relay_calls) == self._pending_relay_calls.maxlen:
            LOGGER.warning(
                "relay pending 队列已满（%d 条），最旧调用将被覆盖", _MAX_PENDING_CALLS
            )

    # ------------------------------------------------------------------ #
    # 防重放（Task H1）                                                  #
    # ------------------------------------------------------------------ #

    def _reserve_request_id(self, request_id: str) -> bool:
        """登记调用键占用；窗口内已存在同键返回 False（重放）。

        顺带执行**有界清理**：清除超出 :attr:`replay_window_s` 窗口的过期键；
        超过硬上限 ``_MAX_CONSUMED_REQUEST_IDS`` 时丢最旧，防内存无界。
        必须在 :attr:`_lock` 保护语义下调用（本方法内部自行加锁）。
        """
        now = time.monotonic()
        with self._lock:
            # 窗口外过期键清理
            for stale in [
                k
                for k, ts in self._consumed_request_ids.items()
                if now - ts >= self.replay_window_s
            ]:
                del self._consumed_request_ids[stale]
            if request_id in self._consumed_request_ids:
                return False
            self._consumed_request_ids[request_id] = now
            # 硬上限兜底：丢最旧
            if len(self._consumed_request_ids) > _MAX_CONSUMED_REQUEST_IDS:
                oldest = min(self._consumed_request_ids, key=self._consumed_request_ids.get)
                del self._consumed_request_ids[oldest]
            return True

    def _release_request_id(self, request_id: str) -> None:
        """投递失败时回滚调用键占用（允许同键重试）。"""
        with self._lock:
            self._consumed_request_ids.pop(request_id, None)

    # ------------------------------------------------------------------ #
    # 调用分派（embedded 直调 + relay 投递-等待-回报）                    #
    # ------------------------------------------------------------------ #

    def call(
        self,
        tool_id: str,
        arguments: Optional[Dict[str, Any]] = None,
        *,
        request_id: Optional[str] = None,
        timeout_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        """调用已注册工具：embedded 直接执行 handler；relay 命中走投递-等待-回报链路。

        - 未注册的 ``tool_id``（embedded 与 relay 均未命中）抛
          :class:`CxfToolNotFound`（不静默，既有语义不变）；
        - embedded handler 抛出的任何异常被包装为 ``{success:false, error}``
          返回，**不向上抛**；成功返回 ``{success:true, result, tool}``；
        - relay 路径**全程不抛**，错误以 ``error_code`` 表达（对齐 CX-O 返回外壳）：
          目标未注册 / dispatcher 缺失 / 投递失败 → ``RELAY_UNREACHABLE``；
          窗口内无回报 → ``RELAY_TIMEOUT``；同 ``request_id`` 窗口内重复 →
          ``REPLAY_DETECTED``。

        :param tool_id: 工具稳定标识（embedded 的 tool_id 或 relay 工具名）
        :param arguments: 结构化工具参数（缺省空 dict）
        :param request_id: 仅 relay 路径生效——外部指定调用键（防重放检查依据）；
            缺省自动生成 uuid4 hex
        :param timeout_s: 仅 relay 路径生效——本次调用的回报等待窗口（秒）；
            缺省用 :attr:`relay_timeout_s`
        :return: embedded ``{success, result|error, tool}``；
            relay ``{success, result|error, tool, request_id}``
            （失败时附 ``error_code``）
        :raises CxfDisabled: CXFC 总开关关闭
        :raises CxfToolNotFound: 工具未注册（embedded 与 relay 均未命中）
        """
        if not self.enabled:
            raise CxfDisabled("CXFC 未启用（enabled=False），禁止调用工具")
        entry = self._registry.get(tool_id)
        if entry is not None:
            # ---- 既有 embedded 路径（行为零回归）----
            arguments = arguments or {}
            try:
                result = entry["handler"](arguments)
                return {"success": True, "result": result, "tool": tool_id}
            except Exception as exc:  # noqa: BLE001 - handler 异常包装返回，不向上抛
                LOGGER.error("CXFC 工具 %r 调用失败：%s", tool_id, exc)
                return {"success": False, "error": str(exc), "tool": tool_id}

        # ---- relay 分派：工具名索引 -> 目标 ----
        with self._lock:
            plugin_id = self._relay_tool_index.get(tool_id)
            target = self._relay_targets.get(plugin_id) if plugin_id else None
        if plugin_id is not None and target is None:
            # 工具索引存在但目标条目缺失（目标未注册 / 内部不一致）→ 通道不可达
            req_id = str(request_id).strip() if request_id else uuid.uuid4().hex
            LOGGER.warning("relay 工具 %r 索引指向未注册目标 %r", tool_id, plugin_id)
            return {
                "success": False,
                "error_code": RELAY_UNREACHABLE,
                "error": f"relay 目标未注册：{plugin_id!r}",
                "tool": tool_id,
                "request_id": req_id,
            }
        if plugin_id is None:
            raise CxfToolNotFound(f"CXFC 工具未注册：{tool_id!r}")
        return self._call_relay(plugin_id, tool_id, arguments, request_id, timeout_s)

    def _call_relay(
        self,
        plugin_id: str,
        tool_id: str,
        arguments: Optional[Dict[str, Any]],
        request_id: Optional[str],
        timeout_s: Optional[float],
    ) -> Dict[str, Any]:
        """relay 路径：参数校验 → 防重放 → 投递 → 等待回报 → 组装返回外壳。

        等待实现：per-call ``threading.Event`` 带超时等待，回报线程经
        :meth:`fulfill_relay` 填充载荷后唤醒；超时后清理等待者（迟到回报
        将得到 False，api_server 映射 404）。
        """
        # 投递契约：arguments 必须为对象（无效形态不占用调用键，直接返回错误外壳）
        if arguments is not None and not isinstance(arguments, dict):
            return {
                "success": False,
                "error": "arguments 必须为对象（dict）",
                "tool": tool_id,
            }
        req_id = str(request_id).strip() if request_id else uuid.uuid4().hex
        wait_s = max(0.0, self.relay_timeout_s if timeout_s is None else float(timeout_s))
        tool = tool_id

        # 1) 防重放：同 request_id 在窗口内重复作为调用键 → 拒绝
        if not self._reserve_request_id(req_id):
            LOGGER.warning("relay 调用键重放被拒：request_id=%r", req_id)
            return {
                "success": False,
                "error_code": REPLAY_DETECTED,
                "error": (
                    f"request_id {req_id!r} 在防重放窗口（{self.replay_window_s:g}s）内"
                    "已作为调用键消费"
                ),
                "tool": tool,
                "request_id": req_id,
            }

        # 2) 通道校验：目标缺失 / dispatcher 缺失 → RELAY_UNREACHABLE
        with self._lock:
            target = self._relay_targets.get(plugin_id)
            dispatcher = target.get("dispatcher") if target else None
        if dispatcher is None:
            self._release_request_id(req_id)  # 未投递，回滚调用键允许重试
            return {
                "success": False,
                "error_code": RELAY_UNREACHABLE,
                "error": f"relay 目标 {plugin_id!r} 的投递通道（dispatcher）缺失",
                "tool": tool,
                "request_id": req_id,
            }

        # 3) 注册等待者（先登记再投递：同步回报的 dispatcher 可立即 fulfill）
        waiter: Dict[str, Any] = {
            "event": threading.Event(),
            "payload": None,
            "tool": tool,
            "request_id": req_id,
        }
        with self._lock:
            self._relay_waiters[req_id] = waiter

        # 4) 投递（dispatcher 异常视为通道不可达）
        call_dict = {
            "type": RELAY_CALL_TYPE,
            "plugin_id": plugin_id,
            "tool": tool,
            "arguments": dict(arguments or {}),
            "request_id": req_id,
        }
        try:
            dispatcher(call_dict)
        except Exception as exc:  # noqa: BLE001 - 投递失败包装为 RELAY_UNREACHABLE
            with self._lock:
                self._relay_waiters.pop(req_id, None)
            self._release_request_id(req_id)
            LOGGER.error("relay 投递失败 plugin=%r tool=%r：%s", plugin_id, tool, exc)
            return {
                "success": False,
                "error_code": RELAY_UNREACHABLE,
                "error": f"relay 投递失败：{exc}",
                "tool": tool,
                "request_id": req_id,
            }

        # 5) 等待回报（调用线程阻塞，Event 一对一唤醒）
        signaled = waiter["event"].wait(timeout=wait_s)
        if signaled and waiter["payload"] is not None:
            return dict(waiter["payload"])

        # 6) 超时（或防御分支： signaled 但载荷缺失）→ 清理等待者
        with self._lock:
            self._relay_waiters.pop(req_id, None)
        return {
            "success": False,
            "error_code": RELAY_TIMEOUT,
            "error": f"relay 回报等待超时（{wait_s:g}s）",
            "tool": tool,
            "request_id": req_id,
        }

    def fulfill_relay(self, request_id: str, success: bool, result_or_error: Any = None) -> bool:
        """回填一个等待中的 relay 调用（api_server ``POST /api/cxfc/relay/result`` 调用）。

        - ``request_id`` 未命中等待者（未知 / 已超时清理 / 已被回报过）→ 返回
          False（api_server 映射 404）；
        - 命中：按 ``success`` 组装 ``{success, result|error, tool, request_id}``
          载荷，填充后经 ``threading.Event`` 唤醒等待线程，返回 True。

        线程语义：可在**任意线程**调用（api_server 单线程 HTTP 线程 / 测试线程）；
        等待表由 :attr:`_lock` 保护，与 ``call`` 侧等待线程通过 per-call Event
        一对一唤醒；支持多个 relay 调用并发等待、各自独立回填。

        :param request_id: 被回报的调用键
        :param success: 前端执行是否成功
        :param result_or_error: 成功时为结果载荷；失败时为错误描述
        :return: 是否命中并回填了等待中的调用
        :raises ValueError: request_id 为空或 success 非布尔
        """
        if not isinstance(success, bool):
            raise ValueError("success 必须为布尔值")
        req_id = str(request_id or "").strip()
        if not req_id:
            raise ValueError("request_id 为必填参数")
        with self._lock:
            waiter = self._relay_waiters.pop(req_id, None)
        if waiter is None:
            return False
        if success:
            payload = {
                "success": True,
                "result": result_or_error,
                "tool": waiter["tool"],
                "request_id": req_id,
            }
        else:
            payload = {
                "success": False,
                "error": result_or_error,
                "tool": waiter["tool"],
                "request_id": req_id,
            }
        waiter["payload"] = payload
        waiter["event"].set()
        return True

    def pending_relay_calls(
        self,
        limit: int = DEFAULT_PENDING_LIMIT,
        plugin_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """取走（并从队列移除）至多 ``limit`` 条待执行 relay 调用。

        - **at-most-once 取走语义**：取走的调用不会重复下发；前端取走后未回报，
          调用方最终在超时窗口后收到 ``RELAY_TIMEOUT``；
        - ``plugin_id`` 过滤：仅取走该目标的调用，其它目标的调用保留在队列；
        - ``limit <= 0`` 返回空列表。

        :param limit: 单次取走条数上限
        :param plugin_id: 可选目标过滤
        :return: 调用 dict 列表，每项 ``{type, plugin_id, tool, arguments, request_id}``
        """
        try:
            n = int(limit)
        except (TypeError, ValueError):
            n = DEFAULT_PENDING_LIMIT
        if n <= 0:
            return []
        taken: List[Dict[str, Any]] = []
        with self._lock:
            kept: List[Dict[str, Any]] = []
            while self._pending_relay_calls and len(taken) < n:
                item = self._pending_relay_calls.popleft()
                if plugin_id is not None and item.get("plugin_id") != plugin_id:
                    kept.append(item)
                else:
                    taken.append(item)
            # 未取走的按原顺序放回队首（保持原 deque 对象与 maxlen 不变）
            while kept:
                self._pending_relay_calls.appendleft(kept.pop())
        return taken

    # ------------------------------------------------------------------ #
    # 工具列表                                                           #
    # ------------------------------------------------------------------ #

    def list_tools(self) -> List[Dict[str, Any]]:
        """返回已注册工具的描述列表（不含 handler / dispatcher 可调用对象）。

        Task H1 结构补齐：embedded 与 relay 输出**同构**，均含
        ``tool_id / name / description / parameters / returns / transport``
        （对齐 CX-O ``/tools`` 端点字段）；relay 工具额外携带 ``plugin_id``
        供回报路由归因，``tool_id`` 即工具名（与 :meth:`call` 分派键一致）。

        :return: 工具描述 dict 列表
        """
        result: List[Dict[str, Any]] = []
        for entry in self._registry.values():
            result.append(
                {
                    "tool_id": entry["tool_id"],
                    "name": entry["name"],
                    "description": entry["description"],
                    "parameters": entry["parameters"],
                    "returns": entry.get("returns") or {},
                    "transport": entry["transport"],
                }
            )
        with self._lock:
            for target in self._relay_targets.values():
                for name in target["tool_names"]:
                    desc = target["tools"][name]
                    result.append(
                        {
                            "tool_id": name,
                            "name": name,
                            "description": desc["description"],
                            "parameters": desc["parameters"],
                            "returns": desc["returns"],
                            "transport": TRANSPORT_RELAY,
                            "plugin_id": target["plugin_id"],
                        }
                    )
        return result
