# -*- coding: utf-8 -*-
"""本地多 Agent 人设管理单元测试（Task E2）。

覆盖：种子初始化、CRUD（list/get/create/update/delete/set_enabled）、持久化往返
（重载 AgentManager 实例验证落盘）、AgentNotFound 异常。
"""

import json

import pytest

from lite.management.local_agents import AgentManager, AgentNotFound


@pytest.fixture()
def manager(tmp_path):
    """用临时目录隔离存储路径的 AgentManager 实例。"""
    return AgentManager(path=str(tmp_path / "agents.json"))


# ---------------------------------------------------------------- 种子初始化
def test_seed_init(tmp_path):
    path = tmp_path / "agents.json"
    assert not path.exists()
    mgr = AgentManager(path=str(path))
    # 首启自动建文件并注入默认种子
    assert path.exists()
    seeds = mgr.list()
    # 20261005：内置种子为 default（软软）+ memory-agent（记忆管理助手）
    assert len(seeds) == 2
    seed = seeds[0]
    assert seed.id == "default"
    assert seed.name == "软软"
    assert "记在心上" in seed.persona
    assert seed.voice == "cx-open"
    assert seed.enabled is True
    # 内置记忆管理助手随首启注入（persona 为 [memory:op] 指令协议）
    mem_agent = mgr.get("memory-agent")
    assert mem_agent.name == "记忆管理助手"
    assert "[memory:" in mem_agent.persona
    assert mem_agent.enabled is True


# ---------------------------------------------------------------- 历史默认人设迁移
def test_legacy_seed_persona_migrated(tmp_path):
    """旧版默认人设（精确匹配）加载时迁移为新默认，并落盘持久化。"""
    path = tmp_path / "agents.json"
    legacy = {
        "id": "default",
        "name": "软软",
        "persona": "温柔可靠的赛博伴侣，话少但事事记在心上",
        "voice": "cx-open",
        "enabled": True,
        "created_at": "2026-09-24T23:07:33.538039",
        "updated_at": "2026-09-24T23:07:33.538039",
    }
    path.write_text(json.dumps([legacy], ensure_ascii=False), encoding="utf-8")
    mgr = AgentManager(path=str(path))
    seed = mgr.get("default")
    assert seed.persona == "话不多但事事记在心上，安静又可靠"
    # 迁移结果落盘（重载仍为新文案）
    reloaded = AgentManager(path=str(path))
    assert reloaded.get("default").persona == "话不多但事事记在心上，安静又可靠"


def test_legacy_seed_migration_leaves_custom_persona(tmp_path):
    """用户自定义人设（非旧默认原文）：一律不动。"""
    path = tmp_path / "agents.json"
    custom = {
        "id": "default",
        "name": "软软",
        "persona": "我自己写的人设，谁也别动",
        "voice": "cx-open",
        "enabled": True,
        "created_at": "2026-09-24T23:07:33.538039",
        "updated_at": "2026-09-24T23:07:33.538039",
    }
    path.write_text(json.dumps([custom], ensure_ascii=False), encoding="utf-8")
    mgr = AgentManager(path=str(path))
    assert mgr.get("default").persona == "我自己写的人设，谁也别动"


# ---------------------------------------------------------------- CRUD
def test_create(manager):
    agent = manager.create(name="小夜", persona="安静的夜猫子，擅长深夜聊天")
    assert agent.id.startswith("agent-")
    assert agent.name == "小夜"
    assert agent.persona == "安静的夜猫子，擅长深夜聊天"
    assert agent.voice == "cx-open"  # 默认音色
    assert agent.enabled is True
    assert agent.created_at and agent.updated_at


def test_create_custom_voice(manager):
    agent = manager.create(name="小夜", persona="吉他手", voice="miku")
    assert agent.voice == "miku"


def test_get(manager):
    created = manager.create(name="小夜", persona="……")
    fetched = manager.get(created.id)
    assert fetched.id == created.id
    assert fetched.name == "小夜"


def test_get_missing_raises(manager):
    with pytest.raises(AgentNotFound):
        manager.get("agent-does-not-exist")


def test_update(manager):
    created = manager.create(name="小夜", persona="原始人设")
    updated = manager.update(created.id, name="小夜二号", persona="新的人设")
    assert updated.name == "小夜二号"
    assert updated.persona == "新的人设"
    # 持久化已生效：重取可见
    assert manager.get(created.id).name == "小夜二号"
    assert updated.updated_at >= created.updated_at


def test_update_unknown_fields_ignored(manager):
    created = manager.create(name="小夜", persona="……")
    updated = manager.update(created.id, name="新名", bogus="忽略")
    assert updated.name == "新名"


def test_update_missing_raises(manager):
    with pytest.raises(AgentNotFound):
        manager.update("agent-nope", name="x")


# ---------------------------------------------------------------- M4 严格解析
def test_update_enabled_strict_parsing(manager):
    """M4：enabled 支持的字面量映射正确，bool 原样透传。"""
    created = manager.create(name="小夜", persona="……")
    # 字符串真值
    for raw in ("true", "1", "yes", "TRUE", " Yes "):
        manager.update(created.id, enabled=raw)
        assert manager.get(created.id).enabled is True
    # 字符串假值（含空串）
    for raw in ("false", "0", "", "no", "False"):
        manager.update(created.id, enabled=raw)
        assert manager.get(created.id).enabled is False
    # bool 原样
    manager.update(created.id, enabled=True)
    assert manager.get(created.id).enabled is True
    manager.update(created.id, enabled=False)
    assert manager.get(created.id).enabled is False


@pytest.mark.parametrize("bad", ["on", "off", "enable", "2", "是"])
def test_update_enabled_bad_string_raises(manager, bad):
    """M4：其余字符串一律 ValueError，不落盘、不静默取真值。"""
    created = manager.create(name="小夜", persona="……")
    before = manager.get(created.id).enabled
    with pytest.raises(ValueError, match="invalid enabled value"):
        manager.update(created.id, enabled=bad)
    assert manager.get(created.id).enabled == before  # 失败不改变状态


@pytest.mark.parametrize("bad_type", [1, 0, None, [True]])
def test_update_enabled_non_bool_non_str_raises(manager, bad_type):
    """M4：其他类型（含 int / None / list）一律 ValueError。"""
    created = manager.create(name="小夜", persona="……")
    with pytest.raises(ValueError, match="invalid enabled value"):
        manager.update(created.id, enabled=bad_type)


def test_delete(manager):
    created = manager.create(name="小夜", persona="……")
    assert len(manager.list()) == 3  # 种子 default + memory-agent + 新建
    manager.delete(created.id)
    assert len(manager.list()) == 2
    with pytest.raises(AgentNotFound):
        manager.get(created.id)


def test_delete_missing_raises(manager):
    with pytest.raises(AgentNotFound):
        manager.delete("agent-nope")


def test_set_enabled(manager):
    created = manager.create(name="小夜", persona="……")
    assert created.enabled is True
    manager.set_enabled(created.id, False)
    assert manager.get(created.id).enabled is False
    manager.set_enabled(created.id, True)
    assert manager.get(created.id).enabled is True


def test_list_enabled_filter(manager):
    a = manager.create(name="小夜", persona="……")
    manager.set_enabled(a.id, False)
    enabled = manager.list(enabled=True)
    disabled = manager.list(enabled=False)
    assert all(x.enabled for x in enabled)
    assert all(not x.enabled for x in disabled)
    assert len(manager.list(enabled=True)) == 2  # 种子 default + memory-agent 启用
    assert manager.list(enabled=False) == [a]


# ---------------------------------------------------------------- 持久化往返
def test_persistence_roundtrip(tmp_path):
    path = tmp_path / "agents.json"
    mgr1 = AgentManager(path=str(path))
    mgr1.create(name="小夜", persona="夜猫子", voice="miku")

    # 重载新实例：应从盘上读到刚才的数据（内置种子 + 新建）
    mgr2 = AgentManager(path=str(path))
    names = [a.name for a in mgr2.list()]
    assert names == ["软软", "记忆管理助手", "小夜"]
    night = mgr2.get([a.id for a in mgr2.list() if a.name == "小夜"][0])
    assert night.persona == "夜猫子"
    assert night.voice == "miku"


def test_persistence_utf8_not_escaped(tmp_path):
    path = tmp_path / "agents.json"
    mgr = AgentManager(path=str(path))
    mgr.create(name="软糖", persona="中文人设不转义")
    raw = path.read_text("utf-8")
    assert "软糖" in raw
    assert "\\u" not in raw


def test_file_missing_creates_empty_list_then_seed(tmp_path):
    """文件不存在时按空列表初始化并注入种子，写盘后 become 可读。"""
    path = tmp_path / "agents.json"
    mgr = AgentManager(path=str(path))
    parsed = json.loads(path.read_text("utf-8"))
    assert isinstance(parsed, list)
    assert parsed[0]["id"] == "default"
    assert mgr.list()[0].name == "软软"


# ---------------------------------------------------------------- 批次5（第三轮体检）：脏数据兜底
def test_dirty_entry_without_id_skipped_not_fatal(tmp_path, capsys):
    """M-17：单条脏数据缺 id 键仅跳过并告警，AgentManager 构造不再被拖垮。"""
    path = tmp_path / "agents.json"
    path.write_text(
        json.dumps(
            [
                {"name": "坏条目无id", "persona": "p"},  # 缺 id：KeyError
                {"id": "a2", "name": "好条目", "persona": "ok"},
                "非 dict 条目",
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    mgr = AgentManager(path=str(path))
    names = [a.name for a in mgr.list()]
    assert "好条目" in names
    assert "坏条目无id" not in names
    assert "已跳过" in capsys.readouterr().out


# ---------------------------------------------------------------- L6a 原子写
def test_save_atomic_preserves_original_on_dump_failure(tmp_path, monkeypatch):
    """L6a：json.dump 抛错时 agents.json 原内容完好，tmp 被清理且异常上抛。"""
    path = tmp_path / "agents.json"
    mgr = AgentManager(path=str(path))
    mgr.create(name="小夜", persona="原有数据")
    original_raw = path.read_text("utf-8")
    assert "小夜" in original_raw

    import lite.management.local_agents as la

    def broken_dump(obj, fh, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(la.json, "dump", broken_dump)

    with pytest.raises(OSError, match="disk full"):
        mgr.create(name="崩溃写", persona="不应落盘")

    # 原 agents.json 内容完好无损
    assert path.read_text("utf-8") == original_raw
    # 同目录无残留 .tmp 文件
    leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_save_atomic_no_leftover_tmp_on_success(tmp_path):
    """L6a：正常写入后同目录无 .tmp 残留，内容可解析。"""
    path = tmp_path / "agents.json"
    mgr = AgentManager(path=str(path))
    mgr.create(name="小夜", persona="原子写成功路径")
    leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    parsed = json.loads(path.read_text("utf-8"))
    assert [a["name"] for a in parsed] == ["软软", "记忆管理助手", "小夜"]


# ---------------------------------------------------------------- 第四轮体检批次C：损坏隔离
def test_corrupt_json_isolated_not_overwritten(tmp_path, caplog):
    """损坏 agents.json：先改名隔离（.corrupt-<时间戳>）再初始化种子，
    原始数据不得被种子覆盖丢失，且打印中文 ERROR 告警。"""
    import logging

    path = tmp_path / "agents.json"
    corrupt_raw = "{这不是合法 JSON"
    path.write_text(corrupt_raw, encoding="utf-8")

    with caplog.at_level(logging.ERROR, logger="lite.management.local_agents"):
        mgr = AgentManager(path=str(path))

    # 原损坏内容被完整隔离保留（未物理丢失）
    isolated = list(tmp_path.glob("agents.json.corrupt-*"))
    assert len(isolated) == 1
    assert isolated[0].read_text(encoding="utf-8") == corrupt_raw
    # 正式位为种子初始化产物（新文件内容与损坏内容不同）
    parsed = json.loads(path.read_text("utf-8"))
    assert parsed[0]["id"] == "default"
    assert mgr.list()[0].name == "软软"
    # 中文 ERROR 告警已记录
    assert any("解析失败" in rec.getMessage() for rec in caplog.records)


def test_non_list_top_level_isolated(tmp_path):
    """顶层结构非列表（如被写成 dict）同样按损坏隔离，防种子覆盖写回。"""
    path = tmp_path / "agents.json"
    path.write_text('{"id": "default"}', encoding="utf-8")
    mgr = AgentManager(path=str(path))
    isolated = list(tmp_path.glob("agents.json.corrupt-*"))
    assert len(isolated) == 1
    assert json.loads(isolated[0].read_text("utf-8")) == {"id": "default"}
    # 正式位重建为种子列表（default + 内置 memory-agent）
    assert [a.id for a in mgr.list()] == ["default", "memory-agent"]


# ---------------------------------------------------------------- 内置记忆管理助手（20261005，spec: align-wizard-settings-memory-pet）
def test_memory_agent_persona_contains_protocol_and_ops(tmp_path):
    """memory-agent persona 完整覆盖 [memory:op {...}] 指令协议与五类操作集。"""
    mgr = AgentManager(path=str(tmp_path / "agents.json"))
    agent = mgr.get("memory-agent")
    # 协议格式与五个操作（search/read/write/update/delete）逐个登记在 persona
    for token in (
        "[memory:op {json参数}]",
        "[memory:search",
        "[memory:read",
        "[memory:write",
        "[memory:update",
        "[memory:delete",
    ):
        assert token in agent.persona, f"persona 缺少协议说明：{token}"
    # 类型值域四类在 persona 中说明
    for t in ("long_term", "short_term", "permanent", "diary"):
        assert t in agent.persona


def test_memory_agent_reseeded_after_delete_and_reload(tmp_path):
    """用户删除 memory-agent 后重载自动补种（管理通道内置 agent 不随删除消失）。"""
    path = tmp_path / "agents.json"
    mgr1 = AgentManager(path=str(path))
    mgr1.delete("memory-agent")
    assert "memory-agent" not in {a.id for a in mgr1.list()}
    # 重载：幂等补种
    mgr2 = AgentManager(path=str(path))
    agent = mgr2.get("memory-agent")
    assert agent.name == "记忆管理助手"
    # 落盘持久化（重载读盘仍存在）
    parsed = json.loads(path.read_text("utf-8"))
    assert "memory-agent" in {a["id"] for a in parsed}


def test_memory_agent_upgrades_legacy_file_without_it(tmp_path):
    """既有 agents.json（无 memory-agent）升级路径：加载即补种，用户数据不动。"""
    path = tmp_path / "agents.json"
    legacy = {
        "id": "default",
        "name": "软软",
        "persona": "我自己定义的人设不动",
        "voice": "cx-open",
        "enabled": True,
        "created_at": "2026-09-24T00:00:00",
        "updated_at": "2026-09-24T00:00:00",
    }
    path.write_text(json.dumps([legacy], ensure_ascii=False), encoding="utf-8")
    mgr = AgentManager(path=str(path))
    ids = [a.id for a in mgr.list()]
    assert ids == ["default", "memory-agent"]
    # 用户自定义人设不被迁移逻辑改动
    assert mgr.get("default").persona == "我自己定义的人设不动"