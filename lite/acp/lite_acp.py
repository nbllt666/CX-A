# -*- coding: utf-8 -*-
"""轻量版 ACP（Task G1）——保留通信核心，砍掉重量级局域网机制。

依据《CX-A 补充文档 · ACP 与 CXFC》§2：ACP 改造保留——保留 Agent 注册 /
消息路由 / 心跳核心；局域网发现与分组协作降为可选（默认关）；新增云端中转
（跨公网通信）。消息结构 ``{action, request_id, data}`` 对齐 CX-O §3.3。

轻量设计（不复用 CX-O 的重型 asyncio/单例/锁/广播）：主路径为**单线程内存实现**
（无锁设计以此前提成立）；自 T2 起 :meth:`LiteACP.start_heartbeat` 会拉起一个
后台 **daemon 线程**并发清扫 agents 注册表——多线程接入前须外部串行化或后续加锁：
- 存储：内存 dict（agent registry / 路由信箱 / 事件 handler / 分组），无持久化；
  主路径无线程，唯心跳清扫运行在独立后台 daemon 线程。
- 开关：``enabled=False`` 时核心操作抛 :class:`AcpDisabled`；
  ``lan_discovery=False`` / ``group_enabled=False`` / ``cloud_relay=False``
  时对应能力抛 :class:`AcpDisabled`。
- 消息结构常量 :data:`MSG_STRUCT = {"action", "request_id", "data"}` 供跨版本断言对齐。

路径规范：本项目一律基于 ``os.path.dirname(os.path.abspath(__file__))`` 推导，
不使用相对路径。

------------------------------------------------------------------------------
节点互联层（20261017 CX-O 全量迁移批次 1，recon_acp.md §9）
------------------------------------------------------------------------------
以 CX-O ``ACPManager`` 语义为准新增的**节点互联**能力（CX-A 本机单角色，ACP
用途 = 与局域网内 CX-O/CX-A 节点互发消息）：

- **独立注册表** ``self.nodes`` / ``self.connections`` / ``self.node_messages``
  （CX-O AgentInfo/ConnectionInfo/wire 消息形态，:mod:`lite.acp.wire` 冻结契约）
  ——与上方 lite 旧注册表 ``self.agents``（信箱路由语义，既有测试基线）**并存
  不混用**；旧方法零改动。
- 持久化：``<app_root>/data/acp/nodes.json`` / ``connections.json``——
  ``metadata.source`` 属 :data:`lite.acp.wire.LOCAL_AGENT_SOURCES` 的本机动态
  节点不落盘（对齐 CX-O）；
- 消息投递：外部节点 urllib POST（port==9999 视为 CX-O 主系统，改投其
  ``/api/acp/receive``；其余 POST ``/acp/message``）；消息槽按对端分键
  （500 条/键 FIFO，仅内存）；
- 心跳清扫扩展：对 nodes 中 online 的外部节点，last_seen 超时先 GET
  ``/health`` 探活（port==9999 的主系统节点不可知其 HTTP 端口，仅按信标
  新鲜度判定）再决定刷新或置 offline。
"""

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime

from lite.config.paths import app_root

from .wire import (
    LOCAL_AGENT_SOURCES,
    MAX_MESSAGES_PER_KEY,
    agent_info_from_beacon,
    make_agent_info,
    make_connection_info,
    make_message,
    message_from_wire,
    now_iso,
)
from .cloud_relay import CloudRelay
from .discovery import LiteLanDiscovery

#: 原生日志记录器
LOGGER = logging.getLogger(__name__)

#: 消息结构字段集合（对齐 CX-O §3.3，供跨版本互通断言）
MSG_STRUCT = {"action", "request_id", "data"}

#: 单个路由信箱的消息容量上限（M-18，第三轮体检批次5）：超限截断最旧，
#: 防同进程高频 route_message 造成内存无界增长。
MAX_MAILBOX = 256


def _now():
    """当前单调时钟（可测试注入点）。"""
    return time.monotonic()


def _elapsed_since(iso_ts, now_wall=None):
    """ISO8601 时间戳距 now_wall（墙钟秒）的秒数；解析失败返回 None。"""
    try:
        past = datetime.fromisoformat(str(iso_ts))
    except (ValueError, TypeError):
        return None
    now_wall = time.time() if now_wall is None else now_wall
    return max(0.0, now_wall - past.timestamp())


class AcpDisabled(RuntimeError):
    """ACP 或其下某项能力被配置关闭时抛出。

    触发场景：``acp.enabled=False`` 调用注册/路由/心跳等核心操作；或
    ``acp.lan_discovery=False`` / ``acp.group_enabled=False`` /
    ``acp.cloud_relay=False`` 时调用对应能力。
    """


class AcpAgentNotFound(KeyError):
    """路由/访问目标 Agent 未注册时抛出。"""


class LiteACP:
    """轻量版 ACP：Agent 注册 / 消息路由 / 心跳 + 可选局域网发现 + 云端中转。

    主路径单线程内存实现：所有状态存于内存 dict，无异步、无锁、无持久化
    （无锁以单线程前提成立）。注意：:meth:`start_heartbeat` 会拉起后台 daemon
    线程并发清扫 agents 注册表——多线程接入前须外部串行化或后续加锁。
    构造时从配置 ``acp`` 段读取开关与默认参数。
    """

    def __init__(self, config=None, relay_transport=None, data_dir=None):
        """构造轻量 ACP。

        :param config: 配置源。可为 ``ConfigManager`` 实例（用其 ``get(section,key)``）
            或 ``{section: {key: value}}`` 映射；缺省内部创建现实 ConfigManager。
        :param relay_transport: 可选注入的云端中转 transport（含 ``send()`` /
            ``is_reachable()``），缺省在 ``relay_via_cloud`` 时按配置创建 :class:`CloudRelay`。
        :param data_dir: 节点互联层持久化目录（nodes.json / connections.json 所在）；
            缺省 ``<app_root>/data/acp``（frozen-aware）；测试注入 tmp_path 隔离。
        """
        #: 配置源（ConfigManager 或 dict，均可经 _get 读取）
        self._cfg = config
        self._enabled = bool(self._get("acp", "enabled", False))
        self.agent_id = self._get("acp", "agent_id", "cxa-agent-001")
        self.agent_name = str(self._get("acp", "agent_name", "CX-A") or "CX-A")
        self.heartbeat_interval = float(self._get("acp", "heartbeat_interval", 10))
        #: 外部节点离线判定阈值（秒；对齐 CX-O acp.connection.timeout=30）
        self.heartbeat_timeout = float(self._get("acp", "heartbeat_timeout", 30))
        #: 跨节点消息投递 / 健康探活超时（秒；对齐 CX-O 5s）
        self.delivery_timeout = float(self._get("acp", "delivery_timeout_s", 5))
        self._lan_discovery_enabled = bool(self._get("acp", "lan_discovery", False))
        self._group_enabled = bool(self._get("acp", "group_enabled", False))
        self._cloud_relay_enabled = bool(self._get("acp", "cloud_relay", True))
        self._cloud_relay_endpoint = self._get("acp", "cloud_relay_endpoint", "")
        #: 云端中转 transport（注入 mock 或 CloudRelay）
        self._relay_transport = relay_transport
        #: 云端中转惰性缓存（仅在注入缺省时按配置创建一次）
        self._relay = None

        #: Agent 注册表：{agent_id: {"manifest":..., "last_seen":..., "status":...}}
        self.agents = {}
        #: 路由信箱：{to_id: [ {action, request_id, data}, ... ]}
        self._routes = {}
        #: 事件 handler 注册表：{action: callable(message)}
        self._handlers = {}
        #: 分组表：{group_id: {"members": [...], "created": ts}}
        self._groups = {}
        #: 后台心跳清扫线程（daemon，可选启动）
        self._heartbeat_thread = None
        self._stop_evt = threading.Event()

        # ---------------- 节点互联层状态（20261017，与上方 lite 旧语义并存） ----------------
        #: 节点注册表：{agent_id: AgentInfo dict}（wire 契约形态，见 lite.acp.wire）
        self.nodes = {}
        #: 连接表（通讯录条目）：{connection_id: ConnectionInfo dict}
        self.connections = {}
        #: 节点消息槽：{对端 agent_id: [wire 消息,...]}（仅内存，500 条/键 FIFO）
        self.node_messages = {}
        #: 互联层共享锁（心跳线程 / HTTP 线程 / 发现线程并发接入）
        self._store_lock = threading.RLock()
        #: 持久化目录（测试注入 tmp_path；缺省 <app_root>/data/acp）
        self._data_dir = data_dir or os.path.join(app_root(), "data", "acp")
        #: 常驻 UDP 发现（批次 2）：发现器实例 / 广播线程 / 停止哨兵 / 宣告端口
        self._node_discovery = None
        self._discovery_thread = None
        self._discovery_stop = threading.Event()
        self._node_http_port = 0

    # ------------------------------------------------------------------ #
    # 内部：配置读取 / 启用闸门                                           #
    # ------------------------------------------------------------------ #

    def _get(self, section, key, default=None):
        """从配置源读取 (section, key)；兼容 ConfigManager 与 dict。"""
        if self._cfg is None:
            return default
        # 先按 dict 处理（dict 也自带 .get，需优先判定以区分 ConfigManager）
        if isinstance(self._cfg, dict):
            section_map = self._cfg.get(section)
            if not isinstance(section_map, dict):
                return default
            return section_map.get(key, default)
        # 其余按 ConfigManager 风格（get(section, key, default)）
        getter = getattr(self._cfg, "get", None)
        if callable(getter):
            return getter(section, key, default)
        return default

    def _require_enabled(self):
        """核心操作前置闸门：ACP 关闭时抛 :class:`AcpDisabled`。"""
        if not self._enabled:
            raise AcpDisabled("ACP 未启用（config.acp.enabled=False）")

    def set_enabled(self, enabled):
        """热切换总开关（PUT /api/settings acp.enabled 热接线专用）。

        实例的 ``_enabled`` 是构造时快照；配置热更后须经本方法同步，否则
        initialize 等闸门方法仍按旧值拒绝（20261017 批次 3 实测）。

        :param enabled: True 开启 / False 关闭
        :return: 切换后的开关值
        """
        self._enabled = bool(enabled)
        return self._enabled

    # ------------------------------------------------------------------ #
    # 保留核心：注册 / 注销 / 心跳 / 状态                                  #
    # ------------------------------------------------------------------ #

    def register_agent(self, agent_id, manifest=None):
        """注册 Agent。

        注册即视为在线（last_seen 置为当前时刻）。重复注册覆盖 manifest 并刷新心跳。

        :param agent_id: Agent 标识
        :param manifest: Agent 描述/能力清单（可选）
        :return: True 注册成功
        :raises AcpDisabled: ACP 未启用
        """
        self._require_enabled()
        if not agent_id:
            return False
        self.agents[agent_id] = {
            "manifest": manifest or {},
            "last_seen": _now(),
            "status": "online",
        }
        return True

    def unregister_agent(self, agent_id):
        """注销 Agent。

        幂等：存在则移除（含其路由信箱）并返回 True；不存在返回 False，不报错。

        :raises AcpDisabled: ACP 未启用
        """
        self._require_enabled()
        existed = agent_id in self.agents
        self.agents.pop(agent_id, None)
        self._routes.pop(agent_id, None)
        return existed

    def heartbeat(self, agent_id):
        """刷新 Agent 心跳并返回自上次心跳经过的秒数。

        返回 0.0 表示该 Agent 此前未被记录（或首次心跳）。
        """
        self._require_enabled()
        record = self.agents.get(agent_id)
        if record is None:
            return 0.0
        prev = record["last_seen"]
        now = _now()
        record["last_seen"] = now
        record["status"] = "online"
        return now - prev

    def status(self, agent_id):
        """查询 Agent 在线状态。

        - ``online``：已注册且心跳未超时（未超过 2×heartbeat_interval）
        - ``offline``：已注册但心跳超时
        - ``unknown``：未注册
        """
        self._require_enabled()
        record = self.agents.get(agent_id)
        if record is None:
            return "unknown"
        if _now() - record["last_seen"] > 2 * self.heartbeat_interval:
            return "offline"
        return "online"

    # ------------------------------------------------------------------ #
    # 心跳清扫：后台线程把超时 Agent 自动置 offline（对齐 CX-O 心跳循环）   #
    # ------------------------------------------------------------------ #

    def start_heartbeat(self):
        """启动后台心跳清扫线程（幂等，daemon）。

        每 ``heartbeat_interval`` 秒执行一次 :meth:`_heartbeat_tick`，把心跳超时
        （>2×interval）未刷新的 Agent 状态刷新为 ``offline`` 并派发 ``offline`` 事件，
        使「跨节点自动离线」真正落地。ACP 未启用时不启动。

        :return: True 已启动 / 已在运行；False ACP 关闭拒绝启动
        """
        if not self._enabled:
            return False
        if self._heartbeat_thread is not None and self._heartbeat_thread.is_alive():
            return True
        self._stop_evt.clear()
        thread = threading.Thread(
            target=self._heartbeat_loop,
            name="cxa-acp-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread = thread
        thread.start()
        LOGGER.info("ACP 心跳清扫线程已启动（interval=%ss）", self.heartbeat_interval)
        return True

    def stop_heartbeat(self):
        """停止后台心跳线程（幂等，join 超时保护），不阻塞主流程。"""
        self._stop_evt.set()
        thread = self._heartbeat_thread
        self._heartbeat_thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(1.0, self.heartbeat_interval + 1.0))

    def _heartbeat_loop(self):
        """后台循环：等待 interval 秒后被唤醒执行一次清扫。"""
        while not self._stop_evt.wait(self.heartbeat_interval):
            try:
                self._heartbeat_tick()
            except Exception as exc:  # noqa: BLE001 - 清扫失败不 kill 线程
                LOGGER.warning("ACP 心跳清扫失败：%s", exc)

    def _heartbeat_tick(self):
        """心跳清扫一次：把心跳超时且仍为 online 的 Agent 置 offline 并派发事件。"""
        self._require_enabled()
        threshold = 2 * self.heartbeat_interval
        now = _now()
        for agent_id, record in list(self.agents.items()):
            if now - record["last_seen"] > threshold and record["status"] != "offline":
                record["status"] = "offline"
                LOGGER.info("ACP Agent %s 心跳超时，已置为 offline", agent_id)
                self.handle("offline", {"agent_id": agent_id})
        # 节点互联层清扫（20261017）：外部节点 last_seen 超时 → 探活或置 offline
        try:
            self._sweep_nodes()
        except Exception as exc:  # noqa: BLE001 - 节点清扫失败不 kill 心跳线程
            LOGGER.warning("ACP 节点清扫失败：%s", exc)

    # ------------------------------------------------------------------ #
    # 保留核心：消息路由                                                   #
    # ------------------------------------------------------------------ #

    def route_message(self, from_id, to_id, payload):
        """路由一条消息。

        分配 request_id，按 ``{action: "message", request_id, data: {from, to, payload}}``
        结构与 CX-O §3.3 对齐，写入目标 Agent 的路由信箱。

        :param from_id: 发送方 Agent ID
        :param to_id: 接收方 Agent ID（必须已注册）
        :param payload: 消息负载
        :return: 分配的 request_id
        :raises AcpAgentNotFound: to_id 未注册
        :raises AcpDisabled: ACP 未启用
        """
        self._require_enabled()
        if to_id not in self.agents:
            raise AcpAgentNotFound(f"目标 Agent 未注册：{to_id}")
        request_id = uuid.uuid4().hex
        message = {
            "action": "message",
            "request_id": request_id,
            "data": {"from": from_id, "to": to_id, "payload": payload},
        }
        # M-18（第三轮体检批次5）：信箱容量上限——高频 route_message 时截断
        # 最旧消息，防止内存无界增长（N9 的 found_agents 上限语义对齐）
        mailbox = self._routes.setdefault(to_id, [])
        mailbox.append(message)
        if len(mailbox) > MAX_MAILBOX:
            del mailbox[: len(mailbox) - MAX_MAILBOX]
        return request_id

    def get_messages(self, agent_id):
        """读取指定 Agent 的路由信箱（只读建议）。"""
        return list(self._routes.get(agent_id, []))

    # ------------------------------------------------------------------ #
    # 事件订阅：on(action) / handle(action)                                #
    # ------------------------------------------------------------------ #

    def on(self, action, handler=None):
        """注册 action 的 handler。

        支持两种用法：
        - 装饰器：``@acp.on("ping")``
        - 直接：``acp.on("ping", fn)``

        :param action: 事件名
        :param handler: 可选，直接传入的处理函数；缺省返回装饰器
        """
        def decorator(fn):
            self._handlers[action] = fn
            return fn

        if handler is not None:
            self._handlers[action] = handler
            return handler
        return decorator

    def handle(self, action, message=None):
        """派发事件：若有注册的 handler 则调用并返回其结果。

        未注册 action 的处理函数时返回 None（表示未投递）。
        """
        self._require_enabled()
        fn = self._handlers.get(action)
        if fn is None:
            return None
        return fn(message)

    def call_agent(self, to_id, action, data):
        """高层封装：构造 ``{action, request_id, data}`` 消息并本地派发。

        简化：本地双子同进程直接经 handler 派发（不写路由信箱，不做网络投递），
        返回投递结果。

        :param to_id: 目标 Agent ID
        :param action: 要触发的 action（须已通过 on() 注册）
        :param data: 消息负载
        :return: {"request_id", "action", "delivered", "result"}
        :raises AcpDisabled: ACP 未启用
        """
        self._require_enabled()
        request_id = uuid.uuid4().hex
        message = {
            "action": action,
            "request_id": request_id,
            "data": {"to": to_id, "payload": data},
        }
        result = self.handle(action, message)
        return {
            "request_id": request_id,
            "action": action,
            "delivered": result is not None,
            "result": result,
        }

    # ------------------------------------------------------------------ #
    # 可选能力：局域网发现（默认关）                                       #
    # ------------------------------------------------------------------ #

    def discover_lan(self, timeout=2, discovery_factory=None):
        """局域网发现（默认关）。

        M3 真实化流程：start 后主动 ``broadcast_presence`` 宣告本机一次，随后在
        可配置的等待窗口内分片轮询 ``found_agents``，窗口结束收集返回并确保
        stop 清理。接收侧由 :class:`LiteLanDiscovery` 的后台线程 bind + recvfrom 完成。

        第四轮体检批次C：固定窗口收集——窗口内持续累积 ``found_agents``（多节点
        发现），不再命中首个信标即提前收窗（修复前单节点命中即提前返回，多节点
        发现失效）。

        :param timeout: 等待窗口秒数；缺省 2s。窗口结束后以最终快照为准。
        :param discovery_factory: 增量注入参数——自定义发现器工厂（测试确定性注入用）；
            缺省使用真实 :class:`LiteLanDiscovery`。
        :return: 发现的 agent dict 列表（已剔除本机自身信标）
        :raises AcpDisabled: ``acp.lan_discovery=False``
        """
        self._require_enabled()
        if not self._lan_discovery_enabled:
            raise AcpDisabled("ACP 局域网发现已关闭（config.acp.lan_discovery=False）")
        window = max(0.0, float(timeout or 0))
        discovery = (discovery_factory or LiteLanDiscovery)()
        try:
            discovery.start(port=9999)
            # 主动宣告一次本机存在（对端回包/对端广播均可触发 found_agents 登记）；
            # 广播失败仅告警不阻断——发现侧仍可收集其他节点的广播。
            try:
                discovery.broadcast_presence(
                    self.agent_id, agent_name="CX-A local agent"
                )
            except Exception as exc:  # noqa: BLE001 - 广播失败不阻断发现收集
                LOGGER.warning("局域网发现广播失败（继续被动收集）：%s", exc)
            # 固定窗口分片睡眠收集（第四轮体检批次C：去掉"命中即提前收窗"，
            # 多节点信标在窗口内陆续到达均可收集），避免一次性 sleep 不可中断
            step = 0.05
            waited = 0.0
            while waited < window:
                time.sleep(min(step, window - waited))
                waited += min(step, window - waited)
            # 收集并剔除本机自身信标（同机回环会把自家广播也送达本端口）
            return [
                agent
                for agent, _addr in list(discovery.found_agents)
                if agent.get("agent_id") != self.agent_id
            ]
        finally:
            discovery.stop()

    # ------------------------------------------------------------------ #
    # 可选能力：分组协作（默认关）                                         #
    # ------------------------------------------------------------------ #

    def group(self, members):
        """创建分组（默认关）。

        :param members: 组成员 Agent ID 列表
        :return: 生成的 group_id
        :raises AcpDisabled: ``acp.group_enabled=False``
        """
        self._require_enabled()
        if not self._group_enabled:
            raise AcpDisabled("ACP 分组协作已关闭（config.acp.group_enabled=False）")
        group_id = uuid.uuid4().hex
        self._groups[group_id] = {
            "members": list(members),
            "created": _now(),
        }
        return group_id

    # ------------------------------------------------------------------ #
    # 新增能力：云端中转（跨公网，跨版本互通兜底）                          #
    # ------------------------------------------------------------------ #

    def _resolve_relay(self):
        """解析云端中转实例。

        优先用注入的 relay_transport；缺省按配置 endpoint 惰性创建 :class:`CloudRelay`，
        并透传可选鉴权令牌 ``acp.cloud_relay_token``（第四轮体检批次C，缺省空串
        即不带鉴权头）。
        """
        if self._relay_transport is not None:
            return self._relay_transport
        if self._relay is None:
            self._relay = CloudRelay(
                endpoint=self._cloud_relay_endpoint,
                token=self._get("acp", "cloud_relay_token", "") or "",
            )
        return self._relay

    def relay_via_cloud(self, payload):
        """云端中转（跨公网通信）。

        :param payload: 待中转的数据（通常为 ``{action, request_id, data}``）
        :return: 远端回执（dict），由 transport.send 返回
        :raises AcpDisabled: ``acp.cloud_relay=False``
        """
        self._require_enabled()
        if not self._cloud_relay_enabled:
            raise AcpDisabled("ACP 云端中转已关闭（config.acp.cloud_relay=False）")
        return self._resolve_relay().send(payload)

    # ------------------------------------------------------------------ #
    # 节点互联层（20261017 CX-O 全量迁移批次 1）                            #
    # ------------------------------------------------------------------ #

    def initialize(self, agent_id=None, agent_name=None):
        """注册本机节点（节点互联层入口，对齐 CX-O ``ACPManager.initialize``）。

        本机节点 ``metadata.source="cxa_main"``（属 :data:`LOCAL_AGENT_SOURCES`，
        不落盘）；同时加载持久化的外部节点与连接表。

        :param agent_id: 覆盖本机节点标识（缺省用 config.acp.agent_id）
        :param agent_name: 覆盖本机节点名称（缺省用 config.acp.agent_name）
        :return: 本机 AgentInfo dict（副本）
        :raises AcpDisabled: ACP 未启用
        """
        self._require_enabled()
        if agent_id:
            self.agent_id = str(agent_id)
        if agent_name:
            self.agent_name = str(agent_name)
        self._load_persisted()
        info = make_agent_info(
            self.agent_id,
            name=self.agent_name,
            host="127.0.0.1",
            port=0,
            status="online",
            capabilities=["chat", "memory", "tools"],
            last_seen=now_iso(),
            metadata={"source": "cxa_main", "description": "CX-A 桌宠本机节点"},
        )
        with self._store_lock:
            self.nodes[self.agent_id] = info
        return dict(info)

    def register_node(self, agent_id, name="", host="", port=0, version="1.0.0",
                      capabilities=None, metadata=None, source="manual"):
        """注册/更新一个外部节点（发现即注册；手动添加 source="manual"）。

        四元组去重（对齐 CX-O M-C 修复）：同 id 且 host/port/version 与已注册
        完全一致 → 仅刷新 last_seen（status 恢复 online），不触发落盘；
        任一变化 → 整体更新并持久化。

        :return: (AgentInfo dict, changed: bool)
        :raises AcpDisabled: ACP 未启用
        """
        self._require_enabled()
        if not agent_id:
            return None, False
        meta = dict(metadata or {})
        meta.setdefault("source", source)
        with self._store_lock:
            existing = self.nodes.get(agent_id)
            if (
                existing is not None
                and existing.get("host") == str(host or "")
                and int(existing.get("port") or 0) == int(port or 0)
                and existing.get("version") == str(version or "1.0.0")
            ):
                existing["last_seen"] = now_iso()
                existing["status"] = "online"
                if name:
                    existing["name"] = str(name)
                return dict(existing), False
            info = make_agent_info(
                agent_id, name=name, host=host, port=port, status="online",
                version=version, capabilities=capabilities, last_seen=now_iso(),
                metadata=meta,
            )
            self.nodes[agent_id] = info
            self._persist_nodes_locked()
            return dict(info), True

    def remove_node(self, agent_id):
        """移除节点（连带其连接条目与消息槽；本机节点不可移除）。"""
        self._require_enabled()
        if not agent_id or agent_id == self.agent_id:
            return False
        with self._store_lock:
            existed = self.nodes.pop(agent_id, None) is not None
            if existed:
                self.connections = {
                    cid: c for cid, c in self.connections.items()
                    if c.get("remote_agent_id") != agent_id
                }
                self.node_messages.pop(agent_id, None)
                self._persist_nodes_locked()
            return existed

    def list_nodes(self, online_only=False):
        """列出节点（本机置顶；online_only 过滤；返回副本列表）。"""
        self._require_enabled()
        with self._store_lock:
            nodes = [dict(v) for v in self.nodes.values()]
        nodes.sort(key=lambda n: 0 if n.get("id") == self.agent_id else 1)
        if online_only:
            nodes = [n for n in nodes if n.get("status") == "online"]
        return nodes

    def upsert_connection(self, remote_agent_id, host="", port=0,
                          remote_agent_name="Remote Agent"):
        """建立连接条目（CX-O 语义：通讯录记录，无握手；同远端复用更新）。"""
        self._require_enabled()
        with self._store_lock:
            for conn in self.connections.values():
                if conn.get("remote_agent_id") == remote_agent_id:
                    conn["host"] = str(host or "")
                    conn["port"] = int(port or 0)
                    conn["status"] = "connecting"
                    conn["last_activity"] = now_iso()
                    self._persist_connections_locked()
                    return dict(conn)
            conn = make_connection_info(
                self.agent_id, remote_agent_id, host=host, port=port,
                remote_agent_name=remote_agent_name,
            )
            self.connections[conn["id"]] = conn
            self._persist_connections_locked()
            return dict(conn)

    def remove_connection(self, connection_id):
        """删除连接条目（对齐 CX-O DELETE /connect/{id}；幂等返回是否删除）。"""
        self._require_enabled()
        with self._store_lock:
            existed = self.connections.pop(connection_id, None) is not None
            if existed:
                self._persist_connections_locked()
            return existed

    def list_connections(self):
        """列出连接条目（副本列表）。"""
        self._require_enabled()
        with self._store_lock:
            return [dict(c) for c in self.connections.values()]

    def send_message(self, to_agent_id, content, msg_type="chat", from_name=None):
        """向节点发送一条消息（存本地槽 + 外部投递；对齐 CX-O send_message）。

        投递路径：目标 port>0 且 !=9999 → POST ``/acp/message``；port==9999
        （CX-O 主系统，不可直接投递）→ POST ``/api/acp/receive``；目标未知或
        port==0（本地形态）→ 仅存本地槽（静默，对齐 CX-O）。

        :return: wire 消息 dict（含分配的 id/timestamp）
        :raises AcpDisabled: ACP 未启用
        """
        self._require_enabled()
        node = self.nodes.get(to_agent_id)
        msg = make_message(
            msg_type=msg_type,
            from_agent_id=self.agent_id,
            from_agent_name=from_name or self.agent_name,
            to_agent_id=to_agent_id,
            content=content,
            is_sent=True,
        )
        self._append_slot(to_agent_id, msg)
        if node is not None and int(node.get("port") or 0) > 0:
            self._deliver(node, msg)
        return msg

    def receive_external_message(self, payload):
        """接收外部投递（POST /api/acp/receive 与 /acp/message 共用入口）。

        归一化（type/msg_type 双键兼容）→ 按发送者分键存槽 → 返回归一化 dict；
        非法载荷返回 None（HTTP 层据此回 400）。

        :raises AcpDisabled: ACP 未启用
        """
        self._require_enabled()
        msg = message_from_wire(payload)
        if msg is None:
            return None
        self._append_slot(str(msg.get("from_agent_id") or ""), msg)
        return msg

    def get_node_messages(self, key, limit=50):
        """读取与某节点/群组的消息槽（最近 limit 条，时间正序）。"""
        self._require_enabled()
        with self._store_lock:
            slot = list(self.node_messages.get(str(key or ""), []))
        return slot[-max(1, int(limit or 50)):]

    def node_stats(self):
        """互联层统计（字段对齐 CX-O GET /api/acp/stats 的 statistics）。"""
        self._require_enabled()
        with self._store_lock:
            nodes = list(self.nodes.values())
            conns = list(self.connections.values())
            total_messages = sum(len(v) for v in self.node_messages.values())
        online = sum(1 for n in nodes if n.get("status") == "online")
        return {
            "total_agents": len(nodes),
            "online_agents": online,
            "total_connections": len(conns),
            "active_connections": sum(1 for c in conns if c.get("status") == "connected"),
            "total_groups": 0,  # 群组本轮裁剪（recon_acp.md §9.1）
            "total_messages": total_messages,
            "unread_messages": 0,
            "local_agent_id": self.agent_id,
            "local_agent_name": self.agent_name,
            "local_http_port": int(self._get("acp", "local_http_port", 0) or 0),
        }

    # ---------------- 节点互联层内部：投递 / 槽 / 持久化 / 清扫 ---------------- #

    def _append_slot(self, key, msg):
        """按对端分键追加消息槽（500 条/键 FIFO 截断；对齐 CX-O M-E 语义）。"""
        slot_key = str(key or "")
        with self._store_lock:
            slot = self.node_messages.setdefault(slot_key, [])
            slot.append(msg)
            if len(slot) > MAX_MESSAGES_PER_KEY:
                del slot[: len(slot) - MAX_MESSAGES_PER_KEY]

    def _deliver(self, node, msg):
        """把 wire 消息投递到外部节点（尽力而为：失败仅告警，不重试不回滚）。

        端口双轨（recon_acp.md §3.4）：port==9999 为 CX-O 主系统 → 投其
        ``/api/acp/receive``（主系统标准入口）；其余 POST ``/acp/message``。
        """
        host = str(node.get("host") or "")
        port = int(node.get("port") or 0)
        if not host or port <= 0:
            return
        if port == 9999:
            url = f"http://{host}:{port}/api/acp/receive"
        else:
            url = f"http://{host}:{port}/acp/message"
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(msg).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.delivery_timeout) as resp:
                if int(resp.status) >= 300:
                    LOGGER.warning("ACP 消息投递非 2xx（%s → %s）：%s", node.get("id"), url, resp.status)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            LOGGER.warning("ACP 消息投递失败（%s → %s）：%s", node.get("id"), url, exc)

    def _probe_health(self, node):
        """GET http://{host}:{port}/health 探活（5s 超时；200 即存活）。"""
        host = str(node.get("host") or "")
        port = int(node.get("port") or 0)
        if not host or port <= 0:
            return False
        try:
            with urllib.request.urlopen(
                f"http://{host}:{port}/health", timeout=self.delivery_timeout
            ) as resp:
                return int(resp.status) == 200
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def _sweep_nodes(self):
        """节点清扫：last_seen 超时的 online 外部节点 → 探活刷新或置 offline。

        port==9999 的主系统节点不可知其 HTTP 端口（双轨语义），/health 探活
        必然失败——改按**信标新鲜度**判定：last_seen 超过 2×discovery 间隔
        （默认 60s）仍未见新信标才置 offline（偏差已在规划文档注记）。
        """
        now_wall = time.time()
        with self._store_lock:
            snapshot = [(nid, dict(n)) for nid, n in self.nodes.items()]
        for node_id, node in snapshot:
            if node_id == self.agent_id:
                continue
            meta = node.get("metadata") or {}
            if meta.get("source") in LOCAL_AGENT_SOURCES:
                continue
            if node.get("status") != "online":
                continue
            last_seen = str(node.get("last_seen") or "")
            elapsed = _elapsed_since(last_seen, now_wall)
            if elapsed is None or elapsed <= self.heartbeat_timeout:
                continue
            if int(node.get("port") or 0) == 9999:
                if elapsed <= 2.0 * float(self._get("acp", "discovery_interval", 30)):
                    continue  # 主系统节点：信标新鲜度内视为在线
                probe_ok = False
            else:
                probe_ok = self._probe_health(node)
            with self._store_lock:
                current = self.nodes.get(node_id)
                if current is None or current.get("status") != "online":
                    continue  # 已被并发修改（新信标刷新），不覆盖
                if probe_ok:
                    current["last_seen"] = now_iso()
                else:
                    current["status"] = "offline"
                    LOGGER.info("ACP 节点 %s 探活失败，已置为 offline", node_id)

    def _persist_nodes_locked(self):
        """落盘节点注册表（须持 _store_lock；本机动态来源不落盘）。"""
        self._write_json(
            "nodes.json",
            {
                nid: n for nid, n in self.nodes.items()
                if (n.get("metadata") or {}).get("source") not in LOCAL_AGENT_SOURCES
            },
        )

    def _persist_connections_locked(self):
        """落盘连接表（须持 _store_lock）。"""
        self._write_json("connections.json", self.connections)

    def _write_json(self, filename, obj):
        """原子写 JSON（tmp + os.replace；失败仅告警不抛——持久化尽力而为）。"""
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            target = os.path.join(self._data_dir, filename)
            tmp = target + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(obj, fh, ensure_ascii=False)
            os.replace(tmp, target)
        except OSError as exc:
            LOGGER.warning("ACP 持久化写盘失败（%s）：%s", filename, exc)

    def _load_persisted(self):
        """启动加载持久化的外部节点与连接表（本机节点由 initialize 重新注册）。"""
        for filename, attr in (("nodes.json", "nodes"), ("connections.json", "connections")):
            target = os.path.join(self._data_dir, filename)
            try:
                with open(target, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                getattr(self, attr).update(data)

    # ---------------- 节点互联层：UDP 发现常驻循环（批次 2） ---------------- #

    def start_discovery(self, local_http_port=0, discovery_factory=None):
        """启动常驻 UDP 发现（幂等；对齐 CX-O ``ACPManager.start`` 发现分支）。

        - 监听 ``config.acp.discovery_port``（默认 9999，CX-O BEACON 协议互通）；
        - 每 ``discovery_interval`` 秒广播一次本机 BEACON（六字段齐全，宣告
          ``local_http_port``——对端据此 POST /acp/message 回联；广播目标固定
          discovery_port）；
        - 收到的新节点信标经回调 ``register_node(source="lan")`` 注册（发现即
          注册，四元组去重）；
        - ``enabled`` 闸门：关闭时抛 :class:`AcpDisabled`（未开启绝不建 socket）。

        :param local_http_port: 本机 HTTP 服务端口（BEACON port 字段；0=未宣告）
        :param discovery_factory: 测试注入（fake 发现器工厂）
        :return: True 已启动 / 已在运行
        :raises AcpDisabled: ACP 未启用
        """
        self._require_enabled()
        if self._node_discovery is not None and self._discovery_thread is not None \
                and self._discovery_thread.is_alive():
            return True
        discovery = (discovery_factory or LiteLanDiscovery)(
            on_beacon=self._on_node_beacon,
        )
        discovery.start(port=int(self._get("acp", "discovery_port", 9999) or 9999))
        self._node_discovery = discovery
        self._node_http_port = int(local_http_port or 0)
        self._discovery_stop.clear()
        thread = threading.Thread(
            target=self._discovery_loop, name="cxa-acp-discovery", daemon=True
        )
        self._discovery_thread = thread
        thread.start()
        LOGGER.info(
            "ACP 节点发现已启动（bind=%s announce_port=%s interval=%ss）",
            self._get("acp", "discovery_port", 9999), self._node_http_port,
            self._get("acp", "discovery_interval", 30),
        )
        return True

    def stop_discovery(self):
        """停止常驻发现（幂等：停广播线程 + 关 socket）。"""
        self._discovery_stop.set()
        thread = self._discovery_thread
        self._discovery_thread = None
        discovery = self._node_discovery
        self._node_discovery = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        if discovery is not None:
            discovery.stop()

    def _discovery_loop(self):
        """常驻循环：每 discovery_interval 秒广播一次本机存在 + 清理过期记录。"""
        interval = max(5.0, float(self._get("acp", "discovery_interval", 30) or 30))
        while not self._discovery_stop.wait(interval):
            discovery = self._node_discovery
            if discovery is None:
                break
            try:
                discovery.broadcast_presence(
                    self.agent_id,
                    agent_name=self.agent_name,
                    capabilities=["chat", "memory", "tools"],
                    port=self._node_http_port or None,
                    target_port=int(self._get("acp", "discovery_port", 9999) or 9999),
                    timestamp=now_iso(),
                )
                discovery.prune_expired(max_age_seconds=2 * interval)
            except Exception as exc:  # noqa: BLE001 - 广播失败不 kill 线程
                LOGGER.warning("ACP 信标广播失败（下一周期重试）：%s", exc)

    def _on_node_beacon(self, agent):
        """发现回调：新节点信标 → 注册进 nodes（发现即注册，四元组去重在册）。"""
        agent_id = str(agent.get("agent_id") or "")
        if not agent_id or agent_id == self.agent_id:
            return
        self.register_node(
            agent_id,
            name=str(agent.get("agent_name") or ""),
            host=str(agent.get("host") or ""),
            port=int(agent.get("port") or 0),
            version=str(agent.get("version") or "1.0.0"),
            capabilities=list(agent.get("capabilities") or []),
            source="lan",
        )