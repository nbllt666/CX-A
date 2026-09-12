# -*- coding: utf-8 -*-
"""自适应频率屏幕采样器（AdaptiveSampler）。

职责：按当前采样间隔决定是否采集一帧屏幕，计算与上一帧的变化率，并据变化率
反向调节采样间隔——变化越大采样越频繁（抓重点），静止越久采样越稀疏（省资源）。

频率调节规则（变化率 ratio 与阈值 high/low）：
- ratio >= high_threshold → interval = max_interval_s（最高频，默认 2s）；
- ratio <= low_threshold  → interval = min_interval_s（最低频，默认 30s）；
- 中间区间线性插值：ratio 从 low → high 映射 interval 从 min_interval_s → max_interval_s。

命名约定（与配置契约键一致）：``min_interval_s`` 是**最低频率对应的最长间隔**（静止态），
``max_interval_s`` 是**最高频率对应的最短间隔**（剧变态）。

事件产出：仅当变化率 >= high_threshold 时产出事件 dict
``{timestamp, frame, change_ratio}``；其余采样（含首次基线采样）返回 None。

内存防线：内部记录的上一帧与事件携带的帧均为**立即降采样后的定长帧**
（由 detector 归一化，默认 64x36），原始帧不保留（隐私红线：原始帧不外传）。

首次采样仅建立基线：不产出事件、不调整间隔（无参照帧，变化率无意义）。
"""

import time

from lite.vision.change_detector import ChangeDetector


class AdaptiveSampler:
    """自适应频率屏幕采样器（由帧间变化率驱动采样间隔）。"""

    def __init__(self, screen_backend, min_interval_s: float = 30,
                 max_interval_s: float = 2, high_threshold: float = 0.35,
                 low_threshold: float = 0.05, detector: ChangeDetector = None):
        """构造采样器。

        :param screen_backend: 屏幕后端抽象，须实现 ``capture() -> frame``
            （返回灰度像素序列）；测试注入脚本化 mock。
        :param min_interval_s: 最低频率对应的最长间隔秒数（静止态，默认 30）
        :param max_interval_s: 最高频率对应的最短间隔秒数（剧变态，默认 2）
        :param high_threshold: 变化率高阈值（>= 触发事件 + 最高频，默认 0.35）
        :param low_threshold: 变化率低阈值（<= 降至最低频，默认 0.05）
        :param detector: 变化率检测器；缺省新建 :class:`ChangeDetector`（64x36）
        """
        if min_interval_s < max_interval_s:
            raise ValueError(
                f"min_interval_s（最长间隔）必须 >= max_interval_s（最短间隔），"
                f"收到 min={min_interval_s}, max={max_interval_s}"
            )
        if not (0.0 <= low_threshold < high_threshold <= 1.0):
            raise ValueError(
                f"阈值须满足 0 <= low < high <= 1，收到 low={low_threshold}, high={high_threshold}"
            )
        self._backend = screen_backend
        self._min_interval_s = float(min_interval_s)
        self._max_interval_s = float(max_interval_s)
        self._high_threshold = float(high_threshold)
        self._low_threshold = float(low_threshold)
        self._detector = detector or ChangeDetector()
        #: 当前采样间隔（初始取最高频，尽快建立基线并响应变化）
        self._interval = self._max_interval_s
        #: 上次采样时间戳（None 表示尚未采样）
        self._last_sample_ts = None
        #: 上次采样帧（已降采样定长存储，防内存膨胀）
        self._last_frame = None
        #: 最近一次成功比对的变化率（尚未比对过时为 None）
        self._last_ratio = None

    # ------------------------------------------------------------------ #
    # 对外接口                                                            #
    # ------------------------------------------------------------------ #

    @property
    def current_interval(self) -> float:
        """当前采样间隔（秒）。测试与运维观测频率调节曲线的透出口。"""
        return self._interval

    @property
    def last_change_ratio(self):
        """最近一次成功比对的变化率（尚未比对过时为 None）。"""
        return self._last_ratio

    def tick(self, now: float = None):
        """驱动一次采样决策。

        按当前间隔判断是否到期：未到期返回 None；到期则采集一帧并与上一帧
        比对变化率，更新间隔，且仅当变化率 >= high_threshold 时产出事件。

        Args:
            now: 当前时间戳（秒）；缺省用 ``time.time()``。测试注入脚本化时间。

        Returns:
            dict | None: 变化达标时返回
                ``{"timestamp": now, "frame": 降采样帧, "change_ratio": 变化率}``；
                静止 / 未到期 / 首次基线采样返回 None。
        """
        if now is None:
            now = time.time()
        now = float(now)
        if self._last_sample_ts is not None and (now - self._last_sample_ts) < self._interval:
            return None  # 未到期：本轮不采样
        return self._sample(now)

    # ------------------------------------------------------------------ #
    # 内部：采样与频率调节                                                #
    # ------------------------------------------------------------------ #

    def _sample(self, now: float):
        """执行一次采样：采集 → 降采样存储 → 比对 → 调频 → 事件判定。"""
        frame = self._backend.capture()
        # 帧数据立即降采样存储（防内存膨胀；原始帧即弃不保留）
        norm_frame = self._detector.normalize(frame)
        prev_frame = self._last_frame
        self._last_frame = norm_frame
        self._last_sample_ts = now

        if prev_frame is None:
            # 首次采样仅建立基线：无参照帧，不产出事件、不调整间隔
            return None

        ratio = self._detector.change_ratio(prev_frame, norm_frame)
        self._last_ratio = ratio
        self._update_interval(ratio)

        if ratio >= self._high_threshold:
            return {"timestamp": now, "frame": norm_frame, "change_ratio": ratio}
        return None

    def _update_interval(self, ratio: float) -> None:
        """按变化率更新采样间隔（高→最高频 / 低→最低频 / 中间线性插值）。"""
        if ratio >= self._high_threshold:
            self._interval = self._max_interval_s
            return
        if ratio <= self._low_threshold:
            self._interval = self._min_interval_s
            return
        # 中间线性插值：ratio 从 low → high 映射 interval 从 min → max
        t = (ratio - self._low_threshold) / (self._high_threshold - self._low_threshold)
        self._interval = self._min_interval_s + t * (self._max_interval_s - self._min_interval_s)
