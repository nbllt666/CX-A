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
    mid = mgr.add_memory("vip memory token", type="long_term", importance_score=0.96, embed_fn=_embed)
    m = mgr.promote(mid)
    assert m["type"] == "permanent"
    assert m["permanent"] in (True, 1)


def test_permanent_threshold_aligned_to_095(mgr):
    """永久晋级 importance 阈值对齐 permanent_threshold=0.95：0.94 不升、0.96 升。"""
    mid_low = mgr.add_memory("edge low memory token", type="long_term", importance_score=0.94, embed_fn=_embed)
    m_low = mgr.promote(mid_low)
    assert m_low["type"] == "long_term"  # 0.94 < 0.95 不升 permanent

    mid_high = mgr.add_memory("edge high memory token", type="long_term", importance_score=0.96, embed_fn=_embed)
    m_high = mgr.promote(mid_high)
    assert m_high["type"] == "permanent"  # 0.96 >= 0.95 升 permanent


def test_permanent_threshold_custom_override(mgr, tmp_path):
    """构造传入 permanent_threshold 可覆盖默认 0.95。"""
    m = MemoryManager(
        store=MemoryStore(db_path=str(tmp_path / "custom.db")),
        vector_store=InMemoryVectorStore(),
        permanent_threshold=0.80,
    )
    mid = m.add_memory("custom threshold memory", type="long_term", importance_score=0.82, embed_fn=_embed)
    mem = m.promote(mid)
    assert mem["type"] == "permanent"


def test_dedup_threshold_custom_override(mgr, tmp_path):
    """构造传入 dedup_threshold 可覆盖默认 0.85（对齐 config.memory.dedup 接线）。"""
    m = MemoryManager(
        store=MemoryStore(db_path=str(tmp_path / "custom_dedup.db")),
        vector_store=InMemoryVectorStore(),
        dedup_threshold=0.5,
    )
    # 两句 Jaccard 相似度约 4/6≈0.667：默认阈值 0.85 下各自入库，0.5 阈值下第二条被去重
    mid1 = m.add_memory("alpha beta gamma delta epsilon", embed_fn=_embed)
    mid2 = m.add_memory("alpha beta gamma delta zeta", embed_fn=_embed)
    assert mid1 is not None
    assert mid2 is None, "相似度 0.667 >= 自定义阈值 0.5，第二条应在写入口被去重"
    # 对照：同内容对在默认阈值 0.85 下不会被去重（行为随配置变化）
    mgr_mid1 = mgr.add_memory("alpha beta gamma delta epsilon", embed_fn=_embed)
    mgr_mid2 = mgr.add_memory("alpha beta gamma delta zeta", embed_fn=_embed)
    assert mgr_mid1 is not None and mgr_mid2 is not None


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


# ---------------------------------------------------------------- 批次3（第三轮体检）：软删除清向量
def test_soft_delete_also_removes_vector(mgr):
    """M-10：soft_delete 命中后向量库中对应向量同步删除，不再成为孤儿。"""
    mid = mgr.add_memory("orphan vector cleanup token", embed_fn=_embed)
    assert mid is not None
    assert str(mid) in mgr.vector_store._vectors

    assert mgr.soft_delete(mid) is True
    assert str(mid) not in mgr.vector_store._vectors
    # 记录软删除后检索不可见（is_deleted 过滤既有行为不变）
    assert mgr.retrieve("orphan vector cleanup", embed_fn=_embed, top_k=5) == []


def test_soft_delete_miss_returns_false(mgr):
    """M-10：soft_delete 未命中返回 False，不触碰向量库。"""
    assert mgr.soft_delete(99999) is False


def test_soft_delete_survives_vector_store_error(mgr):
    """M-10：向量清理抛异常时仅告警，软删除语义不回滚。"""
    class _BoomVectorStore(InMemoryVectorStore):
        def delete(self, vector_id):
            raise RuntimeError("boom")

    m = MemoryManager(
        store=MemoryStore(db_path=str(mgr.store.db_path) + ".x.db"),
        vector_store=_BoomVectorStore(),
    )
    mid = m.add_memory("boom cleanup token", embed_fn=_embed)
    assert m.soft_delete(mid) is True
    # 软删除已生效（get 侧 is_deleted=1）
    assert m.store.get(mid)["is_deleted"] == 1


# ---------------------------------------------------------------- 中文分词（中-2，第四轮体检批次B）
def test_tokenize_text_mixed_script():
    """共享分词：拉丁整词小写、CJK 相邻 bigram、标点/空白为分隔符。"""
    from lite.memory.manager import tokenize_text

    assert tokenize_text("alpha Beta 12") == {"alpha", "beta", "12"}
    assert tokenize_text("冷萃") == {"冷萃"}
    assert tokenize_text("用户偏好冷萃咖啡") == {
        "用户", "户偏", "偏好", "好冷", "冷萃", "萃咖", "咖啡",
    }
    assert tokenize_text("") == set()
    assert tokenize_text(None) == set()


def test_chinese_similarity_one_char_diff_above_half(mgr):
    """中文两条仅差一字的记忆相似度应超 0.5（修复前整句单 token 相似度恒 0）。"""
    a = "她喜欢在午后的图书馆靠窗位置安静地读一本厚厚的散文集并做摘抄笔记"
    b = "她喜欢在午后的图书馆靠门位置安静地读一本厚厚的散文集并做摘抄笔记"
    sim = MemoryManager._text_similarity(a, b)
    assert sim > 0.5, f"bigram 分词后相似度应显著大于 0，实际 {sim:.3f}"


def test_chinese_write_dedup_triggered(mgr):
    """两条仅差一字的 32 字中文记忆（bigram 相似度约 0.88）在写入口被去重。"""
    a = "她喜欢在午后的图书馆靠窗位置安静地读一本厚厚的散文集并做摘抄笔记"
    b = "她喜欢在午后的图书馆靠门位置安静地读一本厚厚的散文集并做摘抄笔记"
    mid1 = mgr.add_memory(a)
    mid2 = mgr.add_memory(b)
    assert mid1 is not None
    assert mid2 is None, f"相似度 {MemoryManager._text_similarity(a, b):.3f} 应达默认阈值 0.85"