# -*- coding: utf-8 -*-
"""MemoryDistiller 记忆蒸馏测试：JSON/行式解析、长消息切分、离线保护、空输入。

使用内存 FakeCloud 模拟 CloudAdapter（is_online / chat），不发起真实网络请求。
"""

import pytest

from lite.memory import DistillationPaused, MemoryDistiller
from lite.memory.distillation import _estimate_tokens
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