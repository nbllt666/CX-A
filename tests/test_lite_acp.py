# -*- coding: utf-8 -*-
"""Task G1 轻量版 ACP（lite/acp/lite_acp.py + discovery.py）单元测试。

覆盖（全 mock，无真实网络）：
- Agent 注册 / 注销（幂等）/ 心跳 / 状态机（心跳超时 offline）
- route_message 消息结构含 action/request_id/data + 未注册目标报错
- call_agent + on() handler 派发
- 配置关闭抛 AcpDisabled（enabled=False / lan_discovery=False / group_enabled=False / cloud_relay=False）
- 云端中转 mock transport 注入
- 局域网发现协议解析（parse_packet / broadcast 注入 fake socket）
- 配置：新增段默认值可读 + 热更新段判定
"""

import json
import os
import socket
import tempfile
import time

import pytest

import lite.acp.lite_acp as lite_acp
from lite.acp import AcpAgentNotFound, AcpDisabled, LiteACP
from lite.acp.discovery import LiteLanDiscovery
from lite.acp.lite_acp import MSG_STRUCT
from lite.config.config_manager import ConfigManager, DEFAULTS, HOT_RELOAD_SECTIONS


# ------------------------------------------------------------------ #
# 工具：配置构造                                                     #
# ------------------------------------------------------------------ #

def _config(**acp_over):
    """构造 acp 段配置映射（默认 enabled=True，便于测试核心能力）。"""
    acp = {
        "enabled": True,
        "agent_id": "cxa-agent-001",
        "heartbeat_interval": 10,
        "lan_discovery": False,
        "group_enabled": False,
        "cloud_relay": True,
        "cloud_relay_endpoint": "",
    }
    acp.update(acp_over)
    return {"acp": acp}


@pytest.fixture
def clock(monkeypatch):
    """可控单调时钟：替换 lite_acp._now，支持手动推进。"""
    state = {"t": 1000.0}
    monkeypatch.setattr(lite_acp, "_now", lambda: state["t"])

    class _Clock:
        def advance(self, dt):
            state["t"] += dt

    return _Clock()


class MockRelayTransport:
    """云端中转 mock transport。"""

    def __init__(self, receipt=None, reachable=True):
        self.receipt = receipt or {"status": "ok"}
        self.reachable = reachable
        self.sent = []

    def send(self, payload):
        self.sent.append(payload)
        return self.receipt

    def is_reachable(self, timeout=5):
        return self.reachable


# ------------------------------------------------------------------ #
# 1. 注册 / 注销 / 状态机                                             #
# ------------------------------------------------------------------ #

def test_register_agent_and_status_online():
    """注册后为 online。"""
    acp = LiteACP(config=_config())
    assert acp.register_agent("alice", {"desc": "assistant"}) is True
    assert acp.agents["alice"]["manifest"] == {"desc": "assistant"}
    assert acp.status("alice") == "online"


def test_unregister_idempotent():
    """注销幂等：二次注销返回 False 且不报错，状态回 unknown。"""
    acp = LiteACP(config=_config())
    acp.register_agent("bob")
    assert acp.unregister_agent("bob") is True
    assert acp.unregister_agent("bob") is False
    assert acp.status("bob") == "unknown"


def test_status_unknown_for_unregistered():
    """未注册 Agent 状态为 unknown。"""
    acp = LiteACP(config=_config())
    assert acp.status("ghost") == "unknown"


def test_heartbeat_returns_elapsed_and_offline_timeout(clock):
    """心跳返回自上次心跳经过秒数；超时（2×interval）判 offline。"""
    acp = LiteACP(config=_config())  # interval=10 → offline 阈值 20
    acp.register_agent("alice")  # last_seen=t=1000
    clock.advance(5)  # t=1005
    assert acp.heartbeat("alice") == pytest.approx(5.0)
    assert acp.status("alice") == "online"

    clock.advance(15)  # t=1020，自 last_seen(1005) 过去 15s < 20 → 在线
    assert acp.status("alice") == "online"

    clock.advance(6)  # t=1026，过去 21s > 20 → 离线
    assert acp.status("alice") == "offline"


def test_heartbeat_unknown_agent_returns_zero():
    """对未注册 Agent 心跳返回 0.0 且不报错。"""
    acp = LiteACP(config=_config())
    assert acp.heartbeat("ghost") == 0.0


def test_heartbeat_tick_marks_offline_and_emits_event(clock):
    """心跳清扫 tick 把超时 Agent 置 offline 并派发 offline 事件；心跳可恢复。"""
    acp = LiteACP(config=_config())  # interval=10 -> 阈值 20
    events = []
    acp.on("offline", lambda msg: events.append(msg))
    acp.register_agent("alice")  # last_seen=t=1000

    clock.advance(19)  # t=1019，未超阈值
    acp._heartbeat_tick()
    assert acp.status("alice") == "online"
    assert events == []

    clock.advance(3)  # t=1022，超过 20
    acp._heartbeat_tick()
    assert events == [{"agent_id": "alice"}]  # offline 事件已派发
    assert acp.agents["alice"]["status"] == "offline"
    assert acp.status("alice") == "offline"

    # 心跳刷新后恢复 online，且不重复派发 offline
    acp.heartbeat("alice")
    assert acp.status("alice") == "online"
    clock.advance(2)  # 自心跳过去 2s < 20
    acp._heartbeat_tick()
    assert acp.status("alice") == "online"
    assert len(events) == 1


def test_heartbeat_thread_lifecycle():
    """start_heartbeat 启动 daemon 线程；stop_heartbeat 幂等停止；ACP 关闭时不启动。"""
    acp = LiteACP(config=_config())  # interval=10
    try:
        assert acp.start_heartbeat() is True
        assert acp._heartbeat_thread is not None and acp._heartbeat_thread.is_alive()
        # 幂等：重复 start 不新建线程
        assert acp.start_heartbeat() is True
        assert acp._heartbeat_thread.is_alive()
    finally:
        acp.stop_heartbeat()
    assert acp._heartbeat_thread is None or not acp._heartbeat_thread.is_alive()
    # 停止后可再启动
    try:
        assert acp.start_heartbeat() is True
    finally:
        acp.stop_heartbeat()
    # ACP 关闭时拒绝启动
    acp_off = LiteACP(config=_config(enabled=False))
    assert acp_off.start_heartbeat() is False


# ------------------------------------------------------------------ #
# 2. 消息路由结构                                                     #
# ------------------------------------------------------------------ #

def test_route_message_structure():
    """route_message 产出 {action, request_id, data}，data 含 from/to/payload。"""
    acp = LiteACP(config=_config())
    acp.register_agent("alice")
    acp.register_agent("bob")
    rid = acp.route_message("alice", "bob", {"text": "hi"})
    assert isinstance(rid, str) and rid

    msg = acp.get_messages("bob")[0]
    assert set(msg.keys()) == MSG_STRUCT
    assert msg["action"] == "message"
    assert msg["request_id"] == rid
    assert msg["data"]["from"] == "alice"
    assert msg["data"]["to"] == "bob"
    assert msg["data"]["payload"] == {"text": "hi"}


def test_route_message_unregistered_raises():
    """目标 Agent 未注册时抛 AcpAgentNotFound。"""
    acp = LiteACP(config=_config())
    acp.register_agent("alice")
    with pytest.raises(AcpAgentNotFound):
        acp.route_message("alice", "ghost", {"text": "hi"})


# ------------------------------------------------------------------ #
# 3. call_agent + on() handler 派发                                    #
# ------------------------------------------------------------------ #

def test_call_agent_and_handler_dispatch():
    """call_agent 经 on() 注册的 handler 派发，返回投递结果。"""
    acp = LiteACP(config=_config())
    received = []
    acp.on("ping", lambda msg: received.append(msg) or "pong")
    resp = acp.call_agent("bob", "ping", {"text": "hi"})

    assert resp["action"] == "ping"
    assert "request_id" in resp and resp["request_id"]
    assert resp["delivered"] is True
    assert resp["result"] == "pong"
    assert received[0]["data"]["to"] == "bob"
    assert received[0]["data"]["payload"] == {"text": "hi"}


def test_on_decorator_style():
    """on() 装饰器用法同样生效。"""
    acp = LiteACP(config=_config())

    @acp.on("hello")
    def _hello(msg):
        return "hi-" + msg["data"]["payload"]["name"]

    resp = acp.call_agent("bob", "hello", {"name": "x"})
    assert resp["result"] == "hi-x"


def test_call_agent_no_handler_undelivered():
    """未注册 action 时 call_agent delivered=False。"""
    acp = LiteACP(config=_config())
    resp = acp.call_agent("bob", "noop", {})
    assert resp["delivered"] is False
    assert resp["result"] is None


# ------------------------------------------------------------------ #
# 4. 配置关闭 → 抛 AcpDisabled                                         #
# ------------------------------------------------------------------ #

def test_enabled_false_raises_on_core_ops():
    """ACP 总开关关闭时核心操作一律抛 AcpDisabled。"""
    acp = LiteACP(config=_config(enabled=False))
    with pytest.raises(AcpDisabled):
        acp.register_agent("a", {})
    with pytest.raises(AcpDisabled):
        acp.status("a")
    with pytest.raises(AcpDisabled):
        acp.heartbeat("a")
    with pytest.raises(AcpDisabled):
        acp.route_message("a", "b", {})
    with pytest.raises(AcpDisabled):
        acp.call_agent("b", "x", {})


def test_lan_discovery_disabled_raises():
    """lan_discovery=False 时 discover_lan 抛 AcpDisabled。"""
    acp = LiteACP(config=_config(lan_discovery=False))
    with pytest.raises(AcpDisabled):
        acp.discover_lan()


def test_lan_discovery_enabled_returns_list():
    """lan_discovery=True 时 discover_lan 返回列表（确定性注入 stub，无真实网络）。"""
    stub = _StubDiscovery()
    acp = LiteACP(config=_config(lan_discovery=True))
    agents = acp.discover_lan(timeout=0, discovery_factory=lambda: stub)
    assert isinstance(agents, list)


class _StubDiscovery:
    """discover_lan 的确定性注入桩：预填 found_agents，记录 start/broadcast/stop。"""

    def __init__(self, prefill=None):
        self.found_agents = list(prefill or [])
        self.started = False
        self.stopped = False
        self.broadcast_calls = []
        self.start_port = None

    def start(self, port=9999):
        self.started = True
        self.start_port = port

    def broadcast_presence(self, *args, **kwargs):
        self.broadcast_calls.append((args, kwargs))

    def stop(self):
        self.stopped = True


def test_discover_lan_broadcasts_collects_and_stops():
    """M3：discover_lan 启动后广播一次、分片等待收集、最终 stop 清理并剔除自身信标。"""
    other = ({"agent_id": "node-a", "host": "192.168.1.20", "port": 9000}, ("192.168.1.20", 9000))
    myself = ({"agent_id": "cxa-agent-001", "host": "192.168.1.9", "port": 9999}, ("192.168.1.9", 9999))
    stub = _StubDiscovery(prefill=[myself, other])
    acp = LiteACP(config=_config(lan_discovery=True))  # agent_id 默认 cxa-agent-001

    agents = acp.discover_lan(timeout=0.1, discovery_factory=lambda: stub)

    # 自身信标被剔除，仅保留外部节点
    assert [a["agent_id"] for a in agents] == ["node-a"]
    # 生命周期：启动 port=9999、广播恰一次、结束 stop 清理
    assert stub.started and stub.start_port == 9999
    assert len(stub.broadcast_calls) == 1
    args, kwargs = stub.broadcast_calls[0]
    assert args[0] == "cxa-agent-001"
    assert stub.stopped


class _FakeSocket:
    """可编排收包的 fake UDP socket：bind/settimeout/recvfrom/sendto 全覆盖。"""

    def __init__(self):
        self.sent = []
        self.closed = False
        self.bound = None
        self.timeout_set = None
        self._queue = []

    def setsockopt(self, *args):
        pass

    def bind(self, addr):
        self.bound = addr

    def settimeout(self, value):
        self.timeout_set = value

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def close(self):
        self.closed = True

    def recvfrom(self, _bufsize):
        # 从队列短轮询取包；无包则快速超时，避免测试线程长时间阻塞
        deadline = time.monotonic() + 0.05
        while time.monotonic() < deadline:
            if self._queue:
                return self._queue.pop(0)
            time.sleep(0.005)
        raise socket.timeout("no packet")

    def queue_packet(self, data, addr):
        self._queue.append((data, addr))


def test_group_disabled_raises():
    """group_enabled=False 时 group() 抛 AcpDisabled。"""
    acp = LiteACP(config=_config(group_enabled=False))
    with pytest.raises(AcpDisabled):
        acp.group(["a"])


def test_group_enabled_returns_id():
    """group_enabled=True 时 group() 返回 group_id 并登记成员。"""
    acp = LiteACP(config=_config(group_enabled=True))
    gid = acp.group(["alice", "bob"])
    assert isinstance(gid, str) and gid
    assert acp._groups[gid]["members"] == ["alice", "bob"]


def test_relay_cloud_disabled_raises():
    """cloud_relay=False 时 relay_via_cloud 抛 AcpDisabled。"""
    acp = LiteACP(config=_config(cloud_relay=False))
    with pytest.raises(AcpDisabled):
        acp.relay_via_cloud({"action": "message"})


def test_relay_via_cloud_injected_transport():
    """云端中转注入 mock transport：回执透传 + 负载被投递。"""
    t = MockRelayTransport(receipt={"id": "r1"})
    acp = LiteACP(config=_config(cloud_relay=True), relay_transport=t)
    resp = acp.relay_via_cloud({"action": "message", "data": {}})
    assert resp == {"id": "r1"}
    assert t.sent == [{"action": "message", "data": {}}]


# ------------------------------------------------------------------ #
# 5. 局域网发现协议（parse_packet / broadcast fake socket）            #
# ------------------------------------------------------------------ #

def test_discovery_parse_valid_packet():
    """parse_packet 正确解析 ACP_BEACON 并登记 found_agents。"""
    d = LiteLanDiscovery()
    raw = json.dumps({
        "type": "ACP_BEACON",
        "agent_id": "node1",
        "agent_name": "n1",
        "port": 9999,
        "capabilities": ["chat"],
    }).encode()
    agent = d.parse_packet(raw, ("192.168.1.5", 5678))
    assert agent["agent_id"] == "node1"
    assert agent["host"] == "192.168.1.5"
    assert agent["port"] == 9999
    assert agent["capabilities"] == ["chat"]
    assert d.found_agents[0][0] == agent


def test_discovery_parse_invalid_packet():
    """非法报文（非 JSON / 非 ACP_BEACON）解析返回 None 且不登记。"""
    d = LiteLanDiscovery()
    assert d.parse_packet(b"not-json", ("host", 1)) is None
    wrong = json.dumps({"type": "OTHER", "agent_id": "x"}).encode()
    assert d.parse_packet(wrong, ("host", 1)) is None
    assert d.found_agents == []


def test_discovery_start_stop_with_fake_socket():
    """start/stop 以注入 fake socket 完成生命周期。"""
    d = LiteLanDiscovery(socket_factory=lambda *a, **k: _FakeSocket())
    d.start(port=9999)
    assert d._running
    d.stop()
    assert not d._running


def test_discovery_start_binds_wildcard_and_starts_recv_thread():
    """M3：start 后 bind ("0.0.0.0", port) 且后台接收线程存活、stop 后线程回收。"""
    d = LiteLanDiscovery(socket_factory=lambda *a, **k: _FakeSocket())
    d.start(port=12345)
    try:
        assert d._socket.bound == ("0.0.0.0", 12345)
        assert d._recv_thread is not None and d._recv_thread.is_alive()
    finally:
        d.stop()
    assert d._recv_thread is None or not d._recv_thread.is_alive()
    # stop 幂等 + close 别名可用
    d.stop()
    d.close()


def test_discovery_broadcast_presence():
    """broadcast_presence 构造合法 ACP_BEACON 并发送到广播地址。"""
    d = LiteLanDiscovery(socket_factory=lambda *a, **k: _FakeSocket())
    d.start(port=12345)
    sock = d._socket
    d.broadcast_presence("me", agent_name="name", capabilities=["chat"])
    d.stop()

    assert sock.closed
    assert len(sock.sent) == 1
    raw, addr = sock.sent[0]
    buf = json.loads(raw.decode())
    assert buf["type"] == "ACP_BEACON"
    assert buf["agent_id"] == "me"
    assert buf["agent_name"] == "name"
    assert buf["port"] == 12345
    assert addr == (d.broadcast_address, 12345)


def test_discovery_broadcast_not_started_raises():
    """未 start 即广播抛 AcpDiscoveryError。"""
    from lite.acp.discovery import AcpDiscoveryError
    d = LiteLanDiscovery()
    with pytest.raises(AcpDiscoveryError):
        d.broadcast_presence("me")


def test_discovery_recv_loop_delivers_packets_to_found_agents():
    """M3 端到端（确定性注入）：后台线程收包 -> parse_packet 命中登记 found_agents。"""
    fake = _FakeSocket()
    d = LiteLanDiscovery(socket_factory=lambda *a, **k: fake)
    beacon = json.dumps({
        "type": "ACP_BEACON",
        "agent_id": "node-x",
        "agent_name": "nx",
        "port": 9000,
        "capabilities": ["chat"],
    }).encode("utf-8")
    d.start(port=9999)
    thread = d._recv_thread
    try:
        fake.queue_packet(beacon, ("192.168.1.66", 5566))
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not d.found_agents:
            time.sleep(0.02)
        assert len(d.found_agents) == 1
        agent, addr = d.found_agents[0]
        assert agent["agent_id"] == "node-x"
        assert agent["host"] == "192.168.1.66"
        assert addr == ("192.168.1.66", 5566)
    finally:
        d.stop()
    # 线程已被哨兵+join 干净回收
    assert thread is not None and not thread.is_alive()


# ------------------------------------------------------------------ #
# 6. 配置：新增段默认值可读 + 热更新                                    #
# ------------------------------------------------------------------ #

def test_new_config_sections_defaults():
    """DEFAULTS 新增 acp/cxfc/tools 三段的默认值严格符合补充文档 §6。"""
    assert DEFAULTS["acp"] == {
        "enabled": False,
        "agent_id": "cxa-agent-001",
        "heartbeat_interval": 10,
        "lan_discovery": False,
        "group_enabled": False,
        "cloud_relay": True,
        "cloud_relay_endpoint": "",
    }
    assert DEFAULTS["cxfc"] == {"enabled": False, "embedded_only": True}
    assert DEFAULTS["tools"] == {
        "computer_control": False,
        "memory_tools": True,
        "system_tools": True,
    }


def test_new_sections_in_hot_reload():
    """acp/cxfc/tools 均应标记为可热更新段。"""
    for section in ("acp", "cxfc", "tools"):
        assert section in HOT_RELOAD_SECTIONS


def test_config_manager_reads_new_sections():
    """ConfigManager 实例可读取新增段默认值，reloadable 判定为热更新。"""
    with tempfile.TemporaryDirectory() as d:
        cfg = ConfigManager(
            config_path=os.path.join(d, "config.json"),
            data_dir=os.path.join(d, "data"),
        )
        assert cfg.get("acp", "enabled") is False
        assert cfg.get("acp", "agent_id") == "cxa-agent-001"
        assert cfg.get("acp", "cloud_relay") is True
        assert cfg.get("cxfc", "embedded_only") is True
        assert cfg.get("tools", "memory_tools") is True
        assert cfg.reloadable("acp") is True
        assert cfg.reloadable("cxfc") is True
        assert cfg.reloadable("tools") is True


def test_msg_struct_constant():
    """消息结构常量与 CX-O §3.3 对齐。"""
    assert MSG_STRUCT == {"action", "request_id", "data"}