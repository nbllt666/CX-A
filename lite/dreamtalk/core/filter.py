"""梦境内容闸门：移植 CX-O 梦境过滤的事实性断言拦截。

梦境是虚构联想，不得断言用户真实经历。本模块只做确定性文本校验，
不引入 LLM 兜底，避免过滤本身产生新的幻觉。
"""

from __future__ import annotations

import re
from typing import List

# 事实性断言：第二人称事实锚点 + 后续具体事件描述（六类）
FACTUAL_PATTERNS = (
    re.compile(r"你昨天.+"),
    re.compile(r"你刚才.+"),
    re.compile(r"你说过.+"),
    re.compile(r"你曾经.+"),
    re.compile(r"你做了.+"),
    re.compile(r"我记得你.+"),
)

# 空文本重试后仍为空时的占位
DREAM_BROKEN_TEXT = "（梦在这里断了一瞬）"

# 首次越界后的纠正提示（只重写一次）
REWRITE_HINT = (
    "刚才那段叙述触碰了梦的契约：梦中不得断言用户的真实经历。"
    "请把它改写成纯粹的梦境意象或隐喻，保留原有的画面与动作，"
    "不要出现「你昨天」「你说过」「你曾经」「你做了」这类事实断言，"
    "只输出改写后的这一段，不要解释。"
)

# 上游命名别名，便于按 CX-D 代码逐行对照
CORRECTION_INSTRUCTION = REWRITE_HINT


def check_text(text: str) -> List[str]:
    """返回命中的事实性断言模式列表；空列表表示通过。"""
    content = (text or "").strip()
    if not content:
        return []
    return [pattern.pattern for pattern in FACTUAL_PATTERNS if pattern.search(content)]


def is_clean(text: str) -> bool:
    """文本是否通过梦境契约校验。"""
    return not check_text(text)
