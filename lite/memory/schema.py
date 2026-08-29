# -*- coding: utf-8 -*-
"""记忆数据契约——memories 表结构定义（SQLite 数据契约）。

严格遵循工程文档 §8.3。共 22 个字段，其中 global_id / version / sync_status /
origin 为同步预留字段——本阶段仅建列登记，不实现任何同步逻辑，供后续阶段接续。
"""

import uuid

# 记忆类型取值枚举（与 CX-O 对齐）
MEMORY_TYPES = ("long_term", "short_term", "permanent")

# 表名
TABLE_NAME = "memories"

# memories 表 DDL（版本对齐工程文档 §8.3）
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type VARCHAR(20) NOT NULL,
    content TEXT NOT NULL,
    vector_id VARCHAR(100),
    metadata TEXT,
    importance INTEGER DEFAULT 3,
    importance_score FLOAT DEFAULT 0.6,
    decay_type VARCHAR(20) DEFAULT 'ebbinghaus_opt',
    decay_params TEXT,
    reactivation_count INTEGER DEFAULT 0,
    emotion_score FLOAT DEFAULT 0.0,
    permanent BOOLEAN DEFAULT FALSE,
    tags TEXT,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    is_deleted BOOLEAN DEFAULT FALSE,
    source VARCHAR(50) DEFAULT 'user',
    agent_id VARCHAR(100) DEFAULT 'default',
    global_id VARCHAR(64),
    version INTEGER DEFAULT 1,
    sync_status VARCHAR(20) DEFAULT 'local',
    origin VARCHAR(20) DEFAULT 'local'
);
"""

# 检索高频过滤组合索引（第四轮体检批次B·中-1a）：
# list / list_recent / 检索候选查询均按 agent_id + is_deleted 过滤，
# 组合索引避免全表扫描；IF NOT EXISTS 幂等，既有库在 storage 初始化处自动补建。
CREATE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_memories_agent ON memories(agent_id, is_deleted)"
)

# 逐字段契约：字段名 -> (SQL 声明类型, 默认值, 是否 NOT NULL)
# 默认值以 SQL 落库值为准（python 侧用于 add 时字段自动补全）。
COLUMNS = {
    "id": ("INTEGER", None, True),
    "type": ("VARCHAR", None, True),
    "content": ("TEXT", None, True),
    "vector_id": ("VARCHAR", None, False),
    "metadata": ("TEXT", None, False),
    "importance": ("INTEGER", 3, False),
    "importance_score": ("FLOAT", 0.6, False),
    "decay_type": ("VARCHAR", "ebbinghaus_opt", False),
    "decay_params": ("TEXT", None, False),
    "reactivation_count": ("INTEGER", 0, False),
    "emotion_score": ("FLOAT", 0.0, False),
    "permanent": ("BOOLEAN", False, False),
    "tags": ("TEXT", None, False),
    "created_at": ("TIMESTAMP", None, False),
    "updated_at": ("TIMESTAMP", None, False),
    "is_deleted": ("BOOLEAN", False, False),
    "source": ("VARCHAR", "user", False),
    "agent_id": ("VARCHAR", "default", False),
    "global_id": ("VARCHAR", None, False),
    "version": ("INTEGER", 1, False),
    "sync_status": ("VARCHAR", "local", False),
    "origin": ("VARCHAR", "local", False),
}

# 同步预留字段（本阶段仅建列，同步逻辑后置）
SYNC_RESERVED_COLUMNS = ("global_id", "version", "sync_status", "origin")


def generate_global_id() -> str:
    """生成全局唯一 ID（同步预留字段 global_id 使用）。"""
    return uuid.uuid4().hex