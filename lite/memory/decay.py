# -*- coding: utf-8 -*-
"""记忆衰减计算器——移植自 CX-O 的 DecayCalculator。

提供两种衰减模型：
- 艾宾浩斯优化版（decay_type='ebbinghaus_opt'）：
      T(t) = 1 / (1 + (Δt / T50)^k)
- 双阶段指数（decay_type='two_stage'）：
      T(t) = α·e^(-λ1·Δt) + (1-α)·e^(-λ2·Δt)

⚠️ 命名说明：CX-O 中把「双阶段指数」命名为 decay_type='exponential'，
真正的艾宾浩斯命名为 'ebbinghaus'，存在同名异义（详见交付报告 CX-O 问题 #1）。
为根治该歧义，CX-A schema 默认与存根统一使用**独立枚举名** 'ebbinghaus_opt'：
- 'ebbinghaus_opt' / 'exponential' / 'ebbinghaus' -> 艾宾浩斯优化版（后两者为兼容
  存量数据的别名，仅用于读取，不推荐新写入）；
- 'two_stage' -> 双阶段指数。
默认参数与 CX-O 源码对齐：T50=30 天 / k=2.0；α=0.6 / λ1=0.25 / λ2=0.04。

另提供再激活加成逻辑（对齐 CX-O calculate_reactivation_score）：
    enhanced = base * (1 + 0.2 * count) + 0.1 + 0.05 * |emotion|
    result   = min(enhanced, 1.0)
"""

import math
from datetime import datetime

# 参数默认值（对齐 CX-O decay.py 源码）
EBBINGHAUS_DEFAULTS = {"t50": 30.0, "k": 2.0}
TWO_STAGE_DEFAULTS = {"alpha": 0.6, "lambda1": 0.25, "lambda2": 0.04}

# 再激活加成参数（对齐 CX-O calculate_reactivation_score）
REACTIVATION_GAIN = 0.2  # 每次访问的乘法加成系数
REACTIVATION_BASE = 0.1  # 发生再激活时的固定加分
EMOTION_BOOST = 0.05     # 情感强度加分系数

# 艾宾浩斯 canonical 名与兼容别名（exponential/ebbinghaus 仅为存量数据读取保留）
EBBINGHAUS_CANONICAL = "ebbinghaus_opt"
EBBINGHAUS_ALIASES = (EBBINGHAUS_CANONICAL, "exponential", "ebbinghaus")
TWO_STAGE_CANONICAL = "two_stage"
TWO_STAGE_ALIASES = (TWO_STAGE_CANONICAL,)
ZERO_ALIASES = ("zero",)


def age_seconds_from_created(created_at, now=None):
    """把存储层的时间戳字符串换算为距 now 的秒数。

    支持 CX-A storage._now() 的 ``%Y-%m-%d %H:%M:%S.%f`` 格式，以及 ISO 格式的通用解析。
    解析失败时返回 0.0。
    """
    if not created_at:
        return 0.0
    if now is None:
        now = datetime.now()
    if isinstance(now, str):
        now = _parse_datetime(now)
    created = _parse_datetime(created_at)
    if created is None:
        return 0.0
    delta = (now - created).total_seconds()
    return max(delta, 0.0)


def _parse_datetime(text):
    """尝试多种常见时间格式解析 datetime；失败返回 None。"""
    text = str(text).strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


class DecayCalculator:
    """记忆衰减计算器——按衰减类型计算时间衰减、再激活加成后的记忆分数（因子 0~1）。"""

    # 「极高重要性免疫衰减」阈值（对齐 CX-O zero/permanent 与 config.memory.permanent_threshold）
    DEFAULT_PERMANENT_IMPORTANCE_THRESHOLD = 0.95

    def __init__(self, permanent_importance_threshold=None):
        """初始化衰减计算器。

        Args:
            permanent_importance_threshold: 极高重要性免疫衰减的阈值
                （默认 0.95，对齐 config.memory.permanent_threshold；
                由 MemoryManager 构造时注入自身持有的阈值，M5 接线）。

        默认以真实时钟为准（每次打分即时取 ``datetime.now()``）；
        测试可通过 :meth:`set_current_time` 冻结基准时间。
        """
        self._time_override = None
        self.permanent_importance_threshold = float(
            permanent_importance_threshold
            if permanent_importance_threshold is not None
            else self.DEFAULT_PERMANENT_IMPORTANCE_THRESHOLD
        )

    # ------------------------------------------------------------ 时间基准
    def set_current_time(self, value):
        """设置计算基准时间（测试用）；传 None 恢复真实时钟。"""
        self._time_override = value

    def _now(self):
        """当前基准时间：有覆写用覆写值，否则返回实时时钟。"""
        if self._time_override is not None:
            return self._time_override
        return datetime.now()

    # ------------------------------------------------------------ 衰减因子
    def decay_factor(self, days_elapsed, decay_type="ebbinghaus_opt", params=None):
        """计算纯度高的时间保留因子（0~1，不乘 importance）。

        参数决定走哪种模型：
        - ebbinghaus_opt / exponential / ebbinghaus -> 艾宾浩斯优化版；
        - two_stage                -> 双阶段指数；
        - zero / permanent         -> 恒 1.0。
        未识别类型回退双阶段指数。
        """
        if days_elapsed is None or days_elapsed <= 0:
            return 1.0
        dtype = (decay_type or "ebbinghaus_opt").lower()
        params = params or {}

        if dtype in ZERO_ALIASES:
            return 1.0
        if dtype in EBBINGHAUS_ALIASES:
            t50 = float(params.get("t50", EBBINGHAUS_DEFAULTS["t50"]))
            k = float(params.get("k", EBBINGHAUS_DEFAULTS["k"]))
            return self.calculate_ebbinghaus_decay(1.0, days_elapsed, t50=t50, k=k)
        # two_stage（默认）
        alpha = float(params.get("alpha", TWO_STAGE_DEFAULTS["alpha"]))
        lambda1 = float(params.get("lambda1", TWO_STAGE_DEFAULTS["lambda1"]))
        lambda2 = float(params.get("lambda2", TWO_STAGE_DEFAULTS["lambda2"]))
        return self.calculate_exponential_decay(
            1.0, days_elapsed, alpha=alpha, lambda1=lambda1, lambda2=lambda2
        )

    # ------------------------------------------------------------ CX-O 兼容方法
    def calculate_ebbinghaus_decay(self, importance, days_elapsed, t50=30.0, k=2.0):
        """艾宾浩斯优化版衰减：``T(t)=importance/(1+(t/T50)^k)``。"""
        if days_elapsed <= 0 or t50 <= 0:
            return importance
        factor = 1.0 / (1.0 + (days_elapsed / t50) ** k)
        return min(importance * factor, 1.0)

    def calculate_exponential_decay(
        self, importance, days_elapsed, alpha=0.6, lambda1=0.25, lambda2=0.04
    ):
        """双阶段指数衰减：``T(t)=importance·(α·e^(-λ1·t)+(1-α)·e^(-λ2·t))``。"""
        if days_elapsed <= 0:
            return importance
        factor = alpha * math.exp(-lambda1 * days_elapsed) + (1 - alpha) * math.exp(
            -lambda2 * days_elapsed
        )
        return min(importance * factor, 1.0)

    # ------------------------------------------------------------ 强度/保留/再激活
    def retention(self, importance, age_seconds, decay_type="ebbinghaus_opt", params=None):
        """记忆保留分数（importance 衰减后），0~1。永久记忆路径请用 score。"""
        days = age_seconds / 86400.0
        return self.decay_factor(days, decay_type, params) * float(importance)

    def strength(self, importance, age_seconds, decay_type="ebbinghaus_opt", params=None):
        """记忆强度（同 retention，即衰减后分数）。"""
        return self.retention(importance, age_seconds, decay_type, params)

    def apply_reactivation(self, base_score, reactivation_count, emotion_score=0.0):
        """应用再激活加成与情感加成（对齐 CX-O calculate_reactivation_score）。"""
        if reactivation_count is None or reactivation_count <= 0:
            return max(base_score + 0.05 * abs(emotion_score or 0.0), 0.0)
        enhanced = base_score * (1.0 + REACTIVATION_GAIN * reactivation_count) + REACTIVATION_BASE
        enhanced += EMOTION_BOOST * abs(emotion_score or 0.0)
        return min(enhanced, 1.0)

    # ------------------------------------------------------------ 统一入口
    def score(
        self,
        importance,
        age_seconds,
        decay_type="ebbinghaus_opt",
        params=None,
        reactivation_count=0,
        emotion_score=0.0,
        permanent=False,
    ):
        """计算最终记忆因子（0~1）。

        Args:
            importance: 重要性分数（0~1）。
            age_seconds: 距上次记忆建立经过的秒数。
            decay_type: 衰减类型（ebbinghaus_opt/two_stage/zero；exponential、
                ebbinghaus 为兼容存量别名）。
            params: 衰减参数 dict（t50/k 或 alpha/lambda1/lambda2）。
            reactivation_count: 再激活次数（访问次数越多分越高）。
            emotion_score: 情感强度（越高分越高）。
            permanent: 是否为永久记忆（恒 1.0）。

        Returns:
            float: 0~1 的分数，先按时间衰减，再叠加再激活与情感加成。
        """
        importance = max(0.0, min(1.0, float(importance)))
        # 永久记忆或极高重要性记忆不随时间衰减（对齐 CX-O zero/permanent 逻辑；
        # 阈值由构造参数注入，默认 0.95 对齐 config.memory.permanent_threshold）
        if permanent or importance >= self.permanent_importance_threshold:
            return 1.0

        base = self.strength(importance, age_seconds, decay_type, params)
        return self.apply_reactivation(base, reactivation_count, emotion_score)