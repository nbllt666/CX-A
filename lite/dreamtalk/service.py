"""DreamTalk 应用门面：入睡、共演、插话、醒来与清醒侧读取。

本模块只依赖端口协议与 ``core/`` 纯领域层，不直接依赖 FastAPI / SQLite /
ModelRouter：LLM、存储、记忆源、随机源、时钟与事件出口全部由构造注入。
同步存储调用统一放入线程池（``asyncio.to_thread``），事件推送异常隔离，
失败只告警、不影响梦境推进。

事件口径（载荷只含会话状态与回合，不含梦外上下文）：

- ``dream.session_started``：会话创建后推送一次
- ``dream.turn_added``：每个新增回合推送一次（加载旧回合数做差）
- ``dream.session_completed``：会话结束（含醒来、无梦、达到最大轮数）推送一次
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
from datetime import datetime
from typing import Any, Dict, List, Optional

from .adapters.events import NullEventSink
from .core.dialogue import DialogueConfig, DreamDialogue
from .core.errors import DreamTalkError, InvalidMode, StoreError
from .core.models import (
    CONTRACT_VERSION,
    MODES,
    STATUS_ENDED,
    DreamRecall,
    DreamSession,
    MemoryMaterial,
    SessionView,
    Turn,
)
from .core.modes import ModeWeights, select_mode
from .ports import (
    Clock,
    EventSink,
    LLMClient,
    MemorySource,
    RandomSource,
    SessionStore,
)

logger = logging.getLogger(__name__)

_AUTO_MODE = "auto"


class _SystemClock:
    """默认时钟：使用本机当前时间。"""

    def now(self) -> datetime:
        """返回当前时间。"""
        return datetime.now()


class DreamTalkService:
    """DreamTalk 门面：把端口调用编排为一场可推进、可醒来的梦。

    Args:
        dreamer: 梦者角色 LLM 客户端
        director: 潜意识导演角色 LLM 客户端
        store: 会话与回合存储（同步短操作，由本类放入线程池）
        memory_source: 记忆素材源；缺省时以空素材入睡
        events: 事件出口；缺省为 ``NullEventSink()``
        rng: 随机源（模式判定用）；缺省为 ``random.Random()``
        clock: 时钟；缺省为系统时钟
        dialogue_config: 编排参数；缺省使用 ``DialogueConfig()``
        mode_weights: 自动模式基础权重；缺省使用 ``ModeWeights()``
        enabled: 服务开关，仅影响 ``get_status()`` 的状态口径
    """

    def __init__(
        self,
        *,
        dreamer: LLMClient,
        director: LLMClient,
        store: SessionStore,
        memory_source: Optional[MemorySource] = None,
        events: Optional[EventSink] = None,
        rng: Optional[RandomSource] = None,
        clock: Optional[Clock] = None,
        dialogue_config: Optional[DialogueConfig] = None,
        mode_weights: Optional[ModeWeights] = None,
        enabled: bool = True,
    ):
        self._dreamer = dreamer
        self._director = director
        self._store = store
        self._memory_source = memory_source
        self._events = events if events is not None else NullEventSink()
        self._rng = rng if rng is not None else random.Random()
        self._clock = clock if clock is not None else _SystemClock()
        self._mode_weights = mode_weights if mode_weights is not None else ModeWeights()
        self._enabled = bool(enabled)
        self._sessions_started = 0
        self._locks: Dict[str, asyncio.Lock] = {}
        # 事件由门面统一推送（会话/回合/结束各一次），编排器不再重复推送
        self._dialogue = DreamDialogue(
            dreamer=dreamer,
            director=director,
            store=store,
            rng=self._rng,
            clock=self._clock,
            config=dialogue_config,
        )

    # ================================================================ 入睡
    async def start_session(
        self,
        agent_id: str = "default",
        mode: str = "auto",
    ) -> SessionView:
        """创建一场梦境会话。

        记忆素材由注入的 ``MemorySource`` 采集一次（无 source 时为空素材），
        采集失败降级为空素材；显式 ``memory`` 且无当天记忆时抛
        ``NoMemoryMaterial``；显式非法模式抛 ``InvalidMode``。

        Args:
            agent_id: agent 标识，空值按 ``default`` 处理
            mode: auto / memory / wild / dreamless，默认 auto

        Returns:
            SessionView: 会话 + 本场回合（已结束时含醒来摘要）

        Raises:
            InvalidMode: 模式不在允许枚举内（422）
            NoMemoryMaterial: 显式记忆梦但采集素材无当天记忆（422）
            StoreError: 会话存储不可用（500）
        """
        agent = _clean_text(agent_id) or "default"
        requested = _clean_text(mode) or _AUTO_MODE
        forced: Optional[str] = None if requested == _AUTO_MODE else requested
        if forced is not None and forced not in MODES:
            raise InvalidMode(f"未知梦境模式: {requested}")

        material = await self._collect_material(agent)
        last_mode = await self._store_call(self._store.last_mode, agent)
        chosen, reason = select_mode(
            material.day_memories,
            material.emotion,
            self._mode_weights,
            self._rng,
            forced=forced,
            last_mode=last_mode,
        )
        view = await self._dialogue.start(material, chosen, reason, agent_id=agent)
        self._sessions_started += 1
        await self._emit(
            "dream.session_started",
            {
                "session_id": view.session.id,
                "agent_id": view.session.agent_id,
                "mode": view.session.mode,
                "status": view.session.status,
            },
        )
        await self._emit_turns(view.session.id, view.turns)
        if view.session.status == STATUS_ENDED:
            await self._emit_completed(view.session)
        return view

    # ================================================================ 推进
    async def interject(self, session_id: str, content: str) -> SessionView:
        """醒者插话并推进一轮；内容为空等价于继续做梦。

        Raises:
            SessionNotFound: 会话不存在（404）
            SessionEnded: 会话已结束（409）
            StoreError: 会话存储不可用（500）
        """
        if not _clean_text(content):
            return await self._progress(session_id, advance=True)
        return await self._progress(session_id, content=content)

    async def advance(self, session_id: str) -> SessionView:
        """不插话，直接推进一轮。

        Raises:
            SessionNotFound: 会话不存在（404）
            SessionEnded: 会话已结束（409）
            StoreError: 会话存储不可用（500）
        """
        return await self._progress(session_id, advance=True)

    async def wake(self, session_id: str) -> SessionView:
        """主动醒来：结束会话并生成醒来摘要。

        Raises:
            SessionNotFound: 会话不存在（404）
            SessionEnded: 会话已结束（重复醒来，409）
            StoreError: 会话存储不可用（500）
        """
        lock = self._lock_for(session_id)
        async with lock:
            view = await self._dialogue.wake(session_id)
            await self._emit_completed(view.session)
            return view

    # ================================================================ 读取
    async def get_session(self, session_id: str) -> SessionView:
        """读取梦境档案（会话 + 全部回合）。

        Raises:
            SessionNotFound: 会话不存在（404）
        """
        return await self._dialogue.get_session(session_id)

    async def list_sessions(
        self,
        agent_id: str = "default",
        limit: int = 20,
    ) -> List[DreamSession]:
        """按 agent 过滤，按创建时间倒序列出最近会话（不含回合）。"""
        agent = _clean_text(agent_id) or "default"
        return await self._store_call(
            self._store.list_sessions, agent, max(int(limit), 0)
        )

    async def get_recall(self, session_id: str) -> DreamRecall:
        """读取清醒侧摘要（不含回合）。

        Raises:
            SessionNotFound: 会话不存在（404）
            SessionEnded: 会话尚未结束，暂无可读摘要（409）
        """
        return await self._dialogue.get_recall(session_id)

    async def list_recalls(
        self,
        agent_id: str = "default",
        limit: int = 20,
    ) -> List[DreamRecall]:
        """按 agent 过滤，列出已结束会话的摘要（不含回合）。"""
        count = max(int(limit), 0)
        sessions = await self.list_sessions(agent_id, count)
        recalls = [
            DreamRecall.from_session(session)
            for session in sessions
            if session.status == STATUS_ENDED
        ]
        return recalls[:count]

    # ================================================================ 状态
    def get_status(self) -> Dict[str, Any]:
        """返回服务状态（status / enabled / contract_version / sessions）。"""
        return {
            "status": "idle" if self._enabled else "disabled",
            "enabled": self._enabled,
            "contract_version": CONTRACT_VERSION,
            "sessions": self._sessions_started,
        }

    # ================================================================ 内部实现
    async def _progress(
        self,
        session_id: str,
        *,
        content: Optional[str] = None,
        advance: bool = False,
    ) -> SessionView:
        """串行化推进同一会话，并按「旧回合数做差」推送新增回合事件。"""
        lock = self._lock_for(session_id)
        async with lock:
            previous = await self._store_call(self._store.get_turns, session_id)
            if advance:
                view = await self._dialogue.advance(session_id)
            else:
                view = await self._dialogue.interject(session_id, content or "")
            await self._emit_turns(session_id, view.turns[len(previous) :])
            if view.session.status == STATUS_ENDED:
                await self._emit_completed(view.session)
            return view

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        """取会话级推进锁，保证同一会话的插话 / 醒来不交叉。"""
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    async def _collect_material(self, agent_id: str) -> MemoryMaterial:
        """入睡时采集一次素材快照；无源或失败时返回空素材。"""
        if self._memory_source is None:
            return MemoryMaterial(source="cxa_memory")
        try:
            material = self._memory_source.collect(agent_id)
            if inspect.isawaitable(material):
                material = await material
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 采集失败降级为空素材
            logger.warning("梦境记忆素材采集失败，按空素材入睡: %s", exc)
            return MemoryMaterial(source="cxa_memory")
        if not isinstance(material, MemoryMaterial):
            logger.warning("记忆源返回了非 MemoryMaterial 快照，按空素材入睡")
            return MemoryMaterial(source="cxa_memory")
        return material

    async def _store_call(self, func, *args, **kwargs):
        """把同步短存储操作放入线程池，并把未知异常适配为 ``StoreError``。"""
        try:
            return await asyncio.to_thread(func, *args, **kwargs)
        except asyncio.CancelledError:
            raise
        except DreamTalkError:
            raise
        except Exception as exc:  # noqa: BLE001 - 存储故障统一为领域 500 语义
            raise StoreError(f"梦境存储不可用: {exc}") from exc

    # ---------------------------------------------------------------- 事件
    async def _emit_turns(self, session_id: str, turns: List[Turn]) -> None:
        """逐个推送新增回合事件。"""
        for turn in turns:
            await self._emit(
                "dream.turn_added",
                {"session_id": session_id, "turn": turn.to_dict()},
            )

    async def _emit_completed(self, session: DreamSession) -> None:
        """推送会话结束事件。"""
        await self._emit(
            "dream.session_completed",
            {
                "session_id": session.id,
                "mode": session.mode,
                "title": session.title,
                "summary": session.summary,
                "status": session.status,
            },
        )

    async def _emit(self, event_type: str, data: Dict[str, Any]) -> None:
        """推送事件；事件失败不得影响梦境推进。"""
        try:
            await self._events.emit(event_type, data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 事件失败不得影响梦境
            logger.warning("梦境事件推送失败: %s (%s)", event_type, exc)


def _clean_text(value: Any) -> str:
    """去除首尾空白；非字符串或 None 返回空串。"""
    if value is None:
        return ""
    return str(value).strip()
