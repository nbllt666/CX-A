# -*- coding: utf-8 -*-
"""DecayCalculator 衰减计算测试：随时间递减、再激活回升、exponential 与 two_stage 差异、permanent。"""

import pytest

from lite.memory.decay import (
    EBBINGHAUS_DEFAULTS,
    TWO_STAGE_DEFAULTS,
    DecayCalculator,
    age_seconds_from_created,
)

DAY = 86400.0


@pytest.fixture()
def calc():
    return DecayCalculator()


# ---------------------------------------------------------------- 随时间递减
def test_score_decays_over_time(calc):
    """同一记忆随时间推移分数递减（未再激活时）。"""
    early = calc.score(importance=0.8, age_seconds=1 * DAY, decay_type="exponential")
    late = calc.score(importance=0.8, age_seconds=180 * DAY, decay_type="exponential")
    assert early > late
    assert 0.0 <= late < early <= 1.0


def test_zero_age_returns_importance(calc):
    """age=0 时衰减因子为 1，分数保留 importance。"""
    s = calc.score(importance=0.7, age_seconds=0, decay_type="two_stage")
    assert s == pytest.approx(0.7)
    s2 = calc.score(importance=0.7, age_seconds=0, decay_type="exponential")
    assert s2 == pytest.approx(0.7)


# ---------------------------------------------------------------- 再激活回升
def test_reactivation_boosts_score(calc):
    """长期未访问的弱记忆在再激活加成后分数回升。"""
    base = calc.score(importance=0.6, age_seconds=200 * DAY, decay_type="two_stage", reactivation_count=0)
    boosted = calc.score(
        importance=0.6, age_seconds=200 * DAY, decay_type="two_stage", reactivation_count=5
    )
    assert boosted > base


def test_reactivation_emotion_bonus(calc):
    """情感强度为再激活提供额外加成。"""
    no_emotion = calc.score(
        importance=0.6, age_seconds=100 * DAY, decay_type="two_stage",
        reactivation_count=2, emotion_score=0.0,
    )
    with_emotion = calc.score(
        importance=0.6, age_seconds=100 * DAY, decay_type="two_stage",
        reactivation_count=2, emotion_score=0.8,
    )
    assert with_emotion > no_emotion


# ---------------------------------------------------------------- exponential vs two_stage
def test_exponential_and_two_stage_differ(calc):
    """exponential（艾宾浩斯）与 two_stage（双阶段指数）在相同输入下行为不同。"""
    # importance 需 <0.95 以避开「高重要性免疫衰减」逻辑（对齐 CX-O）
    # 艾宾浩斯：1/(1+(t/T50)^k)；T50=30,k=2
    e = calc.score(importance=0.9, age_seconds=100 * DAY, decay_type="exponential")
    # 双阶段指数：α·e^(-λ1 t)+(1-α)·e^(-λ2 t)；α=.6,λ1=.25,λ2=.04
    t = calc.score(importance=0.9, age_seconds=100 * DAY, decay_type="two_stage")
    assert e != pytest.approx(t)
    # 在 100 天处艾宾浩斯保持在 0.0826 附近，双阶段指数接近 0
    assert e > t


def test_ebbinghaus_opt_canonical_and_alias_consistency(calc):
    """独立枚举名 ebbinghaus_opt 与存量别名 exponential/ebbinghaus 行为一致（兼容读取）。"""
    base = calc.score(importance=0.9, age_seconds=100 * DAY, decay_type="ebbinghaus_opt")
    alias_exp = calc.score(importance=0.9, age_seconds=100 * DAY, decay_type="exponential")
    alias_eb = calc.score(importance=0.9, age_seconds=100 * DAY, decay_type="ebbinghaus")
    assert base == pytest.approx(alias_exp)
    assert base == pytest.approx(alias_eb)
    # two_stage 保持独立语义（与艾宾浩斯不同）
    two = calc.score(importance=0.9, age_seconds=100 * DAY, decay_type="two_stage")
    assert base != pytest.approx(two)


def test_ebbinghaus_default_t50(calc):
    """t50 天处艾宾浩斯保留约 50%（T50=30 天；importance=0.9）。"""
    s = calc.score(importance=0.9, age_seconds=30 * DAY, decay_type="exponential")
    assert s == pytest.approx(0.45, abs=0.02)


# ---------------------------------------------------------------- permanent / 高重要
def test_permanent_always_one(calc):
    """永久记忆分数恒为 1.0，不随时间衰减。"""
    for age in (0, 10 * DAY, 1000 * DAY):
        assert calc.score(importance=0.5, age_seconds=age, decay_type="two_stage", permanent=True) == pytest.approx(1.0)


def test_high_importance_immune_to_decay(calc):
    """importance>=0.95 的记忆不随时间衰减（对齐 CX-O zero/permanent 语义）。"""
    s = calc.score(importance=0.96, age_seconds=500 * DAY, decay_type="two_stage")
    assert s == pytest.approx(1.0)


# ---------------------------------------------------------------- 阈值注入（M5 接线）
def test_permanent_importance_threshold_injection():
    """DecayCalculator 构造参数可注入免疫衰减阈值；默认行为不变（0.95）。"""
    calc_default = DecayCalculator()
    # 默认阈值不变：0.94 < 0.95，长时间后随时间衰减（不为 1.0）
    s_default = calc_default.score(importance=0.94, age_seconds=500 * DAY, decay_type="two_stage")
    assert s_default < 1.0

    # 注入自定义阈值 0.50 后：0.55 >= 0.50 触发免疫衰减恒为 1.0，
    # 同一输入在默认阈值下则正常衰减——行为随构造参数变化
    calc_custom = DecayCalculator(permanent_importance_threshold=0.50)
    assert calc_custom.permanent_importance_threshold == pytest.approx(0.5)
    s_custom = calc_custom.score(importance=0.55, age_seconds=500 * DAY, decay_type="two_stage")
    s_default_low = calc_default.score(importance=0.55, age_seconds=500 * DAY, decay_type="two_stage")
    assert s_custom == pytest.approx(1.0)
    assert s_default_low < 1.0


# ---------------------------------------------------------------- 参数对齐 CX-O
def test_params_aligned_with_cxo():
    """对齐 CX-O 默认参数：双阶段 α=0.6/λ1=0.25/λ2=0.04；艾宾浩斯 T50=30/k=2。"""
    assert TWO_STAGE_DEFAULTS == {"alpha": 0.6, "lambda1": 0.25, "lambda2": 0.04}
    assert EBBINGHAUS_DEFAULTS == {"t50": 30.0, "k": 2.0}


def test_retention_and_strength(calc):
    """retention / strength 均返回衰减后分数。"""
    r = calc.retention(0.8, 10 * DAY, "two_stage")
    s = calc.strength(0.8, 10 * DAY, "two_stage")
    assert r == pytest.approx(s)
    assert 0.0 <= r <= 0.8


def test_age_seconds_from_created(calc):
    """时间戳解析为年龄秒数；解析失败返回 0。"""
    assert age_seconds_from_created("2026-08-26 00:00:00.000000", "2026-08-26 01:00:00.000000") == pytest.approx(3600.0)
    assert age_seconds_from_created("bad-time", "2026-08-26 00:00:00.000000") == 0.0


# ---------------------------------------------------------------- L8：无再激活分支上界钳制
def test_reactivation_none_branch_clamped_to_upper(calc):
    """无再激活分支 emotion_score 超大时结果仍 <=1.0（与有再激活分支口径一致）。"""
    s = calc.apply_reactivation(0.9, None, emotion_score=100.0)
    assert 0.0 <= s <= 1.0
    assert s == pytest.approx(1.0)

    s2 = calc.apply_reactivation(0.9, 0, emotion_score=50.0)
    assert 0.0 <= s2 <= 1.0

    # 下界行为保持不变：负 base 情感为 0 时钳到 0
    low = calc.apply_reactivation(-0.5, None, emotion_score=0.0)
    assert low == pytest.approx(0.0)