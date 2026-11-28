# -*- coding: utf-8 -*-
"""DreamTalkService 编排测试：主链路、无梦、轮数上限、幂等与上下文隔离。"""

import asyncio

import pytest

from lite.dreamtalk.adapters.memory import ManualMemorySource
from lite.dreamtalk.adapters.store import MemorySessionStore
from lite.dreamtalk.core.dialogue import DialogueConfig
from lite.dreamtalk.core.errors import NoMemoryMaterial, SessionEnded, SessionNotFound
from lite.dreamtalk.core.models import (
    MODE_DREAMLESS,
    MODE_MEMORY,
    MODE_WILD,
    STATUS_ENDED,
    Emotion,
    MemoryMaterial,
)
from lite.dreamtalk.service import DreamTalkService

_SUMMARY_JSON = (
    '{"title": "走廊尽头的灯", "summary": "灯一盏盏亮起，我推开了门。",'
    ' "emotion_residue": {"valence": 0.2, "arousal": 0.5}}'
)


class _RecordingClient:
    """记录每次调用 messages 的确定性客户端（含摘要 JSON）。"""

    def __init__(self, name="client"):
        self.name = name
        self.calls = []

    async def chat(self, messages, *, temperature: float = 0.0) -> str:
        self.calls.append({"messages": list(messages), "temperature": temperature})
        system = messages[0].content if messages else ""
        if "梦醒后的记录者" in system:
            return _SUMMARY_JSON
        if "【你的身份：潜意识导演】" in system:
            return f"{self.name}导演：雾把走廊的轮廓擦掉了。"
        return f"{self.name}梦者：我向前走，脚下泛起涟漪。"


class _NonJsonClient(_RecordingClient):
    """摘要返回非 JSON：触发本地截断降级。"""

    async def chat(self, messages, *, temperature: float = 0.0) -> str:
        self.calls.append({"messages": list(messages), "temperature": temperature})
        system = messages[0].content if messages else ""
        if "梦醒后的记录者" in system:
            return "这不是 JSON"
        if "【你的身份：潜意识导演】" in system:
            return "导演：站台向上升。"
        return "梦者：我把钥匙握在手里。"


def _service(client, store=None, material=None, config=None, mode_weights=None):
    return DreamTalkService(
        dreamer=client,
        director=client,
        store=store or MemorySessionStore(),
        memory_source=ManualMemorySource(material),
        dialogue_config=config,
        mode_weights=mode_weights,
    )


def _material(*memories, entities=None, valence=0.0, arousal=0.0, source="manual"):
    return MemoryMaterial(
        day_memories=list(memories),
        entities=list(entities or []),
        emotion=Emotion(valence, arousal),
        source=source,
    )


# ------------------------------------------------------------------ 主链路
def test_main_chain_turn_order_and_recall():
    client = _RecordingClient()
    service = _service(client, material=_material("楼下的猫", "海边的风"))

    view = asyncio.run(service.start_session("default", MODE_MEMORY))
    assert [turn.speaker for turn in view.turns] == ["director", "dreamer"]
    session_id = view.session.id
    assert view.session.status != STATUS_ENDED

    view = asyncio.run(service.interject(session_id, "我伸手碰了碰门"))
    assert [turn.speaker for turn in view.turns] == [
        "director", "dreamer", "awakened", "director", "dreamer",
    ]

    view = asyncio.run(service.wake(session_id))
    assert view.session.status == STATUS_ENDED
    assert view.session.title == "走廊尽头的灯"
    assert view.recall is not None
    assert "turns" not in view.recall.to_dict()

    recall = asyncio.run(service.get_recall(session_id))
    assert recall.session_id == session_id
    assert recall.summary == "灯一盏盏亮起，我推开了门。"

    archive = asyncio.run(service.get_session(session_id))
    assert len(archive.turns) == 5


def test_empty_interject_does_not_add_awakened_turn():
    client = _RecordingClient()
    service = _service(client, material=_material("猫"))
    session_id = asyncio.run(service.start_session("default", MODE_WILD)).session.id
    view = asyncio.run(service.interject(session_id, "   "))
    assert [turn.speaker for turn in view.turns] == ["director", "dreamer", "director", "dreamer"]


def test_temperature_follows_mode_and_role():
    client = _RecordingClient()
    config = DialogueConfig(director_temperature=0.5, dreamer_temperature=0.6,
                            wild_director_temperature=1.1, wild_dreamer_temperature=1.2)
    service = _service(client, material=_material("猫"), config=config)
    asyncio.run(service.start_session("default", MODE_MEMORY))
    temps = [call["temperature"] for call in client.calls[:2]]
    assert temps == [0.5, 0.6]

    client.calls.clear()
    asyncio.run(service.start_session("default", MODE_WILD))
    assert [call["temperature"] for call in client.calls[:2]] == [1.1, 1.2]


# ------------------------------------------------------------------ 无梦与拒绝
def test_dreamless_makes_zero_llm_calls():
    client = _RecordingClient()
    service = _service(client, material=_material())
    view = asyncio.run(service.start_session("default", MODE_DREAMLESS))
    assert client.calls == []
    assert view.turns == []
    assert view.session.status == STATUS_ENDED
    assert view.session.title == "一夜无梦"
    assert view.session.summary


def test_memory_mode_without_material_is_rejected():
    client = _RecordingClient()
    service = _service(client, material=_material())
    with pytest.raises(NoMemoryMaterial):
        asyncio.run(service.start_session("default", MODE_MEMORY))
    assert client.calls == []


def test_wake_twice_and_message_after_end_raise_409():
    client = _RecordingClient()
    service = _service(client, material=_material("猫"))
    session_id = asyncio.run(service.start_session("default", MODE_MEMORY)).session.id
    asyncio.run(service.wake(session_id))
    with pytest.raises(SessionEnded):
        asyncio.run(service.wake(session_id))
    with pytest.raises(SessionEnded):
        asyncio.run(service.interject(session_id, "还在吗"))


def test_unknown_session_raises_404():
    service = _service(_RecordingClient(), material=_material("猫"))
    with pytest.raises(SessionNotFound):
        asyncio.run(service.get_session("nope"))
    with pytest.raises(SessionNotFound):
        asyncio.run(service.interject("nope", "hi"))


# ------------------------------------------------------------------ 轮数与摘要
def test_max_rounds_ends_session_automatically():
    client = _RecordingClient()
    service = _service(
        client, material=_material("猫"), config=DialogueConfig(max_rounds=1)
    )
    view = asyncio.run(service.start_session("default", MODE_MEMORY))
    assert view.session.status == STATUS_ENDED
    assert len(view.turns) == 2
    assert view.session.summary
    assert view.recall is not None


def test_summary_falls_back_to_local_truncation():
    client = _NonJsonClient()
    service = _service(client, material=_material("猫"))
    session_id = asyncio.run(service.start_session("default", MODE_MEMORY)).session.id
    view = asyncio.run(service.wake(session_id))
    assert view.session.title == "无名之梦"
    assert "钥匙" in view.session.summary


# ------------------------------------------------------------------ 上下文隔离
def test_messages_contain_only_material_and_dream_turns():
    client = _RecordingClient()
    service = _service(client, material=_material("SENTINEL-MEM-1"), )
    asyncio.run(service.start_session("default", MODE_MEMORY))
    chat_sentinel = "SENTINEL-CHAT-1"
    for call in client.calls:
        for message in call["messages"]:
            assert message.role in ("system", "user", "assistant")
            assert chat_sentinel not in message.content
            assert "【回忆】" not in message.content
    system = client.calls[0]["messages"][0].content
    assert "SENTINEL-MEM-1" in system  # 素材是梦内唯一外部输入
    assert "[潜意识导演]" not in system


def test_consecutive_sessions_do_not_inherit():
    store = MemorySessionStore()
    first_client = _RecordingClient("第一场")
    first = _service(first_client, store=store, material=_material("第一场记忆"))
    asyncio.run(first.start_session("default", MODE_MEMORY))

    second_client = _RecordingClient("第二场")
    second = _service(second_client, store=store, material=_material("第二场记忆"))
    asyncio.run(second.start_session("default", MODE_MEMORY))
    for call in second_client.calls:
        for message in call["messages"]:
            assert "第一场" not in message.content


def test_material_source_is_preserved():
    client = _RecordingClient()
    material = _material("猫", source="cxa_memory")
    service = _service(client, material=material)
    view = asyncio.run(service.start_session("default", MODE_MEMORY))
    assert view.session.effective_material.source == "cxa_memory"


def test_context_turns_limits_history_in_messages():
    """context_turns=2 时只携带最近两个回合。"""
    client = _RecordingClient()
    service = _service(
        client, material=_material("猫"), config=DialogueConfig(context_turns=2)
    )
    session_id = asyncio.run(service.start_session("default", MODE_MEMORY)).session.id
    asyncio.run(service.interject(session_id, "继续"))
    last_call = client.calls[-1]["messages"]
    # system + 最近 2 个回合 + 本轮指令
    assert len(last_call) == 4
