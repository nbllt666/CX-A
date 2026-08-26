# -*- coding: utf-8 -*-
"""Task B1 语音三件套单元测试。

覆盖：
- LiteVAD：合成"短音 + 长静音"判定结束；持续语音不触发；阈值逻辑
- LiteASR：Mock backend 透传；SenseVoice 未装时代理 import backend 报错路径
- LiteTTS：Mock backend 合成已知字节；默认 voice=cx-open 生效；melotts 未装报错路径
- build_default_pipeline：无三方库回退 mock 并打印 warning
"""

import math
import struct
import sys

import pytest

from lite.audio import (
    LiteVAD,
    LiteASR,
    LiteTTS,
    MockASRBackend,
    MockTTSBackend,
    SenseVoiceBackend,
    MeloTTSBackend,
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