# -*- coding: utf-8 -*-
"""API 服务集成测试（A9）——真实起服 + urllib HTTP 请求。

确实启动单线程 HTTPServer 于临时数据目录，用 urllib.request 走真实 HTTP 访问各端点：
health、空列表、add 后列表可查、search 可返回、delete 后列表不含、中文 UTF-8 往返。
同时覆盖 404 / 400 等边界路由。
"""

import json
import socket
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer
from urllib.parse import urlencode

import pytest

from lite.computer_control import ComputerControl, ToolBridge
from lite.computer_control.security import ControlAuthorizer
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
        assert resp.headers.get("Access-Control-Allow-Headers") == "Content-Type"


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


def test_delete_memory_huge_id_returns_structured_500(api_server):
    """超大数字 id 触发 sqlite OverflowError 时兜底为结构化 500（而非连接中断）。"""
    _store, _pipeline, base = api_server
    huge_id = "9" * 30
    status, body = http_delete(f"{base}/api/memories/{huge_id}")
    assert status == 500
    assert body["error"] == "internal error"
    assert isinstance(body.get("detail"), str)
    assert len(body["detail"]) <= 200


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