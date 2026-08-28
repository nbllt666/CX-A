# -*- coding: utf-8 -*-
"""轻量局域网发现（Task G1，可选，默认关）。

依据《CX-A 补充文档 · ACP 与 CXFC》§2.2：CX-O 的 UDP 广播发现
（``discovery_port=9999``）在轻量版降级为可选（默认关）。本模块提供极简、可测试
的 UDP 广播实现，作为 ``LiteACP.discover_lan`` 的底层能力。

设计要点（不复用 CX-O 的 asyncio 常驻循环/单例，采用单线程轻量实现）：
- ``start(port)`` / ``stop()``：创建/关闭 UDP socket（SO_BROADCAST + SO_REUSEADDR）。
- ``broadcast_presence``：向局域网广播 ``ACP_BEACON`` 报文宣告本机存在。
- ``parse_packet``：解析收到的信标，归一化为 agent dict；非法报文返回 None。
- 测试通过注入 ``socket_factory`` 或直接测 ``parse_packet`` 协议字段，避免真实发包。
"""

import json
import socket
import threading
import time

#: 信标报文类型（与 CX-O discover.py 保持一致，便于跨版本互通）
MSG_TYPE = "ACP_BEACON"

#: found_agents 条目上限（N9，20260828_模块0_API鉴权与安全链路修复）：
#: 防恶意信标刷包撑大内存；超限丢弃并按每秒至多一条告警限频。
MAX_FOUND_AGENTS = 256


class AcpDiscoveryError(Exception):
    """局域网发现错误（未启动即广播 / socket 异常等）。"""


class LiteLanDiscovery:
    """轻量 UDP 局域网发现器。

    非线程/非 asyncio，纯同步轻量实现。``found_agents`` 为事件列表
    （``[(agent_dict, (host, port)), ...]``），由 ``parse_packet`` 命中时追加。
    """

    def __init__(self, broadcast_address="255.255.255.255", socket_factory=None):
        """构造发现器。

        :param broadcast_address: UDP 广播地址，默认广播域全地址
        :param socket_factory: 可注入的 socket 工厂；测试传入 fake 工厂以隔离网络，
            缺省使用真实 ``socket.socket``
        """
        self.broadcast_address = broadcast_address
        self._socket_factory = socket_factory or socket.socket
        #: 发现到的 agent 事件列表（[(agent_dict, addr), ...]）
        self.found_agents = []
        #: 已见 (agent_id, addr) 集合（N9：重复信标不再重复登记）
        self._seen_agents = set()
        #: 上次超限告警时刻（monotonic 秒；N9 告警限频：每秒至多一条）
        self._last_overflow_warn = 0.0
        self._running = False
        self._socket = None
        self.port = 0
        #: 后台接收线程与停止哨兵（M3 真实化：start 后 bind 并循环收包）
        self._recv_thread = None
        self._stop_event = threading.Event()

    def start(self, port=9999):
        """启动发现：创建 UDP socket（广播 + 地址复用），bind 通配端口并开启后台接收线程。

        M3 真实化修复：此前 start 只建 socket 不 bind、不收包，``discover_lan``
        恒返空列表。现在启动后台 daemon 线程循环 ``recvfrom`` 并交
        :meth:`parse_packet` 解析，命中合法 ACP_BEACON 即登记 ``found_agents``。

        :param port: 本机信标宣告端口（加入 ACP_BEACON 报文供对端回连）
        :raises AcpDiscoveryError: socket 创建失败或端口 bind 失败时抛出
        """
        if self._running:
            return
        try:
            self._socket = self._socket_factory(
                socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP
            )
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except Exception as exc:  # noqa: BLE001 - socket 创建失败转为发现错误
            self._running = False
            self._socket = None
            raise AcpDiscoveryError(f"局域网发现 socket 创建失败：{exc}") from exc
        try:
            # 接收侧显式 bind 通配地址——不 bind 则内核不向本端口投递报文
            self._socket.bind(("0.0.0.0", port))
        except Exception as exc:
            self._running = False
            self._close_socket_quietly()
            raise AcpDiscoveryError(f"局域网发现端口绑定失败（port={port}）：{exc}") from exc
        try:
            # 收包用超时轮询停止哨兵，避免阻塞式 recv 卡住线程退出（沿用仓库 Event 线程风格）；
            # fake socket 无 settimeout 时静默跳过，不影响后续注入式测试。
            self._socket.settimeout(0.5)
        except Exception:  # noqa: BLE001 - 注入式 fake socket 可能不支持超时设置
            pass
        self.port = port
        self._stop_event.clear()
        self._running = True
        thread = threading.Thread(
            target=self._recv_loop, name="cxa-lan-discovery", daemon=True
        )
        self._recv_thread = thread
        thread.start()

    def stop(self):
        """停止发现：置停止哨兵、关闭 socket 并回收后台接收线程（幂等）。"""
        self._running = False
        self._stop_event.set()
        self._close_socket_quietly()
        thread = self._recv_thread
        self._recv_thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)

    def close(self):
        """关闭并回收资源（stop 的别名，供「关闭」语义调用方使用）。"""
        self.stop()

    def _close_socket_quietly(self):
        """静默关闭当前 socket（幂等，异常仅吞掉用于收尾路径）。"""
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None

    def _recv_loop(self):
        """后台接收循环：在本端口上循环 recvfrom，命中合法信标即登记 found_agents。

        - ``socket.timeout``：无数据超时轮询，回到哨兵判定继续；
        - ``OSError``：socket 已关闭 / 不可用，退出线程；
        - 单包解析异常不影响后续接收。
        """
        while not self._stop_event.is_set():
            sock = self._socket
            if sock is None:
                break
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self.parse_packet(data, addr)
            except Exception:  # noqa: BLE001 - 单包解析失败不影响整体接收
                continue

    def broadcast_presence(self, agent_id, agent_name="", capabilities=None, port=None):
        """向局域网广播一条 ACP_BEACON 报文，宣告本机 Agent 存在。

        :param agent_id: 本机节点标识（config.acp.agent_id）
        :param agent_name: 节点名称
        :param capabilities: 能力列表（如 [\"chat\", \"memory\"]）
        :param port: 信标中宣告的端口；缺省用 start 时的 port
        """
        if self._socket is None:
            raise AcpDiscoveryError("局域网发现未启动，无法广播（先调用 start()）")
        announce_port = port or self.port
        message = {
            "type": MSG_TYPE,
            "agent_id": agent_id,
            "agent_name": agent_name,
            "timestamp": None,  # 由调用方按需填充；轻量版不强制 ISO 时间
            "version": "1.0.0",
            "capabilities": list(capabilities or []),
            "port": announce_port,
        }
        self._socket.sendto(
            json.dumps(message).encode("utf-8"),
            (self.broadcast_address, self.port),
        )

    def parse_packet(self, data, addr):
        """解析收到的信标报文。

        轻量实现 + 可测试：直接把协议字段（type / agent_id / agent_name / port /
        capabilities）归一化为 agent dict；命中合法 ``ACP_BEACON`` 时追加到
        ``found_agents``。非法报文返回 None。

        :param data: 原始字节报文
        :param addr: 来源地址 (host, port)
        :return: 归一化 agent dict；非法报文返回 None
        """
        try:
            msg = json.loads(data.decode("utf-8", errors="replace"))
        except (ValueError, TypeError):
            return None
        if not isinstance(msg, dict) or msg.get("type") != MSG_TYPE:
            return None
        agent_id = msg.get("agent_id", "")
        agent = {
            "agent_id": agent_id,
            "agent_name": msg.get("agent_name", ""),
            "host": addr[0],
            "port": msg.get("port", 0),
            "capabilities": list(msg.get("capabilities", []) or []),
            "version": msg.get("version", "1.0.0"),
            "timestamp": msg.get("timestamp", ""),
        }
        if agent_id:
            # N9：按 (agent_id, addr) 去重 + 条目上限，防恶意信标污染与内存放大
            key = (agent_id, (addr[0], addr[1]))
            if key in self._seen_agents:
                return agent
            if len(self.found_agents) >= MAX_FOUND_AGENTS:
                now = time.monotonic()
                if now - self._last_overflow_warn >= 1.0:
                    self._last_overflow_warn = now
                    ts = time.strftime("%Y-%m-%d %H:%M:%S")
                    print(
                        f"[{ts}] [WARNING] 局域网发现条目已达上限 {MAX_FOUND_AGENTS}，"
                        f"丢弃新信标 agent_id={agent_id!r} addr={addr}"
                    )
                return agent
            self._seen_agents.add(key)
            self.found_agents.append((agent, addr))
        return agent