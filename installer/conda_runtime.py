# -*- coding: utf-8 -*-
"""内置 Miniconda 运行时与语音 sidecar 环境安装（installer/conda_runtime.py）。

人类裁决（2026-09-25）：「安装程序应该安装一个内置 miniconda 环境」——
MeloTTS（卡 tokenizers<0.14 无 cp314 轮子）与 llama-cpp-python（无 cp314 轮子）
在项目主环境（Python 3.14）均装不上，故以内置 conda 建独立 Python 3.10
sidecar 环境（``runtime/voice``）承载语音真引擎与本地推理。

职责边界（本模块只做「环境装配」，不做「引擎接线」）：
1. :func:`install_conda`——Miniconda NSIS 安装器静默装到 ``<root>/runtime/conda``；
2. :func:`create_voice_env`——``conda create -p`` 建 ``<root>/runtime/voice``（Python 3.10）。

设计约束（与 installer 既有口径对齐）：
- 全中文 [INFO]/[WARN] 输出并带时间戳；路径全部由入参/``__file__`` 推导，禁止相对路径；
- 命令执行经 ``runner(cmd) -> (returncode, output)`` 注入，单测完全 mock、绝不触网；
- 任何一步失败**不抛错、不阻断主安装**（与 bootstrap GPU 装配同口径），
  以告警 + 结构化结果返回，由调用方决定展示；
- 幂等：目标已就位（``conda.exe`` / 环境 ``python.exe`` 存在）直接跳过；
- conda 通道经 ``--override-channels -c <tuna>`` 参数化传入，
  **不读写用户全局 .condarc**（便携原则：不污染用户机器）。

发现记录（2026-09-25）：Miniconda 安装器**不得使用无扩展名文件名**——
``Start-Process``/ShellExecute 对无扩展名文件报「找不到所需信息」，
故统一为 ``miniconda_installer.exe``（见规划 §四-7）。
"""

import contextlib
import datetime
import json
import os
import shutil
import subprocess
import sys

#: 本安装器目录（installer/），基于文件绝对位置推导，禁止相对路径。
_INSTALLER_DIR = os.path.dirname(os.path.abspath(__file__))
#: 项目根目录（installer 的直接上级）。开发态下与「便携根」同义。
PROJECT_ROOT = os.path.dirname(_INSTALLER_DIR)
#: 内置组件源目录（installer/bundled/）。
BUNDLED_DIR = os.path.join(_INSTALLER_DIR, "bundled")

#: Miniconda 安装器文件名（bundled/ 下的源；统一 .exe 后缀，见模块 docstring）。
CONDA_INSTALLER_NAME = "miniconda_installer.exe"
#: conda 运行时相对落点（与 manifest 登记的 install_target 一致）。
CONDA_DIR_REL = os.path.join("runtime", "conda")
#: 语音 sidecar 环境相对落点。
VOICE_ENV_DIR_REL = os.path.join("runtime", "voice")
#: sidecar 环境 Python 版本（3.10：MeloTTS 钉 ``transformers==4.27.4``，其依赖
#: ``tokenizers<0.14`` 在 3.11+/3.12+ 无对应轮子（只有 cp310 及更早），源码构建
#: 需 Rust 工具链——本机实测 3.12 装 tokenizers 0.13.x 直接失败（2026-09-25），
#: 故 sidecar 固定 3.10（MeloTTS / funasr 生态成熟版本线）。
VOICE_PYTHON_VERSION = "3.10"
#: conda 通道（清华 anaconda 镜像；``--override-channels`` 下不读用户 .condarc）。
CONDA_MIRROR_MAIN = "https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main"
#: runner 默认超时（秒）：NSIS 安装与 conda create 均可能分钟级。
DEFAULT_TIMEOUT_S = 1800


def _log_info(message):
    """以 [INFO] + 时间戳形式输出中文安装进度。"""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[INFO] {timestamp} {message}")


def _log_warn(message):
    """以 [WARN] + 时间戳形式输出中文风险提示。"""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[WARN] {timestamp} {message}")


def default_runner(cmd, timeout=DEFAULT_TIMEOUT_S):
    """默认命令执行器：``cmd -> (returncode, 合并输出)``。

    与 ``bootstrap._default_install_runner`` 同口径：Windows 下注入
    ``CREATE_NO_WINDOW`` 避免安装期弹出控制台窗口；捕获全部异常降级为
    ``(-1, 描述)``，永不抛错（安装失败不阻断主安装）。

    :param cmd: 命令参数列表（如 ``[installer, "/S", "/D=<target>"]``）。
    :param timeout: 超时秒数（NSIS 安装 / conda create 均放宽）。
    :return: ``(returncode, 合并输出文本)``
    """
    creationflags = 0x08000000 if sys.platform == "win32" else 0
    try:
        completed = subprocess.run(
            list(cmd),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            creationflags=creationflags,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        return completed.returncode, output
    except Exception as exc:  # noqa: BLE001 - 安装失败不阻断主安装
        return -1, f"{type(exc).__name__}: {exc}"


def conda_exe_path(root):
    """推导内置 conda 的可执行文件路径。

    :param root: 安装根（便携根）。
    :return: ``<root>/runtime/conda/Scripts/conda.exe`` 绝对路径。
    """
    return os.path.join(root, CONDA_DIR_REL, "Scripts", "conda.exe")


def voice_env_python(root):
    """推导语音 sidecar 环境的 Python 解释器路径。

    :param root: 安装根（便携根）。
    :return: ``<root>/runtime/voice/python.exe`` 绝对路径。
    """
    return os.path.join(root, VOICE_ENV_DIR_REL, "python.exe")


def find_bundled_source(root, name, bundled_dir=None):
    """按优先级查找随包组件源（文件或目录）。

    1. 显式 ``bundled_dir``（测试注入 / 调用方指定）；
    2. ``<root>/runtime/_bundled/<name>``（随包分发落点，用户机器侧）；
    3. ``installer/bundled/<name>``（开发态 / 构建机源）。

    用户机器上不存在 ``installer/bundled``（安装器自带源落位到
    ``runtime/_bundled``），故第 2 候选是产品链路的关键路径。

    :param root: 安装根（便携根）。
    :param name: 组件源名（文件名或目录名）。
    :param bundled_dir: 显式组件源目录；None 表示不启用该候选。
    :return: 存在的源绝对路径；均不存在时返回 None。
    """
    candidates = []
    if bundled_dir:
        candidates.append(os.path.join(bundled_dir, name))
    candidates.append(os.path.join(root, "runtime", "_bundled", name))
    candidates.append(os.path.join(BUNDLED_DIR, name))
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def find_installer_source(root, bundled_dir=None):
    """按优先级查找 Miniconda 安装器源文件。

    1. 显式 ``bundled_dir``（测试注入 / 调用方指定）；
    2. ``<root>/runtime/_bundled/<name>``（随包分发落点，用户机器侧）；
    3. ``installer/bundled/<name>``（开发态 / 构建机源）。

    :param root: 安装根（便携根）。
    :param bundled_dir: 显式组件源目录；None 表示不启用该候选。
    :return: 存在的源文件绝对路径；均不存在时返回 None。
    """
    return find_bundled_source(root, CONDA_INSTALLER_NAME, bundled_dir=bundled_dir)


def install_conda(root, runner=None, bundled_dir=None):
    """把内置 Miniconda 静默安装到 ``<root>/runtime/conda``（幂等、失败不阻断）。

    Windows NSIS 参数口径：``<installer> /S /D=<target>``——``/D=`` 必须为
    最后一个参数且**不带引号**（路径含空格时 NSIS 解析存在平台风险，此时
    追加告警但继续执行，不阻断）。

    :param root: 安装根（便携根）。
    :param runner: 命令执行器注入（测试 mock 入口）；缺省用 :func:`default_runner`。
    :param bundled_dir: 显式组件源目录；缺省按 :func:`find_installer_source` 优先级查找。
    :return: dict，字段 installed / skipped / reason / target / conda_exe /
        returncode / output_tail。
    """
    root = root or PROJECT_ROOT
    target = os.path.join(root, CONDA_DIR_REL)
    exe = conda_exe_path(root)
    if os.path.isfile(exe):
        _log_info(f"内置 conda 运行时已就位，跳过安装：{exe}")
        return {
            "installed": True, "skipped": True, "reason": "already-installed",
            "target": target, "conda_exe": exe, "returncode": 0, "output_tail": "",
        }
    src = find_installer_source(root, bundled_dir=bundled_dir)
    if src is None:
        _log_warn(
            "内置 conda 安装器源缺失（已查 runtime/_bundled 与 installer/bundled），"
            "跳过运行时安装（不影响主安装）"
        )
        return {
            "installed": False, "skipped": True, "reason": "source-missing",
            "target": target, "conda_exe": exe, "returncode": None, "output_tail": "",
        }
    if " " in target:
        _log_warn(
            "安装目标路径含空格：NSIS /D= 对空格路径解析存在平台风险，"
            "建议将便携包解压到无空格路径"
        )
    os.makedirs(os.path.dirname(target), exist_ok=True)
    exec_runner = runner or default_runner
    _log_info(f"开始静默安装内置 conda 运行时：{src} -> {target}")
    returncode, output = exec_runner([src, "/S", f"/D={target}"])
    installed = returncode == 0 and os.path.isfile(exe)
    if installed:
        _log_info(f"内置 conda 运行时安装完成：{exe}")
    else:
        _log_warn(
            f"内置 conda 运行时安装未完成（returncode={returncode}，不影响主安装）"
        )
    return {
        "installed": installed, "skipped": False,
        "reason": "" if installed else "install-failed",
        "target": target, "conda_exe": exe,
        "returncode": returncode, "output_tail": (output or "")[-500:],
    }


def create_voice_env(root, runner=None, python_version=None, conda_channels=None):
    """创建语音 sidecar 环境（``conda create -p <root>/runtime/voice python=<版本>``）。

    通道经 ``--override-channels -c <tuna>`` 参数化传入，避免读写用户全局
    ``.condarc``；幂等（环境 ``python.exe`` 已存在直接跳过）；失败不阻断。

    :param root: 安装根（便携根）。
    :param runner: 命令执行器注入（测试 mock 入口）；缺省用 :func:`default_runner`。
    :param python_version: sidecar Python 版本；缺省 :data:`VOICE_PYTHON_VERSION`。
    :param conda_channels: 通道列表；缺省仅清华主通道 :data:`CONDA_MIRROR_MAIN`。
    :return: dict，字段 created / skipped / reason / target / python / returncode /
        output_tail。
    """
    root = root or PROJECT_ROOT
    version = python_version or VOICE_PYTHON_VERSION
    target = os.path.join(root, VOICE_ENV_DIR_REL)
    py = voice_env_python(root)
    if os.path.isfile(py):
        _log_info(f"语音 sidecar 环境已就位，跳过创建：{py}")
        return {
            "created": True, "skipped": True, "reason": "already-created",
            "target": target, "python": py, "returncode": 0, "output_tail": "",
        }
    conda = conda_exe_path(root)
    if not os.path.isfile(conda):
        _log_warn("内置 conda 未安装，跳过 sidecar 环境创建（不影响主安装）")
        return {
            "created": False, "skipped": True, "reason": "conda-missing",
            "target": target, "python": py, "returncode": None, "output_tail": "",
        }
    channels = list(conda_channels or (CONDA_MIRROR_MAIN,))
    cmd = [conda, "create", "-p", target, "-y", f"python={version}", "--override-channels"]
    for channel in channels:
        cmd.extend(["-c", channel])
    exec_runner = runner or default_runner
    _log_info(f"开始创建语音 sidecar 环境（Python {version}）：{target}")
    returncode, output = exec_runner(cmd)
    created = returncode == 0 and os.path.isfile(py)
    if created:
        _log_info(f"语音 sidecar 环境创建完成：{py}")
    else:
        _log_warn(
            f"语音 sidecar 环境创建未完成（returncode={returncode}，不影响主安装）"
        )
    return {
        "created": created, "skipped": False,
        "reason": "" if created else "create-failed",
        "target": target, "python": py,
        "returncode": returncode, "output_tail": (output or "")[-500:],
    }


# ------------------------------------------------------------------ #
# 语音依赖装配（GPU 探测分叉 + MeloTTS 链路）                          #
# ------------------------------------------------------------------ #

#: sidecar 语音依赖 pip 清单（MeloTTS 中文链路完整可信集；2026-09-25 实测逐项打通）。
#: 版本钉法：``librosa==0.9.1`` 与 ``transformers==4.27.4`` 为 MeloTTS 官方
#: requirements 硬约束（melo/api.py 的位置参数调用与 librosa>=1.0 不兼容；
#: transformers 4.27 决定 tokenizers<0.14 与 BERT 权重加载行为）；
#: ``numpy==1.26.4`` 与 librosa 0.9.1 / torch cu128 双兼容。
#: 全语言段为 ``melo/text/cleaner.py`` 顶层硬导入链所必需（缺一即 ImportError）；
#: ``fugashi`` 虽无直接 import，但 transformers 的日语 tokenizer（MecabTokenizer）强制要求。
VOICE_MELOTTS_PACKAGES = (
    # —— 核心链路（官方 requirements 硬约束）——
    "numpy==1.26.4",
    "librosa==0.9.1",
    "transformers==4.27.4",
    "soundfile",
    "cached_path",
    "pypinyin==0.50.0",
    "jieba==0.42.1",
    "cn2an==0.5.22",
    "num2words==0.5.12",
    "inflect==7.0.0",
    "pydub==0.25.1",
    "unidecode==1.3.7",
    "anyascii==0.3.2",
    "loguru==0.7.2",
    "tqdm",
    "txtsplit",
    "langid==1.1.6",
    # —— 全语言硬导入链（cleaner.py 顶层 import 所必需；实测补齐）——
    "mecab-python3==1.0.9",
    "unidic-lite==1.0.8",
    "pykakasi==2.2.1",
    "fugashi==1.3.0",
    "jamo==0.4.1",
    "g2p_en==2.1.0",
    "eng_to_ipa==0.0.2",
    "g2pkk>=0.1.1",
    "gruut[de,es,fr]==2.2.3",
    "six",
    # —— TTS ORT 加速（2026-10-01）：``ml_dtypes==0.5.4`` 钉版兼容 numpy 1.26.4
    #    （0.6.x 要求 numpy>=2，会诱发 resolver 升级 numpy）。
    #    ORT 变体包（onnxruntime / -gpu / -directml）**不再硬编码**于此——改由
    #    ``bootstrap.resolve_ort_package`` 按加速画像分叉装配（有核显 → DirectML；
    #    无核显 N 卡 → GPU；无核显 A/Intel → DirectML；无 GPU → CPU）。
    #    见 20261001_模块0_全组件加速双模式.md §2.1 ——
    "ml_dtypes==0.5.4",
)
#: ASR 引擎本体（SenseVoice 经 funasr 调用）。版本对齐试装环境实测线（1.4.16）；
#: 其运行依赖 torch / torchaudio（由 GPU 依赖链先行装好，此处不再重复钉版本）。
VOICE_ASR_PACKAGES = ("funasr==1.4.16",)
#: ASR ONNX 运行时（20261002 批 B：SenseVoice ONNX 优先路径，torch 回退）。
#: 版本对齐试装环境实测线（0.4.3）；其依赖 onnxruntime 由 ORT 变体分叉装配
#: 先行提供（同一 import 名互斥，此处不重复钉），kaldi-native-fbank 由 pip
#: 自动解析（镜像可拉）。
VOICE_ASR_ONNX_PACKAGES = ("funasr-onnx==0.4.3",)
#: sidecar **不装**的包：Windows 长路径墙导致 llama-cpp-python 必然安装失败
#: （规划 §四-15），本地推理改走 llama.cpp 预编译二进制（llama-cli / llama-server）。
VOICE_SKIPPED_PACKAGES = ("llama-cpp-python",)
#: MeloTTS 随包源码目录名（``bundled/`` 下）；``--no-deps`` 安装以跳过其
#: requirements 里的日语/多语言重链（Windows 上部分包无轮子且中文链路用不到）。
MELOTTS_SRC_DIR_NAME = "melotts_src"


def rewrite_pip_command(command_line, env_python):
    """把 ``pip install ...`` 命令改写为在 sidecar 解释器内执行。

    命令由本仓装配器生成（:func:`bootstrap.build_gpu_dependency_commands` 等），
    无含空格路径，沿用 bootstrap ``cmd.split()`` 的切分口径。

    :param command_line: 形如 ``pip install torch --index-url <url>`` 的字符串。
    :param env_python: sidecar 环境 Python 可执行文件路径。
    :return: 参数列表 ``[env_python, "-m", "pip", "install", ...]``；
        非 ``pip install`` 命令（说明性注释等）返回 None。
    """
    parts = command_line.split()
    if len(parts) >= 2 and parts[0] == "pip" and parts[1] == "install":
        return [env_python, "-m", "pip", *parts[1:]]
    return None


def install_voice_dependencies(root, recommend, cuda_version=None, channel=None, runner=None,
                               has_igpu=None, profile=None):
    """在 sidecar 环境内安装推理依赖（GPU 探测分叉）与语音引擎依赖（MeloTTS 链路）。

    命令装配保持单一真相源：推理依赖复用
    ``bootstrap.build_gpu_dependency_commands``（cuda → torch / llama-cpp cu128 链；
    cpu → CPU 轮子；探测失败由调用方降级传入 ``cpu``），**ORT 变体包按加速画像
    分叉装配**（有核显 → DirectML；无核显 N 卡 → GPU；无核显 A/Intel → DirectML；
    无 GPU → CPU——同一 import 名互斥，不再随 cuda 分支捆绑 ``onnxruntime-gpu``）；
    语音链路追加 :data:`VOICE_MELOTTS_PACKAGES` 与随包 MeloTTS 源码（``--no-deps``）。

    逐条执行、失败仅告警（不抛、不阻断，与既有安装口径一致）。

    :param root: 安装根（便携根）。
    :param recommend: GPU 检测结论 ``"cuda" | "rocm" | "cpu"``。
    :param cuda_version: 驱动报告的 CUDA 版本（仅注释说明用）。
    :param channel: 统一下载源通道（``"mirror"`` / ``"official"``）；普通 pip 包
        经镜像追加索引；torch / llama-cpp-python 专用源不受影响（Task 9 口径）。
        缺省 ``None`` 时回落 :data:`bootstrap.DEFAULT_CHANNEL`（镜像）。
    :param runner: 命令执行器注入（测试 mock 入口）；缺省 :func:`default_runner`。
    :param has_igpu: 是否含核显（画像显式传入）；与 ``profile`` 二选一。
    :param profile: 硬件画像 dict（优先于 ``has_igpu``）。
    :return: dict —— installed / skipped / reason / commands（已执行命令清单）/
        errors（失败条目列表）/ ort_package（本次分叉装配的 ORT 变体）。
    """
    from installer import bootstrap  # 延迟导入：避免与 bootstrap 的潜在循环依赖

    root = root or PROJECT_ROOT
    python = voice_env_python(root)
    if not os.path.isfile(python):
        _log_warn("语音 sidecar 环境不存在，跳过语音依赖安装（不影响主安装）")
        return {
            "installed": False, "skipped": True, "reason": "env-missing",
            "commands": [], "errors": [], "ort_package": None,
        }

    # ORT 分叉画像：显式入参优先；未提供时仅在无注入 runner（生产默认链）时真实探测，
    # 注入 runner（测试替身）时按无核显保守继续，避免污染注入通道。
    ort_profile = profile
    if not isinstance(ort_profile, dict):
        if has_igpu is not None:
            ort_profile = {"recommend": recommend, "has_igpu": bool(has_igpu)}
        elif runner is None:
            detected_igpu, _dgpu_vendor = _detect_gpu_inventory(None)
            ort_profile = {"recommend": recommend, "has_igpu": detected_igpu}
        else:
            ort_profile = {"recommend": recommend, "has_igpu": False}

    resolved_channel = channel if channel is not None else bootstrap.DEFAULT_CHANNEL
    index_url = bootstrap.pip_index_url(resolved_channel)
    commands = []
    for line in bootstrap.build_gpu_dependency_commands(
        recommend, cuda_version, channel=channel, profile=ort_profile
    ):
        # sidecar 跳过 llama-cpp-python（必然失败的已知墙，见 VOICE_SKIPPED_PACKAGES）
        if not line.startswith("#") and any(pkg in line for pkg in VOICE_SKIPPED_PACKAGES):
            _log_info(f"[语音依赖] 跳过 {line.split()[2]}（改走 llama.cpp 预编译二进制，见规划 §四-15）")
            continue
        commands.append(line)
    packages = " ".join(VOICE_MELOTTS_PACKAGES)
    commands.append(
        "# MeloTTS 中文链路依赖（librosa 0.9.1 / transformers 4.27.4 为官方硬约束）"
    )
    if index_url:
        commands.append(f"pip install {packages} -i {index_url}")
    else:
        commands.append(f"pip install {packages}")
    asr_packages = " ".join(VOICE_ASR_PACKAGES)
    if index_url:
        commands.append(f"pip install {asr_packages} -i {index_url}")
    else:
        commands.append(f"pip install {asr_packages}")
    # ASR ONNX 运行时（20261002 批 B）：SenseVoice ONNX 优先路径（torch 回退）
    asr_onnx_packages = " ".join(VOICE_ASR_ONNX_PACKAGES)
    if index_url:
        commands.append(f"pip install {asr_onnx_packages} -i {index_url}")
    else:
        commands.append(f"pip install {asr_onnx_packages}")
    melotts_src = find_bundled_source(root, MELOTTS_SRC_DIR_NAME)
    if melotts_src and os.path.isdir(melotts_src):
        suffix = f" -i {index_url}" if index_url else ""
        commands.append(f"pip install --no-deps {melotts_src}{suffix}")
    else:
        _log_warn(
            f"MeloTTS 随包源码缺失（已查 runtime/_bundled 与 installer/bundled 下的 "
            f"{MELOTTS_SRC_DIR_NAME}），跳过引擎本体安装"
        )

    exec_runner = runner or default_runner
    executed, errors = [], []
    for command_line in commands:
        if command_line.startswith("#"):
            _log_info(f"[语音依赖] {command_line.lstrip('# ')}")
            continue
        argv = rewrite_pip_command(command_line, python)
        if argv is None:
            continue
        _log_info(f"[语音依赖] 正在执行：{command_line}")
        returncode, output = exec_runner(argv)
        executed.append(command_line)
        if returncode != 0:
            _log_warn(f"[语音依赖] 命令执行失败（不阻断主安装）：{command_line}")
            errors.append(
                {"command": command_line, "returncode": returncode, "output": (output or "")[-500:]}
            )
    installed = bool(executed) and not errors
    if installed:
        _log_info(f"语音依赖安装完成（共 {len(executed)} 条命令）")
    return {
        "installed": installed, "skipped": False,
        "reason": "" if installed else ("partial-failure" if executed else "no-commands"),
        "commands": executed, "errors": errors,
        "ort_package": bootstrap.resolve_ort_package(ort_profile),
    }


# ------------------------------------------------------------------ #
# nltk 数据预置（g2p_en 运行时依赖，随包分发）                          #
# ------------------------------------------------------------------ #

#: nltk 数据随包目录名（``bundled/`` 下）。包含 g2p_en 运行时所需的
#: ``taggers/averaged_perceptron_tagger[_eng]`` 与 ``corpora/cmudict``
#: （约 15MB）；不经 nltk downloader 下载（实测其报 "Security Violation"）。
NLTK_DATA_DIR_NAME = "nltk_data"


def provision_nltk_data(root, bundled_dir=None):
    """把随包 nltk 数据预置到 ``<root>/data/nltk_data``（幂等、失败不阻断）。

    语音文本前端（g2p_en → nltk pos_tag/cmudict）在**运行时**需要
    ``averaged_perceptron_tagger_eng`` 等资源；这些数据随 ``installer/bundled``
    分发，避免用户机器经 nltk downloader 下载失败。

    幂等口径：目标目录已存在且非空时直接跳过（不覆盖、不删除）。

    :param root: 安装根（便携根）。
    :param bundled_dir: 显式组件源目录（测试注入）；缺省按
        :func:`find_bundled_source` 优先级查找（``runtime/_bundled`` → ``installer/bundled``）。
    :return: dict，字段 provisioned / skipped / reason / target。
    """
    root = root or PROJECT_ROOT
    src = find_bundled_source(root, NLTK_DATA_DIR_NAME, bundled_dir=bundled_dir)
    dst = os.path.join(root, "data", "nltk_data")
    if not src or not os.path.isdir(src):
        _log_warn(
            f"随包 nltk 数据缺失（已查 runtime/_bundled 与 installer/bundled 下的 "
            f"{NLTK_DATA_DIR_NAME}），跳过预置（语音文本前端可能不可用）"
        )
        return {"provisioned": False, "skipped": True, "reason": "source-missing", "target": dst}
    if os.path.isdir(dst) and len(os.listdir(dst)) > 0:
        _log_info(f"nltk 数据已就位，跳过预置：{dst}")
        return {"provisioned": True, "skipped": True, "reason": "already-present", "target": dst}
    shutil.copytree(src, dst, dirs_exist_ok=True)
    _log_info(f"nltk 数据预置完成：{dst}")
    return {"provisioned": True, "skipped": False, "reason": "", "target": dst}


# ------------------------------------------------------------------ #
# 安装期一次性装配（独立安装程序调用入口）                              #
# ------------------------------------------------------------------ #

#: 装配全过程日志相对路径（便于用户机器侧排查；不在 zip 白名单、随安装根落盘）。
PROVISION_LOG_REL = os.path.join("runtime", "provision.log")

#: 安装期 TTS 预热脚本（在 sidecar 解释器内执行）。
#:
#: 目的：MeloTTS 中文链路首次合成会经 HF 下载 `myshell-ai/MeloTTS-Chinese` 与
#: BERT `hfl/chinese-roberta-wwm-ext-large`（合计约 1.5GB，本机实测约 70s），
#: 慢网下会超过前端 300s 请求超时口径 → 用户首次点「朗读」可能失败。
#: 安装期预热把该下载提前完成，缓存落 `<root>/data/hf_cache`（便携原则）。
#:
#: 口径（与 ``lite.audio.voice_bridge_client._build_env`` 的运行环境契约逐项对齐）：
#: - HF_HOME / NLTK_DATA / TEMP / TMP 全部重定向到 `<root>/data/`（便携原则）；
#: - 缺省走官方端点（与 voice_bridge_client 同口径，见规划 §四-10）；
#: - 预热失败仅告警（真引擎仍可用——首次合成时再下载）。
#:
#: 2026-09-25 实装两连修：① 缺 NLTK_DATA 时 g2p_en 报 `Resource 'cmudict' not found`
#: （nltk 只搜默认路径，找不到随包落位的 `<root>/data/nltk_data`）；② 环境变量在脚本内
#: 自设，避免改动 runner 接口（注入 runner 的单测因此仍可断言命令形状）。
VOICE_WARMUP_SCRIPT = (
    "import os, sys\n"
    "root = sys.argv[1]\n"
    "device = sys.argv[2]\n"
    "data = os.path.join(root, 'data')\n"
    "os.makedirs(os.path.join(data, 'tmp'), exist_ok=True)\n"
    "os.environ.setdefault('HF_HOME', os.path.join(data, 'hf_cache'))\n"
    "os.environ.setdefault('HF_HUB_DISABLE_SYMLINKS_WARNING', '1')\n"
    "os.environ.setdefault('NLTK_DATA', os.path.join(data, 'nltk_data'))\n"
    "os.environ['TEMP'] = os.environ['TMP'] = os.path.join(data, 'tmp')\n"
    "import numpy as np\n"
    "from melo.api import TTS\n"
    "engine = TTS(language='ZH', device=device, use_hf=True)\n"
    "audio = engine.tts_to_file('你好，欢迎使用。', 0, output_path=None, speed=1.0, quiet=True)\n"
    "print('TTS_WARMUP_OK', int(np.asarray(audio).size))\n"
)


class _TeeStream:
    """把写入同时送往多个流（控制台 + 日志文件）——单流失败不影响其余。

    每次写入后立即 flush：装配耗时可达数十分钟，日志须**实时可见**（用户/支持
    可在安装过程中 tail ``runtime/provision.log`` 判断卡在哪一步），且进程被强杀
    时不丢已产生的诊断。
    """

    def __init__(self, *streams):
        """记录需要同时接收写入的流（``None`` 自动剔除）。"""
        self._streams = [stream for stream in streams if stream is not None]

    def write(self, data):
        """向所有流写入并返回长度（``print`` 依赖返回值语义）。"""
        for stream in self._streams:
            try:
                stream.write(data)
            except Exception:  # noqa: BLE001 - 单个流失败不影响其余
                pass
        self.flush()
        return len(data)

    def flush(self):
        """刷新所有流（失败忽略）。"""
        for stream in self._streams:
            try:
                stream.flush()
            except Exception:  # noqa: BLE001 - 单个流失败不影响其余
                pass


#: 预热单次命令超时（秒）：含约 1.5GB 权重下载，放宽到 1 小时。
WARMUP_TIMEOUT_S = 3600

#: HF 缓存中 MeloTTS 中文权重的目录名（幂等判定依据：存在即视为已预热）。
WARMUP_CACHE_MARKER = "models--myshell-ai--MeloTTS-Chinese"


def warmup_voice_models(root, device="cpu", runner=None):
    """安装期 TTS 预热：在 sidecar 解释器内下载 MeloTTS 权重并完成一次真实合成。

    幂等判定：``<root>/data/hf_cache/hub/models--myshell-ai--MeloTTS-Chinese``
    已存在即跳过（依据实际资产而非标记文件，缓存被清理后会重新预热）。

    失败仅告警（真引擎不受影响——首次合成时会自行下载）；返回值如实记录结果。

    :param root: 安装根（便携根）。
    :param device: 推理设备（``"cuda"`` / ``"cpu"``）；预热脚本按该设备加载模型。
    :param runner: 命令执行器注入（测试 mock 入口）；缺省用 :func:`default_runner`
        并放宽超时到 :data:`WARMUP_TIMEOUT_S`。
    :return: dict —— warmed / skipped / reason / returncode / output_tail。
    """
    root = root or PROJECT_ROOT
    python = voice_env_python(root)
    cache_marker = os.path.join(root, "data", "hf_cache", "hub", WARMUP_CACHE_MARKER)
    if os.path.isdir(cache_marker):
        _log_info("MeloTTS 权重已预热，跳过（缓存已就位）")
        return {"warmed": True, "skipped": True, "reason": "already-warm",
                "returncode": 0, "output_tail": ""}
    if not os.path.isfile(python):
        _log_warn("语音 sidecar 环境不存在，跳过 TTS 预热（不影响主安装）")
        return {"warmed": False, "skipped": True, "reason": "env-missing",
                "returncode": None, "output_tail": ""}

    argv = [python, "-c", VOICE_WARMUP_SCRIPT, root, str(device or "cpu")]
    _log_info(f"[TTS 预热] 正在下载并加载语音权重（设备 {device}，首次约 1.5GB，请耐心等待）…")
    if runner is not None:
        returncode, output = runner(argv)
    else:
        returncode, output = default_runner(argv, timeout=WARMUP_TIMEOUT_S)
    warmed = returncode == 0 and "TTS_WARMUP_OK" in (output or "")
    if warmed:
        _log_info("[TTS 预热] 完成：权重已缓存到 data/hf_cache（首次合成无需再下载）")
    else:
        _log_warn("[TTS 预热] 未完成（不阻断主安装）：首次合成时将自行下载权重")
    return {
        "warmed": warmed, "skipped": False,
        "reason": "" if warmed else "warmup-failed",
        "returncode": returncode, "output_tail": (output or "")[-500:],
    }


def _detect_gpu(runner=None):
    """检测 GPU 并返回 ``(recommend, cuda_version, vendor)``；任何异常降级 CPU。

    探测核心为 ``lite.runtime.hardware_profile``（唯一真相源，经
    :mod:`installer.gpu_detect` 薄适配），与首启向导同一实现。
    """
    try:
        from installer.gpu_detect import GpuDetector
    except ImportError:  # pragma: no cover - CLI 直跑 / 冻结态包上下文差异兜底
        from gpu_detect import GpuDetector

    try:
        report = GpuDetector(runner=runner).detect()
    except Exception as exc:  # noqa: BLE001 - 检测失败一律降级 CPU，不阻断
        _log_warn(f"GPU 检测失败（降级 CPU）：{type(exc).__name__}: {exc}")
        return "cpu", None, "cpu"
    return (
        str(report.get("recommend") or "cpu"),
        report.get("cuda_version"),
        str(report.get("gpu_vendor") or "cpu"),
    )


def _detect_gpu_inventory(runner=None):
    """检测 GPU 画像增量，返回 ``(has_igpu, dgpu_vendor)``；任何异常降级无核显。

    探测核心为 ``lite.runtime.hardware_profile.probe_gpu_inventory``（唯一真相源，
    经 :mod:`installer.gpu_detect` 薄适配的增量方法），与首启向导同一实现。
    """
    try:
        from installer.gpu_detect import GpuDetector
    except ImportError:  # pragma: no cover - CLI 直跑 / 冻结态包上下文差异兜底
        from gpu_detect import GpuDetector

    try:
        summary = GpuDetector(runner=runner).detect_inventory()
    except Exception as exc:  # noqa: BLE001 - 枚举失败按无核显保守继续
        _log_warn(f"GPU 清单枚举失败（按无核显继续）：{type(exc).__name__}: {exc}")
        return False, None
    return bool(summary.get("has_igpu")), summary.get("dgpu_vendor")


#: 方案落盘的目标键：``accel_plan`` 产出的点分键 → ``config.json`` 的 (段, 键)。
#: （20261002 批 A：追加 local_llm.backend / embedding.backend 两键。）
_ACCEL_PLAN_KEYS = (
    ("accel.mode", ("accel", "mode")),
    ("tts.accel", ("tts", "accel")),
    ("tts.accel_device", ("tts", "accel_device")),
    ("asr.device", ("asr", "device")),
    ("local_llm.device", ("local_llm", "device")),
    ("local_llm.backend", ("local_llm", "backend")),
    ("embedding.device", ("embedding", "device")),
    ("embedding.backend", ("embedding", "backend")),
)


def apply_accel_plan(root, profile=None, config_path=None):
    """把加速方案（模式 + 各组件落点）**保守**写入 ``<root>/config.json``。

    口径（spec「安装链」/ 变更文档 §2.2）：**裸 JSON 保守读改**——

    - 文件不存在 → 跳过并告警（不新建）；
    - 键已存在 → 不覆盖（尊重用户 / 向导显式配置）；
    - 读取 / 写入异常 → 仅告警不阻断（写入走临时文件 + ``os.replace`` 原子替换）。

    决策复用 :func:`lite.runtime.hardware_profile.accel_plan` /
    :func:`~lite.runtime.hardware_profile.derive_default_mode`（唯一真相源）。

    :param root: 安装根（便携根）。
    :param profile: 硬件画像 dict；缺省（None）表示无画像，跳过落盘。
    :param config_path: 目标配置文件绝对路径；缺省 ``<root>/config.json``。
    :return: dict —— applied（已写入键）/ existing（键已存在跳过）/ plan（决策产物）/
        mode / path / applied_ok / reason。
    """
    root = root or PROJECT_ROOT
    path = config_path or os.path.join(root, "config.json")
    result = {
        "applied": [], "existing": [], "plan": {}, "mode": None,
        "path": path, "applied_ok": False, "reason": "",
    }
    if not isinstance(profile, dict):
        result["reason"] = "profile-missing"
        _log_warn("加速方案落盘跳过：缺少硬件画像（不影响主安装）")
        return result

    try:
        from lite.runtime.hardware_profile import accel_plan, derive_default_mode
    except Exception as exc:  # noqa: BLE001 - 方案器不可用则跳过，不阻断
        result["reason"] = "planner-unavailable"
        _log_warn(f"加速方案器不可用（跳过落盘）：{type(exc).__name__}: {exc}")
        return result

    try:
        mode = derive_default_mode(profile)
        plan = accel_plan(profile, mode)
    except Exception as exc:  # noqa: BLE001 - 决策异常仅告警
        result["reason"] = "plan-failed"
        _log_warn(f"加速方案决策失败（跳过落盘）：{type(exc).__name__}: {exc}")
        return result
    result["mode"] = mode
    result["plan"] = plan

    if not os.path.isfile(path):
        result["reason"] = "config-missing"
        _log_warn(f"config.json 不存在（跳过加速方案落盘）：{path}")
        return result

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("config.json 顶层不是对象")
    except Exception as exc:  # noqa: BLE001 - 读取失败仅告警
        result["reason"] = "config-unreadable"
        _log_warn(f"config.json 读取失败（跳过加速方案落盘）：{type(exc).__name__}: {exc}")
        return result

    try:
        for plan_key, (section, key) in _ACCEL_PLAN_KEYS:
            if plan_key not in plan:
                continue
            node = data.get(section)
            if not isinstance(node, dict):
                node = {}
                data[section] = node
            if key in node:
                result["existing"].append(plan_key)  # 键已存在：不覆盖（尊重显式配置）
                continue
            node[key] = plan[plan_key]
            result["applied"].append(plan_key)
    except Exception as exc:  # noqa: BLE001 - 组装异常仅告警
        result["reason"] = "apply-failed"
        _log_warn(f"加速方案组装失败（跳过落盘）：{type(exc).__name__}: {exc}")
        return result

    if not result["applied"]:
        result["applied_ok"] = True  # 无需写入（相关键均已存在）
        _log_info("加速方案未改动 config.json（相关键均已存在，尊重显式配置）")
        return result

    try:
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
        result["applied_ok"] = True
        _log_info(
            f"加速方案已写入 config.json：{', '.join(result['applied'])}（模式 {mode}）"
        )
    except Exception as exc:  # noqa: BLE001 - 写入失败仅告警不阻断
        result["reason"] = "write-failed"
        _log_warn(f"config.json 写入失败（仅告警不阻断）：{type(exc).__name__}: {exc}")
    return result


#: 运行时装配的磁盘预检下限（GB）：conda 基座（~0.6）+ sidecar 环境（~5~8，GPU 版）
#: + MeloTTS/HF 预热缓存（~1.5）+ nltk 数据与安装余量；不足时告警并跳过装配（不阻断）。
PROVISION_MIN_DISK_GB = 12.0


def _probe_disk_free_gb(root):
    """磁盘预检：取安装根所在盘的可用空间（GB），不可用返回 None。

    探测复用 ``lite.runtime.hardware_profile.probe_disk_free_gb``（唯一真相源，
    与首启向导 / 安装器 GPU 探测同一实现）；导入或探测异常按"未知"处理——
    预检不可用**不阻断装配**。
    """
    try:
        from lite.runtime.hardware_profile import probe_disk_free_gb

        return probe_disk_free_gb(root)
    except Exception as exc:  # noqa: BLE001 - 预检不可用按未知继续
        _log_warn(f"磁盘空间预检不可用（按未知继续装配）：{type(exc).__name__}: {exc}")
        return None


def provision_runtime(root=None, runner=None, log_path=None, channel=None):
    """安装期一次性装配运行时（内置 conda + 语音 sidecar 环境 + 语音依赖 + nltk 数据）。

    由独立安装程序在展开载荷后调用（``backend.exe --provision-runtime``）。口径：

    - **一次性装完**（人类裁决③）：conda 静默安装 → sidecar 环境（Python 3.10）→
      推理/语音依赖（GPU 自动探测分叉：torch 链 + 按画像装配的 ORT 变体）→
      nltk 数据预置 → **加速方案落盘**（保守写 ``config.json``）→ **TTS 权重预热**
      （MeloTTS 中文链路约 1.5GB 提前下载，消除首次合成超过前端 300s 超时的风险；
      设备**固定 CPU**，隔离 melo torch-GPU 风险路径）；
    - **磁盘预检**（规划 §四-4 / 独立安装程序 §四-2）：装配前探测安装盘可用空间，
      低于 :data:`PROVISION_MIN_DISK_GB` 时告警并**整链跳过**（不阻断主安装）；
    - **任何一步失败不阻断主安装**：全程只 [WARN] 不抛错，返回值如实记录各步结果，
      调用方退出码不因装配失败而变（应用仍可启动，语音自动降级）；
    - **进度可见**：全过程 [INFO] 同时打印到控制台并追加写入 ``<root>/runtime/provision.log``；
    - **幂等**：各步自身幂等，重复执行（修复/重装）只补缺失部分。

    :param root: 安装根（便携根）；缺省 :data:`PROJECT_ROOT`。
    :param runner: 命令执行器注入（测试 mock 入口）；缺省各步用 :func:`default_runner`。
    :param log_path: 装配日志路径；缺省 ``<root>/runtime/provision.log``。
    :param channel: 统一下载源通道（``"mirror"`` / ``"official"``）；缺省读安装根
        配置（无配置时回落 :data:`bootstrap.DEFAULT_CHANNEL`）。
    :return: dict —— recommend / cuda_version / gpu_vendor / has_igpu / skipped_reason /
        conda / voice_env / dependencies / nltk / accel / warmup / report_path。
    """
    root = root or PROJECT_ROOT
    path = log_path or os.path.join(root, PROVISION_LOG_REL)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    resolved_channel = channel
    if resolved_channel is None:
        try:
            from installer import bootstrap

            resolved_channel = bootstrap.resolve_download_channel(root)
        except Exception as exc:  # noqa: BLE001 - 通道解析失败回落默认，不阻断
            _log_warn(f"下载源通道解析失败（回落默认）：{type(exc).__name__}: {exc}")

    with open(path, "a", encoding="utf-8", errors="replace") as log_file:
        with contextlib.redirect_stdout(_TeeStream(sys.stdout, log_file)):
            _log_info("==== 运行时装配开始（内置 conda + 语音 sidecar 环境） ====")
            recommend, cuda_version, vendor = _detect_gpu(runner)
            _log_info(f"GPU 检测：{vendor} → 推荐 {recommend}（CUDA 版本：{cuda_version or '不适用'}）")
            # 画像增量（核显识别）：供 ORT 分叉与加速方案落盘
            has_igpu, dgpu_vendor = _detect_gpu_inventory(runner)
            _log_info(f"核显识别：{'检测到核显' if has_igpu else '未检测到核显'}（ORT 变体按此分叉）")
            accel_profile = {
                "recommend": recommend,
                "cuda_version": cuda_version,
                "gpu_vendor": vendor,
                "has_igpu": has_igpu,
                "dgpu_vendor": dgpu_vendor,
            }
            # 磁盘预检（规划 §四-4）：可用空间不足时整链跳过，避免半装坏环境
            disk_free_gb = _probe_disk_free_gb(root)
            skipped_reason = None
            if disk_free_gb is not None and disk_free_gb < PROVISION_MIN_DISK_GB:
                skipped_reason = (
                    f"磁盘可用空间不足（可用 {disk_free_gb:.1f} GB < 需 "
                    f"{PROVISION_MIN_DISK_GB:.1f} GB），已跳过运行时装配"
                )
                _log_warn(skipped_reason + "（应用仍可启动，语音与本地推理自动降级）")
            # 各步自身均「失败不抛」，此处的兜底只防装配链自身的意外异常
            # （口径：安装器不因运行时装配失败而中断主安装）
            conda_result, env_result, deps_result, nltk_result = {}, {}, {}, {}
            accel_result, warmup_result = {}, {}
            report_path = os.path.join(root, "data", "install_report.json")
            if skipped_reason is None:
                try:
                    conda_result = install_conda(root, runner=runner)
                    env_result = create_voice_env(root, runner=runner)
                    deps_result = install_voice_dependencies(
                        root, recommend, cuda_version, channel=resolved_channel, runner=runner
                    )
                    nltk_result = provision_nltk_data(root)
                    # 加速方案落盘（保守读改）：写 accel.mode 与各组件落点
                    accel_result = apply_accel_plan(root, profile=accel_profile)
                    # 预热放在依赖安装之后：MeloTTS 需已在 sidecar 环境内可导入；
                    # 设备固定 CPU（隔离 melo torch-GPU 风险路径，spec 强制口径）
                    warmup_result = warmup_voice_models(root, "cpu", runner=runner)
                except Exception as exc:  # noqa: BLE001 - 装配链异常仅告警，不阻断安装
                    _log_warn(f"运行时装配出现未预期异常（已忽略，主安装不受影响）：{type(exc).__name__}: {exc}")

            report = {
                "gpu_vendor": vendor,
                "cuda_version": cuda_version,
                "recommend": recommend,
                "has_igpu": has_igpu,
                "conda_installed": bool(conda_result.get("installed")),
                "voice_env_created": bool(env_result.get("created") or env_result.get("skipped")),
                "dependencies_installed": bool(deps_result.get("installed")),
                "nltk_provisioned": bool(nltk_result.get("provisioned")),
                "accel_mode": (accel_result.get("mode") if isinstance(accel_result, dict) else None),
                "accel_plan": (accel_result.get("plan") if isinstance(accel_result, dict) else None),
                "tts_warmed": bool(warmup_result.get("warmed")),
                "channel": resolved_channel,
                "errors": list(deps_result.get("errors") or []),
                "skipped_reason": skipped_reason,
                "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            os.makedirs(os.path.dirname(report_path), exist_ok=True)
            with open(report_path, "w", encoding="utf-8") as handle:
                json.dump(report, handle, ensure_ascii=False, indent=2)
            _log_info(f"装配报告已写入：{report_path}")
            _log_info(f"装配日志：{path}")
            _log_info("==== 运行时装配结束 ====")

    return {
        "recommend": recommend,
        "cuda_version": cuda_version,
        "gpu_vendor": vendor,
        "has_igpu": has_igpu,
        "skipped_reason": skipped_reason,
        "conda": conda_result,
        "voice_env": env_result,
        "dependencies": deps_result,
        "nltk": nltk_result,
        "accel": accel_result,
        "warmup": warmup_result,
        "report_path": report_path,
    }