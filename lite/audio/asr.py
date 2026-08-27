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

__all__ = [
    "ASRBackend",
    "SenseVoiceBackend",
    "MockASRBackend",
    "LiteASR",
    "resolve_torch_device",
    "data_dir",
]


def data_dir():
    """推导本项目 ``data/`` 目录（存放模型 / 音色等资产）。

    本文件位于 ``<root>/lite/audio/asr.py``，逐级向上取三次 dirname 即得
    ``<root>``，再拼接 ``data`` 得 ``<root>/data``。路径推导一律基于
    ``os.path.dirname(os.path.abspath(__file__))``，禁止相对路径。
    """
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data"
    )


def resolve_torch_device(device="cpu"):
    """把配置的设备意图归一化为 torch 推理设备串（TTS/ASR 共用）。

    规则（与 llama.cpp 侧 device 键语义统一）：
    - ``"gpu"``：``torch.cuda.is_available()`` 为真返回 ``"cuda"``；
      CUDA 不可用或 torch 未安装时打印告警并回落 ``"cpu"``（不崩溃）；
    - 其余值原样放行（兼容 ``"cpu"`` / ``"cuda:0"`` 等显式 torch 设备串）；
    - None / 空串回 ``"cpu"``；大小写不敏感。
    torch 延迟导入——未装 torch 的环境（如当前 3.14 态）构造不报错。

    :param device: 配置设备意图（"cpu"/"gpu"/显式 torch 设备串）
    :return: str，可直接传给 torch 生态模型构造的设备串
    """
    d = str(device or "cpu").strip().lower() or "cpu"
    if d != "gpu":
        return d
    try:
        import torch
    except ImportError:
        print("[WARN] 配置 device=gpu 但 torch 未安装，回落 cpu")
        return "cpu"
    if torch.cuda.is_available():
        return "cuda"
    print("[WARN] 配置 device=gpu 但 CUDA 不可用，回落 cpu")
    return "cpu"


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
        :param device: 推理设备意图，``"cpu"``(默认)/``"gpu"``/显式 torch 设备串；
            构造时经 ``resolve_torch_device`` 归一化（gpu 不可用自动回落 cpu）
        """
        self.model_path = model_path or os.path.join(data_dir(), "SenseVoiceSmall")
        self.device = resolve_torch_device(device)
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
        """识别音频并返回 ``{text, emotion, event}``。

        L12：bytes（裸 int16 PCM）先转换为 funasr 公开支持的 numpy 波形数组
        （float32，取值 [-1, 1]）再投递 generate；ndarray/list 按需 asarray 为
        float32。numpy 延迟导入。
        """
        self._load()
        res = self._model.generate(input=_coerce_to_waveform(audio), language="auto", use_itn=True)
        return _parse_funasr_result(res)


def _coerce_to_waveform(audio):
    """把各类音频输入正规化为 funasr 可靠消费的 numpy 波形数组（L12）。

    - bytes / bytearray：按裸 int16 PCM 解析，转 float32 并归一化到 [-1, 1]
      （funasr 对裸 PCM bytes 解析不可靠，numpy 波形是其公开支持形态）；
    - ndarray：按需转 float32；
    - list/tuple 等序列：asarray 为 float32。
    numpy 延迟导入（本仓 asr 未强依赖 numpy 场景保持可构造）。
    """
    import numpy as np

    if isinstance(audio, (bytes, bytearray)):
        return np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
    return np.asarray(audio, dtype=np.float32)


def _parse_funasr_result(res):
    """把 funasr 输出解析为统一 ``{text, emotion, event}`` dict。

    L12 双形态兼容：
    - 平铺 dict-list：``res[0]`` 直接是 ``{"text": ...}``（funasr 常见形态）；
    - 嵌套双层：``res[0][0]`` 是 ``{"text": ...}``（SenseVoice 富文本旧形态）。
    SenseVoice 富文本文案形如 ``<|zh|><|NEUTRAL|><|Speech|>你好``，
    从 raw text 提取情感与事件标签，产出去掉标签的纯净文本。
    解析失败时打印告警并返回空结果（text=""，emotion=""，event=None）——
    宽 except 兜底保留，但不再静默吞掉全部转写失败。
    """
    import re

    from datetime import datetime

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        first = res[0] if res else None
        if isinstance(first, dict):
            raw = first.get("text", "")
        elif isinstance(first, (list, tuple)) and first:
            raw = (
                first[0].get("text", "")
                if isinstance(first[0], dict)
                else str(first[0])
            )
        else:
            print(f"{timestamp} [ERROR] ASR 结果解析失败：未识别的返回形态 {type(res).__name__}，已回退空文本")
            return {"text": "", "emotion": "", "event": None}
    except Exception as exc:  # noqa: BLE001 - 空/异常结果兜底（含告警防静默）
        print(f"{timestamp} [ERROR] ASR 结果解析异常（返回空文本）：{exc}")
        return {"text": "", "emotion": "", "event": None}

    clean = re.sub(r"<\|.*?\|>", "", str(raw), count=0, flags=re.MULTILINE).strip()
    emo_match = re.search(
        r"<\|(HAPPY|SAD|ANGRY|NEUTRAL|FEARFUL|DISGUSTED|SURPRISED)\|>", str(raw)
    )
    event_match = re.search(
        r"<\|(BGM|Speech|Applause|Laughter|Cry|Sneeze|Breath|Cough|Sing|Speech_Noise)\|>",
        str(raw),
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