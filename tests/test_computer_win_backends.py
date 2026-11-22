# -*- coding: utf-8 -*-
"""Windows 电脑控制真实后端单元测试（不触碰真实屏幕/键鼠）。

覆盖：
- 生产装配 build_computer_deps 注入 WinScreenBackend / WinKeyboardBackend；
- 截图：base64 PNG → bytes；空截图 → ExecutionError；
- 点击：SetCursorPos + mouse_event 左键按下/抬起；
- 输入：SendInput Unicode 序列（含中文、换行）。
"""

import base64
import ctypes
import sys

import pytest

from lite.computer_control.control import ExecutionError
from lite.computer_control.win_backends import (
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    VK_RETURN,
    WinKeyboardBackend,
    WinScreenBackend,
    _INPUT,
)


class FakeUser32:
    """记录 Win32 调用的 user32 替身。"""

    def __init__(self) -> None:
        self.cursor = None
        self.mouse_events = []
        self.batches = []

    def SetCursorPos(self, x, y):  # noqa: N802 - 对齐 Win32 命名
        self.cursor = (x, y)
        return 1

    def mouse_event(self, flags, dx, dy, data, extra):
        self.mouse_events.append(int(flags))

    def SendInput(self, count, array, size):  # noqa: N802
        batch = []
        for i in range(int(count)):
            item = array[i]
            ki = item.union.ki
            batch.append(
                {
                    "type": int(item.type),
                    "vk": int(ki.wVk),
                    "scan": int(ki.wScan),
                    "flags": int(ki.dwFlags),
                }
            )
        self.batches.append(batch)
        return int(count)


def test_build_computer_deps_injects_windows_backends(tmp_path):
    """生产装配必须注入真实后端（Windows）。"""
    if sys.platform != "win32":
        pytest.skip("仅 Windows")
    from lite.server.api_server import build_computer_deps

    computer, _authorizer, _bridge = build_computer_deps(str(tmp_path))
    assert isinstance(computer._screen_backend, WinScreenBackend)
    assert isinstance(computer._keyboard_backend, WinKeyboardBackend)


def test_screen_backend_decodes_base64_png():
    png = b"\x89PNG\r\n\x1a\nfake-body"
    backend = WinScreenBackend(
        capture=lambda max_width=None: base64.b64encode(png).decode("ascii")
    )
    assert backend.screenshot() == png


def test_screen_backend_empty_raises():
    backend = WinScreenBackend(capture=lambda max_width=None: None)
    with pytest.raises(ExecutionError):
        backend.screenshot()


def test_keyboard_click_calls_win32():
    fake = FakeUser32()
    backend = WinKeyboardBackend(user32=fake)
    backend.click(12, 34)
    assert fake.cursor == (12, 34)
    assert fake.mouse_events == [MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP]


def test_keyboard_type_unicode_sequence():
    fake = FakeUser32()
    backend = WinKeyboardBackend(user32=fake)
    backend.type_text("A你\n")
    assert len(fake.batches) == 1
    batch = fake.batches[0]
    # 每个字符 down+up = 2 条；A / 你 / \n 共 6 条
    assert len(batch) == 6
    # A
    assert batch[0]["scan"] == ord("A") and batch[0]["flags"] == KEYEVENTF_UNICODE
    assert batch[1]["flags"] == KEYEVENTF_UNICODE | KEYEVENTF_KEYUP
    # 你（UTF-16 code unit 0x4F60）
    assert batch[2]["scan"] == 0x4F60
    # 换行 → VK_RETURN
    assert batch[4]["vk"] == VK_RETURN
    assert batch[5]["vk"] == VK_RETURN and batch[5]["flags"] == KEYEVENTF_KEYUP


def test_input_struct_size_is_platform_correct():
    """INPUT 结构在 64 位下为 40 字节（防止 ctypes 布局错误导致 SendInput 失败）。"""
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(_INPUT) == 40
