# -*- coding: utf-8 -*-
"""帧间变化率检测器（ChangeDetector）。

职责：对两帧灰度像素数据计算归一化变化率（0~1），作为自适应采样频率的驱动信号。

设计要点：
- 输入为灰度像素列表 / bytes（元素取值 0~255）；目标分辨率宽高可配，
  默认下采样 64x36（2304 像素，定长表示防内存膨胀）；
- lite 不感知源分辨率（screen_backend 仅给出一维像素序列），降采样采用
  **等距索引重采样**：源长于目标时等距抽取，短于目标时等距重复补齐——
  保证任意输入归一化为同一长度后可比较；生产截图管线可在 backend 侧
  先做真二维降采样，本模块结果不受影响；
- 变化率定义：逐像素平均绝对差 / 255，即完全相同 → 0.0，完全反转 → 1.0；
- 纯标准库实现；numpy 为可选加速路径，不可用时自动回退纯 Python 循环。
"""

try:  # numpy 可选加速依赖，缺失时回退纯 Python
    import numpy as _np
except ImportError:  # pragma: no cover - 依赖探测分支
    _np = None


class ChangeDetector:
    """帧间变化率检测器（灰度像素平均绝对差归一化）。"""

    def __init__(self, width: int = 64, height: int = 36):
        """构造检测器。

        :param width: 降采样目标宽度（默认 64）
        :param height: 降采样目标高度（默认 36）
        """
        if width <= 0 or height <= 0:
            raise ValueError(f"降采样宽高必须为正数，收到 width={width}, height={height}")
        #: 降采样目标宽度
        self.width = int(width)
        #: 降采样目标高度
        self.height = int(height)
        #: 定长像素表示的目标长度（width * height）
        self.target_size = self.width * self.height

    # ------------------------------------------------------------------ #
    # 帧归一化                                                            #
    # ------------------------------------------------------------------ #

    def normalize(self, frame):
        """把任意长度的一维灰度帧归一化为定长像素列表。

        - bytes / bytearray / memoryview 输入先转为整数列表；
        - 长度恰为目标长度：逐像素取整后原样返回（幂等：归一化结果再归一化不变）；
        - 长度不一致：等距索引重采样到目标长度（长→抽取，短→重复补齐）；
        - None 返回 None（由 change_ratio 决定语义）。

        :param frame: 灰度像素序列（list/tuple/bytes 等）
        :return: 定长整数像素列表（长度 target_size），或 None
        """
        if frame is None:
            return None
        if isinstance(frame, memoryview):
            frame = bytes(frame)
        if isinstance(frame, (bytes, bytearray)):
            pixels = list(frame)
        else:
            pixels = [int(p) for p in frame]
        n = len(pixels)
        if n == self.target_size:
            return pixels
        if n == 0:
            return []
        step = n / self.target_size
        return [pixels[int(i * step)] for i in range(self.target_size)]

    # ------------------------------------------------------------------ #
    # 变化率计算                                                          #
    # ------------------------------------------------------------------ #

    def change_ratio(self, prev_frame, curr_frame) -> float:
        """计算两帧归一化变化率（0~1）。

        定义：逐像素平均绝对差 / 255。相同帧 → 0.0；全异（0 vs 255）→ 1.0；
        部分变化 → 中间值。

        Args:
            prev_frame: 上一帧灰度像素序列（或 None）
            curr_frame: 当前帧灰度像素序列（或 None）

        Returns:
            float: 变化率，范围 [0.0, 1.0]。
                双 None / 双空帧 → 0.0；单侧 None / 单侧空帧 → 1.0（视为完全变化）。
        """
        prev_empty = prev_frame is None or len(prev_frame) == 0
        curr_empty = curr_frame is None or len(curr_frame) == 0
        if prev_empty and curr_empty:
            return 0.0
        if prev_empty or curr_empty:
            return 1.0

        prev_norm = self.normalize(prev_frame)
        curr_norm = self.normalize(curr_frame)
        n = self.target_size

        if _np is not None:  # numpy 可选加速路径
            a = _np.asarray(prev_norm, dtype=_np.float32)
            b = _np.asarray(curr_norm, dtype=_np.float32)
            ratio = float(_np.mean(_np.abs(a - b))) / 255.0
        else:  # 纯标准库回退路径
            total = 0
            for p, c in zip(prev_norm, curr_norm):
                total += abs(p - c)
            ratio = (total / n) / 255.0

        # 浮点钳位，防御异常输入导致的越界
        if ratio < 0.0:
            return 0.0
        if ratio > 1.0:
            return 1.0
        return ratio
