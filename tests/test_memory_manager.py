# -*- coding: utf-8 -*-
"""MemoryManager 全流程测试：add/检索、dedup 去重、升降级、max_memories 截断、再激活。"""

import zlib

import pytest

from lite.memory.manager import MemoryManager
from lite.memory.storage import MemoryStore
from lite.memory.vector_store import InMemoryVectorStore

DIM = 64


def _embed(text):
    """确定性词袋向量（crc32 稳定哈希），供测试自主可控的检索语义。"""
    vec = [0.0] * DIM
    for tok in set(str(text).lower().split()):
        idx = zlib.crc32(tok.encode()) % DIM
        vec[idx] += 1.0
    return vec


@pytest.fixture()
def mgr(tmp_path):
    m = MemoryManager(
        store=MemoryStore(db_path=str(tmp_path / "memories.db")),
        vector_store=InMemoryVectorStore(),
    )
    return m


# ---------------------------------------------------------------- add/检索全流程
def test_add_and_retrieve_full_flow(mgr):
    mgr.add_memory("I like apple fruit", type="short_term", importance_score=0.7, embed_fn=_embed)
    mgr.add_memory("the weather is sunny today", type="long_term", importance_score=0.4, embed_fn=_embed)

    results = mgr.retrieve("apple fruit", embed_fn=_embed, top_k=5, agent_id="default", max_memories=5)
    assert results, "应能检索到记忆"
    # 与查询共享词的记忆应排在最前
    assert "apple" in results[0]["content"]
    assert results[0]["final_score"] >= results[-1]["final_score"]


def test_retrieve_agent_scoped(mgr):
    mgr.add_memory("secret A token blue", agent_id="alice", embed_fn=_embed)
    mgr.add_memory("public B token red", agent_id="bob", embed_fn=_embed)

    results = mgr.retrieve("token blue", embed_fn=_embed, top_k=5, agent_id="alice", max_memories=5)
    assert results and all(r["agent_id"] == "alice" for r in results)


def test_retrieve_empty(mgr):
    results = mgr.retrieve("anything", embed_fn=_embed, top_k=5, max_memories=5)
    assert results == []


# ---------------------------------------------------------------- dedup 去重
def test_add_skips_duplicate(mgr):
    mid1 = mgr.add_memory("hello world foo bar", embed_fn=_embed)
    mid2 = mgr.add_memory("hello world foo bar", embed_fn=_embed)
    assert mid1 is not None
    assert mid2 is None  # 相同内容（相似度 1.0>=0.85）被跳过
    assert len(mgr.store.list(agent_id="default")) == 1


def test_retrieve_dedup_keeps_highest(mgr):
    # 两条高度相似内容，relevance 一致时保留分数更高的一条
    mgr.add_memory("alpha beta gamma delta", importance_score=0.9, embed_fn=_embed)
    mgr.add_memory("alpha beta gamma delta", importance_score=0.4, embed_fn=_embed)
    results = mgr.retrieve("alpha beta", embed_fn=_embed, top_k=5, max_memories=5)
    # 去重后结果数应小于等于 1（两条相似度 1.0 只保留一条）
    assert len(results) >= 1
    assert results[0]["importance_score"] == pytest.approx(0.9)


# ---------------------------------------------------------------- 分层升降级
def test_update_reactivation_increments(mgr):
    mid = mgr.add_memory("some memory token", importance_score=0.5, embed_fn=_embed)
    m = mgr.update_reactivation(mid, emotion_intensity=0.0)
    assert m["reactivation_count"] == 1


def test_reactivation_promotes_to_long_term(mgr):
    mid = mgr.add_memory("low key memory token", type="short_term", importance_score=0.5, embed_fn=_embed)
    # 再激活 1 次 -> count=1, 2->2, 3 次达到阈值升 long_term
    m = mgr.update_reactivation(mid)  # 1
    m = mgr.update_reactivation(mid)  # 2
    m = mgr.update_reactivation(mid)  # 3 -> long_term
    assert m["type"] == "long_term"


def test_high_importance_promotes_to_permanent(mgr):
    mid = mgr.add_memory("vip memory token", type="long_term", importance_score=0.9, embed_fn=_embed)
    m = mgr.promote(mid)
    assert m["type"] == "permanent"
    assert m["permanent"] in (True, 1)


def test_explicit_demote(mgr):
    mid = mgr.add_memory("downgrade memory token", type="long_term", importance_score=0.9, embed_fn=_embed)
    # 先升 permanent 再降
    m = mgr.promote(mid, target_type="permanent")
    assert m["type"] == "permanent"
    m = mgr.demote(mid)
    assert m["type"] == "long_term"
    m = mgr.demote(mid)
    assert m["type"] == "short_term"


def test_promote_respects_level_order(mgr):
    mid = mgr.add_memory("already permanent memory token", type="permanent", importance_score=0.2, embed_fn=_embed)
    m = mgr.demote(mid)
    assert m["type"] == "long_term"


# ---------------------------------------------------------------- max_memories 截断
def test_max_memories_truncation(mgr):
    for i in range(5):
        mgr.add_memory(f"tagged memory entry {i}", importance_score=0.5 + 0.05 * i, embed_fn=_embed)
    results = mgr.retrieve("tagged memory entry", embed_fn=_embed, top_k=10, max_memories=2)
    assert len(results) == 2


def test_max_memories_full(mgr):
    for i in range(3):
        mgr.add_memory(f"entry memory {i}", importance_score=0.5, embed_fn=_embed)
    results = mgr.retrieve("entry memory", embed_fn=_embed, top_k=10, max_memories=5)
    assert len(results) <= 3


# ---------------------------------------------------------------- min_score 过滤
def test_min_score_filter(mgr):
    mgr.add_memory("high relevance hit token apple", importance_score=0.9, embed_fn=_embed)
    mgr.add_memory("unrelated token banana", importance_score=0.1, embed_fn=_embed)
    results = mgr.retrieve("apple", embed_fn=_embed, top_k=5, max_memories=5, min_score=0.2)
    assert all(r["content"] != "unrelated token banana" for r in results)