# -*- coding: utf-8 -*-
"""完全内置的电脑控制核心（Task D1）：ComputerControl。

本实现为**完全内置**版本——不走 CXFC 插件，但工具命名 / 参数 / 返回结构
与 `C:\\CX-O\\public\\interface_stub\\computer_control.pyi` 对齐。

对齐要点：
- 三个工具稳定标识：``computer_screen_control`` / ``computer_keyboard_control`` /
  ``computer_run_command``；
- 返回外壳：``ToolResult`` / ``ScreenResult`` / ``KeyboardResult`` / ``CommandResult``；
- 错误契约：``NotAuthorizedError(403)`` / ``InvalidArgumentError(400)`` /
  ``TimeoutError(504)`` / ``ExecutionError(500)``，均带 ``error_code`` 与
  ``http_status`` 属性；
- 安全边界：本地授权未开启时抛 ``NotAuthorizedError``，**不执行任何本机动作**。

设计要点（便于无显卡 / 无依赖测试）：
- ``screen_backend`` / ``keyboard_backend`` 为注入点，仅封装本机副作用；
- 运行指令（run_command）走本模块自带 ``subprocess``，默认超时 kill 进程树、
  输出按字节限量截断、密钥类模式脱敏、危险指令黑名单拦截。

路径 / 导入规范：本模块使用标准库与包绝对导入。
"""

from __future__ import annotations

import locale
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    # 工具稳定标识
    "TOOL_SCREEN",
    "TOOL_KEYBOARD",
    "TOOL_COMMAND",
    # 错误契约
    "PluginError",
    "NotAuthorizedError",
    "InvalidArgumentError",
    "ControlTimeoutError",
    "TimeoutError",  # 兼容别名（L-14 重命名后保留）
    "ExecutionError",
    # 结果结构
    "ToolResult",
    "ScreenResult",
    "KeyboardResult",
    "CommandResult",
    # 主类
    "ComputerControl",
    "DANGEROUS_COMMANDS",
    "CONFIRM_REQUIRED_WRAPPERS",
    "BLOCKED",
]

# --------------------------------------------------------------------------- #
# 工具稳定标识（与 pyi 对齐）                                                  #
# --------------------------------------------------------------------------- #

#: 屏幕控制工具稳定标识
TOOL_SCREEN = "computer_screen_control"
#: 键盘控制工具稳定标识
TOOL_KEYBOARD = "computer_keyboard_control"
#: 运行指令工具稳定标识
TOOL_COMMAND = "computer_run_command"

#: 黑名单拦截专用错误码（非 pyi 定义的 HTTP 错误，用于拒绝危险指令）
BLOCKED = "BLOCKED"


# --------------------------------------------------------------------------- #
# 错误契约（内联定义，error_code / http_status 与 pyi 对齐）                   #
# --------------------------------------------------------------------------- #


class PluginError(Exception):
    """所有电脑控制错误的基类，携带错误码与建议 HTTP 状态码。"""

    #: 统一错误码
    error_code: str = "SYSTEM_ERROR"
    #: 建议映射的 HTTP 状态码
    http_status: int = 500

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.__class__.__doc__ or self.error_code)
        self.message = str(self)

    def __str__(self) -> str:
        return super().__str__()


class NotAuthorizedError(PluginError):
    """本地授权未开启（授权被撤销或未授权）。不执行任何本机动作。"""

    error_code: str = "NOT_AUTHORIZED"
    http_status: int = 403


class InvalidArgumentError(PluginError):
    """工具名或参数不符合契约。不执行本机动作。"""

    error_code: str = "INVALID_ARGUMENT"
    http_status: int = 400


class ControlTimeoutError(PluginError):
    """执行超时：超出 timeout_s，已回收整个进程树。

    L-14（第三轮体检批次5）：原类名 ``TimeoutError`` 遮蔽内建同名异常——任何
    模块一旦 ``from lite.computer_control import TimeoutError``，其作用域内
    ``except TimeoutError`` 将不再捕获 socket/subprocess 内建超时，语义静默
    反转。现更名 ControlTimeoutError，模块级保留旧名兼容别名。
    """

    error_code: str = "TIMEOUT"
    http_status: int = 504


#: 兼容别名：历史导入名（pyi 对齐期命名）。新代码请使用 :class:`ControlTimeoutError`。
TimeoutError = ControlTimeoutError


class ExecutionError(PluginError):
    """工具执行失败：进程启动失败、系统权限不足或执行链路错误。"""

    error_code: str = "EXECUTION_FAILED"
    http_status: int = 500


# --------------------------------------------------------------------------- #
# 结果结构（dataclass，字段与 pyi 对齐）                                       #
# --------------------------------------------------------------------------- #


@dataclass
class ToolResult:
    """工具调用的公共返回外壳。"""

    #: 是否成功
    success: bool = False
    #: 工具稳定标识
    tool: str = ""
    #: 工具返回的数据（屏幕字节 / 键盘动作描述 / 指令输出等）
    result: Any = None
    #: 失败原因描述；成功时为 None
    error: Optional[str] = None
    #: 错误码；成功时为 None
    error_code: Optional[str] = None
    #: 调用时的授权状态
    authorized: bool = True


@dataclass
class ScreenResult(ToolResult):
    """屏幕控制返回：在 ToolResult 基础上携带 action。"""

    #: 已执行的动作标识
    action: str = "screenshot"


@dataclass
class KeyboardResult(ToolResult):
    """键盘控制返回：在 ToolResult 基础上携带 action。"""

    #: 已执行的动作标识（click / type）
    action: str = ""


@dataclass
class CommandResult(ToolResult):
    """运行指令返回：在 ToolResult 基础上携带执行细节。"""

    #: 子进程退出码；超时或被拒绝时为 None
    exit_code: Optional[int] = None
    #: 标准输出（已截断）
    stdout: str = ""
    #: 标准错误（已截断）
    stderr: str = ""
    #: 是否超时
    timed_out: bool = False
    #: 输出是否被截断（超过 output_limit）
    truncated: bool = False


# --------------------------------------------------------------------------- #
# 危险指令黑名单                                                              #
# --------------------------------------------------------------------------- #

#: 危险指令前缀黑名单。命中（命令去首尾空白、转小写后，等于或以其起始）
#: 一律拒绝执行，返回 error_code=BLOCKED，不触碰本机。
DANGEROUS_COMMANDS: List[str] = [
    "del",
    "rmdir",
    "rd",
    "rm",
    "format",
    "shutdown",
    "reboot",
    "reg delete",
    "remove-item",
    "taskkill /f",
    "rmtree",
    "powershell -c remove",
    "powershell -executionpolicy bypass -command remove",
]

#: 需强制二次确认的包裹器名单（N2，20260828_模块0_API鉴权与安全链路修复）：
#: 命令首 token（含带引号绝对路径形态取 basename）命中此名单时，黑名单之外仍
#: 强制进入高危确认闸——防止 ``cmd /c del ...`` / ``powershell -c ri ...`` /
#: ``C:\\...\\cmd.exe /c rd ...`` 等包裹器 / 别名形态整体绕过黑名单。
CONFIRM_REQUIRED_WRAPPERS: List[str] = [
    "cmd",
    "cmd.exe",
    "powershell",
    "powershell.exe",
    "pwsh",
    "bash",
    "sh",
    "sh.exe",
    "wscript",
    "wscript.exe",
    "cscript",
    "cscript.exe",
    "mshta",
    "rundll32",
    "regsvr32",
    "python",
    "pythonw",
    "node",
    "start",
]


# 密钥 / 密码类模式：api_key / apikey / password / passwd / token / secret
# 后跟冒号或等号分隔的值，命中的值部分整体脱敏为 ***。
_REDACT_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|password|passwd|token|secret)(\s*[:=]\s*)([^\s,:;]+)"
)

# Bearer 令牌形态：``Authorization: Bearer sk-xxx`` 中的令牌值脱敏（M-4）
_BEARER_PATTERN = re.compile(r"(?i)\b(bearer\s+)([^\s,;\"']+)")


def _redact(text: str) -> str:
    """脱敏常见密钥 / 密码模式：``api_key=sk-xxx`` -> ``api_key=***``。

    同时覆盖 ``Bearer <token>`` 形态（M-4，20260828_模块0_电脑控制安全链修复）。

    :param text: 待处理的文本
    :return: 命中模式的值被掩码后的文本
    """
    text = _REDACT_PATTERN.sub(r"\1\2***", text)
    return _BEARER_PATTERN.sub(r"\1***", text)


# --------------------------------------------------------------------------- #
# 默认后端（未注入时的兜底，避免真实触碰屏幕 / 键盘）                          #
# --------------------------------------------------------------------------- #


class _DefaultScreenBackend:
    """默认屏幕后端：未注入时调用即报错，避免无显卡环境误触碰真实屏幕。"""

    def screenshot(self, region: Optional[Dict[str, Any]] = None) -> bytes:
        raise ExecutionError(
            "未注入 screen_backend，本内置版本不触碰真实屏幕；"
            "请在测试或运行环境注入 mock 或真实截图实现。"
        )


class _DefaultKeyboardBackend:
    """默认键盘后端：未注入时调用即报错，避免误操作真实键鼠。"""

    def click(self, x: int, y: int) -> None:
        raise ExecutionError(
            "未注入 keyboard_backend，本内置版本不触碰真实鼠标；"
            "请在测试或运行环境注入 mock 或真实键鼠实现。"
        )

    def type_text(self, text: str) -> None:
        raise ExecutionError(
            "未注入 keyboard_backend，本内置版本不触碰真实键盘；"
            "请在测试或运行环境注入 mock 或真实键鼠实现。"
        )


# --------------------------------------------------------------------------- #
# 主类 ComputerControl                                                        #
# --------------------------------------------------------------------------- #


class ComputerControl:
    """完全内置电脑控制（屏幕 / 键盘 / 运行指令）的核心类。

    :param authorized: 初始授权状态，默认开启；关闭后任何调用抛 NotAuthorizedError
    :param confirm_dangerous: 是否对危险指令二次确认（暂为约定字段，黑名单硬拒）
    :param timeout_s: 指令默认超时秒数，默认 30；超时 kill 进程树
    :param output_limit: 指令输出截断字节数，默认 8192
    :param data_dir: 数据目录（供截图落地等扩展使用，Task D 后续取用）
    :param screen_backend: 屏幕后端，需提供 ``screenshot(region=None) -> bytes``
    :param keyboard_backend: 键盘后端，需提供 ``click(x, y)`` 与 ``type_text(text)``
    """

    def __init__(
        self,
        authorized: bool = True,
        confirm_dangerous: bool = True,
        timeout_s: int = 30,
        output_limit: int = 8192,
        data_dir: Optional[str] = None,
        screen_backend: Any = None,
        keyboard_backend: Any = None,
    ) -> None:
        self._authorized = bool(authorized)
        self.confirm_dangerous = bool(confirm_dangerous)
        self.timeout_s = int(timeout_s or 30)
        self.output_limit = int(output_limit or 8192)
        self.data_dir = data_dir
        self._screen_backend: Any = screen_backend or _DefaultScreenBackend()
        self._keyboard_backend: Any = keyboard_backend or _DefaultKeyboardBackend()

    # ------------------------------------------------------------------ #
    # 授权                                                               #
    # ------------------------------------------------------------------ #

    @property
    def authorized(self) -> bool:
        """当前授权状态（D2 取用）。"""
        return self._authorized

    def set_authorized(self, authorized: bool) -> None:
        """设置授权状态。

        :param authorized: True 允许执行本机动作；False 关闭（拒绝一切调用）
        """
        self._authorized = bool(authorized)

    def _ensure_authorized(self, tool: str) -> None:
        """授权闸门：未授权立即抛 NotAuthorizedError，不执行任何本机动作。

        :param tool: 待执行工具标识（仅用于日志）
        :raises NotAuthorizedError: 授权未开启
        """
        if not self._authorized:
            self._log("deny", tool)
            raise NotAuthorizedError(
                f"本地授权未开启，已拒绝电脑控制调用（{tool}），未执行任何本机动作。"
            )

    # ------------------------------------------------------------------ #
    # 统一入口                                                           #
    # ------------------------------------------------------------------ #

    def call_tool(self, tool: str, arguments: Dict[str, Any]) -> ToolResult:
        """统一调用入口：按 tool 分派到屏幕 / 键盘 / 指令。

        顺序约束（与 pyi 调用顺序对齐到本内置版本）：
        1. 本地授权未开启 -> 抛 NotAuthorizedError；
        2. 未知 tool -> 抛 InvalidArgumentError；
        3. 参数 / 执行失败 -> 对应错误或结果外壳返回。

        :param tool: 工具稳定标识（TOOL_SCREEN / TOOL_KEYBOARD / TOOL_COMMAND）
        :param arguments: 结构化工具参数
        :return: ToolResult 或子类
        :raises NotAuthorizedError: 授权未开启
        :raises InvalidArgumentError: 未知工具或参数非法
        """
        self._ensure_authorized(tool)
        arguments = arguments or {}

        if tool == TOOL_SCREEN:
            return self.screenshot_action(arguments.get("region"))

        if tool == TOOL_KEYBOARD:
            action = (arguments.get("action") or "").lower()
            if action == "click":
                return self.click_action(arguments.get("x"), arguments.get("y"))
            if action in ("type", "type_text") or "text" in arguments:
                return self.type_action(arguments.get("text", ""))
            raise InvalidArgumentError(
                f"键盘工具参数非法：需 action=click 或 type，收到 {action!r}"
            )

        if tool == TOOL_COMMAND:
            return self.run_command(
                arguments.get("command", ""),
                timeout_s=arguments.get("timeout_s"),
            )

        self._log("unknown", tool)
        raise InvalidArgumentError(f"未知电脑控制工具：{tool!r}")

    def execute(self, tool: str, arguments: Dict[str, Any]) -> ToolResult:
        """``call_tool`` 的同义别名，供内部 / 调用方语义化使用。

        :param tool: 工具稳定标识
        :param arguments: 结构化工具参数
        :return: ToolResult 或子类
        """
        return self.call_tool(tool, arguments)

    # ------------------------------------------------------------------ #
    # 屏幕控制                                                           #
    # ------------------------------------------------------------------ #

    def screenshot_action(self, region: Optional[Dict[str, Any]] = None) -> ScreenResult:
        """截取屏幕，委托 screen_backend.screenshot(region)。

        :param region: 可选裁剪区域（后端约定的 dict，如 {left, top, width, height}）
        :return: ScreenResult；result 为图像字节
        """
        self._ensure_authorized(TOOL_SCREEN)
        self._log("action", TOOL_SCREEN, extra=f"region={region}")
        try:
            image = self._screen_backend.screenshot(region)
        except PluginError:
            raise
        except Exception as exc:  # noqa: BLE001 - 转换兜底后统一抛执行错误
            raise ExecutionError(f"屏幕截图失败：{exc}") from exc
        return ScreenResult(
            success=True,
            tool=TOOL_SCREEN,
            result=image,
            action="screenshot",
            authorized=self._authorized,
        )

    # ------------------------------------------------------------------ #
    # 键盘控制                                                           #
    # ------------------------------------------------------------------ #

    def click_action(self, x: int, y: int) -> KeyboardResult:
        """在 (x, y) 处点击，委托 keyboard_backend.click(x, y)。

        :param x: 横坐标
        :param y: 纵坐标
        :return: KeyboardResult（action="click"）
        """
        self._ensure_authorized(TOOL_KEYBOARD)
        self._log("action", TOOL_KEYBOARD, extra=f"click=({x},{y})")
        try:
            self._keyboard_backend.click(x, y)
        except PluginError:
            raise
        except Exception as exc:  # noqa: BLE001 - 转换兜底
            raise ExecutionError(f"点击失败 ({x},{y})：{exc}") from exc
        return KeyboardResult(
            success=True,
            tool=TOOL_KEYBOARD,
            result={"action": "click", "x": x, "y": y},
            action="click",
            authorized=self._authorized,
        )

    def type_action(self, text: str) -> KeyboardResult:
        """输入文本，委托 keyboard_backend.type_text(text)。

        :param text: 要输入的文本
        :return: KeyboardResult（action="type"）
        """
        self._ensure_authorized(TOOL_KEYBOARD)
        self._log("action", TOOL_KEYBOARD, extra="type")
        try:
            self._keyboard_backend.type_text(text)
        except PluginError:
            raise
        except Exception as exc:  # noqa: BLE001 - 转换兜底
            raise ExecutionError(f"文本输入失败：{exc}") from exc
        return KeyboardResult(
            success=True,
            tool=TOOL_KEYBOARD,
            result={"action": "type", "text": text},
            action="type",
            authorized=self._authorized,
        )

    # ------------------------------------------------------------------ #
    # 运行指令                                                           #
    # ------------------------------------------------------------------ #

    def run_command(
        self, command: str, timeout_s: Optional[int] = None
    ) -> CommandResult:
        """运行指令，自带护栏：授权 / 黑名单 / 超时杀进程树 / 输出截断 / 脱敏。

        本方法不抛指令级异常，一律以 CommandResult 返回（成功或失败）；
        仅授权未开启时抛 NotAuthorizedError。

        :param command: 要执行的指令字符串
        :param timeout_s: 超时秒数；缺省用构造时的 timeout_s
        :return: CommandResult
        :raises NotAuthorizedError: 授权未开启
        """
        self._ensure_authorized(TOOL_COMMAND)
        self._log("action", TOOL_COMMAND, extra=command[:80])

        if self._is_dangerous(command):
            return CommandResult(
                success=False,
                tool=TOOL_COMMAND,
                error="黑名单拦截：指令命中危险名单，已拒绝执行",
                error_code=BLOCKED,
                authorized=self._authorized,
                exit_code=None,
                stdout="",
                stderr="",
            )

        timeout = float(timeout_s) if timeout_s is not None else float(self.timeout_s)
        # L-2：超时下界钳制，防止 timeout_s=0 / 负数对未瞬时结束进程立即误杀
        timeout = max(timeout, 1.0)
        try:
            exit_code, stdout, stderr, timed_out = self._run_subprocess(command, timeout)
        except ExecutionError:
            raise
        except Exception as exc:  # noqa: BLE001 - 启动失败回退
            raise ExecutionError(f"指令启动失败：{exc}") from exc

        stdout, stdout_trunc = self._truncate(stdout or "")
        stderr, stderr_trunc = self._truncate(stderr or "")
        stdout = _redact(stdout)
        stderr = _redact(stderr)

        if timed_out:
            return CommandResult(
                success=False,
                tool=TOOL_COMMAND,
                error=f"执行超时（>{timeout:g}s），已回收进程树",
                error_code="TIMEOUT",
                authorized=self._authorized,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                timed_out=True,
                truncated=stdout_trunc or stderr_trunc,
            )

        if exit_code == 0:
            return CommandResult(
                success=True,
                tool=TOOL_COMMAND,
                result=stdout,
                authorized=self._authorized,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                truncated=stdout_trunc or stderr_trunc,
            )

        return CommandResult(
            success=False,
            tool=TOOL_COMMAND,
            error=f"指令退出码非 0：{exit_code}",
            error_code="EXECUTION_FAILED",
            authorized=self._authorized,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            truncated=stdout_trunc or stderr_trunc,
        )

    # ------------------------------------------------------------------ #
    # 护栏内部实现                                                       #
    # ------------------------------------------------------------------ #

    #: 危险前缀后的合法边界字符（空格 / 制表 / 开关符 / 路径符 / 冒号 / 点）
    #: 使 "del dirty.tmp" 命中 "del"，同时避免误伤 "delete..." 等正常词。
    _DANGEROUS_BOUNDARY = " \t-/\\:."

    #: 组合命令分隔符：&&、||、&、|、; 与换行
    #: （H-1：Windows shell 可串接多条子命令，需逐段判定防绕过）
    _COMMAND_SPLIT_PATTERN = re.compile(r"&&|\|\||[&|;\n\r]")

    @classmethod
    def _split_command_segments(cls, command: str) -> List[str]:
        """把组合命令拆分为子命令段（H-1，20260828_模块0_电脑控制安全链修复）。

        Windows shell 下 ``&``/``&&``/``|``/``;`` 与换行可串接多条子命令，
        括号分组可改变首 token——逐段判定防止 ``echo hi & rd /s /q C:\\x``
        这类"首段无害、后段危险"的组合命令绕过黑名单与确认闸。
        括号统一视作段边界处理，防止 ``(rd /s /q x)`` 以 "(" 开头骗过首 token 判定。

        :param command: 原始指令字符串
        :return: 拆分后的非空子命令段列表；整串无分隔符时为单段
        """
        cmd = command or ""
        # 括号分组视作段边界（替换为分号强制分段）
        cmd = cmd.replace("(", "; ").replace(")", "; ")
        segments = [seg.strip() for seg in cls._COMMAND_SPLIT_PATTERN.split(cmd)]
        nonempty = [seg for seg in segments if seg]
        return nonempty or [cmd.strip()]

    @classmethod
    def _is_dangerous(cls, command: str) -> bool:
        """判定命令是否命中危险黑名单（H-1：逐段判定，任一段命中即整体命中）。

        对每个子命令段独立做前缀 + 边界匹配，组合命令中任何一段命中
        危险名单即整体判定为危险，杜绝"首段无害掩护后段危险"的绕过。

        :param command: 指令字符串
        :return: True 表示危险
        """
        for segment in cls._split_command_segments(command):
            cmd = segment.lower()
            if not cmd:
                continue
            for dangerous in DANGEROUS_COMMANDS:
                d = dangerous.lower().strip()
                if cmd == d:
                    return True
                if cmd.startswith(d) and cmd[len(d)] in cls._DANGEROUS_BOUNDARY:
                    return True
        return False

    @staticmethod
    def _first_token_basename(segment: str) -> str:
        """取单个命令段首 token 的 basename（引号路径 / 盘符路径统一处理）。

        :param segment: 单个子命令段
        :return: 首 token 的 basename（小写）
        """
        segment = segment.strip()
        if not segment:
            return ""
        if segment[0] in ('"', "'"):
            end = segment.find(segment[0], 1)
            first = segment[1:end] if end > 0 else segment[1:]
        else:
            first = segment.split()[0]
        # basename 统一按两种分隔符切分：Windows 反斜杠在 POSIX 平台不被
        # os.path.basename 识别，故手动再切一次，保证跨平台判定一致
        basename = os.path.basename(first).lower()
        return basename.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]

    @classmethod
    def _requires_confirmation(cls, command: str) -> bool:
        """判定命令是否命中包裹器名单（N2 + H-1：逐段判定首 token）。

        每个子命令段独立取首 token（含带引号绝对路径形态取 basename）判定
        是否命中 ``CONFIRM_REQUIRED_WRAPPERS``——组合命令中任何一段首 token
        为包裹器即整体需进高危确认闸。黑名单命中（``_is_dangerous``）仍优先
        直接 BLOCKED，两者叠加生效。

        :param command: 指令字符串
        :return: True 表示存在包裹器段，需经高危确认闸
        """
        cmd = (command or "").strip()
        if not cmd:
            return False
        for segment in cls._split_command_segments(cmd):
            if not segment:
                continue
            if cls._first_token_basename(segment) in CONFIRM_REQUIRED_WRAPPERS:
                return True
        return False

    def _run_subprocess(self, command: str, timeout: float) -> Tuple[Optional[int], str, str, bool]:
        """底层进程执行：Windows 套 shell、CREATE_NO_WINDOW；超时回收进程树。

        :param command: 指令字符串
        :param timeout: 超时秒数
        :return: (exit_code, stdout, stderr, timed_out)；超时后 exit_code 为 None
        """
        creationflags = 0
        start_new_session = False
        if os.name == "nt":  # pragma: no cover - 平台分支
            args = command
            shell = True
            creationflags = (
                subprocess.CREATE_NEW_PROCESS_GROUP
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        else:  # pragma: no cover - POSIX 分支
            args = ["/bin/sh", "-c", command]
            shell = False
            start_new_session = True

        proc = subprocess.Popen(
            args,
            shell=shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            start_new_session=start_new_session,
            # M-3：Windows shell 输出跟随控制台代码页（中文系统 GBK/cp936），
            # 按 locale 编码解码避免中文输出全为 U+FFFD；POSIX 保持 UTF-8
            encoding=locale.getpreferredencoding(False) if os.name == "nt" else "utf-8",
            errors="replace",
            text=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return proc.returncode, stdout, stderr, False
        except subprocess.TimeoutExpired:
            self._kill_process_tree(proc)
            stdout, stderr = proc.communicate()
            return None, stdout, stderr, True

    @staticmethod
    def _kill_process_tree(proc: subprocess.Popen) -> None:
        """超时后回收整个进程树。

        - Windows：taskkill /F /T（连同子进程树）；
        - POSIX：向整组进程发 SIGKILL。
        """
        try:
            if os.name == "nt":  # pragma: no cover - 平台分支
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:  # pragma: no cover - POSIX 分支
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:  # noqa: BLE001 - 回收失败不阻断结果返回
            pass
        finally:
            proc.kill()

    def _truncate(self, text: str) -> Tuple[str, bool]:
        """按字节数截断文本至 output_limit，超长置 truncated=True。

        按 UTF-8 字节口径截断，避免中文等被拦腰切断产生乱码字节。

        :param text: 原始文本
        :return: (截断后文本, 是否被截断)
        """
        limit = self.output_limit
        if limit <= 0:
            return "", bool(text)
        try:
            payload = text.encode("utf-8", errors="replace")
        except (UnicodeEncodeError, AttributeError):
            return "", False
        if len(payload) <= limit:
            return text, False
        # 按字节截断后回退解码，去掉末尾可能不完整的多字节序列
        head = payload[:limit]
        return head.decode("utf-8", errors="replace"), True

    # ------------------------------------------------------------------ #
    # 日志（中文，简单记录每次调用 action / authorized）                   #
    # ------------------------------------------------------------------ #

    def _log(self, event: str, tool: str, extra: str = "") -> None:
        """打印中文日志：记录工具调用 / 授权状态 / 拒绝事件。

        M-4：extra 统一过 ``_redact`` 脱敏，防止 ``set TOKEN=sk-xxx`` 类
        命令把密钥明文带进终端日志。

        :param event: 事件（action / deny / unknown）
        :param tool: 工具标识
        :param extra: 附加说明
        """
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        extra = _redact(extra)
        extra = f" | {extra}" if extra else ""
        print(f"[{ts}] [INFO] 电脑控制 event={event} tool={tool}{extra} authorized={self._authorized}")