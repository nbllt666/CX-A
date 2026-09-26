# -*- coding: utf-8 -*-
"""向量存储适配层——封装 LanceDB、内存与 SQLite 持久向量检索。

VectorStore 为抽象基类，定义统一 upsert / search / delete 接口；
LanceVectorStore 适配 LanceDB（可选依赖 lancedb，未安装时实例化抛 RuntimeError）；
InMemoryVectorStore 为纯 Python 实现（cosine 距离），供单测与无 lancedb 环境使用；
SQLiteVectorStore 为纯标准库 SQLite 持久向量库（float32 BLOB 落盘、brute-force cosine），
供真实语义嵌入向量的长期保存与重启后检索。
"""

import json
import logging
import math
import os
import sqlite3
from abc import ABC, abstractmethod
from array import array
from datetime import datetime

# 原生日志记录器（prepare 嵌入模型标识变化告警留痕）
LOGGER = logging.getLogger(__name__)


def _now() -> str:
    """当前时间戳（微秒精度，与 MemoryStore 同口径）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")


def _parse_metadata(value):
    """读取 metadata：JSON 解析成功返回结构，失败回落原字符串（与 storage.py 同口径）。"""
    if value is None:
        return None
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


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

    def vector_ids(self):
        """可选能力：返回已知向量 id 集合；未实现该能力的子类返回 None（调用方需容错）。"""
        return None


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
        """按 vector_id 删除向量。

        G-5：filter 单引号 doubling 转义——node_id 含 ``'`` 时不再使
        LanceDB filter 语法崩溃。
        """
        if self._table is None:
            return False
        from_lance_filter = "vector_id = '{0}'".format(
            str(vector_id).replace("'", "''")
        )
        self._table.delete(from_lance_filter)
        return True

    def vector_ids(self):
        """可选能力 best-effort：返回已知向量 id 集合。

        依赖 lancedb 表可导出 Arrow（``to_arrow()["vector_id"]``）；
        表尚未创建或导出异常时返回 None（调用方容错）。
        """
        if self._table is None:
            return None
        try:
            column = self._table.to_arrow()["vector_id"].to_pylist()
        except Exception:
            return None
        return set(column)


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

    def vector_ids(self):
        """可选能力实现：返回已知向量 id 集合。"""
        return set(self._vectors.keys())


class SQLiteVectorStore(VectorStore):
    """SQLite 持久向量库：向量以 float32 BLOB 落盘（与 memories.db 同库不同表），
    检索为全量 brute-force cosine（纯标准库，语义与 InMemoryVectorStore 一致）。
    """

    def __init__(self, db_path, model_tag="", dim=None,
                 table_name="memory_vectors", meta_table_name="memory_vectors_meta"):
        """初始化持久向量库并建立向量表 / 元表。

        惰性建立 sqlite3 连接（check_same_thread=False，与 MemoryStore 同口径：
        单线程串行访问；db_path 的父目录自动创建）；构造即建表（CREATE TABLE
        IF NOT EXISTS，幂等）——但构造不做任何清空，清空仅由 prepare() 触发。
        """
        self.db_path = db_path
        self.model_tag = model_tag
        self.dim = dim
        self.table_name = table_name
        self.meta_table_name = meta_table_name
        self._conn = None
        #: 脏向量行告警去重标记（20260926 深挖：损坏 blob 不再使检索整体失败）
        self._corrupt_warned = False
        self._ensure_tables()

    # ------------------------------------------------------------------ 连接与建表
    def _connect(self):
        """惰性建立连接，必要时自动创建数据库所在目录（口径同 MemoryStore）。"""
        if self._conn is None:
            parent = os.path.dirname(self.db_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        return self._conn

    def _ensure_tables(self):
        """确保向量表与元表存在（CREATE TABLE IF NOT EXISTS，幂等）。"""
        conn = self._connect()
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self.table_name} ("
            "vector_id TEXT PRIMARY KEY,"
            " dim INTEGER NOT NULL,"
            " norm REAL NOT NULL,"
            " vector BLOB NOT NULL,"
            " metadata TEXT,"
            " updated_at TEXT NOT NULL)"
        )
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self.meta_table_name} ("
            "key TEXT PRIMARY KEY, value TEXT)"
        )
        conn.commit()

    def close(self):
        """关闭数据库连接（幂等）。"""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------ 元表读写
    def _read_meta(self, key):
        """读取元记录值（缺失返回 None）。"""
        row = self._connect().execute(
            f"SELECT value FROM {self.meta_table_name} WHERE key=?", (key,)
        ).fetchone()
        return None if row is None else row[0]

    def _write_meta(self, key, value):
        """覆写元记录值（None 以 NULL 存）。"""
        conn = self._connect()
        conn.execute(
            f"INSERT OR REPLACE INTO {self.meta_table_name} (key, value) VALUES (?, ?)",
            (key, None if value is None else str(value)),
        )
        conn.commit()

    def _meta_matches(self, previous):
        """比对元记录与当前 model_tag / dim 是否一致（dim 为 None 表示不校验维度）。"""
        if previous["model_tag"] != self.model_tag:
            return False
        if self.dim is None:
            return True
        return previous["dim"] == int(self.dim)

    def prepare(self) -> dict:
        """按当前 model_tag / dim 校准向量索引表，返回三态动作描述。

        - init：元表无记录 → 写入当前值；
        - ok：与元记录一致 → 无操作；
        - reset：不一致 → 清空向量表（DELETE）+ 覆写元记录并告警
          （嵌入模型标识变化必须重建索引）。

        dim 为 None 表示不校验维度。注意：仅 prepare() 会清空向量，构造不会。

        Returns:
            dict: {"action": "init|ok|reset", "previous": {...}|None, "current": {...}}
            其中 previous / current 为 {"model_tag": ..., "dim": ...} 形态。
        """
        self._ensure_tables()
        prev_tag = self._read_meta("model_tag")
        prev_dim = self._read_meta("dim")
        previous = None
        if prev_tag is not None or prev_dim is not None:
            previous = {
                "model_tag": prev_tag,
                "dim": None if prev_dim is None else int(prev_dim),
            }
        current = {"model_tag": self.model_tag, "dim": self.dim}

        if previous is None:
            action = "init"
        elif self._meta_matches(previous):
            action = "ok"
        else:
            conn = self._connect()
            conn.execute(f"DELETE FROM {self.table_name}")
            conn.commit()
            LOGGER.warning(
                "嵌入模型标识变化（旧=%s 新=%s），已重置向量索引表",
                previous, current,
            )
            action = "reset"

        if action != "ok":
            self._write_meta("model_tag", self.model_tag)
            self._write_meta("dim", self.dim)
        return {"action": action, "previous": previous, "current": current}

    # ------------------------------------------------------------------ 写
    def upsert(self, vector_id, vector, metadata=None) -> bool:
        """写入或覆盖向量（INSERT OR REPLACE）。

        norm 存写入时算好的 L2 范数；向量以 float32 BLOB 落盘；metadata 非 None
        时以 JSON（ensure_ascii=False）存文本。
        """
        self._ensure_tables()
        values = [float(x) for x in vector]
        blob = array("f", values).tobytes()
        norm = math.sqrt(sum(x * x for x in values))
        meta_text = None if metadata is None else json.dumps(metadata, ensure_ascii=False)
        conn = self._connect()
        conn.execute(
            f"INSERT OR REPLACE INTO {self.table_name} "
            "(vector_id, dim, norm, vector, metadata, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(vector_id), len(values), norm, blob, meta_text, _now()),
        )
        conn.commit()
        return True

    def delete(self, vector_id) -> bool:
        """按 vector_id 删除向量，返回是否命中（rowcount > 0）。"""
        self._ensure_tables()
        conn = self._connect()
        cur = conn.execute(
            f"DELETE FROM {self.table_name} WHERE vector_id=?", (str(vector_id),)
        )
        conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------ 读
    def _cosine_row(self, query, qnorm, dim, norm, blob):
        """单行 cosine：dot / (qnorm * row.norm)；任一方为 0、维度不匹配或 blob 损坏返回 0.0。

        20260926 深挖加固：损坏的向量行（长度非法 blob / NULL）此前会把
        ``ValueError`` 抛到 API 层，被误映射为 HTTP 400 "bad_request"（把服务端
        数据问题表现为客户端错误，且整次检索失败）。现按"单条脏数据记 0 分"
        口径处理（与 InMemoryVectorStore._cosine 的兜底一致），并首次告警一次。
        """
        if qnorm == 0 or not norm or dim != len(query):
            return 0.0
        try:
            row = array("f")
            row.frombytes(blob)
            row = list(row)
        except (TypeError, ValueError, BufferError) as exc:
            if not self._corrupt_warned:
                self._corrupt_warned = True
                LOGGER.warning("向量行数据损坏已按 0 分跳过（后续同类不再重复告警）：%s", exc)
            return 0.0
        if len(row) != len(query):
            return 0.0
        dot = sum(x * y for x, y in zip(query, row))
        return dot / (qnorm * norm)

    def search(self, vector, top_k=10) -> list:
        """全量 brute-force cosine 检索，返回按相关性降序的 top_k 条。

        排序规则：score 降序，同分按 vector_id 字符串升序（确定性 tie-break）。
        查询向量范数只算一次，逐行 dot / (qnorm * row.norm)；任一方为 0 或维度
        不匹配时该行 score 记 0.0（与 InMemoryVectorStore._cosine 兜底一致）。
        metadata 读取 json.loads 失败回落原字符串（与 storage.py 同口径）。

        Args:
            vector: 查询向量。
            top_k: 返回条数上限，经 int() 转换，≤0 直接返回 []。
        Returns:
            list[dict]: [{"vector_id", "score", "metadata"}]。
        """
        limit = int(top_k)
        if limit <= 0:
            return []
        self._ensure_tables()
        query = [float(x) for x in vector]
        qnorm = math.sqrt(sum(x * x for x in query))
        rows = self._connect().execute(
            f"SELECT vector_id, dim, norm, vector, metadata FROM {self.table_name}"
        ).fetchall()
        scored = [
            {
                "vector_id": vector_id,
                "score": self._cosine_row(query, qnorm, dim, norm, blob),
                "metadata": _parse_metadata(meta_text),
            }
            for vector_id, dim, norm, blob, meta_text in rows
        ]
        scored.sort(key=lambda item: (-item["score"], str(item["vector_id"])))
        return scored[:limit]

    def count(self) -> int:
        """返回向量条数。"""
        self._ensure_tables()
        row = self._connect().execute(
            f"SELECT COUNT(*) FROM {self.table_name}"
        ).fetchone()
        return int(row[0]) if row else 0

    def vector_ids(self) -> set:
        """可选能力实现：返回已知向量 id 的 str 集合。"""
        self._ensure_tables()
        rows = self._connect().execute(
            f"SELECT vector_id FROM {self.table_name}"
        ).fetchall()
        return {row[0] for row in rows}