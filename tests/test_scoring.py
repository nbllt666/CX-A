# -*- coding: utf-8 -*-
"""三维打分与场景权重测试：权重生效、importance 高者分高、场景权重切换。"""

import pytest

from lite.memory.scoring import DEFAULT_WEIGHTS, SCENE_CONFIGS, _get_weights, score_memories


def _cand(importance=0.5, relevance=0.5, cid="x"):
    """构造一个不含 created_at 的候选（age=0，time 维度取 importance，便于确定性断言）。"""
    return {"id": cid, "content": f"记忆{cid}", "importance_score": importance, "score": relevance}


# ---------------------------------------------------------------- 默认/三维权重
def test_default_weights_aligned_with_cxo():
    assert DEFAULT_WEIGHTS == {"importance": 0.35, "time": 0.25, "relevance": 0.4}


def test_get_weights_default():
    w = _get_weights(None)
    assert w["importance"] + w["time"] + w["relevance"] == pytest.approx(1.0)
    assert w == DEFAULT_WEIGHTS


# ---------------------------------------------------------------- importance 高者分高
def test_higher_importance_scores_higher():
    cands = [
        _cand(importance=0.9, relevance=1.0, cid="hi"),
        _cand(importance=0.3, relevance=1.0, cid="lo"),
    ]
    result = score_memories(cands, query="q")
    assert result[0]["id"] == "hi"
    assert result[0]["final_score"] > result[1]["final_score"]


def test_final_score_bounded():
    cands = [_cand(importance=1.0, relevance=1.0)]
    result = score_memories(cands, query="q")
    assert result[0]["final_score"] <= 1.0


def test_component_scores_recorded():
    cands = [_cand(importance=0.8, relevance=0.7)]
    result = score_memories(cands, query="q")
    comp = result[0]["component_scores"]
    assert set(comp.keys()) == {"importance", "time", "relevance"}
    assert comp["importance"] == pytest.approx(0.8)


# ---------------------------------------------------------------- 场景权重切换
def test_scene_specific_weights_exist():
    for scene in ("对话", "素描", "工作", "学习", "召回"):
        w = _get_weights(scene)
        assert set(w.keys()) == {"importance", "time", "relevance"}


def test_recall_scene_boosts_relevance():
    """召回场景下相关性主导：低重要但高相关的候选在召回场景得分更高，且高于对话场景。"""
    cand = _cand(importance=0.2, relevance=1.0, cid="rele")
    recall = score_memories([dict(cand)], query="q", scene_context="召回")[0]["final_score"]
    chat = score_memories([dict(cand)], query="q", scene_context="对话")[0]["final_score"]
    assert recall > chat


def test_get_weights_switches_between_scenes():
    w_chat = _get_weights("对话")
    w_sketch = _get_weights("素描")
    assert w_chat != w_sketch


def test_scene_config_keys_mirror_cxo():
    """场景配置应覆盖 CX-O 的七个核心语义场景与 CX-A 业务的素描/工作。"""
    for scene in SCENE_CONFIGS:
        conf = SCENE_CONFIGS[scene]
        assert set(conf.keys()) == {"importance", "time", "relevance"}
    # 校验 CX-O 派生关键值
    assert SCENE_CONFIGS["chat"]["relevance"] == 0.35
    assert SCENE_CONFIGS["creative"]["time"] == 0.40
    assert SCENE_CONFIGS["recall"]["relevance"] == 0.50


def test_dict_scene_context():
    cands = [_cand(importance=0.5, relevance=0.5)]
    r1 = score_memories([dict(cands[0])], query="q", scene_context={"importance": 1.0, "time": 0.0, "relevance": 0.0})
    assert r1[0]["final_score"] == pytest.approx(0.5)


def test_explicit_weights_override():
    cands = [_cand(importance=1.0, relevance=0.0, cid="a"), _cand(importance=0.0, relevance=1.0, cid="b")]
    # importance 权重为 1：a 胜
    ra = score_memories([dict(cands[0]), dict(cands[1])], query="q", importance_weight=1.0, time_weight=0.0, relevance_weight=0.0)
    assert ra[0]["id"] == "a"


# ---------------------------------------------------------------- 脏数据容错（低-8，第四轮体检批次B）
def test_dirty_importance_score_skipped_as_zero():
    """单条脏 importance_score：按 0 分保留在末位，不再放大为整链异常。"""
    cands = [
        {"id": "bad", "content": "脏数据", "importance_score": "oops", "score": 0.8},
        _cand(importance=0.7, relevance=0.8, cid="good"),
    ]
    result = score_memories(cands, query="q")
    by_id = {c["id"]: c for c in result}
    assert by_id["bad"]["final_score"] == 0.0
    assert by_id["good"]["final_score"] > 0.0
    # 脏数据排在末位
    assert result[-1]["id"] == "bad"


def test_dirty_relevance_score_skipped_as_zero():
    """脏 score（相关性非数值）同样按 0 分跳过，不抛 TypeError。"""
    cands = [{"id": "bad2", "content": "x", "importance_score": 0.5, "score": "not-a-number"}]
    result = score_memories(cands, query="q")
    assert result[0]["final_score"] == 0.0


def test_dirty_decay_params_tolerated_in_scoring():
    """脏 decay_params（参数值为非数值）经 decay 容错回退默认，打分正常完成。"""
    cand = {
        "id": "d",
        "content": "x",
        "importance_score": 0.5,
        "score": 0.5,
        "created_at": "2026-08-01 00:00:00.000000",
        "decay_params": {"t50": "abc", "k": None},
    }
    result = score_memories([cand], query="q")
    assert 0.0 <= result[0]["final_score"] <= 1.0