"""DreamTalk 会话存储适配器：SQLite 短连接实现与内存测试替身。

每个公开方法使用独立短连接（避免跨线程共享连接），库文件与主业务数据隔离。
``sessions`` 以 ``agent_id`` 列隔离不同 agent，``material_json`` 保存入睡素材快照；
``turns`` 的 ``seq`` 在同一会话内单调递增（事务内取号）。所有 SQLite 失败统一
适配为 ``StoreError``（HTTP 500 语义）。
"""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Union

from ..core.errors import StoreError
from ..core.models import (
    STATUS_ENDED,
    DreamSession,
    Emotion,
    MemoryMaterial,
    Turn,
)

_MATERIAL_SOURCE_DEFAULT = "cxa_memory"


class SQLiteSessionStore:
    """梦境会话 / 回合的 SQLite 存储（短连接 + WAL）。

    Args:
        db_path: 数据库文件路径；父目录不存在时自动创建
    """

    def __init__(self, db_path: Union[str, Path]):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # -------------------------------------------------------------- 连接与建表
    def _connect(self) -> sqlite3.Connection:
        """建立一次短连接。"""
        conn = sqlite3.connect(self.db_path, timeout=20.0)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """短连接上下文：退出时关闭；SQLite 异常统一转 ``StoreError``。"""
        conn = self._connect()
        try:
            yield conn
        except sqlite3.Error as exc:
            raise StoreError(f"梦境存储不可用: {exc}") from exc
        finally:
            conn.close()

    def _init_db(self) -> None:
        """建表、建索引并启用 WAL。"""
        try:
            with self._connection() as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        id TEXT PRIMARY KEY,
                        agent_id TEXT NOT NULL DEFAULT 'default',
                        created_at TEXT NOT NULL,
                        mode TEXT NOT NULL,
                        mode_reason TEXT,
                        status TEXT NOT NULL,
                        material_json TEXT,
                        title TEXT,
                        summary TEXT,
                        emotion_residue_json TEXT,
                        ended_at TEXT,
                        turn_count INTEGER NOT NULL DEFAULT 0,
                        memory_written INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE IF NOT EXISTS turns (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        seq INTEGER NOT NULL,
                        speaker TEXT NOT NULL,
                        content TEXT NOT NULL,
                        flagged INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT,
                        UNIQUE(session_id, seq)
                    );
                    CREATE INDEX IF NOT EXISTS idx_dreamtalk_turns_session
                        ON turns(session_id, seq);
                    CREATE INDEX IF NOT EXISTS idx_dreamtalk_sessions_agent
                        ON sessions(agent_id, created_at DESC);
                    """
                )
                self._ensure_memory_written_column(conn)
                conn.commit()
        except sqlite3.Error as exc:
            raise StoreError(f"梦境存储初始化失败: {exc}") from exc

    @staticmethod
    def _ensure_memory_written_column(conn: sqlite3.Connection) -> None:
        """旧库兼容：缺少 ``memory_written`` 列时补建（幂等）。

        CX-A 扩展列，仅由装配层用于「醒来摘要回写记忆」的幂等标记，
        核心领域层不感知该列。
        """
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
        if "memory_written" not in columns:
            conn.execute(
                "ALTER TABLE sessions ADD COLUMN memory_written INTEGER NOT NULL DEFAULT 0"
            )

    # -------------------------------------------------------------- 会话
    def create_session(self, session: DreamSession) -> None:
        """持久化新建会话（含素材快照 JSON）。"""
        material = session.effective_material
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    id, agent_id, created_at, mode, mode_reason, status,
                    material_json, title, summary, emotion_residue_json,
                    ended_at, turn_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.id,
                    session.agent_id or "default",
                    session.created_at,
                    session.mode,
                    session.mode_reason,
                    session.status,
                    _dump_json(material.to_dict()),
                    session.title,
                    session.summary,
                    _dump_json(session.emotion_residue or {}),
                    session.ended_at,
                    int(session.turn_count),
                ),
            )
            conn.commit()

    def get_session(self, session_id: str) -> Optional[DreamSession]:
        """按会话 ID 读取；不存在返回 None。"""
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT s.*, (
                    SELECT COUNT(*) FROM turns t WHERE t.session_id = s.id
                ) AS turn_count
                FROM sessions s WHERE s.id = ?
                """,
                (session_id,),
            ).fetchone()
            return _row_to_session(row) if row is not None else None

    def list_sessions(self, agent_id: str = "default", limit: int = 20) -> List[DreamSession]:
        """按 agent 过滤，按创建时间倒序列出最近会话。"""
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT s.*, (
                    SELECT COUNT(*) FROM turns t WHERE t.session_id = s.id
                ) AS turn_count
                FROM sessions s
                WHERE s.agent_id = ?
                ORDER BY s.created_at DESC, s.rowid DESC
                LIMIT ?
                """,
                (agent_id or "default", max(int(limit), 0)),
            ).fetchall()
            return [_row_to_session(row) for row in rows]

    # -------------------------------------------------------------- 回合
    def append_turn(
        self,
        session_id: str,
        speaker: str,
        content: str,
        flagged: bool = False,
        created_at: str = "",
    ) -> Turn:
        """追加一个回合；``seq`` 在写事务内单调递增。"""
        created = created_at or datetime.now().isoformat()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq "
                    "FROM turns WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                seq = int(row["next_seq"])
                conn.execute(
                    """
                    INSERT INTO turns (session_id, seq, speaker, content, flagged, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (session_id, seq, speaker, content, 1 if flagged else 0, created),
                )
                conn.execute(
                    "UPDATE sessions SET turn_count = ? WHERE id = ?",
                    (seq, session_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return Turn(
            seq=seq,
            speaker=speaker,
            content=content,
            flagged=bool(flagged),
            created_at=created,
            id=f"{session_id}:{seq}",
            session_id=session_id,
        )

    def get_turns(self, session_id: str) -> List[Turn]:
        """按 ``seq`` 升序读取某会话的全部回合。"""
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT session_id, seq, speaker, content, flagged, created_at
                FROM turns WHERE session_id = ? ORDER BY seq ASC
                """,
                (session_id,),
            ).fetchall()
            return [
                Turn(
                    seq=int(_value(row, "seq", 0)),
                    speaker=str(_value(row, "speaker", "")),
                    content=str(_value(row, "content", "")),
                    flagged=bool(_value(row, "flagged", 0)),
                    created_at=str(_value(row, "created_at", "") or ""),
                    id=f"{session_id}:{int(_value(row, 'seq', 0))}",
                    session_id=session_id,
                )
                for row in rows
            ]

    # -------------------------------------------------------------- 结束与摘要
    def end_session(
        self,
        session_id: str,
        *,
        title: str,
        summary: str,
        emotion_residue: Mapping[str, float],
        ended_at: str,
    ) -> None:
        """结束会话并写入醒来摘要。"""
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET status = ?, title = ?, summary = ?,
                    emotion_residue_json = ?, ended_at = ?,
                    turn_count = (
                        SELECT COUNT(*) FROM turns t WHERE t.session_id = sessions.id
                    )
                WHERE id = ?
                """,
                (
                    STATUS_ENDED,
                    title,
                    summary,
                    _dump_json(dict(emotion_residue or {})),
                    ended_at,
                    session_id,
                ),
            )
            conn.commit()

    def update_summary(
        self,
        session_id: str,
        *,
        title: str,
        summary: str,
        emotion_residue: Mapping[str, float],
    ) -> None:
        """仅更新摘要字段，不改会话状态。"""
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET title = ?, summary = ?, emotion_residue_json = ?
                WHERE id = ?
                """,
                (title, summary, _dump_json(dict(emotion_residue or {})), session_id),
            )
            conn.commit()

    def last_mode(self, agent_id: str = "default") -> Optional[str]:
        """返回该 agent 最近一场会话的模式；无会话返回 None。"""
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT mode FROM sessions
                WHERE agent_id = ?
                ORDER BY created_at DESC, rowid DESC LIMIT 1
                """,
                (agent_id or "default",),
            ).fetchone()
            return str(row["mode"]) if row is not None else None

    # -------------------------------------------------------------- 记忆回写标记
    def mark_memory_written(self, session_id: str) -> None:
        """标记该会话的醒来摘要已回写 CX-A 记忆库（幂等标记，CX-A 扩展）。"""
        with self._connection() as conn:
            conn.execute(
                "UPDATE sessions SET memory_written = 1 WHERE id = ?", (session_id,)
            )
            conn.commit()

    def memory_written(self, session_id: str) -> bool:
        """该会话的醒来摘要是否已回写记忆库（CX-A 扩展）。"""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT memory_written FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            return bool(_value(row, "memory_written", 0)) if row is not None else False


class MemorySessionStore:
    """内存会话存储：与 ``SQLiteSessionStore`` 同签名的测试替身。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._sessions: Dict[str, DreamSession] = {}
        self._turns: Dict[str, List[Turn]] = {}
        self._order: List[str] = []
        self._memory_written: set = set()

    def create_session(self, session: DreamSession) -> None:
        """保存会话副本。"""
        with self._lock:
            self._sessions[session.id] = copy.deepcopy(session)
            self._turns[session.id] = []
            self._order.append(session.id)

    def get_session(self, session_id: str) -> Optional[DreamSession]:
        """读取会话副本；不存在返回 None。"""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            result = copy.deepcopy(session)
            result.turn_count = len(self._turns.get(session_id, []))
            return result

    def list_sessions(self, agent_id: str = "default", limit: int = 20) -> List[DreamSession]:
        """按 agent 过滤，按最近创建倒序列出。"""
        with self._lock:
            target = agent_id or "default"
            sessions = [
                self._sessions[session_id]
                for session_id in reversed(self._order)
                if self._sessions.get(session_id) is not None
                and self._sessions[session_id].agent_id == target
            ]
            return [copy.deepcopy(item) for item in sessions[: max(int(limit), 0)]]

    def append_turn(
        self,
        session_id: str,
        speaker: str,
        content: str,
        flagged: bool = False,
        created_at: str = "",
    ) -> Turn:
        """追加回合；``seq`` 在同一会话内自增。"""
        with self._lock:
            turns = self._turns.setdefault(session_id, [])
            seq = len(turns) + 1
            turn = Turn(
                seq=seq,
                speaker=speaker,
                content=content,
                flagged=bool(flagged),
                created_at=created_at or datetime.now().isoformat(),
                id=f"{session_id}:{seq}",
                session_id=session_id,
            )
            turns.append(turn)
            return copy.deepcopy(turn)

    def get_turns(self, session_id: str) -> List[Turn]:
        """按追加顺序返回回合副本。"""
        with self._lock:
            return [copy.deepcopy(turn) for turn in self._turns.get(session_id, [])]

    def end_session(
        self,
        session_id: str,
        *,
        title: str,
        summary: str,
        emotion_residue: Mapping[str, float],
        ended_at: str,
    ) -> None:
        """结束会话并写入摘要。"""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return
            session.status = STATUS_ENDED
            session.title = title
            session.summary = summary
            session.emotion_residue = dict(emotion_residue or {})
            session.ended_at = ended_at
            session.turn_count = len(self._turns.get(session_id, []))

    def update_summary(
        self,
        session_id: str,
        *,
        title: str,
        summary: str,
        emotion_residue: Mapping[str, float],
    ) -> None:
        """仅更新摘要字段。"""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return
            session.title = title
            session.summary = summary
            session.emotion_residue = dict(emotion_residue or {})

    def last_mode(self, agent_id: str = "default") -> Optional[str]:
        """返回该 agent 最近一场会话的模式。"""
        with self._lock:
            target = agent_id or "default"
            for session_id in reversed(self._order):
                session = self._sessions.get(session_id)
                if session is not None and session.agent_id == target:
                    return session.mode
            return None

    def mark_memory_written(self, session_id: str) -> None:
        """标记该会话的醒来摘要已回写记忆库（测试替身，CX-A 扩展）。"""
        with self._lock:
            self._memory_written.add(str(session_id))

    def memory_written(self, session_id: str) -> bool:
        """该会话的醒来摘要是否已回写记忆库（测试替身，CX-A 扩展）。"""
        with self._lock:
            return str(session_id) in self._memory_written


# ------------------------------------------------------------------ 工具
def _dump_json(value: Any) -> Optional[str]:
    """序列化为 JSON 文本；None 原样返回。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _load_json(value: Any) -> Any:
    """把 JSON 文本还原为对象；非法值返回 None。"""
    if value is None or not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def _value(row: Any, key: str, default: Any = None) -> Any:
    """读取行字段；缺失列或 None 时返回默认值（兼容 sqlite3.Row）。"""
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _row_to_session(row: Any) -> DreamSession:
    """把数据行还原为 ``DreamSession``（解析 ``material_json``）。"""
    return DreamSession(
        id=str(_value(row, "id", "")),
        agent_id=str(_value(row, "agent_id", "default") or "default"),
        created_at=str(_value(row, "created_at", "")),
        mode=str(_value(row, "mode", "")),
        mode_reason=str(_value(row, "mode_reason", "") or ""),
        status=str(_value(row, "status", "")),
        material=_material_from_json(_load_json(_value(row, "material_json"))),
        title=str(_value(row, "title", "") or ""),
        summary=str(_value(row, "summary", "") or ""),
        emotion_residue=_residue_from_json(
            _load_json(_value(row, "emotion_residue_json"))
        ),
        ended_at=_value(row, "ended_at", None),
        turn_count=int(_value(row, "turn_count", 0) or 0),
    )


def _material_from_json(value: Any) -> MemoryMaterial:
    """从 ``material_json`` 还原素材快照；非法结构降级为空素材。"""
    if not isinstance(value, Mapping):
        return MemoryMaterial(source=_MATERIAL_SOURCE_DEFAULT)
    memories = value.get("day_memories") or []
    entities = value.get("entities") or []
    return MemoryMaterial(
        day_memories=[str(item).strip() for item in memories if str(item).strip()],
        entities=[str(item).strip() for item in entities if str(item).strip()],
        emotion=Emotion.from_mapping(value.get("emotion")).clamped(),
        source=str(value.get("source") or _MATERIAL_SOURCE_DEFAULT),
    )


def _residue_from_json(value: Any) -> Dict[str, float]:
    """把情绪残留 JSON 还原为 ``字段 -> 浮点`` 字典。"""
    if not isinstance(value, Mapping):
        return {}
    return {str(key): _to_float(item, 0.0) for key, item in value.items()}


def _to_float(value: Any, default: float) -> float:
    """安全转浮点，失败返回默认值。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
