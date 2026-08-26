# -*- coding: utf-8 -*-
"""极简 CXFC 实现（Task G2）：仅保留 ``embedded`` 进程内注册，砍掉其它传输。

对齐 `.trae/documents/CX-A 补充文档 · ACP 与 CXFC.md` §3：CXFC 大幅简化——
**砍掉** ``direct`` 传输（插件自带 HTTP 服务）与 UDP 发现 / 心跳；
**仅保留** ``embedded`` 进程内注册（一体两面注册内置工具用）。

与 CX-O ``CXFCManager`` 的对照（见 :file:`C:\\CX-O\\CX-O-SERVER\\server\\core\\cxfc\\manager.py`）：
- CX-O 的 ``register_embedded_plugin`` 需 plugin_id + 工具/技能注册表、持久化 + 心跳任务
  （``_check_heartbeats_loop``）；本实现只维护一个进程内注册表 dict，无网络 / 心跳 / 发现；
- CX-O 的 ``call_tool`` 按 transport 分派到 relay / embedded / direct HTTP；本实现仅进程内
  ``handler(arguments)`` 直接调用，handler 异常包装为 ``{success:false, error}`` 而非上抛；
- CX-O 的 ``discovery.py``（UDP 广播 / 扫描）在本实现中被整体砍掉，无任何套接字创建。

路径规范：本模块不写入磁盘文件；若需路径一律基于
``os.path.dirname(os.path.abspath(__file__))`` 推导，禁止相对路径。

本模块只使用标准库（``logging``），可独立加载、无三方运行依赖。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, List, Optional

#: 本模块所在目录（供路径推导；本实现无磁盘 IO，仅声明规范）
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

#: 唯一允许的传输方式（embedded_only 模式下强制）
TRANSPORT_EMBEDDED = "embedded"

#: 原生日志记录器
LOGGER = logging.getLogger(__name__)

__all__ = ["LiteCXFC", "CxfDisabled", "CxfToolNotFound", "TRANSPORT_EMBEDDED"]


class CxfDisabled(Exception):
    """CXFC 总开关关闭（``enabled=False``）时，``register_embedded`` / ``call`` 抛出的自定义异常。

    含义：极简版 CXFC 默认关闭，需显式开启后才可注册 / 调用工具。
    """


class CxfToolNotFound(Exception):
    """调用未注册的工具时抛出的自定义异常。

    含义：调用方试图调用一个未在进程内注册表中登记的工具 id。
    """


class LiteCXFC:
    """极简 CXFC：仅保留 ``embedded`` 进程内注册，是内部工具的进程内挂载点。

    设计要点（与 CX-O 对照）：
    - **进程内注册表**：内部用 ``dict`` 保存 ``tool_id -> 描述``，不落盘、不跨进程；
    - **砍掉网络机制**：无 ``direct`` HTTP / 无 UDP 发现 / 无心跳 / 无独立插件进程；
    - **禁用即抛**：``enabled=False`` 时 ``register_embedded`` / ``call`` 抛
      :class:`CxfDisabled`；
    - **未注册即抛**：``call`` 传入未注册工具抛 :class:`CxfToolNotFound`；
    - **异常包装**：handler 抛出任何异常时不向上抛，包装返回 ``{success:false, error}``。

    :param enabled: CXFC 总开关；默认 False（轻量版默认关，用内置工具系统）
    :param embedded_only: 是否仅允许 ``embedded`` 传输；默认 True（砍掉 direct/relay）
    :param config: 配置（ConfigManager / 裸 dict / None）；非 None 时以 ``cxfc`` 段覆写上两开关
    """

    def __init__(
        self,
        enabled: bool = False,
        embedded_only: bool = True,
        config: Any = None,
    ) -> None:
        #: 进程内注册表：tool_id -> 工具描述 dict
        self._registry: Dict[str, Dict[str, Any]] = {}
        self.enabled = bool(enabled)
        self.embedded_only = bool(embedded_only)

        # 配置段覆盖（缺省段 / 键时回退构造参数携入的默认值）
        if config is not None:
            self.enabled = bool(self._get_config(config, "cxfc", "enabled", self.enabled))
            self.embedded_only = bool(
                self._get_config(config, "cxfc", "embedded_only", self.embedded_only)
            )

    @staticmethod
    def _get_config(config: Any, section: str, key: str, default: Any) -> Any:
        """从 ConfigManager 或裸 dict 读取配置项；段/键缺失时返回 default。"""
        if isinstance(config, dict):
            sec = config.get(section)
            if isinstance(sec, dict):
                return sec.get(key, default)
            return default
        # 兼容 ConfigManager（get(section, key, default)）
        try:
            return config.get(section, key, default)
        except (TypeError, AttributeError):
            return default

    # ------------------------------------------------------------------ #
    # embedded 注册                                                      #
    # ------------------------------------------------------------------ #

    def register_embedded(
        self,
        tool_id: str,
        name: str,
        handler: Callable,
        description: str = "",
        parameters: Optional[Dict[str, Any]] = None,
        transport: str = TRANSPORT_EMBEDDED,
    ) -> bool:
        """进程内注册一个工具（Callable handler），不走任何网络。

        - ``(tool_id, name, handler)`` 为必填；缺失抛 :class:`ValueError`；
        - 重复 ``tool_id`` 覆盖旧注册并打印 warning（覆盖语义）；
        - ``embedded_only=True`` 时 ``transport`` 仅允许 ``"embedded"``，
          传入其它值抛 :class:`ValueError`（禁止网络传输参数）。

        :param tool_id: 工具稳定标识（唯一键）
        :param name: 工具名
        :param handler: 进程内可调用对象，签名 ``handler(arguments: dict) -> Any``
        :param description: 工具描述（缺省空串）
        :param parameters: 参数 schema（缺省空 dict）
        :param transport: 传输方式，仅允许 ``"embedded"``
        :return: 注册成功返回 True
        :raises CxfDisabled: CXFC 总开关关闭
        :raises ValueError: 必填缺失，或 embedded_only 下 transport 非法
        """
        if not self.enabled:
            raise CxfDisabled("CXFC 未启用（enabled=False），禁止注册工具")
        if tool_id is None or not str(tool_id).strip():
            raise ValueError("tool_id 为必填参数")
        if name is None or not str(name).strip():
            raise ValueError("name 为必填参数")
        if not callable(handler):
            raise ValueError("handler 必须为可调用对象（Callable）")
        if self.embedded_only and transport != TRANSPORT_EMBEDDED:
            raise ValueError(
                f"embedded_only 模式仅允许 transport='{TRANSPORT_EMBEDDED}'，收到 {transport!r}；"
                "direct/relay 网络传输已从极简版 CXFC 砍掉"
            )

        if tool_id in self._registry:
            LOGGER.warning(
                "重复注册工具 %r（name=%r），将以新 handler 覆盖旧注册", tool_id, name
            )

        self._registry[tool_id] = {
            "tool_id": tool_id,
            "name": str(name),
            "handler": handler,
            "description": description or "",
            "parameters": parameters or {},
            "transport": TRANSPORT_EMBEDDED,
        }
        return True

    # ------------------------------------------------------------------ #
    # 进程内调用                                                         #
    # ------------------------------------------------------------------ #

    def call(self, tool_id: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """进程内调用已注册工具的 handler(arguments)，返回结果 dict。

        - 未注册的 ``tool_id`` 抛 :class:`CxfToolNotFound`（不静默）；
        - handler 抛出的任何异常被包装为 ``{success:false, error}`` 返回，**不向上抛**；
        - 成功返回 ``{success:true, result, tool}``。

        :param tool_id: 工具稳定标识
        :param arguments: 结构化工具参数（缺省空 dict）
        :return: ``{success, result|error, tool}``
        :raises CxfDisabled: CXFC 总开关关闭
        :raises CxfToolNotFound: 工具未注册
        """
        if not self.enabled:
            raise CxfDisabled("CXFC 未启用（enabled=False），禁止调用工具")
        entry = self._registry.get(tool_id)
        if entry is None:
            raise CxfToolNotFound(f"CXFC 工具未注册：{tool_id!r}")

        arguments = arguments or {}
        try:
            result = entry["handler"](arguments)
            return {"success": True, "result": result, "tool": tool_id}
        except Exception as exc:  # noqa: BLE001 - handler 异常包装返回，不向上抛
            LOGGER.error("CXFC 工具 %r 调用失败：%s", tool_id, exc)
            return {"success": False, "error": str(exc), "tool": tool_id}

    # ------------------------------------------------------------------ #
    # 工具列表                                                           #
    # ------------------------------------------------------------------ #

    def list_tools(self) -> List[Dict[str, Any]]:
        """返回已注册工具的描述列表（不含 handler 可调用对象）。

        :return: 每个元素含 ``tool_id`` / ``name`` / ``description`` / ``parameters`` / ``transport``
        """
        result = []
        for entry in self._registry.values():
            result.append(
                {
                    "tool_id": entry["tool_id"],
                    "name": entry["name"],
                    "description": entry["description"],
                    "parameters": entry["parameters"],
                    "transport": entry["transport"],
                }
            )
        return result