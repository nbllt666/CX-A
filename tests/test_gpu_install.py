# -*- coding: utf-8 -*-
"""Task H4 GPU 依赖装配与安装单元测试（pytest，tmp_path + mock runner）。

覆盖：
- build_gpu_dependency_commands 三分支清单（cuda / rocm / cpu）
- install_gpu_dependencies：引导式报告落盘字段齐全
- auto_install=True：mock runner 失败不抛错、installed=False、errors 留痕；
  全成功则 installed=True 且注释条目不执行
- install() 一键编排末尾追加 GPU 报告步骤（既有行为不变）
"""

import json
import os

import pytest

from installer import bootstrap
from installer.gpu_detect import detect_via_runner

#: 与 test_gpu_detect.py 同源的 nvidia-smi 成功输出（CUDA Version: 12.4）。
_NVIDIA_SMI_OK = (
    "NVIDIA-SMI 551.61       Driver Version: 551.61       CUDA Version: 12.4\n"
)


def _runner_map(responses, calls=None):
    """构造按命令关键字分派的 mock runner；calls 非空时记录全部调用命令。"""
    def runner(cmd):
        if calls is not None:
            calls.append(cmd)
        for keyword, (rc, stdout) in responses.items():
            if keyword in cmd:
                return rc, stdout
        return -1, ""
    return runner


# ------------------------------------------------------------------ #
# build_gpu_dependency_commands：三分支清单                            #
# ------------------------------------------------------------------ #

def test_build_commands_cuda_branch():
    """cuda 分支：仅 cu128 torch / llama-cpp 链——ORT 包已解耦，不再随 cuda 分支捆绑。"""
    commands = bootstrap.build_gpu_dependency_commands("cuda", "12.4")

    joined = "\n".join(commands)
    assert "pip install torch --index-url https://download.pytorch.org/whl/cu128" in joined
    assert (
        "pip install llama-cpp-python --extra-index-url "
        "https://abetlen.github.io/llama-cpp-python/whl/cu128 --force-reinstall --no-cache-dir"
    ) in commands
    # 解耦（Task 3）：cuda 分支不再捆绑任何 ORT 变体（改由画像分叉装配）
    executable = [c for c in commands if not c.startswith("#")]
    assert not any(c.startswith("pip install onnxruntime") for c in executable)
    # 版本号写入注释字段而非硬编码进命令
    assert "12.4" in joined and "12.4" not in [
        c for c in commands if c.startswith("pip")
    ]


def test_build_commands_rocm_branch():
    """rocm 分支（Linux）：torch ROCm index（ORT 已解耦）+ llama.cpp Vulkan 构建说明。"""
    commands = bootstrap.build_gpu_dependency_commands("rocm", None, platform="linux")

    assert "pip install torch --index-url https://download.pytorch.org/whl/rocm6.0" in commands
    executable = [c for c in commands if not c.startswith("#")]
    assert not any(c.startswith("pip install onnxruntime") for c in executable)
    # llama.cpp Vulkan 构建说明（说明性注释条目）
    assert any(c.startswith("#") and "Vulkan" in c for c in commands)


def test_build_commands_rocm_branch_windows_falls_back_cpu(tmp_path=None):
    """rocm 分支（Windows/缺省）：无 PyTorch ROCm 轮子 → torch CPU 轮子 + 中文注释
    （避免必失败的 rocm6.0 安装命令阻断安装链；20261002 用户问询发现）。"""
    commands = bootstrap.build_gpu_dependency_commands("rocm", None)

    assert "pip install torch --index-url https://download.pytorch.org/whl/cpu" in commands
    assert "pip install torchaudio --index-url https://download.pytorch.org/whl/cpu" in commands
    joined = "\n".join(commands)
    assert "rocm6.0" not in joined
    assert "Windows" in joined  # 中文注释说明回退原因
    assert any(c.startswith("#") and "Vulkan" in c for c in commands)


def test_build_commands_rocm_branch_linux_via_sys_platform_monkeypatch(monkeypatch):
    """platform 缺省取 sys.platform：monkeypatch 成 linux 同样产出 ROCm 轮子。"""
    monkeypatch.setattr(bootstrap.sys, "platform", "linux")

    commands = bootstrap.build_gpu_dependency_commands("rocm", None)

    assert "pip install torch --index-url https://download.pytorch.org/whl/rocm6.0" in commands


def test_build_commands_cpu_branch():
    """cpu 分支：仅 CPU 版 torch 链（ORT 解耦）；显式画像时装配 CPU 版 ORT。"""
    commands = bootstrap.build_gpu_dependency_commands("cpu", None)

    assert "pip install torch --index-url https://download.pytorch.org/whl/cpu" in commands
    executable = [c for c in commands if not c.startswith("#")]
    assert not any(c.startswith("pip install onnxruntime") for c in executable)
    joined = "\n".join(commands)
    assert "onnxruntime-gpu" not in joined
    assert "cu128" not in joined and "cu121" not in joined

    # 显式画像（无核显、无 GPU）→ 装配 CPU 版 ORT
    with_profile = bootstrap.build_gpu_dependency_commands("cpu", None, has_igpu=False)
    assert "pip install onnxruntime" in with_profile


# ------------------------------------------------------------------ #
# ORT 包分叉：四类机器（画像驱动，互斥）                                #
# ------------------------------------------------------------------ #

#: (标签, 画像, 期望 ORT 变体)——四类机器分叉唯一真相源断言表。
_ORT_FORK_CASES = [
    ("有核显（任意独显组合）",
     {"has_igpu": True, "recommend": "cuda", "dgpu_vendor": "nvidia"},
     "onnxruntime-directml"),
    ("无核显 NVIDIA",
     {"has_igpu": False, "recommend": "cuda", "gpu_vendor": "nvidia"},
     "onnxruntime-gpu"),
    ("无核显 AMD（Windows）",
     {"has_igpu": False, "recommend": "rocm", "gpu_vendor": "amd"},
     "onnxruntime-directml"),
    ("无 GPU",
     {"has_igpu": False, "recommend": "cpu", "gpu_vendor": "cpu"},
     "onnxruntime"),
]


@pytest.mark.parametrize("label,profile,expected", _ORT_FORK_CASES)
def test_ort_fork_four_machine_classes(label, profile, expected):
    """四类机器的 ORT 包分叉正确，且命令清单**互斥**（只含一个 ORT 变体）。"""
    assert bootstrap.build_ort_package_command(profile) == f"pip install {expected}"

    commands = bootstrap.build_gpu_dependency_commands(
        profile["recommend"], "12.4", profile=profile
    )
    variants = [c for c in commands if c.startswith("pip install onnxruntime")]
    assert variants == [f"pip install {expected}"]  # 互斥：唯一变体且为期望项


# ------------------------------------------------------------------ #
# Linux ROCm 面（20261002 批 A；代码路径 + 单测覆盖，Linux 真机未验证）  #
# ------------------------------------------------------------------ #

#: (标签, 画像)——Linux + AMD/ROCm 画像 → onnxruntime-rocm。
_LINUX_ROCM_CASES = [
    ("recommend=rocm 无核显",
     {"has_igpu": False, "recommend": "rocm", "gpu_vendor": "amd"}),
    ("dgpu=amd 无 recommend",
     {"has_igpu": False, "recommend": "cpu", "gpu_vendor": "cpu",
      "dgpu_vendor": "amd"}),
    ("vendor=amd has_igpu",
     {"has_igpu": True, "recommend": "rocm", "gpu_vendor": "amd"}),
    ("仅 AMD 核显（gpus 清单）",
     {"has_igpu": True, "recommend": "cpu", "gpu_vendor": "amd",
      "gpus": [{"vendor": "amd", "type": "igpu"}]}),
]


@pytest.mark.parametrize("label,profile", _LINUX_ROCM_CASES)
def test_ort_fork_linux_rocm_package(label, profile):
    """Linux + AMD/ROCm 画像 → onnxruntime-rocm（platform 参数注入驱动）。"""
    for platform in ("linux", "linux515"):
        assert bootstrap.resolve_ort_package(profile, platform=platform) == (
            bootstrap.ORT_PACKAGE_ROCM
        )
        assert bootstrap.build_ort_package_command(
            profile, platform=platform
        ) == f"pip install {bootstrap.ORT_PACKAGE_ROCM}"


def test_ort_fork_linux_rocm_via_sys_platform_monkeypatch(monkeypatch):
    """platform 缺省取 sys.platform：monkeypatch 平台同样驱动 rocm 分叉。"""
    import sys as _sys

    profile = {"has_igpu": False, "recommend": "rocm", "gpu_vendor": "amd"}
    monkeypatch.setattr(_sys, "platform", "linux")
    assert bootstrap.resolve_ort_package(profile) == bootstrap.ORT_PACKAGE_ROCM

    monkeypatch.setattr(_sys, "platform", "win32")
    assert bootstrap.resolve_ort_package(profile) == bootstrap.ORT_PACKAGE_DIRECTML


def test_ort_fork_linux_non_amd_keeps_windows_matrix():
    """Linux 非 AMD 画像不产 rocm：N 卡仍 gpu、无 GPU 仍 cpu（例外面最小化）。"""
    nvidia = {"has_igpu": False, "recommend": "cuda", "gpu_vendor": "nvidia"}
    assert bootstrap.resolve_ort_package(nvidia, platform="linux") == (
        bootstrap.ORT_PACKAGE_GPU
    )
    nogpu = {"has_igpu": False, "recommend": "cpu", "gpu_vendor": "cpu"}
    assert bootstrap.resolve_ort_package(nogpu, platform="linux") == (
        bootstrap.ORT_PACKAGE_CPU
    )


def test_ort_fork_windows_explicit_platform_unchanged():
    """Windows 显式 platform="win32" 与缺省逐字节一致（既有矩阵回归不变）。"""
    for _label, profile, expected in _ORT_FORK_CASES:
        assert bootstrap.resolve_ort_package(profile, platform="win32") == expected


def test_ort_fork_without_profile_keeps_legacy_no_ort():
    """未提供画像（旧调用口径）：三分支仅 torch 链，不装配 ORT（保持最简旧语义）。"""
    for recommend in ("cuda", "rocm", "cpu"):
        commands = bootstrap.build_gpu_dependency_commands(recommend, "12.4")
        assert not any(c.startswith("pip install onnxruntime") for c in commands)


def test_ort_fork_mirror_appends_index_only():
    """镜像通道：ORT 分叉命令追加国内索引；None / official 一字不改。"""
    profile = {"has_igpu": False, "recommend": "cuda"}
    mirror_index = "https://pypi.tuna.tsinghua.edu.cn/simple"
    assert bootstrap.build_ort_package_command(profile) == "pip install onnxruntime-gpu"
    assert bootstrap.build_ort_package_command(profile, "official") == "pip install onnxruntime-gpu"
    assert (
        bootstrap.build_ort_package_command(profile, "mirror")
        == f"pip install onnxruntime-gpu -i {mirror_index}"
    )
    # 命令清单中的 ORT 行同样被镜像改写（互斥仍成立）
    commands = bootstrap.build_gpu_dependency_commands(
        "cuda", "12.4", channel="mirror", profile=profile
    )
    assert f"pip install onnxruntime-gpu -i {mirror_index}" in commands


def test_install_gpu_dependencies_profile_drives_ort_fork(tmp_path):
    """引导报告按注入画像装配 ORT 分叉（有核显 → DirectML），清单互斥。"""
    report_path = os.path.join(str(tmp_path), "data", "install_report.json")
    runner = _runner_map({"nvidia-smi": (0, _NVIDIA_SMI_OK)})

    result = bootstrap.install_gpu_dependencies(
        report_path=report_path, runner=runner,
        profile={"has_igpu": True, "recommend": "cuda", "dgpu_vendor": "nvidia"},
    )

    assert result["has_igpu"] is True
    assert result["ort_package"] == "onnxruntime-directml"
    variants = [c for c in result["pending_commands"] if c.startswith("pip install onnxruntime")]
    assert variants == ["pip install onnxruntime-directml"]
    assert not any("onnxruntime-gpu" in c for c in result["pending_commands"])


# ------------------------------------------------------------------ #
# install_gpu_dependencies：引导式报告                                 #
# ------------------------------------------------------------------ #

def test_install_gpu_dependencies_guided_report(tmp_path):
    """引导式（默认）：报告字段齐全落盘，不做真实安装（installed=False）。"""
    report_path = os.path.join(str(tmp_path), "data", "install_report.json")
    runner = _runner_map({"nvidia-smi": (0, _NVIDIA_SMI_OK)})

    result = bootstrap.install_gpu_dependencies(report_path=report_path, runner=runner)

    # 返回值字段齐全
    assert result["gpu_vendor"] == "nvidia"
    assert result["cuda_version"] == "12.4"
    assert result["recommend"] == "cuda"
    assert result["installed"] is False
    assert result["timestamp"]
    assert len(result["pending_commands"]) == 5  # 注释 + 4 条 pip 命令（含 torchaudio）

    # 报告落盘且与返回值一致
    assert os.path.isfile(report_path)
    with open(report_path, encoding="utf-8") as fh:
        on_disk = json.load(fh)
    assert on_disk == result


def test_install_gpu_dependencies_cpu_report_default_path(tmp_path, monkeypatch):
    """runner 未注入且全探测失败（mock gpu_detect.default_runner）→ cpu 报告落到项目根 data/。"""
    cpu_report = {
        "gpu_vendor": "cpu", "cuda_version": None, "recommend": "cpu",
        "details": "未检测到可用的独立 GPU",
    }
    monkeypatch.setattr(
        bootstrap, "GpuDetector",
        lambda runner=None: type("D", (), {"detect": lambda self, runner=None: dict(cpu_report)})(),
    )
    report_path = os.path.join(str(tmp_path), "install_report.json")

    result = bootstrap.install_gpu_dependencies(report_path=report_path)

    assert result["gpu_vendor"] == "cpu"
    assert result["recommend"] == "cpu"
    assert result["pending_commands"] == [
        "pip install torch --index-url https://download.pytorch.org/whl/cpu",
        "pip install torchaudio --index-url https://download.pytorch.org/whl/cpu",
        "pip install onnxruntime",
    ]
    assert os.path.isfile(report_path)


# ------------------------------------------------------------------ #
# install_gpu_dependencies：auto_install=True                          #
# ------------------------------------------------------------------ #

def test_install_gpu_dependencies_auto_install_failure_no_raise(tmp_path):
    """auto_install=True + 安装命令失败：不抛错、installed=False、errors 留痕。"""
    report_path = os.path.join(str(tmp_path), "data", "install_report.json")
    calls = []

    def runner(cmd):
        """检测通道放行 nvidia-smi，安装通道一律失败。"""
        calls.append(cmd)
        if "nvidia-smi" in cmd:
            return 0, _NVIDIA_SMI_OK
        return 1, "ERROR: simulated pip failure"

    result = bootstrap.install_gpu_dependencies(
        report_path=report_path, auto_install=True, runner=runner
    )

    # 不抛错即达成本断言；结论字段校验
    assert result["gpu_vendor"] == "nvidia"
    assert result["installed"] is False
    # 4 条 pip 命令全部失败并留痕（注释条目不计入；含 torchaudio，2026-09-25 新增）
    assert len(result["errors"]) == 4
    assert all(err["returncode"] == 1 for err in result["errors"])
    assert all(err["command"].startswith("pip install") for err in result["errors"])
    assert all("simulated pip failure" in err["output"] for err in result["errors"])
    # 检测调用 1 次 + 安装调用 4 次（注释条目被跳过）
    assert len([c for c in calls if c.startswith("pip install")]) == 4
    assert not any(c.startswith("#") for c in calls)


def test_install_gpu_dependencies_auto_install_success(tmp_path):
    """auto_install=True + 全部命令成功：installed=True、errors 缺省为空列表。"""
    report_path = os.path.join(str(tmp_path), "data", "install_report.json")
    executed = []

    def runner(cmd):
        if "nvidia-smi" in cmd:
            return 0, _NVIDIA_SMI_OK
        executed.append(cmd)
        return 0, "Successfully installed"

    result = bootstrap.install_gpu_dependencies(
        report_path=report_path, auto_install=True, runner=runner
    )

    assert result["installed"] is True
    assert result["errors"] == []
    assert len(executed) == 4  # 含 torchaudio（2026-09-25 新增）


def test_install_gpu_dependencies_auto_install_rocm_skips_notes(tmp_path):
    """rocm 分支自动安装：可执行命令全部执行，# 说明条目仅打印不执行。"""
    report_path = os.path.join(str(tmp_path), "data", "install_report.json")
    executed = []
    detect_report = {
        "gpu_vendor": "amd", "cuda_version": None, "recommend": "rocm",
        "details": "检测到 AMD GPU",
    }

    def runner(cmd):
        # 借用纯函数式检测逻辑：探测命令走真实枚举 mock，安装命令一律成功
        if any(k in cmd for k in ("nvidia-smi", "win32_VideoController", "enum-devices")):
            probe_runner = _runner_map({"win32_VideoController": (0, "AMD Radeon RX 7900 XT\n")})
            return probe_runner(cmd)
        executed.append(cmd)
        return 0, "Successfully installed"

    result = bootstrap.install_gpu_dependencies(
        report_path=report_path, auto_install=True, runner=runner
    )

    assert result["recommend"] == "rocm"
    assert result["installed"] is True
    # rocm 清单可执行命令（torch rocm / torchaudio rocm / onnxruntime-directml）
    # 全部执行；说明性注释被跳过
    executable = [c for c in result["pending_commands"] if not c.startswith("#")]
    assert len(executable) == 3
    assert set(executed) == set(executable)


# ------------------------------------------------------------------ #
# install() 一键编排追加 GPU 报告步骤（既有行为不变）                    #
# ------------------------------------------------------------------ #

def test_install_orchestration_writes_gpu_report(tmp_path, monkeypatch):
    """bootstrap.install(tmp_root) 后 tmp_root/data/install_report.json 存在且为 cpu/cuda 结论之一。"""
    from installer import gpu_detect as gpu_detect_mod

    root = str(tmp_path / "orchroot")
    os.makedirs(root)

    # 注入恒定 cpu 结论的检测 runner，避免依赖宿主机真实硬件
    monkeypatch.setattr(
        gpu_detect_mod, "default_runner", _runner_map({})
    )

    problems, builtin_warnings = bootstrap.install(root)

    # 既有 install() 契约不变：返回二元组
    assert isinstance(problems, list)
    assert isinstance(builtin_warnings, list)

    # 追加步骤生效：GPU 报告随安装根落盘
    report_path = os.path.join(root, "data", "install_report.json")
    assert os.path.isfile(report_path)
    with open(report_path, encoding="utf-8") as fh:
        report = json.load(fh)
    assert report["recommend"] == "cpu"  # 全探测失败 → 引导式 CPU 清单
    assert report["installed"] is False
    assert report["pending_commands"][0].startswith("pip install torch")


def test_install_orchestration_gpu_step_failure_not_blocking(tmp_path, monkeypatch):
    """GPU 步骤抛异常时 install() 仍正常完成（try/except 兜底不阻断主安装）。"""
    from installer import gpu_detect as gpu_detect_mod

    root = str(tmp_path / "boomroot")
    os.makedirs(root)

    def exploding_runner(cmd):
        raise RuntimeError("simulated detect crash")

    monkeypatch.setattr(gpu_detect_mod, "default_runner", exploding_runner)

    problems, builtin_warnings = bootstrap.install(root)

    # 主安装流程正常走完（返回值结构不变，数据目录初始化成功）
    assert isinstance(problems, list)
    assert isinstance(builtin_warnings, list)
    assert os.path.isdir(os.path.join(root, "data", "lancedb"))
    # GPU 报告未产出（步骤被兜底跳过），但流程不受影响
    assert not os.path.exists(os.path.join(root, "data", "install_report.json"))


# ------------------------------------------------------------------ #
# detect_via_runner 引用自检（保证模块导出契约稳定）                     #
# ------------------------------------------------------------------ #

def test_detect_via_runner_exported():
    """bootstrap 依赖的 gpu_detect 导出面稳定：detect_via_runner 可直接调用。"""
    report = detect_via_runner(_runner_map({}))
    assert report["gpu_vendor"] == "cpu"
