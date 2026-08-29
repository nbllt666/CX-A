# -*- coding: utf-8 -*-
"""云端中转通道（Task G1）——跨公网 Agent 通信。

依据《CX-A 补充文档 · ACP 与 CXFC》§2.2/§5.4：轻量版 ACP 新增云端中转兜底，
供跨公网（跨设备）Agent 通信，不依赖局域网直连。

设计要点（不复用 CX-O 的重型 asyncio/单例/持久化，仅走最简 transport 抽象）：
- ``send(payload)``：默认以 urllib POST JSON 到 ``cloud_relay_endpoint``，
  成功返回远端回执（dict）；可注入 transport（mock）接管底层传输，供测试。
- ``is_reachable(timeout)``：轻量连通性探测，仅网络异常返回 False。
- 鉴权与安全（第四轮体检批次C）：构造可选 ``token``，非空时请求附
  ``Authorization: Bearer <token>`` 头；endpoint 为明文 ``http://`` 时打印
  一次性 ``LOGGER.warning`` 告警（口径对齐 remote.py M-16）。
"""

import json
import logging
import urllib.request

#: 原生日志记录器
LOGGER = logging.getLogger(__name__)

#: 默认 POST 超时（秒）
_DEFAULT_SEND_TIMEOUT = 10.0


class CloudRelayError(Exception):
    """云端中转错误。

    触发场景：未配置 ``cloud_relay_endpoint``、负载不可序列化或远端点请求失败。
    """


class CloudRelay:
    """轻量云端中转。

    传输抽象：构造时传入 ``transport``（含 ``send(payload)->dict`` 与
    ``is_reachable(timeout)->bool``）即委托给外部传输；缺省走内置 urllib
    POST JSON 到 endpoint。测试注入内存 mock transport，避免真实网络。
    """

    def __init__(self, endpoint="", transport=None, token=""):
        """构造云端中转。

        :param endpoint: 云端中转地址（config.acp.cloud_relay_endpoint）
        :param transport: 可选注入的传输对象；为 None 时使用内置 urllib 传输
        :param token: 可选鉴权令牌（config.acp.cloud_relay_token）；非空时请求
            附 ``Authorization: Bearer <token>`` 头（第四轮体检批次C）
        """
        self.endpoint = str(endpoint or "").rstrip("/")
        self.transport = transport
        self.token = str(token or "")
        #: 明文 HTTP 告警是否已发出（仅告警一次，M-16 口径）
        self._plain_http_warned = False

    def _auth_headers(self):
        """构造默认传输的请求头：JSON 类型 + 可选 Bearer 鉴权头。"""
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _warn_plain_http_once(self):
        """endpoint 为 http:// 明文传输时打印一次性安全告警（M-16 口径）。

        跨公网投递的负载与令牌可被链路嗅探/伪造，至少显式告警提示用户，不静默；
        无论 endpoint 形态如何，本方法整个生命周期仅生效一次。
        """
        if self._plain_http_warned:
            return
        self._plain_http_warned = True
        if self.endpoint.lower().startswith("http://"):
            LOGGER.warning(
                "云端中转 endpoint 为明文 HTTP（%s）：跨公网投递的负载与令牌"
                "可被链路嗅探/伪造，建议尽快切换 HTTPS",
                self.endpoint,
            )

    def send(self, payload):
        """向云端中转投递一条消息 / 负载。

        :param payload: 待中转的数据（dict，通常为 ``{action, request_id, data}``）
        :return: 远端回执（dict）；注入 transport 时为其 ``send(payload)`` 返回值
        :raises CloudRelayError: 未配置 endpoint、负载不可序列化或底层传输失败
        """
        if self.transport is not None:
            return self.transport.send(payload)

        if not self.endpoint:
            raise CloudRelayError(
                "未配置云端中转地址（config.acp.cloud_relay_endpoint 为空）"
            )
        self._warn_plain_http_once()
        try:
            # 第四轮体检批次C：json.dumps 纳入 try——不可序列化负载此前抛裸
            # TypeError 穿透 CloudRelayError 统一异常契约
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                self.endpoint,
                data=body,
                headers=self._auth_headers(),
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=_DEFAULT_SEND_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001 - 传输失败一律转为中转错误
            raise CloudRelayError(f"云端中转请求失败（{self.endpoint}）：{exc}") from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}

    def is_reachable(self, timeout=5):
        """轻量连通性探测。

        收到任何响应（无论状态码）即认为可达；网络异常或未配置 endpoint 返回 False。

        :param timeout: 探测超时秒数，默认 5
        """
        if self.transport is not None:
            return bool(self.transport.is_reachable(timeout))
        if not self.endpoint:
            return False
        try:
            with urllib.request.urlopen(self.endpoint, timeout=timeout):
                pass
            return True
        except Exception:  # noqa: BLE001 - 连通性探测失败一律视为不可达
            return False
