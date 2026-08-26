# -*- coding: utf-8 -*-
"""Remote 公网预留架构测试（Task F4）——RemoteTransport 基类抽象、HTTP 默认
transport 行为等价、public_endpoint 占位存取与不侵入局域网行为。

覆盖：
- RemoteTransport 基类抽象方法调用抛 NotImplementedError；
- HTTPRemoteTransport（既有 urllib 实现迁入）经 monkeypatch urlopen 行为等价；
- set_public_endpoint / clear_public_endpoint 存取正确，且不影响 get_status
  （mock transport 验证仍走局域网 endpoint）；
- RemoteController 既有三方法（get_status / control / push_config）在此扩展后
  行为不变。

这些用例与既有 tests/test_remote.py、tests/test_remote_api.py 互补，复用作假配置
与 mock transport 手法，不破坏既有断言。
"""

import urllib.request

import pytest

from lite.management.remote import (
    HTTPRemoteTransport,
    RemoteController,
    RemoteDisabled,
    RemoteTransport,
)


class _FakeConfig:
    """仅提供 remote 段字段的轻量配置替身（借用既有 test_remote.py 手法）。"""

    def __init__(self, endpoint, enabled):
        self._remote = {"endpoint": endpoint, "enabled": enabled}

    def get(self, section, key, default=None):
        if section != "remote":
            return default
        return self._remote.get(key, default)


class _RecorderTransport:
    """记录每次调用并按需返回固定数据 / 抛异常的 mock transport。"""

    def __init__(self, response=None):
        self.response = response
        self.calls = []

    def request(self, method, url, json_body=None, timeout=10):
        self.calls.append({"method": method, "url": url, "body": json_body})
        return self.response


def _enabled_controller(transport, endpoint="http://remote-cxo"):
    """构造已启用（enabled=true）的遥控控制器。"""
    return RemoteController(
        config=_FakeConfig(endpoint, True), transport=transport
    )


# ---------------------------------------------------------------- RemoteTransport 抽象
def test_remote_transport_base_request_raises_not_implemented():
    """RemoteTransport 基类抽象方法调用抛 NotImplementedError。"""
    with pytest.raises(NotImplementedError):
        RemoteTransport().request("GET", "/api/admin/status")


def test_remote_transport_is_http_remote_transport_base():
    """HTTPRemoteTransport 继承自公开基类 RemoteTransport。"""
    assert issubclass(HTTPRemoteTransport, RemoteTransport)


# ---------------------------------------------------------------- HTTPRemoteTransport 行为等价
class _FakeURLResponse:
    """最小 urllib 响应替身（read 返回字节、支持上下文管理器）。"""

    def __init__(self, payload):
        self._payload = payload

    def read(self, n=-1):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_http_remote_transport_json_response(monkeypatch):
    """HTTPRemoteTransport 经 mock urlopen 返回解析后的 JSON dict。"""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return _FakeURLResponse(b'{"online": true, "load": 0.5}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    transport = HTTPRemoteTransport()

    result = transport.request("GET", "http://remote-cxo/api/admin/status")

    assert result == {"online": True, "load": 0.5}
    assert captured["req"].full_url == "http://remote-cxo/api/admin/status"
    assert captured["req"].get_method() == "GET"


def test_http_remote_transport_non_json_returns_raw(monkeypatch):
    """HTTPRemoteTransport 对非 JSON 响应返回 {"raw": <body>}。"""
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda req, timeout=None: _FakeURLResponse(b"plain")
    )
    transport = HTTPRemoteTransport()

    result = transport.request("GET", "http://remote-cxo/api/admin/status")

    assert result == {"raw": "plain"}


def test_http_remote_transport_sends_json_body(monkeypatch):
    """HTTPRemoteTransport POST 携带 json_body 并设置 Content-Type。"""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return _FakeURLResponse(b'{"status": "success"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    transport = HTTPRemoteTransport()

    result = transport.request(
        "POST", "http://remote-cxo/api/admin/control", json_body={"action": "enable"}
    )

    assert result == {"status": "success"}
    assert captured["req"].get_method() == "POST"
    header_keys = {k.lower(): v for k, v in captured["req"].headers.items()}
    assert header_keys["content-type"] == "application/json"
    assert captured["req"].data is not None


# ---------------------------------------------------------------- public_endpoint 占位
def test_public_endpoint_default_none():
    """public_endpoint 默认 None（未配置公网预留）。"""
    ctrl = _enabled_controller(_RecorderTransport({}))
    assert ctrl.public_endpoint is None


def test_set_public_endpoint_stores_url():
    """set_public_endpoint 仅存储 URL（去尾部斜杠），不做连接/认证。"""
    ctrl = _enabled_controller(_RecorderTransport({}))
    ctrl.set_public_endpoint("https://public-cxo.example/")
    assert ctrl.public_endpoint == "https://public-cxo.example"


def test_set_public_endpoint_none_clears():
    """set_public_endpoint(None) 视为清除。"""
    ctrl = _enabled_controller(_RecorderTransport({}))
    ctrl.set_public_endpoint("https://public-cxo.example")
    ctrl.set_public_endpoint(None)
    assert ctrl.public_endpoint is None


def test_clear_public_endpoint_returns_none():
    """clear_public_endpoint 归复原点为 None。"""
    ctrl = _enabled_controller(_RecorderTransport({}))
    ctrl.set_public_endpoint("https://public-cxo.example")
    ctrl.clear_public_endpoint()
    assert ctrl.public_endpoint is None


def test_public_endpoint_does_not_affect_get_status():
    """设置 public_endpoint 后 get_status 仍走局域网 endpoint。"""
    expected = {"status": "success", "snapshot": {"online": True}}
    transport = _RecorderTransport(expected)
    ctrl = _enabled_controller(transport, endpoint="http://lan-cxo")

    ctrl.set_public_endpoint("https://public-cxo.example")
    result = ctrl.get_status()

    assert result == expected
    assert transport.calls[-1]["url"] == "http://lan-cxo/api/admin/status"


def test_public_endpoint_not_used_by_any_request():
    """设置 public_endpoint 后，control / push_config 同样不触碰公网端点。"""
    transport = _RecorderTransport({"status": "success"})
    ctrl = _enabled_controller(transport, endpoint="http://lan-cxo")
    ctrl.set_public_endpoint("https://public-cxo.example")

    ctrl.control("enable")
    ctrl.push_config({"llm": {"model": "x"}})

    urls = [call["url"] for call in transport.calls]
    assert urls == [
        "http://lan-cxo/api/admin/control",
        "http://lan-cxo/api/admin/config",
    ]
    assert all("http://lan-cxo" in url for url in urls)


# ---------------------------------------------------------------- 既有三方法行为不变
def test_existing_methods_unchanged_after_extension():
    """RemoteController 既有三方法在此扩展后行为不变（复用 mock transport 断言）。"""
    transport = _RecorderTransport({"status": "success", "result": {"ok": True}})
    ctrl = _enabled_controller(transport)

    # get_status
    assert ctrl.get_status() == transport.response
    # control
    assert ctrl.control("enable") == transport.response
    # push_config
    assert ctrl.push_config({"x": 1}) == transport.response

    # 三次调用均命中局域网 endpoint 对应路径
    urls = [call["url"] for call in transport.calls]
    assert urls == [
        "http://remote-cxo/api/admin/status",
        "http://remote-cxo/api/admin/control",
        "http://remote-cxo/api/admin/config",
    ]


def test_disabled_behavior_unchanged_after_extension():
    """扩展后未启用（enabled=false）仍抛 RemoteDisabled，行为不变。"""
    ctrl = RemoteController(config=_FakeConfig("http://remote-cxo", False))
    with pytest.raises(RemoteDisabled):
        ctrl.get_status()