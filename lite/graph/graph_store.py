# -*- coding: utf-8 -*-
"""知识图谱存储服务（Task A6）。

GraphStore 负责知识图谱的实体（节点）/关系（边）的 SQLite 持久化，
并为节点提供外部注入的向量化链路，用于语义检索。

设计要点：
- SQLite 持久化，数据库文件默认位于 data/graph.db，路径由本文件绝对路径逐级推导，
  禁止相对路径。
- 向量关联不依赖具体模型（不引入 llama.cpp / sentences-transformer），
  由外部注入 embed_callable（可调用对象）计算向量，写入 VectorStore
  （默认 InMemoryVectorStore，可选 LanceVectorStore）。
- 纯标准库 + lite.memory.vector_store，不引用任何未实现的模块。
"""

import json
import os
import sqlite3
from datetime import datetime


def _default_db_path() -> str:
    """推导默认数据库路径：<项目根>/data/graph.db。

    以当前文件绝对路径逐级上溯找到项目根（lite/graph/graph_store.py → CX-A），
    再拼接 data/graph.db。
    """
    here = os.path.dirname(os.path.abspath(__file__))          # lite/graph
    lite_dir = os.path.dirname(here)                            # lite
    project_root = os.path.dirname(lite_dir)                    # CX-A
    return os.path.join(project_root, "data", "graph.db")


def _now() -> str:
    """返回当前时间的 ISO 格式字符串。"""
    return datetime.now().isoformat()


class GraphStore:
    """SQLite 知识图谱存储 + 可选向量语义检索。

    Args:
        db_path: SQLite 数据库文件路径。为 None 时使用默认路径 data/graph.db；
            测试可传入 ":memory:"。
        embed_callable: 外部注入的向量化函数，签名 embed(text: str) -> list[float]。
            为 None 时跳过向量写入，GraphStore 仅用作纯图存储。
        vector_store: 向量存储实现（继承 lite.memory.vector_store.VectorStore）。
            为 None 时默认使用 InMemoryVectorStore。

    Example:
        >>> store = GraphStore(db_path=":memory:", embed_callable=my_embed)
        >>> store.upsert_node("n1", "Alice", "person", {"age": 30})
        >>> hits = store.search_nodes("Alice", top_k=3)
    """

    def __init__(self, db_path=None, embed_callable=None, vector_store=None):
        self.db_path = db_path if db_path is not None else _default_db_path()
        self.embed = embed_callable
        if vector_store is None:
            from lite.memory.vector_store import InMemoryVectorStore
            vector_store = InMemoryVectorStore()
        self.vector_store = vector_store
        # 数据库目录不存在时自动创建（文件库场景）。
        if self.db_path != ":memory:":
            parent = os.path.dirname(self.db_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._create_tables()

    # ------------------------------------------------------------------ #
    # 基础设施
    # ------------------------------------------------------------------ #
    def _create_tables(self) -> None:
        """按契约创建 nodes / edges 表。"""
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS nodes (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    type TEXT,
                    properties TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS edges (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL REFERENCES nodes(id),
                    target TEXT NOT NULL REFERENCES nodes(id),
                    relation TEXT NOT NULL,
                    properties TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source);
                CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target);
                """
            )

    def close(self) -> None:
        """关闭数据库连接。"""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------ #
    # 节点 CRUD
    # ------------------------------------------------------------------ #
    def upsert_node(self, node_id, name, type=None, properties=None):
        """写入或更新节点。

        节点存在则覆盖，不存在则插入。写入后同步计算向量并写入向量库。

        Args:
            node_id: 节点主键（文本）。
            name: 节点名称（NOT NULL）。
            type: 节点类型，可空。
            properties: 属性字典，可空。

        Returns:
            dict: 序列化后的节点（含 id/name/type/properties/created_at/updated_at）。
        """
        properties = properties or {}
        sql = (
            "INSERT INTO nodes (id, name, type, properties, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "name=excluded.name, type=excluded.type, "
            "properties=excluded.properties, updated_at=excluded.updated_at"
        )
        now = _now()
        with self._conn:
            self._conn.execute(
                sql,
                (node_id, name, type, json.dumps(properties, ensure_ascii=False), now, now),
            )
        self._index_node_vector(node_id, name, type, properties)
        return self.get_node(node_id)

    def get_node(self, node_id):
        """按 id 获取节点，不存在返回 None。

        Returns:
            dict 或 None：节点含 id/name/type/properties(已反序列化)/created_at/updated_at。
        """
        cur = self._conn.execute(
            "SELECT id, name, type, properties, created_at, updated_at "
            "FROM nodes WHERE id = ?",
            (node_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_node(row)

    def delete_node(self, node_id):
        """删除节点并连带删除与之关联的所有边。

        同时删除该节点对应的向量索引。

        Returns:
            bool: 节点原本存在则 True，否则 False。
        """
        existed = self.get_node(node_id) is not None
        if not existed:
            return False
        with self._conn:
            self._conn.execute("DELETE FROM edges WHERE source = ? OR target = ?", (node_id, node_id))
            self._conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
        try:
            self.vector_store.delete(node_id)
        except Exception:
            # 向量删除失败不影响图数据本身，容忍并继续。
            pass
        return True

    def list_nodes(self, limit=100, offset=0):
        """分页列出全部节点。

        Returns:
            list[dict]: 按 created_at 升序返回节点列表。
        """
        cur = self._conn.execute(
            "SELECT id, name, type, properties, created_at, updated_at "
            "FROM nodes ORDER BY created_at ASC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [self._row_to_node(row) for row in cur.fetchall()]

    # ------------------------------------------------------------------ #
    # 边 CRUD
    # ------------------------------------------------------------------ #
    def add_edge(self, edge_id, source, target, relation, properties=None):
        """新增一条有向边，校验源/目标节点必须存在。

        Args:
            edge_id: 边主键（文本）。
            source: 源节点 id。
            target: 目标节点 id。
            relation: 关系类型（NOT NULL）。
            properties: 属性字典，可空。

        Returns:
            dict: 序列化后的边（含 id/source/target/relation/properties/created_at）。

        Raises:
            ValueError: source 或 target 节点不存在时。
        """
        if self.get_node(source) is None:
            raise ValueError(f"源节点不存在: {source}")
        if self.get_node(target) is None:
            raise ValueError(f"目标节点不存在: {target}")
        properties = properties or {}
        now = _now()
        with self._conn:
            self._conn.execute(
                "INSERT INTO edges (id, source, target, relation, properties, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (edge_id, source, target, relation,
                 json.dumps(properties, ensure_ascii=False), now),
            )
        return self.get_edge(edge_id)

    def get_edge(self, edge_id):
        """按 id 获取边，不存在返回 None。"""
        cur = self._conn.execute(
            "SELECT id, source, target, relation, properties, created_at "
            "FROM edges WHERE id = ?",
            (edge_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_edge(row)

    def get_edges_by_node(self, node_id):
        """获取与指定节点相连的全部边（作为源或目标）。

        Returns:
            list[dict]: 关联该节点的边列表。
        """
        cur = self._conn.execute(
            "SELECT id, source, target, relation, properties, created_at "
            "FROM edges WHERE source = ? OR target = ? "
            "ORDER BY created_at ASC",
            (node_id, node_id),
        )
        return [self._row_to_edge(row) for row in cur.fetchall()]

    def delete_edge(self, edge_id):
        """按 id 删除边。

        Returns:
            bool: 边原本存在则 True，否则 False。
        """
        existed = self.get_edge(edge_id) is not None
        if not existed:
            return False
        with self._conn:
            self._conn.execute("DELETE FROM edges WHERE id = ?", (edge_id,))
        return True

    # ------------------------------------------------------------------ #
    # 向量关联与语义检索
    # ------------------------------------------------------------------ #
    def _node_embedding_text(self, name, type, properties) -> str:
        """拼装节点向量化文本：名称 + 类型 + 属性值合并。"""
        parts = [name]
        if type:
            parts.append(type)
        for value in (properties or {}).values():
            if isinstance(value, (list, dict)):
                value = json.dumps(value, ensure_ascii=False)
            parts.append(str(value))
        return " ".join(parts)

    def _index_node_vector(self, node_id, name, type, properties) -> None:
        """计算节点向量写入向量库。未注入 embed_callable 时跳过。"""
        if self.embed is None:
            return
        text = self._node_embedding_text(name, type, properties)
        vector = self.embed(text)
        self.vector_store.upsert(
            node_id,
            list(vector),
            metadata={"node_id": node_id, "name": name, "type": type},
        )

    def search_nodes(self, query, top_k=10):
        """语义检索节点。

        流程：embed(查询) → VectorStore 检索 top_k → 取回节点及其关系。

        Args:
            query: 查询文本。
            top_k: 返回的最大结果数。

        Returns:
            list[dict]: 每个元素为
                {"node": {...节点}, "edges": [...关联边], "score": float}，
                按相关性降序。

        Raises:
            ValueError: 未注入 embed_callable 时。
        """
        if self.embed is None:
            raise ValueError("未注入 embed_callable，无法执行语义检索。")
        query_vector = list(self.embed(query))
        results = self.vector_store.search(query_vector, top_k=int(top_k))
        hits = []
        for res in results:
            node_id = res.get("vector_id")
            node = self.get_node(node_id)
            if node is None:
                continue
            hits.append(
                {
                    "node": node,
                    "edges": self.get_edges_by_node(node_id),
                    "score": float(res.get("score", 0.0)),
                }
            )
        return hits

    # ------------------------------------------------------------------ #
    # 结果序列化辅助
    # ------------------------------------------------------------------ #
    @staticmethod
    def _row_to_node(row):
        """将 nodes 行转换为字典，properties 反序列化为字典。"""
        return {
            "id": row["id"],
            "name": row["name"],
            "type": row["type"],
            "properties": json.loads(row["properties"]) if row["properties"] else {},
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _row_to_edge(row):
        """将 edges 行转换为字典，properties 反序列化为字典。"""
        return {
            "id": row["id"],
            "source": row["source"],
            "target": row["target"],
            "relation": row["relation"],
            "properties": json.loads(row["properties"]) if row["properties"] else {},
            "created_at": row["created_at"],
        }