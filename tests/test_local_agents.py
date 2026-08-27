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
    assert len(seeds) == 1
    seed = seeds[0]
    assert seed.id == "default"
    assert seed.name == "软软"
    assert "赛博伴侣" in seed.persona
    assert seed.voice == "cx-open"
    assert seed.enabled is True


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
    assert len(manager.list()) == 2  # 种子 default + 新建
    manager.delete(created.id)
    assert len(manager.list()) == 1
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
    assert len(manager.list(enabled=True)) == 1  # 只有种子 default 启用
    assert manager.list(enabled=False) == [a]


# ---------------------------------------------------------------- 持久化往返
def test_persistence_roundtrip(tmp_path):
    path = tmp_path / "agents.json"
    mgr1 = AgentManager(path=str(path))
    mgr1.create(name="小夜", persona="夜猫子", voice="miku")

    # 重载新实例：应从盘上读到刚才的数据（种子 + 新建）
    mgr2 = AgentManager(path=str(path))
    names = [a.name for a in mgr2.list()]
    assert names == ["软软", "小夜"]
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