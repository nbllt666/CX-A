"""DreamTalk 纯领域模型：梦境会话、回合、记忆素材与醒来摘要。

本模块只做数据定义与纯转换，不依赖任何基础设施（数据库 / HTTP / 环境变量）。
``from_row()`` 的输入是行映射（Mapping / sqlite3.Row 皆可），取行与落库由适配器负责。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional

CONTRACT_VERSION = "1.0"


class Mode(str, Enum):
    """梦境模式：记忆梦 / 天马行空 / 一夜无梦。"""

    MEMORY = "memory"
    WILD = "wild"
    DREAMLESS = "dreamless"


class Speaker(str, Enum):
    """回合说话人：潜意识导演 / 梦者 / 醒者。"""

    DIRECTOR = "director"
    DREAMER = "dreamer"
    AWAKENED = "awakened"


class Status(str, Enum):
    """会话状态：进行中 / 已结束。"""

    ACTIVE = "active"
    ENDED = "ended"


MODE_MEMORY = Mode.MEMORY.value
MODE_WILD = Mode.WILD.value
MODE_DREAMLESS = Mode.DREAMLESS.value
MODES = (MODE_MEMORY, MODE_WILD, MODE_DREAMLESS)

MODE_LABELS = {
    MODE_MEMORY: "记忆梦",
    MODE_WILD: "天马行空",
    MODE_DREAMLESS: "一夜无梦",
}

SPEAKER_DIRECTOR = Speaker.DIRECTOR.value
SPEAKER_DREAMER = Speaker.DREAMER.value
SPEAKER_AWAKENED = Speaker.AWAKENED.value

SPEAKER_LABELS = {
    SPEAKER_DIRECTOR: "潜意识导演",
    SPEAKER_DREAMER: "梦者",
    SPEAKER_AWAKENED: "醒者",
}

STATUS_ACTIVE = Status.ACTIVE.value
STATUS_ENDED = Status.ENDED.value


@dataclass
class Message:
    """发送给 LLM 的一条消息（role 为 system / user / assistant）。"""

    role: str
    content: str


@dataclass
class Emotion:
    """情绪基调：效价（-1~1）与唤醒度（0~1）。"""

    valence: float = 0.0
    arousal: float = 0.0

    def intensity(self) -> float:
        """情绪强度：|valence| + arousal，截断到 1.0。"""
        return min(1.0, abs(float(self.valence)) + float(self.arousal))

    def clamped(self) -> "Emotion":
        """裁剪到合法区间，避免外部输入越界影响提示词。"""
        return Emotion(
            valence=max(-1.0, min(1.0, float(self.valence))),
            arousal=max(0.0, min(1.0, float(self.arousal))),
        )

    def to_dict(self) -> Dict[str, float]:
        """转为 JSON 友好的效价 / 唤醒度字典。"""
        return {"valence": float(self.valence), "arousal": float(self.arousal)}

    @classmethod
    def from_mapping(cls, value: Any) -> "Emotion":
        """从映射或 ``Emotion`` 构造；非法值按 0.0 处理。"""
        if isinstance(value, Emotion):
            return value
        if not isinstance(value, Mapping):
            return cls()
        return cls(
            valence=_to_float(value.get("valence"), 0.0),
            arousal=_to_float(value.get("arousal"), 0.0),
        )


@dataclass
class MemoryMaterial:
    """本次入睡时解析出的记忆素材快照（梦内唯一的外部输入）。"""

    day_memories: List[str] = field(default_factory=list)
    entities: List[str] = field(default_factory=list)
    emotion: Emotion = field(default_factory=Emotion)
    source: str = "cxa_memory"

    def is_empty(self) -> bool:
        """没有任何可用素材（记忆与零散意象都为空）。"""
        has_memories = any(str(m).strip() for m in self.day_memories)
        has_entities = any(str(e).strip() for e in self.entities)
        return not (has_memories or has_entities)

    def to_dict(self) -> Dict[str, Any]:
        """转为 JSON 友好的素材快照。"""
        return material_to_dict(self)


@dataclass
class Turn:
    """梦境中的一个回合。

    字段顺序保证上游 ``Turn(seq=..., speaker=..., content=...)`` 的构造方式仍然可用；
    ``id`` / ``session_id`` 为 CX-O 契约补充字段（见 dreamtalk.schema.json）。
    """

    seq: int
    speaker: str
    content: str
    flagged: bool = False
    created_at: str = ""
    id: str = ""
    session_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """转为数据契约 ``dreamtalk_turn`` 实体字典。"""
        return {
            "id": str(self.id),
            "session_id": str(self.session_id),
            "seq": int(self.seq),
            "speaker": str(self.speaker),
            "content": str(self.content),
            "flagged": bool(self.flagged),
            "created_at": str(self.created_at),
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Turn":
        """从数据行构造回合；缺失字段按空值处理。"""
        return cls(
            seq=int(_row_value(row, "seq", 0) or 0),
            speaker=str(_row_value(row, "speaker", "")),
            content=str(_row_value(row, "content", "")),
            flagged=bool(_row_value(row, "flagged", 0)),
            created_at=str(_row_value(row, "created_at", "")),
            id=str(_row_value(row, "id", "")),
            session_id=str(_row_value(row, "session_id", "")),
        )


@dataclass
class DreamSession:
    """一场梦境会话（按 ``agent_id`` 隔离，互不继承）。"""

    id: str = ""
    agent_id: str = "default"
    created_at: str = ""
    mode: str = MODE_MEMORY
    mode_reason: str = ""
    status: str = STATUS_ACTIVE
    material: Optional[MemoryMaterial] = field(default_factory=MemoryMaterial)
    title: str = ""
    summary: str = ""
    emotion_residue: Dict[str, float] = field(default_factory=dict)
    ended_at: Optional[str] = None
    turn_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """转为数据契约 ``dreamtalk_session`` 实体字典（不含素材内部字段）。"""
        return {
            "id": str(self.id),
            "agent_id": str(self.agent_id),
            "created_at": str(self.created_at),
            "mode": str(self.mode),
            "mode_reason": str(self.mode_reason),
            "status": str(self.status),
            "title": str(self.title),
            "summary": str(self.summary),
            "emotion_residue": {
                str(key): _to_float(value, 0.0)
                for key, value in (self.emotion_residue or {}).items()
            },
            "ended_at": self.ended_at,
            "turn_count": int(self.turn_count),
        }

    @property
    def effective_material(self) -> MemoryMaterial:
        """素材快照；缺失时返回空快照，保证提示词构造不因 None 崩溃。"""
        return self.material if isinstance(self.material, MemoryMaterial) else MemoryMaterial()

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "DreamSession":
        """从数据行构造会话（含素材快照的 JSON 列还原）。"""
        emotion_raw = _as_mapping(_load_json(_row_value(row, "emotion_json")))
        memory_raw = _as_list(_load_json(_row_value(row, "seeds_json")))
        entity_raw = _as_list(_load_json(_row_value(row, "entities_json")))
        residue_raw = _as_mapping(_load_json(_row_value(row, "emotion_residue_json")))
        material = MemoryMaterial(
            day_memories=[str(item) for item in memory_raw if str(item).strip()],
            entities=[str(item) for item in entity_raw if str(item).strip()],
            emotion=Emotion.from_mapping(emotion_raw),
            source=str(_row_value(row, "material_source", "cxa_memory") or "cxa_memory"),
        )
        ended_at = _row_value(row, "ended_at", None)
        return cls(
            id=str(_row_value(row, "id", "")),
            agent_id=str(_row_value(row, "agent_id", "default") or "default"),
            created_at=str(_row_value(row, "created_at", "")),
            mode=str(_row_value(row, "mode", MODE_MEMORY)),
            mode_reason=str(_row_value(row, "mode_reason", "") or ""),
            status=str(_row_value(row, "status", STATUS_ACTIVE)),
            material=material,
            title=str(_row_value(row, "title", "") or ""),
            summary=str(_row_value(row, "summary", "") or ""),
            emotion_residue={
                str(key): _to_float(value, 0.0) for key, value in residue_raw.items()
            },
            ended_at=None if ended_at is None else str(ended_at),
            turn_count=int(_row_value(row, "turn_count", 0) or 0),
        )


@dataclass
class DreamRecall:
    """清醒侧唯一可见的梦境摘要（不含原始回合）。"""

    session_id: str = ""
    created_at: str = ""
    mode: str = MODE_MEMORY
    title: str = ""
    summary: str = ""
    emotion_residue: Dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_session(cls, session: DreamSession) -> "DreamRecall":
        """由会话生成清醒侧摘要。"""
        return cls(
            session_id=session.id,
            created_at=session.created_at,
            mode=session.mode,
            title=session.title,
            summary=session.summary,
            emotion_residue=dict(session.emotion_residue or {}),
        )

    def to_dict(self) -> Dict[str, Any]:
        """转为数据契约 ``dreamtalk_recall`` 实体字典。"""
        return {
            "session_id": str(self.session_id),
            "created_at": str(self.created_at),
            "mode": str(self.mode),
            "title": str(self.title),
            "summary": str(self.summary),
            "emotion_residue": {
                str(key): _to_float(value, 0.0)
                for key, value in (self.emotion_residue or {}).items()
            },
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "DreamRecall":
        """从数据行构造醒来摘要。"""
        residue_raw = _as_mapping(_load_json(_row_value(row, "emotion_residue_json")))
        return cls(
            session_id=str(_row_value(row, "session_id", "") or _row_value(row, "id", "")),
            created_at=str(_row_value(row, "created_at", "")),
            mode=str(_row_value(row, "mode", MODE_MEMORY)),
            title=str(_row_value(row, "title", "") or ""),
            summary=str(_row_value(row, "summary", "") or ""),
            emotion_residue={
                str(key): _to_float(value, 0.0) for key, value in residue_raw.items()
            },
        )


@dataclass
class DreamContext:
    """梦内上下文：素材快照 + 梦内回合 + 模式，绝不携带梦外历史。"""

    material: MemoryMaterial = field(default_factory=MemoryMaterial)
    turns: List[Turn] = field(default_factory=list)
    mode: str = MODE_MEMORY
    session_id: str = ""


@dataclass
class SessionView:
    """服务层返回：会话 + 可见回合（可选醒来摘要）。"""

    session: DreamSession
    turns: List[Turn] = field(default_factory=list)
    recall: Optional[DreamRecall] = None

    def to_dict(self) -> Dict[str, Any]:
        """转为 API 友好的视图字典。"""
        return {
            "session": self.session.to_dict(),
            "turns": [turn.to_dict() for turn in self.turns],
            "recall": self.recall.to_dict() if self.recall is not None else None,
        }


def mode_label(mode: str) -> str:
    """模式的中文名；未知模式原样返回。"""
    return MODE_LABELS.get(mode, mode)


def speaker_label(speaker: str) -> str:
    """说话人的中文名；未知说话人原样返回。"""
    return SPEAKER_LABELS.get(speaker, speaker)


def emotion_to_dict(emotion: Emotion) -> Dict[str, float]:
    """情绪裁剪后的字典表示。"""
    return emotion.clamped().to_dict()


def material_to_dict(material: MemoryMaterial) -> Dict[str, Any]:
    """素材快照的 JSON 友好表示。"""
    return {
        "day_memories": list(material.day_memories),
        "entities": list(material.entities),
        "emotion": emotion_to_dict(material.emotion),
        "source": material.source,
    }


# ------------------------------------------------------------------ 纯转换工具
def _row_value(row: Mapping[str, Any], key: str, default: Any = None) -> Any:
    """读取行字段；缺失列或 None 时返回默认值（兼容 sqlite3.Row）。"""
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _load_json(value: Any) -> Any:
    """把行中的 JSON 文本还原为对象；非法值原样返回。"""
    if value is None or not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


def _as_list(value: Any) -> List[Any]:
    """确保返回列表；非列表按空列表处理。"""
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _as_mapping(value: Any) -> Dict[str, Any]:
    """确保返回字典；非字典按空字典处理。"""
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _to_float(value: Any, default: float) -> float:
    """安全转浮点，失败返回默认值。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
