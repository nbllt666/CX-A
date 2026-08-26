# -*- coding: utf-8 -*-
"""云端中转通道（Task G1）——跨公网 Agent 通信。

依据《CX-A 补充文档 · ACP 与 CXFC》§2.2/§5.4：轻量版 ACP 新增云端中转兜底，
供跨公网（跨设备）Agent 通信，不依赖局域网直连。

设计要点（不复用 CX-O 的重型 asyncio/单例/持久化，仅走最简 transport 抽象）：
- ``send(payload)``：默认以 urllib POST JSON 到 ``cloud_relay_endpoint``，
  成功返回远端回执（dict）；可注入 transport（mock）接管底层传输，供测试。
- ``is_reachable(timeout)``：轻量连通性探测，仅网络异常返回 False。
"""

import json
import urllib.request

#: 默认 POST 超时（秒）
_DEFAULT_SEND_TIMEOUT = 10.0


class CloudRelayError(Exception):
    """云端中转错误。

    触发场景：未配置 ``cloud_relay_endpoint``、传输不可用或远端点请求失败。
    """


class CloudRelay:
    """轻量云端中转。

    传输抽象：构造时传入 ``transport``（含 ``send(payload)->dict`` 与
    ``is_reachable(timeout)->bool``）即委托给外部传输；缺省走内置 urllib
    POST JSON 到 endpoint。测试注入内存 mock transport，避免真实网络。
    """

    def __init__(self, endpoint="", transport=None):
        """构造云端中转。

        :param endpoint: 云端中转地址（config.acp.cloud_relay_endpoint）
        :param transport: 可选注入的传输对象；为 None 时使用内置 urllib 传输
        """
        self.endpoint = str(endpoint or "").rstrip("/")
        self.transport = transport

    def send(self, payload):
        """向云端中转投递一条消息 / 负载。

        :param payload: 待中转的数据（dict，通常为 ``{action, request_id, data}``）
        :return: 远端回执（dict）；注入 transport 时为其 ``send(payload)`` 返回值
        :raises CloudRelayError: 未配置 endpoint 或底层传输失败
        """
        if self.transport is not None:
            return self.transport.send(payload)

        if not self.endpoint:
            raise CloudRelayError(
                "未配置云端中转地址（config.acp.cloud_relay_endpoint 为空）"
            )
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
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