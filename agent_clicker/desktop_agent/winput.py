"""Windows mouse input via SendInput (the modern, drawing-app-friendly API).

Why not SetCursorPos + mouse_event:
  - SetCursorPos moves the cursor but does NOT reliably generate WM_MOUSEMOVE
    messages — many apps (Paint, Photoshop, browsers in drag mode) read mouse
    moves from input events, not the cursor position. So while a button is
    held, SetCursorPos moves the cursor visually but the app sees no drag.
  - mouse_event is deprecated and has subtle multi-monitor coordinate bugs.

SendInput with MOUSEEVENTF_VIRTUALDESK gives proper input-stream events that
EVERY drawing app respects, on every monitor including negative virtual coords.
"""
from __future__ import annotations
import ctypes
import ctypes.wintypes as wt
import time

_u = ctypes.windll.user32

# --- system metrics for virtual-desktop normalization ---------------------
SM_XVIRTUALSCREEN  = 76
SM_YVIRTUALSCREEN  = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


def _virt_rect() -> tuple[int, int, int, int]:
    """Return (left, top, width, height) of the virtual desktop."""
    return (
        _u.GetSystemMetrics(SM_XVIRTUALSCREEN),
        _u.GetSystemMetrics(SM_YVIRTUALSCREEN),
        _u.GetSystemMetrics(SM_CXVIRTUALSCREEN),
        _u.GetSystemMetrics(SM_CYVIRTUALSCREEN),
    )


# --- SendInput plumbing ---------------------------------------------------
# NOTE: Windows' INPUT struct has a SINGLE union over MOUSEINPUT/KEYBDINPUT/
# HARDWAREINPUT. cbSize passed to SendInput MUST equal sizeof(INPUT) which is
# fixed (40 bytes on 64-bit). Having separate INPUT structs per input type
# yields the wrong size and gets rejected with GetLastError=87
# (ERROR_INVALID_PARAMETER). All three field types live in one union here.
class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG), ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD), ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wt.DWORD),
        ("wParamL", wt.WORD), ("wParamH", wt.WORD),
    ]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT),
                    ("ki", _KEYBDINPUT),
                    ("hi", _HARDWAREINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _U)]


INPUT_MOUSE              = 0
MOUSEEVENTF_MOVE         = 0x0001
MOUSEEVENTF_LEFTDOWN     = 0x0002
MOUSEEVENTF_LEFTUP       = 0x0004
MOUSEEVENTF_RIGHTDOWN    = 0x0008
MOUSEEVENTF_RIGHTUP      = 0x0010
MOUSEEVENTF_MIDDLEDOWN   = 0x0020
MOUSEEVENTF_MIDDLEUP     = 0x0040
MOUSEEVENTF_WHEEL        = 0x0800
MOUSEEVENTF_ABSOLUTE     = 0x8000
MOUSEEVENTF_VIRTUALDESK  = 0x4000

_BTN_FLAGS = {
    "left":   (MOUSEEVENTF_LEFTDOWN,   MOUSEEVENTF_LEFTUP),
    "right":  (MOUSEEVENTF_RIGHTDOWN,  MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def _send(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> None:
    inp = _INPUT()
    inp.type = INPUT_MOUSE
    inp.mi = _MOUSEINPUT(dx, dy, data, flags, 0, None)
    n = _u.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
    if n != 1:
        # very unusual — UIPI / locked workstation / etc.
        err = ctypes.GetLastError()
        raise OSError(f"SendInput failed (n={n}, GetLastError={err})")


def _abs_xy(x: int, y: int) -> tuple[int, int]:
    """Convert virtual-screen pixel (x,y) -> normalized 0..65535 absolute coords."""
    vx, vy, vw, vh = _virt_rect()
    # +1 like Microsoft sample to avoid rounding into adjacent pixel
    ax = int((int(x) - vx) * 65535 / max(1, vw - 1))
    ay = int((int(y) - vy) * 65535 / max(1, vh - 1))
    return ax, ay


# --- public API -----------------------------------------------------------
class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def position() -> tuple[int, int]:
    p = _POINT()
    _u.GetCursorPos(ctypes.byref(p))
    return (p.x, p.y)


def move_to(x: int, y: int) -> None:
    """Single absolute move event. Always emits WM_MOUSEMOVE."""
    ax, ay = _abs_xy(x, y)
    _send(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, ax, ay)


def mouse_down(button: str = "left") -> None:
    down, _up = _BTN_FLAGS[button]
    _send(down)


def mouse_up(button: str = "left") -> None:
    _down, up = _BTN_FLAGS[button]
    _send(up)


def click(button: str = "left", clicks: int = 1, interval: float = 0.06, cancel_event=None) -> None:
    down, up = _BTN_FLAGS[button]
    for i in range(clicks):
        if cancel_event is not None and cancel_event.is_set():
            return
        _send(down)
        _sleep_cancelable(0.02, cancel_event)
        if cancel_event is not None and cancel_event.is_set():
            _send(up)
            return
        _send(up)
        if i < clicks - 1:
            _sleep_cancelable(interval, cancel_event)


def scroll(dy: int) -> None:
    _send(MOUSEEVENTF_WHEEL, 0, 0, int(dy) * 120)


def move_rel(dx: int, dy: int) -> None:
    """Relative mouse move via SendInput (NO ABSOLUTE flag).

    Use this for FPS / mouse-look games. They read raw mouse deltas through
    DirectInput or Raw Input and ignore SetCursorPos-style absolute moves.
    `dx`/`dy` are pixel deltas (positive dx = right, positive dy = down).
    """
    _send(MOUSEEVENTF_MOVE, int(dx), int(dy))


# --- KEYBOARD via SendInput with scan codes -------------------------------
# pyautogui's keyboard path uses keybd_event under the hood, which many games
# and most anti-cheats ignore. SendInput with KEYEVENTF_SCANCODE is the real
# hardware-equivalent path — accepted by Steam / Unity / Unreal / DirectInput.

INPUT_KEYBOARD          = 1
KEYEVENTF_EXTENDEDKEY   = 0x0001
KEYEVENTF_KEYUP         = 0x0002
KEYEVENTF_SCANCODE      = 0x0008

# Scan-code map for keys games actually care about. Extended keys (arrows,
# numpad nav, right alt/ctrl) need the EXTENDEDKEY flag *and* the scan code
# with high byte 0xE0.
_SCAN = {
    # letters
    "a": 0x1E, "b": 0x30, "c": 0x2E, "d": 0x20, "e": 0x12, "f": 0x21,
    "g": 0x22, "h": 0x23, "i": 0x17, "j": 0x24, "k": 0x25, "l": 0x26,
    "m": 0x32, "n": 0x31, "o": 0x18, "p": 0x19, "q": 0x10, "r": 0x13,
    "s": 0x1F, "t": 0x14, "u": 0x16, "v": 0x2F, "w": 0x11, "x": 0x2D,
    "y": 0x15, "z": 0x2C,
    # number row
    "1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06,
    "6": 0x07, "7": 0x08, "8": 0x09, "9": 0x0A, "0": 0x0B,
    # modifiers & common
    "esc": 0x01, "escape": 0x01,
    "tab": 0x0F, "space": 0x39, " ": 0x39,
    "enter": 0x1C, "return": 0x1C,
    "backspace": 0x0E, "back": 0x0E, "bksp": 0x0E,
    "shift": 0x2A, "lshift": 0x2A, "rshift": 0x36,
    "ctrl": 0x1D, "lctrl": 0x1D,
    "alt": 0x38, "lalt": 0x38,
    "capslock": 0x3A, "caps": 0x3A,
    "minus": 0x0C, "-": 0x0C, "equals": 0x0D, "=": 0x0D,
    "[": 0x1A, "]": 0x1B, ";": 0x27, "'": 0x28, "`": 0x29,
    "\\": 0x2B, ",": 0x33, ".": 0x34, "/": 0x35,
    # F-keys
    "f1": 0x3B, "f2": 0x3C, "f3": 0x3D, "f4": 0x3E, "f5": 0x3F,
    "f6": 0x40, "f7": 0x41, "f8": 0x42, "f9": 0x43, "f10": 0x44,
    "f11": 0x57, "f12": 0x58,
}
# Extended keys: scan code OR'd with 0xE000 conceptually; we pass the low byte
# as scan and set EXTENDEDKEY flag. Marked separately so the flag is set.
_SCAN_EXT = {
    "up": 0x48, "down": 0x50, "left": 0x4B, "right": 0x4D,
    "ins": 0x52, "insert": 0x52, "del": 0x53, "delete": 0x53,
    "home": 0x47, "end": 0x4F, "pageup": 0x49, "pgup": 0x49,
    "pagedown": 0x51, "pgdn": 0x51,
    "rctrl": 0x1D, "ralt": 0x38, "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C,
}


def _scan_for(key: str) -> tuple[int, bool]:
    """Return (scan_code, is_extended). Raises KeyError if unmapped."""
    k = key.lower().strip()
    if k in _SCAN_EXT:
        return _SCAN_EXT[k], True
    if k in _SCAN:
        return _SCAN[k], False
    raise KeyError(f"no scan code for key {key!r} — supported keys: "
                   "letters, digits, esc, tab, space, enter, backspace, "
                   "shift, ctrl, alt, caps, arrows, ins/del/home/end/pgup/pgdn, "
                   "win, f1..f12, and standard punctuation")


def _send_key(scan: int, extended: bool, key_up: bool) -> None:
    flags = KEYEVENTF_SCANCODE
    if extended:
        flags |= KEYEVENTF_EXTENDEDKEY
    if key_up:
        flags |= KEYEVENTF_KEYUP
    inp = _INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki = _KEYBDINPUT(0, scan, flags, 0, None)
    n = _u.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
    if n != 1:
        err = ctypes.GetLastError()
        raise OSError(f"SendInput(keyboard) failed (n={n}, GetLastError={err})")


def key_down(key: str) -> None:
    scan, ext = _scan_for(key)
    _send_key(scan, ext, key_up=False)


def key_up(key: str) -> None:
    scan, ext = _scan_for(key)
    _send_key(scan, ext, key_up=True)


def key_tap(key: str, hold: float = 0.0) -> None:
    """Press, optionally hold, release. Uses scan codes."""
    scan, ext = _scan_for(key)
    _send_key(scan, ext, key_up=False)
    if hold > 0:
        time.sleep(hold)
    _send_key(scan, ext, key_up=True)


def slide_to(x: int, y: int, duration: float, frame_dt: float = 1.0 / 90.0, cancel_event=None) -> None:
    """Stream MOUSEEVENTF_MOVE events frame-by-frame so apps see a continuous
    drag while a button is held. ease-out, ~90 fps, min 8 frames."""
    if cancel_event is not None and cancel_event.is_set():
        return
    duration = max(0.0, float(duration))
    cx, cy = position()
    fx, fy = int(x), int(y)
    dx, dy_ = fx - cx, fy - cy
    dist = (dx * dx + dy_ * dy_) ** 0.5
    if duration < 0.005 or dist < 1.0:
        move_to(fx, fy)
        return
    frames = max(8, int(duration / frame_dt))
    for i in range(1, frames + 1):
        if cancel_event is not None and cancel_event.is_set():
            return
        t = i / frames
        e = 1.0 - (1.0 - t) ** 2     # quadratic ease-out
        move_to(cx + dx * e, cy + dy_ * e)
        _sleep_cancelable(frame_dt, cancel_event)


def _sleep_cancelable(seconds: float, cancel_event=None) -> None:
    if cancel_event is None:
        time.sleep(seconds)
        return
    end = time.time() + max(0.0, float(seconds))
    while not cancel_event.is_set():
        remaining = end - time.time()
        if remaining <= 0:
            return
        time.sleep(min(0.02, remaining))
