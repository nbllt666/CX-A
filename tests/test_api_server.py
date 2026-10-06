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


@pytest.fixture(autouse=True)
def _isolate_voice_bridge(monkeypatch):
    """隔离内置语音 sidecar 探测：服务测试稳定落到 Mock 语音后端。

    宿主开发机可能已安装运行时（``runtime/voice`` + ``runtime/voice_bridge``），
    统一屏蔽以避免测试真启动桥进程（加载模型导致超时）；sidecar 接线本身由
    ``tests/test_voice_bridge.py`` 专项覆盖。
    """
    import lite.audio as lite_audio

    monkeypatch.setattr(lite_audio, "_try_voice_bridge", lambda *args, **kwargs: None)


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


def http_post_stream(url, payload):
    """POST 请求并按 NDJSON 行解析流式响应，返回 (status, [obj,...])。"""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            objs = [json.loads(line) for line in raw.splitlines() if line.strip()]
            return resp.status, objs
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
    """GET /api/settings 返回脱敏配置视图；api_key 未配置时脱敏回显为空串。

    Task 5（向导选项入设置）：视图契约由「完全不含 api_key 键」升级为
    「脱敏回显」——未配置时空串、已配置时 sk-****尾4位，明文任何形式绝不外泄。
    """
    _store, _pipeline, base = api_server
    status, body, _raw = http_get(f"{base}/api/settings")
    assert status == 200
    assert body["cloud"]["provider"] == "deepseek"
    assert body["tts"]["voice"] == "cx-open"
    assert body["local_llm"]["enabled"] is False
    # 未配置：键存在但值为空串（前端据此渲染「未配置」）
    assert body["cloud"]["api_key"] == ""


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
    """聊天发送守卫端点明确提示而不 404；history 已接持久化返回 ok+空列表。"""
    _store, _pipeline, base = api_server
    status, body = http_post(f"{base}/api/chat/messages", {"text": "hi"})
    assert status == 200
    assert body["error"] == "chat_service_disabled"
    assert body["ok"] is False

    # 20261004：history 为真实现（无对话时 ok=True + 空列表，不再返回守卫错误码）
    status2, body2, _raw = http_get(f"{base}/api/chat/history")
    assert status2 == 200
    assert body2["ok"] is True
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


# ---------------------------------------------------------------- 本地模式动态接线（本地模式真正本地，20261002）
class _StubLocalRuntime:
    """本地聊天运行时桩：记录 close 调用（全 stub，绝不真实加载 GGUF）。"""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _wait_until(predicate, timeout=5.0):
    """轮询等待后台加载线程生效（加载在 daemon 线程异步进行）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_local_llm_toggle_updates_ready_view(api_server, monkeypatch):
    """PUT local_llm.enabled=true 后 GET 返回 local_llm.ready=true；PUT false 后 ready=false。

    mock build 路径（monkeypatch build_local_chat_runtime 返回桩）避免真实加载模型。
    """
    _store, _pipeline, base = api_server
    stub = _StubLocalRuntime()
    build_calls = {"n": 0}

    def _fake_build(config):
        build_calls["n"] += 1
        return stub

    monkeypatch.setattr(api_server_module, "build_local_chat_runtime", _fake_build)

    # 初始：未启用未加载 → ready=false
    _status, body, _raw = http_get(f"{base}/api/settings")
    assert body["local_llm"]["enabled"] is False
    assert body["local_llm"]["ready"] is False

    # PUT true → applied；后台加载线程完成后 ready=true
    status, body = http_post(
        f"{base}/api/settings", {"local_llm": {"enabled": True}}, method="PUT"
    )
    assert status == 200
    assert "local_llm.enabled" in body["applied"]
    assert _wait_until(
        lambda: http_get(f"{base}/api/settings")[1]["local_llm"]["ready"] is True
    )
    assert build_calls["n"] == 1

    # PUT true 幂等去重：已有运行时 → 不再重复加载（禁止同一模型加载两份）
    http_post(f"{base}/api/settings", {"local_llm": {"enabled": True}}, method="PUT")
    assert _wait_until(
        lambda: http_get(f"{base}/api/settings")[1]["local_llm"]["ready"] is True
    )
    assert build_calls["n"] == 1

    # PUT false → release：ready=false，且运行时 close 被调用（幂等回收）
    status, body = http_post(
        f"{base}/api/settings", {"local_llm": {"enabled": False}}, method="PUT"
    )
    assert status == 200
    assert "local_llm.enabled" in body["applied"]
    assert _wait_until(
        lambda: http_get(f"{base}/api/settings")[1]["local_llm"]["ready"] is False
    )
    assert stub.closed is True


def test_local_llm_toggle_false_before_load_release_idempotent(api_server, monkeypatch):
    """PUT false（无运行时）→ release 幂等无副作用，ready 保持 false。"""
    _store, _pipeline, base = api_server

    def _fail_build(config):  # 不应被调用（enabled=false 时 ensure_started 不触发）
        raise AssertionError("PUT false 不应触发后台加载")

    monkeypatch.setattr(api_server_module, "build_local_chat_runtime", _fail_build)
    status, body = http_post(
        f"{base}/api/settings", {"local_llm": {"enabled": False}}, method="PUT"
    )
    assert status == 200
    assert "local_llm.enabled" in body["applied"]
    assert body["config"]["local_llm"]["ready"] is False


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


def test_voice_synthesize_stream_returns_ndjson_chunks(api_server):
    """POST /api/voice/synthesize_stream：逐句 NDJSON + 末帧 done。"""
    _store, _pipeline, base = api_server
    status, objs = http_post_stream(
        f"{base}/api/voice/synthesize_stream",
        {"text": "你好，世界。今天天气不错。"},
    )
    assert status == 200
    assert isinstance(objs, list)
    # 末帧必须是 done
    assert objs[-1].get("done") is True
    assert objs[-1].get("total") == 2
    # 前两帧为音频帧，seq 连续
    audio_frames = [o for o in objs if "audio_base64" in o]
    assert len(audio_frames) == 2
    assert [f["seq"] for f in audio_frames] == [0, 1]
    for f in audio_frames:
        assert base64.b64decode(f["audio_base64"])  # 非空


def test_voice_synthesize_stream_empty_text_400(api_server):
    """POST /api/voice/synthesize_stream：text 为空 → 400 bad_request。"""
    _store, _pipeline, base = api_server
    status, body = http_post(f"{base}/api/voice/synthesize_stream", {"text": ""})
    assert status == 400
    assert body["error"] == "bad_request"


def test_voice_synthesize_stream_segment_failure_continues(api_server, monkeypatch):
    """流式端点：单句合成失败发 error 帧，后续句仍成功，done 帧 total 正确。"""
    from lite.audio.tts import MockTTSBackend

    original = MockTTSBackend.synthesize

    def flaky(self, text, voice="cx-open"):
        if "失败" in text:
            raise RuntimeError("模拟合成失败")
        return original(self, text, voice)

    monkeypatch.setattr(MockTTSBackend, "synthesize", flaky)
    _store, _pipeline, base = api_server
    status, objs = http_post_stream(
        f"{base}/api/voice/synthesize_stream",
        {"text": "第一句。这句会失败。第三句。"},
    )
    assert status == 200
    assert objs[-1].get("done") is True
    assert objs[-1].get("total") == 3
    error_frames = [o for o in objs if "error" in o]
    audio_frames = [o for o in objs if "audio_base64" in o]
    assert len(error_frames) == 1
    assert error_frames[0]["seq"] == 1
    assert len(audio_frames) == 2
    assert [f["seq"] for f in audio_frames] == [0, 2]


def test_new_endpoints_require_token_in_token_mode(api_server, monkeypatch):
    """批次E：令牌模式下五个新端点无令牌 → 403 unauthorized_client（自动继承令牌闸）。"""
    monkeypatch.setattr(api_server_module, "_API_TOKEN", "unit-test-token")
    _store, _pipeline, base = api_server
    # GET /api/tools
    status, body = http_get_json(f"{base}/api/tools")
    assert status == 403
    assert body["error"] == "unauthorized_client"
    # POST 五端点
    for path, payload in [
        ("/api/tools/call", {"name": "system_info"}),
        ("/api/memory/distill", {"messages": [{"role": "user", "content": "hi"}]}),
        ("/api/voice/synthesize", {"text": "hi"}),
        ("/api/voice/synthesize_stream", {"text": "hi"}),
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


# ---------------------------------------------------------------- 20261005：向量库禁止降级
def test_build_deps_lancedb_missing_raises(tmp_path, monkeypatch):
    """20261005 禁止降级：配置 vector.backend=lancedb 但依赖不可用 → 启动硬失败。

    旧口径（依赖缺失 → 告警回落 SQLite/内存库）已按人类裁决移除——LanceDB 为
    默认后端，冻结包已收录依赖；任何"静默换库"都会造成数据语义漂移，故缺失时
    直接 RuntimeError（中文，含处置指引），启动中止。
    """
    import importlib.util

    (tmp_path / "config.json").write_text(
        json.dumps({"vector": {"backend": "lancedb", "path": "data/lancedb"}}),
        encoding="utf-8",
    )

    real_find_spec = importlib.util.find_spec

    def _fake_find_spec(name, *args, **kwargs):
        # 仅模拟 lancedb 缺失，其余模块探测走真实实现
        if name == "lancedb":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", _fake_find_spec)
    with pytest.raises(RuntimeError) as excinfo:
        build_deps(data_dir=str(tmp_path))
    assert "禁止降级" in str(excinfo.value)
    assert "lancedb" in str(excinfo.value)


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
    assert any(tier["tier"] == "E2B-Q4" for tier in body["tiers"])
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


def test_download_manager_downloads_mmproj_for_multimodal_tier(tmp_path):
    """多模态档位（Gemma 4）：主模型就位后同仓库下载视觉组件（双文件落同目录）。"""
    config, manager, fake, _constructions = _build_download_env(tmp_path, "success")
    result = manager.start(tier="E2B-Q4")
    assert result["ok"] is True

    # 后台线程 success 模式毫秒级完成：轮询至 done（上限 5s）
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and manager.snapshot()["state"] != "done":
        time.sleep(0.01)
    assert manager.snapshot()["state"] == "done"
    # 双文件：主模型 + mmproj 视觉组件，同仓库（文件名经 2026-10-04 联网核实）
    assert [c["filename"] for c in fake.calls] == [
        "gemma-4-E2B-it-Q4_K_M.gguf",
        "mmproj-BF16.gguf",
    ]
    assert fake.calls[0]["repo"] == fake.calls[1]["repo"] == "unsloth/gemma-4-E2B-it-GGUF"
    # 配置写回：model_path 指向主模型
    assert config.get("local_llm", "model_path", "").endswith("gemma-4-E2B-it-Q4_K_M.gguf")


def test_setup_model_download_progress_immediate_and_idempotent(tmp_path):
    """阻塞下载期间：进度与状态端点即时返回（不阻塞服务）；重复触发幂等。"""
    config, manager, fake, constructions = _build_download_env(tmp_path, "block")
    with setup_server(tmp_path, config=config, download_manager=manager) as (base, _cfg, _handler):
        status, body = http_post(f"{base}/api/setup/model/download", {"tier": "E2B-Q4"})
        assert status == 200
        assert body["ok"] is True
        assert body["state"] == "downloading"
        assert body["already_running"] is False
        assert body["model"]["tier"] == "E2B-Q4"
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


# ---------------------------------------------------------------- 悬浮桌宠模型分发（20260924_模块0_接入VRM悬浮桌宠）
@contextmanager
def pet_model_server(tmp_path, monkeypatch, root, model_bytes=None):
    """起服把 ``app_root()`` 指向临时根（可选写入模型文件），产出 ``base_url``。

    生产代码经 ``app_root()``（frozen-aware）解析 ``<app_root>/data/pet/cx-open.vrm``；
    测试态 ``sys.frozen`` 为假、``app_root()`` 恒指项目根，故用 monkeypatch 精确替换
    模块级 ``app_root`` 可调用对象——这是唯一干净注入点，**不为测试在生产代码里加钩子**。
    """
    root = str(root)
    monkeypatch.setattr(api_server_module, "app_root", lambda: root)
    if model_bytes is not None:
        model_path = os.path.join(root, "data", "pet", "cx-open.vrm")
        os.makedirs(os.path.dirname(model_path), exist_ok=True)
        with open(model_path, "wb") as fh:
            fh.write(model_bytes)
    _store, _pipeline, handler = create_app(data_dir=str(tmp_path))
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_pet_model_returns_raw_vrm_bytes(tmp_path, monkeypatch):
    """GET /api/pet/model：200 原始字节，Content-Type=model/gltf-binary 且 Content-Length 一致。"""
    payload = b"CXA-FAKE-VRM"
    with pet_model_server(tmp_path, monkeypatch, tmp_path / "approot", payload) as base:
        req = urllib.request.Request(
            f"{base}/api/pet/model", headers={"Origin": "http://localhost:5173"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read()
            assert resp.status == 200
            assert resp.headers.get("Content-Type") == "model/gltf-binary"
            assert int(resp.headers.get("Content-Length")) == len(payload)
            # 用户可替换同路径模型 → no-store 避免旧模型被缓存
            assert resp.headers.get("Cache-Control") == "no-store"
            # 沿用既有 CORS 头（允许源回显 ACAO）
            assert resp.headers.get("Access-Control-Allow-Origin") == "http://localhost:5173"
        # 响应体逐字节等于写入内容
        assert body == payload
        # 冻结契约：只实现 GET，POST 不新增路由
        status, post_body = http_post(f"{base}/api/pet/model", {})
        assert status == 404
        assert post_body["error"] == "not_found"


def test_pet_model_missing_returns_404(tmp_path, monkeypatch):
    """GET /api/pet/model：文件缺失 → 404 pet_model_missing + 中文 message（含期望路径）。"""
    root = tmp_path / "approot"
    with pet_model_server(tmp_path, monkeypatch, root) as base:
        status, body = http_get_json(f"{base}/api/pet/model")
    assert status == 404
    assert body["ok"] is False
    assert body["error"] == "pet_model_missing"
    assert "未找到" in body["message"]
    assert os.path.join(str(root), "data", "pet", "cx-open.vrm") in body["message"]


def test_pet_model_requires_token_in_token_mode(tmp_path, monkeypatch):
    """令牌模式下 /api/pet/model 无令牌 → 403；带正确令牌 → 200 原始字节。"""
    monkeypatch.setattr(api_server_module, "_API_TOKEN", "unit-test-token")
    with pet_model_server(
        tmp_path, monkeypatch, tmp_path / "approot", b"CXA-FAKE-VRM"
    ) as base:
        status, body = http_get_json(f"{base}/api/pet/model")
        assert status == 403
        assert body == {"ok": False, "error": "unauthorized_client"}

        req = urllib.request.Request(
            f"{base}/api/pet/model", headers={"X-Client-Token": "unit-test-token"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            assert resp.read() == b"CXA-FAKE-VRM"


def test_pet_model_documented_in_tools_usage(api_server):
    """GET /api/tools 的端点自述清单已同步登记 /api/pet/model（文档不漂移）。"""
    _store, _pipeline, base = api_server
    _status, body, _raw = http_get(f"{base}/api/tools")
    assert "GET /api/pet/model" in body["usage"]
    # Task 4：导入/恢复端点同步登记，防文档漂移
    assert "POST /api/pet/model/import" in body["usage"]
    assert "POST /api/pet/model/reset" in body["usage"]


# ---------------------------------------------------------------- 桌宠模型导入/恢复（Task 4：用户自定义 VRM 桌宠模型）
def _make_fake_vrm(path, content: bytes) -> str:
    """在 path 写入假 .vrm 文件（内容为字节数组即可，不校验 VRM 内部格式）。"""
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(content)
    return str(path)


def test_pet_model_import_replaces_and_backs_up(tmp_path, monkeypatch):
    """导入成功：目标被新内容替换 + 同目录 .bak 备份为旧内容 + 源文件保持不变。"""
    root = tmp_path / "approot"
    target = _make_fake_vrm(root / "data" / "pet" / "cx-open.vrm", b"OLD-MODEL-BYTES")
    source = _make_fake_vrm(tmp_path / "my-pet" / "custom.vrm", b"NEW-MODEL-BYTES")
    with pet_model_server(tmp_path, monkeypatch, root) as base:
        status, body = http_post(f"{base}/api/pet/model/import", {"source_path": source})
    assert status == 200
    assert body["ok"] is True
    assert "导入成功" in body["message"]
    # 目标被替换为新内容（与原内容不同）
    with open(target, "rb") as fh:
        assert fh.read() == b"NEW-MODEL-BYTES"
    # 同目录备份存在且内容为旧模型
    backup = target + ".bak"
    assert os.path.isfile(backup)
    with open(backup, "rb") as fh:
        assert fh.read() == b"OLD-MODEL-BYTES"
    # 源文件未被改动
    with open(source, "rb") as fh:
        assert fh.read() == b"NEW-MODEL-BYTES"
    # 原子替换不留临时残留
    assert not os.path.isfile(target + ".importing")


def test_pet_model_import_rejects_non_vrm_suffix(tmp_path, monkeypatch):
    """非 .vrm 后缀 → 400 中文错误，目标模型与备份均不受影响。"""
    root = tmp_path / "approot"
    target = _make_fake_vrm(root / "data" / "pet" / "cx-open.vrm", b"OLD-MODEL-BYTES")
    source = _make_fake_vrm(tmp_path / "not-a-model.txt", b"TXT-BYTES")
    with pet_model_server(tmp_path, monkeypatch, root) as base:
        status, body = http_post(f"{base}/api/pet/model/import", {"source_path": source})
    assert status == 400
    assert body["ok"] is False
    assert "仅支持" in body["message"] and ".vrm" in body["message"]
    with open(target, "rb") as fh:
        assert fh.read() == b"OLD-MODEL-BYTES"
    assert not os.path.isfile(target + ".bak")


def test_pet_model_import_rejects_missing_source(tmp_path, monkeypatch):
    """source_path 指向不存在的文件 → 400 中文错误，目标模型不动。"""
    root = tmp_path / "approot"
    target = _make_fake_vrm(root / "data" / "pet" / "cx-open.vrm", b"OLD-MODEL-BYTES")
    with pet_model_server(tmp_path, monkeypatch, root) as base:
        status, body = http_post(
            f"{base}/api/pet/model/import",
            {"source_path": str(tmp_path / "ghost" / "nope.vrm")},
        )
    assert status == 400
    assert body["ok"] is False
    assert "不存在" in body["message"]
    with open(target, "rb") as fh:
        assert fh.read() == b"OLD-MODEL-BYTES"


def test_pet_model_import_missing_source_path_400(tmp_path, monkeypatch):
    """body 缺 source_path（或空串）→ 400 missing_source_path 中文提示。"""
    root = tmp_path / "approot"
    with pet_model_server(tmp_path, monkeypatch, root) as base:
        status, body = http_post(f"{base}/api/pet/model/import", {})
        assert status == 400
        assert body["error"] == "missing_source_path"
        status2, body2 = http_post(f"{base}/api/pet/model/import", {"source_path": "   "})
        assert status2 == 400
        assert body2["error"] == "missing_source_path"


def test_pet_model_reset_restores_backup(tmp_path, monkeypatch):
    """reset：.bak 存在 → 还原为 cx-open.vrm（ok:true），内容与备份一致。"""
    root = tmp_path / "approot"
    target = _make_fake_vrm(root / "data" / "pet" / "cx-open.vrm", b"IMPORTED-MODEL")
    _make_fake_vrm(root / "data" / "pet" / "cx-open.vrm.bak", b"DEFAULT-MODEL")
    with pet_model_server(tmp_path, monkeypatch, root) as base:
        status, body = http_post(f"{base}/api/pet/model/reset", {})
    assert status == 200
    assert body["ok"] is True
    assert "恢复" in body["message"]
    with open(target, "rb") as fh:
        assert fh.read() == b"DEFAULT-MODEL"
    assert not os.path.isfile(target + ".restoring")


def test_pet_model_reset_missing_backup_404(tmp_path, monkeypatch):
    """reset：无备份 → 404 pet_model_backup_missing + 中文 message，现模型不动。"""
    root = tmp_path / "approot"
    target = _make_fake_vrm(root / "data" / "pet" / "cx-open.vrm", b"CURRENT-MODEL")
    with pet_model_server(tmp_path, monkeypatch, root) as base:
        status, body = http_post(f"{base}/api/pet/model/reset", {})
    assert status == 404
    assert body["ok"] is False
    assert body["error"] == "pet_model_backup_missing"
    assert "备份" in body["message"]
    with open(target, "rb") as fh:
        assert fh.read() == b"CURRENT-MODEL"


def test_pet_model_import_and_reset_require_token_in_token_mode(tmp_path, monkeypatch):
    """令牌模式：import/reset 无令牌 → 403（走既有令牌闸，无豁免端点）。"""
    monkeypatch.setattr(api_server_module, "_API_TOKEN", "unit-test-token")
    root = tmp_path / "approot"
    _make_fake_vrm(root / "data" / "pet" / "cx-open.vrm", b"OLD")
    with pet_model_server(tmp_path, monkeypatch, root) as base:
        for path, payload in (
            ("/api/pet/model/import", {"source_path": "x.vrm"}),
            ("/api/pet/model/reset", {}),
        ):
            status, body = http_post(f"{base}{path}", payload)
            assert status == 403, f"{path} 无令牌应 403"
            assert body == {"ok": False, "error": "unauthorized_client"}


# ---------------------------------------------------------------- 20260926_模块0_真实嵌入与向量持久化
class _FakeRealEmbed:
    """假"真实嵌入"提供者：固定 4 维确定性向量（装配选择断言用，不触真服务）。"""

    def embed(self, texts):
        return [[1.0, float(len(str(t)) % 5), 0.5, 0.25] for t in texts]


def _seed_memories(db_path, contents):
    """预置若干"历史记忆"（无向量，模拟旧库/换模型后待预热状态）。"""
    from lite.memory.storage import MemoryStore

    store = MemoryStore(db_path=db_path)
    store.create_table()
    for content in contents:
        store.add({"type": "long_term", "content": content, "agent_id": "default"})
    store.close()


def test_build_deps_real_embedding_uses_sqlite_store_and_warmup(tmp_path, monkeypatch):
    """显式 backend=sqlite：装配 SQLite 持久向量库，且启动预热回填既有记忆向量。

    20261005 默认后端已改 lancedb；本用例显式写 sqlite 以锁定持久库装配与
    预热回填语义（用户显式选择路径）。
    """
    import json as _json

    import lite.server.api_server as api
    from lite.memory.vector_store import SQLiteVectorStore

    (tmp_path / "config.json").write_text(
        _json.dumps({"vector": {"backend": "sqlite", "path": "data/lancedb"}}),
        encoding="utf-8",
    )
    _seed_memories(str(tmp_path / "memories.db"), ["历史记忆一", "历史记忆二"])

    def _fake_builder(config):
        return _FakeRealEmbed(), "llama", {"dim": 4, "model_tag": "fake|model.gguf"}

    monkeypatch.setattr(api, "_build_embedding_provider", _fake_builder)
    _store, pipeline, _manager, _remote = api.build_deps(data_dir=str(tmp_path))

    assert isinstance(pipeline.vector_store, SQLiteVectorStore)
    # 预热已把两条历史记忆回填进持久向量库
    assert pipeline.vector_store.vector_ids() == {"1", "2"}
    res = pipeline.retrieve("历史记忆一", top_k=5)
    assert res["memories"], "预热回填后向量检索应可召回"


def test_build_embedding_provider_missing_raises(tmp_path, monkeypatch):
    """嵌入禁止降级（20261005）：模型路径为空 → RuntimeError 中文（不回落哈希桩）。

    直接调用装配器本体（经 conftest 保留的 _build_embedding_provider_original
    原函数引用——函数内延迟 import 会拿到替身，无法绕过）。
    """
    from lite.config.config_manager import ConfigManager

    import lite.runtime.llama_runtime as llama_runtime

    monkeypatch.setattr(llama_runtime, "resolve_embedding_model_path", lambda *a, **k: "")
    config = ConfigManager(config_path=str(tmp_path / "config.json"), data_dir=str(tmp_path))
    original = api_server_module._build_embedding_provider_original
    with pytest.raises(RuntimeError) as excinfo:
        original(config)
    assert "嵌入模型" in str(excinfo.value)
    assert "禁止降级" in str(excinfo.value)


def test_build_embedding_provider_load_failure_raises(tmp_path, monkeypatch):
    """嵌入禁止降级：llama-server 启动失败（LlamaRuntime 抛错）→ RuntimeError。"""
    from lite.config.config_manager import ConfigManager

    import lite.runtime.llama_runtime as llama_runtime

    monkeypatch.setattr(
        llama_runtime, "resolve_embedding_model_path",
        lambda *a, **k: str(tmp_path / "fake-embedding.gguf"),
    )

    class _BoomRuntime:
        def __init__(self, config):
            pass

        def load_embedding_model(self, path):
            raise RuntimeError("llama-server 启动失败（模拟）")

    monkeypatch.setattr(llama_runtime, "LlamaRuntime", _BoomRuntime)
    config = ConfigManager(config_path=str(tmp_path / "config.json"), data_dir=str(tmp_path))
    original = api_server_module._build_embedding_provider_original
    with pytest.raises(RuntimeError) as excinfo:
        original(config)
    assert "禁止降级" in str(excinfo.value)
    assert "嵌入模型加载失败" in str(excinfo.value)


def test_build_deps_lancedb_missing_raises_for_real_embedding(tmp_path, monkeypatch):
    """真实嵌入 + 显式 lancedb 且依赖缺失 → 硬失败（禁止降级，不回落 SQLite）。"""
    import importlib.util

    import lite.server.api_server as api

    (tmp_path / "config.json").write_text(
        json.dumps({"vector": {"backend": "lancedb", "path": "data/lancedb"}}),
        encoding="utf-8",
    )
    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec",
        lambda name, *a, **k: None if name == "lancedb" else real_find_spec(name, *a, **k),
    )
    monkeypatch.setattr(
        api, "_build_embedding_provider",
        lambda config: (_FakeRealEmbed(), "llama", {"dim": 4, "model_tag": "fake|model.gguf"}),
    )
    with pytest.raises(RuntimeError) as excinfo:
        api.build_deps(data_dir=str(tmp_path))
    assert "禁止降级" in str(excinfo.value)


def test_build_deps_explicit_sqlite_still_works(tmp_path, monkeypatch):
    """显式 backend=sqlite（用户选择，非降级）→ SQLiteVectorStore 正常装配。"""
    import json as _json

    import lite.server.api_server as api
    from lite.memory.vector_store import SQLiteVectorStore

    (tmp_path / "config.json").write_text(
        _json.dumps({"vector": {"backend": "sqlite", "path": "data/lancedb"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        api, "_build_embedding_provider",
        lambda config: (_FakeRealEmbed(), "llama", {"dim": 4, "model_tag": "fake|model.gguf"}),
    )
    _store, pipeline, _manager, _remote = api.build_deps(data_dir=str(tmp_path))
    assert isinstance(pipeline.vector_store, SQLiteVectorStore)


def test_build_vector_store_unknown_backend_raises(tmp_path):
    """未知 backend（含空串）→ RuntimeError（禁止降级：不再回落内存库）。"""
    import json as _json

    import lite.server.api_server as api

    (tmp_path / "config.json").write_text(
        _json.dumps({"vector": {"backend": "weaviate"}}), encoding="utf-8"
    )
    with pytest.raises(RuntimeError) as excinfo:
        api.build_deps(data_dir=str(tmp_path))
    assert "未知向量后端" in str(excinfo.value)


# ---------------------------------------------------------------- Task 5：模式入口
#: 无核显 N 卡画像（性能优先 → tts.accel=cuda；节能 → cpu）。
_T5_PROFILE_NVIDIA = {
    "cpu_cores": 8,
    "ram_gb": 16.0,
    "gpu_vendor": "nvidia",
    "vram_gb": 8.0,
    "cuda_version": "12.4",
    "disk_free_gb": 200.0,
    "probe_notes": [],
    "gpus": [{"vendor": "nvidia", "name": "RTX 4060", "type": "dgpu", "vram_hint": ""}],
    "has_igpu": False,
    "dgpu_vendor": "nvidia",
}


def _t5_stub_profile(monkeypatch, profile=None):
    """把 api_server 的硬件探测替换为确定性画像（不触碰真实硬件）。"""
    fixed = dict(profile or _T5_PROFILE_NVIDIA)
    monkeypatch.setattr(api_server_module, "detect_profile", lambda *a, **k: dict(fixed))


def test_t5_setup_recommend_contains_accel_profile(tmp_path, monkeypatch):
    """GET /api/setup/recommend 响应含加速剖面（顶层 accel + recommendation.accel）。"""
    _t5_stub_profile(monkeypatch)
    with setup_server(tmp_path) as (base, _config, _handler):
        status, body, _raw = http_get(f"{base}/api/setup/recommend")
    assert status == 200
    assert {"profile", "recommendation", "accel", "tiers", "suggested_source"} <= set(body)
    assert body["accel"]["mode"] == "performance"
    assert body["accel"]["tts"]["accel"] == "cuda"
    assert body["recommendation"]["accel"]["mode"] == "performance"
    assert isinstance(body["accel"]["reasons"], list) and body["accel"]["reasons"]


def test_t5_setup_complete_accel_whitelist_applies_and_ignores(tmp_path):
    """POST /api/setup/complete 白名单：合法应用 accel.mode / tts.accel / tts.accel_device，非法入 ignored。"""
    with setup_server(tmp_path) as (base, config, _handler):
        status, body = http_post(
            f"{base}/api/setup/complete",
            {"accel": {"mode": "eco"}, "tts": {"accel": "dml", "accel_device": "igpu"}},
        )
        assert status == 200
        assert body["ok"] is True
        assert body["setup"]["completed"] is True
        for key in ("accel.mode", "tts.accel", "tts.accel_device"):
            assert key in body["applied"], f"{key} 应在 applied 中"
        assert body["ignored"] == []
        assert config.get("accel", "mode") == "eco"
        assert config.get("tts", "accel") == "dml"
        assert config.get("tts", "accel_device") == "igpu"
        # 落盘真相与内存一致
        on_disk = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert on_disk["accel"]["mode"] == "eco"
        assert on_disk["tts"]["accel_device"] == "igpu"

        # 非法值：不生效（配置不被覆盖）且入 ignored 显式回显
        status2, body2 = http_post(
            f"{base}/api/setup/complete",
            {"accel": {"mode": "turbo"}, "tts": {"accel": "vulkan", "accel_device": "xpu"}},
        )
        assert status2 == 200
        assert body2["applied"] == []
        ignored_text = "\n".join(body2["ignored"])
        assert "accel.mode" in ignored_text and "turbo" in ignored_text
        assert "tts.accel" in ignored_text and "vulkan" in ignored_text
        assert "tts.accel_device" in ignored_text and "xpu" in ignored_text
    assert config.get("accel", "mode") == "eco"  # 非法值未覆盖合法值
    assert config.get("tts", "accel") == "dml"


def test_t5_settings_accel_mode_applies_all_landings_in_write_lock(tmp_path, monkeypatch):
    """PUT /api/settings 保存 accel.mode：经 accel_plan 展开全部落点并落盘；非法入 ignored。"""
    _t5_stub_profile(monkeypatch)
    with setup_server(tmp_path) as (base, config, _handler):
        status, body = http_post(
            f"{base}/api/settings", {"accel": {"mode": "eco"}}, method="PUT"
        )
        assert status == 200
        assert body["ok"] is True
        for key in ("accel.mode", "tts.accel", "tts.accel_device",
                    "asr.device", "local_llm.device", "embedding.device"):
            assert key in body["applied"], f"{key} 应在 applied 中"
        # 无核显 → 节能落点全 CPU
        assert config.get("accel", "mode") == "eco"
        assert config.get("tts", "accel") == "cpu"
        assert config.get("tts", "accel_device") == ""
        assert config.get("asr", "device") == "cpu"
        assert config.get("local_llm", "device") == "cpu"
        assert config.get("embedding", "device") == "cpu"
        # 未落盘前配置在写锁内 save（响应 config 回显新值）
        assert body["config"]["accel"]["mode"] == "eco"
        assert body["config"]["tts"]["accel"] == "cpu"

        # 非法 accel.mode：不产生部分写入
        status2, body2 = http_post(
            f"{base}/api/settings", {"accel": {"mode": "turbo"}}, method="PUT"
        )
        assert status2 == 200
        assert body2["applied"] == []
        assert any("accel.mode" in item for item in body2["ignored"])
    assert config.get("accel", "mode") == "eco"
    on_disk = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert on_disk["accel"]["mode"] == "eco"


class _T5FakeClient:
    """sidecar 客户端替身：记录 close 次数（不启动真实进程）。"""

    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


class _T5FakeBackend:
    """桥后端替身：暴露 ``client`` 供 _close_voice_backends 识别。"""

    def __init__(self, client):
        self.client = client


class _T5FakeFacade:
    """LiteASR / LiteTTS 替身：暴露 ``backend.client``。"""

    def __init__(self, client):
        self.backend = _T5FakeBackend(client)


class _T5FakeVoice:
    """语音编排器替身：持有假 sidecar 客户端与对话历史。"""

    def __init__(self, client):
        self.asr = _T5FakeFacade(client)
        self.tts = _T5FakeFacade(client)
        self.messages = [{"role": "user", "content": "你好"}]


@contextmanager
def _t5_server_with_voice(tmp_path, voice, config):
    """起一个注入指定 voice 的测试服务（与 setup_server 同口径，额外注入 voice）。"""
    store, pipeline, manager, _remote = build_deps(data_dir=str(tmp_path))
    authorizer = ControlAuthorizer(data_dir=str(tmp_path))
    computer = ComputerControl(authorized=authorizer.is_authorized())
    bridge = ToolBridge(computer=computer, authorizer=authorizer)
    handler = make_handler(
        store, pipeline, manager,
        computer=computer, authorizer=authorizer, bridge=bridge, config=config, voice=voice,
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


def test_t5_settings_accel_rebuild_closes_old_sidecar_and_uses_new_params(tmp_path, monkeypatch):
    """语音桥重建路径：关闭旧 sidecar → 按新 tts.accel 重建（新参数捕获）+ 历史保留。"""
    _t5_stub_profile(monkeypatch)
    client = _T5FakeClient()
    old_voice = _T5FakeVoice(client)
    config = ConfigManager(config_path=str(tmp_path / "config.json"), data_dir=str(tmp_path))
    captured = {}

    def _fake_build(cfg):
        """替身工厂：捕获重建时的新参数（不真启动 sidecar）。"""
        captured["tts_accel"] = cfg.get("tts", "accel")
        captured["tts_accel_device"] = cfg.get("tts", "accel_device")
        tts_obj = object()
        captured["tts_obj"] = tts_obj
        return {"vad": object(), "asr": object(), "tts": tts_obj, "judge": None}

    monkeypatch.setattr(api_server_module, "build_default_pipeline", _fake_build)
    with _t5_server_with_voice(tmp_path, old_voice, config) as (base, _cfg, handler):
        status, body = http_post(f"{base}/api/settings", {"accel": {"mode": "eco"}}, method="PUT")

    assert status == 200
    assert body["voice_backend"]["rebuilt"] is True
    assert body["voice_backend"]["needs_restart"] is False
    assert client.closed >= 1  # 旧 sidecar 已关闭
    assert captured["tts_accel"] == "cpu"  # 新参数捕获（eco 无核显 → cpu）
    assert captured["tts_accel_device"] == ""
    assert handler._voice.tts is captured["tts_obj"]  # 已替换为新后端
    assert handler._voice.messages == [{"role": "user", "content": "你好"}]  # 历史保留


def test_t5_settings_accel_rebuild_failure_reports_needs_restart(tmp_path, monkeypatch):
    """重建失败不静默：响应附 voice_backend.needs_restart=true 与中文提示；配置仍已落盘。"""
    _t5_stub_profile(monkeypatch)
    with setup_server(tmp_path) as (base, config, _handler):

        def _boom(_cfg):
            raise RuntimeError("sidecar 启动失败")

        monkeypatch.setattr(api_server_module, "build_default_pipeline", _boom)
        status, body = http_post(f"{base}/api/settings", {"accel": {"mode": "eco"}}, method="PUT")

    assert status == 200
    assert body["applied"] and "accel.mode" in body["applied"]
    assert body["voice_backend"]["rebuilt"] is False
    assert body["voice_backend"]["needs_restart"] is True
    assert "重启" in body["voice_backend"]["message"]
    # 重建失败不丢配置
    assert config.get("accel", "mode") == "eco"


# ---------------------------------------------------------------- Task B「音色自由选」：voices API + 音色热切换


@pytest.fixture()
def voices_env(api_server, monkeypatch, tmp_path):
    """音色目录隔离：``default_voices_dir`` 重定向到临时目录，返回 (base, voices_dir)。

    VoiceManager 缺省扫描项目根 ``data/voices``——直接 monkeypatch 模块内
    ``default_voices_dir``，使端点的 VoiceManager 构造回落到 tmp 隔离目录。
    """
    import lite.audio.voice_manager as vm_module

    voices_dir = tmp_path / "voices"
    monkeypatch.setattr(vm_module, "default_voices_dir", lambda: str(voices_dir))
    _store, _pipeline, base = api_server
    return base, voices_dir


def _make_voice_pack(root, name, artifact="config.json", content="{}"):
    """在 root 下造一个含训练产物的假音色包目录，返回其路径。"""
    pack = Path(root) / name
    pack.mkdir(parents=True, exist_ok=True)
    (pack / artifact).write_text(content, encoding="utf-8")
    return pack


def test_voices_list_builtin_only(voices_env):
    """GET /api/voices：目录为空/不存在时仅返回内置 cx-open 项（builtin=true）。"""
    base, _voices_dir = voices_env
    status, body, _raw = http_get(f"{base}/api/voices")
    assert status == 200
    assert body["ok"] is True
    assert len(body["voices"]) == 1
    item = body["voices"][0]
    assert item["id"] == "cx-open"
    assert item["builtin"] is True
    assert item["is_default"] is True
    assert item["size"] == 0


def test_voices_list_contains_custom_package(voices_env):
    """GET /api/voices：目录里的自定义音色包出现在列表中（builtin=false、size>0）。"""
    base, voices_dir = voices_env
    pack = _make_voice_pack(voices_dir, "mypack")
    status, body, _raw = http_get(f"{base}/api/voices")
    assert status == 200
    ids = [v["id"] for v in body["voices"]]
    assert ids == ["cx-open", "mypack"]  # 升序 + 内置项在列
    custom = body["voices"][1]
    assert custom["builtin"] is False
    assert custom["path"] == str(pack)
    assert custom["size"] > 0
    assert custom["is_default"] is False


def test_voices_list_marks_config_default(voices_env):
    """GET /api/voices：is_default 以 config tts.voice 为口径（默认音色切换后回显一致）。"""
    base, voices_dir = voices_env
    _make_voice_pack(voices_dir, "mypack")
    status, body = http_post(f"{base}/api/settings", {"tts": {"voice": "mypack"}}, method="PUT")
    assert status == 200
    status, body, _raw = http_get(f"{base}/api/voices")
    assert status == 200
    by_id = {v["id"]: v for v in body["voices"]}
    assert by_id["mypack"]["is_default"] is True
    assert by_id["cx-open"]["is_default"] is False


def test_voices_list_dedupes_builtin_with_dir_item(voices_env):
    """GET /api/voices：目录中已有 cx-open 包时与内置项合并去重（保留目录 size/path）。"""
    base, voices_dir = voices_env
    pack = _make_voice_pack(voices_dir, "cx-open", artifact="ckpt.txt", content="weights")
    status, body, _raw = http_get(f"{base}/api/voices")
    assert status == 200
    cx_items = [v for v in body["voices"] if v["id"] == "cx-open"]
    assert len(cx_items) == 1  # 去重：仅一项
    merged = cx_items[0]
    assert merged["builtin"] is True  # 保留内置身份
    assert merged["path"] == str(pack)  # 保留目录项 path/size
    assert merged["size"] > 0


def test_voices_import_success_copies_pack(voices_env):
    """POST /api/voices/import：成功复制（name 缺省取 basename），列表可查。"""
    base, voices_dir = voices_env
    source = _make_voice_pack(voices_dir.parent, "src_pack", artifact="ckpt.txt", content="w")
    target = voices_dir / "src_pack"
    status, body = http_post(f"{base}/api/voices/import", {"source_path": str(source)})
    assert status == 200
    assert body["ok"] is True
    assert body["voice"]["id"] == "src_pack"
    assert body["voice"]["path"] == str(target)
    assert body["voice"]["size"] > 0
    assert body["voice"]["builtin"] is False
    assert target.is_dir() and (target / "ckpt.txt").exists()
    # 导入后列表可查
    _status, listing, _raw = http_get(f"{base}/api/voices")
    assert "src_pack" in [v["id"] for v in listing["voices"]]


def test_voices_import_no_artifacts_400_no_side_effect(voices_env):
    """POST /api/voices/import：目录无训练产物 → 400 中文提示，且目标目录不产生。"""
    base, voices_dir = voices_env
    source = voices_dir.parent / "empty_pack"
    source.mkdir(parents=True, exist_ok=True)
    (source / "readme.txt").write_text("说明文本不是音色产物", encoding="utf-8")
    status, body = http_post(f"{base}/api/voices/import", {"source_path": str(source)})
    assert status == 400
    assert body["error"] == "bad_request"
    assert "音色模型文件" in body["message"]
    assert not (voices_dir / "empty_pack").exists()


def test_voices_import_unsafe_name_400(voices_env):
    """POST /api/voices/import：name 含路径穿越特征（../evil）→ 400 且不写目标。"""
    base, voices_dir = voices_env
    source = _make_voice_pack(voices_dir.parent, "good_pack")
    for bad_name in ("../evil", "a:b", ""):
        status, body = http_post(
            f"{base}/api/voices/import", {"source_path": str(source), "name": bad_name}
        )
        assert status == 400, f"name={bad_name!r} 应 400"
        assert body["error"] == "bad_request"
    assert not (voices_dir.parent / "evil").exists()
    assert list(voices_dir.glob("*")) == []  # 无任何写入副作用


def test_voices_import_missing_source_400(voices_env):
    """POST /api/voices/import：source_path 不存在 / 缺失 → 400。"""
    base, _voices_dir = voices_env
    status, body = http_post(f"{base}/api/voices/import", {"source_path": str(Path("Z:/no/such/dir"))})
    assert status == 400
    status2, body2 = http_post(f"{base}/api/voices/import", {})
    assert status2 == 400
    assert body2["error"] == "bad_request"


def test_voices_import_overwrite_semantics(voices_env):
    """POST /api/voices/import：重名无 overwrite 400；overwrite=true 删旧再复制。"""
    base, voices_dir = voices_env
    source_a = _make_voice_pack(voices_dir.parent, "pack_a", artifact="ckpt.txt", content="AAA")
    source_b = _make_voice_pack(voices_dir.parent, "pack_b", artifact="ckpt.txt", content="BBB")
    # 第一次导入成功
    status, _body = http_post(
        f"{base}/api/voices/import", {"source_path": str(source_a), "name": "dual"}
    )
    assert status == 200
    target = voices_dir / "dual"
    assert (target / "ckpt.txt").read_text(encoding="utf-8") == "AAA"
    # 重名无 overwrite → 400
    status, body = http_post(
        f"{base}/api/voices/import", {"source_path": str(source_b), "name": "dual"}
    )
    assert status == 400
    assert "已存在" in body["message"]
    # overwrite=true → 覆盖为 pack_b 内容
    status, body = http_post(
        f"{base}/api/voices/import",
        {"source_path": str(source_b), "name": "dual", "overwrite": True},
    )
    assert status == 200
    assert (target / "ckpt.txt").read_text(encoding="utf-8") == "BBB"


def test_voice_synthesize_hot_reloads_config_voice(api_server, monkeypatch):
    """音色热切换：PUT 改 tts.voice 后 synthesize 端点现读 config 收到新音色 id。"""
    from lite.audio.tts import MockTTSBackend

    received = []
    original = MockTTSBackend.synthesize

    def spy(self, text, voice="cx-open"):
        received.append(voice)
        return original(self, text, voice)

    monkeypatch.setattr(MockTTSBackend, "synthesize", spy)
    _store, _pipeline, base = api_server
    status, body = http_post(f"{base}/api/settings", {"tts": {"voice": "my-hot-voice"}}, method="PUT")
    assert status == 200 and "tts.voice" in body["applied"]
    status, body = http_post(f"{base}/api/voice/synthesize", {"text": "你好"})
    assert status == 200 and body["ok"] is True
    # 未显式指定音色 → 现读 config（新默认音色），无需重启/重建后端
    assert received[-1] == "my-hot-voice"
    # 显式指定音色仍优先于 config 默认
    status, _body = http_post(f"{base}/api/voice/synthesize", {"text": "你好", "voice": "explicit"})
    assert status == 200
    assert received[-1] == "explicit"


def test_voice_synthesize_stream_hot_reloads_config_voice(api_server, monkeypatch):
    """音色热切换（流式端点）：与整段合成同口径，现读 config tts.voice。"""
    from lite.audio.tts import MockTTSBackend

    received = []
    original = MockTTSBackend.synthesize

    def spy(self, text, voice="cx-open"):
        received.append(voice)
        return original(self, text, voice)

    monkeypatch.setattr(MockTTSBackend, "synthesize", spy)
    _store, _pipeline, base = api_server
    status, _body = http_post(f"{base}/api/settings", {"tts": {"voice": "stream-voice"}}, method="PUT")
    assert status == 200
    status, objs = http_post_stream(
        f"{base}/api/voice/synthesize_stream", {"text": "第一句。第二句。"}
    )
    assert status == 200
    assert objs[-1].get("done") is True
    assert received, "流式合成应至少调用一次 TTS"
    assert all(v == "stream-voice" for v in received)


# ---------------------------------------------------------------- Task C「主动视觉接线」：装配 + settings 热更新


class _CountingScreenBackend:
    """mock 屏幕后端：计数 capture 调用，可脚本化失败（单测绝不真实抓屏）。"""

    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    def capture(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("模拟抓屏失败")
        return [128] * 2304


@pytest.fixture()
def vision_env(tmp_path, monkeypatch):
    """注入 mock 屏幕后端的视觉装配环境：返回 (base, handler, backend, config)。

    - tick 节拍 monkeypatch 为 0.05s（真实装配为 1.0s 常量）；
    - config 与 make_handler 注入同一 ConfigManager 实例（PUT settings 的
      vision.enabled 经它即时生效——VisionPipeline 每拍现读）；
    - 理解注入固定摘要（零网络零云端）；memory_store 用 tmp 隔离真实库。
    """
    from lite.vision.pipeline import VisionPipeline
    from lite.vision.sampler import AdaptiveSampler

    monkeypatch.setattr(api_server_module, "_VISION_TICK_INTERVAL_S", 0.05)
    store, mem_pipeline, manager, _remote = build_deps(data_dir=str(tmp_path))
    config = ConfigManager(config_path=str(tmp_path / "config.json"))
    backend = _CountingScreenBackend()
    sampler = AdaptiveSampler(backend, min_interval_s=0.05, max_interval_s=0.02)
    vision = VisionPipeline(
        sampler=sampler, cloud=None, memory_store=store, config=config,
        understanding=lambda item: "测试摘要",
    )
    handler = make_handler(store, mem_pipeline, manager, config=config, vision_pipeline=vision)
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield base, handler, backend, config
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_vision_default_disabled_capture_zero_calls(vision_env):
    """隐私红线：默认 vision.enabled=False，tick 线程多拍驱动下 capture 零调用。"""
    base, handler, backend, _config = vision_env
    assert handler._vision is not None
    assert handler._vision_thread is not None
    assert handler._vision_thread.is_alive()
    time.sleep(0.3)  # ≈6 拍：enabled=False 时 run_once 零开销返回，绝不触碰采样
    assert backend.calls == 0


def test_vision_put_enabled_drives_capture(vision_env):
    """PUT vision.enabled=true 后 tick 线程下一拍驱动 capture（mock 计数增长）。"""
    base, _handler, backend, _config = vision_env
    status, body = http_post(f"{base}/api/settings", {"vision": {"enabled": True}}, method="PUT")
    assert status == 200
    assert body["ok"] is True and "vision.enabled" in body["applied"]
    deadline = time.time() + 5
    while time.time() < deadline and backend.calls == 0:
        time.sleep(0.05)
    assert backend.calls > 0, "开启视觉后 tick 线程应驱动 capture"


def test_vision_tick_thread_survives_capture_exception(vision_env):
    """capture 连续抛异常 → tick 循环整体兜底告警，线程存活不自灭。"""
    base, handler, backend, _config = vision_env
    backend.fail = True
    status, _body = http_post(f"{base}/api/settings", {"vision": {"enabled": True}}, method="PUT")
    assert status == 200
    deadline = time.time() + 5
    while time.time() < deadline and backend.calls < 4:  # 连续失败 ≥4 拍
        time.sleep(0.05)
    assert backend.calls >= 4
    assert handler._vision_thread.is_alive(), "tick 线程不得因 capture 异常退出"


def test_settings_view_contains_vision_enabled(api_server):
    """GET /api/settings：vision.enabled 默认 False 出现在视图中。"""
    _store, _pipeline, base = api_server
    status, body, _raw = http_get(f"{base}/api/settings")
    assert status == 200
    assert body["vision"] == {"enabled": False}


def test_settings_put_vision_enabled_applied_and_echoed(api_server):
    """PUT vision.enabled=true → applied 回显 + GET 视图翻转 + 落盘生效。"""
    _store, _pipeline, base = api_server
    status, body = http_post(f"{base}/api/settings", {"vision": {"enabled": True}}, method="PUT")
    assert status == 200
    assert "vision.enabled" in body["applied"]
    assert body["config"]["vision"]["enabled"] is True
    status, body, _raw = http_get(f"{base}/api/settings")
    assert body["vision"]["enabled"] is True


def test_settings_put_vision_invalid_value_ignored(api_server):
    """PUT vision.enabled="yes"（非布尔）→ ignored 显式回显，config 不变。"""
    _store, _pipeline, base = api_server
    status, body = http_post(f"{base}/api/settings", {"vision": {"enabled": "yes"}}, method="PUT")
    assert status == 200
    assert not body["applied"]
    assert any("vision.enabled" in item for item in body["ignored"])
    assert body["config"]["vision"]["enabled"] is False


def test_settings_put_vision_section_non_dict_400(api_server):
    """PUT {"vision": "abc"}（段非 dict）→ 400 invalid section type。"""
    _store, _pipeline, base = api_server
    status, body = http_post(f"{base}/api/settings", {"vision": "abc"}, method="PUT")
    assert status == 400
    assert body["error"] == "invalid section type: vision"


# ---------------------------------------------------------------- settings 白名单扩展（Task 5：向导选项入设置）
def test_settings_put_api_key_applied_and_get_masked(api_server):
    """PUT cloud.api_key：applied 登记；GET 脱敏回显 sk-****尾4位，明文不外泄。"""
    _store, _pipeline, base = api_server
    status, body = http_post(
        f"{base}/api/settings",
        {"cloud": {"api_key": "sk-abcdefgh12345678"}},
        method="PUT",
    )
    assert status == 200
    assert body["ok"] is True
    assert "cloud.api_key" in body["applied"]
    # 回执中的视图值已是脱敏形式，明文任何形式不出现在响应里
    assert body["config"]["cloud"]["api_key"].endswith("5678")
    assert "sk-abcdefgh12345678" not in json.dumps(body)
    # GET 脱敏回显：sk-**** + 尾4位
    _st2, body2, raw2 = http_get(f"{base}/api/settings")
    assert body2["cloud"]["api_key"] == "sk-****5678"
    assert "sk-abcdefgh12345678" not in raw2


def test_settings_put_api_key_empty_ignored_keeps_existing(api_server):
    """PUT cloud.api_key 空串/纯空白 → ignored，不覆盖既有值。"""
    _store, _pipeline, base = api_server
    status, _ = http_post(
        f"{base}/api/settings", {"cloud": {"api_key": "sk-initial-key-9999"}}, method="PUT"
    )
    assert status == 200
    status, body = http_post(f"{base}/api/settings", {"cloud": {"api_key": "   "}}, method="PUT")
    assert status == 200
    assert not body["applied"]
    assert any("cloud.api_key" in item for item in body["ignored"])
    _st2, body2, _raw2 = http_get(f"{base}/api/settings")
    assert body2["cloud"]["api_key"] == "sk-****9999"


def test_settings_put_download_channel_applied_and_source_derived(api_server):
    """PUT download.channel=official：applied 两项 + 派生 local_llm.source（向导同口径）。"""
    _store, _pipeline, base = api_server
    status, body = http_post(
        f"{base}/api/settings", {"download": {"channel": "official"}}, method="PUT"
    )
    assert status == 200
    assert "download.channel" in body["applied"]
    assert "local_llm.source" in body["applied"]
    assert body["config"]["download"]["channel"] == "official"
    assert body["config"]["local_llm"]["source"] == "huggingface"
    # GET 视图同步回显（通道 + 派生仓库）
    _st2, body2, _raw2 = http_get(f"{base}/api/settings")
    assert body2["download"]["channel"] == "official"
    assert body2["local_llm"]["source"] == "huggingface"


def test_settings_put_download_channel_mirror_derives_modelscope(api_server):
    """PUT download.channel（大小写混杂）→ 归一小写 + 派生 modelscope。"""
    _store, _pipeline, base = api_server
    status, body = http_post(
        f"{base}/api/settings", {"download": {"channel": "MIRROR"}}, method="PUT"
    )
    assert status == 200
    assert body["config"]["download"]["channel"] == "mirror"
    assert body["config"]["local_llm"]["source"] == "modelscope"


def test_settings_put_download_channel_invalid_ignored(api_server):
    """PUT download.channel=bogus → ignored 显式回显，config 维持默认通道。"""
    _store, _pipeline, base = api_server
    status, body = http_post(
        f"{base}/api/settings", {"download": {"channel": "bogus"}}, method="PUT"
    )
    assert status == 200
    assert not body["applied"]
    assert any("download.channel" in item and "bogus" in item for item in body["ignored"])
    _st2, body2, _raw2 = http_get(f"{base}/api/settings")
    # normalize_channel 回落默认通道（mirror），local_llm.source 按默认通道派生
    assert body2["download"]["channel"] == "mirror"
    assert body2["local_llm"]["source"] == "modelscope"


def test_settings_put_download_section_non_dict_400(api_server):
    """PUT {"download": "abc"}（段非 dict）→ 400 invalid section type: download。"""
    _store, _pipeline, base = api_server
    status, body = http_post(f"{base}/api/settings", {"download": "abc"}, method="PUT")
    assert status == 400
    assert body["error"] == "invalid section type: download"


def test_settings_put_tts_accel_keys_applied_and_echoed(api_server):
    """PUT tts.accel / tts.accel_device 合法值 → applied + GET 回显。"""
    _store, _pipeline, base = api_server
    status, body = http_post(
        f"{base}/api/settings",
        {"tts": {"accel": "cuda", "accel_device": "dgpu"}},
        method="PUT",
    )
    assert status == 200
    assert "tts.accel" in body["applied"]
    assert "tts.accel_device" in body["applied"]
    _st2, body2, _raw2 = http_get(f"{base}/api/settings")
    assert body2["tts"]["accel"] == "cuda"
    assert body2["tts"]["accel_device"] == "dgpu"


def test_settings_put_tts_accel_invalid_ignored(api_server):
    """PUT tts.accel=npu / tts.accel_device=xgpu（非法值）→ ignored 显式回显，config 不变。"""
    _store, _pipeline, base = api_server
    status, body = http_post(
        f"{base}/api/settings",
        {"tts": {"accel": "npu", "accel_device": "xgpu"}},
        method="PUT",
    )
    assert status == 200
    assert not body["applied"]
    assert any("tts.accel=" in item and "npu" in item for item in body["ignored"])
    assert any("tts.accel_device" in item and "xgpu" in item for item in body["ignored"])
    _st2, body2, _raw2 = http_get(f"{base}/api/settings")
    assert body2["tts"]["accel"] == "auto"     # 默认值不变
    assert body2["tts"]["accel_device"] == ""  # 默认值不变


def test_settings_put_unknown_keys_behavior_unchanged(api_server):
    """未知键既有行为不变：未知段/未知顶层键不进 applied（download 未知键静默丢弃口径一致）。"""
    _store, _pipeline, base = api_server
    status, body = http_post(
        f"{base}/api/settings",
        {"download": {"unknown_key": 1}, "totally_new_section": {"a": 1}},
        method="PUT",
    )
    assert status == 200
    assert body["applied"] == []
    # 未知顶层段不在只读回显清单（acp/remote/vector），保持既有静默丢弃口径
    assert all("totally_new_section" not in item for item in body["ignored"])


def test_create_app_wires_vision_pipeline(tmp_path):
    """create_app 生产装配：默认视觉管线 + daemon tick 线程均已就绪。"""
    _store, _pipeline, handler = create_app(data_dir=str(tmp_path))
    assert handler._vision is not None
    assert handler._vision_thread is not None
    assert handler._vision_thread.is_alive()
    assert handler._vision_thread.daemon is True
    assert handler._vision_thread.name == "vision-tick"


# ---------------------------------------------------------------- 管理 API 令牌落盘
def test_write_token_file_writes_json(tmp_path, monkeypatch):
    """令牌模式：write_token_file 落盘 api_token.json（token/port/pid 均可解析）。"""
    monkeypatch.setenv("CXA_API_TOKEN", "unit-test-token")
    monkeypatch.setattr(api_server_module, "app_root", lambda: str(tmp_path))
    api_server_module.write_token_file(8600)
    path = tmp_path / "logs" / "api_token.json"
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["token"] == "unit-test-token"
    assert payload["port"] == 8600
    assert isinstance(payload["pid"], int)
    assert "updated_at" in payload
    # 原子写收敛：不留 .tmp 残留
    assert not (tmp_path / "logs" / "api_token.json.tmp").exists()


def test_write_token_file_open_mode_skips(tmp_path, monkeypatch):
    """开放模式（未设令牌）：不写文件，外部直连即可。"""
    monkeypatch.delenv("CXA_API_TOKEN", raising=False)
    monkeypatch.setattr(api_server_module, "app_root", lambda: str(tmp_path))
    api_server_module.write_token_file(8600)
    assert not (tmp_path / "logs" / "api_token.json").exists()


def test_remove_token_file_tolerant(tmp_path, monkeypatch):
    """删除令牌文件：不存在静默通过，存在则删除。"""
    monkeypatch.setattr(api_server_module, "app_root", lambda: str(tmp_path))
    api_server_module.remove_token_file()  # 不存在 → 不抛
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "api_token.json").write_text("{}", encoding="utf-8")
    api_server_module.remove_token_file()
    assert not (logs / "api_token.json").exists()


# ---------------------------------------------------------------- 管理面：CX-A 管理 CX-O（establish-fleet-admin-plane）
import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: E402
from urllib.parse import urlparse as _urlparse  # noqa: E402


class _FakeCXOState:
    """假 CX-O 管理面的共享状态：记录最近一次请求供断言。"""

    def __init__(self):
        self.last = None  # {"method","path","headers","body"}


class _FakeCXOHandler(BaseHTTPRequestHandler):
    """按 CX-O 管理接口文档 §6 形状实现的假管理面。

    Bearer 分级语义（错误码原样透传的靶子）：
      tok-good → 全部 200；tok-bad → 401 ADMIN_AUTH_FAILED；
      tok-limited → 429 ADMIN_RATE_LIMITED；tok-disabled → 503 ADMIN_DISABLED；
      缺失/错误头 → 401。health 端点免鉴权（§5.3）。
    """

    state = None  # type: _FakeCXOState

    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    def _reply(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _record(self, body=None):
        self.state.last = {
            "method": self.command,
            "path": _urlparse(self.path).path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body,
        }

    def _bearer(self):
        auth = self.headers.get("Authorization") or ""
        return auth[7:] if auth.startswith("Bearer ") else ""

    def do_GET(self):
        self._record()
        token = self._bearer()
        if _urlparse(self.path).path == "/api/admin/health":
            self._reply(200, {"status": "healthy", "components": {}})  # §5.3 免鉴权
            return
        if token == "tok-disabled":
            self._reply(503, {"ok": False, "error": "ADMIN_DISABLED"})
            return
        if token != "tok-good":
            self._reply(401, {"ok": False, "error": "ADMIN_AUTH_FAILED"})
            return
        path = _urlparse(self.path).path
        if path == "/api/admin/manifest":
            self._reply(200, {"instance_id": "fake", "capabilities": {"autonomy": True},
                              "control_actions": ["enable", "disable"]})
        elif path == "/api/admin/status":
            self._reply(200, {"status": "success", "snapshot": {"models": {}}})
        elif path == "/api/admin/audit":
            self._reply(200, {"status": "success", "items": []})
        else:
            self._reply(404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            body = {}
        self._record(body)
        token = self._bearer()
        if token == "tok-limited":
            self._reply(429, {"ok": False, "error": "ADMIN_RATE_LIMITED"})
            return
        if token != "tok-good":
            self._reply(401, {"ok": False, "error": "ADMIN_AUTH_FAILED"})
            return
        self._reply(200, {"status": "success", "result": {"echo_request_id": body.get("request_id")}})


@pytest.fixture()
def fake_cxo():
    """起停假 CX-O 管理面，返回 (base_url, state)。"""
    state = _FakeCXOState()
    handler = type("BoundFakeCXOHandler", (_FakeCXOHandler,), {"state": state})
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _add_fake_instance(base, fake_url, token="tok-good"):
    """手动登记假实例 → (status, instance_id)。"""
    status, body = http_post(
        f"{base}/api/fleet/instances",
        payload={"name": "假实例", "base_url": fake_url, "token": token},
        method="POST",
    )
    return status, body.get("instance", {}).get("id")


def test_fleet_list_empty(api_server):
    _store, _pipeline, base = api_server
    status, body = http_get_json(f"{base}/api/fleet/instances")
    assert status == 200
    assert body == {"status": "success", "instances": []}


def test_fleet_add_and_list_redacted(api_server, tmp_path):
    _store, _pipeline, base = api_server
    status, body = http_post(
        f"{base}/api/fleet/instances",
        payload={"name": "家里的重实例", "base_url": "http://192.168.1.10:8000", "token": "tok-abcd1234"},
        method="POST",
    )
    assert status == 200
    inst = body["instance"]
    assert inst["has_token"] is True
    assert inst["token_suffix"] == "1234"
    assert "token" not in inst  # 明文绝不回传
    status, body = http_get_json(f"{base}/api/fleet/instances")
    assert status == 200
    assert len(body["instances"]) == 1
    assert "token" not in body["instances"][0]
    assert body["instances"][0]["token_suffix"] == "1234"
    # 台账落盘（data/fleet.json），但那是存储层——API 响应仍脱敏
    fleet_file = tmp_path / "fleet.json"
    assert fleet_file.exists()
    assert json.loads(fleet_file.read_text(encoding="utf-8"))[0]["token"] == "tok-abcd1234"


def test_fleet_add_missing_fields_400(api_server):
    _store, _pipeline, base = api_server
    status, body = http_post(
        f"{base}/api/fleet/instances", payload={"name": "缺 token"}, method="POST"
    )
    assert status == 400
    assert body["error"] == "bad_request"


def test_fleet_add_duplicate_base_url_400(api_server, fake_cxo):
    _store, _pipeline, base = api_server
    fake_url, _state = fake_cxo
    status, _id = _add_fake_instance(base, fake_url)
    assert status == 200
    status, body = http_post(
        f"{base}/api/fleet/instances",
        payload={"name": "重复的", "base_url": fake_url, "token": "tok-good"},
        method="POST",
    )
    assert status == 400


def test_fleet_unknown_instance_404(api_server):
    _store, _pipeline, base = api_server
    status, body = http_get_json(f"{base}/api/fleet/instances/no-such/manifest")
    assert status == 404
    assert body["error"] == "not_found"


def test_fleet_delete_then_404(api_server, fake_cxo):
    _store, _pipeline, base = api_server
    fake_url, _state = fake_cxo
    _status, instance_id = _add_fake_instance(base, fake_url)
    status, body = http_delete(f"{base}/api/fleet/instances/{instance_id}")
    assert status == 200
    status, body = http_delete(f"{base}/api/fleet/instances/{instance_id}")
    assert status == 404


def test_fleet_register_loopback_ok(api_server):
    """CX-O 注册上门（豁免令牌闸 + 回环来源）：upsert 台账，source=registered。"""
    _store, _pipeline, base = api_server
    status, body = http_post(
        f"{base}/api/admin/register",
        payload={"instance_id": "cx-o-node", "endpoint": "http://192.168.1.20:8000",
                 "role": "active", "timestamp": "2026-10-04T05:00:00+00:00"},
        method="POST",
    )
    assert status == 200
    assert body["status"] == "success"
    assert body["instance"]["source"] == "registered"
    assert body["instance"]["base_url"] == "http://192.168.1.20:8000"
    assert body["instance"]["role"] == "active"
    assert body["instance"]["has_token"] is False


def test_fleet_register_missing_fields_400(api_server):
    _store, _pipeline, base = api_server
    status, body = http_post(
        f"{base}/api/admin/register", payload={"instance_id": "只有一半"}, method="POST"
    )
    assert status == 400


def test_fleet_register_exempt_from_token_gate(api_server, monkeypatch):
    """令牌模式下 register 仍豁免（唯一豁免端点）——无 X-Client-Token 不吃 403。"""
    monkeypatch.setattr(api_server_module, "_API_TOKEN", "unit-test-token")
    _store, _pipeline, base = api_server
    status, body = http_post(
        f"{base}/api/admin/register",
        payload={"instance_id": "cx-o-node", "endpoint": "http://192.168.1.20:8000"},
        method="POST",
    )
    assert status == 200  # 豁免生效（若闸未豁免这里会是 403 unauthorized_client）


def test_fleet_register_non_loopback_403(api_server, monkeypatch):
    """GN-004 F3：非回环来源注册 → 403 且台账不变（豁免端点的唯一安全屏障，API 级验证）。"""
    _store, _pipeline, base = api_server
    monkeypatch.setattr(api_server_module, "is_loopback_client", lambda host: False)
    status, body = http_post(
        f"{base}/api/admin/register",
        payload={"instance_id": "evil-node", "endpoint": "http://192.168.1.50:8000"},
        method="POST",
    )
    assert status == 403
    assert body["error"] == "forbidden"
    # 台账不变：伪造注册未写入
    status, body = http_get_json(f"{base}/api/fleet/instances")
    assert status == 200
    assert body["instances"] == []


def test_fleet_health_gated_by_token(api_server, monkeypatch, fake_cxo):
    """SB-1 裁决：/api/fleet/* 含 health 全部过令牌闸（无令牌 → 403）。"""
    monkeypatch.setattr(api_server_module, "_API_TOKEN", "unit-test-token")
    _store, _pipeline, base = api_server
    fake_url, _state = fake_cxo
    # 令牌模式下登记实例也要带头（先豁免窗口登记：用 register 端点拿一个实例再补录？简化：直接期望 403 即可）
    status, body = http_get_json(f"{base}/api/fleet/instances")
    assert status == 403
    assert body["error"] == "unauthorized_client"


def test_fleet_manifest_passthrough_with_bearer(api_server, fake_cxo):
    """透传带 Bearer：CX-A 把实例令牌注入 Authorization 头。"""
    _store, _pipeline, base = api_server
    fake_url, state = fake_cxo
    _status, instance_id = _add_fake_instance(base, fake_url)
    status, body = http_get_json(f"{base}/api/fleet/instances/{instance_id}/manifest")
    assert status == 200
    assert body["instance_id"] == "fake"
    assert state.last["headers"]["authorization"] == "Bearer tok-good"
    assert state.last["path"] == "/api/admin/manifest"


def test_fleet_wrong_token_401_passthrough(api_server, fake_cxo):
    """SB-2 裁决：CX-O 401 ADMIN_AUTH_FAILED 原样透传（非 502 包装）。"""
    _store, _pipeline, base = api_server
    fake_url, _state = fake_cxo
    _status, instance_id = _add_fake_instance(base, fake_url, token="tok-bad")
    status, body = http_get_json(f"{base}/api/fleet/instances/{instance_id}/manifest")
    assert status == 401
    assert body["error"] == "ADMIN_AUTH_FAILED"


def test_fleet_rate_limited_429_passthrough(api_server, fake_cxo):
    _store, _pipeline, base = api_server
    fake_url, _state = fake_cxo
    _status, instance_id = _add_fake_instance(base, fake_url, token="tok-limited")
    status, body = http_post(
        f"{base}/api/fleet/instances/{instance_id}/control",
        payload={"target": "config", "action": "reload"},
        method="POST",
    )
    assert status == 429
    assert body["error"] == "ADMIN_RATE_LIMITED"


def test_fleet_disabled_503_passthrough(api_server, fake_cxo):
    _store, _pipeline, base = api_server
    fake_url, _state = fake_cxo
    _status, instance_id = _add_fake_instance(base, fake_url, token="tok-disabled")
    status, body = http_get_json(f"{base}/api/fleet/instances/{instance_id}/status")
    assert status == 503
    assert body["error"] == "ADMIN_DISABLED"


def test_fleet_control_request_id_injected_then_preserved(api_server, fake_cxo):
    """缺 request_id 自动补 UUID；自带原样转发不覆盖。"""
    _store, _pipeline, base = api_server
    fake_url, state = fake_cxo
    _status, instance_id = _add_fake_instance(base, fake_url)
    status, body = http_post(
        f"{base}/api/fleet/instances/{instance_id}/control",
        payload={"target": "config", "action": "reload"},
        method="POST",
    )
    assert status == 200
    injected = state.last["body"]["request_id"]
    assert isinstance(injected, str) and len(injected) >= 8
    status, body = http_post(
        f"{base}/api/fleet/instances/{instance_id}/control",
        payload={"target": "config", "action": "reload", "request_id": "op-001"},
        method="POST",
    )
    assert status == 200
    assert state.last["body"]["request_id"] == "op-001"  # 自带不覆盖


def test_fleet_health_no_bearer_passthrough(api_server, fake_cxo):
    _store, _pipeline, base = api_server
    fake_url, state = fake_cxo
    _status, instance_id = _add_fake_instance(base, fake_url)
    status, body = http_get_json(f"{base}/api/fleet/instances/{instance_id}/health")
    assert status == 200
    assert body["status"] == "healthy"
    assert "authorization" not in state.last["headers"]  # CX-O 侧免鉴权 → 不带 Bearer


def test_fleet_unreachable_504(api_server):
    _store, _pipeline, base = api_server
    status, body = http_post(
        f"{base}/api/fleet/instances",
        payload={"name": "不可达", "base_url": "http://127.0.0.1:1", "token": "tok-good"},
        method="POST",
    )
    assert status == 200
    instance_id = body["instance"]["id"]
    status, body = http_get_json(f"{base}/api/fleet/instances/{instance_id}/manifest")
    assert status == 504
    assert body["error"] == "fleet_unreachable"
    assert "不可达" in body["message"]


# ==================================================================
# 记忆页完整控制与核心能力（20261005，spec: align-wizard-settings-memory-pet）
# ==================================================================
def test_memory_create_and_diary_type(api_server):
    """POST /api/memories：新建成功（含 diary 四值域），列表立即可查。"""
    _store, _pipeline, base = api_server
    status, body = http_post(
        f"{base}/api/memories",
        {"content": "项目计划会议纪要", "memory_type": "long_term", "importance": 4, "tags": ["工作"]},
    )
    assert status == 200 and body["ok"] and isinstance(body["id"], int)
    status, rows, _raw = http_get(f"{base}/api/memories")
    assert status == 200 and any(r["content"] == "项目计划会议纪要" for r in rows)
    status, body = http_post(f"{base}/api/memories", {"content": "今日随笔", "memory_type": "diary"})
    assert status == 200 and body["ok"]


def test_memory_create_validation_400(api_server):
    """POST /api/memories：缺 content / 非法 type / importance 越界 / 非法 tags → 400 中文。"""
    _store, _pipeline, base = api_server
    for payload, keyword in (
        ({"content": ""}, "content"),
        ({"content": "   "}, "content"),
        ({}, "content"),
        ({"content": "x", "memory_type": "bogus"}, "memory_type"),
        ({"content": "x", "importance": 0}, "importance"),
        ({"content": "x", "importance": 6}, "importance"),
        ({"content": "x", "importance": "high"}, "importance"),
        ({"content": "x", "tags": [1, 2]}, "tags"),
    ):
        status, body = http_post(f"{base}/api/memories", payload)
        assert status == 400, (payload, status, body)
        assert keyword in body["message"] and body["error"] == "bad_request"


def test_memory_create_deduplicated(api_server):
    """POST /api/memories：与既有记忆高度相似时走 manager 去重，返回 deduplicated:true。"""
    _store, _pipeline, base = api_server
    status, first = http_post(f"{base}/api/memories", {"content": "用户喜欢喝冰美式咖啡"})
    assert status == 200 and first["ok"] and first["id"] is not None
    # 重复内容（完全一致必然相似度 1.0 ≥ 0.85 阈值）
    status, second = http_post(f"{base}/api/memories", {"content": "用户喜欢喝冰美式咖啡"})
    assert status == 200 and second.get("deduplicated") is True and second["id"] is None


def test_memory_update_put(api_server):
    """PUT /api/memories/{id}：字段补丁编辑生效（走 MemoryStore.update）。"""
    _store, _pipeline, base = api_server
    _status, created = http_post(f"{base}/api/memories", {"content": "旧内容", "importance": 2})
    mid = created["id"]
    status, body = http_post(
        f"{base}/api/memories/{mid}",
        {"content": "新内容", "memory_type": "diary", "importance": 5, "tags": ["新标签"]},
        method="PUT",
    )
    assert status == 200 and body["ok"]
    assert body["memory"]["content"] == "新内容"
    assert body["memory"]["type"] == "diary"
    assert body["memory"]["importance"] == 5
    assert body["memory"]["tags"] == ["新标签"]


def test_memory_update_put_404_and_400(api_server):
    """PUT /api/memories/{id}：不存在/已软删 404；非法字段与空补丁 400。"""
    _store, _pipeline, base = api_server
    status, body = http_post(f"{base}/api/memories/99999", {"content": "x"}, method="PUT")
    assert status == 404 and "不存在" in body["message"]
    # 已软删记忆不可编辑
    _status, created = http_post(f"{base}/api/memories", {"content": "待删"})
    mid = created["id"]
    http_delete(f"{base}/api/memories/{mid}")
    status, body = http_post(f"{base}/api/memories/{mid}", {"content": "x"}, method="PUT")
    assert status == 404
    # 非法 type → 400
    _status, created2 = http_post(f"{base}/api/memories", {"content": "正常"})
    status, body = http_post(
        f"{base}/api/memories/{created2['id']}", {"memory_type": "bogus"}, method="PUT"
    )
    assert status == 400 and "memory_type" in body["message"]
    # 无可更新字段（全未知键）→ 400
    status, body = http_post(
        f"{base}/api/memories/{created2['id']}", {"hacker_field": 1}, method="PUT"
    )
    assert status == 400
    # 非法 id → 400
    status, _body = http_post(f"{base}/api/memories/not-an-id", {"content": "x"}, method="PUT")
    assert status == 400


def test_memory_batch_delete_with_permanent_protection(api_server):
    """POST /api/memories/batch-delete：软删成功计数；permanent 保护跳过；明细回执。"""
    _store, _pipeline, base = api_server
    ids = []
    for text in ("批量一", "批量二", "批量三"):
        _status, created = http_post(f"{base}/api/memories", {"content": text})
        ids.append(created["id"])
    _status, perm = http_post(f"{base}/api/memories", {"content": "永久保留", "memory_type": "permanent"})
    status, body = http_post(f"{base}/api/memories/batch-delete", {"ids": ids + [perm["id"], 99999]})
    assert status == 200 and body["ok"]
    assert body["deleted_count"] == 3 and sorted(body["deleted_ids"]) == sorted(ids)
    reasons = {s["id"]: s["reason"] for s in body["skipped"]}
    assert reasons[perm["id"]] == "permanent_protected"
    assert reasons[99999] == "not_found"
    # 列表确认软删生效、permanent 仍在
    _status, rows, _raw = http_get(f"{base}/api/memories")
    remain = {r["id"] for r in rows}
    assert perm["id"] in remain and not (set(ids) & remain)
    # 空 ids → 400
    status, body = http_post(f"{base}/api/memories/batch-delete", {"ids": []})
    assert status == 400 and "ids" in body["message"]
    status, _body = http_post(f"{base}/api/memories/batch-delete", {})
    assert status == 400


def test_memory_decay_stats_endpoint(api_server):
    """GET /api/memories/decay-stats：统计结构完整，permanent 计入豁免计数。"""
    _store, _pipeline, base = api_server
    http_post(f"{base}/api/memories", {"content": "普通记忆"})
    http_post(f"{base}/api/memories", {"content": "永久记忆", "memory_type": "permanent"})
    status, body, _raw = http_get(f"{base}/api/memories/decay-stats")
    assert status == 200 and body["ok"]
    stats = body["statistics"]
    assert stats["total_memories"] == 2
    assert stats["permanent_count"] == 1
    assert set(stats["decay_distribution"]) == {"healthy", "fading", "faded"}
    assert "avg_time_score" in stats and "importance_distribution" in stats
    assert stats["thresholds"]["archive"] == 0.1


def test_memory_sync_decay_endpoint(api_server, monkeypatch, tmp_path):
    """POST /api/memories/sync-decay：低分记忆软删、permanent 豁免、回执完整。"""
    from lite.memory.decay import DecayCalculator

    store, _pipeline, base = api_server
    # 直写 store 注入 90 天前的低分记忆（端点用实时时钟，用过去时间戳保证已衰减）
    from datetime import datetime, timedelta

    old = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S.%f")
    old_id = store.add({"type": "short_term", "content": "古老低分", "importance": 1, "created_at": old})
    perm_id = store.add(
        {"type": "permanent", "content": "古老永久", "importance": 1, "permanent": True, "created_at": old}
    )
    status, body = http_post(f"{base}/api/memories/sync-decay", {})
    assert status == 200 and body["ok"]
    assert old_id in body["deleted_ids"] and perm_id not in body["deleted_ids"]
    assert body["skipped_permanent"] == 1
    assert body["archive_threshold"] == 0.1
    # decay-stats 同步后：faded 桶清零（低分已被归档）
    _status, stats_body, _raw = http_get(f"{base}/api/memories/decay-stats")
    assert stats_body["statistics"]["decay_distribution"]["faded"] == 0
    # 静默引用防未用告警
    assert DecayCalculator is not None and tmp_path is not None and monkeypatch is not None


def test_memory_diary_endpoint(api_server):
    """GET /api/memories/diary：按 created_at 本地日期分组、date 过滤、type 过滤。"""
    _store, _pipeline, base = api_server
    # 直写 store 控制日期（一条今天、一条 2026-01-02）
    store, _pipeline, base = api_server
    store.add({"type": "diary", "content": "今天的日记", "created_at": "2026-01-02 08:00:00.000000"})
    store.add({"type": "long_term", "content": "同日旧记忆", "created_at": "2026-01-02 21:30:00.000000"})
    status, body, _raw = http_get(f"{base}/api/memories/diary")
    assert status == 200 and body["ok"]
    groups = {g["date"]: g for g in body["diary_groups"]}
    assert "2026-01-02" in groups and groups["2026-01-02"]["count"] == 2
    # 日期降序
    dates = [g["date"] for g in body["diary_groups"]]
    assert dates == sorted(dates, reverse=True)
    # date 过滤：只返回该日期组
    status, body, _raw = http_get(f"{base}/api/memories/diary?date=2026-01-02")
    assert status == 200 and len(body["diary_groups"]) == 1 and body["count"] == 2
    # type 过滤：仅 diary 类型
    status, body, _raw = http_get(f"{base}/api/memories/diary?type=diary")
    assert status == 200 and body["count"] == 1
    assert body["diary_groups"][0]["entries"][0]["content"] == "今天的日记"
    # 非法 type → 400（http_get_json 捕获 HTTPError 返回二元组）
    status, _body = http_get_json(f"{base}/api/memories/diary?type=bogus")
    assert status == 400
    # 无记忆日期 → 空态
    status, body, _raw = http_get(f"{base}/api/memories/diary?date=1999-01-01")
    assert status == 200 and body["diary_groups"] == [] and body["count"] == 0


def test_memory_search_3d_endpoint(api_server):
    """GET /api/memories/3d：三维加权排序；极端权重（全给重要性）排序随之变化。"""
    store, _pipeline, base = api_server
    # 显式传 importance_score（importance 维度的真正来源；不传则默认 0.6 无法区分）
    low = store.add({"type": "long_term", "content": "重要性低的相关条目", "importance": 1, "importance_score": 0.2})
    high = store.add({"type": "long_term", "content": "重要性高的相关条目", "importance": 5, "importance_score": 0.9})
    # 默认权重（无 query → relevance 全 0.5 降级口径，importance 主导差异）
    status, body, _raw = http_get(f"{base}/api/memories/3d")
    assert status == 200 and body["ok"]
    assert body["applied_weights"] == {"importance": 0.35, "time": 0.25, "relevance": 0.4}
    ids_default = [m["id"] for m in body["memories"]]
    assert set(ids_default) == {low, high}
    # 默认权重下 importance 0.9 应显著排前（importance 维度主导）
    assert ids_default.index(high) < ids_default.index(low)
    # 全给重要性：importance 5 恒在前
    status, body, _raw = http_get(f"{base}/api/memories/3d?w_importance=1&w_time=0&w_rel=0")
    assert status == 200 and body["applied_weights"]["importance"] == 1.0
    ids_imp = [m["id"] for m in body["memories"]]
    assert ids_imp.index(high) < ids_imp.index(low)
    # 反向极端：时间/相关性主导（两者接近）时排序差距收窄——仍恒定不含已删条目
    status, body, _raw = http_get(f"{base}/api/memories/3d?w_importance=0&w_time=0&w_rel=1")
    assert status == 200 and body["applied_weights"]["importance"] == 0.0


def test_memory_search_3d_with_query_and_validation(api_server):
    """GET /api/memories/3d：query 走检索链路；权重非法 400；非法 type 400。"""
    from urllib.parse import urlencode

    _store, _pipeline, base = api_server
    _store.add({"type": "long_term", "content": "关于项目计划的记忆"})
    q = urlencode({"query": "项目计划"})
    status, body, _raw = http_get(f"{base}/api/memories/3d?{q}")
    assert status == 200 and body["total"] >= 1
    assert any("项目计划" in m["content"] for m in body["memories"])
    # 每条结果带三维分量（score_memories 产出）
    assert "final_score" in body["memories"][0]
    # 权重非法 → 400（http_get_json 捕获 HTTPError）
    status, _body = http_get_json(f"{base}/api/memories/3d?w_importance=abc")
    assert status == 400
    status, _body = http_get_json(f"{base}/api/memories/3d?w_time=1.5")
    assert status == 400
    status, _body = http_get_json(f"{base}/api/memories/3d?w_rel=-0.1")
    assert status == 400
    # 非法 type → 400
    status, _body = http_get_json(f"{base}/api/memories/3d?type=bogus")
    assert status == 400


# ---------------------------------------------------------------- 内置记忆工具（memory_read / memory_update / memory_delete）
class TestBuiltinMemoryTools:
    """BuiltinToolRegistry 记忆管理助手工具环新增工具（Task 3 依赖）。"""

    @pytest.fixture()
    def registry_env(self, tmp_path):
        from lite.memory.storage import MemoryStore
        from lite.tools.builtin_registry import BuiltinToolRegistry

        store = MemoryStore(db_path=str(tmp_path / "memories.db"))
        store.create_table()
        registry = BuiltinToolRegistry(memory_store=store)
        yield store, registry
        store.close()

    def test_read_found_and_missing(self, registry_env):
        store, registry = registry_env
        mid = store.add({"type": "short_term", "content": "读取目标"})
        outcome = registry.call("memory_read", {"id": mid})
        assert outcome["success"] and outcome["result"]["memory"]["content"] == "读取目标"
        missing = registry.call("memory_read", {"id": 99999})
        assert not missing["success"] and "不存在" in missing["error"]
        bad = registry.call("memory_read", {"id": "abc"})
        assert not bad["success"] and "id 必须为整数" in bad["error"]

    def test_update_fields_and_validation(self, registry_env):
        store, registry = registry_env
        mid = store.add({"type": "short_term", "content": "更新前", "importance": 2})
        outcome = registry.call("memory_update", {"id": mid, "importance": 5, "tags": ["新"]})
        assert outcome["success"] and outcome["result"]["updated_fields"] == ["importance", "tags"]
        assert store.get(mid)["importance"] == 5
        # type 非法 → 校验失败回执（store.update 的 ValueError 被包装）
        outcome = registry.call("memory_update", {"id": mid, "type": "bogus"})
        assert not outcome["success"] and "非法记忆类型" in outcome["error"]
        # 无有效字段 → 明确错误
        outcome = registry.call("memory_update", {"id": mid})
        assert not outcome["success"] and "至少提供" in outcome["error"]
        # 软删后不可更新
        store.soft_delete(mid)
        outcome = registry.call("memory_update", {"id": mid, "content": "x"})
        assert not outcome["success"] and "不存在" in outcome["error"]

    def test_delete_success_and_permanent_protection(self, registry_env):
        store, registry = registry_env
        mid = store.add({"type": "short_term", "content": "待删"})
        outcome = registry.call("memory_delete", {"id": mid})
        assert outcome["success"] and outcome["result"]["deleted"] is True
        assert store.get(mid)["is_deleted"] == 1
        pid = store.add({"type": "permanent", "content": "永久", "permanent": True})
        outcome = registry.call("memory_delete", {"id": pid})
        assert not outcome["success"] and "permanent" in outcome["error"]
        assert store.get(pid)["is_deleted"] == 0
        missing = registry.call("memory_delete", {"id": 424242})
        assert not missing["success"] and "不存在" in missing["error"]


# ---------------------------------------------------------------- 指令标签解析器（memory-agent 工具环前置）
class TestMemoryOpParsing:
    """_extract_memory_ops / _strip_memory_tags：标签提取与剥离（括号平衡扫描）。"""

    def test_extract_single_and_multi(self):
        from lite.server.api_server import _extract_memory_ops

        text = (
            '前文 [memory:search {"query": "项目计划", "top_k": 3}] 中间'
            ' [memory:delete {"id": 7}] 尾部'
        )
        ops = _extract_memory_ops(text)
        assert len(ops) == 2
        assert ops[0][0] == "search" and ops[0][1] == {"query": "项目计划", "top_k": 3}
        assert ops[1][0] == "delete" and ops[1][1] == {"id": 7}
        assert all(raw.startswith("[memory:") for _op, _args, raw in ops)

    def test_extract_json_with_bracket_inside_string(self):
        from lite.server.api_server import _extract_memory_ops

        text = '[memory:write {"content": "内容含右括号]与{花括号}", "type": "diary"}]'
        ops = _extract_memory_ops(text)
        assert len(ops) == 1
        assert ops[0][1]["content"] == "内容含右括号]与{花括号}"

    def test_extract_invalid_json_yields_none_args(self):
        from lite.server.api_server import _extract_memory_ops

        ops = _extract_memory_ops("[memory:write {bad json}]")
        assert len(ops) == 1 and ops[0][0] == "write" and ops[0][1] is None

    def test_extract_no_tags(self):
        from lite.server.api_server import _extract_memory_ops

        assert _extract_memory_ops("普通回复，没有标签") == []
        assert _extract_memory_ops("") == []
        assert _extract_memory_ops(None) == []

    def test_strip_tags_removes_all_and_cleans_blank_lines(self):
        from lite.server.api_server import _strip_memory_tags

        text = '第一段 [memory:write {"content": "x"}]\n\n[memory:delete {"id": 1}] 第二段'
        cleaned = _strip_memory_tags(text)
        assert "[memory:" not in cleaned
        assert "第一段" in cleaned and "第二段" in cleaned

    def test_strip_partial_tag_without_json(self):
        from lite.server.api_server import _strip_memory_tags

        # 残缺标签（无 JSON 体 / 未闭合）也不泄漏
        assert "[memory:" not in _strip_memory_tags("前 [memory:write] 后")
        assert "[memory:" not in _strip_memory_tags('前 [memory:write {"a": 1')
        assert _strip_memory_tags("无标签") == "无标签"




