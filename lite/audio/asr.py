# -*- coding: utf-8 -*-
"""LiteASR：本地语音识别，复用 SenseVoice（funasr），返回 ``{text, emotion, event}``。

对齐工程文档 §7.3：``transcribe`` 返回 ``{"text": str, "emotion": "", "event": None}``。
- ``ASRBackend``：抽象基类，定义统一识别契约。
- ``SenseVoiceBackend``：可选导入 funasr 的官方后端；funasr 未安装时抛
  ``RuntimeError``（提示 pip install funasr），并兼容 ``data/`` 下的模型路径配置。
- ``MockASRBackend``：测试用后端，可预设返回文本 / 情感 / 事件。

默认不实际加载模型（funasr 未安装时测试只用 Mock）。
"""

import base64
import os

__all__ = [
    "ASRBackend",
    "SenseVoiceBackend",
    "BridgeASRBackend",
    "MockASRBackend",
    "LiteASR",
    "resolve_torch_device",
    "data_dir",
]


def data_dir():
    """推导本项目 ``data/`` 目录（存放模型 / 音色等资产）。

    M-14（第三轮体检批次4）：根解析统一收敛到 ``lite.config.paths.app_root()``
    （frozen-aware）——PyInstaller 冻结态下 ``__file__`` 位于
    ``runtime/backend/_internal/``，原 ``__file__`` 上溯推导指向错误位置；
    开发态行为与原实现一致。
    """
    from lite.config.paths import data_root

    return data_root()


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

    def transcribe(self, audio, sample_rate=None):
        """识别带音频，返回 ``{text, emotion, event}``。

        :param audio: PCM 音频字节
        :param sample_rate: 可选输入采样率（Hz）；桥后端据此重采样到 16k
            （SenseVoice 期望值），其余后端可忽略（缺省 None 表示未提供）
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

    def transcribe(self, audio, sample_rate=None):
        """识别音频并返回 ``{text, emotion, event}``。

        L12：bytes（裸 int16 PCM）先转换为 funasr 公开支持的 numpy 波形数组
        （float32，取值 [-1, 1]）再投递 generate；ndarray/list 按需 asarray 为
        float32。numpy 延迟导入。

        :param sample_rate: 可选输入采样率；**进程内路径按 16k 假定处理**（不做
            重采样——重采样唯一实现位于 sidecar 桥内 `voice_bridge._resample`）
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


def _coerce_to_pcm16_bytes(audio):
    """把音频输入归一化为裸 int16 PCM 字节（sidecar 桥解码口径，与 ASR 约定对齐）。

    - bytes / bytearray：按既有约定视为裸 int16 PCM，**原样返回**（不触 numpy——
      主进程冻结包刻意不含 numpy，前端上传的 PCM 即走本分支）；
    - ndarray / list：float 波形（[-1, 1]）按 ×32768 钳制转 int16；
      已是 int16 的数组原样编码。numpy 仅在**本分支内**延迟导入。

    :param audio: 音频输入（bytes / ndarray / list）
    :return: bytes，裸 int16 PCM
    """
    if isinstance(audio, (bytes, bytearray)):
        return bytes(audio)

    import numpy as np

    arr = np.asarray(audio)
    if arr.dtype == np.int16:
        return arr.tobytes()
    return (
        np.clip(np.asarray(arr, dtype=np.float32) * 32768.0, -32768, 32767)
        .astype(np.int16)
        .tobytes()
    )


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


class BridgeASRBackend(ASRBackend):
    """经 sidecar 桥调用 SenseVoice 的识别后端（stdio + 常驻进程，uv1 协议）。

    与 :class:`SenseVoiceBackend` 的差异：funasr 与模型仅在 sidecar conda
    环境内加载，主进程只做 PCM 归一（→ base64）与结果解析（复用
    :func:`_parse_funasr_result`，跨进程不产生第二份解析逻辑）。
    """

    def __init__(self, client, device="cpu"):
        """初始化桥后端。

        :param client: VoiceBridgeClient 实例（进程管理与协议交互）
        :param device: 推理设备意图（透传 sidecar；"gpu" 由客户端归一为 auto）
        """
        self.client = client
        self.device = device or "cpu"

    def transcribe(self, audio, sample_rate=None):
        """经桥识别音频并返回 ``{text, emotion, event}``。

        :param audio: bytes（裸 int16 PCM）/ ndarray / list 音频输入
        :param sample_rate: 输入采样率（Hz）；桥侧非 16k 时自动重采样
            （SenseVoice 期望 16kHz）；缺省 None → 16000（历史默认口径）
        :return: dict，键为 text / emotion / event
        :raises RuntimeError: 桥不可用或 sidecar 返回错误时（消息含中文诊断）
        """
        audio_b64 = base64.b64encode(_coerce_to_pcm16_bytes(audio)).decode("ascii")
        header, _payload = self.client.request(
            {
                "op": "asr",
                "audio_b64": audio_b64,
                "sample_rate": int(sample_rate or 16000),
            }
        )
        return _parse_funasr_result([{"text": header.get("raw", "")}])


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

    def transcribe(self, audio, sample_rate=None):
        """直接返回预设结果，不解析音频（``sample_rate`` 仅为契约兼容而忽略）。"""
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

    def transcribe(self, audio, sample_rate=None):
        """识别音频，返回归一化的 ``{text, emotion, event}``。

        :param audio: PCM 音频字节
        :param sample_rate: 可选输入采样率（Hz）；透传后端（桥后端据此重采样
            到 SenseVoice 期望的 16kHz），缺省 None 表示未提供
        """
        result = self.backend.transcribe(audio, sample_rate)
        if not isinstance(result, dict):
            result = {}
        return {
            "text": str(result.get("text", "")),
            "emotion": str(result.get("emotion", "")),
            "event": result.get("event", None),
        }