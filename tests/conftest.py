# -*- coding: utf-8 -*-
"""tests 根夹具：宿主环境隔离。

整包冒烟教训（2026-08-28）：终端残留 ``CXA_API_TOKEN`` 会被 pytest 继承，
api_server 模块级读取后进入令牌模式，导致全部 HTTP 测试 403 误报失败。
此 autouse 夹具在每条测试前清除该环境变量并复位 api_server 模块级令牌，
使测试结果与宿主终端状态解耦；令牌专项测试在测试体内自行覆写，不受影响。
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
