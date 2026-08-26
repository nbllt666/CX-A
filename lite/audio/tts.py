# -*- coding: utf-8 -*-
"""LiteTTS：本地语音合成，默认 MeloTTS + cx-open 音色。

对齐工程文档 §7.3：``synthesize(text, voice="cx-open") -> bytes``（wav/pcm 音频字节）。
- ``TTSBackend``：抽象基类，定义统一合成契约。
- ``MeloTTSBackend``：可选导入 melotts 的官方后端；melotts 未安装时抛
  ``RuntimeError``（提示 pip install melotts），voice 映射指向 ``data/voices/``。
- ``MockTTSBackend``：测试用后端，返回已知字节。
"""

import os

__all__ = ["TTSBackend", "MeloTTSBackend", "MockTTSBackend", "LiteTTS", "data_dir"]


def data_dir():
    """推导本项目 ``data/`` 目录（存放模型 / 音色等资产）。

    本文件位于 ``<root>/lite/audio/tts.py``，逐级向上取两次 dirname 即得
    ``<root>/data``。路径推导一律基于
    ``os.path.dirname(os.path.abspath(__file__))``，禁止相对路径。
    """
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


class TTSBackend:
    """语音合成后端抽象基类。"""

    def synthesize(self, text, voice="cx-open"):
        """合成文本为 wav/pcm 音频字节。

        :param text: 待合成文本
        :param voice: 音色标识，默认 ``cx-open``
        :return: bytes 音频字节
        """
        raise NotImplementedError


class MeloTTSBackend(TTSBackend):
    """基于 melotts 的本地合成后端（可选依赖）。

    melotts 未安装时不阻塞构造，仅在首次 ``synthesize``（惰性加载）时抛
    ``RuntimeError`` 提示安装。音色目录默认 ``<root>/data/voices``，
    音色路径映射为 ``data/voices/<voice>``。
    """

    def __init__(self, default_voice="cx-open", voice_dir=""):
        """初始化后端。

        :param default_voice: 默认音色标识，默认 ``cx-open``
        :param voice_dir: 音色根目录；留空时回退 ``data/voices``
        """
        self.default_voice = default_voice or "cx-open"
        self.voice_dir = voice_dir or os.path.join(data_dir(), "voices")
        #: voice 标识 → 音色模型目录映射（默认 cx-open 指向 data/voices/cx-open）
        self._voice_map = {}
        self._engine = None

    def _ensure_engine(self):
        """惰性加载 MeloTTS 引擎；未安装时抛 RuntimeError。"""
        if self._engine is not None:
            return
        try:
            # 官方 PyPI 分发名为 bettamelo/melotts，但模块导入语是 melo（包名 melo.api）
            from melo.api import TTS  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "MeloTTSBackend 需要 MeloTTS 库，请安装：pip install C:\\CX-A\\MeloTTS，"
                "并将音色放置到 data/voices/"
            ) from exc
        self._TTSType = TTS
        self._engine = True

    def _voice_path(self, voice):
        """推导某音色的模型目录路径：``data/voices/<voice>``。"""
        mapped = self._voice_map.get(voice)
        if mapped:
            return mapped
        return os.path.join(self.voice_dir, voice)

    def synthesize(self, text, voice=None):
        """合成文本为 wav 音频字节（真实引擎委托）。

        逻辑：
        - 音色目录 ``data/voices/<voice>`` 下若存在 config.json/ckpt（训练产物），
          则传入 ``config_path/ckpt_path`` 走本地模型；否则走官方默认模型
          （中文默认，首次会自动下载权重）。
        - ``tts_to_file(..., output_path=None)`` 返回 float32 音频数组，
          此处封装为 16-bit PCM WAV 字节并返回。
        """
        self._ensure_engine()
        _voice = voice or self.default_voice
        _voice_path = self._voice_path(_voice)
        cfg = os.path.join(_voice_path, "config.json")
        ckpt = os.path.join(_voice_path, "ckpt.txt")  # 训练产物常见名；视后端而定
        tts = self._TTSType(
            language="ZH",
            device="cpu",
            use_hf=True,
            config_path=cfg if os.path.exists(cfg) else None,
            ckpt_path=ckpt if os.path.exists(ckpt) else None,
        )
        speaker_id = 0  # 默认说话人；可经 tts.hps.data.spk2id 扩展
        audio = tts.tts_to_file(text, speaker_id, output_path=None, speed=1.0, quiet=True)
        import io

        import numpy as np
        import soundfile as sf

        sr = tts.hps.data.sampling_rate
        pcm = (np.asarray(audio, dtype=np.float32) * 32767.0).astype(np.int16)
        buf = io.BytesIO()
        sf.write(buf, pcm, sr, format="WAV")
        return buf.getvalue()


class MockTTSBackend(TTSBackend):
    """测试用合成后端：返回已知字节，可预设 audio。"""

    #: 默认返回的可识别占位字节（非真实 wav，仅用于测试引用相等）。
    _CAN = b"cx-open-mock-tts-wav"

    def __init__(self, audio=None):
        """初始化 Mock 后端。

        :param audio: 预设返回字节；留空使用内置占位字节。
        """
        self.audio = audio if audio is not None else self._CAN

    def synthesize(self, text, voice="cx-open"):
        """直接返回预设字节，不执行真实合成。"""
        return self.audio


class LiteTTS:
    """本地语音合成门面：构造注入 backend，默认 Mock、默认音色 cx-open。

    对齐工程文档 §7.3：``synthesize(text, voice="cx-open") -> bytes``。
    """

    def __init__(self, backend=None, default_voice="cx-open", voice_resolver=None):
        """初始化 LiteTTS。

        :param backend: TTSBackend 实例；缺省使用 MockTTSBackend
        :param default_voice: 默认音色标识，默认 ``cx-open``
        :param voice_resolver: 可调用 ``voice_id -> 路径|None``；非 None 时
            合成前先把音色 id 解析为 MeloTTS 可加载路径（Taxk F2 自定义
            音色）。默认 None 即原样传递，保持既有行为不变。
        """
        self.backend = backend if backend is not None else MockTTSBackend()
        self.default_voice = default_voice or "cx-open"
        self.voice_resolver = voice_resolver

    def synthesize(self, text, voice=None):
        """合成文本为 wav/pcm 音频字节。

        :param text: 待合成文本
        :param voice: 音色标识；留空使用默认音色（cx-open）
        """
        _voice = voice or self.default_voice
        if self.voice_resolver is not None:
            _voice = self.voice_resolver(_voice)
        return self.backend.synthesize(text, _voice)