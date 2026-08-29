# -*- coding: utf-8 -*-
"""远端 CX-O 遥控（Task E3）——RemoteController 基于 HTTP 转发至 CX-A 管理面。

透过 lite/config/config_manager.py 的 ``remote`` 配置段（endpoint / enabled，
默认未启用）驱动本控制器，向远端 CX-O 管理面暴露的状态监控 / 控制 / 配置下发
端点发起请求：

- GET  {endpoint}/api/admin/status       读取远端状态快照
- POST {endpoint}/api/admin/control      下发单条控制指令（enable/disable/pause）
- POST {endpoint}/api/admin/config       下发配置补丁

传输层默认为基于 urllib.request 的 HTTP transport；测试可注入 mock transport，
以隔离真实网络依赖。功能未启用（remote.enabled=false）时，所有操作抛
RemoteDisabled；远端不可达（超时 / 连接失败）抛 RemoteUnreachable；远端返回
非 2xx 抛 RemoteError。

路径规范：本文件路径推导仅使用 os.path.dirname(os.path.abspath(__file__))，
禁止相对路径；配置依赖由注入的 ConfigManager 或等价 object 提供。

架构预留（Task F4）：
    远端控制本期仅走局域网 endpoint（config remote.endpoint），不下发公网。
    为远端重度遥控预留的公网接入方向为：TLS + 令牌 + request_id 防重放 +
    隧道（TLS/tunnel），鉴权要求对齐 CX-O 管理文档。传输抽象已提升为公开基类
    RemoteTransport，本期仅提供 HTTPRemoteTransport（局域网默认实现）；公网信道
    （TLS/隧道）transport 后续在该基类下新增。RemoteController 预留
    public_endpoint 存取接口（仅存储 URL，不做连接/认证），属 P2 后置占位，
    不影响本期局域网既有行为。
"""

import http.client
import json
import urllib.error
import urllib.request

from lite.config.config_manager import ConfigManager


class RemoteDisabled(Exception):
    """远端遥控功能未启用（config remote.enabled=false）时抛出的异常。

    API 层捕获该异常转换为 HTTP 503。
    """


class RemoteUnreachable(Exception):
    """远端 CX-O 管理面不可达（超时 / 连接失败 / 网络错误）时抛出的异常。

    API 层捕获该异常转换为 HTTP 504。
    """


class RemoteError(Exception):
    """远端返回非 2xx 响应（未授权 / 服务内部错误等）时抛出的异常。"""


class RemoteTransport:
    """统一远端传输接口（公开基类，Task F4 架构预留）。

    本期仅提供 HTTPRemoteTransport（局域网默认实现）；面向远端重度遥控的
    公网信道（TLS/隧道）transport 后续在本基类下新增。基类 request 为抽象
    占位，直接调用抛 NotImplementedError；具体 transport 必须实现该方法。

    公网接入方向（预留，见模块 docstring）：TLS + 令牌 + request_id 防重放 +
    隧道，鉴权要求对齐 CX-O 管理文档。
    """

    def request(self, method, path, body=None):
        """发起一次远端请求并返回解析后的 JSON dict（抽象方法）。

        Args:
            method: HTTP 方法（GET / POST / ...）。
            path: 请求路径；对 HTTP 实现即完整 URL（endpoint + path）。
            body: 可选的 JSON 请求体（dict 或 None）。

        Returns:
            dict: 解析后的 JSON 响应。

        Raises:
            NotImplementedError: 基类抽象占位，必须由具体 transport 实现。
        """
        raise NotImplementedError(
            "RemoteTransport.request 是抽象方法，请注入/使用具体 transport "
            "（如 HTTPRemoteTransport）"
        )


class HTTPRemoteTransport(RemoteTransport):
    """基于 urllib.request 的默认 HTTP transport（局域网，Task E3 迁入）。

    继承公开基类 RemoteTransport，保持既有默认行为不变：仅负责发起请求并返回
    解析后的 JSON dict；网络错误（非 2xx / 连接失败 / 超时）以原始 urllib 异常
    向上抛出，由 RemoteController._request 统一转换为 RemoteError /
    RemoteUnreachable。
    """

    def request(self, method, url, json_body=None, timeout=10):
        """发起一次 HTTP 请求并返回解析后的 JSON（覆盖基类抽象方法）。

        Args:
            method: HTTP 方法（GET / POST 等）。
            url: 完整请求 URL（endpoint + path）。
            json_body: 可选的 JSON 请求体（None 表示无体）。
            timeout: 请求超时秒数。

        Returns:
            dict: 解析后的 JSON 响应；响应体非 JSON 时返回 {"raw": <body>}。

        Raises:
            urllib.error.HTTPError: 远端返回非 2xx。
            urllib.error.URLError / OSError / TimeoutError: 连接失败或超时。
        """
        data = None
        headers = {}
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}


class RemoteController:
    """远端 CX-O 遥控控制器。

    职责：
    1. 从 config ``remote`` 段读取 endpoint / enabled；
    2. get_status 调用远端状态监控端点，原样透传返回的 JSON 字段；
    3. control 下发启用 / 禁用 / 暂停控制指令；
    4. push_config 下发配置补丁；
    5. _request 统一封装 HTTP 请求、JSON 解析与异常映射。
    """

    #: 合法控制动作枚举（对齐 CX-O /control 的 enable / disable / pause）
    ACTIONS = ("enable", "disable", "pause")

    def __init__(self, config=None, transport=None):
        """初始化远端遥控控制器。

        Args:
            config: 配置管理器（提供 .get(section, key, default)）；None 时用
                默认 ConfigManager（remote.enabled 默认 false）。
            transport: HTTP 传输对象（提供 request(method, url, json_body, timeout)）；
            None 时用基于 urllib.request 的默认 transport。测试可注入 mock
            transport 以便隔离网络。
        """
        self._config = config if config is not None else ConfigManager()
        self._transport = (
            transport if transport is not None else HTTPRemoteTransport()
        )
        #: 远端 CX-O 管理面基础地址（去掉尾部斜杠，便于与 path 拼接）
        self.endpoint = str(self._config.get("remote", "endpoint") or "").rstrip("/")
        #: 是否启用远端遥控（默认 false；未启用时操作抛 RemoteDisabled）
        self.enabled = bool(self._config.get("remote", "enabled", False))
        #: 公网遥控预留端点（Task F4，P2 后置；None 表示未配置）
        #: 本期仅存储 URL，不做连接/认证，不影响局域网 endpoint 既有行为。
        self.public_endpoint = None
        #: M-16（第三轮体检批次4）：明文 HTTP 告警是否已发出（仅告警一次）
        self._plain_http_warned = False

    # ------------------------------------------------------------------ #
    # 内部：启用判定 / 统一请求封装                                        #
    # ------------------------------------------------------------------ #

    def _check_enabled(self):
        """功能未启用时抛 RemoteDisabled（其余调用方不可见）。"""
        if not self.enabled:
            raise RemoteDisabled("远端遥控未启用（config remote.enabled=false）")

    def _warn_plain_http_once(self):
        """endpoint 为 http:// 明文传输时打印一次性显著安全告警（M-16）。

        本期传输层无认证头、不强制 TLS 为 F4 预留的设计取舍；启用态 + 明文
        HTTP 的组合下控制指令与配置补丁可被局域网嗅探/伪造，至少显式告警
        提示用户，不静默。
        """
        if self._plain_http_warned:
            return
        if self.endpoint.lower().startswith("http://"):
            print(
                f"[WARN] 远端遥控 endpoint 为明文 HTTP（{self.endpoint}）："
                "控制指令与配置补丁可被局域网嗅探/伪造，建议尽快切换 HTTPS（TLS+令牌为 F4 预留）"
            )
        self._plain_http_warned = True

    def _request(self, method, path, json_body=None, timeout=10):
        """统一 HTTP 请求封装：拼接 URL、调用 transport 并映射异常。

        Args:
            method: HTTP 方法（GET / POST）。
            path: 端点路径（相对 endpoint，形如 /api/admin/status）。
            json_body: 可选的 JSON 请求体。
            timeout: 请求超时秒数，默认 10。

        Returns:
            dict: 解析后的 JSON 响应。

        Raises:
            RemoteUnreachable: 连接失败 / 超时 / 其它网络异常。
            RemoteError: 远端返回非 2xx。
        """
        url = f"{self.endpoint}{path}"
        self._warn_plain_http_once()
        try:
            return self._transport.request(
                method, url, json_body=json_body, timeout=timeout
            )
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {"status": exc.code, "reason": exc.reason}
            raise RemoteError(f"远端 {method} {url} 返回非 2xx（{exc.code}）: {payload}") from exc
        except (
            urllib.error.URLError,
            http.client.HTTPException,
            OSError,
            TimeoutError,
        ) as exc:
            # 第四轮体检批次C：补捕 http.client.HTTPException——响应中途的
            # IncompleteRead / BadStatusLine（网关 504/502 常见形态）此前会穿透
            # RemoteUnreachable 统一映射，API 层 504 语义失效
            raise RemoteUnreachable(f"远端 {method} {url} 不可达: {exc}") from exc

    # ------------------------------------------------------------------ #
    # 公开接口                                                            #
    # ------------------------------------------------------------------ #

    def get_status(self, timeout=10):
        """调用远端状态监控端点，原样透传 JSON 字段。

        Args:
            timeout: 请求超时秒数，默认 10。

        Returns:
            dict: 远端状态快照（online / load / budget 等字段原样透传）。

        Raises:
            RemoteDisabled: 未启用。
            RemoteUnreachable: 远端不可达。
            RemoteError: 远端返回非 2xx。
        """
        self._check_enabled()
        return self._request("GET", "/api/admin/status", timeout=timeout)

    def control(self, action, agent_id=None):
        """下发单条控制指令（enable / disable / pause）。

        Args:
            action: 控制动作，枚举 enable / disable / pause。
            agent_id: 目标智能体 id，可省。

        Returns:
            dict: 远端执行结果。

        Raises:
            RemoteDisabled: 未启用。
            RemoteUnreachable: 远端不可达。
            RemoteError: 远端返回非 2xx。
            ValueError: action 不在合法枚举内。
        """
        self._check_enabled()
        if action not in self.ACTIONS:
            raise ValueError(f"未知控制动作 {action!r}，应为 {self.ACTIONS}")
        body = {"action": action}
        if agent_id is not None:
            body["agent_id"] = agent_id
        return self._request("POST", "/api/admin/control", json_body=body)

    def push_config(self, patch):
        """下发配置补丁。

        Args:
            patch: 配置补丁字典。

        Returns:
            dict: 远端配置更新结果。

        Raises:
            RemoteDisabled: 未启用。
            RemoteUnreachable: 远端不可达。
            RemoteError: 远端返回非 2xx。
        """
        self._check_enabled()
        return self._request("POST", "/api/admin/config", json_body=patch)

    def set_public_endpoint(self, endpoint):
        """设置公网遥控预留端点（Task F4，P2 后置占位）。

        P2 后置：本期公网（TLS/隧道）通道尚未实现，本方法仅存储 URL，不做任何
        连接或认证；不改变局域网 endpoint 的既有行为，get_status 等操作仍只走
        self.endpoint。

        Args:
            endpoint: 公网基础 URL 字符串（去尾部斜杠存储），None 视为清除。
        """
        self.public_endpoint = (
            None if endpoint is None else str(endpoint).rstrip("/") or None
        )

    def clear_public_endpoint(self):
        """清除公网遥控预留端点（归还原点 public_endpoint=None，P2 后置占位）。

        仅清空存储的 URL，不影响局域网 endpoint 与既有操作行为。
        """
        self.public_endpoint = None