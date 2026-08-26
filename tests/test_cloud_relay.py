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