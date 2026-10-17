# -*- coding: utf-8 -*-
"""ACP 节点互联 REST 端点集成测试（20261017 CX-O 全量迁移批次 3）。

覆盖：enabled 闸门（503）、节点 CRUD/连接/发送/接收/统计端点、开放接收面
（/acp/message、/acp/health 豁免 Host/令牌闸）、X-ACP-Key 协议令牌、
PUT acp.enabled 热接线。全 mock 无真实 UDP/LLM。
"""

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

import pytest

import lite.acp.lite_acp as lite_acp_mod
from lite.computer_control import ComputerControl, ToolBridge
from lite.computer_control.security import ControlAuthorizer
from lite.config.config_manager import ConfigManager
from lite.server.api_server import build_deps, make_handler


class _RaisingFallback:
    """chat 即抛的兜底替身：自动回复链路静默失败（不污染消息槽断言）。"""

    status = "local"

    def local_mode_enabled(self):
        return True

    def chat(self, messages):
        raise RuntimeError("测试桩：聊天管线不可用")


def _make_config(tmp_path, enabled=True, auth_token=""):
    cfg = ConfigManager(
        config_path=str(tmp_path / "config.json"),
        data_dir=str(tmp_path / "data"),
    )
    cfg.set("acp", "enabled", enabled)
    cfg.set("acp", "auth_token", auth_token)
    return cfg


@contextmanager
def running_acp_server(tmp_path, enabled=True, auth_token="", hotwire=None):
    """起服（ACP 开关 + stub 心跳/发现/投递，无真实 socket 与网络）。

    :param hotwire: 可选打点列表——stub 方法被调用时追加方法名（热接线断言用）。
      stub 签名透传（*a, **k），恢复由本 fixture finally 负责；**测试内严禁再对
      LiteACP 类做 monkeypatch.setattr**——with 块内的 monkeypatch teardown 会把
      stub 写回造成跨文件泄漏（曾致 test_lite_acp 污染，20261017 实证）。
    """
    monkey_target = lite_acp_mod.LiteACP
    # 类级 stub：常驻线程 / UDP socket / 真实网络投递不进测试（实例方法覆写）
    orig = (monkey_target.start_heartbeat, monkey_target.start_discovery,
            monkey_target._deliver)

    def _stub(name):
        def _call(*args, **kwargs):
            if hotwire is not None:
                hotwire.append(name)
            return True
        return _call

    monkey_target.start_heartbeat = _stub("start_heartbeat")
    monkey_target.start_discovery = _stub("start_discovery")
    monkey_target._deliver = lambda self, *a, **k: (
        hotwire.append("deliver") if hotwire is not None else None
    )
    try:
        store, pipeline, manager, _remote = build_deps(data_dir=str(tmp_path))
        authorizer = ControlAuthorizer(data_dir=str(tmp_path))
        computer = ComputerControl(authorized=authorizer.is_authorized())
        bridge = ToolBridge(computer=computer, authorizer=authorizer)
        handler = make_handler(
            store, pipeline, manager,
            computer=computer, authorizer=authorizer, bridge=bridge,
            chat_fallback=_RaisingFallback(),
            config=_make_config(tmp_path, enabled=enabled, auth_token=auth_token),
        )
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{httpd.server_address[1]}"
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
            store.close()
            close = getattr(pipeline.vector_store, "close", None)
            if callable(close):
                close()
    finally:
        (monkey_target.start_heartbeat, monkey_target.start_discovery,
         monkey_target._deliver) = orig


def _post(url, payload, headers=None, method="POST"):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_acp_disabled_gate(tmp_path):
    """enabled=False：管理端点 503 acp_disabled（默认关零暴露面）。"""
    with running_acp_server(tmp_path, enabled=False) as base:
        status, body = _get(f"{base}/api/acp/agents")
        assert status == 503
        assert body["error"] == "acp_disabled"
        # 开放接收面同样被闸（未启用即拒绝）
        status, body = _post(f"{base}/acp/message", {"type": "chat"})
        assert status == 503


def test_agents_list_contains_local_node(tmp_path):
    """GET /api/acp/agents：本机节点置顶（source=cxa_main，online）。"""
    with running_acp_server(tmp_path) as base:
        status, body = _get(f"{base}/api/acp/agents")
        assert status == 200
        assert body["status"] == "success"
        assert body["total"] >= 1
        local = body["agents"][0]
        assert local["id"] == "cxa-agent-001"
        assert local["metadata"]["source"] == "cxa_main"
        assert local["status"] == "online"


def test_manual_add_and_delete_node(tmp_path):
    """POST /api/acp/agents 手动加节点 → 列表可见 → DELETE 移除 → 404。"""
    with running_acp_server(tmp_path) as base:
        status, body = _post(
            f"{base}/api/acp/agents",
            {"name": "家里的CX-O", "host": "192.168.1.8", "port": 8001},
        )
        assert status == 200
        agent_id = body["agent"]["id"]
        assert agent_id == "manual_192.168.1.8_8001"

        status, body = _get(f"{base}/api/acp/agents")
        assert any(a["id"] == agent_id for a in body["agents"])

        # 非法端口 400
        status, _ = _post(f"{base}/api/acp/agents", {"host": "h", "port": 0})
        assert status == 400

        status_del, _ = _delete(f"{base}/api/acp/agents/{agent_id}")
        assert status_del == 200
        status_del, _ = _delete(f"{base}/api/acp/agents/{agent_id}")
        assert status_del == 404
        status, body = _get(f"{base}/api/acp/agents")
        assert all(a["id"] != agent_id for a in body["agents"])


def _delete(url):
    req = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_connect_disconnect_roundtrip(tmp_path):
    """connect 建通讯录条目（未知节点 404）；DELETE 断开（幂等 404）。"""
    with running_acp_server(tmp_path) as base:
        _post(f"{base}/api/acp/agents", {"host": "10.0.0.1", "port": 8600, "name": "n1"})
        status, body = _post(
            f"{base}/api/acp/connect", {"agent_id": "manual_10.0.0.1_8600"}
        )
        assert status == 200
        conn_id = body["connection"]["id"]
        assert body["connection"]["status"] == "connecting"

        status, body = _get(f"{base}/api/acp/connections")
        assert body["total"] == 1

        status, _ = _delete(f"{base}/api/acp/connect/{conn_id}")
        assert status == 200
        status, _ = _delete(f"{base}/api/acp/connect/{conn_id}")
        assert status == 404

        # 未知节点 connect → 404
        status, body = _post(f"{base}/api/acp/connect", {"agent_id": "ghost"})
        assert status == 404


def test_send_and_receive_roundtrip(tmp_path):
    """send 入槽 + receive 外部投递入槽（自动回复链路失败静默不污染断言）。"""
    with running_acp_server(tmp_path) as base:
        _post(f"{base}/api/acp/agents", {"host": "10.0.0.1", "port": 8600, "name": "n1"})
        agent_id = "manual_10.0.0.1_8600"
        status, body = _post(
            f"{base}/api/acp/send",
            {"to_agent_id": agent_id, "content": {"text": "在忙吗"}},
        )
        assert status == 200
        assert body["message_id"]

        status, body = _get(f"{base}/api/acp/messages?agent_id={agent_id}")
        assert status == 200
        assert body["messages"][-1]["content"] == {"text": "在忙吗"}
        assert body["messages"][-1]["is_sent"] is True

        # receive：CX-O 主系统标准入口（双键兼容）
        status, body = _post(
            f"{base}/api/acp/receive",
            {"msg_type": "chat", "from_agent_id": "cxo-main",
             "from_agent_name": "CX-O", "content": {"text": "你好呀"}},
        )
        assert status == 200
        assert body["data"]["type"] == "chat"
        status, body = _get(f"{base}/api/acp/messages?agent_id=cxo-main")
        assert body["messages"][-1]["content"] == {"text": "你好呀"}

        # 非法消息结构 400
        status, _ = _post(f"{base}/api/acp/receive", "bad")
        assert status == 400


def test_open_receive_face_bypasses_host_and_token(tmp_path, monkeypatch):
    """/acp/message 与 /acp/health：豁免 Host/令牌闸（外部节点无本机令牌）。"""
    # 模拟令牌环境：设置启动令牌 env（N1 闸会要求 X-Client-Token）
    monkeypatch.setenv("CXA_API_TOKEN", "secret-token")
    try:
        with running_acp_server(tmp_path) as base:
            # 带恶意 Host 且无令牌 → 开放面仍可达
            status, body = _get(f"{base}/acp/health", headers={"Host": "evil.example.com"})
            assert status == 200
            assert body["status"] == "ok"

            status, body = _post(
                f"{base}/acp/message",
                {"type": "chat", "from_agent_id": "node-x", "content": {"text": "hi"}},
                headers={"Host": "evil.example.com"},
            )
            assert status == 200
            assert body["status"] == "received"

            # 对照：非豁免路径被 Host 闸拦截
            status, _ = _get(f"{base}/api/acp/agents", headers={"Host": "evil.example.com"})
            assert status == 403
    finally:
        monkeypatch.delenv("CXA_API_TOKEN", raising=False)


def test_acp_key_token_matrix(tmp_path):
    """X-ACP-Key：空=开放；配置后缺头/错值 403、正确 200（对齐 CX-O）。"""
    with running_acp_server(tmp_path) as base:
        status, _ = _post(f"{base}/api/acp/receive", {"type": "chat", "from_agent_id": "x"})
        assert status == 200  # 缺省开放

    with running_acp_server(tmp_path, auth_token="shared-key") as base:
        status, _ = _post(f"{base}/api/acp/receive", {"type": "chat", "from_agent_id": "x"})
        assert status == 403  # 缺头
        status, _ = _post(
            f"{base}/api/acp/receive", {"type": "chat", "from_agent_id": "x"},
            headers={"X-ACP-Key": "wrong"},
        )
        assert status == 403
        status, _ = _post(
            f"{base}/api/acp/receive", {"type": "chat", "from_agent_id": "x"},
            headers={"X-ACP-Key": "shared-key"},
        )
        assert status == 200


def test_stats_endpoint(tmp_path):
    """GET /api/acp/stats：statistics 字段对齐 CX-O。"""
    with running_acp_server(tmp_path) as base:
        status, body = _get(f"{base}/api/acp/stats")
        assert status == 200
        stats = body["statistics"]
        assert stats["local_agent_id"] == "cxa-agent-001"
        assert stats["local_agent_name"] == "CX-A"
        assert stats["total_agents"] >= 1


def test_settings_acp_roundtrip_and_hot_wiring(tmp_path):
    """PUT acp.enabled/auth_token：白名单生效 + 热接线调用（hotwire 打点）+ GET 回显。

    initialize/stop_discovery/stop_heartbeat 为真方法（无 socket 风险，不经 stub）；
    start_heartbeat/start_discovery 经 fixture stub 打点观察。
    """
    calls: list[str] = []
    # 热接线断言经 fixture 的 hotwire 打点（严禁测试内 monkeypatch LiteACP 类——
    # with 块内 setattr 的 teardown 会把 fixture stub 写回造成跨文件泄漏）
    with running_acp_server(tmp_path, enabled=False, hotwire=calls) as base:
        status, body = _post(f"{base}/api/settings", {"acp": {"enabled": True}}, method="PUT")
        assert status == 200
        assert "acp.enabled" in body["applied"]
        assert calls.count("start_heartbeat") == 1
        assert calls.count("start_discovery") == 1
        # 真initialize 生效：本机节点出现在列表
        status, body = _get(f"{base}/api/acp/agents")
        assert any(a["id"] == "cxa-agent-001" for a in body["agents"])

        status, body = _post(
            f"{base}/api/settings", {"acp": {"auth_token": "k1"}}, method="PUT"
        )
        assert "acp.auth_token" in body["applied"]

        status, body = _get(f"{base}/api/settings")
        assert body["acp"]["enabled"] is True
        assert body["acp"]["agent_id"] == "cxa-agent-001"

        status, body = _post(f"{base}/api/settings", {"acp": {"enabled": False}}, method="PUT")
        # 关闭再开启：start_discovery 第二次热接线（stop 为真方法，不产生打点）
        status, body = _post(f"{base}/api/settings", {"acp": {"enabled": True}}, method="PUT")
        assert calls.count("start_discovery") == 2

        # 非布尔入 ignored
        status, body = _post(f"{base}/api/settings", {"acp": {"enabled": "yes"}}, method="PUT")
        assert any("acp.enabled" in item for item in body["ignored"])
