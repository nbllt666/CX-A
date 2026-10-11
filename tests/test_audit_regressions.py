"""Regression coverage for conversation isolation and concurrent voice requests."""

import json
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from lite.cloud.adapter import CloudUnavailableError
from lite.cloud.fallback import OfflineFallbackManager, OFFLINE_PROMPT
from lite.server import api_server
from tests.test_chat_api import _FakeCloud, http_get, http_post, running_server


def read_stream(base, message="hello"):
    request = urllib.request.Request(
        f"{base}/api/chat/message_stream",
        data=json.dumps({"message": message}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return [json.loads(line) for line in response if line.strip()]


def test_chat_inference_receives_previous_turn(tmp_path):
    cloud = _FakeCloud(chunks=["remembered"])
    with running_server(tmp_path, cloud=cloud) as (base, _):
        http_post(f"{base}/api/chat/message", {"message": "My name is Alice"})
        http_post(f"{base}/api/chat/message", {"message": "What is my name?"})
    assert cloud.calls[1][1:] == [
        {"role": "user", "content": "My name is Alice"},
        {"role": "assistant", "content": "remembered"},
        {"role": "user", "content": "What is my name?"},
    ]


def test_chat_and_history_are_agent_scoped(tmp_path):
    cloud = _FakeCloud(chunks=["reply"])
    with running_server(tmp_path, cloud=cloud) as (base, _):
        http_post(f"{base}/api/chat/message", {"message": "private", "agent_id": "alice"})
        http_post(f"{base}/api/chat/message", {"message": "default"})
        http_post(f"{base}/api/chat/message", {"message": "manage", "agent_id": "memory-agent"})
        _, default_history = http_get(f"{base}/api/chat/history")
        _, alice_history = http_get(f"{base}/api/chat/history?agent_id=alice")
    assert cloud.calls[1][1:] == [{"role": "user", "content": "default"}]
    assert cloud.calls[2][1:] == [{"role": "user", "content": "manage"}]
    assert [entry["content"] for entry in default_history["messages"]] == ["default", "reply"]
    assert [entry["content"] for entry in alice_history["messages"]] == ["private", "reply"]


def test_context_is_bounded_and_accepts_legacy_history(tmp_path):
    path = tmp_path / "history.json"
    legacy = [{"role": "assistant", "content": "orphan"}, None]
    for index in range(30):
        legacy.extend([
            {"role": "user", "content": f"u{index}"},
            {"role": "assistant", "content": f"a{index}"},
        ])
    path.write_text(json.dumps(legacy), encoding="utf-8")
    context = api_server._chat_context(str(path), "default")
    assert len(context) == api_server._CHAT_CONTEXT_MESSAGES
    assert context[0]["content"] == "u20"
    assert api_server._chat_context(str(path), "alice") == []
    legacy[-1]["content"] = "x" * (api_server._CHAT_CONTEXT_CHARS + 1)
    path.write_text(json.dumps(legacy), encoding="utf-8")
    assert api_server._chat_context(str(path), "default") == []


def test_stream_reads_final_offline_status_and_does_not_save_hint(tmp_path):
    cloud = _FakeCloud(online=False)
    fallback = OfflineFallbackManager(cloud=cloud)
    assert fallback.status == "cloud"
    with running_server(tmp_path, cloud=cloud, chat_fallback=fallback) as (base, _):
        frames = read_stream(base)
        _, history = http_get(f"{base}/api/chat/history")
    assert frames[-1]["offline"] is True
    assert frames[-1]["clean_text"] == OFFLINE_PROMPT
    assert history["messages"] == []


def test_failed_stream_returns_partial_text_without_persisting(tmp_path):
    class FailingCloud(_FakeCloud):
        def chat(self, messages):
            yield "partial"
            raise CloudUnavailableError("disconnected")

    cloud = FailingCloud()
    fallback = SimpleNamespace(chat=cloud.chat, status="cloud")
    with running_server(tmp_path, cloud=cloud, chat_fallback=fallback) as (base, _):
        frames = read_stream(base)
        _, history = http_get(f"{base}/api/chat/history")
    assert frames[-1]["clean_text"] == "partial"
    assert frames[-1]["incomplete"] is True
    assert frames[-1]["ok"] is False
    assert history["messages"] == []


def test_done_refresh_waits_for_history_persistence(tmp_path, monkeypatch):
    append_started = threading.Event()
    done_seen = threading.Event()
    reader_started = threading.Event()
    release = threading.Event()
    append = api_server._append_chat_history
    load = api_server._load_chat_history

    def delayed_append(*args, **kwargs):
        append_started.set()
        assert release.wait(5)
        return append(*args, **kwargs)

    def observed_load(*args, **kwargs):
        if append_started.is_set():
            reader_started.set()
        return load(*args, **kwargs)

    def consume(base):
        request = urllib.request.Request(
            f"{base}/api/chat/message_stream",
            data=b'{"message":"hello"}',
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            for line in response:
                if json.loads(line).get("done"):
                    done_seen.set()

    monkeypatch.setattr(api_server, "_append_chat_history", delayed_append)
    monkeypatch.setattr(api_server, "_load_chat_history", observed_load)
    with running_server(tmp_path, cloud=_FakeCloud(chunks=["reply"])) as (base, _):
        with ThreadPoolExecutor(max_workers=2) as executor:
            stream = executor.submit(consume, base)
            try:
                assert append_started.wait(2)
                assert done_seen.wait(2)
                refresh = executor.submit(http_get, f"{base}/api/chat/history")
                assert reader_started.wait(2)
                with pytest.raises(TimeoutError):
                    refresh.result(timeout=0.05)
            finally:
                release.set()
            stream.result(timeout=2)
            assert [row["content"] for row in refresh.result(timeout=2)[1]["messages"]] == ["hello", "reply"]


def test_tts_and_health_complete_while_chat_stream_is_open(tmp_path):
    waiting = threading.Event()
    release = threading.Event()
    synthesized = threading.Event()

    class SlowCloud(_FakeCloud):
        def chat(self, messages):
            yield "first sentence."
            waiting.set()
            assert release.wait(5)
            yield "second sentence."

    def synthesize(text, voice):
        synthesized.set()
        return b"RIFFtest"

    voice = SimpleNamespace(tts=SimpleNamespace(synthesize=synthesize))
    with running_server(tmp_path, cloud=SlowCloud(), voice=voice) as (base, _):
        with ThreadPoolExecutor(max_workers=3) as executor:
            stream = executor.submit(read_stream, base)
            try:
                assert waiting.wait(2)
                health = executor.submit(http_get, f"{base}/api/health")
                speech = executor.submit(http_post, f"{base}/api/voice/synthesize", {"text": "first"})
                assert health.result(timeout=1)[0] == 200
                assert speech.result(timeout=1)[0] == 200
                assert synthesized.is_set()
                assert not stream.done()
            finally:
                release.set()
            assert stream.result(timeout=2)[-1]["ok"] is True


def test_production_server_uses_threaded_requests(tmp_path, monkeypatch):
    marker = object()
    monkeypatch.setattr(api_server, "create_app", lambda *a, **k: (None, None, marker))
    server = api_server.create_server(port=0, data_dir=str(tmp_path))
    try:
        assert isinstance(server, api_server.ThreadingHTTPServer)
        assert server.RequestHandlerClass is marker
    finally:
        server.server_close()


@pytest.mark.parametrize("custom", ["absolute", "relative"])
def test_lance_assembly_uses_configured_path_and_embedding_identity(tmp_path, monkeypatch, custom):
    captured = {}

    class FakeLance:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def prepare(self):
            captured["prepared"] = True
            return {"action": "ready"}

    from lite.memory import vector_store

    monkeypatch.setattr(vector_store, "LanceVectorStore", FakeLance)
    monkeypatch.setattr(api_server.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(api_server, "app_root", lambda: str(tmp_path))
    raw_path = str(tmp_path / "custom-index") if custom == "absolute" else "custom-index"
    config = SimpleNamespace(get=lambda section, key, default=None: {
        ("vector", "backend"): "lancedb", ("vector", "path"): raw_path,
    }.get((section, key), default))
    api_server._build_vector_store(config, str(tmp_path / "data"), "llama", {"dim": 3, "model_tag": "new"})
    assert captured == {
        "db_path": str(tmp_path / "custom-index"), "model_tag": "new", "dim": 3, "prepared": True,
    }


def test_memory_update_and_3d_query_use_fresh_scores_and_filters(tmp_path):
    with running_server(tmp_path, cloud=_FakeCloud()) as (base, _):
        _, first = http_post(f"{base}/api/memories", {
            "content": "shared query", "memory_type": "permanent", "importance": 1,
        })
        _, second = http_post(f"{base}/api/memories", {
            "content": "shared query short term", "memory_type": "short_term",
        })
        assert second["id"] is not None
        http_post(f"{base}/api/memories", {"content": "shared query", "agent_id": "other"})
        status, updated = http_post(
            f"{base}/api/memories/{first['id']}", {"importance": 5}, method="PUT",
        )
        assert status == 200
        assert updated["memory"]["importance_score"] == 1.0
        query = urlencode({"query": "shared query", "type": "permanent", "w_importance": 1, "w_time": 0, "w_rel": 0})
        status, result = http_get(f"{base}/api/memories/3d?{query}")
        assert status == 200
        assert result["degraded"] is False
        assert [row["id"] for row in result["memories"]] == [first["id"]]


def test_3d_fallback_limits_scoring_and_preserves_total(tmp_path, monkeypatch):
    score = api_server._memory_score_3d
    limits = []

    def observed_score(*args, **kwargs):
        limits.append(kwargs.get("limit"))
        return score(*args, **kwargs)

    monkeypatch.setattr(api_server, "_memory_score_3d", observed_score)
    with running_server(tmp_path, cloud=_FakeCloud()) as (base, _):
        for index in range(3):
            http_post(f"{base}/api/memories", {"content": f"unique {index}", "importance": index + 1})
        _, strongest = http_post(f"{base}/api/memories", {"content": "strongest", "importance": 5})
        query = urlencode({"limit": 1, "w_importance": 1, "w_time": 0, "w_rel": 0})
        status, result = http_get(f"{base}/api/memories/3d?{query}")
    assert status == 200
    assert limits == [1]
    assert result["total"] == 4
    assert [row["id"] for row in result["memories"]] == [strongest["id"]]
