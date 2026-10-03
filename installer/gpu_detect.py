# -*- coding: utf-8 -*-
"""GPU 硬件检测（installer/gpu_detect.py）——薄适配层。

本文件**不再保留第二份实现**：探测核心（``default_runner`` /
``detect_via_runner`` / ``GpuDetector`` 及全部模块级常量）已下沉到
``lite/runtime/hardware_profile.py``（首启向导与安装器共用同一真相源），
此处仅按原公开名再导出，保持既有调用方契约不变。

对齐 spec「安装程序 GPU 加速依赖」：
- NVIDIA：调用 ``nvidia-smi`` 读取驱动报告的 CUDA 版本（输出右上角
  "CUDA Version: 12.x"）；成功 → vendor=nvidia + recommend=cuda。
- AMD：nvidia-smi 不可用/失败时，经 ``wmic``（Win11 24H2 起逐步移除）或
  ``pnputil`` 枚举显示设备名，命中 AMD/Radeon → vendor=amd + recommend=rocm。
- 均失败 → vendor=cpu + recommend=cpu（无独显或驱动检测不可用的兜底）。

设计约束：
- 检测核心 ``detect_via_runner`` 为纯函数式，接受 ``runner(cmd)->(returncode, stdout)``
  注入，单测可完全 mock 外部命令；
- 默认 runner 在 Windows 下注入 ``CREATE_NO_WINDOW`` 避免安装期弹出控制台窗口，
  并捕获所有异常降级为 ``(非 0, "")``，保证检测永不抛错（失败不阻断主安装）。

导入兼容（两种调用方式均可用）：
1. 包上下文（``from installer.gpu_detect import ...``，如 bootstrap.py）；
2. CLI 直跑兜底（``from gpu_detect import ...``）——``python installer/bootstrap.py``
   时 bootstrap 已把项目根注入 ``sys.path``（见 bootstrap.py 第 34-35 行），
   故 ``from lite.runtime.hardware_profile import ...`` 在两种方式下都成立；
   此处仍加 try/except 兜底：极端情况下（项目根未在 sys.path）按文件位置
   上溯项目根再注入后重试，避免 import 期直接崩溃。

monkeypatch 兼容：``GpuDetector`` 以子类形式薄适配——``__init__`` 显式从
**本模块命名空间**解析 ``default_runner``，使既有测试对
``installer.gpu_detect.default_runner`` 的 monkeypatch 继续生效
（若直接 re-export 父类，默认 runner 会在 hardware_profile 命名空间解析，
monkeypatch 将失效）。
"""

# ---- 探测核心下沉至 lite.runtime.hardware_profile（唯一实现）----
try:  # 常规路径：包上下文 / CLI 直跑（项目根已注入 sys.path）
    from lite.runtime.hardware_profile import (  # noqa: F401
        _AMD_PNPUTIL_CMD,
        _AMD_WMIC_CMD,
        _CREATE_NO_WINDOW,
        _CUDA_UMD_VERSION_RE,
        _CUDA_VERSION_RE,
        _DETECT_TIMEOUT_S,
        _NVIDIA_SMI_CMD,
        _summarize_gpus,
        default_runner,
        detect_via_runner,
        probe_gpu_inventory,
    )
    from lite.runtime.hardware_profile import GpuDetector as _GpuDetector
except ImportError:  # pragma: no cover - 项目根未在 sys.path 的极端兜底
    import os
    import sys

    _PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _PROJECT_ROOT not in sys.path:
        sys.path.insert(0, _PROJECT_ROOT)
    from lite.runtime.hardware_profile import (  # noqa: F401,E402
        _AMD_PNPUTIL_CMD,
        _AMD_WMIC_CMD,
        _CREATE_NO_WINDOW,
        _CUDA_UMD_VERSION_RE,
        _CUDA_VERSION_RE,
        _DETECT_TIMEOUT_S,
        _NVIDIA_SMI_CMD,
        _summarize_gpus,
        default_runner,
        detect_via_runner,
        probe_gpu_inventory,
    )
    from lite.runtime.hardware_profile import GpuDetector as _GpuDetector  # noqa: E402


class GpuDetector(_GpuDetector):
    """GPU 检测器（薄适配）：默认 runner 从本模块命名空间解析。

    与 ``lite.runtime.hardware_profile.GpuDetector`` 同形（``__init__(runner=None)``
    / ``detect(runner=None)``），仅覆盖默认 runner 的解析来源，保持既有
    monkeypatch 语义不变。
    """

    def __init__(self, runner=None):
        """初始化检测器：runner 缺省时取本模块 ``default_runner``。"""
        super().__init__(runner if runner is not None else default_runner)

    def probe_inventory(self, runner=None):
        """枚举 GPU 清单（薄适配：复用 ``probe_gpu_inventory`` 唯一实现）。

        公开 API 的**增量**方法（既有 ``__init__`` / ``detect`` 与返回结构不变），
        供安装链取核显 / 独显结论用于 ORT 包分叉；失败降级为空清单 + 说明，不抛错。

        :param runner: 临时覆盖实例级 runner；缺省用本实例绑定的 runner。
        :return: ``(gpus, notes)``——与
            ``lite.runtime.hardware_profile.probe_gpu_inventory`` 同构。
        """
        return probe_gpu_inventory(runner if runner is not None else self._runner)

    def detect_inventory(self, runner=None):
        """返回画像增量摘要（核显 / 独显结论），供 ORT 包分叉使用。

        为公开 API 的增量方法：不改变 ``detect()`` 契约，仅在既有检测之外补充
        ``gpus`` / ``has_igpu`` / ``dgpu_vendor``；枚举失败降级为「无核显」保守结论，
        绝不抛错（安装失败不阻断主安装的口径不变）。

        :param runner: 临时覆盖实例级 runner。
        :return: dict —— ``gpus`` / ``has_igpu`` / ``dgpu_vendor`` / ``notes``。
        """
        gpus, notes = self.probe_inventory(runner=runner)
        has_igpu, dgpu_vendor = _summarize_gpus(gpus)
        return {
            "gpus": gpus,
            "has_igpu": has_igpu,
            "dgpu_vendor": dgpu_vendor,
            "notes": notes,
        }


__all__ = [
    "GpuDetector",
    "default_runner",
    "detect_via_runner",
    "probe_gpu_inventory",
    "_CREATE_NO_WINDOW",
    "_NVIDIA_SMI_CMD",
    "_AMD_WMIC_CMD",
    "_AMD_PNPUTIL_CMD",
    "_CUDA_VERSION_RE",
    "_CUDA_UMD_VERSION_RE",
    "_DETECT_TIMEOUT_S",
]