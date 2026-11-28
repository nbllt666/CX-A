"""DreamTalk 适配器层：把基础设施依赖包装成核心层端口。

四个适配器模块：

- ``llm``：``MockLLMClient``（确定性离线）与 ``CXAChatLLMClient``（CX-A LLM 通道）
- ``memory``：``CXAMemorySource``（只读 CX-A 记忆采集）与 ``ManualMemorySource``（测试注入）
- ``store``：``SQLiteSessionStore``（短连接 + WAL）与 ``MemorySessionStore``（测试替身）
- ``events``：``NullEventSink``（默认空实现）
"""

from .events import NullEventSink
from .llm import CXAChatLLMClient, MockLLMClient
from .memory import CXAMemorySource, ManualMemorySource
from .store import MemorySessionStore, SQLiteSessionStore

__all__ = [
    "CXAChatLLMClient",
    "CXAMemorySource",
    "ManualMemorySource",
    "MemorySessionStore",
    "MockLLMClient",
    "NullEventSink",
    "SQLiteSessionStore",
]
