# -*- coding: utf-8 -*-
"""API 服务集成测试（A9）——真实起服 + urllib HTTP 请求。

确实启动单线程 HTTPServer 于临时数据目录，用 urllib.request 走真实 HTTP 访问各端点：
health、空列表、add 后列表可查、search 可返回、delete 后列表不含、中文 UTF-8 往返。
同时覆盖 404 / 400 等边界路由。
"""

import base64
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import HTTPServer
from pathlib import Path
from urllib.parse import urlencode

import pytest

from lite.computer_control import ComputerControl, ToolBridge
from lite.computer_control.security import ControlAuthorizer
from lite.config.config_manager import ConfigManager
from lite.runtime.download_manager import ModelDownloadManager
import lite.server.api_server as api_server_module
from lite.server.api_server import build_deps, create_app, make_handler


@pytest.fixture()
def computer_env(tmp_path):
    """L3 测试用电脑控制依赖：fake 键盘后端 + authorizer + bridge 起服，返回 (base, authorizer, keyboard)。"""

    class _FakeKeyboard:
        def __init__(self):
            self.clicks = []

        def click(self, x, y):
            self.clicks.append((x, y))

        def type_text(self, text):
            pass

    store, pipeline, manager, _remote = build_deps(data_dir=str(tmp_path))
    authorizer = ControlAuthorizer(data_dir=str(tmp_path))
    keyboard = _FakeKeyboard()
    computer = ComputerControl(authorized=authorizer.is_authorized(), keyboard_backend=keyboard)
    bridge = ToolBridge(computer=computer, authorizer=authorizer)
    handler = make_handler(
        store, pipeline, manager,
        computer=computer, authorizer=authorizer, bridge=bridge,
    )
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    base_url = f"http://127.0.0.1:{port}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield base_url, authorizer, keyboard
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def api_server(tmp_path):
    """真实起服：临时数据目录 + 单线程 HTTPServer，返回 (store, pipeline, base_url)。"""
    store, pipeline, handler = create_app(data_dir=str(tmp_path))
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    base = f"http://127.0.0.1:{port}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield store, pipeline, base
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def http_get(url):
    """GET 请求返回 (status, JSON, raw_text)。"""
    with urllib.request.urlopen(url, timeout=5) as resp:
        raw = resp.read().decode("utf-8")
        return resp.status, json.loads(raw), raw


def http_delete(url):
    """DELETE 请求返回 (status, JSON)，非 2xx 同样解析错误体返回。"""
    req = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def http_status(url):
    """仅返回状态码（用于 404 断言，uurlopen 对非 2xx 抛 HTTPError）。"""
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def http_post(url, payload, method="POST"):
    """POST/PUT 请求返回 (status, JSON)，非 2xx 同样解析错误体返回。"""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def search_url(base, **params):
    """构造 search 端点 URL，查询参数做 URL 编码（支持中文 query）。"""
    querystring = urlencode({k: str(v) for k, v in params.items() if v is not None})
    return f"{base}/api/memories/search?{querystring}"


# ---------------------------------------------------------------- health
def test_health_ok(api_server):
    _store, _pipeline, base = api_server
    status, body, _raw = http_get(f"{base}/api/health")
    assert status == 200
    assert body == {"status": "ok"}


# ---------------------------------------------------------------- 空列表
def test_empty_list(api_server):
    _store, _pipeline, base = api_server
    status, body, _raw = http_get(f"{base}/api/memories")
    assert status == 200
    assert body == []


# ---------------------------------------------------------------- add 后列表可查
def test_add_then_list_contains(api_server):
    _store, pipeline, base = api_server
    mem_id = pipeline.add("用户偏好冷萃咖啡，半糖")
    status, body, _raw = http_get(f"{base}/api/memories")
    assert status == 200
    ids = [m["id"] for m in body]
    assert mem_id in ids
    hit = next(m for m in body if m["id"] == mem_id)
    assert hit["content"] == "用户偏好冷萃咖啡，半糖"
    assert hit["agent_id"] == "default"


# ---------------------------------------------------------------- search 可返回
def test_search_returns_memories(api_server):
    _store, pipeline, base = api_server
    pipeline.add("用户昨晚说起想去山顶看流星雨")
    pipeline.add("今天天气很好适合散步")
    status, body, _raw = http_get(search_url(base, q="流星雨", top_k=5))
    assert status == 200
    assert isinstance(body.get("memories"), list)
    assert len(body["memories"]) >= 1
    assert "流星雨" in body["memories"][0]["content"]
    assert body.get("context_text")


def test_search_empty_query(api_server):
    _store, _pipeline, base = api_server
    status, body, _raw = http_get(f"{base}/api/memories/search?q=")
    assert status == 200
    assert body["memories"] == []
    assert body["context_text"] == "【回忆】"


# ---------------------------------------------------------------- delete 后列表不含
def test_delete_removes_from_list(api_server):
    _store, pipeline, base = api_server
    mem_id = pipeline.add("待删除的记忆内容")
    status, body, _raw = http_get(f"{base}/api/memories")
    assert mem_id in [m["id"] for m in body]

    status, body = http_delete(f"{base}/api/memories/{mem_id}")
    assert status == 200
    assert body == {"ok": True, "id": mem_id}

    status, body, _raw = http_get(f"{base}/api/memories")
    assert mem_id not in [m["id"] for m in body]
    # 软删除后原记录仍可查（区分软删）
    assert _store.get(mem_id)["is_deleted"] == 1


def test_delete_missing_returns_404(api_server):
    _store, _pipeline, base = api_server
    status, body = http_delete(f"{base}/api/memories/99999")
    assert status == 404
    assert body["ok"] is False


# ---------------------------------------------------------------- 中文 UTF-8 往返
def test_chinese_utf8_roundtrip(api_server):
    _store, pipeline, base = api_server
    chinese = "记住：她最喜欢春天的樱花和夏天的麦田—中文内容"
    pipeline.add(chinese)
    status, body, raw = http_get(f"{base}/api/memories")
    assert status == 200
    # raw 响应体中应直接包含未经 \u 转义的中文原文
    assert chinese in raw
    assert chinese in body[0]["content"]


# ---------------------------------------------------------------- 边界路由
def test_unknown_route_returns_404(api_server):
    _store, _pipeline, base = api_server
    assert http_status(f"{base}/api/not-exist") == 404


def test_bad_limit_returns_400(api_server):
    _store, _pipeline, base = api_server
    assert http_status(f"{base}/api/memories?limit=abc") == 400


# ---------------------------------------------------------------- 状态 / 设置 / 聊天守卫（管理面收敛为纯 API）
def test_status_endpoint(api_server):
    """GET /api/status 返回轻量系统状态。"""
    _store, _pipeline, base = api_server
    status, body, _raw = http_get(f"{base}/api/status")
    assert status == 200
    assert body["status"] == "ok"
    assert body["app"] == "CX-A/CX-Lite"
    assert "uptime_seconds" in body
    assert body["companion"] is True


def test_settings_get_masked_without_api_key(api_server):
    """GET /api/settings 返回脱敏配置视图，绝不包含 API Key。"""
    _store, _pipeline, base = api_server
    status, body, raw = http_get(f"{base}/api/settings")
    assert status == 200
    assert body["cloud"]["provider"] == "deepseek"
    assert body["tts"]["voice"] == "cx-open"
    assert body["local_llm"]["enabled"] is False
    assert "api_key" not in raw.lower()
    assert "api_key" not in json.dumps(body)


def test_settings_update_hot_reload(api_server):
    """PUT /api/settings 应用白名单键并热更新，再次 GET 可见。"""
    _store, _pipeline, base = api_server
    status, body = http_post(
        f"{base}/api/settings",
        {"cloud": {"provider": "tongyi"}, "tts": {"voice": "ling"}},
        method="PUT",
    )
    assert status == 200
    assert "cloud.provider" in body["applied"]
    assert "tts.voice" in body["applied"]
    assert body["config"]["cloud"]["provider"] == "tongyi"
    assert body["config"]["tts"]["voice"] == "ling"

    _st2, body2, _raw = http_get(f"{base}/api/settings")
    assert body2["cloud"]["provider"] == "tongyi"
    assert body2["tts"]["voice"] == "ling"


def test_settings_update_ignores_unknown_provider(api_server):
    """PUT provider 不在白名单时 ignored 且不覆盖原值。"""
    _store, _pipeline, base = api_server
    status, body = http_post(
        f"{base}/api/settings",
        {"cloud": {"provider": "bogus"}},
        method="PUT",
    )
    assert status == 200
    assert body["applied"] == []
    assert any("bogus" in item for item in body["ignored"])
    assert body["config"]["cloud"]["provider"] == "deepseek"


def test_chat_endpoints_guard(api_server):
    """聊天端点本期为未启用守卫：明确提示而不 404，避免直连误判。"""
    _store, _pipeline, base = api_server
    status, body = http_post(f"{base}/api/chat/messages", {"text": "hi"})
    assert status == 200
    assert body["error"] == "chat_service_disabled"
    assert body["ok"] is False

    status2, body2, _raw = http_get(f"{base}/api/chat/history")
    assert status2 == 200
    assert body2["error"] == "chat_service_disabled"
    assert body2["messages"] == []


# ---------------------------------------------------------------- 安全加固（20260827_模块0_API服务安全加固）
def raw_request(port, method, path, host=None):
    """发送裸 HTTP 请求（绕过 urllib 自动补头），返回完整响应原文。

    仅用于 Host 伪造场景：urllib 无法可靠覆盖自动生成的 Host 头，
    故用 socket 直连以精确控制请求行与头字段。
    """
    headers = {
        "Host": host if host is not None else f"127.0.0.1:{port}",
        "Connection": "close",
    }
    head = "\r\n".join([f"{method} {path} HTTP/1.0"] + [f"{k}: {v}" for k, v in headers.items()])
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall((head + "\r\n\r\n").encode("ascii"))
        chunks = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


def test_options_preflight_204_with_cors_headers(api_server):
    """do_OPTIONS：允许源预检返回 204，并附带放行 CORS 头集合。"""
    _store, _pipeline, base = api_server
    req = urllib.request.Request(
        f"{base}/api/health",
        method="OPTIONS",
        headers={"Origin": "http://localhost:5173"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 204
        assert resp.read() == b""  # 空 body（Content-Length: 0）
        assert resp.headers.get("Access-Control-Allow-Origin") == "http://localhost:5173"
        assert resp.headers.get("Access-Control-Allow-Methods") == "GET, POST, PUT, DELETE, OPTIONS"
        # 中-3（第四轮体检批次B）：预检允许头补 X-Client-Token，与令牌闸一致
        assert resp.headers.get("Access-Control-Allow-Headers") == "Content-Type, X-Client-Token"


def test_cors_headers_echo_allowlisted_origin_only(api_server):
    """JSON 响应仅对白名单 Origin 回显 ACAO；非白名单 Origin 不带任何 Access-Control 头。"""
    _store, _pipeline, base = api_server
    req = urllib.request.Request(
        f"{base}/api/health", headers={"Origin": "http://127.0.0.1:5173"}
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        assert resp.headers.get("Access-Control-Allow-Origin") == "http://127.0.0.1:5173"

    req2 = urllib.request.Request(
        f"{base}/api/health", headers={"Origin": "http://evil.example.com"}
    )
    with urllib.request.urlopen(req2, timeout=5) as resp2:
        assert resp2.status == 200
        assert resp2.headers.get("Access-Control-Allow-Origin") is None


def test_bad_host_returns_403(api_server):
    """Host 不指向本服务（DNS rebinding 形态）→ 403 结构化响应。"""
    _store, _pipeline, base = api_server
    port = int(base.rsplit(":", 1)[1])
    raw = raw_request(port, "GET", "/api/health", host="evil.example.com:8600")
    first_line = raw.split("\r\n", 1)[0]
    assert first_line.startswith("HTTP/")
    assert "403" in first_line


def test_settings_invalid_section_type_400(api_server):
    """PUT /api/settings 段存在但非 dict（{"cloud": "x"}）→ 400 结构化错误而非 AttributeError 冒泡。"""
    _store, _pipeline, base = api_server
    status, body = http_post(f"{base}/api/settings", {"cloud": "x"}, method="PUT")
    assert status == 400
    assert body["error"] == "invalid section type: cloud"


def test_delete_memory_huge_id_returns_400(api_server):
    """低-6：超出 64 位整数范围的记忆 id 边界处显式 400，不再触发 OverflowError → 500。"""
    _store, _pipeline, base = api_server
    huge_id = "9" * 30
    status, body = http_delete(f"{base}/api/memories/{huge_id}")
    assert status == 400
    assert body["ok"] is False
    assert body["error"] == "bad_request"


def test_delete_memory_int64_boundary_ids(api_server):
    """低-6 边界：int64 上界（合法但不存在）→ 404；上界+1 与下界-1（超界）→ 400。"""
    _store, _pipeline, base = api_server
    status, _body = http_delete(f"{base}/api/memories/9223372036854775807")
    assert status == 404
    status2, body2 = http_delete(f"{base}/api/memories/9223372036854775808")
    assert status2 == 400
    assert body2["error"] == "bad_request"
    status3, _body3 = http_delete(f"{base}/api/memories/-9223372036854775809")
    assert status3 == 400


def test_post_wrong_content_type_400(api_server):
    """POST 带 body 但 Content-Type 非 application/json（urllib 默认表单类型）→ 400。"""
    _store, _pipeline, base = api_server
    req = urllib.request.Request(
        f"{base}/api/computer/authorize",
        data=json.dumps({"enabled": True}).encode("utf-8"),
        method="POST",  # 不设置 Content-Type，urllib 自动补 application/x-www-form-urlencoded
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    assert status == 400


# ---------------------------------------------------------------- M5 config.memory 装配接线
def test_create_app_wires_memory_config(tmp_path):
    """create_app 按 data_dir 下 config.json 的 memory 段装配 pipeline/manager 行为。"""
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    cfg = {"memory": {"max_memories": 2, "dedup": 0.5, "permanent_threshold": 0.6}}
    (cfg_dir / "config.json").write_text(json.dumps(cfg), encoding="utf-8")

    store, pipeline, _handler = create_app(data_dir=str(cfg_dir))

    # 配置注入生效（非默认值 30/0.85/0.95）
    assert pipeline.max_memories == 2
    assert pipeline.manager.dedup_threshold == pytest.approx(0.5)
    assert pipeline.manager._permanent_threshold == pytest.approx(0.6)

    # 行为随之变化（dedup=0.5）：两句 Jaccard≈4/6≈0.667，默认阈值下各自入库，
    # 自定义阈值下第二条在写入口即被去重
    first = pipeline.add("alpha beta gamma delta epsilon")
    second = pipeline.add("alpha beta gamma delta zeta")
    assert first is not None
    assert second is None

    # 行为随之变化（permanent_threshold=0.6）：importance_score=0.62 的记忆可直晋永久
    mid = store.add({"type": "long_term", "content": "custom permanent wiring check"})
    store.update(mid, {"importance_score": 0.62})
    promoted = pipeline.manager.promote(mid)
    assert promoted["type"] == "permanent"


def test_create_app_default_memory_config_unchanged(tmp_path):
    """config.json 未提供 memory 段时装配结果保持默认行为（30/0.85/0.95）。"""
    store, pipeline, _handler = create_app(data_dir=str(tmp_path))
    assert pipeline.max_memories == 30
    assert pipeline.manager.dedup_threshold == pytest.approx(0.85)
    assert pipeline.manager._permanent_threshold == pytest.approx(0.95)
    assert len(store.list()) == 0


# ---------------------------------------------------------------- L2/L3 收口与外壳统一（20260827_模块0_低危清理_服务通信侧）
def http_raw_body(url, raw, method="POST", content_type="application/json"):
    """发送任意原始 body（协议边界场景：malformed JSON 等），返回 (status, JSON)。"""
    headers = {"Content-Type": content_type} if content_type else {}
    req = urllib.request.Request(url, data=raw, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_settings_malformed_json_returns_bad_json(api_server):
    """L2：settings PUT malformed JSON → 400 bad_json（不再吞错按空补丁处理）。"""
    _store, _pipeline, base = api_server
    status, body = http_raw_body(f"{base}/api/settings", b"{not-valid-json", method="PUT")
    assert status == 400
    assert body["error"] == "bad_json"
    assert body["ok"] is False


def test_settings_non_dict_json_returns_bad_json(api_server):
    """L2：合法 JSON 但非 dict（数组）→ 视为结构不符的坏请求 400 bad_json。"""
    _store, _pipeline, base = api_server
    status, body = http_raw_body(f"{base}/api/settings", b"[1,2,3]", method="PUT")
    assert status == 400
    assert body["error"] == "bad_json"


def test_settings_empty_body_returns_empty_body_error(api_server):
    """L2：settings PUT 空 body {} 与 malformed 区分 → 400 empty_body。"""
    _store, _pipeline, base = api_server
    status, body = http_post(f"{base}/api/settings", {}, method="PUT")
    assert status == 400
    assert body["error"] == "empty_body"
    assert body["ok"] is False


def test_other_endpoints_empty_body_keep_semantics(api_server):
    """L2：其余端点空 body 维持既有语义（agents create 走必填字段校验而非 bad_json）。"""
    _store, _pipeline, base = api_server
    # 空 dict 补丁对 agents/create：既非 None 也非非法 -> 走 name/persona 必填校验
    status, body = http_post(f"{base}/api/agents", {})
    assert status == 400
    assert body["error"] == "bad_request"
    assert "persona" in body["message"]


def test_agents_update_malformed_json_returns_bad_json(api_server):
    """L2：agents update malformed JSON → 400 bad_json。"""
    _store, _pipeline, base = api_server
    status, body = http_raw_body(f"{base}/api/agents/default", b"@@", method="PUT")
    assert status == 400
    assert body["error"] == "bad_json"


def test_settings_readonly_known_keys_echo_in_ignored(api_server):
    """L2：GET 可见但白名单只读键（acp/remote section、cloud.base_url）进 ignored 回显。"""
    _store, _pipeline, base = api_server
    status, body = http_post(
        f"{base}/api/settings",
        {
            "cloud": {"provider": "openai", "base_url": "https://mirror.example.com/v1"},
            "acp": {"enabled": True},
            "remote": {"enabled": True},
            "vector": {"dim": 64},
        },
        method="PUT",
    )
    assert status == 200
    assert body["ok"] is True
    assert body["applied"] == ["cloud.provider"]
    ignored_text = "\n".join(body["ignored"])
    assert "acp" in ignored_text
    assert "remote" in ignored_text
    assert "vector" in ignored_text
    assert "cloud.base_url" in ignored_text
    # provider 本身仍正常生效
    assert body["config"]["cloud"]["provider"] == "openai"


def test_settings_put_response_ok_true_and_applied(api_server):
    """L3 外壳统一：settings PUT 正常响应带 ok:true 与 applied 数组。"""
    _store, _pipeline, base = api_server
    status, body = http_post(f"{base}/api/settings", {"tts": {"voice": "ling"}}, method="PUT")
    assert status == 200
    assert body["ok"] is True
    assert body["applied"] == ["tts.voice"]


def test_computer_call_plugin_error_without_authorized_field(computer_env):
    """L3：非授权类 PluginError 不误标 authorized 字段（键不存在）。"""
    base, authorizer, _kb = computer_env
    http_post(f"{base}/api/computer/authorize", payload={"enabled": True}, method="POST")

    status, body = http_post(
        f"{base}/api/computer/call",
        payload={"tool": "no_such_tool", "arguments": {}},
        method="POST",
    )
    assert status == 400
    assert body["ok"] is False
    assert body["error_code"] == "INVALID_ARGUMENT"
    assert "authorized" not in body  # 非授权类错误不得携带该字段


def test_computer_call_unauthorized_keeps_authorized_false(computer_env):
    """L3：NotAuthorizedError 分支仍显式携带 authorized:false（语义保留）。"""
    base, _authorizer, _kb = computer_env
    status, body = http_post(
        f"{base}/api/computer/call",
        payload={"tool": "computer_keyboard_control", "arguments": {}},
        method="POST",
    )
    assert status == 403
    assert body["authorized"] is False
    assert body["ok"] is False
    assert body["error_code"] == "NOT_AUTHORIZED"


def test_computer_authorize_success_ok_true(computer_env):
    """L3 外壳统一：authorize 成功响应增量补 ok:true。"""
    base, _authorizer, _kb = computer_env
    status, body = http_post(
        f"{base}/api/computer/authorize", payload={"enabled": True}, method="POST"
    )
    assert status == 200
    assert body["ok"] is True
    assert body["authorized"] is True


# ---------------------------------------------------------------- 启动令牌鉴权与请求体上限（20260828_模块0_API鉴权与安全链路修复·批次A）
def http_get_json(url, headers=None):
    """GET 请求返回 (status, JSON)；非 2xx 解析错误体返回（token 场景用）。"""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_token_open_mode_allows_requests(api_server, monkeypatch):
    """N1 开放模式：_API_TOKEN 为空（env CXA_API_TOKEN 未设置）时请求正常放行。"""
    monkeypatch.setattr(api_server_module, "_API_TOKEN", "")
    monkeypatch.setattr(api_server_module, "_TOKEN_OPEN_MODE_WARNED", True)
    _store, _pipeline, base = api_server
    status, body, _raw = http_get(f"{base}/api/memories")
    assert status == 200
    assert body == []


def test_token_mode_missing_header_403(api_server, monkeypatch):
    """N1 令牌模式：无 X-Client-Token 头（如恶意 sandboxed iframe）→ 403 unauthorized_client。"""
    monkeypatch.setattr(api_server_module, "_API_TOKEN", "unit-test-token")
    _store, _pipeline, base = api_server
    status, body = http_get_json(f"{base}/api/memories")
    assert status == 403
    assert body == {"ok": False, "error": "unauthorized_client"}


def test_token_mode_wrong_header_403(api_server, monkeypatch):
    """N1 令牌模式：令牌不匹配 → 403 unauthorized_client。"""
    monkeypatch.setattr(api_server_module, "_API_TOKEN", "unit-test-token")
    _store, _pipeline, base = api_server
    status, body = http_get_json(f"{base}/api/memories", headers={"X-Client-Token": "wrong-token"})
    assert status == 403
    assert body["error"] == "unauthorized_client"


def test_token_mode_correct_header_200(api_server, monkeypatch):
    """N1 令牌模式：令牌匹配 → 200 正常业务响应。"""
    monkeypatch.setattr(api_server_module, "_API_TOKEN", "unit-test-token")
    _store, _pipeline, base = api_server
    status, body = http_get_json(f"{base}/api/memories", headers={"X-Client-Token": "unit-test-token"})
    assert status == 200
    assert body == []


def test_token_mode_post_also_guarded(api_server, monkeypatch):
    """N1：POST 同样过令牌闸（对头放行业务、无头 403），非仅 GET。"""
    monkeypatch.setattr(api_server_module, "_API_TOKEN", "unit-test-token")
    _store, _pipeline, base = api_server

    status, body = http_post(f"{base}/api/computer/authorize", payload={"enabled": True})
    assert status == 403
    assert body["error"] == "unauthorized_client"

    req = urllib.request.Request(
        f"{base}/api/computer/authorize",
        data=json.dumps({"enabled": True}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Client-Token": "unit-test-token"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200


def test_token_mode_health_exempt(api_server, monkeypatch):
    """N1：GET /api/health 豁免令牌校验（健康探测不带自定义头也能探活）。"""
    monkeypatch.setattr(api_server_module, "_API_TOKEN", "unit-test-token")
    _store, _pipeline, base = api_server
    status, body = http_get_json(f"{base}/api/health")
    assert status == 200
    assert body == {"status": "ok"}


def test_token_mode_options_exempt(api_server, monkeypatch):
    """N1：OPTIONS 预检豁免令牌校验（浏览器预检不携带自定义头）。"""
    monkeypatch.setattr(api_server_module, "_API_TOKEN", "unit-test-token")
    _store, _pipeline, base = api_server
    req = urllib.request.Request(f"{base}/api/memories", method="OPTIONS")
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 204


def test_payload_too_large_413(api_server):
    """N6：请求体超过 1MB 上限 → 413 payload_too_large，不进入业务处理。"""
    _store, _pipeline, base = api_server
    big = b'{"pad": "' + b"A" * 1048577 + b'"}'
    status, body = http_raw_body(f"{base}/api/settings", big, method="PUT")
    assert status == 413
    assert body["ok"] is False
    assert body["error"] == "payload_too_large"


# ---------------------------------------------------------------- 生产装配接线（20260828_模块0_生产装配接线·批次E）
def test_tools_list_contains_usage(api_server):
    """GET /api/tools：工具清单非空（system_info/memory_* 恒注册）且附 usage 端点自述。"""
    _store, _pipeline, base = api_server
    status, body, _raw = http_get(f"{base}/api/tools")
    assert status == 200
    assert body["ok"] is True
    assert len(body["tools"]) >= 3
    ids = {t["id"] for t in body["tools"]}
    assert {"system_info", "memory_write", "memory_search"} <= ids
    # 按 registry 实际数据结构如实映射
    sample = body["tools"][0]
    assert {"id", "name", "description", "source", "category", "enabled"} <= set(sample)
    usage = body["usage"]
    for key in ("POST /api/tools/call", "POST /api/memory/distill",
                "POST /api/voice/synthesize", "POST /api/voice/transcribe"):
        assert key in usage


def test_tools_call_system_info_success(api_server):
    """POST /api/tools/call：调用无害工具 system_info（category=time）返回真实结果。"""
    _store, _pipeline, base = api_server
    status, body = http_post(
        f"{base}/api/tools/call", {"name": "system_info", "arguments": {"category": "time"}}
    )
    assert status == 200
    assert body["ok"] is True
    assert body["result"]["category"] == "time"
    assert body["result"]["iso"]
    assert body["result"]["timestamp"] > 0


def test_tools_call_unknown_tool_404(api_server):
    """POST /api/tools/call：未名工具 → 404 not_found。"""
    _store, _pipeline, base = api_server
    status, body = http_post(f"{base}/api/tools/call", {"name": "no_such_tool", "arguments": {}})
    assert status == 404
    assert body["ok"] is False
    assert body["error"] == "not_found"


def test_memory_distill_cloud_not_configured_400(api_server):
    """POST /api/memory/distill：未配置云端（api_key 为空）→ 400 cloud_not_configured。"""
    _store, _pipeline, base = api_server
    status, body = http_post(
        f"{base}/api/memory/distill", {"messages": [{"role": "user", "content": "记住我喜欢冷萃"}]}
    )
    assert status == 400
    assert body["error"] == "cloud_not_configured"
    assert body["ok"] is False


def test_memory_distill_bad_messages_400(api_server):
    """POST /api/memory/distill：messages 为空列表 / 非列表 / 缺 role-content → 400。"""
    _store, _pipeline, base = api_server
    for payload in ({}, {"messages": []}, {"messages": "not-a-list"},
                    {"messages": [{"role": "user"}]}, {"messages": [{"content": "x"}]}):
        status, body = http_post(f"{base}/api/memory/distill", payload)
        assert status == 400, f"payload={payload!r} 应 400"
        assert body["error"] == "bad_request"


def test_voice_synthesize_returns_base64_audio(api_server):
    """POST /api/voice/synthesize：MockTTS 也应产出非空字节 → 200 + base64 wav。"""
    _store, _pipeline, base = api_server
    status, body = http_post(f"{base}/api/voice/synthesize", {"text": "你好，世界"})
    assert status == 200
    assert body["ok"] is True
    assert body["mime"] == "audio/wav"
    audio = base64.b64decode(body["audio_base64"])
    assert len(audio) > 0


def test_voice_synthesize_empty_text_400(api_server):
    """POST /api/voice/synthesize：text 缺失 / 空白 → 400 bad_request。"""
    _store, _pipeline, base = api_server
    for payload in ({}, {"text": ""}, {"text": "   "}):
        status, body = http_post(f"{base}/api/voice/synthesize", payload)
        assert status == 400
        assert body["error"] == "bad_request"


def test_voice_transcribe_returns_text(api_server):
    """POST /api/voice/transcribe：MockASR 返回占位文本 → 200 {ok:true, text}。"""
    _store, _pipeline, base = api_server
    audio_b64 = base64.b64encode(b"\x00\x01" * 64).decode("ascii")
    status, body = http_post(
        f"{base}/api/voice/transcribe", {"audio_base64": audio_b64, "sample_rate": 16000}
    )
    assert status == 200
    assert body["ok"] is True
    assert isinstance(body["text"], str)


def test_voice_transcribe_bad_base64_400(api_server):
    """POST /api/voice/transcribe：非法 base64 / 缺字段 → 400 bad_request。"""
    _store, _pipeline, base = api_server
    status, body = http_post(f"{base}/api/voice/transcribe", {"audio_base64": "@@not-base64@@"})
    assert status == 400
    assert body["error"] == "bad_request"
    status2, body2 = http_post(f"{base}/api/voice/transcribe", {})
    assert status2 == 400
    assert body2["error"] == "bad_request"


def test_new_endpoints_require_token_in_token_mode(api_server, monkeypatch):
    """批次E：令牌模式下五个新端点无令牌 → 403 unauthorized_client（自动继承令牌闸）。"""
    monkeypatch.setattr(api_server_module, "_API_TOKEN", "unit-test-token")
    _store, _pipeline, base = api_server
    # GET /api/tools
    status, body = http_get_json(f"{base}/api/tools")
    assert status == 403
    assert body["error"] == "unauthorized_client"
    # POST 四端点
    for path, payload in [
        ("/api/tools/call", {"name": "system_info"}),
        ("/api/memory/distill", {"messages": [{"role": "user", "content": "hi"}]}),
        ("/api/voice/synthesize", {"text": "hi"}),
        ("/api/voice/transcribe", {"audio_base64": "AAAA"}),
    ]:
        status, body = http_post(f"{base}{path}", payload)
        assert status == 403, f"{path} 无令牌应 403"
        assert body["error"] == "unauthorized_client"


def test_create_app_wires_runtime_deps(tmp_path):
    """create_app 生产装配：voice/registry/distiller 均已构造且同源共享 store 上下文。"""
    store, pipeline, handler = create_app(data_dir=str(tmp_path))
    # handler 类属性绑定（批次E 全量注入，非 None 回落）
    assert handler._voice is not None
    assert handler._registry is not None
    assert handler._distiller is not None
    # 同源性：registry / distiller 与 create_app 产物共享同一 store / pipeline
    assert handler._registry.memory_store is store
    assert handler._registry.pipeline is pipeline
    assert handler._registry.manager is pipeline.manager
    assert handler._distiller._store is store
    assert handler._distiller._manager is pipeline.manager
    # voice 三件套就位（Mock 兜底装配零失败）
    assert handler._voice.asr is not None
    assert handler._voice.tts is not None


# ---------------------------------------------------------------- 第三轮体检批次2：输入加固


def test_limit_negative_rejected(api_server):
    """M-6：limit=-1 回 400（SQLite LIMIT -1 语义为无限制，与意图相反）。"""
    _store, _pipeline, base = api_server
    try:
        status, payload, _raw = http_get(f"{base}/api/memories?" + urlencode({"limit": "-1"}))
    except urllib.error.HTTPError as exc:
        status, payload = exc.code, json.loads(exc.read().decode("utf-8"))
    assert status == 400
    assert payload["error"] == "bad_request"


def test_limit_above_cap_clamped(api_server):
    """M-6：limit 超上限被钳制到 1000，正常返回 200 而非 500。"""
    _store, _pipeline, base = api_server
    status, _payload, _raw = http_get(
        f"{base}/api/memories?" + urlencode({"limit": "99999999999999999999"})
    )
    assert status == 200


def test_top_k_negative_rejected(api_server):
    """M-6：top_k=-1 回 400。"""
    _store, _pipeline, base = api_server
    try:
        status, payload, _raw = http_get(
            f"{base}/api/memories/search?" + urlencode({"q": "test", "top_k": "-1"})
        )
    except urllib.error.HTTPError as exc:
        status, payload = exc.code, json.loads(exc.read().decode("utf-8"))
    assert status == 400
    assert payload["error"] == "bad_request"


def test_distill_messages_over_limit_400(api_server):
    """H-5：messages 超过 200 条上限回 400，不触发云端调用。"""
    _store, _pipeline, base = api_server
    messages = [{"role": "user", "content": f"msg-{i}"} for i in range(201)]
    status, payload = http_post(f"{base}/api/memory/distill", {"messages": messages})
    assert status == 400
    assert "上限" in payload["message"]


def test_synthesize_text_over_limit_400(api_server):
    """M-5：synthesize text 超过 5000 字符上限回 400，不触发合成。"""
    _store, _pipeline, base = api_server
    status, payload = http_post(f"{base}/api/voice/synthesize", {"text": "啊" * 5001})
    assert status == 400
    assert "上限" in payload["message"]


def test_computer_call_arguments_non_dict_400(computer_env):
    """L-3：arguments 为非 dict（如字符串）时显式 400，不再静默替换 {}。"""
    base, authorizer, _keyboard = computer_env
    authorizer.authorize()
    status, payload = http_post(f"{base}/api/computer/call", {"tool": "computer_run_command", "arguments": "oops"})
    assert status == 400
    assert payload["error"] == "bad_request"


def test_tools_call_arguments_non_dict_400(api_server):
    """L-3：tools/call 的 arguments 为非 dict 时显式 400。"""
    _store, _pipeline, base = api_server
    status, payload = http_post(f"{base}/api/tools/call", {"name": "system_info", "arguments": [1, 2]})
    assert status == 400
    assert payload["error"] == "bad_request"


def test_distill_agent_id_sanitized(api_server):
    """L-5：超长 agent_id 被限长到 100 字符（未配置云端时走 400 cloud_not_configured 前无异常）。"""
    _store, _pipeline, base = api_server
    status, payload = http_post(
        f"{base}/api/memory/distill",
        {"messages": [{"role": "user", "content": "hi"}], "agent_id": "x" * 500},
    )
    # 未配置云端 api_key 的测试环境：服务端在蒸馏前先拒绝（400 cloud_not_configured），
    # 关键断言是不因超长 agent_id 出现 500
    assert status == 400
    assert payload["error"] == "cloud_not_configured"


def test_payload_too_large_bounded_read(tmp_path):
    """H-4：声明超大 Content-Length 的慢客户端不再让服务永久阻塞。

    raw socket 发送声明 10MB body 的请求头但只发 1 字节——修复前服务按声明值
    read(10MB) 永久等待；修复后有界丢弃（64KB）+ ApiHandler.timeout socket
    超时，最坏阻塞 timeout 秒后仍能回 413。
    """
    _store, _pipeline, handler = create_app(data_dir=str(tmp_path))
    handler.timeout = 2  # 缩短 socket 超时以加速测试（生产为 30s）
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        try:
            # POST /api/computer/authorize 先读 body 再处理（chat/messages 有
            # 不读 body 的未启用守卫前置、memories 无 POST 路由，均不适合本场景）
            sock.sendall(
                b"POST /api/computer/authorize HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 10485760\r\n"
                b"\r\n"
                b"x"  # 声明 10MB 实际只发 1 字节（恶意慢客户端形态）
            )
            data = sock.recv(4096)
            # recv 可能只拿到响应头，补收一次确保 JSON body（43 字节）到手
            try:
                data += sock.recv(4096)
            except OSError:
                pass
        finally:
            sock.close()
        status_line = data.split(b"\r\n", 1)[0]
        assert b"413" in status_line
        assert b"payload_too_large" in data
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_agents_update_unknown_fields_ignored(api_server):
    """L-4：PUT /api/agents 只接受白名单字段，未知字段被忽略而非透传底层。"""
    _store, _pipeline, base = api_server
    status, agent = http_post(
        f"{base}/api/agents", {"name": "A1", "persona": "P1"}
    )
    assert status == 201
    agent_id = agent["id"]
    status, updated = http_post(
        f"{base}/api/agents/{agent_id}", {"name": "A2", "hacker_field": "evil"}, method="PUT"
    )
    assert status == 200
    assert updated["name"] == "A2"
    assert "hacker_field" not in updated


# ---------------------------------------------------------------- 低-5/低-7/中-4（第四轮体检批次B）
def test_internal_error_sanitized_no_detail_leak(api_server, monkeypatch, caplog):
    """低-5：兜底 500 对外只回错误码与异常类别名（detail=类名），完整异常仅写日志。"""
    import logging as _logging

    _store, pipeline, base = api_server

    def _boom(*_args, **_kwargs):
        raise RuntimeError("机密内部信息 secret-internal-detail")

    monkeypatch.setattr(pipeline, "retrieve", _boom)
    with caplog.at_level(_logging.ERROR, logger="lite.server.api_server"):
        status, body = http_get_json(f"{base}/api/memories/search?" + urlencode({"q": "x"}))
    assert status == 500
    assert body["error"] == "internal error"
    assert body["detail"] == "RuntimeError"
    assert "secret-internal-detail" not in json.dumps(body)
    assert "secret-internal-detail" in caplog.text


class _LeakRegistry:
    """注册表桩：call 返回 success=False 且 error 携带内部异常文本（模拟 registry 包装 str(exc)）。"""

    def list_tools(self):
        return [
            {
                "id": "boom_tool",
                "name": "boom_tool",
                "description": "测试桩工具",
                "source": "builtin",
                "category": "test",
                "enabled": True,
            }
        ]

    def call(self, tool_id, arguments=None):
        return {
            "success": False,
            "tool": tool_id,
            "authorized": True,
            "error": "RuntimeError: 机密内部细节 secret-tool-detail",
            "result": None,
        }


def test_tools_call_failure_message_sanitized(tmp_path, caplog):
    """低-5：tools/call 失败分支对外只回固定类别摘要，完整内部错误仅写日志。"""
    import logging as _logging

    store, pipeline, manager, _remote = build_deps(data_dir=str(tmp_path))
    handler = make_handler(store, pipeline, manager, registry=_LeakRegistry())
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    base = f"http://127.0.0.1:{port}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with caplog.at_level(_logging.WARNING, logger="lite.server.api_server"):
            status, body = http_post(f"{base}/api/tools/call", {"name": "boom_tool", "arguments": {}})
        assert status == 400
        assert body["error"] == "tool_failed"
        assert body["message"] == "工具执行失败"
        assert "secret-tool-detail" not in json.dumps(body)
        assert "secret-tool-detail" in caplog.text
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_top_k_zero_accepted_returns_200(api_server):
    """低-7：top_k=0 为显式取值（管线内钳为候选 1），API 层正常返回 200。"""
    _store, pipeline, base = api_server
    pipeline.add("topk zero probe content")
    status, body, _raw = http_get(
        f"{base}/api/memories/search?" + urlencode({"q": "probe", "top_k": "0"})
    )
    assert status == 200
    assert "memories" in body


class _FakeImmediateServer:
    """serve_forever 立即以 KeyboardInterrupt 返回的假服务器（验证 main 放行路径用）。"""

    def serve_forever(self):
        raise KeyboardInterrupt

    def server_close(self):
        pass


def test_main_refuses_non_loopback_without_token(monkeypatch, capsys):
    """中-4：-h 0.0.0.0 且未配置 CXA_API_TOKEN → 默认拒绝启动并输出中文错误提示。"""
    monkeypatch.delenv("CXA_API_TOKEN", raising=False)
    monkeypatch.delenv("CXA_ALLOW_UNSAFE", raising=False)

    def _no_server(**_kwargs):
        raise AssertionError("拒绝启动路径不应创建服务器")

    monkeypatch.setattr(api_server_module, "create_server", _no_server)
    with pytest.raises(SystemExit):
        api_server_module.main(["-h", "0.0.0.0", "-p", "8600"])
    out = capsys.readouterr().out
    assert "拒绝启动" in out
    assert "CXA_API_TOKEN" in out


def test_main_allows_unsafe_non_loopback_with_banner(monkeypatch, capsys):
    """中-4：CXA_ALLOW_UNSAFE=1 显式放行非回环监听，并打印醒目风险横幅。"""
    monkeypatch.delenv("CXA_API_TOKEN", raising=False)
    monkeypatch.setenv("CXA_ALLOW_UNSAFE", "1")
    monkeypatch.setattr(
        api_server_module, "create_server", lambda **_kwargs: _FakeImmediateServer()
    )
    api_server_module.main(["-h", "0.0.0.0", "-p", "8600"])
    out = capsys.readouterr().out
    assert "安全警告" in out
    assert "CXA_ALLOW_UNSAFE" in out


def test_main_allows_non_loopback_with_token(monkeypatch, capsys):
    """中-4：已配置 CXA_API_TOKEN 时非回环监听放行（令牌闸兜底）。"""
    monkeypatch.setenv("CXA_API_TOKEN", "unit-test-token")
    monkeypatch.delenv("CXA_ALLOW_UNSAFE", raising=False)
    monkeypatch.setattr(
        api_server_module, "create_server", lambda **_kwargs: _FakeImmediateServer()
    )
    api_server_module.main(["-h", "0.0.0.0", "-p", "8600"])
    out = capsys.readouterr().out
    assert "已启动" in out


def test_main_loopback_default_unaffected(monkeypatch, capsys):
    """中-4：默认回环监听不触发安全闸（既有行为保持）。"""
    monkeypatch.delenv("CXA_API_TOKEN", raising=False)
    monkeypatch.delenv("CXA_ALLOW_UNSAFE", raising=False)
    monkeypatch.setattr(
        api_server_module, "create_server", lambda **_kwargs: _FakeImmediateServer()
    )
    api_server_module.main(["-p", "8600"])
    out = capsys.readouterr().out
    assert "已启动" in out
    assert "拒绝启动" not in out


# ---------------------------------------------------------------- 第四轮体检批次E：向量库降级透明化
def test_build_deps_lancedb_degrade_warning(tmp_path, monkeypatch, caplog):
    """批次E：配置 vector.backend=lancedb 但依赖不可用时，中文告警且 InMemory 兜底不变。

    monkeypatch importlib.util.find_spec 使 "lancedb" 探测为缺失（对应 frozen
    产物 excludes lancedb 形态），验证 build_deps 降级路径：告警可见 +
    InMemoryVectorStore 兜底不变。
    """
    import importlib.util
    import logging

    from lite.memory.vector_store import InMemoryVectorStore

    real_find_spec = importlib.util.find_spec

    def _fake_find_spec(name, *args, **kwargs):
        # 仅模拟 lancedb 缺失，其余模块探测走真实实现
        if name == "lancedb":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", _fake_find_spec)
    with caplog.at_level(logging.WARNING, logger="lite.server.api_server"):
        _store, pipeline, _manager, _remote = build_deps(data_dir=str(tmp_path))

    # 中文降级告警已发出（不再静默降级）
    assert any("已降级为内存向量库" in rec.getMessage() for rec in caplog.records)
    # 兜底不变：装配仍为内存向量库
    assert isinstance(pipeline.vector_store, InMemoryVectorStore)


# ---------------------------------------------------------------- 首启向导接口族（Task 6 / Task 7）
class _FakeDownloader:
    """假下载器替身（**绝不触网**）：用 Event 阻塞模拟长下载，可断言构造次数与入参。

    三种模式：
    - ``block``：阻塞至 :attr:`release` 置位后经 ``progress_cb`` 触发取消检查
      （取消标记命中时内部哨兵从 ``progress_cb`` 抛出，中断本次「下载」）；
    - ``fail``：直接抛异常（模拟网络中断）；
    - ``success``：写一个 ``.tmp`` 后返回落位路径（模拟原子改名完成）。
    """

    def __init__(self, dest_dir=None, source=None, mode="success"):
        self.dest_dir = dest_dir or os.getcwd()
        self.source = source
        self.mode = mode
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = []

    def download(self, repo, filename, source=None, progress_cb=None, verify_size_gb=None):
        """模拟下载（签名与 ``LlmDownloader.download`` 一致，无端点参数）。"""
        self.calls.append(
            {"repo": repo, "filename": filename, "source": source}
        )
        os.makedirs(self.dest_dir, exist_ok=True)
        tmp_path = os.path.join(self.dest_dir, filename + ".tmp")
        with open(tmp_path, "wb") as fh:
            fh.write(b"partial-model-bytes")
        self.started.set()
        if progress_cb is not None:
            progress_cb(0, 100)
        if self.mode == "block":
            self.release.wait(timeout=10)
            if progress_cb is not None:
                progress_cb(50, 100)  # 取消标记在此命中 → 抛内部哨兵中断下载
            return Path(os.path.join(self.dest_dir, filename))
        if self.mode == "fail":
            raise RuntimeError("connection reset by peer")
        if progress_cb is not None:
            progress_cb(100, 100)
        return Path(os.path.join(self.dest_dir, filename))


@contextmanager
def setup_server(tmp_path, config=None, download_manager=None):
    """起一个绑定临时端口的 setup 测试服务，产出 ``(base, config, handler)``。"""
    store, pipeline, manager, _remote = build_deps(data_dir=str(tmp_path))
    if config is None:
        config = ConfigManager(config_path=str(tmp_path / "config.json"), data_dir=str(tmp_path))
    authorizer = ControlAuthorizer(data_dir=str(tmp_path))
    computer = ComputerControl(authorized=authorizer.is_authorized())
    bridge = ToolBridge(computer=computer, authorizer=authorizer)
    handler = make_handler(
        store, pipeline, manager,
        computer=computer, authorizer=authorizer, bridge=bridge, config=config,
        download_manager=download_manager,
    )
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", config, handler
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _wait_for_state(base, expected, timeout=5.0):
    """轮询下载进度直至状态命中（超时抛断言错误并附带最后一次快照）。"""
    deadline = time.monotonic() + timeout
    body = None
    while time.monotonic() < deadline:
        _status, body, _raw = http_get(f"{base}/api/setup/model/progress")
        if body["state"] == expected:
            return body
        time.sleep(0.05)
    raise AssertionError(f"下载状态未在 {timeout}s 内变为 {expected}，最后快照：{body}")


# ---------------------------------------------------------------- Task 7：状态端
def test_setup_status_new_config_requires_wizard(tmp_path):
    """新 config（setup.completed=False）→ wizard_required=True 且响应不含 api_key。"""
    with setup_server(tmp_path) as (base, _config, _handler):
        status, body, raw = http_get(f"{base}/api/setup/status")
    assert status == 200
    assert body["completed"] is False
    assert body["wizard_required"] is True
    assert body["download"]["channel"] == "mirror"
    assert body["local_llm_source"] == "modelscope"
    assert "api_key" not in raw.lower()


def test_setup_status_legacy_config_without_setup_section(tmp_path):
    """老 config（无 setup 段）→ 视为已完成初始化，向导不再出现。"""
    (tmp_path / "config.json").write_text(
        json.dumps({"cloud": {"provider": "deepseek"}}), encoding="utf-8"
    )
    with setup_server(tmp_path) as (base, config, _handler):
        _status, body, raw = http_get(f"{base}/api/setup/status")
    assert config.get("setup", "completed") is True
    assert body["completed"] is True
    assert body["wizard_required"] is False
    assert "api_key" not in raw.lower()


def test_setup_status_never_exposes_api_key(tmp_path):
    """即便已配置 api_key，status 响应序列化文本中也不得出现密钥值。"""
    config = ConfigManager(config_path=str(tmp_path / "config.json"), data_dir=str(tmp_path))
    config.set("cloud", "api_key", "sk-secret-must-not-leak")
    with setup_server(tmp_path, config=config) as (base, _config, _handler):
        _status, _body, raw = http_get(f"{base}/api/setup/status")
    assert "sk-secret-must-not-leak" not in raw
    assert "api_key" not in raw.lower()


def test_setup_status_source_falls_back_to_channel(tmp_path):
    """配置 source 非法时，``local_llm_source`` 回落 ``model_repo_for_channel(channel)``。"""
    # mirror + 非法 source → modelscope（国内路线派生）
    (tmp_path / "mirror.json").write_text(
        json.dumps({"download": {"channel": "mirror"}, "local_llm": {"source": "bogus"}}),
        encoding="utf-8",
    )
    cfg = ConfigManager(config_path=str(tmp_path / "mirror.json"), data_dir=str(tmp_path))
    with setup_server(tmp_path, config=cfg) as (base, _cfg, _h):
        _status, body, _raw = http_get(f"{base}/api/setup/status")
    assert body["download"]["channel"] == "mirror"
    assert body["local_llm_source"] == "modelscope"

    # official + 非法 source → huggingface（海外路线派生）
    (tmp_path / "official.json").write_text(
        json.dumps({"download": {"channel": "official"}, "local_llm": {"source": "bogus"}}),
        encoding="utf-8",
    )
    cfg2 = ConfigManager(config_path=str(tmp_path / "official.json"), data_dir=str(tmp_path))
    with setup_server(tmp_path, config=cfg2) as (base2, _cfg2, _h2):
        _status2, body2, _raw2 = http_get(f"{base2}/api/setup/status")
    assert body2["download"]["channel"] == "official"
    assert body2["local_llm_source"] == "huggingface"


def test_setup_status_reads_configured_source_truth(tmp_path):
    """配置里手工改过的合法 source 直接回显真相（不在 status 重新派生覆盖）。"""
    (tmp_path / "manual.json").write_text(
        json.dumps({"download": {"channel": "official"}, "local_llm": {"source": "modelscope"}}),
        encoding="utf-8",
    )
    cfg = ConfigManager(config_path=str(tmp_path / "manual.json"), data_dir=str(tmp_path))
    with setup_server(tmp_path, config=cfg) as (base, _cfg, _h):
        _status, body, _raw = http_get(f"{base}/api/setup/status")
    assert body["download"]["channel"] == "official"
    assert body["local_llm_source"] == "modelscope"


# ---------------------------------------------------------------- Task 7：推荐端
def test_setup_recommend_structure_and_suggested_source(tmp_path):
    """推荐端结构完整；镜像通道建议魔塔、官方通道建议 HuggingFace。"""
    with setup_server(tmp_path) as (base, _config, _handler):
        status, body, _raw = http_get(f"{base}/api/setup/recommend")
    assert status == 200
    assert {"profile", "recommendation", "tiers", "suggested_source"} <= set(body)
    assert body["suggested_source"] == "modelscope"
    assert any(tier["tier"] == "1.7B" for tier in body["tiers"])
    assert all("tier" in tier and "repo" in tier for tier in body["tiers"])
    assert {"use_local", "device", "tier", "config_patch", "model"} <= set(body["recommendation"])
    assert "probe_notes" in body["profile"]

    (tmp_path / "official.json").write_text(
        json.dumps({"download": {"channel": "official"}}), encoding="utf-8"
    )
    official_cfg = ConfigManager(
        config_path=str(tmp_path / "official.json"), data_dir=str(tmp_path)
    )
    with setup_server(tmp_path, config=official_cfg) as (base2, _cfg2, _h2):
        _status2, body2, _raw2 = http_get(f"{base2}/api/setup/recommend")
    assert body2["suggested_source"] == "huggingface"


def test_setup_recommend_degrades_on_probe_failure(tmp_path, monkeypatch):
    """硬件探测抛异常时仍返回 200 且结构完整（降级为走云，不返回 5xx）。"""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(api_server_module, "detect_profile", _boom)
    with setup_server(tmp_path) as (base, _config, _handler):
        status, body, _raw = http_get(f"{base}/api/setup/recommend")
    assert status == 200
    assert {"profile", "recommendation", "tiers", "suggested_source"} <= set(body)
    assert body["recommendation"]["use_local"] is False
    assert body["recommendation"]["config_patch"]["local_llm"]["enabled"] is False
    assert any("硬件探测失败" in note for note in body["profile"]["probe_notes"])


# ---------------------------------------------------------------- Task 7：complete 端
def test_setup_complete_applies_whitelist_and_encrypts_key(tmp_path):
    """合法提交：白名单键全部生效、setup 置完成、密钥以 Fernet 密文落盘且可还原。"""
    with setup_server(tmp_path) as (base, config, _handler):
        status, body = http_post(
            f"{base}/api/setup/complete",
            {
                "cloud": {"provider": "tongyi", "api_key": "sk-unit-test-key"},
                "download": {"channel": "official"},
                "local_llm": {"source": "huggingface", "enabled": False},
            },
        )
        assert status == 200
        assert body["ok"] is True
        assert body["setup"]["completed"] is True
        assert body["setup"]["completed_at"]
        for key in ("cloud.provider", "cloud.api_key", "download.channel",
                    "local_llm.source", "local_llm.enabled"):
            assert key in body["applied"], f"{key} 应在 applied 中"
        assert body["ignored"] == []
        assert body["config"]["cloud"]["provider"] == "tongyi"
        # 状态端点随之放行（向导完成后不再出现）
        _status, sbody, _raw = http_get(f"{base}/api/setup/status")
        assert sbody["wizard_required"] is False
        assert sbody["download"]["channel"] == "official"
        assert sbody["local_llm_source"] == "huggingface"

    assert config.get("setup", "completed") is True
    assert config.get("download", "channel") == "official"
    # 落盘为密文（明文密钥不出现在 config.json），重载可还原
    on_disk = (tmp_path / "config.json").read_text(encoding="utf-8")
    assert "sk-unit-test-key" not in on_disk
    assert "cxa_enc:" in on_disk
    reloaded = ConfigManager(config_path=str(tmp_path / "config.json"), data_dir=str(tmp_path))
    assert reloaded.get("cloud", "api_key") == "sk-unit-test-key"


def test_setup_complete_ignores_invalid_channel_and_source(tmp_path):
    """非法 download.channel / local_llm.source → 不生效但进 ignored 显式回显。"""
    with setup_server(tmp_path) as (base, config, _handler):
        status, body = http_post(
            f"{base}/api/setup/complete",
            {"download": {"channel": "xx"}, "local_llm": {"source": "yy"}},
        )
    assert status == 200
    assert body["ok"] is True
    assert body["applied"] == []
    ignored_text = "\n".join(body["ignored"])
    assert "download.channel" in ignored_text and "xx" in ignored_text
    assert "local_llm.source" in ignored_text and "yy" in ignored_text
    assert config.get("download", "channel") == "mirror"
    assert config.get("local_llm", "source") == "modelscope"
    assert body["setup"]["completed"] is True  # 非法值不阻断向导完成


def test_setup_complete_derives_source_from_channel(tmp_path):
    """通道派生模型仓库：mirror→modelscope / official→huggingface，且 applied 各登记一次。"""
    for channel, expected in (("mirror", "modelscope"), ("official", "huggingface")):
        case_dir = tmp_path / channel
        case_dir.mkdir()
        config = ConfigManager(
            config_path=str(case_dir / "config.json"), data_dir=str(case_dir)
        )
        with setup_server(case_dir, config=config) as (base, cfg, _handler):
            status, body = http_post(
                f"{base}/api/setup/complete", {"download": {"channel": channel}}
            )
        assert status == 200
        assert "download.channel" in body["applied"]
        # 派生键只登记一次（不因显式/派生双路径重复）
        assert body["applied"].count("local_llm.source") == 1
        assert body["ignored"] == []
        assert cfg.get("download", "channel") == channel
        assert cfg.get("local_llm", "source") == expected
        # 落盘真相与派生值一致
        on_disk = json.loads((case_dir / "config.json").read_text(encoding="utf-8"))
        assert on_disk["local_llm"]["source"] == expected


def test_setup_complete_conflicting_source_ignored(tmp_path):
    """显式 source 与通道派生值冲突：不生效并入 ignored（配置以通道派生为准）。"""
    with setup_server(tmp_path) as (base, config, _handler):
        status, body = http_post(
            f"{base}/api/setup/complete",
            {"download": {"channel": "mirror"}, "local_llm": {"source": "huggingface"}},
        )
    assert status == 200
    assert body["applied"].count("local_llm.source") == 1
    assert config.get("local_llm", "source") == "modelscope"
    ignored_text = "\n".join(body["ignored"])
    assert "local_llm.source" in ignored_text
    assert "huggingface" in ignored_text
    assert "download.channel" in ignored_text  # 说明由下载路线决定


def test_setup_complete_consistent_source_applied_once(tmp_path):
    """通道与显式 source 一致：照常 applied 且不重复登记，ignored 为空。"""
    with setup_server(tmp_path) as (base, config, _handler):
        status, body = http_post(
            f"{base}/api/setup/complete",
            {"download": {"channel": "official"}, "local_llm": {"source": "huggingface"}},
        )
    assert status == 200
    assert body["applied"].count("local_llm.source") == 1
    assert body["ignored"] == []
    assert config.get("local_llm", "source") == "huggingface"


def test_setup_complete_source_without_channel_still_applies(tmp_path):
    """未给通道时保留手工配置能力：显式且合法的 source 照常 applied。"""
    with setup_server(tmp_path) as (base, config, _handler):
        status, body = http_post(
            f"{base}/api/setup/complete", {"local_llm": {"source": "huggingface"}}
        )
    assert status == 200
    assert "local_llm.source" in body["applied"]
    assert body["ignored"] == []
    assert config.get("local_llm", "source") == "huggingface"
    assert config.get("download", "channel") == "mirror"  # 未给通道 → 保持默认


def test_setup_complete_without_api_key_still_completes(tmp_path):
    """省略 api_key（稍后补填）仍可完成向导，cloud.api_key 保持空值。"""
    with setup_server(tmp_path) as (base, config, _handler):
        status, body = http_post(
            f"{base}/api/setup/complete", {"cloud": {"provider": "deepseek"}}
        )
    assert status == 200
    assert body["setup"]["completed"] is True
    assert "cloud.api_key" not in body["applied"]
    assert config.get("cloud", "api_key") == ""


def test_setup_complete_empty_body_and_bad_json(tmp_path):
    """空 body 与非法 JSON 分别返回可区分的错误码，且不产生部分写入。"""
    with setup_server(tmp_path) as (base, config, _handler):
        status, body = http_post(f"{base}/api/setup/complete", {})
        assert status == 400
        assert body["error"] == "empty_body"

        status2, body2 = http_raw_body(f"{base}/api/setup/complete", b"{not-valid-json")
        assert status2 == 400
        assert body2["error"] == "bad_json"
    # 两次坏请求均未改动配置（无部分写入）
    assert config.get("setup", "completed") is False


def test_setup_complete_invalid_section_type_400(tmp_path):
    """段存在但非 dict（{"cloud": "x"}）→ 400 段类型错误（与既有 /api/settings 口径一致）。"""
    with setup_server(tmp_path) as (base, config, _handler):
        status, body = http_post(f"{base}/api/setup/complete", {"cloud": "x"})
        assert status == 400
        assert body["error"] == "invalid section type: cloud"
        status2, body2 = http_post(f"{base}/api/setup/complete", {"download": [1, 2]})
        assert status2 == 400
        assert body2["error"] == "invalid section type: download"
    assert config.get("setup", "completed") is False


def test_setup_complete_apply_recommended_with_explicit_override(tmp_path, monkeypatch):
    """apply_recommended=true 应用推荐补丁，且显式传入的键优先（用户手改覆盖推荐）。"""
    profile = {
        "cpu_cores": 8,
        "ram_gb": 16.0,
        "gpu_vendor": "nvidia",
        "vram_gb": 8.0,
        "cuda_version": "12.4",
        "disk_free_gb": 200.0,
        "probe_notes": [],
    }
    monkeypatch.setattr(api_server_module, "detect_profile", lambda *a, **k: profile)
    with setup_server(tmp_path) as (base, config, _handler):
        status, body = http_post(
            f"{base}/api/setup/complete",
            {"apply_recommended": True, "local_llm": {"enabled": False}},
        )
    assert status == 200
    # 推荐补丁生效：device=gpu（nvidia 显存 8GB 分档），embedding 同步切 gpu
    assert config.get("local_llm", "device") == "gpu"
    assert config.get("embedding", "device") == "gpu"
    # 显式传入的 enabled=False 覆盖推荐值 True
    assert config.get("local_llm", "enabled") is False
    assert "config_patch.local_llm.device" in body["applied"]
    assert "config_patch.embedding.device" in body["applied"]
    assert "config_patch.local_llm.enabled" not in body["applied"]


def test_setup_config_write_lock_shared_with_download_manager(tmp_path):
    """Task 7：默认构建的下载管理器与请求线程共用同一把模块级写锁。"""
    with setup_server(tmp_path) as (_base, _config, handler):
        assert handler._download_manager._write_lock is api_server_module._CONFIG_WRITE_LOCK


# ---------------------------------------------------------------- Task 6：下载端（进度 / 幂等 / 取消 / 失败 / 完成）
def _build_download_env(tmp_path, mode):
    """构造「临时配置 + 假下载器管理器」组合，返回 (config, manager, fake, constructions)。"""
    config = ConfigManager(config_path=str(tmp_path / "config.json"), data_dir=str(tmp_path))
    fake = _FakeDownloader(dest_dir=str(tmp_path / "models"), mode=mode)
    constructions = []

    def _factory(**kwargs):
        constructions.append(kwargs)
        return fake

    manager = ModelDownloadManager(
        dest_dir=str(tmp_path / "models"),
        config_manager=config,
        downloader_factory=_factory,
        write_lock=api_server_module._CONFIG_WRITE_LOCK,
    )
    return config, manager, fake, constructions


def test_setup_model_download_progress_immediate_and_idempotent(tmp_path):
    """阻塞下载期间：进度与状态端点即时返回（不阻塞服务）；重复触发幂等。"""
    config, manager, fake, constructions = _build_download_env(tmp_path, "block")
    with setup_server(tmp_path, config=config, download_manager=manager) as (base, _cfg, _handler):
        status, body = http_post(f"{base}/api/setup/model/download", {"tier": "1.7B"})
        assert status == 200
        assert body["ok"] is True
        assert body["state"] == "downloading"
        assert body["already_running"] is False
        assert body["model"]["tier"] == "1.7B"
        assert fake.started.wait(timeout=5)

        # 下载被替身阻塞：进度端点必须即时返回（单次请求 < 2s）
        started = time.monotonic()
        _status, progress, _raw = http_get(f"{base}/api/setup/model/progress")
        elapsed = time.monotonic() - started
        assert elapsed < 2.0, f"进度端点被下载阻塞（耗时 {elapsed:.2f}s）"
        assert progress["state"] == "downloading"
        assert set(progress) >= {"state", "downloaded", "total", "percent", "file", "error", "model"}

        # 下载进行中：其余端点仍即时响应（证明后台线程不阻塞服务）
        started = time.monotonic()
        status2, sbody, _raw2 = http_get(f"{base}/api/setup/status")
        elapsed2 = time.monotonic() - started
        assert status2 == 200 and elapsed2 < 2.0, f"status 被下载阻塞（耗时 {elapsed2:.2f}s）"
        assert sbody["wizard_required"] is True

        # 重复触发：幂等，不启动第二个线程 / 不二次构造下载器
        _status3, again = http_post(f"{base}/api/setup/model/download", {})
        assert again["already_running"] is True
        assert again["state"] == "downloading"
        assert len(constructions) == 1

        # 收尾：先取消再放行替身，避免下载线程在测试结束后继续写配置
        http_post(f"{base}/api/setup/model/cancel", {})
        fake.release.set()


def test_setup_model_cancel_interrupts_and_keeps_tmp(tmp_path):
    """取消：状态置 canceled、取消标记被传递、.tmp 保留供断点续传。"""
    config, manager, fake, _constructions = _build_download_env(tmp_path, "block")
    with setup_server(tmp_path, config=config, download_manager=manager) as (base, _cfg, _handler):
        http_post(f"{base}/api/setup/model/download", {})
        assert fake.started.wait(timeout=5)

        status, body = http_post(f"{base}/api/setup/model/cancel", {})
        assert status == 200
        assert body == {"ok": True, "state": "canceled"}
        assert manager.cancel_event.is_set()

        # 放行阻塞的替身：下一次 progress_cb 命中取消标记并中断下载
        fake.release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and manager._thread.is_alive():
            time.sleep(0.05)
        assert not manager._thread.is_alive(), "取消后下载线程未退出"

        _status2, progress, _raw = http_get(f"{base}/api/setup/model/progress")
        assert progress["state"] == "canceled"
        # 临时文件保留（LlmDownloader 异常路径保留 .tmp 供续传的替身等价语义）
        tmp_files = list((tmp_path / "models").glob("*.tmp"))
        assert tmp_files, "取消后应保留 .tmp 供断点续传"
        assert config.get("local_llm", "model_path") == ""  # 取消不写回模型路径


def test_setup_model_download_failure_reports_channel_and_endpoint(tmp_path):
    """失败：state=failed，error 为中文且含通道 / 端点字样与「切换」提示（不静默换源）。"""
    config, manager, fake, _constructions = _build_download_env(tmp_path, "fail")
    with setup_server(tmp_path, config=config, download_manager=manager) as (base, _cfg, _handler):
        http_post(f"{base}/api/setup/model/download", {"source": "huggingface"})
        progress = _wait_for_state(base, "failed")
    assert progress["error"]
    assert "下载失败" in progress["error"]
    assert "通道" in progress["error"]
    assert "端点" in progress["error"]
    assert "切换" in progress["error"]
    # 默认镜像通道 + huggingface 仓库：端点恒为官方站点（国内路线不再经 hf-mirror）
    assert "mirror" in progress["error"]
    assert "https://huggingface.co" in progress["error"]
    assert "hf-mirror.com" not in progress["error"]
    assert config.get("local_llm", "model_path") == ""


def test_setup_model_download_success_writes_model_path(tmp_path):
    """成功：state=done，local_llm.model_path 已写入配置并落盘。"""
    config, manager, fake, _constructions = _build_download_env(tmp_path, "success")
    with setup_server(tmp_path, config=config, download_manager=manager) as (base, _cfg, _handler):
        status, body = http_post(f"{base}/api/setup/model/download", {"source": "modelscope"})
        assert status == 200
        progress = _wait_for_state(base, "done")
    expected = str(tmp_path / "models" / body["model"]["filename"])
    assert progress["state"] == "done"
    assert progress["downloaded"] == 100 and progress["percent"] == 100.0
    assert progress["error"] is None
    assert config.get("local_llm", "model_path") == expected
    on_disk = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert on_disk["local_llm"]["model_path"] == expected


def test_setup_model_download_unknown_tier_400(tmp_path):
    """未知档位 → 400（中文错误），不启动下载、不静默换档。"""
    config, manager, fake, constructions = _build_download_env(tmp_path, "success")
    with setup_server(tmp_path, config=config, download_manager=manager) as (base, _cfg, _handler):
        status, body = http_post(f"{base}/api/setup/model/download", {"tier": "99B"})
        assert status == 400
        assert body["error"] == "bad_request"
        assert "未知模型档位" in body["message"]
        _status2, progress, _raw = http_get(f"{base}/api/setup/model/progress")
    assert progress["state"] == "idle"
    assert constructions == []
    assert fake.calls == []


def test_setup_model_download_unknown_source_400(tmp_path):
    """显式 source 不在 MODEL_REPOS 白名单 → 400（中文错误），不启动下载、不静默换源。"""
    config, manager, fake, constructions = _build_download_env(tmp_path, "success")
    with setup_server(tmp_path, config=config, download_manager=manager) as (base, _cfg, _handler):
        status, body = http_post(f"{base}/api/setup/model/download", {"source": "bogus"})
        assert status == 400
        assert body["error"] == "bad_request"
        assert "bogus" in body["message"]
        _status2, progress, _raw = http_get(f"{base}/api/setup/model/progress")
    assert progress["state"] == "idle"
    assert constructions == []
    assert fake.calls == []


def test_setup_model_progress_idle_before_any_download(tmp_path):
    """未触发下载时进度端点返回 idle 空快照（字段齐备）。"""
    with setup_server(tmp_path) as (base, _config, _handler):
        status, body, _raw = http_get(f"{base}/api/setup/model/progress")
    assert status == 200
    assert body == {
        "state": "idle",
        "downloaded": 0,
        "total": 0,
        "percent": 0.0,
        "file": "",
        "error": None,
        "model": None,
    }


# ---------------------------------------------------------------- Task 6 / Task 7：setup 端点鉴权
def test_setup_endpoints_require_token_in_token_mode(api_server, monkeypatch):
    """令牌模式下 setup 六个端点无令牌一律 403（不新增豁免端点）。"""
    monkeypatch.setattr(api_server_module, "_API_TOKEN", "unit-test-token")
    _store, _pipeline, base = api_server
    for path in ("/api/setup/status", "/api/setup/recommend", "/api/setup/model/progress"):
        status, body = http_get_json(f"{base}{path}")
        assert status == 403, f"{path} 无令牌应 403"
        assert body["error"] == "unauthorized_client"
    for path, payload in (
        ("/api/setup/complete", {"download": {"channel": "official"}}),
        ("/api/setup/model/download", {}),
        ("/api/setup/model/cancel", {}),
    ):
        status, body = http_post(f"{base}{path}", payload)
        assert status == 403, f"{path} 无令牌应 403"
        assert body["error"] == "unauthorized_client"



