# -*- coding: utf-8 -*-
"""LiteTTS：本地语音合成，默认 MeloTTS + cx-open 音色。

对齐工程文档 §7.3：``synthesize(text, voice="cx-open") -> bytes``（wav/pcm 音频字节）。
- ``TTSBackend``：抽象基类，定义统一合成契约。
- ``MeloTTSBackend``：可选导入 melotts 的官方后端；melotts 未安装时抛
  ``RuntimeError``（附准确安装指引），音色目录统一推导自 ``data/voices/<voice>``；
  默认音色 ``cx-open`` 依赖 MeloTTS 官方模型（首次使用联网自动下载）。
- ``MockTTSBackend``：测试用后端，返回已知字节。
"""

import os
import warnings

# data/ 目录推导统一收敛到 asr.data_dir（三级 dirname），消除复制漂移。
from lite.audio.asr import data_dir  # noqa: F401  （re-export 供既有引用使用）
from lite.audio.asr import resolve_torch_device
from lite.audio.voice_manager import DEFAULT_VOICE_ID, is_unsafe_voice_id

__all__ = ["TTSBackend", "MeloTTSBackend", "MockTTSBackend", "LiteTTS", "data_dir"]


def _audio_to_pcm16(audio):
    """把 float 音频数组转换为 16-bit PCM int16 数组（L9：clip 防越界回绕爆音）。

    - 先统一 asarray 为 float32，再乘 32767 后做 [-32768, 32767] 采样值钳制，
      最后 astype(np.int16)；越界样本（如 2.0 / -2.0）不再回绕。
    - numpy 在函数内延迟导入（本仓 tts 未强依赖 numpy 场景保持可构造）。

    :param audio: 数组形态的合成音频（float32 波形或数值序列）
    :return: np.int16 PCM 数组
    """
    import numpy as np

    return np.clip(
        np.asarray(audio, dtype=np.float32) * 32767.0, -32768, 32767
    ).astype(np.int16)


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

    def __init__(self, default_voice="cx-open", voice_dir="", device="cpu"):
        """初始化后端。

        :param default_voice: 默认音色标识，默认 ``cx-open``
        :param voice_dir: 音色根目录；留空时回退 ``data/voices``
        :param device: 推理设备意图，``"cpu"``(默认)/``"gpu"``/显式 torch 设备串；
            构造时经 ``resolve_torch_device`` 归一化（gpu 不可用自动回落 cpu）
        """
        self.default_voice = default_voice or "cx-open"
        self.voice_dir = voice_dir or os.path.join(data_dir(), "voices")
        self.device = resolve_torch_device(device)
        self._TTSType = None
        #: (config_path, ckpt_path) 组合 → 已构建的 TTS 引擎实例。
        #: MeloTTS 构造需加载 BERT/pinyin/声学权重，代价秒级以上，
        #: 必须按音色配置组合缓存复用，禁止每次合成重建。
        self._engines = {}

    #: 引擎缓存容量上限（M-8，第三轮体检批次3）：每引擎加载百 MB 级权重，
    #: 超限淘汰最旧条目，防止多引擎常驻内存。
    _MAX_CACHED_ENGINES = 3

    def _ensure_lib(self):
        """校验 MeloTTS 库可导入；未安装时抛 RuntimeError（附准确安装指引）。"""
        if self._TTSType is not None:
            return
        try:
            # 官方 PyPI 分发名为 bettamelo/melotts，但模块导入语是 melo（包名 melo.api）
            from melo.api import TTS
        except ImportError as exc:
            raise RuntimeError(
                "需要 MeloTTS 库：pip install -e <MeloTTS源码目录>（含 melo.api 模块），"
                "并将自定义音色放置到 <项目>/data/voices/"
            ) from exc
        self._TTSType = TTS

    def _get_engine(self, config_path=None, ckpt_path=None):
        """取（或惰性构建并缓存）某音色配置组合对应的 TTS 引擎实例（LRU 限容）。"""
        self._ensure_lib()
        key = (config_path or "", ckpt_path or "")
        engine = self._engines.get(key)
        if engine is not None:
            # LRU：命中提升到最新位（dict 保序）
            self._engines[key] = self._engines.pop(key)
            return engine
        engine = self._TTSType(
            language="ZH",
            device=self.device,
            use_hf=True,
            config_path=config_path,
            ckpt_path=ckpt_path,
        )
        self._engines[key] = engine
        while len(self._engines) > self._MAX_CACHED_ENGINES:
            oldest = next(iter(self._engines))
            self._engines.pop(oldest)
            warnings.warn(f"TTS 引擎缓存超过上限 {self._MAX_CACHED_ENGINES}，已淘汰最旧引擎：{oldest}")
        return engine

    def _voice_path(self, voice):
        """推导某音色的模型目录路径：``data/voices/<voice>``（M13：删除永不填充的 _voice_map 死分支）。

        H-2（第三轮体检批次3）：voice 含路径穿越特征（``/``、``\\``、``..``、盘符）
        时返回 None——不再拼接逃逸 ``data/voices/`` 根目录的任意路径，由调用方
        回退默认音色。与 VoiceManager.resolve_voice 的 L13 校验同口径。
        """
        if is_unsafe_voice_id(voice):
            return None
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
        self._ensure_lib()
        _voice = voice or self.default_voice
        # H-2：非法音色标识（路径穿越特征）告警并回退默认音色，不拼接逃逸路径；
        # default_voice 亦非法（构造参数异常）时最终兜底官方默认 cx-open
        if is_unsafe_voice_id(_voice):
            warnings.warn(
                f"音色标识含路径穿越特征，已拒绝并回退官方默认音色：{_voice!r}",
                UserWarning,
                stacklevel=2,
            )
            _voice = self.default_voice
            if is_unsafe_voice_id(_voice):
                _voice = DEFAULT_VOICE_ID
        _voice_path = self._voice_path(_voice)
        cfg = os.path.join(_voice_path, "config.json")
        ckpt = os.path.join(_voice_path, "ckpt.txt")  # 训练产物常见名；视后端而定
        has_cfg = os.path.exists(cfg)
        has_ckpt = os.path.exists(ckpt)
        if not has_cfg and not has_ckpt:
            # G-4 告警：音色目录缺少训练产物，将静默回退官方默认音色——必须显式告警
            warnings.warn(
                f"音色目录缺少 config.json/ckpt.txt，已回退官方默认音色：{_voice_path}",
                UserWarning,
                stacklevel=2,
            )
        tts = self._get_engine(
            config_path=cfg if has_cfg else None,
            ckpt_path=ckpt if has_ckpt else None,
        )
        speaker_id = 0  # 默认说话人；可经 tts.hps.data.spk2id 扩展
        audio = tts.tts_to_file(text, speaker_id, output_path=None, speed=1.0, quiet=True)
        import io

        import soundfile as sf

        sr = tts.hps.data.sampling_rate
        pcm = _audio_to_pcm16(audio)  # L9：clip 钳制在 helper 内完成
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