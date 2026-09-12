# -*- coding: utf-8 -*-
"""Task H1 relay API 集成测试——真实起服 + urllib HTTP 请求（复用既有起服模式）。

覆盖：
- GET  /api/cxfc/relay/pending?plugin_id=  拉取并取走待执行调用（含已取走语义）；
- POST /api/cxfc/relay/result              回报 → call 全链路 success / failure；
- 未知 request_id 回报 → 404 语义 JSON；
- 非法 body → 400；空 pending → 空列表；
- create_app 默认装配（未显式注入 cxfc）下端点可用。
"""

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import HTTPServer

import pytest

from lite.cxfc import LiteCXFC
from lite.server.api_server import build_deps, create_app, make_handler


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


#: 标准 relay 工具描述（对齐 CX-O /tools 端点字段）
_RELAY_TOOLS = [
    {
        "name": "relay_greet",
        "description": "经前端转接的问候工具",
        "parameters": {
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"}},
        },
        "returns": {"type": "object"},
    }
]


@pytest.fixture()
def relay_server(tmp_path):
    """注入已开启 relay 的 LiteCXFC（pending 队列 dispatcher）的单线程 HTTPServer。

    返回 (base, cxfc)：cxfc 供测试线程直接发起 relay call（模拟 Agent 调用方），
    与 HTTP 线程解耦——call 的阻塞等待在调用线程，HTTP 线程仅做取走/回报。
    """
    store, pipeline, manager, _ = build_deps(data_dir=str(tmp_path))
    cxfc = LiteCXFC(enabled=True, embedded_only=False, relay_timeout_s=5.0)
    cxfc.register_relay("plugin_a", _RELAY_TOOLS, dispatcher=cxfc.pending_dispatcher())
    handler = make_handler(store, pipeline, manager, cxfc=cxfc)
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    base = f"http://127.0.0.1:{port}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield base, cxfc
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _poll_pending(base, plugin_id="plugin_a", timeout=5.0):
    """轮询拉取 pending 直到取到调用或超时；返回 calls 列表。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, body = http_request(f"{base}/api/cxfc/relay/pending?plugin_id={plugin_id}")
        assert status == 200
        assert body["ok"] is True
        if body["calls"]:
            return body["calls"]
        time.sleep(0.05)
    return []


# ---------------------------------------------------------------- 全链路 success
def test_pending_then_result_full_chain(relay_server):
    """pending 拉取 → result 回报 → call 全链路 success（含已取走语义与 request_id 一致性）。"""
    base, cxfc = relay_server
    outcomes = {}

    def runner():
        outcomes["result"] = cxfc.call("relay_greet", {"name": "小明"})

    t = threading.Thread(target=runner)
    t.start()

    calls = _poll_pending(base)
    assert len(calls) == 1
    call = calls[0]
    # 投递 dict 对齐 CX-O §2.2 形状
    assert call["type"] == "cxfc_relay_call"
    assert call["plugin_id"] == "plugin_a"
    assert call["tool"] == "relay_greet"
    assert call["arguments"] == {"name": "小明"}
    assert call["request_id"]

    # 已取走语义：再次拉取为空
    status, body = http_request(f"{base}/api/cxfc/relay/pending?plugin_id=plugin_a")
    assert status == 200
    assert body["ok"] is True
    assert body["calls"] == []

    # 回报
    status, body = http_request(
        f"{base}/api/cxfc/relay/result",
        payload={
            "request_id": call["request_id"],
            "plugin_id": "plugin_a",
            "success": True,
            "result": {"greeting": "你好，小明"},
        },
        method="POST",
    )
    assert status == 200
    assert body == {"status": "ok"}

    t.join(timeout=5)
    assert not t.is_alive()
    result = outcomes["result"]
    assert result["success"] is True
    assert result["result"] == {"greeting": "你好，小明"}
    assert result["request_id"] == call["request_id"]


# ---------------------------------------------------------------- 失败回报链路
def test_failure_result_propagates_error(relay_server):
    """回报 success=False 时 call 返回 success=False 且 error 随回报内容。"""
    base, cxfc = relay_server
    outcomes = {}
    t = threading.Thread(target=lambda: outcomes.setdefault("r", cxfc.call("relay_greet", {})))
    t.start()
    calls = _poll_pending(base)
    assert len(calls) == 1

    status, body = http_request(
        f"{base}/api/cxfc/relay/result",
        payload={
            "request_id": calls[0]["request_id"],
            "plugin_id": "plugin_a",
            "success": False,
            "error": "前端执行失败",
        },
        method="POST",
    )
    assert status == 200
    assert body == {"status": "ok"}

    t.join(timeout=5)
    result = outcomes["r"]
    assert result["success"] is False
    assert result["error"] == "前端执行失败"
    assert result["request_id"] == calls[0]["request_id"]


# ---------------------------------------------------------------- 异常语义
def test_result_unknown_request_id_404(relay_server):
    """未知 request_id 回报 → 404 语义 JSON（ok:false + not_found）。"""
    base, _cxfc = relay_server
    status, body = http_request(
        f"{base}/api/cxfc/relay/result",
        payload={
            "request_id": "does-not-exist",
            "plugin_id": "plugin_a",
            "success": True,
            "result": {},
        },
        method="POST",
    )
    assert status == 404
    assert body["ok"] is False
    assert body["error"] == "not_found"


def test_result_bad_body_400(relay_server):
    """request_id 缺失 / success 非布尔 → 400 bad_request。"""
    base, _cxfc = relay_server
    # request_id 缺失
    status, body = http_request(
        f"{base}/api/cxfc/relay/result", payload={"success": True}, method="POST"
    )
    assert status == 400
    assert body["error"] == "bad_request"
    # success 非布尔
    status, body = http_request(
        f"{base}/api/cxfc/relay/result",
        payload={"request_id": "x", "success": "yes"},
        method="POST",
    )
    assert status == 400
    assert body["error"] == "bad_request"


def test_result_duplicate_fulfill_404(relay_server):
    """同一 request_id 重复回报：首次 ok，第二次 404（等待者已被取走）。"""
    base, cxfc = relay_server
    outcomes = {}
    t = threading.Thread(target=lambda: outcomes.setdefault("r", cxfc.call("relay_greet", {})))
    t.start()
    calls = _poll_pending(base)
    rid = calls[0]["request_id"]

    status, body = http_request(
        f"{base}/api/cxfc/relay/result",
        payload={"request_id": rid, "plugin_id": "plugin_a", "success": True, "result": {"v": 1}},
        method="POST",
    )
    assert status == 200 and body == {"status": "ok"}

    status, body = http_request(
        f"{base}/api/cxfc/relay/result",
        payload={"request_id": rid, "plugin_id": "plugin_a", "success": True, "result": {"v": 2}},
        method="POST",
    )
    assert status == 404
    assert body["ok"] is False

    t.join(timeout=5)
    assert outcomes["r"]["result"] == {"v": 1}  # 首次回报生效


# ---------------------------------------------------------------- pending 空态
def test_pending_empty_without_calls(relay_server):
    """无待执行调用时 pending 返回空列表。"""
    base, _cxfc = relay_server
    status, body = http_request(f"{base}/api/cxfc/relay/pending")
    assert status == 200
    assert body["ok"] is True
    assert body["calls"] == []
    assert body["count"] == 0


def test_pending_plugin_filter_isolates_targets(relay_server):
    """plugin_id 过滤隔离：仅取走匹配目标的调用，其它目标调用保留。"""
    base, cxfc = relay_server
    cxfc.register_relay(
        "plugin_b", [{"name": "relay_other"}], dispatcher=cxfc.pending_dispatcher()
    )
    outcomes = {}

    def runner_a():
        outcomes["a"] = cxfc.call("relay_greet", {})

    def runner_b():
        outcomes["b"] = cxfc.call("relay_other", {})

    threads = [threading.Thread(target=runner_a), threading.Thread(target=runner_b)]
    for t in threads:
        t.start()

    # 先取走 plugin_a 的调用（plugin_b 的保留）
    deadline = time.monotonic() + 5
    calls_a = []
    while time.monotonic() < deadline:
        calls_a = _poll_pending(base, plugin_id="plugin_a", timeout=0.5)
        if calls_a:
            break
    assert len(calls_a) == 1
    assert calls_a[0]["plugin_id"] == "plugin_a"

    # plugin_b 的调用仍在队列
    status, body = http_request(f"{base}/api/cxfc/relay/pending?plugin_id=plugin_b")
    assert status == 200
    assert body["count"] == 1
    assert body["calls"][0]["plugin_id"] == "plugin_b"

    # 回报 plugin_a → 成功收场
    status, _body = http_request(
        f"{base}/api/cxfc/relay/result",
        payload={
            "request_id": calls_a[0]["request_id"],
            "plugin_id": "plugin_a",
            "success": True,
            "result": {"done": True},
        },
        method="POST",
    )
    assert status == 200
    threads[0].join(timeout=5)
    threads[1].join(timeout=5)
    assert outcomes["a"]["success"] is True
    assert outcomes["a"]["result"] == {"done": True}


# ---------------------------------------------------------------- 默认装配可用性
def test_default_assembly_pending_endpoint_works(tmp_path):
    """create_app 默认装配（未显式注入 cxfc，config 默认 enabled=False）下 pending 端点可用。"""
    _store, _pipeline, handler = create_app(data_dir=str(tmp_path))
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    base = f"http://127.0.0.1:{port}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = http_request(f"{base}/api/cxfc/relay/pending")
        assert status == 200
        assert body["ok"] is True
        assert body["calls"] == []
        # 未知 request_id 回报同样得到 404 语义
        status, body = http_request(
            f"{base}/api/cxfc/relay/result",
            payload={"request_id": "nope", "success": True, "result": {}},
            method="POST",
        )
        assert status == 404
        assert body["ok"] is False
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
