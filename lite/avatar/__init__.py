# -*- coding: utf-8 -*-
"""虚拟形象表情子包（Task H3）：表情标签解析，对齐 CX-O tagParser 标签协议。

对外导出：

- :class:`EmotionTagParser`：``[emotion:x]`` 标签解析器
  （``parse(text) -> {clean_text, mood, tags}``；未知/非法标签原文保留）；
- :data:`SUPPORTED_EMOTIONS`：支持的情绪集（CX-O 兼容子集 + CX-A 扩展 shy）；
- :data:`DEFAULT_MOOD`：无已识别标签时的默认情绪档位（calm）。
"""

from lite.avatar.tags import DEFAULT_MOOD, SUPPORTED_EMOTIONS, EmotionTagParser

__all__ = ["EmotionTagParser", "SUPPORTED_EMOTIONS", "DEFAULT_MOOD"]
