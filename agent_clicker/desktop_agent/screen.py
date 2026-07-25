"""Monitor enumeration + screenshot capture via mss."""
from __future__ import annotations
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import mss
from PIL import Image


@dataclass
class Monitor:
    index: int            # 1-based; 0 = "all monitors" virtual screen
    left: int
    top: int
    width: int
    height: int
    label: str

    def contains(self, gx: int, gy: int) -> bool:
        return self.left <= gx < self.left + self.width and self.top <= gy < self.top + self.height


def list_monitors() -> list[Monitor]:
    out: list[Monitor] = []
    with mss.mss() as sct:
        for i, m in enumerate(sct.monitors):
            label = ("All monitors" if i == 0 else f"Monitor {i}") + f"  {m['width']}x{m['height']} @ ({m['left']},{m['top']})"
            out.append(Monitor(index=i, left=m["left"], top=m["top"],
                               width=m["width"], height=m["height"], label=label))
    return out


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("bmiHeader", _BitmapInfoHeader), ("bmiColors", wintypes.DWORD * 3)]


def capture_region(left: int, top: int, width: int, height: int) -> Image.Image:
    """Capture physical desktop pixels with a native GDI fallback.

    mss 10/Pillow can intermittently fail on Windows 11 with Python 3.13 even
    though the desktop DC is available. This path uses a compatible bitmap and
    GetDIBits directly, while still honoring WDA_EXCLUDEFROMCAPTURE windows.
    """
    if not sys_platform_windows():
        raise OSError("native GDI capture is only available on Windows")
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi32.BitBlt.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.POINTER(_BitmapInfo), wintypes.UINT]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL

    screen_dc = user32.GetDC(None)
    memory_dc = gdi32.CreateCompatibleDC(screen_dc)
    bitmap = gdi32.CreateCompatibleBitmap(screen_dc, int(width), int(height))
    old = gdi32.SelectObject(memory_dc, bitmap)
    try:
        if not gdi32.BitBlt(memory_dc, 0, 0, int(width), int(height), screen_dc, int(left), int(top), 0x00CC0020 | 0x40000000):
            raise OSError("native BitBlt capture failed")
        info = _BitmapInfo()
        info.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
        info.bmiHeader.biWidth = int(width)
        info.bmiHeader.biHeight = -int(height)
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0
        pixels = (ctypes.c_ubyte * (int(width) * int(height) * 4))()
        rows = gdi32.GetDIBits(memory_dc, bitmap, 0, int(height), pixels, ctypes.byref(info), 0)
        if rows != int(height):
            raise OSError(f"native GetDIBits captured {rows}/{height} rows")
        return Image.frombuffer("RGB", (int(width), int(height)), bytes(pixels), "raw", "BGRX", 0, 1)
    finally:
        if old:
            gdi32.SelectObject(memory_dc, old)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory_dc:
            gdi32.DeleteDC(memory_dc)
        if screen_dc:
            user32.ReleaseDC(None, screen_dc)


def sys_platform_windows() -> bool:
    import sys

    return sys.platform.startswith("win")


def capture(mon: Monitor) -> Image.Image:
    try:
        with mss.mss() as sct:
            raw = sct.grab({"left": mon.left, "top": mon.top, "width": mon.width, "height": mon.height})
            return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    except Exception:
        return capture_region(mon.left, mon.top, mon.width, mon.height)
