"""DreamTalk 领域异常：由 API 层按 ``status_code`` 映射为 HTTP 状态码。

与 ``public/interface_stub/dreamtalk_service.pyi`` 的异常契约逐项对应：

- ``DreamTalkError``：基类（HTTP 400）
- ``SessionNotFound``：会话不存在（HTTP 404）
- ``SessionEnded``：会话已结束或重复醒来（HTTP 409）
- ``NoMemoryMaterial``：记忆梦缺少当天记忆素材（HTTP 422）
- ``InvalidMode``：非法梦境模式（HTTP 422）
- ``StoreError``：会话存储不可用（HTTP 500；CX-O 扩展）
"""

from __future__ import annotations


class DreamTalkError(Exception):
    """DreamTalk 领域异常基类（默认 HTTP 400）。"""

    status_code = 400


class SessionNotFound(DreamTalkError):
    """梦境会话不存在（HTTP 404）。"""

    status_code = 404


class SessionEnded(DreamTalkError):
    """梦境会话已结束，不能继续推进或重复醒来（HTTP 409）。"""

    status_code = 409


class NoMemoryMaterial(DreamTalkError):
    """记忆梦缺少当天记忆素材（HTTP 422）。"""

    status_code = 422


class InvalidMode(DreamTalkError):
    """非法的梦境模式（HTTP 422）。"""

    status_code = 422


class StoreError(DreamTalkError):
    """梦境会话存储不可用或读写失败（HTTP 500；CX-O 扩展）。"""

    status_code = 500
