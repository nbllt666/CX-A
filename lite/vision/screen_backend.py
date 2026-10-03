# -*- coding: utf-8 -*-
"""Windows 屏幕采样后端（WindowsGrayscaleScreenBackend）——纯标准库 ctypes GDI 实现。

职责：为 :class:`~lite.vision.sampler.AdaptiveSampler` 提供 ``capture() -> 灰度像素序列``
的屏幕采样后端（冻结包 excludes numpy/PIL，installer/backend.spec——必须零新依赖）。

实现路径（全部经 ctypes 直调 Win32 GDI，无第三方依赖）：
    user32.GetDC(None) 全屏 DC
    → gdi32.CreateCompatibleDC 兼容内存 DC
    → gdi32.CreateDIBSection 顶部朝下 32bpp DIB（负 biHeight）
    → gdi32.BitBlt（SRCCOPY | CAPTUREBLT，覆盖全虚拟屏）
    → 读取 BGRA 位图缓冲 → 64x36 网格等距采样点
    → ``(r*299 + g*587 + b*114) // 1000`` 灰度 int 列表（长度 2304）
    → 逐级释放 SelectObject / DeleteObject / DeleteDC / ReleaseDC

隐私红线：原始帧不出本机——GDI 抓屏结果仅在内存中立即降采样为 64x36 灰度
一维序列，仅该降采样结果参与理解链路；不落盘、不编码外传原始位图
（对齐 lite/vision 包级隐私声明）。

容错契约（对 sampler 的失败帧语义）：``capture()`` 整体 try/except——
非 Windows 平台、任何 GDI 调用失败、缓冲读取异常一律经 LOGGER.warning
（[ScreenBackend][WARN] 前缀，对齐项目日志风格）后返回**上一帧的副本**，
失败原因记录到实例属性 ``last_error``（成功后清 None）；连续失败不抛出。
失败帧回放上一帧使变化率为 0（sampler 单侧 None 会被 change_ratio 判为
1.0 完全变化而误报剧变事件——回退副本实现失败帧零抖动）；首帧前失败
返回 None（sampler 首次采样仅建立基线，无事件产出，可接受）。
"""

import ctypes
import logging
import sys
import threading

#: 原生日志记录器（告警统一携带 [ScreenBackend][WARN] 前缀）
LOGGER = logging.getLogger(__name__)

#: 降采样网格尺寸（与 ChangeDetector 默认 64x36 对齐，长度 2304 定长帧）
_GRID_WIDTH = 64
_GRID_HEIGHT = 36

#: BitBlt 光栅操作码：SRCCOPY（0x00CC0020）| CAPTUREBLT（0x40000000，
#: 含分层窗口——否则 LayeredWindow 不入帧）
_SRCCOPY = 0x00CC0020
_CAPTUREBLT = 0x40000000

#: GetSystemMetrics 的虚拟屏常量（SM_XVIRTUALSCREEN / SM_YVIRTUALSCREEN /
#: SM_CXVIRTUALSCREEN / SM_CYVIRTUALSCREEN）
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79

#: DIB 颜色格式：BI_RGB（未压缩）+ DIB_RGB_COLORS
_BI_RGB = 0
_DIB_RGB_COLORS = 0


class BITMAPINFOHEADER(ctypes.Structure):
    """GDI BITMAPINFOHEADER 结构体（ctypes 直调 CreateDIBSection 用）。"""

    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class BITMAPINFO(ctypes.Structure):
    """GDI BITMAPINFO 结构体（DIB 颜色表头；BI_RGB 无调色板项）。"""

    _fields_ = [("bmiHeader", BITMAPINFOHEADER)]


class WindowsGrayscaleScreenBackend:
    """ctypes GDI 抓屏 → 64x36 灰度一维序列（纯标准库，零新依赖）。

    实例属性：
    - ``last_error``：最近一次 ``capture()`` 失败原因（str）或 None（成功后清空）；
    - 失败回退：抓屏失败时返回上一帧的**副本**（调用方修改返回值不污染缓存），
      首帧前失败返回 None（sampler 首帧仅建基线）。
    """

    def __init__(self):
        """初始化后端（构造期零 GDI 调用——首次 capture 才触碰屏幕 API）。"""
        self.last_error = None
        # capture 仅被视觉 tick 单线程调用；加锁保证缓存读写原子性（防御
        # 未来多线程复用同一 backend 实例的误用场景）
        self._lock = threading.Lock()
        self._last_frame = None

    def capture(self):
        """抓取一帧屏幕并降采样为 64x36 灰度一维序列。

        :return: 长度 2304 的灰度 int 列表；失败时返回上一帧副本，
            首帧前失败返回 None（整体 try/except，绝不向 sampler 抛异常）。
        """
        try:
            frame = self._capture_gdi()
        except Exception as exc:  # noqa: BLE001 - 任何失败都不向 sampler 抛出
            reason = f"{exc.__class__.__name__}: {exc}"
            LOGGER.warning("[ScreenBackend][WARN] 屏幕采样失败，回退上一帧（首帧前返回 None）：%s", reason)
            with self._lock:
                self.last_error = reason
                if self._last_frame is None:
                    return None
                return list(self._last_frame)
        with self._lock:
            self.last_error = None
            self._last_frame = list(frame)
        return frame

    # ------------------------------------------------------------------ #
    # 内部：GDI 抓屏与降采样                                               #
    # ------------------------------------------------------------------ #

    def _capture_gdi(self):
        """执行一次 GDI 抓屏 → 网格灰度降采样（任何失败向上抛，由 capture 兜底）。

        :return: 长度 2304 的灰度 int 列表。
        :raises OSError: 非 Windows 平台 / 虚拟屏尺寸探测失败。
        :raises ctypes.WinError: GDI 调用返回空句柄。
        """
        if sys.platform != "win32":
            raise OSError("非 Windows 平台不支持 GDI 抓屏")
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        screen_x = user32.GetSystemMetrics(_SM_XVIRTUALSCREEN)
        screen_y = user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)
        screen_w = user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN)
        screen_h = user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN)
        if screen_w <= 0 or screen_h <= 0:
            raise OSError(f"虚拟屏尺寸探测失败：{screen_w}x{screen_h}")

        # DIB 头：32bpp BGRA、顶部朝下（负 biHeight，首行=屏幕顶部）
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = screen_w
        bi.bmiHeader.biHeight = -screen_h
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = _BI_RGB

        h_screen = user32.GetDC(None)
        if not h_screen:
            raise ctypes.WinError()
        h_mem = gdi32.CreateCompatibleDC(h_screen)
        if not h_mem:
            user32.ReleaseDC(None, h_screen)
            raise ctypes.WinError()
        bits_ptr = ctypes.c_void_p()
        h_bmp = gdi32.CreateDIBSection(
            h_mem, ctypes.byref(bi), _DIB_RGB_COLORS, ctypes.byref(bits_ptr), None, 0
        )
        if not h_bmp or not bits_ptr:
            gdi32.DeleteDC(h_mem)
            user32.ReleaseDC(None, h_screen)
            raise ctypes.WinError()

        prev_obj = gdi32.SelectObject(h_mem, h_bmp)
        try:
            ok = gdi32.BitBlt(
                h_mem, 0, 0, screen_w, screen_h,
                h_screen, screen_x, screen_y, _SRCCOPY | _CAPTUREBLT,
            )
            if not ok:
                raise ctypes.WinError()
            # BGRA 位图缓冲整块读出（CreateDIBSection 托管内存 → bytes 快照）
            buf = ctypes.string_at(bits_ptr, screen_w * screen_h * 4)
        finally:
            # 逐级释放 GDI 对象（顺序：选中文本还原 → 位图 → 内存 DC → 屏幕 DC）
            gdi32.SelectObject(h_mem, prev_obj)
            gdi32.DeleteObject(h_bmp)
            gdi32.DeleteDC(h_mem)
            user32.ReleaseDC(None, h_screen)

        return self._grid_grayscale(buf, screen_w, screen_h)

    @staticmethod
    def _grid_grayscale(bgra_buf, screen_w, screen_h):
        """BGRA 缓冲按 64x36 网格等距采样并转灰度（纯标准库循环）。

        :param bgra_buf: 32bpp BGRA 位图字节缓冲（每像素 4 字节）。
        :param screen_w: 位图宽度（像素）。
        :param screen_h: 位图高度（像素）。
        :return: 长度 ``_GRID_WIDTH * _GRID_HEIGHT``（2304）的灰度 int 列表，
            逐点取 ``(r*299 + g*587 + b*114) // 1000``（ITU-R 601 口径）。
        """
        gray = []
        for row in range(_GRID_HEIGHT):
            # 网格中心点映射（+0.5 避免边界像素偏置；顶部朝下 DIB 行序即屏幕行序）
            sy = int((row + 0.5) * screen_h / _GRID_HEIGHT)
            row_base = sy * screen_w
            for col in range(_GRID_WIDTH):
                sx = int((col + 0.5) * screen_w / _GRID_WIDTH)
                offset = (row_base + sx) * 4
                b = bgra_buf[offset]
                g = bgra_buf[offset + 1]
                r = bgra_buf[offset + 2]
                gray.append((r * 299 + g * 587 + b * 114) // 1000)
        return gray
