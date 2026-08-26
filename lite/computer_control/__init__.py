# -*- coding: utf-8 -*-
"""电脑控制子包（screenshot / click / type_text / run_command），Task D 实现。

完全内置版本（不走 CXFC 插件），但工具命名 / 参数 / 返回结构与
`C:\\CX-O\\public\\interface_stub\\computer_control.pyi` 对齐。

对外导出：
- 三个工具稳定标识常量：``TOOL_SCREEN`` / ``TOOL_KEYBOARD`` / ``TOOL_COMMAND``；
- 主类：``ComputerControl``；
- 结果结构：``ToolResult`` / ``ScreenResult`` / ``KeyboardResult`` / ``CommandResult``；
- 错误契约：``PluginError`` / ``NotAuthorizedError`` / ``InvalidArgumentError`` /
  ``TimeoutError`` / ``ExecutionError``；
- 护栏常量：``DANGEROUS_COMMANDS`` / ``BLOCKED``；
- Task D2 安全机制：``ControlAuthorizer``（永久授权 / 撤销 / 高危确认 / 审计）；
- Task D3 接线层：``ToolBridge``（决策 → 授权校验 → 高危确认 → 执行 → 审计回填）。
"""

from lite.computer_control.control import (
    BLOCKED,
    DANGEROUS_COMMANDS,
    TOOL_COMMAND,
    TOOL_KEYBOARD,
    TOOL_SCREEN,
    CommandResult,
    ComputerControl,
    ExecutionError,
    InvalidArgumentError,
    KeyboardResult,
    NotAuthorizedError,
    PluginError,
    ScreenResult,
    TimeoutError,
    ToolResult,
    _redact,
)
from lite.computer_control.security import ControlAuthorizer
from lite.computer_control.tool_bridge import ToolBridge

__all__ = [
    "TOOL_SCREEN",
    "TOOL_KEYBOARD",
    "TOOL_COMMAND",
    "ComputerControl",
    "ToolResult",
    "ScreenResult",
    "KeyboardResult",
    "CommandResult",
    "PluginError",
    "NotAuthorizedError",
    "InvalidArgumentError",
    "TimeoutError",
    "ExecutionError",
    "DANGEROUS_COMMANDS",
    "BLOCKED",
    "ControlAuthorizer",
    "ToolBridge",
]