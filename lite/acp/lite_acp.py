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
"""

import logging
import os
import threading
import time
import uuid

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
        #: 后台心跳清扫线程（daemon，可选启动）
        self._heartbeat_thread = None
        self._stop_evt = threading.Event()

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