# -*- coding: utf-8 -*-
"""Task D3 电脑控制接线层（ToolBridge）单元测试。

覆盖完整链路（决策 → 授权校验 → 高危确认 → 执行 → 审计回填）：
- 授权开启时 execute 成功回填 result（含 result / tool / authorized / error_code）；
- 未授权时抛 NotAuthorizedError（不透传任何本机副作用）；
- 高危指令确认被拒时返回 {"error": "需要确认", "authorized": False}，不执行；
- 每次执行/拒绝均落 audit.jsonl 审计记录（action=call_tool）；
- list_tools 返回三工具描述（参数 / 返回 Schema 简版）。
"""

import json
import sys

import pytest

from lite.computer_control import (
    TOOL_COMMAND,
    TOOL_KEYBOARD,
    TOOL_SCREEN,
    ControlAuthorizer,
    ComputerControl,
    NotAuthorizedError,
    ToolBridge,
)

#: 当前 python 解释器可执行文件，用于构建无害子进程命令
PY = sys.executable


class FakeScreenBackend:
    """内存 mock 屏幕后端：返回固定字节，不触碰真实屏幕。"""

    def screenshot(self, region=None) -> bytes:
        return b"\x89PNG\r\n\x1a\nfake-image"


class FakeKeyboardBackend:
    """内存 mock 键盘后端：无真实副作用。"""

    def click(self, x, y) -> None:
        pass

    def type_text(self, text) -> None:
        pass


def _audit_path(tmp_path) -> str:
    """返回本次测试用的审计日志路径。"""
    return str(tmp_path / "audit.jsonl")


def _read_audit(tmp_path) -> list:
    """读取审计日志全部记录（空文件返回 []）。"""
    path = _audit_path(tmp_path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return [json.loads(ln) for ln in fh.read().splitlines() if ln.strip()]
    except FileNotFoundError:
        return []


def _make_bridge(tmp_path, *, authorized=True, confirm_fn=None):
    """构造 authorizer + computer + bridge 三者，authorizer 为单例复用。"""
    authorizer = ControlAuthorizer(
        data_dir=str(tmp_path),
        confirm_fn=confirm_fn,
    )
    computer = ComputerControl(
        authorized=authorizer.is_authorized(),
        screen_backend=FakeScreenBackend(),
        keyboard_backend=FakeKeyboardBackend(),
    )
    if authorized:
        authorizer.authorize()
        computer.set_authorized(True)
    bridge = ToolBridge(computer=computer, authorizer=authorizer)
    return authorizer, computer, bridge


# ------------------------------------------------------------------ #
# 1. 授权开启时 execute 成功回填                                      #
# ------------------------------------------------------------------ #


def test_execute_command_success_fills_result(tmp_path):
    """授权开启：指令工具执行成功，结果 dict 含 result/authorized/tool/error_code。"""
    _auth, _computer, bridge = _make_bridge(tmp_path, authorized=True)
    payload = bridge.execute(
        TOOL_COMMAND, {"command": f'"{PY}" -c "print(\'EXEC_OK\')"'}
    )
    assert payload["success"] is True
    assert payload["tool"] == TOOL_COMMAND
    assert payload["authorized"] is True
    assert payload["error_code"] is None
    assert "EXEC_OK" in (payload.get("result") or "")
    assert payload["exit_code"] == 0


def test_execute_screen_success_fills_result(tmp_path):
    """授权开启：屏幕工具执行成功，result 为图像字节。"""
    _auth, _computer, bridge = _make_bridge(tmp_path, authorized=True)
    payload = bridge.execute(TOOL_SCREEN, {})
    assert payload["success"] is True
    assert payload["tool"] == TOOL_SCREEN
    assert payload["action"] == "screenshot"
    assert payload["result"] == b"\x89PNG\r\n\x1a\nfake-image"


def test_execute_keyboard_success_fills_result(tmp_path):
    """授权开启：键盘工具执行成功，result 含动作详情。"""
    _auth, _computer, bridge = _make_bridge(tmp_path, authorized=True)
    payload = bridge.execute(TOOL_KEYBOARD, {"action": "click", "x": 12, "y": 34})
    assert payload["success"] is True
    assert payload["tool"] == TOOL_KEYBOARD
    assert payload["action"] == "click"
    assert payload["result"] == {"action": "click", "x": 12, "y": 34}


# ------------------------------------------------------------------ #
# 2. 未授权                                                          #
# ------------------------------------------------------------------ #


def test_execute_unauthorized_raises(tmp_path):
    """未授权时 execute 抛 NotAuthorizedError，不执行任何本机动作。"""
    _auth, _computer, bridge = _make_bridge(tmp_path, authorized=False)
    with pytest.raises(NotAuthorizedError) as exc:
        bridge.execute(TOOL_SCREEN, {})
    assert exc.value.error_code == "NOT_AUTHORIZED"
    assert exc.value.http_status == 403


# ------------------------------------------------------------------ #
# 3. 高危指令确认                                                     #
# ------------------------------------------------------------------ #


def test_execute_command_confirm_rejected_returns_need_confirm(tmp_path):
    """需确认的高危指令未获确认：返回 {"error":"需要确认","authorized":False}，不执行。"""
    # 注入 confirm_fn 恒为 False（安全否决）
    _auth, _computer, bridge = _make_bridge(
        tmp_path, authorized=True, confirm_fn=lambda c: False
    )
    payload = bridge.execute(TOOL_COMMAND, {"command": "del dirty.tmp"})
    assert payload["authorized"] is False
    assert payload["success"] is False
    assert payload["error"] == "需要确认"
    assert payload["error_code"] == "NEEDS_CONFIRMATION"


def test_execute_command_confirm_allowed_passes_through(tmp_path):
    """需确认的高危指令经 confirm 放行（True 时）可正常执行。"""
    # 用非危险命令 + confirm 恒 True 验证链路放行
    _auth, _computer, bridge = _make_bridge(
        tmp_path, authorized=True, confirm_fn=lambda c: True
    )
    payload = bridge.execute(
        TOOL_COMMAND, {"command": f'"{PY}" -c "print(\'OK_AFTER_CONFIRM\')"'}
    )
    assert payload["success"] is True
    assert "OK_AFTER_CONFIRM" in (payload.get("result") or "")


# ------------------------------------------------------------------ #
# 4. 审计                                                             #
# ------------------------------------------------------------------ #


def test_audit_recorded_on_success(tmp_path):
    """授权执行成功后，audit.jsonl 追加一条 action=call_tool 审计记录。"""
    _auth, _computer, bridge = _make_bridge(tmp_path, authorized=True)
    bridge.execute(TOOL_SCREEN, {"region": "全屏"})

    # N4 起 authorize() 自身也会写审计记录；本用例只关心 call_tool 记录
    records = [r for r in _read_audit(tmp_path) if r["action"] == "call_tool"]
    assert len(records) == 1
    rec = records[0]
    assert rec["action"] == "call_tool"
    assert rec["tool"] == TOOL_SCREEN
    assert rec["authorized"] is True
    assert rec["result"] == "成功"
    assert set(("timestamp", "args", "result", "error_code")).issubset(rec)


def test_audit_recorded_on_unauthorized(tmp_path):
    """未授权被拒也记审计，error_code 可提取为 NOT_AUTHORIZED。"""
    _auth, _computer, bridge = _make_bridge(tmp_path, authorized=False)
    with pytest.raises(NotAuthorizedError):
        bridge.execute(TOOL_SCREEN, {})

    records = _read_audit(tmp_path)
    assert len(records) == 1
    assert records[0]["action"] == "call_tool"
    assert records[0]["authorized"] is False
    assert records[0]["error_code"] == "NOT_AUTHORIZED"


def test_audit_recorded_on_confirm_rejected(tmp_path):
    """高危指令未确认也被记审计，结果摘要保留 NEEDS_CONFIRMATION（不在既有已知码集时派生字段为 None）。"""
    _auth, _computer, bridge = _make_bridge(
        tmp_path, authorized=True, confirm_fn=lambda c: False
    )
    bridge.execute(TOOL_COMMAND, {"command": "del dirty.tmp"})

    # N4 起 authorize() 自身也会写审计记录；本用例只关心 call_tool 记录
    records = [r for r in _read_audit(tmp_path) if r["action"] == "call_tool"]
    assert len(records) == 1
    assert records[0]["tool"] == TOOL_COMMAND
    assert records[0]["authorized"] is False
    # 安全模块仅从固定已知码集提取派生 error_code；NEEDS_CONFIRMATION 作为文本保留在 result 中
    assert "NEEDS_CONFIRMATION" in (records[0]["result"] or "")


# ------------------------------------------------------------------ #
# 5. list_tools                                                       #
# ------------------------------------------------------------------ #


def test_list_tools_returns_three_descriptions(tmp_path):
    """list_tools 返回屏幕 / 键盘 / 指令三工具，名称与契约对齐，含 Schema 简版。"""
    _auth, _computer, bridge = _make_bridge(tmp_path, authorized=True)
    tools = bridge.list_tools()
    assert [t["name"] for t in tools] == [TOOL_SCREEN, TOOL_KEYBOARD, TOOL_COMMAND]
    for t in tools:
        assert "description" in t
        assert "parameters" in t and isinstance(t["parameters"], dict)
        assert "returns" in t and isinstance(t["returns"], dict)


# ------------------------------------------------------------------ #
# 6. 包导出 + 集成                                                    #
# ------------------------------------------------------------------ #


def test_package_exports_tool_bridge():
    """ToolBridge 从包级导出。"""
    from lite import computer_control

    assert computer_control.ToolBridge is ToolBridge


# ------------------------------------------------------------------ #
# 7. 批次1（第三轮体检）：实例授权状态不被 execute 污染                #
# ------------------------------------------------------------------ #


def test_execute_does_not_persist_computer_authorized(tmp_path):
    """M-1：execute 后 ComputerControl 实例授权状态恢复原值，不被永久污染。

    修复前 execute 强制 set_authorized(True) 且从不恢复——未授权实例经一次
    bridge 调用后独立闸门被永久打开，任何绕过 bridge 的直接调用畅通无阻。
    """
    authorizer = ControlAuthorizer(data_dir=str(tmp_path))
    authorizer.authorize()
    # 构造实例级未授权的 computer（模拟上层按 config authorized=False 装配）
    computer = ComputerControl(
        authorized=False,
        screen_backend=FakeScreenBackend(),
        keyboard_backend=FakeKeyboardBackend(),
    )
    bridge = ToolBridge(computer=computer, authorizer=authorizer)
    # bridge 链路（authorizer 已授权）执行成功
    payload = bridge.execute(TOOL_SCREEN, {})
    assert payload["success"] is True
    # 实例级闸门保持原值 False——未被永久污染
    assert computer.authorized is False
    # 绕过 bridge 的直接调用仍被实例闸门拒绝
    with pytest.raises(NotAuthorizedError):
        computer.call_tool(TOOL_SCREEN, {})


def test_execute_restores_computer_authorized_on_exception(tmp_path):
    """M-1：执行抛异常时实例授权状态也在 finally 中恢复。"""
    authorizer = ControlAuthorizer(data_dir=str(tmp_path))
    authorizer.authorize()

    class _BoomScreen:
        def screenshot(self, region=None):
            raise RuntimeError("backend boom")

    computer = ComputerControl(
        authorized=False,
        screen_backend=_BoomScreen(),
        keyboard_backend=FakeKeyboardBackend(),
    )
    bridge = ToolBridge(computer=computer, authorizer=authorizer)
    with pytest.raises(Exception):
        bridge.execute(TOOL_SCREEN, {})
    assert computer.authorized is False