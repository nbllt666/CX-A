# -*- coding: utf-8 -*-
"""Task B2 语音对话全链路单元测试。

覆盖（全 mock，无网络 / 无真实音频）：
- 完整链路：VAD 判结束 → ASR 转写 → 云端流式回复 → TTS → 断言 text/audio 与历史
- H3 话语缓冲：非 end 帧累积缓冲；end 帧把整段拼接音频交给 ASR；首帧即 end 保持旧行为
- 自言自语：判定器返回 False → should_reply False，且不调用 cloud / tts
- 默认态：不注入判定器 → 默认回复（should_reply True，不哑巴）
- 离线：cloud 离线、注入 offline_local → 走本地兜底；未注入 → 提示文案
- M10 异常兜底：cloud.chat / offline_local / asr.transcribe / tts.synthesize /
  judge 任一层抛异常均不中断 feed_audio 循环，回落 OFFLINE_HINT 或结构化 dict
- 历史滚动：超过 20 条时裁剪最旧条目
- 判定出口 judge_should_reply_text：None / 未就绪 → True；False → False
- 启发式桩 HeuristicJudge 规则
- E2E：真实 LiteVAD + 轮换转写 ASR + mock 云/TTS 多轮音频流走通全链路
"""

import math
import struct

import pytest

from lite.audio import (
    LiteASR,
    LiteTTS,
    LiteVAD,
    LiteVoicePipeline,
    MockASRBackend,
    MockTTSBackend,
    ShouldReplyJudge,
    HeuristicJudge,
    judge_should_reply_text,
)
from lite.audio.pipeline import OFFLINE_HINT

SR = 16000


# ------------------------------------------------------------------ #
# Mock 组件                                                          #
# ------------------------------------------------------------------ #

class FakeVAD:
    """恒触发"判结束"的 VAD：每次返回 True，不关心音频内容。"""

    def __init__(self):
        self.reset_calls = 0

    def reset(self):
        self.reset_calls += 1

    def detect_speech_end(self, chunk):
        return True


class FakeCloud:
    """可控在线状态与流式回复的云端 mock。"""

    def __init__(self, online=True, chunks=("你", "好")):
        self.online = online
        self.chunks = list(chunks)
        self.chat_calls = 0
        self.last_messages = None

    def is_online(self, timeout=5):
        return bool(self.online)

    def chat(self, messages):
        self.chat_calls += 1
        self.last_messages = list(messages)
        yield from self.chunks


class FakeJudge(ShouldReplyJudge):
    """可控就绪位与判定结果的判定器 mock。"""

    def __init__(self, ready=True, result=True):
        self._ready = ready
        self._result = result
        self.judge_calls = 0

    @property
    def model_ready(self):
        return self._ready

    def judge(self, user_text):
        self.judge_calls += 1
        return self._result


#: 固定合成返回的音频字节（供断言引用相等）。
CAN_AUDIO = b"mock-melotts-wav"


def _asr(text):
    """构造返回固定文本的 LiteASR。"""
    return LiteASR(backend=MockASRBackend(text=text))


def _tts():
    """构造返回固定字节的 LiteTTS。"""
    return LiteTTS(backend=MockTTSBackend(audio=CAN_AUDIO))


def _pipeline(**kw):
    """构造全 mock 的 LiteVoicePipeline，缺省为「在线回复」组合。"""
    defaults = dict(vad=FakeVAD(), asr=_asr("帮我查天气"), tts=_tts(),
                    cloud=FakeCloud())
    defaults.update(kw)
    return LiteVoicePipeline(**defaults)


# ------------------------------------------------------------------ #
# 1. 完整链路                                                        #
# ------------------------------------------------------------------ #

def test_full_link_cloud_reply_updates_history():
    """VAD 判结束→ASR→判定→云端流式→TTS：返回 text/audio 且历史更新。"""
    cloud = FakeCloud(chunks=("好的", "，已帮你", "查好天气"))
    pipe = _pipeline(asr=_asr("帮我查天气"), cloud=cloud)

    out = pipe.feed_audio(b"audio-chunk")

    assert out == {"should_reply": True, "text": "好的，已帮你查好天气",
                   "audio": CAN_AUDIO}
    # 云端收到含用户消息的 messages
    assert cloud.last_messages == [{"role": "user", "content": "帮我查天气"}]
    # 历史已写入 user + assistant
    assert pipe.messages == [
        {"role": "user", "content": "帮我查天气"},
        {"role": "assistant", "content": "好的，已帮你查好天气"},
    ]


def test_feed_audio_none_when_vad_not_trigger():
    """VAD 未判结束时，feed_audio 返回 None（仍在听，不产生任何副作用）。"""
    vad = FakeVAD()
    vad.detect_speech_end = lambda chunk: False
    cloud = FakeCloud()
    pipe = _pipeline(vad=vad, cloud=cloud, asr=_asr("帮"))
    assert pipe.feed_audio(b"x") is None
    assert cloud.chat_calls == 0
    assert pipe.messages == []


def test_empty_transcript_returns_false():
    """转写文本为空时不回复（不产生回复链路）。"""
    cloud = FakeCloud()
    pipe = _pipeline(asr=_asr(""), cloud=cloud)
    assert pipe.feed_audio(b"x") == {"should_reply": False}
    assert cloud.chat_calls == 0


# ------------------------------------------------------------------ #
# 2. 自言自语（判定 False）                                          #
# ------------------------------------------------------------------ #

def test_self_talk_no_reply_no_cloud_no_tts():
    """判定 False 时 should_reply False，且不调用 cloud / tts / 写入历史。"""
    judge = FakeJudge(ready=True, result=False)
    tts = _tts()
    cloud = FakeCloud()
    pipe = _pipeline(judge=judge, cloud=cloud, tts=tts)

    out = pipe.feed_audio(b"x")

    assert out == {"should_reply": False}
    assert judge.judge_calls == 1
    assert cloud.chat_calls == 0
    assert pipe.messages == []


# ------------------------------------------------------------------ #
# 3. 默认态（不注入判定器）                                          #
# ------------------------------------------------------------------ #

def test_default_reply_without_judge():
    """未注入判定器时走默认态：should_reply True（不哑巴）。"""
    cloud = FakeCloud(chunks=("你好",))
    pipe = _pipeline(judge=None, cloud=cloud, asr=_asr("有人在吗"))
    out = pipe.feed_audio(b"x")
    assert out["should_reply"] is True
    assert out["text"] == "你好"
    assert out["audio"] == CAN_AUDIO


def test_default_reply_when_judge_not_ready():
    """判定器已注入但 model_ready=False 时同样走默认态回复。"""
    judge = FakeJudge(ready=False, result=False)
    cloud = FakeCloud(chunks=("回",))
    pipe = _pipeline(judge=judge, cloud=cloud)
    out = pipe.feed_audio(b"x")
    assert out["should_reply"] is True
    # 模型未就绪：不调用 judge.judge
    assert judge.judge_calls == 0


# ------------------------------------------------------------------ #
# 4. 离线                                                            #
# ------------------------------------------------------------------ #

def test_offline_uses_injected_offline_local():
    """cloud 离线且注入 offline_local → 走本地兜底。"""
    cloud = FakeCloud(online=False)

    def offline_local(messages):
        return "本地回复内容"

    pipe = _pipeline(cloud=cloud, offline_local=offline_local)
    out = pipe.feed_audio(b"x")
    assert out["should_reply"] is True
    assert out["text"] == "本地回复内容"
    assert out["audio"] == CAN_AUDIO
    # 真实回复计历史
    assert pipe.messages == [
        {"role": "user", "content": "帮我查天气"},
        {"role": "assistant", "content": "本地回复内容"},
    ]


def test_offline_no_offline_local_returns_hint():
    """cloud 离线且未注入 offline_local → 返回提示文案（仍走 TTS 播报）。"""
    pipe = _pipeline(cloud=FakeCloud(online=False), offline_local=None)
    out = pipe.feed_audio(b"x")
    assert out["should_reply"] is True
    assert "当前离线" in out["text"]
    assert out["audio"] == CAN_AUDIO
    # 提示文案不计入历史（仅有 user，无 assistant）
    assert pipe.messages == [{"role": "user", "content": "帮我查天气"}]


def test_offline_local_can_be_streaming_generator():
    """offline_local 返回流式文本块迭代器时也能拼接。"""
    cloud = FakeCloud(online=False)

    def offline_local(messages):
        yield "本"
        yield "地"

    pipe = _pipeline(cloud=cloud, offline_local=offline_local)
    out = pipe.feed_audio(b"x")
    assert out["text"] == "本地"


# ------------------------------------------------------------------ #
# 5. 历史滚动                                                        #
# ------------------------------------------------------------------ #

def test_history_rolls_to_twenty():
    """超过 20 条时裁剪最旧的条目，保留最近 20 条。"""
    pipe = _pipeline(cloud=FakeCloud(chunks=("回",)))
    # 每轮 feed 写入 user + assistant 共 2 条；11 轮=22 条 → 裁剪到 20
    for _ in range(11):
        out = pipe.feed_audio(b"x")
        assert out["should_reply"] is True
    assert len(pipe.messages) == 20
    total = 22
    assert len(pipe.messages) == min(total, 20)
    # 最新条是 assistant 且内容为 "回"
    assert pipe.messages[-1] == {"role": "assistant", "content": "回"}


# ------------------------------------------------------------------ #
# 6. 判定出口（judge_should_reply_text）                             #
# ------------------------------------------------------------------ #

def test_judge_export_default_when_none():
    """judge 为 None 时默认回复 True。"""
    assert judge_should_reply_text("随便说说", None) is True


def test_judge_export_default_when_not_ready():
    """judge 未就绪时默认回复 True。"""
    assert judge_should_reply_text("随便说说", FakeJudge(ready=False)) is True


def test_judge_export_passthrough_result():
    """judge 就绪时透传判定结果。"""
    assert judge_should_reply_text("帮个忙", FakeJudge(ready=True, result=True)) is True
    assert judge_should_reply_text("嗯嗯", FakeJudge(ready=True, result=False)) is False


# ------------------------------------------------------------------ #
# 7. HeuristicJudge 规则                                             #
# ------------------------------------------------------------------ #

@pytest.mark.parametrize(
    "text,expected",
    [
        ("帮我带个外卖", True),   # 含「帮我」
        ("你在家吗", True),        # 含「你」
        ("请问几点了", True),      # 含「请问」
        ("今天天气呢", True),      # 疑问句尾「呢」
        ("能不能快点", True),      # 含「能不能」
        ("谢谢你", True),          # 含「您」→「你」命中
        ("随便看看", False),       # 无关键词且非疑问
        ("天气不错", False),       # 陈述句
        ("", False),               # 空文本
    ],
)
def test_heuristic_judge_rules(text, expected):
    """HeuristicJudge 就绪时按规则判定。"""
    assert HeuristicJudge(judge_model_ready=True).judge(text) is expected


def test_heuristic_not_ready_default_reply():
    """HeuristicJudge 未就绪时默认回复 True（不哑巴）。"""
    judge = HeuristicJudge(judge_model_ready=False)
    assert judge.model_ready is False
    assert judge._backend is None  # 未就绪不加载任何后端
    assert judge.judge("随便说说") is True


def test_heuristic_ready_has_no_backend_load():
    """HeuristicJudge 就绪位仅占位：backend 仍为 None（C1 前无真实模型）。"""
    judge = HeuristicJudge(judge_model_ready=True)
    assert judge.model_ready is True
    assert judge._backend is None


# ------------------------------------------------------------------ #
# 8. 包导出                                                          #
# ------------------------------------------------------------------ #

def test_package_exports_new_symbols():
    """amt：LiteVoicePipeline / ShouldReplyJudge / HeuristicJudge 可由 lite.audio 导入。"""
    from lite.audio import LiteVoicePipeline as P, \
        ShouldReplyJudge as S, HeuristicJudge as H
    assert P is LiteVoicePipeline
    assert S is ShouldReplyJudge
    assert H is HeuristicJudge


# ------------------------------------------------------------------ #
# 9. E2E：多轮音频流走通全链路（B2.2）                              #
# ------------------------------------------------------------------ #

def _silence(dur_ms):
    """生成 dur_ms 的纯静音 PCM 字节（int16 zero）。"""
    return b"\x00\x00" * int(SR * dur_ms / 1000)


def _speech(dur_ms, amplitude=6000, freq=440):
    """生成 dur_ms 的单频正弦 PCM 字节（int16 little-endian）。"""
    n = int(SR * dur_ms / 1000)
    out = bytearray()
    for i in range(n):
        v = int(amplitude * math.sin(2 * math.pi * freq * i / SR))
        out += struct.pack("<h", v)
    return bytes(out)


class LoopingASRBackend:
    """按调用次数循环返回不同转写文本的定序 ASR 后端。"""

    def __init__(self, texts):
        self.texts = list(texts)
        self.n = 0

    def transcribe(self, audio):
        text = self.texts[self.n % len(self.texts)]
        self.n += 1
        return {"text": text, "emotion": "", "event": None}


def test_e2e_multi_round_audio_stream(capsys):
    """B2.2 E2E：真实 LiteVAD + 轮换 ASR + mock 云/TTS，多轮音频流走通全链路。

    三段口语：'帮我介绍下你自己'（应回复）→ '今天天气不错'（自言自语，不回）→
    '请问几点下课'（应回复）。验证判定是否回复对多轮流的真实演算与历史滚动。
    """
    vad = LiteVAD(silence_ms=300)
    asr = LiteASR(backend=LoopingASRBackend(
        ["帮我介绍下你自己", "今天天气不错", "请问几点下课"]))
    tts = _tts()
    cloud = FakeCloud(chunks=("好的", "。"))
    judge = HeuristicJudge(judge_model_ready=True)

    pipe = LiteVoicePipeline(vad=vad, asr=asr, tts=tts, cloud=cloud, judge=judge)
    pipe.start_session()

    results = []
    for round_no in range(3):
        # 说话帧：未判结束 → None
        assert pipe.feed_audio(_speech(200)) is None
        # 静音帧：累计超 300ms → 触发结束，走完一轮
        out = pipe.feed_audio(_silence(600))
        results.append(out)
        print(f"[E2E] 第{round_no + 1}轮 text='{out.get('text', '')}' "
              f"should_reply={out.get('should_reply')}")

    # 第 1、3 轮应回复（帮我 / 请问）；第 2 轮自言自语不回
    assert results[0]["should_reply"] is True
    assert results[1]["should_reply"] is False
    assert results[2]["should_reply"] is True
    # 回复内容为云端两块流式拼接
    assert results[0]["text"] == "好的。"
    assert results[2]["text"] == "好的。"
    # audio 为 TTS 合成字节
    assert results[0]["audio"] == CAN_AUDIO
    # 云端仅被 2 轮应回复调用
    assert cloud.chat_calls == 2
    # 历史 = 2 轮有效对话（user + assistant 各 1）= 4 条
    assert len(pipe.messages) == 4
    assert pipe.messages[0] == {"role": "user", "content": "帮我介绍下你自己"}
    assert pipe.messages[-1] == {"role": "assistant", "content": "好的。"}

    print("[E2E] 最终对话历史：" + repr(pipe.messages))


# ------------------------------------------------------------------ #
# 10. H3 话语缓冲                                                     #
# ------------------------------------------------------------------ #

class ScriptedVAD:
    """按脚本逐帧返回结束信号的 VAD mock（True=判结束；耗尽后重复末项）。"""

    def __init__(self, script):
        self.script = list(script)
        self.i = 0

    def reset(self):
        """复位回放进度（对齐 LiteVAD 单次触发后复位的语义）。"""
        self.i = 0

    def detect_speech_end(self, chunk):
        idx = min(self.i, len(self.script) - 1)
        self.i += 1
        return bool(self.script[idx])


class RecordingASRBackend:
    """记录每次收到的音频字节并返回固定文本的 ASR 后端 mock。"""

    def __init__(self, text="收到"):
        self.text = text
        self.frames = []

    def transcribe(self, audio):
        self.frames.append(audio)
        return {"text": self.text, "emotion": "", "event": None}


def test_utterance_buffer_transcribes_full_concatenation():
    """连续喂多段非静音 chunk 后喂 end 帧：ASR 收到完整拼接字节（H3）。"""
    backend = RecordingASRBackend(text="帮我查天气")
    pipe = _pipeline(vad=ScriptedVAD([False, False, True]),
                     asr=LiteASR(backend=backend))

    assert pipe.feed_audio(b"a") is None       # 非 end 帧：入缓冲
    assert pipe.feed_audio(b"bb") is None      # 非 end 帧：入缓冲
    out = pipe.feed_audio(b"ccc")              # end 帧：整段转写

    assert out["should_reply"] is True
    # ASR 收到 = 缓冲累积各段 + 当前 end 帧的拼接结果（含触发帧的语义选择见 pipeline 注释）
    assert backend.frames == [b"a" + b"bb" + b"ccc"]
    # 触发后缓冲已清空
    assert pipe._utterance_buffer == []


def test_utterance_buffer_byte_length_at_least_sum_of_chunks():
    """验收口径：ASR 收到的字节长度 >= 各非静音段之和（等于含尾帧的精确拼接）。"""
    backend = RecordingASRBackend()
    chunks = [b"x" * 1600, b"y" * 3200, b"z" * 800]
    total = sum(len(c) for c in chunks)
    tail_silence = b"\x00" * 100
    pipe = _pipeline(vad=ScriptedVAD([False, False, False, True]),
                     asr=LiteASR(backend=backend))
    for c in chunks:
        assert pipe.feed_audio(c) is None
    out = pipe.feed_audio(tail_silence)

    got = backend.frames[0]
    assert len(got) >= total          # 合理比例口径：不低于各段之和
    assert got == b"".join(chunks) + tail_silence   # 精确拼接结果
    assert out["should_reply"] is True


def test_first_frame_is_end_keeps_legacy_behavior():
    """首帧即 end（无前文语音）：保持旧行为，仅转写当前触发帧。"""
    backend = RecordingASRBackend(text="hmm")
    pipe = _pipeline(vad=ScriptedVAD([True]), asr=LiteASR(backend=backend))
    out = pipe.feed_audio(b"only")
    assert out["should_reply"] is True
    assert backend.frames == [b"only"]


class ToggleVAD:
    """手动开关结束信号的 VAD mock；reset 回监听态（供 start_session 路径使用）。"""

    def __init__(self):
        self.end_signal = False

    def reset(self):
        self.end_signal = False

    def detect_speech_end(self, chunk):
        e = bool(self.end_signal)
        if e:
            self.end_signal = False  # 触发一次即复位（对齐 LiteVAD 单次触发语义）
        return e


def test_start_session_clears_utterance_buffer():
    """start_session 必须一并清空话语缓冲，防止上一轮残留跨轮污染（H3）。"""
    backend = RecordingASRBackend()
    vad = ToggleVAD()
    pipe = _pipeline(vad=vad, asr=LiteASR(backend=backend))

    assert pipe.feed_audio(b"stale") is None   # 残留在缓冲里
    assert pipe._utterance_buffer == [b"stale"]
    pipe.start_session()
    assert pipe._utterance_buffer == []        # 会话复位清空缓冲

    # 新一轮：手动给出结束信号 → 首帧即 end → 只转写该帧，不含 stale 残留
    vad.end_signal = True
    out = pipe.feed_audio(b"fresh")
    assert backend.frames == [b"fresh"]
    assert out["should_reply"] is True


# ------------------------------------------------------------------ #
# 11. M10 异常兜底                                                    #
# ------------------------------------------------------------------ #

class BoomChatCloud(FakeCloud):
    """chat 流式调用抛异常的云端 mock。"""

    def chat(self, messages):
        raise RuntimeError("网络已断")


def test_cloud_chat_exception_falls_back_to_offline_local(capsys):
    """cloud.chat 抛异常时自动降级 offline_local，循环不中断（M10 三级降级）。"""
    def offline_local(messages):
        return "本地兜底回复"

    pipe = _pipeline(cloud=BoomChatCloud(online=True), offline_local=offline_local)
    out = pipe.feed_audio(b"x")

    assert out["should_reply"] is True
    assert out["text"] == "本地兜底回复"
    # 本地兜底的真实回复计入历史
    assert pipe.messages[-1] == {"role": "assistant", "content": "本地兜底回复"}
    assert "降级" in capsys.readouterr().out


def test_cloud_and_offline_local_both_fail_returns_hint(capsys):
    """云端与本地兜底均抛异常时回落 OFFLINE_HINT 文案（不入历史，TTS 照常）。"""
    def boom_local(messages):
        raise RuntimeError("本地也炸了")

    pipe = _pipeline(cloud=BoomChatCloud(online=True), offline_local=boom_local)
    out = pipe.feed_audio(b"x")

    assert out["should_reply"] is True
    assert out["text"] == OFFLINE_HINT
    assert out["audio"] == CAN_AUDIO            # 提示文案照常 TTS 播报
    assert pipe.messages == [{"role": "user", "content": "帮我查天气"}]  # 不入历史
    assert "本地兜底生成失败" in capsys.readouterr().out


def test_asr_exception_returns_structured_false_and_loop_survives():
    """asr.transcribe 抛异常时返回结构化 should_reply=False 且后续轮次存活。"""
    class BoomASRBackend:
        def transcribe(self, audio):
            raise RuntimeError("识别引擎崩溃")

    vad = ToggleVAD()
    vad.end_signal = True
    pipe = _pipeline(vad=vad, asr=LiteASR(backend=BoomASRBackend()))

    out = pipe.feed_audio(b"x")
    assert out == {"should_reply": False}
    assert pipe.messages == []                  # 未写入任何历史

    pipe.asr = _asr("帮")                       # ASR 恢复可用
    vad.end_signal = True
    out2 = pipe.feed_audio(b"x")
    assert out2["should_reply"] is True         # 循环未中断，正常走完回复链


def test_tts_exception_returns_text_with_empty_audio():
    """tts.synthesize 抛异常时仍返回完整文本回复，audio 为空 bytes（不哑巴）。"""
    class BoomTTSBackend:
        def synthesize(self, text, voice="cx-open"):
            raise RuntimeError("合成器故障")

    cloud = FakeCloud(chunks=("好的", "。"))
    pipe = _pipeline(tts=LiteTTS(backend=BoomTTSBackend()), cloud=cloud)

    out = pipe.feed_audio(b"x")

    assert out["should_reply"] is True
    assert out["text"] == "好的。"              # 文本回复不受 TTS 故障影响
    assert out["audio"] == b""
    # 真实回复正常计入历史
    assert pipe.messages[-1] == {"role": "assistant", "content": "好的。"}


def test_custom_judge_exception_defaults_to_reply():
    """自定义判定器连 model_ready 属性都抛异常时，双保险兜底为默认回复（M10）。"""
    class ToxicJudge(ShouldReplyJudge):
        @property
        def model_ready(self):
            raise RuntimeError("毒属性实现")

        def judge(self, user_text):
            return False

    cloud = FakeCloud(chunks=("你好",))
    pipe = _pipeline(judge=ToxicJudge(), cloud=cloud)
    out = pipe.feed_audio(b"x")
    assert out["should_reply"] is True          # 判定越界也默认回复（不哑巴）