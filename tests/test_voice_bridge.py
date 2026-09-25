# -*- coding: utf-8 -*-
"""语音 sidecar 桥单测（协议 uv1：bridge 脚本 / 客户端 / 桥后端）。

覆盖三层：
1. bridge 脚本进程级协议——用当前解释器直跑 ``lite/audio/voice_bridge.py``，
   仅 ping / 坏请求路径（不加载 melo/funasr，主环境无重依赖可跑）；
2. VoiceBridgeClient 进程管理与帧协议——替身 bridge 脚本 + 真子进程；
3. BridgeTTSBackend / BridgeASRBackend 与替身 client 的契约——
   wav 封装 / 音色回退链 / 解析复用（_parse_funasr_result）。
"""

import json
import os
import struct
import subprocess
import sys
import time

import pytest

from lite.audio.asr import BridgeASRBackend, _coerce_to_pcm16_bytes
from lite.audio.tts import BridgeTTSBackend
from lite.audio.voice_bridge_client import VoiceBridgeClient, VoiceBridgeError

#: 源码 bridge 脚本路径（测试直跑对象）。
BRIDGE_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "lite", "audio", "voice_bridge.py",
)

#: 替身 bridge 脚本：覆盖 ping / tts（含载荷）/ 错误 / 卡死 / shutdown 五种路径。
#: 环境变量 ``FAKE_BRIDGE_NOISY=1`` 时，每次响应前向 stdout 打一行非 JSON 噪声
#: （模拟第三方库裸打印），用于验证客户端的协议流容忍逻辑。
_FAKE_BRIDGE = r'''
import json
import os
import sys
import time

for line in sys.stdin.buffer:
    req = json.loads(line.decode("utf-8"))
    if os.environ.get("FAKE_BRIDGE_NOISY") == "1":
        sys.stdout.write("Notice: fake library noise line\n")
        sys.stdout.flush()
    rid = req.get("id")
    op = req.get("op")
    payload = b""
    if op == "ping":
        frame = {"id": rid, "ok": True, "pong": True}
    elif op == "tts":
        payload = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 24
        frame = {"id": rid, "ok": True, "length": len(payload),
                 "sampling_rate": 44100, "format": "wav"}
    elif op == "sleep":
        time.sleep(30)
        frame = {"id": rid, "ok": True}
    elif op == "boom":
        frame = {"id": rid, "ok": False, "error": "模拟失败"}
    elif op == "shutdown":
        out = sys.stdout.buffer
        out.write((json.dumps({"id": rid, "ok": True, "bye": True}) + "\n").encode())
        out.flush()
        break
    else:
        frame = {"id": rid, "ok": False, "error": "未知操作"}
    out = sys.stdout.buffer
    out.write((json.dumps(frame) + "\n").encode())
    if payload:
        out.write(payload)
    out.flush()
'''


def _write_fake_bridge(tmp_path):
    """把替身 bridge 脚本落盘并返回路径。"""
    path = tmp_path / "fake_bridge.py"
    path.write_text("import struct\n" + _FAKE_BRIDGE, encoding="utf-8")
    return str(path)


def _read_frame(stream):
    """从字节流读一帧（单行 JSON 头 + 可选载荷），返回 ``(header, payload)``。"""
    header_line = stream.readline()
    assert header_line, "桥无响应（输出流关闭）"
    header = json.loads(header_line.decode("utf-8"))
    length = int(header.get("length") or 0)
    payload = stream.read(length) if length else b""
    return header, payload


# ------------------------------------------------------------------ #
# 1. bridge 脚本进程级协议（真实脚本直跑）                              #
# ------------------------------------------------------------------ #

def test_bridge_script_ping_and_shutdown(tmp_path):
    """真 bridge 脚本：ping 回 pong 帧；shutdown 后进程以 0 退出（EOF 机制）。"""
    proc = subprocess.Popen(
        [sys.executable, BRIDGE_SRC, "--root", str(tmp_path), "--device", "cpu"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        proc.stdin.write(b'{"op": "ping", "id": 1}\n')
        proc.stdin.flush()
        header, _payload = _read_frame(proc.stdout)
        assert header["ok"] is True and header["pong"] is True
        assert header["protocol"] == "uv1"

        proc.stdin.write(b'{"op": "shutdown", "id": 2}\n')
        proc.stdin.flush()
        header, _payload = _read_frame(proc.stdout)
        assert header["bye"] is True
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_bridge_script_bad_json_and_unknown_op(tmp_path):
    """坏 JSON 与未知操作均回 ok=false 错误帧且进程继续存活。"""
    proc = subprocess.Popen(
        [sys.executable, BRIDGE_SRC, "--root", str(tmp_path), "--device", "cpu"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        proc.stdin.write(b'not-a-json\n')
        proc.stdin.flush()
        header, _payload = _read_frame(proc.stdout)
        assert header["ok"] is False and "解析失败" in header["error"]

        proc.stdin.write(b'{"op": "nope", "id": 7}\n')
        proc.stdin.flush()
        header, _payload = _read_frame(proc.stdout)
        assert header["ok"] is False and header["id"] == 7
        assert "未知操作" in header["error"]

        # 进程仍存活（可继续 ping）
        proc.stdin.write(b'{"op": "ping", "id": 8}\n')
        proc.stdin.flush()
        header, _payload = _read_frame(proc.stdout)
        assert header["pong"] is True
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_bridge_script_hf_endpoint_env_set(tmp_path):
    """--hf-endpoint 注入后，子进程环境在引擎加载前设置 HF_ENDPOINT（经 ping 过程验证存活）。"""
    proc = subprocess.Popen(
        [sys.executable, BRIDGE_SRC, "--root", str(tmp_path), "--device", "cpu",
         "--hf-endpoint", "https://hf-mirror.test"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        proc.stdin.write(b'{"op": "ping", "id": 1}\n')
        proc.stdin.flush()
        header, _payload = _read_frame(proc.stdout)
        assert header["pong"] is True
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


# ------------------------------------------------------------------ #
# 2. VoiceBridgeClient（替身脚本 + 真子进程）                           #
# ------------------------------------------------------------------ #

def _client(tmp_path, timeout=10):
    """构造指向替身 bridge 的客户端（用当前解释器直跑）。"""
    return VoiceBridgeClient(
        root=str(tmp_path),
        python_exe=sys.executable,
        script_path=_write_fake_bridge(tmp_path),
        timeout=timeout,
    )


def test_client_device_normalization():
    """设备归一：gpu → auto；其余原样；空值回 cpu。"""
    assert VoiceBridgeClient._normalize_device("gpu") == "auto"
    assert VoiceBridgeClient._normalize_device("GPU ") == "auto"
    assert VoiceBridgeClient._normalize_device("cuda:0") == "cuda:0"
    assert VoiceBridgeClient._normalize_device("") == "cpu"
    assert VoiceBridgeClient._normalize_device(None) == "cpu"


def test_client_ping_tts_and_restart(tmp_path):
    """客户端：ping / tts（含载荷）帧正确；进程被杀后自动重启继续服务。"""
    client = _client(tmp_path)
    try:
        header, _payload = client.request({"op": "ping"})
        assert header["pong"] is True

        header, payload = client.request({"op": "tts", "text": "hi"})
        assert header["sampling_rate"] == 44100
        assert header["format"] == "wav"
        assert payload[:4] == b"RIFF"  # uv1：tts 载荷为完整 wav 字节

        # 模拟进程崩溃：下次请求应自动重启并成功
        client._proc.kill()
        client._proc.wait(timeout=10)
        header, _payload = client.request({"op": "ping"})
        assert header["pong"] is True
    finally:
        client.close()
    assert client._proc is None


def test_client_error_response_raises(tmp_path):
    """sidecar 返回 ok=false：客户端抛 VoiceBridgeError 且保留中文错误。"""
    client = _client(tmp_path)
    try:
        with pytest.raises(VoiceBridgeError) as ei:
            client.request({"op": "boom"})
        assert "模拟失败" in str(ei.value)
    finally:
        client.close()


def test_client_timeout_kills_and_raises(tmp_path):
    """卡死请求：超时抛错并终止卡死进程（不悬挂）。"""
    client = _client(tmp_path, timeout=0.5)
    try:
        with pytest.raises(VoiceBridgeError) as ei:
            client.request({"op": "sleep"})
        assert "超时" in str(ei.value)
        # 卡死进程已被回收（重启一次后仍卡死 → 最终失败并清理）
        assert client._proc is None or client._proc.poll() is not None
    finally:
        client.close()


def test_client_unavailable_and_resolve(tmp_path):
    """available()：解释器/脚本缺失时为 False；脚本解析分发落点优先。"""
    missing = VoiceBridgeClient(
        root=str(tmp_path), python_exe=str(tmp_path / "no-python.exe"),
        script_path=str(tmp_path / "no-script.py"),
    )
    assert missing.available() is False

    # 分发落点存在时 _resolve_script 应命中 <root>/runtime/voice_bridge/bridge.py
    distributed = tmp_path / "runtime" / "voice_bridge"
    distributed.mkdir(parents=True)
    (distributed / "bridge.py").write_text("# bridge", encoding="utf-8")
    resolved = VoiceBridgeClient(root=str(tmp_path))
    assert resolved.script_path == str(distributed / "bridge.py")


def test_client_close_idempotent(tmp_path):
    """close() 幂等：未启动进程时无副作用；启动后可重复关闭。"""
    client = _client(tmp_path)
    client.close()  # 未启动：无副作用
    client.request({"op": "ping"})
    client.close()
    client.close()
    assert client._proc is None


def test_client_env_redirects_to_portable_root(tmp_path):
    """子进程环境重定向：HF_HOME / NLTK_DATA / TEMP 落便携根；HF_ENDPOINT 仅在显式给出时注入。"""
    root = str(tmp_path)
    client = VoiceBridgeClient(root=root)
    env = client._build_env()
    assert env["HF_HOME"] == os.path.join(root, "data", "hf_cache")
    assert env["NLTK_DATA"] == os.path.join(root, "data", "nltk_data")
    assert env["TEMP"] == os.path.join(root, "data", "tmp")
    assert env["TMP"] == os.path.join(root, "data", "tmp")

    # 缺省不主动注入 HF_ENDPOINT（走官方端点）；显式给出时注入
    assert client._build_env().get("HF_ENDPOINT") == os.environ.get("HF_ENDPOINT")
    explicit = VoiceBridgeClient(root=root, hf_endpoint="https://mirror.test")
    assert explicit._build_env()["HF_ENDPOINT"] == "https://mirror.test"


def test_client_skips_non_json_noise_lines(tmp_path, monkeypatch):
    """容忍兜底：bridge 前导非 JSON 行被跳过并记入诊断，协议请求仍成功。

    真实场景：funasr 加载时向 stdout 裸打印 "Notice: ffmpeg is not installed..."。
    """
    monkeypatch.setenv("FAKE_BRIDGE_NOISY", "1")
    client = _client(tmp_path)
    try:
        header, _payload = client.request({"op": "ping"})
        assert header["pong"] is True
        assert any("非协议输出" in line for line in client._stderr_lines)
    finally:
        client.close()


def test_capture_proto_stream_moves_stdout_to_stderr(monkeypatch):
    """bridge 协议流隔离：捕获原始 buffer 为协议流，sys.stdout 指向 stderr。"""
    import io

    from lite.audio import voice_bridge

    class _FakeStdout:
        """最小 stdout 替身：协议捕获只需 .buffer 属性。"""

        def __init__(self, buffer):
            """记录替身缓冲。"""
            self.buffer = buffer

    buffer = io.BytesIO()
    monkeypatch.setattr(voice_bridge, "_PROTO_OUT", None)
    monkeypatch.setattr(sys, "stdout", _FakeStdout(buffer))

    voice_bridge._capture_proto_stream()

    assert voice_bridge._PROTO_OUT is buffer
    assert sys.stdout is sys.stderr


# ------------------------------------------------------------------ #
# 3. Bridge 后端契约（替身 client）                                     #
# ------------------------------------------------------------------ #

class _StubClient:
    """替身桥客户端：记录请求并返回预设帧。"""

    def __init__(self, header=None, payload=b""):
        """预设响应帧（header / payload）与调用记录容器。"""
        self._header = {"ok": True, **(header or {})}
        self._payload = payload
        self.calls = []

    def request(self, payload, timeout=None):
        """记录请求并返回预设帧（不触碰真实进程）。"""
        self.calls.append(payload)
        return dict(self._header), self._payload


def test_bridge_tts_backend_passes_wav_payload_through(tmp_path):
    """BridgeTTSBackend：载荷即完整 WAV，主进程**原样透传**（不再自行封装）。

    2026-09-25 打包态缺陷回归：封装曾在主进程做（需 numpy + soundfile），而冻结
    后端刻意排除二者 → 真机朗读/识别直接 503「No module named 'numpy'」。
    """
    from lite.audio import voice_bridge

    wav = voice_bridge._pack_wav([0.0, 0.5, -0.5, 0.25], 44100)
    stub = _StubClient(
        {"sampling_rate": 44100, "length": len(wav), "format": "wav"}, wav
    )
    backend = BridgeTTSBackend(client=stub, voice_dir=str(tmp_path))
    with pytest.warns(UserWarning):  # 音色目录缺训练产物 → 回退官方默认（G-4 告警）
        got = backend.synthesize("你好")
    assert got == wav
    assert got[:4] == b"RIFF" and got[8:12] == b"WAVE"
    assert stub.calls[0]["op"] == "tts"
    assert stub.calls[0]["text"] == "你好"


def test_bridge_tts_backend_works_without_numpy_or_soundfile(tmp_path, monkeypatch):
    """冻结包口径：主进程路径不得依赖 numpy / soundfile（导入即失败为回归）。"""
    import builtins

    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name.split(".")[0] in ("numpy", "soundfile"):
            raise ImportError(f"blocked for test: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    wav = b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 24
    stub = _StubClient({"sampling_rate": 44100, "format": "wav"}, wav)
    backend = BridgeTTSBackend(client=stub, voice_dir=str(tmp_path))
    with pytest.warns(UserWarning):
        assert backend.synthesize("你好") == wav


def test_bridge_asr_backend_bytes_path_without_numpy(tmp_path, monkeypatch):
    """冻结包口径：前端上传的 bytes PCM 走 ASR 主进程路径时不得触发 numpy 导入。"""
    import builtins

    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name.split(".")[0] == "numpy":
            raise ImportError(f"blocked for test: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    stub = _StubClient({"raw": "<|zh|><|NEUTRAL|><|Speech|>你好"})
    backend = BridgeASRBackend(client=stub)
    assert backend.transcribe(b"\x01\x00" * 8, sample_rate=16000)["text"] == "你好"


def test_pack_wav_clips_out_of_range_samples():
    """_pack_wav：越界样本被 clip（不回绕），产出标准 16-bit PCM WAV 头。"""
    import io
    import wave

    from lite.audio import voice_bridge

    wav = voice_bridge._pack_wav([2.0, -2.0, 0.0], 16000)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    assert len(wav) == 44 + 3 * 2  # 44 字节头 + 3 样本 × 2 字节
    with wave.open(io.BytesIO(wav), "rb") as reader:
        assert reader.getframerate() == 16000
        assert reader.getnchannels() == 1
        assert reader.getsampwidth() == 2
        frames = reader.readframes(3)
    assert struct.unpack("<3h", frames) == (32767, -32768, 0)


def test_bridge_tts_backend_rejects_traversal_voice(tmp_path):
    """音色标识含路径穿越：告警并回退默认音色（请求里的 voice 仍是合法目录）。"""
    stub = _StubClient({"sampling_rate": 44100}, b"RIFF\x00\x00\x00\x00WAVE")
    backend = BridgeTTSBackend(client=stub, voice_dir=str(tmp_path))
    with pytest.warns(UserWarning):
        backend.synthesize("你好", voice="../../etc/passwd")
    voice_arg = stub.calls[0]["voice"]
    assert ".." not in voice_arg
    assert voice_arg.startswith(str(tmp_path))


def test_bridge_asr_backend_parses_raw(tmp_path):
    """BridgeASRBackend：富文本 raw 经 _parse_funasr_result 解析出 text/emotion/event。"""
    stub = _StubClient({"raw": "<|zh|><|HAPPY|><|Speech|>你好世界"})
    backend = BridgeASRBackend(client=stub)
    result = backend.transcribe(b"\x00\x00" * 16)
    assert result["text"] == "你好世界"
    assert result["emotion"] == "HAPPY"
    assert result["event"] == "Speech"
    assert stub.calls[0]["op"] == "asr"


def test_coerce_to_pcm16_bytes_variants():
    """PCM 归一：bytes 原样；int16 ndarray 原样；float 数组按 ×32768 钳制。"""
    assert _coerce_to_pcm16_bytes(b"\x01\x02") == b"\x01\x02"
    out = _coerce_to_pcm16_bytes([0.0, 1.0, -1.0])
    assert struct.unpack("<3h", out) == (0, 32767, -32768)


# ------------------------------------------------------------------ #
# 4. 装配接线：_try_voice_bridge（sidecar 结构探测）                     #
# ------------------------------------------------------------------ #

def test_try_voice_bridge_returns_backends_when_sidecar_present(tmp_path):
    """sidecar 结构齐备（python.exe + bridge 脚本）时返回共享 client 的双后端。"""
    from lite.audio import _try_voice_bridge

    root = tmp_path / "portable"
    (root / "runtime" / "voice").mkdir(parents=True)
    (root / "runtime" / "voice" / "python.exe").write_bytes(b"fake")
    (root / "runtime" / "voice_bridge").mkdir(parents=True)
    (root / "runtime" / "voice_bridge" / "bridge.py").write_text("# bridge", encoding="utf-8")

    backends = _try_voice_bridge({"device": "gpu"}, {"device": "gpu", "voice": "cx-open"}, root=str(root))
    assert backends is not None
    asr_backend, tts_backend = backends
    assert isinstance(asr_backend, BridgeASRBackend)
    assert isinstance(tts_backend, BridgeTTSBackend)
    # 共享同一 client（单常驻进程）
    assert asr_backend.client is tts_backend.client
    assert asr_backend.client.device == "auto"  # gpu 归一


def test_try_voice_bridge_none_when_sidecar_absent(tmp_path):
    """sidecar 结构缺失时返回 None（回退既有进程内路径，零副作用）。"""
    from lite.audio import _try_voice_bridge

    assert _try_voice_bridge({}, {}, root=str(tmp_path / "nope")) is None


# ------------------------------------------------------------------ #
# 5. ASR 采样率透传与重采样（44.1k 慢放问题修复）                        #
# ------------------------------------------------------------------ #

def test_bridge_asr_backend_passes_sample_rate(tmp_path):
    """BridgeASRBackend：sample_rate 写入请求；缺省回落 16000（历史口径）。"""
    stub = _StubClient({"raw": "<|zh|>你好"})
    backend = BridgeASRBackend(client=stub)

    backend.transcribe(b"\x00\x00" * 8, sample_rate=44100)
    assert stub.calls[0]["sample_rate"] == 44100
    assert stub.calls[0]["op"] == "asr"

    backend.transcribe(b"\x00\x00" * 8)
    assert stub.calls[1]["sample_rate"] == 16000


def test_bridge_resample_output_length():
    """_resample：44.1k → 16k 后样本数约为原长 × 16000/44100（±2 容差）。"""
    import numpy as np

    from lite.audio import voice_bridge

    wave = np.zeros(44100, dtype=np.float32)
    out = voice_bridge._resample(wave, 44100, voice_bridge.ASR_TARGET_SAMPLE_RATE)
    assert abs(len(out) - 16000) <= 2


# ------------------------------------------------------------------ #
# 6. chat op（llama-cli 外部推理：argv / 解析 / 失败路径）              #
# ------------------------------------------------------------------ #

def test_chat_argv_shape():
    """chat argv 口径：-m/-p/-n/-c/-ngl/-s/--temp/--no-display-prompt/-st/--simple-io。"""
    from lite.audio import voice_bridge

    argv = voice_bridge.build_chat_argv(
        "C:/x/llama-cli.exe", "C:/m/m.gguf", "你好",
        max_tokens=48, n_ctx=512, n_gpu_layers=-1, temperature=0.7,
    )
    assert argv[0].endswith("llama-cli.exe")
    assert argv[1] == "-m" and argv[2] == "C:/m/m.gguf"
    assert argv[3] == "-p" and argv[4] == "你好"
    joined = " ".join(argv)
    for token in ("-n 48", "-c 512", "-ngl -1", "-s 42", "--temp 0.7",
                  "--no-display-prompt", "-st", "--simple-io"):
        assert token in joined


def test_chat_argv_defaults_on_bad_values():
    """argv 取值兜底：非法/None 参数回退桥侧默认。"""
    from lite.audio import voice_bridge

    argv = voice_bridge.build_chat_argv(
        "cli", "m.gguf", "hi", max_tokens=None, n_ctx="bad",
        n_gpu_layers=None, temperature=None,
    )
    joined = " ".join(argv)
    assert f"-n {voice_bridge.CHAT_DEFAULT_MAX_TOKENS}" in joined
    assert f"-c {voice_bridge.CHAT_DEFAULT_N_CTX}" in joined
    assert f"-ngl {voice_bridge.CHAT_DEFAULT_N_GPU_LAYERS}" in joined
    assert f"--temp {voice_bridge.CHAT_DEFAULT_TEMPERATURE}" in joined


def test_chat_parse_output_extracts_reply():
    """解析：切性能块 + 取 prompt 回显之后的内容（真实输出形态的浓缩样本）。"""
    from lite.audio import voice_bridge

    raw = (
        "Loading model...\n"
        "build      : b11178-f9af9be21\n"
        "\n\n> 你好，请用一句话介绍你自己\n我是Qwen，由阿里云创建的AI助手。\n\n"
        "[ Prompt: 404.9 t/s | Generation: 142.5 t/s ]\n\n\nExiting...\n"
    )
    reply = voice_bridge.parse_chat_output(raw, "你好，请用一句话介绍你自己")
    assert reply == "我是Qwen，由阿里云创建的AI助手。"


def test_chat_parse_output_fallback_without_echo_match():
    """回显未命中（CLI 截断等）时退化取 ``> `` 回显行之后内容。"""
    from lite.audio import voice_bridge

    raw = "banner\n> 这是一段被截断的提示…\n真正的回复\n\n[ Prompt: 1.0 t/s ]\nExiting...\n"
    assert voice_bridge.parse_chat_output(raw, "完整提示词（未出现在输出中）") == "真正的回复"


def test_chat_parse_output_handles_crlf_and_multiline_prompt():
    """实测回归：CLI 文本模式把回显的 ``\\n`` 打成 ``\\r\\n``——多行提示词仍须被完整剥离。

    首跑探针曾因 CRLF 致回显匹配失败而走兜底分支，回复里残留提示词末行
    （"user: 你好，请用一句话介绍你自己。"），本用例钉住该修复。
    """
    from lite.audio import voice_bridge

    prompt = "system: 你是 CX-A，一个桌宠助手。\nuser: 你好，请用一句话介绍你自己。"
    raw = (
        "Loading model...\n\n"
        "> system: 你是 CX-A，一个桌宠助手。\r\nuser: 你好，请用一句话介绍你自己。\r\n"
        "你好，我是 CX-A，很高兴认识你。\r\n\r\n"
        "[ Prompt: 100.0 t/s | Generation: 40.0 t/s ]\r\n\r\nExiting...\r\n"
    )
    assert voice_bridge.parse_chat_output(raw, prompt) == "你好，我是 CX-A，很高兴认识你。"


def test_handle_chat_success_with_runner(tmp_path):
    """_handle_chat：runner 注入下断言 argv 形状，解析回复并回 ok=true。"""
    from lite.audio import voice_bridge

    model = tmp_path / "m.gguf"
    model.write_bytes(b"gguf")
    seen = {}

    def fake_runner(argv, timeout):
        seen["argv"] = argv
        seen["timeout"] = timeout
        return 0, "banner\n> 你好\n回复内容\n\n[ Prompt: 1.0 t/s ]\n\nExiting...\n", ""

    header, payload = voice_bridge._handle_chat(
        str(tmp_path), {"prompt": "你好", "model": str(model)}, runner=fake_runner
    )
    assert header["ok"] is True and header["text"] == "回复内容"
    assert payload == b""
    assert seen["argv"][0] == voice_bridge._llama_cli_path(str(tmp_path))
    assert seen["timeout"] == voice_bridge.CHAT_DEFAULT_TIMEOUT_S


def test_handle_chat_rejects_empty_prompt_and_missing_model(tmp_path):
    """空提示词 / 缺模型文件：回中文错误且不触发 runner。"""
    from lite.audio import voice_bridge

    def boom(argv, timeout):  # pragma: no cover - 不应被调用
        raise AssertionError("runner 不应被调用")

    header, _ = voice_bridge._handle_chat(
        str(tmp_path), {"prompt": "hi", "model": str(tmp_path / "nope.gguf")}, runner=boom
    )
    assert header["ok"] is False and "不存在" in header["error"]

    header, _ = voice_bridge._handle_chat(
        str(tmp_path), {"prompt": "   ", "model": "x"}, runner=boom
    )
    assert header["ok"] is False and "提示词为空" in header["error"]


def test_handle_chat_timeout_and_nonzero_exit(tmp_path):
    """超时 → 中文超时错误；非零退出 → 带 stderr 尾部的错误。"""
    from lite.audio import voice_bridge

    model = tmp_path / "m.gguf"
    model.write_bytes(b"gguf")

    def timeout_runner(argv, timeout):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    header, _ = voice_bridge._handle_chat(
        str(tmp_path), {"prompt": "hi", "model": str(model)}, runner=timeout_runner
    )
    assert header["ok"] is False and "超时" in header["error"]

    def fail_runner(argv, timeout):
        return 2, "", "CUDA error: out of memory"

    header, _ = voice_bridge._handle_chat(
        str(tmp_path), {"prompt": "hi", "model": str(model)}, runner=fail_runner
    )
    assert header["ok"] is False and "退出码 2" in header["error"]
    assert "CUDA error" in header["error"]


def test_handle_chat_cli_missing_without_runner(tmp_path):
    """无 runner 且 cli 不在位：回「llama-cli 不存在」错误（不拉起进程）。"""
    from lite.audio import voice_bridge

    model = tmp_path / "m.gguf"
    model.write_bytes(b"gguf")
    header, _ = voice_bridge._handle_chat(str(tmp_path), {"prompt": "hi", "model": str(model)})
    assert header["ok"] is False and "llama-cli 不存在" in header["error"]


def test_bridge_script_chat_missing_model_returns_error(tmp_path):
    """真 bridge 脚本：chat 请求缺模型文件 → 中文错误帧，进程继续存活（dispatch 接通）。"""
    proc = subprocess.Popen(
        [sys.executable, BRIDGE_SRC, "--root", str(tmp_path), "--device", "cpu"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        req = {"op": "chat", "id": 11, "prompt": "你好", "model": str(tmp_path / "nope.gguf")}
        proc.stdin.write((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
        proc.stdin.flush()
        header, _payload = _read_frame(proc.stdout)
        assert header["ok"] is False and "不存在" in header["error"]

        proc.stdin.write(b'{"op": "ping", "id": 12}\n')
        proc.stdin.flush()
        header, _payload = _read_frame(proc.stdout)
        assert header["pong"] is True
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)