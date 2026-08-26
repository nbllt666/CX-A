# -*- coding: utf-8 -*-
"""MemoryStore 的 CRUD / 软删除 / updated_at 自动刷新 / type 校验 / agent_id 过滤测试。"""

import os

import pytest

from lite.memory.storage import MemoryStore


@pytest.fixture()
def store(tmp_path):
    s = MemoryStore(db_path=str(tmp_path / "memories.db"))
    s.create_table()
    yield s
    s.close()


# ---------------------------------------------------------------- 建表
def test_create_table(store):
    # 再次建表（幂等）不应报错
    assert store.create_table() is True


# ---------------------------------------------------------------- CRUD
def test_add_and_get(store):
    mid = store.add({"type": "short_term", "content": "用户喜欢咖啡", "agent_id": "alice"})
    assert isinstance(mid, int) and mid >= 1
    row = store.get(mid)
    assert row["id"] == mid
    assert row["content"] == "用户喜欢咖啡"
    assert row["type"] == "short_term"
    assert row["agent_id"] == "alice"


def test_add_auto_fills_defaults(store):
    mid = store.add({"type": "long_term", "content": "默认值检查"})
    row = store.get(mid)
    assert row["importance"] == 3
    assert row["importance_score"] == 0.6
    assert row["decay_type"] == "ebbinghaus_opt"
    assert row["reactivation_count"] == 0
    assert row["emotion_score"] == 0.0
    assert row["permanent"] == 0
    assert row["is_deleted"] == 0
    assert row["source"] == "user"
    assert row["agent_id"] == "default"
    assert row["version"] == 1
    assert row["sync_status"] == "local"
    assert row["origin"] == "local"
    assert row["created_at"] is not None
    assert row["updated_at"] is not None


def test_get_missing_returns_none(store):
    assert store.get(99999) is None


def test_update_content(store):
    mid = store.add({"type": "short_term", "content": "旧内容"})
    rows = store.update(mid, {"content": "新内容", "importance": 5})
    assert rows == 1
    row = store.get(mid)
    assert row["content"] == "新内容"
    assert row["importance"] == 5


def test_update_refreshes_updated_at(store):
    mid = store.add({"type": "short_term", "content": "a"})
    before = store.get(mid)["updated_at"]
    store.update(mid, {"content": "b"})
    after = store.get(mid)["updated_at"]
    assert after != before, "updated_at 应在更新时自动刷新"
    assert store.get(mid)["content"] == "b"


def test_update_empty_fields(store):
    mid = store.add({"type": "short_term", "content": "a"})
    assert store.update(mid, {}) == 0


# ---------------------------------------------------------------- 软删除
def test_soft_delete(store):
    mid = store.add({"type": "short_term", "content": "待删除"})
    assert store.soft_delete(mid) is True
    row = store.get(mid)
    assert row["is_deleted"] == 1
    ids = [r["id"] for r in store.list()]
    assert mid not in ids, "默认 list 不应包含已软删除记录"
    ids_all = [r["id"] for r in store.list(include_deleted=True)]
    assert mid in ids_all


def test_soft_delete_missing(store):
    assert store.soft_delete(99999) is False


# ---------------------------------------------------------------- type 与 content 校验
def test_add_invalid_type_raises(store):
    with pytest.raises(ValueError):
        store.add({"type": "invalid_type", "content": "x"})


def test_add_empty_content_raises(store):
    with pytest.raises(ValueError):
        store.add({"type": "short_term", "content": ""})
    with pytest.raises(ValueError):
        store.add({"type": "short_term", "content": "   "})
    with pytest.raises(ValueError):
        store.add({"type": "short_term", "content": 123})


def test_update_invalid_type_raises(store):
    mid = store.add({"type": "short_term", "content": "x"})
    with pytest.raises(ValueError):
        store.update(mid, {"type": "bad"})


def test_list_invalid_type_raises(store):
    with pytest.raises(ValueError):
        store.list(type="bad")


# ---------------------------------------------------------------- 列表与过滤
def test_list_type_filter(store):
    store.add({"type": "long_term", "content": "a"})
    store.add({"type": "long_term", "content": "b"})
    store.add({"type": "short_term", "content": "c"})
    lt = store.list(type="long_term")
    assert {r["content"] for r in lt} == {"a", "b"}


def test_list_agent_id_filter(store):
    store.add({"type": "long_term", "content": "a", "agent_id": "x"})
    store.add({"type": "long_term", "content": "b", "agent_id": "y"})
    store.add({"type": "short_term", "content": "c", "agent_id": "x"})
    alice = store.list(agent_id="x")
    assert {r["content"] for r in alice} == {"a", "c"}
    both = store.list(type="long_term", agent_id="x")
    assert [r["content"] for r in both] == ["a"]


def test_list_limit(store):
    for i in range(5):
        store.add({"type": "long_term", "content": f"m{i}"})
    assert len(store.list(type="long_term", limit=2)) == 2


def test_list_order_by_id_asc(store):
    store.add({"type": "long_term", "content": "first"})
    store.add({"type": "long_term", "content": "second"})
    ids = [r["id"] for r in store.list(type="long_term")]
    assert ids == sorted(ids)


# ---------------------------------------------------------------- 默认路径
def test_default_db_path_is_absolute_under_data():
    s = MemoryStore()
    assert os.path.isabs(s.db_path)
    assert os.path.basename(s.db_path) == "memories.db"
    assert os.path.basename(os.path.dirname(s.db_path)) == "data"