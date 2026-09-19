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

import sys
import types

from lite.runtime import hardware_profile as hp

#: 档位替身表（含 repo / filename / 预估体积），仅用于测试注入。
_FAKE_TIERS = {
    "0.5B": {
        "repo": "Qwen/Qwen2.5-0.5B-Instruct-GGUF",
        "filename": "qwen2.5-0.5b-instruct-q4_k_m.gguf",
        "approximate_size_gb": 0.4,
        "family": "Qwen2.5-0.5B",
        "quant": "Q4_K_M",
    },
    "1.7B": {
        "repo": "Qwen/Qwen1.5-1.8B-Chat-GGUF",
        "filename": "qwen1_5-1_8b-chat-q4_k_m.gguf",
        "approximate_size_gb": 1.7,
        "family": "Qwen1.5-1.8B",
        "quant": "Q4_K_M",
    },
    "4B": {
        "repo": "Qwen/Qwen3-4B-GGUF",
        "filename": "qwen3-4b-q4_k_m.gguf",
        "approximate_size_gb": 2.7,
        "family": "Qwen3-4B",
        "quant": "Q4_K_M",
    },
    "8B": {
        "repo": "Qwen/Qwen3-8B-GGUF",
        "filename": "Qwen3-8B-Q4_K_M.gguf",
        "approximate_size_gb": 4.682,
        "family": "Qwen3-8B",
        "quant": "Q4_K_M",
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
    assert result["tier"] == "0.5B"
    assert result["config_patch"]["local_llm"] == {"enabled": False, "device": "cpu"}
    assert result["model"] is None
    assert any("内存" in reason and "云端" in reason for reason in result["reasons"])


def test_recommend_8gb_memory_cpu_1_7b(monkeypatch):
    """内存 8GB → 本机 cpu + 1.7B 档，model 含 repo / 文件名 / 预估体积。"""
    _inject_tiers(monkeypatch)
    profile = _profile(ram_gb=8.0)

    result = hp.recommend_for(profile)

    assert result["use_local"] is True
    assert result["device"] == "cpu"
    assert result["tier"] == "1.7B"
    assert result["config_patch"]["local_llm"] == {"enabled": True, "device": "cpu"}
    assert "embedding" not in result["config_patch"]
    assert result["model"]["repo"]
    assert result["model"]["filename"]
    assert result["model"]["approximate_size_gb"] == 1.7


def test_recommend_nvidia_10gb_vram_gpu_8b(monkeypatch):
    """nvidia 10GB 显存 + 内存充足 → device=gpu、tier=8B、embedding 同步走 gpu。"""
    _inject_tiers(monkeypatch)
    profile = _profile(ram_gb=32.0, gpu_vendor="nvidia", vram_gb=10.0, cuda_version="12.4")

    result = hp.recommend_for(profile)

    assert result["use_local"] is True
    assert result["device"] == "gpu"
    assert result["tier"] == "8B"
    assert result["config_patch"]["local_llm"] == {"enabled": True, "device": "gpu"}
    assert result["config_patch"]["embedding"] == {"device": "gpu"}
    assert any("显存" in reason for reason in result["reasons"])


def test_recommend_nvidia_6gb_vram_gpu_4b(monkeypatch):
    """nvidia 6GB 显存 → device=gpu、tier=4B。"""
    _inject_tiers(monkeypatch)
    profile = _profile(ram_gb=32.0, gpu_vendor="nvidia", vram_gb=6.0)

    result = hp.recommend_for(profile)

    assert result["device"] == "gpu"
    assert result["tier"] == "4B"
    assert result["model"]["approximate_size_gb"] == 2.7


def test_recommend_nvidia_high_vram_capped_by_memory(monkeypatch):
    """显存 10GB 但内存 < 16GB → 档位受内存上限约束，最高 4B。"""
    _inject_tiers(monkeypatch)
    profile = _profile(ram_gb=8.0, gpu_vendor="nvidia", vram_gb=10.0)

    result = hp.recommend_for(profile)

    assert result["device"] == "gpu"
    assert result["tier"] == "4B"
    assert any("内存" in reason and "4B" in reason for reason in result["reasons"])


def test_recommend_vram_below_4gb_keeps_cpu(monkeypatch):
    """显存 < 4GB → 维持 cpu + 1.7B，不上 GPU。"""
    _inject_tiers(monkeypatch)
    profile = _profile(ram_gb=32.0, gpu_vendor="nvidia", vram_gb=2.0)

    result = hp.recommend_for(profile)

    assert result["device"] == "cpu"
    assert result["tier"] == "1.7B"
    assert result["config_patch"]["local_llm"] == {"enabled": True, "device": "cpu"}
    assert "embedding" not in result["config_patch"]


def test_recommend_disk_insufficient_downgrades_tier(monkeypatch):
    """磁盘 3.0GB 放不下 8B（4.682×1.05）→ 逐级降档到 4B。"""
    _inject_tiers(monkeypatch)
    profile = _profile(ram_gb=32.0, gpu_vendor="nvidia", vram_gb=10.0)

    result = hp.recommend_for(profile, disk_free_gb=3.0)

    assert result["use_local"] is True
    assert result["tier"] == "4B"
    assert any("磁盘" in reason and "4B" in reason for reason in result["reasons"])


def test_recommend_disk_insufficient_even_for_smallest_goes_cloud(monkeypatch):
    """连最小档 0.5B（0.4×1.05）都不足 → use_local=False，理由含所需/可用对比。"""
    _inject_tiers(monkeypatch)
    profile = _profile(ram_gb=32.0, gpu_vendor="nvidia", vram_gb=10.0)

    result = hp.recommend_for(profile, disk_free_gb=0.1)

    assert result["use_local"] is False
    assert result["tier"] == "0.5B"
    assert result["config_patch"]["local_llm"]["enabled"] is False
    assert any(
        "所需" in reason and "可用" in reason and "磁盘" in reason
        for reason in result["reasons"]
    )


def test_recommend_disk_param_overrides_profile_field(monkeypatch):
    """disk_free_gb 参数优先于画像字段。"""
    _inject_tiers(monkeypatch)
    profile = _profile(ram_gb=32.0, gpu_vendor="nvidia", vram_gb=10.0, disk_free_gb=100.0)

    result = hp.recommend_for(profile, disk_free_gb=3.0)

    assert result["tier"] == "4B"


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
