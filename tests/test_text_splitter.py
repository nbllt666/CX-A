# -*- coding: utf-8 -*-
"""文本切分工具单测（lite/audio/text_splitter.py）。"""

from lite.audio.text_splitter import split_by_punctuation


def test_empty_returns_empty_list():
    """空文本 / None / 纯空白 → 空列表。"""
    assert split_by_punctuation("") == []
    assert split_by_punctuation(None) == []
    assert split_by_punctuation("   \n\t  ") == []


def test_no_punctuation_single_segment():
    """无标点短文本 → 原样返回单段。"""
    assert split_by_punctuation("你好啊") == ["你好啊"]


def test_split_on_sentence_end_punct():
    """句末标点（。！？；）硬切，标点留在句尾。"""
    text = "你好，世界。今天天气不错！开心吗；是的。"
    result = split_by_punctuation(text)
    assert result == ["你好，世界。", "今天天气不错！", "开心吗；", "是的。"]


def test_english_punctuation_also_splits():
    """英文句末标点（.!?;）同样切分。"""
    assert split_by_punctuation("Hello. World! How? Yes;") == [
        "Hello.", " World!", " How?", " Yes;",
    ]


def test_long_segment_splits_on_comma():
    """超过 max_seg_chars 的段在逗号处二次切分。"""
    text = "今天天气很好，我们一起出去玩吧，然后吃了一顿大餐，接着看了一场电影，最后回家休息了，明天还要继续工作，后天也是一样的日程安排，大后天还要加班加点赶项目进度，大大后天才能稍微休息一下。"
    result = split_by_punctuation(text, max_seg_chars=80)
    assert len(result) > 1
    for seg in result:
        assert len(seg) <= 80
    # 标点保留
    assert "".join(result) == text


def test_custom_max_seg_chars():
    """max_seg_chars 可配置；设小后逗号切分更细。"""
    text = "a，b，c，d，e。"
    result = split_by_punctuation(text, max_seg_chars=2)
    assert "".join(result) == text
    assert all(len(seg) <= 2 for seg in result)


def test_only_punctuation():
    """纯标点文本 → 每句一个标点段。"""
    assert split_by_punctuation("。。。") == ["。", "。", "。"]


def test_whitespace_segments_filtered():
    """切分后产生的空白段被过滤。"""
    result = split_by_punctuation("。   。")
    assert result == ["。", "   。"]  # 中间空白保留在第二段，但不会产生纯空白段


# ---------------------------------------------------------------- 首段细切分（20260930 首帧优化）
def test_first_sentence_splits_at_comma_for_faster_first_frame():
    """首句超 first_seg_chars 即在逗号处切细（首帧延迟 ≈ 首段合成时长）；
    其余句维持 max_seg_chars 大粒度（后续段合成与播放并行）。"""
    text = (
        "今天过得还不错，刚刚在整理一下数据，感觉挺平静的。"          # 25 字 > 24 → 切细
        "明天我们一起去公园散步，然后去吃顿好的，最后看场电影吧。"     # 33 字 <= 80 → 不切
    )
    result = split_by_punctuation(text)
    assert result[0] == "今天过得还不错，"          # 首段切到首个逗号（8 字）
    assert result[-1] == "明天我们一起去公园散步，然后去吃顿好的，最后看场电影吧。"  # 后续句不切
    assert "".join(result) == text


def test_first_sentence_short_enough_stays_whole():
    """首句本就不超上限 → 不动（无额外合成调用）。"""
    text = "你好呀。今天过得怎么样？"
    assert split_by_punctuation(text) == ["你好呀。", "今天过得怎么样？"]


def test_first_sentence_13_chars_splits_by_measured_curve():
    """实测曲线（20260930 warm）：首段 ≤10 字 ≈1.0s、13 字 ≈1.7s、22 字 ≈2.1s；
    13 字首句必须被切细（阈值 12 的定档依据；`len > cap` 严格判定）。"""
    assert split_by_punctuation("你好呀，今天过得还不错呢。") == [
        "你好呀，", "今天过得还不错呢。",
    ]
    assert split_by_punctuation("早上醒来时阳光正好，感觉整个人都充满了能量。") == [
        "早上醒来时阳光正好，", "感觉整个人都充满了能量。",
    ]


def test_first_seg_chars_explicit_override():
    """显式 first_seg_chars：更小阈值让首段切得更细。"""
    text = "今天过得还不错，刚刚在整理一下数据。"
    result = split_by_punctuation(text, first_seg_chars=5)
    assert result[0] == "今天过得还不错，"  # 首个逗号处切开（细小阈值触发）
    assert "".join(result) == text


def test_first_seg_chars_legacy_equivalent():
    """显式传 first_seg_chars=max_seg_chars → 旧口径（首段与其余段同上限，不细切）。"""
    text = "今天过得还不错，刚刚在整理一下数据，感觉挺平静的。"
    legacy = split_by_punctuation(text, max_seg_chars=80, first_seg_chars=80)
    assert legacy == [text]  # 25 字 <= 80 → 整句不切
    # 默认（首帧优化开启）下同一文本会被逗号切细
    assert split_by_punctuation(text) != [text]
