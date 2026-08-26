# -*- coding: utf-8 -*-
"""Remote API 集成测试（Task E3）——真实起服 + urllib HTTP 请求。

复用既有起服模式：注入带 mock transport 的 RemoteController，验证三个远端外露
端点 GET /api/remote/status、POST /api/remote/control、POST /api/remote/push_config
转发正确；未启用（enabled=false）时返回 503。
"""

import json
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer

import pytest

from lite.management.remote import RemoteController
from lite.server.api_server import build_deps, make_handler


class _FakeConfig:
    """仅提供 remote 段字段的轻量配置替身。"""

    def __init__(self, endpoint, enabled):
        self._remote = {"endpoint": endpoint, "enabled": enabled}

    def get(self, section, key, default=None):
        if section != "remote":
            return default
        return self._remote.get(key, default)


#: 启用态 RemoteController 的 mock 固定响应
_STATUS_RESPONSE = {"status": "success", "snapshot": {"online": True, "load": 0.3, "budget": 60}}
_CONTROL_RESPONSE = {"status": "success", "result": {"ok": True}}
_CONFIG_RESPONSE = {"status": "success", "message": "配置已更新"}


class _FakeTransport:
    """按端点返回固定数据的 mock transport，不对真实网络发起请求。"""

    def __init__(self, status=_STATUS_RESPONSE, control=_CONTROL_RESPONSE, config=_CONFIG_RESPONSE):
        self._status = status
        self._control = control
        self._config = config
        self.calls = []

    def request(self, method, url, json_body=None, timeout=10):
        self.calls.append({"method": method, "url": url, "body": json_body})
        # 依据路径选择对应固定响应（status / control / config）
        if url.endswith("/api/admin/status"):
            return self._status
        if url.endswith("/api/admin/control"):
            return self._control
        return self._config


def http_request(url, payload=None, method="GET"):
    """任意方法请求，返回 (status, JSON)；非 2xx 同样解析错误体返回。"""
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, body


def _serve(tmp_path, remote):
    """构造注入指定 RemoteController 的单线程 HTTPServer，返回 (server, base)。"""
    store, pipeline, manager, _ = build_deps(data_dir=str(tmp_path))
    handler = make_handler(store, pipeline, manager, remote)
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    base = f"http://127.0.0.1:{port}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread, base


@pytest.fixture()
def enabled_server(tmp_path):
    """启用态远端服务：mock transport 返回固定数据，断言 API 转发结果。"""
    transport = _FakeTransport(_STATUS_RESPONSE)
    remote = RemoteController(config=_FakeConfig("http://remote-cxo", True), transport=transport)
    httpd, thread, base = _serve(tmp_path, remote)
    try:
        yield base, transport
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def disabled_server(tmp_path):
    """未启用态远端服务：enabled=false，所有远端端点返回 503。"""
    remote = RemoteController(config=_FakeConfig("", False), transport=_FakeTransport({}))
    httpd, thread, base = _serve(tmp_path, remote)
    try:
        yield base
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------- status 转发
def test_remote_status_forward(enabled_server):
    base, transport = enabled_server
    status, body = http_request(f"{base}/api/remote/status")
    assert status == 200
    assert body == _STATUS_RESPONSE  # 原样透传
    call = transport.calls[-1]
    assert call["method"] == "GET"
    assert call["url"] == "http://remote-cxo/api/admin/status"


# ---------------------------------------------------------------- control 转发
def test_remote_control_forward(enabled_server):
    base, transport = enabled_server
    status, body = http_request(
        f"{base}/api/remote/control",
        payload={"action": "enable", "agent_id": "default"},
        method="POST",
    )
    assert status == 200
    assert body == _CONTROL_RESPONSE
    call = transport.calls[-1]
    assert call["method"] == "POST"
    assert call["url"] == "http://remote-cxo/api/admin/control"
    assert call["body"] == {"action": "enable", "agent_id": "default"}


def test_remote_control_bad_action_400(enabled_server):
    base, _transport = enabled_server
    status, body = http_request(
        f"{base}/api/remote/control", payload={"action": "explode"}, method="POST"
    )
    assert status == 400
    assert body["error"] == "bad_request"


# ---------------------------------------------------------------- push_config 转发
def test_remote_push_config_forward(enabled_server):
    base, transport = enabled_server
    patch = {"autonomy": {"enabled": True}}
    status, body = http_request(
        f"{base}/api/remote/push_config", payload=patch, method="POST"
    )
    assert status == 200
    assert body == _CONFIG_RESPONSE
    call = transport.calls[-1]
    assert call["method"] == "POST"
    assert call["url"] == "http://remote-cxo/api/admin/config"
    assert call["body"] == patch


def test_remote_push_config_empty_400(enabled_server):
    base, _transport = enabled_server
    status, body = http_request(f"{base}/api/remote/push_config", payload={}, method="POST")
    assert status == 400


# ---------------------------------------------------------------- 未启用 -> 503
def test_remote_disabled_503_on_status(disabled_server):
    base = disabled_server
    status, body = http_request(f"{base}/api/remote/status")
    assert status == 503
    assert body["error"] == "remote_disabled"


def test_remote_disabled_503_on_control(disabled_server):
    base = disabled_server
    status, body = http_request(
        f"{base}/api/remote/control", payload={"action": "enable"}, method="POST"
    )
    assert status == 503
    assert body["error"] == "remote_disabled"


def test_remote_disabled_503_on_push_config(disabled_server):
    base = disabled_server
    status, body = http_request(
        f"{base}/api/remote/push_config", payload={"llm": {"model": "x"}}, method="POST"
    )
    assert status == 503
    assert body["error"] == "remote_disabled"