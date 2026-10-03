# -*- coding: utf-8 -*-
"""CX-A 安装引导（bootstrap）——目录初始化 / 组件校验 / 内置组件落位 / 数据目录初始化。

对齐工程文档 §4（无 GPU、不依赖外部服务）：
- 解压内置组件（Electron / llama.cpp / qwen3-embedding / LanceDB / MeloTTS / SenseVoice / 后端）
- 初始化数据目录
- 当前为开发态：组件二进制尚不可得，installer 实现"结构初始化 + 引导流程"，真实组件放置留目录占位。

路径规范：本项目所有路径均基于
``os.path.dirname(os.path.abspath(__file__))`` 逐级推导，禁止相对路径或字符串斜杠拼接。
本文件位于 ``<root>/installer/bootstrap.py``，上溯一级即项目根（c:\\CX-A）。
"""

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys

#: 本安装器目录（installer/），基于文件绝对位置推导，禁止相对路径。
_INSTALLER_DIR = os.path.dirname(os.path.abspath(__file__))
#: 项目根目录（installer 的直接上级）。
PROJECT_ROOT = os.path.dirname(_INSTALLER_DIR)

#: 内置组件源目录（installer/bundled/）。开发态仅供占位，真实二进制后续填充。
BUNDLED_DIR = os.path.join(_INSTALLER_DIR, "bundled")
#: 组件清单文件绝对路径。
MANIFEST_PATH = os.path.join(_INSTALLER_DIR, "manifest.json")

# CLI 直跑支持（MU1）：``python installer/bootstrap.py`` 直接执行时本模块不在包
# 上下文中，project_root 不会自动进入 sys.path。这里基于 __file__ 三级路径
# （文件 -> installer/ -> 项目根）推导项目根并显式注入，保证下方 import lite.* 可用。
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lite.config.config_manager import ConfigManager  # noqa: E402
from lite.config.download_sources import (  # noqa: E402
    CHANNELS,
    DEFAULT_CHANNEL,
    normalize_channel,
    pip_index_url,
)
from lite.memory.storage import MemoryStore  # noqa: E402

# Task H4：GPU 加速依赖检测与清单装配（失败不阻断主安装，故导入失败同样兜底）
try:
    from installer.gpu_detect import GpuDetector, default_runner as _gpu_default_runner  # noqa: E402
except ImportError:  # pragma: no cover - CLI 直跑包上下文缺失时的兜底路径
    from gpu_detect import GpuDetector, default_runner as _gpu_default_runner  # type: ignore[no-redef]  # noqa: E402


#: 数据目录相对项目根的子目录（与工程文档 §4 及 manifest install_target 对齐）。
REQUIRED_DATA_DIRS = (
    os.path.join("data", "lancedb"),
    os.path.join("data", "local_llm"),
    os.path.join("data", "voices"),
)


def _log_info(message):
    """以 [INFO] + 时间戳形式输出中文安装进度。"""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[INFO] {timestamp} {message}")


def _log_warn(message):
    """以 [WARN] + 时间戳形式输出中文风险提示。"""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[WARN] {timestamp} {message}")


# ------------------------------------------------------------------ #
# 清单加载                                                           #
# ------------------------------------------------------------------ #


def load_manifest(path=None):
    """加载组件清单 manifest.json，返回 dict。

    :param path: manifest.json 绝对路径；缺省使用 installer 内置清单。
    :return: 清单 dict（含 components 列表）。
    """
    path = path or MANIFEST_PATH
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ------------------------------------------------------------------ #
# 目录初始化                                                          #
# ------------------------------------------------------------------ #


def ensure_dirs(root):
    """创建数据目录（幂等）。

    创建 data/（memories.db、lancedb/、local_llm/、voices/）、logs/ 等数据目录。
    M-15（第三轮体检批次4）：子目录清单从 ``REQUIRED_DATA_DIRS`` 派生（单一
    真相源），消除与 verify_components 双份维护的漂移风险。
    重复调用不会报错或产生重复目录。

    :param root: 安装根目录（真实使用 PROJECT_ROOT，测试可传 tmp_path）。
    :return: 已确保存在的目录绝对路径列表。
    """
    root = root or PROJECT_ROOT
    _log_info("开始初始化数据目录...")
    dirs = [os.path.join(root, "data")]
    dirs.extend(os.path.join(root, rel) for rel in REQUIRED_DATA_DIRS)
    dirs.append(os.path.join(root, "logs"))
    for directory in dirs:
        os.makedirs(directory, exist_ok=True)

    # memories.db 占位（真实建表由 init_workplace 完成，此处仅为目录齐全性设占位文件）
    db_path = os.path.join(root, "data", "memories.db")
    if not os.path.exists(db_path):
        with open(db_path, "w", encoding="utf-8") as fh:
            fh.write("")
    _log_info(f"数据目录就绪：{', '.join(os.path.relpath(d, root) for d in dirs)} + data/memories.db")
    return dirs


# ------------------------------------------------------------------ #
# 组件校验                                                            #
# ------------------------------------------------------------------ #


def verify_components(root):
    """校验内置组件占位，返回问题/警告列表。

    - 必需数据目录（data/lancedb、data/local_llm、data/voices）缺失时追加问题；
    - 依据 manifest 逐项记录内置组件"安装态 / 待装态"——开发态二进制缺失时
      返回警告而非抛错，保证安装流程可继续。

    :param root: 安装根目录。
    :return: list[str] 问题/警告描述。
    """
    root = root or PROJECT_ROOT
    problems = []

    # 1. 必需数据目录占位校验
    for rel in REQUIRED_DATA_DIRS:
        if not os.path.isdir(os.path.join(root, rel)):
            problems.append(f"缺少必需数据目录：{rel}（请先运行 ensure_dirs）")

    # 2. 组件安装态 / 待装态记录（依据 manifest）
    manifest = load_manifest()
    for comp in manifest["components"]:
        target = os.path.join(root, comp["install_target"])
        # HP1 修复：目录型组件必须「非空」才判定已安装——裸 os.path.exists 会把
        # ensure_dirs 预建/重装残留的空占位目录误判为“已安装”。
        if os.path.isdir(target):
            installed = len(os.listdir(target)) > 0
        else:
            installed = os.path.exists(target)
        if comp["status"] == "builtin":
            state = "已安装" if installed else "待装态"
        else:
            state = "已安装" if installed else "可选未装"
        if comp["status"] == "builtin" and not installed:
            problems.append(
                f"内置组件[{comp['name']}]处于待装态：{comp['install_target']} 尚未就位（"
                f"size={comp.get('size_estimate')}）"
            )
        elif comp["status"] == "builtin" and installed:
            _log_info(f"组件[{comp['name']}] 已安装：{comp['install_target']}")

    return problems


# ------------------------------------------------------------------ #
# 内置组件落位                                                        #
# ------------------------------------------------------------------ #


def _under_data_prefix(dst, root):
    """判定 dst 是否位于 ``<root>/data`` 运行数据前缀之下。

    :param dst: 目标路径（绝对）。
    :param root: 安装根目录。
    :return: True 表示 dst 属于 data/ 运行数据前缀（如 data/lancedb、data/voices/x）。
    """
    try:
        rel = os.path.normpath(os.path.relpath(dst, root))
    except ValueError:
        # Windows 跨盘符等无法求相对路径的情形，保守判定为非运行数据前缀
        return False
    return rel == "data" or rel.startswith("data" + os.sep)


def _is_nonempty_dir(path):
    """目录存在且至少含一项内容时返回 True（listdir 判定，与 verify_components 同口径）。"""
    return os.path.isdir(path) and len(os.listdir(path)) > 0


def _copytree(src, dst, root=None):
    """递归拷贝 src 到 dst；目标已存在则先移除再拷贝（保证结果确定性）。

    运行数据目录保护（HP1）：当 dst 属于 ``<root>/data`` 运行数据前缀、且已存在
    非空内容（用户向量库 / 本地模型 / 自定义音色等）时，跳过拷贝并告警
    “检测到已有运行数据，跳过覆盖”，防止重跑安装器擦除既有数据；
    普通全新落位行为不变。

    :param src: 源目录绝对路径。
    :param dst: 目标目录绝对路径。
    :param root: 安装根目录（用于 data/ 前缀判定）；缺省用真实项目根 PROJECT_ROOT。
    """
    if _under_data_prefix(dst, root or PROJECT_ROOT) and _is_nonempty_dir(dst):
        _log_warn(f"检测到已有运行数据，跳过覆盖：{dst}")
        return
    if os.path.exists(dst):
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        else:
            os.remove(dst)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copytree(src, dst)


def install_builtin_assets(root, manifest=None):
    """将"内置组件"从 installer/bundled/ 拷贝/解压到数据目录。

    按 manifest.json 中 status=builtin 的组件，把 bundled/<key> 拷贝到
    <root>/<install_target>。真实二进制缺失时仅记录警告，不失败（开发态允许）。

    :param root: 安装根目录。
    :param manifest: 组件清单 dict；缺省加载 installer 内置清单。
    :return: list[str] 缺失源组件警告。
    """
    root = root or PROJECT_ROOT
    manifest = manifest or load_manifest()
    warnings = []
    for comp in manifest["components"]:
        if comp["status"] != "builtin":
            continue
        key = comp["key"]
        src = os.path.join(BUNDLED_DIR, key)
        dst = os.path.join(root, comp["install_target"])
        if not os.path.exists(src):
            warnings.append(f"内置组件[{comp['name']}]源缺失：{src}（开发态跳过，不失败）")
            _log_warn(f"[跳过] {comp['name']} 源缺失，标记待装态")
            continue
        if os.path.isdir(src):
            _copytree(src, dst, root=root)
        else:
            # 批次E：文件型组件与目录型同口径的运行数据保护——dst 已存在且位于
            # data/ 运行数据前缀下时跳过覆盖并告警，防止未来组件擦除用户数据
            if _under_data_prefix(dst, root) and os.path.exists(dst):
                _log_warn(f"检测到已有运行数据，跳过覆盖：{dst}")
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
        _log_info(f"[安装] {comp['name']} -> {comp['install_target']}")

    if warnings:
        _log_info(f"共 {len(warnings)} 项内置组件源缺失（开发态待装，不影响安装流程整体完成）")
    else:
        _log_info("全部内置组件落位完成。")
    return warnings


# ------------------------------------------------------------------ #
# 数据目录初始化（config.json + memories.db）                           #
# ------------------------------------------------------------------ #


def init_workplace(root):
    """初始化工作区：生成 config.json 并完成 memories.db 建表。

    - 通过 ConfigManager 首次启动自动生成含默认值的 config.json；
    - 通过 MemoryStore.create_table() 触发 memories 表建表。

    :param root: 安装根目录。
    :return: 已初始化好的 ConfigManager 实例。
    """
    root = root or PROJECT_ROOT
    cfg = ConfigManager(
        config_path=os.path.join(root, "config.json"),
        data_dir=os.path.join(root, "data"),
    )
    _log_info("config.json 已生成 / 加载（默认云端提供商：%s）" % cfg.get("cloud", "provider"))

    store = MemoryStore(db_path=os.path.join(root, "data", "memories.db"))
    store.create_table()
    store.close()
    _log_info("memories.db 建表完成（memories 表就绪）")
    return cfg


# ------------------------------------------------------------------ #
# GPU 加速依赖（Task H4）：检测 → 清单装配 → 引导式安装                  #
# ------------------------------------------------------------------ #


#: ORT（onnxruntime）变体包名——四者同一 import 名 ``onnxruntime``，同一 Runtime
#: 只能装一个变体（互斥）；分叉装配的唯一真相源见 :func:`resolve_ort_package`。
ORT_PACKAGE_CPU = "onnxruntime"
ORT_PACKAGE_GPU = "onnxruntime-gpu"
ORT_PACKAGE_DIRECTML = "onnxruntime-directml"
#: Linux ROCm 变体（20261002 批 A：``platform`` 为 Linux 且画像指向 AMD/ROCm
#: 时产出；Windows 行为不受影响——ROCm EP 缺失时由 bridge 既有回退链兜底）。
ORT_PACKAGE_ROCM = "onnxruntime-rocm"


def resolve_ort_package(profile, platform=None):
    """按加速画像决定 ORT 变体包名（唯一分叉口径）。

    映射（spec「安装链」/ 裁决①，Windows 现实路径）：

    - 有核显（任意独显组合）→ ``onnxruntime-directml``（双模式前提）；
    - 无核显 NVIDIA（``recommend=cuda``）→ ``onnxruntime-gpu``；
    - 无核显 AMD（Windows，``recommend=rocm``）→ ``onnxruntime-directml``；
    - 无可用 GPU（``recommend=cpu``）→ ``onnxruntime``（CPU 兜底）。

    **Linux 例外**（20261002 批 A）：``platform`` 以 ``"linux"`` 开头且画像指向
    AMD / ROCm（``recommend=rocm`` 或 ``gpu_vendor`` / ``dgpu_vendor`` 为
    ``amd``）→ ``onnxruntime-rocm``（DirectML 为 Windows 专有技术，Linux 无
    DML 路径）。Linux 真机未验证，代码路径 + 单测覆盖口径。

    :param profile: 画像 dict（``has_igpu`` / ``recommend`` / ``gpu_vendor`` /
        ``dgpu_vendor``）；非法 / 缺失一律按无 GPU 保守回退 CPU 版。
    :param platform: 平台标识（``sys.platform`` 同构串）；None 缺省取当前
        ``sys.platform``（Windows 构建链不受影响，历史行为逐字不变）。
    :return: 变体包名字符串。
    """
    profile = profile if isinstance(profile, dict) else {}
    sys_platform = sys.platform if platform is None else str(platform)
    is_linux = sys_platform.startswith("linux")
    recommend = str(profile.get("recommend") or "")
    vendor = str(profile.get("gpu_vendor") or "")
    dgpu_vendor = profile.get("dgpu_vendor")
    has_amd_igpu = any(
        isinstance(g, dict)
        and g.get("type") == "igpu"
        and str(g.get("vendor") or "").strip().lower() == "amd"
        for g in (profile.get("gpus") or [])
    )
    if is_linux and (
        recommend == "rocm"
        or dgpu_vendor == "amd"
        or vendor == "amd"
        or has_amd_igpu
    ):
        return ORT_PACKAGE_ROCM
    if bool(profile.get("has_igpu")):
        return ORT_PACKAGE_DIRECTML
    if recommend == "cuda" or dgpu_vendor == "nvidia" or vendor == "nvidia":
        return ORT_PACKAGE_GPU
    if recommend == "rocm" or dgpu_vendor in ("amd", "intel") or vendor == "amd":
        return ORT_PACKAGE_DIRECTML
    return ORT_PACKAGE_CPU


def build_ort_package_command(profile, channel=None, platform=None):
    """按画像装配 ORT 变体安装命令（与 torch 链解耦的单一分叉入口）。

    :param profile: 画像 dict（见 :func:`resolve_ort_package`）。
    :param channel: 统一下载源通道；``"mirror"`` 时对普通 pip 包追加国内索引
        （``None`` / ``"official"`` 与历史逐字相同，不追加）。
    :param platform: 平台标识透传 :func:`resolve_ort_package`（None 缺省当前平台）。
    :return: 形如 ``pip install onnxruntime-directml`` 的命令字符串。
    """
    command = f"pip install {resolve_ort_package(profile, platform=platform)}"
    index_url = pip_index_url(channel) if channel is not None else None
    if index_url:
        command = _append_pip_index(command, index_url)
    return command


def _ort_profile_for(recommend, has_igpu=None, profile=None):
    """组装 ORT 分叉所需的最小画像；未提供画像信息时返回 None（不装配 ORT）。

    未传 ``profile`` / ``has_igpu`` 时保持最简旧语义（仅 torch 链，不捆绑 ORT），
    保证既有调用方（``channel=None`` 逐字不变）与下游装配不被破坏。
    """
    if isinstance(profile, dict):
        merged = dict(profile)
        merged.setdefault("recommend", recommend)
        return merged
    if has_igpu is not None:
        return {"recommend": recommend, "has_igpu": bool(has_igpu)}
    return None


def build_gpu_dependency_commands(recommend, cuda_version=None, channel=None,
                                  has_igpu=None, profile=None, platform=None):
    """按 GPU 检测结论装配加速依赖命令清单（torch 链 + ORT 分叉）。

    清单条目均为可直接执行的命令字符串；以 ``# `` 开头的条目为说明性注释
    （引导用户手动完成 llama.cpp 特殊构建等步骤），自动安装时会被跳过。

    **解耦（防分叉失效）**：ORT 包（``onnxruntime*``）不再随 cuda / rocm / cpu
    分支捆绑——三分支只保留 torch / torchaudio / llama-cpp 链；ORT 变体改由画像经
    :func:`resolve_ort_package` 统一装配（同一 import 名互斥，避免 DML / CPU 机器被
    同 import 名的 ``onnxruntime-gpu`` 覆盖）。未提供 ``profile`` / ``has_igpu`` 时
    只输出 torch 链（保持最简旧语义）。

    Task 9（统一下载源）：``channel`` 为 ``"mirror"`` 时，仅对**普通 pip 包**
    （``onnxruntime`` / ``onnxruntime-gpu`` / ``onnxruntime-directml``）在命令末尾
    追加 ``-i <国内镜像索引>``（索引 URL 经
    ``lite.config.download_sources.pip_index_url`` 派生，禁止硬编码）；
    ``torch``（pytorch 官方轮子源）与 ``llama-cpp-python``（abetlen 专用源）
    命令一字不改——镜像站不代理这两类 whl。``channel=None``（未指定）与
    ``"official"`` 输出不含任何镜像参数。

    :param recommend: 检测结论 ``"cuda" | "rocm" | "cpu"``。
    :param cuda_version: 驱动报告的 CUDA 版本（如 "12.4"），仅用于注释说明。
    :param channel: 统一下载源通道（``"mirror"`` / ``"official"``）；
        ``None`` 表示未指定，保持既有输出不变。
    :param has_igpu: 是否含核显（``None`` 表示未提供画像，不装配 ORT）。
    :param profile: 硬件画像 dict（优先于 ``has_igpu``）。
    :return: list[str] 命令清单。
    """
    if recommend == "cuda":
        commands = [
            # 人工裁决（2026-09-12）：cuda 分支采用 cu128 新线（torch 最新构建线），
            # 覆盖 CUDA 12.8+ 运行时，适配新驱动（如 CUDA 13.x UMD 报告）；
            # cuda_version 写入注释而非硬编码进命令，保持命令可直接执行。
            f"# 检测到驱动 CUDA 版本：{cuda_version or '未知'}；下列 cu128 轮子面向 CUDA 12.8+ 运行时",
            "pip install torch --index-url https://download.pytorch.org/whl/cu128",
            # torchaudio 与 torch 同版本族、同源（MeloTTS / funasr 的硬依赖；
            # 2026-09-25 实装发现：缺它则 sidecar 内 melo.api 导入即失败）
            "pip install torchaudio --index-url https://download.pytorch.org/whl/cu128",
            "pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu128 --force-reinstall --no-cache-dir",
        ]
    elif recommend == "rocm":
        sys_platform = sys.platform if platform is None else str(platform)
        if sys_platform.startswith("linux"):
            commands = [
                "# PyTorch ROCm 官方轮子（仅 Linux 有 ROCm 轮子）",
                "pip install torch --index-url https://download.pytorch.org/whl/rocm6.0",
                "pip install torchaudio --index-url https://download.pytorch.org/whl/rocm6.0",
            ]
        else:
            # Windows 无 PyTorch ROCm 轮子（ROCm 构建仅面向 Linux）：AMD 机器
            # torch 侧回退 CPU 轮子（语音识别 CPU；TTS 加速走 DirectML，由
            # ORT 画像分叉装配；llama.cpp 走 Vulkan 构建），避免必失败的
            # rocm6.0 安装命令阻断安装链（20261002 用户问询发现）
            commands = [
                "# Windows 平台无 PyTorch ROCm 轮子：torch 侧回退 CPU"
                "（TTS 加速走 DirectML / llama.cpp 走 Vulkan，见 ORT 分叉与注释）",
                "pip install torch --index-url https://download.pytorch.org/whl/cpu",
                "pip install torchaudio --index-url https://download.pytorch.org/whl/cpu",
            ]
    else:
        commands = [
            "pip install torch --index-url https://download.pytorch.org/whl/cpu",
            "pip install torchaudio --index-url https://download.pytorch.org/whl/cpu",
        ]

    # ORT 分叉（与 torch 链解耦）：画像齐备时统一装配唯一变体（互斥）
    ort_profile = _ort_profile_for(recommend, has_igpu=has_igpu, profile=profile)
    if ort_profile is not None:
        commands.append(build_ort_package_command(ort_profile))

    # 说明性注释（llama.cpp 特殊构建等；自动安装跳过）
    if recommend == "rocm":
        commands.append(
            "# llama.cpp 建议使用 Vulkan 预编译构建（llama-*-bin-win-vulkan-x64.zip），"
            "解压至 data/local_llm/ 供本地推理调用"
        )

    # channel 未指定（None）时不做任何改写；official 经 pip_index_url 派生为 None，
    # 同样不追加——两种情形输出与历史版本逐字相同。
    index_url = pip_index_url(channel) if channel is not None else None
    if index_url:
        commands = [_append_pip_index(cmd, index_url) for cmd in commands]
    return commands


#: 镜像通道下追加 ``-i <国内索引>`` 的目标包（普通 pip 包）。torch 走 pytorch 官方
#: 轮子源、llama-cpp-python 走 abetlen 专用源，镜像站不代理其 whl，故不在此列。
#: （20261002 批 A：追加 ``onnxruntime-rocm``——Linux ROCm 变体同为普通 pip 包。）
_MIRROR_ELIGIBLE_PACKAGES = (
    "onnxruntime", "onnxruntime-gpu", "onnxruntime-directml", "onnxruntime-rocm",
)


def _append_pip_index(command, index_url):
    """对普通 pip 包安装命令追加 ``-i <index_url>``，其余命令原样返回。

    仅改写形如 ``pip install <包名> ...`` 且包名属于
    :data:`_MIRROR_ELIGIBLE_PACKAGES` 的命令；说明性注释（``#`` 开头）与
    torch / llama-cpp-python 专用源命令保持不变。

    :param command: 单条命令字符串。
    :param index_url: 待追加的索引源 URL（如清华 PyPI 镜像）。
    :return: 改写后的命令字符串（不满足条件时返回原字符串）。
    """
    parts = command.split()
    if len(parts) >= 3 and parts[0] == "pip" and parts[1] == "install" \
            and parts[2] in _MIRROR_ELIGIBLE_PACKAGES:
        return f"{command} -i {index_url}"
    return command


def _default_install_runner(cmd):
    """自动安装的默认命令执行器：``cmd -> (returncode, 合并输出)``。

    与检测 runner 分离：安装型命令（pip 大包下载）耗时远长于检测命令，
    超时放宽至 3600s；同样注入 CREATE_NO_WINDOW 且永不抛异常。
    """
    creationflags = 0x08000000 if sys.platform == "win32" else 0
    try:
        completed = subprocess.run(
            cmd.split(),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=3600,
            creationflags=creationflags,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        return completed.returncode, output
    except Exception as exc:  # noqa: BLE001 - 安装失败不阻断主安装
        return -1, f"{type(exc).__name__}: {exc}"


def _build_accel_profile(report, detector, runner=None):
    """组装用于 ORT 分叉的最小画像 dict。

    核显枚举仅在 ``runner is None``（生产默认链）时执行——注入 runner（测试替身 /
    自定义执行器）时不做真实硬件探测，避免污染注入通道；枚举失败降级「无核显」。
    """
    has_igpu, dgpu_vendor = False, None
    if runner is None:
        try:
            summary = detector.detect_inventory(runner=runner)
            has_igpu = bool(summary.get("has_igpu"))
            dgpu_vendor = summary.get("dgpu_vendor")
        except Exception as exc:  # noqa: BLE001 - 枚举失败按无核显保守继续
            _log_warn(f"GPU 清单枚举失败（按无核显继续 ORT 分叉）：{type(exc).__name__}: {exc}")
    return {
        "recommend": report.get("recommend"),
        "gpu_vendor": report.get("gpu_vendor"),
        "cuda_version": report.get("cuda_version"),
        "has_igpu": has_igpu,
        "dgpu_vendor": dgpu_vendor,
    }


def install_gpu_dependencies(report_path=None, auto_install=False, runner=None,
                            channel=None, profile=None):
    """GPU 加速依赖检测与引导式安装（失败不阻断主安装）。

    流程：GpuDetector 检测 →（核显枚举）→ :func:`build_gpu_dependency_commands`
    装配「torch 链 + ORT 分叉」清单 → 写报告 → 依 ``auto_install`` 决定仅打印
    引导命令或逐条真实执行。

    :param report_path: 检测报告落盘绝对路径；缺省 ``<项目根>/data/install_report.json``。
    :param auto_install: False（默认）仅报告 + 打印待执行命令（引导式）；
        True 时逐条 subprocess 执行，单条失败仅 [WARN] 并记入 errors，不抛不阻断。
    :param runner: 命令执行器注入（检测与自动安装共用；测试 mock 入口）。
    :param channel: 统一下载源通道（Task 9）；透传给命令装配。``None``（默认）
        保持既有行为——不读配置、不追加镜像索引，输出与历史版本逐字相同。
    :param profile: 显式画像 dict（覆盖内核显枚举结果）；``None`` 时由检测报告与
        核显枚举组装。
    :return: 报告 dict，字段：gpu_vendor / cuda_version / recommend / has_igpu /
        ort_package / pending_commands / installed / timestamp（自动安装时附 errors）。
    """
    # 1. 检测（runner 注入点贯穿检测与安装）
    detector = GpuDetector(runner=runner)
    report = detector.detect()
    _log_info(f"GPU 检测完成：{report['gpu_vendor']} → 推荐 {report['recommend']}（{report['details']}）")

    # 1b. 画像增量（核显识别）：供 ORT 包分叉
    if not isinstance(profile, dict):
        profile = _build_accel_profile(report, detector, runner=runner)

    # 2. 依检测结论 + 画像装配依赖清单（torch 链 + ORT 分叉）
    commands = build_gpu_dependency_commands(
        report["recommend"], report.get("cuda_version"), channel=channel, profile=profile
    )
    result = {
        "gpu_vendor": report["gpu_vendor"],
        "cuda_version": report.get("cuda_version"),
        "recommend": report["recommend"],
        "has_igpu": bool(profile.get("has_igpu")),
        "ort_package": resolve_ort_package(profile),
        "pending_commands": list(commands),
        "installed": False,
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    # 3a. 引导式（默认）：仅打印待执行命令，不做真实安装
    if not auto_install:
        for cmd in commands:
            _log_info(f"[GPU 引导] 待执行命令：{cmd}")
        _log_info("[GPU 引导] 依赖安装为引导式：请手动执行上述命令，或启用自动安装后重跑。")
    # 3b. 自动安装：逐条执行；说明性注释（# 开头）跳过；失败仅告警不阻断
    else:
        exec_runner = runner if runner is not None else _default_install_runner
        errors = []
        executed = 0
        for cmd in commands:
            if cmd.startswith("#"):
                _log_info(f"[GPU 说明] {cmd.lstrip('# ')}")
                continue
            executed += 1
            _log_info(f"[GPU 安装] 正在执行：{cmd}")
            returncode, output = exec_runner(cmd)
            if returncode != 0:
                _log_warn(f"[GPU 安装] 命令执行失败（不阻断主安装）：{cmd}")
                errors.append({"command": cmd, "returncode": returncode, "output": output[-500:]})
        if executed > 0 and not errors:
            result["installed"] = True
        result["errors"] = errors

    # 4. 报告落盘（路径基于 __file__ 推导项目根，禁止相对路径）
    path = report_path or os.path.join(PROJECT_ROOT, "data", "install_report.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    _log_info(f"GPU 依赖检测报告已写入：{path}")
    return result


# ------------------------------------------------------------------ #
# 一键安装编排                                                        #
# ------------------------------------------------------------------ #


def resolve_download_channel(root=None, channel=None, config_manager=None):
    """解析本次安装使用的统一下载源通道（镜像 / 官方）。

    优先级（Task 9「安装期判断镜像源 / 官方源」）：

    1. 显式 ``channel`` 入参 → 经
       :func:`lite.config.download_sources.normalize_channel` 归一后使用；
    2. ``<root>/config.json`` 的 ``download.channel``（与前端首启向导、
       ``/api/setup/status`` 读取同一配置键，不存在第二套真相）；
    3. 仍读不到（文件不存在 / 解析失败 / 配置对象异常）→
       :data:`DEFAULT_CHANNEL`（即 ``"mirror"``）。

    任何读取异常均回落默认通道，绝不抛错——安装流程不得因配置问题中断。

    :param root: 安装根目录；缺省用真实项目根 PROJECT_ROOT。
    :param channel: 显式通道取值；``None`` 表示未显式指定。
    :param config_manager: 已构造的 ConfigManager（如 init_workplace 的返回值）；
        缺省按 ``root`` 新建。
    :return: 归一化后的通道字符串（``"mirror"`` 或 ``"official"``）。
    """
    if channel is not None:
        return normalize_channel(channel)
    root = root or PROJECT_ROOT
    try:
        cfg = config_manager or ConfigManager(
            config_path=os.path.join(root, "config.json"),
            data_dir=os.path.join(root, "data"),
        )
        return normalize_channel(cfg.get("download", "channel", DEFAULT_CHANNEL))
    except Exception as exc:  # noqa: BLE001 - 配置不可读一律回落默认通道
        _log_warn(
            f"下载通道配置读取失败（已回落默认通道 {DEFAULT_CHANNEL}）："
            f"{type(exc).__name__}: {exc}"
        )
        return DEFAULT_CHANNEL


def install(root=None, channel=None):
    """一键安装编排：目录初始化 → 组件校验 → 内置组件落位 → 数据目录初始化 → GPU 依赖装配。

    全程中文 [INFO] 提示。返回 （problems, builtin_warnings），供调用方展示或落盘。
    批次E：problems 为安装完成后复查 verify_components 的终态结果——刚落位的
    组件不再被误报为"待装态"；安装前快照仅用于过程中的告警输出。

    Task 9：``channel`` 为统一下载源通道（镜像 / 官方）。未显式给出时经
    :func:`resolve_download_channel` 读 ``<root>/config.json`` 的
    ``download.channel``，仍无则默认 ``mirror``；解析结果显式传给 GPU 依赖装配，
    镜像通道下普通 pip 包追加国内索引（pytorch / llama-cpp-python 专用源不变）。

    :param root: 安装根目录；缺省用真实项目根 PROJECT_ROOT。
    :param channel: 统一下载源通道；``None``（默认）时按上述优先级解析。
    :return: (problems, builtin_warnings) 二元组（既有契约不变）。
    """
    root = root or PROJECT_ROOT
    ensure_dirs(root)
    # 安装前快照：仅用于过程告警输出（让用户知道安装前缺什么）
    for p in verify_components(root):
        _log_warn(p)
    builtin_warnings = install_builtin_assets(root)
    cfg = init_workplace(root)
    # Task 9：安装期解析统一下载源通道（显式入参 → root/config.json → 默认镜像）
    resolved_channel = resolve_download_channel(root, channel, config_manager=cfg)
    _log_info(
        f"依赖安装使用下载通道：{resolved_channel}"
        f"（mirror=国内镜像加速 / official=官方直连，取自 {'命令行参数' if channel is not None else 'config.json'}）"
    )
    # Task H4：GPU 加速依赖检测（引导式，auto_install=False 不做真实安装，
    # 检测失败不阻断主安装）；报告随本次安装根落 data/install_report.json
    try:
        install_gpu_dependencies(
            report_path=os.path.join(root, "data", "install_report.json"),
            channel=resolved_channel,
        )
    except Exception as exc:  # noqa: BLE001 - GPU 步骤失败绝不阻断主安装
        _log_warn(f"GPU 依赖检测步骤异常（已跳过，不阻断主安装）：{type(exc).__name__}: {exc}")
    # 批次E：安装后复查，保证返回报告反映安装后实况
    problems = verify_components(root)
    _log_info("一键安装流程完成。")
    return problems, builtin_warnings


def main(argv=None):
    """CLI 入口：``python installer/bootstrap.py [--channel mirror|official]``。

    ``--channel`` 缺省为 ``None``，交由 :func:`install` 按 ``config.json`` 的
    ``download.channel`` 解析（仍无则默认镜像通道）。

    :param argv: 命令行参数列表；缺省取 ``sys.argv[1:]``。
    :return: :func:`install` 的 ``(problems, builtin_warnings)`` 二元组。
    """
    parser = argparse.ArgumentParser(
        description="CX-A 一键安装引导（目录初始化 / 组件校验 / 内置组件落位 / 依赖装配）"
    )
    parser.add_argument(
        "--channel",
        choices=list(CHANNELS),
        default=None,
        help="统一下载源通道：mirror=国内镜像加速（默认），official=官方直连；缺省读 config.json",
    )
    args = parser.parse_args(argv)
    return install(channel=args.channel)


if __name__ == "__main__":  # pragma: no cover - CLI 直跑入口
    main()