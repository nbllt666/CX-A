# -*- coding: utf-8 -*-
"""RemoteController 单元测试（Task E3）——注入 mock transport 验证透传与会话。

覆盖：get_status 字段原样透传；control 三动作（enable/disable/pause）发送正确
请求体；push_config 发送配置补丁；未启用抛 RemoteDisabled；transport 抛网络异常
映射为 RemoteUnreachable。
"""

import urllib.error

import pytest

from lite.management.remote import (
    RemoteController,
    RemoteDisabled,
    RemoteError,
    RemoteUnreachable,
)


class _FakeConfig:
    """仅提供 remote 段字段的轻量配置替身（避免触达真实 config.json）。"""

    def __init__(self, endpoint, enabled):
        self._remote = {"endpoint": endpoint, "enabled": enabled}

    def get(self, section, key, default=None):
        if section != "remote":
            return default
        return self._remote.get(key, default)


class _RecorderTransport:
    """记录每次调用并按需返回固定数据 / 抛异常的 mock transport。"""

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def request(self, method, url, json_body=None, timeout=10):
        self.calls.append(
            {"method": method, "url": url, "body": json_body, "timeout": timeout}
        )
        if self.error is not None:
            raise self.error
        return self.response


def _enabled_controller(transport, endpoint="http://remote-cxo"):
    """构造已启用（enabled=true）的遥控控制器。"""
    return RemoteController(
        config=_FakeConfig(endpoint, True), transport=transport
    )


def _disabled_controller(endpoint="http://remote-cxo"):
    """构造未启用（enabled=false）的遥控控制器。"""
    return RemoteController(
        config=_FakeConfig(endpoint, False), transport=_RecorderTransport({})
    )


# ---------------------------------------------------------------- get_status 透传
def test_get_status_transfers_fields():
    response = {"status": "success", "snapshot": {"online": True, "load": 0.7, "budget": 80}}
    transport = _RecorderTransport(response)
    ctrl = _enabled_controller(transport)

    result = ctrl.get_status()

    assert result == response  # 原样透传，不加壳不改字段
    call = transport.calls[-1]
    assert call["method"] == "GET"
    assert call["url"] == "http://remote-cxo/api/admin/status"
    assert call["body"] is None


def test_get_status_passes_timeout():
    transport = _RecorderTransport({"online": True})
    ctrl = _enabled_controller(transport)

    ctrl.get_status(timeout=3)

    assert transport.calls[-1]["timeout"] == 3


# ---------------------------------------------------------------- control 三动作
@pytest.mark.parametrize("action", ["enable", "disable", "pause"])
def test_control_sends_action(action):
    transport = _RecorderTransport({"status": "success", "result": {"ok": True}})
    ctrl = _enabled_controller(transport)

    result = ctrl.control(action, agent_id="default")

    assert result == {"status": "success", "result": {"ok": True}}
    call = transport.calls[-1]
    assert call["method"] == "POST"
    assert call["url"] == "http://remote-cxo/api/admin/control"
    assert call["body"] == {"action": action, "agent_id": "default"}


def test_control_without_agent_id():
    transport = _RecorderTransport({"result": {}})
    ctrl = _enabled_controller(transport)

    ctrl.control("pause")

    assert transport.calls[-1]["body"] == {"action": "pause"}


def test_control_unknown_action_raises_valueerror():
    transport = _RecorderTransport({})
    ctrl = _enabled_controller(transport)
    with pytest.raises(ValueError):
        ctrl.control("explode")


# ---------------------------------------------------------------- push_config
def test_push_config_sends_patch():
    patch = {"llm": {"provider": "vllm", "model": "x"}}
    transport = _RecorderTransport({"status": "success", "message": "配置已更新"})
    ctrl = _enabled_controller(transport)

    result = ctrl.push_config(patch)

    assert result["status"] == "success"
    call = transport.calls[-1]
    assert call["method"] == "POST"
    assert call["url"] == "http://remote-cxo/api/admin/config"
    assert call["body"] == patch  # patch 原样透传为请求体


# ---------------------------------------------------------------- 未启用
def test_disabled_raises_on_all_operations():
    ctrl = _disabled_controller()
    with pytest.raises(RemoteDisabled):
        ctrl.get_status()
    with pytest.raises(RemoteDisabled):
        ctrl.control("enable")
    with pytest.raises(RemoteDisabled):
        ctrl.push_config({"x": 1})


# ---------------------------------------------------------------- 不可达
def test_transport_urlerror_to_unreachable():
    transport = _RecorderTransport(error=urllib.error.URLError("boom"))
    ctrl = _enabled_controller(transport)
    with pytest.raises(RemoteUnreachable):
        ctrl.get_status()


def test_transport_oserror_to_unreachable():
    transport = _RecorderTransport(error=OSError("connection refused"))
    ctrl = _enabled_controller(transport)
    with pytest.raises(RemoteUnreachable):
        ctrl.get_status()


# ---------------------------------------------------------------- 非 2xx -> RemoteError
def test_non_2xx_maps_to_remote_error():
    from lite.management import remote as _remote

    transport = _RecorderTransport(
        error=urllib.error.HTTPError(
            "http://remote-cxo/api/admin/status", 403, "Forbidden", None, None
        )
    )
    ctrl = _enabled_controller(transport, endpoint="http://remote-cxo")
    with pytest.raises(RemoteError):
        ctrl.get_status()