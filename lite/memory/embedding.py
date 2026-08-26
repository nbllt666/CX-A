# -*- coding: utf-8 -*-
"""嵌入接口抽象与轻量确定性桩实现。

EmbeddingProvider 为嵌入统一抽象基类，定义 ``embed(texts) -> list[list[float]]`` 接口；
LiteEmbeddingProvider 为 C1 真实嵌入未接入前的确定性哈希桩实现：
基于粗粒度词袋哈希（crc32）构造「随机但确定」的向量——同一文本永远得到同一向量，
共享词越多（同一话题）向量越接近，保证单测 / 离线场景下检索可复现。

⚠️ C1 后替换 llama.cpp 真实模型：本桩仅用于本地单测、无 GPU 环境或需可复现检索的离线场景，
其体积与语义近似性远弱于真实模型。正式接入时仅需在本 provider 的 embed 方法内替换为
llama.cpp 的嵌入调用，接口与调用方均无需改动（本模块不 import 未实现的 llama_runtime）。
"""

from abc import ABC, abstractmethod

import math
import zlib

# 默认向量维度（与既有测试词袋向量口径一致）
DEFAULT_DIM = 64


class EmbeddingProvider(ABC):
    """嵌入抽象基类。

    负责把文本批量转换为稠密向量，供向量检索层（VectorStore）使用。
    所有实现必须满足：同一文本序列在同一实例的多次调用中返回一致的向量（确定性）。
    """

    @abstractmethod
    def embed(self, texts: list) -> list:
        """批量文本嵌入。

        Args:
            texts: 文本列表（list[str]）。
        Returns:
            list[list[float]]: 与输入等长的向量列表，维度固定。
        """


class LiteEmbeddingProvider(EmbeddingProvider):
    """轻量确定性嵌入桩（C1 前使用）。

    通过对 token 做 crc32 哈希映射到固定维度计数，再做 L2 归一化，
    得到「随机但确定」的向量。语义粒度粗，仅保证同话题可召回、结果可复现。
    """

    def __init__(self, dim: int = DEFAULT_DIM):
        """初始化时固定维度。

        Args:
            dim: 向量维度（默认 64）。
        """
        self.dim = int(dim)

    @staticmethod
    def _tokens(text) -> set:
        """粗粒度分词：小写并按空白切分（与检索一致性口径一致）。"""
        return set(str(text).lower().split())

    def _embed_one(self, text) -> list:
        """单条文本的确定性词袋向量（L2 归一化）。"""
        vec = [0.0] * self.dim
        for tok in self._tokens(text):
            idx = zlib.crc32(tok.encode("utf-8")) % self.dim
            vec[idx] += 1.0
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]

    def embed(self, texts: list) -> list:
        """批量文本嵌入，返回与输入等长的归一化向量列表。"""
        return [self._embed_one(t) for t in texts]