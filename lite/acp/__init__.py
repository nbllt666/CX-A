# -*- coding: utf-8 -*-
"""CX-A 轻量版 ACP 包入口（Task G1）。

依据《CX-A 补充文档 · ACP 与 CXFC》对重度版 CX-O ACP 做轻量化改造：
保留 Agent 注册 / 消息路由 / 心跳核心；局域网发现与分组协作降为可选（默认关）；
新增云端中转（跨公网通信）。消息结构 ``{action, request_id, data}`` 与 CX-O §3.3 对齐。

对外导出 LiteACP / LiteLanDiscovery / CloudRelay 及异常 AcpDisabled / AcpAgentNotFound。
"""

from .lite_acp import AcpAgentNotFound, AcpDisabled, LiteACP
from .discovery import LiteLanDiscovery
from .cloud_relay import CloudRelay, CloudRelayError

__all__ = [
    "LiteACP",
    "LiteLanDiscovery",
    "CloudRelay",
    "CloudRelayError",
    "AcpDisabled",
    "AcpAgentNotFound",
]