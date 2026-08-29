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

import http.client

import pytest

from lite.cloud.adapter import (
    PROVIDER_BASE_URLS,
    CloudAdapter,
    CloudConfigError,
    CloudUnavailableError,
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
# 5.1 N5：urllib 路径 4xx 语义与 requests 对齐（HTTPError 算在线）      #
# ------------------------------------------------------------------ #

def test_is_online_urllib_http_error_counts_online(tmp_path, monkeypatch):
    """N5：urllib 路径收到 HTTPError（4xx/5xx）说明网关可达 → is_online 返回 True。"""
    import urllib.error
    import urllib.request

    from lite.cloud import adapter as adapter_mod

    adapter, _ = _make_adapter(tmp_path)
    # 强制走 urllib 兜底路径（测试环境可能已安装 requests）
    monkeypatch.setattr(adapter_mod, "_HAS_REQUESTS", False)

    def _fake_urlopen(url, timeout=None):
        raise urllib.error.HTTPError(
            url=url, code=401, msg="Unauthorized", hdrs=None, fp=None,
        )

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    assert adapter.is_online(timeout=2) is True


def test_is_online_urllib_url_error_counts_offline(tmp_path, monkeypatch):
    """N5：连接层异常（URLError 非 HTTPError）→ is_online 返回 False。"""
    import urllib.error
    import urllib.request

    from lite.cloud import adapter as adapter_mod

    adapter, _ = _make_adapter(tmp_path)
    monkeypatch.setattr(adapter_mod, "_HAS_REQUESTS", False)

    def _fake_urlopen(url, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    assert adapter.is_online(timeout=2) is False


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


# ------------------------------------------------------------------ #
# 8. L5 配置契约：temperature / model 自 cloud 段读取                  #
# ------------------------------------------------------------------ #

def _capture_sse_payload(adapter, messages=None):
    """monkeypatch _post_sse 捕获 chat 请求 payload（不发真实网络）。"""
    captured = {}

    def fake_post_sse(endpoint, payload, headers):
        captured["endpoint"] = endpoint
        captured["payload"] = payload
        captured["headers"] = headers
        return iter([])

    adapter._post_sse = fake_post_sse
    list(adapter.chat(messages or [{"role": "user", "content": "hi"}]))
    return captured


def test_temperature_defaults_and_config_override(tmp_path):
    """payload.temperature 默认 0.7；cloud.temperature 配置后取配置值。"""
    adapter, cm = _make_adapter(tmp_path)
    captured = _capture_sse_payload(adapter)
    assert captured["payload"]["temperature"] == pytest.approx(0.7)

    cm.set("cloud", "temperature", 0.2)
    adapter.reload()
    captured2 = _capture_sse_payload(adapter)
    assert captured2["payload"]["temperature"] == pytest.approx(0.2)


def test_temperature_invalid_config_falls_back(tmp_path):
    """cloud.temperature 非法时 reload 回落默认 0.7，不抛异常。"""
    adapter, cm = _make_adapter(tmp_path)
    cm.set("cloud", "temperature", "not-a-number")
    adapter.reload()
    assert adapter._temperature == pytest.approx(0.7)


def test_model_default_mapping_by_provider(tmp_path):
    """model 未配置时按 provider 官方缺省映射（不再回退 provider 名）。"""
    from lite.cloud.adapter import PROVIDER_DEFAULT_MODELS

    adapter, _ = _make_adapter(tmp_path)  # deepseek
    assert adapter._model == ""
    captured = _capture_sse_payload(adapter)
    assert captured["payload"]["model"] == PROVIDER_DEFAULT_MODELS["deepseek"]
    assert captured["payload"]["model"] == "deepseek-chat"


def test_model_config_then_param_priority(tmp_path):
    """model 优先级：构造参数 > cloud.model 配置 > provider 缺省映射。"""
    # 仅配置
    adapter_a, cm = _make_adapter(tmp_path)
    cm.set("cloud", "model", "my-finetuned")
    adapter_a.reload()
    captured = _capture_sse_payload(adapter_a)
    assert captured["payload"]["model"] == "my-finetuned"

    # 构造参数覆盖配置
    adapter_b, cm_b = _make_adapter(tmp_path, model="param-model")
    cm_b.set("cloud", "model", "cfg-model")
    adapter_b.reload()
    captured_b = _capture_sse_payload(adapter_b)
    assert captured_b["payload"]["model"] == "param-model"


def test_model_without_mapping_omits_key_with_warning(tmp_path, caplog):
    """provider 无映射且未配置 model 时，payload 不含 model 键并打 WARN。"""
    import logging

    adapter, _ = _make_adapter(tmp_path, provider="tongyi")
    with caplog.at_level(logging.WARNING, logger="lite.cloud.adapter"):
        captured = _capture_sse_payload(adapter)
    assert "model" not in captured["payload"]
    assert any(("model" in rec.getMessage() and "网关" in rec.getMessage()) for rec in caplog.records)


def test_model_resolution_survives_reload(tmp_path):
    """reload 后 model/temperature 配置仍然生效（热更新链路完整）。"""
    adapter, cm = _make_adapter(tmp_path, model="keep-me")
    cm.set("cloud", "temperature", 0.5)
    adapter.reload()
    assert adapter._model == "keep-me"  # 构造参数在 reload 后仍优先
    captured = _capture_sse_payload(adapter)
    assert captured["payload"]["temperature"] == pytest.approx(0.5)

    # 清除构造优先后（新实例），配置 model 生效
    cm.set("cloud", "model", "from-config")
    adapter2, _ = _make_adapter(tmp_path)
    adapter2._config_manager = cm
    adapter2.reload()
    captured2 = _capture_sse_payload(adapter2)
    assert captured2["payload"]["model"] == "from-config"


# ------------------------------------------------------------------ #
# 9. 第四轮体检批次C：HTTPException 归一 + 探测响应关闭                 #
# ------------------------------------------------------------------ #

@pytest.mark.parametrize(
    "http_exc",
    [http.client.IncompleteRead(b"partial"), http.client.BadStatusLine("502 Bad Gateway")],
    ids=["IncompleteRead", "BadStatusLine"],
)
def test_stream_chat_maps_http_exception_to_unavailable(tmp_path, http_exc):
    """流式响应中途抛 http.client.HTTPException（API 网关 504/502 常见形态）应
    归一为 CloudUnavailableError，不再穿透统一异常契约（fallback 降级依赖）。"""
    adapter, _ = _make_adapter(tmp_path)

    def _fake_post_sse(endpoint, payload, headers):
        raise http_exc
        yield  # pragma: no cover - 使函数成为生成器

    adapter._post_sse = _fake_post_sse
    with pytest.raises(CloudUnavailableError):
        list(adapter.chat([{"role": "user", "content": "hi"}]))


def test_http_get_closes_requests_response(tmp_path, monkeypatch):
    """第四轮体检批次C：_http_get 的 requests 探测响应以 with 确定性关闭，
    不留滞留连接。"""
    import types

    from lite.cloud import adapter as adapter_mod

    adapter, _ = _make_adapter(tmp_path)
    closed = {"flag": False}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            closed["flag"] = True
            return False

    fake_requests = types.ModuleType("requests")
    fake_requests.get = lambda url, timeout=None: _FakeResp()
    monkeypatch.setattr(adapter_mod, "_HAS_REQUESTS", True)
    monkeypatch.setattr(adapter_mod, "requests", fake_requests)

    assert adapter._http_get("https://example.com", 2) is True
    assert closed["flag"] is True