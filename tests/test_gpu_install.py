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
    """cuda 分支：含 cu128 torch / onnxruntime-gpu / llama-cpp-python cu128 轮三条命令（人工裁决 2026-09-12）。"""
    commands = bootstrap.build_gpu_dependency_commands("cuda", "12.4")

    joined = "\n".join(commands)
    assert "pip install torch --index-url https://download.pytorch.org/whl/cu128" in joined
    assert "pip install onnxruntime-gpu" in commands
    assert (
        "pip install llama-cpp-python --extra-index-url "
        "https://abetlen.github.io/llama-cpp-python/whl/cu128 --force-reinstall --no-cache-dir"
    ) in commands
    # 版本号写入注释字段而非硬编码进命令
    assert "12.4" in joined and "12.4" not in [
        c for c in commands if c.startswith("pip")
    ]


def test_build_commands_rocm_branch():
    """rocm 分支：torch ROCm index + DirectML onnxruntime + llama.cpp Vulkan 构建说明。"""
    commands = bootstrap.build_gpu_dependency_commands("rocm", None)

    joined = "\n".join(commands)
    assert "pip install torch --index-url https://download.pytorch.org/whl/rocm6.0" in commands
    assert "pip install onnxruntime-directml" in commands
    # 仅约束可执行命令条目（# 说明性注释允许提及 onnxruntime-gpu 作为对比说明）
    executable = [c for c in commands if not c.startswith("#")]
    assert all("onnxruntime-gpu" not in c for c in executable)
    # llama.cpp Vulkan 构建说明（说明性注释条目）
    assert any(c.startswith("#") and "Vulkan" in c for c in commands)


def test_build_commands_cpu_branch():
    """cpu 分支：CPU 版 torch + onnxruntime，不含 GPU 专用包。"""
    commands = bootstrap.build_gpu_dependency_commands("cpu", None)

    assert "pip install torch --index-url https://download.pytorch.org/whl/cpu" in commands
    assert "pip install onnxruntime" in commands
    joined = "\n".join(commands)
    assert "onnxruntime-gpu" not in joined
    assert "cu128" not in joined and "cu121" not in joined


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
    assert len(result["pending_commands"]) == 4  # 注释 + 3 条 pip 命令

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
    # 3 条 pip 命令全部失败并留痕（注释条目不计入）
    assert len(result["errors"]) == 3
    assert all(err["returncode"] == 1 for err in result["errors"])
    assert all(err["command"].startswith("pip install") for err in result["errors"])
    assert all("simulated pip failure" in err["output"] for err in result["errors"])
    # 检测调用 1 次 + 安装调用 3 次（注释条目被跳过）
    assert len([c for c in calls if c.startswith("pip install")]) == 3
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
    assert len(executed) == 3


def test_install_gpu_dependencies_auto_install_rocm_skips_notes(tmp_path):
    """rocm 分支自动安装：3 条可执行命令被执行，# 说明条目仅打印不执行。"""
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
    # rocm 清单 3 条可执行命令（torch rocm / onnxruntime-directml）中
    # 可执行条目全部执行；1 条注释说明被跳过
    executable = [c for c in result["pending_commands"] if not c.startswith("#")]
    assert len(executable) == 2
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
