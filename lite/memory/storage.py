# -*- coding: utf-8 -*-
"""记忆持久化存储——基于 sqlite3 标准库的 MemoryStore 实现。

提供 memories 表建表、新增、按 id 查询、更新（自动刷新 updated_at）、软删除、
列表查询与 agent_id 过滤能力。同步预留字段仅随建表登记，本阶段不实现同步逻辑。
TEXT 结构字段（metadata/decay_params/tags）写读均有 JSON 序列化防线（L7）：
写入 dict/list 自动 dumps；读取 try loads 失败回落原字符串。
"""

import json
import logging
import os
import sqlite3
from datetime import datetime

from .schema import COLUMNS, CREATE_INDEX_SQL, CREATE_TABLE_SQL, MEMORY_TYPES

# 原生日志记录器（低-10：update 未知键告警留痕）
LOGGER = logging.getLogger(__name__)

# 解析路径：lite/memory/schema.py -> lite/memory -> lite -> 项目根目录
_MEMORY_DIR = os.path.dirname(os.path.abspath(__file__))
_LITE_DIR = os.path.dirname(_MEMORY_DIR)
_PROJECT_ROOT = os.path.dirname(_LITE_DIR)


def _default_db_path():
    """默认数据库路径：项目根目录下 data/memories.db。

    M-14（第三轮体检批次4）：根解析统一收敛到 ``lite.config.paths.app_root()``
    （frozen-aware），冻结态不再误指向 ``_internal/data``。
    """
    from lite.config.paths import data_root

    return os.path.join(data_root(), "memories.db")


def _now() -> str:
    """当前时间戳（微秒精度，保证相邻写入时间可区分）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")


def _row_to_dict(row):
    """sqlite3.Row 转 dict；空行返回 None。

    读取侧 JSON 防线（L7）：TEXT 结构字段（metadata/decay_params/tags）逐列
    try json.loads，解析成功返回结构化值（dict/list），失败回落原字符串——
    保证消费方总能拿到 dict/list 或 None，或调用方有意写入的纯文本。
    """
    data = dict(row) if row is not None else None
    if data is not None:
        for col in _JSON_TEXT_COLUMNS:
            if col in data:
                data[col] = _parse_json_text(data[col])
    return data


def _parse_json_text(value):
    """尝试把字符串解析为 JSON 结构；非字符串、解析失败一律原样返回。"""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


def _serialize_json_text(value):
    """写入口正规化（L7）：dict/list 自动 JSON 序列化，str 原样存，None 保持 None。

    防止 dict 直接传给 sqlite3 抛 InterfaceError；也防止既有代码忘记序列化。
    """
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


# 插入/更新可操作的列（id 由自动增量维护，created_at 默认由本类写入）
_INSERT_COLUMNS = [
    "type", "content", "vector_id", "metadata", "importance",
    "importance_score", "decay_type", "decay_params",
    "reactivation_count", "emotion_score", "permanent", "tags",
    "created_at", "updated_at", "is_deleted", "source", "agent_id",
    "global_id", "version", "sync_status", "origin",
]

# TEXT 结构字段（JSON 序列化防线作用域，L7）：仅限这三个语义为结构的文本列，
# content 等自由文本列不做解析（避免"恰好是合法 JSON 的正文"被误转换）。
_JSON_TEXT_COLUMNS = ("metadata", "decay_params", "tags")


class MemoryStore:
    """记忆存储访问层（sqlite3 标准库）。"""

    def __init__(self, db_path=None):
        # 未显式提供 db_path 时，指向项目根目录下 data/memories.db（禁止相对路径）
        self.db_path = db_path or _default_db_path()
        self._conn = None
        # 中-1a：检索索引就绪标记（连接级一次性补建，避免每次 _ensure_table 重复执行）
        self._index_ready = False

    # ------------------------------------------------------------------ 连接管理
    def _connect(self) -> sqlite3.Connection:
        """惰性建立连接，必要时自动创建数据库所在目录。

        以 check_same_thread=False 建立连接：允许连接被创建线程之外的线程复用。
        依赖方（lite/server/api_server.py 的轻量 REST 服务）为单线程串行访问，
        不存在并发读写，此放宽是安全的。既有单线程调用行为不变。
        """
        if self._conn is None:
            parent = os.path.dirname(self.db_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def close(self):
        """关闭数据库连接。"""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    # ------------------------------------------------------------------ 建表
    def create_table(self) -> bool:
        """创建 memories 表，并确保检索组合索引存在（中-1a）。"""
        conn = self._connect()
        conn.execute(CREATE_TABLE_SQL)
        conn.execute(CREATE_INDEX_SQL)
        conn.commit()
        self._index_ready = True
        return True

    def _ensure_table(self):
        """表不存在时自动建表（auto_init 规范）。

        中-1a：每次调用幂等确保检索索引存在——CREATE INDEX IF NOT EXISTS
        既有库（无索引旧库）升级路径在此补建；连接级标记避免重复执行。
        """
        conn = self._connect()
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memories'"
        ).fetchone()
        if row is None:
            conn.execute(CREATE_TABLE_SQL)
            conn.commit()
        if not self._index_ready:
            conn.execute(CREATE_INDEX_SQL)
            conn.commit()
            self._index_ready = True

    # ------------------------------------------------------------------ 校验
    @staticmethod
    def _validate_type(value):
        if value not in MEMORY_TYPES:
            raise ValueError(f"非法记忆类型 {value!r}，可选: {MEMORY_TYPES}")

    @staticmethod
    def _validate_content(value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("记忆内容 content 不能为空")

    # ------------------------------------------------------------------ 写
    def add(self, memory: dict) -> int:
        """新增一条记忆，返回新记录 id。

        Args:
            memory: 记忆字段 dict，至少包含 type 与 content。
        Returns:
            int: 新记录的 id。
        Raises:
            ValueError: type 非法 或 content 为空。
        """
        self._validate_type(memory.get("type"))
        self._validate_content(memory.get("content"))
        self._ensure_table()

        now = _now()
        values = []
        for col in _INSERT_COLUMNS:
            default = COLUMNS.get(col, (None, None, False))[1]
            if col in ("created_at", "updated_at"):
                # 时间戳缺失时自动填充当前时间
                values.append(memory.get(col, None) or now)
            elif col in _JSON_TEXT_COLUMNS:
                # L7：TEXT 结构字段写入口正规化，dict/list 自动序列化防 InterfaceError
                values.append(_serialize_json_text(memory.get(col, default)))
            else:
                values.append(memory.get(col, default))
        placeholders = ",".join("?" for _ in values)
        sql = f"INSERT INTO memories ({','.join(_INSERT_COLUMNS)}) VALUES ({placeholders})"
        cur = self._connect().execute(sql, values)
        self._connect().commit()
        return cur.lastrowid

    def update(self, memory_id: int, fields: dict) -> int:
        """按 id 更新字段，自动刷新 updated_at。返回受影响行数。

        未知键处理（低-10，第四轮体检批次B）：部分未知键时忽略并在日志告警
        指明被忽略键名；全部为未知键时直接返回 0——不再空刷新 updated_at 并
        以 rowcount 误导调用方"更新成功"。

        Raises:
            ValueError: fields 中 type 非法。
        """
        if not fields:
            return 0
        if "type" in fields:
            self._validate_type(fields["type"])
        unknown_keys = [k for k in fields if k not in _INSERT_COLUMNS]
        if unknown_keys:
            LOGGER.warning("update 忽略未知字段 keys=%s（memory_id=%s）", unknown_keys, memory_id)
        updates = {
            k: (_serialize_json_text(v) if k in _JSON_TEXT_COLUMNS else v)
            for k, v in fields.items()
            if k in _INSERT_COLUMNS
        }
        if not updates:
            # 全为未知键：无事可做，返回 0（低-10：不再空刷新 updated_at 误导调用方）
            LOGGER.warning("update 仅收到未知字段，未做任何更新（memory_id=%s）", memory_id)
            return 0
        updates["updated_at"] = _now()
        sets = ",".join(f"{k}=?" for k in updates)
        params = list(updates.values()) + [memory_id]
        self._ensure_table()
        cur = self._connect().execute(f"UPDATE memories SET {sets} WHERE id=?", params)
        self._connect().commit()
        return cur.rowcount

    def soft_delete(self, memory_id: int) -> bool:
        """软删除：置 is_deleted=1，同时刷新 updated_at。返回是否命中。"""
        self._ensure_table()
        cur = self._connect().execute(
            "UPDATE memories SET is_deleted=1, updated_at=? WHERE id=?",
            (_now(), memory_id),
        )
        self._connect().commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------ 读
    def get(self, memory_id: int):
        """按 id 获取单条记忆（不区分是否已软删除），不存在返回 None。"""
        self._ensure_table()
        row = self._connect().execute(
            "SELECT * FROM memories WHERE id=?", (memory_id,)
        ).fetchone()
        return _row_to_dict(row)

    def list(self, type=None, limit=None, include_deleted=False, agent_id=None) -> list:
        """查询记忆列表。

        Args:
            type: 记忆类型过滤（None=全部）。
            limit: 返回条数上限（None=不限）。
            include_deleted: 是否包含已软删除记录，默认仅返回未删除。
            agent_id: 按 agent 归属过滤（None=全部）。
        Returns:
            list[dict]: 命中的记忆记录，按 id 升序。
        Raises:
            ValueError: type 非法。
        """
        self._ensure_table()
        conditions, params = [], []
        if not include_deleted:
            conditions.append("is_deleted=0")
        if type is not None:
            self._validate_type(type)
            conditions.append("type=?")
            params.append(type)
        if agent_id is not None:
            conditions.append("agent_id=?")
            params.append(agent_id)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = f"SELECT * FROM memories{where} ORDER BY id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = self._connect().execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]

    def list_recent(self, agent_id=None, limit=500, include_deleted=False) -> list:
        """按 id 降序返回最近 limit 条记忆（有界查询，中-1c）。

        供写入口相似判定（manager._find_similar）等只需近期样本的场景使用，
        默认上限 500 条，避免每次写入对全 agent 全表扫描。过滤语义与
        :meth:`list` 一致，仅排序方向与默认上限不同。

        Args:
            agent_id: 按 agent 归属过滤（None=全部）。
            limit: 返回条数上限（默认 500）。
            include_deleted: 是否包含已软删除记录，默认仅返回未删除。
        Returns:
            list[dict]: 最近写入的记忆记录，按 id 降序。
        """
        self._ensure_table()
        conditions, params = [], []
        if not include_deleted:
            conditions.append("is_deleted=0")
        if agent_id is not None:
            conditions.append("agent_id=?")
            params.append(agent_id)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = f"SELECT * FROM memories{where} ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        rows = self._connect().execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]