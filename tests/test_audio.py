# -*- coding: utf-8 -*-
"""Task B1 语音三件套单元测试。

覆盖：
- LiteVAD：合成"短音 + 长静音"判定结束；持续语音不触发；阈值逻辑
- LiteASR：Mock backend 透传；SenseVoice 未装时代理 import backend 报错路径
- LiteTTS：Mock backend 合成已知字节；默认 voice=cx-open 生效；melotts 未装报错路径
- build_default_pipeline：无三方库回退 mock 并打印 warning
- 工厂 judge 接线（H4）：未配置默认态 None；enabled+runtime 可用为委托包装实例；
  加载失败回退 HeuristicJudge
"""

import math
import os
import struct
import sys
import types

import pytest

from lite.audio import (
    LiteVAD,
    LiteASR,
    LiteTTS,
    MockASRBackend,
    MockTTSBackend,
    SenseVoiceBackend,
    MeloTTSBackend,
    ShouldReplyJudge,
    HeuristicJudge,
    build_default_pipeline,
)

SR = 16000
SW = 2


# ------------------------------------------------------------------ #
# PCM 合成辅助                                                       #
# ------------------------------------------------------------------ #

def _silence(dur_ms):
    """生成 dur_ms 的纯静音 PCM 字节（int16 zero）。"""
    n = int(SR * dur_ms / 1000)
    return b"\x00\x00" * n


def _speech(dur_ms, amplitude=6000, freq=440):
    """生成 dur_ms 的单频正弦 PCM 字节（int16 little-endian）。"""
    n = int(SR * dur_ms / 1000)
    out = bytearray()
    for i in range(n):
        v = int(amplitude * math.sin(2 * math.pi * freq * i / SR))
        out += struct.pack("<h", v)
    return bytes(out)


# ------------------------------------------------------------------ #
# 1. LiteVAD                                                         #
# ------------------------------------------------------------------ #

def test_vad_speech_plus_long_silence_triggers_end():
    """先短音后累计超过 silence_ms 静音，应触发一次结束信号。"""
    vad = LiteVAD(silence_ms=600)
    # 说话
    assert vad.detect_speech_end(_speech(300)) is False
    # 静音未达标
    assert vad.detect_speech_end(_silence(400)) is False
    # 累计到 700ms > 600ms → 结束触发
    assert vad.detect_speech_end(_silence(300)) is True


def test_vad_continuous_speech_never_triggers():
    """持续说话（无长静音）不应触发结束。"""
    vad = LiteVAD(silence_ms=600)
    for _ in range(20):
        assert vad.detect_speech_end(_speech(200)) is False


def test_vad_auto_reset_allows_next_round():
    """触发结束后内部复位，可继续下一轮检测。"""
    vad = LiteVAD(silence_ms=400)
    assert vad.detect_speech_end(_speech(200)) is False
    assert vad.detect_speech_end(_silence(500)) is True  # 结束触发并复位
    # 下一轮：再说话再静音，仍能再次触发
    assert vad.detect_speech_end(_speech(200)) is False
    assert vad.detect_speech_end(_silence(500)) is True


def test_vad_threshold_logic():
    """阈值逻辑：高阈值下低幅信号判为静音不产生说话态；稳定说话仍不触发。"""
    # 幅值 6000 → ≈-17.8dB，低于 -10dB 阈值 → 判为静音，从未进入说话态
    vad = LiteVAD(speech_threshold_dB=-10.0, silence_ms=400)
    assert vad.detect_speech_end(_speech(300, amplitude=6000)) is False
    assert vad.detect_speech_end(_silence(500)) is False

    # 幅值 20000 → ≈-7.3dB，高于 -10dB → 判为说话，稳定说话不触发
    vad2 = LiteVAD(speech_threshold_dB=-10.0, silence_ms=400)
    assert vad2.detect_speech_end(_speech(300, amplitude=20000)) is False


def test_vad_db_of_silence_is_neg_inf():
    """纯静音的 dB 值应为 -inf（等价无限小声）。"""
    assert math.isinf(LiteVAD()._db_of(_silence(100)))


# ------------------------------------------------------------------ #
# 2. LiteASR                                                         #
# ------------------------------------------------------------------ #

def test_asr_mock_passthrough():
    """Mock backend 返回的 {text, emotion, event} 应被 LiteASR 透传。"""
    backend = MockASRBackend(text="你好", emotion="HAPPY", event="Speech")
    asr = LiteASR(backend=backend, device="cpu")
    assert asr.transcribe(b"audio") == {
        "text": "你好",
        "emotion": "HAPPY",
        "event": "Speech",
    }


def test_asr_default_backend_is_mock():
    """LiteASR 缺省构造应使用 MockASRBackend。"""
    asr = LiteASR()
    assert isinstance(asr.backend, MockASRBackend)
    assert asr.transcribe(b"x") == {"text": "", "emotion": "", "event": None}


def test_asr_sensevoice_import_error(monkeypatch):
    """SenseVoice 未装时，backend 首次 transcribe 应抛 RuntimeError 提示 funasr。"""
    monkeypatch.setitem(sys.modules, "funasr", None)  # 强制模拟未安装
    backend = SenseVoiceBackend()
    with pytest.raises(RuntimeError, match="funasr"):
        backend.transcribe(b"audio")


# ------------------------------------------------------------------ #
# 3. LiteTTS                                                         #
# ------------------------------------------------------------------ #

def test_tts_mock_returns_bytes():
    """Mock backend 应合成返回已知字节；LiteTTS 透传。"""
    canned = b"RIFF________WAVE:canned"
    tts = LiteTTS(backend=MockTTSBackend(audio=canned), default_voice="cx-open")
    assert tts.synthesize("你好") == canned


def test_tts_default_voice_cx_open():
    """默认 voice 缺省应使用 cx-open；显式传入 voice 应覆盖默认。"""
    calls = []

    class RecordingBackend(MockTTSBackend):
        def synthesize(self, text, voice="cx-open"):
            calls.append(voice)
            return b"wav"

    tts = LiteTTS(backend=RecordingBackend(), default_voice="cx-open")
    tts.synthesize("hi")
    assert calls == ["cx-open"]
    tts.synthesize("hi", voice="other")
    assert calls == ["cx-open", "other"]


def test_tts_melotts_import_error(monkeypatch):
    """melo(MeloTTS) 未装时，backend 首次 synthesize 应抛 RuntimeError 提示 MeloTTS。"""
    monkeypatch.setitem(sys.modules, "melo.api", None)  # 强制模拟未安装
    backend = MeloTTSBackend()
    with pytest.raises(RuntimeError, match="MeloTTS"):
        backend.synthesize("你好")


# ------------------------------------------------------------------ #
# 4. 工厂                                                           #
# ------------------------------------------------------------------ #

def test_factory_falls_back_to_mock(monkeypatch, capsys):
    """无三方库（funasr / melotts 均不可用）时应回退 mock 并打印 warning。"""
    monkeypatch.setitem(sys.modules, "funasr", None)
    monkeypatch.setitem(sys.modules, "melo.api", None)
    cfg = {"asr": {"device": "cpu"}, "tts": {"voice": "cx-open"}, "vad": {}}
    pipe = build_default_pipeline(cfg)
    assert isinstance(pipe["vad"], LiteVAD)
    assert isinstance(pipe["asr"], LiteASR)
    assert isinstance(pipe["tts"], LiteTTS)
    assert isinstance(pipe["asr"].backend, MockASRBackend)
    assert isinstance(pipe["tts"].backend, MockTTSBackend)
    assert "回退" in capsys.readouterr().out


# ------------------------------------------------------------------ #
# 5. 工厂 judge 接线（H4）                                            #
# ------------------------------------------------------------------ #

def test_factory_includes_judge_key_disabled_by_default():
    """local_llm 未配置时 judge 键存在且为默认态 None（既有行为不变）。"""
    cfg = {"asr": {"device": "cpu"}, "tts": {}, "vad": {}}
    pipe = build_default_pipeline(cfg)
    assert "judge" in pipe
    assert pipe["judge"] is None


def test_factory_judge_enabled_with_runtime(monkeypatch):
    """enabled=True 且 runtime 可用（fake LlamaRuntime 注入）时 judge 为
    包装实例且委托 judge_should_reply 生效。"""

    class FakeRuntime:
        """可判定的 fake LlamaRuntime：load 恒成功，判定按前缀返回。"""

        def __init__(self, config=None):
            self.config = config

        def load_local_llm(self, path):
            return True

        def judge_should_reply(self, text):
            return str(text).startswith("帮我")

    fake_mod = types.ModuleType("lite.runtime.llama_runtime")
    fake_mod.LlamaRuntime = FakeRuntime
    monkeypatch.setitem(sys.modules, "lite.runtime.llama_runtime", fake_mod)

    cfg = {"local_llm": {"enabled": True, "model_path": "D:/models/fake.gguf"}}
    pipe = build_default_pipeline(cfg)

    judge = pipe["judge"]
    assert isinstance(judge, ShouldReplyJudge)   # 包装实例符合契约
    assert judge.model_ready is True
    assert judge.judge("帮我带杯咖啡") is True    # 委托 fake runtime 判定
    assert judge.judge("随便看看") is False


def test_factory_judge_enabled_load_failure_falls_back(monkeypatch, capsys):
    """模型加载失败（load_local_llm 返回 False）时打 warning 并回退 HeuristicJudge。"""

    class DeadRuntime:
        """加载恒失败的 fake LlamaRuntime。"""

        def __init__(self, config=None):
            self.warnings = ["GGUF 文件不存在"]

        def load_local_llm(self, path):
            return False

    fake_mod = types.ModuleType("lite.runtime.llama_runtime")
    fake_mod.LlamaRuntime = DeadRuntime
    monkeypatch.setitem(sys.modules, "lite.runtime.llama_runtime", fake_mod)

    cfg = {"local_llm": {"enabled": True, "model_path": "D:/missing/model.gguf"}}
    pipe = build_default_pipeline(cfg)

    assert isinstance(pipe["judge"], HeuristicJudge)
    assert "接线失败" in capsys.readouterr().out


def test_factory_judge_real_missing_model_falls_back():
    """真机冒烟：未装 llama-cpp / 模型文件缺失 → 延迟导入链不炸、回退 HeuristicJudge。"""
    cfg = {"local_llm": {"enabled": True, "model_path": "Z:/definitely-missing/x.gguf"}}
    pipe = build_default_pipeline(cfg)
    assert isinstance(pipe["judge"], HeuristicJudge)


def test_factory_judge_enabled_without_model_path_stays_none():
    """enabled=True 但未配置 model_path：无法接线，保持默认态 None。"""
    cfg = {"local_llm": {"enabled": True}}
    pipe = build_default_pipeline(cfg)
    assert pipe["judge"] is None


# ------------------------------------------------------------------ #
# L9：PCM float32→int16 越界钳制                                      #
# ------------------------------------------------------------------ #

def test_tts_pcm_clip_out_of_range_samples():
    """越界样本（2.0/-2.0）clip 后不回绕：32767 / -32768，dtype 为 int16。"""
    np = pytest.importorskip("numpy")
    from lite.audio.tts import _audio_to_pcm16

    pcm = _audio_to_pcm16([2.0, -2.0])
    assert int(pcm[0]) == 32767
    assert int(pcm[1]) == -32768
    assert pcm.dtype == np.int16


def test_tts_pcm_conversion_preserves_in_range_values():
    """区间内样本转换保持保真（0.5 → 约 16383）。"""
    pytest.importorskip("numpy")
    from lite.audio.tts import _audio_to_pcm16

    pcm = _audio_to_pcm16([0.0, 0.5, -0.5])
    assert pcm.tolist() == [0, 16383, -16383]


# ------------------------------------------------------------------ #
# L12：ASR 输入健壮化与结果双形态解析                                  #
# ------------------------------------------------------------------ #

class _FakeFunasrModel:
    """可捕获 generate 入参并按预设返回形态响应的 fake 模型。"""

    def __init__(self, result):
        self.result = result
        self.captured_kwargs = {}

    def generate(self, input=None, **kwargs):
        self.captured_kwargs["input"] = input
        self.captured_kwargs.update(kwargs)
        return self.result


_FLAT_RESULT = [{"text": "<|zh|><|NEUTRAL|><|Speech|>你好"}]
_NESTED_RESULT = [[{"text": "<|zh|><|HAPPY|><|Laughter|>在吗"}]]


def _make_loaded_backend(fake_model):
    backend = SenseVoiceBackend()
    backend._model = fake_model
    backend._loaded = True  # 跳过 funasr 真实加载
    return backend


def test_asr_sensevoice_bytes_converted_and_flat_dict_parsed():
    """bytes 输入转 float32 波形投递 generate；平铺 dict-list 结果解析正确。"""
    np = pytest.importorskip("numpy")
    model = _FakeFunasrModel(_FLAT_RESULT)
    backend = _make_loaded_backend(model)

    out = backend.transcribe(struct.pack("<hh", 32767, -32768))

    arr = model.captured_kwargs["input"]
    assert isinstance(arr, np.ndarray)
    assert arr.dtype == np.float32
    assert arr.tolist() == pytest.approx([32767 / 32768.0, -1.0])
    assert out["text"] == "你好"
    assert out["emotion"] == "NEUTRAL"
    assert out["event"] == "Speech"


def test_asr_sensevoice_nested_dict_list_parsed():
    """嵌套双层形态 res[0][0]["text"] 解析正确。"""
    pytest.importorskip("numpy")
    backend = _make_loaded_backend(_FakeFunasrModel(_NESTED_RESULT))

    out = backend.transcribe([0.1, -0.2])

    assert out["text"] == "在吗"
    assert out["emotion"] == "HAPPY"
    assert out["event"] == "Laughter"


def test_asr_sensevoice_ndarray_input_passthrough_float32(capsys):
    """ndarray 输入按需 asarray 为 float32；解析异常路径打印告警不静默。"""
    np = pytest.importorskip("numpy")
    # 返回未识别形态 → 兜底空文本且打印 ERROR 告警
    model = _FakeFunasrModel({"unexpected": "shape"})
    backend = _make_loaded_backend(model)

    wave = np.array([0.5, -0.5], dtype=np.int16).astype(np.float64)  # 非 float32 输入
    out = backend.transcribe(wave)

    assert model.captured_kwargs["input"].dtype == np.float32
    assert out == {"text": "", "emotion": "", "event": None}
    assert "[ERROR]" in capsys.readouterr().out


def test_asr_sensevoice_empty_result_warns_not_silent(capsys):
    """funasr 返回空列表时打印告警并返回空结构，不再静默吞掉失败。"""
    pytest.importorskip("numpy")
    backend = _make_loaded_backend(_FakeFunasrModel([]))
    out = backend.transcribe(b"\x00\x00")
    assert out == {"text": "", "emotion": "", "event": None}
    assert "ASR" in capsys.readouterr().out


# ------------------------------------------------------------------ #
# GPU 开关：resolve_torch_device 归一化 + TTS/ASR 后端透传            #
# ------------------------------------------------------------------ #

def _make_torch(available):
    """构造 fake torch 模块：``cuda.is_available()`` 返回 available。"""
    torch_mod = types.ModuleType("torch")
    cuda_mod = types.ModuleType("torch.cuda")
    cuda_mod.is_available = lambda: available
    torch_mod.cuda = cuda_mod
    return torch_mod


def test_resolve_torch_device_passthrough():
    """None/空 → cpu；显式 torch 串原样放行；大小写不敏感。"""
    from lite.audio.asr import resolve_torch_device

    assert resolve_torch_device(None) == "cpu"
    assert resolve_torch_device("") == "cpu"
    assert resolve_torch_device("cpu") == "cpu"
    assert resolve_torch_device("CUDA:0") == "cuda:0"
    assert resolve_torch_device(" CUDA ") == "cuda"


def test_resolve_torch_device_gpu_with_cuda(monkeypatch):
    """gpu 且 CUDA 可用 → cuda。"""
    from lite.audio.asr import resolve_torch_device

    monkeypatch.setitem(sys.modules, "torch", _make_torch(True))
    assert resolve_torch_device("GPU") == "cuda"


def test_resolve_torch_device_cuda_unavailable_falls_back(monkeypatch, capsys):
    """gpu 但 CUDA 不可用 → 回落 cpu 并打印告警（不崩溃）。"""
    from lite.audio.asr import resolve_torch_device

    monkeypatch.setitem(sys.modules, "torch", _make_torch(False))
    assert resolve_torch_device("gpu") == "cpu"
    assert "回落" in capsys.readouterr().out


def test_resolve_torch_device_no_torch_falls_back(monkeypatch, capsys):
    """gpu 但 torch 未安装（sys.modules 置 None 模拟 ImportError）→ 回落 cpu。"""
    from lite.audio.asr import resolve_torch_device

    monkeypatch.setitem(sys.modules, "torch", None)
    assert resolve_torch_device("gpu") == "cpu"
    assert "torch" in capsys.readouterr().out


def test_sensevoice_backend_device_normalized(monkeypatch):
    """SenseVoiceBackend 构造即归一化 device：gpu→cuda / 不可用回落 cpu。"""
    monkeypatch.setitem(sys.modules, "torch", _make_torch(True))
    assert SenseVoiceBackend(device="gpu").device == "cuda"
    monkeypatch.setitem(sys.modules, "torch", _make_torch(False))
    assert SenseVoiceBackend(device="gpu").device == "cpu"
    assert SenseVoiceBackend(device="cpu").device == "cpu"


def test_melotts_backend_device_normalized_and_passed(monkeypatch):
    """MeloTTSBackend 归一化 device，并把设备透传到 melo.api.TTS 构造。"""
    recorded = {}

    class _FakeTTS:
        def __init__(self, language=None, device=None, use_hf=None, config_path=None, ckpt_path=None, **kw):
            recorded["device"] = device

    fake_api = types.ModuleType("melo.api")
    fake_api.TTS = _FakeTTS
    fake_melo = types.ModuleType("melo")
    fake_melo.api = fake_api
    monkeypatch.setitem(sys.modules, "melo", fake_melo)
    monkeypatch.setitem(sys.modules, "melo.api", fake_api)
    monkeypatch.setitem(sys.modules, "torch", _make_torch(True))

    backend = MeloTTSBackend(device="gpu")
    assert backend.device == "cuda"
    backend._get_engine()  # 触发 fake TTS 构造，验证设备透传
    assert recorded["device"] == "cuda"


# ------------------------------------------------------------------ #
# G-4：音色目录缺训练产物回退官方默认音色时必须告警                    #
# ------------------------------------------------------------------ #

class _StopAfterEngine(Exception):
    """在 _get_engine 处截断合成流程的哨兵异常（避免依赖真实引擎/soundfile）。"""


def _make_melotts_backend(tmp_path, monkeypatch, pack_files):
    """构造带音色目录的 MeloTTSBackend，_get_engine 被哨兵异常截断。

    :param pack_files: dict[音色id, 文件名列表]，在 voice_dir 下构造目录内容
    """
    voice_dir = tmp_path / "voices"
    for pack_id, files in pack_files.items():
        pack = voice_dir / pack_id
        pack.mkdir(parents=True, exist_ok=True)
        for f in files:
            (pack / f).write_bytes(b"x")

    backend = MeloTTSBackend(voice_dir=str(voice_dir))
    # 预置 _TTSType 跳过 _ensure_lib 的真实 melo 导入（测试环境无需安装 MeloTTS）
    backend._TTSType = object

    def _stop(config_path=None, ckpt_path=None):
        _StopAfterEngine.config_path = config_path
        _StopAfterEngine.ckpt_path = ckpt_path
        raise _StopAfterEngine

    monkeypatch.setattr(backend, "_get_engine", _stop)
    return backend


def test_melotts_missing_voice_config_warns_and_falls_back(tmp_path, monkeypatch):
    """G-4：cfg/ckpt 均缺失回退官方默认音色时发 UserWarning 告警。"""
    import warnings

    backend = _make_melotts_backend(tmp_path, monkeypatch, {"lonely_pack": ["readme.txt"]})

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(_StopAfterEngine):
            backend.synthesize("你好", voice="lonely_pack")

    messages = [str(w.message) for w in caught]
    assert any(
        "音色目录缺少 config.json/ckpt.txt" in m and "已回退官方默认音色" in m
        for m in messages
    ), messages
    # 回退官方默认：config/ckpt 均以 None 传入引擎
    assert _StopAfterEngine.config_path is None
    assert _StopAfterEngine.ckpt_path is None


def test_melotts_with_voice_config_no_fallback_warning(tmp_path, monkeypatch):
    """G-4：config.json 存在时走本地模型路径，不发回退告警。"""
    import warnings

    backend = _make_melotts_backend(
        tmp_path, monkeypatch, {"full_pack": ["config.json", "ckpt.txt"]}
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(_StopAfterEngine):
            backend.synthesize("你好", voice="full_pack")

    assert not any("已回退官方默认音色" in str(w.message) for w in caught)
    assert _StopAfterEngine.config_path.endswith("config.json")
    assert _StopAfterEngine.ckpt_path.endswith("ckpt.txt")


# ------------------------------------------------------------------ #
# 批次3（第三轮体检）：voice 路径穿越 / 引擎 LRU                        #
# ------------------------------------------------------------------ #


@pytest.mark.parametrize("evil", ["../evil", "a/b", "a\\b", "C:pack", ".."])
def test_voice_path_rejects_traversal_ids(tmp_path, evil):
    """H-2：含路径穿越特征的 voice id 一律返回 None，不拼接逃逸路径。"""
    backend = MeloTTSBackend(voice_dir=str(tmp_path / "voices"))
    assert backend._voice_path(evil) is None


def test_voice_path_allows_normal_id(tmp_path):
    """H-2：正常音色 id 照常拼接目录路径。"""
    voice_dir = tmp_path / "voices"
    backend = MeloTTSBackend(voice_dir=str(voice_dir))
    assert backend._voice_path("myvoice") == str(voice_dir / "myvoice")


def test_melotts_unsafe_voice_falls_back_with_warning(tmp_path, monkeypatch):
    """H-2：synthesize 收到非法 voice 时告警并回退官方默认音色（cx-open）。"""
    import warnings

    backend = _make_melotts_backend(tmp_path, monkeypatch, {})

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(_StopAfterEngine):
            backend.synthesize("你好", voice="../evil_pack")

    assert any("路径穿越特征" in str(w.message) for w in caught), [str(w.message) for w in caught]
    # 回退默认音色 cx-open（目录缺产物）→ 引擎收到 None/None
    assert _StopAfterEngine.config_path is None
    assert _StopAfterEngine.ckpt_path is None


def test_engine_cache_lru_eviction(tmp_path, monkeypatch):
    """M-8：引擎缓存超上限（3）时淘汰最旧条目，命中刷新最新位。"""
    voice_dir = tmp_path / "voices"
    voice_dir.mkdir(parents=True)
    backend = MeloTTSBackend(voice_dir=str(voice_dir))
    backend._TTSType = lambda **kwargs: object()  # 跳过真实 melo 导入，引擎占位 object()

    keys = [(f"cfg{i}.json", f"ckpt{i}.txt") for i in range(4)]
    for cfg, ckpt in keys[:3]:
        backend._get_engine(config_path=cfg, ckpt_path=ckpt)
    assert len(backend._engines) == 3
    assert keys[0] in backend._engines

    # 命中 keys[0] → 刷新为最新位；随后新增 keys[3] → 淘汰的应是最旧的 keys[1]
    backend._get_engine(config_path=keys[0][0], ckpt_path=keys[0][1])
    backend._get_engine(config_path=keys[3][0], ckpt_path=keys[3][1])
    assert len(backend._engines) == 3
    assert keys[1] not in backend._engines
    assert keys[0] in backend._engines and keys[3] in backend._engines


# ------------------------------------------------------------------ #
# 第四轮体检批次C：绝对路径形态音色（与 resolve_voice 契约对齐）        #
# ------------------------------------------------------------------ #


def test_voice_path_accepts_absolute_path_inside_voice_dir(tmp_path):
    """voice_dir 内合法绝对路径（resolve_voice 契约返回值）应可解析为引擎目录。"""
    voices = tmp_path / "voices"
    pack = voices / "custom_a"
    pack.mkdir(parents=True)
    (pack / "config.json").write_bytes(b"x")
    backend = MeloTTSBackend(voice_dir=str(voices))
    abs_path = str(pack)
    assert backend._voice_path(abs_path) == os.path.abspath(abs_path)


def test_voice_path_rejects_absolute_path_outside_voice_dir(tmp_path):
    """目录外绝对路径被拒：返回 None，不逃逸音色根目录。"""
    backend = MeloTTSBackend(voice_dir=str(tmp_path / "voices"))
    outside = tmp_path / "elsewhere" / "pack"
    outside.mkdir(parents=True)
    assert backend._voice_path(str(outside)) is None


def test_voice_path_rejects_absolute_path_missing_dir(tmp_path):
    """voice_dir 前缀内但目录不存在的绝对路径同样拒绝（存在性校验）。"""
    backend = MeloTTSBackend(voice_dir=str(tmp_path / "voices"))
    ghost = str(tmp_path / "voices" / "ghost_pack")
    assert backend._voice_path(ghost) is None


def test_melotts_absolute_path_inside_voice_dir_uses_local_model(tmp_path, monkeypatch):
    """绝对路径在 voice_dir 内且目录存在 → 合成走该目录的 config/ckpt，无回退告警。"""
    import warnings

    backend = _make_melotts_backend(
        tmp_path, monkeypatch, {"full_pack": ["config.json", "ckpt.txt"]}
    )
    abs_path = str(tmp_path / "voices" / "full_pack")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(_StopAfterEngine):
            backend.synthesize("你好", voice=abs_path)

    assert _StopAfterEngine.config_path == os.path.join(abs_path, "config.json")
    assert _StopAfterEngine.ckpt_path == os.path.join(abs_path, "ckpt.txt")
    assert not any("路径穿越" in str(w.message) for w in caught)


def test_melotts_absolute_path_outside_voice_dir_falls_back_with_warning(tmp_path, monkeypatch):
    """目录外绝对路径：告警并回退官方默认音色，不加载外部目录。"""
    import warnings

    backend = _make_melotts_backend(tmp_path, monkeypatch, {})
    outside = tmp_path / "outside_pack"
    outside.mkdir()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(_StopAfterEngine):
            backend.synthesize("你好", voice=str(outside))

    assert any("路径穿越特征" in str(w.message) for w in caught), [str(w.message) for w in caught]
    # 回退官方默认：目录缺产物 → 引擎收到 None/None
    assert _StopAfterEngine.config_path is None
    assert _StopAfterEngine.ckpt_path is None