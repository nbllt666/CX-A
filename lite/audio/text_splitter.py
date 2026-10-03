# -*- coding: utf-8 -*-
"""文本切分工具（lite/audio/text_splitter.py）——按标点符号把长文本切成短句。

供 TTS 流式合成使用：把整段文本按句末标点切成语义完整的短句，逐句合成
可显著降低首音延迟，并把 MeloTTS decoder 塌陷导致的静音段隔离在单句内。

设计约束：
- **纯标准库**：不引入 jieba / 正则第三方依赖（sidecar 与主环境一致性）。
- **保留标点**：句末标点留在对应句尾，不丢弃（合成停顿更自然）。
- **两级切分**：
  1. 句末标点（。！？；.!?;）→ 硬切，每句语义完整；
  2. 若某句仍超过上限，在逗号（，,、）处二次切分。
- **首段专用上限**（20260930 首帧优化）：**第 1 句**用更小的 ``first_seg_chars``
  触发逗号切分——首帧延迟 ≈ 首个音频段合成时长，首段越短首音越早；其余句维持
  ``max_seg_chars`` 大粒度（后续段合成与播放并行，无需更细，少切一次少一次固定开销）。
- **空段过滤**：纯空白段不返回（避免空合成请求）。
- **单句直通**：整段无句末标点时，若长度 <= 上限原样返回，
  否则在逗号处切；无逗号则整段返回（不硬切词）。
"""

#: 一级切分标点（句末，硬切）。
_SENTENCE_END_PUNCT = "。！？；.!?;"
#: 二级切分标点（逗号类，仅在超限句内使用）。
_CLAUSE_PUNCT = "，,、"
#: 首段专用切分上限默认值（20260930 首帧优化）：首句超此长度即在逗号处切细，
#: 压缩 TTS 首帧延迟。定档依据为 warm 实测曲线（`first_seg_curve_probe.py`，
#: 经验曲线驱动、未建解析成本模型——实测存在非线性台阶）：
#: 首段 ≤10 字 ≈1.0s、13 字 ≈1.7s、22 字 ≈2.1s。取 12：`len > cap` 严格判定下
#: **13 字级首句即被切细**，而本已 ≤1s 的 ≤10 字首句不受打扰。
_DEFAULT_FIRST_SEG_CHARS = 12

__all__ = ["split_by_punctuation"]


def split_by_punctuation(text, max_seg_chars=80, first_seg_chars=None):
    """按标点符号把文本切成短句列表。

    :param text: 待切分文本
    :param max_seg_chars: 非首段的最大字符数；超过时在逗号处二次切分
    :param first_seg_chars: **首段**专用上限（20260930 首帧优化）；首句超此长度即
        在逗号处切细。缺省（``None``）取 ``min(_DEFAULT_FIRST_SEG_CHARS, max_seg_chars)``
        ——既开启首帧优化，又不高于调用方给定的总上限；显式传 ``max_seg_chars``
        即为旧口径（首段与其余段同上限）
    :return: 切分后的短句列表（不含空段）
    """
    if text is None:
        return []
    text = str(text)
    if not text.strip():
        return []

    # 第一级：按句末标点切分，标点留在句尾
    segments = _split_on_chars(text, _SENTENCE_END_PUNCT, keep_punct=True)

    # 第二级：超限句在逗号处二次切分（首句用更小的上限——首帧延迟主导项）
    first_cap = (
        min(_DEFAULT_FIRST_SEG_CHARS, max_seg_chars)
        if first_seg_chars is None
        else first_seg_chars
    )
    result = []
    for index, seg in enumerate(segments):
        if not seg.strip():
            continue
        cap = first_cap if index == 0 else max_seg_chars
        if len(seg) <= cap:
            result.append(seg)
        else:
            result.extend(_split_on_chars(seg, _CLAUSE_PUNCT, keep_punct=True))

    # 过滤二次切分后可能产生的空段
    return [s for s in result if s.strip()]


def _split_on_chars(text, chars, keep_punct=True):
    """按给定字符集合切分文本。

    :param text: 输入文本
    :param chars: 切分字符集合字符串
    :param keep_punct: True 时标点留在前一段末尾；False 时丢弃
    :return: 切分后的片段列表（可能含空串）
    """
    segments = []
    buf = []
    for ch in text:
        buf.append(ch)
        if ch in chars:
            piece = "".join(buf)
            buf = []
            if keep_punct:
                segments.append(piece)
            else:
                segments.append(piece[:-1])
    if buf:
        segments.append("".join(buf))
    return segments
