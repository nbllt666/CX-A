# -*- coding: utf-8 -*-
"""CX-A 配置系统：config.json 加载 / CXA_ 环境变量覆盖 / 缺失字段自动补全 / 热更新判定 / API Key 加密存储。

对齐工程文档 §13（配置系统）与 §13.2（主要配置段）的默认值定义。

- 路径规范：本项目所有路径均基于
  ``os.path.dirname(os.path.abspath(__file__))`` 逐级向上推导项目根，
  禁止使用相对路径或字符串斜杠拼接。
- 本项目默认配置根 ``config.json`` 位于项目根目录，
  密钥文件 ``data/.cxa_key`` 位于数据目录。
"""

import json
import os

try:  # cryptography 为可选依赖
    from cryptography.fernet import Fernet

    _HAS_FERNET = True
    _FERNET_IMPORT_ERROR = None
except ImportError as _exc:  # pragma: no cover - 依赖缺失降级路径
    _HAS_FERNET = False
    _FERNET_IMPORT_ERROR = str(_exc)

#: 环境变量前缀
_ENV_PREFIX = "CXA_"

#: 加密值在 config.json 中的标记前缀，用于加载时识别并解密
_ENC_PREFIX = "cxa_enc:"

#: 密钥文件相对数据目录的文件名
_KEY_FILENAME = ".cxa_key"

#: 全局默认配置。对应工程文档 §13.2「主要配置段」
DEFAULTS = {
    #: temperature/model 为云端推理参数契约键（L5 收口：消除 adapter 硬编码违约）
    "cloud": {
        "provider": "deepseek",
        "api_key": "",
        "base_url": "",
        "temperature": 0.7,
        "model": "",
    },
    # device：推理设备开关（"cpu"(默认)/"gpu"）。gpu 时 LlamaRuntime 以
    # n_gpu_layers=-1 全层卸载（可用 local_llm.n_gpu_layers 高级覆盖）。
    "local_llm": {
        "enabled": False,
        "model_path": "",
        "source": "modelscope",
        "device": "cpu",
    },
    "embedding": {"model": "qwen3-embedding:0.6b", "runtime": "llama.cpp", "device": "cpu"},
    "vector": {"backend": "lancedb", "path": "data/lancedb"},
    "tts": {"engine": "melotts", "voice": "cx-open"},
    "asr": {"engine": "sensevoice", "device": "cpu"},
    "vad": {"mode": "webrtc"},
    "memory": {"max_memories": 30, "dedup": 0.85, "permanent_threshold": 0.95},
    "computer_control": {"authorized": False, "confirm_dangerous": True},
    "sync": {"enabled": False},
    "remote": {"endpoint": "", "enabled": False},
    #: 轻量版 ACP（补充文档 §6.1）：多 Agent 协作 / 心跳 / 可选局域网发现 / 云端中转
    "acp": {
        "enabled": False,
        "agent_id": "cxa-agent-001",
        "heartbeat_interval": 10,
        "lan_discovery": False,
        "group_enabled": False,
        "cloud_relay": True,
        "cloud_relay_endpoint": "",
    },
    #: 轻量版 CXFC（补充文档 §6.2）：极简 embedded-only 插件注册（默认关，用内置工具系统）
    "cxfc": {"enabled": False, "embedded_only": True},
    #: 内置工具系统（补充文档 §6.3）：电脑控制 / 记忆读写 / 系统信息
    "tools": {"computer_control": False, "memory_tools": True, "system_tools": True},
}

#: 热更新段：切换后即时生效，无需重启（工程文档 §13.3）
HOT_RELOAD_SECTIONS = (
    "cloud",
    "local_llm",
    "tts",
    "asr",
    "vad",
    "memory",
    "computer_control",
    "sync",
    "remote",
    "acp",
    "cxfc",
    "tools",
)

#: 需重启段：变更后必须重启进程（工程文档 §13.3，向量库路径 / 运行时配置）
NEED_RESTART_SECTIONS = ("vector", "embedding")


def _derive_project_root():
    """推导项目根目录。

    本文件位于 ``<root>/lite/config/config_manager.py``，
    逐级向上取三次 dirname 即得到项目根（c:\\CX-A）。
    """
    _config_dir = os.path.dirname(os.path.abspath(__file__))  # <root>/lite/config
    _lite_dir = os.path.dirname(_config_dir)                  # <root>/lite
    return os.path.dirname(_lite_dir)                         # <root>


def _deep_merge(defaults, current):
    """将 defaults 递归补齐到 current 中缺失的段与键，不覆盖已有值。

    - 缺失的段：整段从 defaults 补齐
    - 缺失的键：单个键从 defaults 补齐
    - 已有值：保持不变（含显式用户自定义）
    """
    merged = dict(current)
    for section, default_value in defaults.items():
        if not isinstance(default_value, dict):
            if section not in merged:
                merged[section] = default_value
            continue
        if section not in merged or not isinstance(merged.get(section), dict):
            merged[section] = dict(default_value)
            continue
        for key, value in default_value.items():
            if key not in merged[section]:
                merged[section][key] = value
    return merged


def _coerce_env_value(raw, default):
    """依据默认值类型，将环境变量字符串转换为对应类型。

    - bool：'true'/'1' -> True；'false'/'0' -> False
    - int/float：尝试数值转换，失败保留字符串
    - 其余：原样字符串
    """
    if isinstance(default, bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return raw
    if isinstance(default, float):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return raw
    return str(raw)


class ConfigManager:
    """CX-A 全局配置管理器。

    职责：
    1. 首次启动（config.json 不存在）自动生成含默认值的 config.json；
    2. 支持 ``CXA_`` 前缀环境变量覆盖（下划线路径映射为嵌套键）；
    3. 加载时用默认值补齐缺失段与缺失键；
    4. 提供热更新段判定（reloadable）；
    5. 对 cloud.api_key 做 Fernet 加密存储，降级明文时置 warning 标记。
    """

    def __init__(self, config_path=None, data_dir=None):
        """初始化配置管理器。

        :param config_path: config.json 绝对路径；缺省为项目根 config.json
        :param data_dir: 数据目录绝对路径；缺省为项目根 data/
        """
        self.project_root = _derive_project_root()
        self.config_path = config_path or os.path.join(self.project_root, "config.json")
        self.data_dir = data_dir or os.path.join(self.project_root, "data")

        #: 加载/解密过程中的降级与风险提示（如 API Key 明文存储）。
        self.warnings = []
        self._fernet_cache = None

        self._ensure_config_file()
        self._config = self._read_config()
        self._config = _deep_merge(DEFAULTS, self._config)
        self._apply_env_overrides()

    # ------------------------------------------------------------------ #
    # 内部：项目根 / 密钥 / 文件 IO                                        #
    # ------------------------------------------------------------------ #

    def _ensure_config_file(self):
        """首次启动时自动生成含默认值的 config.json。"""
        if os.path.exists(self.config_path):
            return
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as fh:
            json.dump(DEFAULTS, fh, ensure_ascii=False, indent=2)

    def _load_or_create_key(self):
        """读取数据目录下的密钥文件；不存在则以 Fernet 生成并落盘。

        :return: Fernet 密钥字符串（UTF-8 解码后）
        """
        key_path = self._key_path
        if os.path.exists(key_path):
            with open(key_path, "r", encoding="utf-8") as fh:
                return fh.read().strip()
        os.makedirs(self.data_dir, exist_ok=True)
        key = Fernet.generate_key().decode("utf-8")
        with open(key_path, "w", encoding="utf-8") as fh:
            fh.write(key)
        return key

    @property
    def _key_path(self):
        """密钥文件绝对路径（数据目录下 .cxa_key）。"""
        return os.path.join(self.data_dir, _KEY_FILENAME)

    def _get_fernet(self):
        """惰性构造 Fernet 实例（带缓存）。"""
        if self._fernet_cache is None:
            self._fernet_cache = Fernet(self._load_or_create_key().encode("utf-8"))
        return self._fernet_cache

    def _encrypt(self, value):
        """对明文 api_key 加密，返回可写入 config.json 的字符串。

        加密失败或 cryptography 不可用时降级明文，并在 warnings 中置标记。
        """
        if not value:
            return ""
        if not _HAS_FERNET:
            self.warnings.append("cryptography 不可用，API Key 已降级为明文存储")
            return value
        try:
            token = self._get_fernet().encrypt(value.encode("utf-8")).decode("utf-8")
            return _ENC_PREFIX + token
        except Exception as exc:  # noqa: BLE001 - 任何加密异常均降级明文
            self.warnings.append(f"API Key 加密失败（{exc}），已降级为明文存储")
            return value

    def _decrypt_in_place(self, raw):
        """解密 config.json 中已加密（cxa_enc: 前缀）的 cloud.api_key。

        按引用就地改写 raw 中的明文，防止重复解密。
        """
        section = raw.get("cloud")
        if not isinstance(section, dict):
            return
        api_key = section.get("api_key", "")
        if not isinstance(api_key, str) or not api_key.startswith(_ENC_PREFIX):
            return
        token = api_key[len(_ENC_PREFIX):]
        if not _HAS_FERNET:
            self.warnings.append("cryptography 不可用，无法解密已加密的 API Key")
            return
        try:
            plain = self._get_fernet().decrypt(token.encode("utf-8")).decode("utf-8")
            section["api_key"] = plain
        except Exception as exc:  # noqa: BLE001 - 密钥错配等情况
            self.warnings.append(f"API Key 解密失败（{exc}），已保留原样")

    def _read_config(self):
        """读取 config.json 并就地解密 api_key，返回原始 dict。"""
        if not os.path.exists(self.config_path):
            raw = json.loads(json.dumps(DEFAULTS))
        else:
            with open(self.config_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        self._decrypt_in_place(raw)
        return raw

    # ------------------------------------------------------------------ #
    # 内部：环境变量覆盖                                                  #
    # ------------------------------------------------------------------ #

    def _parse_env_name(self, suffix):
        """将去掉 CXA_ 前缀后的环境变量名解析为 (section, key)。

        采用「最长已知段前缀」匹配，支持段名含下划线（local_llm /
        computer_control）：以 DEFAULTS 中已知段名（按长度降序）试探，
        匹配 ``段名_`` 前缀，剩余部分即键名。
        """
        lowered_suffix = suffix.lower()
        for section in sorted(DEFAULTS.keys(), key=len, reverse=True):
            prefix = section.lower() + "_"
            if lowered_suffix.startswith(prefix):
                key = lowered_suffix[len(prefix):]
                return section, key
        parts = lowered_suffix.split("_", 1)
        if len(parts) == 2:
            return parts[0], parts[1]
        return lowered_suffix, ""

    def _apply_env_overrides(self):
        """以 CXA_ 前缀环境变量覆盖内存配置。"""
        for env_name, env_value in os.environ.items():
            if not env_name.startswith(_ENV_PREFIX):
                continue
            suffix = env_name[len(_ENV_PREFIX):]
            section, key = self._parse_env_name(suffix)
            if section not in self._config or not key:
                continue
            default_value = DEFAULTS.get(section, {}).get(key)
            self._config[section][key] = _coerce_env_value(env_value, default_value)

    # ------------------------------------------------------------------ #
    # 公开接口                                                            #
    # ------------------------------------------------------------------ #

    @property
    def config(self):
        """返回当前内存配置的浅包装（只读建议）。"""
        return self._config

    def get(self, section, key, default=None):
        """读取配置项。（section, key）不存在时返回 default。"""
        section_map = self._config.get(section)
        if not isinstance(section_map, dict):
            return default
        return section_map.get(key, default)

    def set(self, section, key, value, reloadable=True):
        """写入配置项并立即生效（内存层面）。

        :param reloadable: 标记本次写入是否需要热更新。为 True 且属于
            需重启段（vector / embedding）时仅记录，不做其他动作；
            本骨架阶段不做进程内重载，交由调用方自行决定重启。
        """
        if section not in self._config or not isinstance(self._config[section], dict):
            self._config[section] = {}
        self._config[section][key] = value

    def save(self):
        """将当前内存配置（api_key 加密后）持久化到 config.json。"""
        payload = json.loads(json.dumps(self._config))
        api_key = payload.get("cloud", {}).get("api_key", "")
        payload["cloud"]["api_key"] = self._encrypt(api_key)
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

    def reloadable(self, key):
        """判断配置段/键是否可热更新（无需重启）。

        :param key: 配置段名（如 'cloud' / 'vector'）
        :return: True 热更新即时生效；False 需重启；None 未知段
        """
        if key in HOT_RELOAD_SECTIONS:
            return True
        if key in NEED_RESTART_SECTIONS:
            return False
        return None