"""双 LLM 梦境编排：梦者 × 潜意识导演交替共演，含上下文隔离与醒来摘要。

梦内上下文只由 ``MemoryMaterial`` 快照与本会话回合构成；``build_messages()``
只接受 ``DreamContext``，没有任何梦外历史入口。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .errors import SessionEnded, SessionNotFound
from .filter import DREAM_BROKEN_TEXT, REWRITE_HINT, check_text
from .models import (
    MODE_DREAMLESS,
    MODE_WILD,
    SPEAKER_AWAKENED,
    SPEAKER_DREAMER,
    SPEAKER_DIRECTOR,
    SPEAKER_LABELS,
    STATUS_ACTIVE,
    STATUS_ENDED,
    DreamContext,
    DreamRecall,
    DreamSession,
    MemoryMaterial,
    Message,
    SessionView,
    Turn,
)
from .personas import material_block, system_prompt, turn_instruction

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DialogueConfig:
    """对话编排参数（由适配器配置映射，核心不读环境变量）。"""

    max_rounds: int = 8
    context_turns: int = 16
    director_temperature: float = 0.85
    dreamer_temperature: float = 0.9
    wild_director_temperature: float = 1.0
    wild_dreamer_temperature: float = 1.1
    summary_temperature: float = 0.4
    summary_title_max_chars: int = 12
    summary_max_chars: int = 120
    transcript_max_chars: int = 4000


class _SystemClock:
    """默认时钟：使用本机当前时间。"""

    def now(self) -> datetime:
        """返回当前时间。"""
        return datetime.now()


class _NullEventSink:
    """默认事件出口：静默丢弃事件。"""

    async def emit(self, event_type: str, data: Dict[str, Any]) -> None:
        """空实现，不推送任何事件。"""
        return None


_DREAMLESS_SUMMARY = "没有画面，也没有声音。{reason}。你在黑暗里安静地沉下去。"

_DIRECTOR_FALLBACK = (
    "雾把四周的轮廓擦掉了。你站在一条没有尽头的走廊里，"
    "尽头的灯一盏一盏亮起，又在你回头时熄灭。"
)
_DREAMER_FALLBACK = (
    "我向前走，脚下的地板像水面一样泛起涟漪。"
    "走廊尽头传来纸张翻动的声音，我伸手推开了那扇没有把手的门。"
)

_SUMMARY_SYSTEM = (
    "你是梦醒后的记录者。根据一场梦的完整片段，写一份只保留余韵的摘要。"
    "不要复述原文，不要分析心理，不要给建议。"
    "只输出一个 JSON 对象："
    '{"title": "不超过12字的标题", "summary": "不超过120字的余韵", '
    '"emotion_residue": {"valence": 数值, "arousal": 数值}}。'
    "不要输出任何多余文字。"
)


class DreamDialogue:
    """梦境对话编排器。

    只通过构造注入的端口工作；梦内上下文仅由 ``MemoryMaterial`` 与
    本会话回合构成，不读取任何梦外数据。
    """

    def __init__(
        self,
        *,
        dreamer,
        director,
        store,
        rng=None,
        clock=None,
        events=None,
        config: Optional[DialogueConfig] = None,
    ):
        """注入两个 LLM 客户端、会话存储与可选随机源 / 时钟 / 事件出口。

        Args:
            dreamer: 梦者角色 LLM 客户端
            director: 潜意识导演角色 LLM 客户端
            store: 会话与回合存储（同步短操作）
            rng: 随机源注入位；编排本身不消耗随机性，模式判定见 ``modes.select_mode``
            clock: 时钟，缺省使用本机时间
            events: 事件出口，缺省静默丢弃
            config: 编排参数，缺省使用 ``DialogueConfig()``
        """
        self._dreamer = dreamer
        self._director = director
        self._store = store
        self._rng = rng
        self._clock = clock or _SystemClock()
        self._events = events or _NullEventSink()
        self._config = config or DialogueConfig()
        self._locks: Dict[str, asyncio.Lock] = {}

    # ================================================================ 对外入口
    async def start(
        self,
        material: MemoryMaterial,
        mode: str,
        mode_reason: str,
        agent_id: str = "default",
    ) -> SessionView:
        """创建一场会话；无梦模式不调用 LLM，直接结束。"""
        now = self._clock.now().isoformat()
        session = DreamSession(
            id=uuid.uuid4().hex,
            agent_id=agent_id or "default",
            created_at=now,
            mode=mode,
            mode_reason=mode_reason,
            status=STATUS_ACTIVE,
            material=material,
        )
        await self._store_call(self._store.create_session, session)
        await self._emit(
            "dream.session_started",
            {
                "session_id": session.id,
                "agent_id": session.agent_id,
                "mode": mode,
                "status": STATUS_ACTIVE,
            },
        )

        if mode == MODE_DREAMLESS:
            turns: List[Turn] = []
            session.summary = _truncate(
                _DREAMLESS_SUMMARY.format(reason=mode_reason),
                self._config.summary_max_chars,
            )
            session.title = "一夜无梦"
            session.emotion_residue = material.emotion.clamped().to_dict()
            session.status = STATUS_ENDED
            session.ended_at = self._clock.now().isoformat()
            await self._store_call(
                self._store.end_session,
                session.id,
                title=session.title,
                summary=session.summary,
                emotion_residue=session.emotion_residue,
                ended_at=session.ended_at,
            )
            await self._emit_completed(session)
            return SessionView(
                session=session,
                turns=turns,
                recall=DreamRecall.from_session(session),
            )

        turns = await self._run_round(session, [])
        if self._rounds_played(turns) >= self._config.max_rounds:
            await self._end_with_recall(session, turns)
        session.turn_count = len(turns)
        return SessionView(
            session=session,
            turns=turns,
            recall=DreamRecall.from_session(session) if session.status == STATUS_ENDED else None,
        )

    async def interject(self, session_id: str, content: str) -> SessionView:
        """醒者插话（内容为空等价于继续做梦）。"""
        lock = self._lock_for(session_id)
        async with lock:
            session = await self._load_active(session_id)
            turns = await self._store_call(self._store.get_turns, session_id)
            text = (content or "").strip()
            if text:
                turn = await self._append_turn(
                    session, SPEAKER_AWAKENED, text, flagged=False
                )
                turns.append(turn)
            await self._run_round(session, turns)
            recall = None
            if self._rounds_played(turns) >= self._config.max_rounds:
                recall = await self._end_with_recall(session, turns)
            session.turn_count = len(turns)
            return SessionView(session=session, turns=turns, recall=recall)

    async def advance(self, session_id: str) -> SessionView:
        """不插话，直接推进一轮。"""
        return await self.interject(session_id, "")

    async def wake(self, session_id: str) -> SessionView:
        """主动醒来：结束会话并生成摘要（结束时重复调用抛 409）。"""
        lock = self._lock_for(session_id)
        async with lock:
            session = await self._load_active(session_id)
            turns = await self._store_call(self._store.get_turns, session_id)
            recall = await self._end_with_recall(session, turns)
            session.turn_count = len(turns)
            return SessionView(session=session, turns=turns, recall=recall)

    async def get_session(self, session_id: str) -> SessionView:
        """读取会话与全部回合（梦境档案）。"""
        session = await self._store_call(self._store.get_session, session_id)
        if session is None:
            raise SessionNotFound(f"梦境会话不存在: {session_id}")
        turns = await self._store_call(self._store.get_turns, session_id)
        session.turn_count = len(turns)
        recall = DreamRecall.from_session(session) if session.status == STATUS_ENDED else None
        return SessionView(session=session, turns=turns, recall=recall)

    async def get_recall(self, session_id: str) -> DreamRecall:
        """读取清醒侧摘要；未结束的会话不允许导出。"""
        session = await self._store_call(self._store.get_session, session_id)
        if session is None:
            raise SessionNotFound(f"梦境会话不存在: {session_id}")
        if session.status != STATUS_ENDED:
            raise SessionEnded("梦境尚未结束，暂时没有醒来摘要")
        return DreamRecall.from_session(session)

    # ================================================================ 回合生成
    async def _run_round(
        self, session: DreamSession, turns: List[Turn]
    ) -> List[Turn]:
        """生成一轮：导演 -> 梦者。返回新回合列表。"""
        new_turns: List[Turn] = []
        for speaker in (SPEAKER_DIRECTOR, SPEAKER_DREAMER):
            context = DreamContext(
                material=session.effective_material,
                turns=list(turns),
                mode=session.mode,
                session_id=session.id,
            )
            content, flagged = await self._generate_turn(speaker, context)
            turn = await self._append_turn(session, speaker, content, flagged)
            turns.append(turn)
            new_turns.append(turn)
        return new_turns

    async def _generate_turn(
        self, speaker: str, context: DreamContext
    ) -> Tuple[str, bool]:
        """生成单个回合；空文本重试一次，事实断言越界纠正一次。"""
        client = self._dreamer if speaker == SPEAKER_DREAMER else self._director
        temperature = self._temperature(speaker, context.mode)
        messages = self.build_messages(context, speaker)

        text = await self._safe_chat(client, messages, temperature)
        if not text.strip():
            text = await self._safe_chat(client, messages, temperature)
        if not text.strip():
            return DREAM_BROKEN_TEXT, False

        if not check_text(text):
            return text.strip(), False

        correction = list(messages) + [
            Message(role="assistant", content=text.strip()),
            Message(role="user", content=REWRITE_HINT),
        ]
        rewritten = await self._safe_chat(client, correction, temperature)
        if rewritten.strip():
            text = rewritten.strip()
        return text, bool(check_text(text))

    def build_messages(self, context: DreamContext, role: str) -> List[Message]:
        """构造发送给 LLM 的 messages：只含素材快照、梦内回合与本轮指令。"""
        system = system_prompt(role, context.mode) + "\n\n" + material_block(
            context.material
        )
        messages: List[Message] = [Message(role="system", content=system)]
        recent = context.turns[-max(int(self._config.context_turns), 0) :]
        for turn in recent:
            if turn.speaker == role:
                messages.append(Message(role="assistant", content=turn.content))
            else:
                label = SPEAKER_LABELS.get(turn.speaker, turn.speaker)
                messages.append(
                    Message(role="user", content=f"[{label}] {turn.content}")
                )
        last_speaker = recent[-1].speaker if recent else ""
        messages.append(
            Message(
                role="user",
                content=turn_instruction(role, bool(recent), last_speaker),
            )
        )
        return messages

    async def _safe_chat(self, client, messages: List[Message], temperature: float) -> str:
        """调用 LLM；异常时返回本地兜底文本，绝不向上抛出。"""
        try:
            result = await client.chat(messages, temperature=temperature)
            return str(result or "")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 梦境链路必须自愈
            logger.warning("梦境 LLM 调用失败，使用本地兜底文本: %s", exc)
            system = messages[0].content if messages else ""
            if "【你的身份：潜意识导演】" in system:
                return _DIRECTOR_FALLBACK
            return _DREAMER_FALLBACK

    def _temperature(self, speaker: str, mode: str) -> float:
        """按角色与模式取采样温度。"""
        if mode == MODE_WILD:
            if speaker == SPEAKER_DREAMER:
                return self._config.wild_dreamer_temperature
            return self._config.wild_director_temperature
        if speaker == SPEAKER_DREAMER:
            return self._config.dreamer_temperature
        return self._config.director_temperature

    # ================================================================ 回合持久化
    async def _append_turn(
        self, session: DreamSession, speaker: str, content: str, flagged: bool
    ) -> Turn:
        """追加回合到存储并广播回合事件。"""
        turn = await self._store_call(
            self._store.append_turn,
            session.id,
            speaker,
            content,
            flagged,
            self._clock.now().isoformat(),
        )
        # 契约要求回合 id / session_id 非空；存储未回填时由编排层补齐
        if not turn.id:
            turn.id = f"{session.id}:{turn.seq}"
        if not turn.session_id:
            turn.session_id = session.id
        await self._emit(
            "dream.turn_added",
            {
                "session_id": session.id,
                "turn": {
                    "seq": turn.seq,
                    "speaker": turn.speaker,
                    "content": turn.content,
                    "flagged": turn.flagged,
                    "created_at": turn.created_at,
                },
            },
        )
        return turn

    # ================================================================ 醒来摘要
    async def _end_with_recall(
        self, session: DreamSession, turns: List[Turn]
    ) -> DreamRecall:
        """结束会话：生成标题与余韵，写入存储并发出完成事件。"""
        title, summary, residue = await self.generate_summary(
            DreamContext(
                material=session.effective_material,
                turns=list(turns),
                mode=session.mode,
                session_id=session.id,
            )
        )
        session.title = title
        session.summary = summary
        session.emotion_residue = residue
        session.status = STATUS_ENDED
        session.ended_at = self._clock.now().isoformat()
        await self._store_call(
            self._store.end_session,
            session.id,
            title=title,
            summary=summary,
            emotion_residue=residue,
            ended_at=session.ended_at,
        )
        await self._emit_completed(session)
        return DreamRecall.from_session(session)

    async def generate_summary(
        self, context: DreamContext
    ) -> Tuple[str, str, Dict[str, float]]:
        """为一组梦内上下文生成 ``(标题, 余韵, 情绪残留)``。

        优先由导演客户端生成 JSON 摘要；失败时本地截断降级，绝不抛出。
        """
        material = context.material if isinstance(context.material, MemoryMaterial) else MemoryMaterial()
        transcript = _transcript(context.turns)
        if not transcript:
            return (
                "无名之梦",
                "梦还没有成形，就散了。",
                material.emotion.clamped().to_dict(),
            )
        messages = [
            Message(role="system", content=_SUMMARY_SYSTEM),
            Message(
                role="user",
                content=(
                    f"【今夜素材】\n{material_block(material)}\n\n"
                    f"【梦境全文】\n{transcript[:_truncate_len(self._config.transcript_max_chars)]}"
                ),
            ),
        ]
        raw = await self._safe_chat(self._director, messages, self._config.summary_temperature)
        parsed = _parse_summary(raw)
        if parsed is not None:
            title, summary, residue = parsed
            return (
                _truncate(title, self._config.summary_title_max_chars),
                _truncate(summary, self._config.summary_max_chars),
                residue or material.emotion.clamped().to_dict(),
            )
        return self._local_summary(material, context.turns)

    def _local_summary(
        self, material: MemoryMaterial, turns: Sequence[Turn]
    ) -> Tuple[str, str, Dict[str, float]]:
        """本地降级：取最后一回合的片段，保留素材情绪。"""
        candidates = [t for t in turns if t.speaker == SPEAKER_DREAMER and t.content.strip()]
        if not candidates:
            candidates = [t for t in turns if t.content.strip()]
        text = candidates[-1].content.strip() if candidates else ""
        summary = _truncate(
            text or "梦醒时只剩下一点潮湿的光。", self._config.summary_max_chars
        )
        return "无名之梦", summary, material.emotion.clamped().to_dict()

    # ================================================================ 内部工具
    async def _load_active(self, session_id: str) -> DreamSession:
        """读取仍处于进行中的会话；不存在抛 404，已结束抛 409。"""
        session = await self._store_call(self._store.get_session, session_id)
        if session is None:
            raise SessionNotFound(f"梦境会话不存在: {session_id}")
        if session.status != STATUS_ACTIVE:
            raise SessionEnded("梦境已经结束，不能再继续")
        return session

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        """取会话级推进锁，保证同一会话的推进串行化。"""
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    def _rounds_played(self, turns: Sequence[Turn]) -> int:
        """已完成的轮数（以导演回合数计）。"""
        return sum(1 for turn in turns if turn.speaker == SPEAKER_DIRECTOR)

    async def _store_call(self, func, *args, **kwargs):
        """把同步短存储操作放入线程池，避免阻塞事件循环。"""
        return await asyncio.to_thread(func, *args, **kwargs)

    async def _emit(self, event_type: str, data: Dict[str, Any]) -> None:
        """推送事件；事件失败不得影响梦境。"""
        try:
            await self._events.emit(event_type, data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 事件失败不得影响梦境
            logger.warning("梦境事件推送失败: %s (%s)", event_type, exc)

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


# ------------------------------------------------------------------ 工具函数
def _truncate(text: str, max_chars: int) -> str:
    """按字符数截断，超出时以省略号收尾。"""
    content = (text or "").strip()
    if len(content) <= max_chars:
        return content
    if max_chars <= 1:
        return content[:max_chars]
    return content[: max_chars - 1] + "…"


def _truncate_len(max_chars: int) -> int:
    """把截断长度规整为非负整数。"""
    return max(int(max_chars), 0)


def _transcript(turns: Sequence[Turn]) -> str:
    """把回合序列渲染为带说话人标签的文本。"""
    lines = []
    for turn in turns:
        label = SPEAKER_LABELS.get(turn.speaker, turn.speaker)
        lines.append(f"[{label}] {turn.content.strip()}")
    return "\n".join(lines).strip()


def _parse_summary(text: str) -> Optional[Tuple[str, str, Dict[str, float]]]:
    """从模型输出中提取 JSON 摘要；失败返回 None。"""
    obj = _extract_json_object(text or "")
    if not isinstance(obj, dict):
        return None
    title = str(obj.get("title") or "").strip()
    summary = str(obj.get("summary") or "").strip()
    if not summary:
        return None
    residue = obj.get("emotion_residue")
    emotion_residue: Dict[str, float] = {}
    if isinstance(residue, dict):
        for key in ("valence", "arousal"):
            try:
                emotion_residue[key] = float(residue.get(key) or 0.0)
            except (TypeError, ValueError):
                emotion_residue[key] = 0.0
    return title or "无名之梦", summary, emotion_residue


def _extract_json_object(text: str) -> Any:
    """剥离代码块后取首个合法 JSON 对象。"""
    fence = re.search(r"```(?:json|JSON)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1)
    text = text.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except (ValueError, TypeError):
        pass
    decoder = json.JSONDecoder()
    index = 0
    length = len(text)
    while index < length:
        if text[index] in "{[":
            try:
                obj, end = decoder.raw_decode(text, index)
                if isinstance(obj, dict):
                    return obj
                index = end
                continue
            except ValueError:
                pass
        index += 1
    return None
