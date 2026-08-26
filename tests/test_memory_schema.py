# -*- coding: utf-8 -*-
"""memories 表字段完整性契约测试。

对 CREATE TABLE 在建表后通过 PRAGMA table_info 验证列名 / 类型 / 默认值 / NOT NULL
全覆盖，其中同步预留 4 字段（global_id / version / sync_status / origin）必须存在。
"""

import sqlite3

import pytest

from lite.memory.schema import (
    COLUMNS,
    CREATE_TABLE_SQL,
    MEMORY_TYPES,
    SYNC_RESERVED_COLUMNS,
)

# 期望字段 -> DDL 声明的完整类型（含长度修饰，对应 PRAGMA table_info 的 type 列）
EXPECTED_COLUMNS = {
    "id": "INTEGER",
    "type": "VARCHAR(20)",
    "content": "TEXT",
    "vector_id": "VARCHAR(100)",
    "metadata": "TEXT",
    "importance": "INTEGER",
    "importance_score": "FLOAT",
    "decay_type": "VARCHAR(20)",
    "decay_params": "TEXT",
    "reactivation_count": "INTEGER",
    "emotion_score": "FLOAT",
    "permanent": "BOOLEAN",
    "tags": "TEXT",
    "created_at": "TIMESTAMP",
    "updated_at": "TIMESTAMP",
    "is_deleted": "BOOLEAN",
    "source": "VARCHAR(50)",
    "agent_id": "VARCHAR(100)",
    "global_id": "VARCHAR(64)",
    "version": "INTEGER",
    "sync_status": "VARCHAR(20)",
    "origin": "VARCHAR(20)",
}

# 期望落库默认值（SQLite dflt_value 归一化后的值）
EXPECTED_DEFAULTS = {
    "importance": "3",
    "importance_score": "0.6",
    "decay_type": "ebbinghaus_opt",
    "reactivation_count": "0",
    "emotion_score": "0.0",
    "permanent": "0",
    "is_deleted": "0",
    "source": "user",
    "agent_id": "default",
    "version": "1",
    "sync_status": "local",
    "origin": "local",
}

# 必须 NOT NULL 的字段
NOT_NULL_COLUMNS = ["type", "content"]


def _norm_default(raw):
    """归一化 PRAGMA dflt_value：去掉引号，FALSE/0 归一为 '0'。"""
    if raw is None:
        return None
    s = str(raw)
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        s = s[1:-1]
    if s.lower() == "false":
        return "0"
    return s


@pytest.fixture(scope="module")
def table_info():
    """在内存库中执行建表 SQL 并读取 sqlite_master 列信息。"""
    conn = sqlite3.connect(":memory:")
    conn.executescript(CREATE_TABLE_SQL)
    info = {}
    for row in conn.execute("PRAGMA table_info(memories)").fetchall():
        # row: cid, name, type, notnull, dflt_value, pk
        info[row[1]] = {
            "type": row[2],
            "notnull": row[3],
            "dflt": _norm_default(row[4]),
            "pk": row[5],
        }
    conn.close()
    return info


def test_table_exists(table_info):
    assert table_info, "memories 表未成功创建"


def test_field_count(table_info):
    # 工程文档 §8.3 共 22 字段
    assert len(table_info) == 22
    assert len(EXPECTED_COLUMNS) == 22


def test_all_expected_columns_present(table_info):
    assert set(table_info.keys()) == set(EXPECTED_COLUMNS.keys())


def test_column_types(table_info):
    for name, expected_type in EXPECTED_COLUMNS.items():
        assert table_info[name]["type"] == expected_type, f"字段 {name} 类型不符"


def test_sync_reserved_columns_present(table_info):
    for name in SYNC_RESERVED_COLUMNS:
        assert name in table_info, f"同步预留字段 {name} 缺失"


def test_sync_reserved_types(table_info):
    assert table_info["global_id"]["type"] == "VARCHAR(64)"
    assert table_info["version"]["type"] == "INTEGER"
    assert table_info["sync_status"]["type"] == "VARCHAR(20)"
    assert table_info["origin"]["type"] == "VARCHAR(20)"


def test_not_null_columns(table_info):
    for name in NOT_NULL_COLUMNS:
        assert table_info[name]["notnull"] == 1, f"字段 {name} 应 NOT NULL"


def test_id_is_pk_autoincrement(table_info):
    assert table_info["id"]["pk"] == 1, "id 应是主键"


def test_defaults(table_info):
    for name, expected in EXPECTED_DEFAULTS.items():
        assert table_info[name]["dflt"] == expected, f"字段 {name} 默认值不符"


def test_default_nullable_columns(table_info):
    for name, (_, default, notnull) in COLUMNS.items():
        if default is None:
            assert table_info[name]["dflt"] is None, f"字段 {name} 不应有默认值"


def test_memory_types_enum():
    assert set(MEMORY_TYPES) == {"long_term", "short_term", "permanent"}


def test_columns_contract_matches_schema_module(table_info):
    # 契约常量 COLUMNS 与建表实际列完全一致
    assert set(COLUMNS.keys()) == set(table_info.keys())