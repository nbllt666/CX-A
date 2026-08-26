# -*- coding: utf-8 -*-
"""管理能力子包（本地多 Agent 管理 + 远端 CX-O 遥控），Task E2/E3 实现。

E2（本文件先落）：本地多 Agent 人设管理——AgentManager / Agent / AgentNotFound。
E3（RemotePage 接入）：远端 CX-O 遥控——RemoteController 及 RemoteDisabled /
RemoteUnreachable / RemoteError 异常族。
"""

from lite.management.local_agents import Agent, AgentManager, AgentNotFound
from lite.management.remote import (
    HTTPRemoteTransport,
    RemoteController,
    RemoteDisabled,
    RemoteError,
    RemoteTransport,
    RemoteUnreachable,
)

__all__ = [
    "Agent",
    "AgentManager",
    "AgentNotFound",
    "RemoteController",
    "RemoteDisabled",
    "RemoteUnreachable",
    "RemoteError",
    "RemoteTransport",
    "HTTPRemoteTransport",
]