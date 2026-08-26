# -*- coding: utf-8 -*-
"""语音对话子包（Task B）：LiteVAD / LiteASR / LiteTTS / 判定 / 全链路编排。

- ``LiteVAD``：原始 VAD，能量法 + 静音计时，只判"用户说完"（结束信号）。
- ``LiteASR``：本地语音识别，返回 ``{text, emotion, event}``。
- ``LiteTTS``：本地语音合成，默认 MeloTTS + cx-open 音色。
- ``ShouldReplyJudge`` / ``HeuristicJudge`` / ``LlmJudge``：是否回复判定器（Task B2）。
- ``judge_should_reply_text``：判定统一出口（默认态兜底，不哑巴）。
- ``LiteVoicePipeline``：语音对话全链路编排（VAD→ASR→判定→回复→TTS，Task B2）。

``build_default_pipeline(config)`` 便捷工厂：按 config 的 asr / tts / vad 段
装配默认三件套；缺少三方库（funasr / melotts）时自动回退 Mock 并打印 warning。
"""

from lite.audio.vad import LiteVAD
from lite.audio.asr import (
    ASRBackend,
    SenseVoiceBackend,
    MockASRBackend,
    LiteASR,
)
from lite.audio.tts import (
    TTSBackend,
    MeloTTSBackend,
    MockTTSBackend,
    LiteTTS,
)
from lite.audio.voice_manager import VoiceManager
from lite.audio.judge import (
    ShouldReplyJudge,
    HeuristicJudge,
    LlmJudge,
    judge_should_reply_text,
)
from lite.audio.pipeline import LiteVoicePipeline

__all__ = [
    "LiteVAD",
    "ASRBackend",
    "SenseVoiceBackend",
    "MockASRBackend",
    "LiteASR",
    "TTSBackend",
    "MeloTTSBackend",
    "MockTTSBackend",
    "LiteTTS",
    "VoiceManager",
    "ShouldReplyJudge",
    "HeuristicJudge",
    "LlmJudge",
    "judge_should_reply_text",
    "LiteVoicePipeline",
    "build_default_pipeline",
]


def _resolve_section(cfg, name):
    """从 config 中取某段的 dict；兼容裸 dict 与 ConfigManager 实例。"""
    if cfg is None:
        return {}
    if isinstance(cfg, dict):
        sec = cfg.get(name, {})
        return sec if isinstance(sec, dict) else {}
    inner = getattr(cfg, "config", None)
    if isinstance(inner, dict):
        sec = inner.get(name, {})
        return sec if isinstance(sec, dict) else {}
    return {}


def _try_sensevoice(asr_cfg):
    """探测 funasr 可用性：可用返回 SenseVoiceBackend，否则返回 None（回退 Mock）。"""
    try:
        import funasr  # noqa: F401
    except ImportError:
        return None
    return SenseVoiceBackend(
        model_path=asr_cfg.get("model_path", ""),
        device=asr_cfg.get("device", "cpu"),
    )


def _try_melotts(tts_cfg):
    """探测 MeloTTS 可用性：可用返回 MeloTTSBackend，否则返回 None（回退 Mock）。"""
    try:
        # 官方 PyPI 分发名 melotts，模块导入语 melo.api
        from melo.api import TTS  # noqa: F401
    except ImportError:
        return None
    return MeloTTSBackend(default_voice=tts_cfg.get("voice", "cx-open"))


def build_default_pipeline(config=None):
    """便捷工厂：按 config 的 asr / tts / vad 段装配默认语音三件套。

    - VAD：纯标准库实现，始终可用，参数取自 ``config["vad"]``（缺省用默认值）。
    - ASR：funasr 可用时用 SenseVoiceBackend；不可用时回退 MockASRBackend 并警告。
    - TTS：melotts 可用时用 MeloTTSBackend；不可用时回退 MockTTSBackend 并警告。

    :param config: 配置（含 asr / tts / vad 三段）；支持裸 dict 或 ConfigManager。
    :return: dict，含 ``vad`` / ``asr`` / ``tts`` 三个已装配实例。
    """
    vad_cfg = _resolve_section(config, "vad")
    asr_cfg = _resolve_section(config, "asr")
    tts_cfg = _resolve_section(config, "tts")

    vad = LiteVAD(
        sample_rate=vad_cfg.get("sample_rate", 16000),
        speech_threshold_dB=vad_cfg.get("speech_threshold_dB", -35.0),
        silence_ms=vad_cfg.get("silence_ms", 600),
    )

    asr_backend = _try_sensevoice(asr_cfg)
    if asr_backend is None:
        asr_backend = MockASRBackend()
        print("[LiteAudio][WARN] funasr 未安装，LiteASR 回退 MockASRBackend")

    tts_backend = _try_melotts(tts_cfg)
    if tts_backend is None:
        tts_backend = MockTTSBackend()
        print("[LiteAudio][WARN] melotts 未安装，LiteTTS 回退 MockTTSBackend")

    return {
        "vad": vad,
        "asr": LiteASR(backend=asr_backend, device=asr_cfg.get("device", "cpu")),
        "tts": LiteTTS(
            backend=tts_backend,
            default_voice=tts_cfg.get("voice", "cx-open"),
        ),
    }