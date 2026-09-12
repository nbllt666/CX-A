# -*- coding: utf-8 -*-
"""GPU 硬件检测（installer/gpu_detect.py）。

对齐 spec「安装程序 GPU 加速依赖」：
- NVIDIA：调用 ``nvidia-smi`` 读取驱动报告的 CUDA 版本（输出右上角
  "CUDA Version: 12.x"）；成功 → vendor=nvidia + recommend=cuda。
- AMD：nvidia-smi 不可用/失败时，经 ``wmic``（Win11 24H2 起逐步移除）或
  ``pnputil`` 枚举显示设备名，命中 AMD/Radeon → vendor=amd + recommend=rocm。
- 均失败 → vendor=cpu + recommend=cpu（无独显或驱动检测不可用的兜底）。

设计约束：
- 检测核心 :func:`detect_via_runner` 为纯函数式，接受 ``runner(cmd)->(returncode, stdout)``
  注入，单测可完全 mock 外部命令；
- 默认 runner 在 Windows 下注入 ``CREATE_NO_WINDOW`` 避免安装期弹出控制台窗口，
  并捕获所有异常降级为 ``(非 0, "")``，保证检测永不抛错（失败不阻断主安装）。
"""

import re
import subprocess
import sys

#: Windows 进程创建标志：不显示控制台窗口（subprocess.CREATE_NO_WINDOW 的数值，
#: 直接写数值以兼容非 Windows 平台的 import 期常量缺失问题）。
_CREATE_NO_WINDOW = 0x08000000

#: NVIDIA 探测命令（无参数运行即输出驱动与 CUDA 信息）。
_NVIDIA_SMI_CMD = "nvidia-smi"

#: AMD 探测命令 1：wmic 枚举显示控制器名称（老版本 Windows 可用）。
_AMD_WMIC_CMD = "wmic path win32_VideoController get name"

#: AMD 探测命令 2：pnputil 枚举 Display 类设备（wmic 被移除的新版 Windows 兜底）。
_AMD_PNPUTIL_CMD = "pnputil /enum-devices /class Display"

#: nvidia-smi 输出中的 CUDA 版本提取（经典格式，形如 "CUDA Version: 12.4"）。
_CUDA_VERSION_RE = re.compile(r"CUDA Version:\s*(\d+(?:\.\d+)?)")

#: 新版驱动（616.x 起）右上角改用 UMD 命名（形如 "CUDA UMD Version: 13.4"），
#: 与经典格式共存解析，保证新旧驱动均能报告 CUDA 版本。
_CUDA_UMD_VERSION_RE = re.compile(r"CUDA UMD Version:\s*(\d+(?:\.\d+)?)")

#: 检测类命令统一超时（秒）：nvidia-smi / wmic / pnputil 均为轻量枚举命令。
_DETECT_TIMEOUT_S = 30


def default_runner(cmd):
    """默认命令执行器：``cmd -> (returncode, stdout)``，永不抛异常。

    - 命令不存在 / 执行超时 / 其它异常统一降级为 ``(-1, "")``；
    - Windows 下注入 ``CREATE_NO_WINDOW``，避免安装器界面弹出黑色控制台窗口；
    - 解码失败以 replace 兜底（wmic 等命令输出编码随系统区域变化）。

    :param cmd: 命令字符串（以空格切分为 argv）。
    :return: (returncode, stdout) 二元组。
    """
    creationflags = _CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        completed = subprocess.run(
            cmd.split(),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_DETECT_TIMEOUT_S,
            creationflags=creationflags,
        )
        return completed.returncode, completed.stdout or ""
    except Exception:  # noqa: BLE001 - FileNotFoundError/TimeoutError 等统一兜底
        return -1, ""


def detect_via_runner(runner):
    """纯函数式检测核心：依次探测 NVIDIA → AMD → CPU，返回检测报告 dict。

    :param runner: 命令执行器 ``runner(cmd)->(returncode, stdout)``。
    :return: 检测报告 dict，字段：
        - ``gpu_vendor``: ``"nvidia" | "amd" | "cpu"``
        - ``cuda_version``: 驱动报告的 CUDA 版本字符串（如 "12.4"），非 NVIDIA 为 None
        - ``recommend``: ``"cuda" | "rocm" | "cpu"``
        - ``details``: 中文检测说明
    """
    # ---- 1. NVIDIA：nvidia-smi 可用即判定，并解析驱动报告的 CUDA 版本 ----
    returncode, stdout = runner(_NVIDIA_SMI_CMD)
    if returncode == 0 and stdout.strip():
        match = _CUDA_VERSION_RE.search(stdout) or _CUDA_UMD_VERSION_RE.search(stdout)
        cuda_version = match.group(1) if match else None
        detail = "检测到 NVIDIA GPU"
        if cuda_version:
            detail += f"（驱动支持 CUDA {cuda_version}）"
        else:
            detail += "（未能从驱动输出解析 CUDA 版本，按 CUDA 12 路径处理）"
        return {
            "gpu_vendor": "nvidia",
            "cuda_version": cuda_version,
            "recommend": "cuda",
            "details": detail,
        }

    # ---- 2. AMD：wmic 优先，pnputil 兜底（新版 Windows 已移除 wmic）----
    for probe_cmd, via in ((_AMD_WMIC_CMD, "wmic"), (_AMD_PNPUTIL_CMD, "pnputil")):
        returncode, stdout = runner(probe_cmd)
        if returncode == 0 and ("AMD" in stdout.upper() or "RADEON" in stdout.upper()):
            return {
                "gpu_vendor": "amd",
                "cuda_version": None,
                "recommend": "rocm",
                "details": f"检测到 AMD GPU（经 {via} 枚举），推荐 ROCm / DirectML 加速路径",
            }

    # ---- 3. 兜底：无独显或检测全部失败 → CPU 推理路径 ----
    return {
        "gpu_vendor": "cpu",
        "cuda_version": None,
        "recommend": "cpu",
        "details": "未检测到可用的独立 GPU（nvidia-smi 与 AMD 设备枚举均未命中），回退 CPU 推理路径",
    }


class GpuDetector:
    """GPU 检测器：封装 runner 注入点，供 bootstrap 与测试复用。

    :param runner: 自定义命令执行器；缺省使用 :func:`default_runner`。
    """

    def __init__(self, runner=None):
        """初始化检测器并绑定命令执行器。"""
        self._runner = runner if runner is not None else default_runner

    def detect(self, runner=None):
        """执行 GPU 检测，返回检测报告 dict。

        :param runner: 临时覆盖实例级 runner（便于测试复用同一检测器）。
        :return: :func:`detect_via_runner` 同构的检测报告 dict。
        """
        return detect_via_runner(runner if runner is not None else self._runner)
