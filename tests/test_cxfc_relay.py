# -*- coding: utf-8 -*-
"""Task H1 CXFC relay 传输兼容单元测试（全 mock，无网络、无真实前端）。

覆盖（对齐 spec「CXFC relay 传输兼容」各 Scenario）：
- 注册→call→mock dispatcher 投递→fulfill 回报→success 结果（同步 + 跨线程异步）；
- dispatcher 缺失 / 目标未注册（孤儿索引）/ 投递异常 → RELAY_UNREACHABLE（不抛）；
- 投递后无回报 → RELAY_TIMEOUT（超时窗口参数设 0.1s）；
- 同 request_id 重放 → REPLAY_DETECTED；防重放窗口过期后允许重用；
- embedded_only=True 时 register_relay 抛 ValueError；enabled=False 抛 CxfDisabled；
- pending_relay_calls 取走语义与 plugin_id 过滤（经 pending_dispatcher 全链路）；
- list_tools embedded/relay 同构（name/transport/parameters/returns）；
- 既有 embedded 行为零回归（embedded 优先解析、未知工具仍抛 CxfToolNotFound）。
"""

import logging
import threading
import time

import pytest

from lite.cxfc import (
    RELAY_TIMEOUT,
    RELAY_UNREACHABLE,
    REPLAY_DETECTED,
    TRANSPORT_RELAY,
    CxfDisabled,
    CxfToolNotFound,
    LiteCXFC,
)

#: 静音原生日志的重复 warning 输出噪音
logging.getLogger("lite.cxfc.lite_cxfc").setLevel(logging.CRITICAL)

#: 标准 relay 工具描述列表（对齐 CX-O /tools 端点字段：name/description/parameters/returns）
_RELAY_TOOLS = [
    {
        "name": "relay_greet",
        "description": "经前端转接的问候工具",
        "parameters": {
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"}},
        },
        "returns": {"type": "object"},
    },
]


def _make_relay_cxfc(**kwargs) -> LiteCXFC:
    """构造开启 relay 的 LiteCXFC（embedded_only=False）。"""
    params = {"enabled": True, "embedded_only": False}
    params.update(kwargs)
    return LiteCXFC(**params)


# ------------------------------------------------------------------ 成功回报链路
def test_relay_call_fulfilled_sync_success():
    """同步回报：mock dispatcher 投递时立即 fulfill，call 返回 success + result。"""
    cxfc = _make_relay_cxfc()
    captured = []

    def dispatcher(call_dict):
        captured.append(dict(call_dict))
        # 同步回报：等待者已在投递前登记，可立即回填
        assert cxfc.fulfill_relay(call_dict["request_id"], True, {"greeting": "你好"}) is True

    assert cxfc.register_relay("plugin_a", _RELAY_TOOLS, dispatcher=dispatcher) is True

    result = cxfc.call("relay_greet", {"name": "小明"})
    assert result["success"] is True
    assert result["result"] == {"greeting": "你好"}
    assert result["tool"] == "relay_greet"
    assert len(result["request_id"]) == 32  # uuid4 hex
    assert len(captured) == 1
    # 投递 dict 对齐 CX-O §2.2 WS 广播形状
    assert captured[0]["type"] == "cxfc_relay_call"
    assert captured[0]["plugin_id"] == "plugin_a"
    assert captured[0]["tool"] == "relay_greet"
    assert captured[0]["arguments"] == {"name": "小明"}
    assert captured[0]["request_id"] == result["request_id"]


def test_relay_call_async_fulfill_from_another_thread():
    """异步回报：投递后在另一线程 fulfill，等待线程被 Event 唤醒拿到结果。"""
    cxfc = _make_relay_cxfc(relay_timeout_s=5.0)
    seen = []

    def dispatcher(call_dict):
        seen.append(call_dict["request_id"])

    cxfc.register_relay("plugin_a", _RELAY_TOOLS, dispatcher=dispatcher)

    outcomes = {}

    def runner():
        outcomes["result"] = cxfc.call("relay_greet", {"name": "异步"})

    t = threading.Thread(target=runner)
    t.start()
    deadline = time.monotonic() + 2
    while not seen and time.monotonic() < deadline:
        time.sleep(0.01)
    assert seen, "dispatcher 未在窗口内收到投递"

    assert cxfc.fulfill_relay(seen[0], True, {"ok": 1}) is True
    t.join(timeout=5)
    assert not t.is_alive()
    result = outcomes["result"]
    assert result["success"] is True
    assert result["result"] == {"ok": 1}
    assert result["request_id"] == seen[0]


def test_relay_call_failure_result_propagates_error():
    """回报 success=False 时，call 返回 success=False 且 error 随回报内容。"""
    cxfc = _make_relay_cxfc()
    holder = {}

    def dispatcher(call_dict):
        holder["rid"] = call_dict["request_id"]

    cxfc.register_relay("plugin_a", _RELAY_TOOLS, dispatcher=dispatcher)
    outcomes = {}
    t = threading.Thread(target=lambda: outcomes.setdefault("r", cxfc.call("relay_greet", {})))
    t.start()
    deadline = time.monotonic() + 2
    while "rid" not in holder and time.monotonic() < deadline:
        time.sleep(0.01)
    assert cxfc.fulfill_relay(holder["rid"], False, "前端执行失败") is True
    t.join(timeout=5)
    result = outcomes["r"]
    assert result["success"] is False
    assert result["error"] == "前端执行失败"
    assert result["request_id"] == holder["rid"]


# ------------------------------------------------------------------ 不可达 / 超时
def test_relay_unreachable_without_dispatcher():
    """目标已注册但 dispatcher 缺失（通道未注入）→ RELAY_UNREACHABLE，不抛异常。"""
    cxfc = _make_relay_cxfc()
    assert cxfc.register_relay("plugin_a", _RELAY_TOOLS) is True

    result = cxfc.call("relay_greet", {"name": "x"})
    assert result["success"] is False
    assert result["error_code"] == RELAY_UNREACHABLE
    assert "request_id" in result


def test_relay_unreachable_on_dispatcher_exception():
    """dispatcher 投递时抛异常（投递失败）→ RELAY_UNREACHABLE，不抛异常。"""
    cxfc = _make_relay_cxfc()

    def bad_dispatcher(call_dict):
        raise RuntimeError("通道炸了")

    cxfc.register_relay("plugin_a", _RELAY_TOOLS, dispatcher=bad_dispatcher)

    result = cxfc.call("relay_greet", {})
    assert result["success"] is False
    assert result["error_code"] == RELAY_UNREACHABLE


def test_relay_unreachable_when_target_missing():
    """工具索引存在但目标条目缺失（目标未注册的孤儿状态）→ RELAY_UNREACHABLE。"""
    cxfc = _make_relay_cxfc()
    cxfc.register_relay("plugin_a", _RELAY_TOOLS)
    # 构造孤儿索引：工具名指向一个未注册的 plugin_id（白盒注入，覆盖「目标未注册」语义）
    cxfc._relay_tool_index["orphan_tool"] = "ghost_plugin"

    result = cxfc.call("orphan_tool", {})
    assert result["success"] is False
    assert result["error_code"] == RELAY_UNREACHABLE


def test_relay_timeout_without_fulfillment():
    """投递成功但窗口内无回报（窗口 0.1s）→ RELAY_TIMEOUT；等待者被清理，迟到回报得 False。"""
    cxfc = _make_relay_cxfc(relay_timeout_s=0.1)
    cxfc.register_relay("plugin_a", _RELAY_TOOLS, dispatcher=lambda call_dict: None)

    start = time.monotonic()
    result = cxfc.call("relay_greet", {"name": "x"})
    elapsed = time.monotonic() - start

    assert result["success"] is False
    assert result["error_code"] == RELAY_TIMEOUT
    assert result["request_id"]
    # 确实等待过窗口（宽松下界防调度抖动），且未悬挂到默认 10s
    assert 0.05 <= elapsed < 5
    # 超时后等待者已清理：迟到回报返回 False（api 侧映射 404）
    assert cxfc.fulfill_relay(result["request_id"], True, {"late": True}) is False


def test_call_time_timeout_override():
    """call 传 timeout_s 可覆盖构造默认窗口（默认 10s 不生效，0.1s 内超时）。"""
    cxfc = _make_relay_cxfc(relay_timeout_s=10.0)
    cxfc.register_relay("plugin_a", _RELAY_TOOLS, dispatcher=lambda call_dict: None)

    start = time.monotonic()
    result = cxfc.call("relay_greet", {}, timeout_s=0.1)
    elapsed = time.monotonic() - start

    assert result["error_code"] == RELAY_TIMEOUT
    assert elapsed < 5


# ------------------------------------------------------------------ 防重放
def test_replay_detected_on_same_request_id():
    """同 request_id 窗口内重复作为调用键 → REPLAY_DETECTED，且不触发第二次投递。"""
    cxfc = _make_relay_cxfc()
    dispatched = []

    def dispatcher(call_dict):
        dispatched.append(call_dict["request_id"])
        cxfc.fulfill_relay(call_dict["request_id"], True, {"n": len(dispatched)})

    cxfc.register_relay("plugin_a", _RELAY_TOOLS, dispatcher=dispatcher)

    first = cxfc.call("relay_greet", {}, request_id="fixed-req-1")
    assert first["success"] is True

    second = cxfc.call("relay_greet", {}, request_id="fixed-req-1")
    assert second["success"] is False
    assert second["error_code"] == REPLAY_DETECTED
    assert second["request_id"] == "fixed-req-1"
    assert len(dispatched) == 1  # 重放未触发第二次投递


def test_replay_window_expiry_allows_reuse():
    """防重放窗口过期后，同 request_id 允许重新作为调用键（有界清理语义）。"""
    cxfc = _make_relay_cxfc(replay_window_s=0.05, relay_timeout_s=2.0)

    def dispatcher(call_dict):
        cxfc.fulfill_relay(call_dict["request_id"], True, {"v": 1})

    cxfc.register_relay("plugin_a", _RELAY_TOOLS, dispatcher=dispatcher)

    first = cxfc.call("relay_greet", {}, request_id="same-id")
    assert first["success"] is True
    time.sleep(0.12)  # 超过 0.05s 窗口
    second = cxfc.call("relay_greet", {}, request_id="same-id")
    assert second["success"] is True


# ------------------------------------------------------------------ 注册校验 / 禁用
def test_register_relay_rejected_when_embedded_only():
    """embedded_only=True（默认）时 register_relay 抛 ValueError（与既有语义一致）。"""
    cxfc = LiteCXFC(enabled=True, embedded_only=True)
    with pytest.raises(ValueError):
        cxfc.register_relay("plugin_a", _RELAY_TOOLS)


def test_disabled_relay_raises_cxf_disabled():
    """enabled=False 时 register_relay / call 均抛 CxfDisabled（relay 同受总开关约束）。"""
    cxfc = LiteCXFC()  # enabled 默认 False
    with pytest.raises(CxfDisabled):
        cxfc.register_relay("plugin_a", _RELAY_TOOLS)
    with pytest.raises(CxfDisabled):
        cxfc.call("anything", {})


def test_register_relay_validations():
    """register_relay 必填与形态校验：plugin_id / tools / dispatcher 非法均抛 ValueError。"""
    cxfc = _make_relay_cxfc()
    with pytest.raises(ValueError):
        cxfc.register_relay("", _RELAY_TOOLS)
    with pytest.raises(ValueError):
        cxfc.register_relay(None, _RELAY_TOOLS)
    with pytest.raises(ValueError):
        cxfc.register_relay("p", "not-a-list")
    with pytest.raises(ValueError):
        cxfc.register_relay("p", [{"description": "缺 name"}])
    with pytest.raises(ValueError):
        cxfc.register_relay("p", _RELAY_TOOLS, dispatcher="not-callable")


def test_duplicate_relay_plugin_overrides():
    """重复 plugin_id 覆盖旧注册：旧工具索引被清理，新工具列表生效。"""
    cxfc = _make_relay_cxfc()
    cxfc.register_relay("plugin_a", [{"name": "old_tool"}])
    assert cxfc.register_relay("plugin_a", _RELAY_TOOLS) is True

    tool_ids = {t["tool_id"] for t in cxfc.list_tools()}
    assert tool_ids == {"relay_greet"}  # 旧 old_tool 索引随覆盖清理
    with pytest.raises(CxfToolNotFound):
        cxfc.call("old_tool", {})


# ------------------------------------------------------------------ pending 取走
def test_pending_relay_calls_take_semantics_and_filter():
    """pending_dispatcher 投递 → pending_relay_calls 按目标过滤取走 → 回报成功；未取走者超时。"""
    cxfc = _make_relay_cxfc(relay_timeout_s=2.0)
    cxfc.register_relay("plugin_a", _RELAY_TOOLS, dispatcher=cxfc.pending_dispatcher())
    cxfc.register_relay("plugin_b", [{"name": "relay_other"}], dispatcher=cxfc.pending_dispatcher())

    outcomes = {}

    def runner_a():
        outcomes["a"] = cxfc.call("relay_greet", {"name": "x"})

    def runner_b():
        outcomes["b"] = cxfc.call("relay_other", {})

    threads = [threading.Thread(target=runner_a), threading.Thread(target=runner_b)]
    for t in threads:
        t.start()

    # 轮询按目标过滤取走 plugin_a 的调用（不影响 plugin_b 的待执行项）
    deadline = time.monotonic() + 2
    taken = []
    while time.monotonic() < deadline:
        taken = cxfc.pending_relay_calls(plugin_id="plugin_a")
        if taken:
            break
        time.sleep(0.01)
    assert len(taken) == 1
    assert taken[0]["type"] == "cxfc_relay_call"
    assert taken[0]["plugin_id"] == "plugin_a"
    assert taken[0]["tool"] == "relay_greet"
    assert taken[0]["arguments"] == {"name": "x"}

    # 回报 plugin_a 的调用 → runner_a 拿到 success
    assert cxfc.fulfill_relay(taken[0]["request_id"], True, {"hi": True}) is True

    # 剩余 pending 只含 plugin_b 的调用（at-most-once：plugin_a 的已取走不再出现）
    rest = cxfc.pending_relay_calls()
    assert len(rest) == 1
    assert rest[0]["plugin_id"] == "plugin_b"

    for t in threads:
        t.join(timeout=5)
    assert outcomes["a"]["success"] is True
    assert outcomes["a"]["result"] == {"hi": True}
    # plugin_b 的调用从未被取走回报 → 超时收场
    assert outcomes["b"]["success"] is False
    assert outcomes["b"]["error_code"] == RELAY_TIMEOUT


def test_pending_limit_zero_returns_empty():
    """limit<=0 时不取走任何调用。"""
    cxfc = _make_relay_cxfc()
    cxfc.register_relay("plugin_a", _RELAY_TOOLS, dispatcher=cxfc.pending_dispatcher())
    outcomes = {}
    t = threading.Thread(target=lambda: outcomes.setdefault("r", cxfc.call("relay_greet", {}, timeout_s=0.3)))
    t.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        time.sleep(0.02)
        with cxfc._lock:
            if len(cxfc._pending_relay_calls) >= 1:
                break
    assert cxfc.pending_relay_calls(limit=0) == []
    t.join(timeout=5)


def test_fulfill_unknown_request_id_returns_false():
    """fulfill 未知的 request_id 返回 False（api 侧映射 404）。"""
    cxfc = _make_relay_cxfc()
    assert cxfc.fulfill_relay("nonexistent", True, {}) is False


def test_duplicate_fulfill_returns_false():
    """同一 request_id 重复回报：首次 True，第二次 False（等待者已被取走）。"""
    cxfc = _make_relay_cxfc(relay_timeout_s=5.0)
    holder = {}

    def dispatcher(call_dict):
        holder["rid"] = call_dict["request_id"]

    cxfc.register_relay("plugin_a", _RELAY_TOOLS, dispatcher=dispatcher)
    outcomes = {}
    t = threading.Thread(target=lambda: outcomes.setdefault("r", cxfc.call("relay_greet", {})))
    t.start()
    deadline = time.monotonic() + 2
    while "rid" not in holder and time.monotonic() < deadline:
        time.sleep(0.01)

    assert cxfc.fulfill_relay(holder["rid"], True, {"v": 1}) is True
    assert cxfc.fulfill_relay(holder["rid"], True, {"v": 2}) is False
    t.join(timeout=5)
    assert outcomes["r"]["result"] == {"v": 1}  # 首次回报生效，重复回报被拒


def test_fulfill_relay_validations():
    """fulfill_relay 必填校验：request_id 为空 / success 非布尔抛 ValueError。"""
    cxfc = _make_relay_cxfc()
    with pytest.raises(ValueError):
        cxfc.fulfill_relay("", True, {})
    with pytest.raises(ValueError):
        cxfc.fulfill_relay("x", "yes", {})


# ------------------------------------------------------------------ 工具描述结构
def test_list_tools_embedded_and_relay_structure():
    """list_tools 输出 embedded 与 relay 同构：name/transport/parameters/returns 全量存在。"""
    cxfc = _make_relay_cxfc()
    cxfc.register_embedded(
        "emb", "嵌入式", lambda a: a,
        description="d", parameters={"type": "object"}, returns={"type": "string"},
    )
    cxfc.register_relay("plugin_a", _RELAY_TOOLS)

    tools = {t["tool_id"]: t for t in cxfc.list_tools()}
    assert set(tools) == {"emb", "relay_greet"}
    for entry in tools.values():
        for key in ("name", "transport", "parameters", "returns", "description"):
            assert key in entry
    assert tools["emb"]["transport"] == "embedded"
    assert tools["emb"]["returns"] == {"type": "string"}
    assert tools["relay_greet"]["transport"] == TRANSPORT_RELAY
    assert tools["relay_greet"]["plugin_id"] == "plugin_a"
    assert tools["relay_greet"]["parameters"]["required"] == ["name"]
    assert tools["relay_greet"]["returns"] == {"type": "object"}


# ------------------------------------------------------------------ 既有 embedded 零回归
def test_embedded_precedence_and_unknown_tool_still_raises():
    """embedded 注册表优先解析（零回归）；embedded 与 relay 均未命中的工具仍抛 CxfToolNotFound。"""
    cxfc = _make_relay_cxfc()
    cxfc.register_embedded("shared", "嵌入式优先", lambda a: {"side": "embedded"})
    cxfc.register_relay("plugin_a", [{"name": "shared"}, {"name": "only_relay"}])

    result = cxfc.call("shared", {})
    assert result["success"] is True
    assert result["result"] == {"side": "embedded"}

    with pytest.raises(CxfToolNotFound):
        cxfc.call("totally_unknown", {})


def test_embedded_handler_exception_wrapping_unchanged():
    """既有 embedded 异常包装语义不变：handler 抛异常返回 {success:false, error} 不上抛。"""

    def boom(arguments):
        raise RuntimeError("boom 崩溃")

    cxfc = _make_relay_cxfc()
    cxfc.register_embedded("boom", "Boom", boom)
    result = cxfc.call("boom", {})
    assert result["success"] is False
    assert "boom 崩溃" in result["error"]


# ------------------------------------------------------------------ 配置覆盖
def test_config_overrides_relay_params():
    """config cxfc 段可覆写 embedded_only / relay_timeout_s / replay_window_s。"""
    cxfc = LiteCXFC(
        config={
            "cxfc": {
                "enabled": True,
                "embedded_only": False,
                "relay_timeout_s": 0.1,
                "replay_window_s": 1.0,
            }
        }
    )
    assert cxfc.enabled is True
    assert cxfc.embedded_only is False
    assert cxfc.relay_timeout_s == 0.1
    assert cxfc.replay_window_s == 1.0

    cxfc.register_relay("p", _RELAY_TOOLS, dispatcher=lambda call_dict: None)
    result = cxfc.call("relay_greet", {})
    assert result["error_code"] == RELAY_TIMEOUT
