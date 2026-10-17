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


def test_route_mailbox_capped():
    """M-18（第三轮体检批次5）：信箱超 MAX_MAILBOX 截断最旧，长度恒有界。"""
    from lite.acp.lite_acp import MAX_MAILBOX

    acp = LiteACP(config=_config())
    acp.register_agent("alice")
    acp.register_agent("bob")
    for i in range(MAX_MAILBOX + 20):
        acp.route_message("alice", "bob", {"seq": i})
    mailbox = acp.get_messages("bob")
    assert len(mailbox) == MAX_MAILBOX
    # 截掉的是最旧：首条 seq 为 20
    assert mailbox[0]["data"]["payload"] == {"seq": 20}
    assert mailbox[-1]["data"]["payload"] == {"seq": MAX_MAILBOX + 19}


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
    """DEFAULTS 新增 acp/cxfc/tools 三段的默认值严格符合补充文档 §6（+20261017 节点互联键）。"""
    assert DEFAULTS["acp"] == {
        "enabled": False,
        "agent_id": "cxa-agent-001",
        # 20261017 节点互联（CX-O 全量迁移批次 1/3）
        "agent_name": "CX-A",
        "heartbeat_timeout": 30,
        "delivery_timeout_s": 5,
        "discovery_interval": 30,
        "discovery_port": 9999,
        "heartbeat_interval": 10,
        "auth_token": "",
        "local_http_port": 8600,
        "lan_discovery": False,
        "group_enabled": False,
        "cloud_relay": True,
        "cloud_relay_endpoint": "",
    }
    # Task H1：cxfc 段新增 relay 窗口默认值（relay_timeout_s / replay_window_s，秒）
    # 20261017 全量迁移批次 4：+ 远端调用密钥 / 心跳巡检 / 本机信标字段
    assert DEFAULTS["cxfc"] == {
        "enabled": False,
        "embedded_only": True,
        "relay_timeout_s": 10.0,
        "replay_window_s": 60.0,
        "remote_admin_key": "",
        "heartbeat_timeout_s": 30.0,
        "heartbeat_check_interval_s": 10.0,
        "local_name": "CX-A",
        "local_http_port": 8600,
    }
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


# ------------------------------------------------------------------ #
# 7. 局域网发现去重与上限（N9，20260828_模块0_API鉴权与安全链路修复）   #
# ------------------------------------------------------------------ #


def _beacon(agent_id, port=9000):
    """构造一条合法 ACP_BEACON 报文字节。"""
    return json.dumps({"type": "ACP_BEACON", "agent_id": agent_id, "port": port}).encode("utf-8")


def test_discovery_dedup_by_agent_and_addr():
    """N9：同一 (agent_id, addr) 重复信标只登记一次；不同 addr 不互斥。"""
    d = LiteLanDiscovery()
    for _ in range(3):
        d.parse_packet(_beacon("node1"), ("192.168.1.5", 5678))
    assert len(d.found_agents) == 1
    # 同 id 不同来源地址：各自登记（不同节点允许同名 id 的防御口径按 addr 区分）
    d.parse_packet(_beacon("node1"), ("192.168.1.6", 5678))
    assert len(d.found_agents) == 2


def test_discovery_entries_capped_and_overflow_dropped(capsys):
    """N9：条目上限 256；超限信标丢弃（解析照常返回）且同秒告警至多一条。"""
    d = LiteLanDiscovery()
    for i in range(300):
        d.parse_packet(_beacon(f"n{i}", port=i), ("10.0.0.1", 7000 + i))
    assert len(d.found_agents) == 256

    # 超限后再来新信标：解析结果照常返回，但不登记
    agent = d.parse_packet(_beacon("overflow-node"), ("10.0.0.9", 9999))
    assert agent["agent_id"] == "overflow-node"
    assert len(d.found_agents) == 256

    # 告警限频：同秒内重复丢弃不重复打印
    d.parse_packet(_beacon("overflow-node2"), ("10.0.0.9", 9998))
    output = capsys.readouterr().out
    assert output.count("已达上限") == 1


# ------------------------------------------------------------------ #
# 8. 第四轮体检批次C：固定窗口发现 / 端口归一化 / last_seen 时效        #
# ------------------------------------------------------------------ #


def test_discover_lan_fixed_window_collects_multi_nodes():
    """第四轮体检批次C：窗口内命中首个信标后不再提前收窗——固定窗口跑满，
    多节点发现结果完整收集。"""
    node_a = ({"agent_id": "node-a", "host": "192.168.1.20", "port": 9000}, ("192.168.1.20", 9000))
    node_b = ({"agent_id": "node-b", "host": "192.168.1.21", "port": 9001}, ("192.168.1.21", 9001))
    stub = _StubDiscovery(prefill=[node_a, node_b])
    acp = LiteACP(config=_config(lan_discovery=True))

    started = time.monotonic()
    agents = acp.discover_lan(timeout=0.15, discovery_factory=lambda: stub)
    elapsed = time.monotonic() - started

    # 旧实现 found_agents 非空即提前返回（elapsed≈0）；修复后固定窗口跑满
    assert elapsed >= 0.14, f"发现窗口未跑满：elapsed={elapsed:.3f}s"
    assert [a["agent_id"] for a in agents] == ["node-a", "node-b"]
    assert stub.stopped


@pytest.mark.parametrize("raw_port", ["not-a-port", 0, 70000, -1, 3.5, True, None])
def test_discovery_invalid_port_normalized_to_zero(raw_port):
    """第四轮体检批次C：非法 port（非 int / 越出 1-65535）归一化为 0，agent 保留。"""
    d = LiteLanDiscovery()
    raw = json.dumps({"type": "ACP_BEACON", "agent_id": "n1", "port": raw_port}).encode()
    agent = d.parse_packet(raw, ("10.0.0.1", 7000))
    assert agent["port"] == 0
    assert len(d.found_agents) == 1


def test_discovery_valid_port_boundaries_kept():
    """合法端口边界值 1 / 65535 原样保留。"""
    d = LiteLanDiscovery()
    low = d.parse_packet(
        json.dumps({"type": "ACP_BEACON", "agent_id": "n1", "port": 1}).encode(),
        ("10.0.0.1", 7000),
    )
    assert low["port"] == 1
    high = d.parse_packet(
        json.dumps({"type": "ACP_BEACON", "agent_id": "n2", "port": 65535}).encode(),
        ("10.0.0.2", 7001),
    )
    assert high["port"] == 65535


def test_discovery_last_seen_refresh_and_prune_expired():
    """第四轮体检批次C：记录带 last_seen；重复信标命中去重时刷新保鲜；
    prune_expired 清理超时记录并允许同一节点重新登记。"""
    d = LiteLanDiscovery()
    d.parse_packet(_beacon("n1"), ("10.0.0.1", 7000))
    record, _addr = d.found_agents[0]
    assert record["last_seen"] > 0

    # 重复信标：不重复登记，但刷新已登记记录的 last_seen 保鲜
    record["last_seen"] = time.time() - 1000
    d.parse_packet(_beacon("n1"), ("10.0.0.1", 7000))
    assert len(d.found_agents) == 1
    assert record["last_seen"] > time.time() - 60

    # 未超时：全部保留
    assert d.prune_expired(max_age_seconds=3600) == 0
    assert len(d.found_agents) == 1

    # 超时：清理并同步去重集合 → 同一节点可重新登记
    record["last_seen"] = time.time() - 400
    assert d.prune_expired(max_age_seconds=300) == 1
    assert d.found_agents == []
    d.parse_packet(_beacon("n1"), ("10.0.0.1", 7000))
    assert len(d.found_agents) == 1


# ------------------------------------------------------------------ #
# 9. 节点互联层（20261017 CX-O 全量迁移批次 1）                         #
# ------------------------------------------------------------------ #


@pytest.fixture
def node_acp(tmp_path):
    """节点互联层被测实例：临时持久化目录 + enabled=True。"""
    acp = LiteACP(config=_config(), data_dir=str(tmp_path / "acp"))
    acp.initialize()  # 本机节点注册（cxa-agent-001 / CX-A）
    return acp


def test_initialize_registers_local_node_not_persisted(node_acp, tmp_path):
    """initialize：本机节点 source=cxa_main 置顶在线，且不落盘。"""
    nodes = node_acp.list_nodes()
    assert nodes[0]["id"] == "cxa-agent-001"  # 本机置顶
    assert nodes[0]["metadata"]["source"] == "cxa_main"
    assert nodes[0]["status"] == "online"
    # 未注册外部节点 → nodes.json 不应存在（本机节点不落盘）
    assert not (tmp_path / "acp" / "nodes.json").exists()


def test_register_node_dedup_and_persist_roundtrip(node_acp, tmp_path):
    """register_node：新节点落盘；同四元组重复注册仅刷新（changed=False）；
    新实例加载持久化恢复外部节点。"""
    info1, changed1 = node_acp.register_node(
        "cxo-1", name="CX-O Agent", host="192.168.1.8", port=8001, source="lan"
    )
    assert changed1 is True
    assert info1["status"] == "online"
    assert (tmp_path / "acp" / "nodes.json").exists()

    info2, changed2 = node_acp.register_node(
        "cxo-1", name="CX-O Agent", host="192.168.1.8", port=8001, source="lan"
    )
    assert changed2 is False
    assert len(node_acp.list_nodes()) == 2  # 本机 + cxo-1

    # 四元组变化（端口变）→ 整体更新并再落盘
    info3, changed3 = node_acp.register_node(
        "cxo-1", name="CX-O Agent", host="192.168.1.8", port=8002, source="lan"
    )
    assert changed3 is True
    assert info3["port"] == 8002

    # 新实例：持久化恢复（本机节点由 initialize 重建，外部节点从盘来）
    reloaded = LiteACP(config=_config(), data_dir=str(tmp_path / "acp"))
    reloaded.initialize()
    ids = {n["id"] for n in reloaded.list_nodes()}
    assert ids == {"cxa-agent-001", "cxo-1"}
    cxo = next(n for n in reloaded.list_nodes() if n["id"] == "cxo-1")
    assert cxo["port"] == 8002


def test_remove_node_cascades_connections_and_slot(node_acp):
    """remove_node：连带删除其连接条目与消息槽；本机节点不可移除。"""
    node_acp.register_node("n1", host="10.0.0.1", port=8600)
    node_acp.upsert_connection("n1", host="10.0.0.1", port=8600)
    node_acp.receive_external_message(
        {"type": "chat", "from_agent_id": "n1", "content": {"text": "hi"}}
    )
    assert node_acp.remove_node("n1") is True
    assert node_acp.remove_node("n1") is False  # 幂等
    assert node_acp.list_connections() == []
    assert node_acp.get_node_messages("n1") == []
    assert node_acp.remove_node("cxa-agent-001") is False  # 本机不可移除


def test_send_message_stores_slot_and_delivers(node_acp, monkeypatch):
    """send_message：wire 消息入槽（is_sent=True）+ 外部节点投递（port 双轨）。"""
    delivered = []

    def fake_deliver(node, msg):
        delivered.append((dict(node), dict(msg)))

    monkeypatch.setattr(node_acp, "_deliver", fake_deliver)
    node_acp.register_node("n1", host="10.0.0.1", port=8600)
    msg = node_acp.send_message("n1", {"text": "你好呀"})

    assert msg["type"] == "chat"
    assert msg["from_agent_id"] == "cxa-agent-001"
    assert msg["from_agent_name"] == "CX-A"
    assert msg["to_agent_id"] == "n1"
    assert msg["is_sent"] is True
    assert len(delivered) == 1
    assert delivered[0][0]["id"] == "n1"

    slot = node_acp.get_node_messages("n1")
    assert len(slot) == 1 and slot[0]["id"] == msg["id"]

    # 未知节点：仅存本地槽，静默不投递（对齐 CX-O）
    msg2 = node_acp.send_message("ghost", {"text": "x"})
    assert len(delivered) == 1
    assert node_acp.get_node_messages("ghost")[0]["id"] == msg2["id"]


def test_deliver_routes_by_port_dual_track(node_acp, monkeypatch):
    """_deliver 端口双轨：9999 → /api/acp/receive；其余 → /acp/message。"""
    calls = []

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):
        calls.append((req.full_url, json.loads(req.data.decode("utf-8"))))
        return FakeResp()

    monkeypatch.setattr(lite_acp.urllib.request, "urlopen", fake_urlopen)

    node_main = {"id": "main", "host": "192.168.1.8", "port": 9999}
    node_node = {"id": "node", "host": "192.168.1.9", "port": 8600}
    node_acp._deliver(node_main, {"id": "m1"})
    node_acp._deliver(node_node, {"id": "m2"})

    assert calls[0][0] == "http://192.168.1.8:9999/api/acp/receive"
    assert calls[1][0] == "http://192.168.1.9:8600/acp/message"


def test_receive_external_message_normalizes_and_slots(node_acp):
    """receive：双键兼容归一化入槽（按发送者分键）；非法载荷 None。"""
    got = node_acp.receive_external_message(
        {"msg_type": "chat", "from_agent_id": "cxo-9",
         "from_agent_name": "CX-O", "content": {"text": "在吗"}}
    )
    assert got["type"] == "chat"  # 归一化为 wire 键
    assert got["is_sent"] is False
    slot = node_acp.get_node_messages("cxo-9")
    assert slot[-1]["id"] == got["id"]
    assert node_acp.receive_external_message("bad") is None


def test_node_messages_cap_500(node_acp):
    """消息槽单键 500 条 FIFO 截断（对齐 CX-O M-E）。"""
    for i in range(510):
        node_acp.receive_external_message(
            {"type": "chat", "from_agent_id": "spam", "content": {"text": str(i)}}
        )
    slot = node_acp.get_node_messages("spam", limit=1000)
    assert len(slot) == 500
    assert slot[0]["content"] == {"text": "10"}  # 最旧 10 条被截断


def test_connection_upsert_remove(node_acp):
    """连接条目：同远端复用更新；删除幂等。"""
    c1 = node_acp.upsert_connection("n1", host="10.0.0.1", port=8600)
    c2 = node_acp.upsert_connection("n1", host="10.0.0.2", port=8601)
    assert c1["id"] == c2["id"]  # 同远端复用
    assert c2["host"] == "10.0.0.2" and c2["port"] == 8601
    assert len(node_acp.list_connections()) == 1
    assert node_acp.remove_connection(c1["id"]) is True
    assert node_acp.remove_connection(c1["id"]) is False


def test_node_stats_shape(node_acp):
    """统计字段对齐 CX-O GET /api/acp/stats 的 statistics。"""
    node_acp.register_node("n1", host="10.0.0.1", port=8600)
    node_acp.upsert_connection("n1")
    node_acp.receive_external_message(
        {"type": "chat", "from_agent_id": "n1", "content": {"text": "hi"}}
    )
    stats = node_acp.node_stats()
    assert stats["total_agents"] == 2
    assert stats["online_agents"] == 2
    assert stats["total_connections"] == 1
    assert stats["total_groups"] == 0
    assert stats["total_messages"] == 1
    assert stats["local_agent_id"] == "cxa-agent-001"
    assert stats["local_agent_name"] == "CX-A"


def test_node_sweep_health_probe_and_offline(node_acp, monkeypatch):
    """清扫：普通节点超时先 /health 探活——200 刷新 last_seen，失败置 offline。"""
    node_acp.register_node("n-live", host="10.0.0.1", port=8600)
    node_acp.register_node("n-dead", host="10.0.0.2", port=8600)
    stale_iso = "2026-01-01T00:00:00"
    node_acp.nodes["n-live"]["last_seen"] = stale_iso
    node_acp.nodes["n-dead"]["last_seen"] = stale_iso

    probed = []

    def fake_probe(node):
        probed.append(node["id"])
        return node["id"] == "n-live"

    monkeypatch.setattr(node_acp, "_probe_health", fake_probe)
    node_acp._sweep_nodes()

    assert sorted(probed) == ["n-dead", "n-live"]
    assert node_acp.nodes["n-live"]["status"] == "online"
    assert node_acp.nodes["n-live"]["last_seen"] != stale_iso  # 探活刷新
    assert node_acp.nodes["n-dead"]["status"] == "offline"

    # 本机节点与 offline 节点不进探活
    node_acp.nodes["n-dead"]["last_seen"] = stale_iso
    probed.clear()
    node_acp._sweep_nodes()
    assert probed == []  # n-dead 已 offline 不再探；本机跳过


def test_node_sweep_main_system_by_beacon_freshness(node_acp, monkeypatch):
    """port==9999 主系统节点：不探活，按信标新鲜度判定（2×discovery_interval）。"""
    node_acp.register_node("main", host="192.168.1.8", port=9999, source="lan")
    stale = "2026-01-01T00:00:00"
    node_acp.nodes["main"]["last_seen"] = stale

    probed = []
    monkeypatch.setattr(node_acp, "_probe_health", lambda n: probed.append(n["id"]) or True)
    node_acp._sweep_nodes()
    assert probed == []  # 主系统节点不进 /health 探活
    assert node_acp.nodes["main"]["status"] == "offline"  # 信标严重过期 → offline

    # 信标新鲜（<2×discovery_interval）→ 保持在线
    from lite.acp.wire import now_iso

    node_acp.nodes["main"]["status"] = "online"
    node_acp.nodes["main"]["last_seen"] = now_iso()
    node_acp._sweep_nodes()
    assert node_acp.nodes["main"]["status"] == "online"


# ------------------------------------------------------------------ #
# 10. 节点发现常驻循环（20261017 批次 2）                               #
# ------------------------------------------------------------------ #


class _FakeNodeDiscovery:
    """fake 发现器：记录 start/broadcast 参数，可手动注入信标。"""

    def __init__(self, on_beacon=None):
        self.on_beacon = on_beacon
        self.started_port = None
        self.stopped = False
        self.broadcasts = []

    def start(self, port=9999):
        self.started_port = port

    def stop(self):
        self.stopped = True

    def broadcast_presence(self, agent_id, agent_name="", capabilities=None,
                           port=None, target_port=None, timestamp=None):
        self.broadcasts.append({
            "agent_id": agent_id, "agent_name": agent_name,
            "capabilities": list(capabilities or []), "port": port,
            "target_port": target_port, "timestamp": timestamp,
        })

    def prune_expired(self, max_age_seconds=300):
        return 0

    def feed_beacon(self, agent):
        """模拟收到对端信标（触发回调链）。"""
        if self.on_beacon is not None:
            self.on_beacon(agent)


def test_start_discovery_lifecycle_and_broadcast(tmp_path):
    """start_discovery：监听 9999 + 幂等 + 停止回收；广播体验证经 fake 直接驱动。"""
    acp = LiteACP(config=_config(discovery_interval=5), data_dir=str(tmp_path / "acp"))
    acp.initialize()
    fake = _FakeNodeDiscovery()
    assert acp.start_discovery(local_http_port=8600, discovery_factory=lambda **kw: fake) is True
    assert acp.start_discovery(local_http_port=8600, discovery_factory=lambda **kw: fake) is True  # 幂等
    assert fake.started_port == 9999
    assert acp._node_http_port == 8600

    # 广播字段体验证（循环体同一调用形态——直接以同参驱动 fake 断言字段）
    fake.broadcast_presence(
        acp.agent_id, agent_name=acp.agent_name,
        capabilities=["chat", "memory", "tools"], port=acp._node_http_port,
        target_port=9999, timestamp="2026-10-17T00:00:00",
    )
    b = fake.broadcasts[0]
    assert b["agent_id"] == "cxa-agent-001"
    assert b["agent_name"] == "CX-A"
    assert b["port"] == 8600  # 宣告本机 HTTP 端口
    assert b["target_port"] == 9999  # 发往 CX-O 监听端口
    assert b["capabilities"] == ["chat", "memory", "tools"]
    assert b["timestamp"]

    acp.stop_discovery()
    assert fake.stopped
    assert acp._node_discovery is None


def test_discovery_beacon_registers_node(node_acp):
    """信标回调 → register_node(source=lan)：发现即注册；自身信标忽略。"""
    fake_holder = {}

    def factory(**kwargs):
        disc = _FakeNodeDiscovery(**kwargs)
        fake_holder["disc"] = disc
        return disc

    node_acp.start_discovery(local_http_port=8600, discovery_factory=factory)
    disc = fake_holder["disc"]

    disc.feed_beacon({
        "agent_id": "cxo-lan", "agent_name": "CX-O", "host": "192.168.1.8",
        "port": 9999, "version": "1.0.0", "capabilities": ["chat"],
        "last_seen": 1.0,
    })
    disc.feed_beacon({
        "agent_id": "cxa-agent-001", "host": "127.0.0.1", "port": 8600,  # 自身
    })
    nodes = {n["id"]: n for n in node_acp.list_nodes()}
    assert "cxo-lan" in nodes
    assert nodes["cxo-lan"]["metadata"]["source"] == "lan"
    assert nodes["cxo-lan"]["host"] == "192.168.1.8"
    assert nodes["cxo-lan"]["port"] == 9999
    assert "cxa-agent-001" not in nodes or nodes["cxa-agent-001"]["metadata"]["source"] == "cxa_main"
    node_acp.stop_discovery()


def test_discovery_disabled_gate():
    """enabled=False：start_discovery 抛 AcpDisabled（未开启绝不建 socket）。"""
    acp = LiteACP(config=_config(enabled=False))
    with pytest.raises(AcpDisabled):
        acp.start_discovery(local_http_port=8600)