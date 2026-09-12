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


def build_gpu_dependency_commands(recommend, cuda_version=None):
    """按 GPU 检测结论装配加速依赖命令清单。

    清单条目均为可直接执行的命令字符串；以 ``# `` 开头的条目为说明性注释
    （引导用户手动完成 llama.cpp 特殊构建等步骤），自动安装时会被跳过。

    :param recommend: 检测结论 ``"cuda" | "rocm" | "cpu"``。
    :param cuda_version: 驱动报告的 CUDA 版本（如 "12.4"），仅用于注释说明。
    :return: list[str] 命令清单。
    """
    if recommend == "cuda":
        commands = [
            # 人工裁决（2026-09-12）：cuda 分支采用 cu128 新线（torch 最新构建线），
            # 覆盖 CUDA 12.8+ 运行时，适配新驱动（如 CUDA 13.x UMD 报告）；
            # cuda_version 写入注释而非硬编码进命令，保持命令可直接执行。
            f"# 检测到驱动 CUDA 版本：{cuda_version or '未知'}；下列 cu128 轮子面向 CUDA 12.8+ 运行时",
            "pip install torch --index-url https://download.pytorch.org/whl/cu128",
            "pip install onnxruntime-gpu",
            "pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu128 --force-reinstall --no-cache-dir",
        ]
    elif recommend == "rocm":
        commands = [
            "# PyTorch ROCm 官方轮子（Linux）；Windows 平台 AMD 建议改用 DirectML 后端",
            "pip install torch --index-url https://download.pytorch.org/whl/rocm6.0",
            "# Windows + AMD：onnxruntime 采用 DirectML 版本（替代 onnxruntime-gpu）",
            "pip install onnxruntime-directml",
            "# llama.cpp 建议使用 Vulkan 预编译构建（llama-*-bin-win-vulkan-x64.zip），解压至 data/local_llm/ 供本地推理调用",
        ]
    else:
        commands = [
            "pip install torch --index-url https://download.pytorch.org/whl/cpu",
            "pip install onnxruntime",
        ]
    return commands


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


def install_gpu_dependencies(report_path=None, auto_install=False, runner=None):
    """GPU 加速依赖检测与引导式安装（失败不阻断主安装）。

    流程：GpuDetector 检测 → :func:`build_gpu_dependency_commands` 装配清单 →
    写报告 → 依 ``auto_install`` 决定仅打印引导命令或逐条真实执行。

    :param report_path: 检测报告落盘绝对路径；缺省 ``<项目根>/data/install_report.json``。
    :param auto_install: False（默认）仅报告 + 打印待执行命令（引导式）；
        True 时逐条 subprocess 执行，单条失败仅 [WARN] 并记入 errors，不抛不阻断。
    :param runner: 命令执行器注入（检测与自动安装共用；测试 mock 入口）。
    :return: 报告 dict，字段：gpu_vendor / cuda_version / recommend /
        pending_commands / installed / timestamp（自动安装时附 errors 列表）。
    """
    # 1. 检测（runner 注入点贯穿检测与安装）
    detector = GpuDetector(runner=runner)
    report = detector.detect()
    _log_info(f"GPU 检测完成：{report['gpu_vendor']} → 推荐 {report['recommend']}（{report['details']}）")

    # 2. 依检测结论装配依赖清单
    commands = build_gpu_dependency_commands(report["recommend"], report.get("cuda_version"))
    result = {
        "gpu_vendor": report["gpu_vendor"],
        "cuda_version": report.get("cuda_version"),
        "recommend": report["recommend"],
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


def install(root=None):
    """一键安装编排：目录初始化 → 组件校验 → 内置组件落位 → 数据目录初始化。

    全程中文 [INFO] 提示。返回 （problems, builtin_warnings），供调用方展示或落盘。
    批次E：problems 为安装完成后复查 verify_components 的终态结果——刚落位的
    组件不再被误报为"待装态"；安装前快照仅用于过程中的告警输出。
    """
    root = root or PROJECT_ROOT
    ensure_dirs(root)
    # 安装前快照：仅用于过程告警输出（让用户知道安装前缺什么）
    for p in verify_components(root):
        _log_warn(p)
    builtin_warnings = install_builtin_assets(root)
    init_workplace(root)
    # Task H4：GPU 加速依赖检测（引导式，auto_install=False 不做真实安装，
    # 检测失败不阻断主安装）；报告随本次安装根落 data/install_report.json
    try:
        install_gpu_dependencies(report_path=os.path.join(root, "data", "install_report.json"))
    except Exception as exc:  # noqa: BLE001 - GPU 步骤失败绝不阻断主安装
        _log_warn(f"GPU 依赖检测步骤异常（已跳过，不阻断主安装）：{type(exc).__name__}: {exc}")
    # 批次E：安装后复查，保证返回报告反映安装后实况
    problems = verify_components(root)
    _log_info("一键安装流程完成。")
    return problems, builtin_warnings


if __name__ == "__main__":  # pragma: no cover - CLI 直跑入口
    install()