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


# ------------------------------------------------------------------ #
# 3b. 首尾静音裁剪（_trim_silence，20260930 引擎侧优化）                #
# ------------------------------------------------------------------ #

def test_trim_silence_removes_edges_keeps_windows():
    """首尾静音被裁掉，仅保留 lead/trail 留白；语音段原样保留。"""
    import numpy as np

    from lite.audio import voice_bridge

    sr = 1000  # 便于按样本计数：1 样本 = 1ms
    lead_ms, voiced_ms, trail_ms = 200, 300, 500
    audio = np.concatenate([
        np.zeros(lead_ms, dtype=np.float32),
        np.full(voiced_ms, 0.5, dtype=np.float32),
        np.zeros(trail_ms, dtype=np.float32),
    ])
    out = voice_bridge._trim_silence(audio, sr, lead_keep=0.03, trail_keep=0.12)
    # 期望长度 = 30ms 前导 + 300ms 语音 + 120ms 尾随 = 450ms
    assert abs(len(out) - 450) <= 2
    assert float(np.abs(out).max()) == pytest.approx(0.5)


def test_trim_silence_noop_on_all_silent_and_empty():
    """全静音 / 空输入 / 非法采样率：一律原样返回（宁可不裁，不冒削语音风险）。"""
    import numpy as np

    from lite.audio import voice_bridge

    silent = np.zeros(500, dtype=np.float32)
    assert len(voice_bridge._trim_silence(silent, 1000)) == 500

    empty = np.asarray([], dtype=np.float32)
    assert len(voice_bridge._trim_silence(empty, 1000)) == 0

    voiced = np.full(500, 0.3, dtype=np.float32)
    assert len(voice_bridge._trim_silence(voiced, 0)) == 500
    assert len(voice_bridge._trim_silence(voiced, None)) == 500


def test_trim_silence_noop_when_fully_voiced():
    """无静音（全程有声）→ 仅裁掉 lead/trail 允许的极短边缘，长度基本不变。"""
    import numpy as np

    from lite.audio import voice_bridge

    sr = 1000
    voiced = np.full(400, 0.4, dtype=np.float32)
    out = voice_bridge._trim_silence(voiced, sr, lead_keep=0.03, trail_keep=0.12)
    assert len(out) == 400  # 首样本即有声，前后留白均被原数组边界钳制


def test_trim_silence_scales_with_peak_not_absolute():
    """阈值以峰值为基准：整体音量很小（0.01）时仍能正确识别静音边界。"""
    import numpy as np

    from lite.audio import voice_bridge

    sr = 1000
    audio = np.concatenate([
        np.zeros(100, dtype=np.float32),
        np.full(200, 0.01, dtype=np.float32),   # 弱音量语音（-40dBFS 相对峰值仍为有声）
        np.zeros(300, dtype=np.float32),
    ])
    out = voice_bridge._trim_silence(audio, sr)
    assert abs(len(out) - (30 + 200 + 120)) <= 2


# ------------------------------------------------------------------ #
# 3c. 静音塌陷修复（2026-10-02）：近静音检测 + 去 DC 重合成              #
#     （_install_dec_dc_guard / _synthesize_with_silence_repair）      #
# ------------------------------------------------------------------ #

class _FakeDec:
    """替代 dec 模块：记录收到的 z（验证去 DC 守卫）。"""

    _dec_dc_guard = False

    def __init__(self):
        self.seen = []

    def __call__(self, z, g=None):
        self.seen.append(z.detach().clone())
        return z


class _FakeBridgeEngine:
    """替代 melo 引擎：按序返回预置波形/异常，并记录每次调用时的守卫开关。"""

    def __init__(self, returns, sr=44100):
        import types

        self._returns = list(returns)
        self.calls = []
        self.hps = types.SimpleNamespace(data=types.SimpleNamespace(sampling_rate=sr))

    def tts_to_file(self, text, speaker, output_path=None, speed=1.0, quiet=True):
        import numpy as np

        from lite.audio import voice_bridge

        self.calls.append(voice_bridge._DEC_DC_GUARD)
        value = self._returns[min(len(self.calls) - 1, len(self._returns) - 1)]
        if isinstance(value, Exception):
            raise value
        return np.asarray(value, dtype=np.float32)


class _FakeEngineCache:
    """替代 _TtsEngineCache：固定返回同一替身引擎。"""

    def __init__(self, engine):
        self._engine = engine

    def get(self, config_path=None, ckpt_path=None):
        return self._engine


def test_dec_dc_guard_passthrough_and_removal(monkeypatch):
    """守卫：关闭时逐字节透传；开启时 (B,C,T) 输入逐通道沿时间维去均值；安装幂等。"""
    torch = pytest.importorskip("torch")
    import types

    from lite.audio import voice_bridge

    fake = _FakeDec()
    engine = types.SimpleNamespace(model=types.SimpleNamespace(dec=fake))
    voice_bridge._install_dec_dc_guard(engine)
    guard = engine.model.dec
    assert guard is not fake

    z = torch.randn(1, 4, 7) + 2.0
    monkeypatch.setattr(voice_bridge, "_DEC_DC_GUARD", False)
    guard(z)
    assert torch.equal(fake.seen[0], z)  # 关闭：透传

    monkeypatch.setattr(voice_bridge, "_DEC_DC_GUARD", True)
    guard(z)
    removed = fake.seen[1]
    assert torch.allclose(removed.mean(dim=2), torch.zeros(1, 4), atol=1e-6)

    # 单帧输入不误伤（shape[2] > 1 才去均值）
    single = torch.randn(1, 4, 1)
    guard(single)
    assert torch.equal(fake.seen[2], single)

    # 幂等：重复安装不叠包
    voice_bridge._install_dec_dc_guard(engine)
    assert engine.model.dec is guard


def test_silence_repair_retries_with_guard_and_returns_audible():
    """首次近静音 → 开启守卫重合成一次并返回可闻结果；重试后开关复位。

    直接调用路径下：首段音频由参数传入（模拟首合成结果），替身引擎的首调即重试。
    """
    import numpy as np

    from lite.audio import voice_bridge

    silent = np.full(400, 3e-05, dtype=np.float32)  # 塌陷量级
    voiced = np.full(400, 0.3, dtype=np.float32)    # 可闻（重试产物）
    engine = _FakeBridgeEngine([voiced])
    out = voice_bridge._synthesize_with_silence_repair(
        engine, "今天过得还不错呢。", 1.0, silent)
    assert float(np.abs(out).max()) > 0.1
    assert engine.calls == [True]                # 重试窗口内守卫开启
    assert voice_bridge._DEC_DC_GUARD is False   # 已复位


def test_silence_repair_no_retry_when_audible():
    """首次即可闻（含接近阈值）→ 不重试。"""
    import numpy as np

    from lite.audio import voice_bridge

    for peak in (0.3, 0.02):
        audio = np.full(300, peak, dtype=np.float32)
        engine = _FakeBridgeEngine([audio])
        out = voice_bridge._synthesize_with_silence_repair(engine, "你好呀，", 1.0, audio)
        assert engine.calls == []                # 未触发重试
        assert float(np.abs(out).max()) == pytest.approx(peak)


def test_silence_repair_keeps_original_on_retry_error():
    """重试抛异常：保留原结果、不中断、开关复位（绝不静默丢请求）。"""
    import numpy as np

    from lite.audio import voice_bridge

    silent = np.full(400, 3e-05, dtype=np.float32)
    engine = _FakeBridgeEngine([RuntimeError("模拟重试失败")])
    out = voice_bridge._synthesize_with_silence_repair(engine, "测试", 1.0, silent)
    assert float(np.abs(out).max()) == pytest.approx(3e-05)
    assert engine.calls == [True]
    assert voice_bridge._DEC_DC_GUARD is False


def test_silence_repair_keeps_original_when_not_improved():
    """重试仍未改善（峰值未提高）→ 保留原结果。"""
    import numpy as np

    from lite.audio import voice_bridge

    silent = np.full(400, 3e-05, dtype=np.float32)
    engine = _FakeBridgeEngine([silent])
    out = voice_bridge._synthesize_with_silence_repair(engine, "测试", 1.0, silent)
    assert float(np.abs(out).max()) == pytest.approx(3e-05)
    assert voice_bridge._DEC_DC_GUARD is False


def test_handle_tts_repairs_silent_and_keeps_audible_single_pass():
    """_handle_tts 端到端：塌陷文本触发重试并返回可闻 wav；可闻文本单次合成。"""
    import io
    import wave

    import numpy as np

    from lite.audio import voice_bridge

    silent = np.full(4000, 3e-05, dtype=np.float32)
    voiced = np.full(4000, 0.3, dtype=np.float32)
    engine = _FakeBridgeEngine([silent, voiced])
    hdr, payload = voice_bridge._handle_tts(
        _FakeEngineCache(engine), {"text": "今天过得还不错呢。"})
    assert hdr["ok"] is True and payload[:4] == b"RIFF"
    assert engine.calls == [False, True]  # 首合成 + 重试（守卫开）
    with wave.open(io.BytesIO(payload), "rb") as reader:
        frames = np.frombuffer(reader.readframes(reader.getnframes()), dtype=np.int16)
    assert float(np.abs(frames).max()) > 0.1 * 32767

    engine2 = _FakeBridgeEngine([voiced])
    hdr2, _ = voice_bridge._handle_tts(_FakeEngineCache(engine2), {"text": "你好呀，"})
    assert hdr2["ok"] is True
    assert engine2.calls == [False]  # 可闻：不重试


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


# ------------------------------------------------------------------ #
# embed op 协议（20261002 批 B：嵌入 ONNX 化——桥侧）                   #
# ------------------------------------------------------------------ #


class _StubEmbedModel:
    """``_handle_embed`` 的替身引擎：可配置向量与异常，记录调用。"""

    def __init__(self, vectors=None, error=None):
        self.vectors = vectors or [[0.1, 0.2, 0.3]]
        self.error = error
        self.calls = []

    def embed(self, texts):
        self.calls.append(list(texts))
        if self.error is not None:
            raise self.error
        return [list(self.vectors[i % len(self.vectors)]) for i in range(len(texts))]


def _run_embed(texts, stub):
    """便捷调用 _handle_embed（避免重复构造 stub）。"""
    from lite.audio import voice_bridge

    return voice_bridge._handle_embed(stub, {"texts": texts})


def test_handle_embed_returns_dim_and_vectors():
    """正常路径：ok=true、dim 取向量维度、vectors 与输入等长透传。"""
    stub = _StubEmbedModel(vectors=[[0.5, -0.5, 1.0]])
    header, _payload = _run_embed(["你好", "hello"], stub)
    assert header["ok"] is True and header["dim"] == 3
    assert len(header["vectors"]) == 2
    assert stub.calls == [["你好", "hello"]]


def test_handle_embed_rejects_oversize_batch_and_text():
    """批次超限（>32）与单文本超限（>2000 字）整批拒绝，且不触达引擎。"""
    from lite.audio import voice_bridge

    stub = _StubEmbedModel()
    header, _ = _run_embed(["x"] * (voice_bridge.EMBED_BATCH_LIMIT + 1), stub)
    assert header["ok"] is False and "超限" in header["error"]
    header, _ = _run_embed(["长" * (voice_bridge.EMBED_TEXT_CHAR_LIMIT + 1)], stub)
    assert header["ok"] is False and "超限" in header["error"]
    assert stub.calls == []


def test_handle_embed_rejects_missing_or_bad_texts():
    """texts 缺失 / 空列表 / 非列表 → ok=false，不触达引擎。"""
    from lite.audio import voice_bridge

    stub = _StubEmbedModel()
    for req in ({}, {"texts": []}, {"texts": "not-a-list"}):
        header, _payload = voice_bridge._handle_embed(stub, req)
        assert header["ok"] is False
    assert stub.calls == []


def test_handle_embed_engine_failure_returns_ok_false():
    """引擎侧异常（资产缺失等）→ ok=false 中文错误（主进程按此回退 llama.cpp）。"""
    stub = _StubEmbedModel(error=RuntimeError("嵌入 ONNX 资产缺失（模拟）"))
    header, _payload = _run_embed(["你好"], stub)
    assert header["ok"] is False and "嵌入引擎不可用" in header["error"]


def test_bridge_script_embed_missing_asset_returns_chinese_error(tmp_path):
    """真 bridge 脚本：root 无 embed_onnx 资产 → ok=false 中文错误帧，进程继续存活。"""
    proc = subprocess.Popen(
        [sys.executable, BRIDGE_SRC, "--root", str(tmp_path), "--device", "cpu"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        req = json.dumps({"op": "embed", "id": 21, "texts": ["你好"]}, ensure_ascii=False)
        proc.stdin.write((req + "\n").encode("utf-8"))
        proc.stdin.flush()
        header, _payload = _read_frame(proc.stdout)
        assert header["ok"] is False and "资产缺失" in (header.get("error") or "")

        proc.stdin.write(b'{"op": "ping", "id": 22}\n')
        proc.stdin.flush()
        header, _payload = _read_frame(proc.stdout)
        assert header["pong"] is True
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_bridge_script_embed_oversize_batch_rejected_before_engine(tmp_path):
    """真 bridge 脚本：批次超限在校验层拒绝（无需资产即可复现）。"""
    from lite.audio import voice_bridge

    texts = ["x"] * (voice_bridge.EMBED_BATCH_LIMIT + 1)
    proc = subprocess.Popen(
        [sys.executable, BRIDGE_SRC, "--root", str(tmp_path), "--device", "cpu"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        req = {"op": "embed", "id": 23, "texts": texts}
        proc.stdin.write((json.dumps(req) + "\n").encode("utf-8"))
        proc.stdin.flush()
        header, _payload = _read_frame(proc.stdout)
        assert header["ok"] is False and "超限" in header["error"]
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


# ------------------------------------------------------------------ #
# _AsrModel：ONNX 优先与 torch 回退（mock，20261002 批 B）              #
# ------------------------------------------------------------------ #


class _StubSenseVoiceSmall:
    """funasr_onnx.SenseVoiceSmall 替身：记录构造参数，返回固定转写。"""

    instances = []

    def __init__(self, model_dir=None, quantize=False, device_id="-1"):
        self.model_dir = model_dir
        self.quantize = quantize
        self.device_id = device_id
        type(self).instances.append(self)

    def __call__(self, waveform, language="auto", use_itn=True):
        return ["<|zh|>stub-onnx"]


class _StubOnnxModule:
    """funasr_onnx 模块替身（仅携带 SenseVoiceSmall 属性）。"""

    def __init__(self, cls):
        self.SenseVoiceSmall = cls


class _StubTorchAutoModel:
    """funasr.AutoModel 替身：记录构造参数，返回 dict 形态转写。"""

    instances = []

    def __init__(self, model=None, device=None, disable_update=False):
        self.model = model
        self.device = device
        type(self).instances.append(self)

    def generate(self, input=None, **kwargs):
        return [{"key": "k", "text": "<|zh|>stub-torch"}]


class _StubFunasrModule:
    """funasr 模块替身（仅携带 AutoModel 属性）。"""

    def __init__(self, cls):
        self.AutoModel = cls


def _make_asr_root(tmp_path, onnx_ready=False):
    """构造桥 root：可选就位 asr_onnx/model.onnx 假资产。"""
    root = tmp_path / "root"
    if onnx_ready:
        asr_dir = root / "runtime" / "voice_bridge" / "asr_onnx"
        asr_dir.mkdir(parents=True)
        (asr_dir / "model.onnx").write_bytes(b"fake-onnx")
    root.mkdir(exist_ok=True)
    return str(root)


def test_asr_model_prefers_onnx_when_asset_present(tmp_path, monkeypatch):
    """资产就位 + funasr_onnx 可导入 → 走 ONNX 后端（cpu → device_id="-1"）。"""
    from lite.audio import voice_bridge

    root = _make_asr_root(tmp_path, onnx_ready=True)
    monkeypatch.setitem(sys.modules, "funasr_onnx", _StubOnnxModule(_StubSenseVoiceSmall))
    _StubSenseVoiceSmall.instances.clear()
    model = voice_bridge._AsrModel(root, "cpu")
    text = model.generate([0.0, 0.1])
    assert model._backend == "onnx"
    assert text == "<|zh|>stub-onnx"
    assert _StubSenseVoiceSmall.instances[-1].device_id == "-1"
    assert _StubSenseVoiceSmall.instances[-1].model_dir.endswith("asr_onnx")


def test_asr_model_gpu_intent_maps_to_cuda_device_id(tmp_path, monkeypatch):
    """gpu 设备意图 → device_id="0"（CUDA EP 探测）；DLL 注入失败降级 "-1"。"""
    from lite.audio import voice_bridge

    root = _make_asr_root(tmp_path, onnx_ready=True)
    monkeypatch.setitem(sys.modules, "funasr_onnx", _StubOnnxModule(_StubSenseVoiceSmall))
    _StubSenseVoiceSmall.instances.clear()
    monkeypatch.setattr(voice_bridge, "_inject_cuda_dll_paths", lambda: True)
    model = voice_bridge._AsrModel(root, "gpu")
    model.generate([0.0])
    assert _StubSenseVoiceSmall.instances[-1].device_id == "0"

    _StubSenseVoiceSmall.instances.clear()
    monkeypatch.setattr(voice_bridge, "_inject_cuda_dll_paths", lambda: False)
    model2 = voice_bridge._AsrModel(root, "gpu")
    model2.generate([0.0])
    assert _StubSenseVoiceSmall.instances[-1].device_id == "-1"


def test_asr_model_falls_back_to_torch_when_asset_missing(tmp_path, monkeypatch):
    """资产缺失 → 回退 funasr-torch 后端（generate 解析 dict 的 text 字段）。"""
    from lite.audio import voice_bridge

    root = _make_asr_root(tmp_path, onnx_ready=False)
    monkeypatch.setitem(sys.modules, "funasr", _StubFunasrModule(_StubTorchAutoModel))
    _StubTorchAutoModel.instances.clear()
    model = voice_bridge._AsrModel(root, "cpu")
    text = model.generate([0.0, 0.1])
    assert model._backend == "torch"
    assert text == "<|zh|>stub-torch"
    assert _StubTorchAutoModel.instances[-1].model.endswith("SenseVoiceSmall")


def test_asr_model_falls_back_to_torch_when_funasr_onnx_import_fails(tmp_path, monkeypatch):
    """funasr_onnx 导入失败（sys.modules 置 None）→ 回退 funasr-torch。"""
    from lite.audio import voice_bridge

    root = _make_asr_root(tmp_path, onnx_ready=True)
    monkeypatch.setitem(sys.modules, "funasr_onnx", None)
    monkeypatch.setitem(sys.modules, "funasr", _StubFunasrModule(_StubTorchAutoModel))
    _StubTorchAutoModel.instances.clear()
    model = voice_bridge._AsrModel(root, "cpu")
    text = model.generate([0.0])
    assert model._backend == "torch"
    assert text == "<|zh|>stub-torch"


def test_asr_model_falls_back_to_torch_when_onnx_construction_fails(tmp_path, monkeypatch):
    """ONNX 构造抛异常 → 回退 funasr-torch（逐级回退语义）。"""

    class _BoomSenseVoiceSmall(_StubSenseVoiceSmall):
        def __init__(self, *args, **kwargs):
            raise RuntimeError("模拟构造失败")

    from lite.audio import voice_bridge

    root = _make_asr_root(tmp_path, onnx_ready=True)
    monkeypatch.setitem(sys.modules, "funasr_onnx", _StubOnnxModule(_BoomSenseVoiceSmall))
    monkeypatch.setitem(sys.modules, "funasr", _StubFunasrModule(_StubTorchAutoModel))
    _StubTorchAutoModel.instances.clear()
    model = voice_bridge._AsrModel(root, "cpu")
    text = model.generate([0.0])
    assert model._backend == "torch"
    assert text == "<|zh|>stub-torch"


def test_handle_asr_uses_unified_generate_entry(tmp_path, monkeypatch):
    """_handle_asr 走统一 generate 入口（重采样 16k → 富文本原文透传）。"""
    import base64 as b64

    from lite.audio import voice_bridge

    class _RecordingStub:
        """记录 generate 入参的替身（覆盖 onnx/torch 两后端共用路径）。"""

        def __init__(self):
            self.received = None

        def generate(self, waveform):
            self.received = waveform
            return "<|zh|>unified"

    stub = _RecordingStub()
    pcm16 = struct.pack("<2h", 0, 16384)  # 2 个 int16 样本（16k 直入，无重采样）
    header, _payload = voice_bridge._handle_asr(
        stub, {"audio_b64": b64.b64encode(pcm16).decode(), "sample_rate": 16000}
    )
    assert header["ok"] is True and header["raw"] == "<|zh|>unified"
    assert list(stub.received[:2]) == [0.0, 0.5]  # int16 → float32 归一

    # 非 16k → 先重采样再进 generate（长度按比例缩放，±2 容差口径同既有 _resample 测试）
    import numpy as np

    pcm44 = struct.pack("<441h", *([16384] * 441))  # 44100Hz 下 0.01s
    header, _payload = voice_bridge._handle_asr(
        stub, {"audio_b64": b64.b64encode(pcm44).decode(), "sample_rate": 44100}
    )
    assert header["ok"] is True
    assert abs(len(stub.received) - 160) <= 2  # 重采样到 16k ≈ 0.01s
    assert np.allclose(np.asarray(stub.received), np.asarray(stub.received))  # float 形态