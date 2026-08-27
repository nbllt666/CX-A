# -*- coding: utf-8 -*-
"""向量存储适配层——封装 LanceDB 与内存向量检索。

VectorStore 为抽象基类，定义统一 upsert / search / delete 接口；
LanceVectorStore 适配 LanceDB（可选依赖 lancedb，未安装时实例化抛 RuntimeError）；
InMemoryVectorStore 为纯 Python 实现（cosine 距离），供单测与无 lancedb 环境使用。
"""

import math
from abc import ABC, abstractmethod


class VectorStore(ABC):
    """向量存储抽象基类。

    vector_id 用于与 memories.vector_id 字段关联。
    """

    @abstractmethod
    def upsert(self, vector_id, vector, metadata=None):
        """写入或更新向量。vector_id 已存在则覆盖。"""

    @abstractmethod
    def search(self, vector, top_k=10):
        """检索与查询向量最相似的 top_k 条。

        Returns:
            list[dict]: 按相关性降序，含 vector_id / score / metadata 字段。
        """

    @abstractmethod
    def delete(self, vector_id):
        """按 vector_id 删除向量，返回是否命中。"""


class LanceVectorStore(VectorStore):
    """LanceDB 向量存储适配（可选依赖 lancedb）。

    未安装 lancedb 时，实例化直接抛出 RuntimeError，提示 pip install lancedb；
    测试不依赖本类，若环境中无 lancedb 请使用 InMemoryVectorStore。
    """

    def __init__(self, db_path="data/lancedb", table_name="memories"):
        try:
            import lancedb
        except ImportError as exc:  # pragma: no cover - 依赖缺失路径
            raise RuntimeError(
                "未安装 lancedb，无法使用 LanceVectorStore。请执行 pip install lancedb；"
                "或在无 lancedb 环境下改用 InMemoryVectorStore。"
            ) from exc
        self._lancedb = lancedb
        self.db_path = db_path
        self.table_name = table_name
        self._conn = lancedb.connect(db_path)
        self._table = self._open_table()

    def _open_table(self):
        try:
            return self._conn.open_table(self.table_name)
        except Exception:
            # 表尚未创建：返回 None，由 upsert 以首条记录惰性建表。
            # （部分 lancedb 版本不允许用空列表建表，必须携带数据或 schema）
            return None

    def upsert(self, vector_id, vector, metadata=None):
        """写入或覆盖向量。先删重名再写入，保证按 vector_id 唯一。"""
        self.delete(vector_id)
        record = {"vector_id": vector_id, "vector": list(vector)}
        if metadata:
            record["metadata"] = metadata
        if self._table is None:
            self._table = self._conn.create_table(self.table_name, data=[record])
        else:
            self._table.add([record])
        return True

    def search(self, vector, top_k=10):
        """检索 top_k 条最相似向量（LanceDB 返回 L2 距离，此处转为相似度）。

        与 InMemoryVectorStore 口径对齐：score 为相似度、越大越相似，
        归一公式 ``score = 1 / (1 + L2距离)``——相同向量为 1.0，越远越接近 0。
        """
        if self._table is None:
            return []
        results = self._table.search(list(vector)).limit(int(top_k)).to_list()
        return [
            {
                "vector_id": r.get("vector_id"),
                "score": (1.0 / (1.0 + float(r.get("_distance"))))
                if r.get("_distance") is not None
                else 0.0,
                "metadata": r.get("metadata"),
            }
            for r in results
        ]

    def delete(self, vector_id):
        """按 vector_id 删除向量。"""
        if self._table is None:
            return False
        from_lance_filter = "vector_id = '{0}'".format(vector_id)
        self._table.delete(from_lance_filter)
        return True


class InMemoryVectorStore(VectorStore):
    """纯 Python 内存向量存储（cosine 距离），供单测与无 lancedb 环境使用。"""

    def __init__(self):
        self._vectors = {}
        self._metas = {}

    @staticmethod
    def _cosine(a, b):
        """cosine 相似度；零向量或不匹配维度返回 0。"""
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    def upsert(self, vector_id, vector, metadata=None):
        """写入或覆盖向量，并关联 metadata。"""
        self._vectors[vector_id] = list(vector)
        if metadata is not None:
            self._metas[vector_id] = metadata
        else:
            self._metas.setdefault(vector_id, {})
        return True

    def search(self, vector, top_k=10):
        """按 cosine 相似度降序返回 top_k 条。"""
        scored = [
            {
                "vector_id": vid,
                "score": self._cosine(vector, vec),
                "metadata": self._metas.get(vid, {}),
            }
            for vid, vec in self._vectors.items()
        ]
        scored.sort(key=lambda item: item["score"], reverse=True)
        return scored[: max(0, int(top_k))]

    def delete(self, vector_id):
        """按 vector_id 删除向量，返回是否命中。"""
        existed = vector_id in self._vectors
        self._vectors.pop(vector_id, None)
        self._metas.pop(vector_id, None)
        return existed