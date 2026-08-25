"""Read-only snapshots of supported coding application windows.

The phone mirror deliberately streams the real desktop pixels instead of
reimplementing another product's chat UI or attaching to its private state.
``PrintWindow`` can capture an unobscured backing surface even when another
window covers it, and no input path is exposed here.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from io import BytesIO
import os
import threading
from typing import Any


APPS = {
    "codex": "Codex",
    "claude": "Claude",
    "cursor": "Cursor",
}

_CAPTURE_LOCKS = {name: threading.Lock() for name in APPS}
_USER32 = ctypes.windll.user32 if os.name == "nt" else None
_GDI32 = ctypes.windll.gdi32 if os.name == "nt" else None
_KERNEL32 = ctypes.windll.kernel32 if os.name == "nt" else None

if os.name == "nt":
    _KERNEL32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _KERNEL32.OpenProcess.restype = wintypes.HANDLE
    _KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
    _USER32.GetWindowDC.argtypes = [wintypes.HWND]
    _USER32.GetWindowDC.restype = wintypes.HDC
    _USER32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    _USER32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    _GDI32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    _GDI32.CreateCompatibleDC.restype = wintypes.HDC
    _GDI32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    _GDI32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    _GDI32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    _GDI32.SelectObject.restype = wintypes.HGDIOBJ
    _GDI32.GetDIBits.argtypes = [
        wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT,
    ]
    _GDI32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    _GDI32.DeleteDC.argtypes = [wintypes.HDC]


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


def _process_path(pid: int) -> str:
    if _KERNEL32 is None:
        return ""
    process = _KERNEL32.OpenProcess(0x1000, False, int(pid))
    if not process:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if _KERNEL32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    finally:
        _KERNEL32.CloseHandle(process)


def _matches(app: str, process_path: str) -> bool:
    path = process_path.casefold().replace("/", "\\")
    if app == "codex":
        return path.endswith("\\chatgpt.exe") and "\\openai.codex_" in path
    if app == "claude":
        return path.endswith("\\claude.exe")
    if app == "cursor":
        return path.endswith("\\cursor.exe") and "\\cursor\\" in path
    return False


def _window_title(hwnd: int) -> str:
    length = _USER32.GetWindowTextLengthW(hwnd)
    if not length:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    _USER32.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value.strip()


def _find_window(app: str) -> tuple[int, str, tuple[int, int]] | None:
    if _USER32 is None or app not in APPS:
        return None
    candidates: list[tuple[int, int, str, tuple[int, int]]] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd: int, _lparam: int) -> bool:
        if not _USER32.IsWindowVisible(hwnd) or _USER32.IsIconic(hwnd):
            return True
        title = _window_title(hwnd)
        if not title:
            return True
        pid = wintypes.DWORD()
        _USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not _matches(app, _process_path(pid.value)):
            return True
        rect = wintypes.RECT()
        if not _USER32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        width = max(0, rect.right - rect.left)
        height = max(0, rect.bottom - rect.top)
        if width >= 240 and height >= 180:
            candidates.append((width * height, int(hwnd), title, (width, height)))
        return True

    _USER32.EnumWindows(callback_type(visit), 0)
    if not candidates:
        return None
    _area, hwnd, title, size = max(candidates, key=lambda item: item[0])
    return hwnd, title, size


def list_apps() -> dict[str, Any]:
    apps = []
    for key, label in APPS.items():
        found = _find_window(key)
        apps.append({
            "id": key,
            "label": label,
            "available": found is not None,
            "title": found[1] if found else "",
        })
    return {"ok": True, "apps": apps}


def capture_jpeg(app: str) -> tuple[bytes | None, dict[str, Any]]:
    """Capture one real app window without foregrounding or interacting with it."""
    if app not in APPS:
        return None, {"ok": False, "error": "unknown native app"}
    with _CAPTURE_LOCKS[app]:
        found = _find_window(app)
        if found is None:
            return None, {"ok": False, "error": f"Open {APPS[app]} on the PC to monitor it."}
        hwnd, title, (width, height) = found
        window_dc = _USER32.GetWindowDC(hwnd)
        memory_dc = _GDI32.CreateCompatibleDC(window_dc)
        bitmap = _GDI32.CreateCompatibleBitmap(window_dc, width, height)
        previous = _GDI32.SelectObject(memory_dc, bitmap)
        try:
            if not _USER32.PrintWindow(hwnd, memory_dc, 2):
                return None, {"ok": False, "error": f"{APPS[app]} window capture failed."}
            info = _BitmapInfoHeader()
            info.biSize = ctypes.sizeof(_BitmapInfoHeader)
            info.biWidth = width
            info.biHeight = -height
            info.biPlanes = 1
            info.biBitCount = 32
            pixels = ctypes.create_string_buffer(width * height * 4)
            if not _GDI32.GetDIBits(memory_dc, bitmap, 0, height, pixels, ctypes.byref(info), 0):
                return None, {"ok": False, "error": f"{APPS[app]} window pixels were unavailable."}

            from PIL import Image

            image = Image.frombuffer("RGBA", (width, height), pixels, "raw", "BGRA", 0, 1).convert("RGB")
            image.thumbnail((1920, 1080), Image.Resampling.LANCZOS)
            output = BytesIO()
            image.save(output, "JPEG", quality=78, optimize=True)
            return output.getvalue(), {
                "ok": True,
                "app": app,
                "label": APPS[app],
                "title": title,
                "width": image.width,
                "height": image.height,
            }
        finally:
            _GDI32.SelectObject(memory_dc, previous)
            _GDI32.DeleteObject(bitmap)
            _GDI32.DeleteDC(memory_dc)
            _USER32.ReleaseDC(hwnd, window_dc)
