# -*- coding: utf-8 -*-
"""应用根目录解析（M-14，第三轮体检批次4）——开发态 / PyInstaller 冻结态双轨。

修复前 lite/ 各模块（audio.asr / audio.voice_manager / memory.storage /
config.config_manager / graph.graph_store）均以 ``__file__`` 逐级上溯推导
``<root>/data``；PyInstaller onedir 冻结态下 ``__file__`` 位于
``runtime/backend/_internal/lite/...``，推导结果指向 ``_internal/data``
而非便携根 ``data/``——真实模型装载必然失败（潜伏缺陷）。

本模块提供唯一的 frozen-aware 根解析函数 ``app_root()``：

- 冻结态（``sys.frozen``）：从 ``sys.executable`` 上溯——
  ``<root>/runtime/backend/backend.exe`` → backend → runtime → root；
- 开发态：从本文件上溯——``<root>/lite/config/paths.py`` → config → lite → root。

各模块的 ``data_dir()`` / 默认路径推导统一改调本函数，禁止再各自用
``__file__`` 上溯。

路径规范：一律基于 ``sys.executable`` / ``os.path.abspath(__file__)`` 推导，
禁止相对路径与字符串斜杠拼接。
"""

import os
import sys

__all__ = ["app_root", "data_root"]


def app_root() -> str:
    """推导应用安装根目录（开发态 = 项目根；冻结态 = 便携根）。

    :return: str 绝对路径。
    """
    if getattr(sys, "frozen", False):
        # 冻结态：<root>/runtime/backend/backend.exe
        # exe 路径 -> backend 目录 -> runtime 目录 -> 安装根
        exe_path = os.path.abspath(sys.executable)
        backend_dir = os.path.dirname(exe_path)
        runtime_dir = os.path.dirname(backend_dir)
        return os.path.dirname(runtime_dir)
    # 开发态：lite/config/paths.py -> lite -> 项目根
    _config_dir = os.path.dirname(os.path.abspath(__file__))
    _lite_dir = os.path.dirname(_config_dir)
    return os.path.dirname(_lite_dir)


def data_root() -> str:
    """推导数据目录：``<app_root>/data``。

    :return: str 绝对路径。
    """
    return os.path.join(app_root(), "data")
