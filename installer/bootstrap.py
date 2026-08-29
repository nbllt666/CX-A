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
    # 批次E：安装后复查，保证返回报告反映安装后实况
    problems = verify_components(root)
    _log_info("一键安装流程完成。")
    return problems, builtin_warnings


if __name__ == "__main__":  # pragma: no cover - CLI 直跑入口
    install()