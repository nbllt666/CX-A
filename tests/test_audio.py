# -*- coding: utf-8 -*-
"""Task B1 语音三件套单元测试。

覆盖：
- LiteVAD：合成"短音 + 长静音"判定结束；持续语音不触发；阈值逻辑
- LiteASR：Mock backend 透传；SenseVoice 未装时代理 import backend 报错路径
- LiteTTS：Mock backend 合成已知字节；默认 voice=cx-open 生效；melotts 未装报错路径
- build_default_pipeline：无三方库回退 mock 并打印 warning
- 工厂 judge 接线（H4）：未配置默认态 None；enabled+runtime 可用为委托包装实例；
  加载失败回退 HeuristicJudge
"""

import math
import struct
import sys
import types

import pytest

from lite.audio import (
    LiteVAD,
    LiteASR,
    LiteTTS,
    MockASRBackend,
    MockTTSBackend,
    SenseVoiceBackend,
    MeloTTSBackend,
    ShouldReplyJudge,
    HeuristicJudge,
    build_default_pipeline,
)

SR = 16000
SW = 2


# ------------------------------------------------------------------ #
# PCM 合成辅助                                                       #
# ------------------------------------------------------------------ #

def _silence(dur_ms):
    """生成 dur_ms 的纯静音 PCM 字节（int16 zero）。"""
    n = int(SR * dur_ms / 1000)
    return b"\x00\x00" * n


def _speech(dur_ms, amplitude=6000, freq=440):
    """生成 dur_ms 的单频正弦 PCM 字节（int16 little-endian）。"""
    n = int(SR * dur_ms / 1000)
    out = bytearray()
    for i in range(n):
        v = int(amplitude * math.sin(2 * math.pi * freq * i / SR))
        out += struct.pack("<h", v)
    return bytes(out)


# ------------------------------------------------------------------ #
# 1. LiteVAD                                                         #
# ------------------------------------------------------------------ #

def test_vad_speech_plus_long_silence_triggers_end():
    """先短音后累计超过 silence_ms 静音，应触发一次结束信号。"""
    vad = LiteVAD(silence_ms=600)
    # 说话
    assert vad.detect_speech_end(_speech(300)) is False
    # 静音未达标
    assert vad.detect_speech_end(_silence(400)) is False
    # 累计到 700ms > 600ms → 结束触发
    assert vad.detect_speech_end(_silence(300)) is True


def test_vad_continuous_speech_never_triggers():
    """持续说话（无长静音）不应触发结束。"""
    vad = LiteVAD(silence_ms=600)
    for _ in range(20):
        assert vad.detect_speech_end(_speech(200)) is False


def test_vad_auto_reset_allows_next_round():
    """触发结束后内部复位，可继续下一轮检测。"""
    vad = LiteVAD(silence_ms=400)
    assert vad.detect_speech_end(_speech(200)) is False
    assert vad.detect_speech_end(_silence(500)) is True  # 结束触发并复位
    # 下一轮：再说话再静音，仍能再次触发
    assert vad.detect_speech_end(_speech(200)) is False
    assert vad.detect_speech_end(_silence(500)) is True


def test_vad_threshold_logic():
    """阈值逻辑：高阈值下低幅信号判为静音不产生说话态；稳定说话仍不触发。"""
    # 幅值 6000 → ≈-17.8dB，低于 -10dB 阈值 → 判为静音，从未进入说话态
    vad = LiteVAD(speech_threshold_dB=-10.0, silence_ms=400)
    assert vad.detect_speech_end(_speech(300, amplitude=6000)) is False
    assert vad.detect_speech_end(_silence(500)) is False

    # 幅值 20000 → ≈-7.3dB，高于 -10dB → 判为说话，稳定说话不触发
    vad2 = LiteVAD(speech_threshold_dB=-10.0, silence_ms=400)
    assert vad2.detect_speech_end(_speech(300, amplitude=20000)) is False


def test_vad_db_of_silence_is_neg_inf():
    """纯静音的 dB 值应为 -inf（等价无限小声）。"""
    assert math.isinf(LiteVAD()._db_of(_silence(100)))


# ------------------------------------------------------------------ #
# 2. LiteASR                                                         #
# ------------------------------------------------------------------ #

def test_asr_mock_passthrough():
    """Mock backend 返回的 {text, emotion, event} 应被 LiteASR 透传。"""
    backend = MockASRBackend(text="你好", emotion="HAPPY", event="Speech")
    asr = LiteASR(backend=backend, device="cpu")
    assert asr.transcribe(b"audio") == {
        "text": "你好",
        "emotion": "HAPPY",
        "event": "Speech",
    }


def test_asr_default_backend_is_mock():
    """LiteASR 缺省构造应使用 MockASRBackend。"""
    asr = LiteASR()
    assert isinstance(asr.backend, MockASRBackend)
    assert asr.transcribe(b"x") == {"text": "", "emotion": "", "event": None}


def test_asr_sensevoice_import_error(monkeypatch):
    """SenseVoice 未装时，backend 首次 transcribe 应抛 RuntimeError 提示 funasr。"""
    monkeypatch.setitem(sys.modules, "funasr", None)  # 强制模拟未安装
    backend = SenseVoiceBackend()
    with pytest.raises(RuntimeError, match="funasr"):
        backend.transcribe(b"audio")


# ------------------------------------------------------------------ #
# 3. LiteTTS                                                         #
# ------------------------------------------------------------------ #

def test_tts_mock_returns_bytes():
    """Mock backend 应合成返回已知字节；LiteTTS 透传。"""
    canned = b"RIFF________WAVE:canned"
    tts = LiteTTS(backend=MockTTSBackend(audio=canned), default_voice="cx-open")
    assert tts.synthesize("你好") == canned


def test_tts_default_voice_cx_open():
    """默认 voice 缺省应使用 cx-open；显式传入 voice 应覆盖默认。"""
    calls = []

    class RecordingBackend(MockTTSBackend):
        def synthesize(self, text, voice="cx-open"):
            calls.append(voice)
            return b"wav"

    tts = LiteTTS(backend=RecordingBackend(), default_voice="cx-open")
    tts.synthesize("hi")
    assert calls == ["cx-open"]
    tts.synthesize("hi", voice="other")
    assert calls == ["cx-open", "other"]


def test_tts_melotts_import_error(monkeypatch):
    """melo(MeloTTS) 未装时，backend 首次 synthesize 应抛 RuntimeError 提示 MeloTTS。"""
    monkeypatch.setitem(sys.modules, "melo.api", None)  # 强制模拟未安装
    backend = MeloTTSBackend()
    with pytest.raises(RuntimeError, match="MeloTTS"):
        backend.synthesize("你好")


# ------------------------------------------------------------------ #
# 4. 工厂                                                           #
# ------------------------------------------------------------------ #

def test_factory_falls_back_to_mock(monkeypatch, capsys):
    """无三方库（funasr / melotts 均不可用）时应回退 mock 并打印 warning。"""
    monkeypatch.setitem(sys.modules, "funasr", None)
    monkeypatch.setitem(sys.modules, "melo.api", None)
    cfg = {"asr": {"device": "cpu"}, "tts": {"voice": "cx-open"}, "vad": {}}
    pipe = build_default_pipeline(cfg)
    assert isinstance(pipe["vad"], LiteVAD)
    assert isinstance(pipe["asr"], LiteASR)
    assert isinstance(pipe["tts"], LiteTTS)
    assert isinstance(pipe["asr"].backend, MockASRBackend)
    assert isinstance(pipe["tts"].backend, MockTTSBackend)
    assert "回退" in capsys.readouterr().out


# ------------------------------------------------------------------ #
# 5. 工厂 judge 接线（H4）                                            #
# ------------------------------------------------------------------ #

def test_factory_includes_judge_key_disabled_by_default():
    """local_llm 未配置时 judge 键存在且为默认态 None（既有行为不变）。"""
    cfg = {"asr": {"device": "cpu"}, "tts": {}, "vad": {}}
    pipe = build_default_pipeline(cfg)
    assert "judge" in pipe
    assert pipe["judge"] is None


def test_factory_judge_enabled_with_runtime(monkeypatch):
    """enabled=True 且 runtime 可用（fake LlamaRuntime 注入）时 judge 为
    包装实例且委托 judge_should_reply 生效。"""

    class FakeRuntime:
        """可判定的 fake LlamaRuntime：load 恒成功，判定按前缀返回。"""

        def __init__(self, config=None):
            self.config = config

        def load_local_llm(self, path):
            return True

        def judge_should_reply(self, text):
            return str(text).startswith("帮我")

    fake_mod = types.ModuleType("lite.runtime.llama_runtime")
    fake_mod.LlamaRuntime = FakeRuntime
    monkeypatch.setitem(sys.modules, "lite.runtime.llama_runtime", fake_mod)

    cfg = {"local_llm": {"enabled": True, "model_path": "D:/models/fake.gguf"}}
    pipe = build_default_pipeline(cfg)

    judge = pipe["judge"]
    assert isinstance(judge, ShouldReplyJudge)   # 包装实例符合契约
    assert judge.model_ready is True
    assert judge.judge("帮我带杯咖啡") is True    # 委托 fake runtime 判定
    assert judge.judge("随便看看") is False


def test_factory_judge_enabled_load_failure_falls_back(monkeypatch, capsys):
    """模型加载失败（load_local_llm 返回 False）时打 warning 并回退 HeuristicJudge。"""

    class DeadRuntime:
        """加载恒失败的 fake LlamaRuntime。"""

        def __init__(self, config=None):
            self.warnings = ["GGUF 文件不存在"]

        def load_local_llm(self, path):
            return False

    fake_mod = types.ModuleType("lite.runtime.llama_runtime")
    fake_mod.LlamaRuntime = DeadRuntime
    monkeypatch.setitem(sys.modules, "lite.runtime.llama_runtime", fake_mod)

    cfg = {"local_llm": {"enabled": True, "model_path": "D:/missing/model.gguf"}}
    pipe = build_default_pipeline(cfg)

    assert isinstance(pipe["judge"], HeuristicJudge)
    assert "接线失败" in capsys.readouterr().out


def test_factory_judge_real_missing_model_falls_back():
    """真机冒烟：未装 llama-cpp / 模型文件缺失 → 延迟导入链不炸、回退 HeuristicJudge。"""
    cfg = {"local_llm": {"enabled": True, "model_path": "Z:/definitely-missing/x.gguf"}}
    pipe = build_default_pipeline(cfg)
    assert isinstance(pipe["judge"], HeuristicJudge)


def test_factory_judge_enabled_without_model_path_stays_none():
    """enabled=True 但未配置 model_path：无法接线，保持默认态 None。"""
    cfg = {"local_llm": {"enabled": True}}
    pipe = build_default_pipeline(cfg)
    assert pipe["judge"] is None


# ------------------------------------------------------------------ #
# L9：PCM float32→int16 越界钳制                                      #
# ------------------------------------------------------------------ #

def test_tts_pcm_clip_out_of_range_samples():
    """越界样本（2.0/-2.0）clip 后不回绕：32767 / -32768，dtype 为 int16。"""
    np = pytest.importorskip("numpy")
    from lite.audio.tts import _audio_to_pcm16

    pcm = _audio_to_pcm16([2.0, -2.0])
    assert int(pcm[0]) == 32767
    assert int(pcm[1]) == -32768
    assert pcm.dtype == np.int16


def test_tts_pcm_conversion_preserves_in_range_values():
    """区间内样本转换保持保真（0.5 → 约 16383）。"""
    pytest.importorskip("numpy")
    from lite.audio.tts import _audio_to_pcm16

    pcm = _audio_to_pcm16([0.0, 0.5, -0.5])
    assert pcm.tolist() == [0, 16383, -16383]


# ------------------------------------------------------------------ #
# L12：ASR 输入健壮化与结果双形态解析                                  #
# ------------------------------------------------------------------ #

class _FakeFunasrModel:
    """可捕获 generate 入参并按预设返回形态响应的 fake 模型。"""

    def __init__(self, result):
        self.result = result
        self.captured_kwargs = {}

    def generate(self, input=None, **kwargs):
        self.captured_kwargs["input"] = input
        self.captured_kwargs.update(kwargs)
        return self.result


_FLAT_RESULT = [{"text": "<|zh|><|NEUTRAL|><|Speech|>你好"}]
_NESTED_RESULT = [[{"text": "<|zh|><|HAPPY|><|Laughter|>在吗"}]]


def _make_loaded_backend(fake_model):
    backend = SenseVoiceBackend()
    backend._model = fake_model
    backend._loaded = True  # 跳过 funasr 真实加载
    return backend


def test_asr_sensevoice_bytes_converted_and_flat_dict_parsed():
    """bytes 输入转 float32 波形投递 generate；平铺 dict-list 结果解析正确。"""
    np = pytest.importorskip("numpy")
    model = _FakeFunasrModel(_FLAT_RESULT)
    backend = _make_loaded_backend(model)

    out = backend.transcribe(struct.pack("<hh", 32767, -32768))

    arr = model.captured_kwargs["input"]
    assert isinstance(arr, np.ndarray)
    assert arr.dtype == np.float32
    assert arr.tolist() == pytest.approx([32767 / 32768.0, -1.0])
    assert out["text"] == "你好"
    assert out["emotion"] == "NEUTRAL"
    assert out["event"] == "Speech"


def test_asr_sensevoice_nested_dict_list_parsed():
    """嵌套双层形态 res[0][0]["text"] 解析正确。"""
    pytest.importorskip("numpy")
    backend = _make_loaded_backend(_FakeFunasrModel(_NESTED_RESULT))

    out = backend.transcribe([0.1, -0.2])

    assert out["text"] == "在吗"
    assert out["emotion"] == "HAPPY"
    assert out["event"] == "Laughter"


def test_asr_sensevoice_ndarray_input_passthrough_float32(capsys):
    """ndarray 输入按需 asarray 为 float32；解析异常路径打印告警不静默。"""
    np = pytest.importorskip("numpy")
    # 返回未识别形态 → 兜底空文本且打印 ERROR 告警
    model = _FakeFunasrModel({"unexpected": "shape"})
    backend = _make_loaded_backend(model)

    wave = np.array([0.5, -0.5], dtype=np.int16).astype(np.float64)  # 非 float32 输入
    out = backend.transcribe(wave)

    assert model.captured_kwargs["input"].dtype == np.float32
    assert out == {"text": "", "emotion": "", "event": None}
    assert "[ERROR]" in capsys.readouterr().out


def test_asr_sensevoice_empty_result_warns_not_silent(capsys):
    """funasr 返回空列表时打印告警并返回空结构，不再静默吞掉失败。"""
    pytest.importorskip("numpy")
    backend = _make_loaded_backend(_FakeFunasrModel([]))
    out = backend.transcribe(b"\x00\x00")
    assert out == {"text": "", "emotion": "", "event": None}
    assert "ASR" in capsys.readouterr().out