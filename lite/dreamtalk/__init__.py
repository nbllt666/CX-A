"""DreamTalk 双 LLM 梦境包。

导出纯领域核心层（``core/``）、端口协议（``ports.py``）与应用门面
（``service.py``）的 ``DreamTalkService``；适配器按需从 ``adapters/`` 导入。
"""

from .core.errors import (
    DreamTalkError,
    InvalidMode,
    NoMemoryMaterial,
    SessionEnded,
    SessionNotFound,
    StoreError,
)
from .core.models import (
    CONTRACT_VERSION,
    MODE_DREAMLESS,
    MODE_MEMORY,
    MODE_WILD,
    MODES,
    STATUS_ACTIVE,
    STATUS_ENDED,
    DreamContext,
    DreamRecall,
    DreamSession,
    Emotion,
    MemoryMaterial,
    Message,
    SessionView,
    Turn,
)
from .ports import (
    Clock,
    EventSink,
    LLMClient,
    MemorySource,
    RandomSource,
    SessionStore,
)
from .service import DreamTalkService

__all__ = [
    "CONTRACT_VERSION",
    "DreamTalkError",
    "DreamTalkService",
    "InvalidMode",
    "NoMemoryMaterial",
    "SessionEnded",
    "SessionNotFound",
    "StoreError",
    "MODE_DREAMLESS",
    "MODE_MEMORY",
    "MODE_WILD",
    "MODES",
    "STATUS_ACTIVE",
    "STATUS_ENDED",
    "DreamContext",
    "DreamRecall",
    "DreamSession",
    "Emotion",
    "MemoryMaterial",
    "Message",
    "SessionView",
    "Turn",
    "Clock",
    "EventSink",
    "LLMClient",
    "MemorySource",
    "RandomSource",
    "SessionStore",
]
