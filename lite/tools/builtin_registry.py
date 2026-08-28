# -*- coding: utf-8 -*-
"""内置工具系统（Task G2）：``BuiltinToolRegistry``，承载电脑控制 / 记忆读写 / 系统信息。

对齐 `.trae/documents/CX-A 补充文档 · ACP 与 CXFC.md` §4：CXFC 砍 direct / 发现后，
核心能力由**内置工具系统**承载，供云端 / 本地 LLM 进程内直接调用。

内置工具清单：
- **电脑控制三件**：``computer_screen_control`` / ``computer_keyboard_control`` /
  ``computer_run_command``——优先走 ``ToolBridge.execute``（复用 D3 完整链路：
  授权校验 + 高危确认 + 执行 + 审计）；启用受 ``tools.computer_control``（默认 False）
  与永久授权开关（ControlAuthorizer，内置于 ToolBridge）共同约束；
- **记忆读写**：``memory_write``（content/type/importance/tags -> MemoryStore.add）、
  ``memory_search``（query/top_k -> MemoryRetrievalPipeline.retrieve，未注入 pipeline
  用 store.list 简化）；受 ``tools.memory_tools``（默认 True）；
- **系统信息**：``system_info``（请求类别 time/status/voices）；受 ``tools.system_tools``
  （默认 True）。

统一入口 ``call(tool_id, arguments)``：查注册表 -> 类别开关校验 -> 调用 handler ->
返回 ``{success, tool, result|error, authorized}``；未知工具 / 被类别开关禁用 /
handler 异常一律返回 ``success=False`` 的明确错误，**不向上抛**（区别于 LiteCXFC 的
``CxfToolNotFound`` / ``CxfDisabled`` 异常语义）。

依赖注入：``computer`` / ``computer_bridge`` / ``memory_store`` / ``pipeline`` 任一
未注入时，按类别注册「错误处理工具」，调用时返回明确错误（不抛），保证注册表随时可列。

路径规范：本模块无磁盘 IO；若需路径一律基于
``os.path.dirname(os.path.abspath(__file__))`` 推导，禁止相对路径。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict
from typing import Any, Callable, Dict, List, Optional, Tuple

from lite.computer_control.control import (
    TOOL_COMMAND,
    TOOL_KEYBOARD,
    TOOL_SCREEN,
    NotAuthorizedError,
    _redact,
)

#: 本模块所在目录（供路径推导；本实现无磁盘 IO，仅声明规范）
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

#: 原生日志记录器
LOGGER = logging.getLogger(__name__)

#: 内置工具来源标识
SOURCE_BUILTIN = "builtin"

#: 电脑控制三件稳定标识（与 computer_control 对齐）
COMPUTER_TOOLS = (TOOL_SCREEN, TOOL_KEYBOARD, TOOL_COMMAND)

#: 类别配置键（config.tools 段）与默认开关
_CATEGORY_KEYS = {
    TOOL_SCREEN: ("computer_control", False),
    TOOL_KEYBOARD: ("computer_control", False),
    TOOL_COMMAND: ("computer_control", False),
    "memory_write": ("memory_tools", True),
    "memory_search": ("memory_tools", True),
    "system_info": ("system_tools", True),
}

__all__ = ["BuiltinToolRegistry", "SOURCE_BUILTIN"]


class BuiltinToolRegistry:
    """内置工具注册表：核心能力直接内置，不走插件协议。

    :param computer: 可选的完全内置电脑控制实例（:class:`ComputerControl`）。注意：
        **禁止仅注入 computer**——直连回退必须同时注入 ``authorizer``（:class:`ControlAuthorizer`）
        把关授权/高危确认/审计，否则调用一律拒绝（安全装配禁令）。
    :param computer_bridge: 可选的接线层（:class:`ToolBridge`），电脑控制优先走
        ``execute(tool, arguments)``（内部已由 ControlAuthorizer 把关授权）
    :param authorizer: 可选的授权/审计门（:class:`ControlAuthorizer`），供 computer
        直连回退路径复用；同时注入 ``computer`` 与 ``authorizer`` 时，回退路径
        与 bridge 享有同等安全链路（授权 + 高危确认 + 审计）
    :param memory_store: 可选记忆存储（``MemoryStore.add/list``）
    :param pipeline: 可选记忆检索管线（``MemoryRetrievalPipeline.retrieve``）
    :param config: 配置（ConfigManager / 裸 dict / None）；按 ``tools`` 段读取各
        类别开关；缺省/缺失时用内置默认（computer_control=False、memory_tools=True、
        system_tools=True）。注意：config 仅在**注册期**做一次类别开关快照；
    :param tools_provider: 可选可调用对象（返回当前 ``tools`` 配置段 dict）；注入后
        每次 :meth:`call` 判定类别开关时**优先实时调用** provider 读取最新开关，
        运行期改动 ``tools.*`` 即时生效（键缺失按 ``_CATEGORY_KEYS`` 默认值兜底）。
        provider 未注入或调用失败/返回非 dict 时回落注册期快照，行为与旧版一致。
    """

    def __init__(
        self,
        computer: Any = None,
        computer_bridge: Any = None,
        authorizer: Any = None,
        memory_store: Any = None,
        pipeline: Any = None,
        config: Any = None,
        tools_provider: Optional[Callable[[], Dict[str, Any]]] = None,
        manager: Any = None,
    ) -> None:
        self.computer = computer
        self.computer_bridge = computer_bridge
        self.authorizer = authorizer
        self.memory_store = memory_store
        self.pipeline = pipeline
        self.config = config
        #: 可选的实时 tools 开关提供者（覆盖注册期快照）
        self.tools_provider = tools_provider
        #: 可选 MemoryManager（G-1 统一写入口）：注入后 memory_write 优先走
        #: ``manager.add_memory``（相似去重 + 向量化语义）；None 时回落 store.add
        self.manager = manager

        #: 注册表：tool_id -> 工具描述 dict（含 handler / category / enabled）
        self._tools: Dict[str, Dict[str, Any]] = {}
        self._register_builtins()

    # ------------------------------------------------------------------ #
    # 配置读取                                                          #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _get_config(config: Any, section: str, key: str, default: Any) -> Any:
        """从 ConfigManager 或裸 dict 读取配置项；段/键缺失时返回 default。"""
        if config is None:
            return default
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

    def _tools_enabled(self, key: str, default: bool) -> bool:
        """读取 ``tools.<key>`` 类别开关；缺失 / 非法回退 default。"""
        return bool(self._get_config(self.config, "tools", key, default))

    def _current_enabled(self, tool_id: str, registered_enabled: bool) -> bool:
        """判定类别开关**当前值**：tools_provider 实时读取优先，未注入回落注册期快照。

        :param tool_id: 工具稳定标识
        :param registered_enabled: 注册期快照值（provider 未注入 / 失败时兜底）
        :return: 当前该类别是否启用
        """
        mapping = _CATEGORY_KEYS.get(tool_id)
        if self.tools_provider is None or mapping is None:
            return registered_enabled
        key, default = mapping
        try:
            live = self.tools_provider()
        except Exception as exc:  # noqa: BLE001 - provider 失败回落快照
            LOGGER.warning("tools_provider 调用失败，回落注册期开关快照：%s", exc)
            return registered_enabled
        if not isinstance(live, dict):
            return registered_enabled
        # 兼容误传完整配置 dict（含 tools 段）的情形：剥一层取 tools 段本身
        section = live.get("tools") if isinstance(live.get("tools"), dict) else live
        # 键缺失按 _CATEGORY_KEYS 默认值兜底（与注册期 _tools_enabled 语义一致：
        # memory_tools/system_tools 缺失即 True、computer_control 缺失即 False）
        return bool(section.get(key, default))

    # ------------------------------------------------------------------ #
    # 注册辅助                                                          #
    # ------------------------------------------------------------------ #

    def _register(
        self,
        tool_id: str,
        name: str,
        description: str,
        handler: Callable,
        category: str,
        enabled: bool,
        disabled_error: str,
    ) -> None:
        """登记一个内置工具条目（含启用状态与禁用时的明确错误文案）。"""
        self._tools[tool_id] = {
            "id": tool_id,
            "name": name,
            "description": description,
            "handler": handler,
            "source": SOURCE_BUILTIN,
            "category": category,
            "enabled": bool(enabled),
            "disabled_error": disabled_error,
        }

    # ------------------------------------------------------------------ #
    # 内置工具注册                                                      #
    # ------------------------------------------------------------------ #

    def _register_builtins(self) -> None:
        """启动时注册全部内置工具（电脑控制 / 记忆读写 / 系统信息）。"""
        self._register_computer_tools()
        self._register_memory_tools()
        self._register_system_tools()

    # ----- 电脑控制三件 -----

    def _register_computer_tools(self) -> None:
        """注册电脑控制三件（屏幕 / 键盘 / 运行指令）。

        优先走 ``computer_bridge.execute``（复用 ToolBridge：授权 + 高危确认 + 审计）；
        未注入 bridge 但注入 computer 时退化为 ``computer.call_tool``；
        两者均未注入时注册「错误处理工具」，调用返回明确错误（不抛）。
        """
        enabled = self._tools_enabled("computer_control", False)
        descriptions = {
            TOOL_SCREEN: "截取当前屏幕画面，返回图像字节。",
            TOOL_KEYBOARD: "模拟键鼠输入：点击坐标或输入文本。",
            TOOL_COMMAND: "在本地执行一条指令（含黑名单拦截 / 超时杀进程 / 输出脱敏护栏）。",
        }
        for tool_id, description in descriptions.items():
            self._register(
                tool_id=tool_id,
                name=tool_id.replace("computer_", "").replace("_", " "),
                description=description,
                handler=self._make_computer_handler(tool_id),
                category="computer_control",
                enabled=enabled,
                disabled_error="电脑控制未授权",
            )

    def _make_computer_handler(self, tool_id: str) -> Callable:
        """构造电脑控制单工具 handler：优先 bridge，退化为 computer，否则明确错误。"""

        def handler(arguments: Dict[str, Any]) -> Dict[str, Any]:
            # 1) 优先走 ToolBridge（授权校验 + 高危确认 + 审计回填，内部已把关）
            if self.computer_bridge is not None:
                try:
                    res = self.computer_bridge.execute(tool_id, arguments or {})
                except NotAuthorizedError:
                    return {
                        "success": False,
                        "tool": tool_id,
                        "authorized": False,
                        "error": "电脑控制未授权",
                        "result": None,
                    }
                except Exception as exc:  # noqa: BLE001 - 桥异常包装返回
                    return {
                        "success": False,
                        "tool": tool_id,
                        "authorized": True,
                        "error": str(exc),
                        "result": None,
                    }
                if isinstance(res, dict):
                    res.setdefault("authorized", True)
                return res if isinstance(res, dict) else {"success": True, "result": res, "tool": tool_id}

            # 2) 退化为 ComputerControl 直接调用：必须经 ControlAuthorizer 把关
            #    （授权 + 高危确认 + 审计），禁止「仅注入 computer」绕过安全链路
            if self.computer is not None:
                if self.authorizer is None:
                    return {
                        "success": False,
                        "tool": tool_id,
                        "authorized": False,
                        "error": "电脑控制装配不完整：computer 直连须注入 authorizer（建议改用 computer_bridge）",
                        "result": None,
                    }
                try:
                    # a) 永久授权开关
                    if not self.authorizer.is_authorized():
                        self._audit_direct(tool_id, arguments, authorized=False, summary="拒绝：本地授权未开启 error_code=NOT_AUTHORIZED")
                        return {
                            "success": False,
                            "tool": tool_id,
                            "authorized": False,
                            "error": "电脑控制未授权",
                            "result": None,
                        }
                    # b0) 授权状态同步（MU2）：authorizer 自 security_state.json 恢复
                    #     的授权态需传导到 computer 内部闸门；方向约束——仅当
                    #     authorizer 已开启（上方 is_authorized 通过）才同步 True，
                    #     不得反向令未授权状态放行任何本机动作。
                    sync_authorized = getattr(self.computer, "set_authorized", None)
                    if callable(sync_authorized):
                        sync_authorized(True)
                    # b) 高危指令二次确认（指令类工具）
                    if tool_id == TOOL_COMMAND:
                        command = str((arguments or {}).get("command") or "")
                        if not self.authorizer.confirm(command):
                            self._audit_direct(tool_id, arguments, authorized=False, summary="拒绝：高危指令未确认 error_code=NEEDS_CONFIRMATION")
                            return {
                                "success": False,
                                "tool": tool_id,
                                "authorized": False,
                                "error": "需要确认",
                                "error_code": "NEEDS_CONFIRMATION",
                                "result": None,
                            }
                    # c) 执行 + 审计
                    result = self.computer.call_tool(tool_id, arguments or {})
                    self._audit_direct(tool_id, arguments, authorized=True, summary="成功")
                except NotAuthorizedError:
                    return {
                        "success": False,
                        "tool": tool_id,
                        "authorized": False,
                        "error": "电脑控制未授权",
                        "result": None,
                    }
                except Exception as exc:  # noqa: BLE001 - 异常包装返回
                    return {
                        "success": False,
                        "tool": tool_id,
                        "authorized": True,
                        "error": str(exc),
                        "result": None,
                    }
                if hasattr(result, "__dataclass_fields__"):
                    return asdict(result)
                return {"success": True, "tool": tool_id, "result": result}

            # 3) 后端未注入 -> 明确错误（不抛）
            return {
                "success": False,
                "tool": tool_id,
                "authorized": False,
                "error": "电脑控制后端未注入（computer/computer_bridge 均为 None）",
                "result": None,
            }

        return handler

    @staticmethod
    def _audit_summary_text(arguments) -> str:
        """把直连回退的工具参数转成脱敏摘要（避免敏感内容进审计日志）。"""
        raw = str(arguments or {})
        return _redact(raw)[:200]

    def _audit_direct(self, tool_id, arguments, authorized, summary):
        """直连回退路径的审计记录（委托 authorizer.audit，失败仅告警不抛出）。

        :param tool_id: 工具稳定标识
        :param arguments: 工具参数（脱敏摘要后入审计）
        :param authorized: 本次是否放行
        :param summary: 审计结果摘要
        """
        if self.authorizer is None:
            return
        try:
            self.authorizer.audit(
                action="call_tool",
                tool=tool_id,
                arguments_summary=self._audit_summary_text(arguments),
                authorized=authorized,
                result_summary=summary,
            )
        except Exception:  # noqa: BLE001 - 审计失败不阻断工具链路
            LOGGER.warning("内置工具审计失败（tool=%s, summary=%s）", tool_id, summary)

    # ----- 记忆读写 -----

    def _register_memory_tools(self) -> None:
        """注册记忆读写工具（memory_write / memory_search），受 memory_tools 开关约束。"""
        enabled = self._tools_enabled("memory_tools", True)
        self._register(
            tool_id="memory_write",
            name="memory_write",
            description="写入一条记忆（content 必填；type/importance/tags 可选）。",
            handler=self._make_memory_write_handler(),
            category="memory_tools",
            enabled=enabled,
            disabled_error="记忆读写工具未启用",
        )
        self._register(
            tool_id="memory_search",
            name="memory_search",
            description="检索记忆（query 必填；top_k 可选，缺省用系统默认条数）。",
            handler=self._make_memory_search_handler(),
            category="memory_tools",
            enabled=enabled,
            disabled_error="记忆读写工具未启用",
        )

    def _make_memory_write_handler(self) -> Callable:
        """构造 memory_write handler：优先 manager.add_memory，回落 memory_store.add。"""

        def handler(arguments: Dict[str, Any]) -> Dict[str, Any]:
            store = self.memory_store
            if store is None and self.pipeline is not None:
                store = getattr(self.pipeline, "store", None)
            if store is None and self.manager is None:
                return {
                    "success": False,
                    "tool": "memory_write",
                    "authorized": True,
                    "error": "记忆存储后端未注入（memory_store / pipeline / manager 均为 None）",
                    "result": None,
                }
            args = arguments or {}
            tags = args.get("tags") or []
            if not isinstance(tags, str):  # tags 列为 TEXT，list/dict 需 JSON 序列化
                tags = json.dumps(tags, ensure_ascii=False)
            content = args.get("content", "")
            mem_type = args.get("type", "long_term")
            importance = args.get("importance", 3)
            try:
                if self.manager is not None:
                    # G-1 统一写入口：优先走 manager.add_memory（相似去重 + 向量化）。
                    # 返回 None 表示被去重跳过（未实际写入），如实返回标记。
                    mem_id = self.manager.add_memory(
                        content=content,
                        type=mem_type,
                        importance=importance,
                        agent_id=args.get("agent_id", "default"),
                    )
                    if mem_id is None:
                        return {
                            "success": True,
                            "tool": "memory_write",
                            "authorized": True,
                            "result": {"id": None, "deduplicated": True},
                        }
                else:
                    # manager 缺席：保持原 store.add 直写行为兜底
                    payload = {
                        "content": content,
                        "type": mem_type,
                        "importance": importance,
                        "tags": tags,
                    }
                    mem_id = store.add(payload)
            except Exception as exc:  # noqa: BLE001 - 异常包装返回
                return {
                    "success": False,
                    "tool": "memory_write",
                    "authorized": True,
                    "error": str(exc),
                    "result": None,
                }
            return {
                "success": True,
                "tool": "memory_write",
                "authorized": True,
                "result": {"id": mem_id},
            }

        return handler

    def _make_memory_search_handler(self) -> Callable:
        """构造 memory_search handler：优先 pipeline.retrieve，退化 store.list 简化。"""

        def handler(arguments: Dict[str, Any]) -> Dict[str, Any]:
            args = arguments or {}
            query = args.get("query", "")
            top_k = args.get("top_k")

            # 优先走完整检索管线（向量 + 关键词 + 打分 + 注入上下文）
            if self.pipeline is not None:
                try:
                    kw = {"query": query}
                    if top_k is not None:
                        kw["top_k"] = int(top_k)
                    result = self.pipeline.retrieve(**kw)
                except Exception as exc:  # noqa: BLE001 - 异常包装返回
                    return {
                        "success": False,
                        "tool": "memory_search",
                        "authorized": True,
                        "error": str(exc),
                        "result": None,
                    }
                return {
                    "success": True,
                    "tool": "memory_search",
                    "authorized": True,
                    "result": result,
                }

            # 退化：仅用 store.list 简化检索（无向量打分）
            if self.memory_store is not None:
                try:
                    # G-8 修复：先取全量再做子串过滤 + 相关度排序，避免"前 N 条"截断丢失命中项
                    memories = self.memory_store.list()
                except Exception as exc:  # noqa: BLE001 - 异常包装返回
                    return {
                        "success": False,
                        "tool": "memory_search",
                        "authorized": True,
                        "error": str(exc),
                        "result": None,
                    }
                q = str(query or "")
                if q:
                    # query 非空：按 content 含 query 子串过滤，按（子串命中、importance 降序）排序
                    hits = [m for m in memories if q in str(m.get("content") or "")]
                    hits.sort(
                        key=lambda m: (
                            q in str(m.get("content") or ""),
                            int(m.get("importance") or 0),
                        ),
                        reverse=True,
                    )
                else:
                    # query 为空：返回最近 limit 条（store.list 按 id 升序，取尾部即最新）
                    hits = list(memories)
                if top_k is not None and int(top_k) > 0:
                    # query 非空：按相关度序取前 N；query 为空：取尾部 N 条（最新）
                    hits = hits[: int(top_k)] if q else hits[-int(top_k):]
                return {
                    "success": True,
                    "tool": "memory_search",
                    "authorized": True,
                    "result": {"memories": hits, "context_text": "【回忆】", "degraded": True},
                }

            # 后端未注入 -> 明确错误（不抛）
            return {
                "success": False,
                "tool": "memory_search",
                "authorized": True,
                "error": "记忆检索后端未注入（memory_store / pipeline 均为 None）",
                "result": None,
            }

        return handler

    # ----- 系统信息 -----

    def _register_system_tools(self) -> None:
        """注册系统信息工具（system_info），受 system_tools 开关约束。"""
        enabled = self._tools_enabled("system_tools", True)
        self._register(
            tool_id="system_info",
            name="system_info",
            description="获取系统信息（请求类别 category=time / status / voices）。",
            handler=self._make_system_info_handler(),
            category="system_tools",
            enabled=enabled,
            disabled_error="系统信息工具未启用",
        )

    @staticmethod
    def _make_system_info_handler() -> Callable:
        """构造 system_info handler：按 category 返回时间 / 进程状态 / 音色列表。"""

        def handler(arguments: Dict[str, Any]) -> Dict[str, Any]:
            category = (arguments or {}).get("category", "status")

            if category == "time":
                return {
                    "success": True,
                    "tool": "system_info",
                    "authorized": True,
                    "result": {
                        "category": "time",
                        "iso": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "timestamp": time.time(),
                        "timezone": time.strftime("%Z"),
                    },
                }

            if category == "status":
                return {
                    "success": True,
                    "tool": "system_info",
                    "authorized": True,
                    "result": {
                        "category": "status",
                        "status": "ok",
                        "app": "CX-A/CX-Lite",
                        "pid": os.getpid(),
                        "uptime_seconds": time.monotonic(),
                    },
                }

            if category == "voices":
                try:  # 音色列表为可选能力（VoiceManager），失败不抛、降级为错误
                    from lite.audio.voice_manager import VoiceManager

                    vm = VoiceManager()
                    voices = vm.list_voices()
                except Exception as exc:  # noqa: BLE001 - 可选能力降级
                    return {
                        "success": False,
                        "tool": "system_info",
                        "authorized": True,
                        "error": f"音色列表不可用：{exc}",
                        "result": None,
                    }
                return {
                    "success": True,
                    "tool": "system_info",
                    "authorized": True,
                    "result": {"category": "voices", "voices": voices},
                }

            return {
                "success": False,
                "tool": "system_info",
                "authorized": True,
                "error": f"未知系统信息类别：{category!r}（可选 time / status / voices）",
                "result": None,
            }

        return handler

    # ------------------------------------------------------------------ #
    # 统一调用入口                                                      #
    # ------------------------------------------------------------------ #

    def call(self, tool_id: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """统一调用入口：查注册表 -> 类别开关校验 -> 调用 handler -> 返回结果 dict。

        返回 ``{success, tool, result|error, authorized}``；以下情况一律返回
        ``success=False`` 的明确错误、**不抛异常**：
        - 未知工具 id；
        - 该工具所属类别开关被禁用（如电脑控制未授权）；
        - handler 抛出异常（包装为 error）。

        :param tool_id: 内置工具稳定标识
        :param arguments: 结构化工具参数（缺省空 dict）
        :return: ``{success, tool, result|error, authorized}``
        """
        entry = self._tools.get(tool_id)
        if entry is None:
            return {
                "success": False,
                "tool": tool_id,
                "authorized": True,
                "error": f"未知内置工具：{tool_id!r}",
                "result": None,
            }
        # 类别开关实时判定：tools_provider 注入时读当前值（运行期改 tools.* 即时生效），
        # 未注入/失败回落注册期快照
        if not self._current_enabled(tool_id, bool(entry["enabled"])):
            return {
                "success": False,
                "tool": tool_id,
                "authorized": False,
                "error": entry["disabled_error"],
                "result": None,
            }

        try:
            result = entry["handler"](arguments or {})
        except Exception as exc:  # noqa: BLE001 - 异常包装返回，不向上抛
            LOGGER.error("内置工具 %r 调用失败：%s", tool_id, exc)
            return {
                "success": False,
                "tool": tool_id,
                "authorized": True,
                "error": str(exc),
                "result": None,
            }

        # handler 已返回完整外壳（如电脑控制 bridge 结果）则补 authorized 后直返
        if isinstance(result, dict) and "success" in result:
            result.setdefault("authorized", True)
            return result
        return {
            "success": True,
            "tool": tool_id,
            "authorized": True,
            "result": result,
        }

    # ------------------------------------------------------------------ #
    # 工具清单                                                          #
    # ------------------------------------------------------------------ #

    def list_tools(self) -> List[Dict[str, Any]]:
        """返回已注册内置工具清单（含启用状态与禁用原因）。

        :return: 每个元素含 ``id`` / ``name`` / ``description`` / ``source=builtin``
                 / ``category`` / ``enabled``
        """
        return [
            {
                "id": e["id"],
                "name": e["name"],
                "description": e["description"],
                "source": e["source"],
                "category": e["category"],
                "enabled": e["enabled"],
            }
            for e in self._tools.values()
        ]