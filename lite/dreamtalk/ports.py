"""DreamTalk 端口协议：核心层只依赖这些协议，便于替换真实适配器。

本模块仅依赖 ``core.models``（仅类型检查期导入），不得引入 sqlite / fastapi /
httpx / os 等基础设施依赖。所有存储与随机源 / 时钟方法均为同步短操作，
由服务层放入线程池执行；仅 LLM 调用、记忆采集与事件推送为异步。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Protocol, Sequence, runtime_checkable

if TYPE_CHECKING:
    from .core.models import DreamSession, MemoryMaterial, Message, Turn


@runtime_checkable
class LLMClient(Protocol):
    """异步 LLM 客户端：输入消息序列，返回纯文本。"""

    async def chat(self, messages: Sequence["Message"], *, temperature: float) -> str:
        """调用 LLM 生成一次回复；失败由适配器降级为确定性离线文本。"""
        ...


@runtime_checkable
class MemorySource(Protocol):
    """记忆源：入睡时解析一次素材快照；梦境回合期间不再访问。"""

    async def collect(self, agent_id: str = "default") -> "MemoryMaterial":
        """采集一次记忆素材快照。"""
        ...


@runtime_checkable
class SessionStore(Protocol):
    """会话与回合存储；方法为同步短操作，由服务层放入线程池执行。"""

    def create_session(self, session: "DreamSession") -> None:
        """持久化新建的梦境会话。"""
        ...

    def get_session(self, session_id: str) -> Optional["DreamSession"]:
        """按会话 ID 读取；不存在返回 None。"""
        ...

    def list_sessions(self, agent_id: str = "default", limit: int = 20) -> List["DreamSession"]:
        """按 agent 过滤，按创建时间倒序列出最近会话。"""
        ...

    def append_turn(
        self,
        session_id: str,
        speaker: str,
        content: str,
        flagged: bool = False,
        created_at: str = "",
    ) -> "Turn":
        """追加一个回合；seq 在同一会话内单调递增。"""
        ...

    def get_turns(self, session_id: str) -> List["Turn"]:
        """按 seq 升序读取某会话的全部回合。"""
        ...

    def end_session(
        self,
        session_id: str,
        *,
        title: str,
        summary: str,
        emotion_residue: Dict[str, float],
        ended_at: str,
    ) -> None:
        """结束会话并写入醒来摘要。"""
        ...

    def update_summary(
        self,
        session_id: str,
        *,
        title: str,
        summary: str,
        emotion_residue: Dict[str, float],
    ) -> None:
        """仅更新摘要字段，不改会话状态。"""
        ...

    def last_mode(self, agent_id: str = "default") -> Optional[str]:
        """返回该 agent 最近一场会话的模式；用于自动模式的衰减偏置。"""
        ...


@runtime_checkable
class RandomSource(Protocol):
    """随机源：模式判定可注入固定值以便测试。"""

    def random(self) -> float:
        """返回 [0, 1) 的随机数。"""
        ...


@runtime_checkable
class Clock(Protocol):
    """时钟：可注入固定时间以便测试。"""

    def now(self) -> datetime:
        """返回当前时间。"""
        ...


@runtime_checkable
class EventSink(Protocol):
    """事件出口：默认空实现；CX-O 装配后接 WebSocket 广播。"""

    async def emit(self, event_type: str, data: Dict[str, Any]) -> None:
        """推送一个梦境事件（只含会话状态与回合，不含梦外上下文）。"""
        ...
