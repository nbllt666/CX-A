"""梦境模式的自动决策：记忆梦 / 天马行空 / 一夜无梦。

决策只依赖入睡时的素材数量、情绪强度与上一场模式，不读取任何梦外上下文。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from .errors import InvalidMode, NoMemoryMaterial
from .models import (
    MODE_DREAMLESS,
    MODE_MEMORY,
    MODE_WILD,
    MODES,
    Emotion,
    mode_label,
)

# 情绪强度阈值：达到后显著提升天马行空权重
_HIGH_EMOTION_THRESHOLD = 0.7


@dataclass(frozen=True)
class ModeWeights:
    """自动模式的基础权重（使用前会归一化）。"""

    memory: float = 0.5
    wild: float = 0.3
    dreamless: float = 0.2

    def sanitized(self) -> "ModeWeights":
        """负值与非法值归零；全零时回退默认权重。"""
        values = {
            "memory": _safe_float(self.memory),
            "wild": _safe_float(self.wild),
            "dreamless": _safe_float(self.dreamless),
        }
        if sum(values.values()) <= 0:
            return ModeWeights()
        return ModeWeights(**values)


def _safe_float(value: object) -> float:
    """把权重值规整为非负浮点，非法值归零。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0


def select_mode(
    memories: Sequence[str],
    emotion: Emotion,
    weights: ModeWeights,
    rng,
    forced: Optional[str] = None,
    last_mode: Optional[str] = None,
) -> Tuple[str, str]:
    """选择今夜的模式，返回 ``(mode, mode_reason)``。

    - ``forced`` 非空时直接采用（记忆梦仍要求至少一条当天记忆）；
    - 自动模式下：无当天记忆时记忆梦权重归零；记忆 >= 3 条降低无梦权重；
      高情绪提升天马行空；上一场无梦则降低无梦权重；
    - 归一化后按注入的随机源落入区间。
    """
    memory_items = [str(m).strip() for m in memories if str(m).strip()]

    if forced is not None:
        if forced not in MODES:
            raise InvalidMode(f"未知梦境模式: {forced}")
        if forced == MODE_MEMORY and not memory_items:
            raise NoMemoryMaterial("记忆梦至少需要一条当天记忆")
        return forced, f"手动指定：{mode_label(forced)}"

    base = weights.sanitized()
    factors = {
        MODE_MEMORY: base.memory,
        MODE_WILD: base.wild,
        MODE_DREAMLESS: base.dreamless,
    }
    notes: List[str] = []
    intensity = emotion.intensity()

    if not memory_items:
        # 没有记忆碎片就不可能织出记忆梦：硬性归零
        factors[MODE_MEMORY] = 0.0
        factors[MODE_DREAMLESS] *= 2.0
        factors[MODE_WILD] *= 0.5
        notes.append("今夜没有记忆碎片")
    elif len(memory_items) >= 3:
        factors[MODE_DREAMLESS] *= 0.5
        factors[MODE_WILD] *= 1.2
        notes.append(f"今日有 {len(memory_items)} 条记忆在暗处发亮")

    if intensity >= _HIGH_EMOTION_THRESHOLD:
        factors[MODE_WILD] *= 1.8
        notes.append("情绪翻涌")

    if last_mode == MODE_DREAMLESS:
        factors[MODE_DREAMLESS] *= 0.3
        notes.append("昨夜刚经历过无梦")

    total = sum(factors.values())
    if total <= 0:
        return MODE_DREAMLESS, "，".join(notes + ["意识沉入安静的深水，今夜无梦"])

    roll = _clamp_roll(rng.random())
    cursor = 0.0
    chosen = MODE_DREAMLESS
    for mode in MODES:
        weight = factors[mode] / total
        if weight <= 0:
            continue
        cursor += weight
        if roll < cursor:
            chosen = mode
            break

    reasons = {
        MODE_MEMORY: "记忆的碎片被悄悄织进梦里",
        MODE_WILD: "潜意识失重，今夜天马行空",
        MODE_DREAMLESS: "意识沉入安静的深水，今夜无梦",
    }
    return chosen, "，".join(notes + [reasons[chosen]])


def _clamp_roll(value: object) -> float:
    """把随机值夹到 [0, 1)，避免 1.0 落空后错误命中最后一项。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(max(number, 0.0), 0.999999999)
