# -*- coding: utf-8 -*-
"""语音 sidecar 桥接进程（lite/audio/voice_bridge.py）——stdio JSON 协议 uv1。

**自包含约束**：本脚本在 sidecar conda 环境（``<root>/runtime/voice``）内运行，
该环境**不包含项目包 lite**——故本文件禁止 import lite.*，仅使用标准库 +
sidecar 内第三方库（numpy / torch / melo / funasr）。它同时是 bridge 协议的
**唯一真相源**（客户端 :mod:`lite.audio.voice_bridge_client` 与之对齐）。

帧协议 uv1：
- 请求：stdin 单行 UTF-8 JSON：
    {"op": "ping", "id": 1}
    {"op": "tts", "id": 2, "text": "你好", "voice": "<音色目录或空>",
     "speed": 1.0, "language": "ZH"}
    {"op": "asr", "id": 3, "audio_b64": "<int16 PCM base64>", "sample_rate": 16000}
    {"op": "chat", "id": 5, "prompt": "<完整提示词>", "model": "<GGUF 绝对路径>",
     "max_tokens": 128, "n_ctx": 2048, "n_gpu_layers": 0, "temperature": 0.7,
     "timeout": 300}
    {"op": "embed", "id": 6, "texts": ["<文本1>", ...]}
    {"op": "shutdown", "id": 4}
- 响应：stdout 单行 JSON 头 + ``\\n`` + 可选原始载荷：
    ping     → {"id": 1, "ok": true, "pong": true}
    tts      → {"id": 2, "ok": true, "length": N, "sampling_rate": 44100,
                "format": "wav"} + N 字节 **完整 WAV** 载荷
               （16-bit PCM mono；clip 钳制与 wav 封装由本进程完成——主进程是
                冻结包，刻意不含 numpy/soundfile，2026-09-25 打包态实测：
                封装放在主进程会直接 503「No module named 'numpy'」）
    asr      → {"id": 3, "ok": true, "raw": "<富文本标签原文>"}
               （标签解析由主进程 asr._parse_funasr_result 复用完成）
    chat     → {"id": 5, "ok": true, "text": "<回复文本>"}
               （llama.cpp **预编译二进制**单轮外部推理：每次请求拉起一次
                ``<root>/runtime/llama/llama-cli.exe``，绕开 llama-cpp-python
                的安装墙——见规划 §四-15；模型冷加载约 2~4s）
    embed    → {"id": 6, "ok": true, "dim": N, "vectors": [[...]]}
               （Qwen3-Embedding ONNX last-token pooling + L2 归一 float32；
                资产缺失 / 超限 / 引擎失败 → ok=false 中文错误，主进程回退
                llama.cpp 嵌入）
    错误     → {"id": N, "ok": false, "error": "中文描述"}
- stderr：中文诊断日志（主进程可收集，不参与协议）。

设计约束：
- 引擎按需惰性加载并常驻缓存（TTS 按 音色目录 键缓存；ASR 单例）；
- chat 为外部子进程（llama-cli），不入常驻缓存；请求内 ``timeout`` 可控（缺省 300s）；
- 单请求失败不影响后续请求（异常兜底为该请求的 ok=false 响应）；
- ``shutdown`` 后干净退出（引擎析构交由进程退出处理）。

TTS ORT 加速（2026-10-01）：
- 默认 checkpoint 引擎的 enc_p / flow / dp / dec 在加载后被 ONNX Runtime
  会话替换（CPU EP 全链 ≈1.4~1.9x；sdp 保持 torch——实验结论为负收益）；
- **多后端加速（同日落地 + 双模式 spec 扩展）**：``--accel`` 取
  ``off / cpu / auto / cuda / dml / rocm``——``auto`` 运行时探测（CUDA 优先，
  tf32 关闭，全链 ≈2.9~5.3x）；``cuda`` / ``dml``（DirectML，消费
  ``--accel-device`` 设备提示）/ ``rocm``（Linux 部署面预留；Windows 无分发）
  指定后端，不可用 / 构造失败 / 推理异常逐级回退 CPU EP（含运行时熔断降级）；
  ``off`` 纯 torch、``cpu`` 跳过 GPU 探测。torch 侧（melo 本体）保持
  ``--device`` 口径（产品默认 cpu），**不走 MeloTTS 官方 torch-GPU runtime**；
- 资产目录：与 bridge.py 同目录的 ``tts_onnx/``（分发态由 build.py::assemble
  从 installer/bundled/tts_onnx 落位）；依赖/资产缺失时静默回退纯 torch，
  TTS 行为不受影响。见 .trae/documents/20261001_模块0_TTS引擎ORT落地.md
  与 20261001_模块0_TTS引擎GPU落地.md。

运行方式（由主后端拉起，勿手工改参数口径）::

    <root>/runtime/voice/python.exe <本脚本> --root <便携根> --device cuda
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import traceback

#: 协议版本（客户端对齐校验用）。
PROTOCOL_VERSION = "uv1"
#: TTS 引擎缓存上限（每引擎百 MB 级权重）。
TTS_ENGINE_CACHE_LIMIT = 3
#: TTS ORT 加速资产目录名（分发态：与 bridge.py 同目录；由打包链落位，
#: 见 .trae/documents/20261001_模块0_TTS引擎ORT落地.md）。
TTS_ORT_ASSET_DIR_NAME = "tts_onnx"
#: ORT 化模块清单（2026-10-01 第二批裁决⑥ + Task 4B 实验验证：**收敛为 dec 单模块**）。
#: enc_p / dp 为负收益、flow 仅小幅正收益但会引入额外 shape 抖动面；sdp 保持 torch。
TTS_ORT_MODULE_NAMES = ("dec",)
#: dec 形状稳定化桶宽（帧，第二批裁决⑤）：GPU 后端上把 dec 输入 z 的长度补零到该值
#: 的整数倍再推理（输出按比例裁剪），把有限桶内的输入形状钉死，消除 ORT CUDA 的
#: per-new-shape 重规划代价。真机实测（真实 3 文本 tts）：dec-only 无补零 1.86x →
#: 补零（256）3.69x。0 = 关闭。**仅 GPU 类 provider 启用**——CPU EP 上补零徒增算力。
TTS_ORT_DEC_PAD_BUCKET = 256
#: TTS ORT CUDA 设备号（进程内可见的第一个 CUDA 设备）；多卡隔离经部署级环境
#: 变量 ``CUDA_VISIBLE_DEVICES`` 控制（torch/ORT 双侧一致）。
TTS_ORT_CUDA_DEVICE_ID = 0
#: ``--accel`` 值域：off=纯 torch；cpu=跳过 GPU 探测直用 CPU EP；auto=运行时探测
#: （CUDA 优先）；cuda / dml / rocm=指定后端（不可用 / 失败逐级回退 CPU EP）。
ACCEL_CHOICES = ("off", "cpu", "auto", "cuda", "dml", "rocm")
#: ``--accel-device`` 值域：仅 DirectML 消费（""=自动 / igpu=核显 / dgpu=独显 提示）。
ACCEL_DEVICE_CHOICES = ("", "igpu", "dgpu")
#: ``--accel`` 缺省（运行时探测，保持既有语义）。
ACCEL_DEFAULT = "auto"
#: ``--accel-device`` 缺省（不指定设备提示）。
ACCEL_DEVICE_DEFAULT = ""
#: 进程级加速意图（``main`` 解析 ``--accel`` / ``--accel-device`` 后写入；
#: :func:`_load_ort_sessions` 读取）。默认值保证直跑 / 未接线场景等价既有 auto 行为。
_ACCEL = ACCEL_DEFAULT
_ACCEL_DEVICE = ACCEL_DEVICE_DEFAULT
#: DirectML 设备提示 → ``device_id`` 映射——**本期为空：精确映射未闭合**。
#: Task 1（2026-10-01）结论：双卡本机 ``device_id`` 0/1 均可建会话，但
#: igpu / dgpu → device_id 的精确对应未在有核显真机确认（DXGI 序含虚拟适配器干扰）。
#: 故保守留空：设备提示仅作日志记录意图，``device_id`` 交由 DML 自动枚举适配器；
#: 待有核显真机确认后回填本表，:func:`_try_build_dml_sessions` 即自动生效。
_DML_DEVICE_ID_BY_HINT = {}
#: 进程级 ORT 会话缓存：None=未加载；False=不可用（依赖/资产问题，不再重试）；
#: dict=常驻复用（会话与 torch 引擎实例无关，可跨引擎重建复用）。
_ORT_SESSIONS = None
#: 当前 ORT 会话的提供者（"cuda" / "dml" / "rocm" / "cpu" / None=未加载）——
#: 熔断降级的判定依据（GPU 类 provider 含 cuda / dml / rocm）。
_ORT_PROVIDER = None
#: 熔断标志：GPU 推理异常后置 True（进程级，只降不升——后续不再探测 CUDA）。
_ORT_FORCE_CPU = False
#: DLL 目录句柄保活表（``os.add_dll_directory`` 返回对象被 GC 即从搜索路径移除，
#: 必须持引用；键=目录路径）。
_DLL_DIR_HANDLES = {}
#: 协议输出流（进程启动时捕获的原始 stdout.buffer）。
#: 第三方库（funasr 等）会在加载时向 stdout 裸打印（实例："Notice: ffmpeg is
#: not installed..."），若协议共用 stdout 会污染帧流——故在加载任何引擎前
#: 由 :func:`_capture_proto_stream` 固定协议流并把 ``sys.stdout`` 换到 stderr。
_PROTO_OUT = None


def _capture_proto_stream():
    """固定协议输出流并隔离第三方库的 stdout 打印（在引擎加载前调用）。

    - 捕获进程启动时的 ``sys.stdout.buffer`` 作为**协议专用输出**；
    - ``sys.stdout`` 指向 ``sys.stderr``——此后任何库的裸 print 全部落入
      stderr（主进程收集为诊断，不干扰帧协议）。
    """
    global _PROTO_OUT
    if _PROTO_OUT is not None:
        return
    _PROTO_OUT = sys.stdout.buffer
    sys.stdout = sys.stderr


def _install_torch_load_compat():
    """放宽 ``torch.load`` 的 ``weights_only`` 默认值（torch>=2.6 兼容 shim）。

    torch 2.6 起 ``torch.load`` 默认 ``weights_only=True``，而 MeloTTS 检查点
    （``checkpoint.pth``）与 funasr 若干权重含非 tensor 的 pickle 对象，会被
    直接拒绝反序列化。本进程加载的均为**本地可信模型文件**，故统一把默认值
    放宽为 ``False``（仅在 sidecar 进程内生效，不影响主进程）。
    """
    try:
        import torch
    except ImportError:  # 无 torch 的兜底场景（如纯 ping 冒烟）
        return
    original = torch.load
    if getattr(original, "_cxa_weights_only_compat", False):
        return

    def _load(*args, **kwargs):
        """包装 torch.load：缺省 weights_only=False，显式传入时尊重调用方。"""
        kwargs.setdefault("weights_only", False)
        return original(*args, **kwargs)

    _load._cxa_weights_only_compat = True
    torch.load = _load


def _resolve_device(device):
    """解析设备意图：``"auto"`` 经 sidecar 的 torch 判定为 cuda/cpu；其余原样。

    主进程无法判断 sidecar 的 CUDA 可用性，故 ``gpu`` 意图在客户端被归一为
    ``auto`` 后由本函数在本进程内解析（唯一判定点）。
    """
    value = str(device or "cpu").strip().lower() or "cpu"
    if value != "auto":
        return value
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:  # noqa: BLE001 - torch 异常一律回 cpu，不阻断桥启动
        return "cpu"


def _log(message):
    """向 stderr 输出中文诊断（不干扰 stdout 协议流）。"""
    print(f"[voice_bridge] {message}", file=sys.stderr, flush=True)


def _normalize_accel(value):
    """归一 ``--accel``：值域内小写原样返回；值域外一律回 ``"auto"`` + 中文日志（不抛）。"""
    normalized = str(value if value is not None else "").strip().lower()
    if normalized in ACCEL_CHOICES:
        return normalized
    _log(f"非法 --accel 值 {value!r}，归一为 {ACCEL_DEFAULT}")
    return ACCEL_DEFAULT


def _normalize_accel_device(value):
    """归一 ``--accel-device``：值域内小写原样；值域外一律回 ``""``（自动）+ 中文日志。"""
    normalized = str(value if value is not None else "").strip().lower()
    if normalized in ACCEL_DEVICE_CHOICES:
        return normalized
    _log(f"非法 --accel-device 值 {value!r}，归一为自动（空）")
    return ACCEL_DEVICE_DEFAULT


def _write_frame(header, payload=b""):
    """写出一帧响应：单行 JSON 头 + ``\\n`` + 可选原始载荷（协议专用流）。"""
    out = _PROTO_OUT if _PROTO_OUT is not None else sys.stdout.buffer
    out.write(json.dumps(header, ensure_ascii=False).encode("utf-8"))
    out.write(b"\n")
    if payload:
        out.write(payload)
    out.flush()


class _TtsEngineCache:
    """TTS 引擎懒加载与 LRU 缓存（键 = (config_path, ckpt_path)）。"""

    def __init__(self, device, language="ZH", hf_endpoint=""):
        """记录构造参数；引擎在首次请求时构造。"""
        self._device = device
        self._language = language
        self._hf_endpoint = hf_endpoint
        self._engines = {}
        self._tts_type = None

    def _ensure_lib(self):
        """导入 melo.api.TTS（失败时抛 RuntimeError 交调用方回中文错误）。"""
        if self._tts_type is not None:
            return
        if self._hf_endpoint:
            # MeloTTS 经 huggingface_hub 下载权重；镜像端点须在导入/下载前设置
            os.environ.setdefault("HF_ENDPOINT", self._hf_endpoint)
        from melo.api import TTS

        self._tts_type = TTS

    def get(self, config_path=None, ckpt_path=None):
        """取（或构造并缓存）指定音色配置组合的引擎实例。"""
        self._ensure_lib()
        key = (config_path or "", ckpt_path or "")
        engine = self._engines.get(key)
        if engine is not None:
            self._engines[key] = self._engines.pop(key)  # LRU 提升
            return engine
        engine = self._tts_type(
            language=self._language,
            device=self._device,
            use_hf=True,
            config_path=config_path,
            ckpt_path=ckpt_path,
        )
        # TTS ORT 加速（2026-10-01 落地）：仅官方默认模型路径启用；
        # 自定义音色（本地 config/ckpt 权重）与导出资产不匹配 → 纯 torch。
        # 失败静默回退（_apply_ort_accel 内部保证不抛异常）。
        if not config_path and not ckpt_path:
            _apply_ort_accel(engine)
        # 静音塌陷修复（2026-10-02）：dec 去 DC 守卫（默认透传，仅近静音重试窗口内启用）
        _install_dec_dc_guard(engine)
        self._engines[key] = engine
        while len(self._engines) > TTS_ENGINE_CACHE_LIMIT:
            oldest = next(iter(self._engines))
            self._engines.pop(oldest)
        return engine


#: ASR ONNX 资产目录名（funasr 导出产物 + config.yaml/am.mvn/bpe.model 辅助文件；
#: 开发态由导出脚本落位，分发态归批打包——见 20261002_模块0_加速未闭合项收口.md 批 B）。
ASR_ORT_ASSET_DIR_NAME = "asr_onnx"


class _AsrModel:
    """SenseVoice 模型懒加载单例（ONNX 优先，funasr-torch 回退；20261002 批 B）。

    - **ONNX 优先**：``<root>/runtime/voice_bridge/asr_onnx/model.onnx`` 存在且
      ``funasr_onnx`` 可导入时走 ORT 推理（CPU EP 或 CUDA EP；加载失败逐级
      回退 funasr-torch，全中文日志）；
    - **设备语义**：``device`` 为 ``gpu`` / ``cuda`` / ``auto`` 时传
      ``device_id=0``（CUDA EP 探测，不可用由 funasr-onnx 内部自动回退 CPU），
      其余（``cpu`` 等）传 ``"-1"``（纯 CPU）；
    - :meth:`generate` 统一两个后端的调用与结果解析差异，返回富文本标签原文。
    """

    def __init__(self, root, device):
        """记录 torch 模型路径、ONNX 资产目录与设备；模型在首次请求时加载。"""
        self._model_path = os.path.join(root, "data", "SenseVoiceSmall")
        self._onnx_dir = os.path.join(root, "runtime", "voice_bridge", ASR_ORT_ASSET_DIR_NAME)
        self._device = device
        self._model = None
        self._backend = None  # "onnx" / "torch"

    def get(self):
        """取（或加载）ASR 引擎实例（ONNX 优先，失败回退 funasr-torch）。"""
        if self._model is not None:
            return self._model
        model = self._try_onnx()
        if model is not None:
            self._model = model
            self._backend = "onnx"
            return self._model
        _log("ASR ONNX 路径不可用，回退 funasr-torch 推理")
        from funasr import AutoModel

        self._model = AutoModel(
            model=self._model_path,
            device=self._device,
            disable_update=True,
        )
        self._backend = "torch"
        return self._model

    def _try_onnx(self):
        """尝试构造 funasr-onnx SenseVoiceSmall；不可用返回 None（调用方回退 torch）。"""
        model_file = os.path.join(self._onnx_dir, "model.onnx")
        if not os.path.isfile(model_file):
            _log(f"ASR ONNX 资产缺失（{model_file}），尝试回退 funasr-torch")
            return None
        try:
            from funasr_onnx import SenseVoiceSmall
        except Exception as exc:  # noqa: BLE001 - 依赖缺失/异常一律回退
            _log(f"ASR ONNX 不可用（funasr_onnx 导入失败：{type(exc).__name__}: {exc}），"
                 "回退 funasr-torch")
            return None
        # 设备语义：gpu/cuda/auto → device_id="0"（CUDA EP 探测，OrtInferSession
        # 内部不可用时自动回退 CPU 并告警）；cpu 等其余值 → "-1"（纯 CPU EP）。
        device_id = "0" if str(self._device or "").strip().lower() in ("gpu", "cuda", "auto") else "-1"
        if device_id == "0":
            # CUDA EP 所需 cudart/cublas 库来自 torch/lib（先注入 DLL 搜索路径）
            if not _inject_cuda_dll_paths():
                _log("ASR ONNX CUDA 不可用（torch/lib DLL 注入失败），改用纯 CPU EP")
                device_id = "-1"
        try:
            model = SenseVoiceSmall(model_dir=self._onnx_dir, quantize=False, device_id=device_id)
            _log(f"ASR ONNX 已启用（目录 {self._onnx_dir}，device_id={device_id}）")
            return model
        except Exception as exc:  # noqa: BLE001 - 构造失败回退 torch
            _log(f"ASR ONNX 加载失败（{type(exc).__name__}: {exc}），回退 funasr-torch")
            return None

    def generate(self, waveform):
        """统一转写入口：16kHz float32 波形 → 富文本标签原文（str）。

        - ONNX 后端：``model(wav, language="auto", use_itn=True)`` → ``list[str]``；
        - torch 后端：``model.generate(input=wav, ...)`` → ``list[dict]``（取 ``text``）。
        """
        model = self.get()
        if self._backend == "onnx":
            # funasr-onnx 0.4.x load_data：裸 np.ndarray 命中数组分支（list 元素被当路径）
            res = model(waveform, language="auto", use_itn=True)
            return str(res[0]) if res else ""
        res = model.generate(input=waveform, language="auto", use_itn=True)
        first = res[0] if res else None
        if isinstance(first, dict):
            return str(first.get("text", ""))
        if isinstance(first, (list, tuple)) and first and isinstance(first[0], dict):
            return str(first[0].get("text", ""))
        return ""


def _voice_config_paths(voice_dir):
    """按音色目录推导 (config_path, ckpt_path)；目录为空/缺文件时返回 (None, None)。

    与主进程 MeloTTSBackend.synthesize 的推导口径一致：
    ``config.json`` 存在才传 config_path，``ckpt.txt`` 存在才传 ckpt_path；
    两者皆缺 → 走 MeloTTS 官方默认模型（首次联网下载权重）。
    """
    if not voice_dir:
        return None, None
    cfg = os.path.join(voice_dir, "config.json")
    ckpt = os.path.join(voice_dir, "ckpt.txt")
    return (cfg if os.path.exists(cfg) else None,
            ckpt if os.path.exists(ckpt) else None)


# ------------------------------------------------------------------ #
# TTS ORT 加速（2026-10-01 落地；实验依据与证据见                     #
# .trae/documents/20261001_模块0_TTS引擎加速实验.md + 落地文档）       #
# ------------------------------------------------------------------ #


def _ort_asset_dir():
    """ORT 资产目录：与 bridge.py 同目录的 ``tts_onnx/``（分发态由打包链落位）。"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), TTS_ORT_ASSET_DIR_NAME)


def _inject_cuda_dll_paths():
    """把 sidecar 的 torch/lib 注入 DLL 搜索路径（ORT CUDA EP 复用其 CUDA/cuDNN 库）。

    torch（cu128）自带 ``cudart64_12 / cublas64_12 / cublasLt64_12 / cudnn64_9``
    全套，满足 onnxruntime-gpu 1.23.2 的 CUDA 12.x + cuDNN 9.x 需求（实验验证）；
    ``os.add_dll_directory`` 的返回句柄**必须持引用**（被 GC 即从搜索路径移除），
    故存入 :data:`_DLL_DIR_HANDLES` 保活；PATH 前置作为兜底（部分加载路径只用
    PATH 搜索）。torch 缺失/注入异常时返回 False（调用方回退 CPU EP）。
    """
    try:
        import torch

        torch_lib = os.path.join(os.path.dirname(os.path.abspath(torch.__file__)), "lib")
    except Exception:  # noqa: BLE001 - 无 torch 场景（纯 ping 冒烟等）
        return False
    if not os.path.isdir(torch_lib):
        return False
    if torch_lib not in _DLL_DIR_HANDLES:
        try:
            if hasattr(os, "add_dll_directory"):
                _DLL_DIR_HANDLES[torch_lib] = os.add_dll_directory(torch_lib)
            else:  # pragma: no cover - 非 Windows 兜底（仅 PATH 前置）
                _DLL_DIR_HANDLES[torch_lib] = None
            os.environ["PATH"] = torch_lib + os.pathsep + os.environ.get("PATH", "")
        except Exception:  # noqa: BLE001 - 注入失败按不可用处理
            _DLL_DIR_HANDLES.pop(torch_lib, None)
            return False
    return True


def _try_build_cuda_sessions(ort, paths):
    """尝试构造 4 条 CUDA EP 会话；不可用/失败返回 None（调用方回退 CPU EP）。

    探测链（逐级失败即否，均记 stderr 中文日志）：
    provider 枚举（CPU 版包直接否）→ torch/lib DLL 注入 → ``torch.cuda.is_available()``
    （CUDA 可用性唯一判定点）→ 逐模块构造。构造参数：``device_id``（默认 0）+
    **tf32 关闭**（实验结论：默认 TF32 数值差 6.06e-04 潜在可闻 → 关闭后 3.26e-06
    且更快）。

    注：**不得**设置 ``session.disable_cpu_ep_fallback=1``——真机实测（2026-10-01）
    这些导出图含少量被分配到默认 CPU EP 的节点，禁用回退会直接拒绝创建会话
    （``This session contains graph nodes that are assigned to the default CPU EP,
    but fallback to CPU EP has been explicitly disabled``）；主要算子在 GPU 上
    （加速数据可证），少量节点落 CPU 属正常回退，无"假 GPU"之虞。
    """
    try:
        if "CUDAExecutionProvider" not in ort.get_available_providers():
            _log("ORT CUDA 不可用（onnxruntime 未含 CUDAExecutionProvider），回退 CPU EP")
            return None
    except Exception as exc:  # noqa: BLE001 - 枚举异常按不可用处理
        _log(f"ORT CUDA 不可用（provider 枚举异常：{type(exc).__name__}），回退 CPU EP")
        return None
    if not _inject_cuda_dll_paths():
        _log("ORT CUDA 不可用（torch/lib DLL 注入失败），回退 CPU EP")
        return None
    try:
        import torch

        if not torch.cuda.is_available():
            _log("ORT CUDA 不可用（torch 判定 CUDA 不可用），回退 CPU EP")
            return None
    except Exception as exc:  # noqa: BLE001 - torch 探测异常按不可用处理
        _log(f"ORT CUDA 不可用（torch 探测异常：{type(exc).__name__}），回退 CPU EP")
        return None
    try:
        options = {"device_id": TTS_ORT_CUDA_DEVICE_ID, "use_tf32": 0}
        sessions = {}
        for name, path in paths.items():
            opts = ort.SessionOptions()
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            sessions[name] = ort.InferenceSession(
                path, sess_options=opts,
                providers=[("CUDAExecutionProvider", options)],
            )
        return sessions
    except Exception as exc:  # noqa: BLE001 - 构造失败整体回退（已建会话随作用域释放）
        _log(f"ORT CUDA 会话构造失败（{type(exc).__name__}: {exc}），回退 CPU EP")
        return None


def _try_build_dml_sessions(ort, paths):
    """尝试构造 4 条 DirectML EP 会话；不可用 / 失败返回 None（调用方回退 CPU EP）。

    探测链（逐级失败即否，均记 stderr 中文日志）：DML provider 枚举（CPU 版 /
    onnxruntime-gpu 包直接否）→ 逐模块构造。设备提示 :data:`_ACCEL_DEVICE` 仅记录
    意图：**igpu / dgpu → ``device_id`` 精确映射本期未闭合**（见
    :data:`_DML_DEVICE_ID_BY_HINT`），故当前不传 ``device_id``，交由 DML 按 DXGI 序
    自动枚举适配器；映射表回填后本函数自动生效。与 CUDA 链同理，**不得**设置
    ``session.disable_cpu_ep_fallback=1``（导出图含少量默认 CPU EP 节点）。
    """
    try:
        if "DmlExecutionProvider" not in ort.get_available_providers():
            _log("ORT DML 不可用（onnxruntime 未含 DmlExecutionProvider），回退 CPU EP")
            return None
    except Exception as exc:  # noqa: BLE001 - 枚举异常按不可用处理
        _log(f"ORT DML 不可用（provider 枚举异常：{type(exc).__name__}），回退 CPU EP")
        return None
    options = {}
    if _ACCEL_DEVICE:
        label = "核显" if _ACCEL_DEVICE == "igpu" else "独显"
        mapped = _DML_DEVICE_ID_BY_HINT.get(_ACCEL_DEVICE)
        if mapped is None:
            _log(
                f"ORT DML 设备提示 accel_device={_ACCEL_DEVICE}（{label}）；"
                "device_id 精确映射未闭合，交由 DML 自动枚举适配器"
            )
        else:
            options["device_id"] = mapped
            _log(f"ORT DML 设备提示 accel_device={_ACCEL_DEVICE}（{label}）→ device_id={mapped}")
    try:
        sessions = {}
        for name, path in paths.items():
            opts = ort.SessionOptions()
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            sessions[name] = ort.InferenceSession(
                path, sess_options=opts, providers=[("DmlExecutionProvider", options)]
            )
        return sessions
    except Exception as exc:  # noqa: BLE001 - 构造失败整体回退（已建会话随作用域释放）
        _log(f"ORT DML 会话构造失败（{type(exc).__name__}: {exc}），回退 CPU EP")
        return None


def _try_build_rocm_sessions(ort, paths):
    """尝试构造 4 条 ROCm EP 会话；无该 EP / 失败返回 None（调用方回退 CPU EP）。

    事实：``onnxruntime-rocm`` **无 Windows 分发**（Task 1 已核实），故 Windows 便携包
    必然先命中「未含 ROCMExecutionProvider」并明确回退 CPU EP；ROCm 为 Linux 部署面
    预留，值域保留。日志区分回退原因以便诊断。
    """
    try:
        if "ROCMExecutionProvider" not in ort.get_available_providers():
            _log(
                "ORT ROCm 不可用（onnxruntime 未含 ROCMExecutionProvider；Windows 无分发），"
                "回退 CPU EP"
            )
            return None
    except Exception as exc:  # noqa: BLE001 - 枚举异常按不可用处理
        _log(f"ORT ROCm 不可用（provider 枚举异常：{type(exc).__name__}），回退 CPU EP")
        return None
    try:
        sessions = {}
        for name, path in paths.items():
            opts = ort.SessionOptions()
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            sessions[name] = ort.InferenceSession(
                path, sess_options=opts, providers=[("ROCMExecutionProvider", {})]
            )
        return sessions
    except Exception as exc:  # noqa: BLE001 - 构造失败整体回退
        _log(f"ORT ROCm 会话构造失败（{type(exc).__name__}: {exc}），回退 CPU EP")
        return None


def _provider_label(provider):
    """会话加载完成日志里的 provider 标签（CUDA 标签与既有逐字兼容）。"""
    if provider == "cuda":
        return f"CUDA EP(device={TTS_ORT_CUDA_DEVICE_ID}, tf32=off)"
    if provider == "dml":
        return f"DML EP(device_hint={_ACCEL_DEVICE or 'auto'})"
    if provider == "rocm":
        return "ROCm EP"
    return "CPU EP"


def _load_ort_sessions():
    """加载（并进程级缓存）ORT 会话；不可用时返回 False（不抛、不再重试）。

    四条会话对应 enc_p / flow / dp / dec 的 ONNX 资产（默认 checkpoint 口径）。
    **provider 选择**（:data:`_ACCEL`，spec「``tts.accel`` 值域扩展与多后端 bridge」）：

    - ``off``：不加载 ORT（纯 torch），直接返回 False；
    - ``cpu``：跳过全部 GPU 探测，直用 CPU EP；
    - ``auto``：运行时探测（CUDA 优先，失败回 CPU EP——既有语义）；
    - ``cuda`` / ``dml`` / ``rocm``：指定后端（构造失败 / 不可用逐级回退 CPU EP；
      ``dml`` 额外消费 ``--accel-device`` 设备提示）。

    熔断标志（``_ORT_FORCE_CPU``）置位后固定 CPU（不再探测任何 GPU EP）。
    onnxruntime 缺失、资产缺失或加载异常 → 记 stderr 日志并回退 torch 推理。
    """
    global _ORT_SESSIONS, _ORT_PROVIDER
    if _ORT_SESSIONS is not None:
        return _ORT_SESSIONS
    if _ACCEL == "off":
        _log("ORT 加速已关闭（--accel off），纯 torch 推理")
        _ORT_SESSIONS = False
        return False
    # 先做纯文件资产检查：缺资产时零依赖开销（开发态/未落位环境不触碰 onnxruntime）。
    asset_dir = _ort_asset_dir()
    paths = {name: os.path.join(asset_dir, f"{name}.onnx") for name in TTS_ORT_MODULE_NAMES}
    missing = [name for name, path in paths.items() if not os.path.isfile(path)]
    if missing:
        _log(f"ORT 资产缺失 {missing}（目录 {asset_dir}），继续 torch 推理")
        _ORT_SESSIONS = False
        return False
    try:
        import onnxruntime as ort
    except Exception as exc:  # noqa: BLE001 - 依赖缺失一律回退
        _log(f"ORT 加速不可用（onnxruntime 导入失败：{type(exc).__name__}），继续 torch 推理")
        _ORT_SESSIONS = False
        return False
    try:
        t0 = time.perf_counter()
        sessions = None
        provider = "cpu"
        if not _ORT_FORCE_CPU:
            # auto/cuda → CUDA 探测链；dml → DirectML 构造；rocm → ROCm 构造；
            # cpu → 跳过探测（直落下方 CPU EP）。
            if _ACCEL in ("auto", "cuda"):
                sessions = _try_build_cuda_sessions(ort, paths)
                if sessions:
                    provider = "cuda"
            elif _ACCEL == "dml":
                sessions = _try_build_dml_sessions(ort, paths)
                if sessions:
                    provider = "dml"
            elif _ACCEL == "rocm":
                sessions = _try_build_rocm_sessions(ort, paths)
                if sessions:
                    provider = "rocm"
        if sessions is None:
            sessions = {}
            for name, path in paths.items():
                opts = ort.SessionOptions()
                opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
                sessions[name] = ort.InferenceSession(
                    path, sess_options=opts, providers=["CPUExecutionProvider"]
                )
        _ORT_SESSIONS = sessions
        _ORT_PROVIDER = provider
        _log(
            f"ORT 会话加载完成（{len(sessions)} 模块，{_provider_label(provider)}，"
            f"{time.perf_counter() - t0:.2f}s，资产 {asset_dir}）"
        )
    except Exception:  # noqa: BLE001 - 加载失败回退
        _log(f"ORT 会话加载失败，继续 torch 推理：{traceback.format_exc()}")
        _ORT_SESSIONS = False
        _ORT_PROVIDER = None
    return _ORT_SESSIONS


def _fallback_ort_to_cpu():
    """熔断降级：把全部 ORT 会话重建为 CPU EP（进程级、只降不升、幂等）。

    GPU 会话推理异常时调用（历史崩驱动风险的运行时兜底；最坏情况 = 回退 CPU
    体验，不中断服务）。置位 ``_ORT_FORCE_CPU`` 后重新加载会话——包装实例均
    经全局 :data:`_ORT_SESSIONS` 动态取会话，自动指向 CPU 会话。

    :return: 可用会话 dict；重建失败（连 CPU 会话都不可用）返回 None。
    """
    global _ORT_SESSIONS, _ORT_PROVIDER, _ORT_FORCE_CPU
    if _ORT_FORCE_CPU:
        return _ORT_SESSIONS if isinstance(_ORT_SESSIONS, dict) else None
    _ORT_FORCE_CPU = True
    _ORT_PROVIDER = None
    _ORT_SESSIONS = None  # 强制重新加载（_load_ort_sessions 见标志 → 固定 CPU EP）
    sessions = _load_ort_sessions()
    if isinstance(sessions, dict):
        _ORT_SESSIONS = sessions
        _ORT_PROVIDER = "cpu"
        return sessions
    return None


def _run_session_with_fallback(module_name, run_once):
    """按模块名取会话执行 ``run_once(session)``；CUDA 会话异常时熔断重试一次。

    - 会话经全局 :data:`_ORT_SESSIONS` **动态取**（熔断重建后自动指向 CPU 会话）；
    - 仅 GPU 类会话（``_ORT_PROVIDER`` 为 ``cuda`` / ``dml`` / ``rocm``）触发熔断；
      CPU 会话异常原样抛出（避免对非 GPU 异常做无意义的重建循环）；
    - 熔断重建失败 / 重试仍失败 → 原样抛出（由上层请求级兜底接管，不吞异常）。
    """
    sessions = _ORT_SESSIONS if isinstance(_ORT_SESSIONS, dict) else None
    session = sessions.get(module_name) if sessions else None
    if session is None:
        raise RuntimeError(f"ORT 会话缺失：{module_name}（加速资产不可用）")
    try:
        return run_once(session)
    except Exception as exc:  # noqa: BLE001 - GPU 推理异常 → 熔断降级
        if _ORT_PROVIDER not in ("cuda", "dml", "rocm"):
            raise
        _log(
            f"GPU 推理异常（provider={_ORT_PROVIDER} 模块 {module_name}："
            f"{type(exc).__name__}: {exc}），熔断降级到 CPU 会话并重试一次"
        )
        fallback = _fallback_ort_to_cpu()
        if not fallback or module_name not in fallback:
            raise
        return run_once(fallback[module_name])


def _dec_pad_target(length):
    """dec 形状稳定化的补零目标帧长：上取整到 :data:`TTS_ORT_DEC_PAD_BUCKET` 的倍数。

    ``bucket <= 0``（关闭）时原样返回 ``length``。纯函数（便于单测，不依赖 torch）。
    """
    bucket = TTS_ORT_DEC_PAD_BUCKET
    if bucket <= 0:
        return int(length)
    return ((int(length) + bucket - 1) // bucket) * bucket


def _dec_trim_count(out_len, origin_len, padded_len):
    """补零输出按比例裁剪后保留的样本数（至少 1）。

    dec 上采样比固定，故裁剪比例 = 原始帧长 / 补零帧长；未补零时原样返回。
    """
    out_len = int(out_len)
    if out_len <= 0 or padded_len <= origin_len:
        return out_len
    return max(1, int(round(out_len * origin_len / padded_len)))


def _dec_shape_pad_enabled(provider):
    """dec 形状稳定化是否启用：仅 GPU 类 provider（cuda / dml / rocm）且桶宽 > 0。"""
    return provider in ("cuda", "dml", "rocm") and TTS_ORT_DEC_PAD_BUCKET > 0


def _apply_ort_accel(engine):
    """把默认 checkpoint 引擎的 **dec** 子模块替换为 ORT 包装（推理路径专用）。

    模块收敛（第二批裁决⑥，Task 4B 实验验证）：仅 dec 走 ORT——enc_p / dp 为负收益、
    flow 仅小幅正收益且其输入 shape 随文本抖动（引入额外 per-new-shape 重规划面），
    故一律回 torch。**形状稳定化（裁决⑤）**：GPU 后端上把 dec 输入 z 补零到
    :data:`TTS_ORT_DEC_PAD_BUCKET` 的整数倍再推理、输出按比例裁剪，消除 ORT CUDA 的
    per-new-shape 重规划代价；补零对 CPU EP 为负（徒增算力），故仅 GPU 类 provider 启用。

    启用条件：仅**官方默认模型**（``config_path/ckpt_path`` 均空——与产品
    ``cx-open`` 默认链路一致）；自定义音色权重与导出资产不匹配，调用方不应
    调用。任何失败均静默回退（返回 False，TTS 行为不受影响）。

    :return: True 表示已启用 ORT。
    """
    if not _load_ort_sessions():  # 确保会话就绪（包装实例运行时经全局动态取会话）
        return False
    try:
        import torch  # 延迟导入：纯 ping 冒烟等无 torch 场景不触发

        class _OrtBase(torch.nn.Module):
            """ORT 模块基类：torch<->numpy 边界转换 + 会话执行（含熔断降级）。"""

            def __init__(self, module_name, feed_names):
                super().__init__()
                self._name = module_name
                self._feed = list(feed_names)

            def _run(self, tensors):
                import numpy as np

                feed = {}
                for name, value in zip(self._feed, tensors):
                    feed[name] = np.ascontiguousarray(value.detach().cpu().numpy())
                return _run_session_with_fallback(
                    self._name, lambda session: session.run(None, feed)
                )

            @staticmethod
            def _t(array):
                import numpy as np

                return torch.from_numpy(np.ascontiguousarray(array))

        class _OrtDec(_OrtBase):
            """dec 推理包装（未补零；CPU EP 口径）。"""

            def forward(self, z, g=None):
                return self._t(self._run([z, g])[0])

        class _OrtDecPad(_OrtBase):
            """dec 推理包装（形状稳定化）：z 沿长度维补零到桶倍数，输出按比例裁剪。

            裁剪比例 = 原始帧数 / 补零帧数 × 输出总长（dec 上采样比固定）；补零保真度
            已实测（同 z 上 torch vs ORT(补零) max|diff|=1.32e-05，相对峰值 0.00%）。
            """

            def forward(self, z, g=None):
                length = int(z.shape[2])
                padded = _dec_pad_target(length)
                if padded > length:
                    z = torch.nn.functional.pad(z, (0, padded - length))
                out = self._run([z, g])[0]
                keep = _dec_trim_count(out.shape[2], length, padded)
                if keep != out.shape[2]:
                    out = out[:, :, :keep]
                return self._t(out)

        use_pad = _dec_shape_pad_enabled(_ORT_PROVIDER)
        model = engine.model
        model.dec = _OrtDecPad("dec", ["z", "g"]) if use_pad else _OrtDec("dec", ["z", "g"])
        _log(
            f"TTS ORT 加速已启用（dec 单模块，provider={_ORT_PROVIDER or 'cpu'}，"
            f"形状桶={'开(' + str(TTS_ORT_DEC_PAD_BUCKET) + ')' if use_pad else '关'}；"
            "enc_p/flow/dp/sdp 保持 torch）"
        )
        return True
    except Exception:  # noqa: BLE001 - 包装构造/替换失败回退
        _log(f"TTS ORT 加速启用失败，继续 torch 推理：{traceback.format_exc()}")
        return False


#: L9 口径的 int16 采样值上限（clip 防越界回绕爆音）。
_PCM16_MAX = 32767
_PCM16_MIN = -32768

#: 首尾静音裁剪阈值（相对峰值；-40dBFS ≈ 1% 幅度）——20260930 引擎侧优化实测：
#: MeloTTS 分段音频尾随静音 0.05~0.60s 不等（"你好呀，"后达 0.57s；有效语音约四~五成），
#: 流式分段播放时表现为段间空档；前导静音 0~0.10s 推迟"听到人声"时刻。
TRIM_SILENCE_THRESH_DB = -40.0
#: 裁剪后保留的前导留白（秒）——极短自然呼吸感，避免起音被削。
TRIM_LEAD_KEEP_S = 0.03
#: 裁剪后保留的尾随留白（秒）——保留句间自然停顿。
TRIM_TRAIL_KEEP_S = 0.12


def _trim_silence(samples, sampling_rate, thresh_db=TRIM_SILENCE_THRESH_DB,
                  lead_keep=TRIM_LEAD_KEEP_S, trail_keep=TRIM_TRAIL_KEEP_S):
    """裁掉音频首尾静音，仅保留极短自然留白（不改语音内容，零质量损失）。

    判定口径：以**峰值**为基准的 -40dBFS 能量阈值（不依赖绝对值，弱音量音频同样适用）；
    全静音 / 空输入 / 异常采样率（<=0）一律原样返回（宁可不裁，不冒削掉语音的风险）。

    :param samples: float32 波形（numpy 数组或序列）
    :param sampling_rate: 采样率（Hz）
    :param thresh_db: 静音阈值（dBFS，相对峰值）
    :param lead_keep: 保留的前导留白（秒）
    :param trail_keep: 保留的尾随留白（秒）
    :return: 裁剪后的 float32 波形
    """
    import numpy as np

    audio = np.asarray(samples, dtype=np.float32)
    if audio.size == 0:
        return audio
    rate = float(sampling_rate or 0)
    if rate <= 0:
        return audio
    peak = float(np.abs(audio).max())
    if peak <= 0.0:
        return audio
    threshold = peak * (10.0 ** (thresh_db / 20.0))
    voiced = np.where(np.abs(audio) > threshold)[0]
    if voiced.size == 0:
        return audio
    start = int(voiced[0] - max(0.0, lead_keep) * rate)
    end = int(voiced[-1] + 1 + max(0.0, trail_keep) * rate)
    start = max(0, start)
    end = min(audio.size, end)
    if end <= start:
        return audio
    return audio[start:end]


def _pack_wav(samples, sampling_rate):
    """把 float32 波形打包为 16-bit PCM 单声道 WAV 字节。

    - float → int16 前先 clip（与主进程历史 ``_audio_to_pcm16`` 同口径，
      越界样本不再回绕成爆音）；
    - 用标准库 ``wave`` 封装（不引入 soundfile 依赖；本进程 numpy 常驻）。
    """
    import io
    import wave

    import numpy as np

    pcm = np.clip(
        np.asarray(samples, dtype=np.float32) * _PCM16_MAX, _PCM16_MIN, _PCM16_MAX
    ).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(int(sampling_rate))
        writer.writeframes(pcm.tobytes())
    return buf.getvalue()


# ------------------------------------------------------------------ #
# 静音塌陷修复（2026-10-02）：近静音检测 + 去 DC 重合成                #
# 根因与证据见 .trae/documents/20261002_模块0_TTS静音塌陷修复.md       #
# ------------------------------------------------------------------ #

#: 近静音检测阈值（峰值）：dec 塌陷输出 ~1.6e-05；正常语句峰值 ≥ ~0.1。
SILENCE_RETRY_PEAK = 0.005
#: dec 去 DC 守卫开关（模块级；桥为单线程，仅在 _handle_tts 重试窗口内为 True）。
_DEC_DC_GUARD = False


def _install_dec_dc_guard(engine):
    """给 dec 包一层「去 DC 守卫」（默认透传；仅近静音重试窗口内启用）。

    守卫对 dec 输入做 ``z' = z − 逐通道时间均值``（dim=2），用于修复
    「DC 主导 z → dec 输出塌陷」的文本相关近静音（2026-10-02 实测复活 ×11146）。
    正常路径零行为变化（开关关闭时逐字节透传）；任何失败仅告警不抛。

    :param engine: melo ``TTS`` 引擎实例（须已就绪 ``model.dec``）。
    """
    try:
        import torch

        model = engine.model
        inner = getattr(model, "dec", None)
        if inner is None or getattr(inner, "_dec_dc_guard", False):
            return

        class _DecDCGuard(torch.nn.Module):
            """dec 输入去 DC 守卫（启用时逐通道沿时间维去均值）。"""

            def __init__(self, inner_dec):
                super().__init__()
                self._inner = inner_dec

            def forward(self, z, g=None):
                if _DEC_DC_GUARD and z.dim() == 3 and z.shape[2] > 1:
                    z = z - z.mean(dim=2, keepdim=True)
                return self._inner(z, g)

        wrapper = _DecDCGuard(inner)
        wrapper._dec_dc_guard = True
        model.dec = wrapper
    except Exception:  # noqa: BLE001 - 守卫安装失败不影响正常合成
        _log(f"dec 去 DC 守卫安装失败（不影响正常合成）：{traceback.format_exc()}")


def _peak_of(samples):
    """波形峰值（float32；空输入返回 0）。"""
    import numpy as np

    arr = np.asarray(samples, dtype=np.float32)
    return float(np.abs(arr).max()) if arr.size else 0.0


def _synthesize_with_silence_repair(engine, text, speed, audio):
    """近静音检测 + 去 DC 重合成修复（2026-10-02 静音塌陷修复）。

    首次合成峰值低于 :data:`SILENCE_RETRY_PEAK`（dec 塌陷 ~1.6e-05；正常 ≥ ~0.1）
    时，在「去 DC 守卫」开启窗口内重合成一次；返回峰值更高者。
    重试异常 / 未改善：保留原结果并告警——**绝不静默返回近静音**。
    """
    peak = _peak_of(audio)
    if peak >= SILENCE_RETRY_PEAK:
        return audio
    global _DEC_DC_GUARD
    _log(f"检测到近静音输出（peak={peak:.2e}），启用去 DC 重合成：{text[:24]!r}")
    try:
        _DEC_DC_GUARD = True
        retry = engine.tts_to_file(text, 0, output_path=None, speed=speed, quiet=True)
    except Exception:  # noqa: BLE001 - 重试失败保留原结果（流程不中断）
        _log(f"去 DC 重合成失败（保留原结果）：{traceback.format_exc()}")
        return audio
    finally:
        _DEC_DC_GUARD = False
    retry_peak = _peak_of(retry)
    if retry_peak > peak:
        _log(f"去 DC 重合成完成：peak {peak:.2e} → {retry_peak:.2e}")
        return retry
    _log(f"去 DC 重合成未改善（peak {peak:.2e} → {retry_peak:.2e}），保留原结果")
    return audio


def _handle_tts(engine_cache, req):
    """处理 tts 请求：合成音频并返回**完整 wav 载荷**。

    返回 ``(header, payload)``：header 含 length / sampling_rate / format=wav，
    payload 为 wav 字节（主进程只做 base64 封装，不再需要 numpy/soundfile）。
    合成后先做首尾静音裁剪（``_trim_silence``，20260930：消段间空档 + 感知首音提前）；
    近静音输出（文本相关 dec 塌陷）先经 ``_synthesize_with_silence_repair``
    去 DC 重合成修复（2026-10-02，见 `20261002_模块0_TTS静音塌陷修复.md`），再裁剪。
    """
    text = str(req.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "合成文本为空"}, b""
    config_path, ckpt_path = _voice_config_paths(str(req.get("voice") or ""))
    engine = engine_cache.get(config_path=config_path, ckpt_path=ckpt_path)
    speed = float(req.get("speed") or 1.0)
    audio = engine.tts_to_file(text, 0, output_path=None, speed=speed, quiet=True)
    audio = _synthesize_with_silence_repair(engine, text, speed, audio)
    sampling_rate = int(engine.hps.data.sampling_rate)
    audio = _trim_silence(audio, sampling_rate)
    payload = _pack_wav(audio, sampling_rate)
    return (
        {
            "ok": True,
            "length": len(payload),
            "sampling_rate": sampling_rate,
            "format": "wav",
        },
        payload,
    )


#: ASR 目标采样率（SenseVoice 期望 16kHz 单声道）。
ASR_TARGET_SAMPLE_RATE = 16000


def _resample(waveform, source_rate, target_rate):
    """把 float32 波形从 source_rate 重采样到 target_rate。

    优先 torchaudio（保真），不可用时回落 numpy 线性插值；两者均失败则原样返回
    （宁可识别劣化也不中断请求）。

    :param waveform: numpy float32 一维波形
    :param source_rate: 源采样率（Hz）
    :param target_rate: 目标采样率（Hz）
    :return: 重采样后的 float32 波形
    """
    import numpy as np

    try:
        import torch
        import torchaudio

        tensor = torch.from_numpy(np.asarray(waveform, dtype=np.float32))
        return (
            torchaudio.functional.resample(tensor, source_rate, target_rate)
            .numpy()
            .astype(np.float32)
        )
    except Exception:  # noqa: BLE001 - 兜底线性插值（不依赖 torchaudio）
        try:
            count = max(1, int(len(waveform) * target_rate / source_rate))
            src_index = np.arange(len(waveform), dtype=np.float64)
            dst_index = np.linspace(0, len(waveform) - 1, count)
            return np.interp(dst_index, src_index, np.asarray(waveform, dtype=np.float64)).astype(
                np.float32
            )
        except Exception:  # noqa: BLE001 - 最终兜底：原样返回
            return waveform


def _handle_asr(asr_model, req):
    """处理 asr 请求：int16 PCM base64 →（按需重采样到 16k）→ 转写。

    SenseVoice 期望 **16kHz** 输入；桥接请求携带 ``sample_rate``（缺省 16000），
    非 16k 时先重采样再识别——否则 44.1kHz 等音频会被"慢放"，造成丢字与
    语种/情感误判（2026-09-25 实测）。

    转写经 :meth:`_AsrModel.generate` 统一入口（ONNX 优先 / torch 回退），返回
    富文本标签原文。标签解析（情感/事件/纯文本）不在本进程做——主进程经
    ``lite.audio.asr._parse_funasr_result`` 复用同一实现，避免双份漂移。
    """
    import numpy as np

    raw_b64 = req.get("audio_b64") or ""
    if not raw_b64:
        return {"ok": False, "error": "音频载荷为空"}, b""
    try:
        pcm = base64.b64decode(raw_b64, validate=True)
    except Exception as exc:  # noqa: BLE001 - 无效 base64 归为业务错误
        return {"ok": False, "error": f"音频 base64 解码失败：{exc}"}, b""
    waveform = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    source_rate = int(req.get("sample_rate") or ASR_TARGET_SAMPLE_RATE)
    if source_rate > 0 and source_rate != ASR_TARGET_SAMPLE_RATE:
        waveform = _resample(waveform, source_rate, ASR_TARGET_SAMPLE_RATE)
    raw = asr_model.generate(waveform)
    return {"ok": True, "raw": raw}, b""


#: llama-cli 相对安装根的落点（llama.cpp 预编译二进制，见规划 §四-15）。
LLAMA_CLI_REL = ("runtime", "llama", "llama-cli.exe")

# ------------------------------------------------------------------ #
# embed op（20261002 批 B：嵌入 ONNX 化——桥协议侧）                    #
# ------------------------------------------------------------------ #

#: 嵌入 ONNX 资产目录名（``<root>/runtime/voice_bridge/embed_onnx/``；含
#: model.onnx + tokenizer 文件。资产缺失 → embed op 返回 ok=false 中文错误，
#: 主进程按此回退 llama.cpp 嵌入，不中断）。
EMBED_ORT_ASSET_DIR_NAME = "embed_onnx"
#: embed op 单请求批次上限（超出整批拒绝，报中文错误）。
EMBED_BATCH_LIMIT = 32
#: embed op 单文本字符上限（超出整批拒绝，报中文错误）。
EMBED_TEXT_CHAR_LIMIT = 2000


class _EmbedModel:
    """Qwen3-Embedding ONNX 引擎（懒加载；last-token pooling + L2 归一）。

    语义对齐 Qwen3-Embedding 官方 last-token 口径：取每个序列**最后一个有效
    token**（右 padding 时 = ``attention_mask.sum(1) - 1``）位置的 hidden state，
    再做 L2 归一。与 llama.cpp GGUF 嵌入口径同构（均不加 instruction 前缀，
    上层用法语义由主进程决定）。

    资产要求：``model.onnx``（输入 input_ids / attention_mask 动态轴）+
    transformers 可加载的 tokenizer 文件。任一缺失/加载失败 → :meth:`embed`
    抛 RuntimeError（中文），由 embed op 转 ok=false（主进程回退 llama.cpp）。
    """

    def __init__(self, root):
        """记录资产目录；会话与 tokenizer 在首次请求时加载。"""
        self._dir = os.path.join(root, "runtime", "voice_bridge", EMBED_ORT_ASSET_DIR_NAME)
        self._session = None
        self._tokenizer = None

    def get(self):
        """取（或加载）ORT 会话与 tokenizer（CPU EP；嵌入负载小，固定 CPU 稳妥）。"""
        if self._session is not None:
            return self._session
        model_file = os.path.join(self._dir, "model.onnx")
        if not os.path.isfile(model_file):
            raise RuntimeError(f"嵌入 ONNX 资产缺失（{model_file}），无法执行 embed")
        try:
            import onnxruntime as ort
        except Exception as exc:  # noqa: BLE001 - 依赖缺失归为资产不可用
            raise RuntimeError(f"onnxruntime 不可用（{type(exc).__name__}: {exc}），无法执行 embed") from exc
        try:
            from transformers import AutoTokenizer

            tokenizer = AutoTokenizer.from_pretrained(self._dir)
        except Exception as exc:  # noqa: BLE001 - tokenizer 缺失/不兼容归为资产不可用
            raise RuntimeError(f"嵌入 tokenizer 加载失败（{type(exc).__name__}: {exc}），无法执行 embed") from exc
        try:
            opts = ort.SessionOptions()
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            session = ort.InferenceSession(
                model_file, sess_options=opts, providers=["CPUExecutionProvider"]
            )
        except Exception as exc:  # noqa: BLE001 - 会话构造失败归为资产不可用
            raise RuntimeError(f"嵌入 ONNX 会话构造失败（{type(exc).__name__}: {exc}），无法执行 embed") from exc
        self._tokenizer = tokenizer
        self._session = session
        _log(f"嵌入 ONNX 已启用（目录 {self._dir}，CPU EP）")
        return self._session

    def embed(self, texts):
        """批量文本嵌入：``list[str]`` → L2 归一 float32 向量列表（list[list[float]]）。"""
        import numpy as np

        session = self.get()
        encoded = self._tokenizer(
            list(texts), padding=True, truncation=True, max_length=8192, return_tensors="np"
        )
        input_ids = np.ascontiguousarray(encoded["input_ids"].astype(np.int64))
        attention_mask = np.ascontiguousarray(encoded["attention_mask"].astype(np.int64))
        outputs = session.run(None, {"input_ids": input_ids, "attention_mask": attention_mask})
        hidden = np.asarray(outputs[0], dtype=np.float32)  # (B, L, H)
        last_idx = attention_mask.sum(axis=1).astype(np.int64) - 1  # 最后有效 token 位置
        batch_idx = np.arange(hidden.shape[0])
        pooled = hidden[batch_idx, last_idx]  # (B, H) last-token pooling
        norm = np.linalg.norm(pooled, axis=1, keepdims=True)
        norm = np.where(norm > 0, norm, 1.0)
        return (pooled / norm).astype(np.float32).tolist()


def _handle_embed(embed_model, req):
    """处理 embed 请求：``{"op":"embed","texts":[...]} → {"ok":true,"dim":N,"vectors":[[...]]}。

    校验（整批拒绝，中文错误）：texts 须为非空列表；批次 ≤ :data:`EMBED_BATCH_LIMIT`；
    单文本 ≤ :data:`EMBED_TEXT_CHAR_LIMIT` 字。引擎侧失败（资产缺失 / 推理异常）
    统一转 ok=false——主进程按此回退 llama.cpp 嵌入，不中断。
    """
    texts = req.get("texts")
    if not isinstance(texts, list) or not texts:
        return {"ok": False, "error": "embed 请求须携带非空 texts 列表"}, b""
    if len(texts) > EMBED_BATCH_LIMIT:
        return {
            "ok": False,
            "error": f"embed 批次超限：{len(texts)} 条 > {EMBED_BATCH_LIMIT} 条上限",
        }, b""
    norm_texts = []
    for item in texts:
        text = str(item or "")
        if len(text) > EMBED_TEXT_CHAR_LIMIT:
            return {
                "ok": False,
                "error": f"embed 单文本超限：{len(text)} 字 > {EMBED_TEXT_CHAR_LIMIT} 字上限",
            }, b""
        norm_texts.append(text)
    try:
        vectors = embed_model.embed(norm_texts)
    except Exception as exc:  # noqa: BLE001 - 引擎侧失败转 ok=false（主进程回退 llama.cpp）
        return {"ok": False, "error": f"嵌入引擎不可用：{exc}"}, b""
    dim = len(vectors[0]) if vectors else 0
    return {"ok": True, "dim": dim, "vectors": vectors}, b""


#: chat 单次生成的默认上限（请求可覆盖）。
CHAT_DEFAULT_MAX_TOKENS = 128
#: chat 请求默认上下文窗口（请求可覆盖）。
CHAT_DEFAULT_N_CTX = 2048
#: chat 默认 GPU 卸载层数（0 = 纯 CPU；请求可覆盖）。
CHAT_DEFAULT_N_GPU_LAYERS = 0
#: chat 默认采样温度（请求可覆盖）。
CHAT_DEFAULT_TEMPERATURE = 0.7
#: chat 固定随机种子（复现性；llama-cli ``-s``）。
CHAT_DEFAULT_SEED = 42
#: chat 请求默认超时（秒）——含模型冷加载；与前端 300s 请求口径一致。
CHAT_DEFAULT_TIMEOUT_S = 300
#: llama-cli 输出中的性能块起点（回复解析的终止锚点，2026-09-25 实测形态）。
CHAT_PERF_MARKER = "[ Prompt:"


def _llama_cli_path(root):
    """llama-cli 绝对路径（``<root>/runtime/llama/llama-cli.exe``）。"""
    return os.path.join(root, *LLAMA_CLI_REL)


def _safe_int(value, default):
    """整数取值兜底：None / 非法值回退 default。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default):
    """浮点取值兜底：None / 非法值回退 default。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def build_chat_argv(cli, model, prompt, max_tokens, n_ctx, n_gpu_layers,
                    temperature, seed=CHAT_DEFAULT_SEED):
    """构造 llama-cli 单轮调用 argv（口径与 2026-09-25 实测一致）。

    ``-st`` 单轮、``--simple-io`` 简化 IO、``--no-display-prompt`` 降噪；实测
    stdout 仍含 ``> <提示词>`` 回显，须经 :func:`parse_chat_output` 解析。
    """
    return [
        str(cli), "-m", str(model), "-p", str(prompt),
        "-n", str(_safe_int(max_tokens, CHAT_DEFAULT_MAX_TOKENS)),
        "-c", str(_safe_int(n_ctx, CHAT_DEFAULT_N_CTX)),
        "-ngl", str(_safe_int(n_gpu_layers, CHAT_DEFAULT_N_GPU_LAYERS)),
        "-s", str(_safe_int(seed, CHAT_DEFAULT_SEED)),
        "--temp", str(_safe_float(temperature, CHAT_DEFAULT_TEMPERATURE)),
        "--no-display-prompt", "-st", "--simple-io",
    ]


def parse_chat_output(text, prompt):
    """从 llama-cli 输出中提取回复文本（2026-09-25 实测形态的解析口径）。

    实测输出：``...banner...\\n\\n> <提示词回显>\\n<回复>\\n\\n[ Prompt: ... ]\\n\\nExiting...``。
    CLI stdout 为文本模式（``\\n`` 实际打印成 ``\\r\\n``），故先统一行尾再匹配；解析两步：
    ① 切掉性能块起点（``[ Prompt:``）之后；② 取提示词回显（首个完整出现）之后的内容并
    strip。回显未命中（CLI 截断等）时退化为"最后一处 ``> `` 回显行之后"。
    """
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    prompt = str(prompt).replace("\r\n", "\n").replace("\r", "\n")
    cut = text.find(CHAT_PERF_MARKER)
    if cut != -1:
        text = text[:cut]
    idx = text.find(prompt)
    if idx != -1:
        text = text[idx + len(prompt):]
    else:
        marker = text.rfind("\n> ")
        if marker != -1:
            text = text[marker + 3:]
            newline = text.find("\n")
            if newline != -1:
                text = text[newline + 1:]
    return text.strip()


def _default_llama_runner(argv, timeout):
    """默认 runner：跑一次 llama-cli，返回 ``(returncode, stdout, stderr)`` 文本。

    超时由 :func:`subprocess.run` 抛 ``TimeoutExpired``（调用方转中文错误）；
    stdout/stderr 按 UTF-8 解码（errors=replace，解码问题不阻断解析）。
    """
    proc = subprocess.run(argv, capture_output=True, timeout=timeout)
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", errors="replace"),
        proc.stderr.decode("utf-8", errors="replace"),
    )


def _handle_chat(root, req, runner=None):
    """处理 chat 请求：llama-cli 单轮推理（外部预编译二进制，规划 §四-15）。

    返回 ``(header, payload)``（payload 恒为空字节）。runner 可注入（单测断言
    argv 与解析，不触真推理）；缺模型 / 空提示词 / 超时 / 非零退出均回中文错误。
    """
    prompt = str(req.get("prompt") or "")
    if not prompt.strip():
        return {"ok": False, "error": "提示词为空"}, b""
    model = str(req.get("model") or "").strip()
    if not model or not os.path.exists(model):
        return {"ok": False, "error": f"本地模型文件不存在：{model or '（未提供）'}"}, b""
    cli = _llama_cli_path(root)
    if runner is None and not os.path.exists(cli):
        return {"ok": False, "error": f"llama-cli 不存在：{cli}（本地推理不可用）"}, b""
    timeout = _safe_int(req.get("timeout"), CHAT_DEFAULT_TIMEOUT_S)
    if timeout <= 0:
        timeout = CHAT_DEFAULT_TIMEOUT_S
    argv = build_chat_argv(
        cli, model, prompt,
        req.get("max_tokens"), req.get("n_ctx"),
        req.get("n_gpu_layers"), req.get("temperature"),
    )
    run = runner if runner is not None else _default_llama_runner
    try:
        returncode, stdout, stderr = run(argv, timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"本地推理超时（>{timeout}s），已终止本次推理"}, b""
    except Exception as exc:  # noqa: BLE001 - 拉起失败归为业务错误
        return {"ok": False, "error": f"llama-cli 启动失败：{type(exc).__name__}: {exc}"}, b""
    if returncode != 0:
        tail = (str(stderr or "").strip() or str(stdout or "").strip())[-200:]
        return {"ok": False, "error": f"llama-cli 退出码 {returncode}：{tail}"}, b""
    reply = parse_chat_output(stdout, prompt)
    if not reply:
        return {"ok": False, "error": "本地推理未产生有效回复（输出解析为空）"}, b""
    return {"ok": True, "text": reply}, b""


def main(argv=None):
    """桥接主循环：逐行读请求、分派、按帧回响应，直至 shutdown / stdin EOF。"""
    # 最先固定协议流并隔离库打印（任何引擎加载前；funasr 等会向 stdout 裸打印）
    _capture_proto_stream()
    parser = argparse.ArgumentParser(description="CX-A 语音 sidecar 桥接进程（stdio JSON 协议 uv1）")
    parser.add_argument("--root", required=True, help="便携根（推导 data/SenseVoiceSmall 等资产路径）")
    parser.add_argument("--device", default="cpu", help="推理设备（cpu / cuda / cuda:0 等）")
    parser.add_argument(
        "--accel", default=ACCEL_DEFAULT,
        help="TTS ORT 加速后端（off / cpu / auto / cuda / dml / rocm；缺省 auto）",
    )
    parser.add_argument(
        "--accel-device", default=ACCEL_DEVICE_DEFAULT,
        help="DirectML 设备提示（空 / igpu / dgpu；仅 dml 后端消费）",
    )
    parser.add_argument("--language", default="ZH", help="MeloTTS 语言（默认 ZH）")
    parser.add_argument("--hf-endpoint", default="", help="HF 镜像端点（设置 HF_ENDPOINT，缺省不改）")
    args = parser.parse_args(argv)

    _install_torch_load_compat()
    # 加速意图归一后写入进程级全局（_load_ort_sessions 读取；非法值归一 + 中文日志）
    global _ACCEL, _ACCEL_DEVICE
    _ACCEL = _normalize_accel(args.accel)
    _ACCEL_DEVICE = _normalize_accel_device(args.accel_device)
    device = _resolve_device(args.device)
    engine_cache = _TtsEngineCache(device, language=args.language, hf_endpoint=args.hf_endpoint)
    asr_model = _AsrModel(args.root, device)
    embed_model = _EmbedModel(args.root)
    _log(
        f"已启动（protocol={PROTOCOL_VERSION} root={args.root} device={device} "
        f"accel={_ACCEL} accel_device={_ACCEL_DEVICE or 'auto'}）"
    )

    for line in sys.stdin.buffer:
        try:
            req = json.loads(line.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - 坏帧回错误响应后继续
            _write_frame({"id": None, "ok": False, "error": f"请求 JSON 解析失败：{exc}"})
            continue
        req_id = req.get("id")
        op = str(req.get("op") or "")
        try:
            if op == "ping":
                _write_frame({"id": req_id, "ok": True, "pong": True, "protocol": PROTOCOL_VERSION})
            elif op == "tts":
                header, payload = _handle_tts(engine_cache, req)
                _write_frame({"id": req_id, **header}, payload)
            elif op == "asr":
                header, payload = _handle_asr(asr_model, req)
                _write_frame({"id": req_id, **header}, payload)
            elif op == "embed":
                header, payload = _handle_embed(embed_model, req)
                _write_frame({"id": req_id, **header}, payload)
            elif op == "chat":
                header, payload = _handle_chat(args.root, req)
                _write_frame({"id": req_id, **header}, payload)
            elif op == "shutdown":
                _write_frame({"id": req_id, "ok": True, "bye": True})
                _log("收到 shutdown，退出")
                return 0
            else:
                _write_frame({"id": req_id, "ok": False, "error": f"未知操作：{op}"})
        except Exception as exc:  # noqa: BLE001 - 单请求失败不影响后续请求
            _log(f"请求处理失败（op={op}）：{traceback.format_exc()}")
            _write_frame({"id": req_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    _log("stdin 关闭，退出")
    return 0


if __name__ == "__main__":  # pragma: no cover - sidecar 直跑入口
    sys.exit(main())