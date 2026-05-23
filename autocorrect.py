import ctypes
import json
import os
import sys
import threading
import time
from ctypes import wintypes


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "corrections.json")
LOG_PATH = os.path.join(BASE_DIR, "autocorrect.log")
MUTEX_NAME = "Local\\CodexComputerHelperAutocorrect"

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104

VK_BACK = 0x08
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_F12 = 0x7B
VK_V = 0x56

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_LWIN = 0x5B
VK_RWIN = 0x5C

LLKHF_INJECTED = 0x10
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
ERROR_ALREADY_EXISTS = 183
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]


HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
)

user32.SetWindowsHookExW.argtypes = [
    ctypes.c_int,
    HOOKPROC,
    wintypes.HINSTANCE,
    wintypes.DWORD,
]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = [
    wintypes.HHOOK,
    ctypes.c_int,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.CallNextHookEx.restype = ctypes.c_long
user32.GetMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
]
user32.GetMessageW.restype = wintypes.BOOL
user32.GetKeyboardState.argtypes = [ctypes.POINTER(ctypes.c_ubyte * 256)]
user32.GetKeyboardState.restype = wintypes.BOOL
user32.ToUnicode.argtypes = [
    wintypes.UINT,
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_ubyte * 256),
    wintypes.LPWSTR,
    ctypes.c_int,
    wintypes.UINT,
]
user32.ToUnicode.restype = ctypes.c_int
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.restype = wintypes.HGLOBAL


corrections = {}
prefix_corrections = {}
enabled = True
last_config_mtime = 0.0
last_config_check = 0.0
word_buffer = []
hook_handle = None


def log(message):
    try:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_PATH, "a", encoding="utf-8") as file:
            file.write(f"{timestamp} {message}\n")
    except OSError:
        pass


def load_config(force=False):
    global corrections, prefix_corrections, enabled, last_config_mtime, last_config_check

    now = time.monotonic()
    if not force and now - last_config_check < 1:
        return
    last_config_check = now

    try:
        mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
        corrections = {}
        return

    if not force and mtime == last_config_mtime:
        return

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as file:
            config = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        log(f"Could not load corrections.json: {exc}")
        return

    enabled = bool(config.get("enabled", True))
    raw_corrections = config.get("corrections", config)
    corrections = {
        str(wrong).casefold(): str(right)
        for wrong, right in raw_corrections.items()
        if isinstance(wrong, str) and isinstance(right, str) and wrong
    }
    raw_prefix_corrections = config.get("prefixCorrections", {})
    prefix_corrections = {
        str(wrong).casefold(): str(right)
        for wrong, right in raw_prefix_corrections.items()
        if isinstance(wrong, str) and isinstance(right, str) and wrong
    }
    last_config_mtime = mtime
    log(
        "Loaded "
        f"{len(corrections)} corrections and "
        f"{len(prefix_corrections)} prefix corrections; enabled={enabled}"
    )


def is_key_down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def has_ctrl_alt_win():
    return (
        is_key_down(VK_CONTROL)
        or is_key_down(VK_MENU)
        or is_key_down(VK_LWIN)
        or is_key_down(VK_RWIN)
    )


def is_toggle_hotkey(vk):
    return (
        vk == VK_F12
        and is_key_down(VK_CONTROL)
        and is_key_down(VK_MENU)
        and is_key_down(VK_SHIFT)
    )


def translate_key(vk, scan_code):
    keyboard_state = (ctypes.c_ubyte * 256)()
    if not user32.GetKeyboardState(ctypes.byref(keyboard_state)):
        return ""

    if 0 <= vk < 256:
        keyboard_state[vk] |= 0x80

    buffer = ctypes.create_unicode_buffer(8)
    result = user32.ToUnicode(vk, scan_code, keyboard_state, buffer, len(buffer), 0)
    if result > 0:
        return buffer.value[:result]
    return ""


def is_word_char(text):
    return len(text) == 1 and (text.isalpha() or text == "'")


def match_case(original, replacement):
    if original.isupper():
        return replacement.upper()
    if len(original) > 1 and original[0].isupper() and original[1:].islower():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def find_correction(typed):
    exact = corrections.get(typed.casefold())
    if exact:
        return match_case(typed, exact)

    typed_key = typed.casefold()
    for wrong in sorted(prefix_corrections, key=len, reverse=True):
        if typed_key.startswith(wrong) and len(typed) > len(wrong):
            prefix = typed[: len(wrong)]
            suffix = typed[len(wrong) :]
            replacement = match_case(prefix, prefix_corrections[wrong])
            return replacement + suffix

    return ""


def make_input(vk=0, scan=0, flags=0):
    item = INPUT()
    item.type = 1
    item.union.ki = KEYBDINPUT(vk, scan, flags, 0, None)
    return item


def send_inputs(items):
    if not items:
        return
    array_type = INPUT * len(items)
    array = array_type(*items)
    user32.SendInput(len(items), array, ctypes.sizeof(INPUT))


def send_virtual_key(vk):
    send_inputs(
        [
            make_input(vk=vk),
            make_input(vk=vk, flags=KEYEVENTF_KEYUP),
        ]
    )


def send_modified_key(modifier, key):
    send_inputs(
        [
            make_input(vk=modifier),
            make_input(vk=key),
            make_input(vk=key, flags=KEYEVENTF_KEYUP),
            make_input(vk=modifier, flags=KEYEVENTF_KEYUP),
        ]
    )


def send_text(text):
    items = []
    for char in text:
        code = ord(char)
        if code > 0xFFFF:
            continue
        items.append(make_input(scan=code, flags=KEYEVENTF_UNICODE))
        items.append(make_input(scan=code, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    send_inputs(items)


def get_clipboard_text():
    if not user32.OpenClipboard(None):
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text):
    data = (text + "\0").encode("utf-16-le")
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        return False

    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        kernel32.GlobalFree(handle)
        return False

    try:
        ctypes.memmove(pointer, data, len(data))
    finally:
        kernel32.GlobalUnlock(handle)

    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(handle)
        return False

    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            return False
        return True
    finally:
        user32.CloseClipboard()


def paste_text(text):
    old_text = get_clipboard_text()
    if not set_clipboard_text(text):
        send_text(text)
        return

    send_modified_key(VK_CONTROL, VK_V)
    time.sleep(0.05)
    if old_text is not None:
        set_clipboard_text(old_text)


def delayed_replace(typed, replacement, boundary_text):
    time.sleep(0.02)
    for _ in typed:
        send_virtual_key(VK_BACK)
    paste_text(replacement + boundary_text)
    log(f"Corrected '{typed}' -> '{replacement}'")


def replace_current_word(boundary_text=""):
    typed = "".join(word_buffer)
    correction = find_correction(typed)
    word_buffer.clear()

    if not enabled or not correction:
        return False

    replacement = correction
    threading.Thread(
        target=delayed_replace,
        args=(typed, replacement, boundary_text),
        daemon=True,
    ).start()
    return True


def handle_key(vk, scan_code):
    global enabled

    load_config()

    if is_toggle_hotkey(vk):
        enabled = not enabled
        word_buffer.clear()
        log(f"Enabled toggled to {enabled}")
        return True

    if has_ctrl_alt_win():
        word_buffer.clear()
        return False

    if vk == VK_BACK:
        if word_buffer:
            word_buffer.pop()
        return False

    if vk == VK_ESCAPE:
        word_buffer.clear()
        return False

    if vk == VK_SPACE:
        return replace_current_word(boundary_text=" ")
    if vk == VK_RETURN:
        word_buffer.clear()
        return False
    if vk == VK_TAB:
        word_buffer.clear()
        return False

    text = translate_key(vk, scan_code)
    if is_word_char(text):
        word_buffer.append(text)
        if len(word_buffer) > 64:
            word_buffer.clear()
        return False

    if text:
        return replace_current_word(boundary_text=text)

    word_buffer.clear()
    return False


def hook_callback(n_code, w_param, l_param):
    if n_code == 0 and w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
        event = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
        if not event.flags & LLKHF_INJECTED:
            try:
                if handle_key(event.vkCode, event.scanCode):
                    return 1
            except Exception as exc:
                log(f"Hook error: {exc}")
                word_buffer.clear()

    return user32.CallNextHookEx(hook_handle, n_code, w_param, l_param)


def ensure_single_instance():
    kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        sys.exit(0)


def main():
    global hook_handle

    ensure_single_instance()
    load_config(force=True)

    callback = HOOKPROC(hook_callback)
    hook_handle = user32.SetWindowsHookExW(WH_KEYBOARD_LL, callback, None, 0)
    if not hook_handle:
        error = ctypes.get_last_error()
        log(f"Could not install keyboard hook: {error}")
        raise ctypes.WinError(error)

    log("Autocorrect started")
    message = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(message), None, 0, 0) != 0:
        pass


if __name__ == "__main__":
    main()
