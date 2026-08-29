# -*- coding: utf-8 -*-
"""记忆三维打分——重要性 / 时间 / 相关性加权评分（移植自 CX-O MemoryRouter）。

权重规则：
- 默认权重 importance=0.35 / time=0.25 / relevance=0.4（对齐 CX-O RoutingConfig）。
- 场景感知：_get_weights 按 scene_context 返回对应场景权重（对齐 CX-O SCENE_CONFIGS，
  并对齐 CX-A 业务新增「素描 / 工作」等场景，未命中回退默认权重）。

打分公式（对齐 CX-O _score_memories）：
    final_score = importance·w_i + time·w_t + relevance·w_r   （上限 1.0）

其中 importance 取自 importance_score（缺省按 importance 等级归一）；
time 基于距 now 的时间并按衰减矫正（复用 DecayCalculator）；
relevance 由外部（向量检索相似度）给出并通过候选 dict 的 ``score`` 字段传入，本模块不内嵌
相关性计算（任务约定：relevance 由外部传入）。
"""

import logging

from .decay import DecayCalculator, age_seconds_from_created

# 原生日志记录器（低-8：脏数据告警留痕）
LOGGER = logging.getLogger(__name__)

# 默认三维权重（对齐 CX-O RoutingConfig）
DEFAULT_WEIGHTS = {"importance": 0.35, "time": 0.25, "relevance": 0.4}

# 场景 -> 权重映射（对齐 CX-O SCENE_CONFIGS；含 CX-A 业务新增的「素描/工作」别名）
# 各场景关键值均继承自 CX-O：task/problem_solving 偏相关性，chat 偏重要性，
# creative（对应「素描/创造」）偏时间，recall 偏相关性。
SCENE_CONFIGS = {
    # 闲聊 / 情感对话（CX-O chat）
    "对话": {"importance": 0.45, "time": 0.20, "relevance": 0.35},
    "chat": {"importance": 0.45, "time": 0.20, "relevance": 0.35},
    # 素描 / 创造性（CX-O creative：creative 系列）
    "素描": {"importance": 0.30, "time": 0.40, "relevance": 0.30},
    "sketch": {"importance": 0.30, "time": 0.40, "relevance": 0.30},
    "创造": {"importance": 0.30, "time": 0.40, "relevance": 0.30},
    "creative": {"importance": 0.30, "time": 0.40, "relevance": 0.30},
    # 工作 / 任务 / 问题解决（CX-O problem_solving/task）
    "工作": {"importance": 0.25, "time": 0.20, "relevance": 0.55},
    "work": {"importance": 0.25, "time": 0.20, "relevance": 0.55},
    "任务": {"importance": 0.30, "time": 0.20, "relevance": 0.50},
    "task": {"importance": 0.30, "time": 0.20, "relevance": 0.50},
    "problem_solving": {"importance": 0.25, "time": 0.20, "relevance": 0.55},
    # 学习 / 知识获取（CX-O learning）
    "学习": {"importance": 0.35, "time": 0.20, "relevance": 0.45},
    "learning": {"importance": 0.35, "time": 0.20, "relevance": 0.45},
    # 记忆召回（CX-O recall）
    "召回": {"importance": 0.25, "time": 0.25, "relevance": 0.50},
    "recall": {"importance": 0.25, "time": 0.25, "relevance": 0.50},
    # 首次交互（CX-O first_interaction）
    "首次交互": {"importance": 0.30, "time": 0.30, "relevance": 0.40},
    "first_interaction": {"importance": 0.30, "time": 0.30, "relevance": 0.40},
}


def _get_weights(scene_context=None) -> dict:
    """场景权重调整：按 scene_context 返回三维权重 dict。

    兼容 scene_context 为「场景名」或「已含权重键的 dict」。未命中回退默认权重。

    Returns:
        dict: 含 importance / time / relevance 三个键的权重（三者和约等于 1）。
    """
    if isinstance(scene_context, dict):
        w = {
            "importance": float(scene_context.get("importance", DEFAULT_WEIGHTS["importance"])),
            "time": float(scene_context.get("time", DEFAULT_WEIGHTS["time"])),
            "relevance": float(scene_context.get("relevance", DEFAULT_WEIGHTS["relevance"])),
        }
        return w
    key = scene_context
    if key:
        conf = SCENE_CONFIGS.get(key)
        if conf:
            return {"importance": conf["importance"], "time": conf["time"], "relevance": conf["relevance"]}
    return dict(DEFAULT_WEIGHTS)


def _importance_of(candidate) -> float:
    """取候选的重要性分数（0~1）。importance_score 优先，其次按 importance 等级归一（3/5）。"""
    score = candidate.get("importance_score")
    if score is not None:
        return max(0.0, min(1.0, float(score)))
    importance = candidate.get("importance")
    if importance is not None:
        value = int(importance) / 5.0 if isinstance(importance, int) or (isinstance(importance, float) and importance.is_integer()) else float(importance)
        return max(0.0, min(1.0, float(value)))
    return 0.6


def score_memories(
    candidates,
    query=None,
    importance_weight=None,
    time_weight=None,
    relevance_weight=None,
    scene_context=None,
    _decay_calculator=None,
):
    """对候选记忆做三维加权打分，返回附加 final_score / component_scores 并按分数降序的列表。

    Args:
        candidates: 候选记忆 dict 列表。每项需含用于打分的关键字段：
            - importance_score / importance（重要性）
            - created_at / decay_type / decay_params / reactivation_count / permanent（时间衰减必需）
            - score（相关性，由外部向量检索提供；缺省 0.5）
        query: 查询字符串（本模块不内嵌相关性计算，仅传递保留）。
        importance_weight / time_weight / relevance_weight: 显式传入权重（优先级最高）。
        scene_context: 场景上下文（场景名或 dict），用于 _get_weights 提取权重。
        _decay_calculator: 测试注入用衰减计算器；缺省内部懒建。

    Returns:
        list[dict]: 候选（原地叠加 final_score 与 component_scores），按 final_score 降序。
    """
    if importance_weight is not None or time_weight is not None or relevance_weight is not None:
        w = _get_weights(scene_context)
        weights = {
            "importance": w["importance"] if importance_weight is None else float(importance_weight),
            "time": w["time"] if time_weight is None else float(time_weight),
            "relevance": w["relevance"] if relevance_weight is None else float(relevance_weight),
        }
    else:
        weights = _get_weights(scene_context)

    decay = _decay_calculator if _decay_calculator is not None else DecayCalculator()

    for cand in candidates:
        try:
            importance = _importance_of(cand)
            time_score = decay.score(
                importance=importance,
                age_seconds=_age_seconds(cand, decay),
                decay_type=cand.get("decay_type", "ebbinghaus_opt"),
                params=cand.get("decay_params"),
                reactivation_count=cand.get("reactivation_count", 0),
                emotion_score=cand.get("emotion_score", 0.0),
                permanent=cand.get("permanent", False),
            )
            raw_relevance = cand.get("score", 0.5)
            relevance = 0.5 if raw_relevance is None else float(raw_relevance)
        except (TypeError, ValueError) as exc:
            # 低-8：单条脏数据按 0 分跳过（保留在列表末位），不再使整链 500
            LOGGER.warning("候选记忆 id=%r 打分失败，按 0 分跳过：%s", cand.get("id"), exc)
            cand["time_score_on_retrieve"] = 0.0
            cand["component_scores"] = {"importance": 0.0, "time": 0.0, "relevance": 0.0}
            cand["final_score"] = 0.0
            continue
        final = (
            importance * weights["importance"]
            + time_score * weights["time"]
            + relevance * weights["relevance"]
        )
        cand["time_score_on_retrieve"] = time_score
        cand["component_scores"] = {
            "importance": importance,
            "time": time_score,
            "relevance": relevance,
        }
        cand["final_score"] = min(final, 1.0)

    candidates.sort(key=lambda m: m.get("final_score", 0), reverse=True)
    return candidates


def _age_seconds(cand, decay):
    """计算候选记忆距 now 的秒数（利用 decay 的解析与当前基准）。"""
    created = cand.get("created_at")
    if created is None:
        return 0.0
    now = decay._now()
    return age_seconds_from_created(created, now)