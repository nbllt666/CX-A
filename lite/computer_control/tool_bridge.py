# -*- coding: utf-8 -*-
"""LLM 工具调用接线层（Task D3）：``ToolBridge``。

把「LLM 工具调用决策」接到「:class:`ComputerControl` 实际执行」与
「:class:`ControlAuthorizer` 安全校验 / 审计」之上，形成完整链路：

    决策 → 工具调用（屏幕 / 键盘 / 指令）→ 本地执行（永久授权开关校验）
    → 结果回填

链路顺序（与工程文档 §9 对齐）：

1. **权限检查**：先查 ``authorizer.is_authorized()``；未授权时抛
   :class:`NotAuthorizedError`，不执行任何本机动作。
2. **高危指令确认**：指令类工具（``computer_run_command``）走
   ``authorizer.confirm()`` 二次确认；需确认但未通过时返回
   ``{"authorized": False, "error": "需要确认"}``，不执行。
3. **实际执行**：委托 ``computer.call_tool(tool, arguments)`` 分派到真实后端。
4. **操作审计 + 结果回填**：把 :class:`ToolResult`（或其子类）转 dict 回填调用方，
   并对每次执行写一条 ``authorizer.audit()`` 审计记录，保证安全链路可回溯。

对外还提供 ``list_tools()``，返回三个稳定工具的描述（参数 / 返回的 JSON Schema
简版），供调用方做工具发现 / 白名单校验。

本模块使用包绝对导入，符合规范。
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, List

from lite.computer_control.control import (
    TOOL_COMMAND,
    TOOL_KEYBOARD,
    TOOL_SCREEN,
    ComputerControl,
    NotAuthorizedError,
    ToolResult,
    _redact,
)
from lite.computer_control.security import ControlAuthorizer

__all__ = ["ToolBridge"]

#: 高危指令未通过二次确认时的错误码
ERROR_NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"


class ToolBridge:
    """LLM 工具调用的接线层：统一做「授权校验 → 高危确认 → 执行 → 审计 / 回填」。

    :param computer: 已装配的 :class:`ComputerControl` 实例（负责真实本机副作用）
    :param authorizer: 已装配的 :class:`ControlAuthorizer` 实例（永久授权 + 高危确认 + 审计）
    """

    def __init__(
        self,
        computer: ComputerControl,
        authorizer: ControlAuthorizer,
    ) -> None:
        self._computer = computer
        self._authorizer = authorizer

    # ------------------------------------------------------------------ #
    # 主入口                                                             #
    # ------------------------------------------------------------------ #

    def execute(self, tool: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """执行一次完整的工具调用链路，返回可序列化的结果 dict。

        与 proc bridge 的字段对齐，返回 dict 至少包含：
        ``result`` / ``authorized`` / ``tool`` / ``error_code``，成功时
        ``success=True``；失败 / 被拒时携带 ``error`` 描述。

        :param tool: 工具稳定标识（TOOL_SCREEN / TOOL_KEYBOARD / TOOL_COMMAND）
        :param arguments: 结构化工具参数
        :return: 结果 dict（ToolResult 转 dict + 审计）
        :raises NotAuthorizedError: 永久授权未开启（未执行任何本机动作）
        """
        arguments = arguments or {}

        # 1. 权限检查：永久授权开关未开启 -> 抛 NotAuthorizedError（由上层映射 403）
        if not self._authorizer.is_authorized():
            self._audit(
                tool=tool,
                args=arguments,
                authorized=False,
                result_summary="拒绝：本地授权未开启 error_code=NOT_AUTHORIZED",
            )
            raise NotAuthorizedError(
                f"本地授权未开启，已拒绝电脑控制调用（{tool}），未执行任何本机动作。"
            )

        # 2. 高危指令二次确认（指令类工具）：需确认但未确认 -> 直接返回「需要确认」
        if tool == TOOL_COMMAND:
            command = str(arguments.get("command") or "")
            if not self._authorizer.confirm(command):
                self._audit(
                    tool=tool,
                    args=arguments,
                    authorized=False,
                    result_summary="拒绝：高危指令未确认 error_code=NEEDS_CONFIRMATION",
                )
                return {
                    "success": False,
                    "tool": tool,
                    "authorized": False,
                    "error": "需要确认",
                    "error_code": ERROR_NEEDS_CONFIRMATION,
                    "result": None,
                }

        # 3. 实际执行：委托 computer.call_tool 分派到真实后端
        # 批次A（第四轮体检，20260829_模块0_电脑控制安全与工具层修复）：改传
        # authorized_override=True 显式单次放行——不再在共享实例状态上先改后
        # 还原，消除原 set_authorized(True)/finally 恢复的 TOCTOU 与并发互覆
        # （A 的 finally 会翻转 B 的授权；执行期 revoke 被临时提权覆盖）。
        result: ToolResult = self._computer.call_tool(
            tool, dict(arguments), authorized_override=True
        )

        # 4. 操作审计 + 结果回填
        summary = self._result_summary(result)
        self._audit(
            tool=tool,
            args=arguments,
            authorized=self._authorizer.is_authorized(),
            result_summary=summary,
        )
        return self._to_dict(result)

    def list_tools(self) -> List[Dict[str, Any]]:
        """返回三个稳定工具的描述（参数 / 返回的 JSON Schema 简版）。

        供 LLM 做工具发现、参数校验与白名单判断。

        :return: 三个工具描述 dict 的列表
        """
        return [
            {
                "name": TOOL_SCREEN,
                "description": "截取当前屏幕画面，返回图像字节（可能为 PNG）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "region": {
                            "type": "object",
                            "description": "可选裁剪区域 {left, top, width, height}，省略为全屏",
                            "required": False,
                        }
                    },
                },
                "returns": {
                    "type": "object",
                    "properties": {
                        "success": {"type": "boolean"},
                        "tool": {"type": "string"},
                        "result": {"type": "string", "format": "bytes", "description": "图像字节"},
                        "authorized": {"type": "boolean"},
                        "error_code": {"type": ["string", "null"]},
                        "action": {"type": "string", "const": "screenshot"},
                    },
                },
            },
            {
                "name": TOOL_KEYBOARD,
                "description": "模拟键鼠输入：点击指定坐标，或键盘输入一段文本。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["click", "type"],
                            "description": "click=点击 / type=输入文本",
                            "required": True,
                        },
                        "x": {"type": "integer", "description": "click 时的横坐标", "required": False},
                        "y": {"type": "integer", "description": "click 时的纵坐标", "required": False},
                        "text": {"type": "string", "description": "type 时要输入的文本", "required": False},
                    },
                },
                "returns": {
                    "type": "object",
                    "properties": {
                        "success": {"type": "boolean"},
                        "tool": {"type": "string"},
                        "result": {"type": "object"},
                        "authorized": {"type": "boolean"},
                        "error_code": {"type": ["string", "null"]},
                        "action": {"type": "string", "enum": ["click", "type"]},
                    },
                },
            },
            {
                "name": TOOL_COMMAND,
                "description": "在本地执行一条指令（含黑名单拦截 / 超时杀进程 / 输出脱敏护栏）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "要执行的指令", "required": True},
                        "timeout_s": {
                            "type": "number",
                            "description": "超时秒数，缺省用系统默认",
                            "required": False,
                        },
                    },
                },
                "returns": {
                    "type": "object",
                    "properties": {
                        "success": {"type": "boolean"},
                        "tool": {"type": "string"},
                        "result": {"type": ["string", "null"], "description": "标准输出"},
                        "authorized": {"type": "boolean"},
                        "error_code": {"type": ["string", "null"]},
                        "exit_code": {"type": ["integer", "null"]},
                        "stdout": {"type": "string"},
                        "stderr": {"type": "string"},
                        "timed_out": {"type": "boolean"},
                        "truncated": {"type": "boolean"},
                    },
                },
            },
        ]

    # ------------------------------------------------------------------ #
    # 内部辅助                                                           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _to_dict(result: ToolResult) -> Dict[str, Any]:
        """把 ToolResult（或其子类）的值全部转成 dict，便于 JSON 回填。

        :param result: ToolResult 或其子类实例
        :return: 由 dataclasses.asdict 展开的普通 dict
        """
        return asdict(result)

    @staticmethod
    def _result_summary(result: ToolResult) -> str:
        """根据执行结果生成审计用的结果摘要（便于提取 error_code）。

        :param result: ToolResult 或其子类实例
        :return: 中文结果摘要
        """
        if result.success:
            return "成功"
        detail = result.error or "执行失败"
        code = result.error_code
        if code:
            return f"失败：{detail} error_code={code}"
        return f"失败：{detail}"

    @staticmethod
    def _summarize_args(args: Dict[str, Any]) -> str:
        """把工具参数转成一段脱敏的中文摘要（避免密钥进审计日志）。

        :param args: 工具参数 dict
        :return: 不超过 200 字的逗号连接参数摘要
        """
        parts = []
        for key, value in (args or {}).items():
            parts.append(f"{key}={value}")
        raw = ", ".join(str(p) for p in parts)
        return _redact(raw)[:200]

    def _audit(
        self,
        tool: str,
        args: Dict[str, Any],
        authorized: bool,
        result_summary: str,
    ) -> None:
        """写一条操作审计记录（action=call_tool）。"""
        self._authorizer.audit(
            action="call_tool",
            tool=tool,
            arguments_summary=self._summarize_args(args),
            authorized=authorized,
            result_summary=result_summary,
        )