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
- 全部路径基于 ``os.path.abspath(__file__)`` 推导，禁止相对路径与 ``../../``；
- 可选依赖（psutil / requests 等）统一 try/except ImportError 降级；
- 探测类函数不打印噪声，失败信息进 ``probe_notes``；
- 依赖外部命令一律经 ``runner`` 注入点，测试可完全 mock，不触碰真实硬件。
"""

import os
import re
import shutil
import subprocess
import sys

#: 运行时目录（lite/runtime/），基于文件绝对位置推导，禁止相对路径。
_RUNTIME_DIR = os.path.dirname(os.path.abspath(__file__))

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
        （中文说明，空表示探测全部成功）。
    """
    exe = runner if runner is not None else default_runner
    notes: list[str] = []

    #: 安装根：优先入参，其次基于本文件上溯（lite/runtime → lite → 项目根）。
    if root is None:
        root = os.path.dirname(os.path.dirname(_RUNTIME_DIR))

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
        "probe_notes": notes,
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
        ``config_patch`` / ``model`` / ``reasons`` / ``probe_notes``。
    """
    notes: list[str] = list(profile.get("probe_notes") or [])
    reasons: list[str] = []
    config_patch: dict = {}

    ram_gb = profile.get("ram_gb")
    gpu_vendor = profile.get("gpu_vendor")
    vram_gb = profile.get("vram_gb")
    free_gb = disk_free_gb if disk_free_gb is not None else profile.get("disk_free_gb")

    # ---- 内存不可得：未知，走云保守处理 ----
    if ram_gb is None:
        note = "内存总量未知，无法评估本地推理可行性，建议先走云端"
        notes.append(note)
        reasons.append(note)
        return _make_recommendation(
            use_local=False, device="cpu", tier="0.5B",
            config_patch={"local_llm": {"enabled": False, "device": "cpu"}},
            reasons=reasons, notes=notes,
        )

    # ---- 内存不足：走云 ----
    if ram_gb < 8:
        reason = f"内存仅 {ram_gb:.1f} GB（低于本地推理阈值 8 GB），建议走云端"
        reasons.append(reason)
        return _make_recommendation(
            use_local=False, device="cpu", tier="0.5B",
            config_patch={"local_llm": {"enabled": False, "device": "cpu"}},
            reasons=reasons, notes=notes,
        )

    # ---- 内存充足：默认本机 cpu + 1.7B ----
    use_local = True
    device = "cpu"
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
            device = "gpu"
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

    if not use_local:
        config_patch = {"local_llm": {"enabled": False, "device": device}}
    else:
        config_patch = {"local_llm": {"enabled": True, "device": device}}
        if device == "gpu":
            config_patch["embedding"] = {"device": "gpu"}

    return _make_recommendation(
        use_local=use_local, device=device, tier=tier,
        config_patch=config_patch, reasons=reasons, notes=notes,
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


def _make_recommendation(use_local, device, tier, config_patch, reasons, notes) -> dict:
    """组装推荐结果 dict 并填充 model 字段（延迟取档、失败降级）。"""
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