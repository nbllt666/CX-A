# -*- coding: utf-8 -*-
"""Task D1 电脑控制核心单元测试。

覆盖（全 mock backend，禁用真实屏幕 / 键盘 / 危险命令）：
- call_tool 未授权抛 NotAuthorizedError（authorized=False）；
- 屏幕控制：mock 屏幕后端返回字节 -> ScreenResult(action="screenshot")；
- 键盘控制：click / type_text 的 mock 调用断言与返回；
- 指令成功：无害命令返回 exit_code / stdout；
- 指令超时：长睡眠命令 timeout 后 timed_out=True；
- 输出截断：超过 output_limit 置 truncated=True；
- 脱敏：含 api_key=sk-xxx 的输出被掩码；
- 黑名单：del / shutdown 类命令返回 BLOCKED。
"""

import sys

import pytest

from lite.computer_control import (
    TOOL_COMMAND,
    TOOL_KEYBOARD,
    TOOL_SCREEN,
    BLOCKED,
    CommandResult,
    ComputerControl,
    InvalidArgumentError,
    KeyboardResult,
    NotAuthorizedError,
    ScreenResult,
)

#: 当前 python 解释器可执行文件，用于构建无害子进程命令
PY = sys.executable


# ------------------------------------------------------------------ #
# mock 后端                                                          #
# ------------------------------------------------------------------ #


class FakeScreenBackend:
    """内存 mock 屏幕后端：返回固定字节，记录 region 调用。"""

    def __init__(self, image: bytes = b"\x89PNG\r\n\x1a\nfake-image"):
        self.image = image
        self.regions = []

    def screenshot(self, region=None) -> bytes:
        self.regions.append(region)
        return self.image


class FakeKeyboardBackend:
    """内存 mock 键盘后端：记录 click / type_text 调用但无真实副作用。"""

    def __init__(self):
        self.clicks = []
        self.types = []

    def click(self, x, y) -> None:
        self.clicks.append((x, y))

    def type_text(self, text) -> None:
        self.types.append(text)


def _make(screen=None, keyboard=None, **kw) -> ComputerControl:
    """构造 ComputerControl，缺省注入全 mock 后端。"""
    return ComputerControl(
        screen_backend=screen or FakeScreenBackend(),
        keyboard_backend=keyboard or FakeKeyboardBackend(),
        **kw,
    )


# ------------------------------------------------------------------ #
# 1. 授权                                                             #
# ------------------------------------------------------------------ #


def test_call_tool_unauthorized_raises_not_authorized():
    """未授权时 call_tool 立即抛 NotAuthorizedError，且不触碰后端。"""
    screen = FakeScreenBackend()
    ctrl = _make(screen=screen, authorized=False)
    with pytest.raises(NotAuthorizedError) as exc:
        ctrl.call_tool(TOOL_SCREEN, {})
    assert exc.value.error_code == "NOT_AUTHORIZED"
    assert exc.value.http_status == 403
    assert screen.regions == []  # 未执行任何本机动作


def test_unauthorized_for_all_tools():
    """未授权时任何工具（含 execute 别名）均拒绝。"""
    ctrl = _make(authorized=False)
    for tool in (TOOL_SCREEN, TOOL_KEYBOARD, TOOL_COMMAND):
        with pytest.raises(NotAuthorizedError):
            ctrl.call_tool(tool, {})


def test_set_authorized_toggle():
    """set_authorized 可运行时切换授权状态。"""
    ctrl = _make(authorized=False)
    with pytest.raises(NotAuthorizedError):
        ctrl.run_command(f'"{PY}" -c "print(\'x\')"')
    ctrl.set_authorized(True)
    assert ctrl.authorized is True
    res = ctrl.run_command(f'"{PY}" -c "print(\'x\')"')
    assert res.success is True


# ------------------------------------------------------------------ #
# 2. 屏幕控制                                                         #
# ------------------------------------------------------------------ #


def test_screenshot_action_mock_backend():
    """屏幕截图委托 mock 后端，返回字节并携带 action="screenshot"。"""
    screen = FakeScreenBackend(image=b"\x00\x01\x02")
    ctrl = _make(screen=screen)
    res = ctrl.screenshot_action()
    assert isinstance(res, ScreenResult)
    assert res.success is True
    assert res.action == "screenshot"
    assert res.result == b"\x00\x01\x02"
    assert res.tool == TOOL_SCREEN
    assert screen.regions == [None]


def test_screenshot_action_region_passed():
    """region 参数透传给 mock 后端。"""
    screen = FakeScreenBackend()
    ctrl = _make(screen=screen)
    region = {"left": 0, "top": 0, "width": 800, "height": 600}
    res = ctrl.call_tool(TOOL_SCREEN, {"region": region})
    assert res.success is True
    assert screen.regions == [region]


# ------------------------------------------------------------------ #
# 3. 键盘控制                                                         #
# ------------------------------------------------------------------ #


def test_click_action_mock_backend():
    """click 委托 mock 后端，记录坐标并返回 KeyboardResult(action="click")。"""
    kb = FakeKeyboardBackend()
    ctrl = _make(keyboard=kb)
    res = ctrl.click_action(120, 340)
    assert isinstance(res, KeyboardResult)
    assert res.success is True
    assert res.action == "click"
    assert res.tool == TOOL_KEYBOARD
    assert kb.clicks == [(120, 340)]


def test_type_action_mock_backend():
    """type_text 委托 mock 后端，记录文本并返回 KeyboardResult(action="type")。"""
    kb = FakeKeyboardBackend()
    ctrl = _make(keyboard=kb)
    res = ctrl.type_action("你好 world")
    assert isinstance(res, KeyboardResult)
    assert res.success is True
    assert res.action == "type"
    assert kb.types == ["你好 world"]


def test_keyboard_call_tool_dispatch_click_and_type():
    """call_tool 键盘分派：action=click / action=type 各自路由到对应后端。"""
    kb = FakeKeyboardBackend()
    ctrl = _make(keyboard=kb)
    ctrl.call_tool(TOOL_KEYBOARD, {"action": "click", "x": 1, "y": 2})
    ctrl.call_tool(TOOL_KEYBOARD, {"action": "type", "text": "abc"})
    assert kb.clicks == [(1, 2)]
    assert kb.types == ["abc"]


def test_keyboard_call_tool_invalid_action():
    """键盘 action 非法时抛 InvalidArgumentError，不触碰后端。"""
    kb = FakeKeyboardBackend()
    ctrl = _make(keyboard=kb)
    with pytest.raises(InvalidArgumentError) as exc:
        ctrl.call_tool(TOOL_KEYBOARD, {"action": "fly"})
    assert exc.value.error_code == "INVALID_ARGUMENT"
    assert exc.value.http_status == 400
    assert kb.clicks == [] and kb.types == []


def test_unknown_tool_raises_invalid_argument():
    """未知工具抛 InvalidArgumentError。"""
    ctrl = _make()
    with pytest.raises(InvalidArgumentError):
        ctrl.call_tool("computer_fly", {})


# ------------------------------------------------------------------ #
# 4. 指令成功                                                          #
# ------------------------------------------------------------------ #


def test_command_success_exit_zero_and_stdout():
    """无害命令成功：exit_code=0 且 stdout 含预期内容。"""
    ctrl = _make()
    res = ctrl.run_command(f'"{PY}" -c "print(\'EXEC_OK\')"')
    assert isinstance(res, CommandResult)
    assert res.success is True
    assert res.exit_code == 0
    assert "EXEC_OK" in res.stdout


# ------------------------------------------------------------------ #
# 5. 指令超时                                                          #
# ------------------------------------------------------------------ #


def test_command_timeout_timed_out_true():
    """长睡眠命令超时：timed_out=True，error_code=TIMEOUT，success=False。"""
    ctrl = _make(timeout_s=30)
    res = ctrl.run_command(
        f'"{PY}" -c "import time;time.sleep(10)"', timeout_s=0.5
    )
    assert res.timed_out is True
    assert res.success is False
    assert res.error_code == "TIMEOUT"
    assert res.exit_code is None


# ------------------------------------------------------------------ #
# 6. 输出截断                                                          #
# ------------------------------------------------------------------ #


def test_command_output_truncation():
    """输出超过 output_limit 时 truncated=True 且长度受限。"""
    ctrl = _make(output_limit=100)
    res = ctrl.run_command(f'"{PY}" -c "print(\'A\'*1000)"')
    assert res.truncated is True
    # 1000 个 A + 换行 = 1001 字节；limit=100，故实际返回 <=100 字节
    assert len(res.stdout.encode("utf-8")) <= 100


def test_command_output_not_truncated_when_small():
    """小输出不置 truncated。"""
    ctrl = _make(output_limit=8192)
    res = ctrl.run_command(f'"{PY}" -c "print(\'tiny\')"')
    assert res.truncated is False
    assert "tiny" in res.stdout


# ------------------------------------------------------------------ #
# 7. 脱敏                                                              #
# ------------------------------------------------------------------ #


def test_command_output_redacted_secret():
    """stderr / stdout 中 api_key=sk-xxx 类密钥被掩码。"""
    ctrl = _make()
    res = ctrl.run_command(f'"{PY}" -c "print(\'api_key=sk-1234567890\')"')
    assert "api_key=***" in res.stdout
    assert "sk-1234567890" not in res.stdout


def test_redact_helper_covers_password_token():
    """脱敏辅助覆盖 password / token 模式。"""
    from lite.computer_control import _redact

    out = _redact("token: abc123 password = 'hunter2' api_key=sk-secret")
    assert "abc123" not in out
    assert "hunter2" not in out
    assert "sk-secret" not in out
    assert "***" in out


# ------------------------------------------------------------------ #
# 8. 黑名单                                                            #
# ------------------------------------------------------------------ #


@pytest.mark.parametrize(
    "dangerous",
    [
        "del dirty.tmp",
        "rmdir /s backup",
        "rd /q temp",
        "rm -rf /",
        "format c:",
        "shutdown /s /t 0",
        "reboot",
        "reg delete HKLM\\x /f",
        "powershell -c Remove-Item C:\\foo -Recurse",
        "del",
    ],
)
def test_dangerous_command_blocked(dangerous):
    """危险指令命中黑名单：success=False，error_code=BLOCKED，且不触碰本机。"""
    ctrl = _make()
    res = ctrl.run_command(dangerous)
    assert isinstance(res, CommandResult)
    assert res.success is False
    assert res.error_code == BLOCKED
    assert "黑名单" in (res.error or "")


def test_not_dangerous_command_allowed():
    """非危险无害命令不被误拦。"""
    ctrl = _make()
    res = ctrl.run_command(f'"{PY}" -c "print(\'safe\')"')
    assert res.success is True
    assert res.error_code is None


# ------------------------------------------------------------------ #
# 9. 可导入性 / execute 别名                                            #
# ------------------------------------------------------------------ #


def test_package_exports_alignment():
    """包导出与契约对齐：三个工具标识 + 主类 + 结果 / 异常类。"""
    from lite import computer_control

    assert computer_control.TOOL_SCREEN == "computer_screen_control"
    assert computer_control.TOOL_KEYBOARD == "computer_keyboard_control"
    assert computer_control.TOOL_COMMAND == "computer_run_command"
    assert computer_control.ComputerControl is ComputerControl


def test_execute_alias_matches_call_tool():
    """execute 与 call_tool 等价（同义别名）。"""
    ctrl = _make()
    screen = FakeScreenBackend()
    ctrl2 = _make(screen=screen)
    res1 = ctrl2.call_tool(TOOL_SCREEN, {})
    res2 = ctrl2.execute(TOOL_SCREEN, {})
    assert res1.success == res2.success is True
    assert res1.action == res2.action == "screenshot"


def test_error_http_status_mapping():
    """错误类的 error_code / http_status 与 pyi 对齐。"""
    assert NotAuthorizedError("x").http_status == 403
    assert InvalidArgumentError("x").http_status == 400
    from lite.computer_control import ExecutionError, TimeoutError

    assert TimeoutError("x").error_code == "TIMEOUT"
    assert TimeoutError("x").http_status == 504
    assert ExecutionError("x").error_code == "EXECUTION_FAILED"
    assert ExecutionError("x").http_status == 500


# ------------------------------------------------------------------ #
# 10. 包裹器确认闸（N2，20260828_模块0_API鉴权与安全链路修复·批次A）    #
# ------------------------------------------------------------------ #


def test_requires_confirmation_wrapper_first_token():
    """N2：_requires_confirmation 解析首 token（裸名 / 大小写 / 引号绝对路径）。"""
    assert ComputerControl._requires_confirmation("cmd /c del C:\\x") is True
    assert ComputerControl._requires_confirmation("POWERSHELL -c ri C:\\x") is True
    assert ComputerControl._requires_confirmation('"C:\\Windows\\System32\\cmd.exe" /c rd /q temp') is True
    assert ComputerControl._requires_confirmation("start notepad") is True
    assert ComputerControl._requires_confirmation("") is False


def test_requires_confirmation_allows_plain_commands():
    """N2：普通命令与既有测试用的带引号 python.exe 全路径不被误报。"""
    assert ComputerControl._requires_confirmation(f'"{PY}" -c "print(1)"') is False
    assert ComputerControl._requires_confirmation("notepad") is False
    assert ComputerControl._requires_confirmation("dir /s") is False