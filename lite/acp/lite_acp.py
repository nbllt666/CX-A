# -*- coding: utf-8 -*-
"""轻量版 ACP（Task G1）——保留通信核心，砍掉重量级局域网机制。

依据《CX-A 补充文档 · ACP 与 CXFC》§2：ACP 改造保留——保留 Agent 注册 /
消息路由 / 心跳核心；局域网发现与分组协作降为可选（默认关）；新增云端中转
（跨公网通信）。消息结构 ``{action, request_id, data}`` 对齐 CX-O §3.3。

轻量设计（不复用 CX-O 的重型 asyncio/单例/锁/广播，单线程内存实现）：
- 存储：内存 dict（agent registry / 路由信箱 / 事件 handler / 分组），无持久化、无线程。
- 开关：``enabled=False`` 时核心操作抛 :class:`AcpDisabled`；
  ``lan_discovery=False`` / ``group_enabled=False`` / ``cloud_relay=False``
  时对应能力抛 :class:`AcpDisabled`。
- 消息结构常量 :data:`MSG_STRUCT = {"action", "request_id", "data"}` 供跨版本断言对齐。

路径规范：本项目一律基于 ``os.path.dirname(os.path.abspath(__file__))`` 推导，
不使用相对路径。
"""

import os
import time
import uuid

from .cloud_relay import CloudRelay
from .discovery import LiteLanDiscovery

#: 消息结构字段集合（对齐 CX-O §3.3，供跨版本互通断言）
MSG_STRUCT = {"action", "request_id", "data"}


def _now():
    """当前单调时钟（可测试注入点）。"""
    return time.monotonic()


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

    单线程内存实现：所有状态存于内存 dict，无异步、无锁、无持久化。
    构造时从配置 ``acp`` 段读取开关与默认参数。
    """

    def __init__(self, config=None, relay_transport=None):
        """构造轻量 ACP。

        :param config: 配置源。可为 ``ConfigManager`` 实例（用其 ``get(section,key)``）
            或 ``{section: {key: value}}`` 映射；缺省内部创建现实 ConfigManager。
        :param relay_transport: 可选注入的云端中转 transport（含 ``send()`` /
            ``is_reachable()``），缺省在 ``relay_via_cloud`` 时按配置创建 :class:`CloudRelay`。
        """
        #: 配置源（ConfigManager 或 dict，均可经 _get 读取）
        self._cfg = config
        self._enabled = bool(self._get("acp", "enabled", False))
        self.agent_id = self._get("acp", "agent_id", "cxa-agent-001")
        self.heartbeat_interval = float(self._get("acp", "heartbeat_interval", 10))
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
        self._routes.setdefault(to_id, []).append(message)
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

    def discover_lan(self, timeout=2):
        """局域网发现（默认关）。

        :param timeout: 预留的等待秒数（单线程实现下为极简收集，不阻塞阻塞等待）
        :return: 发现的 agent dict 列表（无网络时一般为空）
        :raises AcpDisabled: ``acp.lan_discovery=False``
        """
        self._require_enabled()
        if not self._lan_discovery_enabled:
            raise AcpDisabled("ACP 局域网发现已关闭（config.acp.lan_discovery=False）")
        discovery = LiteLanDiscovery()
        discovery.start(port=9999)
        try:
            #: 单线程下仅做当前已收集事件的快照归拢，不做阻塞式监听。
            return [agent for agent, _addr in discovery.found_agents]
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

        优先用注入的 relay_transport；缺省按配置 endpoint 惰性创建 :class:`CloudRelay`。
        """
        if self._relay_transport is not None:
            return self._relay_transport
        if self._relay is None:
            self._relay = CloudRelay(endpoint=self._cloud_relay_endpoint)
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