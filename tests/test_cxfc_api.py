# -*- coding: utf-8 -*-
"""CXFC direct + 远端调用 REST 端点集成测试（20261017 CX-O 全量迁移批次 5）。

覆盖：enabled 闸门、插件注册（TOFU/一次性令牌）、心跳、发现、远端节点 CRUD、
跨机调用（mock 客户端）、settings cxfc 段回显与热接线。全 mock 无真实网络。
"""

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

import lite.cxfc.direct as cxfc_direct_mod
from lite.computer_control import ComputerControl, ToolBridge
from lite.computer_control.security import ControlAuthorizer
from lite.config.config_manager import ConfigManager
from lite.server.api_server import build_deps, make_handler


def _make_config(tmp_path, enabled=True, admin_key=""):
    cfg = ConfigManager(
        config_path=str(tmp_path / "config.json"),
        data_dir=str(tmp_path / "data"),
    )
    cfg.set("cxfc", "enabled", enabled)
    cfg.set("cxfc", "remote_admin_key", admin_key)
    return cfg


@contextmanager
def running_cxfc_server(tmp_path, enabled=True, admin_key=""):
    """起服（CXFC 开关 + stub 心跳线程，无真实 socket），yield base_url。

    stub 签名透传（*a, **k）且 finally 恢复——严禁测试内对 CxfcDirectManager
    类做 monkeypatch.setattr（with 块内 setattr 的 teardown 会把 stub 写回泄漏）。
    """
    target = cxfc_direct_mod.CxfcDirectManager
    orig_hb = target.start_heartbeat
    target.start_heartbeat = lambda self, *a, **k: True
    try:
        store, pipeline, manager, _remote = build_deps(data_dir=str(tmp_path))
        authorizer = ControlAuthorizer(data_dir=str(tmp_path))
        computer = ComputerControl(authorized=authorizer.is_authorized())
        bridge = ToolBridge(computer=computer, authorizer=authorizer)
        handler = make_handler(
            store, pipeline, manager,
            computer=computer, authorizer=authorizer, bridge=bridge,
            config=_make_config(tmp_path, enabled=enabled, admin_key=admin_key),
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
        target.start_heartbeat = orig_hb


def _req(url, payload=None, method="POST"):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_cxfc_disabled_gate(tmp_path):
    """enabled=False：端点 503 cxfc_disabled（默认关零暴露面）。"""
    with running_cxfc_server(tmp_path, enabled=False) as base:
        status, body = _req(f"{base}/api/cxfc/plugins", method="GET")
        assert status == 503
        assert body["error"] == "cxfc_disabled"


def test_register_heartbeat_delete_roundtrip(tmp_path):
    """注册（一次性令牌、列表无哈希）→ 心跳 alive/404 → 删除 404 幂等。"""
    with running_cxfc_server(tmp_path) as base:
        status, body = _req(f"{base}/api/cxfc/register", {
            "host": "127.0.0.1", "port": 9100, "name": "本地插件",
            "tools": [{"name": "echo", "description": "回显"}],
        })
        assert status == 200
        assert body["status"] == "ok"
        plugin_id = body["plugin_id"]
        assert plugin_id == "cxfc_127.0.0.1_9100"
        assert len(body["plugin_access_token"]) == 64

        status, body = _req(f"{base}/api/cxfc/plugins", method="GET")
        assert body["plugins"][0]["plugin_id"] == plugin_id
        assert "plugin_access_token_hash" not in body["plugins"][0]

        status, body = _req(f"{base}/api/cxfc/heartbeat", {"plugin_id": plugin_id, "port": 9100})
        assert status == 200 and body["status"] == "alive"
        status, _ = _req(f"{base}/api/cxfc/heartbeat", {"plugin_id": "ghost", "port": 1})
        assert status == 404

        status, _ = _req(f"{base}/api/cxfc/plugins/{plugin_id}", method="DELETE")
        assert status == 200
        status, _ = _req(f"{base}/api/cxfc/plugins/{plugin_id}", method="DELETE")
        assert status == 404


def test_register_invalid_tofu_400(tmp_path, monkeypatch):
    """TOFU 指纹不匹配 → 400（注册失败）。"""
    monkeypatch.setattr(
        cxfc_direct_mod, "_cert_fingerprint",
        lambda pem: "a" * 64,
    )
    with running_cxfc_server(tmp_path) as base:
        status, body = _req(f"{base}/api/cxfc/register", {
            "host": "10.0.0.1", "port": 9101,
            "tls_cert_pem": "-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----\n",
            "tls_cert_fingerprint": "b" * 64,
        })
        assert status == 400
        assert "指纹" in body["message"]


def test_remote_nodes_crud_and_call(tmp_path, monkeypatch):
    """远端节点：添加/列表/删除 + 未配密钥调用拒绝 + mock 跨机调用成功。"""
    with running_cxfc_server(tmp_path) as base:
        status, body = _req(f"{base}/api/cxfc/remote/nodes", {
            "host": "192.168.1.8", "port": 8001, "name": "家里的CX-O",
        })
        assert status == 200
        node_id = body["node"]["id"]
        assert node_id == "remote_192.168.1.8_8001"

        status, body = _req(f"{base}/api/cxfc/remote/nodes", method="GET")
        assert body["nodes"][0]["id"] == node_id

        # 未配置管理密钥：跨机调用返回业务失败外壳（不 5xx）
        status, body = _req(
            f"{base}/api/cxfc/remote/nodes/{node_id}/call",
            {"plugin_id": "cxfc_127.0.0.1_9000", "tool": "speak", "arguments": {}},
        )
        assert status == 200
        assert body["result"]["success"] is False
        assert "管理密钥" in body["result"]["error"]

        # mock 客户端调用：透传参数与结果
        captured = {}

        def fake_call(self, node_id_, plugin_id, tool, arguments=None, timeout_s=None):
            captured.update({"node": node_id_, "plugin": plugin_id, "tool": tool})
            return {"success": True, "result": {"echo": tool}}

        monkeypatch.setattr(cxfc_direct_mod.CxfcRemoteClient, "call_plugin", fake_call)
        status, body = _req(
            f"{base}/api/cxfc/remote/nodes/{node_id}/call",
            {"plugin_id": "p1", "tool": "speak", "arguments": {"text": "你好"}},
        )
        assert status == 200
        assert body["result"]["success"] is True
        assert captured == {"node": node_id, "plugin": "p1", "tool": "speak"}

        status, _ = _req(f"{base}/api/cxfc/remote/nodes/{node_id}", method="DELETE")
        assert status == 200
        status, _ = _req(f"{base}/api/cxfc/remote/nodes/{node_id}", method="DELETE")
        assert status == 404


def test_settings_cxfc_roundtrip(tmp_path):
    """PUT cxfc.enabled/remote_admin_key：白名单生效 + GET 脱敏回显。"""
    with running_cxfc_server(tmp_path, enabled=False) as base:
        status, body = _req(
            f"{base}/api/settings",
            {"cxfc": {"enabled": True, "remote_admin_key": "sk-admin-1234"}},
            method="PUT",
        )
        assert status == 200
        assert "cxfc.enabled" in body["applied"]
        assert "cxfc.remote_admin_key" in body["applied"]

        status, body = _req(f"{base}/api/settings", method="GET")
        assert body["cxfc"]["enabled"] is True
        assert "sk-admin-1234" not in body["cxfc"]["remote_admin_key"]
        assert body["cxfc"]["remote_admin_key"]


def test_remote_plugins_enter_tool_registry(tmp_path, monkeypatch):
    """fetch 后远端插件进桌宠工具表：GET /api/tools 可见 remote_ 条目且可调用。"""
    node_id = "remote_192.168.1.8_8001"

    def fake_fetch(self, node_id_):
        plugins = [{
            "plugin_id": "cxfc_127.0.0.1_9000",
            "name": "语音工作站",
            "tools": [
                {"name": "speak", "description": "让对面说话"},
                "raw_tool_name",  # 字符串形态兼容
            ],
        }]
        # 真实 fetch_plugins 会把清单缓存进节点条目——mock 同步该副作用
        self._nodes[node_id_]["plugins"] = plugins
        self._nodes[node_id_]["plugins_fetched_at"] = "t"
        return plugins, dict(self._nodes[node_id_])

    monkeypatch.setattr(cxfc_direct_mod.CxfcRemoteClient, "fetch_plugins", fake_fetch)
    calls = {}

    def fake_call(self, node_id_, plugin_id, tool, arguments=None, timeout_s=None):
        calls.update({"node": node_id_, "plugin": plugin_id, "tool": tool, "args": arguments})
        return {"success": True, "result": {"said": arguments.get("text")}}

    monkeypatch.setattr(cxfc_direct_mod.CxfcRemoteClient, "call_plugin", fake_call)

    with running_cxfc_server(tmp_path) as base:
        # 添加节点（缓存空清单）→ mock fetch 拉到插件
        _req(f"{base}/api/cxfc/remote/nodes", {"host": "192.168.1.8", "port": 8001})
        status, body = _req(f"{base}/api/cxfc/remote/nodes/{node_id}/fetch", method="POST")
        assert status == 200

        # GET /api/tools：远端工具懒刷新可见
        status, body = _req(f"{base}/api/tools", method="GET")
        tools = {t["id"]: t for t in body["tools"]}
        assert tools["remote_remote_192.168.1.8_8001_cxfc_127.0.0.1_9000_speak"]["source"] == "remote"
        assert "remote_remote_192.168.1.8_8001_cxfc_127.0.0.1_9000_raw_tool_name" in tools

        # tools/call：经 caller 跨机调用（X-API-Key 链路注入）；响应外壳 {ok, result}
        status, body = _req(f"{base}/api/tools/call", {
            "name": "remote_remote_192.168.1.8_8001_cxfc_127.0.0.1_9000_speak",
            "arguments": {"text": "你好"},
        })
        assert status == 200
        assert body["ok"] is True
        assert body["result"] == {"said": "你好"}
        assert calls == {
            "node": node_id, "plugin": "cxfc_127.0.0.1_9000", "tool": "speak",
            "args": {"text": "你好"},
        }

        # 再次 fetch（清单变更）→ 旧 remote 工具清空不残留
        def fake_fetch_empty(self, node_id_):
            self._nodes[node_id_]["plugins"] = []
            self._nodes[node_id_]["plugins_fetched_at"] = "t"
            return [], dict(self._nodes[node_id_])

        monkeypatch.setattr(cxfc_direct_mod.CxfcRemoteClient, "fetch_plugins", fake_fetch_empty)
        _req(f"{base}/api/cxfc/remote/nodes/{node_id}/fetch", method="POST")
        status, body = _req(f"{base}/api/tools", method="GET")
        assert all(not t["id"].startswith("remote_") for t in body["tools"])
