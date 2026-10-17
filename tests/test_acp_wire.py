# -*- coding: utf-8 -*-
"""ACP wire 契约层单测（20261017 CX-O 全量迁移批次 1）。

覆盖 lite/acp/wire.py：wire 形态冻结（11 键消息 / 9 键节点 / 11 键连接）、
msg_type→type 映射、接收双键兼容、缺键补默认、非法输入拒绝、LOCAL_AGENT_SOURCES。
"""

from lite.acp.wire import (
    LOCAL_AGENT_SOURCES,
    MAX_MESSAGES_PER_KEY,
    agent_info_from_beacon,
    make_agent_info,
    make_connection_info,
    make_message,
    message_from_wire,
)


class TestMakeMessage:
    """ACPMessageInfo wire 形态（与 CX-O to_dict 逐键对齐）。"""

    def test_wire_keys_and_defaults(self):
        msg = make_message(from_agent_id="a", from_agent_name="甲", to_agent_id="b")
        assert set(msg.keys()) == {
            "id", "type", "from_agent_id", "from_agent_name", "to_agent_id",
            "to_group_id", "content", "timestamp", "is_read", "is_sent", "metadata",
        }
        # wire 键是 type（非 msg_type）——CX-O 消息投递契约
        assert "msg_type" not in msg
        assert msg["type"] == "chat"
        assert msg["to_group_id"] is None
        assert msg["content"] == {}
        assert msg["is_read"] is False
        assert msg["is_sent"] is False
        assert msg["metadata"] == {}
        assert len(msg["id"]) == 32  # uuid4 hex
        assert "T" in msg["timestamp"]  # ISO8601

    def test_custom_type_and_content(self):
        msg = make_message(msg_type="group_message", to_group_id="g1",
                           content={"text": "你好"}, is_sent=True)
        assert msg["type"] == "group_message"
        assert msg["to_agent_id"] is None
        assert msg["to_group_id"] == "g1"
        assert msg["content"] == {"text": "你好"}
        assert msg["is_sent"] is True

    def test_unique_ids(self):
        assert make_message()["id"] != make_message()["id"]


class TestMessageFromWire:
    """接收侧归一化（type/msg_type 双键兼容 + 缺键补默认）。"""

    def test_wire_type_key(self):
        msg = message_from_wire({"type": "chat", "from_agent_id": "cxo",
                                 "from_agent_name": "CX-O", "content": {"text": "hi"}})
        assert msg is not None
        assert msg["type"] == "chat"
        assert msg["from_agent_id"] == "cxo"
        assert msg["content"] == {"text": "hi"}
        assert msg["is_sent"] is False

    def test_legacy_msg_type_key_compat(self):
        msg = message_from_wire({"msg_type": "control", "from_agent_id": "x"})
        assert msg is not None
        assert msg["type"] == "control"  # 归一化为 wire 键

    def test_missing_keys_filled(self):
        msg = message_from_wire({})
        assert msg["type"] == "chat"
        assert msg["from_agent_id"] == ""
        assert msg["content"] == {}
        assert msg["metadata"] == {}
        assert msg["to_agent_id"] is None
        assert msg["id"]

    def test_metadata_non_dict_rejected_to_empty(self):
        msg = message_from_wire({"metadata": "bad"})
        assert msg["metadata"] == {}

    def test_invalid_payloads_return_none(self):
        assert message_from_wire(None) is None
        assert message_from_wire("text") is None
        assert message_from_wire([1, 2]) is None

    def test_content_string_preserved(self):
        msg = message_from_wire({"content": "纯文本也保留"})
        assert msg["content"] == "纯文本也保留"


class TestAgentInfo:
    """ACPAgentInfo 字段与默认值。"""

    def test_defaults(self):
        info = make_agent_info("n1")
        assert info == {
            "id": "n1", "name": "", "host": "", "port": 0, "status": "offline",
            "version": "1.0.0", "capabilities": [], "last_seen": "",
            "metadata": {"source": ""},
        }

    def test_beacon_normalized_with_source_host(self):
        info = agent_info_from_beacon(
            {"agent_id": "cxo-1", "agent_name": "CX-O Agent", "version": "1.0.0",
             "capabilities": ["chat"], "port": 9999},
            host="192.168.1.8",
        )
        assert info["host"] == "192.168.1.8"  # host 取 UDP 源地址
        assert info["port"] == 9999  # port 取载荷（双轨语义）
        assert info["status"] == "online"
        assert info["metadata"]["source"] == "lan"
        assert info["last_seen"]

    def test_beacon_self_port_zero_safe(self):
        info = agent_info_from_beacon({"agent_id": "x", "port": "bad"}, host="h")
        assert info["port"] == 0


class TestConnectionInfo:
    """ACPConnectionInfo（通讯录条目语义）。"""

    def test_shape(self):
        conn = make_connection_info("local", "remote", host="10.0.0.2", port=8600)
        assert set(conn.keys()) == {
            "id", "local_agent_id", "remote_agent_id", "remote_agent_name",
            "host", "port", "status", "connected_at", "last_activity",
            "messages_sent", "messages_received", "metadata",
        }
        assert conn["status"] == "connecting"  # REST connect 无握手
        assert conn["messages_sent"] == 0
        assert conn["remote_agent_name"] == "Remote Agent"


def test_local_sources_and_message_cap():
    """本机动态来源标记与消息槽容量常量（对齐 CX-O 语义）。"""
    assert "cxa_main" in LOCAL_AGENT_SOURCES
    assert MAX_MESSAGES_PER_KEY == 500
