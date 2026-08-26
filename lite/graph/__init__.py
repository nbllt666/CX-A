# -*- coding: utf-8 -*-
"""知识图谱子包（SQLite 实体/关系存储 + 向量语义检索），Task A6 实现。

提供 GraphStore：图节点/边 CRUD（SQLite）+ 外部注入向量的语义检索。
"""

from lite.graph.graph_store import GraphStore

__all__ = ["GraphStore"]