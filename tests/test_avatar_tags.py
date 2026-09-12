# -*- coding: utf-8 -*-
"""表情标签解析器单元测试（Task H3）：EmotionTagParser 全分支覆盖。

覆盖：已识别标签（单/多/居中/大小写/全情绪集）、未知与非法标签原文保留
（对齐 spec Scenario「未知情绪降级」口径）、混合场景、空文本与无标签默认 calm。
"""

import pytest

from lite.avatar import DEFAULT_MOOD, SUPPORTED_EMOTIONS, EmotionTagParser


@pytest.fixture()
def parser():
    """被测解析器实例（无状态，用例间共享无副作用）。"""
    return EmotionTagParser()


class TestRecognizedTags:
    """已识别标签：从 clean_text 移除，mood 取首个已识别情绪。"""

    def test_single_tag_removed_and_mood_set(self, parser):
        """单标签：标签移除 + mood 命中。"""
        result = parser.parse("[emotion:happy]今天真开心")
        assert result == {"clean_text": "今天真开心", "mood": "happy", "tags": ["happy"]}

    @pytest.mark.parametrize("emotion", sorted(SUPPORTED_EMOTIONS))
    def test_all_supported_emotions(self, parser, emotion):
        """全情绪集逐一识别（7 档全通过）。"""
        result = parser.parse(f"[emotion:{emotion}]正文")
        assert result["clean_text"] == "正文"
        assert result["mood"] == emotion
        assert result["tags"] == [emotion]

    def test_multiple_tags_first_wins(self, parser):
        """多标签：全部移除，mood 取首个（sad 在前）。"""
        result = parser.parse("[emotion:sad]呜…[emotion:happy]又好了")
        assert result["clean_text"] == "呜…又好了"
        assert result["mood"] == "sad"
        assert result["tags"] == ["sad", "happy"]

    def test_tag_in_middle(self, parser):
        """标签居中：两侧文本正常拼接。"""
        result = parser.parse("你[emotion:shy]好")
        assert result["clean_text"] == "你好"
        assert result["mood"] == "shy"

    def test_case_insensitive(self, parser):
        """大小写不敏感：标签名与情绪值混写均可识别。"""
        result = parser.parse("[EMOTION:HAPPY]好[Emotion:Sleepy]")
        assert result["clean_text"] == "好"
        assert result["mood"] == "happy"
        assert result["tags"] == ["happy", "sleepy"]

    def test_emotion_value_with_spaces(self, parser):
        """情绪值带空白：strip 后仍识别。"""
        result = parser.parse("[emotion: HAPPY ]你好")
        assert result["clean_text"] == "你好"
        assert result["mood"] == "happy"


class TestUnknownKept:
    """未知 / 非法标签：原文保留在 clean_text（spec Scenario 口径）。"""

    def test_unknown_emotion_kept_and_mood_calm(self, parser):
        """未知情绪 [emotion:confused]：原文保留，表情回落 calm，不报错。"""
        result = parser.parse("[emotion:confused]嗯？")
        assert result["clean_text"] == "[emotion:confused]嗯？"
        assert result["mood"] == "calm"
        assert result["tags"] == []

    def test_unknown_tag_type_kept(self, parser):
        """非 emotion 类型的方括号标签 [badtag:xx]：原文保留。"""
        result = parser.parse("[badtag:xx]文本[emotion:calm]")
        assert result["clean_text"] == "[badtag:xx]文本"
        assert result["mood"] == "calm"
        assert result["tags"] == ["calm"]

    def test_mixed_known_unknown(self, parser):
        """已知 + 未知混合：未知保留、已知移除，mood 仍取已知首个。"""
        result = parser.parse("知道[emotion:confused]吗[emotion:sleepy]")
        assert result["clean_text"] == "知道[emotion:confused]吗"
        assert result["mood"] == "sleepy"
        assert result["tags"] == ["sleepy"]

    def test_empty_param_tag_kept(self, parser):
        """空参数畸形标签 [emotion:]：原文保留，不识别。"""
        result = parser.parse("[emotion:]空参数")
        assert result["clean_text"] == "[emotion:]空参数"
        assert result["mood"] == "calm"
        assert result["tags"] == []


class TestDefaults:
    """无标签 / 空文本：默认 calm。"""

    def test_plain_text_default_calm(self, parser):
        """纯文本无标签：原样返回 + 默认 calm。"""
        assert parser.parse("纯文本没有标签") == {
            "clean_text": "纯文本没有标签",
            "mood": "calm",
            "tags": [],
        }

    def test_empty_text(self, parser):
        """空文本：空产出 + 默认 calm。"""
        assert parser.parse("") == {"clean_text": "", "mood": "calm", "tags": []}

    def test_none_text(self, parser):
        """None 输入（防呆）：按空文本处理。"""
        assert parser.parse(None) == {"clean_text": "", "mood": "calm", "tags": []}

    def test_only_unknown_tags_default_calm(self, parser):
        """仅含未知标签：原文全保留 + 默认 calm。"""
        result = parser.parse("[emotion:confused][badtag:1]")
        assert result["clean_text"] == "[emotion:confused][badtag:1]"
        assert result["mood"] == "calm"
        assert result["tags"] == []


def test_supported_emotions_constant():
    """情绪集常量：CX-O 兼容子集 6 个 + CX-A 扩展 shy，共 7 个。"""
    assert SUPPORTED_EMOTIONS == {
        "happy", "calm", "sad", "surprised", "angry", "sleepy", "shy",
    }


def test_default_mood_constant():
    """默认档位常量为 calm（与解析回落一致）。"""
    assert DEFAULT_MOOD == "calm"
