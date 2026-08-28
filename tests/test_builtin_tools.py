# -*- coding: utf-8 -*-
"""Task G2 内置工具系统（BuiltinToolRegistry）单元测试。

覆盖（与工程补充文档 §4 对齐）：
- 电脑控制三件：注入 mock bridge 返回结果；tools.computer_control=False 时返回
  "电脑控制未授权"错误（不抛）；
- 电脑控制授权联动：bridge 内部抛 NotAuthorizedError 时返回未授权错误；
- 记忆工具：内存级 MemoryStore（tmp db）add/search 成功；
- 记忆检索管线优先：注入 mock pipeline 时 memory_search 走 retrieve；
- 系统工具：time / status 返回正确结构；
- 未注入依赖的工具返回明确错误（不抛）；
- registry.call 未知工具 success=False。
"""

import pytest

from lite.memory.storage import MemoryStore

from lite.tools import BuiltinToolRegistry

#: 各类别默认开启的配置（电脑控制默认关）
_CONFIG_ALL_OFF = {"tools": {"computer_control": False, "memory_tools": True, "system_tools": True}}
#: 电脑控制显式开启的配置
_CONFIG_COMPUTER_ON = {"tools": {"computer_control": True, "memory_tools": True, "system_tools": True}}


class MockBridge:
    """mock ToolBridge：记录调用并返回固定成功结果。"""

    def __init__(self):
        self.calls = []

    def execute(self, tool, arguments):
        self.calls.append((tool, dict(arguments)))
        return {
            "success": True,
            "tool": tool,
            "result": {"mock": "ok", "arg": arguments},
            "authorized": True,
        }


class NotAuthorizedBridge:
    """mock 未授权 bridge：execute 抛 NotAuthorizedError。"""

    def execute(self, tool, arguments):
        from lite.computer_control.control import NotAuthorizedError

        raise NotAuthorizedError("本地授权未开启")


class MockComputer:
    """mock 直连 ComputerControl：记录调用并返回固定成功 dict。"""

    def __init__(self):
        self.calls = []

    def call_tool(self, tool, arguments):
        self.calls.append((tool, dict(arguments)))
        return {"success": True, "tool": tool, "result": {"mock": "direct"}}


class MockAuthorizer:
    """mock ControlAuthorizer：可配置授权/高危确认结果，记录审计。"""

    def __init__(self, authorized=True, confirm_result=True):
        self.authorized = authorized
        self.confirm_result = confirm_result
        self.audits = []

    def is_authorized(self):
        return self.authorized

    def confirm(self, command):
        return self.confirm_result

    def audit(self, **kwargs):
        self.audits.append(kwargs)


class MockPipeline:
    """mock 检索管线：memory_search 应优先走 retrieve。"""

    def __init__(self):
        self.retrieve_kwargs = None

    def retrieve(self, query, top_k=None, **kwargs):
        self.retrieve_kwargs = {"query": query, "top_k": top_k}
        return {"memories": [{"content": query}], "context_text": f"【回忆】{query}"}


# --------------------------------------------------------------------------- #
# 电脑控制                                                                    #
# --------------------------------------------------------------------------- #


def test_computer_tools_via_bridge_success():
    """注入 bridge 且 computer_control=True 时，三件返回成功。"""
    bridge = MockBridge()
    reg = BuiltinToolRegistry(computer_bridge=bridge, config=_CONFIG_COMPUTER_ON)

    for tool_id in ("computer_screen_control", "computer_keyboard_control", "computer_run_command"):
        res = reg.call(tool_id, {"_k": 1})
        assert res["success"] is True, res
        assert res["tool"] == tool_id
        assert res["result"]["mock"] == "ok"
        assert res["authorized"] is True
    assert len(bridge.calls) == 3  # 三件均实际调用 bridge


def test_computer_tools_disabled_returns_not_authorized():
    """computer_control=False（默认）时返回 "电脑控制未授权" 错误（不抛）。"""
    reg = BuiltinToolRegistry(config=_CONFIG_ALL_OFF)  # 未注入后端亦应走禁用判断
    res = reg.call("computer_run_command", {"command": "echo hi"})
    assert res["success"] is False
    assert res["error"] == "电脑控制未授权"
    assert res["authorized"] is False


def test_computer_tools_bridge_raises_not_authorized():
    """bridge 内部抛 NotAuthorizedError（永久授权开关未开）时映射为未授权错误。"""
    reg = BuiltinToolRegistry(computer_bridge=NotAuthorizedBridge(), config=_CONFIG_COMPUTER_ON)
    res = reg.call("computer_run_command", {"command": "whoami"})
    assert res["success"] is False
    assert res["error"] == "电脑控制未授权"
    assert res["authorized"] is False


def test_computer_tools_uninjected_backend_error():
    """computer_control=True 但未注入后端时返回明确错误（不抛）。"""
    reg = BuiltinToolRegistry(config=_CONFIG_COMPUTER_ON)
    res = reg.call("computer_screen_control", {})
    assert res["success"] is False
    assert "未注入" in res["error"]


def test_computer_direct_without_authorizer_rejected():
    """仅注入 computer、无 authorizer/bridge 时拒绝执行（装配禁令），不产生本机动作。"""
    comp = MockComputer()
    reg = BuiltinToolRegistry(computer=comp, config=_CONFIG_COMPUTER_ON)
    res = reg.call("computer_run_command", {"command": "echo hi"})
    assert res["success"] is False
    assert res["authorized"] is False
    assert "装配不完整" in res["error"]
    assert comp.calls == []  # 未执行任何本机动作


def test_computer_direct_authorizer_not_authorized():
    """computer+authorizer 且授权关闭（默认）时拒绝，审计记录 NOT_AUTHORIZED。"""
    comp = MockComputer()
    auth = MockAuthorizer(authorized=False)
    reg = BuiltinToolRegistry(computer=comp, authorizer=auth, config=_CONFIG_COMPUTER_ON)
    res = reg.call("computer_screen_control", {"_k": 1})
    assert res["success"] is False
    assert res["error"] == "电脑控制未授权"
    assert res["authorized"] is False
    assert comp.calls == []
    assert any("NOT_AUTHORIZED" in a["result_summary"] for a in auth.audits)


def test_computer_direct_authorizer_success_and_audit():
    """computer+authorizer 且授权开启时执行成功，且写入审计记录。"""
    comp = MockComputer()
    auth = MockAuthorizer(authorized=True)
    reg = BuiltinToolRegistry(computer=comp, authorizer=auth, config=_CONFIG_COMPUTER_ON)
    res = reg.call("computer_keyboard_control", {"text": "hello"})
    assert res["success"] is True, res
    assert comp.calls == [("computer_keyboard_control", {"text": "hello"})]
    assert len(auth.audits) == 1
    assert auth.audits[0]["authorized"] is True
    assert "hello" in auth.audits[0]["arguments_summary"]


class SyncAwareComputer(MockComputer):
    """带 set_authorized 记录的 mock computer（MU2 同步验证用）。"""

    def __init__(self):
        super().__init__()
        self.authorized_flags = []

    def set_authorized(self, flag):
        self.authorized_flags.append(bool(flag))


def test_computer_direct_syncs_authorized_true_when_authorizer_on():
    """MU2：authorizer 授权开启时，直连回退把授权态同步传导到 computer.set_authorized(True)。"""
    comp = SyncAwareComputer()
    auth = MockAuthorizer(authorized=True)
    reg = BuiltinToolRegistry(computer=comp, authorizer=auth, config=_CONFIG_COMPUTER_ON)
    res = reg.call("computer_screen_control", {"_k": 1})
    assert res["success"] is True, res
    assert comp.authorized_flags == [True]  # 仅在 authorizer 开启侧同步 True


def test_computer_direct_no_sync_when_authorizer_off():
    """MU2 方向约束：authorizer 未开启时提前拒绝，不得触碰 computer 内部闸门。"""
    comp = SyncAwareComputer()
    auth = MockAuthorizer(authorized=False)
    reg = BuiltinToolRegistry(computer=comp, authorizer=auth, config=_CONFIG_COMPUTER_ON)
    res = reg.call("computer_screen_control", {"_k": 1})
    assert res["success"] is False
    assert comp.authorized_flags == []  # 不发生任何方向反转式放行
    assert comp.calls == []


def test_computer_run_command_needs_confirmation_direct():
    """直连回退下运行指令未通过高危确认 -> NEEDS_CONFIRMATION，不执行。"""
    comp = MockComputer()
    auth = MockAuthorizer(authorized=True, confirm_result=False)
    reg = BuiltinToolRegistry(computer=comp, authorizer=auth, config=_CONFIG_COMPUTER_ON)
    res = reg.call("computer_run_command", {"command": "rm -rf /"})
    assert res["success"] is False
    assert res["error_code"] == "NEEDS_CONFIRMATION"
    assert res["authorized"] is False
    assert comp.calls == []


# --------------------------------------------------------------------------- #
# 记忆工具                                                                    #
# --------------------------------------------------------------------------- #


def test_memory_write_and_search_with_tmp_store(tmp_path):
    """内存级 MemoryStore（tmp db）：memory_write add 成功，memory_search 可查回。"""
    store = MemoryStore(db_path=str(tmp_path / "memories.db"))
    reg = BuiltinToolRegistry(memory_store=store, config=_CONFIG_ALL_OFF)

    wres = reg.call("memory_write", {"content": "用户喜欢蓝色", "type": "long_term", "importance": 3, "tags": ["偏好"]})
    assert wres["success"] is True, wres
    mem_id = wres["result"]["id"]
    assert isinstance(mem_id, int)

    sres = reg.call("memory_search", {"query": "蓝色", "top_k": 5})
    assert sres["success"] is True, sres
    assert isinstance(sres["result"]["memories"], list)
    assert any(m.get("content") == "用户喜欢蓝色" for m in sres["result"]["memories"])


def test_memory_search_prefers_pipeline():
    """注入 pipeline 时 memory_search 优先走 retrieve；未注入 pipeline/store 时错误。"""
    pipeline = MockPipeline()
    reg = BuiltinToolRegistry(pipeline=pipeline, config=_CONFIG_ALL_OFF)
    res = reg.call("memory_search", {"query": "今天天气", "top_k": 3})
    assert res["success"] is True, res
    assert pipeline.retrieve_kwargs == {"query": "今天天气", "top_k": 3}
    assert res["result"]["memories"] == [{"content": "今天天气"}]


def test_memory_tools_uninjected_backend_error():
    """未注入 memory_store / pipeline 时返回明确错误（不抛）。"""
    reg = BuiltinToolRegistry(config=_CONFIG_ALL_OFF)
    wres = reg.call("memory_write", {"content": "x"})
    assert wres["success"] is False
    assert "未注入" in wres["error"]
    sres = reg.call("memory_search", {"query": "x"})
    assert sres["success"] is False
    assert "未注入" in sres["error"]


# --------------------------------------------------------------------------- #
# G-1 统一写入口 / G-8 退化检索语义                                            #
# --------------------------------------------------------------------------- #


class RecordingManager:
    """记录 add_memory 调用的 MemoryManager 替身：可配置去重命中。"""

    def __init__(self, dedup_content=None):
        self.calls = []
        self._dedup_content = dedup_content

    def add_memory(self, content, type="short_term", importance=3, agent_id="default", tags=None):
        self.calls.append(
            {"content": content, "type": type, "importance": importance, "agent_id": agent_id, "tags": tags}
        )
        if self._dedup_content is not None and content == self._dedup_content:
            return None  # 模拟去重命中
        return len(self.calls)


def test_memory_write_prefers_manager_injection(tmp_path):
    """G-1：注入 manager 后 memory_write 优先走 manager.add_memory（带去重语义）。"""
    store = MemoryStore(db_path=str(tmp_path / "memories.db"))
    manager = RecordingManager()
    reg = BuiltinToolRegistry(memory_store=store, manager=manager, config=_CONFIG_ALL_OFF)

    res = reg.call("memory_write", {"content": "用户喜欢蓝色", "type": "long_term", "importance": 4})
    assert res["success"] is True, res
    assert manager.calls == [
        {"content": "用户喜欢蓝色", "type": "long_term", "importance": 4, "agent_id": "default", "tags": "[]"}
    ]
    assert res["result"]["id"] == 1
    # 直连 store 未被写入
    assert store.list(type="long_term") == []


def test_memory_write_manager_path_passes_tags(tmp_path):
    """M-12：manager 路径不再静默丢弃 tags——tags 序列化后透传 add_memory。"""
    store = MemoryStore(db_path=str(tmp_path / "memories.db"))
    manager = RecordingManager()
    reg = BuiltinToolRegistry(memory_store=store, manager=manager, config=_CONFIG_ALL_OFF)

    res = reg.call("memory_write", {"content": "用户喜欢蓝色", "tags": ["颜色", "偏好"]})
    assert res["success"] is True, res
    # list 形态 tags 已序列化为 JSON 字符串后透传
    assert manager.calls[0]["tags"] == '["颜色", "偏好"]'


def test_memory_write_manager_dedup_reports_flag():
    """G-1：manager 去重命中（返回 None）时如实返回 deduplicated 标记。"""
    manager = RecordingManager(dedup_content="重复内容")
    reg = BuiltinToolRegistry(manager=manager, config=_CONFIG_ALL_OFF)
    res = reg.call("memory_write", {"content": "重复内容"})
    assert res["success"] is True, res
    assert res["result"] == {"id": None, "deduplicated": True}


def test_memory_search_degraded_substring_filter(tmp_path):
    """G-8：退化路径 query 非空时按 content 子串过滤 + importance 降序，结果带 degraded 标记。"""
    store = MemoryStore(db_path=str(tmp_path / "memories.db"))
    store.add({"type": "short_term", "content": "用户喜欢蓝色汽车", "importance": 2})
    store.add({"type": "short_term", "content": "天空是蓝色的", "importance": 5})
    store.add({"type": "short_term", "content": "完全无关的记忆", "importance": 5})
    reg = BuiltinToolRegistry(memory_store=store, config=_CONFIG_ALL_OFF)

    res = reg.call("memory_search", {"query": "蓝色", "top_k": 5})
    assert res["success"] is True, res
    result = res["result"]
    # 退化标记
    assert result["degraded"] is True
    # 子串过滤：仅含"蓝色"的两条命中，无关记忆不冒充检索结果
    contents = [m["content"] for m in result["memories"]]
    assert contents == ["天空是蓝色的", "用户喜欢蓝色汽车"]  # importance 降序：5 在前
    assert result["memories"][0]["importance"] == 5
    assert result["memories"][1]["importance"] == 2


def test_memory_search_degraded_recent_when_empty_query(tmp_path):
    """G-8：退化路径 query 为空时返回最近 limit 条，同样带 degraded 标记。"""
    store = MemoryStore(db_path=str(tmp_path / "memories.db"))
    for i in range(5):
        store.add({"type": "short_term", "content": f"记忆{i}", "importance": 1})
    reg = BuiltinToolRegistry(memory_store=store, config=_CONFIG_ALL_OFF)

    res = reg.call("memory_search", {"query": "", "top_k": 2})
    assert res["success"] is True, res
    result = res["result"]
    assert result["degraded"] is True
    # store.list 按 id 升序，取尾部 2 条即最新的记忆3、记忆4
    contents = [m["content"] for m in result["memories"]]
    assert contents == ["记忆3", "记忆4"]


# --------------------------------------------------------------------------- #
# 系统工具                                                                    #
# --------------------------------------------------------------------------- #


def test_system_info_time():
    """system_info category=time 返回正确结构。"""
    reg = BuiltinToolRegistry(config=_CONFIG_ALL_OFF)
    res = reg.call("system_info", {"category": "time"})
    assert res["success"] is True, res
    assert res["result"]["category"] == "time"
    assert "iso" in res["result"]
    assert "timestamp" in res["result"]


def test_system_info_status():
    """system_info category=status 返回正确结构。"""
    reg = BuiltinToolRegistry(config=_CONFIG_ALL_OFF)
    res = reg.call("system_info", {"category": "status"})
    assert res["success"] is True, res
    assert res["result"]["category"] == "status"
    assert res["result"]["status"] == "ok"
    assert res["result"]["app"] == "CX-A/CX-Lite"


def test_system_info_unknown_category():
    """system_info 未知类别返回失败错误（不抛）。"""
    reg = BuiltinToolRegistry(config=_CONFIG_ALL_OFF)
    res = reg.call("system_info", {"category": "bogus"})
    assert res["success"] is False
    assert "未知系统信息类别" in res["error"]


# --------------------------------------------------------------------------- #
# 统一入口 / 清单                                                             #
# --------------------------------------------------------------------------- #


def test_call_unknown_tool_returns_success_false():
    """registry.call 未知工具返回 success=False 明确错误（不抛异常）。"""
    reg = BuiltinToolRegistry(config=_CONFIG_ALL_OFF)
    res = reg.call("no_such_tool", {})
    assert res["success"] is False
    assert "未知内置工具" in res["error"]
    assert res["authorized"] is True


def test_list_tools_reports_source_builtin():
    """list_tools 返回全部注册工具，含 id/name/description/source=builtin。"""
    reg = BuiltinToolRegistry(config=_CONFIG_ALL_OFF)
    tools = reg.list_tools()
    ids = {t["id"] for t in tools}
    assert {"computer_screen_control", "computer_keyboard_control", "computer_run_command",
            "memory_write", "memory_search", "system_info"} <= ids
    for t in tools:
        assert t["source"] == "builtin"
        assert "description" in t and t["description"]
        assert t["category"] in ("computer_control", "memory_tools", "system_tools")


# --------------------------------------------------------------------------- #
# tools_provider 实时开关（L1）                                                #
# --------------------------------------------------------------------------- #


def test_tools_provider_runtime_toggle_applies():
    """注入 tools_provider 后，运行期翻动类别开关 call_tool 行为随之变化。"""
    live = {"computer_control": False, "memory_tools": True, "system_tools": True}
    reg = BuiltinToolRegistry(
        config=_CONFIG_COMPUTER_ON,  # 注册期快照为 computer_control=True
        tools_provider=lambda: live,
    )

    # 实时开关为关 -> 即使注册期快照开启也被拒绝
    res = reg.call("system_info", {"category": "status"})
    assert res["success"] is True

    live["system_tools"] = False
    res = reg.call("system_info", {"category": "status"})
    assert res["success"] is False
    assert res["error"] == "系统信息工具未启用"

    # 再翻回来恢复可用
    live["system_tools"] = True
    res = reg.call("system_info", {"category": "status"})
    assert res["success"] is True


def test_tools_provider_overrides_stale_snapshot():
    """provider 实时值优先于注册期快照：注册期关闭、实时打开则放行。"""
    bridge = MockBridge()
    live = {"computer_control": True}
    reg = BuiltinToolRegistry(
        config=_CONFIG_ALL_OFF,  # 注册期快照 computer_control=False
        computer_bridge=bridge,
        tools_provider=lambda: live,
    )
    res = reg.call("computer_run_command", {"command": "echo hi"})
    assert res["success"] is True
    assert len(bridge.calls) == 1


def test_no_provider_keeps_snapshot_semantics():
    """未注入 tools_provider 时保持注册期快照行为不变（兼容旧用法）。"""
    reg = BuiltinToolRegistry(config=_CONFIG_COMPUTER_ON)
    res = reg.call("computer_run_command", {"command": "echo hi"})
    # 无后端注入，但快照判定已放行到 handler -> 返回"后端未注入"而非"未授权"
    assert res["success"] is False
    assert "后端未注入" in res["error"]

    reg2 = BuiltinToolRegistry(config=_CONFIG_ALL_OFF)
    res2 = reg2.call("memory_write", {"content": "x"})
    assert res2["success"] is False
    assert "记忆存储后端未注入" in res2["error"]


def test_tools_provider_missing_key_falls_back_default():
    """provider 返回的 dict 键缺失时按 _CATEGORY_KEYS 默认值兜底。"""
    reg = BuiltinToolRegistry(config=_CONFIG_COMPUTER_ON, tools_provider=lambda: {})
    # memory_tools 缺失 -> 默认 True，可用
    res = reg.call("memory_write", {"content": "x"})
    assert "后端未注入" in (res.get("error") or "") or res["success"] is True
    # computer_control 缺失 -> 默认 False，禁用
    res2 = reg.call("computer_run_command", {"command": "echo hi"})
    assert res2["success"] is False
    assert res2["error"] == "电脑控制未授权"


def test_tools_provider_failure_falls_back_snapshot():
    """provider 抛异常 / 返回非 dict 时回落注册期快照（不抛）。"""
    def bad_provider():
        raise RuntimeError("config busy")

    reg = BuiltinToolRegistry(config=_CONFIG_COMPUTER_ON, tools_provider=bad_provider)
    res = reg.call("computer_run_command", {"command": "echo hi"})
    # 快照为开 -> 走 handler -> 后端未注入错误；若误判被禁用则会是"电脑控制未授权"
    assert res["success"] is False
    assert "后端未注入" in res["error"]

    reg2 = BuiltinToolRegistry(
        config=_CONFIG_COMPUTER_ON,
        tools_provider=lambda: "not-a-dict",
    )
    res2 = reg2.call("computer_screen_control", {})
    assert res2["success"] is False
    assert "后端未注入" in res2["error"]