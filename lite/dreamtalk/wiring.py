"""DreamTalk 装配层：配置映射、服务组装与醒来摘要回写。

本模块是 CX-A 侧**唯一**读取 ``dreamtalk`` 配置段的地方——``http_api`` 与
``api_server`` 只消费本模块产出的运行态，不直接读 config。

装配口径：

- 梦境使用**独立的** ``OfflineFallbackManager``（共享 ``CloudAdapter`` 与本地运行时
  解析器，自有 mode/status），梦境失败不污染聊天面通道状态；
- 会话库 ``<data_dir>/dreamtalk.db``，与主记忆库物理隔离，首次请求时才建库；
- 素材由 ``CXAMemorySource`` 从 CX-A ``lite/memory`` 只读采集；
- 醒来摘要按 ``memory_writeback`` 回写一条 ``source="dreamtalk"`` 的记忆，
  ``memory_written`` 列 + 进程内集合双保险保证幂等。
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, Mapping, Optional

from .adapters.llm import CXAChatLLMClient, MockLLMClient
from .adapters.memory import CXAMemorySource
from .adapters.store import SQLiteSessionStore
from .core.dialogue import DialogueConfig
from .core.errors import StoreError
from .core.models import CONTRACT_VERSION, MODE_DREAMLESS, STATUS_ENDED, SessionView
from .core.modes import ModeWeights
from .service import DreamTalkService

logger = logging.getLogger(__name__)

SECTION = "dreamtalk"

#: 配置键全集（16 键，与 spec「配置契约」及 DEFAULTS["dreamtalk"] 一致）
CONFIG_KEYS = (
    "enabled",
    "max_rounds",
    "context_turns",
    "material_window_days",
    "material_max_items",
    "mode_weights",
    "director_temperature",
    "dreamer_temperature",
    "wild_director_temperature",
    "wild_dreamer_temperature",
    "db_dir",
    "db_filename",
    "recall_limit",
    "summary_title_max_chars",
    "summary_max_chars",
    "memory_writeback",
)

#: 硬兜底默认值（与 lite.config.config_manager.DEFAULTS["dreamtalk"] 逐键一致）
DEFAULT_SETTINGS: Dict[str, Any] = {
    "enabled": True,
    "max_rounds": 8,
    "context_turns": 16,
    "material_window_days": 7,
    "material_max_items": 20,
    "mode_weights": "memory=0.5,wild=0.3,dreamless=0.2",
    "director_temperature": 0.85,
    "dreamer_temperature": 0.9,
    "wild_director_temperature": 1.0,
    "wild_dreamer_temperature": 1.1,
    "db_dir": "",
    "db_filename": "dreamtalk.db",
    "recall_limit": 20,
    "summary_title_max_chars": 12,
    "summary_max_chars": 120,
    "memory_writeback": True,
}

#: 取整且带范围的键
_INT_RANGES = {
    "max_rounds": (1, 50),
    "context_turns": (2, 100),
    "material_window_days": (0, 365),
    "material_max_items": (0, 200),
    "recall_limit": (1, 100),
    "summary_title_max_chars": (1, 200),
    "summary_max_chars": (1, 1000),
}

#: 浮点且带范围的键
_FLOAT_RANGES = {
    "director_temperature": (0.0, 2.0),
    "dreamer_temperature": (0.0, 2.0),
    "wild_director_temperature": (0.0, 2.0),
    "wild_dreamer_temperature": (0.0, 2.0),
}

_BOOL_KEYS = ("enabled", "memory_writeback")
_STR_KEYS = ("mode_weights", "db_dir", "db_filename")

#: 回写记忆的标签与来源标记
WRITEBACK_SOURCE = "dreamtalk"
WRITEBACK_TAGS = ["dreamtalk", "梦话"]




# ====================================================================== 配置读写
def read_settings(config: Any) -> Dict[str, Any]:
    """读取并校验 ``dreamtalk`` 段；任何非法值回退默认值，绝不抛错。"""
    raw = {}
    if config is not None:
        section = getattr(config, "config", None)
        if isinstance(section, Mapping):
            candidate = section.get(SECTION)
            if isinstance(candidate, Mapping):
                raw = dict(candidate)
    settings = dict(DEFAULT_SETTINGS)
    for key in CONFIG_KEYS:
        if key not in raw:
            continue
        value = _coerce(key, raw[key])
        if value is not None:
            settings[key] = value
    return settings


def _coerce(key: str, value: Any) -> Any:
    """把单个配置值规整为合法类型；非法返回 None（调用方保留默认）。"""
    if key in _BOOL_KEYS:
        return value if isinstance(value, bool) else None
    if key in _INT_RANGES:
        low, high = _INT_RANGES[key]
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value if low <= value <= high else None
    if key in _FLOAT_RANGES:
        low, high = _FLOAT_RANGES[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        return number if low <= number <= high else None
    if key == "db_filename":
        text = value.strip() if isinstance(value, str) else ""
        return text or None
    if key in _STR_KEYS:
        if not isinstance(value, str):
            return None
        if key == "mode_weights":
            return value if _mode_weights_ok(value) else None
        return value.strip()
    return None


def _mode_weights_ok(text: str) -> bool:
    """``mode_weights`` 是否可解析且总和 > 0。"""
    try:
        weights = parse_mode_weights(text)
    except ValueError:
        return False
    return (weights.memory + weights.wild + weights.dreamless) > 0


def parse_mode_weights(text: str) -> ModeWeights:
    """解析 ``"memory=0.5,wild=0.3,dreamless=0.2"``；无法解析时抛 ValueError。"""
    values: Dict[str, float] = {}
    for chunk in str(text or "").split(","):
        item = chunk.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"非法模式权重片段: {item}")
        name, raw = item.split("=", 1)
        name = name.strip()
        if name not in ("memory", "wild", "dreamless"):
            raise ValueError(f"未知模式权重键: {name}")
        try:
            values[name] = float(raw.strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"模式权重不是数字: {item}") from exc
    if not values:
        raise ValueError("模式权重为空")
    return ModeWeights(
        memory=values.get("memory", 0.0),
        wild=values.get("wild", 0.0),
        dreamless=values.get("dreamless", 0.0),
    ).sanitized()


def validate_patch(partial: Mapping[str, Any]) -> Dict[str, Any]:
    """校验 ``PUT /config`` 的部分配置；非法抛 ValueError（由 API 层转 422）。"""
    if not isinstance(partial, Mapping):
        raise ValueError("配置必须是 JSON 对象")
    unknown = [key for key in partial if key not in CONFIG_KEYS]
    if unknown:
        raise ValueError(f"未知配置键: {', '.join(sorted(unknown))}")
    normalized: Dict[str, Any] = {}
    for key, value in partial.items():
        coerced = _coerce(key, value)
        if coerced is None:
            raise ValueError(f"配置项 {key} 的取值非法: {value!r}")
        normalized[key] = coerced
    return normalized


def dreamtalk_db_path(settings: Mapping[str, Any], data_dir: str) -> str:
    """解析会话库绝对路径：``db_dir`` 为空跟随数据目录，相对路径按数据目录解析。"""
    filename = str(settings.get("db_filename") or "dreamtalk.db")
    db_dir = str(settings.get("db_dir") or "")
    if not db_dir:
        root = data_dir
    elif os.path.isabs(db_dir):
        root = db_dir
    else:
        root = os.path.join(data_dir, db_dir)
    return os.path.abspath(os.path.join(root, filename))


# ====================================================================== 运行态
class DreamTalkRuntime:
    """梦境运行态门面：进程内单例，按需建库、按请求转发到异步服务。

    Args:
        config: ``ConfigManager`` 实例（配置读写与落盘）
        store: CX-A ``MemoryStore``（素材采集源）
        memory_manager: CX-A ``MemoryManager``（回写入口；None 时跳过回写）
        data_dir: CX-A 数据目录（会话库落点）
        cloud: CX-A ``CloudAdapter``；None 表示纯离线（Mock）
        local_llm: 本地运行时解析器（``() -> runtime|None``）
        llm_client: 注入的 DreamTalk LLM 客户端（测试用，注入即 mock 口径）
    """

    def __init__(
        self,
        *,
        config: Any,
        store: Any,
        memory_manager: Any = None,
        data_dir: Optional[str] = None,
        cloud: Any = None,
        local_llm: Any = None,
        llm_client: Any = None,
        write_lock: Any = None,
    ):
        self._config = config
        #: 配置落盘锁：由 api_server 注入模块级 ``_CONFIG_WRITE_LOCK``，与
        #: ``/api/settings`` 等既有 ``config.save()`` 调用点共用同一把，避免并发整份覆盖。
        self._write_lock = write_lock or threading.RLock()
        self._memory_store = store
        self._memory_manager = memory_manager
        self._data_dir = data_dir or _default_data_dir()
        self._cloud = cloud
        self._local_llm = local_llm
        self._llm_client = llm_client
        self._settings = read_settings(config)
        self._lock = threading.RLock()
        self._service: Optional[DreamTalkService] = None
        self._session_store: Optional[SQLiteSessionStore] = None
        self._mock = llm_client is not None or cloud is None
        self._written: set = set()

    # -------------------------------------------------------------- 状态
    @property
    def settings(self) -> Dict[str, Any]:
        """当前生效配置（只读快照）。"""
        return dict(self._settings)

    @property
    def mock(self) -> bool:
        """是否处于纯离线（Mock）通道。"""
        return self._mock

    def status(self) -> Dict[str, Any]:
        """``/health`` 载荷：不建库、不触网。"""
        service = self._service
        sessions = 0
        if service is not None:
            try:
                sessions = int(service.get_status().get("sessions", 0))
            except Exception:  # noqa: BLE001 - 状态读取失败不影响 health
                sessions = 0
        enabled = bool(self._settings.get("enabled"))
        return {
            "status": "idle" if enabled else "disabled",
            "enabled": enabled,
            "contract_version": CONTRACT_VERSION,
            "sessions": sessions,
            "mode_weights": str(self._settings.get("mode_weights") or ""),
            "memory_writeback": bool(self._settings.get("memory_writeback")),
        }

    def is_enabled(self) -> bool:
        """服务是否启用（禁用时除 health 外一律 503）。"""
        return bool(self._settings.get("enabled"))

    def config_snapshot(self) -> Dict[str, Any]:
        """``GET /config`` 载荷。"""
        return dict(self._settings)

    def apply_config_patch(self, partial: Mapping[str, Any]) -> Dict[str, Any]:
        """深度合并并落盘配置；非法抛 ValueError（API 层转 422）。"""
        normalized = validate_patch(partial)
        with self._lock:
            merged = dict(self._settings)
            merged.update(normalized)
            # 落盘前再校验一次整段（防止历史脏值与本次补丁组合出非法值）
            checked = _validate_merged(merged)
            if self._config is not None:
                with self._write_lock:
                    for key in CONFIG_KEYS:
                        self._config.set(SECTION, key, checked[key])
                    if hasattr(self._config, "save"):
                        self._config.save()
            previous_enabled = self._settings.get("enabled")
            self._settings = checked
            if previous_enabled != checked.get("enabled"):
                # 开关变化需要重建服务（enabled 在构造期注入门面）
                self._service = None
                self._session_store = None
            elif normalized:
                # 其余键影响编排参数与素材口径 → 下次请求重建
                self._service = None
            return dict(self._settings)

    # -------------------------------------------------------------- 服务
    def _ensure_service(self) -> DreamTalkService:
        """首次使用时建库并组装服务（懒构建，避免启动期落盘副作用）。"""
        with self._lock:
            if self._service is not None:
                return self._service
            settings = self._settings
            session_store = SQLiteSessionStore(
                dreamtalk_db_path(settings, self._data_dir)
            )
            mock = MockLLMClient()
            if self._llm_client is not None:
                dreamer = director = self._llm_client
            else:
                fallback = self._build_fallback()
                dreamer = CXAChatLLMClient(fallback, mock=mock)
                director = CXAChatLLMClient(fallback, mock=MockLLMClient(seed=3))
            service = DreamTalkService(
                dreamer=dreamer,
                director=director,
                store=session_store,
                memory_source=CXAMemorySource(
                    self._memory_store,
                    window_days=int(settings["material_window_days"]),
                    max_items=int(settings["material_max_items"]),
                ),
                dialogue_config=DialogueConfig(
                    max_rounds=int(settings["max_rounds"]),
                    context_turns=int(settings["context_turns"]),
                    director_temperature=float(settings["director_temperature"]),
                    dreamer_temperature=float(settings["dreamer_temperature"]),
                    wild_director_temperature=float(
                        settings["wild_director_temperature"]
                    ),
                    wild_dreamer_temperature=float(settings["wild_dreamer_temperature"]),
                    summary_title_max_chars=int(settings["summary_title_max_chars"]),
                    summary_max_chars=int(settings["summary_max_chars"]),
                ),
                mode_weights=parse_mode_weights(settings["mode_weights"]),
                enabled=bool(settings["enabled"]),
            )
            self._session_store = session_store
            self._service = service
            return service

    def _build_fallback(self):
        """构造梦境专用的 CX-A LLM 通道（独立实例，不复用聊天面）。"""
        if self._cloud is None:
            return None
        try:
            from lite.cloud.fallback import OfflineFallbackManager  # noqa: PLC0415

            return OfflineFallbackManager(
                cloud=self._cloud,
                local_llm=self._local_llm,
                config=self._config,
            )
        except Exception as exc:  # noqa: BLE001 - 通道装配失败退化为纯离线
            logger.warning("梦境 LLM 通道装配失败，退化纯离线: %s", exc)
            return None

    # -------------------------------------------------------------- 入睡 / 推进
    async def start_session(self, mode: str = "auto") -> SessionView:
        """入睡并返回会话视图（显式记忆梦无素材时抛领域 422）。"""
        service = self._ensure_service()
        view = await service.start_session("default", mode)
        self._maybe_writeback(view)
        return view

    async def message(self, session_id: str, content: str = "") -> SessionView:
        """醒者插话 / 继续做梦；达到轮数上限时自动结束。"""
        service = self._ensure_service()
        view = await service.interject(session_id, content)
        self._maybe_writeback(view)
        return view

    async def wake(self, session_id: str) -> SessionView:
        """主动醒来并生成摘要；重复醒来抛领域 409。"""
        service = self._ensure_service()
        view = await service.wake(session_id)
        self._maybe_writeback(view)
        return view

    # -------------------------------------------------------------- 读取
    async def get_session(self, session_id: str) -> SessionView:
        """梦境档案（会话 + 全部回合）。"""
        return await self._ensure_service().get_session(session_id)

    async def list_sessions(self, limit: Optional[int] = None):
        """最近会话（不含回合）。"""
        service = self._ensure_service()
        return await service.list_sessions("default", self._limit(limit))

    async def get_recall(self, session_id: str):
        """单场清醒摘要（不含回合）。"""
        return await self._ensure_service().get_recall(session_id)

    async def list_recalls(self, limit: Optional[int] = None):
        """最近已结束会话的摘要列表（不含回合）。"""
        service = self._ensure_service()
        return await service.list_recalls("default", self._limit(limit))

    def _limit(self, limit: Optional[int]) -> int:
        """列表上限：缺省取 ``recall_limit``。"""
        if limit is None:
            return int(self._settings.get("recall_limit", 20))
        return int(limit)

    # -------------------------------------------------------------- 记忆回写
    def _maybe_writeback(self, view: SessionView) -> None:
        """醒来后把摘要回写 CX-A 记忆库（幂等；失败只告警）。"""
        if not self._settings.get("memory_writeback"):
            return
        session = view.session
        if session is None or session.status != STATUS_ENDED:
            return
        if session.mode == MODE_DREAMLESS:
            return
        if not (session.summary or "").strip():
            return
        session_id = session.id
        if session_id in self._written:
            return
        store = self._session_store
        if store is not None:
            try:
                if store.memory_written(session_id):
                    self._written.add(session_id)
                    return
            except StoreError as exc:
                logger.warning("梦境回写标记读取失败（按未写处理）: %s", exc)
        if self._memory_manager is None:
            logger.warning("记忆管理器缺席，跳过梦境摘要回写")
            return
        content = f"梦见：{session.title or '无名之梦'}。{session.summary}"
        metadata = {
            "dreamtalk_session_id": session_id,
            "mode": session.mode,
            "emotion_residue": dict(session.emotion_residue or {}),
        }
        try:
            self._memory_manager.add_memory(
                content=content,
                type="long_term",
                importance=3,
                tags=list(WRITEBACK_TAGS),
                metadata=metadata,
                source=WRITEBACK_SOURCE,
            )
        except Exception as exc:  # noqa: BLE001 - 回写失败不得使醒来失败
            logger.warning("梦境摘要回写记忆库失败（醒来仍成功）: %s", exc)
            return
        self._written.add(session_id)
        if store is not None:
            try:
                store.mark_memory_written(session_id)
            except Exception as exc:  # noqa: BLE001 - 标记失败不影响幂等集合
                logger.warning("梦境回写标记落库失败: %s", exc)


def build_dreamtalk_runtime(
    config: Any,
    store: Any,
    memory_manager: Any = None,
    *,
    cloud: Any = None,
    local_llm: Any = None,
    data_dir: Optional[str] = None,
    llm_client: Any = None,
    write_lock: Any = None,
) -> DreamTalkRuntime:
    """构造梦境运行态（``api_server.make_handler`` 的唯一装配入口）。"""
    return DreamTalkRuntime(
        config=config,
        store=store,
        memory_manager=memory_manager,
        data_dir=data_dir,
        cloud=cloud,
        local_llm=local_llm,
        llm_client=llm_client,
        write_lock=write_lock,
    )


# ====================================================================== 内部工具
def _validate_merged(merged: Mapping[str, Any]) -> Dict[str, Any]:
    """校验整段配置（落盘前），非法抛 ValueError。"""
    checked = dict(DEFAULT_SETTINGS)
    for key in CONFIG_KEYS:
        value = _coerce(key, merged.get(key, DEFAULT_SETTINGS[key]))
        if value is None:
            raise ValueError(f"配置项 {key} 的取值非法: {merged.get(key)!r}")
        checked[key] = value
    return checked


def _default_data_dir() -> str:
    """缺省数据目录：``<app_root>/data``（与 api_server.DEFAULT_DATA_DIR 同源）。"""
    try:
        from lite.config.paths import data_root  # noqa: PLC0415

        return data_root()
    except Exception:  # noqa: BLE001 - 路径解析失败退回进程 cwd 下的 data
        return os.path.abspath("data")
