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


# ---------------------------------------------------------------- TEXT 结构字段 JSON 防线（L7）
def test_add_dict_text_fields_roundtrip(store):
    """dict/list 写入 metadata/decay_params/tags 不抛 InterfaceError，读取自动解析回结构化值。"""
    params = {"t50": 30.0, "k": 2.0}
    meta = {"origin": "chat", "turn": 3}
    tags = ["偏好", "咖啡"]
    mid = store.add(
        {
            "type": "short_term",
            "content": "结构字段往返",
            "decay_params": params,
            "metadata": meta,
            "tags": tags,
        }
    )
    row = store.get(mid)
    assert row["decay_params"] == params
    assert row["metadata"] == meta
    assert row["tags"] == tags


def test_update_dict_text_fields_roundtrip(store):
    """update 同样支持 dict/list 直写，读取回结构化值。"""
    mid = store.add({"type": "short_term", "content": "更新前"})
    rows = store.update(mid, {"tags": ["x"], "decay_params": {"t50": 10}})
    assert rows == 1
    row = store.get(mid)
    assert row["tags"] == ["x"]
    assert row["decay_params"] == {"t50": 10}


def test_str_text_fields_stay_string_with_nonjson_tolerated(store):
    """str 原样存原样读；非 JSON 字符串读侧容错回落原值，不抛异常。"""
    mid = store.add(
        {
            "type": "short_term",
            "content": "字符串形态",
            "tags": "游戏",
            "metadata": "not-json{",
            "decay_params": None,
        }
    )
    row = store.get(mid)
    assert row["tags"] == "游戏"
    assert row["metadata"] == "not-json{"
    assert row["decay_params"] is None


def test_list_and_get_parse_consistently(store):
    """list 查询与 get 走同一行映射，结构化字段解析一致。"""
    store.add({"type": "long_term", "content": "a", "tags": ["t1"]})
    rows = store.list(type="long_term")
    assert [r["tags"] for r in rows] == [["t1"]]


# ---------------------------------------------------------------- 检索组合索引（中-1a，第四轮体检批次B）
def test_agent_index_created_with_table(store):
    """建表时同步创建 agent_id+is_deleted 组合索引（检索链免全表扫描）。"""
    conn = store._connect()
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_memories_agent'"
    ).fetchone()
    assert row is not None


def test_agent_index_rebuilt_on_legacy_db(store):
    """既有库升级路径：无索引旧库经 _ensure_table 幂等补建索引。"""
    conn = store._connect()
    conn.execute("DROP INDEX idx_memories_agent")
    conn.commit()
    store._index_ready = False  # 模拟升级前实例状态
    store.list()  # 任一读写入口都会触发 _ensure_table
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_memories_agent'"
    ).fetchone()
    assert row is not None


# ---------------------------------------------------------------- 有界最近查询（中-1c）
def test_list_recent_desc_bounded(store):
    """list_recent 按 id 降序返回最近 limit 条（供写入口相似判定有界扫描）。"""
    for i in range(6):
        store.add({"type": "long_term", "content": f"m{i}"})
    rows = store.list_recent(limit=4)
    assert [r["content"] for r in rows] == ["m5", "m4", "m3", "m2"]


def test_list_recent_agent_filter_excludes_deleted(store):
    """list_recent 与 list 过滤语义一致：agent 过滤 + 默认排除软删除。"""
    a1 = store.add({"type": "long_term", "content": "a1", "agent_id": "x"})
    store.add({"type": "long_term", "content": "a2", "agent_id": "x"})
    store.add({"type": "long_term", "content": "b1", "agent_id": "y"})
    store.soft_delete(a1)
    rows = store.list_recent(agent_id="x", limit=10)
    assert [r["content"] for r in rows] == ["a2"]


# ---------------------------------------------------------------- update 未知键（低-10）
def test_update_all_unknown_keys_returns_zero_without_touch(store, caplog):
    """全为未知键：返回 0 且不再空刷新 updated_at（消除 rowcount 误导）。"""
    import logging as _logging

    mid = store.add({"type": "short_term", "content": "a"})
    before = store.get(mid)["updated_at"]
    with caplog.at_level(_logging.WARNING, logger="lite.memory.storage"):
        assert store.update(mid, {"hacker_field": "x"}) == 0
    assert store.get(mid)["updated_at"] == before
    assert "hacker_field" in caplog.text


# ---------------------------------------------------------------- diary 类型（20261005 记忆页对齐 CX-O）
def test_add_diary_type_accepted(store):
    """memory_type 四值域：diary 类型可写入并可按类型过滤（spec Task 2.4）。"""
    mid = store.add({"type": "diary", "content": "今天的日记"})
    row = store.get(mid)
    assert row["type"] == "diary"
    rows = store.list(type="diary")
    assert [r["id"] for r in rows] == [mid]
    # 其他类型不受影响
    assert all(r["type"] != "diary" for r in store.list(type="long_term"))


def test_diary_type_rejected_value_invalid():
    """diary 之外的非法值依旧被 _validate_type 拒绝。"""
    s = MemoryStore(db_path=":memory:")
    try:
        with pytest.raises(ValueError):
            s.add({"type": "not_a_type", "content": "x"})
    finally:
        s.close()


# ---------------------------------------------------------------- 衰减统计与同步（20261005 spec Task 2.1）
def _frozen_calculator(at):
    """构造冻结基准时间的 DecayCalculator（测试注入用）。"""
    from datetime import datetime

    from lite.memory.decay import DecayCalculator

    calc = DecayCalculator()
    calc.set_current_time(at)
    return at, calc


def test_decay_stats_permanent_exempt(store):
    """衰减统计：permanent 记忆豁免衰减（分数恒 1.0 桶），普通记忆按时间衰减。"""
    from datetime import datetime, timedelta

    now, calc = _frozen_calculator(datetime(2026, 12, 4, 12, 0, 0))
    old = (now - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S.%f")
    store.add({"type": "short_term", "content": "新鲜记忆", "importance": 3})
    store.add({"type": "short_term", "content": "古老普通", "importance": 1, "created_at": old})
    store.add({"type": "permanent", "content": "古老永久", "importance": 1, "permanent": True, "created_at": old})
    stats = store.decay_stats(now=now, calculator=calc)
    assert stats["total_memories"] == 3
    assert stats["permanent_count"] == 1
    assert stats["non_permanent_count"] == 2
    assert stats["decay_distribution"]["healthy"] == 1  # 新鲜记忆
    assert stats["decay_distribution"]["faded"] == 1  # 90 天低分记忆衰减到归档线以下
    # permanent 不进时间衰减均值（豁免口径对齐 CX-O get_decay_statistics）
    assert stats["avg_time_score"] > 0.0
    assert "fading" in stats["decay_distribution"] and "thresholds" in stats


def test_sync_decay_soft_deletes_low_score_and_spares_permanent(store):
    """衰减同步：低分软删归档、permanent 豁免、新鲜记忆不受影响（spec Task 2.1）。"""
    from datetime import datetime, timedelta

    now, calc = _frozen_calculator(datetime(2026, 12, 4, 12, 0, 0))
    old = (now - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S.%f")
    fresh_id = store.add({"type": "short_term", "content": "新鲜", "importance": 3})
    old_id = store.add({"type": "short_term", "content": "古老低分", "importance": 1, "created_at": old})
    perm_id = store.add(
        {"type": "permanent", "content": "古老永久", "importance": 1, "permanent": True, "created_at": old}
    )
    result = store.sync_decay(now=now, calculator=calc)
    assert result["scanned"] == 3
    assert result["deleted_count"] == 1 and result["deleted_ids"] == [old_id]
    assert result["skipped_permanent"] == 1
    # 软删落库校验
    assert store.get(fresh_id)["is_deleted"] == 0
    assert store.get(old_id)["is_deleted"] == 1
    assert store.get(perm_id)["is_deleted"] == 0


def test_sync_decay_custom_threshold_and_deleter(store):
    """sync_decay 支持自定义阈值与软删回调（api 层传 manager.soft_delete 清向量）。"""
    from datetime import datetime, timedelta

    now, calc = _frozen_calculator(datetime(2026, 12, 4, 12, 0, 0))
    old = (now - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S.%f")
    mid = store.add({"type": "short_term", "content": "古老", "importance": 2, "created_at": old})
    deleted_via = []

    def fake_deleter(memory_id):
        deleted_via.append(memory_id)
        return True

    result = store.sync_decay(archive_threshold=0.9, now=now, calculator=calc, deleter=fake_deleter)
    # 阈值放宽到 0.9：古老记忆衰减分必然低于阈值，经回调删除
    assert mid in result["deleted_ids"]
    assert deleted_via == [mid]
    # 阈值收紧到 0：什么都不删
    result2 = store.sync_decay(archive_threshold=0.0, now=now, calculator=calc)
    assert result2["deleted_count"] == 0


def test_diary_not_promoted_by_manager(tmp_path):
    """diary 类型豁免分层升降级：高重要性日记不被自动改写类型（manager 联动）。"""
    from lite.memory.manager import MemoryManager

    mgr = MemoryManager(db_path=str(tmp_path / "m.db"))
    try:
        mid = mgr.store.add({"type": "diary", "content": "高重要性日记", "importance": 5, "importance_score": 1.0})
        mgr.update_reactivation(mid)  # 触发 _maybe_promote
        assert mgr.store.get(mid)["type"] == "diary"
        # demote 同样豁免
        mgr.demote(mid)
        assert mgr.store.get(mid)["type"] == "diary"
    finally:
        mgr.store.close()


def test_update_partial_unknown_keys_warn_and_apply(store, caplog):
    """部分未知键：合法键照常生效，被忽略键名写入告警日志。"""
    import logging as _logging

    mid = store.add({"type": "short_term", "content": "a"})
    with caplog.at_level(_logging.WARNING, logger="lite.memory.storage"):
        rows = store.update(mid, {"content": "b", "bogus_key": 1})
    assert rows == 1
    assert store.get(mid)["content"] == "b"
    assert "bogus_key" in caplog.text