"""DreamTalk HTTP 适配层：``/api/dreamtalk/*`` 的请求校验与响应序列化。

本模块把 CX-A ``api_server`` 的同步请求处理翻译为梦境运行态的异步调用：
请求体严格白名单（未知字段 400，与 CX-O ``extra="forbid"`` 同语义）、
``limit`` 解析（非整数 400 / 越界 clamp）、领域异常到状态码映射、``asyncio.run`` 桥。
本模块与 ``api_server`` 都**不读取 config**——配置只经 ``wiring`` 产出。

端点（10 个方法 / 8 条唯一路径）：

- GET    ``/health``
- POST   ``/sessions``
- GET    ``/sessions``
- GET    ``/sessions/{id}``
- POST   ``/sessions/{id}/message``
- POST   ``/sessions/{id}/wake``
- GET    ``/sessions/{id}/recall``
- GET    ``/recall``
- GET    ``/config``
- PUT    ``/config``
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, Mapping, Optional, Tuple

from .core.errors import DreamTalkError
from .core.models import CONTRACT_VERSION, MODES
from .wiring import DreamTalkRuntime

logger = logging.getLogger(__name__)

#: 未装配运行态时的 health 契约口径
_DISABLED_STATUS: Dict[str, Any] = {
    "status": "disabled",
    "enabled": False,
    "contract_version": CONTRACT_VERSION,
    "sessions": 0,
    "mode_weights": "",
    "memory_writeback": False,
}

#: ``limit`` 允许区间（越界 clamp，非整数 400）
_LIMIT_MIN = 1
_LIMIT_MAX = 100

#: 请求体允许字段（禁止手填素材 / 梦外上下文）
_START_FIELDS = frozenset({"mode"})
_MESSAGE_FIELDS = frozenset({"content"})
_WAKE_FIELDS = frozenset()

_SESSION_ITEM = re.compile(r"^/sessions/([^/]+)$")
_SESSION_RECALL = re.compile(r"^/sessions/([^/]+)/recall$")
_SESSION_MESSAGE = re.compile(r"^/sessions/([^/]+)/message$")
_SESSION_WAKE = re.compile(r"^/sessions/([^/]+)/wake$")


class _HTTPError(Exception):
    """内部信号：携带状态码与错误码，由 :func:`handle` 转成错误体。"""

    def __init__(self, status: int, error: str, message: str):
        super().__init__(message)
        self.status = status
        self.error = error
        self.message = message


def handle(
    runtime: Optional[DreamTalkRuntime],
    method: str,
    subpath: str,
    query: Mapping[str, Any],
    body: Any,
) -> Tuple[int, Dict[str, Any]]:
    """分发一次梦境请求，返回 ``(status_code, payload)``（永不抛业务异常）。

    Args:
        runtime: 梦境运行态；None 表示未装配（除 health 外 503）
        method: HTTP 方法（GET / POST / PUT）
        subpath: 已剥掉 ``/api/dreamtalk`` 前缀的路径（如 ``/sessions``）
        query: 查询参数（仅消费 ``limit``，其余忽略）
        body: 已解析请求体（``api_server`` 保证 dict 或 None）
    """
    try:
        return _dispatch(runtime, method, subpath, query, body)
    except _HTTPError as exc:
        return exc.status, _error_payload(exc)
    except DreamTalkError as exc:
        mapped = _domain_error(exc)
        return mapped.status, _error_payload(mapped)
    except ValueError as exc:
        mapped = _HTTPError(422, "invalid_config", f"配置字段非法: {exc}")
        return mapped.status, _error_payload(mapped)


def _dispatch(
    runtime: Optional[DreamTalkRuntime],
    method: str,
    subpath: str,
    query: Mapping[str, Any],
    body: Any,
) -> Tuple[int, Dict[str, Any]]:
    """路由分发主体；业务错误以 :class:`_HTTPError` 抛出，由 :func:`handle` 统一映射。"""
    path = subpath or "/"
    if method == "GET" and path == "/health":
        payload = dict(_DISABLED_STATUS) if runtime is None else runtime.status()
        payload["ok"] = True
        return 200, payload

    if runtime is None:
        raise _HTTPError(503, "service_unavailable", "梦境服务不可用")

    # 配置读写不受 enabled 限制（与 CX-O 一致）：否则用户关掉梦境后无法用 API 重新打开
    if method == "GET" and path == "/config":
        return 200, {"ok": True, "config": runtime.config_snapshot()}

    if method == "PUT" and path == "/config":
        partial = _require_object(body)
        return 200, {"ok": True, "config": runtime.apply_config_patch(partial)}

    if not runtime.is_enabled():
        raise _HTTPError(503, "service_unavailable", "梦境服务不可用")

    if method == "GET" and path == "/sessions":
        limit = _parse_limit(query.get("limit"))
        sessions = _run(runtime.list_sessions(limit))
        items = [session.to_dict() for session in sessions]
        return 200, {"ok": True, "items": items, "total": len(items)}

    if method == "POST" and path == "/sessions":
        payload = _require_object(body)
        _reject_unknown(payload, _START_FIELDS)
        mode = payload.get("mode", "auto")
        if not isinstance(mode, str) or (mode != "auto" and mode not in MODES):
            raise _HTTPError(422, "invalid_mode", f"未知梦境模式: {mode!r}")
        view = _run(runtime.start_session(mode))
        return 200, _view_payload(view)

    if method == "GET" and path == "/recall":
        limit = _parse_limit(query.get("limit"))
        recalls = _run(runtime.list_recalls(limit))
        items = [recall.to_dict() for recall in recalls]
        return 200, {"ok": True, "items": items, "total": len(items)}

    match = _SESSION_ITEM.match(path)
    if match and method == "GET":
        view = _run(runtime.get_session(match.group(1)))
        return 200, _view_payload(view)

    match = _SESSION_RECALL.match(path)
    if match and method == "GET":
        recall = _run(runtime.get_recall(match.group(1)))
        return 200, {"ok": True, "recall": recall.to_dict()}

    match = _SESSION_MESSAGE.match(path)
    if match and method == "POST":
        payload = _require_object(body)
        _reject_unknown(payload, _MESSAGE_FIELDS)
        content = payload.get("content", "")
        if not isinstance(content, str):
            raise _HTTPError(400, "bad_request", "content 必须是字符串")
        view = _run(runtime.message(match.group(1), content))
        return 200, _view_payload(view)

    match = _SESSION_WAKE.match(path)
    if match and method == "POST":
        payload = _require_object(body)
        _reject_unknown(payload, _WAKE_FIELDS)
        view = _run(runtime.wake(match.group(1)))
        return 200, _view_payload(view)

    raise _HTTPError(404, "not_found", f"未知梦境端点: {method} {path}")


# ------------------------------------------------------------------ 内部工具
def _error_payload(exc: _HTTPError) -> Dict[str, Any]:
    """把 :class:`_HTTPError` 转成统一错误体。"""
    return {"ok": False, "error": exc.error, "message": exc.message}


def _run(coro) -> Any:
    """在同步请求线程中执行一次梦境协程。"""
    return asyncio.run(coro)


def _view_payload(view) -> Dict[str, Any]:
    """会话视图响应体：``session`` + ``turns`` + 可选 ``recall``。"""
    payload = view.to_dict()
    payload["ok"] = True
    return payload


def _require_object(body: Any) -> Dict[str, Any]:
    """请求体必须是 JSON 对象（空体由 api_server 归一为 ``{}``）。"""
    if body is None:
        raise _HTTPError(400, "bad_request", "请求体必须是合法 JSON 对象")
    if not isinstance(body, Mapping):
        raise _HTTPError(400, "bad_request", "请求体必须是 JSON 对象")
    return dict(body)


def _reject_unknown(payload: Mapping[str, Any], allowed: frozenset) -> None:
    """未知字段一律 400（禁止手填素材 / 梦外上下文）。"""
    unknown = [key for key in payload if key not in allowed]
    if unknown:
        raise _HTTPError(
            400,
            "bad_request",
            f"不支持的字段: {', '.join(sorted(unknown))}（梦境素材只能来自本机记忆）",
        )


def _parse_limit(raw: Any) -> Optional[int]:
    """``limit`` 解析：缺省 None（取配置默认）；非整数 400；越界 clamp 到 1~100。"""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = int(text)
    except (TypeError, ValueError) as exc:
        raise _HTTPError(400, "bad_request", f"limit 必须是整数: {raw!r}") from exc
    return max(_LIMIT_MIN, min(_LIMIT_MAX, value))


def _domain_error(exc: DreamTalkError) -> _HTTPError:
    """领域异常 → HTTP 错误（404 / 409 / 422 / 500）。"""
    status = int(getattr(exc, "status_code", 400))
    code = {
        404: "session_not_found",
        409: "session_ended",
        422: "invalid_mode",
        500: "store_error",
    }.get(status, "bad_request")
    name = exc.__class__.__name__
    if name == "NoMemoryMaterial":
        code = "no_memory_material"
    elif name == "InvalidMode":
        code = "invalid_mode"
    elif name == "SessionEnded":
        code = "session_ended"
    elif name == "SessionNotFound":
        code = "session_not_found"
    elif name == "StoreError":
        code = "store_error"
    return _HTTPError(status, code, str(exc) or name)
