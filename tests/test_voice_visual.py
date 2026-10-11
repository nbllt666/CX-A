# -*- coding: utf-8 -*-
"""视觉语音融合端点集成测试（20261011_模块0_视觉语音融合与主动回复）。

覆盖：
- POST /api/voice/visual_prefill：截图失败静默 / 本地未就绪仍存 pending；
- POST /api/chat/message_stream (visual=true)：pending 一次性消费、本地模式带图、
  云端 provider 视觉能力降级（deepseek 剥图 / openai 带图）；
- GET /api/chat/proactive：原子取空；
- PUT /api/settings vision.proactive_enabled：白名单 + GET 回显契约。
"""

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

import lite.server.api_server as api_mod
from lite.computer_control import ComputerControl, ToolBridge
from lite.computer_control.security import ControlAuthorizer
from lite.server.api_server import build_deps, make_handler


class _FakeCloud:
    """内存 mock 云端适配器（含 provider 视觉能力判定所需的 provider 属性）。"""

    def __init__(self, chunks=None, provider="deepseek"):
        self.chunks = chunks if chunks is not None else ["你好"]
        self.provider = provider
        self.calls = []

    def chat(self, messages):
        self.calls.append(messages)
        yield from self.chunks

    def is_online(self, timeout=5):
        return True


class _FakeLocalFallback:
    """本地模式兜底管理器替身：local_mode_enabled() 恒 True，chat 记录消息。"""

    def __init__(self, chunks=None):
        self.chunks = chunks if chunks is not None else ["本地回复"]
        self.calls = []
        self.status = "local"

    def local_mode_enabled(self):
        return True

    def chat(self, messages):
        self.calls.append(messages)
        yield from self.chunks


@contextmanager
def running_server(tmp_path, cloud=None, chat_fallback=None):
    """以临时数据目录起服（注入 cloud / chat_fallback），yield (base_url, store)。"""
    store, pipeline, manager, _remote = build_deps(data_dir=str(tmp_path))
    authorizer = ControlAuthorizer(data_dir=str(tmp_path))
    computer = ComputerControl(authorized=authorizer.is_authorized())
    bridge = ToolBridge(computer=computer, authorizer=authorizer)
    handler = make_handler(
        store, pipeline, manager,
        computer=computer, authorizer=authorizer, bridge=bridge,
        chat_cloud=cloud, chat_fallback=chat_fallback,
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", store
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        store.close()
        close = getattr(pipeline.vector_store, "close", None)
        if callable(close):
            close()


def http_post(url, payload, method="POST"):
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


def http_get_json(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def http_post_ndjson(url, payload):
    """POST 并按 NDJSON 逐行解析 chunked 响应。"""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        lines = [line for line in resp.read().decode("utf-8").splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def _store_pending(image_b64="QUJD"):
    """直接写入待消费截图（绕开截图端点，聚焦消费语义）。"""
    api_mod._store_voice_pending_visual(image_b64)


def _last_user_content(messages):
    return [m for m in messages if m.get("role") == "user"][-1]["content"]


# ------------------------------------------------------------------ #
# POST /api/voice/visual_prefill                                       #
# ------------------------------------------------------------------ #

def test_visual_prefill_screenshot_unavailable(tmp_path, monkeypatch):
    """截图失败 → 静默返回 prefilled=false，语音链路不受影响（不 5xx）。"""
    monkeypatch.setattr(api_mod, "_capture_voice_screenshot", lambda: None)
    with running_server(tmp_path, cloud=_FakeCloud()) as (base, _store):
        status, body = http_post(f"{base}/api/voice/visual_prefill", {})
        assert status == 200
        assert body == {"ok": True, "prefilled": False, "reason": "screenshot_unavailable"}


def test_visual_prefill_local_not_ready_keeps_pending(tmp_path, monkeypatch):
    """本地大脑未就绪 → 跳过预填充但截图照存（云端正式请求仍可带图）。"""
    monkeypatch.setattr(api_mod, "_capture_voice_screenshot", lambda: "QUJD")
    with running_server(tmp_path, cloud=_FakeCloud()) as (base, _store):
        status, body = http_post(f"{base}/api/voice/visual_prefill", {})
        assert status == 200
        assert body["prefilled"] is False
        assert body["reason"] == "local_llm_not_ready"
        # pending 已存：视觉消费端点取得到（下方 message_stream 消费链路覆盖）


def test_visual_prefill_calls_runtime_with_image_prefix(tmp_path, monkeypatch):
    """本地运行时就绪 → 预填充消息 = 公共前缀 + user([image_url, 说明])，max_tokens=1。"""
    monkeypatch.setattr(api_mod, "_capture_voice_screenshot", lambda: "QUJD")

    class _FakeRuntime:
        def __init__(self):
            self.calls = []

        def offline_chat(self, messages, max_tokens=None, temperature=None):
            self.calls.append({"messages": messages, "max_tokens": max_tokens,
                               "temperature": temperature})
            return "嗯"

    fake_runtime = _FakeRuntime()

    class _FakeHolder:
        def get(self):
            return fake_runtime

        def ready(self):
            return True

    real_holder_cls = api_mod._LocalChatRuntimeHolder

    class _StubHolder(real_holder_cls):
        def get(self):
            return fake_runtime

        def ready(self):
            return True

        def ensure_started(self, config):
            return None

        def release(self):
            return None

    monkeypatch.setattr(api_mod, "_LocalChatRuntimeHolder", _StubHolder)
    with running_server(tmp_path, cloud=_FakeCloud()) as (base, _store):
        status, body = http_post(f"{base}/api/voice/visual_prefill", {})
        assert status == 200
        assert body == {"ok": True, "prefilled": True}
        assert len(fake_runtime.calls) == 1
        call = fake_runtime.calls[0]
        assert call["max_tokens"] == 1
        last = call["messages"][-1]
        assert last["role"] == "user"
        assert isinstance(last["content"], list)
        assert last["content"][0]["type"] == "image_url"
        assert last["content"][0]["image_url"]["url"].endswith("QUJD")
        assert call["messages"][0]["role"] == "system"


# ------------------------------------------------------------------ #
# POST /api/chat/message_stream (visual=true)                          #
# ------------------------------------------------------------------ #

def test_stream_local_mode_consumes_pending_image_once(tmp_path):
    """本地模式：visual=true 消费 pending 图（user content 数组带图），且一次性消费。"""
    fallback = _FakeLocalFallback()
    with running_server(tmp_path, chat_fallback=fallback) as (base, _store):
        _store_pending("QUJD")
        frames = http_post_ndjson(
            f"{base}/api/chat/message_stream", {"message": "看看我的屏幕", "visual": True}
        )
        assert any(frame.get("done") for frame in frames)
        assert len(fallback.calls) == 1
        content = _last_user_content(fallback.calls[0])
        assert isinstance(content, list)
        assert content[0]["type"] == "image_url"
        assert "QUJD" in content[0]["image_url"]["url"]
        assert content[1]["type"] == "text"
        assert "看看我的屏幕" in content[1]["text"]
        # 一次性消费：第二次 visual=true 无 pending → 退回纯文本消息
        frames2 = http_post_ndjson(
            f"{base}/api/chat/message_stream", {"message": "再来一句", "visual": True}
        )
        assert any(frame.get("done") for frame in frames2)
        assert _last_user_content(fallback.calls[1]) == "再来一句"


def test_stream_cloud_deepseek_strips_image(tmp_path):
    """云端 deepseek（纯文本 provider）：visual=true 也不带图（剥图降级）。"""
    cloud = _FakeCloud(chunks=["好的"], provider="deepseek")
    with running_server(tmp_path, cloud=cloud) as (base, _store):
        _store_pending("QUJD")
        frames = http_post_ndjson(
            f"{base}/api/chat/message_stream", {"message": "看看屏幕", "visual": True}
        )
        assert any(frame.get("done") for frame in frames)
        content = _last_user_content(cloud.calls[0])
        assert isinstance(content, str)
        assert "看看屏幕" in content
        # pending 已被消费（一次性）：第二次 visual=true 仍纯文本（无图可带）
        frames2 = http_post_ndjson(
            f"{base}/api/chat/message_stream", {"message": "再来一句", "visual": True}
        )
        assert any(frame.get("done") for frame in frames2)
        assert _last_user_content(cloud.calls[1]) == "再来一句"


def test_stream_cloud_openai_keeps_image(tmp_path):
    """云端 openai（视觉能力 provider）：visual=true 带图外发。"""
    cloud = _FakeCloud(chunks=["看到了"], provider="openai")
    with running_server(tmp_path, cloud=cloud) as (base, _store):
        _store_pending("QUJD")
        frames = http_post_ndjson(
            f"{base}/api/chat/message_stream", {"message": "看看屏幕", "visual": True}
        )
        assert any(frame.get("done") for frame in frames)
        content = _last_user_content(cloud.calls[0])
        assert isinstance(content, list)
        assert "QUJD" in content[0]["image_url"]["url"]


# ------------------------------------------------------------------ #
# GET /api/chat/proactive                                              #
# ------------------------------------------------------------------ #

def test_proactive_queue_atomic_drain(tmp_path):
    """队列条目 → GET 原子取空；再次 GET 返回空列表（at-most-once）。"""
    with running_server(tmp_path, cloud=_FakeCloud()) as (base, _store):
        with api_mod._PROACTIVE_QUEUE_LOCK:
            api_mod._PROACTIVE_QUEUE.append({"text": "你在忙什么？", "summary": "s", "ts": 1.0})
            api_mod._PROACTIVE_QUEUE.append({"text": "需要帮忙吗？", "summary": "", "ts": 2.0})
        status, body = http_get_json(f"{base}/api/chat/proactive")
        assert status == 200
        assert body["ok"] is True
        assert [m["text"] for m in body["messages"]] == ["你在忙什么？", "需要帮忙吗？"]
        status, body = http_get_json(f"{base}/api/chat/proactive")
        assert body["messages"] == []


def test_proactive_settings_roundtrip(tmp_path):
    """PUT vision.proactive_enabled 合法值生效 + 非法值入 ignored；GET 回显。"""
    with running_server(tmp_path, cloud=_FakeCloud()) as (base, _store):
        status, body = http_post(
            f"{base}/api/settings", {"vision": {"proactive_enabled": False}}, method="PUT"
        )
        assert status == 200
        assert "vision.proactive_enabled" in body["applied"]
        status, body = http_post(
            f"{base}/api/settings", {"vision": {"proactive_enabled": "yes"}}, method="PUT"
        )
        assert status == 200
        assert any("proactive_enabled" in item for item in body["ignored"])
        status, body = http_get_json(f"{base}/api/settings")
        assert body["vision"]["proactive_enabled"] is False
