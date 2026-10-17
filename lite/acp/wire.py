# -*- coding: utf-8 -*-
"""ACP wire 契约层（20261017 CX-O 全量迁移批次 1）。

CX-O 与 CX-A 节点互通的**唯一真相源**：消息/节点/连接的 dict 形态、字段默认值
与 wire 映射全部在本模块冻结——任何偏差即断互通（recon_acp.md §9.4）。

对齐契约（源：c:\\CX-O\\public\\schema\\acp.schema.json + core/acp/manager.py）：

- 字段 required 全空、均有默认值（schema 口径）；
- **wire 映射**：Pydantic 字段 ``msg_type`` 序列化为 wire 键 ``type``；接收侧
  兼容 ``type`` / ``msg_type`` 双键（CX-O model_validator 语义）；
- 消息 ``content`` 约定 ``{"text": "..."}``（前端 extractMessageText 兼容
  string/object，本层不强转，保守保留原值）；
- ``datetime`` 一律 ISO8601 字符串（CX-A 无 pydantic，本层手工保真）；
- ``port`` 双轨语义：CX-O 主系统 BEACON/注册 port=9999（含义"不可直接投递"，
  投递改走其 ``/api/acp/receive``）；独立节点 port=HTTP 消息端口。

本模块只使用标准库。
"""

import uuid
from datetime import datetime

#: 消息槽单键容量上限（对齐 CX-O MAX_MESSAGES_PER_KEY=500，超限截断最旧）
MAX_MESSAGES_PER_KEY = 500

#: 本机动态来源标记（对齐 CX-O LOCAL_AGENT_SOURCES：这些 source 的 agent 不落盘）
LOCAL_AGENT_SOURCES = ("cxa_main",)

#: 消息默认类型
DEFAULT_MSG_TYPE = "chat"


def new_id():
    """生成消息/连接/群组唯一标识（uuid4 hex，对齐 CX-O uuid4().hex）。"""
    return uuid.uuid4().hex


def now_iso():
    """当前时刻 ISO8601 字符串（对齐 CX-O datetime.now().isoformat()）。"""
    return datetime.now().isoformat()


def make_message(msg_type=DEFAULT_MSG_TYPE, from_agent_id="", from_agent_name="",
                 to_agent_id=None, to_group_id=None, content=None, metadata=None,
                 is_sent=False):
    """构造一条 ACPMessageInfo 的 **wire 形态** dict（键为 ``type``）。

    :param msg_type: 消息类型（chat / group_message / control / notification）
    :param is_sent: 本机发出为 True；接收/代发记录为 False
    :return: wire dict（11 键齐全，可直接 POST /acp/message 或入槽）
    """
    return {
        "id": new_id(),
        "type": str(msg_type or DEFAULT_MSG_TYPE),
        "from_agent_id": str(from_agent_id or ""),
        "from_agent_name": str(from_agent_name or ""),
        "to_agent_id": to_agent_id if to_agent_id else None,
        "to_group_id": to_group_id if to_group_id else None,
        "content": content if content is not None else {},
        "timestamp": now_iso(),
        "is_read": False,
        "is_sent": bool(is_sent),
        "metadata": dict(metadata or {}),
    }


def message_from_wire(payload):
    """归一化一条外部投递消息（接收侧，兼容 ``type`` / ``msg_type`` 双键）。

    对齐 CX-O ``ACPMessageInfo`` 的 model_validator 语义：缺键补默认值；
    ``metadata`` 强制 dict；``content`` 保守保留原值（dict/str 均合法，前端
    extractMessageText 兼容两种形态）。非法输入返回 None（调用方按 400 处理）。

    :param payload: 外部 POST 来的 wire dict
    :return: 归一化 wire dict（键恒为 ``type``）；非法返回 None
    """
    if not isinstance(payload, dict):
        return None
    msg_type = payload.get("type") or payload.get("msg_type") or DEFAULT_MSG_TYPE
    metadata = payload.get("metadata")
    return {
        "id": str(payload.get("id") or new_id()),
        "type": str(msg_type),
        "from_agent_id": str(payload.get("from_agent_id") or ""),
        "from_agent_name": str(payload.get("from_agent_name") or ""),
        "to_agent_id": payload.get("to_agent_id") or None,
        "to_group_id": payload.get("to_group_id") or None,
        "content": payload.get("content") if payload.get("content") is not None else {},
        "timestamp": str(payload.get("timestamp") or now_iso()),
        "is_read": bool(payload.get("is_read", False)),
        "is_sent": bool(payload.get("is_sent", False)),
        "metadata": dict(metadata) if isinstance(metadata, dict) else {},
    }


def make_agent_info(agent_id, name="", host="", port=0, status="offline",
                    version="1.0.0", capabilities=None, last_seen="", metadata=None):
    """构造一条 ACPAgentInfo 的 dict（字段与默认值对齐 acp.schema.json）。"""
    meta = dict(metadata or {})
    if "source" not in meta:
        meta["source"] = ""
    return {
        "id": str(agent_id or ""),
        "name": str(name or ""),
        "host": str(host or ""),
        "port": int(port or 0),
        "status": str(status or "offline"),
        "version": str(version or "1.0.0"),
        "capabilities": list(capabilities or []),
        "last_seen": str(last_seen or ""),
        "metadata": meta,
    }


def _safe_port(raw):
    """端口容错归一化（对齐 discovery._normalize_port 口径）：非法置 0。"""
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    if raw < 1 or raw > 65535:
        return 0
    return raw


def agent_info_from_beacon(beacon, host):
    """把一条 UDP BEACON 载荷归一化为 AgentInfo（host 取 UDP 源地址）。

    对齐 CX-O ``_scan_network`` 语义：host 来自 recvfrom 的 addr[0]（非报文
    字段）；port 取报文载荷（双轨语义见模块 docstring）；非法 port 置 0。

    :param beacon: 解析后的 BEACON dict（含 agent_id/agent_name/version/capabilities/port）
    :param host: UDP 源 IP
    :return: AgentInfo dict（status=online、last_seen=now、metadata.source="lan"）
    """
    return make_agent_info(
        beacon.get("agent_id", ""),
        name=str(beacon.get("agent_name") or ""),
        host=str(host or ""),
        port=_safe_port(beacon.get("port", 0)),
        status="online",
        version=str(beacon.get("version") or "1.0.0"),
        capabilities=list(beacon.get("capabilities") or []),
        last_seen=now_iso(),
        metadata={"source": "lan"},
    )


def make_connection_info(local_agent_id, remote_agent_id, host="", port=0,
                         remote_agent_name="Remote Agent"):
    """构造一条 ACPConnectionInfo（对齐 CX-O connect 语义：通讯录条目）。

    CX-O 的"连接"不是网络连接：REST connect 仅创建记录（status="connecting"），
    无握手无探活；WS 通道的 CONNECT 才置 connected。CX-A 无 WS 层，REST 建条目。
    """
    return {
        "id": new_id(),
        "local_agent_id": str(local_agent_id or ""),
        "remote_agent_id": str(remote_agent_id or ""),
        "remote_agent_name": str(remote_agent_name or "Remote Agent"),
        "host": str(host or ""),
        "port": int(port or 0),
        "status": "connecting",
        "connected_at": now_iso(),
        "last_activity": None,
        "messages_sent": 0,
        "messages_received": 0,
        "metadata": {},
    }
