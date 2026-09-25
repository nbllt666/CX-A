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
    错误     → {"id": N, "ok": false, "error": "中文描述"}
- stderr：中文诊断日志（主进程可收集，不参与协议）。

设计约束：
- 引擎按需惰性加载并常驻缓存（TTS 按 音色目录 键缓存；ASR 单例）；
- chat 为外部子进程（llama-cli），不入常驻缓存；请求内 ``timeout`` 可控（缺省 300s）；
- 单请求失败不影响后续请求（异常兜底为该请求的 ok=false 响应）；
- ``shutdown`` 后干净退出（引擎析构交由进程退出处理）。

运行方式（由主后端拉起，勿手工改参数口径）::

    <root>/runtime/voice/python.exe <本脚本> --root <便携根> --device cuda
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import traceback

#: 协议版本（客户端对齐校验用）。
PROTOCOL_VERSION = "uv1"
#: TTS 引擎缓存上限（每引擎百 MB 级权重）。
TTS_ENGINE_CACHE_LIMIT = 3
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
        self._engines[key] = engine
        while len(self._engines) > TTS_ENGINE_CACHE_LIMIT:
            oldest = next(iter(self._engines))
            self._engines.pop(oldest)
        return engine


class _AsrModel:
    """SenseVoice 模型懒加载单例。"""

    def __init__(self, root, device):
        """记录模型路径与设备；模型在首次请求时加载。"""
        self._model_path = os.path.join(root, "data", "SenseVoiceSmall")
        self._device = device
        self._model = None

    def get(self):
        """取（或加载）funasr AutoModel 实例。"""
        if self._model is not None:
            return self._model
        from funasr import AutoModel

        self._model = AutoModel(
            model=self._model_path,
            device=self._device,
            disable_update=True,
        )
        return self._model


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


#: L9 口径的 int16 采样值上限（clip 防越界回绕爆音）。
_PCM16_MAX = 32767
_PCM16_MIN = -32768


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


def _handle_tts(engine_cache, req):
    """处理 tts 请求：合成音频并返回**完整 wav 载荷**。

    返回 ``(header, payload)``：header 含 length / sampling_rate / format=wav，
    payload 为 wav 字节（主进程只做 base64 封装，不再需要 numpy/soundfile）。
    """
    text = str(req.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "合成文本为空"}, b""
    config_path, ckpt_path = _voice_config_paths(str(req.get("voice") or ""))
    engine = engine_cache.get(config_path=config_path, ckpt_path=ckpt_path)
    speed = float(req.get("speed") or 1.0)
    audio = engine.tts_to_file(text, 0, output_path=None, speed=speed, quiet=True)
    sampling_rate = int(engine.hps.data.sampling_rate)
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
    """处理 asr 请求：int16 PCM base64 →（按需重采样到 16k）→ funasr 转写。

    SenseVoice 期望 **16kHz** 输入；桥接请求携带 ``sample_rate``（缺省 16000），
    非 16k 时先重采样再识别——否则 44.1kHz 等音频会被"慢放"，造成丢字与
    语种/情感误判（2026-09-25 实测）。

    标签解析（情感/事件/纯文本）不在本进程做——主进程经
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
    model = asr_model.get()
    res = model.generate(input=waveform, language="auto", use_itn=True)
    first = res[0] if res else None
    if isinstance(first, dict):
        raw = str(first.get("text", ""))
    elif isinstance(first, (list, tuple)) and first and isinstance(first[0], dict):
        raw = str(first[0].get("text", ""))
    else:
        raw = ""
    return {"ok": True, "raw": raw}, b""


#: llama-cli 相对安装根的落点（llama.cpp 预编译二进制，见规划 §四-15）。
LLAMA_CLI_REL = ("runtime", "llama", "llama-cli.exe")
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
    parser.add_argument("--language", default="ZH", help="MeloTTS 语言（默认 ZH）")
    parser.add_argument("--hf-endpoint", default="", help="HF 镜像端点（设置 HF_ENDPOINT，缺省不改）")
    args = parser.parse_args(argv)

    _install_torch_load_compat()
    device = _resolve_device(args.device)
    engine_cache = _TtsEngineCache(device, language=args.language, hf_endpoint=args.hf_endpoint)
    asr_model = _AsrModel(args.root, device)
    _log(f"已启动（protocol={PROTOCOL_VERSION} root={args.root} device={device}）")

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