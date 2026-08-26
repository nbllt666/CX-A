# -*- coding: utf-8 -*-
"""Task A2 云端适配层单元测试。

覆盖（全 mock，无真实网络）：
- provider 工厂映射正确（deepseek / tongyi / openai / moonshot 四家 base_url）
- 流式 chat 通过内存 transport 返回多块文本（验证拼接结果）
- 无 api_key 抛 CloudConfigError
- 未知 provider 抛 CloudConfigError
- is_online 成功 / 失败分支（mock 传输返回值）
- reload 后 provider 切换生效
"""

import pytest

from lite.cloud.adapter import (
    PROVIDER_BASE_URLS,
    CloudAdapter,
    CloudConfigError,
)
from lite.config import ConfigManager


def _make_adapter(tmp_path, provider="deepseek", api_key="sk-test", base_url="", **kw):
    """在临时目录构造 ConfigManager 并注入 cloud 段，返回 CloudAdapter。"""
    cm = ConfigManager(
        config_path=str(tmp_path / "config.json"),
        data_dir=str(tmp_path / "data"),
    )
    cm.set("cloud", "provider", provider)
    cm.set("cloud", "api_key", api_key)
    cm.set("cloud", "base_url", base_url)
    return CloudAdapter(config_manager=cm, **kw), cm


# ------------------------------------------------------------------ #
# 1. provider 工厂映射                                                #
# ------------------------------------------------------------------ #

def test_provider_factory_mapping():
    """四家 provider 的默认 base_url 映射正确且与工程文档一致。"""
    assert PROVIDER_BASE_URLS == {
        "deepseek": "https://api.deepseek.com",
        "openai": "https://api.openai.com/v1",
        "moonshot": "https://api.moonshot.cn/v1",
        "tongyi": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    }
    assert set(CloudAdapter.providers) == set(PROVIDER_BASE_URLS.keys())


@pytest.mark.parametrize(
    "provider,expected",
    [
        ("deepseek", "https://api.deepseek.com"),
        ("openai", "https://api.openai.com/v1"),
        ("moonshot", "https://api.moonshot.cn/v1"),
        ("tongyi", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    ],
)
def test_resolve_base_url_default(provider, expected):
    """未显式配置 base_url 时，resolve_base_url 返回各 provider 默认映射。"""
    assert CloudAdapter.resolve_base_url(provider) == expected


def test_resolve_base_url_custom_overrides_default():
    """显式配置 base_url 后应覆盖默认映射。"""
    assert CloudAdapter.resolve_base_url("deepseek", "https://custom.example/") == \
        "https://custom.example"


# ------------------------------------------------------------------ #
# 2. 流式 chat 通过内存 transport 返回多块                           #
# ------------------------------------------------------------------ #

def test_chat_stream_memory_transport(tmp_path):
    """chat 应透传 messages 并拼接内存 transport 返回的多个文本块。"""
    adapter, _ = _make_adapter(tmp_path)

    captured = {}

    def fake_transport(messages):
        # 内存 mock transport：断言入参并逐块产出
        captured["messages"] = messages
        yield "你"
        yield "好"
        yield "世界"

    adapter._transport = fake_transport

    messages = [{"role": "user", "content": "你好"}]
    result = "".join(adapter.chat(messages))
    assert captured["messages"] == messages
    assert result == "你好世界"


# ------------------------------------------------------------------ #
# 3. 无 api_key 抛 CloudConfigError                                  #
# ------------------------------------------------------------------ #

def test_no_api_key_raises_cloud_config_error(tmp_path):
    """cloud.api_key 为空时，chat 应抛 CloudConfigError。"""
    adapter, _ = _make_adapter(tmp_path, api_key="")
    with pytest.raises(CloudConfigError):
        list(adapter.chat([{"role": "user", "content": "hi"}]))


# ------------------------------------------------------------------ #
# 4. 未知 provider 抛 CloudConfigError                               #
# ------------------------------------------------------------------ #

def test_unknown_provider_raises_cloud_config_error(tmp_path):
    """未知 provider 且未显式 base_url 时，chat 应抛 CloudConfigError。"""
    adapter, _ = _make_adapter(tmp_path, provider="not-a-provider")
    with pytest.raises(CloudConfigError):
        list(adapter.chat([{"role": "user", "content": "hi"}]))


def test_unknown_provider_even_with_key(tmp_path):
    """即使配置了 api_key，未知 provider 仍应在访问 base_url 时抛 CloudConfigError。"""
    adapter, _ = _make_adapter(tmp_path, provider="weird", api_key="sk-x")
    with pytest.raises(CloudConfigError):
        _ = adapter.base_url


# ------------------------------------------------------------------ #
# 5. is_online 成功 / 失败分支                                        #
# ------------------------------------------------------------------ #

def test_is_online_success(tmp_path):
    """传输探测返回 True 时，is_online 返回 True。"""
    adapter, _ = _make_adapter(tmp_path)
    adapter._http_get = lambda url, timeout: True
    assert adapter.is_online(timeout=2) is True


def test_is_online_failure(tmp_path):
    """传输探测返回 False 时，is_online 返回 False。"""
    adapter, _ = _make_adapter(tmp_path)
    adapter._http_get = lambda url, timeout: False
    assert adapter.is_online(timeout=2) is False


def test_is_online_captures_base_url(tmp_path):
    """is_online 应把解析后的 base_url 传给底层探测。"""
    adapter, _ = _make_adapter(tmp_path, provider="moonshot")
    captured = {}

    def fake_http_get(url, timeout):
        captured["url"] = url
        captured["timeout"] = timeout
        return True

    adapter._http_get = fake_http_get
    assert adapter.is_online(timeout=3) is True
    assert captured["url"] == "https://api.moonshot.cn/v1"
    assert captured["timeout"] == 3


# ------------------------------------------------------------------ #
# 6. reload 后 provider 切换生效                                      #
# ------------------------------------------------------------------ #

def test_reload_switches_provider(tmp_path):
    """reload 后读取新配置，base_url 与 chat 传输目标随之切换。"""
    adapter, cm = _make_adapter(tmp_path, provider="deepseek")

    # 初始为 deepseek
    assert adapter.base_url == "https://api.deepseek.com"
    assert adapter.provider == "deepseek"

    # 切换 provider 到 openai 并重载
    cm.set("cloud", "provider", "openai")
    adapter.reload()
    assert adapter.base_url == "https://api.openai.com/v1"
    assert adapter.provider == "openai"

    # chat 经内存 transport 验证目标 base_url 已切换
    captured = {}

    def fake_transport(messages):
        captured["base_url"] = adapter.base_url
        yield "ok"

    adapter._transport = fake_transport
    assert "".join(adapter.chat([{"role": "user", "content": "hi"}])) == "ok"
    assert captured["base_url"] == "https://api.openai.com/v1"


def test_reload_updates_api_key(tmp_path):
    """reload 重新读取 api_key，无需重建实例。"""
    adapter, cm = _make_adapter(tmp_path, api_key="key-a")
    assert adapter.api_key == "key-a"

    cm.set("cloud", "api_key", "key-b")
    adapter.reload()
    assert adapter.api_key == "key-b"


# ------------------------------------------------------------------ #
# 7. 可导入性                                                        #
# ------------------------------------------------------------------ #

def test_cloud_package_exports():
    """amt：CloudAdapter 可由 lite.cloud 包直接导入。"""
    from lite.cloud import CloudAdapter as ExportedAdapter

    assert ExportedAdapter is CloudAdapter