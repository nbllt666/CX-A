# -*- coding: utf-8 -*-
"""硬件画像与推荐配置（lite/runtime/hardware_profile.py，Task 3 / Task 4）。

对齐 spec「首启向导 · Requirement: 硬件画像与推荐配置」：
- **GPU 探测**：从 ``installer/gpu_detect.py`` 全量下沉（本文件为唯一实现），
  保持 ``default_runner`` / ``detect_via_runner`` / ``GpuDetector`` 契约不动，
  ``installer/gpu_detect.py`` 改为薄适配层引入本文件再导出；
- **系统画像**：CPU 核数、内存（GB）、GPU 厂商、显存（GB）、CUDA 版本、
  安装根磁盘可用空间，全部通过可选依赖 / 外部命令注入点探测；
- **推荐**：按内存 / 显存 / 磁盘阈值产出推荐配置补丁与本地模型档位，
  探测失败一律降级并记入 ``probe_notes``，绝不抛错（接口永不返回 5xx）。

设计要点（对齐 lite/runtime/model_downloader.py）：
- 路径解析统一收敛到 ``lite.config.paths.app_root()``（frozen-aware），
  禁止用 ``__file__`` 上溯推导安装根（冻结态会指到 ``runtime/backend/_internal``）；
- 可选依赖（psutil / requests 等）统一 try/except ImportError 降级；
- 探测类函数不打印噪声，失败信息进 ``probe_notes``；
- 依赖外部命令一律经 ``runner`` 注入点，测试可完全 mock，不触碰真实硬件。
"""

import json
import os
import re
import shutil
import subprocess
import sys

from lite.config.paths import app_root

# ------------------------------------------------------------------ #
# GPU 探测核心（自 installer/gpu_detect.py 原样下沉，逻辑与正则一字不改）#
# ------------------------------------------------------------------ #

#: Windows 进程创建标志：不显示控制台窗口（subprocess.CREATE_NO_WINDOW 的数值，
#: 直接写数值以兼容非 Windows 平台的 import 期常量缺失问题）。
_CREATE_NO_WINDOW = 0x08000000

#: NVIDIA 探测命令（无参数运行即输出驱动与 CUDA 信息）。
_NVIDIA_SMI_CMD = "nvidia-smi"

#: AMD 探测命令 1：wmic 枚举显示控制器名称（老版本 Windows 可用）。
_AMD_WMIC_CMD = "wmic path win32_VideoController get name"

#: AMD 探测命令 2：pnputil 枚举 Display 类设备（wmic 被移除的新版 Windows 兜底）。
_AMD_PNPUTIL_CMD = "pnputil /enum-devices /class Display"

#: GPU 清单枚举命令（onnxruntime / 核显识别扩展）：PowerShell 经 WMI 枚举
#: ``Win32_VideoController``，输出 Name + PNPDeviceID 的 JSON。
#: 采用 **argv 列表** 形式——``-Command`` 参数含引号与管道，若按空格切分会破坏
#: 语义；``default_runner`` 对 list/tuple 直接作为 argv 执行。
_POWERSHELL_VIDEO_CMD = [
    "powershell",
    "-NoProfile",
    "-Command",
    "Get-CimInstance Win32_VideoController | Select-Object Name,PNPDeviceID | ConvertTo-Json -Depth 2",
]

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

    :param cmd: 命令字符串（以空格切分为 argv），或已切分好的 argv 列表/元组
        （PowerShell 等含引号与管道的命令需以列表传入，避免切分破坏语义）。
    :return: (returncode, stdout) 二元组。
    """
    argv = list(cmd) if isinstance(cmd, (list, tuple)) else cmd.split()
    creationflags = _CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        completed = subprocess.run(
            argv,
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


# ------------------------------------------------------------------ #
# GPU 清单枚举与核显识别（加速画像扩展）                              #
# ------------------------------------------------------------------ #

#: 独显厂商白名单（用于 ``dgpu_vendor`` 结论；other/unknown 不计入）。
_KNOWN_DGPU_VENDORS = ("nvidia", "amd", "intel")


def _vendor_from_name(name_upper: str) -> str:
    """由适配器名称启发式判定厂商（``nvidia`` / ``amd`` / ``intel`` / ``other``）。"""
    if "NVIDIA" in name_upper:
        return "nvidia"
    if "AMD" in name_upper or "RADEON" in name_upper:
        return "amd"
    if "INTEL" in name_upper:
        return "intel"
    return "other"


def _gpu_type_from_name(vendor: str, name_upper: str) -> str:
    """由厂商 + 名称启发式判定适配器类型（``igpu`` / ``dgpu`` / ``unknown``）。

    口径（spec「硬件加速画像」）：
    - NVIDIA → 独显；
    - Intel → 核显（名称含 UHD / Iris / Graphics）；
    - AMD：含 ``RX`` / ``Radeon Pro`` → 独显；含 ``Graphics`` / ``Vega``（无 RX）→ 核显；
    - 无法判定 → ``unknown``（标注不确定，不猜测）。
    """
    if vendor == "nvidia":
        return "dgpu"
    if vendor == "intel":
        return "igpu"
    if vendor == "amd":
        if "RX" in name_upper or "RADEON PRO" in name_upper:
            return "dgpu"
        if "GRAPHICS" in name_upper or "VEGA" in name_upper:
            return "igpu"
        return "unknown"
    if "GRAPHICS" in name_upper or "UHD" in name_upper or "IRIS" in name_upper:
        return "igpu"
    return "unknown"


def _classify_gpu(name: str, pnp_id: str) -> tuple[str, str]:
    """按名称 + PNPDeviceID 判定 ``(vendor, type)``。

    - ``ROOT\\DISPLAY``（虚拟显示适配器）/ Microsoft Basic Display → ``virtual``；
    - 其余走名称启发式。
    """
    name_upper = (name or "").upper()
    pnp_upper = (pnp_id or "").upper()
    if (
        "ROOT\\DISPLAY" in pnp_upper
        or "MICROSOFT BASIC DISPLAY" in name_upper
        or "BASIC RENDER DRIVER" in name_upper
    ):
        return _vendor_from_name(name_upper), "virtual"
    vendor = _vendor_from_name(name_upper)
    return vendor, _gpu_type_from_name(vendor, name_upper)


def probe_gpu_inventory(runner=None) -> tuple[list, list]:
    """枚举 GPU 清单（WMI ``Win32_VideoController`` → JSON 解析）。

    容错口径（对齐 ``probe_notes``）：命令不可用 / 返回为空 / 输出非 JSON /
    解析异常一律降级为**空清单 + 中文说明**，绝不抛错；虚拟适配器（``ROOT\\DISPLAY``）
    照常识别为 ``virtual`` 类型但不会被计入核显 / 独显结论。

    :param runner: 命令执行器（缺省 :func:`default_runner`）；
        以 argv 列表形式收到 ``_POWERSHELL_VIDEO_CMD`` 的副本。
    :return: ``(gpus, notes)``——gpus 为 ``list[dict]``，每项含
        ``vendor`` / ``name`` / ``type``（``igpu`` / ``dgpu`` / ``virtual`` / ``unknown``）
        / ``vram_hint``（名称线索，默认空串，不使用 32-bit 的 AdapterRAM）；
        notes 为中文降级说明列表（空表示枚举成功）。
    """
    exe = runner if runner is not None else default_runner
    try:
        returncode, stdout = exe(list(_POWERSHELL_VIDEO_CMD))
    except Exception as exc:  # noqa: BLE001 - runner 异常统一降级
        return [], [f"GPU 清单枚举异常（{exc}），跳过核显识别"]

    if returncode != 0 or not (stdout or "").strip():
        return [], ["未能枚举 GPU 清单（WMI 命令不可用或返回为空），跳过核显识别"]

    try:
        data = json.loads(stdout)
    except (ValueError, TypeError):
        return [], ["GPU 清单枚举输出无法解析为 JSON，跳过核显识别"]

    items = data if isinstance(data, list) else [data]
    gpus: list = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("Name") or "").strip()
        if not name:
            continue
        pnp_id = str(item.get("PNPDeviceID") or "").strip()
        vendor, gpu_type = _classify_gpu(name, pnp_id)
        gpus.append({
            "vendor": vendor,
            "name": name,
            "type": gpu_type,
            "vram_hint": "",  # 名称线索（留空）；AdapterRAM 为 32-bit 不可用
        })

    if not gpus:
        return [], ["GPU 清单枚举为空，跳过核显识别"]
    return gpus, []


def _summarize_gpus(gpus) -> tuple[bool, str | None]:
    """由 GPU 清单汇总 ``(has_igpu, dgpu_vendor)``。

    - ``has_igpu``：清单中存在 ``igpu`` 类型；
    - ``dgpu_vendor``：首个已知独显厂商（nvidia/amd/intel），无则 None。
    """
    has_igpu = any(g.get("type") == "igpu" for g in gpus)
    dgpu_vendor = None
    for gpu in gpus:
        if gpu.get("type") == "dgpu" and gpu.get("vendor") in _KNOWN_DGPU_VENDORS:
            dgpu_vendor = gpu.get("vendor")
            break
    return has_igpu, dgpu_vendor


# ------------------------------------------------------------------ #
# 系统画像探测                                                       #
# ------------------------------------------------------------------ #

def probe_cpu_cores() -> int | None:
    """探测 CPU 逻辑核心数（``os.cpu_count``）。

    :return: int 核心数；探测失败返回 None。
    """
    try:
        count = os.cpu_count()
        return int(count) if count else None
    except Exception:  # noqa: BLE001 - 探测失败仅降级
        return None


def probe_memory_gb() -> float | None:
    """探测物理内存总量（GB，三级降级：psutil → ctypes → /proc/meminfo）。

    优先级：
    1. 可选依赖 ``psutil``（优先，跨平台）；
    2. 无 psutil 且 Windows：``ctypes`` 调 ``GlobalMemoryStatusEx``，
       结构体 ``MEMORYSTATUSEX`` 的 ``dwLength`` 必须先赋值，返回
       ``ullTotalPhys / 1024**3``；
    3. 其它平台无 psutil：读 ``/proc/meminfo`` 的 ``MemTotal``（KiB→GB）；
    4. 全部失败返回 None。失败原因由调用方/上层记入 probe_notes，本函数不打印。

    :return: float（GB）或探测失败返回 None。
    """
    # ---- 1. 可选依赖 psutil ----
    try:
        import psutil  # noqa: PLC0415 - 可选依赖，未装时降级
        return float(psutil.virtual_memory().total) / (1024 ** 3)
    except ImportError:
        pass  # 继续兜底
    except Exception:  # noqa: BLE001 - 调用异常降级
        return None

    # ---- 2. Windows：ctypes 调 GlobalMemoryStatusEx ----
    if sys.platform == "win32":
        try:
            import ctypes  # noqa: PLC0415

            class MEMORYSTATUSEX(ctypes.Structure):
                """Windows MEMORYSTATUSEX 结构体（仅映射需要字段）。"""
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),   # 必须先赋值为结构体字节长
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return float(stat.ullTotalPhys) / (1024 ** 3)
            return None
        except Exception:  # noqa: BLE001 - 结构体/调用异常降级
            return None

    # ---- 3. 其它平台：/proc/meminfo ----
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    kib = float(line.split()[1])
                    return kib / (1024 ** 2)  # KiB → GB
        return None
    except Exception:  # noqa: BLE001 - 文件不存在/解析失败降级
        return None


def probe_vram_gb(runner=None) -> float | None:
    """探测 NVIDIA 显存总量（GB，经 nvidia-smi 查询）。

    命令：``nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits``，
    输出为单行 MiB 数值（多 GPU 时每行一个）；取第一块返回 MiB→GB。

    :param runner: 命令执行器；缺省使用 :func:`default_runner`。
    :return: float（GB，取首块 GPU）；未检测到或解析失败返回 None。
    """
    exe = runner if runner is not None else default_runner
    cmd = "nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits"
    returncode, stdout = exe(cmd)
    if returncode != 0 or not stdout.strip():
        return None
    try:
        mib = float(stdout.strip().splitlines()[0].strip())
        return round(mib / 1024.0, 1)  # MiB → GB
    except (ValueError, IndexError):  # noqa: PERF203 - 解析失败降级
        return None


def probe_disk_free_gb(path) -> float | None:
    """探测 path 所在磁盘的可用空间（GB）。

    path 允许不存在：逐级向上找最近存在的祖先目录再取 ``shutil.disk_usage``。

    :param path: 目标目录绝对路径。
    :return: float（GB）；探测失败返回 None。
    """
    try:
        probe = path
        while probe and not os.path.exists(probe):
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        if not probe:
            probe = os.getcwd()
        free_bytes = shutil.disk_usage(probe).free
        return float(free_bytes) / (1024 ** 3)
    except Exception:  # noqa: BLE001 - 磁盘不存在/无权限等降级
        return None


def detect_profile(root=None, runner=None) -> dict:
    """综合硬件画像：CPU 核数 / 内存 / GPU / 显存 / CUDA / 磁盘可用空间。

    :param root: 安装根目录（用于磁盘探测）；缺省按文件派生工程根。
    :param runner: 命令执行器；缺省使用 :func:`default_runner`（GPU / 显存探测）。
    :return: dict，字段：``cpu_cores`` / ``ram_gb`` / ``gpu_vendor`` /
        ``vram_gb`` / ``cuda_version`` / ``disk_free_gb`` / ``probe_notes``
        （中文说明，空表示探测全部成功）；增量字段 ``gpus``（GPU 清单）/
        ``has_igpu``（是否含核显）/ ``dgpu_vendor``（首个已知独显厂商或 None）。
    """
    exe = runner if runner is not None else default_runner
    notes: list[str] = []

    #: 安装根：优先入参，其次走 frozen-aware 的 app_root()（开发态＝项目根，
    #: 冻结态＝便携根）。修复前此处用 __file__ 上溯两层，冻结态会指到
    #: runtime/backend/_internal，导致磁盘余量按错误的盘/目录估算。
    if root is None:
        root = app_root()

    cpu_cores = probe_cpu_cores()
    if cpu_cores is None:
        notes.append("未能获取 CPU 核心数")

    ram_gb = probe_memory_gb()
    if ram_gb is None:
        notes.append("未能获取内存总量，内存相关推荐按未知（走云）保守处理")

    gpu_report = detect_via_runner(exe)
    gpu_vendor = gpu_report["gpu_vendor"]
    cuda_version = gpu_report["cuda_version"]
    if gpu_vendor == "cpu":
        notes.append(gpu_report["details"])

    vram_gb = None
    if gpu_vendor == "nvidia":
        vram_gb = probe_vram_gb(exe)
        if vram_gb is None:
            notes.append("检测到 NVIDIA GPU 但未能读取显存，按显存未知处理")

    # ---- GPU 清单枚举（核显识别扩展）：失败降级不抛，仅记 probe_notes ----
    gpus, gpu_notes = probe_gpu_inventory(exe)
    notes.extend(gpu_notes)
    has_igpu, dgpu_vendor = _summarize_gpus(gpus)

    disk_free_gb = probe_disk_free_gb(root)
    if disk_free_gb is None:
        notes.append("未能获取磁盘可用空间")

    return {
        "cpu_cores": cpu_cores,
        "ram_gb": ram_gb,
        "gpu_vendor": gpu_vendor,
        "vram_gb": vram_gb,
        "cuda_version": cuda_version,
        "disk_free_gb": disk_free_gb,
        #: 增量字段（既有字段与行为不变）：GPU 清单 + 核显 / 独显结论
        "gpus": gpus,
        "has_igpu": has_igpu,
        "dgpu_vendor": dgpu_vendor,
        "probe_notes": notes,
    }


# ------------------------------------------------------------------ #
# 加速方案决策（唯一真相源）                                          #
# ------------------------------------------------------------------ #

#: 合法加速模式（性能 / 节能）。
_ACCEL_MODES = ("performance", "eco")


def normalize_mode(mode) -> str:
    """归一加速模式：合法值原样返回，非法 / 缺失归一 ``"performance"``。

    :param mode: ``"performance"`` / ``"eco"``（大小写与首尾空白容忍）。
    :return: 归一后的模式字符串。
    """
    if isinstance(mode, str) and mode.strip().lower() in _ACCEL_MODES:
        return mode.strip().lower()
    return "performance"


def derive_default_mode(hardware) -> str:
    """按硬件画像推导默认加速模式（有独显 → performance；仅核显 / 无 → eco）。

    :param hardware: 画像 dict（含 ``dgpu_vendor`` / ``has_igpu`` / ``gpu_vendor``）。
    :return: ``"performance"`` / ``"eco"``。
    """
    hardware = hardware if isinstance(hardware, dict) else {}
    if hardware.get("dgpu_vendor") in _KNOWN_DGPU_VENDORS:
        return "performance"
    if bool(hardware.get("has_igpu")):
        return "eco"  # 仅核显（含被既有探测判为 amd 的核显场景）
    if hardware.get("gpu_vendor") in ("nvidia", "amd"):
        return "performance"
    return "eco"


def _resolve_recommend(hardware) -> str:
    """取画像推荐口径（优先显式 ``recommend``，否则由 ``gpu_vendor`` 派生）。"""
    recommend = hardware.get("recommend")
    if isinstance(recommend, str) and recommend:
        return recommend
    vendor = hardware.get("gpu_vendor")
    if vendor == "nvidia":
        return "cuda"
    if vendor == "amd":
        return "rocm"
    return "cpu"


def _resolve_dgpu_vendor(hardware, recommend) -> str | None:
    """取首个已知独显厂商（优先显式 ``dgpu_vendor``，否则由推荐口径兜底）。"""
    dgpu = hardware.get("dgpu_vendor")
    if dgpu in _KNOWN_DGPU_VENDORS:
        return dgpu
    if recommend == "cuda":
        return "nvidia"
    if recommend == "rocm":
        return "amd"
    return None


def _resolve_igpu_vendor(hardware) -> str | None:
    """取首个已知**核显**厂商（来自 GPU 清单；缺失 / 未知 → None）。

    20261002 用户裁决（AMD 核显优先）：用于「AMD 核显机器 TTS 目标核显、独显留给
    游戏」的判定。清单缺失 / 厂商未知时返回 None——调用方保守回退既有口径
    （不猜测），与画像「无法判定时标注不确定」的口径一致。
    """
    for gpu in hardware.get("gpus") or []:
        if isinstance(gpu, dict) and gpu.get("type") == "igpu":
            vendor = str(gpu.get("vendor") or "").strip().lower()
            if vendor in _KNOWN_DGPU_VENDORS:
                return vendor
    return None


def _llm_gpu_available(hardware, recommend) -> bool:
    """本地 LLM / 嵌入是否走 GPU——**保真复现既有 ``recommend_for`` 规则**。

    既有口径：``gpu_vendor == "nvidia"`` 且显存可得且 ``vram_gb >= 4``。
    """
    vendor = hardware.get("gpu_vendor")
    if vendor is None and recommend == "cuda":
        vendor = "nvidia"
    vram_gb = hardware.get("vram_gb")
    try:
        vram_ok = vram_gb is not None and float(vram_gb) >= 4
    except (TypeError, ValueError):  # noqa: PERF203 - 异常值保守降级
        vram_ok = False
    return vendor == "nvidia" and vram_ok


def accel_plan(hardware, mode, platform=None) -> dict:
    """加速方案决策（纯函数、唯一真相源）——产出各组件落点。

    映射遵循 spec What Changes §3 表（20261002 批 A 扩展 backend / ROCm 面；
    同日用户裁决：**AMD 核显优先**——TTS 目标核显（Linux ROCm / Windows DML-igpu，
    跨模式一致），独显留给游戏与显示；清单缺失或非 AMD 核显保守回退既有分支）：

    - ``tts.accel`` / ``tts.accel_device``：性能模式先判核显（有核显 → ``dml`` +
      ``dgpu``，双模式前提）；无核显 N 卡 → ``cuda``；无核显 A/Intel → ``dml``；
      无 GPU → ``cpu``。节能模式：有核显 → ``dml`` + ``igpu``；无核显 → ``cpu``。
      **Linux 例外**：Linux + 性能 + AMD 独显 → ``rocm``（DirectML 为 Windows
      专有技术，Linux 走 onnxruntime-rocm）。
    - ``asr.device``：性能且 N 卡 → ``gpu``；否则 ``cpu``。**Linux 例外**：
      Linux + 性能 + AMD 独显 → ``gpu``（torch ROCm 路径）。
    - ``local_llm.device`` / ``embedding.device``：性能且 NVIDIA 显存 ≥4 GB →
      ``gpu``；性能 + AMD/Intel 独显或仅核显 → ``gpu``（llama.cpp Vulkan 路径；
      核显无显存数据不设显存门槛）；否则 ``cpu``（节能模式 / 无 GPU 保真既有行为）。
      **Linux 例外**：Linux + 性能 + AMD/Intel 独显维持 ``cpu``（llama.cpp
      ROCm/HIP 构建未纳入，登记为已闭合项外的平台差异）。
    - ``local_llm.backend`` / ``embedding.backend``：``"cuda"``（N 卡路径）/
      ``"vulkan"``（AMD/Intel 独显与核显路径）/ ``""``（CPU 路径），与 device
      同步产出，仅 device=gpu 时非空。

    非法 ``mode`` 归一 ``"performance"``；探测字段缺失 / 异常一律保守降级为
    ``cpu``，绝不抛错。

    :param hardware: 画像 dict（``recommend`` / ``has_igpu`` / ``dgpu_vendor`` /
        ``gpu_vendor`` / ``vram_gb`` 等）。
    :param mode: ``"performance"`` / ``"eco"``（非法归一 performance）。
    :param platform: 平台标识（``sys.platform`` 同构串，如 ``"win32"`` /
        ``"linux"``）；None 缺省取当前 ``sys.platform``。测试可注入以驱动
        Linux ROCm 分支（Linux 真机未验证，代码路径 + 单测覆盖口径）。
    :return: 落点 dict，键：``accel.mode`` / ``tts.accel`` / ``tts.accel_device`` /
        ``asr.device`` / ``local_llm.device`` / ``local_llm.backend`` /
        ``embedding.device`` / ``embedding.backend`` / ``reasons``（中文理由列表）。
    """
    hardware = hardware if isinstance(hardware, dict) else {}
    normalized = normalize_mode(mode)
    reasons: list[str] = []
    sys_platform = sys.platform if platform is None else str(platform)
    is_linux = sys_platform.startswith("linux")

    recommend = _resolve_recommend(hardware)
    has_igpu = bool(hardware.get("has_igpu"))
    dgpu_vendor = _resolve_dgpu_vendor(hardware, recommend)
    igpu_vendor = _resolve_igpu_vendor(hardware)

    # ---- TTS（ORT）：AMD 核显优先（20261002 用户裁决：防独显被游戏占用）----
    # 有 AMD 核显 → TTS 目标核显（跨模式一致）：Linux 走 ROCm（无 DML）；
    # Windows 走 DML 并指向核显设备。清单缺失 / 非 AMD 核显 → 保守回退既有分支。
    # Linux 例外（20261002 批 A）：Linux + 性能 + AMD 独显 → rocm（DirectML 为
    # Windows 专有技术；ROCm EP 缺失时由 bridge 既有回退链兜底）
    if igpu_vendor == "amd":
        if is_linux:
            tts_accel, tts_device = "rocm", ""
            reasons.append("检测到 AMD 核显，TTS 优先走核显（ROCm 路径），独显留给游戏与显示")
        else:
            tts_accel, tts_device = "dml", "igpu"
            reasons.append("检测到 AMD 核显，TTS 优先指向核显（DirectML），独显留给游戏与显示")
    elif is_linux and normalized == "performance" and dgpu_vendor == "amd":
        tts_accel, tts_device = "rocm", ""
        reasons.append("性能模式：Linux 平台 AMD 独显，TTS 走 ROCm 路径（onnxruntime-rocm）")
    elif normalized == "performance":
        if has_igpu:
            tts_accel, tts_device = "dml", "dgpu"
            reasons.append("性能模式：检测到核显，统一装 DirectML 运行时并以独显设备建会话（双模式前提）")
        elif dgpu_vendor == "nvidia":
            tts_accel, tts_device = "cuda", ""
            reasons.append("性能模式：无核显的 NVIDIA 独显，走 CUDA 满速路径")
        elif dgpu_vendor in ("amd", "intel"):
            tts_accel, tts_device = "dml", ""
            reasons.append("性能模式：无核显的 AMD/Intel 独显，Windows 现实路径走 DirectML")
        else:
            tts_accel, tts_device = "cpu", ""
            reasons.append("性能模式：未检测到可用 GPU，回退 CPU 推理")
    else:
        if has_igpu:
            tts_accel, tts_device = "dml", "igpu"
            reasons.append("节能模式：检测到核显，走 DirectML 并指向核显设备")
        else:
            tts_accel, tts_device = "cpu", ""
            reasons.append("节能模式：无核显可用，回退 CPU 推理")

    # ---- ASR（torch）：仅 N 卡有 GPU 路径；Linux + AMD 独显走 ROCm GPU ----
    if is_linux and normalized == "performance" and dgpu_vendor == "amd":
        asr_device = "gpu"
        reasons.append("ASR：性能模式 Linux 平台 AMD 独显走 GPU（torch ROCm 路径）")
    elif normalized == "performance" and dgpu_vendor == "nvidia":
        asr_device = "gpu"
        reasons.append("ASR：性能模式 NVIDIA 独显走 GPU（torch CUDA）")
    else:
        asr_device = "cpu"
        reasons.append("ASR：按 CPU 推理（节能模式，或非 NVIDIA 后端无 GPU 路径）")

    # ---- 本地 LLM / 嵌入（llama.cpp）：device 与 backend 同步决策 ----
    # （20261002 批 A：N 卡 → cuda；AMD/Intel 独显或仅核显 → vulkan；Linux AMD
    # 独显例外维持 cpu——llama.cpp ROCm/HIP 构建不纳入；其余保真既有行为）
    if normalized == "performance" and _llm_gpu_available(hardware, recommend):
        llm_device, llm_backend = "gpu", "cuda"
        reasons.append("本地 LLM / 嵌入：性能模式 NVIDIA 显存满足阈值（≥4 GB），走 GPU（llama.cpp CUDA）")
    elif normalized == "performance" and is_linux and dgpu_vendor in ("amd", "intel"):
        llm_device, llm_backend = "cpu", ""
        reasons.append(
            "本地 LLM / 嵌入：Linux 平台 AMD/Intel 独显维持 CPU"
            "（llama.cpp ROCm/HIP 构建未纳入，Vulkan 为替代路径）"
        )
    elif normalized == "performance" and (
        dgpu_vendor in ("amd", "intel") or (not dgpu_vendor and has_igpu)
    ):
        llm_device, llm_backend = "gpu", "vulkan"
        reasons.append(
            "本地 LLM / 嵌入：性能模式 AMD/Intel 独显或核显，走 GPU"
            "（llama.cpp Vulkan 路径；核显无显存数据不设显存门槛）"
        )
    else:
        llm_device, llm_backend = "cpu", ""
        reasons.append("本地 LLM / 嵌入：按 CPU 推理（节能模式、显存不足或无可用 GPU 后端）")

    return {
        "accel.mode": normalized,
        "tts.accel": tts_accel,
        "tts.accel_device": tts_device,
        "asr.device": asr_device,
        "local_llm.device": llm_device,
        "local_llm.backend": llm_backend,
        "embedding.device": llm_device,
        "embedding.backend": llm_backend,
        "reasons": reasons,
    }


def _build_config_patch(use_local, device, plan, embedding_gpu) -> dict:
    """由 ``accel_plan`` 落点组装 ``recommend_for`` 的 config_patch。

    ``local_llm`` / ``embedding`` 保留既有结构（``embedding`` 仅在走 GPU 时出现），
    并追加 ``asr`` / ``tts``（``accel`` + ``accel_device``）/ ``accel``（``mode``）。
    backend（20261002 批 A）仅在非空**且**对应组件实际走 GPU 时并入 patch——
    走云 / 显存不足等 device=cpu 场景不带 backend，避免语义矛盾。
    """
    patch = {"local_llm": {"enabled": bool(use_local), "device": device}}
    llm_backend = str(plan.get("local_llm.backend") or "")
    if llm_backend and device == "gpu":
        patch["local_llm"]["backend"] = llm_backend
    if embedding_gpu:
        embedding_patch = {"device": "gpu"}
        emb_backend = str(plan.get("embedding.backend") or "")
        if emb_backend:
            embedding_patch["backend"] = emb_backend
        patch["embedding"] = embedding_patch
    patch["asr"] = {"device": plan["asr.device"]}
    patch["tts"] = {"accel": plan["tts.accel"], "accel_device": plan["tts.accel_device"]}
    patch["accel"] = {"mode": plan["accel.mode"]}
    return patch


def _accel_profile(plan, device, use_local) -> dict:
    """组装加速剖面（模式默认 + 各组件落点 + 中文理由）。

    走云（``use_local=False``）时本地 LLM / 嵌入落点强制 ``cpu``（与 config_patch 一致）。
    """
    llm_device = device if use_local else "cpu"
    embedding_device = "gpu" if (use_local and device == "gpu") else "cpu"
    return {
        "mode": plan["accel.mode"],
        "tts": {"accel": plan["tts.accel"], "accel_device": plan["tts.accel_device"]},
        "asr": {"device": plan["asr.device"]},
        "local_llm": {"device": llm_device},
        "embedding": {"device": embedding_device},
        "reasons": list(plan["reasons"]),
    }


# ------------------------------------------------------------------ #
# 推荐配置推导                                                       #
# ------------------------------------------------------------------ #

#: 本地模型推荐档位排序（大 → 小），供磁盘不足逐级降档使用。
_TIER_ORDER = ("8B", "4B", "1.7B", "0.5B")


def recommend_for(profile, disk_free_gb=None) -> dict:
    """按硬件画像产出推荐配置补丁与本地模型档位。

    阈值口径（spec 冻结）：
    - ``ram_gb`` 不可得 → 按未知走云保守处理；
    - ``ram_gb < 8`` → 走云，tier=0.5B，``local_llm.enabled=False``；
    - ``8 <= ram_gb < 16`` → 本机 cpu + 1.7B；
    - ``ram_gb >= 16`` → 本机 cpu + 1.7B（无 GPU 不上调档位）；
    - ``nvidia && vram_gb`` 可得 → gpu 并按显存分档（8B/4B/1.7B），
      若 ``ram_gb < 16`` 档位最高不超过 4B；
    - 磁盘不足（参数优先，否则画像字段）< 档位体积×1.05 → 逐级降档，
      最小档也不足 → 走云并给所需/可用对比。

    :param profile: :func:`detect_profile` 产出（或其同构 dict）。
    :param disk_free_gb: 磁盘可用空间（GB）覆盖值；None 时取
        ``profile["disk_free_gb"]``。
    :return: dict，字段：``use_local`` / ``device`` / ``tier`` /
        ``config_patch`` / ``model`` / ``reasons`` / ``probe_notes``；
        增量字段 ``accel``（加速剖面：模式默认 + 各组件落点 + 中文理由）。

    说明：``local_llm.device`` / ``embedding.device`` / ``asr.device`` 的取值
    **委托** :func:`accel_plan` 产出（收敛单一真相源），本函数仅做内存 / 显存
    档位与磁盘约束判定。
    """
    notes: list[str] = list(profile.get("probe_notes") or [])
    reasons: list[str] = []
    config_patch: dict = {}

    ram_gb = profile.get("ram_gb")
    gpu_vendor = profile.get("gpu_vendor")
    vram_gb = profile.get("vram_gb")
    free_gb = disk_free_gb if disk_free_gb is not None else profile.get("disk_free_gb")

    # ---- 加速剖面（唯一真相源：accel_plan，模式默认按画像推导）----
    mode = derive_default_mode(profile)
    plan = accel_plan(profile, mode)

    # ---- 内存不可得：未知，走云保守处理 ----
    if ram_gb is None:
        note = "内存总量未知，无法评估本地推理可行性，建议先走云端"
        notes.append(note)
        reasons.append(note)
        return _make_recommendation(
            use_local=False, device="cpu", tier="0.5B",
            config_patch=_build_config_patch(
                use_local=False, device="cpu", plan=plan, embedding_gpu=False,
            ),
            reasons=reasons, notes=notes,
            accel=_accel_profile(plan, device="cpu", use_local=False),
        )

    # ---- 内存不足：走云 ----
    if ram_gb < 8:
        reason = f"内存仅 {ram_gb:.1f} GB（低于本地推理阈值 8 GB），建议走云端"
        reasons.append(reason)
        return _make_recommendation(
            use_local=False, device="cpu", tier="0.5B",
            config_patch=_build_config_patch(
                use_local=False, device="cpu", plan=plan, embedding_gpu=False,
            ),
            reasons=reasons, notes=notes,
            accel=_accel_profile(plan, device="cpu", use_local=False),
        )

    # ---- 内存充足：默认本机 cpu + 1.7B ----
    use_local = True
    # device 委托 accel_plan（唯一真相源）：NVIDIA 显存 ≥4 GB → gpu，否则 cpu
    device = plan["local_llm.device"]
    tier = "1.7B"
    reasons.append("内存满足本地推理阈值（≥ 8 GB），推荐本机运行本地小 LLM")

    # ---- NVIDIA + 显存可得：按显存分档 ----
    if gpu_vendor == "nvidia" and vram_gb is not None:
        if vram_gb >= 10:
            tier = "8B"
        elif vram_gb >= 6:
            tier = "4B"
        elif vram_gb >= 4:
            tier = "1.7B"
        else:
            tier = "1.7B"  # 显存 < 4：维持 cpu + 1.7B，不上 GPU
        if vram_gb >= 4:
            # 内存上限约束：ram < 16 时档位最高不超过 4B
            if ram_gb < 16 and tier == "8B":
                tier = "4B"
                reasons.append("内存低于 16 GB，8B 档受内存上限约束，回落到 4B 档")
            reasons.append(
                f"检测到 NVIDIA GPU（显存约 {vram_gb:.1f} GB），推荐按 {tier} 档显卡推理"
            )
        else:
            reasons.append("显存小于 4 GB，不足以跑 GPU 档位，回退 CPU 推理")
    elif gpu_vendor == "nvidia":
        reasons.append("检测到 NVIDIA GPU 但显存未知，保守按 CPU 推理")
    else:
        reasons.append("未检测到可用于加速的独立 GPU，按 CPU 推理")

    # ---- 磁盘约束：可用空间 < 档位体积×1.05 → 逐级降档 ----
    if free_gb is not None:
        sizes = {name: _tier_size_gb(name) for name in _TIER_ORDER}
        if sizes.get("0.5B") is None:
            # 档位表尚未就绪（并行开发）→ 无法评估体积，不做降档，仅记说明
            note = "模型档位表未就绪，无法校验磁盘空间约束"
            notes.append(note)
            reasons.append(note)
        else:
            # 从大到小找第一个放得下的档位（缺失体积的档位视为放不下，保守）
            candidate = None
            for name in _TIER_ORDER:
                size = sizes.get(name)
                if size is not None and free_gb >= size * 1.05:
                    candidate = name
                    break
            if candidate is None:
                # 连最小档（0.5B）都不足 → 走云，给出所需 / 可用对比
                min_size = sizes["0.5B"]
                use_local = False
                tier = "0.5B"
                device = "cpu"
                reasons.append(
                    f"磁盘可用空间不足：所需至少 {min_size:.1f} GB（0.5B 档，含 5% 余量），"
                    f"实际可用 {free_gb:.1f} GB，建议先走云端"
                )
            elif _TIER_ORDER.index(candidate) > _TIER_ORDER.index(tier):
                # candidate 比当前档位更小 → 降档
                reasons.append(
                    f"磁盘可用空间（{free_gb:.1f} GB）不足以存放 {tier} 档（约 "
                    f"{sizes[tier]:.1f} GB × 1.05），回落到 {candidate} 档"
                )
                tier = candidate
    else:
        note = "磁盘可用空间未知，无法校验档位体积约束"
        notes.append(note)
        reasons.append(note)

    # 磁盘不足时 use_local 可能被翻转为 False → 本地 LLM / 嵌入落点回到 cpu
    if not use_local:
        device = "cpu"

    embedding_gpu = use_local and device == "gpu"
    config_patch = _build_config_patch(
        use_local=use_local, device=device, plan=plan, embedding_gpu=embedding_gpu,
    )

    return _make_recommendation(
        use_local=use_local, device=device, tier=tier,
        config_patch=config_patch, reasons=reasons, notes=notes,
        accel=_accel_profile(plan, device=device, use_local=use_local),
    )


def _tier_size_gb(tier: str) -> float | None:
    """取指定档位的预估体积（GB）；档位表缺失 / 该档不存在时返回 None。

    延迟导入 ``lite.runtime.model_downloader.MODEL_TIERS``（Task 5 才落地），
    避免本文件内联第二份档位表。
    """
    try:
        from lite.runtime.model_downloader import MODEL_TIERS  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - 并行开发期档位表尚未落地
        return None
    try:
        entry = MODEL_TIERS.get(tier)  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 - 档位表结构异常
        return None
    if not entry:
        return None
    try:
        size = float(entry.get("approximate_size_gb"))  # type: ignore[union-attr]
    except (AttributeError, TypeError, ValueError):
        return None
    return size if size and size > 0 else None


def _make_recommendation(use_local, device, tier, config_patch, reasons, notes, accel=None) -> dict:
    """组装推荐结果 dict 并填充 model 字段（延迟取档、失败降级）。

    :param accel: 加速剖面（:func:`_accel_profile` 产出）；缺省为空 dict 兜底。
    """
    model = None
    if use_local:
        model = _resolve_model(tier)
        if model is None:
            note = f"模型档位表未就绪或缺少 {tier} 档信息，模型详情暂缺（可在稍后下载阶段按档位选择）"
            notes.append(note)
            reasons.append(note)
    return {
        "use_local": use_local,
        "device": device,
        "tier": tier,
        "config_patch": config_patch,
        "model": model,
        "reasons": reasons,
        "probe_notes": notes,
        "accel": accel if accel is not None else {},
    }


def _resolve_model(tier: str):
    """按档位取模型详情（含 repo / filename / approximate_size_gb）。

    延迟导入 ``lite.runtime.model_downloader.MODEL_TIERS``；失败或该档不存在
    返回 None（不内联第二份档位表）。
    """
    try:
        from lite.runtime.model_downloader import MODEL_TIERS  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - 并行开发期档位表尚未落地
        return None
    try:
        entry = MODEL_TIERS.get(tier)  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 - 档位表结构异常
        return None
    if not isinstance(entry, dict):
        return None
    return {
        "tier": tier,
        "repo": entry.get("repo"),
        "filename": entry.get("filename"),
        "approximate_size_gb": entry.get("approximate_size_gb"),
        "family": entry.get("family"),
        "quant": entry.get("quant"),
    }