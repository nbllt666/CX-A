# -*- coding: utf-8 -*-
"""Task G1 云端中转（lite/acp/cloud_relay.py）单元测试。

覆盖（全 mock 无网络）：
- 注入 mock transport：send 回执透传、is_reachable True/False
- 缺省 urllib 传输：未配置 endpoint 抛 CloudRelayError、is_reachable 返回 False
- 缺省 urllib POST（monkeypatch urlopen）构造合法请求并解析回执 JSON
"""

import pytest

import lite.acp.cloud_relay as cr
from lite.acp.cloud_relay import CloudRelay, CloudRelayError


class _MockTransport:
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
# 1. 注入 mock transport                                             #
# ------------------------------------------------------------------ #

def test_send_passthrough_receipt():
    """mock transport 的 send 回执直通返回。"""
    t = _MockTransport(receipt={"id": "r1", "status": "ok"})
    relay = CloudRelay(endpoint="https://relay.example/x", transport=t)
    assert relay.send({"action": "message"}) == {"id": "r1", "status": "ok"}
    assert t.sent == [{"action": "message"}]


def test_is_reachable_true_with_transport():
    """"mock transport 可达返回 True。"""
    t = _MockTransport(reachable=True)
    relay = CloudRelay(endpoint="https://relay.example/x", transport=t)
    assert relay.is_reachable(timeout=5) is True


def test_is_reachable_false_with_transport():
    """mock transport 不可达返回 False。"""
    t = _MockTransport(reachable=False)
    relay = CloudRelay(endpoint="https://relay.example/x", transport=t)
    assert relay.is_reachable(timeout=5) is False


# ------------------------------------------------------------------ #
# 2. 缺省 urllib 传输（无 transport）                                  #
# ------------------------------------------------------------------ #

def test_default_transport_no_endpoint_send_raises():
    """未配置 endpoint 时缺省传输 send 抛 CloudRelayError。"""
    relay = CloudRelay(endpoint="")
    with pytest.raises(CloudRelayError):
        relay.send({"action": "message"})


def test_default_transport_no_endpoint_is_reachable_false():
    """未配置 endpoint 时缺省传输 is_reachable 返回 False（不发报文）。"""
    relay = CloudRelay(endpoint="")
    assert relay.is_reachable(timeout=1) is False


def test_default_urllib_post(monkeypatch):
    """缺省 urllib POST：monkeypatch urlopen 验证请求构造 + 回执 JSON 解析。"""
    captured = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"ok": true}'

    def _fake_urlopen(request, timeout=None):
        captured["url"] = getattr(request, "full_url", str(request))
        captured["method"] = getattr(request, "method", None)
        captured["data"] = getattr(request, "data", None)
        return _FakeResp()

    monkeypatch.setattr(cr.urllib.request, "urlopen", _fake_urlopen)

    relay = CloudRelay(endpoint="https://relay.example/relay")
    resp = relay.send({"action": "message", "request_id": "abc"})
    assert resp == {"ok": True}
    assert captured["url"] == "https://relay.example/relay"
    assert captured["method"] == "POST"
    import json as _json
    assert _json.loads(captured["data"]) == {"action": "message", "request_id": "abc"}


# ------------------------------------------------------------------ #
# 3. 第四轮体检批次C：鉴权头 / 明文 HTTP 告警 / 序列化归一               #
# ------------------------------------------------------------------ #

class _CapturingResp:
    """可捕获请求头的假响应（上下文协议 + read）。"""

    def __init__(self, captured, body=b'{"ok": true}'):
        self._captured = captured
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


def test_default_urllib_post_with_token_adds_auth_header(monkeypatch):
    """构造传入 token 时，缺省传输请求附 Authorization: Bearer <token> 头。"""
    captured = {}

    def _fake_urlopen(request, timeout=None):
        captured["headers"] = dict(getattr(request, "headers", {}) or {})
        return _CapturingResp(captured)

    monkeypatch.setattr(cr.urllib.request, "urlopen", _fake_urlopen)

    relay = CloudRelay(endpoint="https://relay.example/relay", token="sekrit")
    assert relay.send({"action": "message"}) == {"ok": True}
    assert captured["headers"]["Authorization"] == "Bearer sekrit"


def test_default_urllib_post_without_token_no_auth_header(monkeypatch):
    """未配置 token 时不带 Authorization 头（既有行为不变）。"""
    captured = {}

    def _fake_urlopen(request, timeout=None):
        captured["headers"] = dict(getattr(request, "headers", {}) or {})
        return _CapturingResp(captured)

    monkeypatch.setattr(cr.urllib.request, "urlopen", _fake_urlopen)

    relay = CloudRelay(endpoint="https://relay.example/relay")
    relay.send({"action": "message"})
    assert "Authorization" not in captured["headers"]


def test_send_non_serializable_payload_raises_cloud_relay_error():
    """不可序列化负载归一为 CloudRelayError（修复前裸 TypeError 穿透契约）。"""
    relay = CloudRelay(endpoint="https://relay.example/relay")
    with pytest.raises(CloudRelayError):
        relay.send({"bad": object()})


def test_plain_http_endpoint_warns_once(caplog, monkeypatch):
    """endpoint 为 http:// 时一次性 LOGGER.warning 明文告警（M-16 口径），https 不告警。"""
    import logging

    import urllib.error

    def _fail_urlopen(request, timeout=None):
        raise urllib.error.URLError("boom")

    monkeypatch.setattr(cr.urllib.request, "urlopen", _fail_urlopen)

    relay = CloudRelay(endpoint="http://relay.example/relay")
    with caplog.at_level(logging.WARNING, logger="lite.acp.cloud_relay"):
        with pytest.raises(CloudRelayError):
            relay.send({"action": "message"})
        with pytest.raises(CloudRelayError):
            relay.send({"action": "message"})
    assert sum("明文 HTTP" in rec.getMessage() for rec in caplog.records) == 1

    caplog.clear()
    https_relay = CloudRelay(endpoint="https://relay.example/relay")
    with caplog.at_level(logging.WARNING, logger="lite.acp.cloud_relay"):
        with pytest.raises(CloudRelayError):
            https_relay.send({"action": "message"})
    assert caplog.records == []