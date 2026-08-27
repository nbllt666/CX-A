# -*- coding: utf-8 -*-
"""LiteASR：本地语音识别，复用 SenseVoice（funasr），返回 ``{text, emotion, event}``。

对齐工程文档 §7.3：``transcribe`` 返回 ``{"text": str, "emotion": "", "event": None}``。
- ``ASRBackend``：抽象基类，定义统一识别契约。
- ``SenseVoiceBackend``：可选导入 funasr 的官方后端；funasr 未安装时抛
  ``RuntimeError``（提示 pip install funasr），并兼容 ``data/`` 下的模型路径配置。
- ``MockASRBackend``：测试用后端，可预设返回文本 / 情感 / 事件。

默认不实际加载模型（funasr 未安装时测试只用 Mock）。
"""

import os

__all__ = ["ASRBackend", "SenseVoiceBackend", "MockASRBackend", "LiteASR", "data_dir"]


def data_dir():
    """推导本项目 ``data/`` 目录（存放模型 / 音色等资产）。

    本文件位于 ``<root>/lite/audio/asr.py``，逐级向上取三次 dirname 即得
    ``<root>``，再拼接 ``data`` 得 ``<root>/data``。路径推导一律基于
    ``os.path.dirname(os.path.abspath(__file__))``，禁止相对路径。
    """
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data"
    )


class ASRBackend:
    """语音识别后端抽象基类。"""

    def transcribe(self, audio):
        """识别带音频，返回 ``{text, emotion, event}``。

        :param audio: PCM 音频字节
        :return: dict，键为 text / emotion / event
        """
        raise NotImplementedError


class SenseVoiceBackend(ASRBackend):
    """基于 funasr.SenseVoice 的本地识别后端（可选依赖）。

    funasr 未安装时不阻塞构造，仅在首次 ``transcribe``（惰性加载）时抛
    ``RuntimeError`` 提示安装。
    """

    def __init__(self, model_path="", device="cpu"):
        """初始化后端。

        :param model_path: SenseVoice 模型目录；留空时回退 ``data/`` 下默认路径
        :param device: 推理设备，默认 ``cpu``
        """
        self.model_path = model_path or os.path.join(data_dir(), "SenseVoiceSmall")
        self.device = device
        self._model = None
        self._loaded = False

    def _load(self):
        """惰性加载 SenseVoice 模型；funasr 未安装时抛 RuntimeError。"""
        if self._loaded:
            return
        try:
            from funasr import AutoModel
        except ImportError as exc:
            raise RuntimeError(
                "SenseVoiceBackend 需要 funasr 库，请运行：pip install funasr"
            ) from exc
        # 仅在此刻构造模型（延迟加载，构造对象本身不占模型显存/内存）。
        self._model = AutoModel(
            model=self.model_path,
            device=self.device,
            disable_update=True,
        )
        self._loaded = True

    def transcribe(self, audio):
        """识别音频并返回 ``{text, emotion, event}``。"""
        self._load()
        res = self._model.generate(input=audio, language="auto", use_itn=True)
        return _parse_funasr_result(res)


def _parse_funasr_result(res):
    """把 funasr 输出解析为统一 ``{text, emotion, event}`` dict。

    SenseVoice 富文本文案形如 ``<|zh|><|NEUTRAL|><|Speech|>你好``，
    从 raw text 提取情感与事件标签，产出去掉标签的纯净文本。
    解析失败时返回空结果（text=""，emotion=""，event=None）。
    """
    import re

    try:
        raw = res[0][0].get("text", "")
    except Exception:  # noqa: BLE001 - 空/异常结果兜底
        return {"text": "", "emotion": "", "event": None}

    clean = re.sub(r"<\|.*?\|>", "", raw, count=0, flags=re.MULTILINE).strip()
    emo_match = re.search(
        r"<\|(HAPPY|SAD|ANGRY|NEUTRAL|FEARFUL|DISGUSTED|SURPRISED)\|>", raw
    )
    event_match = re.search(
        r"<\|(BGM|Speech|Applause|Laughter|Cry|Sneeze|Breath|Cough|Sing|Speech_Noise)\|>",
        raw,
    )
    return {
        "text": clean,
        "emotion": emo_match.group(1) if emo_match else "",
        "event": event_match.group(1) if event_match else None,
    }


class MockASRBackend(ASRBackend):
    """测试用识别后端：预设返回文本 / 情感 / 事件。"""

    def __init__(self, text="", emotion="", event=None):
        """初始化 Mock 后端并预设返回内容。

        :param text: 预设识别文本
        :param emotion: 预设情感
        :param event: 预设事件（None 表示无）
        """
        self.text = text
        self.emotion = emotion
        self.event = event

    def transcribe(self, audio):
        """直接返回预设结果，不解析音频。"""
        return {"text": self.text, "emotion": self.emotion, "event": self.event}


class LiteASR:
    """本地语音识别门面：构造注入 backend，默认 Mock。

    对齐工程文档 §7.3：返回 ``{text, emotion, event}``。
    """

    def __init__(self, backend=None, device="cpu"):
        """初始化 LiteASR。

        :param backend: ASRBackend 实例；缺省使用 MockASRBackend
        :param device: 推理设备，默认 ``cpu``
        """
        self.backend = backend if backend is not None else MockASRBackend()
        self.device = device

    def transcribe(self, audio):
        """识别音频，返回归一化的 ``{text, emotion, event}``。"""
        result = self.backend.transcribe(audio)
        if not isinstance(result, dict):
            result = {}
        return {
            "text": str(result.get("text", "")),
            "emotion": str(result.get("emotion", "")),
            "event": result.get("event", None),
        }