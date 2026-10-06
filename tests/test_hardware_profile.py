# -*- coding: utf-8 -*-
"""Task 3 / Task 4 硬件画像与推荐配置单元测试（pytest，mock runner 注入）。

硬约束：全部用例不触碰真实硬件命令、不联网——GPU / 显存探测经 mock runner
注入；档位表经 ``sys.modules`` 注入替身，避免依赖并行开发的 Task 5 落地状态。

覆盖：
- detect_profile：runner 全失败时降级为 cpu，probe_notes 非空，不抛异常；
- probe_memory_gb：psutil 缺失时走兜底（不抛，返回 float 或 None）；
- recommend_for 五条分支：内存不足走云 / 8GB → 1.7B+cpu / nvidia 10GB 显存
  → gpu+8B / nvidia 6GB 显存 → gpu+4B / 磁盘不足逐级降档（含最小档也不足）；
- vram < 4 维持 cpu；ram_gb 为 None 走云；
- model 延迟导入不可用时返回 None 且 probe_notes 有中文说明。
"""

import json
import sys
import types

from lite.runtime import hardware_profile as hp

#: 档位替身表（含 repo / filename / 视觉组件 / 预估体积），仅用于测试注入。
_FAKE_TIERS = {
    "E2B-Q4": {
        "repo": "unsloth/gemma-4-E2B-it-GGUF",
        "filename": "gemma-4-E2B-it-Q4_K_M.gguf",
        "mmproj_filename": "mmproj-BF16.gguf",
        "mmproj_size_gb": 0.919,
        "approximate_size_gb": 2.894,
        "family": "gemma-4-E2B-it",
        "quant": "Q4_K_M",
    },
    "E2B-Q6": {
        "repo": "unsloth/gemma-4-E2B-it-GGUF",
        "filename": "gemma-4-E2B-it-Q6_K.gguf",
        "mmproj_filename": "mmproj-BF16.gguf",
        "mmproj_size_gb": 0.919,
        "approximate_size_gb": 4.193,
        "family": "gemma-4-E2B-it",
        "quant": "Q6_K",
    },
    "E4B-Q4": {
        "repo": "unsloth/gemma-4-E4B-it-GGUF",
        "filename": "gemma-4-E4B-it-Q4_K_M.gguf",
        "mmproj_filename": "mmproj-BF16.gguf",
        "mmproj_size_gb": 0.923,
        "approximate_size_gb": 4.635,
        "family": "gemma-4-E4B-it",
        "quant": "Q4_K_M",
    },
    "E4B-Q6": {
        "repo": "unsloth/gemma-4-E4B-it-GGUF",
        "filename": "gemma-4-E4B-it-Q6_K.gguf",
        "mmproj_filename": "mmproj-BF16.gguf",
        "mmproj_size_gb": 0.923,
        "approximate_size_gb": 6.589,
        "family": "gemma-4-E4B-it",
        "quant": "Q6_K",
    },
}


def _fail_runner(cmd):
    """全部命令失败的 mock runner（模拟无独显 / 命令不可用）。"""
    return -1, ""


def _inject_tiers(monkeypatch, tiers=_FAKE_TIERS):
    """向 sys.modules 注入带 MODEL_TIERS 的 model_downloader 替身。"""
    fake = types.ModuleType("lite.runtime.model_downloader")
    fake.MODEL_TIERS = tiers
    monkeypatch.setitem(sys.modules, "lite.runtime.model_downloader", fake)


def _inject_missing_tiers(monkeypatch):
    """注入不含 MODEL_TIERS 的替身，模拟档位表尚未落地（延迟导入失败）。"""
    fake = types.ModuleType("lite.runtime.model_downloader")
    monkeypatch.setitem(sys.modules, "lite.runtime.model_downloader", fake)


def _profile(**overrides):
    """构造基础画像 dict（默认无独显 + 16GB 内存 + 磁盘充足）。"""
    profile = {
        "cpu_cores": 8,
        "ram_gb": 16.0,
        "gpu_vendor": "cpu",
        "vram_gb": None,
        "cuda_version": None,
        "disk_free_gb": 100.0,
        "probe_notes": [],
    }
    profile.update(overrides)
    return profile


# ------------------------------------------------------------------ #
# detect_profile：探测降级                                             #
# ------------------------------------------------------------------ #

def test_detect_profile_degrades_when_runner_fails():
    """runner 全失败 → gpu_vendor=cpu、probe_notes 非空、不抛异常。"""
    profile = hp.detect_profile(root=".", runner=_fail_runner)

    assert profile["gpu_vendor"] == "cpu"
    assert profile["cuda_version"] is None
    assert profile["probe_notes"]  # 至少记录 CPU 兜底说明
    assert isinstance(profile["probe_notes"], list)
    # 字段齐全（允许 None，但键必须存在）
    for key in ("cpu_cores", "ram_gb", "vram_gb", "disk_free_gb"):
        assert key in profile


def test_detect_profile_reports_nvidia_without_vram():
    """nvidia-smi 命中但显存查询失败 → 记录显存未知说明，仍返回可用结论。"""
    def runner(cmd):
        if "nvidia-smi" in cmd and "--query-gpu" in cmd:
            return -1, ""  # 显存查询失败
        if "nvidia-smi" in cmd:
            return 0, "NVIDIA-SMI 551.61  Driver Version: 551.61  CUDA Version: 12.4\n"
        return -1, ""

    profile = hp.detect_profile(root=".", runner=runner)

    assert profile["gpu_vendor"] == "nvidia"
    assert profile["cuda_version"] == "12.4"
    assert profile["vram_gb"] is None
    assert any("显存" in note for note in profile["probe_notes"])


# ------------------------------------------------------------------ #
# probe_memory_gb：psutil 缺失兜底                                     #
# ------------------------------------------------------------------ #

def test_probe_memory_gb_without_psutil_falls_back(monkeypatch):
    """psutil 不可用时走兜底：不抛异常，返回 float（GB）或 None。"""
    monkeypatch.setitem(sys.modules, "psutil", None)  # import psutil 抛 ImportError

    result = hp.probe_memory_gb()

    assert result is None or isinstance(result, float)
    if result is not None:
        assert result > 0


# ------------------------------------------------------------------ #
# recommend_for：五条分支                                              #
# ------------------------------------------------------------------ #

def test_recommend_insufficient_memory_goes_cloud(monkeypatch):
    """内存不足（< 8GB）→ 走云：use_local=False、local_llm.enabled=False。"""
    _inject_tiers(monkeypatch)
    profile = _profile(ram_gb=4.0)

    result = hp.recommend_for(profile)

    assert result["use_local"] is False
    assert result["tier"] == "E2B-Q4"
    assert result["config_patch"]["local_llm"] == {"enabled": False, "device": "cpu"}
    assert result["model"] is None
    assert any("内存" in reason and "云端" in reason for reason in result["reasons"])


def test_recommend_8gb_memory_cpu_e2b_q4(monkeypatch):
    """内存 8GB → 本机 cpu + E2B-Q4 档，model 含 repo / 文件名 / 预估体积。"""
    _inject_tiers(monkeypatch)
    profile = _profile(ram_gb=8.0)

    result = hp.recommend_for(profile)

    assert result["use_local"] is True
    assert result["device"] == "cpu"
    assert result["tier"] == "E2B-Q4"
    assert result["config_patch"]["local_llm"] == {"enabled": True, "device": "cpu"}
    assert "embedding" not in result["config_patch"]
    assert result["model"]["repo"]
    assert result["model"]["filename"]
    assert result["model"]["approximate_size_gb"] == 2.894


def test_recommend_nvidia_10gb_vram_gpu_e4b_q6(monkeypatch):
    """nvidia 10GB 显存 + 内存充足 → device=gpu、tier=E4B-Q6、embedding 同步走 gpu。"""
    _inject_tiers(monkeypatch)
    profile = _profile(ram_gb=32.0, gpu_vendor="nvidia", vram_gb=10.0, cuda_version="12.4")

    result = hp.recommend_for(profile)

    assert result["use_local"] is True
    assert result["device"] == "gpu"
    assert result["tier"] == "E4B-Q6"
    # 20261002 批 A：N 卡走 GPU 时 config_patch 并入 backend=cuda（既有断言变更留痕）
    assert result["config_patch"]["local_llm"] == {
        "enabled": True, "device": "gpu", "backend": "cuda",
    }
    assert result["config_patch"]["embedding"] == {"device": "gpu", "backend": "cuda"}
    assert any("显存" in reason for reason in result["reasons"])


def test_recommend_nvidia_6gb_vram_gpu_e4b_q4(monkeypatch):
    """nvidia 6GB 显存 → device=gpu、tier=E4B-Q4。"""
    _inject_tiers(monkeypatch)
    profile = _profile(ram_gb=32.0, gpu_vendor="nvidia", vram_gb=6.0)

    result = hp.recommend_for(profile)

    assert result["device"] == "gpu"
    assert result["tier"] == "E4B-Q4"
    assert result["model"]["approximate_size_gb"] == 4.635


def test_recommend_nvidia_high_vram_capped_by_memory(monkeypatch):
    """显存 10GB 但内存 < 16GB → 档位受内存上限约束，最高 E4B-Q4。"""
    _inject_tiers(monkeypatch)
    profile = _profile(ram_gb=8.0, gpu_vendor="nvidia", vram_gb=10.0)

    result = hp.recommend_for(profile)

    assert result["device"] == "gpu"
    assert result["tier"] == "E4B-Q4"
    assert any("内存" in reason and "E4B-Q4" in reason for reason in result["reasons"])


def test_recommend_vram_below_4gb_keeps_cpu(monkeypatch):
    """显存 < 4GB → 维持 cpu + E2B-Q4，不上 GPU。"""
    _inject_tiers(monkeypatch)
    profile = _profile(ram_gb=32.0, gpu_vendor="nvidia", vram_gb=2.0)

    result = hp.recommend_for(profile)

    assert result["device"] == "cpu"
    assert result["tier"] == "E2B-Q4"
    assert result["config_patch"]["local_llm"] == {"enabled": True, "device": "cpu"}
    assert "embedding" not in result["config_patch"]


def test_recommend_disk_insufficient_downgrades_tier(monkeypatch):
    """磁盘 4.5GB 放不下 E4B-Q4（4.635×1.05）→ 逐级降档到 E2B-Q6。"""
    _inject_tiers(monkeypatch)
    profile = _profile(ram_gb=32.0, gpu_vendor="nvidia", vram_gb=10.0)

    result = hp.recommend_for(profile, disk_free_gb=4.5)

    assert result["use_local"] is True
    assert result["tier"] == "E2B-Q6"
    assert any("磁盘" in reason and "E2B-Q6" in reason for reason in result["reasons"])


def test_recommend_disk_insufficient_even_for_smallest_goes_cloud(monkeypatch):
    """连最小档 E2B-Q4（2.894×1.05）都不足 → use_local=False，理由含所需/可用对比。"""
    _inject_tiers(monkeypatch)
    profile = _profile(ram_gb=32.0, gpu_vendor="nvidia", vram_gb=10.0)

    result = hp.recommend_for(profile, disk_free_gb=0.1)

    assert result["use_local"] is False
    assert result["tier"] == "E2B-Q4"
    assert result["config_patch"]["local_llm"]["enabled"] is False
    assert any(
        "所需" in reason and "可用" in reason and "磁盘" in reason
        for reason in result["reasons"]
    )


def test_recommend_disk_param_overrides_profile_field(monkeypatch):
    """disk_free_gb 参数优先于画像字段。"""
    _inject_tiers(monkeypatch)
    profile = _profile(ram_gb=32.0, gpu_vendor="nvidia", vram_gb=10.0, disk_free_gb=100.0)

    result = hp.recommend_for(profile, disk_free_gb=3.5)

    assert result["tier"] == "E2B-Q4"


# ------------------------------------------------------------------ #
# recommend_for：内存未知 / 档位表缺失                                 #
# ------------------------------------------------------------------ #

def test_recommend_unknown_ram_goes_cloud_with_note(monkeypatch):
    """ram_gb 为 None → 走云，probe_notes 记录未知说明。"""
    _inject_tiers(monkeypatch)
    profile = _profile(ram_gb=None)

    result = hp.recommend_for(profile)

    assert result["use_local"] is False
    assert result["config_patch"]["local_llm"]["enabled"] is False
    assert any("内存" in note and "未知" in note for note in result["probe_notes"])


def test_recommend_model_none_when_tiers_unavailable(monkeypatch):
    """MODEL_TIERS 延迟导入不可用 → model=None，probe_notes 追加中文说明。"""
    _inject_missing_tiers(monkeypatch)
    profile = _profile(ram_gb=16.0)

    result = hp.recommend_for(profile)

    assert result["use_local"] is True
    assert result["model"] is None
    assert any("档位" in note for note in result["probe_notes"])


def test_recommend_no_raise_when_probe_notes_missing():
    """画像缺 probe_notes / 磁盘字段时也不抛异常（健壮性）。"""
    result = hp.recommend_for({"ram_gb": 8.0, "gpu_vendor": "cpu"})

    assert result["use_local"] is True
    assert isinstance(result["probe_notes"], list)


def test_detect_profile_default_root_follows_portable_root_when_frozen(tmp_path, monkeypatch):
    """M-14 回归：冻结态未显式传 ``root`` 时，磁盘余量必须按便携根估算。

    修复前 ``root`` 缺省用 ``__file__`` 上溯两层，冻结态得 ``runtime/backend/_internal``，
    磁盘预检会指向运行时目录（与安装根不同盘时判断直接失真）。
    """
    import os
    import sys

    portable = tmp_path / "CX-A-portable"
    backend = portable / "runtime" / "backend"
    backend.mkdir(parents=True)
    (backend / "backend.exe").write_bytes(b"")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(backend / "backend.exe"))

    seen = {}

    def fake_disk_free(path):
        seen["path"] = path
        return 100.0

    monkeypatch.setattr(hp, "probe_disk_free_gb", fake_disk_free)
    # runner 全失败 → 走 cpu 降级分支，不触碰真实硬件（runner 契约为 (returncode, stdout)）
    profile = hp.detect_profile(runner=lambda cmd: (1, ""))

    assert "path" in seen, "detect_profile 必须调用磁盘探测"
    assert os.path.normpath(seen["path"]) == os.path.normpath(str(portable))
    assert profile["disk_free_gb"] == 100.0


# ------------------------------------------------------------------ #
# GPU 清单枚举与核显识别（画像扩展）                                   #
# ------------------------------------------------------------------ #

def _cmd_contains(cmd, needle):
    """runner 命令匹配：兼容字符串命令与 argv 列表命令。"""
    if isinstance(cmd, (list, tuple)):
        return any(needle in str(part) for part in cmd)
    return needle in str(cmd)


def _inventory_runner(video_json, nvidia_ok=True, vram_mib="8192"):
    """分派 mock runner：nvidia-smi / 显存查询 / WMI 清单枚举（不触网、不碰真机）。"""
    def runner(cmd):
        if _cmd_contains(cmd, "Get-CimInstance"):
            return 0, video_json
        if _cmd_contains(cmd, "--query-gpu"):
            return (0, vram_mib + "\n") if nvidia_ok else (-1, "")
        if _cmd_contains(cmd, "nvidia-smi"):
            if not nvidia_ok:
                return -1, ""
            return 0, "NVIDIA-SMI 551.61  Driver Version: 551.61  CUDA Version: 12.4\n"
        return -1, ""
    return runner


#: N 卡 + 核显 + 虚拟适配器（虚拟项须被识别但不计入核显/独显结论）。
_NVIDIA_INTEL_JSON = json.dumps([
    {"Name": "NVIDIA GeForce RTX 3080", "PNPDeviceID": "PCI\\VEN_10DE&DEV_2206"},
    {"Name": "Intel(R) UHD Graphics 630", "PNPDeviceID": "PCI\\VEN_8086&DEV_3E98"},
    {"Name": "Microsoft Basic Display Adapter", "PNPDeviceID": "ROOT\\DISPLAY\\0000"},
])

#: 仅核显（Intel）——detect_via_runner 既有口径仍判 cpu。
_INTEL_ONLY_JSON = json.dumps(
    {"Name": "Intel(R) UHD Graphics 770", "PNPDeviceID": "PCI\\VEN_8086&DEV_4680"}
)


def test_probe_gpu_inventory_passes_argv_list():
    """WMI 命令以 argv 列表传入（PowerShell 引号/管道不被切分破坏）。"""
    seen = {}

    def runner(cmd):
        seen["cmd"] = cmd
        return -1, ""

    hp.probe_gpu_inventory(runner=runner)
    assert isinstance(seen["cmd"], list)
    assert any("Get-CimInstance Win32_VideoController" in str(p) for p in seen["cmd"])


def test_detect_profile_nvidia_with_igpu_inventory():
    """场景①：N 卡 + 核显 → 两项分类正确、has_igpu=True、dgpu_vendor=nvidia、虚拟项剔除。"""
    profile = hp.detect_profile(root=".", runner=_inventory_runner(_NVIDIA_INTEL_JSON))

    assert profile["gpu_vendor"] == "nvidia"
    assert profile["has_igpu"] is True
    assert profile["dgpu_vendor"] == "nvidia"
    types = [g["type"] for g in profile["gpus"]]
    assert len(profile["gpus"]) == 3
    assert "dgpu" in types and "igpu" in types and "virtual" in types
    # 虚拟适配器识别为 virtual，不贡献核显结论
    virtual = [g for g in profile["gpus"] if g["type"] == "virtual"]
    assert virtual and virtual[0]["vendor"] == "other"


def test_detect_profile_igpu_only():
    """场景②：仅核显 → has_igpu=True、独显清单为空、recommend 仍 cpu。"""
    profile = hp.detect_profile(
        root=".", runner=_inventory_runner(_INTEL_ONLY_JSON, nvidia_ok=False)
    )

    assert profile["gpu_vendor"] == "cpu"
    assert profile["has_igpu"] is True
    assert profile["dgpu_vendor"] is None
    assert all(g["type"] != "dgpu" for g in profile["gpus"])


def test_detect_profile_inventory_degrades_on_failure():
    """场景③：探测全失败 → 清单为空、结论保守、probe_notes 记录降级，不抛错。"""
    profile = hp.detect_profile(root=".", runner=_fail_runner)

    assert profile["gpus"] == []
    assert profile["has_igpu"] is False
    assert profile["dgpu_vendor"] is None
    assert any("GPU 清单" in note for note in profile["probe_notes"])


def test_probe_gpu_inventory_non_json_degrades():
    """WMI 输出非 JSON → 空清单 + 降级说明，不抛错。"""
    gpus, notes = hp.probe_gpu_inventory(runner=lambda cmd: (0, "not-json<<"))

    assert gpus == []
    assert notes and "JSON" in notes[0]


def test_probe_gpu_inventory_runner_exception_degrades():
    """runner 抛异常 → 空清单 + 降级说明，不抛错。"""
    def boom(cmd):
        raise RuntimeError("no powershell")

    gpus, notes = hp.probe_gpu_inventory(runner=boom)

    assert gpus == []
    assert notes


# ------------------------------------------------------------------ #
# accel_plan：五场景（唯一真相源）                                     #
# ------------------------------------------------------------------ #

def test_accel_plan_performance_nvidia_no_igpu():
    """性能 · 无核显 N 卡 → tts=cuda/""；asr/llm/embedding=gpu。"""
    hw = {"gpu_vendor": "nvidia", "vram_gb": 8.0, "has_igpu": False,
          "dgpu_vendor": "nvidia", "recommend": "cuda"}

    plan = hp.accel_plan(hw, "performance")

    assert plan["accel.mode"] == "performance"
    assert plan["tts.accel"] == "cuda"
    assert plan["tts.accel_device"] == ""
    assert plan["asr.device"] == "gpu"
    assert plan["local_llm.device"] == "gpu"
    assert plan["embedding.device"] == "gpu"


def test_accel_plan_performance_with_igpu_prefers_dml_dgpu():
    """性能 · 有核显（裁决①）→ tts=dml + dgpu；N 卡其余组件仍 gpu。"""
    hw = {"gpu_vendor": "nvidia", "vram_gb": 8.0, "has_igpu": True,
          "dgpu_vendor": "nvidia", "recommend": "cuda"}

    plan = hp.accel_plan(hw, "performance")

    assert plan["tts.accel"] == "dml"
    assert plan["tts.accel_device"] == "dgpu"
    assert plan["asr.device"] == "gpu"
    assert plan["local_llm.device"] == "gpu"


def test_accel_plan_performance_with_igpu_non_nvidia_dgpu_others_cpu():
    """性能 · 有核显 + AMD 独显 → tts=dml/dgpu；LLM/嵌入走 Vulkan GPU（批 A 扩展）。

    既有断言变更留痕（20261002 批 A）：原口径「ASR/LLM 无 GPU 路径 → cpu」中
    LLM/嵌入部分已变——Windows 平台 AMD 独显现走 llama.cpp Vulkan（gpu + vulkan）；
    ASR 维持 cpu（torch 无 Windows ROCm 路径，批 A 未扩展）。
    """
    hw = {"gpu_vendor": "amd", "vram_gb": 8.0, "has_igpu": True,
          "dgpu_vendor": "amd", "recommend": "rocm"}

    plan = hp.accel_plan(hw, "performance")

    assert plan["tts.accel"] == "dml" and plan["tts.accel_device"] == "dgpu"
    assert plan["asr.device"] == "cpu"
    assert plan["local_llm.device"] == "gpu"
    assert plan["local_llm.backend"] == "vulkan"
    assert plan["embedding.device"] == "gpu"
    assert plan["embedding.backend"] == "vulkan"


def test_accel_plan_performance_amd_no_igpu_dml_auto_device():
    """性能 · 无核显 AMD 独显 → tts=dml 且设备提示留空（自动）。"""
    hw = {"gpu_vendor": "amd", "has_igpu": False, "dgpu_vendor": "amd", "recommend": "rocm"}

    plan = hp.accel_plan(hw, "performance")

    assert plan["tts.accel"] == "dml" and plan["tts.accel_device"] == ""
    assert plan["asr.device"] == "cpu"


def test_accel_plan_eco_with_igpu():
    """节能 · 有核显 → tts=dml + igpu；其余组件 cpu。"""
    hw = {"gpu_vendor": "nvidia", "vram_gb": 8.0, "has_igpu": True,
          "dgpu_vendor": "nvidia", "recommend": "cuda"}

    plan = hp.accel_plan(hw, "eco")

    assert plan["accel.mode"] == "eco"
    assert plan["tts.accel"] == "dml"
    assert plan["tts.accel_device"] == "igpu"
    assert plan["asr.device"] == "cpu"
    assert plan["local_llm.device"] == "cpu"
    assert plan["embedding.device"] == "cpu"


def test_accel_plan_eco_without_igpu():
    """节能 · 无核显 → tts=cpu/""；其余组件 cpu。"""
    hw = {"gpu_vendor": "nvidia", "vram_gb": 8.0, "has_igpu": False, "dgpu_vendor": "nvidia"}

    plan = hp.accel_plan(hw, "eco")

    assert plan["tts.accel"] == "cpu" and plan["tts.accel_device"] == ""
    assert plan["asr.device"] == "cpu" and plan["local_llm.device"] == "cpu"


def test_accel_plan_invalid_input_degrades_without_raise():
    """异常降级：非法模式归一 performance；非 dict / 异常值不抛错，落点保守 cpu。"""
    plan = hp.accel_plan(None, "turbo")
    assert plan["accel.mode"] == "performance"
    assert plan["tts.accel"] == "cpu" and plan["tts.accel_device"] == ""
    assert plan["asr.device"] == "cpu" and plan["local_llm.device"] == "cpu"

    plan2 = hp.accel_plan(
        {"gpu_vendor": "nvidia", "vram_gb": "bad", "has_igpu": False}, "performance"
    )
    assert plan2["local_llm.device"] == "cpu"  # 异常显存值保守降级


# ------------------------------------------------------------------ #
# accel_plan：backend 决策与 Linux ROCm 面（20261002 批 A）            #
# ------------------------------------------------------------------ #

def test_accel_plan_backend_cuda_for_nvidia():
    """性能 · N 卡 → device=gpu + backend=cuda（Windows 与 Linux 同口径）。"""
    hw = {"gpu_vendor": "nvidia", "vram_gb": 8.0, "has_igpu": False,
          "dgpu_vendor": "nvidia", "recommend": "cuda"}
    for platform in (None, "win32", "linux"):
        plan = hp.accel_plan(hw, "performance", platform=platform)
        assert plan["local_llm.device"] == "gpu"
        assert plan["local_llm.backend"] == "cuda"
        assert plan["embedding.device"] == "gpu"
        assert plan["embedding.backend"] == "cuda"


def test_accel_plan_backend_vulkan_for_amd_dgpu():
    """性能 · 无核显 AMD 独显（Windows）→ device=gpu + backend=vulkan。"""
    hw = {"gpu_vendor": "amd", "has_igpu": False, "dgpu_vendor": "amd",
          "recommend": "rocm"}
    plan = hp.accel_plan(hw, "performance")

    assert plan["local_llm.device"] == "gpu"
    assert plan["local_llm.backend"] == "vulkan"
    assert plan["embedding.device"] == "gpu"
    assert plan["embedding.backend"] == "vulkan"
    # Windows 上 TTS/ASR 落点不变（批 A 仅 LLM/嵌入扩展 vulkan 路径）
    assert plan["tts.accel"] == "dml"
    assert plan["asr.device"] == "cpu"


def test_accel_plan_backend_vulkan_for_igpu_only():
    """性能 · 仅核显（无独显）→ device=gpu + backend=vulkan（核显不设显存门槛）。"""
    hw = {"gpu_vendor": "cpu", "has_igpu": True, "dgpu_vendor": None,
          "recommend": "cpu"}
    plan = hp.accel_plan(hw, "performance")

    assert plan["local_llm.device"] == "gpu"
    assert plan["local_llm.backend"] == "vulkan"
    assert plan["embedding.backend"] == "vulkan"
    # TTS 仍走核显 dml（既有双模式前提不变）
    assert plan["tts.accel"] == "dml" and plan["tts.accel_device"] == "dgpu"


def test_accel_plan_backend_empty_for_eco_and_no_gpu():
    """节能模式与无 GPU 性能模式 → device=cpu + backend=""（既有行为不变）。"""
    nvidia = {"gpu_vendor": "nvidia", "vram_gb": 8.0, "has_igpu": False,
              "dgpu_vendor": "nvidia", "recommend": "cuda"}
    plan_eco = hp.accel_plan(nvidia, "eco")
    assert plan_eco["local_llm.device"] == "cpu"
    assert plan_eco["local_llm.backend"] == ""
    assert plan_eco["embedding.backend"] == ""

    nogpu = {"gpu_vendor": "cpu", "has_igpu": False, "recommend": "cpu"}
    plan_cpu = hp.accel_plan(nogpu, "performance")
    assert plan_cpu["local_llm.device"] == "cpu"
    assert plan_cpu["local_llm.backend"] == ""


def test_accel_plan_linux_amd_dgpu_rocm_face():
    """Linux · 性能 + AMD 独显 → tts=rocm、asr=gpu；LLM/嵌入维持 cpu（ROCm/HIP 未纳入）。"""
    hw = {"gpu_vendor": "amd", "has_igpu": False, "dgpu_vendor": "amd",
          "recommend": "rocm"}
    plan = hp.accel_plan(hw, "performance", platform="linux")

    assert plan["tts.accel"] == "rocm"
    assert plan["tts.accel_device"] == ""
    assert plan["asr.device"] == "gpu"
    assert plan["local_llm.device"] == "cpu"
    assert plan["local_llm.backend"] == ""
    assert plan["embedding.device"] == "cpu"
    assert plan["embedding.backend"] == ""
    # 仅 linux（startswith）前缀命中，"linux515" 等变体同口径
    plan2 = hp.accel_plan(hw, "performance", platform="linux515")
    assert plan2["tts.accel"] == "rocm" and plan2["asr.device"] == "gpu"


def test_accel_plan_linux_intel_dgpu_keeps_cpu_for_llm():
    """Linux · 性能 + Intel 独显 → LLM/嵌入维持 cpu（ROCm/HIP 构建未纳入的例外面）。"""
    hw = {"gpu_vendor": "intel", "has_igpu": False, "dgpu_vendor": "intel",
          "recommend": "cpu"}
    plan = hp.accel_plan(hw, "performance", platform="linux")

    assert plan["local_llm.device"] == "cpu"
    assert plan["local_llm.backend"] == ""


def test_accel_plan_windows_explicit_platform_matches_default():
    """platform 显式 "win32" 与缺省（当前平台）逐字节等价（Windows 回归不变）。"""
    hw = {"gpu_vendor": "amd", "has_igpu": True, "dgpu_vendor": "amd",
          "recommend": "rocm"}
    assert hp.accel_plan(hw, "performance") == hp.accel_plan(
        hw, "performance", platform="win32"
    )


# ---- AMD 核显优先（20261002 用户裁决：防独显被游戏占用）---- #


def test_accel_plan_amd_igpu_prefers_igpu_on_windows_even_performance():
    """Windows · 性能 + N 卡独显 + AMD 核显 → TTS 指向核显（dml+igpu）；
    ASR / LLM 仍按 N 卡（CUDA）不受影响；节能模式同口径（原本就是 igpu）。"""
    hw = {
        "gpu_vendor": "nvidia", "vram_gb": 8.0, "has_igpu": True,
        "dgpu_vendor": "nvidia", "recommend": "cuda",
        "gpus": [
            {"vendor": "nvidia", "type": "dgpu"},
            {"vendor": "amd", "type": "igpu"},
        ],
    }
    perf = hp.accel_plan(hw, "performance")
    assert perf["tts.accel"] == "dml"
    assert perf["tts.accel_device"] == "igpu"
    assert perf["asr.device"] == "gpu"
    assert perf["local_llm.device"] == "gpu"
    assert perf["local_llm.backend"] == "cuda"

    eco = hp.accel_plan(hw, "eco")
    assert eco["tts.accel"] == "dml" and eco["tts.accel_device"] == "igpu"


def test_accel_plan_amd_igpu_only_windows():
    """Windows · 仅 AMD 核显（无独显）→ 性能/节能均 TTS 指向核显（dml+igpu）。"""
    hw = {
        "gpu_vendor": "amd", "has_igpu": True, "dgpu_vendor": None,
        "recommend": "cpu",
        "gpus": [{"vendor": "amd", "type": "igpu"}],
    }
    for mode in ("performance", "eco"):
        plan = hp.accel_plan(hw, mode)
        assert plan["tts.accel"] == "dml"
        assert plan["tts.accel_device"] == "igpu"


def test_accel_plan_amd_igpu_linux_rocm():
    """Linux · AMD 核显（含 N 卡独显组合）→ TTS 走 ROCm（性能/节能一致）。"""
    hw = {
        "gpu_vendor": "nvidia", "vram_gb": 8.0, "has_igpu": True,
        "dgpu_vendor": "nvidia", "recommend": "cuda",
        "gpus": [{"vendor": "amd", "type": "igpu"},
                 {"vendor": "nvidia", "type": "dgpu"}],
    }
    for mode in ("performance", "eco"):
        plan = hp.accel_plan(hw, mode, platform="linux")
        assert plan["tts.accel"] == "rocm"
        assert plan["tts.accel_device"] == ""


def test_accel_plan_intel_igpu_keeps_existing_branch():
    """Intel 核显 / 清单缺失（legacy hw dict）→ 保守回退既有分支（性能=dml+dgpu）。"""
    hw_intel = {
        "gpu_vendor": "nvidia", "vram_gb": 8.0, "has_igpu": True,
        "dgpu_vendor": "nvidia", "recommend": "cuda",
        "gpus": [{"vendor": "intel", "type": "igpu"},
                 {"vendor": "nvidia", "type": "dgpu"}],
    }
    plan = hp.accel_plan(hw_intel, "performance")
    assert plan["tts.accel"] == "dml" and plan["tts.accel_device"] == "dgpu"

    hw_legacy = {"gpu_vendor": "nvidia", "vram_gb": 8.0, "has_igpu": True,
                 "dgpu_vendor": "nvidia", "recommend": "cuda"}  # 无 gpus 清单
    plan_legacy = hp.accel_plan(hw_legacy, "performance")
    assert plan_legacy["tts.accel"] == "dml" and plan_legacy["tts.accel_device"] == "dgpu"


def test_build_config_patch_backend_semantics():
    """config_patch backend 语义：GPU 档并入；走云/降级（device=cpu）不带 backend。"""
    plan_gpu = {
        "accel.mode": "performance", "tts.accel": "dml", "tts.accel_device": "",
        "asr.device": "cpu", "local_llm.device": "gpu", "local_llm.backend": "vulkan",
        "embedding.device": "gpu", "embedding.backend": "vulkan", "reasons": [],
    }
    patch = hp._build_config_patch(
        use_local=True, device="gpu", plan=plan_gpu, embedding_gpu=True
    )
    assert patch["local_llm"]["backend"] == "vulkan"
    assert patch["embedding"] == {"device": "gpu", "backend": "vulkan"}

    # 走云：device 强制 cpu → 不带 backend（避免 device=cpu + backend=vulkan 语义矛盾）
    patch_cloud = hp._build_config_patch(
        use_local=False, device="cpu", plan=plan_gpu, embedding_gpu=False
    )
    assert "backend" not in patch_cloud["local_llm"]
    assert "embedding" not in patch_cloud

    # 老口径 plan（无 backend 键，向后兼容）→ patch 不出现 backend
    plan_legacy = {k: v for k, v in plan_gpu.items() if "backend" not in k}
    patch_legacy = hp._build_config_patch(
        use_local=True, device="gpu", plan=plan_legacy, embedding_gpu=True
    )
    assert "backend" not in patch_legacy["local_llm"]
    assert patch_legacy["embedding"] == {"device": "gpu"}


def test_normalize_mode_and_derive_default_mode():
    """模式归一与画像推导默认模式。"""
    assert hp.normalize_mode("ECO") == "eco"
    assert hp.normalize_mode("  performance ") == "performance"
    assert hp.normalize_mode("") == "performance"

    assert hp.derive_default_mode({"dgpu_vendor": "nvidia"}) == "performance"
    assert hp.derive_default_mode({"gpu_vendor": "nvidia"}) == "performance"
    assert hp.derive_default_mode({"has_igpu": True}) == "eco"
    assert hp.derive_default_mode({"gpu_vendor": "cpu"}) == "eco"


# ------------------------------------------------------------------ #
# recommend_for：加速剖面与委托一致性                                 #
# ------------------------------------------------------------------ #

def test_recommend_for_delegates_device_to_accel_plan(monkeypatch):
    """recommend_for 的 device / config_patch 落点与 accel_plan 一致（单一真相源）。"""
    _inject_tiers(monkeypatch)
    profile = _profile(ram_gb=32.0, gpu_vendor="nvidia", vram_gb=8.0,
                       has_igpu=False, dgpu_vendor="nvidia", cuda_version="12.4")

    result = hp.recommend_for(profile)
    plan = hp.accel_plan(profile, hp.derive_default_mode(profile))

    assert result["accel"]["mode"] == "performance"
    assert result["device"] == plan["local_llm.device"] == "gpu"
    assert result["accel"]["local_llm"]["device"] == plan["local_llm.device"]
    assert result["config_patch"]["accel"]["mode"] == "performance"
    assert result["config_patch"]["tts"]["accel"] == plan["tts.accel"] == "cuda"
    assert result["config_patch"]["tts"]["accel_device"] == ""
    assert result["config_patch"]["asr"]["device"] == plan["asr.device"] == "gpu"
    # 20261002 批 A：embedding patch 并入 backend=cuda（既有断言变更留痕）
    assert result["config_patch"]["embedding"] == {"device": "gpu", "backend": "cuda"}
    assert result["accel"]["reasons"]  # 中文理由非空


def test_recommend_for_eco_mode_when_igpu_only(monkeypatch):
    """仅核显画像 → 默认 eco；tts=dml/igpu；本地 LLM 仍 cpu。"""
    _inject_tiers(monkeypatch)
    profile = _profile(ram_gb=16.0, gpu_vendor="cpu", has_igpu=True, dgpu_vendor=None)

    result = hp.recommend_for(profile)

    assert result["accel"]["mode"] == "eco"
    assert result["config_patch"]["tts"]["accel"] == "dml"
    assert result["config_patch"]["tts"]["accel_device"] == "igpu"
    assert result["device"] == "cpu"
    assert "embedding" not in result["config_patch"]
