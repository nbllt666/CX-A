# -*- coding: utf-8 -*-
"""Task F3：记忆同步预留字段复核与留痕测试。

工程文档 §14：当前暂不同步（轻量↔重度），但表结构已预留同步字段
（global_id / version / sync_status / origin），后期加同步不用改表。
F3 目标：确认字段已建、确认同步逻辑未实现、文档留痕。

本测试断言四层：
  ① schema.COLUMNS 含 4 个同步字段且默认值正确（version=1 / sync_status=local / origin=local）；
  ② 建表后 PRAGMA 实际列含 4 字段且落库默认值正确；
  ③ MemoryStore.add / update 可写入并回读 4 字段（提供字段即可存储，不触发任何同步动作）；
  ④ 同步逻辑未实现（文本断言）：memory 包与 lite 全源码不存在任何执行同步任务的
     函数（def sync_ / def pull( / def push( / def merge_sync ...），也不导入任何同步模块，
     schema 仅用同步字段建列、storage 仅透传——以源码文本断言体现"未实现"。
"""

import re
import sqlite3
from pathlib import Path

import pytest

from lite.memory.schema import COLUMNS, CREATE_TABLE_SQL, SYNC_RESERVED_COLUMNS
from lite.memory.storage import MemoryStore

# lite 源码根与 memory 包根（绝对路径，禁止相对路径）
_LITE_DIR = Path(__file__).resolve().parents[1] / "lite"
_MEMORY_DIR = _LITE_DIR / "memory"

# 同步执行函数签名：实现同步逻辑才会出现的任务函数定义
_SYNC_FN_PATTERNS = (
    "def sync_",
    "def pull(",
    "def push(",
    "def merge_sync",
    "def import_sync",
    "def export_sync",
)

# 导入同步模块的正则：以 import / from 开头且含 sync 令牌（如 import xxx_sync / from ... import sync）
_IMPORT_SYNC_RE = re.compile(r"^\s*(?:import|from)\b.*\bsync\b", re.MULTILINE)


def _iter_python_files(root: Path):
    """递归枚举目录下所有 .py 源码文件（跳过 __pycache__）。"""
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        yield path


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ------------------------------------------------------------------ ① schema 契约
def test_schema_columns_contains_sync_reserved():
    assert set(SYNC_RESERVED_COLUMNS) <= set(COLUMNS), "schema.COLUMNS 应包含全部同步预留字段"


def test_schema_sync_reserved_defaults():
    # 默认值契约：version=1 / sync_status=local / origin=local；global_id 无 SQL 默认（应用层生成）
    assert COLUMNS["global_id"] == ("VARCHAR", None, False)
    assert COLUMNS["version"] == ("INTEGER", 1, False)
    assert COLUMNS["sync_status"] == ("VARCHAR", "local", False)
    assert COLUMNS["origin"] == ("VARCHAR", "local", False)


# ------------------------------------------------------------------ ② PRAGMA 实际列
@pytest.fixture(scope="module")
def table_info():
    """在内存库中执行建表 SQL 并读取 PRAGMA table_info。"""
    conn = sqlite3.connect(":memory:")
    conn.executescript(CREATE_TABLE_SQL)
    info = {}
    for row in conn.execute("PRAGMA table_info(memories)").fetchall():
        # row: cid, name, type, notnull, dflt_value, pk
        raw = row[4]
        dflt = None if raw is None else str(raw).strip("'")
        info[row[1]] = {"type": row[2], "notnull": row[3], "dflt": dflt, "pk": row[5]}
    conn.close()
    return info


def test_pragma_actual_columns_contain_sync_reserved(table_info):
    actual = set(table_info.keys())
    assert set(SYNC_RESERVED_COLUMNS) <= actual, "建表后 PRAGMA 实际列应含 4 个同步字段"


def test_pragma_sync_reserved_defaults(table_info):
    assert table_info["version"]["dflt"] == "1"
    assert table_info["sync_status"]["dflt"] == "local"
    assert table_info["origin"]["dflt"] == "local"
    assert table_info["global_id"]["dflt"] is None


# ------------------------------------------------------------------ ③ 存储可写入/回读（仅透传，不触发同步）
@pytest.fixture()
def store(tmp_path):
    s = MemoryStore(db_path=str(tmp_path / "sync_reserved.db"))
    s.create_table()
    yield s
    s.close()


def test_store_add_writes_and_reads_sync_fields(store):
    mid = store.add(
        {
            "type": "long_term",
            "content": "同步字段写入回读",
            "global_id": "gid-abc-123",
            "version": 7,
            "sync_status": "synced",
            "origin": "remote",
        }
    )
    row = store.get(mid)
    assert row["global_id"] == "gid-abc-123"
    assert row["version"] == 7
    assert row["sync_status"] == "synced"
    assert row["origin"] == "remote"


def test_store_add_apply_defaults_without_sync_logic(store):
    # 不提供同步字段时按默认落库，无任何同步动作被触发（仅本地默认值）
    mid = store.add({"type": "short_term", "content": "默认同步字段"})
    row = store.get(mid)
    assert row["version"] == 1
    assert row["sync_status"] == "local"
    assert row["origin"] == "local"
    assert row["global_id"] is None


def test_store_update_can_alter_sync_status(store):
    mid = store.add({"type": "short_term", "content": "a"})
    store.update(mid, {"sync_status": "pending", "version": 2})
    row = store.get(mid)
    assert row["sync_status"] == "pending"
    assert row["version"] == 2


# ------------------------------------------------------------------ ④ 同步逻辑未实现（文本断言）
def test_memory_package_has_no_sync_function():
    """memory 包内所有源码不得含同步任务函数定义。"""
    for path in _iter_python_files(_MEMORY_DIR):
        src = _source(path)
        for pat in _SYNC_FN_PATTERNS:
            assert pat not in src, f"{path.name} 含同步函数定义 {pat!r}，同步逻辑已实现（违反 F3）"


def test_lite_source_has_no_sync_function():
    """lite 全源码不得含记忆同步任务函数定义。"""
    for path in _iter_python_files(_LITE_DIR):
        src = _source(path)
        for pat in _SYNC_FN_PATTERNS:
            assert pat not in src, f"{path} 含同步函数定义 {pat!r}，同步逻辑已实现（违反 F3）"


def test_memory_package_imports_no_sync_module():
    """memory 包不得导入任何同步相关模块。"""
    for path in _iter_python_files(_MEMORY_DIR):
        hit = _IMPORT_SYNC_RE.search(_source(path))
        assert hit is None, f"{path.name} 第 {hit.group()!r} 行导入了同步模块（违反 F3）"


def test_sync_columns_only_definition_and_pass_through():
    """仅 schema 用同步字段建列、storage 透传，无任何同步逻辑消费（改写/调度）同步字段。"""
    schema_src = _source(_MEMORY_DIR / "schema.py")
    storage_src = _source(_MEMORY_DIR / "storage.py")
    for pat in _SYNC_FN_PATTERNS:
        assert pat not in schema_src
        assert pat not in storage_src
    # MemoryStore 公开 API 面仅为 CRUD/软删/列表，不提供 pull/push/sync 动作
    for forbid in ("def pull", "def push", "def sync"):
        assert forbid not in storage_src, f"storage.py 不应提供同步动作 API {forbid!r}"