# -*- coding: utf-8 -*-
"""断网兜底机制（Task C3）：OfflineFallbackManager。

对齐工程文档 §10.2——将「云端主 LLM ↔ 本地小 LLM」的通道切换收敛为单一对外入口。
本地模式（local_llm.enabled）开启后为**本地优先**路由（隐私红线：设置页承诺
"不上传任何内容"，本地模式下不得探测网络、不得连云端）：

    本地模式开启 → 完全跳过网络探测，直连本地：
        ├─ 本地运行时就绪 → 切 local，本地小 LLM 单块回复
        └─ 未就绪 → 产 LOCAL_NOT_READY_PROMPT 中文引导提示（绝不静默回落云端）
    本地模式未开启 → 在线探测（CloudAdapter.is_online）：
        ├─ 在线 → 云端主 LLM（主力）；云端调用抛 CloudUnavailableError 自动降级
        └─ 断网 → 本地模式未开启，提示「当前离线，开启本地模式可继续对话」

设计要点：
- **状态机**：{@link OfflineFallbackManager.Mode} 严格为 ``"cloud" | "local"`` 二值。
  - ``mode == "cloud"``：当前由云端主 LLM 承接；
  - ``mode == "local"``：当前由本地小 LLM 承接。
  - 本地模式开启但运行时未就绪时**不切换** state 机（mode 维持 ``"cloud"``），仅产出
    引导提示——因为并未真正进入本地承接；该提示态以独立的状态字符串
    {@link OfflineFallbackManager.status}（``"offline_hint"``）记录，供 UI 显示。
- **本地运行时惰性解析**：``local_llm`` 既支持实例（行为同旧版），也支持**零参
  可调用对象**（resolver，每次 chat 现解析当前运行时，返回 None 表示未加载），
  使运行中开启/加载本地模型无需重建管理器即可动态生效。
- **连接探测节流**：``refresh_online`` 在间隔 {@link OfflineFallbackManager.online_probe_interval}
  内复用缓存探测结果，避免每次 chat 都打真实网络；``force=True`` 可强制重探。
  仅本地模式未开启的云端路径会探测。
- **云端调用失败自动降级**：探测在线但 ``cloud.chat`` 真正抛
  {@link CloudUnavailableError} 时，与离线分支同一逻辑降级（切本地或提示）。
- **模式变化事件**：``add_listener`` 注册的回调在模式真正切换时收到新模式字符串；
  同时以字符串写入 {@link OfflineFallbackManager.mode_history} 与
  {@link OfflineFallbackManager.last_mode_event}，供 UI 状态显示与调试回溯。

路径 / 导入规范：本项目一律使用包绝对导入（``from lite.cloud.adapter import ...``）。
"""

import logging
import time
from typing import Callable, Iterator, List, Optional

from lite.cloud.adapter import CloudAdapter, CloudConfigError, CloudUnavailableError

#: 原生日志记录器
LOGGER = logging.getLogger(__name__)

#: 离线提示文案（工程文档 §10.2 与 checklist 验收口径一致）
OFFLINE_PROMPT = "当前离线，开启本地模式可继续对话"

#: 云端配置错误诊断态下的提示文案（N5：未配置 API Key 不得误诊为"断网"）
CONFIG_ERROR_PROMPT = "云端配置未完成，请在设置中填写服务商与 API Key"

#: 本地模式未就绪引导文案（本地优先路由）：本地模式开启但模型未下载/未加载完成
#: 时的中文引导提示——语义是"去把本地大脑下载好"，绝非"断网"，且绝不静默回落
#: 云端（设置页承诺"不上传任何内容"，隐私红线）。
LOCAL_NOT_READY_PROMPT = "本地大脑还没准备好，先去下载好它（设置页或新手引导里可以下）"

#: mode_history 保留的尾部条数上限（第四轮体检批次C：防长驻进程无界增长）
_MAX_MODE_HISTORY = 100


def _llama_runtime_types():
    """惰性解析 LlamaRuntime / LlamaNotReady 类型（避免 fallback <-> runtime 循环导入）。

    运行期仅在需要类型注解 / 异常判断时才导入；导入失败返回 ``(None, RuntimeError)`` 兜底，
    保证本模块在 runtime 子包尚未完成初始化时也可被加载。
    """
    try:
        from lite.runtime.llama_runtime import LlamaNotReady, LlamaRuntime

        return LlamaRuntime, LlamaNotReady
    except Exception:  # noqa: BLE001 - 循环导入兜底
        return None, RuntimeError

#: 有效模式集合（状态机仅允许 "cloud" / "local" 二值）
_VALID_MODES = ("cloud", "local")

#: is_online 探测可注入的回调签名：``() -> bool``
OnlineCheck = Callable[[], bool]


class OfflineFallbackManager:
    """断网兜底管理器：本地模式优先直连本地、云端模式在线走云的统一对外入口。

    Args:
        cloud: CloudAdapter 实例。调用其 ``chat``（流式）与 ``is_online``（探测）。
            本地模式开启时**两者均不被调用**（隐私红线：不探测、不连云）。
        local_llm: 可选的本地小 LLM 运行时，支持两种形态：
            - 实例（如 LlamaRuntime）：行为同旧版，chat 时直接调用其
              ``offline_chat``；
            - 零参可调用对象（resolver）：每次 chat 时惰性调用取得**当前**运行时
              实例，返回 None 表示未加载——运行中开启本地模式 / 模型后台加载完成
              无需重建管理器即可动态生效。
            缺省为 None（此时即使开启本地模式也产出引导提示文案）。
        config: 可选的配置来源，支持三种形态：
            - ``None``：使用默认配置意向（local_llm.enabled 视为 False）；
            - ``dict``：形如 ``{"local_llm": {"enabled": True}}`` 嵌套字典；
            - ``ConfigManager`` 实例（具备 ``get(section, key, default)`` 接口）。
        online_probe_interval: 在线探测节流间隔（秒），默认 30。仅云端路径探测。
        online_check: 可选的在线探测回调（``() -> bool``）；缺省为
            ``cloud.is_online(timeout=5)`` 的包装。测试注入以模拟网络。
    """

    Mode = frozenset(_VALID_MODES)

    def __init__(
        self,
        cloud: CloudAdapter,
        local_llm=None,
        config=None,
        online_probe_interval: float = 30.0,
        online_check: Optional[OnlineCheck] = None,
    ) -> None:
        if cloud is None:
            raise TypeError("cloud 不能为 None，请传入 CloudAdapter 实例。")
        self._cloud = cloud
        self._local_llm = local_llm
        self._config = config
        self._online_probe_interval = float(online_probe_interval)

        #: 在线探测回调：缺省包装 cloud.is_online(timeout=5)，可注入覆盖。
        self._online_check: OnlineCheck = (
            online_check if callable(online_check) else self._default_online_check
        )

        #: 当前状态机模式（严格二值："cloud" | "local"）。
        self._mode: str = "cloud"
        #: 在线探测缓存与时间戳（节流用）。
        self._online_cache: Optional[bool] = None
        self._last_probe_time: Optional[float] = None
        #: 模式切换监听器列表（回调签名：``listener(new_mode: str)``）。
        self._listeners: List[Callable[[str], None]] = []

        #: 状态写入（供 UI 显示）：cloud / local / offline_hint。
        self.status: str = "cloud"
        #: 模式变化事件字符串列表（含时间戳，供 UI / 调试回溯）。
        self.mode_history: List[str] = []
        #: 最近一次模式变化事件字符串。
        self.last_mode_event: Optional[str] = None
        #: 云端配置错误诊断态（N5）：探测捕获 CloudConfigError 时记录其文本，
        #: 离线产出分支据此区分"断网"与"配置未完成"；其它探测异常清 None。
        self._config_error: Optional[str] = None

    # ------------------------------------------------------------------ #
    # 内部：在线探测 / 配置读取                                           #
    # ------------------------------------------------------------------ #

    def _default_online_check(self) -> bool:
        """缺省在线探测：委托 cloud.is_online(timeout=5)。"""
        return bool(self._cloud.is_online(timeout=5))

    def _read_cfg(self, section: str, key: str, default):
        """从多种 config 形态中读取 ``(section, key)``，缺失返回 default。"""
        cfg = self._config
        if cfg is None:
            return default
        # ConfigManager 形态（具备 get(section, key, default)）
        if hasattr(cfg, "get") and not isinstance(cfg, dict):
            try:
                return cfg.get(section, key, default)
            except TypeError:
                pass
        # dict 形态（嵌套字典）
        sec = cfg.get(section) if isinstance(cfg, dict) else None
        if isinstance(sec, dict) and key in sec:
            return sec.get(key, default)
        return default

    # ------------------------------------------------------------------ #
    # 状态机：模式读写 / 监听                                             #
    # ------------------------------------------------------------------ #

    @property
    def mode(self) -> str:
        """当前状态机模式：``"cloud"``（云端）或 ``"local"``（本地兜底）。"""
        return self._mode

    def add_listener(self, listener: Callable[[str], None]) -> None:
        """注册模式切换监听器（回调收到新模式字符串）。

        Args:
            listener: 可调用对象，签名 ``listener(new_mode: str)``。仅当模式
                真正变化时才被调用。
        """
        if callable(listener) and listener not in self._listeners:
            self._listeners.append(listener)

    def change_mode(self, mode: str) -> None:
        """切换状态机模式，并在真正变化时通知监听者 / 记录事件字符串。

        Args:
            mode: 目标模式，仅允许 ``"cloud"`` 或 ``"local"``。
        Raises:
            ValueError: mode 不在合法集合内。
        """
        if mode not in _VALID_MODES:
            raise ValueError(f"非法模式：{mode!r}，仅允许 {_VALID_MODES}")
        if mode == self._mode:
            return
        self._mode = mode
        # 状态写入：模式变化以字符串记录（供 UI / GN-004 回溯）；
        # 第四轮体检批次C：仅保留尾部 _MAX_MODE_HISTORY 条，防长驻进程无界增长
        self.last_mode_event = f"{mode}"
        self.mode_history.append(self.last_mode_event)
        if len(self.mode_history) > _MAX_MODE_HISTORY:
            del self.mode_history[: len(self.mode_history) - _MAX_MODE_HISTORY]
        for listener in list(self._listeners):
            listener(mode)

    def refresh_online(self, force: bool = False) -> bool:
        """探测云端连通性（带节流缓存）。

        距上次探测时长 ≥ {@link online_probe_interval}、或 ``force=True`` 时才
        真实调用在线探测回调；否则复用缓存结果，避免频繁打网络。

        Args:
            force: 为 True 时忽略节流，强制重新探测。
        Returns:
            bool: True 在线；False 离线或探测异常。
        """
        now = time.monotonic()
        if not force and self._last_probe_time is not None:
            elapsed = now - self._last_probe_time
            if elapsed < self._online_probe_interval:
                return bool(self._online_cache)
        try:
            online = bool(self._online_check())
            # 探测成功：清除历史配置错误诊断态
            self._config_error = None
        except CloudConfigError as exc:
            # N5：配置错误（如未配置 API Key）不是断网，置配置错误诊断态供提示分支区分
            online = False
            self._config_error = str(exc)
            LOGGER.warning("云端配置错误，探测按未就绪处理：%s", exc)
        except Exception:  # noqa: BLE001 - 连接层探测异常一律视为离线
            online = False
            self._config_error = None
        self._online_cache = online
        self._last_probe_time = time.monotonic()
        return online

    def local_mode_enabled(self) -> bool:
        """本地模式是否开启（读 config ``local_llm.enabled``，默认 False）。

        Returns:
            bool: True 已开启本地模式；False 未开启。
        """
        return bool(self._read_cfg("local_llm", "enabled", False))

    def _resolve_local_llm(self):
        """解析当前本地小 LLM 运行时（实例 / resolver 两形态收敛口）。

        - callable 形态：每次调用现解析（返回 None 表示未加载）；解析异常按
          未加载处理（返回 None），不向 chat 路径渗漏。
        - 实例 / None 形态：原样返回（兼容既有调用方）。
        """
        if callable(self._local_llm):
            try:
                return self._local_llm()
            except Exception:  # noqa: BLE001 - resolver 异常一律视为未加载
                LOGGER.warning("本地运行时 resolver 解析异常，按未加载处理", exc_info=True)
                return None
        return self._local_llm

    def local_llm_ready(self) -> bool:
        """本地小 LLM 是否就绪（本地优先路由的就绪判定）。

        判定口径：解析当前运行时（实例直取 / resolver 现调），非 None 即视为
        就绪；若运行时暴露 ``is_ready`` / ``model_ready`` 属性，则该属性须为真。
        解析异常（含 resolver 抛错）一律视为未就绪。

        Returns:
            bool: True 本地运行时就绪；False 未加载 / 未就绪 / 解析异常。
        """
        runtime = self._resolve_local_llm()
        if runtime is None:
            return False
        for attr in ("is_ready", "model_ready"):
            flag = getattr(runtime, attr, None)
            if flag is not None and not flag:
                return False
        return True

    # ------------------------------------------------------------------ #
    # 兜底：本地回复 / 离线提示                                           #
    # ------------------------------------------------------------------ #

    def _try_local_chat(self, messages) -> Optional[str]:
        """尝试用本地小 LLM 产出回复，失败返回 None（不抛）。

        Args:
            messages: OpenAI 兼容消息列表。
        Returns:
            Optional[str]: 本地小 LLM 产出的纯文本；未就绪 / 缺失 / 异常时 None。
        """
        runtime = self._resolve_local_llm()
        if runtime is None:
            return None
        try:
            return runtime.offline_chat(messages)
        except Exception:  # noqa: BLE001 - 兜底失败（含 LlamaNotReady）不崩溃
            return None

    def _offline_prompt(self) -> str:
        """按诊断态产出离线提示文案（N5）：有配置错误时提示配置未完成，否则提示断网。"""
        if self._config_error:
            return CONFIG_ERROR_PROMPT
        return OFFLINE_PROMPT

    def _fallback_like_offline(self, messages) -> Iterator[str]:
        """离线（或云端调用失败）降级分支：切本地兜底，否则产提示。"""
        if self.local_mode_enabled():
            self.change_mode("local")
            text = self._try_local_chat(messages)
            if text is None:
                # 本地模型未就绪 / 缺失 → 提示而不抛出
                self.status = "offline_hint"
                yield self._offline_prompt()
                return
            self.status = "local"
            yield text
            return
        # 本地模式未开启：不切换状态机（mode 保持 cloud），仅产提示
        # （N5：按诊断态区分"断网"与"云端配置未完成"文案）
        self.status = "offline_hint"
        yield self._offline_prompt()

    def _cloud_stream(self, messages) -> Iterator[str]:
        """在线分支：透传云端流式；遇到 CloudUnavailableError 自动降级。

        G-3 修复：已产出过 chunk 的中途故障不再追加离线提示——保留已产出的
        半截真实回复即止，避免"半截回复+离线提示"混合文本以 logged=True 污染
        对话历史；仅在尚未产出任何 chunk 时才走降级分支。
        """
        yielded = False
        try:
            self.status = "cloud"
            for chunk in self._cloud.chat(messages):
                yielded = True
                yield chunk
        except CloudUnavailableError:
            if yielded:
                # 中途故障：结束流即可，不追加降级文本（不污染多轮上下文）
                LOGGER.warning("云端流式调用中途故障，保留已产出内容并结束流")
                return
            # 尚未产出任何 chunk：与离线一致的自动降级
            yield from self._fallback_like_offline(messages)

    # ------------------------------------------------------------------ #
    # 公开入口                                                           #
    # ------------------------------------------------------------------ #

    def _local_mode_stream(self, messages) -> Iterator[str]:
        """本地模式分支（本地优先路由）：完全不探测网络、不连云端。

        - 运行时就绪 → 切 local、本地单块产出；本地调用中途失败返回 None 时
          产出 {@link LOCAL_NOT_READY_PROMPT}（本地模式下的语义不再是"断网"，
          且绝不静默回落云端）。
        - 未就绪 → 不切换状态机（mode 保持 cloud），产出引导提示文案。
        """
        if not self.local_llm_ready():
            # 未就绪：不切换状态机（未真正进入本地承接），仅产引导提示
            self.status = "offline_hint"
            yield LOCAL_NOT_READY_PROMPT
            return
        self.change_mode("local")
        text = self._try_local_chat(messages)
        if text is None:
            # 就绪判定通过但本地调用中途失败：产引导提示，不抛、不回落云端
            self.status = "offline_hint"
            yield LOCAL_NOT_READY_PROMPT
            return
        self.status = "local"
        yield text

    def chat(self, messages) -> Iterator[str]:
        """对外统一对话入口（流式，本地优先路由）。

        路由逻辑：
        1. 本地模式开启（``local_llm.enabled``）→ **完全不调用 ``refresh_online``**
           （不探测网络、不连云端，隐私红线）：
           - 本地运行时就绪 → 切 local，走 ``offline_chat`` 单块产出；
             中途失败 → 产 {@link LOCAL_NOT_READY_PROMPT}（不抛、不回落云端）；
           - 未就绪 → 产 {@link LOCAL_NOT_READY_PROMPT} 引导提示（mode 保持 cloud）；
        2. 本地模式未开启 → 先 ``refresh_online`` 探测在线状态：
           - 在线 → 若处于本地兜底模式则自动恢复切回云端，再透传 ``cloud.chat``
             流式；探测在线但调用抛 CloudUnavailableError 时自动降级；
           - 离线 → 产离线提示文案（mode 保持 cloud；若探测到在线但云端故障的
             降级路径中本地模式已被中途开启，则切本地兜底）。

        Args:
            messages: OpenAI 兼容消息列表（[{role, content}, ...]）。
        Returns:
            Iterator[str]: 文本块迭代器。
        """
        if self.local_mode_enabled():
            # 本地优先：不探测、不连云，直连本地（未就绪给引导提示，绝不回落云端）
            yield from self._local_mode_stream(messages)
            return
        online = self.refresh_online()
        if online:
            # 网络可用：若停留在本地兜底模式，自动切回云端并通知监听者
            if self._mode == "local":
                self.change_mode("cloud")
            yield from self._cloud_stream(messages)
            return
        # 离线：切本地兜底或产提示
        yield from self._fallback_like_offline(messages)