# -*- coding: utf-8 -*-
"""CXAMemorySource 采集口径测试（素材只从 CX-A 记忆系统取）。

覆盖：时间窗 / 边缘阈值 / 排序 / 上限 / 补齐 / R2 红线 / 排除梦境回写 /
tags 两种形态 / 日记情绪 / 异常降级 / 零散意象上限。
"""

import asyncio
from datetime import datetime, timedelta

import pytest

from lite.dreamtalk.adapters.memory import CXAMemorySource
from lite.memory.storage import MemoryStore


def _ts(days_ago=0, micro=0):
    """构造 CX-A 存储格式的时间戳（``%Y-%m-%d %H:%M:%S.%f``）。"""
    moment = datetime.now() - timedelta(days=days_ago)
    return moment.strftime("%Y-%m-%d %H:%M:%S.") + f"{micro:06d}"


@pytest.fixture()
def store(tmp_path):
    memory_store = MemoryStore(db_path=str(tmp_path / "memories.db"))
    memory_store.create_table()
    yield memory_store
    memory_store.close()


def _add(store, content, **fields):
    payload = {"type": "long_term", "content": content, "importance": 3}
    payload.update(fields)
    return store.add(payload)


def _collect(store, **kwargs):
    source = CXAMemorySource(store, **kwargs)
    return asyncio.run(source.collect("default"))


# ------------------------------------------------------------------ 窗口与阈值
def test_window_and_edge_threshold(store):
    """边缘集只取窗口内且基线重要性 <0.5 的记忆（不触发补齐）。"""
    _add(store, "边缘一", importance_score=0.1, created_at=_ts(1))
    _add(store, "边缘二", importance_score=0.2, created_at=_ts(2))
    _add(store, "边缘三", importance_score=0.3, created_at=_ts(3))
    _add(store, "窗口外的边缘", importance_score=0.3, created_at=_ts(30))
    _add(store, "窗口内但重要", importance_score=0.7, created_at=_ts(1))
    material = _collect(store, window_days=7, max_items=20)
    assert material.day_memories == ["边缘一", "边缘二", "边缘三"]
    assert "窗口外的边缘" not in material.day_memories
    assert "窗口内但重要" not in material.day_memories
    assert material.source == "cxa_memory"


def test_unparseable_created_at_kept(store):
    """created_at 缺失/不可解析时视为窗口内（不丢素材）。"""
    _add(store, "时间脏数据", importance_score=0.2, created_at="not-a-date")
    assert _collect(store, window_days=1).day_memories == ["时间脏数据"]


def test_sort_by_baseline_then_created_desc(store):
    """基线重要性升序；同分按 created_at 倒序。"""
    _add(store, "A-较低", importance_score=0.1, created_at=_ts(1))
    _add(store, "B-同分旧", importance_score=0.2, created_at=_ts(3))
    _add(store, "C-同分新", importance_score=0.2, created_at=_ts(1))
    material = _collect(store, window_days=7)
    assert material.day_memories[0] == "A-较低"
    assert material.day_memories[1] == "C-同分新"
    assert material.day_memories[2] == "B-同分旧"


def test_max_items_is_respected(store):
    for index in range(5):
        _add(store, f"碎片{index}", importance_score=0.1 + index * 0.01, created_at=_ts(1))
    material = _collect(store, window_days=7, max_items=2)
    assert len(material.day_memories) == 2


def test_max_items_zero_means_no_material(store):
    _add(store, "碎片", importance_score=0.1, created_at=_ts(1))
    material = _collect(store, window_days=7, max_items=0)
    assert material.is_empty()


def test_fill_to_three_with_non_edge_memories(store):
    """边缘不足 3 条时用同窗口其余合格记忆补齐（含基线 ≥0.5）。"""
    _add(store, "边缘一", importance_score=0.2, created_at=_ts(2))
    _add(store, "补齐旧", importance_score=0.6, created_at=_ts(4))
    _add(store, "补齐新", importance_score=0.7, created_at=_ts(1))
    _add(store, "不会被取", importance_score=0.6, created_at=_ts(3))
    material = _collect(store, window_days=7, max_items=20)
    assert len(material.day_memories) == 3
    assert material.day_memories[0] == "边缘一"
    # 补齐按 created_at 倒序取（1 天 > 3 天 > 4 天），补到 3 条即停
    assert material.day_memories[1:] == ["补齐新", "不会被取"]


# ------------------------------------------------------------------ 红线与回写
def test_permanent_and_high_importance_never_dream(store):
    """R2 红线：permanent / type=permanent / 基线 ≥0.95 一律不入梦，也不参与补齐。"""
    _add(store, "永久记忆", type="permanent", importance_score=1.0, created_at=_ts(1))
    _add(store, "高重要性", importance_score=0.98, created_at=_ts(1))
    _add(store, "permanent 标记", permanent=True, importance_score=0.3, created_at=_ts(1))
    material = _collect(store, window_days=7)
    assert material.is_empty()


@pytest.mark.parametrize(
    "fields",
    [
        {"source": "dreamtalk"},
        {"source": "dream"},
        {"metadata": {"dreamtalk_session_id": "s1"}},
        {"metadata": {"dream_session_id": "s1"}},
        {"metadata": '{"dreamtalk_session_id": "s1"}'},
    ],
)
def test_dream_writeback_never_reflows(store, fields):
    """梦境回写记录既不进边缘集，也不进补齐集（防止摘要回流成素材）。"""
    _add(store, "上夜梦境摘要", importance_score=0.2, created_at=_ts(1), **fields)
    material = _collect(store, window_days=7)
    assert material.is_empty()


# ------------------------------------------------------------------ 意象与情绪
def test_entities_parse_list_and_json_string_with_limit(store):
    """tags 的数组与 JSON 字符串两种形态都能解析；去重且上限 8。"""
    _add(store, "一", importance_score=0.1, created_at=_ts(2),
         tags=["猫", "海边", "", "猫"])
    _add(store, "二", importance_score=0.2, created_at=_ts(1),
         tags='["地铁", "海边", "雨"]')
    _add(store, "三", importance_score=0.3, created_at=_ts(1), tags="单个标签")
    material = _collect(store, window_days=7)
    assert material.entities[:2] == ["猫", "海边"]
    for expected in ("地铁", "雨", "单个标签"):
        assert expected in material.entities
    assert len(material.entities) <= 8
    assert len(set(material.entities)) == len(material.entities)


def test_entities_capped_at_eight(store):
    tags = [f"标签{i}" for i in range(12)]
    _add(store, "很多标签", importance_score=0.1, created_at=_ts(1), tags=tags)
    assert len(_collect(store, window_days=7).entities) == 8


def test_diary_emotion_mapping(store):
    """最近非梦境日记的 emotion_score → valence = clamp(2s-1)；arousal 固定 0.4。"""
    _add(store, "旧日记", type="diary", importance_score=0.5, created_at=_ts(30),
         emotion_score=0.9)
    _add(store, "新日记", type="diary", importance_score=0.5, created_at=_ts(1),
         emotion_score=0.2)
    emotion = _collect(store, window_days=7).emotion
    assert emotion.valence == pytest.approx(-0.6)
    assert emotion.arousal == pytest.approx(0.4)


def test_no_diary_gives_neutral_emotion(store):
    _add(store, "普通记忆", importance_score=0.2, created_at=_ts(1))
    emotion = _collect(store, window_days=7).emotion
    assert (emotion.valence, emotion.arousal) == (0.0, 0.0)


def test_dream_diary_is_ignored_for_emotion(store):
    """梦境回写即使用 diary 类型，也不作为情绪基调来源。"""
    _add(store, "梦境日记", type="diary", importance_score=0.2, created_at=_ts(1),
         emotion_score=1.0, source="dreamtalk")
    assert _collect(store, window_days=7).emotion.valence == pytest.approx(0.0)


# ------------------------------------------------------------------ 降级
def test_store_exception_degrades_to_empty_material():
    class _BrokenStore:
        def list_recent(self, **kwargs):
            raise RuntimeError("db down")

    material = asyncio.run(CXAMemorySource(_BrokenStore()).collect("default"))
    assert material.is_empty()
    assert material.source == "cxa_memory"


def test_collect_is_called_once_per_night(store):
    """素材只在入睡时解析一次：多次 collect 才多次读库。"""

    class _CountingStore:
        def __init__(self, inner):
            self.inner = inner
            self.calls = 0

        def list_recent(self, **kwargs):
            self.calls += 1
            return self.inner.list_recent(**kwargs)

    counting = _CountingStore(store)
    source = CXAMemorySource(counting)
    asyncio.run(source.collect("default"))
    assert counting.calls == 1
