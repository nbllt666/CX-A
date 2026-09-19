# -*- coding: utf-8 -*-
"""统一下载源真相源：下载路线（通道）→ pip 索引源 / 模型仓库的唯一映射来源。

本模块是 ``config.json`` 中 ``download.channel`` 语义的**唯一真相源**：

- 通道（channel）即**下载路线**，是面向用户的唯一选择，同时决定两件事：
  ① pip 依赖安装使用的索引源（国内 = 追加清华索引，海外 = 不加 ``-i`` 参数）；
  ② 模型仓库（国内 = ``modelscope`` 魔塔，海外 = ``huggingface``）。
- HuggingFace **恒用官方端点** ``https://huggingface.co``；国内路线下载魔塔，
  魔塔本身即独立国内站（非 HuggingFace 镜像），因此国内路线不再经过
  ``hf-mirror.com``，本模块也不再提供任何 HF 镜像端点派生能力。
- 通道 → 模型仓库的映射由 :func:`model_repo_for_channel` 唯一提供，
  禁止在调用点各自推导。

调用约定：安装器依赖装配、模型下载器等所有调用点**必须**经本模块的派生函数
取得索引源与模型仓库，禁止在调用点各自硬编码 URL / 仓库名。

本模块为纯函数 / 纯常量模块：不 import ``config_manager``（避免循环导入），
不在导入期做任何网络或文件系统操作。
"""

__all__ = [
    "CHANNEL_MIRROR",
    "CHANNEL_OFFICIAL",
    "CHANNELS",
    "DEFAULT_CHANNEL",
    "PIP_MIRROR_INDEX",
    "PIP_OFFICIAL_INDEX",
    "HF_OFFICIAL",
    "MODEL_REPOS",
    "normalize_channel",
    "pip_index_url",
    "model_repo_for_channel",
]

#: 通道：国内路线（默认）——清华 pip 索引 + 魔塔模型仓库
CHANNEL_MIRROR = "mirror"
#: 通道：海外路线——PyPI 默认索引 + HuggingFace 模型仓库
CHANNEL_OFFICIAL = "official"
#: 全部合法通道（配置白名单口径）
CHANNELS = (CHANNEL_MIRROR, CHANNEL_OFFICIAL)
#: 默认通道：国内网络下国内路线更可用，缺省即国内路线
DEFAULT_CHANNEL = CHANNEL_MIRROR

#: 国内路线下的 pip 索引源（清华 PyPI 镜像）
PIP_MIRROR_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
#: 海外路线下的 pip 索引源：None 表示「不追加 ``-i`` 参数」，走 PyPI 默认源
PIP_OFFICIAL_INDEX = None

#: HuggingFace 官方端点（唯一端点：海外路线恒用它，国内路线不再涉及 HF 端点）
HF_OFFICIAL = "https://huggingface.co"

#: 模型仓库规范名集合，与 ``lite/runtime/model_downloader.py::_SOURCE_ALIASES``
#: 的规范名（values）保持一致；此处只提供常量，不做别名解析。
MODEL_REPOS = ("modelscope", "huggingface")


def normalize_channel(value):
    """将任意输入规范化为合法通道。

    ``None`` / 空串 / 非字符串 / 未知取值一律回落 ``DEFAULT_CHANNEL``，
    不抛异常（供配置读取路径使用，保证配置写坏时服务仍可起步）。

    :param value: 待规范化的通道取值（如 ``"mirror"`` / ``" OFFICIAL "``）。
    :return: ``CHANNEL_MIRROR`` 或 ``CHANNEL_OFFICIAL``。
    """
    if isinstance(value, str):
        candidate = value.strip().lower()
        if candidate in CHANNELS:
            return candidate
    return DEFAULT_CHANNEL


def pip_index_url(channel):
    """派生 pip 安装使用的索引源 URL。

    :param channel: 通道取值（非法值按 ``DEFAULT_CHANNEL`` 处理）。
    :return: 国内路线返回 ``PIP_MIRROR_INDEX``；海外路线返回 ``None``
        （语义：不追加 ``-i`` 参数，走 PyPI 默认源）。
    """
    if normalize_channel(channel) == CHANNEL_MIRROR:
        return PIP_MIRROR_INDEX
    return PIP_OFFICIAL_INDEX


def model_repo_for_channel(channel):
    """派生通道对应的模型仓库规范名（通道 → 仓库的唯一映射）。

    :param channel: 通道取值（非法值 / ``None`` 按 ``DEFAULT_CHANNEL`` 处理，
        即回落国内路线）。
    :return: 国内路线返回 ``"modelscope"``（魔塔）；海外路线返回 ``"huggingface"``。
        返回值恒为 ``MODEL_REPOS`` 中的规范名。
    """
    if normalize_channel(channel) == CHANNEL_MIRROR:
        return "modelscope"
    return "huggingface"
