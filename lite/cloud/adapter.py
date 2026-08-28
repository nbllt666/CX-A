# -*- coding: utf-8 -*-
"""云端主 LLM 适配层（Task A2）：CloudAdapter。

对齐工程文档 §10.1——``providers=["deepseek","tongyi","openai","moonshot",...]``，
``chat(messages)`` 流式返回文本块，``is_online()`` 检测云端连通性，并提供断网兜底
（本地 LLM）判定所需的产物。

设计要点：
- 基于 ConfigManager 的 ``cloud`` 配置段（provider / api_key / base_url）；
- provider 工厂：四家默认 base_url 映射，均走 OpenAI Chat Completions 兼容协议；
- 底层 HTTP 传输抽象化为 :meth:`CloudAdapter._stream_chat` 与
  :meth:`CloudAdapter._http_get`，测试注入内存 mock transport，避免真实网络；
- 支持 ``reload()`` 在运行时热切换 provider（重新读取配置并重建客户端）。

路径 / 导入规范：本项目一律使用包绝对导入（``from lite.config import ConfigManager``）。
"""

import json
import logging
import urllib.error
import urllib.request
from typing import Dict, Iterator, List, Optional

from lite.config import ConfigManager

#: 原生日志记录器
LOGGER = logging.getLogger(__name__)

#: requests 为可选依赖，未安装时降级到 urllib 标准库实现
try:  # pragma: no cover - 依赖探测
    import requests  # type: ignore

    _HAS_REQUESTS = True
except ImportError:  # pragma: no cover - urllib 回退路径
    _HAS_REQUESTS = False


class CloudConfigError(Exception):
    """云端配置错误。

    触发场景：``cloud.api_key`` 缺失 / 为空，或 ``cloud.provider`` 不在
    CloudAdapter 支持的提供商列表中。抛出后调用方应提示用户检查 config 配置。
    """


class CloudUnavailableError(Exception):
    """云端不可用错误。

    触发场景：向云端 base_url 发起请求时遭遇连接失败、超时或非 2xx 错误等网络
    故障。调用方据此应切换本地兜底 LLM，或提示用户当前离线。
    """


#: 各云端提供商的默认 base_url（均兼容 OpenAI Chat Completions 协议）
PROVIDER_BASE_URLS: Dict[str, str] = {
    "deepseek": "https://api.deepseek.com",
    "openai": "https://api.openai.com/v1",
    "moonshot": "https://api.moonshot.cn/v1",
    "tongyi": "https://dashscope.aliyuncs.com/compatible-mode/v1",
}

#: 各云端提供商的官方缺省模型名（L5：model 缺省不再回退 provider 名——该名会被网关拒绝）。
#: 以代码现有 provider 集合（deepseek/tongyi/openai/moonshot）为准；tongyi 暂无既定
#: 缺省模型，未配置 cloud.model 时不发 model 键并告警。
PROVIDER_DEFAULT_MODELS: Dict[str, str] = {
    "deepseek": "deepseek-chat",
    "openai": "gpt-4o-mini",
    "moonshot": "moonshot-v1-8k",
}


class CloudAdapter:
    """云端主 LLM 适配器（兼容 deepseek / tongyi / openai / moonshot）"""

    #: 支持的提供商列表（工程文档 §10.1）
    providers = list(PROVIDER_BASE_URLS.keys())

    #: 默认模型名。可在构造时以 model 参数覆盖；为空时依次回落 cloud.model 配置
    #: 与 provider 官方缺省映射，均无则请求不带 model 键（附 WARN）。
    DEFAULT_MODEL = ""

    def __init__(self, config_manager: Optional[ConfigManager] = None,
                 model: str = None, transport=None):
        """构造适配器。

        :param config_manager: ConfigManager 实例；缺省时内部自动创建并加载项目根 config.json
        :param model: 可选，云端模型名；优先级：构造参数 > 配置 ``cloud.model`` >
            provider 官方缺省映射（:data:`PROVIDER_DEFAULT_MODELS`）> 不发 model 键
        :param transport: 可选，注入的底层传输对象；测试用内存 mock 以避免真实网络。
            为 None 时使用内置 requests / urllib 传输。
        """
        self._config_manager = config_manager or ConfigManager()
        #: 构造参数显式指定的模型名（最高优先级；reload 后仍保留）
        self._model_override = model
        self._transport = transport
        #: deepseek 等 provider 各自会重新创建底层客户端；热更新重载即重建。
        self._client = None
        self.reload()

    # ------------------------------------------------------------------ #
    # 配置读取与 provider 工厂                                            #
    # ------------------------------------------------------------------ #

    def reload(self) -> None:
        """重新读取 cloud 配置段并重建客户端（热更新支持）。

        切换 provider / 修改 api_key / base_url / temperature / model 后调用本方法
        立即生效，无需重启进程。
        """
        provider = self._config_manager.get("cloud", "provider", "deepseek")
        api_key = self._config_manager.get("cloud", "api_key", "") or ""
        configured_base_url = self._config_manager.get("cloud", "base_url", "") or ""
        temperature = self._config_manager.get("cloud", "temperature", 0.7)
        configured_model = str(self._config_manager.get("cloud", "model", "") or "").strip()

        self._provider = str(provider)
        self._api_key = str(api_key)
        self._configured_base_url = str(configured_base_url).rstrip("/")
        #: 推理温度（配置契约键 cloud.temperature；缺省 0.7 与历史硬编码一致）
        try:
            self._temperature = float(temperature)
        except (TypeError, ValueError):
            LOGGER.warning("cloud.temperature=%r 非法，回落默认 0.7", temperature)
            self._temperature = 0.7
        #: 模型名解析：构造参数 > cloud.model > DEFAULT_MODEL；
        #: 运行期请求体的最终模型名再经 PROVIDER_DEFAULT_MODELS 兜底（见 _stream_chat）
        self._model = self._model_override or configured_model or self.DEFAULT_MODEL
        # base_url 延迟解析并缓存；切换 provider 后需清除缓存
        self._resolved_base_url = None
        # 重建底层客户端——工厂按 provider 选择对应的传输实现
        self._client = self._build_client()

    @classmethod
    def resolve_base_url(cls, provider: str, configured_base_url: str = "") -> str:
        """解析某 provider 的最终 base_url。

        :param provider: 提供商名（deepseek / tongyi / openai / moonshot）
        :param configured_base_url: 用户显式配置的 base_url；为空时取 provider 默认映射
        :return: 归一化后的 base_url（去除末尾斜杠）
        :raises CloudConfigError: 未知 provider 且未配置显式 base_url
        """
        if configured_base_url:
            return configured_base_url.rstrip("/")
        try:
            return PROVIDER_BASE_URLS[provider]
        except KeyError:
            supported = "、".join(cls.providers)
            raise CloudConfigError(
                f"未知的云端提供商：{provider!r}，支持：{supported}"
            ) from None

    def _build_client(self):
        """按当前 provider 创建底层客户端（此处即传输配置的载体）。

        当前实现透传；预留 provider 差异化扩展点（如未来接入非兼容协议）。
        """
        return {"provider": self._provider, "transport": self._transport}

    # ------------------------------------------------------------------ #
    # 配置校验                                                           #
    # ------------------------------------------------------------------ #

    @property
    def provider(self) -> str:
        """当前云端提供商。"""
        return self._provider

    @property
    def api_key(self) -> str:
        """当前云端 API Key。"""
        return self._api_key

    @property
    def base_url(self) -> str:
        """当前云端 base_url（解析后）。"""
        if self._resolved_base_url is None:
            self._resolved_base_url = self.resolve_base_url(
                self._provider, self._configured_base_url
            )
        return self._resolved_base_url

    def _ensure_ready(self) -> str:
        """校验配置并就绪，返回解析后的 base_url。

        :raises CloudConfigError: 无 api_key 或未知 provider
        """
        if not self._api_key:
            raise CloudConfigError(
                f"云端提供商 {self._provider!r} 未配置 api_key（config 的 "
                f"cloud.api_key 为空），请在 config.json 或环境变量 CXA_CLOUD_API_KEY 中配置。"
            )
        return self.base_url

    # ------------------------------------------------------------------ #
    # 公开接口                                                           #
    # ------------------------------------------------------------------ #

    def chat(self, messages: List[Dict]) -> Iterator[str]:
        """流式调用云端主 LLM，逐块产出文本。

        :param messages: OpenAI 兼容消息列表（[{role, content}, ...]）
        :return: 文本块迭代器。生产实现走真实 HTTP SSE；测试注入内存 mock transport。
        :raises CloudConfigError: 无 api_key 或未知 provider
        :raises CloudUnavailableError: 网络 / 云端调用失败
        """
        self._ensure_ready()
        yield from self._stream_chat(messages)

    def is_online(self, timeout: int = 5) -> bool:
        """检测云端连通性（轻量探测，不触发耗时生成）。

        对当前 base_url 发出一个最轻量的 GET 请求；只要能收到响应（无论状态码
        ）即视为在线，仅网络异常返回 False。

        :param timeout: 探测超时秒数，默认 5
        :return: True 在线；False 离线或网络故障
        """
        self._ensure_ready()
        return self._http_get(self.base_url, timeout)

    # ------------------------------------------------------------------ #
    # 底层传输抽象化（测试注入点）                                        #
    # ------------------------------------------------------------------ #

    def _stream_chat(self, messages: List[Dict]) -> Iterator[str]:
        """底层流式传输：向 /chat/completions 发起 SSE 流式请求并产出文本块。

        本方法为传输抽象层，测试通过注入内存 mock transport（monkeypatch 本方法）
        绕过真实网络。生产实现基于 requests（或 urllib 回退）解析 SSE 数据。
        """
        # 允许注入的 transport 对象接管底层传输
        if self._transport is not None:
            yield from self._transport(messages)
            return

        endpoint = self.base_url + "/chat/completions"
        payload = {
            "messages": messages,
            "stream": True,
            "temperature": self._temperature,
        }
        # L5：模型名解析——实例配置优先，provider 官方缺省映射兜底；均无时不发
        # model 键并告警（回退 provider 名会被网关拒绝，历史行为已移除）
        model_name = self._model or PROVIDER_DEFAULT_MODELS.get(self._provider)
        if model_name:
            payload["model"] = model_name
        else:
            LOGGER.warning(
                "云端 provider %r 未配置 cloud.model 且无官方缺省映射，请求不含 model 键，可能被网关拒绝",
                self._provider,
            )
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            for raw_line in self._post_sse(endpoint, payload, headers):
                content = self._parse_sse_content(raw_line)
                if content:
                    yield content
        except CloudUnavailableError:
            raise
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise CloudUnavailableError(
                f"云端流式调用失败（{self._provider} @ {self.base_url}）：{exc}"
            ) from exc

    def _http_get(self, url: str, timeout: int) -> bool:
        """轻量 GET 探测：收到响应即认为连通，连接层异常返回 False。

        N5 语义统一：任何 HTTP 响应（含 4xx/5xx）均说明网关可达 → 在线；
        仅连接层异常（URLError 非 HTTPError / requests 连接错误）→ 离线。
        与 requests 路径（4xx 不抛异常、返回 True）语义对齐。
        """
        if self._transport is not None:
            return bool(self._transport_online(url, timeout))
        try:
            if _HAS_REQUESTS:  # pragma: no cover - 依赖探测分支
                requests.get(url, timeout=timeout)
            else:  # pragma: no cover - urllib 回退
                try:
                    with urllib.request.urlopen(url, timeout=timeout):
                        pass
                except urllib.error.HTTPError:
                    # 4xx/5xx 也是网关响应：可达（在线），与 requests 路径语义对齐
                    return True
            return True
        except Exception:  # noqa: BLE001 - 连接层探测失败视为离线
            return False

    def _transport_online(self, url: str, timeout: int) -> bool:
        """当注入了 transport 对象时，委托其执行连通性探测。"""
        probe = getattr(self._transport, "is_online", None)
        if callable(probe):
            return bool(probe(url, timeout))
        # 传输对象未提供 is_online 时，退回对根地址做流式探测
        return False

    def _post_sse(self, endpoint: str, payload: dict, headers: dict):
        """发起流式 POST，逐行产出原始 SSE 数据行（不含空行）。"""
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=120) as resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line:
                    yield line

    @staticmethod
    def _parse_sse_content(raw_line: str) -> str:
        """从单行 SSE 数据中解析 OpenAI 兼容的 delta.content 文本块。

        形如 ``data: {"choices":[{"delta":{"content":"你"}}]}``；
        ``[DONE]`` 与空 delta 返回空串。
        """
        if not raw_line.startswith("data: "):
            return ""
        data = raw_line[len("data: "):].strip()
        if data == "[DONE]":
            return ""
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            return ""
        choices = chunk.get("choices") or []
        if not choices:
            return ""
        delta = choices[0].get("delta") or {}
        return delta.get("content") or ""