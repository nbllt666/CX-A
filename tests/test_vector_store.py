# -*- coding: utf-8 -*-
"""InMemoryVectorStore、LanceVectorStore 与 SQLiteVectorStore 的 upsert / search / delete、top_k、相关性排序测试。"""

import sqlite3
import sys

import pytest

from lite.memory.vector_store import (
    InMemoryVectorStore,
    LanceVectorStore,
    SQLiteVectorStore,
    VectorStore,
)


@pytest.fixture()
def vs():
    return InMemoryVectorStore()


def test_upsert_and_search_relevance(vs):
    vs.upsert("v1", [1.0, 0.0])
    vs.upsert("v2", [0.0, 1.0])
    res = vs.search([1.0, 0.0], top_k=2)
    assert len(res) == 2
    # v1 与查询同向，相关性最高
    assert res[0]["vector_id"] == "v1"
    assert res[0]["score"] > res[1]["score"]


def test_top_k_limits(vs):
    for i in range(10):
        vs.upsert(f"v{i}", [float(i), 0.0, 0.0])
    res = vs.search([10.0, 0.0, 0.0], top_k=3)
    assert len(res) == 3
    # 相关性降序
    scores = [r["score"] for r in res]
    assert scores == sorted(scores, reverse=True)


def test_scores_are_cosine_similarity(vs):
    vs.upsert("a", [1.0, 0.0])
    vs.upsert("b", [1.0, 1.0])
    res = vs.search([1.0, 0.0], top_k=2)
    a = next(r for r in res if r["vector_id"] == "a")
    b = next(r for r in res if r["vector_id"] == "b")
    assert abs(a["score"] - 1.0) < 1e-9
    half = 1.0 / (1.0 * 2 ** 0.5)
    assert abs(b["score"] - half) < 1e-9


def test_upsert_overwrite(vs):
    vs.upsert("v1", [1.0, 0.0])
    vs.upsert("v1", [0.0, 1.0])
    res = vs.search([0.0, 1.0], top_k=1)
    assert res[0]["vector_id"] == "v1"
    assert abs(res[0]["score"] - 1.0) < 1e-9


def test_delete(vs):
    vs.upsert("v1", [1.0, 0.0])
    vs.upsert("v2", [0.0, 1.0])
    assert vs.delete("v1") is True
    ids = [r["vector_id"] for r in vs.search([1.0, 0.0], top_k=10)]
    assert "v1" not in ids
    assert "v2" in ids
    # 再删不存在返回 False
    assert vs.delete("v1") is False


def test_metadata_roundtrip(vs):
    vs.upsert("v1", [1.0, 0.0], metadata={"memory_id": 7})
    res = vs.search([1.0, 0.0], top_k=1)
    assert res[0]["metadata"] == {"memory_id": 7}


def test_empty_store(vs):
    assert vs.search([1.0, 0.0]) == []
    assert vs.delete("ghost") is False


def test_zero_vector(vs):
    vs.upsert("v1", [0.0, 0.0])
    res = vs.search([1.0, 0.0], top_k=1)
    assert res[0]["vector_id"] == "v1"
    assert res[0]["score"] == 0.0


def test_abstract_base_not_instantiable():
    with pytest.raises(TypeError):
        VectorStore()


# ---------------------------------------------------------------- 20260926 深挖加固：脏向量行容错
def test_sqlite_search_tolerates_corrupt_blob(tmp_path):
    """损坏 / 非法长度 blob 的向量行按 0 分跳过，不使整次检索失败（此前会 400）。"""
    from lite.memory.vector_store import SQLiteVectorStore

    db = str(tmp_path / "v.db")
    vs = SQLiteVectorStore(db_path=db, model_tag="tag", dim=2)
    vs.prepare()
    vs.upsert("good", [1.0, 0.0])
    vs.upsert("bad", [1.0, 0.0])

    conn = sqlite3.connect(db)
    conn.execute("UPDATE memory_vectors SET vector=? WHERE vector_id='bad'", (b"\x00" * 10,))
    conn.commit()
    conn.close()

    res = vs.search([1.0, 0.0], top_k=5)
    by_id = {r["vector_id"]: r["score"] for r in res}
    assert by_id["good"] == pytest.approx(1.0, abs=1e-6)
    assert by_id["bad"] == 0.0
    assert vs._corrupt_warned is True  # 脏行告警已标记（去重）


def test_lance_requires_lancedb(monkeypatch):
    """模拟 lancedb 缺失时，LanceVectorStore 实例化应抛出带安装提示的 RuntimeError。"""
    monkeypatch.setitem(sys.modules, "lancedb", None)  # import 将触发 ImportError
    with pytest.raises(RuntimeError) as exc_info:
        LanceVectorStore(db_path=":memory:")
    assert "pip install lancedb" in str(exc_info.value)


lancedb = pytest.importorskip("lancedb", reason="真实 lancedb 未安装，跳过真实后端冒烟")


def test_lance_real_backend_smoke(tmp_path):
    """真实 lancedb 后端：upsert（含首次惰性建表）/ search / delete / metadata 往返。"""
    vs = LanceVectorStore(db_path=str(tmp_path / "lancedb"))
    assert vs.upsert("a", [1.0, 0.0], {"t": "x"}) is True
    assert vs.upsert("b", [0.0, 1.0], {"t": "y"}) is True
    res = vs.search([1.0, 0.0], top_k=2)
    assert len(res) == 2
    # L2 距离升序：与查询完全一致的 a 应排最前
    assert res[0]["vector_id"] == "a"
    assert res[0]["metadata"] == {"t": "x"}
    assert vs.delete("a") is True
    ids = [r["vector_id"] for r in vs.search([1.0, 0.0], top_k=5)]
    assert "a" not in ids
    assert "b" in ids


def test_lance_overwrite_replaces(tmp_path):
    """真实 lancedb 后端：同 vector_id 覆盖后按新向量检索。"""
    vs = LanceVectorStore(db_path=str(tmp_path / "lancedb"))
    vs.upsert("v1", [1.0, 0.0])
    vs.upsert("v1", [0.0, 1.0])
    res = vs.search([0.0, 1.0], top_k=1)
    assert res[0]["vector_id"] == "v1"


def test_lance_delete_with_single_quote_filter(tmp_path):
    """G-5：vector_id 含单引号时 upsert/delete 不再因 filter 语法崩溃。"""
    vs = LanceVectorStore(db_path=str(tmp_path / "lancedb"))
    vid = "node'with'quote"
    assert vs.upsert(vid, [1.0, 0.0]) is True
    # 含引号的未命中 filter：转义后不抛异常
    assert vs.delete("missing'id") is True
    # 含引号的命中 filter：正确删除目标向量
    assert vs.delete(vid) is True
    res = vs.search([1.0, 0.0], top_k=5)
    assert all(r["vector_id"] != vid for r in res)


# ---------------------------------------------------------------------------
# SQLiteVectorStore：持久向量库（cosine 口径与 InMemoryVectorStore 一致）
# ---------------------------------------------------------------------------
@pytest.fixture()
def sq(tmp_path):
    """指向临时 db 文件的 SQLite 持久向量库，测试后关闭连接。"""
    store = SQLiteVectorStore(str(tmp_path / "memories.db"))
    yield store
    store.close()


def test_sqlite_upsert_and_search_relevance(sq):
    """upsert 后按 cosine 相关性降序检索（与 InMemory 同口径）。"""
    sq.upsert("v1", [1.0, 0.0])
    sq.upsert("v2", [0.0, 1.0])
    res = sq.search([1.0, 0.0], top_k=2)
    assert len(res) == 2
    # v1 与查询同向，相关性最高
    assert res[0]["vector_id"] == "v1"
    assert res[0]["score"] > res[1]["score"]


def test_sqlite_scores_are_cosine_similarity(sq):
    """score 为 cosine 相似度精确值（float32 存储有精度损耗，用 approx）。"""
    sq.upsert("a", [1.0, 0.0])
    sq.upsert("b", [1.0, 1.0])
    res = sq.search([1.0, 0.0], top_k=2)
    a = next(r for r in res if r["vector_id"] == "a")
    b = next(r for r in res if r["vector_id"] == "b")
    assert a["score"] == pytest.approx(1.0, abs=1e-6)
    assert b["score"] == pytest.approx(1.0 / (2 ** 0.5), abs=1e-6)


def test_sqlite_upsert_overwrite(sq):
    """同 vector_id 覆盖后按新向量检索。"""
    sq.upsert("v1", [1.0, 0.0])
    sq.upsert("v1", [0.0, 1.0])
    res = sq.search([0.0, 1.0], top_k=1)
    assert res[0]["vector_id"] == "v1"
    assert res[0]["score"] == pytest.approx(1.0, abs=1e-6)
    assert sq.count() == 1


def test_sqlite_delete(sq):
    """delete 命中返回 True，未命中返回 False。"""
    sq.upsert("v1", [1.0, 0.0])
    sq.upsert("v2", [0.0, 1.0])
    assert sq.delete("v1") is True
    ids = [r["vector_id"] for r in sq.search([1.0, 0.0], top_k=10)]
    assert "v1" not in ids
    assert "v2" in ids
    # 再删不存在返回 False
    assert sq.delete("v1") is False


def test_sqlite_metadata_roundtrip(sq):
    """metadata 经 JSON 落盘后往返一致（含非 ASCII）。"""
    sq.upsert("v1", [1.0, 0.0], metadata={"memory_id": 7, "标签": "记忆"})
    res = sq.search([1.0, 0.0], top_k=1)
    assert res[0]["metadata"] == {"memory_id": 7, "标签": "记忆"}


def test_sqlite_empty_store(sq):
    """空库 search 返回 []，delete 未命中返回 False。"""
    assert sq.search([1.0, 0.0]) == []
    assert sq.delete("ghost") is False
    assert sq.count() == 0


def test_sqlite_zero_vector(sq):
    """写入零向量：行范数为 0，score 记 0.0。"""
    sq.upsert("v1", [0.0, 0.0])
    res = sq.search([1.0, 0.0], top_k=1)
    assert res[0]["vector_id"] == "v1"
    assert res[0]["score"] == 0.0


def test_sqlite_dim_mismatch_scores_zero(sq):
    """查询与行维度不匹配时该行 score 记 0.0。"""
    sq.upsert("v3", [1.0, 1.0, 1.0])
    res = sq.search([1.0, 0.0], top_k=1)
    assert res[0]["vector_id"] == "v3"
    assert res[0]["score"] == 0.0


def test_sqlite_top_k_limits(sq):
    """top_k 限制条数、按相关性降序；top_k=0 / 负数返回 []。"""
    for i in range(10):
        sq.upsert(f"v{i}", [float(i), 0.0, 0.0])
    res = sq.search([10.0, 0.0, 0.0], top_k=3)
    assert len(res) == 3
    scores = [r["score"] for r in res]
    assert scores == sorted(scores, reverse=True)
    # 同分（v1~v9 均与查询同向）按 vector_id 字符串升序确定性 tie-break
    assert [r["vector_id"] for r in res] == ["v1", "v2", "v3"]
    assert sq.search([10.0, 0.0, 0.0], top_k=0) == []
    assert sq.search([10.0, 0.0, 0.0], top_k=-1) == []


def test_sqlite_cross_instance_persistence(tmp_path):
    """跨实例持久性：新开实例指向同一 db 文件仍能检索到旧向量。"""
    db = str(tmp_path / "memories.db")
    first = SQLiteVectorStore(db)
    first.upsert("v1", [1.0, 0.0], {"m": 1})
    first.close()

    second = SQLiteVectorStore(db)
    res = second.search([1.0, 0.0], top_k=1)
    assert res[0]["vector_id"] == "v1"
    assert res[0]["metadata"] == {"m": 1}
    assert second.count() == 1
    second.close()


def test_sqlite_count_and_vector_ids(sq):
    """count 与 vector_ids 反映当前向量集合。"""
    assert sq.count() == 0
    assert sq.vector_ids() == set()
    sq.upsert("v1", [1.0, 0.0])
    sq.upsert("v2", [0.0, 1.0])
    assert sq.count() == 2
    assert sq.vector_ids() == {"v1", "v2"}


def test_sqlite_prepare_three_states(tmp_path):
    """prepare 三态：init → ok →（换 model_tag）reset 且旧向量被清空。"""
    db = str(tmp_path / "memories.db")
    store = SQLiteVectorStore(db, model_tag="stub64", dim=64)
    # 无元记录 → init
    r1 = store.prepare()
    assert r1["action"] == "init"
    assert r1["previous"] is None
    assert r1["current"] == {"model_tag": "stub64", "dim": 64}

    store.upsert("v1", [1.0, 0.0])
    # 一致 → ok，不清空
    r2 = store.prepare()
    assert r2["action"] == "ok"
    assert r2["previous"] == {"model_tag": "stub64", "dim": 64}
    assert store.count() == 1

    # 换 model_tag → reset，清空旧向量并覆写元记录
    changed = SQLiteVectorStore(db, model_tag="real1024", dim=1024)
    r3 = changed.prepare()
    assert r3["action"] == "reset"
    assert r3["previous"] == {"model_tag": "stub64", "dim": 64}
    assert r3["current"] == {"model_tag": "real1024", "dim": 1024}
    assert changed.count() == 0
    # reset 后新写入可用
    changed.upsert("v2", [0.0, 1.0])
    assert changed.count() == 1
    assert changed.search([0.0, 1.0], top_k=1)[0]["vector_id"] == "v2"

    store.close()
    changed.close()


class _MinimalStore(VectorStore):
    """最小具体子类：仅用于验证基类 vector_ids 的默认（未实现）返回 None。"""

    def upsert(self, vector_id, vector, metadata=None):
        return True

    def search(self, vector, top_k=10):
        return []

    def delete(self, vector_id):
        return False


def test_vector_ids_optional_capability(sq):
    """基类 vector_ids 默认 None；InMemory / SQLite 已实现返回 id 集合。"""
    assert _MinimalStore().vector_ids() is None

    mem = InMemoryVectorStore()
    assert mem.vector_ids() == set()
    mem.upsert("v1", [1.0, 0.0])
    assert mem.vector_ids() == {"v1"}

    sq.upsert("a", [1.0, 0.0])
    assert sq.vector_ids() == {"a"}