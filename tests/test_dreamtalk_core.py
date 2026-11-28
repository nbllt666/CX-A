# -*- coding: utf-8 -*-
"""DreamTalk 领域层测试：纯净性、模式决策、内容闸门与提示词契约。

本文件只验证 ``lite/dreamtalk/core`` 与 ``ports`` 的纯领域行为，不触网、
不建库、不读配置。
"""

import inspect
import os
import re

import pytest

from lite.dreamtalk.core.dialogue import DialogueConfig, DreamDialogue
from lite.dreamtalk.core.errors import (
    InvalidMode,
    NoMemoryMaterial,
    SessionEnded,
    SessionNotFound,
    StoreError,
)
from lite.dreamtalk.core.filter import DREAM_BROKEN_TEXT, check_text, is_clean
from lite.dreamtalk.core.models import (
    CONTRACT_VERSION,
    MODE_DREAMLESS,
    MODE_MEMORY,
    MODE_WILD,
    MODES,
    Emotion,
    MemoryMaterial,
)
from lite.dreamtalk.core.modes import ModeWeights, select_mode
from lite.dreamtalk.core.personas import material_block, system_prompt

_CORE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "lite", "dreamtalk", "core",
)

_FORBIDDEN_IMPORTS = ("fastapi", "uvicorn", "pydantic", "sqlite3", "httpx", "dotenv", "requests")


class _FixedRng:
    """固定随机源：让模式判定的落点可预期。"""

    def __init__(self, value):
        self._value = float(value)

    def random(self):
        return self._value


# ------------------------------------------------------------------ 纯净性
def test_core_has_no_infrastructure_imports():
    """核心层不得引入任何基础设施依赖（HTTP / 数据库 / 环境变量）。"""
    for name in os.listdir(_CORE_DIR):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(_CORE_DIR, name), encoding="utf-8") as fh:
            source = fh.read()
        for forbidden in _FORBIDDEN_IMPORTS:
            assert not re.search(rf"^\s*(import|from)\s+{forbidden}\b", source, re.M), (
                f"{name} 引入了基础设施依赖 {forbidden}"
            )
        assert "os.environ" not in source, f"{name} 读取了环境变量"
        assert "load_config" not in source, f"{name} 读取了配置"


def test_build_messages_accepts_only_dream_context():
    """``build_messages`` 只接受 (context, role)，没有任何 history 形参。"""
    params = list(inspect.signature(DreamDialogue.build_messages).parameters)
    assert params == ["self", "context", "role"]


def test_contract_version_matches_cx_o():
    assert CONTRACT_VERSION == "1.0"
    assert MODES == (MODE_MEMORY, MODE_WILD, MODE_DREAMLESS)


# ------------------------------------------------------------------ 模式决策
def test_auto_mode_never_picks_memory_without_material():
    """无记忆碎片时记忆梦硬性归零，绝不被自动选中。"""
    for roll in (0.0, 0.3, 0.6, 0.99):
        mode, reason = select_mode(
            [], Emotion(), ModeWeights(), _FixedRng(roll), forced=None
        )
        assert mode != MODE_MEMORY
        assert "今夜没有记忆碎片" in reason


def test_no_material_biases_dreamless_up_and_wild_down():
    """无素材：dreamless ×2、wild ×0.5 → 高位随机落点必然是 dreamless。"""
    mode, _ = select_mode([], Emotion(), ModeWeights(), _FixedRng(0.99))
    assert mode == MODE_DREAMLESS
    # wild 权重被压低：低位落点（0.0）仍落在 wild 区间内但区间明显变窄
    assert select_mode([], Emotion(), ModeWeights(), _FixedRng(0.0))[0] == MODE_WILD


def test_many_memories_lower_dreamless_weight():
    """记忆 ≥3 条：dreamless ×0.5、wild ×1.2 → 同一落点从 dreamless 变为 wild。"""
    material = ["a", "b", "c"]
    mode, reason = select_mode(material, Emotion(), ModeWeights(), _FixedRng(0.85))
    assert mode == MODE_WILD
    assert "3 条记忆" in reason
    # 只有 1 条记忆时同一落点落在 dreamless（dreamless 权重未被压低）
    assert select_mode(["a"], Emotion(), ModeWeights(), _FixedRng(0.85))[0] == MODE_DREAMLESS


def test_high_emotion_boosts_wild():
    """情绪强度 ≥0.7 时 wild ×1.8：同一落点从 dreamless 变为 wild。"""
    strong = Emotion(valence=0.9, arousal=0.5)
    mode, reason = select_mode(["a"], strong, ModeWeights(), _FixedRng(0.82))
    assert mode == MODE_WILD
    assert "情绪翻涌" in reason
    # 对照：低情绪同落点为 dreamless
    calm = Emotion(valence=0.0, arousal=0.0)
    assert select_mode(["a"], calm, ModeWeights(), _FixedRng(0.82))[0] == MODE_DREAMLESS


def test_last_dreamless_lowers_dreamless_weight():
    """上一场无梦：dreamless ×0.3 → 同一落点从 dreamless 变为 wild。"""
    mode, reason = select_mode(
        ["a", "b", "c"], Emotion(), ModeWeights(), _FixedRng(0.93), last_mode=MODE_DREAMLESS
    )
    assert mode == MODE_WILD
    assert "昨夜刚经历过无梦" in reason


def test_forced_mode_short_circuits_and_validates():
    assert select_mode(["a"], Emotion(), ModeWeights(), _FixedRng(0.99), forced=MODE_WILD)[0] == MODE_WILD
    with pytest.raises(NoMemoryMaterial):
        select_mode([], Emotion(), ModeWeights(), _FixedRng(0.99), forced=MODE_MEMORY)
    with pytest.raises(InvalidMode):
        select_mode(["a"], Emotion(), ModeWeights(), _FixedRng(0.99), forced="lucid")


def test_mode_weights_sanitized():
    """负权重归零；全零回退默认权重。"""
    weights = ModeWeights(memory=-1.0, wild=0.3, dreamless=0.2).sanitized()
    assert weights.memory == 0.0
    assert weights.wild == 0.3
    fallback = ModeWeights(memory=0.0, wild=0.0, dreamless=-2.0).sanitized()
    assert (fallback.memory, fallback.wild, fallback.dreamless) == (0.5, 0.3, 0.2)


# ------------------------------------------------------------------ 内容闸门
@pytest.mark.parametrize(
    "text",
    [
        "你昨天答应过我一件事",
        "你刚才在走廊里回头",
        "你说过要带我去海边",
        "你曾经住在这栋楼里",
        "你做了那件很重的事",
        "我记得你把钥匙放在窗台",
    ],
)
def test_filter_hits_six_factual_patterns(text):
    assert check_text(text)
    assert not is_clean(text)


def test_filter_passes_dream_imagery():
    assert is_clean("雾把走廊的轮廓擦掉了，我听见远处有纸页翻动")
    assert check_text("") == []


# ------------------------------------------------------------------ 提示词
def test_personas_keep_dream_contract():
    director = system_prompt("director", MODE_MEMORY)
    dreamer = system_prompt("dreamer", MODE_MEMORY)
    for prompt in (director, dreamer):
        assert "不断言用户的真实经历" in prompt
        assert "不给选项" in prompt
        assert "不解释、不总结" in prompt
    assert "禁止替梦者行动" in director
    assert "禁止代替其他角色说话" in dreamer
    wild = system_prompt("dreamer", MODE_WILD)
    assert "时空跳跃" in wild and "梦中梦" in wild


def test_material_block_marks_potential_imagery_only():
    material = MemoryMaterial(
        day_memories=["楼下的猫"], entities=["猫"], emotion=Emotion(0.2, 0.4)
    )
    block = material_block(material)
    assert "只作潜在意象" in block
    assert "禁止复述或断言真实发生" in block
    assert "- 楼下的猫" in block
    assert "猫" in block


def test_dialogue_config_defaults_match_contract():
    config = DialogueConfig()
    assert (config.max_rounds, config.context_turns) == (8, 16)
    assert (config.summary_title_max_chars, config.summary_max_chars) == (12, 120)


def test_dream_broken_text_constant():
    assert DREAM_BROKEN_TEXT == "（梦在这里断了一瞬）"


def test_domain_error_status_codes():
    assert (SessionNotFound.status_code, SessionEnded.status_code) == (404, 409)
    assert (NoMemoryMaterial.status_code, InvalidMode.status_code) == (422, 422)
    assert StoreError.status_code == 500
