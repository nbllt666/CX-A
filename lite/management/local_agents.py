# -*- coding: utf-8 -*-
"""本地多 Agent 人设管理（Task E2）——AgentManager 的本地 JSON 持久化实现。

以 lite/management/local_agents.py 为锚点，用 os.path.dirname(os.path.abspath(__file__))
逐级上溯到项目根，将用户数据落盘到 <项目根>/data/agents.json（默认路径，可注入 path 覆盖）。
文件首次不存在时自动创建空列表并注入默认种子 Agent（id="default"，软软）。

所有写操作（create / update / delete / set_enabled）后立即将全量列表以 UTF-8、
ensure_ascii=False 写回 agents.json，保证中文人设不做 \\u 转义。

异常契约：get / update / delete 命中不存在的 agent 时抛 AgentNotFound。
"""

import json
import os
import uuid
from datetime import datetime

# ------------------------------------------------------------------ 路径推导
# local_agents.py -> lite/management -> lite -> 项目根（逐级上溯 3 次）
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_MANAGEMENT_DIR = os.path.dirname(_THIS_DIR)  # lite
_PROJECT_ROOT = os.path.dirname(_MANAGEMENT_DIR)

# 默认音色（与前端语音默认语音保持一致）
DEFAULT_VOICE = "cx-open"

# 首次初始化时注入的默认种子 Agent
DEFAULT_SEED_ID = "default"
DEFAULT_SEED_NAME = "软软"
DEFAULT_SEED_PERSONA = "温柔可靠的赛博伴侣，话少但事事记在心上"


class Agent:
    """一个本地智能体（人设）实体。

    Attributes:
        id: 唯一标识（字符串）。
        name: 智能体名称。
        persona: 人设描述（一段中文提示词）。
        voice: 自定义音色（默认 cx-open）。
        enabled: 是否启用。
        created_at / updated_at: ISO 格式时间戳（字符串）。
    """

    def __init__(
        self,
        id,
        name,
        persona,
        voice=DEFAULT_VOICE,
        enabled=True,
        created_at=None,
        updated_at=None,
    ):
        """初始化单个 Agent 实体。"""
        self.id = id
        self.name = name
        self.persona = persona
        self.voice = voice or DEFAULT_VOICE
        self.enabled = bool(enabled)
        self.created_at = created_at
        self.updated_at = updated_at

    def to_dict(self):
        """导出为可 JSON 序列化的字典。"""
        return {
            "id": self.id,
            "name": self.name,
            "persona": self.persona,
            "voice": self.voice,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw):
        """从字典还原 Agent 实例（兼容缺失字段，_load 时兜底）。"""
        return cls(
            id=str(raw["id"]),
            name=raw.get("name", ""),
            persona=raw.get("persona", ""),
            voice=raw.get("voice", DEFAULT_VOICE),
            enabled=raw.get("enabled", True),
            created_at=raw.get("created_at"),
            updated_at=raw.get("updated_at"),
        )

    def __repr__(self):  # pragma: no cover - 仅调试展示
        return f"Agent(id={self.id!r}, name={self.name!r}, enabled={self.enabled})"


class AgentNotFound(Exception):
    """按 id 查找/更新/删除智能体时，目标智能体不存在而抛出的异常。

    API 层捕获该异常转换为 HTTP 404。
    """


#: enabled 字段认可的「真」字符串字面量（大小写不敏感、可含首尾空白）
_TRUE_TOKENS = ("true", "1", "yes")
#: enabled 字段认可的「假」字符串字面量（大小写不敏感、可含首尾空白）
_FALSE_TOKENS = ("false", "0", "", "no")


def _parse_enabled(value):
    """严格解析 enabled 布尔值（M4：禁止宽松 bool() 反转语义）。

    规则：
    - ``bool`` 原样返回；
    - ``str`` 归一化（strip + lower）后命中 {"true","1","yes"} -> True，
      命中 {"false","0","","no"} -> False，其余字符串抛 ``ValueError``；
    - 其他类型一律抛 ``ValueError("invalid enabled value")``。

    :raises ValueError: 非法 enabled 值时抛出。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_TOKENS:
            return True
        if normalized in _FALSE_TOKENS:
            return False
        raise ValueError(f"invalid enabled value: {value!r}")
    raise ValueError(f"invalid enabled value: {value!r}")


class AgentManager:
    """本地智能体（Agent）的 CRUD 管理器。

    - 存储：默认 <项目根>/data/agents.json（可用 path 参数覆盖，便于测试隔离）。
    - 初始化：文件不存在时自动创建空列表并注入默认种子 Agent。
    - 持久化：每次变更全量写回，UTF-8 且 ensure_ascii=False。
    - 线程模型：与 api_server 一致，在主处理线程内串行访问，不加锁。
    """

    def __init__(self, path=None):
        """初始化 AgentManager。

        Args:
            path: agents.json 的绝对路径；None 时用 <项目根>/data/agents.json。
        """
        self._path = path or self._default_path()
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        self._agents = []
        self._load()

    # ------------------------------------------------------------ 路径与存储
    @staticmethod
    def _default_path():
        """推导默认存储路径：<项目根>/data/agents.json。"""
        return os.path.join(_PROJECT_ROOT, "data", "agents.json")

    def _load(self):
        """加载 agents.json；文件不存在则初始化种子并写盘。"""
        if os.path.exists(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    raw_list = json.load(f)
            except (OSError, json.JSONDecodeError):
                raw_list = []
        else:
            raw_list = None

        if raw_list is None:
            # 首次初始化：创建含默认种子的空列表
            self._agents = [self._seed_agent()]
            self._save()
            return

        self._agents = [Agent.from_dict(raw) for raw in raw_list if isinstance(raw, dict)]
        # 兼容已损坏/被外部清空的历史数据：至少保证存在一个可用的 default 锚点
        if not any(a.id == DEFAULT_SEED_ID for a in self._agents):
            self._agents.append(self._seed_agent())
            self._save()

    @staticmethod
    def _seed_agent():
        """构造默认种子 Agent（温柔可靠的赛博伴侣「软软」）。"""
        now = datetime.now().isoformat()
        return Agent(
            id=DEFAULT_SEED_ID,
            name=DEFAULT_SEED_NAME,
            persona=DEFAULT_SEED_PERSONA,
            voice=DEFAULT_VOICE,
            enabled=True,
            created_at=now,
            updated_at=now,
        )

    def _save(self):
        """将全量 Agent 列表以 UTF-8 / ensure_ascii=False 写回 agents.json。"""
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump([a.to_dict() for a in self._agents], f, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------ 查询
    def list(self, enabled=None):
        """返回 Agent 列表。

        Args:
            enabled: True 仅返回启用项，False 仅返回停用项，None 返回全部。
        """
        if enabled is None:
            return list(self._agents)
        return [a for a in self._agents if a.enabled == bool(enabled)]

    def get(self, agent_id):
        """按 id 返回单个 Agent，不存在抛 AgentNotFound。"""
        for a in self._agents:
            if a.id == agent_id:
                return a
        raise AgentNotFound(f"智能体 {agent_id!r} 不存在")

    # ------------------------------------------------------------ 写操作
    @staticmethod
    def _new_id():
        """生成本地唯一 agent id（agent-<8位hex>）。"""
        return f"agent-{uuid.uuid4().hex[:8]}"

    def create(self, name, persona, voice=DEFAULT_VOICE):
        """创建新 Agent 并持久化。

        Args:
            name: 智能体名称（必填）。
            persona: 人设描述（必填）。
            voice: 自定义音色（默认 cx-open）。

        Returns:
            Agent: 新建的智能体实例。
        """
        now = datetime.now().isoformat()
        agent = Agent(
            id=self._new_id(),
            name=name,
            persona=persona,
            voice=voice or DEFAULT_VOICE,
            enabled=True,
            created_at=now,
            updated_at=now,
        )
        self._agents.append(agent)
        self._save()
        return agent

    def update(self, agent_id, **fields):
        """按 id 更新 Agent 的若干字段并持久化。

        仅接受白名单字段（name/persona/voice/enabled），未知字段忽略；
        任意字段更新后都会刷新 updated_at。

        Args:
            agent_id: 目标智能体 id。
            **fields: 可更新字段（name/persona/voice/enabled）。

        Returns:
            Agent: 更新后的智能体实例。

        Raises:
            AgentNotFound: 目标智能体不存在。
            ValueError: enabled 字段非法（M4 严格解析：字符串仅接受
                true/1/yes/false/0/no/空串，其余类型与字面量一律拒绝）。
        """
        agent = self.get(agent_id)
        # 规范化布尔字段（enabled 走严格解析，禁止宽松 bool() 造成 "false" -> True 反转）
        normalized = {}
        for key, value in fields.items():
            if key == "enabled":
                normalized[key] = _parse_enabled(value)
            elif key in ("name", "persona", "voice"):
                normalized[key] = value
            # 其余未知字段忽略，不落盘
        for key, value in normalized.items():
            setattr(agent, key, value)
        agent.updated_at = datetime.now().isoformat()
        self._save()
        return agent

    def delete(self, agent_id):
        """删除指定 Agent 并持久化。

        Args:
            agent_id: 目标智能体 id。

        Raises:
            AgentNotFound: 目标智能体不存在。
        """
        agent = self.get(agent_id)
        self._agents.remove(agent)
        self._save()

    def set_enabled(self, agent_id, enabled):
        """开关指定 Agent 的启用状态并持久化（单独便捷入口）。

        Args:
            agent_id: 目标智能体 id。
            enabled: True 启用 / False 停用。

        Returns:
            Agent: 更新后的智能体实例。
        """
        return self.update(agent_id, enabled=bool(enabled))