# -*- coding: utf-8 -*-
"""Agent API 集成测试（Task E2）——真实起服 + urllib HTTP 请求。

复用 A9 的起服模式：临时数据目录的单线程 HTTPServer，走真实 HTTP 验证
GET /api/agents、POST /api/agents、PUT /api/agents/{id}、DELETE /api/agents/{id} 全链路，
并覆盖 404（Agent 不存在）与 enabled 过滤。
保持 tests/test_api_server.py 既有用例全部不受影响。
"""

import json
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer

import pytest

from lite.server.api_server import create_app


@pytest.fixture()
def api_server(tmp_path):
    """真实起服：临时数据目录 + 单线程 HTTPServer，返回 (store, pipeline, manager, base_url)。"""
    store, pipeline, handler = create_app(data_dir=str(tmp_path))
    manager = handler._manager
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    base = f"http://127.0.0.1:{port}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield store, pipeline, manager, base
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


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


# ---------------------------------------------------------------- 种子可见
def test_list_seed(api_server):
    _store, _pipeline, _manager, base = api_server
    status, body = http_request(f"{base}/api/agents")
    assert status == 200
    assert isinstance(body, list)
    assert len(body) == 1
    assert body[0]["id"] == "default"
    assert body[0]["name"] == "软软"


# ---------------------------------------------------------------- POST 创建
def test_create_agent(api_server):
    _store, _pipeline, _manager, base = api_server
    status, agent = http_request(
        f"{base}/api/agents",
        payload={"name": "小夜", "persona": "安静的夜猫子", "voice": "miku"},
        method="POST",
    )
    assert status == 201
    assert agent["name"] == "小夜"
    assert agent["persona"] == "安静的夜猫子"
    assert agent["voice"] == "miku"
    assert agent["enabled"] is True
    assert agent["id"].startswith("agent-")

    # 列表现在有 2 个
    status, body = http_request(f"{base}/api/agents")
    assert len(body) == 2


def test_create_missing_persona_400(api_server):
    _store, _pipeline, _manager, base = api_server
    status, body = http_request(f"{base}/api/agents", payload={"name": "无 persona"}, method="POST")
    assert status == 400
    assert "persona" in body["message"]


# ---------------------------------------------------------------- PUT 更新
def test_update_agent(api_server):
    _store, _pipeline, _manager, base = api_server
    _st, created = http_request(
        f"{base}/api/agents", payload={"name": "小夜", "persona": "原始"}, method="POST"
    )
    agent_id = created["id"]
    status, updated = http_request(
        f"{base}/api/agents/{agent_id}",
        payload={"persona": "新版人设", "enabled": False},
        method="PUT",
    )
    assert status == 200
    assert updated["persona"] == "新版人设"
    assert updated["enabled"] is False
    assert updated["name"] == "小夜"  # 未改动的字段保留


def test_update_missing_404(api_server):
    _store, _pipeline, _manager, base = api_server
    status, body = http_request(
        f"{base}/api/agents/agent-none",
        payload={"name": "x"},
        method="PUT",
    )
    assert status == 404
    assert body["error"] == "not_found"


# ---------------------------------------------------------------- DELETE 删除
def test_delete_agent(api_server):
    _store, _pipeline, _manager, base = api_server
    _st, created = http_request(
        f"{base}/api/agents", payload={"name": "小夜", "persona": "……"}, method="POST"
    )
    agent_id = created["id"]

    status, body = http_request(f"{base}/api/agents/{agent_id}", method="DELETE")
    assert status == 200
    assert body == {"ok": True, "id": agent_id}

    status, body = http_request(f"{base}/api/agents")
    assert all(a["id"] != agent_id for a in body)


def test_delete_missing_404(api_server):
    _store, _pipeline, _manager, base = api_server
    status, body = http_request(f"{base}/api/agents/agent-none", method="DELETE")
    assert status == 404
    assert body["ok"] is False


# ---------------------------------------------------------------- enabled 过滤
def test_list_enabled_filter(api_server):
    _store, _pipeline, _manager, base = api_server
    _st, created = http_request(
        f"{base}/api/agents", payload={"name": "小夜", "persona": "……"}, method="POST"
    )
    agent_id = created["id"]
    http_request(f"{base}/api/agents/{agent_id}", payload={"enabled": False}, method="PUT")

    status, enabled_list = http_request(f"{base}/api/agents?enabled=true")
    assert status == 200
    assert all(a["enabled"] for a in enabled_list)
    assert all(a["id"] != agent_id for a in enabled_list)

    status, disabled_list = http_request(f"{base}/api/agents?enabled=false")
    assert status == 200
    assert [a["id"] for a in disabled_list] == [agent_id]


def test_list_bad_enabled_400(api_server):
    _store, _pipeline, _manager, base = api_server
    status, body = http_request(f"{base}/api/agents?enabled=maybe")
    assert status == 400


# ---------------------------------------------------------------- 中文 UTF-8 往返
def test_agents_chinese_utf8(api_server):
    _store, _pipeline, _manager, base = api_server
    chinese = "温柔可靠的赛博伴侣，话少但事事记在心上"
    _st, created = http_request(
        f"{base}/api/agents", payload={"name": "软软", "persona": chinese}, method="POST"
    )
    # 创建响应本身应原样返回中文
    assert created["persona"] == chinese
    status, body = http_request(f"{base}/api/agents")
    assert status == 200
    hit = next(c for c in body if c["id"] == created["id"])
    assert hit["persona"] == chinese


def test_unknown_agents_route_404(api_server):
    """若非 agents 前缀（误指向其它），应返回 404 而非误匹配。"""
    _store, _pipeline, _manager, base = api_server
    status, _body = http_request(f"{base}/api/agents/extra/seg", method="GET")
    assert status == 404