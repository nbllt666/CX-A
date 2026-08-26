# -*- coding: utf-8 -*-
"""云端适配层子包（Task A2）：CloudAdapter 及云端配置/网络异常。

对外暴露：CloudAdapter、PROVIDER_BASE_URLS、CloudConfigError、CloudUnavailableError，
以及 Task C3 断网兜底的 OfflineFallbackManager 与离线提示文案 OFFLINE_PROMPT。
"""

from .adapter import (
    PROVIDER_BASE_URLS,
    CloudAdapter,
    CloudConfigError,
    CloudUnavailableError,
)
from .fallback import OFFLINE_PROMPT, OfflineFallbackManager

__all__ = [
    "CloudAdapter",
    "PROVIDER_BASE_URLS",
    "CloudConfigError",
    "CloudUnavailableError",
    "OfflineFallbackManager",
    "OFFLINE_PROMPT",
]