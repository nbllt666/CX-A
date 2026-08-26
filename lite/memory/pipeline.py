# -*- coding: utf-8 -*-
"""记忆检索管线 MemoryRetrievalPipeline——组合存储 / 向量 / 嵌入 / 打分各组件。

在 MemoryManager 的「向量 → 相关性 → 三维打分 → 衰减 → 去重 → 截断」策略之上，A5 增量：
- 嵌入接口桩（C1 后由 llama.cpp 替换，见 embedding.py）；
- 向量 + 关键字混合加权（vector_weight=0.6 + keyword_weight=0.4）；
- 完整注入上下文组装（返回 memories 与拼接的 context_text）。

为尽量复用已有能力（避免重复发明轮子）：
- 复用 MemoryManager 的去重策略（_dedup_retrieved，阈值 0.85）与衰减器实例；
- 复用 scoring.score_memories / _get_weights 完成三维加权打分；
- 关键字加权（manager 未覆盖）由本管线在 pipeline 层补齐。
"""

from datetime import datetime

from .decay import DecayCalculator
from .embedding import EmbeddingProvider
from .manager import DEDUP_THRESHOLD, MemoryManager
from .scoring import _get_weights, score_memories
from .storage import MemoryStore
from .vector_store import VectorStore

# 关键字混合加权默认值（与任务契约对齐）
DEFAULT_VECTOR_WEIGHT = 0.6
DEFAULT_KEYWORD_WEIGHT = 0.4
# 检索候选数（未显式传入 top_k 时的默认分量）
DEFAULT_CANDIDATE_COUNT = 20


def _tokenize(text) -> set:
    """粗粒度分词：小写并按空白切分。"""
    return set(str(text or "").lower().split())


def _keyword_overlap(query_tokens, content_tokens) -> float:
    """关键词重合度（0~1）：查询词在内容中的召回率，0/空集返回 0。"""
    if not query_tokens or not content_tokens:
        return 0.0
    inter = len(query_tokens & content_tokens)
    return inter / len(query_tokens)


class MemoryRetrievalPipeline:
    """记忆检索管线门面。

    add 写入 SQLite 并同步写向量（vector_id = 记忆 id）；
    retrieve 完成 向量检索 → 关键字混合加权 → 三维打分 → 衰减 → 去重 → 截断 → 注入上下文。
    """

    def __init__(
        self,
        store: MemoryStore,
        vector_store: VectorStore,
        embed: EmbeddingProvider,
        vector_weight: float = DEFAULT_VECTOR_WEIGHT,
        keyword_weight: float = DEFAULT_KEYWORD_WEIGHT,
        max_memories: int = 30,
        scene_context=None,
    ):
        """初始化检索管线。

        Args:
            store: 持久化存储（MemoryStore）。
            vector_store: 向量存储（VectorStore 实现）。
            embed: 嵌入提供者（EmbeddingProvider 实现，C1 前为桩）。
            vector_weight: 向量相关度权重（默认 0.6）。
            keyword_weight: 关键词相关度权重（默认 0.4）。
            max_memories: 最终注入记忆条数上限（默认 30）。
            scene_context: 场景上下文（场景名或权重 dict），用于三维权重调整。
        """
        self.store = store
        self.vector_store = vector_store
        self.embed = embed
        self.vector_weight = float(vector_weight)
        self.keyword_weight = float(keyword_weight)
        self.max_memories = int(max_memories)
        self.scene_context = scene_context

        # 复用 MemoryManager 以继承其去重 / 衰减策略（store/vector_store 共享实例）
        self.store.create_table()
        self.manager = MemoryManager(store=store, vector_store=vector_store)
        self.decay = self.manager.decay
        self.dedup_threshold = DEDUP_THRESHOLD

    # ------------------------------------------------------------------ 写
    def add(
        self,
        content,
        type_="long_term",
        importance=3,
        tags=None,
        agent_id="default",
        metadata=None,
    ) -> int:
        """新增一条记忆：写入 SQLite + 同步向量，返回记忆 id。

        Args:
            content: 记忆内容（必填）。
            type_: 记忆类型（long_term / short_term / permanent）。
            importance: 重要性等级（1~5）。
            tags: 标签（list[str]）。
            agent_id: 记忆归属 agent。
            metadata: 附加元数据 dict。

        Returns:
            int: 新记忆 id。

        Raises:
            ValueError: type 非法或 content 为空（由 MemoryStore 校验）。
        """
        memory_id = self.store.add(
            {
                "content": content,
                "type": type_,
                "importance": importance,
                "importance_score": max(0.0, min(1.0, int(importance) / 5.0)),
                "decay_type": "exponential",
                "reactivation_count": 0,
                "emotion_score": 0.0,
                "permanent": type_ == "permanent",
                "tags": tags,
                "metadata": metadata,
                "agent_id": agent_id,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f"),
            }
        )
        # 写向量：vector_id = 记忆 id（与 memories.vector_id 关联）
        try:
            vector = self.embed.embed([content])[0]
        except (IndexError, TypeError, ValueError):
            vector = None
        if vector is not None:
            self.vector_store.upsert(
                str(memory_id),
                vector,
                metadata={"memory_id": memory_id, "agent_id": agent_id},
            )
        return memory_id

    # ------------------------------------------------------------------ 读
    def retrieve(self, query, agent_id="default", top_k=None) -> dict:
        """检索并注入上下文。

        流程：
        a) 查询嵌入 → 向量检索（候选数 × 2）；
        b) 关键词检索：查询分词后对候选做关键词重合度打分（0~1）；
        c) 合并分 = vector_weight×向量分 + keyword_weight×关键词分（作为相关性 relevance）；
        d) 复用 scoring.score_memories 三维打分 + _get_weights 场景权重 + DecayCalculator 衰减，
           再复用 MemoryManager 去重（阈值 0.85），最后按 max_memories 截断；
        e) 组装注入上下文。

        Args:
            query: 查询文本。
            agent_id: 限定记忆归属 agent。
            top_k: 向量检索候选数（None 用默认 20）；实际向量召回取 top_k×2。

        Returns:
            dict: {"memories": list[dict], "context_text": str}。
                memories 按 final_score 降序，附加 score/vector_score/keyword_score/
                component_scores/final_score 等字段。
        """
        cand_n = int(top_k) if top_k else DEFAULT_CANDIDATE_COUNT
        cand_n = max(1, cand_n)

        # a) 向量检索
        qvec = self.embed.embed([query])[0]
        hits = self.vector_store.search(qvec, top_k=cand_n * 2)
        hit_by_id = {}
        for hit in hits:
            raw_id = hit.get("vector_id")
            if raw_id in (None, ""):
                continue
            try:
                hit_by_id[int(raw_id)] = float(hit.get("score", 0.0))
            except (TypeError, ValueError):
                continue

        # b) 关键字检索：对全量内存（含衰减所需状态字段）打分
        memories = self.store.list(agent_id=agent_id, include_deleted=False)
        query_tokens = _tokenize(query)
        candidates = []
        for mem in memories:
            mem_id = mem.get("id")
            content_tokens = _tokenize(mem.get("content", ""))
            vector_score = hit_by_id.get(mem_id, 0.0)
            keyword_score = _keyword_overlap(query_tokens, content_tokens)
            # c) 混合加权作为相关性 relevance
            relevance = self.vector_weight * vector_score + self.keyword_weight * keyword_score
            mem["score"] = relevance
            mem["vector_score"] = vector_score
            mem["keyword_score"] = keyword_score
            candidates.append(mem)

        if not candidates:
            return {"memories": [], "context_text": _build_context([])}

        # d) 三维打分（复用 scoring + DecayCalculator 衰减校正）
        weights = _get_weights(self.scene_context)
        scored = score_memories(
            candidates,
            query=query,
            importance_weight=weights["importance"],
            time_weight=weights["time"],
            relevance_weight=weights["relevance"],
            _decay_calculator=self.decay,
        )
        # 复用 manager 去重策略（内容相似度阈值 0.85）
        deduped = self.manager._dedup_retrieved(scored)
        truncated = deduped[: self.max_memories]

        # e) 组装注入上下文
        return {"memories": truncated, "context_text": _build_context(truncated)}


def _build_context(memories) -> str:
    """拼接注入上下文：每行一条「序号. 内容」，空库返回「【回忆】」头即可。"""
    if not memories:
        return "【回忆】"
    lines = ["【回忆】"]
    for i, mem in enumerate(memories, 1):
        lines.append(f"{i}. {mem.get('content', '')}")
    return "\n".join(lines)