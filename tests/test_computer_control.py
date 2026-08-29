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


# ------------------------------------------------------------------ #
# 11. 批次1（第三轮体检）：组合命令黑名单 / 脱敏 / 超时下界             #
# ------------------------------------------------------------------ #


@pytest.mark.parametrize(
    "combined",
    [
        "echo hi & rd /s /q C:\\x",
        "echo hi && del dirty.tmp",
        "cd temp&&del x",
        "echo a | format q:",
        "echo a; shutdown /s",
        "echo a\nrm -rf /",
        "(rd /s /q C:\\x)",
        "(del dirty.tmp)",
        "echo x & del",
    ],
)
def test_combined_dangerous_command_blocked(combined):
    """H-1：组合命令中任何一段命中黑名单即整体 BLOCKED，不执行本机动作。"""
    ctrl = _make()
    res = ctrl.run_command(combined)
    assert res.success is False
    assert res.error_code == BLOCKED
    assert "黑名单" in (res.error or "")


def test_combined_wrapper_requires_confirmation():
    """H-1：包裹器段藏在组合命令后段仍进确认闸。"""
    assert ComputerControl._requires_confirmation("echo hi & cmd /c echo ok") is True
    assert ComputerControl._requires_confirmation("echo hi | powershell -c dir") is True


def test_combined_harmless_commands_not_flagged():
    """H-1：无危险段且无包裹器段的组合命令不误伤。"""
    assert ComputerControl._is_dangerous("echo hi & echo bye") is False
    assert ComputerControl._requires_confirmation("echo hi & echo bye") is False
    # 路径含 & 的合法引用形态不误判（& 在引号内仍是分隔符，属保守方向）
    assert ComputerControl._is_dangerous('"{PY}" -c "print(1)"'.replace("{PY}", PY)) is False


def test_redact_covers_bearer_token():
    """M-4：Bearer 令牌形态纳入脱敏。"""
    from lite.computer_control import _redact

    out = _redact("Authorization: Bearer sk-abc123456")
    assert "sk-abc123456" not in out
    assert "Bearer ***" in out


def test_log_extra_is_redacted(capsys):
    """M-4：_log 的 extra 经 _redact，密钥不进终端日志。"""
    ctrl = _make()
    ctrl._log("action", TOOL_COMMAND, extra="set TOKEN=sk-secret123")
    captured = capsys.readouterr()
    assert "sk-secret123" not in captured.out
    assert "TOKEN=***" in captured.out


def test_command_timeout_lower_bound():
    """L-2：timeout_s=0 / 负数被钳制为 >= 1.0，不再立即误杀。"""
    ctrl = _make()
    res = ctrl.run_command(f'"{PY}" -c "print(\'SLOW_OK\')"', timeout_s=0)
    # 若被 0 超时误杀则 success=False 且 timed_out=True；钳制后应正常完成
    assert res.timed_out is False
    assert res.success is True
    assert "SLOW_OK" in (res.result or "")

    res_neg = ctrl.run_command(f'"{PY}" -c "print(\'NEG_OK\')"', timeout_s=-5)
    assert res_neg.timed_out is False
    assert res_neg.success is True


def test_windows_output_decoded_with_locale_encoding():
    """M-3：Windows 子进程按 locale 编码解码（中文系统 GBK），中文输出不再是 U+FFFD。"""
    ctrl = _make()
    res = ctrl.run_command(f'"{PY}" -c "print(\'中文输出测试\')"')
    assert res.success is True
    assert "中文输出测试" in (res.stdout or "")
    assert "\ufffd" not in (res.stdout or "")


# ------------------------------------------------------------------ #
# 12. 批次A（第四轮体检）：脱敏补漏 / 词元级黑名单 / override / 容错    #
# ------------------------------------------------------------------ #


def test_redact_covers_prefixed_env_style_keys():
    """批次A：前缀型环境变量名（OPENAI_API_KEY / HF_TOKEN）纳入脱敏。

    修复前 ``\\b`` 在 `_` 与词元间不成立，前缀型密钥名整体漏检。
    """
    from lite.computer_control import _redact

    assert _redact("OPENAI_API_KEY=sk-abc123") == "OPENAI_API_KEY=***"
    assert _redact("HF_TOKEN=xxx") == "HF_TOKEN=***"
    assert _redact("AWS_SECRET_ACCESS_KEY=wJalr") == "AWS_SECRET_ACCESS_KEY=***"
    assert "sk-abc123" not in _redact("OPENAI_API_KEY=sk-abc123")


def test_redact_covers_json_style_keys():
    """批次A：JSON 引号形态（"api_key": "sk-x"）纳入脱敏。"""
    from lite.computer_control import _redact

    out = _redact('"api_key": "sk-x"')
    assert out == '"api_key": "***"'
    assert "sk-x" not in out.replace('"api_key"', "")  # 键名保留、值被掩码


@pytest.mark.parametrize(
    "cmd",
    [
        "if exist x del x",
        "for %i in (1) do del c:\\x",
        "if exist x rd /s /q C:\\y",
        "d^el dirty.tmp",
        "for %i in (1) do r^mdir /s c:\\z",
    ],
)
def test_control_flow_destructive_tokens_blocked(cmd):
    """批次A：控制流包裹 / 转义形态下破坏词作为中间词元出现即整体 BLOCKED。

    修复前三层护栏全空：首 token=if 不在包裹器名单、_is_destructive 查
    delete 不查 del、黑名单段首前缀匹配不命中。
    """
    ctrl = _make()
    assert ComputerControl._is_dangerous(cmd) is True
    res = ctrl.run_command(cmd)
    assert res.success is False
    assert res.error_code == BLOCKED
    assert "黑名单" in (res.error or "")


def test_control_flow_wrappers_require_confirmation():
    """批次A：if / for / do / while 控制流关键字补入包裹器确认闸名单。"""
    assert ComputerControl._requires_confirmation("if exist file echo ok") is True
    assert ComputerControl._requires_confirmation("for %i in (1) do echo ok") is True
    assert ComputerControl._requires_confirmation("do something") is True
    # 无破坏词、无包裹器的组合命令仍不误伤
    assert ComputerControl._requires_confirmation("echo hi & echo bye") is False


def test_authorized_override_allows_single_call_without_mutating_state():
    """批次A：authorized_override=True 单次放行且不污染实例授权状态。"""
    screen = FakeScreenBackend()
    ctrl = _make(screen=screen, authorized=False)
    res = ctrl.call_tool(TOOL_SCREEN, {}, authorized_override=True)
    assert res.success is True
    assert res.authorized is True
    assert ctrl.authorized is False  # 实例状态未被读写污染
    assert screen.regions == [None]


def test_run_command_authorized_override_without_mutating_state():
    """批次A：run_command 的 authorized_override 单次放行，实例状态不变。"""
    ctrl = _make(authorized=False)
    res = ctrl.run_command(f'"{PY}" -c "print(\'OVR_OK\')"', authorized_override=True)
    assert res.success is True
    assert "OVR_OK" in (res.result or "")
    assert ctrl.authorized is False


def test_authorized_override_false_denies_authorized_instance():
    """批次A：authorized_override=False 对已授权实例单次拒绝（默认 None 走实例状态）。"""
    ctrl = _make(authorized=True)
    with pytest.raises(NotAuthorizedError):
        ctrl.call_tool(TOOL_SCREEN, {}, authorized_override=False)
    # 默认 None：沿用实例状态，正常放行
    res = ctrl.call_tool(TOOL_SCREEN, {})
    assert res.success is True


def test_run_command_invalid_timeout_falls_back():
    """批次A：timeout_s 非法（如 LLM 传入 "30s"）回退默认超时，不抛指令级异常。"""
    ctrl = _make()
    res = ctrl.run_command(f'"{PY}" -c "print(\'TIME_OK\')"', timeout_s="30s")
    assert res.success is True
    assert "TIME_OK" in (res.result or "")

    res2 = ctrl.run_command(f'"{PY}" -c "print(\'TIME_OK2\')"', timeout_s=object())
    assert res2.success is True
    assert "TIME_OK2" in (res2.result or "")


def test_timeout_second_communicate_fallback_force_close(monkeypatch):
    """批次A：超时回收后二次 communicate 仍超时 -> 强制关闭管道并返回，不无限阻塞。"""
    import subprocess as sp

    from lite.computer_control import control as control_mod

    closed = []

    class _FakeStream:
        def close(self):
            closed.append(True)

    class _FakePopen:
        """二次 communicate 均超时的假进程：验证兜底分支强制关管道。"""

        def __init__(self, *args, **kwargs):
            self.pid = 424242
            self.stdout = _FakeStream()
            self.stderr = _FakeStream()
            self.returncode = None

        def communicate(self, timeout=None):
            raise sp.TimeoutExpired(cmd="fake", timeout=timeout or 0)

        def kill(self):
            pass

        def wait(self, timeout=None):
            return None

    monkeypatch.setattr(control_mod.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(control_mod.subprocess, "run", lambda *a, **kw: None)

    ctrl = _make()
    exit_code, stdout, stderr, timed_out = ctrl._run_subprocess("echo hang", 0.1)
    assert timed_out is True
    assert exit_code is None
    assert stdout == "" and stderr == ""
    assert len(closed) == 2  # stdout / stderr 均被强制关闭