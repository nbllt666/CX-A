# -*- coding: utf-8 -*-
"""MemoryDistiller 记忆蒸馏测试：JSON/行式解析、长消息切分、离线保护、空输入。

使用内存 FakeCloud 模拟 CloudAdapter（is_online / chat），不发起真实网络请求。
"""

import pytest

from lite.memory import DistillationPaused, MemoryDistiller
from lite.memory.distillation import (
    QUALITY_REJECT_THRESHOLD,
    S_DONE,
    S_FAILED,
    S_REJECT,
    DistillStateError,
    _estimate_tokens,
)
from lite.memory.storage import MemoryStore


class FakeCloud:
    """模拟 CloudAdapter：可配置在线状态与流式返回文本。"""

    def __init__(self, response=(), online=True):
        self._response = list(response)
        self._online = online
        self.call_recorder = []  # 记录每次 chat 的 messages 参数

    def is_online(self):
        return self._online

    def chat(self, messages):
        self.call_recorder.append(messages)
        for part in self._response:
            yield part


@pytest.fixture()
def store(tmp_path):
    return MemoryStore(db_path=str(tmp_path / "distill_test.db"))


def _mk_distiller(cloud, store):
    return MemoryDistiller(cloud=cloud, store=store)


def _text_messages(n, chars):
    """构造 n 条同长度 content 的文本消息。"""
    content = "探" * chars
    return [{"role": "user", "content": content} for _ in range(n)]


# ---------------------------------------------------------------- JSON 解析 + 落库
def test_json_list_flow(store):
    cloud = FakeCloud(
        response=[
            '[{"content": "用户偏好阅读科幻小说", "importance": 5},',
            '{"content": "用户喜欢雨天出行"}]',
        ]
    )
    distiller = _mk_distiller(cloud, store)
    added = distiller.distill_long_conversation(
        [{"role": "user", "content": "我喜欢看三体，也喜欢下雨"}, {"role": "assistant", "content": "好的"}]
    )

    assert len(added) == 2
    # 第一条：云端返回 important=5
    assert added[0]["content"] == "用户偏好阅读科幻小说"
    assert added[0]["importance"] == 5
    assert added[0]["type"] == "long_term"
    assert added[0]["agent_id"] == "default"
    # 第二条：云端未给 importance -> 默认 3
    assert added[1]["importance"] == 3

    # 落库字段校验
    row = store.get(added[0]["id"])
    assert row["type"] == "long_term"
    assert row["content"] == "用户偏好阅读科幻小说"
    assert row["importance"] == 5
    assert row["agent_id"] == "default"
    # 自定义 agent 归属
    assert store.list(agent_id="alice") == []


def test_json_list_agent_id(store):
    cloud = FakeCloud(response=['[{"content": "记住：密码是 123", "importance": 4}]'])
    distiller = _mk_distiller(cloud, store)
    added = distiller.distill_long_conversation(
        [{"role": "user", "content": "hi"}], agent_id="alice"
    )
    assert added[0]["agent_id"] == "alice"
    rows = store.list(agent_id="alice")
    assert len(rows) == 1


# ---------------------------------------------------------------- 行式解析 + 落库
def test_line_format_flow(store):
    cloud = FakeCloud(
        response=[
            "- 用户信仰无神论\n",
            "- 用户是素食主义者 [重要度:5]\n",
            "3. 用户一年后计划搬家\n",
        ]
    )
    distiller = _mk_distiller(cloud, store)
    added = distiller.distill_long_conversation(
        [{"role": "user", "content": "我不信神，只吃素"}]
    )

    assert len(added) == 3
    contents = [a["content"] for a in added]
    assert "用户信仰无神论" in contents
    assert "用户是素食主义者" in contents
    assert "用户一年后计划搬家" in contents
    # 携带 [重要度:5] 的条目标记 importance=5
    veg = next(a for a in added if "素食主义者" in a["content"])
    assert veg["importance"] == 5
    # 其余默认 3
    assert next(a for a in added if "无神论" in a["content"])["importance"] == 3


# ---------------------------------------------------------------- 长消息切分
def test_long_conversation_splits_chunks(store):
    # chunk_token_estimate=100，构造总 token 远超上限 -> 应触发多次 cloud.chat
    cloud = FakeCloud(response=["- 事实A", "- 事实B"])
    distiller = _mk_distiller(cloud, store)
    messages = _text_messages(4, 260)  # 每条约 130 token
    assert _estimate_tokens(messages[0]["content"]) > 100
    distiller.distill_long_conversation(messages, chunk_token_estimate=100)

    assert len(cloud.call_recorder) > 1  # 触发分块，多次调用云端


# ---------------------------------------------------------------- 离线保护
def test_offline_raises_paused(store):
    cloud = FakeCloud(response=["- x"], online=False)
    distiller = _mk_distiller(cloud, store)
    with pytest.raises(DistillationPaused):
        distiller.distill_long_conversation(
            [{"role": "user", "content": "hello"}]
        )
    # 离线时不调用 cloud.chat、不落库
    assert cloud.call_recorder == []
    assert store.list(type="long_term") == []


# ---------------------------------------------------------------- 空输入
def test_empty_messages_no_cloud_calls(store):
    cloud = FakeCloud(response=["- should not run"], online=True)
    distiller = _mk_distiller(cloud, store)
    added = distiller.distill_long_conversation([])
    assert added == []
    assert cloud.call_recorder == []
    assert store.list(type="long_term") == []


# ---------------------------------------------------------------- 状态机 + 质量门
def test_session_state_machine_done_flow(store):
    """质量合格：会话终态 done，quality_score 达标，真实落库。"""
    cloud = FakeCloud(response=['[{"content": "用户偏好阅读科幻小说", "importance": 5}]'])
    distiller = _mk_distiller(cloud, store)
    sessions = distiller.distill_with_sessions(
        [{"role": "user", "content": "我喜欢看三体"}]
    )
    assert len(sessions) == 1
    s = sessions[0]
    assert s["state"] == S_DONE  # pending→extracting→quality_check→committing→done
    assert s["quality_score"] >= QUALITY_REJECT_THRESHOLD
    assert len(s["added"]) == 1
    assert s["added"][0]["content"] == "用户偏好阅读科幻小说"
    # 真实落库
    assert len(store.list(type="long_term")) == 1


def test_quality_gate_rejects_noise(store):
    """质量门拒绝乱码/无事实内容：终态 rejected 且不落库。"""
    cloud = FakeCloud(response=["### @@@   ♦♠♣ xx"])
    distiller = _mk_distiller(cloud, store)
    sessions = distiller.distill_with_sessions(
        [{"role": "user", "content": "hello"}]
    )
    assert len(sessions) == 1
    s = sessions[0]
    assert s["state"] == S_REJECT
    assert s["quality_score"] < QUALITY_REJECT_THRESHOLD
    assert s["added"] == []
    assert "低于阈值" in s["reason"]
    assert store.list(type="long_term") == []


def test_quality_gate_rejects_empty_output(store):
    """云端返回空文本：质量分 0.0 被拒绝，不落库。"""
    cloud = FakeCloud(response=["   "])
    distiller = _mk_distiller(cloud, store)
    sessions = distiller.distill_with_sessions(
        [{"role": "user", "content": "hi"}]
    )
    assert sessions[0]["state"] == S_REJECT
    assert sessions[0]["quality_score"] == 0.0
    assert store.list(type="long_term") == []


def test_heuristic_quality_score_deterministic():
    """质量评分是确定性启发式：空/无事实 0 分，正常事实高分。"""
    distiller = MemoryDistiller(cloud=object(), store=object())
    assert distiller._heuristic_quality_score([], "") == 0.0
    assert distiller._heuristic_quality_score([], "nothing") == 0.0
    facts = [{"content": "用户偏好阅读科幻小说"}, {"content": "用户喜欢雨天出行"}]
    score = distiller._heuristic_quality_score(facts, "- 用户偏好阅读科幻小说")
    assert 0.5 <= score <= 0.8
    # 过短事实占比过半 -> 降权
    shorty = [{"content": "a"}, {"content": "b"}, {"content": "c"}]
    assert distiller._heuristic_quality_score(shorty, "- a\n- b\n- c") < 0.3


def test_invalid_state_transition_raises(store):
    """状态机强制合法转移：pending 直接到 done 抛 DistillStateError。"""
    distiller = _mk_distiller(FakeCloud(response=["- ok"]), store)
    session = distiller._new_session("default")
    with pytest.raises(DistillStateError):
        distiller._set_state(session, S_DONE)  # pending -> done 非法
    # 合法路径可推进
    distiller._set_state(session, "extracting")
    assert session["state"] == "extracting"


# ---------------------------------------------------------------- M8 补边与部分落库语义
def test_quality_state_exception_reaches_failed(store, capsys):
    """quality_check 态内抛异常：会话合法转移至 failed，不依赖兜底覆写。

    兜底覆写触发时会向 stderr 输出「非预期转移」warning；补边后正常路径不应出现。
    """
    cloud = FakeCloud(response=['[{"content": "用户偏好阅读科幻小说", "importance": 4}]'])
    distiller = _mk_distiller(cloud, store)

    def _boom(_raw_text):
        raise RuntimeError("quality gate exploded")

    distiller._parse_facts = _boom  # quality_check 态内异常（解析环节）
    sessions = distiller.distill_with_sessions([{"role": "user", "content": "hello"}])
    assert len(sessions) == 1
    s = sessions[0]
    assert s["state"] == S_FAILED
    assert "quality gate exploded" in s["error"]
    # 未进入落库阶段，无部分落库语义，库中无新增
    assert s["partial"] is False
    assert s["committed"] == 0
    assert store.list(type="long_term") == []
    # 补边后为合法转移，不应触发兜底覆写的「非预期转移」warning
    err_out = capsys.readouterr().err
    assert "非预期转移" not in err_out


def test_committing_partial_failure_keeps_committed_rows(store, monkeypatch):
    """committing 第 2 条落库失败：第 1 条仍在库中且 session.partial 为真，
    终态 failed 且 added 如实保留已落库条目。"""
    cloud = FakeCloud(
        response=[
            '[{"content": "用户偏好阅读科幻小说", "importance": 4},',
            '{"content": "用户喜欢雨天出行", "importance": 3}]',
        ]
    )
    distiller = _mk_distiller(cloud, store)
    original_add = distiller._store.add
    call_counter = {"n": 0}

    def flaky_add(payload):
        call_counter["n"] += 1
        if call_counter["n"] == 2:
            raise RuntimeError("db write failed")
        return original_add(payload)

    monkeypatch.setattr(distiller._store, "add", flaky_add)

    sessions = distiller.distill_with_sessions([{"role": "user", "content": "hi"}])
    assert len(sessions) == 1
    s = sessions[0]
    assert s["state"] == S_FAILED
    assert s["partial"] is True
    assert s["committed"] == 1
    assert len(s["added"]) == 1, "已成功落库的第 1 条应如实保留在 added"
    assert s["added"][0]["content"] == "用户偏好阅读科幻小说"
    assert "db write failed" in s["error"]
    # 库中实况：第 1 条在库、第 2 条未入库
    contents = [row["content"] for row in store.list(type="long_term")]
    assert contents == ["用户偏好阅读科幻小说"]
    # 聚合接口如实返回已落库条目（不清空/不误导统计）
    flat = [a for sess in distiller.last_sessions for a in sess.get("added") or []]
    assert [a["content"] for a in flat] == ["用户偏好阅读科幻小说"]


# ---------------------------------------------------------------- G-1 统一写入口（manager 注入）
class RecordingManager:
    """记录 add_memory 调用的 MemoryManager 替身：可配置去重命中。"""

    def __init__(self, dedup_content=None):
        self.calls = []
        self._dedup_content = dedup_content

    def add_memory(self, content, type="short_term", importance=3, agent_id="default"):
        self.calls.append(
            {"content": content, "type": type, "importance": importance, "agent_id": agent_id}
        )
        if self._dedup_content is not None and content == self._dedup_content:
            return None  # 模拟去重命中：跳过写入
        return len(self.calls)


def test_manager_injection_routes_add_memory(store):
    """G-1：注入 manager 后落库走 manager.add_memory（参数对齐 content/type/importance/agent_id）。"""
    cloud = FakeCloud(
        response=[
            '[{"content": "用户偏好阅读科幻小说", "importance": 5},',
            '{"content": "用户喜欢雨天出行"}]',
        ]
    )
    manager = RecordingManager()
    distiller = MemoryDistiller(cloud=cloud, store=store, manager=manager)
    added = distiller.distill_long_conversation(
        [{"role": "user", "content": "我喜欢看三体"}], agent_id="alice"
    )

    # 两条事实均走 manager.add_memory
    assert len(manager.calls) == 2
    assert manager.calls[0] == {
        "content": "用户偏好阅读科幻小说",
        "type": "long_term",
        "importance": 5,
        "agent_id": "alice",
    }
    assert manager.calls[1]["importance"] == 3  # 云端未给 importance -> 默认 3
    # added 如实反映 manager 返回的 id
    assert [a["id"] for a in added] == [1, 2]
    # 直连 store.add 未被触碰（store 无写入）
    assert store.list(type="long_term") == []


def test_manager_dedup_skip_not_counted_in_added(store):
    """G-1：manager 去重命中（返回 None）的条目不计入 added，其余正常落库。"""
    cloud = FakeCloud(
        response=[
            '[{"content": "用户偏好阅读科幻小说", "importance": 4},',
            '{"content": "用户喜欢雨天出行", "importance": 3}]',
        ]
    )
    manager = RecordingManager(dedup_content="用户偏好阅读科幻小说")
    distiller = MemoryDistiller(cloud=cloud, store=store, manager=manager)
    sessions = distiller.distill_with_sessions([{"role": "user", "content": "hi"}])

    s = sessions[0]
    assert s["state"] == S_DONE
    # 第 1 条被去重跳过，仅第 2 条计入 added
    assert len(manager.calls) == 2
    assert [a["content"] for a in s["added"]] == ["用户喜欢雨天出行"]
    assert s["committed"] == 1
    assert s["partial"] is False  # 去重跳过不是落库失败


def test_manager_absent_keeps_store_add(store):
    """G-1：manager 缺席时保持原 store.add 直写行为兜底。"""
    cloud = FakeCloud(response=['[{"content": "用户偏好阅读科幻小说", "importance": 4}]'])
    distiller = _mk_distiller(cloud, store)  # 不传 manager
    added = distiller.distill_long_conversation([{"role": "user", "content": "hi"}])
    assert len(added) == 1
    # 直写 store：库中可查回
    rows = store.list(type="long_term")
    assert len(rows) == 1
    assert rows[0]["content"] == "用户偏好阅读科幻小说"