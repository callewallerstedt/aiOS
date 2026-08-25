"""Windows-specific Tk fixes shared by aiOS processes."""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes


def suppress_tk_monitor_windows(owner_pid: int | None = None) -> int:
    """Hide Tk's per-monitor DPI tracker windows (TtkMonitorClass).

    These are full-display HWNDs Tk creates when the process is DPI-aware.
    On Windows they sometimes flash as blank white frames on startup.
    """
    if not sys.platform.startswith("win"):
        return 0
    pid = int(owner_pid or os.getpid())
    user32 = ctypes.windll.user32
    SW_HIDE = 0
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_NOACTIVATE = 0x08000000
    WS_EX_TRANSPARENT = 0x00000020
    hidden = 0

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def enum_windows(hwnd, _lparam):
        nonlocal hidden
        class_name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_name, 256)
        if class_name.value != "TtkMonitorClass":
            return True
        window_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if int(window_pid.value) != pid:
            return True
        try:
            user32.ShowWindow(hwnd, SW_HIDE)
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(
                hwnd,
                GWL_EXSTYLE,
                style | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT,
            )
            # Park off-screen (do not pass SWP_NOMOVE/SWP_NOSIZE).
            user32.SetWindowPos(hwnd, 0, -32000, -32000, 1, 1, 0x0010)
            hidden += 1
        except OSError:
            pass
        return True

    try:
        user32.EnumWindows(enum_windows, 0)
    except (AttributeError, OSError):
        return 0
    return hidden
