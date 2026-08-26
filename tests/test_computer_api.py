# -*- coding: utf-8 -*-
"""Task D3 电脑控制 API 集成测试——真实起服 + urllib HTTP 请求。

复用既有起服模式：向 make_handler 注入带 mock backend 的电脑控制依赖
（authorizer 单例复用 + computer + ToolBridge），验证三个外露端点：
- GET  /api/computer/status      返回 authorized / confirm_dangerous；
- POST /api/computer/authorize   开→关→开 的幂等与同步；
- POST /api/computer/call        未授权 403；授权后 mock backend 返回回填结果。
"""

import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer

import pytest

from lite.computer_control.security import ControlAuthorizer
from lite.computer_control import TOOL_COMMAND, TOOL_KEYBOARD, ComputerControl, ToolBridge
from lite.server.api_server import build_deps, make_handler


class _FakeScreenBackend:
    """内存 mock 屏幕后端：返回固定字节，避免触碰真实屏幕。"""

    def screenshot(self, region=None) -> bytes:
        return b"\x89PNG\r\n\x1a\nfake-image"


class _FakeKeyboardBackend:
    """内存 mock 键盘后端：记录调用但无真实副作用。"""

    def __init__(self):
        self.clicks = []

    def click(self, x, y) -> None:
        self.clicks.append((x, y))

    def type_text(self, text) -> None:
        pass


#: 当前 python 解释器可执行文件，用于构建无害子进程命令
PY = sys.executable


def http_request(url, payload=None, method="GET"):
    """任意方法请求，返回 (status, JSON)；非 2xx 同样解析错误体返回。"""
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, body


@pytest.fixture()
def computer_server(tmp_path):
    """注入电脑控制依赖（fake backend + ToolBridge）的单线程 HTTPServer。

    返回 (base, authorizer, keyboard_backend)：server 供断言结果；authorizer
    供查看 / 复核授权状态；keyboard_backend 供断言 mock 点击被真实触发。
    """
    store, pipeline, manager, _ = build_deps(data_dir=str(tmp_path))
    authorizer = ControlAuthorizer(data_dir=str(tmp_path))
    keyboard = _FakeKeyboardBackend()
    computer = ComputerControl(
        authorized=authorizer.is_authorized(),
        screen_backend=_FakeScreenBackend(),
        keyboard_backend=keyboard,
    )
    bridge = ToolBridge(computer=computer, authorizer=authorizer)
    handler = make_handler(
        store, pipeline, manager, computer=computer, authorizer=authorizer, bridge=bridge
    )
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    base = f"http://127.0.0.1:{port}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield base, authorizer, keyboard
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------- GET status
def test_computer_status_initial(computer_server):
    """初始未授权：GET /api/computer/status 返回 authorized=False、confirm_dangerous=True。"""
    base, _authorizer, _kb = computer_server
    status, body = http_request(f"{base}/api/computer/status")
    assert status == 200
    assert body["authorized"] is False
    assert body["confirm_dangerous"] is True


# ---------------------------------------------------------------- POST authorize
def test_computer_authorize_on_off_on(computer_server):
    """POST authorize 开→关→开：authorized 状态随之翻转并同步 computer。"""
    base, authorizer, _kb = computer_server

    # 开
    status, body = http_request(
        f"{base}/api/computer/authorize", payload={"enabled": True}, method="POST"
    )
    assert status == 200
    assert body["authorized"] is True
    assert authorizer.is_authorized() is True

    # 关
    status, body = http_request(
        f"{base}/api/computer/authorize", payload={"enabled": False}, method="POST"
    )
    assert status == 200
    assert body["authorized"] is False
    assert authorizer.is_authorized() is False

    # 再开
    status, body = http_request(
        f"{base}/api/computer/authorize", payload={"enabled": True}, method="POST"
    )
    assert status == 200
    assert body["authorized"] is True


def test_computer_authorize_bad_body_400(computer_server):
    """enabled 非布尔时返回 400。"""
    base, _authorizer, _kb = computer_server
    status, body = http_request(
        f"{base}/api/computer/authorize", payload={"enabled": "yes"}, method="POST"
    )
    assert status == 400
    assert body["error"] == "bad_request"


# ---------------------------------------------------------------- POST call
def test_computer_call_unauthorized_403(computer_server):
    """未授权时 POST call 返回 403，且 mock 后端未被触发。"""
    base, _authorizer, kb = computer_server
    status, body = http_request(
        f"{base}/api/computer/call",
        payload={"tool": TOOL_KEYBOARD, "arguments": {"action": "click", "x": 1, "y": 2}},
        method="POST",
    )
    assert status == 403
    assert body["authorized"] is False
    assert body["error_code"] == "NOT_AUTHORIZED"
    assert kb.clicks == []  # 未执行本机动作


def test_computer_call_authorized_returns_result(computer_server):
    """授权后 POST call 走 mock backend 执行并回填结果。"""
    base, _authorizer, kb = computer_server
    http_request(f"{base}/api/computer/authorize", payload={"enabled": True}, method="POST")

    status, body = http_request(
        f"{base}/api/computer/call",
        payload={"tool": TOOL_KEYBOARD, "arguments": {"action": "click", "x": 12, "y": 34}},
        method="POST",
    )
    assert status == 200
    assert body["success"] is True
    assert body["tool"] == TOOL_KEYBOARD
    assert body["result"] == {"action": "click", "x": 12, "y": 34}
    assert kb.clicks == [(12, 34)]  # mock 后端真实被触发

    # GET status 复核授权已开启
    status, status_body = http_request(f"{base}/api/computer/status")
    assert status_body["authorized"] is True


def test_computer_call_command_returns_output(computer_server):
    """授权后指令类工具返回命令输出（走真实 ComputerControl 子进程）。"""
    base, _authorizer, _kb = computer_server
    http_request(f"{base}/api/computer/authorize", payload={"enabled": True}, method="POST")

    status, body = http_request(
        f"{base}/api/computer/call",
        payload={"tool": TOOL_COMMAND, "arguments": {"command": f'"{PY}" -c "print(\'API_EXEC_OK\')"'}},
        method="POST",
    )
    assert status == 200
    assert body["success"] is True
    assert "API_EXEC_OK" in (body.get("result") or "")


def test_computer_call_missing_tool_400(computer_server):
    """tool 缺失返回 400。"""
    base, _authorizer, _kb = computer_server
    status, body = http_request(
        f"{base}/api/computer/call", payload={"arguments": {}}, method="POST"
    )
    assert status == 400
    assert body["error"] == "bad_request"