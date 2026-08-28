# -*- coding: utf-8 -*-
"""InMemoryVectorStore 与 LanceVectorStore 的 upsert / search / delete、top_k、相关性排序测试。"""

import sys

import pytest

from lite.memory.vector_store import (
    InMemoryVectorStore,
    LanceVectorStore,
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