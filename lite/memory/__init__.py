# -*- coding: utf-8 -*-
"""记忆模块——数据契约、持久化存储、向量检索、衰减计算、三维打分与记忆管理器。

导出统一入口：
- MemoryManager：记忆管理器门面（添加/检索/再激活/分层升降级）。
- DecayCalculator：记忆衰减计算器（艾宾浩斯 / 双阶段指数）。
- score_memories / _get_weights：三维加权打分与场景权重调整。
"""

from .decay import DecayCalculator
from .distillation import DistillationPaused, MemoryDistiller
from .embedding import EmbeddingProvider, LiteEmbeddingProvider
from .manager import MemoryManager
from .pipeline import MemoryRetrievalPipeline
from .scoring import DEFAULT_WEIGHTS, score_memories
from .storage import MemoryStore
from .vector_store import InMemoryVectorStore, LanceVectorStore, VectorStore

__all__ = [
    "MemoryManager",
    "MemoryStore",
    "DecayCalculator",
    "score_memories",
    "DEFAULT_WEIGHTS",
    "VectorStore",
    "InMemoryVectorStore",
    "LanceVectorStore",
    "EmbeddingProvider",
    "LiteEmbeddingProvider",
    "MemoryRetrievalPipeline",
]