# -*- coding: utf-8 -*-
"""tests 根夹具：宿主环境隔离 + 嵌入替身注入。

1. 宿主环境隔离（2026-08-28）：终端残留 ``CXA_API_TOKEN`` 会被 pytest 继承，
   api_server 模块级读取后进入令牌模式，导致全部 HTTP 测试 403 误报失败。
   此 autouse 夹具在每条测试前清除该环境变量并复位 api_server 模块级令牌，
   使测试结果与宿主终端状态解耦；令牌专项测试在测试体内自行覆写，不受影响。

2. 嵌入替身注入（20261005，嵌入禁止降级）：``_build_embedding_provider`` 移除
   哈希桩回落路径后，单测环境（无嵌入模型文件）的 build_deps/create_app 会因
   嵌入缺失启动失败——本 autouse 夹具统一注入轻量假 llama 嵌入（64 维确定性
   替身，不走真 llama-server），保证测试链可用；嵌入装配自身的报错语义由显式
   用例覆盖（在 resolve 层单独 patch，或对 _build_embedding_provider 再 patch
   覆盖本夹具）。
"""

import os

import pytest

try:  # 导入失败不放大为全量跳过，仅放弃模块级令牌复位
    from lite.server import api_server as _api_server_mod
except Exception:  # noqa: BLE001 - 收集期不可用则仅保留环境变量隔离
    _api_server_mod = None


@pytest.fixture(autouse=True)
def _isolate_backend_token(monkeypatch):
    """隔离 CXA_API_TOKEN：清环境变量 + 复位 api_server 模块级令牌。"""
    monkeypatch.delenv("CXA_API_TOKEN", raising=False)
    if _api_server_mod is not None:
        monkeypatch.setattr(_api_server_mod, "_API_TOKEN", "")
    yield


@pytest.fixture(autouse=True)
def _fake_embedding_provider(monkeypatch):
    """嵌入替身：build_deps/create_app 测试链统一用轻量假 llama 嵌入。"""
    if _api_server_mod is None:
        yield
        return

    def _fake_builder(config):
        from lite.memory.embedding import LiteEmbeddingProvider

        return LiteEmbeddingProvider(dim=64), "llama", {"dim": 64, "model_tag": "fake|test.gguf"}

    # 保留原函数引用（挂模块属性供"直测装配器报错语义"的用例取用——函数内延迟
    # import 会拿到 patch 后的模块属性，无法绕过本替身）
    _api_server_mod._build_embedding_provider_original = _api_server_mod._build_embedding_provider
    monkeypatch.setattr(_api_server_mod, "_build_embedding_provider", _fake_builder)
    yield
