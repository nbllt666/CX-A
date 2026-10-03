# -*- coding: utf-8 -*-
"""TTS ORT 加速落地单测（bridge 侧；主环境可跑——不依赖 torch/onnxruntime 真物）。

覆盖：
1. 资产目录推导（与 bridge 脚本同目录的 ``tts_onnx/``）；
2. 会话加载的回退路径：onnxruntime 缺失 / 资产缺失 / 已判定不重试（进程级缓存）；
3. provider 选择链（GPU 落地，2026-10-01）：CUDA 探测逐级回退（provider 枚举 /
   DLL 注入 / torch 判定 / 会话构造）/ CUDA 全链可用时会话参数（tf32 关闭；不得
   设置 disable_cpu_ep_fallback）/ 熔断标志置位后固定 CPU（fake onnxruntime，主环境可跑）；
4. 运行时熔断降级：CUDA 会话异常 → CPU 重建重试一次 / CPU 会话异常不熔断 /
   幂等 / 重建失败抛原始异常；
5. ``_apply_ort_accel`` 在无会话时直接回退 False（不触碰 engine）；
6. 真替换路径（需 torch + onnxruntime + 真资产）——条件不满足时跳过
   （主环境无 torch 天然 skip；sidecar 环境可跑）；
7. 形状稳定化与模块收敛（Task 4B）：ORT 模块清单收敛为 dec；补零目标 /
   裁剪计数 / 启用判定三个纯函数（主环境可跑）。

依据：.trae/documents/20261001_模块0_TTS引擎ORT落地.md
      .trae/documents/20261001_模块0_TTS引擎GPU落地.md
"""

import importlib.util
import os
import sys
import types

import pytest

import lite.audio.voice_bridge as bridge


@pytest.fixture(autouse=True)
def _reset_ort_state():
    """每个用例前后复位进程级 ORT 状态（会话缓存 / provider / 熔断 / DLL 句柄）。"""
    bridge._ORT_SESSIONS = None
    bridge._ORT_PROVIDER = None
    bridge._ORT_FORCE_CPU = False
    bridge._DLL_DIR_HANDLES.clear()
    yield
    bridge._ORT_SESSIONS = None
    bridge._ORT_PROVIDER = None
    bridge._ORT_FORCE_CPU = False
    bridge._DLL_DIR_HANDLES.clear()


def test_module_names_and_dir_contract():
    """ORT 常量契约：资产目录名、模块清单（Task 4B 收敛为 dec）、形状桶宽。"""
    assert bridge.TTS_ORT_ASSET_DIR_NAME == "tts_onnx"
    assert bridge.TTS_ORT_MODULE_NAMES == ("dec",)
    assert bridge.TTS_ORT_DEC_PAD_BUCKET == 256


def test_dec_pad_helpers_pure():
    """形状稳定化纯函数：补零目标 / 裁剪计数（主环境可跑，不依赖 torch）。"""
    assert bridge._dec_pad_target(99) == 256
    assert bridge._dec_pad_target(256) == 256
    assert bridge._dec_pad_target(257) == 512
    # 未补零（padded <= origin）时裁剪计数原样返回
    assert bridge._dec_trim_count(50688, 99, 99) == 50688
    # 补零情形：输出按比例裁剪（dec 上采样比固定）
    assert bridge._dec_trim_count(76800, 99, 256) == round(76800 * 99 / 256)
    assert bridge._dec_trim_count(0, 99, 256) == 0


def test_dec_shape_pad_enabled_gating(monkeypatch):
    """形状桶启用判定：仅 GPU 类 provider；桶宽为 0 时全关。"""
    assert bridge._dec_shape_pad_enabled("cuda") is True
    assert bridge._dec_shape_pad_enabled("dml") is True
    assert bridge._dec_shape_pad_enabled("rocm") is True
    assert bridge._dec_shape_pad_enabled("cpu") is False
    assert bridge._dec_shape_pad_enabled(None) is False
    monkeypatch.setattr(bridge, "TTS_ORT_DEC_PAD_BUCKET", 0)
    assert bridge._dec_shape_pad_enabled("cuda") is False
    assert bridge._dec_pad_target(99) == 99


def test_ort_asset_dir_next_to_script():
    expected = os.path.join(os.path.dirname(os.path.abspath(bridge.__file__)), "tts_onnx")
    assert bridge._ort_asset_dir() == expected


def test_load_sessions_missing_assets(tmp_path, monkeypatch, capsys):
    """资产缺失：直接判定（先做文件检查——不触碰 onnxruntime import）。

    同时为 ``sys.modules`` 注入 None 强制 import 失败——若实现退化回
    「先 import 后查资产」，断言将失败（顺序契约保护）。
    """
    monkeypatch.setattr(bridge, "_ort_asset_dir", lambda: str(tmp_path))
    monkeypatch.setitem(sys.modules, "onnxruntime", None)
    assert bridge._load_ort_sessions() is False
    assert "ORT 资产缺失" in capsys.readouterr().err


def test_load_sessions_import_error(tmp_path, monkeypatch, capsys):
    """资产齐备但 onnxruntime 不可导入：返回 False 且记录诊断（不抛）。"""
    for name in bridge.TTS_ORT_MODULE_NAMES:
        (tmp_path / f"{name}.onnx").write_bytes(b"")
    monkeypatch.setattr(bridge, "_ort_asset_dir", lambda: str(tmp_path))
    monkeypatch.setitem(sys.modules, "onnxruntime", None)  # import 将抛 ImportError
    assert bridge._load_ort_sessions() is False
    assert "onnxruntime 导入失败" in capsys.readouterr().err


def test_sessions_false_is_cached_no_retry(tmp_path, monkeypatch):
    """判定不可用后写进程级缓存：二次调用不再触碰文件系统（不重试）。"""
    calls = {"n": 0}
    real_isfile = os.path.isfile

    def counting_isfile(path):
        calls["n"] += 1
        return real_isfile(path)

    monkeypatch.setattr(bridge, "_ort_asset_dir", lambda: str(tmp_path))
    monkeypatch.setattr(os.path, "isfile", counting_isfile)
    assert bridge._load_ort_sessions() is False
    first = calls["n"]
    assert bridge._load_ort_sessions() is False
    assert calls["n"] == first


def test_apply_without_sessions_returns_false(monkeypatch):
    """无可用会话：直接回退 False，不触碰 engine 对象。"""
    monkeypatch.setattr(bridge, "_load_ort_sessions", lambda: False)
    assert bridge._apply_ort_accel(object()) is False


# ------------------------------------------------------------------ #
# provider 选择链（fake onnxruntime；主环境无 torch 可跑）              #
# ------------------------------------------------------------------ #


def _write_fake_assets(dir_path):
    """写入 4 个空 .onnx 资产（仅过文件存在性检查）。"""
    for name in bridge.TTS_ORT_MODULE_NAMES:
        (dir_path / f"{name}.onnx").write_bytes(b"")


def _make_fake_ort(cuda_available=True, cuda_build_error=None,
                   dml_available=False, dml_build_error=None,
                   rocm_available=False, rocm_build_error=None):
    """构造 fake onnxruntime 模块：记录会话构造参数，可控各 EP 可用/失败。

    :return: ``(fake_module, record)``；record 含 cuda_calls / dml_calls /
        rocm_calls / cpu_calls / sessions。
    """
    record = {"cuda_calls": 0, "dml_calls": 0, "rocm_calls": 0,
              "cpu_calls": 0, "sessions": []}

    class _FakeSessionOptions:
        def __init__(self):
            self.graph_optimization_level = None
            self.entries = {}

        def add_session_config_entry(self, key, value):
            self.entries[key] = value

    def _provider_name(providers):
        first = (providers or [None])[0]
        return first[0] if isinstance(first, tuple) else first

    def _inference_session(path, sess_options=None, providers=None):
        name = _provider_name(providers)
        if name == "CUDAExecutionProvider":
            if cuda_build_error is not None:
                raise RuntimeError(cuda_build_error)
            record["cuda_calls"] += 1
        elif name == "DmlExecutionProvider":
            if dml_build_error is not None:
                raise RuntimeError(dml_build_error)
            record["dml_calls"] += 1
        elif name == "ROCMExecutionProvider":
            if rocm_build_error is not None:
                raise RuntimeError(rocm_build_error)
            record["rocm_calls"] += 1
        else:
            record["cpu_calls"] += 1
        record["sessions"].append((path, providers, sess_options))
        return types.SimpleNamespace(run=lambda *_a, **_kw: [], providers=providers)

    def _available_providers():
        names = []
        if cuda_available:
            names.append("CUDAExecutionProvider")
        if dml_available:
            names.append("DmlExecutionProvider")
        if rocm_available:
            names.append("ROCMExecutionProvider")
        names.append("CPUExecutionProvider")
        return names

    fake = types.SimpleNamespace(
        SessionOptions=_FakeSessionOptions,
        GraphOptimizationLevel=types.SimpleNamespace(ORT_ENABLE_ALL="all"),
        get_available_providers=_available_providers,
        InferenceSession=_inference_session,
    )
    return fake, record


def _fake_cuda_injection(monkeypatch, available):
    """把 DLL 注入置为成功 + 注入 fake torch（cuda.is_available=available）。"""
    monkeypatch.setattr(bridge, "_inject_cuda_dll_paths", lambda: True)
    monkeypatch.setitem(
        sys.modules, "torch",
        types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: available)),
    )


def test_cuda_unavailable_providers_falls_back_to_cpu(tmp_path, monkeypatch, capsys):
    """provider 枚举不含 CUDA（CPU 版包）：直接回退 CPU EP。"""
    fake, record = _make_fake_ort(cuda_available=False)
    _write_fake_assets(tmp_path)
    monkeypatch.setattr(bridge, "_ort_asset_dir", lambda: str(tmp_path))
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)
    assert bridge._load_ort_sessions()
    assert bridge._ORT_PROVIDER == "cpu"
    assert record["cuda_calls"] == 0 and record["cpu_calls"] == 1
    assert "未含 CUDAExecutionProvider" in capsys.readouterr().err


def test_cuda_dll_injection_failure_falls_back(tmp_path, monkeypatch, capsys):
    """DLL 注入失败（torch 不可用）：回退 CPU EP（主环境天然覆盖该分支）。"""
    fake, record = _make_fake_ort(cuda_available=True)
    _write_fake_assets(tmp_path)
    monkeypatch.setattr(bridge, "_ort_asset_dir", lambda: str(tmp_path))
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)
    monkeypatch.setitem(sys.modules, "torch", None)  # import torch 将抛 ImportError
    assert bridge._load_ort_sessions()
    assert bridge._ORT_PROVIDER == "cpu"
    assert record["cuda_calls"] == 0 and record["cpu_calls"] == 1
    assert "DLL 注入失败" in capsys.readouterr().err


def test_torch_cuda_unavailable_falls_back(tmp_path, monkeypatch, capsys):
    """torch 判定 CUDA 不可用：回退 CPU EP。"""
    fake, record = _make_fake_ort(cuda_available=True)
    _write_fake_assets(tmp_path)
    monkeypatch.setattr(bridge, "_ort_asset_dir", lambda: str(tmp_path))
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)
    _fake_cuda_injection(monkeypatch, available=False)
    assert bridge._load_ort_sessions()
    assert bridge._ORT_PROVIDER == "cpu"
    assert record["cuda_calls"] == 0 and record["cpu_calls"] == 1
    assert "torch 判定 CUDA 不可用" in capsys.readouterr().err


def test_cuda_session_build_failure_falls_back(tmp_path, monkeypatch, capsys):
    """CUDA 会话构造失败：整体回退 CPU EP（不留半套会话）。"""
    fake, record = _make_fake_ort(cuda_available=True, cuda_build_error="CUDA 初始化失败")
    _write_fake_assets(tmp_path)
    monkeypatch.setattr(bridge, "_ort_asset_dir", lambda: str(tmp_path))
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)
    _fake_cuda_injection(monkeypatch, available=True)
    assert bridge._load_ort_sessions()
    assert bridge._ORT_PROVIDER == "cpu"
    assert record["cuda_calls"] == 0 and record["cpu_calls"] == 1
    err = capsys.readouterr().err
    assert "CUDA 会话构造失败" in err and "回退 CPU EP" in err


def test_cuda_full_chain_selected_with_tf32_off(tmp_path, monkeypatch, capsys):
    """CUDA 探测全链可用：单模块（dec）以 CUDA EP 构造（tf32=0；不得禁用 CPU EP 回退）。"""
    fake, record = _make_fake_ort(cuda_available=True)
    _write_fake_assets(tmp_path)
    monkeypatch.setattr(bridge, "_ort_asset_dir", lambda: str(tmp_path))
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)
    _fake_cuda_injection(monkeypatch, available=True)
    assert bridge._load_ort_sessions()
    assert bridge._ORT_PROVIDER == "cuda"
    assert record["cuda_calls"] == 1 and record["cpu_calls"] == 0
    for _path, providers, opts in record["sessions"]:
        assert providers[0][0] == "CUDAExecutionProvider"
        assert providers[0][1]["use_tf32"] == 0
        assert providers[0][1]["device_id"] == bridge.TTS_ORT_CUDA_DEVICE_ID
        # 图含少量 CPU 分配节点：禁用 CPU EP 回退会拒绝建会话（真机实测否证）
        assert "session.disable_cpu_ep_fallback" not in opts.entries
    assert f"CUDA EP(device={bridge.TTS_ORT_CUDA_DEVICE_ID}, tf32=off)" in capsys.readouterr().err


def test_force_cpu_flag_skips_cuda_probe(tmp_path, monkeypatch, capsys):
    """熔断标志置位（曾降级）：不再探测 CUDA，直接 CPU EP（只降不升）。"""
    fake, record = _make_fake_ort(cuda_available=True)
    _write_fake_assets(tmp_path)
    monkeypatch.setattr(bridge, "_ort_asset_dir", lambda: str(tmp_path))
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)
    _fake_cuda_injection(monkeypatch, available=True)
    monkeypatch.setattr(bridge, "_ORT_FORCE_CPU", True)
    assert bridge._load_ort_sessions()
    assert bridge._ORT_PROVIDER == "cpu"
    assert record["cuda_calls"] == 0 and record["cpu_calls"] == 1
    assert "CPU EP" in capsys.readouterr().err


# ------------------------------------------------------------------ #
# 运行时熔断降级                                                      #
# ------------------------------------------------------------------ #


class _RunProbe:
    """按标签记录调用并在指定标签抛异常的 fake 会话。"""

    def __init__(self, tag, fail=False):
        self.tag = tag
        self.fail = fail

    def run(self, *_args, **_kwargs):
        if self.fail:
            raise RuntimeError(f"{self.tag} run failed")
        return [self.tag, "ok"]


def test_run_with_cpu_fallback_retries_on_cuda_error(monkeypatch, capsys):
    """CUDA 会话推理异常：熔断重建 CPU 会话并重试一次（同请求内恢复）。"""
    rebuilt = {"n": 0}

    def fake_load():
        rebuilt["n"] += 1
        bridge._ORT_SESSIONS = {"dec": _RunProbe("cpu")}
        bridge._ORT_PROVIDER = "cpu"
        return bridge._ORT_SESSIONS

    bridge._ORT_SESSIONS = {"dec": _RunProbe("gpu", fail=True)}
    bridge._ORT_PROVIDER = "cuda"
    monkeypatch.setattr(bridge, "_load_ort_sessions", fake_load)
    result = bridge._run_session_with_fallback("dec", lambda session: session.run(None, {}))
    assert result == ["cpu", "ok"]
    assert bridge._ORT_FORCE_CPU is True
    assert rebuilt["n"] == 1
    assert "熔断降级到 CPU 会话并重试一次" in capsys.readouterr().err


def test_run_without_fallback_for_cpu_session_error(monkeypatch):
    """CPU 会话异常不熔断（非 GPU 语义）：原样抛出，不改熔断标志。"""
    bridge._ORT_SESSIONS = {"dec": _RunProbe("cpu", fail=True)}
    bridge._ORT_PROVIDER = "cpu"
    with pytest.raises(RuntimeError, match="cpu run failed"):
        bridge._run_session_with_fallback("dec", lambda session: session.run(None, {}))
    assert bridge._ORT_FORCE_CPU is False


def test_retry_failure_raises_original_error(monkeypatch):
    """熔断重建失败：抛原始异常（由请求级兜底接管，不吞异常）。"""
    bridge._ORT_SESSIONS = {"dec": _RunProbe("gpu", fail=True)}
    bridge._ORT_PROVIDER = "cuda"
    monkeypatch.setattr(bridge, "_load_ort_sessions", lambda: False)
    with pytest.raises(RuntimeError, match="gpu run failed"):
        bridge._run_session_with_fallback("dec", lambda session: session.run(None, {}))


def test_fallback_to_cpu_is_idempotent(monkeypatch):
    """熔断幂等：已降级后再次调用直接返回现有 CPU 会话（不重复重建）。"""
    calls = {"n": 0}

    def fake_load():
        calls["n"] += 1
        bridge._ORT_SESSIONS = {"dec": _RunProbe("cpu")}
        bridge._ORT_PROVIDER = "cpu"
        return bridge._ORT_SESSIONS

    bridge._ORT_SESSIONS = {"dec": _RunProbe("gpu")}
    bridge._ORT_PROVIDER = "cuda"
    monkeypatch.setattr(bridge, "_load_ort_sessions", fake_load)
    first = bridge._fallback_ort_to_cpu()
    second = bridge._fallback_ort_to_cpu()
    assert first is second
    assert calls["n"] == 1


def test_session_missing_raises():
    """会话缺失（异常时序）：抛出可诊断的 RuntimeError。"""
    bridge._ORT_SESSIONS = {}
    with pytest.raises(RuntimeError, match="ORT 会话缺失"):
        bridge._run_session_with_fallback("dec", lambda session: None)


_HAS_TORCH = importlib.util.find_spec("torch") is not None
_HAS_ORT = importlib.util.find_spec("onnxruntime") is not None
_HAS_ASSETS = all(
    os.path.isfile(os.path.join(bridge._ort_asset_dir(), f"{name}.onnx"))
    for name in bridge.TTS_ORT_MODULE_NAMES
)


@pytest.mark.skipif(
    not (_HAS_TORCH and _HAS_ORT and _HAS_ASSETS),
    reason="需要 torch + onnxruntime + 真资产（sidecar 环境）",
)
def test_apply_replaces_modules_with_real_sessions():
    """真替换路径：dec 子模块被替换为 ORT 包装（需真实环境）。"""

    class _Engine:
        class _Model:
            dec = None

        def __init__(self):
            self.model = self._Model()

    engine = _Engine()
    assert bridge._apply_ort_accel(engine) is True
    for name in bridge.TTS_ORT_MODULE_NAMES:
        assert getattr(engine.model, name) is not None


# ------------------------------------------------------------------ #
# 多后端（--accel / --accel-device；2026-10-01 双模式 spec）            #
# ------------------------------------------------------------------ #


def test_accel_value_normalization(capsys):
    """--accel 归一：值域内原样（含大小写/空白）；非法回 auto + 中文日志。"""
    assert bridge.ACCEL_CHOICES == ("off", "cpu", "auto", "cuda", "dml", "rocm")
    for value in bridge.ACCEL_CHOICES:
        assert bridge._normalize_accel(value) == value
    assert bridge._normalize_accel(" CUDA ") == "cuda"
    assert bridge._normalize_accel("tensorrt") == "auto"
    assert bridge._normalize_accel(None) == "auto"
    err = capsys.readouterr().err
    assert "非法 --accel 值" in err and "归一为 auto" in err


def test_accel_device_value_normalization(capsys):
    """--accel-device 归一：值域内原样；非法回空（自动）+ 中文日志。"""
    assert bridge.ACCEL_DEVICE_CHOICES == ("", "igpu", "dgpu")
    for value in bridge.ACCEL_DEVICE_CHOICES:
        assert bridge._normalize_accel_device(value) == value
    assert bridge._normalize_accel_device(" IGPU ") == "igpu"
    assert bridge._normalize_accel_device("npu") == ""
    err = capsys.readouterr().err
    assert "非法 --accel-device 值" in err


def test_accel_off_skips_ort_entirely(tmp_path, monkeypatch, capsys):
    """--accel off：不加载 ORT（纯 torch）——关闭分支优先于资产检查。"""
    monkeypatch.setattr(bridge, "_ACCEL", "off")
    monkeypatch.setitem(sys.modules, "onnxruntime", None)
    assert bridge._load_ort_sessions() is False
    assert "纯 torch 推理" in capsys.readouterr().err


def test_accel_cpu_skips_gpu_probe(tmp_path, monkeypatch, capsys):
    """--accel cpu：跳过 CUDA/DML 探测直用 CPU EP（即使 CUDA 可用也不构造）。"""
    fake, record = _make_fake_ort(cuda_available=True, dml_available=True)
    _write_fake_assets(tmp_path)
    monkeypatch.setattr(bridge, "_ort_asset_dir", lambda: str(tmp_path))
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)
    _fake_cuda_injection(monkeypatch, available=True)  # 若探测 CUDA 会命中
    monkeypatch.setattr(bridge, "_ACCEL", "cpu")
    assert bridge._load_ort_sessions()
    assert bridge._ORT_PROVIDER == "cpu"
    assert record["cuda_calls"] == 0 and record["dml_calls"] == 0
    assert record["cpu_calls"] == 1
    assert "CPU EP" in capsys.readouterr().err


def test_accel_cuda_forces_cuda_chain(tmp_path, monkeypatch):
    """--accel cuda：走既有 CUDA 探测链（tf32-off 保留）。"""
    fake, record = _make_fake_ort(cuda_available=True)
    _write_fake_assets(tmp_path)
    monkeypatch.setattr(bridge, "_ort_asset_dir", lambda: str(tmp_path))
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)
    _fake_cuda_injection(monkeypatch, available=True)
    monkeypatch.setattr(bridge, "_ACCEL", "cuda")
    assert bridge._load_ort_sessions()
    assert bridge._ORT_PROVIDER == "cuda"
    assert record["cuda_calls"] == 1 and record["cpu_calls"] == 0
    for _path, providers, _opts in record["sessions"]:
        assert providers[0][0] == "CUDAExecutionProvider"
        assert providers[0][1]["use_tf32"] == 0


def test_accel_auto_still_probes_cuda(tmp_path, monkeypatch):
    """--accel auto：运行时探测（CUDA 优先）——既有语义保持。"""
    fake, record = _make_fake_ort(cuda_available=True)
    _write_fake_assets(tmp_path)
    monkeypatch.setattr(bridge, "_ort_asset_dir", lambda: str(tmp_path))
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)
    _fake_cuda_injection(monkeypatch, available=True)
    monkeypatch.setattr(bridge, "_ACCEL", "auto")
    assert bridge._load_ort_sessions()
    assert bridge._ORT_PROVIDER == "cuda"
    assert record["cuda_calls"] == 1


def test_accel_dml_builds_dml_provider_with_hint(tmp_path, monkeypatch, capsys):
    """--accel dml：构造 DmlExecutionProvider 会话；设备提示记录意图（精确映射未闭合）。"""
    fake, record = _make_fake_ort(cuda_available=False, dml_available=True)
    _write_fake_assets(tmp_path)
    monkeypatch.setattr(bridge, "_ort_asset_dir", lambda: str(tmp_path))
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)
    monkeypatch.setattr(bridge, "_ACCEL", "dml")
    monkeypatch.setattr(bridge, "_ACCEL_DEVICE", "igpu")
    assert bridge._load_ort_sessions()
    assert bridge._ORT_PROVIDER == "dml"
    assert record["dml_calls"] == 1 and record["cpu_calls"] == 0
    for _path, providers, _opts in record["sessions"]:
        assert providers[0][0] == "DmlExecutionProvider"
    err = capsys.readouterr().err
    assert "accel_device=igpu" in err and "精确映射未闭合" in err
    assert "DML EP(device_hint=igpu)" in err


def test_accel_dml_without_hint_logs_auto(tmp_path, monkeypatch, capsys):
    """--accel dml 且无设备提示：仍成功构造（device_hint=auto）。"""
    fake, record = _make_fake_ort(cuda_available=False, dml_available=True)
    _write_fake_assets(tmp_path)
    monkeypatch.setattr(bridge, "_ort_asset_dir", lambda: str(tmp_path))
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)
    monkeypatch.setattr(bridge, "_ACCEL", "dml")
    monkeypatch.setattr(bridge, "_ACCEL_DEVICE", "")
    assert bridge._load_ort_sessions()
    assert bridge._ORT_PROVIDER == "dml"
    assert record["dml_calls"] == 1
    assert "DML EP(device_hint=auto)" in capsys.readouterr().err


def test_accel_dml_package_unavailable_falls_back(tmp_path, monkeypatch, capsys):
    """onnxruntime 包不含 DML EP（如 CPU 版）：回退 CPU EP 并记明原因。"""
    fake, record = _make_fake_ort(cuda_available=False, dml_available=False)
    _write_fake_assets(tmp_path)
    monkeypatch.setattr(bridge, "_ort_asset_dir", lambda: str(tmp_path))
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)
    monkeypatch.setattr(bridge, "_ACCEL", "dml")
    assert bridge._load_ort_sessions()
    assert bridge._ORT_PROVIDER == "cpu"
    assert record["dml_calls"] == 0 and record["cpu_calls"] == 1
    err = capsys.readouterr().err
    assert "未含 DmlExecutionProvider" in err and "回退 CPU EP" in err


def test_accel_dml_build_failure_falls_back(tmp_path, monkeypatch, capsys):
    """DML 会话构造失败：整体回退 CPU EP（不中断）。"""
    fake, record = _make_fake_ort(dml_available=True, dml_build_error="DML 初始化失败")
    _write_fake_assets(tmp_path)
    monkeypatch.setattr(bridge, "_ort_asset_dir", lambda: str(tmp_path))
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)
    monkeypatch.setattr(bridge, "_ACCEL", "dml")
    monkeypatch.setattr(bridge, "_ACCEL_DEVICE", "dgpu")
    assert bridge._load_ort_sessions()
    assert bridge._ORT_PROVIDER == "cpu"
    assert record["dml_calls"] == 0 and record["cpu_calls"] == 1
    err = capsys.readouterr().err
    assert "DML 会话构造失败" in err and "回退 CPU EP" in err


def test_accel_rocm_without_ep_falls_back(tmp_path, monkeypatch, capsys):
    """--accel rocm 无 ROCm EP（Windows 无分发）：明确日志回退 CPU EP。"""
    fake, record = _make_fake_ort(cuda_available=True, rocm_available=False)
    _write_fake_assets(tmp_path)
    monkeypatch.setattr(bridge, "_ort_asset_dir", lambda: str(tmp_path))
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)
    monkeypatch.setattr(bridge, "_ACCEL", "rocm")
    assert bridge._load_ort_sessions()
    assert bridge._ORT_PROVIDER == "cpu"
    assert record["rocm_calls"] == 0 and record["cpu_calls"] == 1
    err = capsys.readouterr().err
    assert "未含 ROCMExecutionProvider" in err and "回退 CPU EP" in err


def test_accel_rocm_with_ep_builds(tmp_path, monkeypatch):
    """--accel rocm 且有 ROCm EP（Linux 预留）：构造 ROCm 会话。"""
    fake, record = _make_fake_ort(rocm_available=True)
    _write_fake_assets(tmp_path)
    monkeypatch.setattr(bridge, "_ort_asset_dir", lambda: str(tmp_path))
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)
    monkeypatch.setattr(bridge, "_ACCEL", "rocm")
    assert bridge._load_ort_sessions()
    assert bridge._ORT_PROVIDER == "rocm"
    assert record["rocm_calls"] == 1 and record["cpu_calls"] == 0


def test_dml_session_error_triggers_circuit_break(monkeypatch, capsys):
    """DML 会话推理异常：熔断降级（GPU 类 provider 判定涵盖 dml）。"""
    rebuilt = {"n": 0}

    def fake_load():
        rebuilt["n"] += 1
        bridge._ORT_SESSIONS = {"dec": _RunProbe("cpu")}
        bridge._ORT_PROVIDER = "cpu"
        return bridge._ORT_SESSIONS

    bridge._ORT_SESSIONS = {"dec": _RunProbe("gpu", fail=True)}
    bridge._ORT_PROVIDER = "dml"
    monkeypatch.setattr(bridge, "_load_ort_sessions", fake_load)
    result = bridge._run_session_with_fallback("dec", lambda session: session.run(None, {}))
    assert result == ["cpu", "ok"]
    assert bridge._ORT_FORCE_CPU is True
    assert rebuilt["n"] == 1
    assert "熔断降级到 CPU 会话并重试一次" in capsys.readouterr().err


# ------------------------------------------------------------------ #
# 客户端透传（accel / accel_device；旧调用零修改）                       #
# ------------------------------------------------------------------ #


def test_client_accel_defaults_and_normalization(tmp_path):
    """客户端 accel/accel_device 缺省与归一（旧调用零修改可用）。"""
    from lite.audio.voice_bridge_client import VoiceBridgeClient

    default = VoiceBridgeClient(root=str(tmp_path))
    assert default.accel == "auto" and default.accel_device == ""
    assert VoiceBridgeClient._normalize_accel("DML") == "dml"
    assert VoiceBridgeClient._normalize_accel("tensorrt") == "auto"
    assert VoiceBridgeClient._normalize_accel(None) == "auto"
    assert VoiceBridgeClient._normalize_accel_device("IGPU ") == "igpu"
    assert VoiceBridgeClient._normalize_accel_device("npu") == ""
    explicit = VoiceBridgeClient(root=str(tmp_path), accel="dml", accel_device="igpu")
    assert explicit.accel == "dml" and explicit.accel_device == "igpu"


def test_client_argv_injects_accel_only_when_non_default(tmp_path):
    """命令行注入：非缺省时带 --accel / --accel-device；缺省不注入（向后兼容）。"""
    from lite.audio.voice_bridge_client import VoiceBridgeClient

    base = VoiceBridgeClient(root=str(tmp_path), python_exe="py", script_path="bridge.py")
    argv = base._bridge_argv()
    assert "--accel" not in argv and "--accel-device" not in argv

    explicit = VoiceBridgeClient(
        root=str(tmp_path), python_exe="py", script_path="bridge.py",
        accel="dml", accel_device="dgpu",
    )
    argv = explicit._bridge_argv()
    assert argv[argv.index("--accel") + 1] == "dml"
    assert argv[argv.index("--accel-device") + 1] == "dgpu"


# ------------------------------------------------------------------ #
# 工厂透传（_try_voice_bridge 读 tts.accel / tts.accel_device）          #
# ------------------------------------------------------------------ #


def _make_sidecar_root(tmp_path):
    """构造 sidecar 结构（python.exe + 分发落点 bridge.py），返回便携根。"""
    root = tmp_path / "portable"
    (root / "runtime" / "voice").mkdir(parents=True)
    (root / "runtime" / "voice" / "python.exe").write_bytes(b"fake")
    (root / "runtime" / "voice_bridge").mkdir(parents=True)
    (root / "runtime" / "voice_bridge" / "bridge.py").write_text("# bridge", encoding="utf-8")
    return root


def test_factory_reads_tts_accel(tmp_path):
    """工厂读取 tts.accel / tts.accel_device 并透传客户端。"""
    from lite.audio import _try_voice_bridge

    root = _make_sidecar_root(tmp_path)
    backends = _try_voice_bridge(
        {"device": "cpu"},
        {"device": "cpu", "accel": "dml", "accel_device": "igpu"},
        root=str(root),
    )
    assert backends is not None
    client = backends[0].client
    assert client.accel == "dml" and client.accel_device == "igpu"


def test_factory_accel_missing_keys_default(tmp_path):
    """工厂缺键缺省：无 accel / accel_device 键时等价 auto / ""。"""
    from lite.audio import _try_voice_bridge

    root = _make_sidecar_root(tmp_path)
    backends = _try_voice_bridge({}, {}, root=str(root))
    assert backends is not None
    client = backends[0].client
    assert client.accel == "auto" and client.accel_device == ""


def test_factory_accel_illegal_normalized(tmp_path):
    """工厂非法值归一：由客户端归一为 auto / ""（不抛、不阻断装配）。"""
    from lite.audio import _try_voice_bridge

    root = _make_sidecar_root(tmp_path)
    backends = _try_voice_bridge(
        {}, {"accel": "tensorrt", "accel_device": "npu"}, root=str(root)
    )
    assert backends is not None
    client = backends[0].client
    assert client.accel == "auto" and client.accel_device == ""