# -*- coding: utf-8 -*-
"""Windows 电脑控制真实后端（屏幕截图 / 鼠标点击 / 键盘输入）。

背景（20261121_模块0_电脑控制屏幕键鼠后端）：
``ComputerControl`` 的 ``screen_backend`` / ``keyboard_backend`` 此前只有安全占位
（调用即抛 ``ExecutionError``），生产装配未注入真实实现，导致
``computer_screen_control`` / ``computer_keyboard_control`` 在打包版恒 500。

本模块提供 Windows 实现，供 ``api_server.build_computer_deps`` 注入：

- 截图：复用视觉链路的 :class:`lite.vision.screen_backend.WindowsGrayscaleScreenBackend`
  （GDI 抓屏 + 标准库 PNG 编码），返回 PNG 字节；``region`` 参数当前忽略
  （工具 schema 保留，后续需要裁剪时再扩展）。
- 点击：``SetCursorPos`` + ``mouse_event``（左键按下/抬起）。
- 输入：``SendInput`` + ``KEYEVENTF_UNICODE`` 逐 UTF-16 code unit 发送，支持中文；
  换行/制表符映射为 VK_RETURN / VK_TAB。

约束：仅在 ``sys.platform == "win32"`` 生效；非 Windows 调用即抛中文
``ExecutionError``，保持默认占位语义，不触碰本机。无第三方依赖。
"""

from __future__ import annotations

import base64
import ctypes
import logging
import sys
from typing import Any, Callable, List, Optional

from lite.computer_control.control import ExecutionError

__all__ = ["WinScreenBackend", "WinKeyboardBackend"]

LOGGER = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

#: Win32 常量
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
VK_RETURN = 0x0D
VK_TAB = 0x09

#: 64 位下 ULONG_PTR 为 8 字节；INPUT 结构尺寸随平台变化。
_ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_int32),
        ("dy", ctypes.c_int32),
        ("mouseData", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_uint32),
        ("wParamL", ctypes.c_ushort),
        ("wParamH", ctypes.c_ushort),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [
        ("ki", _KEYBDINPUT),
        ("mi", _MOUSEINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("union", _INPUTUNION)]


def _load_user32():
    """加载并声明 user32 函数签名（仅 Windows）。"""
    if not _IS_WINDOWS:
        raise ExecutionError("电脑控制真实键鼠后端仅支持 Windows")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = ctypes.c_int
    user32.mouse_event.argtypes = [
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        _ULONG_PTR,
    ]
    user32.mouse_event.restype = None
    user32.SendInput.argtypes = [ctypes.c_uint32, ctypes.POINTER(_INPUT), ctypes.c_int]
    user32.SendInput.restype = ctypes.c_uint32
    return user32


class WinScreenBackend:
    """Windows 屏幕后端：GDI 抓屏 → PNG 字节。

    :param capture: 测试注入的截图函数（``max_width=`` 关键字），缺省走视觉后端。
    :param max_width: 截图宽带上限（下采样，控制 payload 体积）。
    """

    def __init__(
        self,
        capture: Optional[Callable[..., Optional[str]]] = None,
        max_width: int = 1920,
    ) -> None:
        self._capture = capture
        self._max_width = int(max_width)

    def _capture_b64(self) -> Optional[str]:
        if self._capture is not None:
            return self._capture(max_width=self._max_width)
        from lite.vision.screen_backend import WindowsGrayscaleScreenBackend

        return WindowsGrayscaleScreenBackend().capture_image_b64(max_width=self._max_width)

    def screenshot(self, region: Optional[dict] = None) -> bytes:
        """截取屏幕并返回 PNG 字节。

        :param region: 可选裁剪区域；当前实现忽略（保留参数兼容工具 schema）。
        :return: PNG 图像字节
        :raises ExecutionError: 非 Windows / 抓屏失败 / 解码失败
        """
        if not _IS_WINDOWS:
            raise ExecutionError("电脑控制屏幕后端仅支持 Windows")
        if region:
            LOGGER.info("[ComputerControl] region 参数当前忽略，返回全屏截图")
        b64 = self._capture_b64()
        if not b64:
            raise ExecutionError("屏幕截图失败：未取得图像数据")
        try:
            image = base64.b64decode(str(b64), validate=False)
        except Exception as exc:  # noqa: BLE001 - 统一转执行错误
            raise ExecutionError(f"屏幕截图解码失败：{exc}") from exc
        if not image:
            raise ExecutionError("屏幕截图失败：图像为空")
        return image


class WinKeyboardBackend:
    """Windows 键鼠后端：点击与 Unicode 文本输入。

    :param user32: 测试注入的 user32 替身；缺省加载真实 user32。
    """

    def __init__(self, user32: Optional[Any] = None) -> None:
        self._user32 = user32

    def _api(self):
        if self._user32 is None:
            self._user32 = _load_user32()
        return self._user32

    @staticmethod
    def _key_input(vk: int, scan: int, flags: int) -> _INPUT:
        item = _INPUT()
        item.type = INPUT_KEYBOARD
        item.union.ki = _KEYBDINPUT(
            wVk=int(vk), wScan=int(scan), dwFlags=int(flags), time=0, dwExtraInfo=0
        )
        return item

    def _send(self, inputs: List[_INPUT]) -> None:
        if not inputs:
            return
        api = self._api()
        count = len(inputs)
        array = (_INPUT * count)(*inputs)
        sent = int(api.SendInput(count, array, ctypes.sizeof(_INPUT)))
        if sent != count:
            raise ExecutionError(
                f"SendInput 失败：已发送 {sent}/{count}，错误码 {ctypes.get_last_error()}"
            )

    def click(self, x: int, y: int) -> None:
        """在屏幕坐标 (x, y) 左键点击。"""
        if not _IS_WINDOWS:
            raise ExecutionError("电脑控制键鼠后端仅支持 Windows")
        try:
            ix, iy = int(x), int(y)
        except (TypeError, ValueError) as exc:
            raise ExecutionError(f"点击坐标必须为整数：({x!r}, {y!r})") from exc
        api = self._api()
        if not api.SetCursorPos(ix, iy):
            raise ExecutionError(
                f"SetCursorPos 失败：({ix},{iy})，错误码 {ctypes.get_last_error()}"
            )
        api.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        api.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def type_text(self, text: str) -> None:
        """逐 UTF-16 code unit 输入文本（支持中文）。"""
        if not _IS_WINDOWS:
            raise ExecutionError("电脑控制键鼠后端仅支持 Windows")
        value = str(text or "")
        if not value:
            return
        inputs: List[_INPUT] = []
        raw = value.encode("utf-16-le", errors="surrogatepass")
        for offset in range(0, len(raw), 2):
            code = int.from_bytes(raw[offset:offset + 2], "little")
            if code in (0x0A, 0x0D):
                inputs.append(self._key_input(VK_RETURN, 0, 0))
                inputs.append(self._key_input(VK_RETURN, 0, KEYEVENTF_KEYUP))
            elif code == 0x09:
                inputs.append(self._key_input(VK_TAB, 0, 0))
                inputs.append(self._key_input(VK_TAB, 0, KEYEVENTF_KEYUP))
            else:
                inputs.append(self._key_input(0, code, KEYEVENTF_UNICODE))
                inputs.append(
                    self._key_input(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)
                )
        self._send(inputs)
