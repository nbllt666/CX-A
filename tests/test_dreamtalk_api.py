# -*- coding: utf-8 -*-
"""``/api/dreamtalk/*`` HTTP 契约测试（真实起服 + 注入 Mock 梦境通道）。

覆盖：端点字段集合、错误码（400/404/409/422/503）、素材不出现在响应、
醒来回写幂等与开关、离线文案不入梦、聊天面通道状态不变、配置热更新即时生效、
回写后清醒侧可检索、梦境锁不阻塞状态端点。
"""

import json
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import pytest

from lite.cloud.adapter import CloudUnavailableError
from lite.cloud.fallback import (
    CONFIG_ERROR_PROMPT,
    LOCAL_NOT_READY_PROMPT,
    OFFLINE_PROMPT,
    OfflineFallbackManager,
)
from lite.config.config_manager import ConfigManager
from lite.dreamtalk.adapters.llm import MockLLMClient
from lite.dreamtalk.wiring import build_dreamtalk_runtime
from lite.server.api_server import build_deps, make_handler

_OFFLINE_TEXTS = {OFFLINE_PROMPT, LOCAL_NOT_READY_PROMPT, CONFIG_ERROR_PROMPT}
_SESSION_FIELDS = {
    "id", "agent_id", "created_at", "mode", "mode_reason", "status",
    "title", "summary", "emotion_residue", "ended_at", "turn_count",
}
_TURN_FIELDS = {"id", "session_id", "seq", "speaker", "content", "flagged", "created_at"}
_RECALL_FIELDS = {"session_id", "created_at", "mode", "title", "summary", "emotion_residue"}


class _FakeCloud:
    """内存 mock 云端：可设在线/离线，记录聊天面调用。"""

    def __init__(self, online=True, error=None):
        self.online = online
        self.error = error
        self.calls = []

    def chat(self, messages):
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        yield "云端的一句话"

    def is_online(self, timeout=5):
        if self.error is not None:
            return False
        return self.online


@contextmanager
def running_server(tmp_path, *, seed_memories=None, cloud=None, dreamtalk=None):
    """以临时数据目录起服，yield ``(base, store, pipeline, config)``。"""
    data_dir = str(tmp_path)
    store, pipeline, manager, _remote = build_deps(data_dir=data_dir)
    config = ConfigManager(
        config_path=str(tmp_path / "config.json"), data_dir=data_dir
    )
    for text, fields in seed_memories or []:
        pipeline.manager.add_memory(content=text, **fields)
    runtime = dreamtalk
    if runtime is None:
        runtime = build_dreamtalk_runtime(
            config,
            store,
            pipeline.manager,
            data_dir=data_dir,
            llm_client=MockLLMClient(),
        )
    handler = make_handler(
        store, pipeline, manager, config=config, chat_cloud=cloud, dreamtalk=runtime
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", store, pipeline, config
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        store.close()
        close = getattr(pipeline.vector_store, "close", None)
        if callable(close):
            close()


def _request(url, payload=None, method="GET", timeout=10):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


_SEED = [
    ("楼下那只很瘦的猫", {"type": "long_term", "importance": 2, "importance_score": 0.2}),
    ("地铁上有人要去海边", {"type": "long_term", "importance": 2, "importance_score": 0.2}),
    ("窗外很白的光", {"type": "long_term", "importance": 2, "importance_score": 0.3}),
]


def _dream_count(store):
    return len([row for row in store.list(agent_id="default") if row.get("source") == "dreamtalk"])


# ------------------------------------------------------------------ health / config
def test_health_and_config_contract(tmp_path):
    with running_server(tmp_path, seed_memories=_SEED) as (base, _store, _pipeline, _config):
        status, body = _request(f"{base}/api/dreamtalk/health")
        assert status == 200
        assert body["ok"] is True
        assert body["status"] == "idle"
        assert body["contract_version"] == "1.0"
        assert body["enabled"] is True
        assert body["mode_weights"] == "memory=0.5,wild=0.3,dreamless=0.2"
        assert body["memory_writeback"] is True
        assert body["sessions"] == 0

        status, body = _request(f"{base}/api/dreamtalk/config")
        assert status == 200
        assert len(body["config"]) == 16
        assert body["config"]["material_window_days"] == 7
        assert body["config"]["material_max_items"] == 20


def test_config_put_merges_validates_and_hot_reloads(tmp_path):
    with running_server(tmp_path, seed_memories=_SEED) as (base, _store, _pipeline, _config):
        status, body = _request(f"{base}/api/dreamtalk/config", {"max_rounds": 2}, "PUT")
        assert status == 200
        assert body["config"]["max_rounds"] == 2

        status, body = _request(f"{base}/api/dreamtalk/config", {"max_rounds": 0}, "PUT")
        assert status == 422
        assert body["ok"] is False

        status, body = _request(f"{base}/api/dreamtalk/config", {"nope": 1}, "PUT")
        assert status == 422

        # 热更新：max_rounds=1 → 新会话第 1 轮即结束
        _request(f"{base}/api/dreamtalk/config", {"max_rounds": 1}, "PUT")
        status, body = _request(f"{base}/api/dreamtalk/sessions", {"mode": "memory"}, "POST")
        assert status == 200
        assert len(body["turns"]) == 2
        assert body["session"]["status"] == "ended"

        # 热更新：enabled=false → 除 health 外 503；改回 true 恢复
        _request(f"{base}/api/dreamtalk/config", {"enabled": False}, "PUT")
        assert _request(f"{base}/api/dreamtalk/sessions")[0] == 503
        assert _request(f"{base}/api/dreamtalk/health")[1]["status"] == "disabled"
        _request(f"{base}/api/dreamtalk/config", {"enabled": True}, "PUT")
        assert _request(f"{base}/api/dreamtalk/sessions")[0] == 200


# ------------------------------------------------------------------ 主链路
def test_main_chain_and_response_contract(tmp_path):
    with running_server(tmp_path, seed_memories=_SEED) as (base, _store, _pipeline, _config):
        status, body = _request(f"{base}/api/dreamtalk/sessions", {"mode": "memory"}, "POST")
        assert status == 200
        assert set(body) >= {"ok", "session", "turns", "recall"}
        assert set(body["session"]) == _SESSION_FIELDS
        assert [turn["speaker"] for turn in body["turns"]] == ["director", "dreamer"]
        for turn in body["turns"]:
            assert set(turn) == _TURN_FIELDS
        # 素材快照不得出现在响应体
        raw = json.dumps(body, ensure_ascii=False)
        for leaked in ("day_memories", "material_json", "entities", "cxa_memory"):
            assert leaked not in raw

        session_id = body["session"]["id"]
        status, body = _request(
            f"{base}/api/dreamtalk/sessions/{session_id}/message", {"content": "我推门"}, "POST"
        )
        assert status == 200
        assert [turn["speaker"] for turn in body["turns"]] == [
            "director", "dreamer", "awakened", "director", "dreamer",
        ]

        status, body = _request(f"{base}/api/dreamtalk/sessions/{session_id}/wake", {}, "POST")
        assert status == 200
        assert body["session"]["status"] == "ended"
        assert set(body["recall"]) == _RECALL_FIELDS

        status, body = _request(f"{base}/api/dreamtalk/sessions/{session_id}/recall")
        assert status == 200
        assert set(body["recall"]) == _RECALL_FIELDS

        status, body = _request(f"{base}/api/dreamtalk/sessions")
        assert status == 200
        assert body["total"] == 1
        assert set(body["items"][0]) == _SESSION_FIELDS

        status, body = _request(f"{base}/api/dreamtalk/recall")
        assert status == 200
        assert body["total"] == 1

        status, body = _request(f"{base}/api/dreamtalk/sessions/{session_id}")
        assert status == 200
        assert len(body["turns"]) == 5


def test_validation_and_error_codes(tmp_path):
    with running_server(tmp_path, seed_memories=_SEED) as (base, _store, _pipeline, _config):
        # 未知字段（禁止手填素材 / 梦外上下文）
        for payload in ({"chat_history": []}, {"day_memories": ["a"]}, {"entities": ["b"]}):
            status, body = _request(f"{base}/api/dreamtalk/sessions", payload, "POST")
            assert status == 400
            assert body["error"] == "bad_request"
        assert _request(f"{base}/api/dreamtalk/sessions")[1]["total"] == 0

        # 非法模式 → 422
        status, body = _request(f"{base}/api/dreamtalk/sessions", {"mode": "lucid"}, "POST")
        assert status == 422
        assert body["error"] == "invalid_mode"

        # 不存在 → 404
        status, body = _request(f"{base}/api/dreamtalk/sessions/nope")
        assert status == 404
        assert body["error"] == "session_not_found"
        assert body["message"]

        # 未结束会话取摘要 → 409
        status, body = _request(f"{base}/api/dreamtalk/sessions", {"mode": "memory"}, "POST")
        session_id = body["session"]["id"]
        status, body = _request(f"{base}/api/dreamtalk/sessions/{session_id}/recall")
        assert status == 409
        assert body["error"] == "session_ended"

        # 醒来后再醒 → 409
        _request(f"{base}/api/dreamtalk/sessions/{session_id}/wake", {}, "POST")
        assert _request(f"{base}/api/dreamtalk/sessions/{session_id}/wake", {}, "POST")[0] == 409
        assert _request(
            f"{base}/api/dreamtalk/sessions/{session_id}/message", {"content": "x"}, "POST"
        )[0] == 409

        # limit：非整数 400；越界 clamp
        assert _request(f"{base}/api/dreamtalk/sessions?limit=abc")[0] == 400
        status, body = _request(f"{base}/api/dreamtalk/sessions?limit=999")
        assert status == 200
        assert body["total"] == 1


def test_memory_mode_without_material_is_422(tmp_path):
    with running_server(tmp_path, seed_memories=[]) as (base, _store, _pipeline, _config):
        status, body = _request(f"{base}/api/dreamtalk/sessions", {"mode": "memory"}, "POST")
        assert status == 422
        assert body["error"] == "no_memory_material"
        assert _request(f"{base}/api/dreamtalk/sessions")[1]["total"] == 0
        # 自动模式在空素材下必然不是记忆梦
        status, body = _request(f"{base}/api/dreamtalk/sessions", {"mode": "auto"}, "POST")
        assert status == 200
        assert body["session"]["mode"] != "memory"


# ------------------------------------------------------------------ 回写
def test_writeback_is_idempotent(tmp_path):
    with running_server(tmp_path, seed_memories=_SEED) as (base, store, pipeline, _config):
        before = _dream_count(store)
        status, body = _request(f"{base}/api/dreamtalk/sessions", {"mode": "memory"}, "POST")
        session_id = body["session"]["id"]
        status, body = _request(f"{base}/api/dreamtalk/sessions/{session_id}/wake", {}, "POST")
        assert status == 200
        assert _dream_count(store) == before + 1

        written = [
            row for row in store.list(agent_id="default")
            if row.get("source") == "dreamtalk"
            and isinstance(row.get("metadata"), dict)
            and row["metadata"].get("dreamtalk_session_id") == session_id
        ]
        assert len(written) == 1
        assert written[0]["tags"] == ["dreamtalk", "梦话"]
        assert written[0]["content"].startswith("梦见：")
        assert written[0]["type"] == "long_term"

        # 清醒侧确实能检索到（用户约束 ③）
        retrieved = pipeline.retrieve(body["recall"]["title"])
        assert "梦见：" in retrieved.get("context_text", "")

        # 重复醒来 409，记忆不增
        assert _request(f"{base}/api/dreamtalk/sessions/{session_id}/wake", {}, "POST")[0] == 409
        assert _dream_count(store) == before + 1


def test_writeback_can_be_disabled(tmp_path):
    with running_server(tmp_path, seed_memories=_SEED) as (base, store, _pipeline, _config):
        _request(f"{base}/api/dreamtalk/config", {"memory_writeback": False}, "PUT")
        before = _dream_count(store)
        status, body = _request(f"{base}/api/dreamtalk/sessions", {"mode": "memory"}, "POST")
        session_id = body["session"]["id"]
        status, body = _request(f"{base}/api/dreamtalk/sessions/{session_id}/wake", {}, "POST")
        assert status == 200
        assert _dream_count(store) == before


def test_dreamless_never_writes_back(tmp_path):
    with running_server(tmp_path, seed_memories=_SEED) as (base, store, _pipeline, _config):
        before = _dream_count(store)
        status, body = _request(f"{base}/api/dreamtalk/sessions", {"mode": "dreamless"}, "POST")
        assert status == 200
        assert body["turns"] == []
        assert body["session"]["status"] == "ended"
        assert _dream_count(store) == before


# ------------------------------------------------------------------ 通道隔离
def test_offline_prompts_never_become_dream_turns(tmp_path):
    cloud = _FakeCloud(error=CloudUnavailableError("boom"))
    with running_server(tmp_path, seed_memories=_SEED, cloud=cloud) as (
        base, _store, _pipeline, _config
    ):
        # 注入了 cloud 的默认运行态：梦境自建独立 fallback
        status, body = _request(f"{base}/api/dreamtalk/sessions", {"mode": "memory"}, "POST")
        assert status == 200
        for turn in body["turns"]:
            assert turn["content"].strip()
            assert turn["content"] not in _OFFLINE_TEXTS


def test_dream_does_not_touch_chat_channel_state(tmp_path):
    cloud = _FakeCloud(error=CloudUnavailableError("boom"))
    chat_fallback = OfflineFallbackManager(cloud=cloud, config=None)
    before = (chat_fallback.mode, chat_fallback.status)

    data_dir = str(tmp_path)
    store, pipeline, manager, _remote = build_deps(data_dir=data_dir)
    config = ConfigManager(config_path=str(tmp_path / "config.json"), data_dir=data_dir)
    for text, fields in _SEED:
        pipeline.manager.add_memory(content=text, **fields)
    runtime = build_dreamtalk_runtime(
        config, store, pipeline.manager, data_dir=data_dir, cloud=cloud
    )
    handler = make_handler(
        store, pipeline, manager, config=config,
        chat_cloud=cloud, chat_fallback=chat_fallback, dreamtalk=runtime,
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        status, body = _request(f"{base}/api/dreamtalk/sessions", {"mode": "memory"}, "POST")
        assert status == 200
        _request(f"{base}/api/dreamtalk/sessions/{body['session']['id']}/wake", {}, "POST")
        assert (chat_fallback.mode, chat_fallback.status) == before
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        store.close()
        close = getattr(pipeline.vector_store, "close", None)
        if callable(close):
            close()


# ------------------------------------------------------------------ 锁
class _SlowRuntime:
    """慢梦境运行态：验证梦境不占用默认 _state_lock。"""

    def __init__(self):
        self.started = threading.Event()

    def status(self):
        return {"status": "idle", "enabled": True, "contract_version": "1.0",
                "sessions": 0, "mode_weights": "", "memory_writeback": False}

    def is_enabled(self):
        return True

    async def start_session(self, mode="auto"):
        self.started.set()
        time.sleep(1.5)
        raise RuntimeError("slow dream aborted")


def test_dream_lock_does_not_block_state_endpoints(tmp_path):
    slow = _SlowRuntime()
    with running_server(tmp_path, seed_memories=_SEED, dreamtalk=slow) as (base, _store, _pipeline, _config):
        result = {}

        def _post():
            result["status"] = _request(
                f"{base}/api/dreamtalk/sessions", {"mode": "auto"}, "POST", timeout=10
            )[0]

        worker = threading.Thread(target=_post, daemon=True)
        worker.start()
        assert slow.started.wait(3), "梦境请求未进入服务"
        started = time.monotonic()
        status, _body = _request(f"{base}/api/memories", timeout=1.5)
        elapsed = time.monotonic() - started
        assert status == 200
        assert elapsed < 1.0, f"梦境在途时 /api/memories 被阻塞 {elapsed:.2f}s（落入 _state_lock）"
        worker.join(timeout=5)
