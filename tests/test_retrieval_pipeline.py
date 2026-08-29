# -*- coding: utf-8 -*-
"""MemoryRetrievalPipeline 检索管线测试（A5）。

覆盖：add 后检索召回同话题记忆、向量+关键字加权起效、max_memories 截断、
去重（内容相似只入一条）、context_text 组装、空库安全、嵌入桩确定性。
全部为全 mock / 无网络用例（确定性嵌入，不依赖 llama.cpp）。
"""

import pytest

from lite.memory.embedding import EmbeddingProvider, LiteEmbeddingProvider
from lite.memory.pipeline import _build_context, MemoryRetrievalPipeline
from lite.memory.storage import MemoryStore
from lite.memory.vector_store import InMemoryVectorStore


@pytest.fixture()
def pipeline(tmp_path):
    """常规管线：SQLite + 内存向量 + 确定性嵌入桩。"""
    return MemoryRetrievalPipeline(
        store=MemoryStore(db_path=str(tmp_path / "memories.db")),
        vector_store=InMemoryVectorStore(),
        embed=LiteEmbeddingProvider(dim=64),
        max_memories=30,
    )


class FakeEmbed(EmbeddingProvider):
    """可控嵌入桩：显式指定查询向量与各内容向量，独立于文本分词，用于加权断言。"""

    QUERY_VEC = [1.0, 0.0, 0.0]
    FAR_VEC = [0.0, 1.0, 0.0]

    def __init__(self, query_text):
        self.query_text = query_text
        self._content_vectors = {}

    def set_content(self, text, vec):
        self._content_vectors[text] = list(vec)

    def embed(self, texts):
        out = []
        for t in texts:
            if t == self.query_text:
                out.append(list(self.QUERY_VEC))
            else:
                out.append(list(self._content_vectors.get(t, self.FAR_VEC)))
        return out


# ---------------------------------------------------------------- add 后检索召回
def test_add_then_retrieve_recalls_same_topic(pipeline):
    pipeline.add("I like apple fruit sweet")
    pipeline.add("the weather sunny today")
    res = pipeline.retrieve("apple fruit", top_k=5)
    assert res["memories"], "应能召回记忆"
    assert "apple" in res["memories"][0]["content"]
    # 且排序按分数降序
    scores = [m["final_score"] for m in res["memories"]]
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------- 向量+关键字加权
def test_vector_and_keyword_weighting_order():
    """「向量近但关键词远」应靠 0.6 向量权压过「关键词近但向量远」的 0.4。"""
    query = "alpha beta gamma delta"
    a_content = "mem alpha beta gamma delta"  # 关键词重合 1.0，向量远
    b_content = "implant unique tokn"         # 向量近，关键词无重合
    fake = FakeEmbed(query_text=query)
    fake.set_content(a_content, FakeEmbed.FAR_VEC)
    fake.set_content(b_content, FakeEmbed.QUERY_VEC)
    store = MemoryStore(db_path=":memory:")
    pipe = MemoryRetrievalPipeline(
        store=store,
        vector_store=InMemoryVectorStore(),
        embed=fake,
        vector_weight=0.6,
        keyword_weight=0.4,
    )
    pipe.add(a_content)
    pipe.add(b_content)
    res = pipe.retrieve(query, top_k=5)

    by_content = {m["content"]: m for m in res["memories"]}
    assert len(by_content) == 2
    # B（向量近 1.0 / 关键词 0）合并分 0.6；A（关键词 1.0 / 向量 0）合并分 0.4
    assert by_content[b_content]["score"] == pytest.approx(0.6, abs=1e-6)
    assert by_content[a_content]["score"] == pytest.approx(0.4, abs=1e-6)
    # 0.6 向量权 > 0.4 关键词权 → 反例：B 必须排在 A 之前
    assert res["memories"][0]["content"] == b_content
    assert res["memories"][1]["content"] == a_content


def test_keyword_weight_breaks_vector_tie():
    """向量相同（均为远）时，关键词重合度更高者应因 0.4 关键词权排前。"""
    query = "alpha beta gamma delta"
    low_content = "alpha beta"                        # 重合 2/4 = 0.5
    high_content = "alpha beta gamma delta all"        # 重合 4/4 = 1.0
    fake = FakeEmbed(query_text=query)
    # 两条均为向量远（0），仅关键词分不同
    fake.set_content(low_content, FakeEmbed.FAR_VEC)
    fake.set_content(high_content, FakeEmbed.FAR_VEC)
    pipe = MemoryRetrievalPipeline(
        store=MemoryStore(db_path=":memory:"),
        vector_store=InMemoryVectorStore(),
        embed=fake,
        vector_weight=0.6,
        keyword_weight=0.4,
    )
    pipe.add(low_content)
    pipe.add(high_content)
    res = pipe.retrieve(query, top_k=5)
    assert res["memories"][0]["content"] == high_content
    assert res["memories"][0]["score"] > res["memories"][1]["score"]


# ---------------------------------------------------------------- 截断 / 去重
def test_max_memories_truncation(pipeline):
    for i in range(5):
        pipeline.add(f"distinct entry memory {i}")
    res = pipeline.retrieve("distinct entry", top_k=10)
    assert len(res["memories"]) == 5
    pipe2 = MemoryRetrievalPipeline(
        store=pipeline.store,
        vector_store=pipeline.vector_store,
        embed=pipeline.embed,
        max_memories=2,
    )
    res2 = pipe2.retrieve("distinct entry", top_k=10)
    assert len(res2["memories"]) == 2


def test_dedup_keeps_one_per_content(pipeline):
    pipeline.add("apple fruit sweet")
    pipeline.add("apple fruit sweet")          # 相同内容（相似度 1.0）
    pipeline.add("apple fruit sour")           # 内容不同（相似度 < 0.85）
    res = pipeline.retrieve("apple fruit", top_k=10)
    contents = [m["content"] for m in res["memories"]]
    # 去重后内容唯一：相同两条只保留一条，不同两条各留一条
    assert len(contents) == len(set(contents))
    assert "apple fruit sour" in contents


# ---------------------------------------------------------------- 写入口去重一致性（M7）
def test_add_dedup_second_similar_returns_none(pipeline):
    """两次 add 相似内容：第二次命中写入口去重返回 None，且库中只保留一条。"""
    first = pipeline.add("user prefers iced americano coffee")
    second = pipeline.add("user prefers iced americano coffee")  # 相似度 1.0 >= 0.85
    third = pipeline.add("totally different topic about mountains")
    assert first is not None
    assert second is None, "重复内容应在写入口被去重，返回 None"
    assert third is not None, "不同内容不应被误去重"
    rows = pipeline.store.list(agent_id="default", include_deleted=False)
    assert len(rows) == 2
    assert {r["content"] for r in rows} == {
        "user prefers iced americano coffee",
        "totally different topic about mountains",
    }


# ---------------------------------------------------------------- 注入上下文组装
def test_context_text_contains_memories(pipeline):
    pipeline.add("记住用户偏好冷萃咖啡")
    res = pipeline.retrieve("冷萃咖啡偏好", top_k=5)
    assert res["context_text"]
    assert "【回忆】" in res["context_text"]
    assert "冷萃" in res["context_text"]


def test_context_text_formatting_ordering(pipeline):
    pipeline.add("first memory content token")
    pipeline.add("second memory content token")
    res = pipeline.retrieve("memory content token", top_k=5)
    text = res["context_text"]
    # 每条记忆以「序号. 」呈现，且序号递增
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("【回忆】")]
    assert len(lines) == len(res["memories"])
    assert all(ln.split(". ")[0].isdigit() for ln in lines)


# ---------------------------------------------------------------- 空库 / 桩能力
def test_empty_store_returns_empty_context(pipeline):
    res = pipeline.retrieve("anything not stored", top_k=5)
    assert res["memories"] == []
    assert res["context_text"] == "【回忆】"


def test_embedding_provider_abstract_not_instantiable():
    with pytest.raises(TypeError):
        EmbeddingProvider()


def test_lite_embedding_provider_deterministic():
    ep = LiteEmbeddingProvider(dim=64)
    v1 = ep.embed(["this is a sample text"])
    v2 = ep.embed(["this is a sample text"])
    assert v1 == v2, "同一文本应得到同一向量（确定性）"
    assert len(v1[0]) == 64
    # 同话题（共享词）文本向量应比无关文本更接近
    same = ep.embed(["apple fruit sweet"])
    topic = ep.embed(["apple fruit pie"])
    other = ep.embed(["quantum physics math"])
    d_same = sum((a - b) ** 2 for a, b in zip(same[0], topic[0]))
    d_other = sum((a - b) ** 2 for a, b in zip(same[0], other[0]))
    assert d_same < d_other


def test_pipeline_imports_without_llama_runtime():
    """A5 不应引入未实现的 llama_runtime：仅可导入本模块即证明依赖闭合。"""
    import lite.memory.pipeline  # noqa: F401


def test_build_context_empty_and_single():
    assert _build_context([]) == "【回忆】"
    text = _build_context([{"content": "hello world"}])
    assert text.startswith("【回忆】")
    assert "1. hello world" in text


# ---------------------------------------------------------------- 中文分词（中-2，第四轮体检批次B）
def test_chinese_keyword_score_positive(pipeline):
    """含中文关键词的查询 keyword_score 应 > 0（修复前整句单 token 恒 0）。"""
    pipeline.add("用户偏好冷萃咖啡不加糖")
    res = pipeline.retrieve("冷萃", top_k=5)
    assert res["memories"], "中文关键词应召回记忆"
    assert res["memories"][0]["keyword_score"] > 0


def test_chinese_text_similarity_shared_tokenizer():
    """manager 与 pipeline 共用同一分词实现：中文仅差一字相似度显著大于 0。"""
    from lite.memory.manager import MemoryManager

    a = "她喜欢在午后的图书馆靠窗位置安静地读一本厚厚的散文集并做摘抄笔记"
    b = "她喜欢在午后的图书馆靠门位置安静地读一本厚厚的散文集并做摘抄笔记"
    assert MemoryManager._text_similarity(a, b) > 0.5


class _SpyVectorStore(InMemoryVectorStore):
    """记录 search 收到 top_k 的探针向量库（低-7 断言用）。"""

    def __init__(self):
        super().__init__()
        self.search_top_ks = []

    def search(self, vector, top_k=10):
        self.search_top_ks.append(top_k)
        return super().search(vector, top_k=top_k)


def test_explicit_top_k_zero_clamped_to_one(tmp_path):
    """低-7：显式 top_k=0 按候选下限 1 处理（向量召回 1×2），不再回落缺省 20。"""
    spy = _SpyVectorStore()
    pipe = MemoryRetrievalPipeline(
        store=MemoryStore(db_path=str(tmp_path / "memories.db")),
        vector_store=spy,
        embed=LiteEmbeddingProvider(dim=64),
    )
    pipe.add("some memory content for probe")
    pipe.retrieve("probe", top_k=0)
    assert spy.search_top_ks == [2]  # cand_n = max(1, 0) = 1 → 召回 2 条
    pipe.retrieve("probe")
    assert spy.search_top_ks[-1] == 40  # 缺省 20 → 召回 40 条