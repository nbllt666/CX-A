"""Regression coverage for vision inference during local model replacement."""

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from lite.server import api_server


def test_vision_resolves_runtime_after_acquiring_chat_lock(tmp_path, monkeypatch):
    class TrackedLock:
        def __init__(self):
            self.lock = threading.RLock()
            self.entering = threading.Event()

        def __enter__(self):
            self.entering.set()
            self.lock.acquire()
            return self

        def __exit__(self, *_):
            self.lock.release()

    calls = []
    old_runtime = SimpleNamespace(offline_chat=lambda messages: calls.append("old"))
    # 20261010 KV 隔离改道：视觉理解走 vision_chat（视觉专用实例）——fake 同步接口
    new_runtime = SimpleNamespace(
        offline_chat=lambda messages: calls.append("offline"),
        vision_chat=lambda messages: "new response",
    )

    class Holder:
        runtime = old_runtime

        def get(self):
            calls.append("get")
            return self.runtime

    holder = Holder()
    captured = {}

    def build_vision_pipeline(**kwargs):
        captured["understand"] = kwargs["local_understanding"]
        return SimpleNamespace()

    monkeypatch.setattr(api_server, "threading", SimpleNamespace(RLock=TrackedLock))
    monkeypatch.setattr(api_server, "_LocalChatRuntimeHolder", lambda: holder)
    monkeypatch.setattr(api_server, "build_vision_pipeline", build_vision_pipeline)
    monkeypatch.setattr(api_server, "start_vision_tick_thread", lambda pipeline: None)
    dependency = SimpleNamespace(data_dir=str(tmp_path))
    handler = api_server.make_handler(
        store=dependency, pipeline=dependency, manager=dependency,
        remote=dependency, computer=dependency, authorizer=dependency,
        bridge=dependency, config=dependency, registry=dependency,
        distiller=dependency, voice=dependency, cxfc=dependency,
        chat_cloud=dependency, chat_fallback=dependency,
        download_manager=dependency,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        with handler._chat_lock:
            handler._chat_lock.entering.clear()
            future = executor.submit(captured["understand"], [{"role": "user", "content": "image"}])
            assert handler._chat_lock.entering.wait(2)
            assert calls == []
            assert not future.done()
            holder.runtime = new_runtime
        assert future.result(timeout=2) == "new response"
    assert calls == ["get"]
