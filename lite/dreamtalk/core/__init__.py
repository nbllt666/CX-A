"""DreamTalk 纯领域核心层：模型、异常、模式决策、内容闸门、提示词与对话编排。

本包不依赖 FastAPI / SQLite / httpx / 环境变量，由
``tests/test_dreamtalk_core_purity.py`` 守护；所有模块间使用包内相对导入。
"""

from .errors import (
    DreamTalkError,
    InvalidMode,
    NoMemoryMaterial,
    SessionEnded,
    SessionNotFound,
    StoreError,
)
from .models import (
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
    Mode,
    SessionView,
    Speaker,
    Status,
    Turn,
)

__all__ = [
    "CONTRACT_VERSION",
    "DreamTalkError",
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
    "Mode",
    "SessionView",
    "Speaker",
    "Status",
    "Turn",
]
