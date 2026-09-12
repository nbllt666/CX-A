# -*- coding: utf-8 -*-
"""Task H4 GPU 检测单元测试（pytest，mock runner 注入，不触碰真实硬件命令）。

覆盖：
- NVIDIA：nvidia-smi 成功 → recommend=cuda，CUDA 版本解析（12.4 / 缺失两种形态）
- AMD：nvidia-smi 失败 + wmic 命中 → rocm；wmic 不可用 + pnputil 兜底命中 → rocm
- CPU：全部探测失败 → cpu 兜底
- detect_via_runner 纯函数式契约与 GpuDetector 实例注入
"""

from installer.gpu_detect import GpuDetector, detect_via_runner

#: nvidia-smi 典型输出片段（右上角含 CUDA Version）。
_NVIDIA_SMI_OK = (
    "Mon Sep 12 10:00:00 2026\n"
    "+-----------------------------------------------------------------------------------------+\n"
    "| NVIDIA-SMI 551.61       Driver Version: 551.61       CUDA Version: 12.4          |\n"
    "|-----------------------------------------+------------------------+----------------------+\n"
    "| GPU  Name                    TCC/WDDM   | Bus-Id          Disp.A | Volatile Uncorr. ECC |\n"
    "|   0  NVIDIA GeForce RTX 4060 WDDM      | 00000000:01:00.0  On   |                  N/A |\n"
    "+-----------------------------------------------------------------------------------------+\n"
)

#: wmic 枚举显示控制器典型输出（AMD 命中）。
_WMIC_AMD_OK = (
    "Name\n"
    "AMD Radeon RX 7900 XT\n"
    "NVIDIA GeForce RTX 4060\n"
)

#: pnputil 枚举 Display 类设备典型输出（AMD 命中，供 wmic 被移除的新版 Windows 兜底）。
_PNPUTIL_AMD_OK = (
    "Instance ID:                PCI\\VEN_1002&DEV_744C...\n"
    "Device Description:         AMD Radeon RX 7900 XT\n"
    "Class Name:                 Display\n"
)


def _runner_map(responses):
    """构造按命令关键字分派的 mock runner：responses 为 {关键字: (rc, stdout)}。"""
    def runner(cmd):
        for keyword, (rc, stdout) in responses.items():
            if keyword in cmd:
                return rc, stdout
        return -1, ""
    return runner


# ------------------------------------------------------------------ #
# NVIDIA：成功检测 + CUDA 版本解析                                     #
# ------------------------------------------------------------------ #

def test_detect_nvidia_cuda_with_version():
    """nvidia-smi 成功 → vendor=nvidia / recommend=cuda，CUDA 版本解析为 12.4。"""
    runner = _runner_map({"nvidia-smi": (0, _NVIDIA_SMI_OK)})

    report = detect_via_runner(runner)

    assert report["gpu_vendor"] == "nvidia"
    assert report["cuda_version"] == "12.4"
    assert report["recommend"] == "cuda"
    assert "NVIDIA" in report["details"]


def test_detect_nvidia_cuda_version_missing():
    """nvidia-smi 成功但输出缺 CUDA Version 行 → 仍判 cuda，cuda_version=None。"""
    runner = _runner_map({"nvidia-smi": (0, "NVIDIA-SMI 551.61  Driver Version: 551.61\n")})

    report = detect_via_runner(runner)

    assert report["gpu_vendor"] == "nvidia"
    assert report["cuda_version"] is None
    assert report["recommend"] == "cuda"


def test_detect_nvidia_cuda_umd_version_new_driver():
    """新驱动（616.x 起）右上角为 "CUDA UMD Version: 13.4" 形态 → 同样解析出版本。"""
    new_driver_output = (
        "Sat Sep 12 10:41:55 2026\n"
        "+-----------------------------------------------------------------------------------------+\n"
        "| NVIDIA-SMI 616.56                 KMD Version: 616.56        CUDA UMD Version: 13.4     |\n"
        "|   0  NVIDIA GeForce RTX 3080      WDDM  |   00000000:03:00.0  On   |                  N/A |\n"
        "+-----------------------------------------------------------------------------------------+\n"
    )
    runner = _runner_map({"nvidia-smi": (0, new_driver_output)})

    report = detect_via_runner(runner)

    assert report["gpu_vendor"] == "nvidia"
    assert report["cuda_version"] == "13.4"
    assert report["recommend"] == "cuda"
    assert "CUDA 13.4" in report["details"]


# ------------------------------------------------------------------ #
# AMD：wmic 命中 / pnputil 兜底                                       #
# ------------------------------------------------------------------ #

def test_detect_amd_via_wmic():
    """nvidia-smi 失败 + wmic 枚举命中 AMD/Radeon → vendor=amd / recommend=rocm。"""
    runner = _runner_map({
        "nvidia-smi": (-1, ""),
        "win32_VideoController": (0, _WMIC_AMD_OK),
    })

    report = detect_via_runner(runner)

    assert report["gpu_vendor"] == "amd"
    assert report["cuda_version"] is None
    assert report["recommend"] == "rocm"
    assert "wmic" in report["details"]


def test_detect_amd_via_pnputil_fallback():
    """nvidia-smi 与 wmic 均失败（新版 Windows 移除 wmic）+ pnputil 命中 → rocm。"""
    runner = _runner_map({
        "nvidia-smi": (-1, ""),
        "win32_VideoController": (-1, ""),  # wmic 不存在
        "enum-devices": (0, _PNPUTIL_AMD_OK),
    })

    report = detect_via_runner(runner)

    assert report["gpu_vendor"] == "amd"
    assert report["recommend"] == "rocm"
    assert "pnputil" in report["details"]


def test_detect_amd_case_insensitive():
    """AMD 枚举输出小写形态（amd/radeon）也应命中（大小写不敏感）。"""
    runner = _runner_map({
        "nvidia-smi": (-1, ""),
        "win32_VideoController": (0, "Name\namd radeon graphics\n"),
    })

    report = detect_via_runner(runner)

    assert report["gpu_vendor"] == "amd"
    assert report["recommend"] == "rocm"


# ------------------------------------------------------------------ #
# CPU 兜底                                                            #
# ------------------------------------------------------------------ #

def test_detect_cpu_fallback_when_all_fail():
    """nvidia-smi / wmic / pnputil 全部失败 → cpu 兜底，recommend=cpu。"""
    runner = _runner_map({})  # 任何命令均返回 (-1, "")

    report = detect_via_runner(runner)

    assert report["gpu_vendor"] == "cpu"
    assert report["cuda_version"] is None
    assert report["recommend"] == "cpu"
    assert report["details"]


def test_detect_cpu_fallback_when_display_is_intel():
    """枚举命中但仅有 Intel 集显（无 AMD/Radeon 关键字）→ 仍回退 cpu。"""
    runner = _runner_map({
        "nvidia-smi": (-1, ""),
        "win32_VideoController": (0, "Name\nIntel(R) UHD Graphics 770\n"),
    })

    report = detect_via_runner(runner)

    assert report["gpu_vendor"] == "cpu"
    assert report["recommend"] == "cpu"


# ------------------------------------------------------------------ #
# detect_via_runner 纯函数契约 / GpuDetector 注入                      #
# ------------------------------------------------------------------ #

def test_detect_via_runner_is_pure_function():
    """detect_via_runner 不依赖全局状态：同一 runner 多次调用结果一致。"""
    runner = _runner_map({"nvidia-smi": (0, _NVIDIA_SMI_OK)})

    first = detect_via_runner(runner)
    second = detect_via_runner(runner)

    assert first == second
    assert first["recommend"] == "cuda"


def test_gpu_detector_instance_runner_injection():
    """GpuDetector 实例化注入 runner 后 detect() 结果一致；detect 可临时换 runner。"""
    nvidia_runner = _runner_map({"nvidia-smi": (0, _NVIDIA_SMI_OK)})
    cpu_runner = _runner_map({})

    detector = GpuDetector(runner=nvidia_runner)
    assert detector.detect()["recommend"] == "cuda"
    # 临时覆盖 runner：同一检测器可切换探测通道
    assert detector.detect(runner=cpu_runner)["recommend"] == "cpu"
