# -*- coding: utf-8 -*-
"""统一下载源真相源单元测试（lite/config/download_sources.py）。

覆盖：
- 通道常量与默认值（mirror / official）
- normalize_channel 的规范化与回落语义（None / 空 / 未知 / 大小写 / 空白）
- pip_index_url（mirror 有值 / official 为 None）
- model_repo_for_channel（mirror -> modelscope / official -> huggingface / 非法值回落）
- hf-mirror 端点能力已移除（HF_MIRROR / hf_endpoint / hf_endpoint_for_config 均不存在）
- MODEL_REPOS 与 model_downloader._SOURCE_ALIASES 规范名集合一致
"""

import pytest

from lite.config import download_sources as ds
from lite.runtime.model_downloader import _SOURCE_ALIASES


# ------------------------------------------------------------------ #
# 1. 常量                                                             #
# ------------------------------------------------------------------ #

def test_channel_constants():
    """通道常量与默认值：mirror 为默认，CHANNELS 恰为两个合法取值。"""
    assert ds.CHANNEL_MIRROR == "mirror"
    assert ds.CHANNEL_OFFICIAL == "official"
    assert ds.CHANNELS == ("mirror", "official")
    assert ds.DEFAULT_CHANNEL == ds.CHANNEL_MIRROR


def test_url_constants():
    """索引源与 HuggingFace 端点常量字面值（唯一真相源）。"""
    assert ds.PIP_MIRROR_INDEX == "https://pypi.tuna.tsinghua.edu.cn/simple"
    assert ds.PIP_OFFICIAL_INDEX is None
    assert ds.HF_OFFICIAL == "https://huggingface.co"


def test_hf_mirror_capability_removed():
    """hf-mirror 端点能力已移除：常量与派生函数均不得残留（防回归）。"""
    assert not hasattr(ds, "HF_MIRROR")
    assert not hasattr(ds, "hf_endpoint")
    assert not hasattr(ds, "hf_endpoint_for_config")
    assert "HF_MIRROR" not in ds.__all__
    assert "hf_endpoint" not in ds.__all__
    assert "hf_endpoint_for_config" not in ds.__all__


# ------------------------------------------------------------------ #
# 2. normalize_channel                                                #
# ------------------------------------------------------------------ #

@pytest.mark.parametrize(
    "value, expected",
    [
        ("mirror", "mirror"),
        ("official", "official"),
        ("MIRROR", "mirror"),
        ("Official", "official"),
        ("  official  ", "official"),
        (None, "mirror"),
        ("", "mirror"),
        ("   ", "mirror"),
        ("unknown", "mirror"),
        (123, "mirror"),
        ("OFFICIAL", "official"),
    ],
)
def test_normalize_channel(value, expected):
    """合法值规范化，非法值 / None / 空串一律回落默认通道且不抛错。"""
    assert ds.normalize_channel(value) == expected


def test_normalize_channel_never_raises():
    """非字符串入参（列表 / dict）也不抛错，回落默认通道。"""
    assert ds.normalize_channel(["official"]) == ds.DEFAULT_CHANNEL
    assert ds.normalize_channel({"channel": "official"}) == ds.DEFAULT_CHANNEL


# ------------------------------------------------------------------ #
# 3. pip_index_url                                                    #
# ------------------------------------------------------------------ #

def test_pip_index_url_mirror():
    """国内路线返回国内索引 URL。"""
    assert ds.pip_index_url("mirror") == ds.PIP_MIRROR_INDEX
    # 非法值按默认通道（mirror）处理
    assert ds.pip_index_url(None) == ds.PIP_MIRROR_INDEX


def test_pip_index_url_official():
    """海外路线返回 None（语义：不加 -i 参数，走 PyPI 默认源）。"""
    assert ds.pip_index_url("official") is None


# ------------------------------------------------------------------ #
# 4. model_repo_for_channel                                           #
# ------------------------------------------------------------------ #

@pytest.mark.parametrize(
    "channel, expected",
    [
        ("mirror", "modelscope"),
        ("official", "huggingface"),
        ("MIRROR", "modelscope"),
        ("Official", "huggingface"),
        ("  OFFICIAL  ", "huggingface"),
        (None, "modelscope"),
        ("", "modelscope"),
        ("   ", "modelscope"),
        ("unknown", "modelscope"),
        (123, "modelscope"),
    ],
)
def test_model_repo_for_channel(channel, expected):
    """通道 → 模型仓库唯一映射：国内路线魔塔，海外路线 HuggingFace，非法值回落魔塔。"""
    repo = ds.model_repo_for_channel(channel)
    assert repo == expected
    assert repo in ds.MODEL_REPOS


def test_model_repo_for_channel_returns_canonical_names():
    """返回值恒为 MODEL_REPOS 中的规范名（可被 _SOURCE_ALIASES 直接解析）。"""
    for channel in ds.CHANNELS:
        repo = ds.model_repo_for_channel(channel)
        assert repo in ds.MODEL_REPOS
        assert repo in _SOURCE_ALIASES


# ------------------------------------------------------------------ #
# 5. 模型仓库规范名一致性                                             #
# ------------------------------------------------------------------ #

def test_model_repos_matches_downloader_canonical_names():
    """MODEL_REPOS 与 model_downloader._SOURCE_ALIASES 的规范名集合一致。"""
    assert set(ds.MODEL_REPOS) == set(_SOURCE_ALIASES.values())
    assert ds.MODEL_REPOS == ("modelscope", "huggingface")
