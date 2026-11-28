"""DreamTalk 记忆源适配器：CX-A 记忆库只读采集与测试用手动素材源。

``CXAMemorySource`` 在入睡时从 CX-A ``lite/memory`` 只读采集一次快照；梦境回合
期间不再访问记忆库。采集口径移植 CX-O ``server/autonomy/dream/collector.py``
的主规则（窗口 / 边缘重要性 / 排序 / 上限 / 补齐 / 意象 / 日记情绪），并按 CX-A
现状做两处适配：

- CX-A 的 ``lite/graph/graph_store.py`` **尚未接入写入链路**（库内无节点），CX-O 的
  「孤立实体（度数 == 1）」暂以「记忆 tags 去重意象」替代；图谱接线后需重新评估该口径；
- CX-A 的记忆写入口会打 ``source="dreamtalk"`` 与 ``metadata.dreamtalk_session_id``
  标记，采集侧必须排除这些记录，否则梦境摘要会回流入梦（自噬）。

采集失败降级为空素材，不阻断入睡。``ManualMemorySource`` 仅供测试或服务内注入。
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from datetime import datetime, timedelta
from typing import Any, List, Mapping, Optional

from ..core.models import Emotion, MemoryMaterial

logger = logging.getLogger(__name__)

# 边缘记忆阈值：基线重要性 < 0.5 视为正在衰减的碎片素材
_EDGE_IMPORTANCE_THRESHOLD = 0.5

# 红线 R2 阈值：基线重要性 >= 0.95 或 permanent 记忆禁止入梦
_PROTECTED_IMPORTANCE_THRESHOLD = 0.95

# 补齐下限：边缘记忆少于该条数时用同窗口其余记忆补齐
_MIN_MEMORY_ITEMS = 3

# 零散意象上限
_MAX_ENTITIES = 8

# 梦境回写的来源标记（CX-A 用 dreamtalk；dream 为历史兼容）
_DREAM_SOURCES = ("dreamtalk", "dream")

# 关联梦境会话的 metadata 键（两代命名都排除）
_DREAM_METADATA_KEYS = ("dreamtalk_session_id", "dream_session_id")

# 候选池倍数（大于上限留过滤余量）
_POOL_FACTOR = 5
_POOL_MIN = 200


class CXAMemorySource:
    """把 CX-A 记忆库的只读快照映射为 ``MemoryMaterial``。

    Args:
        store: CX-A ``MemoryStore``（只需 ``list_recent`` / ``list`` 读方法）
        agent_id: 默认 agent 标识
        window_days: 素材时间窗（天）
        max_items: 素材条数上限
    """

    def __init__(
        self,
        store: Any,
        *,
        agent_id: str = "default",
        window_days: int = 7,
        max_items: int = 20,
    ):
        self._store = store
        self._agent_id = agent_id or "default"
        self._window_days = max(int(window_days), 0)
        self._max_items = max(int(max_items), 0)

    async def collect(self, agent_id: str = "default") -> MemoryMaterial:
        """只读采集一次快照；异常或空库返回空素材。"""
        try:
            return await asyncio.to_thread(
                self._collect_sync, agent_id or self._agent_id
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 采集失败降级为空素材
            logger.warning("CX-A 记忆采集失败，返回空素材: %s", exc)
            return MemoryMaterial(source="cxa_memory")

    # -------------------------------------------------------------- 采集主流程
    def _collect_sync(self, agent_id: str) -> MemoryMaterial:
        """同步采集实现（由 ``collect`` 放入线程池执行）。"""
        if self._max_items <= 0:
            return MemoryMaterial(source="cxa_memory")
        rows = self._load_pool(agent_id)
        cutoff = datetime.now() - timedelta(days=self._window_days)
        candidates = [row for row in rows if self._is_candidate(row, cutoff)]

        edges = [
            row
            for row in candidates
            if _baseline_importance(row) < _EDGE_IMPORTANCE_THRESHOLD
        ]
        # 最边缘优先：基线重要性升序，同分按 created_at 降序
        edges.sort(key=lambda row: (_baseline_importance(row), -_created_ts(row)))
        selected = edges[: self._max_items]

        if len(selected) < _MIN_MEMORY_ITEMS:
            # 素材稀薄会让「记忆梦」长期缺席：用同窗口其余合格记忆按时间倒序补齐
            chosen_ids = {_row_id(row) for row in selected}
            rest = [row for row in candidates if _row_id(row) not in chosen_ids]
            rest.sort(key=lambda row: _created_ts(row), reverse=True)
            for row in rest:
                if len(selected) >= min(_MIN_MEMORY_ITEMS, self._max_items):
                    break
                selected.append(row)

        return MemoryMaterial(
            day_memories=_memory_lines(selected),
            entities=_entity_lines(selected),
            emotion=_diary_emotion(rows),
            source="cxa_memory",
        )

    def _load_pool(self, agent_id: str) -> List[dict]:
        """拉取候选池（只为只读查询，不做任何写入）。"""
        pool_limit = max(self._max_items * _POOL_FACTOR, _POOL_MIN)
        rows = self._store.list_recent(
            agent_id=agent_id, limit=pool_limit, include_deleted=False
        )
        return list(rows or [])

    def _is_candidate(self, row: Mapping[str, Any], cutoff: datetime) -> bool:
        """是否为合格素材：窗口内、非梦境回写、未触碰 R2 红线。"""
        if _is_dream_writeback(row):
            return False
        if _is_protected(row):
            return False
        return _within_window(row, cutoff)


class ManualMemorySource:
    """手动记忆源：返回构造时给定素材的副本（仅测试 / 服务内注入）。"""

    def __init__(self, material: Optional[MemoryMaterial] = None):
        self._material = material or MemoryMaterial(source="manual")

    async def collect(self, agent_id: str = "default") -> MemoryMaterial:
        """返回素材副本，避免调用方修改共享快照。"""
        return copy.deepcopy(self._material)


# ------------------------------------------------------------------ 字段口径
def _baseline_importance(row: Mapping[str, Any]) -> float:
    """基线重要性：``importance_score`` 优先，缺省按 ``importance/5.0``，脏值 0.6。

    与 ``lite/memory/storage.py::_decay_importance_of`` 同口径，避免历史死字段
    让高重要性记忆漏过 R2 红线。
    """
    score = row.get("importance_score")
    if score is not None:
        try:
            return max(0.0, min(1.0, float(score)))
        except (TypeError, ValueError):
            return 0.6
    try:
        return max(0.0, min(1.0, int(row.get("importance", 3)) / 5.0))
    except (TypeError, ValueError):
        return 0.6


def _is_protected(row: Mapping[str, Any]) -> bool:
    """红线 R2：permanent 记忆或基线重要性 >= 0.95 不得入梦。"""
    if bool(row.get("permanent")) or str(row.get("type") or "") == "permanent":
        return True
    return _baseline_importance(row) >= _PROTECTED_IMPORTANCE_THRESHOLD


def _is_dream_writeback(row: Mapping[str, Any]) -> bool:
    """是否为梦境回写记录（来源标记或 metadata 关联会话标记）。"""
    if str(row.get("source") or "") in _DREAM_SOURCES:
        return True
    metadata = row.get("metadata")
    if isinstance(metadata, str):
        metadata = _load_json(metadata)
    if isinstance(metadata, Mapping):
        return any(metadata.get(key) for key in _DREAM_METADATA_KEYS)
    return False


def _within_window(row: Mapping[str, Any], cutoff: datetime) -> bool:
    """``created_at`` 是否落在素材窗口内；缺失/不可解析时视为窗口内（不丢素材）。"""
    created = _parse_created(row.get("created_at"))
    if created is None:
        return True
    return created >= cutoff


def _created_ts(row: Mapping[str, Any]) -> float:
    """``created_at`` 的时间戳；缺失/不可解析返回 0.0。"""
    created = _parse_created(row.get("created_at"))
    return created.timestamp() if created is not None else 0.0


def _parse_created(value: Any) -> Optional[datetime]:
    """解析 CX-A 存储时间戳（``%Y-%m-%d %H:%M:%S.%f`` 或 ISO）；失败返回 None。

    带时区的解析结果先归一到本地时区再剥 tzinfo，避免与 ``datetime.now()``
    相减时抛 aware/naive 混用异常。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _row_id(row: Mapping[str, Any]) -> Any:
    """记录主键（缺失时退回 object id，保证去重集合可用）。"""
    return row.get("id") if row.get("id") is not None else id(row)


def _memory_lines(rows: List[Mapping[str, Any]]) -> List[str]:
    """把记忆条目映射为 ``content`` 文本列表（跳过空白）。"""
    lines: List[str] = []
    for row in rows:
        content = str(row.get("content") or "").strip()
        if content:
            lines.append(content)
    return lines


def _entity_lines(rows: List[Mapping[str, Any]]) -> List[str]:
    """把记忆 tags 去重为「零散意象」（JSON 字符串与数组两种形态都支持）。"""
    entities: List[str] = []
    seen = set()
    for row in rows:
        for tag in _as_tags(row.get("tags")):
            if not tag or tag in seen:
                continue
            seen.add(tag)
            entities.append(tag)
            if len(entities) >= _MAX_ENTITIES:
                return entities
    return entities


def _as_tags(value: Any) -> List[str]:
    """把 tags 归一为去空白字符串列表（JSON 文本先解析，非列表按单标签处理）。"""
    if value is None:
        return []
    if isinstance(value, str):
        parsed = _load_json(value)
        if isinstance(parsed, (list, tuple)):
            return [str(item).strip() for item in parsed if str(item).strip()]
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _load_json(value: str) -> Any:
    """尝试把字符串解析为 JSON；失败返回 None。"""
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _diary_emotion(rows: List[Mapping[str, Any]]) -> Emotion:
    """最近一条非梦境日记的 ``emotion_score`` 映射为情绪基调。

    ``valence = clamp(2s-1, -1, 1)``、``arousal = 0.4``（日记只有情感强度，
    唤醒度按固定低-中值给出）；无日记时返回中性 ``Emotion(0, 0)``。
    """
    diaries = [
        row
        for row in rows
        if str(row.get("type") or "") == "diary" and not _is_dream_writeback(row)
    ]
    if not diaries:
        return Emotion()
    diaries.sort(key=lambda row: _created_ts(row), reverse=True)
    try:
        score = float(diaries[0].get("emotion_score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    score = max(0.0, min(1.0, score))
    return Emotion(valence=max(-1.0, min(1.0, 2.0 * score - 1.0)), arousal=0.4).clamped()
