# -*- coding: utf-8 -*-
"""主动视觉包（lite/vision）——自适应频率屏幕采样与视觉记忆沉淀。

模块构成（Task H2，对齐 CX-O server/core/vision 语义并做 lite 单线程同步化）：
- ``change_detector``：帧间变化率检测（灰度像素平均绝对差归一化）；
- ``sampler``：自适应采样频率调节（变化升频 / 静止降频 / 中间线性插值）；
- ``queue``：有界视觉片段队列（满时丢弃最旧 + consumer 异常隔离 worker 线程）；
- ``pipeline``：采样 → 队列 → 云端理解 → 记忆沉淀生产装配。

隐私红线：默认关（``vision.enabled=false``）；仅采样本机屏幕且帧数据立即降采样，
原始帧不落盘、不外传；关闭状态下绝不触发 ``screen_backend.capture``。
"""
