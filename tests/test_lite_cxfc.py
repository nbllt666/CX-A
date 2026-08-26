# -*- coding: utf-8 -*-
"""Task G2 极简 CXFC（LiteCXFC）单元测试。

覆盖（与工程补充文档 §3 对齐）：
- 注册 / 调用 / 列表：register_embedded + call + list_tools 全链路；
- 未注册错误：call 未注册工具抛 CxfToolNotFound；
- 异常包装：handler 抛异常返回 {success:false, error} 而非上抛；
- 禁用即抛：enabled=False 时 register/call 抛 CxfDisabled；
- embedded_only 校验：transport 仅允许 "embedded"，传其它抛 ValueError；
- 配置段覆盖：cxfc.enabled via 裸 dict 生效；
- 重复注册覆盖：重复 tool_id 覆盖旧注册并返回 True。
"""

import pytest

from lite.cxfc import LiteCXFC, CxfDisabled, CxfToolNotFound

#: 所有测试均关闭原生日志的重复 warning 输出噪音
import logging

logging.getLogger("lite.cxfc.lite_cxfc").setLevel(logging.CRITICAL)


def _echo_handler(arguments):
    """通用 echo handler：原样返回参数，便于断言。"""
    return {"echo": dict(arguments)}


def test_register_call_and_list():
    """注册一个工具后，call 返回 {success:true, result, tool}，list_tools 含该工具。"""
    cxfc = LiteCXFC(enabled=True)

    assert cxfc.register_embedded("tool_a", "工具A", _echo_handler, description="A描述") is True

    result = cxfc.call("tool_a", {"x": 1})
    assert result["success"] is True
    assert result["tool"] == "tool_a"
    assert result["result"] == {"echo": {"x": 1}}

    tools = cxfc.list_tools()
    assert len(tools) == 1
    assert tools[0]["tool_id"] == "tool_a"
    assert tools[0]["name"] == "工具A"
    assert tools[0]["description"] == "A描述"
    assert tools[0]["transport"] == "embedded"


def test_call_unregistered_raises_cxf_tool_not_found():
    """调用未注册工具抛 CxfToolNotFound（不静默）。"""
    cxfc = LiteCXFC(enabled=True)
    with pytest.raises(CxfToolNotFound):
        cxfc.call("nope", {})


def test_handler_exception_wrapped_instead_of_raised():
    """handler 抛异常时返回 {success:false, error} 而非向上抛。"""

    def boom(arguments):
        raise RuntimeError("boom 崩溃")

    cxfc = LiteCXFC(enabled=True)
    cxfc.register_embedded("boom", "Boom工具", boom)

    result = cxfc.call("boom", {})
    assert result["success"] is False
    assert "boom 崩溃" in result["error"]
    assert result["tool"] == "boom"


def test_disabled_register_and_call_raise_cxf_disabled():
    """enabled=False（默认）时 register / call 均抛 CxfDisabled。"""
    cxfc = LiteCXFC()  # enabled 默认为 False
    with pytest.raises(CxfDisabled):
        cxfc.register_embedded("a", "A", _echo_handler)
    with pytest.raises(CxfDisabled):
        cxfc.call("a", {})
    # list_tools 在禁用时仍可用（空列表，不抛）
    assert cxfc.list_tools() == []


def test_embedded_only_rejects_network_transport():
    """embedded_only=True 时 transport 仅允许 "embedded"。"""
    cxfc = LiteCXFC(enabled=True, embedded_only=True)
    with pytest.raises(ValueError):
        cxfc.register_embedded("a", "A", _echo_handler, transport="direct")
    with pytest.raises(ValueError):
        cxfc.register_embedded("a", "A", _echo_handler, transport="relay")
    # 允许 embedded
    assert cxfc.register_embedded("a", "A", _echo_handler, transport="embedded") is True


def test_embedded_only_false_allows_other_transport():
    """embedded_only=False 时不强制校验 transport。"""
    cxfc = LiteCXFC(enabled=True, embedded_only=False)
    assert cxfc.register_embedded("a", "A", _echo_handler, transport="direct") is True


def test_required_fields_validated():
    """(tool_id, name, handler) 为必填，缺失抛 ValueError。"""
    cxfc = LiteCXFC(enabled=True)
    with pytest.raises(ValueError):
        cxfc.register_embedded("", "A", _echo_handler)
    with pytest.raises(ValueError):
        cxfc.register_embedded("a", "", _echo_handler)
    with pytest.raises(ValueError):
        cxfc.register_embedded("a", "A", None)


def test_duplicate_id_overrides_with_warning():
    """重复 tool_id 覆盖旧注册（handler 换成新的），并返回 True。"""
    cxfc = LiteCXFC(enabled=True)
    cxfc.register_embedded("dup", "旧", _echo_handler)
    assert cxfc.call("dup", {})["result"] == {"echo": {}}

    # 覆盖：新 handler 返回不同值
    cxfc.register_embedded("dup", "新", lambda args: {"v": 42})
    result = cxfc.call("dup", {})
    assert result["success"] is True
    assert result["result"] == {"v": 42}
    assert len(cxfc.list_tools()) == 1  # 覆盖而非新增


def test_config_section_overrides_enabled():
    """config 裸 dict 的 cxfc.enabled=True 可覆写构造参数默认关闭态。"""
    cxfc = LiteCXFC(config={"cxfc": {"enabled": True, "embedded_only": True}})
    assert cxfc.enabled is True
    assert cxfc.embedded_only is True
    assert cxfc.register_embedded("a", "A", _echo_handler) is True


def test_internal_registry_no_network_mechanism():
    """内部为纯进程内 dict 注册表，不创建任何网络 / 心跳 / 发现状态。"""
    cxfc = LiteCXFC(enabled=True)
    # 与 CX-O CXFCManager（心跳任务 / UDP 套接字 / direct 转发）对照，本极简版无此类属性
    assert not hasattr(cxfc, "_heartbeat_task")
    assert not hasattr(cxfc, "_broadcast_socket")
    assert not hasattr(cxfc, "_discovery_socket")
    assert hasattr(cxfc, "_registry") and isinstance(cxfc._registry, dict)