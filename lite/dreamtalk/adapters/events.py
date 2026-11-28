"""DreamTalk 事件出口适配器：默认空实现。

事件载荷只含会话状态与回合，不含梦外上下文；推送失败仅告警，不影响梦境推进。
CX-A 无 WebSocket 广播面，故不迁移 CX-O 的 ``WSEventSink``。
"""

from __future__ import annotations

from typing import Any, Dict


class NullEventSink:
    """默认事件出口：静默丢弃事件。"""

    async def emit(self, event_type: str, data: Dict[str, Any]) -> None:
        """空实现，不推送任何事件。"""
        return None
