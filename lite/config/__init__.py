# -*- coding: utf-8 -*-
"""CX-A 配置系统包入口。

对外暴露 ConfigManager 与全局默认值 DEFAULTS，
以及热更新段判定常量（HOT_RELOAD_SECTIONS / NEED_RESTART_SECTIONS）。
"""

from .config_manager import (
    ConfigManager,
    DEFAULTS,
    HOT_RELOAD_SECTIONS,
    NEED_RESTART_SECTIONS,
)

__all__ = [
    "ConfigManager",
    "DEFAULTS",
    "HOT_RELOAD_SECTIONS",
    "NEED_RESTART_SECTIONS",
]