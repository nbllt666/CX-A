# -*- coding: utf-8 -*-
"""电脑控制永久授权与安全机制（Task D2）：``ControlAuthorizer``。

在 Task D1（:mod:`lite.computer_control.control`）的安全边界之上，补齐
「永久授权 + 主动撤销 + 高危指令确认 + 操作审计」四类安全机制。

设计要点：
- **默认全关**：``authorized=False``（本地不授权，不执行任何本机动作）；
- **用户显式开启**：``authorize()`` 幂等开启总开关，重复调用返回 False；
- **主动撤销**：``revoke()`` 立即收回，并持久化；
- **高危指令确认（默认开）**：命中 ``DANGEROUS_COMMANDS`` 黑名单或幂等破坏
  操作时，需经 ``confirm_fn`` 二次确认；未注入回调时默认 False（安全否决）；
- **操作审计（默认开）**：每次关键操作写 ``data/audit.jsonl``；
- **状态持久化**：``data/security_state.json`` 自动加载 / 保存，跨实例恢复。

与 D1 集成：``authorized=authorizer.is_authorized()`` 传给
:class:`ComputerControl`，即可把 D1 的 ``NotAuthorizedError`` 安全边界接上评审。

路径规范：所有数据路径基于 ``os.path.dirname(os.path.abspath(__file__))`` 推导，
不使用相对路径。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable, Dict, Optional

from lite.computer_control.control import DANGEROUS_COMMANDS, DANGEROUS_TOKENS, ComputerControl

__all__ = ["ControlAuthorizer"]

#: 默认数据目录：本模块所在目录的 ``data`` 子目录
#: （由 os.path.dirname(os.path.abspath(__file__)) 推导，禁止相对路径）
_DEFAULT_DATA_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data"
)

#: 幂等破坏操作危险关键词（在黑名单之外，命中即视为高危需二次确认）
_DESTRUCTIVE_IDEMPOTENT: tuple = ("rmtree", "remove", "delete", "drop")

#: 审计日志收集的已知错误码（用于从 result_summary 中提取 error_code 字段）
_KNOWN_ERROR_CODES: tuple = (
    "NOT_AUTHORIZED",
    "INVALID_ARGUMENT",
    "TIMEOUT",
    "EXECUTION_FAILED",
    "BLOCKED",
)


class ControlAuthorizer:
    """电脑控制永久授权与安全机制总控。

    :param data_dir: 数据目录（存放 ``audit.jsonl`` 与 ``security_state.json``）；
        缺省取本模块同级 ``data`` 子目录（``os.path.dirname(os.path.abspath(__file__))`` 推导）
    :param confirm_fn: 高危确认回调 ``confirm_fn(command) -> bool``；
        缺省为安全否决（等价 ``lambda command: False``），注入后用于高危指令二次确认
    """

    def __init__(
        self,
        data_dir: Optional[str] = None,
        confirm_fn: Optional[Callable[[str], bool]] = None,
    ) -> None:
        self._data_dir = data_dir or _DEFAULT_DATA_DIR
        self._confirm_fn = confirm_fn

        # 默认安全基线：授权默认关；高危确认默认开；操作审计默认开
        self.authorized = False
        self.confirm_dangerous = True
        self.audit_enabled = True

        # 状态字段写入锁（仅保护状态文件的读写；审计追加写本身无锁安全）
        self._state_lock = threading.Lock()

        # 载入历史持久化状态（首次运行保持默认）
        self._load_state()

    # ------------------------------------------------------------------ #
    # 数据路径（基于 __file__ 推导，禁止相对路径）                        #
    # ------------------------------------------------------------------ #

    def _state_path(self) -> str:
        """返回授权状态持久化文件路径（data/security_state.json）。"""
        return os.path.join(self._data_dir, "security_state.json")

    def _audit_path(self) -> str:
        """返回操作审计日志路径（data/audit.jsonl）。"""
        return os.path.join(self._data_dir, "audit.jsonl")

    # ------------------------------------------------------------------ #
    # 状态持久化                                                          #
    # ------------------------------------------------------------------ #

    def _save_state(self) -> None:
        """将当前授权三要素持久化到 data/security_state.json。

        L-1：tmp + ``os.replace`` 原子写（对齐 local_agents / config_manager
        口径），防止崩溃 / 断电留下截断文件导致授权状态静默丢失。
        """
        os.makedirs(self._data_dir, exist_ok=True)
        state: Dict[str, Any] = {
            "authorized": bool(self.authorized),
            "confirm_dangerous": bool(self.confirm_dangerous),
            "audit_enabled": bool(self.audit_enabled),
        }
        path = self._state_path()
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)

    def _load_state(self) -> None:
        """从 data/security_state.json 载入历史授权状态；文件缺失 / 损坏则保持默认。"""
        path = self._state_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                state = json.load(fh)
        except (OSError, ValueError):
            # 状态文件损坏则回退默认安全基线，不阻断授权器可用性
            return
        self.authorized = bool(state.get("authorized", False))
        self.confirm_dangerous = bool(state.get("confirm_dangerous", True))
        self.audit_enabled = bool(state.get("audit_enabled", True))

    # ------------------------------------------------------------------ #
    # 授权总开关                                                          #
    # ------------------------------------------------------------------ #

    def _safe_audit(
        self,
        action: str,
        tool: str,
        arguments_summary: str,
        authorized: bool,
        result_summary: str,
    ) -> None:
        """审计写入的容错包装（M-2）：写失败仅告警，不中断授权状态变更。

        修复前 authorize 中 ``authorized=True`` 已生效并持久化后，audit 抛
        OSError（data 目录只读 / 磁盘满）会向上传播，调用方误判"开启失败"，
        而实际本机动作闸门已全部打开——异常路径状态不一致。现改为状态提交
        与审计解耦：审计失败打印告警（留缺失痕迹），授权语义保持一致。

        :param action: 操作事件
        :param tool: 工具稳定标识或空串
        :param arguments_summary: 参数摘要
        :param authorized: 操作时的授权状态
        :param result_summary: 结果摘要
        """
        try:
            self.audit(
                action=action,
                tool=tool,
                arguments_summary=arguments_summary,
                authorized=authorized,
                result_summary=result_summary,
            )
        except OSError as exc:
            print(
                f"[WARN] 电脑控制审计写入失败（action={action}）：{exc}"
                f"——授权状态已变更，但本条审计记录缺失"
            )

    def authorize(self) -> bool:
        """开启授权总开关（用户显式开启）。

        幂等语义：已授权时返回 False（不可重复开启）；本次新开启返回 True。
        开启后立即持久化，并写一条 ``authorize`` 审计（N4：关键安全事件可回溯）。

        :return: 本次是否产生开启动作（True=新开启，False=已开启未变化）
        """
        with self._state_lock:
            if self.authorized:
                return False
            self.authorized = True
            self._save_state()
            # N4：授权开启为关键安全事件，落盘后补审计（记录来源，便于 CSRF 场景回溯）
            # M-2：审计写失败不回滚授权（fail-visible：告警留痕缺失）
            self._safe_audit(
                action="authorize",
                tool="",
                arguments_summary="source=api",
                authorized=True,
                result_summary="授权已开启并持久化",
            )
            return True

    def revoke(self) -> bool:
        """主动撤销授权，立即收回并持久化。

        撤销后写一条 ``revoke`` 审计（N4：与 authorize 对称的安全事件留痕）。

        :return: 恒为 True（撤销动作已执行）
        """
        with self._state_lock:
            self.authorized = False
            self._save_state()
            # N4：撤销亦为关键安全事件，落盘后补审计
            self._safe_audit(
                action="revoke",
                tool="",
                arguments_summary="source=api",
                authorized=False,
                result_summary="授权已撤销并持久化",
            )
            return True

    def is_authorized(self) -> bool:
        """返回当前授权状态。

        :return: True 已授权；False 未授权 / 已撤销
        """
        return self.authorized

    # ------------------------------------------------------------------ #
    # 高危指令确认                                                        #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _is_destructive(command: str) -> bool:
        """判定命令是否命中幂等破坏操作模式（如删库 / 删除 / 清除关键词）。

        批次A（第四轮体检）：在子串判定之外补**词元级判定**——拆 token 后任一
        token 命中 :data:`DANGEROUS_TOKENS`（del/rd/rm 等）即判高危，覆盖
        ``if exist x del x`` 中 del 为中间词元、子串表只含 delete 不含 del 的
        穿透形态；预处理同步剥离 cmd 转义符 ``^``。

        :param command: 指令字符串
        :return: True 表示命中断言破坏模式
        """
        cmd = (command or "").strip().lower().replace("^", "")
        # 词元级判定（批次A）：del 等破坏词作为中间 token 出现时子串表查不到
        tokens = [tok.strip("\"'") for tok in cmd.split()]
        if any(tok in DANGEROUS_TOKENS for tok in tokens):
            return True
        return any(pattern in cmd for pattern in _DESTRUCTIVE_IDEMPOTENT)

    def needs_confirmation(self, command: str) -> bool:
        """判定命令是否需高危二次确认。

        命中条件：``confirm_dangerous=True`` 且（命中 ``DANGEROUS_COMMANDS``
        黑名单 **或** 首 token 命中包裹器名单 ``CONFIRM_REQUIRED_WRAPPERS``
        **或** 幂等破坏操作）。

        :param command: 指令字符串
        :return: True 需确认；False 无需确认（可放行）
        """
        if not self.confirm_dangerous:
            return False
        if ComputerControl._is_dangerous(command):
            return True
        # N2：包裹器形态（cmd /c del ...、powershell -c ri ...、带引号绝对路径等）
        # 黑名单前缀匹配覆盖不到，首 token 命中包裹器名单即强制进确认闸
        if ComputerControl._requires_confirmation(command):
            return True
        if self._is_destructive(command):
            return True
        return False

    def confirm(self, command: str) -> bool:
        """执行高危确认并返回是否放行。

        语义：
        - 无需确认（``needs_confirmation`` 为 False）-> 返回 True（放行）；
        - 需确认且已注入 ``confirm_fn`` -> 返回其调用结果；
        - 需确认但未注入 ``confirm_fn`` -> 返回 False（安全否决，缺省），
          等价 ``lambda command: False``。

        :param command: 指令字符串
        :return: True 放行；False 否决
        """
        if not self.needs_confirmation(command):
            return True
        if self._confirm_fn is None:
            return False
        try:
            return bool(self._confirm_fn(command))
        except Exception:  # noqa: BLE001 - 确认回调异常时安全否决
            return False

    # ------------------------------------------------------------------ #
    # 操作审计                                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_error_code(result_summary: str) -> Optional[str]:
        """从结果摘要中提取已知错误码；未命中返回 None。

        :param result_summary: 结果摘要（如 ``失败：黑名单拦截 error_code=BLOCKED``）
        :return: 已知错误码或 None
        """
        text = (result_summary or "").upper()
        for code in _KNOWN_ERROR_CODES:
            if code in text:
                return code
        return None

    def audit(
        self,
        action: str,
        tool: str,
        arguments_summary: str,
        authorized: bool,
        result_summary: str,
    ) -> None:
        """写一条操作审计记录到 data/audit.jsonl。

        每行一个 UTF-8 JSON 对象，字段含：
        ``timestamp`` / ``action`` / ``tool`` / ``args``（中文参数摘要）/
        ``authorized`` / ``result`` / ``error_code``。

        ``audit_enabled=False`` 时直接跳过、不落盘。

        线程安全说明：本方法以 ``open(..., "a")`` 追加写单行 JSON，CPython 下
        对短行整次追加写是原子操作，故无需额外加锁；跨进程同时追加亦安全。
        冲突时最多损耗首尾完整行。状态字段本身由 ``_state_lock`` 保护，
        与审计写互不影响。

        :param action: 操作事件（如 ``authorize`` / ``revoke`` / ``call_tool``）
        :param tool: 工具稳定标识或空串
        :param arguments_summary: 参数摘要（应已脱敏，避免泄露密钥）
        :param authorized: 操作时的授权状态
        :param result_summary: 结果摘要（成功 / 拒绝 / 错误码）
        """
        if not self.audit_enabled:
            return
        record: Dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "action": action,
            "tool": tool,
            "args": arguments_summary,
            "authorized": bool(authorized),
            "result": result_summary,
            "error_code": self._extract_error_code(result_summary),
        }
        os.makedirs(self._data_dir, exist_ok=True)
        with open(self._audit_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")