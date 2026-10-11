from concurrent.futures import ThreadPoolExecutor

import pytest

from lite.memory.manager import MemoryManager, _tokenize_cached, tokenize_text
from lite.memory.pipeline import MemoryRetrievalPipeline
from lite.memory.scoring import score_memories
from lite.memory.storage import MemoryStore
from lite.memory.vector_store import InMemoryVectorStore, LanceVectorStore, SQLiteVectorStore
from lite.tools.builtin_registry import BuiltinToolRegistry


class Embed:
    def embed(self, texts):
        return [[1.0, 0.0] if text == "banana" else [0.0, 1.0] for text in texts]


def make_manager(tmp_path, vector_store=None, embed_fn=None):
    return MemoryManager(MemoryStore(str(tmp_path / "memories.db")), vector_store, embed_fn=embed_fn)


def test_update_refreshes_importance_embedding_and_agent(tmp_path):
    manager = make_manager(tmp_path, embed_fn=lambda text: Embed().embed([text])[0])
    mid = manager.add_memory("apple", importance=1)
    assert manager.update(mid, {"content": "banana", "importance": 5, "agent_id": "alice"}) == 1
    row = manager.store.get(mid)
    assert row["importance_score"] == 1.0
    assert row["content"] == "banana"
    hit = manager.vector_store.get(str(mid))
    assert hit["vector"] == [1.0, 0.0]
    assert hit["metadata"] == {"memory_id": mid, "agent_id": "alice"}


def test_update_embedding_failure_does_not_change_row(tmp_path):
    def embed(text):
        if text == "bad":
            raise RuntimeError("offline")
        return [1.0, 0.0]

    manager = make_manager(tmp_path, embed_fn=embed)
    mid = manager.add_memory("old", importance=1)
    before = manager.store.get(mid)
    with pytest.raises(RuntimeError, match="offline"):
        manager.update(mid, {"content": "bad", "importance": 5})
    assert manager.store.get(mid) == before
    assert manager.vector_store.get(str(mid))["vector"] == [1.0, 0.0]


def test_update_restores_vector_when_sqlite_update_fails(tmp_path, monkeypatch):
    manager = make_manager(tmp_path, embed_fn=lambda text: Embed().embed([text])[0])
    mid = manager.add_memory("apple")
    before = manager.store.get(mid)
    old_vector = manager.vector_store.get(str(mid))

    def fail(*args):
        raise RuntimeError("database failure")

    monkeypatch.setattr(manager.store, "update", fail)
    with pytest.raises(RuntimeError, match="database failure"):
        manager.update(mid, {"content": "banana", "agent_id": "alice"})
    assert manager.store.get(mid) == before
    assert manager.vector_store.get(str(mid)) == old_vector


def test_update_without_embedder_removes_stale_index(tmp_path):
    manager = make_manager(tmp_path)
    mid = manager.add_memory("old", embed_fn=lambda text: [1.0, 0.0])
    manager.update(mid, {"content": "new"})
    assert manager.vector_store.get(str(mid)) is None


def test_update_none_embedding_removes_stale_index(tmp_path):
    manager = make_manager(tmp_path, embed_fn=lambda text: None)
    mid = manager.add_memory("old", embed_fn=lambda text: [1.0, 0.0])
    assert manager.update(mid, {"content": "new"}) == 1
    assert manager.store.get(mid)["content"] == "new"
    assert manager.vector_store.get(str(mid)) is None


def test_vector_write_failure_restores_previous_snapshot(tmp_path):
    class FailsAfterMutation(InMemoryVectorStore):
        fail_once = False

        def upsert(self, *args, **kwargs):
            result = super().upsert(*args, **kwargs)
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError("write failed after mutation")
            return result

    vectors = FailsAfterMutation()
    manager = make_manager(tmp_path, vectors, embed_fn=lambda text: Embed().embed([text])[0])
    mid = manager.add_memory("apple")
    before = manager.store.get(mid)
    snapshot = vectors.get(str(mid))
    vectors.fail_once = True
    with pytest.raises(RuntimeError, match="after mutation"):
        manager.update(mid, {"content": "banana"})
    assert manager.store.get(mid) == before
    assert vectors.get(str(mid)) == snapshot


def test_memory_update_tool_uses_manager(tmp_path):
    manager = make_manager(tmp_path, embed_fn=lambda text: Embed().embed([text])[0])
    mid = manager.add_memory("apple", importance=1)
    registry = BuiltinToolRegistry(memory_store=manager.store, manager=manager)
    result = registry.call("memory_update", {"id": mid, "content": "banana", "importance": 5})
    assert result["success"]
    assert manager.store.get(mid)["importance_score"] == 1.0
    assert manager.vector_store.get(str(mid))["vector"] == [1.0, 0.0]


def test_custom_weights_are_applied_before_limit_and_type_filter(tmp_path):
    store = MemoryStore(str(tmp_path / "memories.db"))
    vectors = InMemoryVectorStore()
    pipeline = MemoryRetrievalPipeline(store, vectors, Embed(), max_memories=1)
    highest = store.add({"type": "permanent", "content": "irrelevant", "importance_score": 1.0})
    relevant = store.add({"type": "short_term", "content": "banana", "importance_score": 0.8})
    vectors.upsert(str(highest), [0.0, 1.0])
    vectors.upsert(str(relevant), [1.0, 0.0])
    assert pipeline.retrieve("banana")["memories"][0]["id"] == relevant
    result = pipeline.retrieve("banana", importance_weight=1, time_weight=0, relevance_weight=0, limit=2)
    assert [row["id"] for row in result["memories"]] == [highest, relevant]
    assert result["total"] == 2
    result = pipeline.retrieve("banana", mem_type="permanent", limit=10)
    assert [row["id"] for row in result["memories"]] == [highest]


def test_default_agent_scope_is_explicit(tmp_path):
    store = MemoryStore(str(tmp_path / "memories.db"))
    pipeline = MemoryRetrievalPipeline(store, InMemoryVectorStore(), Embed())
    pipeline.add("public banana", agent_id="default")
    pipeline.add("alice secret", agent_id="alice")
    assert {row["agent_id"] for row in pipeline.retrieve("banana")["memories"]} == {"default"}


def test_top_k_scoring_matches_full_stable_sort():
    rows = [{"id": index, "importance_score": (index % 3) / 3, "permanent": True} for index in range(100)]
    expected = score_memories([dict(row) for row in rows])[:17]
    actual = score_memories([dict(row) for row in rows], limit=17)
    assert [row["id"] for row in actual] == [row["id"] for row in expected]


def test_token_cache_cannot_be_mutated_by_callers():
    _tokenize_cached.cache_clear()
    tokenize_text("banana apple").clear()
    assert tokenize_text("banana apple") == {"banana", "apple"}
    assert _tokenize_cached.cache_info().hits == 1


@pytest.mark.parametrize("backend", [InMemoryVectorStore, SQLiteVectorStore])
def test_vector_snapshots_and_concurrent_storage_writes(tmp_path, backend):
    vectors = backend() if backend is InMemoryVectorStore else backend(str(tmp_path / "vectors.db"))
    store = MemoryStore(str(tmp_path / "memories.db"))

    def write(index):
        mid = store.add({"type": "long_term", "content": f"memory {index}"})
        vectors.upsert(str(mid), [float(index), 1.0], metadata={"id": mid})
        assert vectors.get(str(mid))["metadata"] == {"id": mid}
        vectors.search([1.0, 0.0], top_k=3)
        return mid

    with ThreadPoolExecutor(max_workers=8) as executor:
        ids = list(executor.map(write, range(80)))
    assert len(set(ids)) == 80
    assert len(store.list()) == 80
    assert len(vectors.vector_ids()) == 80


def test_lance_identity_persists_and_rebuilds_on_model_or_dimension_change(tmp_path):
    pytest.importorskip("lancedb")
    path = str(tmp_path / "lancedb")
    store = LanceVectorStore(path, model_tag="model-a", dim=2)
    assert store.prepare()["action"] == "init"
    store.upsert("1", [1.0, 0.0], metadata={"agent_id": "default"})
    store.upsert("1", [0.0, 1.0], metadata={"agent_id": "alice"})
    assert store.get("1")["metadata"] == {"agent_id": "alice"}
    reopened = LanceVectorStore(path, model_tag="model-a", dim=2)
    assert reopened.prepare()["action"] == "ok"
    assert reopened.vector_ids() == {"1"}
    changed = LanceVectorStore(path, model_tag="model-b", dim=2)
    assert changed.prepare()["action"] == "reset"
    assert changed.vector_ids() == set()
    changed.upsert("2", [1.0, 0.0])
    resized = LanceVectorStore(path, model_tag="model-b", dim=3)
    assert resized.prepare()["action"] == "reset"
    resized.upsert("3", [1.0, 0.0, 0.0])
    assert resized.vector_ids() == {"3"}


def test_lance_legacy_vectors_without_identity_are_not_reused(tmp_path):
    pytest.importorskip("lancedb")
    path = str(tmp_path / "lancedb")
    legacy = LanceVectorStore(path)
    legacy.upsert("legacy", [1.0, 0.0])
    prepared = LanceVectorStore(path, model_tag="known", dim=2)
    assert prepared.prepare()["action"] == "reset"
    assert prepared.vector_ids() == set()
