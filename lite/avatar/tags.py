# -*- coding: utf-8 -*-
"""虚拟形象表情标签解析（Task H3，对齐 CX-O tagParser 标签协议）。

标签协议：LLM 回复文本内嵌 ``[emotion:x]`` 情绪标签，解析产出干净文本与表情档位，
供后端下发前端驱动 PetAvatar 表情切换。

语义口径（spec「虚拟形象表情优化」Scenario 定稿）：
- 已识别标签（情绪在 :data:`SUPPORTED_EMOTIONS` 内）→ 从 clean_text 中移除，
  mood 取**首个**已识别情绪（无任何已识别标签时默认 calm）；
- 未知情绪标签（如 ``[emotion:confused]``）与其它非法标签 / 非标签方括号文本
  （如 ``[badtag:xx]``）→ **原文保留**在 clean_text 中，不报错、不影响 mood
  （spec Scenario「未知情绪降级」明确口径）。
  注意：CX-O tagParser 对「类型可识别但情感名未知」是剥离（dropped），CX-A spec
  显式选择保留原文——本实现以 CX-A spec 为准，差异已登记承接 note；
- 大小写不敏感：``[EMOTION:HAPPY]`` 与 ``[emotion:happy]`` 等价。
"""

import re

#: 支持的情绪集：CX-O SUPPORTED_EMOTIONS 兼容子集（happy/calm/sad/surprised/angry/
#: sleepy）+ CX-A 扩展 shy；前端 PetAvatar 渲染其中 6 档（angry 由前端回落 calm）
SUPPORTED_EMOTIONS = {"happy", "calm", "sad", "surprised", "angry", "sleepy", "shy"}

#: 默认情绪档位：无任何已识别标签时的回落值
DEFAULT_MOOD = "calm"

#: [emotion:x] 标签匹配正则（大小写不敏感；参数段允许为空以覆盖 [emotion:] 畸形标签）
_EMOTION_TAG_RE = re.compile(r"\[emotion:([^\]]*)\]", re.IGNORECASE)


class EmotionTagParser:
    """表情标签解析器：从 LLM 回复中提取 [emotion:x] 标签。

    解析产出三字段 dict：

    - ``clean_text``：移除已识别标签后的干净文本；未知 / 非法标签原文保留；
    - ``mood``：首个已识别情绪（默认 :data:`DEFAULT_MOOD`）；
    - ``tags``：已识别情绪列表（按出现顺序，可重复、可为空）。
    """

    #: 支持的情绪集（类级引用模块常量，保持单一真相源）
    SUPPORTED_EMOTIONS = SUPPORTED_EMOTIONS

    def parse(self, text):
        """解析文本中的 [emotion:x] 标签。

        :param text: LLM 原始回复文本；None 按空文本处理
        :return: dict ``{clean_text: str, mood: str, tags: list[str]}``
        """
        if not text:
            return {"clean_text": "", "mood": DEFAULT_MOOD, "tags": []}
        mood = None
        tags = []

        def _replace(match):
            """单标签替换：已识别 → 移除并记录；未知 → 原文保留。"""
            nonlocal mood
            emotion = match.group(1).strip().lower()
            if emotion in self.SUPPORTED_EMOTIONS:
                if mood is None:
                    mood = emotion
                tags.append(emotion)
                return ""  # 已识别标签：从 clean_text 移除
            return match.group(0)  # 未知 / 非法标签：原文保留（对齐 spec Scenario）

        clean_text = _EMOTION_TAG_RE.sub(_replace, text)
        return {"clean_text": clean_text, "mood": mood or DEFAULT_MOOD, "tags": tags}
