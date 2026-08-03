import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.error
import urllib.parse
import urllib.request
from tkinter import colorchooser, filedialog, messagebox, simpledialog

import aios_codex_accounts
import codex_usage
import model_pricing
from voice_settings import (
    COMPUTE_TYPES,
    DEFAULT_VOICE_DICTATION,
    LANGUAGE_LABELS,
    SAFE_HOTKEYS,
    VOICE_HOTKEY_OPTIONS,
    WHISPER_LANGUAGES,
    WHISPER_MODELS,
    load_voice_dictation_settings,
    merge_voice_dictation,
    normalize_voice_hotkey,
    resolve_transcribe_language,
    voice_hotkey_label,
    voice_hotkey_to_ahk,
)


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "helper_config.json"
PROJECT_META_FILE = "aios-project.json"
PROJECT_SUMMARY_FILE = "aios-summary.md"
DEFAULT_PROJECT_META = {
    "title": "",
    "status": "active",
    "priority": "normal",
    "due": "",
    "done": False,
    "tags": [],
    "notes": "",
    "tracked_as_todo": False,
    "created": "",
}
HOST = "127.0.0.1"
PORT = 48736
MAX_COMMAND_BYTES = 8 * 1024 * 1024
CODEX_HOME = aios_codex_accounts.active_home(CONFIG_PATH)
os.environ["AIOS_ACTIVE_CODEX_HOME"] = str(CODEX_HOME)
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
CREATE_NEW_CONSOLE = 0x00000010 if os.name == "nt" else 0
OPERATOR_DEFAULT_MODEL = "gpt-5.6-luna"
# The house default for chat, quick answers and Codex. luna is the fast, cheap
# tier of the 5.6 family; raise an individual one to terra or sol in Settings.
DEFAULT_CHAT_MODEL = "gpt-5.6-luna"
# Retired model names that still linger in older helper_config.json files.
LEGACY_MODELS = frozenset({
    "gpt-4o-mini", "gpt-4.1-nano", "gpt-4.1-mini", "gpt-4.1",
    "gpt-5-mini", "gpt-5-nano", "gpt-5",
    "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.5",
})
# Text files the phone attaches are read this far and no further; the agent
# loop trims them again before they reach the model.
PHONE_TEXT_ATTACHMENT_LIMIT = 256 * 1024
DEFAULT_PHONE_RELAY_URL = "https://aios-remote-control.contact-wallerstedt.chatgpt.site"
DEFAULT_PROJECT_ROOT = r"D:\Projects" if Path("D:\\").exists() else str(Path.home() / "Documents" / "aiOS Projects")
TRANSPARENT = "#010203"
WM_DROPFILES = 0x0233
BRAND_FONT_PATH = BASE_DIR / "assets" / "fonts" / "Michroma-Regular.ttf"
BRAND_FONT_FAMILY = "Michroma"
STARTUP_FRAME_DIR = BASE_DIR / "assets" / "startup" / "aios-logo-reveal-frames"
HELPER_HEARTBEAT_PATH = BASE_DIR / ".aios-helper-heartbeat"
APP_ICON_PATH = BASE_DIR / "assets" / "aios-logo.ico"
TRAY_ICON_PATH = BASE_DIR / "assets" / "rectangle-logo.ico"
CODE_PROVIDER_ICON_DIR = BASE_DIR / "assets" / "providers"
CODE_PROVIDER_CHOICES = (
    ("codex", "chatgpt.png", "ChatGPT Codex"),
    ("claude", "claude.png", "Claude"),
    ("cursor", "cursor.png", "Cursor"),
)
APP_USER_MODEL_ID = "aiOS.Desktop.Helper"
APP_MUTEX_NAME = "Local\\aiOS.Desktop.Helper.Singleton"
APP_MUTEX_HANDLE = None
AGENT_CLICKER_DIR = BASE_DIR / "agent_clicker"
RECORDINGS_FOLDER_NAME = "aiOS recordings"
FR_PRIVATE = 0x10
GWL_EXSTYLE = -20
GWLP_WNDPROC = -4
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
WS_POPUP = 0x80000000
SW_SHOWNOACTIVATE = 4
SW_HIDE = 0
HWND_TOPMOST = -1
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
LWA_ALPHA = 0x00000002
WDA_NONE = 0x00000000
WDA_MONITOR = 0x00000001
WDA_EXCLUDEFROMCAPTURE = 0x00000011
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
BI_RGB = 0
WM_APP = 0x8000
WM_TRAYICON = WM_APP + 1
WM_COMMAND = 0x0111
WM_DESTROY = 0x0002
WM_NCHITTEST = 0x0084
HTTRANSPARENT = -1
OPERATOR_INPUT_TAG = 0xA105C11C
GA_ROOT = 2
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203
NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010
NIIF_INFO = 0x00000001
NIIF_WARNING = 0x00000002
NIIF_ERROR = 0x00000003
IDI_APPLICATION = 32512
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010
LR_DEFAULTSIZE = 0x00000040
MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
TPM_RIGHTBUTTON = 0x00000002
TPM_RETURNCMD = 0x00000100
TRAY_SHOW = 1001
TRAY_HIDE = 1002
TRAY_START_VOICE = 1003
TRAY_RESTART_VOICE = 1004
TRAY_RESTART_MACROS = 1005
TRAY_RESTART_APP = 1006
TRAY_QUIT = 1007


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
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


class RGBQUAD(ctypes.Structure):
    _fields_ = [
        ("rgbBlue", ctypes.c_ubyte),
        ("rgbGreen", ctypes.c_ubyte),
        ("rgbRed", ctypes.c_ubyte),
        ("rgbReserved", ctypes.c_ubyte),
    ]


class NativeOperatorOverlay:
    CLASS_NAME = "aiOSOperatorOverlay"

    def __init__(
        self,
        owner,
        title_text="aiOPERATOR controlling computer",
        log_title="aiOPERATOR LOG",
        class_name=None,
        compact=False,
    ):
        self.owner = owner
        self.class_name = class_name or self.CLASS_NAME
        self.windows = {}
        self.labels = {}
        self.log_text = ""
        self.title_text = title_text
        self.log_title = log_title
        self.compact = bool(compact)
        self.placements = {}
        self.enabled = sys.platform.startswith("win")
        self._wndproc = None
        if self.enabled:
            self._init_win32()

    def _init_win32(self):
        self.user32 = ctypes.windll.user32
        self.gdi32 = ctypes.windll.gdi32
        self.kernel32 = ctypes.windll.kernel32
        self.hinstance = self.kernel32.GetModuleHandleW(None)
        self.user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
        self.user32.DefWindowProcW.restype = ctypes.c_ssize_t
        self.user32.CreateWindowExW.restype = wintypes.HWND
        self.user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.UINT,
        ]
        self.user32.SetWindowPos.restype = wintypes.BOOL

        wndproc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t)

        def wndproc(hwnd, message, wparam, lparam):
            if message == 0x000F:
                return self._paint(hwnd)
            if message == 0x0021:
                return 3
            if message == WM_NCHITTEST:
                return HTTRANSPARENT
            return self.user32.DefWindowProcW(hwnd, message, wparam, lparam)

        self._wndproc = wndproc_type(wndproc)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", wndproc_type),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HANDLE),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HANDLE),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = self.hinstance
        wc.lpszClassName = self.class_name
        try:
            self.user32.RegisterClassW(ctypes.byref(wc))
        except OSError:
            pass

    def ensure(self):
        if not self.enabled or self.windows:
            return
        exstyle = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE
        names = ("log",) if self.compact else ("top", "bottom", "left", "right", "label", "log")
        for name in names:
            hwnd = self.user32.CreateWindowExW(
                exstyle,
                self.class_name,
                name,
                WS_POPUP,
                0,
                0,
                1,
                1,
                None,
                None,
                self.hinstance,
                None,
            )
            if not hwnd:
                continue
            self.windows[name] = hwnd
            self.labels[hwnd] = name
            alpha = 238 if self.compact else 210 if name == "log" else 190
            self.user32.SetLayeredWindowAttributes(hwnd, 0, alpha, LWA_ALPHA)
            # Visible to normal screenshots/screen sharing. Agent captures apply
            # exclusion only around the frame grab itself.
            self.user32.SetWindowDisplayAffinity(hwnd, WDA_NONE)

    def show(self, monitor):
        self.ensure()
        if not self.windows:
            return
        inset = 9
        left, top = int(monitor.left), int(monitor.top)
        width, height = int(monitor.width), int(monitor.height)
        if self.compact:
            log_w = min(520, max(360, int(width * 0.30)))
            log_h = min(150, max(112, int(height * 0.14)))
            self.placements = {
                "log": (
                    left + width - inset - log_w - 18,
                    top + height - inset - log_h - 24,
                    log_w,
                    log_h,
                ),
            }
        else:
            border = 8
            label_w = min(560, max(280, width - 120))
            log_w = min(580, max(360, int(width * 0.34)))
            log_h = min(190, max(120, int(height * 0.20)))
            self.placements = {
                "top": (left + inset, top + inset, max(1, width - inset * 2), border),
                "bottom": (left + inset, top + height - inset - border, max(1, width - inset * 2), border),
                "left": (left + inset, top + inset, border, max(1, height - inset * 2)),
                "right": (left + width - inset - border, top + inset, border, max(1, height - inset * 2)),
                "label": (left + max(20, (width - label_w) // 2), top + inset + 18, label_w, 38),
                "log": (left + width - inset - log_w - 12, top + height - inset - log_h - 22, log_w, log_h),
            }
        for name, hwnd in self.windows.items():
            x, y, w, h = self.placements[name]
            # WS_EX_TOPMOST alone is not enough once another topmost or
            # fullscreen window changes the z-order. SetWindowPos both shows
            # and explicitly reasserts the topmost band without stealing focus.
            self.user32.SetWindowPos(
                hwnd, HWND_TOPMOST, x, y, w, h,
                SWP_NOACTIVATE | SWP_SHOWWINDOW,
            )
            self.user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)

    def hide(self):
        if not self.enabled:
            return
        for hwnd in self.windows.values():
            try:
                self.user32.ShowWindow(hwnd, SW_HIDE)
            except OSError:
                pass

    def update(self, wave):
        if not self.enabled:
            return
        alpha = int(175 + wave * 80)
        label_alpha = int(210 + wave * 45)
        log_alpha = 238 if self.compact else int(220 + wave * 30)
        for name, hwnd in self.windows.items():
            value = log_alpha if name == "log" else label_alpha if name == "label" else alpha
            self.user32.SetLayeredWindowAttributes(hwnd, 0, max(0, min(255, value)), LWA_ALPHA)
            placement = self.placements.get(name)
            if placement:
                x, y, w, h = placement
                self.user32.SetWindowPos(
                    hwnd, HWND_TOPMOST, x, y, w, h,
                    SWP_NOACTIVATE | SWP_SHOWWINDOW,
                )
            self.user32.InvalidateRect(hwnd, None, True)

    def set_log(self, text, title=None):
        self.log_text = text or ""
        if title:
            self.log_title = str(title)
        hwnd = self.windows.get("log")
        if hwnd:
            self.user32.InvalidateRect(hwnd, None, True)

    def destroy(self):
        if not self.enabled:
            return
        for hwnd in list(self.windows.values()):
            try:
                self.user32.DestroyWindow(hwnd)
            except OSError:
                pass
        self.windows.clear()
        self.labels.clear()

    def _paint(self, hwnd):
        class PAINTSTRUCT(ctypes.Structure):
            _fields_ = [
                ("hdc", wintypes.HDC),
                ("fErase", wintypes.BOOL),
                ("rcPaint", wintypes.RECT),
                ("fRestore", wintypes.BOOL),
                ("fIncUpdate", wintypes.BOOL),
                ("rgbReserved", ctypes.c_byte * 32),
            ]

        ps = PAINTSTRUCT()
        hdc = self.user32.BeginPaint(hwnd, ctypes.byref(ps))
        try:
            rect = wintypes.RECT()
            self.user32.GetClientRect(hwnd, ctypes.byref(rect))
            if self.labels.get(hwnd) == "label":
                self._paint_label(hdc, rect)
            elif self.labels.get(hwnd) == "log":
                self._paint_log(hdc, rect)
            else:
                self._paint_solid(hdc, rect)
        finally:
            self.user32.EndPaint(hwnd, ctypes.byref(ps))
        return 0

    def _paint_solid(self, hdc, rect):
        brush = self.gdi32.CreateSolidBrush(self._colorref(self.owner.c("accent")))
        try:
            self.user32.FillRect(hdc, ctypes.byref(rect), brush)
        finally:
            self.gdi32.DeleteObject(brush)

    def _paint_label(self, hdc, rect):
        brush = self.gdi32.CreateSolidBrush(self._colorref(self.owner.c("panel")))
        try:
            self.user32.FillRect(hdc, ctypes.byref(rect), brush)
        finally:
            self.gdi32.DeleteObject(brush)
        self.gdi32.SetBkMode(hdc, 1)
        self.gdi32.SetTextColor(hdc, self._colorref(self.owner.c("text")))
        font = self.gdi32.CreateFontW(-16, 0, 0, 0, 700, 0, 0, 0, 0, 0, 0, 5, 0, getattr(self.owner, "brand_font_family", "Segoe UI"))
        old = self.gdi32.SelectObject(hdc, font)
        try:
            self.user32.DrawTextW(hdc, self.title_text, -1, ctypes.byref(rect), 0x00000001 | 0x00000004 | 0x00000020)
        finally:
            self.gdi32.SelectObject(hdc, old)
            self.gdi32.DeleteObject(font)

    def _paint_log(self, hdc, rect):
        panel_brush = self.gdi32.CreateSolidBrush(self._colorref(self.owner.c("panel")))
        accent_brush = self.gdi32.CreateSolidBrush(self._colorref(self.owner.c("accent")))
        try:
            self.user32.FillRect(hdc, ctypes.byref(rect), panel_brush)
            if not self.compact:
                frame = wintypes.RECT(rect.left, rect.top, rect.right, rect.top + 3)
                self.user32.FillRect(hdc, ctypes.byref(frame), accent_brush)
        finally:
            self.gdi32.DeleteObject(panel_brush)
            self.gdi32.DeleteObject(accent_brush)
        self.gdi32.SetBkMode(hdc, 1)
        title_rect = wintypes.RECT(rect.left + 12, rect.top + 9, rect.right - 12, rect.top + 30)
        self.gdi32.SetTextColor(hdc, self._colorref(self.owner.c("accent")))
        title_font = self.gdi32.CreateFontW(-13, 0, 0, 0, 700, 0, 0, 0, 0, 0, 0, 5, 0, getattr(self.owner, "brand_font_family", "Segoe UI"))
        old = self.gdi32.SelectObject(hdc, title_font)
        try:
            self.user32.DrawTextW(hdc, self.log_title, -1, ctypes.byref(title_rect), 0x00000000 | 0x00000020)
        finally:
            self.gdi32.SelectObject(hdc, old)
            self.gdi32.DeleteObject(title_font)

        text_rect = wintypes.RECT(rect.left + 12, rect.top + 32, rect.right - 12, rect.bottom - 9)
        self.gdi32.SetTextColor(hdc, self._colorref(self.owner.c("text")))
        font = self.gdi32.CreateFontW(-13 if self.compact else -12, 0, 0, 0, 400, 0, 0, 0, 0, 0, 0, 5, 0, "Segoe UI")
        old = self.gdi32.SelectObject(hdc, font)
        try:
            text = self.log_text or "waiting..."
            self.user32.DrawTextW(hdc, text, -1, ctypes.byref(text_rect), 0x00000000 | 0x00000010 | 0x00000800)
        finally:
            self.gdi32.SelectObject(hdc, old)
            self.gdi32.DeleteObject(font)

    def _colorref(self, color):
        color = str(color).lstrip("#")
        if len(color) != 6:
            color = "ffffff"
        return int(color[0:2], 16) | (int(color[2:4], 16) << 8) | (int(color[4:6], 16) << 16)


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", RGBQUAD * 1)]


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


def get_setting(name, default=""):
    value = os.environ.get(name)
    if value:
        return value

    try:
        import winreg

        locations = (
            (winreg.HKEY_CURRENT_USER, r"Environment"),
            (
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
            ),
        )
        for root, path in locations:
            try:
                with winreg.OpenKey(root, path) as key:
                    value, _kind = winreg.QueryValueEx(key, name)
                    if value:
                        return str(value)
            except OSError:
                continue
    except OSError:
        pass

    return default


def set_windows_app_id():
    if not sys.platform.startswith("win"):
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except (AttributeError, OSError):
        pass


def enable_per_monitor_dpi_awareness():
    """Keep mss physical pixels and SendInput/Tk coordinates in one space."""
    if not sys.platform.startswith("win"):
        return
    try:
        # PER_MONITOR_AWARE_V2; must run before Tk creates any HWND.
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def claim_single_instance():
    """Close the startup race between the hotkey and Windows startup launcher."""
    global APP_MUTEX_HANDLE
    if not sys.platform.startswith("win"):
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, APP_MUTEX_NAME)
        if not handle:
            return True
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return False
        APP_MUTEX_HANDLE = handle
    except (AttributeError, OSError):
        return True
    return True


DEFAULT_CONFIG = {
    "project_root": get_setting("COMPUTER_HELPER_PROJECT_ROOT", DEFAULT_PROJECT_ROOT),
    "assistant_model": get_setting("COMPUTER_HELPER_MODEL", DEFAULT_CHAT_MODEL),
    "codex_model": DEFAULT_CHAT_MODEL,
    "codex_reasoning": "none",
    "quick_codex_model": DEFAULT_CHAT_MODEL,
    "quick_codex_reasoning": "none",
    "chat_model": DEFAULT_CHAT_MODEL,
    "openai_api_key": "",
    "codex_sandbox": "workspace-write",
    "window": "1120x720+520+90",
    "chat_width": 380,
    "app_usage": {},
    "chat_history": [],
    "todos": [],
    "linked_projects": [],
    "hidden_projects": [],
    "ai_operator": {
        "monitor": "",
        "model": OPERATOR_DEFAULT_MODEL,
        "planner_model": "gpt-5.6-sol",
        "reasoning": "low",
        "steps": "25",
        "delay": "0.20",
        "tts": False,
        "voice": "nova",
        "shell": True,
        "codex_auth": False,
        "provider_mode": "api",
    },
    "phone_relay": {
        "url": DEFAULT_PHONE_RELAY_URL,
        "machine_id": "",
        "machine_token": "",
        "machine_name": os.environ.get("COMPUTERNAME", "My computer"),
        "enabled": False,
    },
    "voice_dictation": dict(DEFAULT_VOICE_DICTATION),
    "dashboard": {
        "notes": "",
        "location": "Lerkil, Sweden",
        "tickers": ["^OMX", "TSLA", "NVDA", "BTC-USD", "SEK=X"],
    },
    "theme": {
        "accent": "#61dafb",
        "panel": "#101722",
        "panel2": "#151f2d",
        "surface": "#0b111b",
        "surface2": "#111a27",
        "text": "#f4f7fb",
        "muted": "#8b98aa",
        "danger": "#ff5f57",
        "success": "#38d996",
        "opacity": 0.94,
        "font_size": 10,
        "radius": 28,
        "always_on_top": True,
        "thinking_base": "#2b3340",
        "thinking_base_opacity": 45,
        "thinking_pulse": "#ffffff",
        "thinking_pulse_opacity": 100,
    },
}

SYSTEM_PROMPT = (
    "You are aiOS, a compact Windows desktop assistant. "
    "Be concise and practical. Prefer direct local commands and concrete steps. "
    "The user uses this app to manage projects, launch apps, import files, and run Codex."
)

KNOWN_APPS = {
    "AI": ["ChatGPT", "Claude", "Codex", "Ollama", "Google AI Studio", "DeepSeek"],
    "Work": [
        "Visual Studio Code",
        "Cursor",
        "Firefox",
        "Google Chrome",
        "Opera GX",
        "Discord",
        "Slack",
        "Microsoft Teams",
        "Zoom",
        "Outlook",
        "OneDrive",
    ],
    "Creative": [
        "Adobe Photoshop",
        "Photoshop",
        "Blender",
        "DaVinci Resolve",
        "OBS Studio",
        "Autodesk Fusion",
        "Revit",
    ],
    "Music": ["FL Studio 2025", "FL Studio 2024", "FL Studio 20", "Kontakt 7", "Spotify"],
    "Games": [
        "Steam",
        "Epic Games Launcher",
        "Riot Client",
        "League of Legends",
        "VALORANT",
        "Roblox",
        "Battle.net",
        "Rockstar Games Launcher",
        "BeamMP-Launcher",
        "osu!",
        "cs2",
    ],
    "System": [
        "Explorer",
        "Downloads",
        "Desktop",
        "PowerShell",
        "Command Prompt",
        "Task Manager",
        "Settings",
        "Control Panel",
    ],
}

SYSTEM_APPS = {
    "Explorer": ("command", ["explorer.exe"]),
    "Downloads": ("path", str(Path.home() / "Downloads")),
    "Desktop": ("path", str(Path.home() / "Desktop")),
    "PowerShell": ("command", ["powershell.exe"]),
    "Command Prompt": ("command", ["cmd.exe"]),
    "Task Manager": ("command", ["taskmgr.exe"]),
    "Settings": ("uri", "ms-settings:"),
    "Control Panel": ("command", ["control.exe"]),
}

IMAGE_FILE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"}


def merge_dict(base, override):
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_dict(result[key], value)
        else:
            result[key] = value
    return result


def load_config():
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    loaded_operator = {}
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as file:
                loaded = json.load(file)
                loaded_operator = loaded.get("ai_operator") if isinstance(loaded.get("ai_operator"), dict) else {}
                config = merge_dict(config, loaded)
        except (OSError, json.JSONDecodeError):
            pass

    if str(config.get("project_root", "")).casefold() == r"c:\codex":
        config["project_root"] = DEFAULT_PROJECT_ROOT
    project_root = Path(str(config.get("project_root") or DEFAULT_PROJECT_ROOT))
    if project_root.drive and not Path(f"{project_root.drive}\\").exists():
        config["project_root"] = DEFAULT_PROJECT_ROOT
    config.setdefault("assistant_model", DEFAULT_CHAT_MODEL)
    config.setdefault("codex_model", DEFAULT_CHAT_MODEL)
    config.setdefault("codex_reasoning", "none")
    config.setdefault("quick_codex_model", DEFAULT_CHAT_MODEL)
    config.setdefault("quick_codex_reasoning", "none")
    config.setdefault("chat_model", DEFAULT_CHAT_MODEL)
    # Everything else in aiOS runs on the 5.6 family; these four were still
    # pinned to retired names and showed up as "gpt-4.1-nano" in the UI.
    for key in ("assistant_model", "codex_model", "quick_codex_model", "chat_model"):
        if config.get(key) in LEGACY_MODELS:
            config[key] = DEFAULT_CHAT_MODEL
    config.setdefault("openai_api_key", "")
    config.setdefault("codex_sandbox", "workspace-write")
    config.setdefault("todos", [])
    config.setdefault("linked_projects", [])
    config.setdefault("hidden_projects", [])
    config["ai_operator"] = merge_dict(DEFAULT_CONFIG["ai_operator"], config.get("ai_operator") or {})
    if not str(loaded_operator.get("provider_mode") or "").strip():
        config["ai_operator"]["provider_mode"] = "codex" if loaded_operator.get("codex_auth") else "api"
    config["phone_relay"] = merge_dict(DEFAULT_CONFIG["phone_relay"], config.get("phone_relay") or {})
    if config["ai_operator"].get("model") == "gpt-5.5":
        config["ai_operator"]["model"] = OPERATOR_DEFAULT_MODEL
    config["voice_dictation"] = merge_voice_dictation(config.get("voice_dictation"))
    config["dashboard"] = merge_dict(DEFAULT_CONFIG["dashboard"], config.get("dashboard") or {})
    if not isinstance(config["dashboard"].get("tickers"), list) or not config["dashboard"]["tickers"]:
        config["dashboard"]["tickers"] = list(DEFAULT_CONFIG["dashboard"]["tickers"])
    migrate_legacy_todos(config)
    return config


def migrate_legacy_todos(config):
    legacy = config.get("todos") or []
    if not legacy:
        return
    root = Path(config.get("project_root") or DEFAULT_CONFIG["project_root"])
    root.mkdir(parents=True, exist_ok=True)
    changed = False
    for item in legacy:
        if not isinstance(item, dict):
            continue
        project_name = str(item.get("project") or "Inbox").strip()
        title = str(item.get("title") or "").strip()
        if project_name == "Inbox":
            project_name = clean_project_name(title) or f"Todo-{item.get('id', int(time.time()))}"
        project_path = root / project_name
        if not project_path.exists():
            project_path.mkdir(parents=True, exist_ok=True)
            for folder in ("src", "docs", "assets", "notes"):
                (project_path / folder).mkdir(exist_ok=True)
            readme = project_path / "README.md"
            if not readme.exists():
                readme.write_text(f"# {project_name}\n\n", encoding="utf-8")
        meta_path = project_path / PROJECT_META_FILE
        meta = dict(DEFAULT_PROJECT_META)
        if meta_path.exists():
            try:
                meta.update(json.loads(meta_path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                pass
        meta["title"] = title or meta.get("title") or project_name
        meta["due"] = str(item.get("due") or meta.get("due") or "").strip()
        meta["priority"] = str(item.get("priority") or meta.get("priority") or "normal")
        meta["done"] = bool(item.get("done"))
        meta["tracked_as_todo"] = not meta["done"]
        meta["created"] = str(item.get("created") or meta.get("created") or datetime.now().isoformat(timespec="seconds"))
        if not isinstance(meta.get("tags"), list):
            meta["tags"] = []
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        changed = True
    if changed:
        config["todos"] = []
        save_config(config)


def save_config(config):
    try:
        with CONFIG_PATH.open("w", encoding="utf-8") as file:
            json.dump(config, file, indent=2)
    except OSError:
        pass


def rounded_rect(canvas, x1, y1, x2, y2, radius, **kwargs):
    points = [
        x1 + radius,
        y1,
        x2 - radius,
        y1,
        x2,
        y1,
        x2,
        y1 + radius,
        x2,
        y2 - radius,
        x2,
        y2,
        x2 - radius,
        y2,
        x1 + radius,
        y2,
        x1,
        y2,
        x1,
        y2 - radius,
        x1,
        y1 + radius,
        x1,
        y1,
    ]
    return canvas.create_polygon(points, smooth=True, splinesteps=24, **kwargs)


def clean_project_name(raw):
    name = raw.strip().strip("\"'")
    name = re.sub(r"^(called|named)\s+", "", name, flags=re.IGNORECASE).strip()
    name = re.sub(r"\s+", " ", name)
    name = re.sub(r'[<>:"/\\|?*]', " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:80]


def parse_due_datetime(due):
    due = str(due or "").strip()
    if not due:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(due, fmt)
            if fmt == "%Y-%m-%d":
                parsed = parsed.replace(hour=23, minute=59, second=0, microsecond=0)
            return parsed
        except ValueError:
            continue
    return None


def format_due_datetime(dt):
    return dt.strftime("%Y-%m-%dT%H:%M")


def split_due_value(due):
    parsed = parse_due_datetime(due)
    if not parsed:
        return "", "18:00"
    return parsed.strftime("%Y-%m-%d"), parsed.strftime("%H:%M")


def find_firefox():
    for candidate in (
        shutil.which("firefox"),
        shutil.which("firefox.exe"),
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Mozilla Firefox" / "firefox.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Mozilla Firefox" / "firefox.exe",
    ):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


OPEN_WITH_APPS = (
    ("Default app", "default"),
    ("Notes", "notepad"),
    ("Mozilla Firefox", "firefox"),
)

CHAT_SYSTEM_PROMPT = """You are aiOS Assistant inside a Windows desktop project manager.

Rules:
- A to-do is a project folder with an optional deadline (YYYY-MM-DD or YYYY-MM-DDTHH:MM).
- Use tools to change the app. Don't tell the user to do it manually if a tool exists.

Deadlines:
- If the user says only a day ("tomorrow", "friday"), use the date form YYYY-MM-DD — do NOT ask for a time.
- If the user gives a time, use YYYY-MM-DDTHH:MM.
- The injected context lists today's date; compute relative dates yourself (only call get_now if the date isn't already in context).
- Clear a deadline with set_project_due due="".

Tools:
- The chat context already lists every project. Match the user's words to that list directly — don't call list_projects unless the list is missing.
- Call get_project_context before answering questions or editing files in a project.
- Chain tools freely in one turn (search → open → update).
- Use write_project_file for README/notes/code edits, rename_project to rename folders.
- Never invent project names; if the user's wording matches nothing in context, call search_projects.

Style (CRITICAL):
- Be extremely terse. Default to 1 short sentence, or a fragment.
- No filler ("Sure!", "I'd be happy to", "Let me know if..."). Don't restate the request.
- After a tool runs, confirm in <=8 words ("Set due tomorrow.", "Renamed to X.").
- Only expand if the user explicitly asks for detail.
"""

AIOS_CHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_projects",
            "description": "List visible aiOS projects with title, due date, and dashboard status.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_project",
            "description": "Open a project detail page by name (partial match OK).",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Project name"}},
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "switch_tab",
            "description": "Switch aiOS tab: Dashboard, Projects, CODE, Apps, Drop, AI Operator, or Settings.",
            "parameters": {
                "type": "object",
                "properties": {"tab": {"type": "string", "enum": ["Dashboard", "Projects", "CODE", "Apps", "Drop", "AI Operator", "Settings"]}},
                "required": ["tab"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_project_summary",
            "description": "Set or replace the summary text for a project (aios-summary.md).",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name; omit to use active project"},
                    "summary": {"type": "string"},
                },
                "required": ["summary"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_project_due",
            "description": "Set or clear project deadline. Use empty string to remove deadline.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name; omit for active project"},
                    "due": {"type": "string", "description": "YYYY-MM-DDTHH:MM or empty to clear"},
                },
                "required": ["due"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_project",
            "description": "Update project metadata: title, status, priority, notes, pin to dashboard.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "title": {"type": "string"},
                    "status": {"type": "string", "enum": ["active", "paused", "done", "archived"]},
                    "priority": {"type": "string", "enum": ["low", "normal", "high"]},
                    "notes": {"type": "string"},
                    "pinned": {"type": "boolean", "description": "Show on dashboard as to-do"},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_todo",
            "description": "Create a new to-do project folder, optionally with deadline.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "due": {"type": "string", "description": "Optional YYYY-MM-DDTHH:MM"},
                    "priority": {"type": "string", "enum": ["low", "normal", "high"]},
                },
                "required": ["title"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_project",
            "description": "Create a new project folder (not necessarily on dashboard).",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "due": {"type": "string"},
                },
                "required": ["title"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_project_context",
            "description": "Read full context for a project: metadata, summary, project memory, and file list. Use this before editing or answering questions about a project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name; omit to use active project"},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_project_files",
            "description": "List files inside a project folder (relative paths, sizes).",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "limit": {"type": "integer", "description": "Max files to return (default 80)"},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_project_file",
            "description": "Read a text file inside a project. Returns up to ~12k chars.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "path": {"type": "string", "description": "Path relative to project root"},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_project_file",
            "description": "Create or overwrite a text file inside a project. Use for notes, README edits, code stubs. Confirm destructive overwrites in your reply.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "path": {"type": "string", "description": "Path relative to project root"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_project_notes",
            "description": "Append a line/paragraph to the project's notes field in metadata.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rename_project",
            "description": "Rename a project folder on disk and update its title metadata.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Current folder name"},
                    "new_name": {"type": "string", "description": "New folder name"},
                },
                "required": ["new_name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_projects",
            "description": "Find projects whose name, title, notes, or summary match the query.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_project_folder",
            "description": "Open a project folder in Windows Explorer.",
            "parameters": {
                "type": "object",
                "properties": {"project": {"type": "string"}},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_now",
            "description": "Return the current local date and time. Use before computing relative deadlines like 'tomorrow'.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]


SAFE_PROJECT_TEXT_EXTS = {
    ".ahk", ".bat", ".cfg", ".cmd", ".conf", ".css", ".csv", ".env",
    ".html", ".ini", ".js", ".json", ".jsx", ".log", ".md", ".mjs",
    ".ps1", ".py", ".rb", ".rs", ".sh", ".sql", ".svg", ".toml",
    ".ts", ".tsx", ".txt", ".vue", ".xml", ".yaml", ".yml", "",
}


def safe_join_project(project_root, project_path, rel_path):
    rel = (rel_path or "").replace("\\", "/").lstrip("/")
    if not rel:
        raise ValueError("path is required")
    target = (Path(project_path) / rel).resolve()
    project_resolved = Path(project_path).resolve()
    root_resolved = Path(project_root).resolve()
    if project_resolved != target and project_resolved not in target.parents:
        raise ValueError("Path escapes the project folder.")
    if root_resolved not in target.parents and target != root_resolved:
        raise ValueError("Path escapes the project root.")
    return target


def openai_chat_payload(model, messages, tools):
    payload = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
    }
    if model_uses_completion_tokens(model):
        payload["max_completion_tokens"] = 1200
    else:
        payload["temperature"] = 0.4
        payload["max_tokens"] = 1200
    return payload


def model_uses_completion_tokens(model):
    name = str(model or "").casefold()
    return name.startswith(("gpt-5", "o1", "o3", "o4"))


def normalize_assistant_tool_message(message):
    clean = {"role": "assistant", "content": message.get("content") or ""}
    tool_calls = message.get("tool_calls")
    if tool_calls:
        clean["tool_calls"] = tool_calls
    return clean


def openai_request(api_key, payload, timeout=90):
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(detail).get("error", {}).get("message", detail)
        except json.JSONDecodeError:
            message = detail or str(exc)
        raise RuntimeError(message) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error: {exc.reason}") from exc


def normalize_name(name):
    text = Path(name).stem.casefold()
    text = re.sub(r"\s+\(\d+\)$", "", text)
    text = text.replace("-webblasaren", "")
    return re.sub(r"\s+", " ", text).strip()


def start_menu_shortcuts():
    roots = [
        Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs",
        Path(os.environ.get("PROGRAMDATA", "")) / r"Microsoft\Windows\Start Menu\Programs",
    ]
    shortcuts = {}
    for root in roots:
        if not root.exists():
            continue
        for item in root.rglob("*.lnk"):
            key = normalize_name(item.name)
            if "uninstall" in key or "readme" in key or "help" in key:
                continue
            shortcuts.setdefault(key, item)
    return shortcuts


def find_codex():
    candidates = [
        Path.home() / "AppData/Local/OpenAI/Codex/bin/codex.exe",
        Path(
            r"C:\Program Files\WindowsApps"
        )
        / "OpenAI.Codex_26.519.2081.0_x64__2p2nqsd0c76g0/app/resources/codex.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    found = shutil.which("codex.exe")
    if found:
        return found
    windows_apps = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "WindowsApps"
    try:
        matches = sorted(windows_apps.glob("OpenAI.Codex_*/*/resources/codex.exe"), reverse=True)
    except OSError:
        matches = []
    if matches:
        return str(matches[0])
    return shutil.which("codex") or ""


def codex_env(home=None):
    env = os.environ.copy()
    env.pop("OPENAI_API_KEY", None)
    if home:
        env["CODEX_HOME"] = str(home)
        env["AIOS_ACTIVE_CODEX_HOME"] = str(home)
    return env


def codex_auth_info():
    """Return (logged_in: bool, label: str) describing the active codex account."""
    return codex_usage.codex_auth_info(CODEX_HOME)


def _legacy_codex_auth_info_unused():
    auth_path = CODEX_HOME / "auth.json"
    if not auth_path.exists():
        return False, "not signed in"
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, "auth unreadable"
    tokens = data.get("tokens") or {}
    account = (
        tokens.get("account_email")
        or tokens.get("email")
        or data.get("account_email")
        or data.get("email")
    )
    if not account:
        id_token = tokens.get("id_token") or data.get("id_token")
        if isinstance(id_token, str) and id_token.count(".") == 2:
            import base64
            try:
                payload = id_token.split(".")[1]
                payload += "=" * (-len(payload) % 4)
                claims = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8", "replace"))
                account = claims.get("email") or claims.get("name")
            except Exception:
                account = None
    plan = (tokens.get("plan_type") or data.get("plan_type") or "").strip()
    label = account or "signed in"
    if plan:
        label = f"{label} · {plan}"
    return True, label


def launch_codex_login():
    global CODEX_HOME
    codex = find_codex()
    if not codex:
        return False
    signed_in, _label = codex_auth_info()
    if signed_in:
        slot = aios_codex_accounts.create_login_slot(CONFIG_PATH)
        CODEX_HOME = Path(slot["home"])
    else:
        CODEX_HOME.mkdir(parents=True, exist_ok=True)
    os.environ["AIOS_ACTIVE_CODEX_HOME"] = str(CODEX_HOME)
    try:
        subprocess.Popen(
            [codex, "login"],
            env=codex_env(CODEX_HOME),
            cwd=str(BASE_DIR),
            creationflags=CREATE_NEW_CONSOLE,
        )
        return True
    except OSError:
        return False


def run_detached(args, cwd=None, visible=False, env=None):
    subprocess.Popen(
        args,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=0 if visible else CREATE_NO_WINDOW,
    )


def tail_lines(path, max_bytes=524288):
    try:
        size = path.stat().st_size
        with path.open("rb") as file:
            file.seek(max(0, size - max_bytes))
            data = file.read().decode("utf-8", errors="replace")
        return data.splitlines()
    except OSError:
        return []


def latest_codex_rate_limits():
    return codex_usage.latest_codex_rate_limits(CODEX_HOME)


def _legacy_latest_codex_rate_limits_unused():
    sessions = CODEX_HOME / "sessions"
    if not sessions.exists():
        return None

    try:
        files = sorted(
            sessions.rglob("*.jsonl"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None

    for path in files[:30]:
        for line in reversed(tail_lines(path)):
            if '"rate_limits"' not in line or '"token_count"' not in line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = item.get("payload", {})
            if payload.get("type") != "token_count":
                continue
            limits = payload.get("rate_limits")
            if limits:
                return limits
    return None


def format_duration(seconds):
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def usage_text(limit, label):
    if not limit:
        return f"{label} --"
    used = float(limit.get("used_percent", 0) or 0)
    remaining = max(0, 100 - used)
    reset = limit.get("resets_at")
    reset_text = "--"
    if reset:
        reset_text = format_duration(float(reset) - time.time())
    return f"{label} {remaining:.0f}% · {reset_text}"


DASHBOARD_CACHE_PATH = BASE_DIR / ".aios-dashboard-cache.json"
DASHBOARD_USER_AGENT = "aiOS-dashboard/1.0 (+local desktop app)"
# code -> (label, glyph). Glyphs are Segoe UI Symbol characters, not emoji, so
# they render in the same monochrome style as the rest of the panel.
WEATHER_CODES = {
    0: ("Clear", "☀"),
    1: ("Mostly clear", "☀"),
    2: ("Partly cloudy", "⛅"),
    3: ("Cloudy", "☁"),
    45: ("Fog", "≈"),
    48: ("Rime fog", "≈"),
    51: ("Light drizzle", "☂"),
    53: ("Drizzle", "☂"),
    55: ("Heavy drizzle", "☂"),
    56: ("Freezing drizzle", "☂"),
    57: ("Freezing drizzle", "☂"),
    61: ("Light rain", "☂"),
    63: ("Rain", "☂"),
    65: ("Heavy rain", "☂"),
    66: ("Freezing rain", "☂"),
    67: ("Freezing rain", "☂"),
    71: ("Light snow", "❄"),
    73: ("Snow", "❄"),
    75: ("Heavy snow", "❄"),
    77: ("Snow grains", "❄"),
    80: ("Light showers", "☂"),
    81: ("Showers", "☂"),
    82: ("Heavy showers", "☂"),
    85: ("Snow showers", "❄"),
    86: ("Snow showers", "❄"),
    95: ("Thunder", "⚡"),
    96: ("Thunder, hail", "⚡"),
    99: ("Thunder, hail", "⚡"),
}
TICKER_LABELS = {
    "^OMX": "OMX 30",
    "^GSPC": "S&P 500",
    "^IXIC": "Nasdaq",
    "SEK=X": "USD/SEK",
    "EURSEK=X": "EUR/SEK",
    "BTC-USD": "Bitcoin",
    "ETH-USD": "Ethereum",
}


def http_json(url, timeout=12):
    request = urllib.request.Request(url, headers={"User-Agent": DASHBOARD_USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def load_dashboard_cache():
    """Last fetched weather/markets, so a fresh launch shows numbers instantly."""
    try:
        with DASHBOARD_CACHE_PATH.open("r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_dashboard_cache(cache):
    try:
        with DASHBOARD_CACHE_PATH.open("w", encoding="utf-8") as file:
            json.dump(cache, file)
    except OSError:
        pass


def weather_code_text(code):
    try:
        return WEATHER_CODES[int(code)]
    except (TypeError, ValueError, KeyError):
        return ("Weather", "☁")


def geocode_location(name):
    url = (
        "https://geocoding-api.open-meteo.com/v1/search"
        f"?name={urllib.parse.quote(str(name))}&count=1&language=en&format=json"
    )
    results = (http_json(url).get("results") or [])
    if not results:
        raise RuntimeError(f"Location not found: {name}")
    first = results[0]
    return float(first["latitude"]), float(first["longitude"]), str(first.get("name") or name)


def fetch_weather_snapshot(location):
    latitude, longitude, label = geocode_location(location)
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={latitude}&longitude={longitude}"
        "&current=temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m"
        "&hourly=temperature_2m,precipitation_probability,weather_code"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset"
        "&forecast_days=5&timezone=auto"
    )
    data = http_json(url)
    current = data.get("current") or {}
    daily = data.get("daily") or {}
    hourly = data.get("hourly") or {}

    days = []
    for index, day in enumerate((daily.get("time") or [])[:5]):
        try:
            date = datetime.fromisoformat(day)
        except ValueError:
            continue
        days.append(
            {
                "name": "Today" if index == 0 else date.strftime("%a"),
                "high": daily.get("temperature_2m_max", [None] * 5)[index],
                "low": daily.get("temperature_2m_min", [None] * 5)[index],
                "rain": daily.get("precipitation_probability_max", [None] * 5)[index],
                "code": daily.get("weather_code", [0] * 5)[index],
            }
        )

    # Next few hours, starting from the current hour.
    hours = []
    times = hourly.get("time") or []
    now_key = datetime.now().strftime("%Y-%m-%dT%H:00")
    start = times.index(now_key) if now_key in times else 0
    for index in range(start, min(start + 18, len(times))):
        hours.append(
            {
                "time": times[index][11:16],
                "temp": (hourly.get("temperature_2m") or [None])[index],
                "rain": (hourly.get("precipitation_probability") or [None])[index],
                "code": (hourly.get("weather_code") or [0])[index],
            }
        )

    return {
        "location": label,
        "temp": current.get("temperature_2m"),
        "feels": current.get("apparent_temperature"),
        "humidity": current.get("relative_humidity_2m"),
        "wind": current.get("wind_speed_10m"),
        "code": current.get("weather_code", 0),
        "sunrise": (daily.get("sunrise") or [""])[0][11:16],
        "sunset": (daily.get("sunset") or [""])[0][11:16],
        "days": days,
        "hours": hours,
        "updated": time.time(),
    }


def fetch_market_quotes(symbols):
    quotes = []
    for symbol in symbols:
        symbol = str(symbol).strip()
        if not symbol:
            continue
        try:
            url = (
                "https://query1.finance.yahoo.com/v8/finance/chart/"
                f"{urllib.parse.quote(symbol)}?range=1d&interval=15m"
            )
            result = (http_json(url).get("chart") or {}).get("result") or []
            if not result:
                raise RuntimeError("no data")
            meta = result[0].get("meta") or {}
            closes = ((result[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
            spark = [float(value) for value in closes if value is not None]
            price = meta.get("regularMarketPrice")
            previous = meta.get("chartPreviousClose") or meta.get("previousClose")
            change = None
            if price is not None and previous:
                change = (float(price) - float(previous)) / float(previous) * 100.0
            quotes.append(
                {
                    "symbol": symbol,
                    "label": TICKER_LABELS.get(symbol.upper(), symbol.replace("-USD", "").replace("=X", "")),
                    "name": str(meta.get("shortName") or symbol),
                    "price": float(price) if price is not None else None,
                    "change": change,
                    "currency": str(meta.get("currency") or ""),
                    "spark": spark[-40:],
                }
            )
        except Exception as exc:
            quotes.append(
                {
                    "symbol": symbol,
                    "label": TICKER_LABELS.get(symbol.upper(), symbol),
                    "name": symbol,
                    "price": None,
                    "change": None,
                    "currency": "",
                    "spark": [],
                    "error": str(exc)[:60],
                }
            )
    return {"quotes": quotes, "updated": time.time()}


def format_price(value, currency=""):
    if value is None:
        return "--"
    value = float(value)
    if value >= 10000:
        text = f"{value:,.0f}".replace(",", " ")
    elif value >= 100:
        text = f"{value:,.2f}".replace(",", " ")
    else:
        text = f"{value:.4f}".rstrip("0").rstrip(".")
    symbols = {"USD": "$", "SEK": "", "EUR": "€"}
    prefix = symbols.get(currency.upper(), "")
    return f"{prefix}{text}"


def query_nvidia_gpu():
    try:
        raw = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=3,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return None
    parts = [part.strip() for part in raw.strip().splitlines()[0].split(",")]
    if len(parts) < 6:
        return None
    def number(text):
        try:
            return float(text)
        except ValueError:
            return None
    return {
        "name": parts[0].replace("NVIDIA ", ""),
        "temp": number(parts[1]),
        "util": number(parts[2]),
        "mem_used": number(parts[3]),
        "mem_total": number(parts[4]),
        "power": number(parts[5]),
    }


class ScrollFrame(tk.Frame):
    _instances = []

    def __init__(self, parent, bg):
        super().__init__(parent, bg=bg)
        self.on_user_scroll = None
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self.window = self.canvas.create_window(0, 0, anchor="nw", window=self.inner)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._update_width)
        self.bind("<Destroy>", self._remove_instance, add="+")
        ScrollFrame._instances.append(self)

    def _remove_instance(self, event=None):
        if event is not None and event.widget is not self:
            return
        try:
            ScrollFrame._instances.remove(self)
        except ValueError:
            pass

    def _on_inner_configure(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._bind_wheel_tree(self.inner)

    def _update_width(self, event):
        self.canvas.itemconfigure(self.window, width=event.width)

    def _bind_wheel_tree(self, widget):
        bindings = getattr(widget, "_aios_scroll_bindings", set())
        binding_key = id(self)
        if binding_key not in bindings:
            try:
                widget.bind("<MouseWheel>", self._mousewheel, add="+")
                widget._aios_scroll_bindings = {*bindings, binding_key}
            except tk.TclError:
                pass
        for child in widget.winfo_children():
            self._bind_wheel_tree(child)

    def _contains_widget(self, widget):
        current = widget
        while current is not None:
            if current == self.inner or current == self.canvas or current == self:
                return True
            current = getattr(current, "master", None)
        return False

    def _mousewheel(self, event):
        if not self.winfo_ismapped() or not self._contains_widget(event.widget):
            return
        target = self._deepest_scroll(event.widget)
        if target is not self:
            return
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        if callable(self.on_user_scroll):
            self.on_user_scroll(event.delta)
        # Do not let Text/Listbox class bindings also scroll their own content.
        return "break"

    def _deepest_scroll(self, widget):
        matches = []
        for frame in list(ScrollFrame._instances):
            try:
                if frame.winfo_exists() and frame.winfo_ismapped() and frame._contains_widget(widget):
                    matches.append(frame)
            except tk.TclError:
                frame._remove_instance()
        if not matches:
            return None
        return max(matches, key=lambda frame: frame._depth())

    def _depth(self):
        depth = 0
        current = self.master
        while current is not None:
            depth += 1
            current = getattr(current, "master", None)
        return depth

    def bind_wheel_forward(self, widget):
        def forward(event):
            self._mousewheel(event)
            return "break"

        widget.bind("<MouseWheel>", forward, add="+")


CODE_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_CODE_INLINE = re.compile(
    r"(?P<code>`[^`\n]+`)"
    r"|(?P<link>\[[^\]\n]+\]\([^)\s]+\))"
    r"|(?P<bold>\*\*[^*\n]+\*\*|__[^_\n]+__)"
    r"|(?P<italic>(?<![\w*])\*[^*\n]+\*(?![\w*])|(?<![\w_])_[^_\n]+_(?![\w_]))"
)


def code_inline_spans(text):
    """Split one markdown line into (text, style) spans for a Tk Text widget."""
    spans = []
    cursor = 0
    for match in _CODE_INLINE.finditer(str(text or "")):
        if match.start() > cursor:
            spans.append((text[cursor:match.start()], ""))
        if match.group("code"):
            spans.append((match.group("code")[1:-1], "code"))
        elif match.group("link"):
            label, _, target = match.group("link")[1:].partition("](")
            spans.append((label, "link", target[:-1]))
        elif match.group("bold"):
            spans.append((match.group("bold")[2:-2], "bold"))
        else:
            spans.append((match.group("italic")[1:-1], "italic"))
        cursor = match.end()
    if cursor < len(text or ""):
        spans.append((text[cursor:], ""))
    return [span for span in spans if span[0]]


def code_markdown_blocks(text):
    """Turn markdown into flat blocks the chat renderer can paint line by line.

    Each block is ``{"type": ..., "spans": [(text, style)], "indent": int}``.
    Fenced code keeps its raw text so commands stay copyable and unstyled.
    """
    blocks = []
    lines = str(text or "").replace("\r\n", "\n").split("\n")
    fence = None
    buffer = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped.startswith("```"):
            if fence is None:
                fence = stripped[3:].strip()
                buffer = []
            else:
                blocks.append({"type": "code", "spans": [("\n".join(buffer), "codeblock")], "indent": 0, "lang": fence})
                fence = None
                buffer = []
            index += 1
            continue
        if fence is not None:
            buffer.append(line)
            index += 1
            continue
        if not stripped:
            blocks.append({"type": "blank", "spans": [], "indent": 0})
            index += 1
            continue
        if "|" in stripped and index + 1 < len(lines):
            header = [cell.strip() for cell in stripped.strip("|").split("|")]
            divider = [cell.strip() for cell in lines[index + 1].strip().strip("|").split("|")]
            if len(header) >= 2 and len(divider) == len(header) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in divider):
                rows = [header]
                index += 2
                while index < len(lines) and "|" in lines[index] and lines[index].strip():
                    row = [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
                    rows.append((row + [""] * len(header))[:len(header)])
                    index += 1
                blocks.append({"type": "table", "rows": rows, "spans": [], "indent": 0})
                continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            level = min(3, len(heading.group(1)))
            blocks.append({"type": f"h{level}", "spans": code_inline_spans(heading.group(2)), "indent": 0})
            index += 1
            continue
        if re.match(r"^([-*_])\s*\1\s*\1[-*_\s]*$", stripped):
            blocks.append({"type": "rule", "spans": [], "indent": 0})
            index += 1
            continue
        if stripped.startswith("> "):
            blocks.append({"type": "quote", "spans": code_inline_spans(stripped[2:]), "indent": 0})
            index += 1
            continue
        indent = (len(line) - len(line.lstrip(" "))) // 2
        bullet = re.match(r"^[-*+]\s+(.*)$", stripped)
        if bullet:
            task = re.match(r"^\[([ xX])\]\s+(.*)$", bullet.group(1))
            if task:
                blocks.append({"type": "task", "spans": code_inline_spans(task.group(2)), "indent": indent, "checked": task.group(1).casefold() == "x"})
            else:
                blocks.append({"type": "bullet", "spans": code_inline_spans(bullet.group(1)), "indent": indent})
            index += 1
            continue
        numbered = re.match(r"^(\d+)[.)]\s+(.*)$", stripped)
        if numbered:
            block = {"type": "number", "spans": code_inline_spans(numbered.group(2)), "indent": indent}
            block["marker"] = f"{numbered.group(1)}."
            blocks.append(block)
            index += 1
            continue
        blocks.append({"type": "text", "spans": code_inline_spans(line.strip()), "indent": indent})
        index += 1
    if fence is not None and buffer:
        blocks.append({"type": "code", "spans": [("\n".join(buffer), "codeblock")], "indent": 0, "lang": fence})
    while blocks and blocks[-1]["type"] == "blank":
        blocks.pop()
    return blocks


def code_activity_key(event):
    """One stable id per tool call so start/update/finish share a single card.

    Providers narrate the same command several times ("Run command", "Running",
    "Ran command"). Without a stable key every phase became its own row.
    """
    activity_id = str(event.get("activity_id") or "").strip()
    if activity_id:
        return activity_id
    for field in ("command", "detail", "title", "text"):
        value = str(event.get(field) or "").strip()
        if value:
            normalized = re.sub(r"^\$\s*", "", value)
            normalized = re.sub(r"^(?:run|ran|running|check|checked|checking)\s+", "", normalized, flags=re.I)
            return f"{event.get('kind') or 'tool'}:{re.sub(r'\s+', ' ', normalized).casefold()[:160]}"
    return f"{event.get('kind') or 'tool'}:{event.get('ts') or ''}"


class HelperOverlay:
    def __init__(self, *, background=False):
        self.config = load_config()
        self.theme = self.config["theme"]
        self.project_root = Path(self.config["project_root"])
        self.project_root.mkdir(parents=True, exist_ok=True)
        self.shortcuts = start_menu_shortcuts()
        self.apps = self._discover_apps()
        self.active_tab = "Dashboard"
        # Which sub-page of Settings is showing; survives tab switches.
        self.settings_page = "General"
        self.settings_status_var = None
        self._settings_status_after = None
        self.active_project = None
        self.page_view = None
        self.busy = False
        self.chat_busy_since = 0.0
        self.history = self.load_chat_history()
        self.dropped_paths = []
        self.codex_process = None
        self.quick_process = None
        self.codex_log = []
        self.code_selected_id = ""
        self.code_jobs = []
        self.code_capabilities = {"providers": []}
        self.code_log_size = 0
        self.code_poll_after = None
        self.code_view_token = 0
        self._code_refresh_seq = 0
        self._code_refresh_inflight = None
        self._code_capabilities_busy = False
        self._code_sessions_signature = None
        self.code_last_notify_ts = 0.0
        self.code_notify_started_at = time.time()
        self.code_notify_offsets = {}
        self.code_notify_busy = False
        self.code_provider_images = {}
        self.code_provider_buttons = {}
        self.chat_run_id = 0
        self._ui_queue = queue.Queue()
        self.thinking_step = 0
        self.thinking_after = None
        self.thinking_frame = None
        self.thinking_label = None
        self._chat_embeds = []
        self._live_tool_count = 0
        self._live_tool_call_ids = set()
        self._agent_turn_active = False
        self._stream_reply_frame = None
        self._stream_reply_var = None
        self._stream_reply_text = ""
        self._stream_reply_meta = None
        self.drag_x = 0
        self.drag_y = 0
        self.chat_resize_start_x = 0
        self.chat_resize_start_w = 0
        self._create_menu_popup = None
        self._autosave_after = None
        self._autosave_project_path = None
        self._dash_cache = load_dashboard_cache()
        self._dash_paint = {}
        self._dash_fetching = set()
        self._dash_notes_after = None
        self._dash_notes_widget = None
        self._dash_net_sample = None
        self._open_with_popup = None
        self._open_with_close_after = None
        self._open_with_target = None
        self._file_tree_expanded = set()
        self._tray_added = False
        self._tray_icon = None
        self._tray_nid = None
        self._tray_wndproc = None
        self._tray_old_wndproc = None
        self.todo_due_var = None
        self.todo_priority_var = None
        self.todo_due_label = None
        self._priority_buttons = []
        self._date_buttons = []
        self._cal_popup = None
        self.quick_tools_status = None
        self._bottom_tray = None
        self._tray_anim_job = None
        self._tray_hide_job = None
        self._tray_current_h = 30
        self._tray_current_relwidth = 0.20
        self._tray_open = False
        self._tray_target_h = 268
        self._tray_peek_h = 30
        self._tray_open_relwidth = 0.88
        self._tray_peek_relwidth = 0.20
        self.quick_tools_handle = None
        self.screen_record_process = None
        self.screen_record_path = None
        self.screen_record_started_at = 0.0
        self.screen_record_popup = None
        self.screen_record_control = None
        self.screen_record_time_label = None
        self.screen_record_quick_btn = None
        self.screen_record_dot_on = True
        self.screen_record_timer_job = None
        self.screen_record_poll_job = None
        self.screen_record_area_overlay = None
        self.phone_photos_popup = None
        self.phone_photos_poll_job = None
        self.phone_photos_qr_image = None
        self.screen_record_monitors = []
        self.screen_record_windows = []
        self.screen_record_monitor_var = None
        self.screen_record_window_var = None
        self.agent_clicker_dir = AGENT_CLICKER_DIR
        self.agent_operator_imported = False
        self.agent_operator_error = None
        self.agent_operator_loop = None
        self.agent_operator_current_task = ""
        # A previous process may have been closed mid-run. Never let that
        # stale state make the phone believe OPERATOR is still active.
        self._phone_mirror_set_idle("helper_started")
        self.agent_operator_monitors = []
        self.agent_operator_event_q = queue.Queue()
        self.agent_operator_current_image = None
        self.agent_operator_tk_preview = None
        self.agent_operator_last_clicks = []
        self.agent_operator_log_buffer = []
        self.agent_operator_overlay_step = 0
        self.agent_operator_overlay_thought = "Looking at the screen..."
        self.agent_operator_overlay_action = ""
        self.agent_operator_booted = False
        self.agent_operator_booting = False
        self.agent_operator_preview_scale = 1.0
        self.agent_operator_preview_origin = (0, 0)
        self.agent_operator_ImageDraw = None
        self.agent_operator_ImageTk = None
        self.agent_operator_capture = None
        self.agent_operator_raw_capture = None
        self.agent_operator_loop_module = None
        self.agent_operator_exec_action = None
        self.agent_operator_Image = None
        self.agent_operator_tts = None
        self.agent_operator_tts_error = None
        self.agent_operator_default_model = OPERATOR_DEFAULT_MODEL
        self.agent_operator_default_voice = "nova"
        self.agent_operator_status_var = None
        self.agent_operator_step_var = None
        self.agent_operator_task = None
        self.agent_operator_attachments = []
        self.agent_operator_attach_strip = None
        self.agent_operator_attach_placeholder = None
        self.agent_operator_ImageGrab = None
        self.agent_operator_context_list = None
        self.agent_operator_context_editor = None
        self.agent_operator_context_file_var = None
        self.agent_operator_context_status_var = None
        self.agent_operator_context_current = None
        self.agent_operator_context_loading = False
        self.agent_operator_context_save_after = None
        self.agent_operator_monitor_var = None
        self.agent_operator_model_var = None
        self.agent_operator_planner_model_var = None
        self.agent_operator_reason_var = None
        self.agent_operator_steps_var = None
        self.agent_operator_delay_var = None
        self.agent_operator_tts_var = None
        self.agent_operator_voice_var = None
        self.agent_operator_shell_var = None
        self.agent_operator_codex_var = None
        self.agent_operator_release_all = None
        self.agent_operator_stop_requested = False
        self.agent_operator_codex_available = False
        self.agent_operator_codex_message = ""
        self.agent_operator_codex_login_after = None
        self.agent_operator_codex_login_started_at = 0.0
        self.agent_operator_codex_login_btn = None
        self.agent_operator_codex_status_label = None
        self.agent_operator_advanced_open = False
        self.agent_operator_advanced_frame = None
        self.agent_operator_advanced_btn = None
        self.agent_operator_task_panel = None
        self.agent_operator_settings = self.config.get("ai_operator") or dict(DEFAULT_CONFIG["ai_operator"])
        self.agent_operator_canvas = None
        self.agent_operator_log = None
        self.agent_operator_run_btn = None
        self.agent_operator_followup_btn = None
        self.agent_operator_pause_btn = None
        self.agent_operator_stop_btn = None
        self.agent_operator_clear_run_btn = None
        self.agent_operator_control_overlay = None
        self.agent_operator_control_canvas = None
        self.agent_operator_control_after = None
        self.agent_operator_control_pulse = 0
        self.agent_operator_control_monitor = None
        self.agent_operator_control_visible = False
        self.agent_operator_native_overlay = None
        self.phone_control_native_overlay = None
        self.phone_control_visible = False
        self.phone_control_after = None
        self.phone_control_deadline = 0.0
        self.phone_control_pulse = 0
        self.phone_control_log = "connected"
        self._operator_passive_window_styles = {}
        self._operator_capture_exclusion_ok = False
        self._agent_capture_affinity_tokens = set()
        self._operator_input_passthrough = False
        self._root_native_wndproc = None
        self._root_original_wndproc = None
        self._root_native_hwnd = None

        self.root = tk.Tk()
        self.root.title("aiOS")
        self._apply_window_icon()
        self.root.geometry(self.config.get("window") or DEFAULT_CONFIG["window"])
        self.root.minsize(860, 560)
        self.root.configure(bg=TRANSPARENT)
        self.root.overrideredirect(True)
        self.root.attributes("-alpha", float(self.c("opacity")))
        self.root.attributes("-topmost", bool(self.c("always_on_top")))
        try:
            self.root.attributes("-transparentcolor", TRANSPARENT)
        except tk.TclError:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.root.bind("<Destroy>", self._on_root_destroy, add="+")
        self.root.withdraw()
        self.brand_font_family = self._init_brand_font()
        self.agent_operator_native_overlay = NativeOperatorOverlay(self, compact=True)
        self.phone_control_native_overlay = NativeOperatorOverlay(
            self,
            "Phone Control",
            "PHONE CONTROL",
            class_name="aiOSPhoneControlOverlay",
        )
        self._init_tray_icon()

        self._clamp_window_to_screen()
        self._build_ui()
        self._install_root_hit_test_passthrough()
        self._bind_keys()
        self._enable_file_drop()
        self._start_command_server()
        self._schedule_usage_refresh()
        self._schedule_chat_watchdog()
        self._schedule_self_health_heartbeat()
        self._poll_ui_queue()
        self._poll_agent_operator_events()
        self._ensure_voice_server()
        self._ensure_hotkeys()
        self.root.after(100, self._start_agent_operator_load)
        self.root.after(400, self._show_aios_windows_in_normal_capture)
        self.root.after(1200, self._ensure_phone_relay)
        self.root.after(1800, self._poll_code_notifications)
        if not background:
            self.show_startup_screen()

    def _build_ui(self):
        self.shell = tk.Canvas(self.root, bg=TRANSPARENT, highlightthickness=0, bd=0)
        self.shell.pack(fill="both", expand=True)
        self.shell.bind("<Configure>", self._redraw_shell)

        self.panel = tk.Frame(self.shell, bg=self.c("panel"), bd=0, highlightthickness=0)
        self.panel_window = self.shell.create_window(18, 18, anchor="nw", window=self.panel)

        self.header = tk.Frame(self.panel, bg=self.c("panel"), height=56)
        self.header.pack(fill="x", padx=18, pady=(10, 0))
        self.header.pack_propagate(False)
        self.header.bind("<ButtonPress-1>", self._start_move)
        self.header.bind("<B1-Motion>", self._drag_move)

        self.title = tk.Label(
            self.header,
            text="aiOS",
            bg=self.c("panel"),
            fg=self.c("text"),
            font=self.brand_font(18),
        )
        self.title.pack(side="left")
        self.bind_drag(self.title)

        self.subtitle = tk.Label(
            self.header,
            text=self._status_subtitle(),
            bg=self.c("panel"),
            fg=self.c("muted"),
            font=self.font(9),
        )
        self.subtitle.pack(side="left", padx=(12, 0))
        self.bind_drag(self.subtitle)

        self._build_header_toolbar()

        self.body = tk.Frame(self.panel, bg=self.c("panel"))
        self.body.pack(fill="both", expand=True, padx=18, pady=(8, 18))

        self.nav = tk.Frame(self.body, bg=self.c("surface"), width=158)
        self.nav.pack(side="left", fill="y", padx=(0, 12))
        self.nav.pack_propagate(False)

        self.page = tk.Frame(self.body, bg=self.c("panel"))
        self.page.pack(side="left", fill="both", expand=True)

        self.chat_panel = tk.Frame(self.body, bg=self.c("surface"), width=int(self.config.get("chat_width", 330)))
        self.chat_panel.pack(side="right", fill="y", padx=(12, 0))
        self.chat_panel.pack_propagate(False)

        self.resize_grip = tk.Frame(
            self.panel,
            bg=self._header_border_color(),
            width=12,
            height=12,
            cursor="size_nw_se",
            highlightthickness=1,
            highlightbackground=self._header_border_color(),
        )
        self.resize_grip.place(relx=1, rely=1, anchor="se", x=-12, y=-12)
        self.resize_grip.bind("<ButtonPress-1>", self._start_resize)
        self.resize_grip.bind("<B1-Motion>", self._drag_resize)

        self._build_nav()
        self._build_chat()
        self._build_bottom_tray()
        self.render_tab(self.active_tab)
        self.render_chat_history()

    def _build_nav(self):
        self.clear(self.nav)
        for tab in ("Dashboard", "Projects", "CODE", "Apps", "Drop", "AI Operator", "Settings"):
            label = "OPERATOR" if tab == "AI Operator" else tab
            button = self.button(
                self.nav,
                label,
                lambda value=tab: self.render_tab(value),
                active=tab == self.active_tab,
            )
            if tab == "AI Operator":
                button.configure(font=self.brand_font(8), pady=11)
            button.pack(fill="x", padx=10, pady=(10 if tab == "Dashboard" else 4, 0))

    def _build_chat(self):
        self.clear(self.chat_panel)
        resize_edge = tk.Frame(self.chat_panel, bg=self.c("surface"), width=8, cursor="sb_h_double_arrow")
        resize_edge.pack(side="left", fill="y")
        resize_edge.bind("<ButtonPress-1>", self._start_chat_resize)
        resize_edge.bind("<B1-Motion>", self._drag_chat_resize)

        chat_content = tk.Frame(self.chat_panel, bg=self.c("surface"))
        chat_content.pack(side="left", fill="both", expand=True)

        chat_head = tk.Frame(chat_content, bg=self.c("surface"))
        chat_head.pack(fill="x", padx=12, pady=(10, 4))
        head_left = tk.Frame(chat_head, bg=self.c("surface"))
        head_left.pack(side="left", fill="x", expand=True)
        tk.Label(
            head_left,
            text="Agent",
            bg=self.c("surface"),
            fg=self.c("text"),
            font=self.font(10, "bold"),
        ).pack(anchor="w")
        voice_cfg = self.config.get("voice_dictation") or {}
        model_label = voice_cfg.get("agent_model") or DEFAULT_VOICE_DICTATION.get("agent_model") or "gpt-5.6-luna"
        key_ok = bool(self.get_openai_api_key())
        auth_label = "API key ready" if key_ok else "No API key"
        meta = f"{model_label} · {auth_label}"
        self.chat_account_label = tk.Label(
            head_left,
            text=meta,
            bg=self.c("surface"),
            fg=self.c("success") if key_ok else self.c("danger"),
            font=self.font(8),
        )
        self.chat_account_label.pack(anchor="w")
        chat_actions = tk.Frame(chat_head, bg=self.c("surface"))
        chat_actions.pack(side="right")
        self.header_chip(chat_actions, "Reset", self.reset_chat, hint="Clear agent chat").pack(side="right", padx=(4, 0))
        self.header_chip(chat_actions, "Settings", lambda: self.render_tab("Settings"), hint="OpenAI key & voice agent").pack(
            side="right"
        )

        chat_box = tk.Frame(chat_content, bg=self.c("surface"))
        chat_box.pack(fill="both", expand=True, padx=(2, 0), pady=(0, 6))
        self.chat_canvas = tk.Canvas(
            chat_box,
            bg=self.c("surface"),
            highlightthickness=0,
            bd=0,
        )
        self.chat_scroll = tk.Scrollbar(
            chat_box,
            orient="vertical",
            command=self.chat_canvas.yview,
            width=8,
            bd=0,
            relief="flat",
            troughcolor=self.c("surface"),
            bg=self.c("surface2"),
            activebackground=self.c("accent"),
            highlightthickness=0,
        )
        self.chat_canvas.configure(yscrollcommand=self.chat_scroll.set)
        self.chat_scroll.pack(side="right", fill="y", padx=(0, 2))
        self.chat_canvas.pack(side="left", fill="both", expand=True)
        self.chat_inner = tk.Frame(self.chat_canvas, bg=self.c("surface"))
        self._chat_canvas_window = self.chat_canvas.create_window((0, 0), window=self.chat_inner, anchor="nw")
        self.chat_inner.bind("<Configure>", self._chat_on_inner_configure)
        self.chat_canvas.bind("<Configure>", self._chat_on_canvas_configure)
        self.chat_canvas.bind("<Enter>", lambda _e: self.chat_canvas.bind_all("<MouseWheel>", self._chat_on_mousewheel))
        self.chat_canvas.bind("<Leave>", lambda _e: self.chat_canvas.unbind_all("<MouseWheel>"))
        # Keep self.chat as the scroll surface for older hasattr checks.
        self.chat = self.chat_inner

        assistant_bg = self.blend_color(self.c("surface"), self.c("text"), 0.08)
        self.thinking_canvas = None
        self.thinking_frame = None
        self.thinking_label = None
        self._chat_embeds = []
        self._live_tool_count = 0
        self._agent_turn_active = False
        self._live_turn_col = None
        self._live_tools_box = None
        self._assistant_bg = assistant_bg
        self._user_bubble_bg = self.blend_color(self.c("surface"), self.c("accent"), 0.22)
        self.thinking_status_text = "Thinking"

        bottom = tk.Frame(chat_content, bg=self.c("surface"))
        bottom.pack(fill="x", padx=12, pady=(0, 12))
        input_wrap = tk.Frame(
            bottom,
            bg=self.c("panel2"),
            highlightthickness=1,
            highlightbackground=self.blend_color(self.c("panel2"), self.c("accent"), 0.18),
        )
        input_wrap.pack(side="left", fill="both", expand=True)
        self.input = tk.Text(
            input_wrap,
            height=3,
            bg=self.c("panel2"),
            fg=self.c("text"),
            insertbackground=self.c("text"),
            selectbackground="#29415d",
            relief="flat",
            bd=0,
            padx=10,
            pady=8,
            wrap="word",
            font=self.font(9),
        )
        self.input.pack(fill="both", expand=True)
        self.send_button = self.button(bottom, "Send", self.send, compact=True)
        self.send_button.pack(side="right", fill="y", padx=(8, 0))

    def render_tab(self, tab):
        self._dash_flush_notes()
        if str(tab).casefold() == "codex":
            tab = "CODE"
        if self.code_poll_after is not None:
            try:
                self.root.after_cancel(self.code_poll_after)
            except tk.TclError:
                pass
            self.code_poll_after = None
        self.active_tab = tab
        self.page_view = None
        self._build_nav()
        self.clear(self.page)
        if tab == "Dashboard":
            self.render_dashboard()
        elif tab == "Projects":
            self.render_projects()
        elif tab == "CODE":
            self.render_code()
        elif tab == "Apps":
            self.render_apps()
        elif tab == "Drop":
            self.render_drop()
        elif tab == "AI Operator":
            self.render_ai_operator()
        elif tab == "Settings":
            self.render_settings()

    def open_project_detail(self, project_path):
        project_path = Path(project_path)
        if not project_path.exists():
            return
        self.active_project = project_path
        self.page_view = ("project", project_path)
        self._render_detail()

    def open_todo_detail(self, todo_id):
        project_path = self._resolve_project(str(todo_id))
        if project_path:
            self.open_project_detail(project_path)

    def open_create_todo(self):
        self._close_create_menu()
        self.page_view = ("create", "todo")
        self._render_detail()

    def open_create_project(self):
        self._close_create_menu()
        self.page_view = ("create", "project")
        self._render_detail()

    def open_project_settings(self, project_path):
        self.page_view = ("project_settings", Path(project_path))
        self._render_detail()

    def close_detail(self):
        self._cancel_autosave()
        self.page_view = None
        self.render_tab(self.active_tab)

    def _cancel_autosave(self):
        if self._autosave_after:
            try:
                self.root.after_cancel(self._autosave_after)
            except tk.TclError:
                pass
        self._autosave_after = None
        self._autosave_project_path = None

    def _render_detail(self):
        self._build_nav()
        self.clear(self.page)
        if not self.page_view:
            self.render_tab(self.active_tab)
            return
        kind, target = self.page_view
        if kind == "project":
            self.render_project_detail(Path(target))
        elif kind == "project_settings":
            self.render_project_settings(Path(target))
        elif kind == "create":
            if target == "todo":
                self.render_create_todo()
            else:
                self.render_create_project()

    def render_dashboard(self):
        self._close_calendar_popup()
        self._close_create_menu()
        self._dash_paint = {}

        head = tk.Frame(self.page, bg=self.c("panel"))
        head.pack(fill="x", pady=(0, 10))
        tk.Label(head, text="Dashboard", bg=self.c("panel"), fg=self.c("text"), font=self.font(18, "bold")).pack(
            side="left"
        )
        self.header_btn(head, "+", self._toggle_create_menu, hint="New To-Do or Project").pack(side="right")
        self.header_btn(head, "\u21bb", self.refresh_dashboard_data, hint="Refresh weather and markets").pack(
            side="right", padx=(0, 6)
        )

        scroll = ScrollFrame(self.page, self.c("panel"))
        scroll.pack(fill="both", expand=True)
        body = scroll.inner
        body.columnconfigure(0, weight=1, uniform="dash")
        body.columnconfigure(1, weight=1, uniform="dash")

        self._dash_hero(body).grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        self._dash_actions(body).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        self._dash_markets_card(body).grid(row=2, column=0, sticky="nsew", padx=(0, 5), pady=(0, 10))
        self._dash_system_card(body).grid(row=2, column=1, sticky="nsew", padx=(5, 0), pady=(0, 10))
        self._dash_notes_card(body).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        self._dash_todo_card(body).grid(row=4, column=0, sticky="nsew", padx=(0, 5), pady=(0, 12))
        self._dash_forecast_card(body).grid(row=4, column=1, sticky="nsew", padx=(5, 0), pady=(0, 12))

        self._dash_refresh_weather()
        self._dash_refresh_markets()
        self._dash_refresh_gpu()

    # ------------------------------------------------------------------ cards

    def dash_card(self, parent, title, meta=""):
        """A titled panel card: returns (card, body). card.meta_label is the right-hand note."""
        border = self.blend_color(self.c("surface"), self.c("muted"), 0.16)
        card = tk.Frame(parent, bg=self.c("surface"), highlightbackground=border, highlightthickness=1, bd=0)
        head = tk.Frame(card, bg=self.c("surface"))
        head.pack(fill="x", padx=14, pady=(11, 0))
        tk.Label(
            head, text=title.upper(), bg=self.c("surface"), fg=self.c("muted"), font=self.font(8, "bold")
        ).pack(side="left")
        card.meta_label = tk.Label(
            head,
            text=meta,
            bg=self.c("surface"),
            fg=self.blend_color(self.c("muted"), self.c("surface"), 0.42),
            font=self.font(8),
        )
        card.meta_label.pack(side="right")
        body = tk.Frame(card, bg=self.c("surface"))
        body.pack(fill="both", expand=True, padx=14, pady=(8, 12))
        card.body = body
        return card

    def _dash_greeting(self):
        hour = datetime.now().hour
        if hour < 5:
            return "Still up"
        if hour < 10:
            return "Good morning"
        if hour < 14:
            return "Good day"
        if hour < 18:
            return "Good afternoon"
        return "Good evening"

    def _dash_hero(self, parent):
        accent = self.c("accent")
        bg = self.blend_color(self.c("surface"), accent, 0.05)
        border = self.blend_color(bg, accent, 0.22)
        hero = tk.Frame(parent, bg=bg, highlightbackground=border, highlightthickness=1, bd=0)

        # The weather column is packed first so it always keeps its full width;
        # the clock column takes whatever is left over.
        right = tk.Frame(hero, bg=bg)
        right.pack(side="right", fill="y", padx=(10, 16), pady=14)
        self._dash_weather_block(right, bg)

        left = tk.Frame(hero, bg=bg)
        left.pack(side="left", fill="both", expand=True, padx=(16, 10), pady=14)
        tk.Label(left, text=self._dash_greeting(), bg=bg, fg=accent, font=self.font(9, "bold")).pack(anchor="w")

        clock_row = tk.Frame(left, bg=bg)
        clock_row.pack(anchor="w", pady=(2, 0))
        clock = tk.Label(clock_row, text="--:--", bg=bg, fg=self.c("text"), font=self.font(32, "bold"))
        clock.pack(side="left")
        seconds = tk.Label(
            clock_row, text="00", bg=bg, fg=self.blend_color(self.c("muted"), bg, 0.25), font=self.font(11, "bold"), width=2
        )
        seconds.pack(side="left", anchor="s", pady=(0, 8), padx=(4, 0))

        date_label = tk.Label(left, text="", bg=bg, fg=self.c("text"), font=self.font(10))
        date_label.pack(anchor="w")
        progress = tk.Canvas(left, height=6, bg=bg, highlightthickness=0, bd=0)
        progress.pack(fill="x", pady=(10, 4))
        span_label = tk.Label(left, text="", bg=bg, fg=self.c("muted"), font=self.font(8))
        span_label.pack(anchor="w")

        self._dash_tick(clock, seconds, date_label, progress, span_label)
        return hero

    def _dash_weather_block(self, parent, bg):
        top = tk.Frame(parent, bg=bg)
        top.pack(anchor="e")
        glyph = tk.Label(top, text="\u2601", bg=bg, fg=self.c("accent"), font=("Segoe UI Symbol", 26))
        glyph.pack(side="left", padx=(0, 8))
        temp = tk.Label(top, text="--\u00b0", bg=bg, fg=self.c("text"), font=self.font(26, "bold"))
        temp.pack(side="left")
        condition = tk.Label(parent, text="Loading weather...", bg=bg, fg=self.c("text"), font=self.font(10))
        condition.pack(anchor="e")
        detail = tk.Label(parent, text="", bg=bg, fg=self.c("muted"), font=self.font(8))
        detail.pack(anchor="e")
        sun = tk.Label(parent, text="", bg=bg, fg=self.blend_color(self.c("muted"), bg, 0.3), font=self.font(8))
        sun.pack(anchor="e", pady=(6, 0))

        def paint():
            data = self._dash_cache.get("weather") or {}
            if data.get("error") or data.get("temp") is None:
                condition.configure(text="Weather unavailable")
                detail.configure(text=str(data.get("error", ""))[:40])
                return
            label, symbol = weather_code_text(data.get("code"))
            glyph.configure(text=symbol)
            temp.configure(text=f"{round(float(data['temp']))}\u00b0")
            condition.configure(text=f"{label} \u00b7 {data.get('location', '')}")
            bits = []
            if data.get("feels") is not None:
                bits.append(f"feels {round(float(data['feels']))}\u00b0")
            if data.get("wind") is not None:
                bits.append(f"wind {round(float(data['wind']))} km/h")
            if data.get("humidity") is not None:
                bits.append(f"{round(float(data['humidity']))}% rh")
            detail.configure(text="  \u00b7  ".join(bits))
            if data.get("sunrise") and data.get("sunset"):
                sun.configure(text=f"\u2191 {data['sunrise']}   \u2193 {data['sunset']}")

        self._dash_paint["weather_now"] = (condition, paint)
        paint()

    def _dash_tick(self, clock, seconds, date_label, progress, span_label):
        if not clock.winfo_exists():
            return
        now = datetime.now()
        clock.configure(text=now.strftime("%H:%M"))
        seconds.configure(text=now.strftime("%S"))
        date_label.configure(text=now.strftime("%A, %d %B %Y").replace(" 0", " "))

        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elapsed = (now - start).total_seconds() / 86400.0
        week = now.isocalendar()[1]
        day_of_year = int(now.strftime("%j"))
        year_days = 366 if (now.year % 4 == 0 and (now.year % 100 != 0 or now.year % 400 == 0)) else 365
        span_label.configure(
            text=f"week {week}  \u00b7  day {day_of_year} of {year_days}  \u00b7  {int(round((1 - elapsed) * 24))}h left today"
        )
        self._dash_draw_day_bar(progress, elapsed)
        self.root.after(1000, lambda: self._dash_tick(clock, seconds, date_label, progress, span_label))

    def _dash_draw_day_bar(self, canvas, elapsed):
        if not canvas.winfo_exists():
            return
        width = canvas.winfo_width()
        if width <= 1:
            canvas.after(60, lambda: self._dash_draw_day_bar(canvas, elapsed))
            return
        bg = canvas.cget("bg")
        canvas.delete("all")
        track = self.blend_color(bg, self.c("muted"), 0.28)
        canvas.create_rectangle(0, 1, width, 5, fill=track, outline="")
        canvas.create_rectangle(0, 1, max(2, width * elapsed), 5, fill=self.c("accent"), outline="")
        weather = self._dash_cache.get("weather") or {}
        for key, color in (("sunrise", "#ffcf70"), ("sunset", "#ff9b6a")):
            stamp = str(weather.get(key) or "")
            if len(stamp) == 5 and ":" in stamp:
                hour, minute = stamp.split(":")
                position = (int(hour) * 60 + int(minute)) / 1440.0 * width
                canvas.create_rectangle(position - 1, 0, position + 1, 6, fill=color, outline="")

    def _dash_actions(self, parent):
        row = tk.Frame(parent, bg=self.c("panel"))
        actions = (
            ("CODE", lambda: self.render_tab("CODE"), "Open Codex, Claude, and Cursor sessions"),
            ("OPERATOR", lambda: self.render_tab("AI Operator"), "Hand the mouse to the agent"),
            ("Drop", lambda: self.render_tab("Drop"), "Drop files in"),
            ("Phone \u2192 PC", self.open_phone_photos, "Send photos from your phone"),
            ("Downloads", self.open_downloads_folder, "Open the downloads folder"),
            ("Record", self.open_screen_recorder_menu, "Record the screen"),
            ("Pad", self._dash_start_macropad, "Start the macropad controller"),
        )
        for index, (label, command, hint) in enumerate(actions):
            self.header_chip(row, label, command, hint=hint).pack(side="left", padx=(0 if index == 0 else 5, 0))
        return row

    def _find_macropad_script(self):
        """Locate the ESP32 macro pad app (macro_pad.py)."""
        candidates = [
            Path(r"C:\1 - Projects\macro keybaord\macro_pad.py"),
            Path.home() / "Documents" / "macro keybaord" / "macro_pad.py",
            Path(__file__).resolve().parent.parent / "macro keybaord" / "macro_pad.py",
        ]
        for path in candidates:
            if path.exists():
                return path
        return None

    def _macropad_running(self):
        if not sys.platform.startswith("win"):
            return False
        try:
            completed = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    (
                        "(Get-CimInstance Win32_Process | Where-Object { "
                        "$_.CommandLine -like '*macro_pad.py*' "
                        "} | Measure-Object).Count"
                    ),
                ],
                capture_output=True,
                text=True,
                timeout=3,
                creationflags=CREATE_NO_WINDOW,
            )
            return int((completed.stdout or "0").strip() or "0") > 0
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return False

    def _start_macropad_app(self):
        script = self._find_macropad_script()
        if script is None:
            return False, "Macropad app not found (macro_pad.py)."
        if self._macropad_running():
            return True, "Macropad already running."
        pythonw = self._find_pythonw() or sys.executable
        try:
            creationflags = 0x00000008 if sys.platform.startswith("win") else 0  # DETACHED_PROCESS
            subprocess.Popen(
                [pythonw, "-B", str(script), "--hidden"],
                cwd=str(script.parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            return True, "Macropad started."
        except OSError as exc:
            return False, f"Could not start macropad: {exc}"

    def _dash_start_macropad(self):
        """Dashboard Pad: start the ESP32 macropad app + ensure AHK hotkeys."""
        ok, msg = self._start_macropad_app()
        # Dictate / open-aiOS keys still need autocorrect.ahk.
        try:
            self._ensure_hotkeys()
        except Exception:
            pass
        try:
            self.local_reply(msg)
        except Exception:
            pass

    def _dash_markets_card(self, parent):
        card = self.dash_card(parent, "Markets", "--")
        body = card.body
        body.columnconfigure(0, weight=1)
        rows = []
        tickers = list((self.config.get("dashboard") or {}).get("tickers") or [])
        for index, symbol in enumerate(tickers[:6]):
            name = tk.Label(
                body,
                text=TICKER_LABELS.get(symbol.upper(), symbol),
                bg=self.c("surface"),
                fg=self.c("text"),
                font=self.font(9, "bold"),
                anchor="w",
            )
            name.grid(row=index, column=0, sticky="w", pady=3)
            spark = tk.Canvas(body, width=58, height=20, bg=self.c("surface"), highlightthickness=0, bd=0)
            spark.grid(row=index, column=1, padx=6)
            price = tk.Label(body, text="--", bg=self.c("surface"), fg=self.c("text"), font=self.font(9), anchor="e")
            price.grid(row=index, column=2, sticky="e")
            change = tk.Label(
                body, text="--", bg=self.c("surface"), fg=self.c("muted"), font=self.font(9, "bold"), anchor="e", width=7
            )
            change.grid(row=index, column=3, sticky="e", padx=(6, 0))
            rows.append((symbol, spark, price, change))

        def paint():
            data = self._dash_cache.get("markets") or {}
            quotes = {quote["symbol"]: quote for quote in data.get("quotes", [])}
            for symbol, spark, price, change in rows:
                quote = quotes.get(symbol)
                if not quote or quote.get("price") is None:
                    price.configure(text="--")
                    change.configure(text="", fg=self.c("muted"))
                    continue
                price.configure(text=format_price(quote["price"], quote.get("currency", "")))
                delta = quote.get("change")
                color = self.c("muted")
                if delta is not None:
                    color = self.c("success") if delta >= 0 else self.c("danger")
                    change.configure(text=f"{delta:+.2f}%", fg=color)
                else:
                    change.configure(text="", fg=color)
                self._dash_draw_spark(spark, quote.get("spark") or [], color)
            if data.get("updated"):
                card.meta_label.configure(text=datetime.fromtimestamp(data["updated"]).strftime("%H:%M"))

        self._dash_paint["markets"] = (card, paint)
        paint()
        return card

    def _dash_draw_spark(self, canvas, values, color):
        if not canvas.winfo_exists():
            return
        canvas.delete("all")
        if len(values) < 2:
            return
        width = int(canvas["width"])
        height = int(canvas["height"])
        low, high = min(values), max(values)
        span = (high - low) or 1.0
        step = width / (len(values) - 1)
        points = []
        for index, value in enumerate(values):
            points.append(index * step)
            points.append(height - 2 - (value - low) / span * (height - 4))
        canvas.create_line(*points, fill=color, width=1, smooth=True)

    def _dash_system_card(self, parent):
        card = self.dash_card(parent, "This machine", "")
        body = card.body
        meters = {
            "cpu": self._dash_meter(body, "CPU"),
            "ram": self._dash_meter(body, "RAM"),
            "gpu": self._dash_meter(body, "GPU"),
            "disk": self._dash_meter(body, "Disk C:"),
        }
        footer = tk.Label(
            body,
            text="",
            bg=self.c("surface"),
            fg=self.blend_color(self.c("muted"), self.c("surface"), 0.25),
            font=self.font(8),
            anchor="w",
        )
        footer.pack(fill="x", pady=(8, 0))
        self._dash_system_tick(card, meters, footer)
        return card

    def _dash_meter(self, parent, title):
        wrap = tk.Frame(parent, bg=self.c("surface"))
        wrap.pack(fill="x", pady=(0, 7))
        head = tk.Frame(wrap, bg=self.c("surface"))
        head.pack(fill="x")
        tk.Label(head, text=title, bg=self.c("surface"), fg=self.c("muted"), font=self.font(8, "bold")).pack(side="left")
        value = tk.Label(head, text="--", bg=self.c("surface"), fg=self.c("text"), font=self.font(9))
        value.pack(side="right")
        bar = tk.Canvas(wrap, height=4, bg=self.c("surface"), highlightthickness=0, bd=0)
        bar.pack(fill="x", pady=(3, 0))
        bar.ratio = 0.0
        bar.color = self.c("accent")
        bar.bind("<Configure>", lambda _event, canvas=bar: self._dash_draw_meter(canvas))
        return {"value": value, "bar": bar}

    def _dash_draw_meter(self, canvas):
        if not canvas.winfo_exists():
            return
        width = canvas.winfo_width()
        if width <= 1:
            return
        canvas.delete("all")
        track = self.blend_color(self.c("surface"), self.c("muted"), 0.30)
        canvas.create_rectangle(0, 0, width, 4, fill=track, outline="")
        if canvas.ratio > 0:
            canvas.create_rectangle(0, 0, max(2, width * canvas.ratio), 4, fill=canvas.color, outline="")

    def _dash_set_meter(self, meter, text, ratio, color=None):
        meter["value"].configure(text=text)
        bar = meter["bar"]
        bar.ratio = max(0.0, min(1.0, float(ratio)))
        if color:
            bar.color = color
        self._dash_draw_meter(bar)

    def _dash_load_color(self, ratio):
        if ratio >= 0.9:
            return self.c("danger")
        if ratio >= 0.7:
            return "#ffb35c"
        return self.c("accent")

    def _dash_system_tick(self, card, meters, footer):
        if not card.winfo_exists():
            return
        try:
            import psutil
        except ImportError:
            footer.configure(text="Install psutil for live system stats")
            return

        cpu = psutil.cpu_percent(interval=None)
        self._dash_set_meter(meters["cpu"], f"{cpu:.0f}%", cpu / 100.0, self._dash_load_color(cpu / 100.0))
        memory = psutil.virtual_memory()
        self._dash_set_meter(
            meters["ram"],
            f"{memory.used / 1024 ** 3:.1f} / {memory.total / 1024 ** 3:.0f} GB",
            memory.percent / 100.0,
            self._dash_load_color(memory.percent / 100.0),
        )
        try:
            disk = psutil.disk_usage("C:\\")
            self._dash_set_meter(
                meters["disk"],
                f"{disk.free / 1024 ** 3:.0f} GB free",
                disk.percent / 100.0,
                self._dash_load_color(disk.percent / 100.0),
            )
        except OSError:
            pass

        gpu = self._dash_cache.get("gpu")
        if gpu:
            memory_text = ""
            if gpu.get("mem_used") and gpu.get("mem_total"):
                memory_text = f" \u00b7 {gpu['mem_used'] / 1024:.1f}/{gpu['mem_total'] / 1024:.0f} GB"
            temperature = f" \u00b7 {gpu['temp']:.0f}\u00b0C" if gpu.get("temp") else ""
            utilisation = float(gpu.get("util") or 0)
            self._dash_set_meter(
                meters["gpu"],
                f"{utilisation:.0f}%{temperature}{memory_text}",
                utilisation / 100.0,
                self._dash_load_color(utilisation / 100.0),
            )
            card.meta_label.configure(text=str(gpu.get("name", "")).replace("GeForce ", "")[:20])
        else:
            self._dash_set_meter(meters["gpu"], "no nvidia gpu", 0.0)

        bits = [f"up {format_duration(time.time() - psutil.boot_time())}"]
        counters = psutil.net_io_counters()
        now = time.time()
        if self._dash_net_sample:
            previous_time, previous_recv, previous_sent = self._dash_net_sample
            gap = max(0.5, now - previous_time)
            down = (counters.bytes_recv - previous_recv) / gap / 1024 ** 2
            up = (counters.bytes_sent - previous_sent) / gap / 1024 ** 2
            bits.append(f"net \u2193 {down:.1f} \u2191 {up:.1f} MB/s")
        self._dash_net_sample = (now, counters.bytes_recv, counters.bytes_sent)
        footer.configure(text="  \u00b7  ".join(bits))

        # nvidia-smi is a subprocess, so it runs off-thread on every third tick.
        card.tick = getattr(card, "tick", 0) + 1
        if card.tick % 3 == 0:
            self._dash_refresh_gpu()
        self.root.after(2000, lambda: self._dash_system_tick(card, meters, footer))

    def _dash_notes_card(self, parent):
        card = self.dash_card(parent, "Notes", "saves as you type")
        body = card.body
        notes = tk.Text(
            body,
            height=7,
            bg=self.c("panel2"),
            fg=self.c("text"),
            insertbackground=self.c("accent"),
            selectbackground="#29415d",
            relief="flat",
            bd=0,
            padx=12,
            pady=10,
            wrap="word",
            undo=True,
            font=self.font(10),
        )
        notes.pack(fill="both", expand=True)
        notes.insert("1.0", str((self.config.get("dashboard") or {}).get("notes") or ""))
        # <<Modified>> catches typing, paste, undo and drops alike.
        notes.edit_modified(False)
        notes.bind("<<Modified>>", self._dash_notes_modified, add="+")
        notes.bind("<FocusOut>", lambda _event: self._dash_flush_notes(), add="+")
        notes.bind("<Destroy>", lambda _event: self._dash_notes_destroyed(), add="+")
        self._dash_notes_widget = notes
        return card

    def _dash_notes_destroyed(self):
        self._dash_flush_notes()
        self._dash_notes_widget = None

    def _dash_notes_modified(self, event):
        widget = event.widget
        try:
            if not widget.edit_modified():
                return
            widget.edit_modified(False)
        except tk.TclError:
            return
        self._dash_notes_changed()

    def _dash_notes_changed(self, _event=None):
        if self._dash_notes_after:
            try:
                self.root.after_cancel(self._dash_notes_after)
            except tk.TclError:
                pass
        self._dash_notes_after = self.root.after(700, self._dash_flush_notes)

    def _dash_flush_notes(self):
        """Silent autosave — no toast, no confirmation, just persist the text."""
        if self._dash_notes_after:
            try:
                self.root.after_cancel(self._dash_notes_after)
            except tk.TclError:
                pass
            self._dash_notes_after = None
        widget = self._dash_notes_widget
        if widget is None:
            return
        try:
            text = widget.get("1.0", "end-1c")
        except tk.TclError:
            self._dash_notes_widget = None
            return
        dashboard = self.config.setdefault("dashboard", {})
        if dashboard.get("notes") == text:
            return
        dashboard["notes"] = text
        save_config(self.config)

    def _dash_todo_card(self, parent):
        todos = self.dashboard_todos()
        card = self.dash_card(parent, "To-Dos", f"{len(todos)} open" if todos else "")
        body = card.body
        if not todos:
            tk.Label(
                body,
                text="Nothing tracked yet.\nTap + to add a to-do with a deadline.",
                bg=self.c("surface"),
                fg=self.c("muted"),
                font=self.font(8),
                justify="left",
                anchor="w",
                wraplength=220,
            ).pack(anchor="w", pady=(0, 4))
            return card
        for project_path, meta in todos[:4]:
            self.dashboard_todo_row(body, project_path, meta).pack(fill="x", pady=(0, 6))
        if len(todos) > 4:
            tk.Button(
                body,
                text=f"+{len(todos) - 4} more in Projects",
                command=lambda: self.render_tab("Projects"),
                bg=self.c("surface"),
                fg=self.c("muted"),
                activebackground=self.c("surface"),
                activeforeground=self.c("accent"),
                relief="flat",
                bd=0,
                anchor="w",
                cursor="hand2",
                font=self.font(8),
            ).pack(fill="x")
        return card

    def _dash_forecast_card(self, parent):
        card = self.dash_card(parent, "Next days", "")
        body = card.body

        def paint():
            self.clear(body)
            data = self._dash_cache.get("weather") or {}
            days = data.get("days") or []
            if not days:
                self.muted(body, "Forecast loading...").pack(anchor="w")
                return
            for day in days:
                row = tk.Frame(body, bg=self.c("surface"))
                row.pack(fill="x", pady=2)
                label, symbol = weather_code_text(day.get("code"))
                tk.Label(
                    row, text=day["name"], bg=self.c("surface"), fg=self.c("text"), font=self.font(9, "bold"), width=6, anchor="w"
                ).pack(side="left")
                tk.Label(
                    row, text=symbol, bg=self.c("surface"), fg=self.c("accent"), font=("Segoe UI Symbol", 11)
                ).pack(side="left", padx=(0, 6))
                high = f"{round(float(day['high']))}\u00b0" if day.get("high") is not None else "--"
                low = f"{round(float(day['low']))}\u00b0" if day.get("low") is not None else "--"
                tk.Label(row, text=high, bg=self.c("surface"), fg=self.c("text"), font=self.font(9)).pack(side="left")
                tk.Label(row, text=f"/ {low}", bg=self.c("surface"), fg=self.c("muted"), font=self.font(9)).pack(
                    side="left", padx=(3, 0)
                )
                rain = day.get("rain")
                if rain is not None:
                    tk.Label(
                        row,
                        text=f"{round(float(rain))}%",
                        bg=self.c("surface"),
                        fg=self.blend_color(self.c("muted"), self.c("accent"), 0.5),
                        font=self.font(8, "bold"),
                    ).pack(side="right")
                tk.Label(row, text=label[:14], bg=self.c("surface"), fg=self.c("muted"), font=self.font(8)).pack(
                    side="right", padx=(0, 8)
                )
            if data.get("updated"):
                card.meta_label.configure(text=datetime.fromtimestamp(data["updated"]).strftime("%H:%M"))

        self._dash_paint["forecast"] = (card, paint)
        paint()
        return card

    # ------------------------------------------------------------------ data

    def refresh_dashboard_data(self):
        self._dash_refresh_weather(force=True)
        self._dash_refresh_markets(force=True)
        self._dash_refresh_gpu()

    def _dash_fresh(self, key, max_age):
        cached = self._dash_cache.get(key) or {}
        return bool(cached) and not cached.get("error") and time.time() - cached.get("updated", 0) < max_age

    def _dash_store(self, key, data):
        self._dash_fetching.discard(key)
        if data.get("error") and self._dash_cache.get(key) and not self._dash_cache[key].get("error"):
            return  # keep the last good payload on a failed refresh
        self._dash_cache[key] = data
        # GPU stats are polled every few seconds and are worthless once stale,
        # so only the slow network payloads go to disk.
        if not data.get("error") and key != "gpu":
            save_dashboard_cache({name: value for name, value in self._dash_cache.items() if name != "gpu"})
        self._dash_repaint(key)

    def _dash_repaint(self, key):
        targets = {"weather": ("weather_now", "forecast"), "markets": ("markets",)}.get(key, (key,))
        for name in targets:
            entry = self._dash_paint.get(name)
            if not entry:
                continue
            widget, paint = entry
            try:
                if widget.winfo_exists():
                    paint()
            except tk.TclError:
                pass

    def _dash_background(self, key, worker):
        if key in self._dash_fetching:
            return
        self._dash_fetching.add(key)

        def run():
            try:
                data = worker()
            except Exception as exc:
                data = {"error": str(exc)[:80], "updated": time.time()}
            self._ui_async(self._dash_store, key, data)

        threading.Thread(target=run, daemon=True).start()

    def _dash_refresh_weather(self, force=False):
        if not force and self._dash_fresh("weather", 900):
            return
        location = str((self.config.get("dashboard") or {}).get("location") or "Lerkil, Sweden")
        self._dash_background("weather", lambda: fetch_weather_snapshot(location))

    def _dash_refresh_markets(self, force=False):
        if not force and self._dash_fresh("markets", 300):
            return
        tickers = list((self.config.get("dashboard") or {}).get("tickers") or [])[:6]
        self._dash_background("markets", lambda: fetch_market_quotes(tickers))

    def _dash_refresh_gpu(self):
        def worker():
            gpu = query_nvidia_gpu()
            if not gpu:
                return {"error": "no gpu", "updated": time.time()}
            gpu["updated"] = time.time()
            return gpu

        self._dash_background("gpu", worker)

    def _build_bottom_tray(self):
        tray_bg = self.blend_color(self.c("panel"), self.c("surface2"), 0.72)
        border = self.blend_color(self._header_border_color(), self.c("accent"), 0.18)
        accent_line = self.blend_color(border, self.c("accent"), 0.62)

        self._bottom_tray = tk.Frame(
            self.panel,
            bg=tray_bg,
            highlightthickness=1,
            highlightbackground=border,
        )

        accent = tk.Frame(self._bottom_tray, bg=accent_line, height=2)
        accent.pack(fill="x")

        handle_row = tk.Frame(self._bottom_tray, bg=tray_bg, height=28, cursor="hand2")
        handle_row.pack(fill="x")
        handle_row.pack_propagate(False)
        self.quick_tools_handle = tk.Label(
            handle_row,
            text="QUICK TOOLS  ↑",
            bg=tray_bg,
            fg=self.blend_color(self.c("muted"), self.c("text"), 0.62),
            font=self.font(8, "bold"),
            cursor="hand2",
        )
        self.quick_tools_handle.place(relx=0.5, rely=0.5, anchor="center")

        content = tk.Frame(self._bottom_tray, bg=tray_bg)
        content.pack(fill="both", expand=True, padx=14, pady=(3, 10))

        groups = tk.Frame(content, bg=tray_bg)
        groups.pack(fill="both", expand=True)

        transfer, transfer_cards = self.quick_tools_category(
            groups, "TRANSFER", "Move images onto this PC", self.c("accent")
        )
        transfer.pack(side="left", fill="both", expand=True, padx=(0, 5))
        phone_photos_btn = self.quick_tool_card(
            transfer_cards,
            "⇄",
            "Phone to PC",
            "Scan QR, shoot, auto-send",
            self.open_phone_photos,
            accent_color=self.c("accent"),
            featured=True,
        )
        phone_photos_btn.pack(side="left", fill="both", expand=True, padx=(0, 3))
        paste_btn = self.quick_tool_card(
            transfer_cards,
            "V",
            "Paste image",
            "Save the clipboard image",
            self.save_clipboard_image,
            accent_color=self.c("accent"),
        )
        paste_btn.pack(side="left", fill="both", expand=True, padx=(3, 0))

        capture_color = "#ffb35c"
        capture, capture_cards = self.quick_tools_category(
            groups, "SCREEN CAPTURE", "Record anything you see", capture_color
        )
        capture.pack(side="left", fill="both", expand=True, padx=5)
        record_btn = self.quick_tool_card(
            capture_cards,
            "●",
            "Record screen",
            "Area, window, or monitor",
            self.open_screen_recorder_menu,
            accent_color=capture_color,
        )
        record_btn.pack(side="left", fill="both", expand=True, padx=(0, 3))
        self.screen_record_quick_btn = record_btn
        recordings_btn = self.quick_tool_card(
            capture_cards,
            "▶",
            "Recordings",
            "Browse your saved videos",
            self.open_recordings_folder,
            accent_color=capture_color,
        )
        recordings_btn.pack(side="left", fill="both", expand=True, padx=(3, 0))

        files_color = "#a99cff"
        files, file_cards = self.quick_tools_category(
            groups, "FILES", "Jump to common folders", files_color
        )
        files.pack(side="left", fill="both", expand=True, padx=(5, 0))
        downloads_btn = self.quick_tool_card(
            file_cards,
            "↓",
            "Downloads",
            "Open your downloads folder",
            self.open_downloads_folder,
            accent_color=files_color,
        )
        downloads_btn.pack(fill="both", expand=True)

        footer = tk.Frame(content, bg=tray_bg)
        footer.pack(fill="x", pady=(8, 0))
        self.quick_tools_status = tk.Label(
            footer,
            text="Ready",
            bg=tray_bg,
            fg=self.blend_color(self.c("muted"), self.c("text"), 0.58),
            anchor="w",
            font=self.font(8, "bold"),
        )
        self.quick_tools_status.pack(side="left")
        close_hint = tk.Label(
            footer,
            text="Move away to close",
            bg=tray_bg,
            fg=self.blend_color(self.c("muted"), tray_bg, 0.72),
            anchor="e",
            font=self.font(8),
        )
        close_hint.pack(side="right")

        self._bottom_tray.place(
            relx=0.5,
            rely=1,
            relwidth=self._tray_peek_relwidth,
            anchor="s",
            height=self._tray_peek_h,
        )
        self._bottom_tray.lift()
        self._tray_current_h = self._tray_peek_h
        self._tray_current_relwidth = self._tray_peek_relwidth

        tray_widgets = {
            self._bottom_tray,
            accent,
            handle_row,
            self.quick_tools_handle,
            content,
            groups,
            transfer,
            capture,
            files,
            footer,
            close_hint,
            paste_btn,
            downloads_btn,
            record_btn,
            recordings_btn,
            phone_photos_btn,
        }
        self._tray_widget_ids = {id(widget) for widget in tray_widgets}
        self._tray_widget_ids.add(id(self.quick_tools_status))

        handle_row.bind("<Button-1>", self._tray_toggle, add="+")
        self.quick_tools_handle.bind("<Button-1>", self._tray_toggle, add="+")

        self.panel.bind("<Motion>", self._tray_on_motion, add="+")
        self.root.bind("<Motion>", self._tray_on_motion, add="+")

    def _tray_widget_contains(self, widget):
        while widget is not None:
            if id(widget) in self._tray_widget_ids:
                return True
            try:
                widget = widget.master
            except AttributeError:
                break
        return False

    def _tray_on_motion(self, _event=None):
        if self._bottom_tray is None:
            return
        try:
            panel_w = self.panel.winfo_width()
            panel_h = self.panel.winfo_height()
            if panel_w <= 1 or panel_h <= 1:
                return
            x_in_panel = self.root.winfo_pointerx() - self.panel.winfo_rootx()
            y_in_panel = self.root.winfo_pointery() - self.panel.winfo_rooty()
        except tk.TclError:
            return

        if self._tray_open:
            try:
                tray_left = self._bottom_tray.winfo_rootx() - self.panel.winfo_rootx()
                tray_top = self._bottom_tray.winfo_rooty() - self.panel.winfo_rooty()
                tray_right = tray_left + self._bottom_tray.winfo_width()
                inside_open_tray = (
                    tray_left - 8 <= x_in_panel <= tray_right + 8
                    and tray_top - 8 <= y_in_panel <= panel_h
                )
            except tk.TclError:
                inside_open_tray = False
            if inside_open_tray:
                self._tray_show()
            else:
                self._tray_schedule_hide()
            return

        collapsed_w = panel_w * self._tray_peek_relwidth
        centered = abs(x_in_panel - panel_w / 2) <= collapsed_w / 2 + 8
        hot_zone = self._tray_peek_h + 8
        if centered and y_in_panel >= panel_h - hot_zone:
            self._tray_show()

    def _tray_show(self, _event=None):
        if self._bottom_tray is None:
            return
        if self._tray_hide_job is not None:
            self.root.after_cancel(self._tray_hide_job)
            self._tray_hide_job = None
        if self._tray_open:
            return
        self._tray_open = True
        if self.quick_tools_handle is not None:
            try:
                self.quick_tools_handle.configure(text="QUICK TOOLS  ↓", fg=self.c("accent"))
            except tk.TclError:
                pass
        self._tray_start_animation()

    def _tray_toggle(self, _event=None):
        if self._tray_open:
            self._tray_close()
        else:
            self._tray_show()
        return "break"

    def _tray_schedule_hide(self, _event=None):
        if self._bottom_tray is None or not self._tray_open:
            return
        if self._tray_hide_job is not None:
            return

        def hide():
            self._tray_hide_job = None
            try:
                widget = self.root.winfo_containing(self.root.winfo_pointerx(), self.root.winfo_pointery())
            except tk.TclError:
                widget = None
            if widget is not None and self._tray_widget_contains(widget):
                return
            self._tray_close()

        self._tray_hide_job = self.root.after(360, hide)

    def _tray_close(self):
        if self._bottom_tray is None:
            return
        if self._tray_hide_job is not None:
            self.root.after_cancel(self._tray_hide_job)
            self._tray_hide_job = None
        if not self._tray_open:
            return
        self._tray_open = False
        if self.quick_tools_handle is not None:
            try:
                self.quick_tools_handle.configure(
                    text="QUICK TOOLS  ↑",
                    fg=self.blend_color(self.c("muted"), self.c("text"), 0.62),
                )
            except tk.TclError:
                pass
        self._tray_start_animation()

    def _tray_start_animation(self):
        if self._bottom_tray is None or self._tray_anim_job is not None:
            return
        self._tray_animation_step()

    def _tray_animation_step(self):
        self._tray_anim_job = None
        if self._bottom_tray is None:
            return

        target_h = self._tray_target_h if self._tray_open else self._tray_peek_h
        target_relwidth = self._tray_open_relwidth if self._tray_open else self._tray_peek_relwidth
        current_h = self._tray_current_h
        current_w = self._tray_current_relwidth

        if abs(current_h - target_h) <= 1 and abs(current_w - target_relwidth) <= 0.005:
            self._tray_current_h = target_h
            self._tray_current_relwidth = target_relwidth
            try:
                self._bottom_tray.place_configure(height=int(target_h), relwidth=target_relwidth)
            except tk.TclError:
                pass
            if not self._tray_open and self.quick_tools_status is not None:
                try:
                    self.quick_tools_status.configure(
                        text="Ready",
                        fg=self.blend_color(self.c("muted"), self.c("text"), 0.58),
                    )
                except tk.TclError:
                    pass
            return

        diff_h = target_h - current_h
        if abs(diff_h) <= 1:
            next_h = target_h
        else:
            step_h = max(2, min(12, int(abs(diff_h) * 0.34)))
            next_h = current_h + step_h if diff_h > 0 else current_h - step_h
            if diff_h > 0 and next_h > target_h:
                next_h = target_h
            elif diff_h < 0 and next_h < target_h:
                next_h = target_h

        diff_w = target_relwidth - current_w
        if abs(diff_w) <= 0.005:
            next_w = target_relwidth
        else:
            step_w = max(0.012, min(0.08, abs(diff_w) * 0.34))
            next_w = current_w + step_w if diff_w > 0 else current_w - step_w
            if diff_w > 0 and next_w > target_relwidth:
                next_w = target_relwidth
            elif diff_w < 0 and next_w < target_relwidth:
                next_w = target_relwidth

        self._tray_current_h = next_h
        self._tray_current_relwidth = next_w
        try:
            self._bottom_tray.place_configure(height=int(next_h), relwidth=next_w)
        except tk.TclError:
            return
        self._tray_anim_job = self.root.after(12, self._tray_animation_step)

    def save_clipboard_image(self):
        saved_path = None
        errors = []
        try:
            saved_path = self._save_clipboard_image_with_pillow()
        except Exception as exc:
            errors.append(str(exc))
        if saved_path is None:
            try:
                saved_path = self._save_clipboard_dib()
            except Exception as exc:
                errors.append(str(exc))

        if saved_path:
            self._set_quick_tools_status(f"Saved {saved_path.name}", True)
            return saved_path

        hint = "No clipboard image"
        if errors:
            hint = f"Could not save image: {errors[-1]}"
        self._set_quick_tools_status(hint, False)
        return None

    def _save_clipboard_image_with_pillow(self):
        try:
            from PIL import ImageGrab
        except ImportError:
            return None

        data = ImageGrab.grabclipboard()
        if data is None:
            return None

        if isinstance(data, list):
            for item in data:
                path = Path(item)
                if path.is_file() and path.suffix.lower() in IMAGE_FILE_EXTS:
                    target = self.unique_download_path("aios-clipboard", path.suffix.lower())
                    shutil.copy2(path, target)
                    return target
            return None

        if hasattr(data, "save"):
            target = self.unique_download_path("aios-clipboard", ".png")
            data.save(target, "PNG")
            return target

        return None

    def _save_clipboard_dib(self):
        if not sys.platform.startswith("win"):
            return None

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
        user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = wintypes.BOOL
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalSize.restype = ctypes.c_size_t
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL

        cf_dib = 8
        cf_dibv5 = 17
        if not user32.OpenClipboard(None):
            return None
        try:
            fmt = 0
            if user32.IsClipboardFormatAvailable(cf_dibv5):
                fmt = cf_dibv5
            elif user32.IsClipboardFormatAvailable(cf_dib):
                fmt = cf_dib
            if not fmt:
                return None
            handle = user32.GetClipboardData(fmt)
            if not handle:
                return None
            size = kernel32.GlobalSize(handle)
            pointer = kernel32.GlobalLock(handle)
            if not pointer or not size:
                return None
            try:
                dib = ctypes.string_at(pointer, size)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

        target = self.unique_download_path("aios-clipboard", ".bmp")
        target.write_bytes(self._dib_to_bmp(dib))
        return target

    def _dib_to_bmp(self, dib):
        if len(dib) < 4:
            raise ValueError("Clipboard bitmap is empty.")
        header_size = int.from_bytes(dib[:4], "little")
        if header_size < 12 or header_size > len(dib):
            raise ValueError("Clipboard bitmap header is invalid.")
        pixel_offset = self._dib_pixel_offset(dib, header_size)
        file_size = 14 + len(dib)
        return (
            b"BM"
            + file_size.to_bytes(4, "little")
            + b"\0\0\0\0"
            + (14 + pixel_offset).to_bytes(4, "little")
            + dib
        )

    def _dib_pixel_offset(self, dib, header_size):
        if header_size == 12:
            bit_count = int.from_bytes(dib[10:12], "little")
            colors = 1 << bit_count if bit_count <= 8 else 0
            return header_size + colors * 3
        if header_size < 40:
            return header_size

        bit_count = int.from_bytes(dib[14:16], "little")
        compression = int.from_bytes(dib[16:20], "little")
        colors_used = int.from_bytes(dib[32:36], "little")
        colors = colors_used or (1 << bit_count if bit_count <= 8 else 0)
        masks = 12 if compression == 3 and header_size == 40 else 0
        return header_size + masks + colors * 4

    def downloads_dir(self):
        path = Path.home() / "Downloads"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def open_downloads_folder(self):
        try:
            os.startfile(str(self.downloads_dir()))
        except OSError as exc:
            self._set_quick_tools_status(str(exc), False)

    def recordings_dir(self):
        path = Path.home() / "Videos" / RECORDINGS_FOLDER_NAME
        path.mkdir(parents=True, exist_ok=True)
        return path

    def open_recordings_folder(self):
        try:
            os.startfile(str(self.recordings_dir()))
            self._set_quick_tools_status("Opened recordings", True)
        except OSError as exc:
            self._set_quick_tools_status(str(exc), False)

    def phone_photos_dir(self):
        path = Path.home() / "Pictures" / "aiOS Phone Photos"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def open_phone_photos(self):
        self._set_quick_tools_status("Creating phone photo link…", True)

        def create_session():
            try:
                req = urllib.request.Request(
                    "http://127.0.0.1:5000/api/photo-drop/session",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=4) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.root.after(0, lambda: self._show_phone_photos_session(payload))
            except Exception as exc:
                message = f"Phone bridge unavailable: {exc}"
                self.root.after(0, lambda value=message: self._set_quick_tools_status(value, False))

        threading.Thread(target=create_session, daemon=True).start()

    def _show_phone_photos_session(self, session):
        self._close_phone_photos()
        try:
            import qrcode
            from PIL import Image, ImageTk

            qr = qrcode.make(session["url"]).convert("RGB").resize(
                (270, 270), Image.Resampling.NEAREST
            )
            qr_image = ImageTk.PhotoImage(qr)
        except Exception as exc:
            self._set_quick_tools_status(f"Could not create QR code: {exc}", False)
            return

        popup = tk.Toplevel(self.root)
        popup.title("Phone Photos")
        popup.configure(bg=self.c("panel"))
        popup.resizable(False, False)
        popup.attributes("-topmost", True)
        popup.geometry("430x590")
        try:
            popup.iconbitmap(str(APP_ICON_PATH))
        except (OSError, tk.TclError):
            pass
        self.phone_photos_popup = popup
        self.phone_photos_qr_image = qr_image

        tk.Label(
            popup, text="Phone Photos", bg=self.c("panel"), fg=self.c("text"),
            font=self.font(19, "bold"),
        ).pack(pady=(22, 4))
        tk.Label(
            popup, text="Scan with any phone on the same Wi-Fi", bg=self.c("panel"),
            fg=self.c("muted"), font=self.font(10),
        ).pack()
        qr_frame = tk.Frame(popup, bg="#ffffff", padx=12, pady=12)
        qr_frame.pack(pady=18)
        tk.Label(qr_frame, image=qr_image, bg="#ffffff").pack()

        status = tk.Label(
            popup, text="Waiting for photos…", bg=self.c("panel"), fg=self.c("accent"),
            font=self.font(11, "bold"),
        )
        status.pack(pady=(0, 4))
        detail = tk.Label(
            popup, text="Photos upload automatically after each shot", bg=self.c("panel"),
            fg=self.c("muted"), font=self.font(9),
        )
        detail.pack()

        actions = tk.Frame(popup, bg=self.c("panel"))
        actions.pack(fill="x", padx=26, pady=(22, 8))
        self.button(
            actions, "Open Folder",
            lambda path=session["folder"]: os.startfile(path),
            compact=True, active=True,
        ).pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.button(
            actions, "Copy Link",
            lambda url=session["url"]: self._copy_phone_photos_link(url),
            compact=True,
        ).pack(side="left", fill="x", expand=True, padx=(5, 0))

        popup.protocol("WM_DELETE_WINDOW", self._close_phone_photos)
        popup.bind("<Escape>", lambda _event: self._close_phone_photos())
        self._set_quick_tools_status("Scan QR to send photos", True)
        self._poll_phone_photos(session["token"], status, detail)

    def _copy_phone_photos_link(self, url):
        self.root.clipboard_clear()
        self.root.clipboard_append(url)
        self._set_quick_tools_status("Phone photo link copied", True)

    def _poll_phone_photos(self, token, status_label, detail_label):
        popup = self.phone_photos_popup
        if popup is None or not popup.winfo_exists():
            return

        def fetch():
            try:
                url = f"http://127.0.0.1:5000/api/photo-drop/{token}/status"
                with urllib.request.urlopen(url, timeout=2) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.root.after(0, lambda: update(payload))
            except Exception:
                self.root.after(0, schedule)

        def update(payload):
            if self.phone_photos_popup is not popup or not popup.winfo_exists():
                return
            count = int(payload.get("count") or 0)
            status_label.configure(text=f"{count} photo{'s' if count != 1 else ''} received")
            last_name = payload.get("last_filename") or "Take photos on your phone"
            detail_label.configure(text=last_name)
            if count:
                self._set_quick_tools_status(f"Received {count} phone photo{'s' if count != 1 else ''}", True)
            schedule()

        def schedule():
            if self.phone_photos_popup is popup and popup.winfo_exists():
                self.phone_photos_poll_job = self.root.after(
                    900, lambda: threading.Thread(target=fetch, daemon=True).start()
                )

        schedule()

    def _close_phone_photos(self):
        if self.phone_photos_poll_job is not None:
            try:
                self.root.after_cancel(self.phone_photos_poll_job)
            except tk.TclError:
                pass
        self.phone_photos_poll_job = None
        popup = self.phone_photos_popup
        self.phone_photos_popup = None
        self.phone_photos_qr_image = None
        if popup is not None:
            try:
                popup.destroy()
            except tk.TclError:
                pass

    def unique_download_path(self, stem, suffix):
        downloads = self.downloads_dir()
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = downloads / f"{stem}-{timestamp}{suffix}"
        if not base.exists():
            return base
        for index in range(2, 1000):
            candidate = downloads / f"{stem}-{timestamp}-{index}{suffix}"
            if not candidate.exists():
                return candidate
        raise FileExistsError("Could not allocate a Downloads filename.")

    def unique_recording_path(self):
        folder = self.recordings_dir()
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = folder / f"aios-recording-{timestamp}.mp4"
        if not base.exists():
            return base
        for index in range(2, 1000):
            candidate = folder / f"aios-recording-{timestamp}-{index}.mp4"
            if not candidate.exists():
                return candidate
        raise FileExistsError("Could not allocate a recording filename.")

    def open_screen_recorder_menu(self):
        if self._screen_recording_active():
            self.stop_screen_recording()
            return

        ffmpeg = self._ffmpeg_path()
        if not ffmpeg:
            self._set_quick_tools_status("FFmpeg not found", False)
            return

        self._close_screen_recorder_menu()
        popup = tk.Toplevel(self.root)
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.configure(bg=self.c("panel"))
        x = self.root.winfo_pointerx() + 4
        y = self.root.winfo_pointery() + 4
        popup.geometry(f"+{x}+{y}")
        self.screen_record_popup = popup

        inner = tk.Frame(popup, bg=self.c("panel"), highlightthickness=1, highlightbackground=self._header_border_color())
        inner.pack(fill="both", expand=True, padx=1, pady=1)

        top = tk.Frame(inner, bg=self.c("panel"))
        top.pack(fill="x", padx=8, pady=(8, 6))
        tk.Label(top, text="Screen Record", bg=self.c("panel"), fg=self.c("text"), font=self.font(9, "bold")).pack(side="left")
        self.header_btn(top, "x", self._close_screen_recorder_menu, hint="Close").pack(side="right")

        self.quick_tool_chip(inner, "Select Area", self.start_area_record_selection, hint="Drag a screen area").pack(fill="x", padx=8, pady=(0, 6))

        self.screen_record_monitors = self._list_recording_monitors()
        monitor_labels = [item["label"] for item in self.screen_record_monitors] or ["No monitors"]
        self.screen_record_monitor_var = tk.StringVar(value=monitor_labels[0])
        monitor_row = tk.Frame(inner, bg=self.c("panel"))
        monitor_row.pack(fill="x", padx=8, pady=(0, 6))
        monitor_menu = tk.OptionMenu(monitor_row, self.screen_record_monitor_var, *monitor_labels)
        self.style_option(monitor_menu)
        monitor_menu.pack(side="left", fill="x", expand=True)
        self.quick_tool_chip(monitor_row, "Monitor", self.start_monitor_recording, hint="Record selected monitor").pack(side="right", padx=(6, 0))

        self.screen_record_windows = self._list_recording_windows()
        window_labels = [item["label"] for item in self.screen_record_windows] or ["No windows"]
        self.screen_record_window_var = tk.StringVar(value=window_labels[0])
        window_row = tk.Frame(inner, bg=self.c("panel"))
        window_row.pack(fill="x", padx=8, pady=(0, 6))
        window_menu = tk.OptionMenu(window_row, self.screen_record_window_var, *window_labels)
        self.style_option(window_menu)
        window_menu.pack(side="left", fill="x", expand=True)
        self.quick_tool_chip(window_row, "Window", self.start_window_recording, hint="Record selected window").pack(side="right", padx=(6, 0))

        self.quick_tool_chip(inner, "Open Folder", self.open_recordings_folder, hint="Open recordings folder").pack(fill="x", padx=8, pady=(0, 8))
        popup.bind("<Escape>", lambda _event: self._close_screen_recorder_menu())

    def _close_screen_recorder_menu(self):
        popup = self.screen_record_popup
        if popup is not None:
            try:
                popup.destroy()
            except tk.TclError:
                pass
        self.screen_record_popup = None

    def _screen_recording_active(self):
        return self.screen_record_process is not None and self.screen_record_process.poll() is None

    def start_area_record_selection(self):
        if self._screen_recording_active():
            self.stop_screen_recording()
            return
        self._close_screen_recorder_menu()
        bounds = self._virtual_screen_bounds()
        overlay = tk.Toplevel(self.root)
        overlay.overrideredirect(True)
        overlay.attributes("-topmost", True)
        overlay.attributes("-alpha", 0.24)
        overlay.configure(bg="#000000")
        overlay.geometry(f"{bounds['width']}x{bounds['height']}+{bounds['left']}+{bounds['top']}")
        self.screen_record_area_overlay = overlay

        canvas = tk.Canvas(overlay, bg="#000000", highlightthickness=0, cursor="crosshair")
        canvas.pack(fill="both", expand=True)
        selection = {"start": None, "rect": None}

        def begin(event):
            selection["start"] = (event.x, event.y)
            if selection["rect"] is not None:
                canvas.delete(selection["rect"])
            selection["rect"] = canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="#ffffff", width=2)

        def drag(event):
            if selection["start"] is None or selection["rect"] is None:
                return
            x0, y0 = selection["start"]
            canvas.coords(selection["rect"], x0, y0, event.x, event.y)

        def finish(event):
            if selection["start"] is None:
                self._cancel_area_record_selection()
                return
            x0, y0 = selection["start"]
            x1, y1 = event.x, event.y
            left = bounds["left"] + min(x0, x1)
            top = bounds["top"] + min(y0, y1)
            width = abs(x1 - x0)
            height = abs(y1 - y0)
            self._cancel_area_record_selection()
            if width < 24 or height < 24:
                self._set_quick_tools_status("Area too small", False)
                return
            self._begin_screen_recording({"left": left, "top": top, "width": width, "height": height}, "Area")

        canvas.bind("<ButtonPress-1>", begin)
        canvas.bind("<B1-Motion>", drag)
        canvas.bind("<ButtonRelease-1>", finish)
        overlay.bind("<Escape>", lambda _event: self._cancel_area_record_selection())
        overlay.focus_force()
        self._set_quick_tools_status("Drag area", True)

    def _cancel_area_record_selection(self):
        overlay = self.screen_record_area_overlay
        if overlay is not None:
            try:
                overlay.destroy()
            except tk.TclError:
                pass
        self.screen_record_area_overlay = None

    def start_monitor_recording(self):
        if self._screen_recording_active():
            self.stop_screen_recording()
            return
        label = self.screen_record_monitor_var.get() if self.screen_record_monitor_var else ""
        monitor = next((item for item in self.screen_record_monitors if item["label"] == label), None)
        if not monitor:
            self._set_quick_tools_status("No monitor selected", False)
            return
        self._close_screen_recorder_menu()
        self._begin_screen_recording(monitor, monitor["label"])

    def start_window_recording(self):
        if self._screen_recording_active():
            self.stop_screen_recording()
            return
        label = self.screen_record_window_var.get() if self.screen_record_window_var else ""
        window = next((item for item in self.screen_record_windows if item["label"] == label), None)
        if not window:
            self._set_quick_tools_status("No window selected", False)
            return
        bounds = self._window_recording_bounds(window["hwnd"])
        if not bounds:
            self._set_quick_tools_status("Window not available", False)
            return
        self._close_screen_recorder_menu()
        self._begin_screen_recording(bounds, window["title"])

    def _begin_screen_recording(self, bounds, label):
        self._set_quick_tools_status("Starting recorder", True)
        try:
            self.root.withdraw()
            self.root.update_idletasks()
        except tk.TclError:
            pass
        self.root.after(220, lambda: self._start_screen_recording_process(bounds, label))

    def _start_screen_recording_process(self, bounds, label):
        ffmpeg = self._ffmpeg_path()
        if not ffmpeg:
            self._restore_after_screen_recording()
            self._set_quick_tools_status("FFmpeg not found", False)
            return

        width = max(2, int(bounds["width"]) // 2 * 2)
        height = max(2, int(bounds["height"]) // 2 * 2)
        left = int(bounds["left"])
        top = int(bounds["top"])
        target = self.unique_recording_path()
        command = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "gdigrab",
            "-draw_mouse",
            "1",
            "-framerate",
            "60",
            "-offset_x",
            str(left),
            "-offset_y",
            str(top),
            "-video_size",
            f"{width}x{height}",
            "-i",
            "desktop",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(target),
        ]
        try:
            self.screen_record_process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                cwd=str(self.recordings_dir()),
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as exc:
            self.screen_record_process = None
            self._restore_after_screen_recording()
            self._set_quick_tools_status(str(exc), False)
            return

        self.screen_record_path = target
        self.screen_record_started_at = time.perf_counter()
        self._set_record_quick_button(True)
        self._show_screen_record_control(label)
        self._set_quick_tools_status(f"Recording {label}", True)
        self._poll_screen_recording()

    def _show_screen_record_control(self, label):
        self._destroy_screen_record_control()
        control = tk.Toplevel(self.root)
        control.overrideredirect(True)
        control.attributes("-topmost", True)
        control.configure(bg=self.c("panel"))
        self.screen_record_control = control

        inner = tk.Frame(control, bg=self.c("panel"), highlightthickness=2, highlightbackground=self.c("danger"))
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        self.screen_record_time_label = tk.Label(
            inner,
            text="● REC 00:00",
            bg=self.c("panel"),
            fg=self.c("danger"),
            font=self.font(10, "bold"),
            padx=10,
            pady=7,
        )
        self.screen_record_time_label.pack(side="left")
        self.record_stop_chip(inner, "Stop Recording", self.stop_screen_recording, hint=f"Stop recording {label}").pack(
            side="left", padx=(0, 6), pady=6
        )
        self.quick_tool_chip(inner, "Folder", self.open_recordings_folder, hint="Open recordings folder").pack(
            side="left", padx=(0, 6), pady=6
        )

        bounds = self._virtual_screen_bounds()
        control.update_idletasks()
        width = control.winfo_reqwidth()
        x = bounds["left"] + bounds["width"] - width - 24
        y = bounds["top"] + 24
        control.geometry(f"+{x}+{y}")
        control.bind("<Escape>", lambda _event: self.stop_screen_recording())
        self._update_screen_record_timer()

    def _update_screen_record_timer(self):
        if not self._screen_recording_active() or self.screen_record_control is None:
            return
        elapsed = max(0, int(time.perf_counter() - self.screen_record_started_at))
        minutes, seconds = divmod(elapsed, 60)
        dot = "●" if self.screen_record_dot_on else " "
        dot_color = self.c("danger") if self.screen_record_dot_on else self.blend_color(self.c("panel"), self.c("text"), 0.28)
        self.screen_record_dot_on = not self.screen_record_dot_on
        try:
            self.screen_record_time_label.configure(text=f"{dot} REC {minutes:02d}:{seconds:02d}", fg=dot_color)
        except tk.TclError:
            return
        self.screen_record_timer_job = self.root.after(500, self._update_screen_record_timer)

    def _poll_screen_recording(self):
        if self.screen_record_process is None:
            return
        if self.screen_record_process.poll() is None:
            self.screen_record_poll_job = self.root.after(1000, self._poll_screen_recording)
            return
        self._finish_screen_recording(stopped=False)

    def stop_screen_recording(self):
        if not self._screen_recording_active():
            self._finish_screen_recording(stopped=True)
            return
        process = self.screen_record_process
        try:
            if process.stdin:
                process.stdin.write(b"q\n")
                process.stdin.flush()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                process.terminate()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
        self._finish_screen_recording(stopped=True)

    def _finish_screen_recording(self, stopped):
        process = self.screen_record_process
        path = self.screen_record_path
        stderr = ""
        if process is not None:
            try:
                if process.stderr:
                    stderr = process.stderr.read().decode("utf-8", "replace").strip()
            except OSError:
                stderr = ""
        self.screen_record_process = None
        self.screen_record_path = None
        self.screen_record_started_at = 0.0
        self._set_record_quick_button(False)
        self._destroy_screen_record_control()
        self._restore_after_screen_recording()

        if path and path.exists() and path.stat().st_size > 0:
            self._set_quick_tools_status(f"Saved {path.name}", True)
            return

        if path and path.exists():
            try:
                path.unlink()
            except OSError:
                pass
        message = "Recording stopped" if stopped else "Recording failed"
        if stderr:
            message = stderr.splitlines()[-1][:96]
            self._set_quick_tools_status(message, False)

    def _ffmpeg_path(self):
        candidates = [
            shutil.which("ffmpeg"),
            r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
            r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        ]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return str(candidate)
        return ""

    def _destroy_screen_record_control(self):
        if self.screen_record_timer_job is not None:
            try:
                self.root.after_cancel(self.screen_record_timer_job)
            except tk.TclError:
                pass
            self.screen_record_timer_job = None
        if self.screen_record_poll_job is not None:
            try:
                self.root.after_cancel(self.screen_record_poll_job)
            except tk.TclError:
                pass
            self.screen_record_poll_job = None
        control = self.screen_record_control
        if control is not None:
            try:
                control.destroy()
            except tk.TclError:
                pass
        self.screen_record_control = None
        self.screen_record_time_label = None

    def _restore_after_screen_recording(self):
        try:
            self.root.deiconify()
            self.root.lift()
        except tk.TclError:
            pass

    def _set_record_quick_button(self, recording):
        button = self.screen_record_quick_btn
        if button is None:
            return
        try:
            if hasattr(button, "_quick_card_title"):
                title = button._quick_card_title
                icon_box = button._quick_card_icon_box
                icon = button._quick_card_icon
                accent = button._quick_card_accent
                if recording:
                    button.configure(
                        highlightbackground=self.c("danger"),
                        highlightcolor=self.c("danger"),
                    )
                    icon_box.configure(bg=self.c("danger"))
                    icon.configure(text="■", bg=self.c("danger"), fg="#ffffff")
                    title.configure(text="Stop recording")
                else:
                    icon_bg = self.blend_color(self.c("surface2"), accent, 0.28)
                    button.configure(
                        highlightbackground=self.blend_color(
                            self._header_border_color(), accent, 0.20
                        ),
                        highlightcolor=accent,
                    )
                    icon_box.configure(bg=icon_bg)
                    icon.configure(text="●", bg=icon_bg, fg=accent)
                    title.configure(text="Record screen")
                return
            if recording:
                button.configure(
                    text="● Stop Recording",
                    bg=self.c("danger"),
                    fg="#ffffff",
                    activebackground=self.c("danger"),
                    activeforeground="#ffffff",
                    highlightbackground=self.c("danger"),
                    highlightcolor=self.c("danger"),
                )
            else:
                button.configure(
                    text="Record",
                    bg=self.c("surface2"),
                    fg=self.c("text"),
                    activebackground=self.blend_color(self.c("surface2"), self.c("accent"), 0.26),
                    activeforeground=self.c("text"),
                    highlightbackground=self.blend_color(self._header_border_color(), self.c("text"), 0.22),
                    highlightcolor=self.blend_color(self._header_border_color(), self.c("text"), 0.22),
                )
        except tk.TclError:
            pass

    def _virtual_screen_bounds(self):
        if sys.platform.startswith("win"):
            user32 = ctypes.windll.user32
            return {
                "left": int(user32.GetSystemMetrics(76)),
                "top": int(user32.GetSystemMetrics(77)),
                "width": int(user32.GetSystemMetrics(78)),
                "height": int(user32.GetSystemMetrics(79)),
            }
        return {"left": 0, "top": 0, "width": self.root.winfo_screenwidth(), "height": self.root.winfo_screenheight()}

    def _list_recording_monitors(self):
        if not sys.platform.startswith("win"):
            bounds = self._virtual_screen_bounds()
            bounds["label"] = f"Monitor 1 {bounds['width']}x{bounds['height']}"
            return [bounds]

        monitors = []

        class RECT(ctypes.Structure):
            _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

        monitor_enum_proc = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            wintypes.HMONITOR,
            wintypes.HDC,
            ctypes.POINTER(RECT),
            wintypes.LPARAM,
        )

        def callback(_hmonitor, _hdc, rect_pointer, _lparam):
            rect = rect_pointer.contents
            width = int(rect.right - rect.left)
            height = int(rect.bottom - rect.top)
            monitors.append({"left": int(rect.left), "top": int(rect.top), "width": width, "height": height})
            return True

        ctypes.windll.user32.EnumDisplayMonitors(0, 0, monitor_enum_proc(callback), 0)
        monitors.sort(key=lambda item: (not self._bounds_contains_origin(item), item["left"], item["top"]))
        for index, monitor in enumerate(monitors, start=1):
            primary = " Primary" if self._bounds_contains_origin(monitor) else ""
            monitor["label"] = f"Monitor {index}{primary} {monitor['width']}x{monitor['height']}"
        return monitors

    def _bounds_contains_origin(self, bounds):
        return bounds["left"] <= 0 < bounds["left"] + bounds["width"] and bounds["top"] <= 0 < bounds["top"] + bounds["height"]

    def _list_recording_windows(self):
        if not sys.platform.startswith("win"):
            return []

        user32 = ctypes.windll.user32
        windows = []
        own_hwnd = 0
        try:
            own_hwnd = int(self.root.winfo_id())
        except tk.TclError:
            pass

        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd, _lparam):
            if hwnd == own_hwnd or not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value.strip()
            if not title or title == "aiOS":
                return True
            bounds = self._window_recording_bounds(hwnd)
            if not bounds or bounds["width"] < 120 or bounds["height"] < 80:
                return True
            windows.append({"hwnd": hwnd, "title": title, "bounds": bounds})
            return True

        user32.EnumWindows(enum_proc(callback), 0)
        windows.sort(key=lambda item: item["title"].casefold())
        used = {}
        for item in windows[:80]:
            title = item["title"]
            compact = title if len(title) <= 44 else title[:41] + "..."
            count = used.get(compact, 0) + 1
            used[compact] = count
            item["label"] = compact if count == 1 else f"{compact} ({count})"
        return windows[:80]

    def _window_recording_bounds(self, hwnd):
        if not sys.platform.startswith("win"):
            return None

        rect = wintypes.RECT()
        got_rect = False
        try:
            got_rect = ctypes.windll.dwmapi.DwmGetWindowAttribute(
                wintypes.HWND(hwnd),
                9,
                ctypes.byref(rect),
                ctypes.sizeof(rect),
            ) == 0
        except (AttributeError, OSError):
            got_rect = False
        if not got_rect:
            if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return None

        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width <= 0 or height <= 0:
            return None
        return {"left": int(rect.left), "top": int(rect.top), "width": width, "height": height}

    def _set_quick_tools_status(self, text, ok):
        color = self.c("success") if ok else self.c("danger")
        if self.quick_tools_status is not None:
            try:
                self.quick_tools_status.configure(text=text, fg=color)
            except tk.TclError:
                pass
        if ok and self._bottom_tray is not None:
            self._tray_show()
            if self._tray_hide_job is not None:
                self.root.after_cancel(self._tray_hide_job)
            self._tray_hide_job = self.root.after(2200, self._tray_close)
        try:
            self.subtitle.configure(text=text, fg=color)
            self.root.after(2600, lambda: self.subtitle.configure(text=self._status_subtitle(), fg=self.c("muted")))
        except tk.TclError:
            pass

    def _toggle_create_menu(self):
        if self._create_menu_popup:
            self._close_create_menu()
            return
        self._show_create_menu()

    def _show_create_menu(self):
        self._close_create_menu()
        popup = tk.Toplevel(self.root)
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.configure(bg=self.c("panel"))
        x = self.root.winfo_pointerx() + 4
        y = self.root.winfo_pointery() + 4
        popup.geometry(f"+{x}+{y}")
        self._create_menu_popup = popup
        inner = tk.Frame(popup, bg=self.c("panel"), highlightthickness=1, highlightbackground=self._header_border_color())
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        self.header_chip(inner, "New To-Do", self.open_create_todo, hint="Project with a deadline").pack(
            fill="x", padx=6, pady=(6, 3)
        )
        self.header_chip(inner, "New Project", self.open_create_project, hint="Project without dashboard pin").pack(
            fill="x", padx=6, pady=(3, 3)
        )
        self.header_chip(inner, "Add Folder", self.open_add_folder, hint="Link an existing folder").pack(
            fill="x", padx=6, pady=(3, 6)
        )
        popup.bind("<FocusOut>", lambda _e: self._close_create_menu())
        popup.bind("<Escape>", lambda _e: self._close_create_menu())

    def _close_create_menu(self):
        popup = getattr(self, "_create_menu_popup", None)
        if popup is not None:
            try:
                popup.destroy()
            except tk.TclError:
                pass
        self._create_menu_popup = None

    # ---------- Project / To-Do helpers ----------

    def project_display_title(self, project_path, meta=None):
        meta = meta or self.load_project_meta(project_path)
        return str(meta.get("title") or Path(project_path).name).strip()

    def is_dashboard_todo(self, meta):
        return bool(meta.get("tracked_as_todo")) and not bool(meta.get("done"))

    def dashboard_todos(self):
        rows = []
        for project_path in self.projects():
            if not Path(project_path).is_dir():
                continue
            meta = self.load_project_meta(project_path)
            if self.is_dashboard_todo(meta):
                rows.append((project_path, meta))
        return sorted(rows, key=self.project_due_sort_key)

    def project_due_sort_key(self, row):
        _path, meta = row
        due = str(meta.get("due", "")).strip()
        parsed = parse_due_datetime(due)
        if parsed:
            return (0, parsed.isoformat(), self.project_display_title(_path, meta).casefold())
        return (1, "9999-99-99", self.project_display_title(_path, meta).casefold())

    def _parse_due(self, due):
        parsed = parse_due_datetime(due)
        return parsed.date() if parsed else None

    def get_due_value(self):
        date = self.todo_due_var.get().strip() if self.todo_due_var else ""
        if not date:
            return ""
        if "T" in date or " " in date:
            parsed = parse_due_datetime(date)
            return format_due_datetime(parsed) if parsed else date
        time_part = "18:00"
        if hasattr(self, "todo_due_time_entry"):
            time_part = self.todo_due_time_entry.get("1.0", "end").strip() or "18:00"
        elif hasattr(self, "todo_due_time_var") and self.todo_due_time_var is not None:
            time_part = self.todo_due_time_var.get().strip() or "18:00"
        if re.fullmatch(r"\d{1,2}:\d{2}", time_part):
            hour, minute = time_part.split(":", 1)
            time_part = f"{int(hour):02d}:{int(minute):02d}"
        else:
            time_part = "18:00"
        return f"{date}T{time_part}"

    def due_countdown_short(self, due):
        text = self.due_countdown(due)
        if text == "due today":
            return "Today"
        if text == "due tomorrow":
            return "Tomorrow"
        if "days left" in text:
            return text.replace(" days left", "d")
        if "overdue" in text:
            return text.replace(" days overdue", "d over").replace("1 day overdue", "1d over")
        return text or "No date"

    def dashboard_todo_row(self, parent, project_path, meta):
        project_path = Path(project_path)
        row = tk.Frame(parent, bg=self.c("surface2"), highlightthickness=0, bd=0)
        priority = str(meta.get("priority", "normal"))
        priority_color = {
            "low": self.c("muted"),
            "normal": self.c("accent"),
            "high": self.c("danger"),
        }.get(priority, self.c("accent"))
        tk.Frame(row, bg=priority_color, width=4).pack(side="left", fill="y")
        done = tk.BooleanVar(value=bool(meta.get("done")))
        tk.Checkbutton(
            row,
            variable=done,
            command=lambda p=project_path, var=done: self.toggle_project_done(p, var.get()),
            bg=self.c("surface2"),
            activebackground=self.c("surface2"),
            selectcolor=self.c("panel2"),
            fg=self.c("text"),
            bd=0,
            highlightthickness=0,
        ).pack(side="left", padx=(8, 4), pady=8)
        text = tk.Frame(row, bg=self.c("surface2"))
        text.pack(side="left", fill="x", expand=True, pady=7)
        title = self.project_display_title(project_path, meta)
        tk.Button(
            text,
            text=title,
            command=lambda p=project_path: self.open_project_detail(p),
            bg=self.c("surface2"),
            fg=self.c("text"),
            activebackground=self.c("surface2"),
            activeforeground=self.c("accent"),
            relief="flat",
            bd=0,
            anchor="w",
            cursor="hand2",
            font=self.font(10, "bold"),
        ).pack(fill="x")
        self.muted(text, f"Folder: {project_path.name}").pack(fill="x")
        due = str(meta.get("due", "")).strip()
        today = datetime.now().date()
        due_date = self._parse_due(due)
        badge_bg = self.c("surface")
        badge_fg = self.c("muted")
        if due_date:
            if due_date < today:
                badge_bg = self.blend_color(self.c("surface2"), self.c("danger"), 0.35)
                badge_fg = self.c("danger")
            elif due_date == today:
                badge_bg = self.blend_color(self.c("surface2"), self.c("accent"), 0.28)
                badge_fg = self.c("accent")
            else:
                badge_fg = self.c("text")
        badge = tk.Label(
            row,
            text=self.due_countdown_short(due) if due else "Set date",
            bg=badge_bg,
            fg=badge_fg,
            font=self.font(11, "bold"),
            padx=12,
            pady=8,
        )
        badge.pack(side="right", padx=10, pady=6)
        return row

    def recent_project_row(self, parent, project_path):
        project_path = Path(project_path)
        meta = self.load_project_meta(project_path)
        row = tk.Frame(parent, bg=self.c("surface2"))
        left = tk.Frame(row, bg=self.c("surface2"))
        left.pack(side="left", fill="x", expand=True, padx=10, pady=8)
        tk.Button(
            left,
            text=project_path.name,
            command=lambda p=project_path: self.open_project_detail(p),
            bg=self.c("surface2"),
            fg=self.c("text"),
            activebackground=self.c("surface2"),
            activeforeground=self.c("accent"),
            relief="flat",
            bd=0,
            anchor="w",
            cursor="hand2",
            font=self.font(9, "bold"),
        ).pack(anchor="w")
        due = str(meta.get("due", "")).strip()
        hint = self.due_countdown(due) if due else str(meta.get("status", "active"))
        self.muted(left, hint).pack(anchor="w")
        actions = tk.Frame(row, bg=self.c("surface2"))
        actions.pack(side="right", padx=8, pady=6)
        if not self.is_dashboard_todo(meta):
            self.button(actions, "Pin", lambda p=project_path: self.pin_project_todo(p), compact=True).pack(side="left", padx=2)
        self.button(actions, "Open", lambda p=project_path: self.open_project_detail(p), compact=True).pack(side="left", padx=2)
        return row

    def toggle_project_done(self, project_path, done):
        meta = self.load_project_meta(project_path)
        meta["done"] = bool(done)
        if done:
            meta["tracked_as_todo"] = False
            meta["status"] = "done"
        else:
            meta["tracked_as_todo"] = True
            if meta.get("status") == "done":
                meta["status"] = "active"
        self.save_project_meta(project_path, meta)
        self.sync_project_todos_memory(Path(project_path).name)
        self._refresh_after_todo_change()

    def snooze_project(self, project_path, days):
        meta = self.load_project_meta(project_path)
        base = parse_due_datetime(meta.get("due")) or datetime.now()
        meta["due"] = format_due_datetime(base + timedelta(days=int(days)))
        meta["tracked_as_todo"] = True
        meta["done"] = False
        self.save_project_meta(project_path, meta)
        self.sync_project_todos_memory(Path(project_path).name)
        self._refresh_after_todo_change()

    def pin_project_todo(self, project_path, due=None):
        meta = self.load_project_meta(project_path)
        if due:
            meta["due"] = due
        meta["tracked_as_todo"] = True
        meta["done"] = False
        meta["status"] = "active"
        self.save_project_meta(project_path, meta)
        self.sync_project_todos_memory(Path(project_path).name)
        self.render_tab("Dashboard")

    def create_todo_project(self, title, due, priority="normal", notes=""):
        project_path = self.allocate_project_path(title)
        self.ensure_project_scaffold(project_path)
        name = project_path.name
        meta = self.load_project_meta(project_path)
        meta["title"] = title.strip() or name
        meta["due"] = due.strip()
        meta["priority"] = priority
        meta["notes"] = notes.strip()
        meta["tracked_as_todo"] = True
        meta["done"] = False
        meta["status"] = "active"
        meta["created"] = datetime.now().isoformat(timespec="seconds")
        self.save_project_meta(project_path, meta)
        return project_path

    def create_plain_project(self, title, due="", priority="normal", notes=""):
        project_path = self.allocate_project_path(title)
        self.ensure_project_scaffold(project_path)
        name = project_path.name
        meta = self.load_project_meta(project_path)
        meta["title"] = title.strip() or name
        meta["due"] = due.strip()
        meta["priority"] = priority
        meta["notes"] = notes.strip()
        meta["tracked_as_todo"] = bool(due.strip())
        meta["done"] = False
        meta["status"] = "active"
        meta["created"] = datetime.now().isoformat(timespec="seconds")
        self.save_project_meta(project_path, meta)
        if meta["tracked_as_todo"]:
            self.sync_project_todos_memory(name)
        return project_path

    def render_create_todo(self):
        self._detail_header("New To-Do", "A project with a deadline — shows on your dashboard")
        card = self.card(self.page)
        card.pack(fill="x", pady=(0, 10))
        form = tk.Frame(card, bg=self.c("surface"))
        form.pack(fill="x", padx=12, pady=12)
        tk.Label(form, text="Name", bg=self.c("surface"), fg=self.c("muted"), font=self.font(9, "bold")).pack(anchor="w")
        self.create_title_entry = self.single_line(form, "")
        self.create_title_entry.pack(fill="x", pady=(4, 10))
        self.create_title_entry.bind("<Return>", lambda _e: self.submit_create_todo())
        tk.Label(form, text="Due date", bg=self.c("surface"), fg=self.c("muted"), font=self.font(9, "bold")).pack(anchor="w")
        self._build_form_due_picker(form, required=False)
        tk.Label(form, text="Priority", bg=self.c("surface"), fg=self.c("muted"), font=self.font(9, "bold")).pack(anchor="w", pady=(8, 0))
        pr = tk.Frame(form, bg=self.c("surface"))
        pr.pack(fill="x", pady=(4, 8))
        self.create_priority_var = tk.StringVar(value="normal")
        self._priority_buttons = []
        for label, val, color in (
            ("low", "low", self.c("muted")),
            ("normal", "normal", self.c("accent")),
            ("high", "high", self.c("danger")),
        ):
            btn = self._priority_pill(pr, label, val, color)
            btn.pack(side="left", padx=2)
            self._priority_buttons.append((btn, val, color))
        self._refresh_priority_buttons()
        actions = tk.Frame(self.page, bg=self.c("panel"))
        actions.pack(fill="x", pady=(8, 0))
        self.button(actions, "Create To-Do", self.submit_create_todo, compact=True).pack(side="left")
        self.button(actions, "Cancel", self.close_detail, compact=True).pack(side="left", padx=(8, 0))

    def render_create_project(self):
        self._detail_header("New Project", "A workspace folder — pin to dashboard anytime")
        card = self.card(self.page)
        card.pack(fill="x", pady=(0, 10))
        form = tk.Frame(card, bg=self.c("surface"))
        form.pack(fill="x", padx=12, pady=12)
        tk.Label(form, text="Name", bg=self.c("surface"), fg=self.c("muted"), font=self.font(9, "bold")).pack(anchor="w")
        self.create_title_entry = self.single_line(form, "")
        self.create_title_entry.pack(fill="x", pady=(4, 10))
        self.create_title_entry.bind("<Return>", lambda _e: self.submit_create_project())
        tk.Label(form, text="Optional deadline", bg=self.c("surface"), fg=self.c("muted"), font=self.font(9, "bold")).pack(anchor="w")
        self._build_form_due_picker(form, required=False)
        actions = tk.Frame(self.page, bg=self.c("panel"))
        actions.pack(fill="x", pady=(8, 0))
        self.button(actions, "Create Project", self.submit_create_project, compact=True).pack(side="left")
        self.button(actions, "Cancel", self.close_detail, compact=True).pack(side="left", padx=(8, 0))

    def _build_form_due_picker(self, parent, *, required=False, default_iso=""):
        row = tk.Frame(parent, bg=self.c("surface"))
        row.pack(fill="x", pady=(4, 0))
        self._due_required = required
        date_part, time_part = split_due_value(default_iso)
        self.todo_due_var = tk.StringVar(value=date_part)
        self.todo_due_time_var = tk.StringVar(value=time_part)
        self._date_buttons = []
        self.todo_due_label = tk.Label(
            row,
            text="",
            bg=self.c("surface"),
            fg=self.c("muted"),
            font=self.font(9, "bold"),
            anchor="w",
        )
        self.todo_due_label.pack(side="left", fill="x", expand=True)
        self.button(row, "Pick date", self._open_calendar_popup, compact=True).pack(side="right", padx=(8, 0))
        if not required:
            self.button(row, "Clear", self._clear_due_date, compact=True).pack(side="right", padx=(4, 0))

        time_row = tk.Frame(parent, bg=self.c("surface"))
        time_row.pack(fill="x", pady=(6, 0))
        tk.Label(time_row, text="Time", bg=self.c("surface"), fg=self.c("muted"), font=self.font(9, "bold")).pack(
            side="left"
        )
        self.todo_due_time_entry = self.single_line(time_row, time_part)
        self.todo_due_time_entry.configure(width=8)
        self.todo_due_time_entry.pack(side="left", padx=(8, 0))
        self.muted(time_row, "HH:MM").pack(side="left", padx=(8, 0))
        self.todo_due_time_entry.bind("<KeyRelease>", lambda _e: self._on_due_time_typed())
        self._refresh_due_buttons()

    def _on_due_time_typed(self):
        if hasattr(self, "todo_due_time_entry"):
            value = self.todo_due_time_entry.get("1.0", "end").strip()
            if hasattr(self, "todo_due_time_var") and self.todo_due_time_var is not None:
                self.todo_due_time_var.set(value)
        self._refresh_due_buttons()
        self._schedule_project_autosave(getattr(self, "_editing_project_path", None))

    def _clear_due_date(self):
        if self.todo_due_var is not None:
            self.todo_due_var.set("")
        if hasattr(self, "todo_due_time_entry"):
            self.todo_due_time_entry.delete("1.0", "end")
        if hasattr(self, "todo_due_time_var") and self.todo_due_time_var is not None:
            self.todo_due_time_var.set("")
        self._refresh_due_buttons()
        path = getattr(self, "_editing_project_path", None)
        if path:
            meta = self.load_project_meta(path)
            meta["due"] = ""
            self.save_project_meta(path, meta)
        self._schedule_project_autosave(path)

    def submit_create_todo(self):
        title = self.create_title_entry.get("1.0", "end").strip()
        due = self.get_due_value()
        priority = self.todo_priority_var.get() if self.todo_priority_var else "normal"
        if not title:
            self.create_title_entry.focus_set()
            return
        if not due:
            pass
        project_path = self.create_todo_project(title, due, priority)
        self.page_view = None
        self.open_project_detail(project_path)

    def submit_create_project(self):
        title = self.create_title_entry.get("1.0", "end").strip()
        due = self.get_due_value()
        if not title:
            self.create_title_entry.focus_set()
            return
        project_path = self.create_plain_project(title, due=due)
        self.page_view = None
        self.open_project_detail(project_path)

    def _priority_pill(self, parent, label, value, color):
        return tk.Button(
            parent,
            text=label,
            command=lambda v=value: self._set_priority(v),
            bg=self.c("panel2"),
            fg=color,
            activebackground=color,
            activeforeground="#061018",
            relief="flat",
            bd=0,
            padx=10,
            pady=5,
            cursor="hand2",
            font=self.font(8, "bold"),
        )

    def _set_priority(self, value):
        if self.todo_priority_var is None:
            return
        self.todo_priority_var.set(value)
        self._refresh_priority_buttons()

    def _refresh_priority_buttons(self):
        if not self._priority_buttons or self.todo_priority_var is None:
            return
        current = self.todo_priority_var.get()
        for btn, val, color in self._priority_buttons:
            try:
                if val == current:
                    btn.configure(bg=color, fg="#061018")
                else:
                    btn.configure(bg=self.c("panel2"), fg=color)
            except tk.TclError:
                pass

    def _date_chip(self, parent, label, offset):
        return tk.Button(
            parent,
            text=label,
            command=lambda o=offset: self._set_due_offset(o),
            bg=self.c("panel2"),
            fg=self.c("text"),
            activebackground=self.c("accent"),
            activeforeground="#061018",
            relief="flat",
            bd=0,
            padx=10,
            pady=5,
            cursor="hand2",
            font=self.font(8, "bold"),
        )

    def _set_due_offset(self, offset):
        if offset is None:
            self._apply_due("")
            return
        date = datetime.now().date() + timedelta(days=int(offset))
        self._apply_due(date.isoformat())

    def _apply_due(self, value):
        if self.todo_due_var is not None:
            self.todo_due_var.set(value)
        self._refresh_due_buttons()

    def _refresh_due_buttons(self):
        if self.todo_due_label is None or self.todo_due_var is None:
            return
        value = self.todo_due_var.get()
        if value:
            try:
                combined = self.get_due_value() if self.todo_due_var else value
                parsed = parse_due_datetime(combined or value)
                if parsed:
                    pretty = parsed.strftime("%a %b %d, %Y · %H:%M")
                    pretty += "  ·  " + self.due_countdown(combined or value)
                    self.todo_due_label.configure(text=pretty, fg=self.c("accent"))
                else:
                    self.todo_due_label.configure(text=value, fg=self.c("muted"))
            except ValueError:
                self.todo_due_label.configure(text=value, fg=self.c("muted"))
        else:
            empty = "Pick a date" if getattr(self, "_due_required", False) else "No deadline"
            self.todo_due_label.configure(text=empty, fg=self.c("muted"))
        if not self._date_buttons:
            return
        today = datetime.now().date()
        for btn, offset in self._date_buttons:
            try:
                if offset is None and not value:
                    btn.configure(bg=self.c("accent"), fg="#061018")
                elif offset is not None and value == (today + timedelta(days=offset)).isoformat():
                    btn.configure(bg=self.c("accent"), fg="#061018")
                else:
                    btn.configure(bg=self.c("panel2"), fg=self.c("text"))
            except tk.TclError:
                pass

    def _open_calendar_popup(self):
        self._close_calendar_popup()
        popup = tk.Toplevel(self.root)
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.configure(bg=self.c("panel"))
        x = self.root.winfo_pointerx() + 8
        y = self.root.winfo_pointery() + 8
        popup.geometry(f"260x240+{x}+{y}")
        self._cal_popup = popup
        existing = self.todo_due_var.get() if self.todo_due_var else ""
        try:
            self._cal_month = datetime.strptime(existing, "%Y-%m-%d").date().replace(day=1)
        except ValueError:
            self._cal_month = datetime.now().date().replace(day=1)
        self._cal_body = tk.Frame(popup, bg=self.c("panel"))
        self._cal_body.pack(fill="both", expand=True, padx=8, pady=8)
        self._render_calendar()
        popup.bind("<FocusOut>", lambda _e: self._close_calendar_popup())
        popup.bind("<Escape>", lambda _e: self._close_calendar_popup())
        popup.focus_set()

    def _close_calendar_popup(self):
        popup = getattr(self, "_cal_popup", None)
        if popup is not None:
            try:
                popup.destroy()
            except tk.TclError:
                pass
        self._cal_popup = None

    def _render_calendar(self):
        if not getattr(self, "_cal_body", None):
            return
        for child in self._cal_body.winfo_children():
            child.destroy()
        month_date = self._cal_month
        header = tk.Frame(self._cal_body, bg=self.c("panel"))
        header.pack(fill="x", pady=(0, 6))
        self.button(header, "<", lambda: self._shift_calendar(-1), compact=True).pack(side="left")
        tk.Label(
            header,
            text=month_date.strftime("%B %Y"),
            bg=self.c("panel"),
            fg=self.c("text"),
            font=self.font(10, "bold"),
        ).pack(side="left", expand=True)
        self.button(header, ">", lambda: self._shift_calendar(1), compact=True).pack(side="right")

        grid = tk.Frame(self._cal_body, bg=self.c("panel"))
        grid.pack(fill="both", expand=True)
        for col, name in enumerate(("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")):
            tk.Label(
                grid,
                text=name,
                bg=self.c("panel"),
                fg=self.c("muted"),
                font=self.font(8, "bold"),
                width=3,
            ).grid(row=0, column=col, padx=1, pady=1)
        import calendar as _cal
        first_weekday = month_date.weekday()  # Monday=0
        days_in_month = _cal.monthrange(month_date.year, month_date.month)[1]
        today = datetime.now().date()
        selected = ""
        if self.todo_due_var:
            selected = self.todo_due_var.get()
        row = 1
        col = first_weekday
        for day in range(1, days_in_month + 1):
            date = month_date.replace(day=day)
            iso = date.isoformat()
            is_today = date == today
            is_selected = iso == selected
            bg = self.c("accent") if is_selected else (self.c("surface2") if is_today else self.c("panel2"))
            fg = "#061018" if is_selected else self.c("text")
            btn = tk.Button(
                grid,
                text=str(day),
                command=lambda d=iso: self._pick_calendar_date(d),
                bg=bg,
                fg=fg,
                activebackground=self.c("accent"),
                activeforeground="#061018",
                relief="flat",
                bd=0,
                width=3,
                pady=2,
                cursor="hand2",
                font=self.font(9),
            )
            btn.grid(row=row, column=col, padx=1, pady=1)
            col += 1
            if col > 6:
                col = 0
                row += 1

    def _shift_calendar(self, direction):
        month = self._cal_month
        if direction > 0:
            if month.month == 12:
                self._cal_month = month.replace(year=month.year + 1, month=1)
            else:
                self._cal_month = month.replace(month=month.month + 1)
        else:
            if month.month == 1:
                self._cal_month = month.replace(year=month.year - 1, month=12)
            else:
                self._cal_month = month.replace(month=month.month - 1)
        self._render_calendar()

    def _pick_calendar_date(self, iso):
        self._apply_due(iso)
        self._close_calendar_popup()
        self._schedule_project_autosave(getattr(self, "_editing_project_path", None))

    def _set_todo_filter(self, key):
        self.todo_filter = key
        self.render_tab("Dashboard")

    def _filter_todos(self, all_todos, today):
        active = [t for t in all_todos if not t.get("done")]
        if self.todo_filter == "done":
            return [t for t in all_todos if t.get("done")]
        if self.todo_filter == "today":
            return [t for t in active if self._todo_due_date(t) == today]
        if self.todo_filter == "overdue":
            return [t for t in active if (self._todo_due_date(t) or today + timedelta(days=999)) < today]
        if self.todo_filter == "week":
            return [
                t for t in active
                if self._todo_due_date(t) and 0 <= (self._todo_due_date(t) - today).days <= 7
            ]
        return active

    def _todo_due_date(self, item):
        try:
            return datetime.strptime(str(item.get("due", "")).strip(), "%Y-%m-%d").date()
        except ValueError:
            return None

    def _clear_done_todos(self):
        self.config["todos"] = [t for t in self.config.get("todos", []) if not t.get("done")]
        save_config(self.config)
        self.render_tab("Dashboard")

    def todo_panel(self, parent, limit=None, group_by_project=False, title="TODO"):
        panel = self.card(parent)
        self.section(panel, title)
        form = tk.Frame(panel, bg=self.c("surface"))
        form.pack(fill="x", padx=12, pady=(0, 10))
        self.todo_title_entry = self.single_line(form, "")
        self.todo_title_entry.pack(side="left", fill="x", expand=True)
        self.todo_title_entry.bind("<Return>", self._todo_return)
        self.todo_title_entry.bind("<KP_Enter>", self._todo_return)
        project_names = ["Inbox"] + [project.name for project in self.projects()[:20]]
        default_project = project_names[1] if len(project_names) > 1 else "Inbox"
        self.todo_project_var = tk.StringVar(value=default_project)
        project_menu = tk.OptionMenu(form, self.todo_project_var, *project_names)
        self.style_option(project_menu)
        project_menu.pack(side="left", padx=(8, 0))
        self.todo_due_entry = self.single_line(form, "")
        self.todo_due_entry.configure(width=12)
        self.todo_due_entry.pack(side="left", padx=(8, 0))
        self.todo_due_entry.bind("<Return>", self._todo_return)
        self.todo_due_entry.bind("<KP_Enter>", self._todo_return)
        self.muted(form, "YYYY-MM-DD").pack(side="left", padx=(6, 0))
        self.button(form, "Add", self.add_todo, compact=True).pack(side="left", padx=(8, 0))

        items = [item for item in self.config.get("todos", []) if not item.get("done")]
        if not items:
            self.muted(panel, "No active tasks. Type a title and press Enter.").pack(
                anchor="w", padx=14, pady=(0, 12)
            )
            return panel

        if group_by_project:
            buckets = {}
            for item in sorted(items, key=self.todo_sort_key):
                key = str(item.get("project") or "Inbox")
                buckets.setdefault(key, []).append(item)
            project_order = [p.name for p in self.projects()]
            ordered_keys = [k for k in project_order if k in buckets] + [
                k for k in buckets if k not in project_order
            ]
            for key in ordered_keys:
                header = tk.Frame(panel, bg=self.c("surface"))
                header.pack(fill="x", padx=12, pady=(4, 2))
                tk.Label(
                    header,
                    text=key,
                    bg=self.c("surface"),
                    fg=self.c("accent"),
                    font=self.font(10, "bold"),
                    anchor="w",
                ).pack(side="left")
                tk.Label(
                    header,
                    text=f"{len(buckets[key])} task(s)",
                    bg=self.c("surface"),
                    fg=self.c("muted"),
                    font=self.font(8),
                ).pack(side="right")
                for item in buckets[key]:
                    self.todo_row(panel, item).pack(fill="x", padx=12, pady=(0, 6))
            return panel

        visible = sorted(items, key=self.todo_sort_key)
        if limit:
            visible = visible[:limit]
        for item in visible:
            self.todo_row(panel, item).pack(fill="x", padx=12, pady=(0, 8))
        return panel

    def _todo_return(self, _event):
        self.add_todo()
        return "break"

    def todo_row(self, parent, item):
        row = tk.Frame(parent, bg=self.c("surface2"), highlightthickness=0, bd=0)
        priority = str(item.get("priority", "normal"))
        priority_color = {
            "low": self.c("muted"),
            "normal": self.c("accent"),
            "high": self.c("danger"),
        }.get(priority, self.c("accent"))
        dot = tk.Frame(row, bg=priority_color, width=4)
        dot.pack(side="left", fill="y")
        project_name = str(item.get("project") or "")
        project_path = self._resolve_project(project_name) if project_name else None
        done = tk.BooleanVar(value=bool(item.get("done")))
        check = tk.Checkbutton(
            row,
            variable=done,
            command=lambda p=project_path, var=done: self.toggle_project_done(p, var.get()) if p else None,
            bg=self.c("surface2"),
            activebackground=self.c("surface2"),
            selectcolor=self.c("panel2"),
            fg=self.c("text"),
            bd=0,
            highlightthickness=0,
        )
        check.pack(side="left", padx=(8, 4), pady=8)
        text = tk.Frame(row, bg=self.c("surface2"))
        text.pack(side="left", fill="x", expand=True, pady=7)
        title_text = str(item.get("title", ""))
        title_color = self.c("muted") if item.get("done") else self.c("text")
        title_btn = tk.Button(
            text,
            text=title_text,
            command=lambda p=project_path: self.open_project_detail(p) if p else None,
            bg=self.c("surface2"),
            fg=title_color,
            activebackground=self.c("surface2"),
            activeforeground=self.c("accent"),
            relief="flat",
            bd=0,
            anchor="w",
            cursor="hand2",
            font=self.font(10, "bold"),
        )
        title_btn.pack(fill="x")
        meta = self.todo_meta(item)
        due_date = self._todo_due_date(item)
        today = datetime.now().date()
        meta_color = self.c("muted")
        if due_date and not item.get("done"):
            if due_date < today:
                meta_color = self.c("danger")
            elif due_date == today:
                meta_color = self.c("accent")
        tk.Label(
            text,
            text=meta,
            bg=self.c("surface2"),
            fg=meta_color,
            font=self.font(8),
            anchor="w",
        ).pack(fill="x")
        actions = tk.Frame(row, bg=self.c("surface2"))
        actions.pack(side="right", padx=6, pady=6)
        self.button(
            actions,
            "Open",
            lambda p=project_path: self.open_project_detail(p) if p else None,
            compact=True,
        ).pack(side="left", padx=2)
        return row

    def _snooze_todo(self, todo_id, days):
        for item in self.config.get("todos", []):
            if item.get("id") == todo_id:
                base = self._todo_due_date(item) or datetime.now().date()
                item["due"] = (base + timedelta(days=int(days))).isoformat()
                save_config(self.config)
                self.sync_project_todos_memory(item.get("project"))
                break
        self._refresh_after_todo_change()

    def todo_sort_key(self, item):
        due = str(item.get("due", "")).strip()
        try:
            parsed = datetime.strptime(due, "%Y-%m-%d").date()
            return (0, parsed.isoformat(), str(item.get("title", "")).casefold())
        except ValueError:
            return (1, "9999-99-99", str(item.get("title", "")).casefold())

    def todo_meta(self, item):
        pieces = []
        project = str(item.get("project", "Inbox") or "Inbox")
        pieces.append(project)
        due = str(item.get("due", "")).strip()
        if due:
            pieces.append(self.due_countdown(due))
        return "  |  ".join(pieces)

    def due_countdown(self, due):
        parsed = parse_due_datetime(due)
        if not parsed:
            return str(due or "")
        seconds = (parsed - datetime.now()).total_seconds()
        if seconds <= 0:
            past = abs(seconds)
            if past < 3600:
                return f"{max(1, int(past // 60))}m overdue"
            if past < 86400:
                return f"{int(past // 3600)}h {int((past % 3600) // 60)}m overdue"
            return f"{int(past // 86400)}d overdue"
        if seconds < 3600:
            return f"{max(1, int(seconds // 60))}m left"
        if seconds < 86400:
            return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m left"
        days = int(seconds // 86400)
        hours = int((seconds % 86400) // 3600)
        if days == 1 and hours == 0:
            return "due tomorrow"
        if days == 1:
            return f"1d {hours}h left"
        return f"{days}d {hours}h left"

    def add_todo(self):
        self.open_create_todo()

    def _refresh_after_todo_change(self):
        if self.page_view and self.page_view[0] == "create":
            self.close_detail()
        elif self.page_view:
            self._render_detail()
        else:
            self.render_tab(self.active_tab)

    def toggle_todo(self, todo_id, done):
        project_path = self._resolve_project(str(todo_id))
        if project_path:
            self.toggle_project_done(project_path, done)

    def delete_todo(self, todo_id):
        project_path = self._resolve_project(str(todo_id))
        if project_path:
            self.toggle_project_done(project_path, True)

    def sync_project_todos_memory(self, project_name):
        project_name = str(project_name or "").strip()
        if not project_name:
            return
        project_path = self._resolve_project(project_name) or (self.project_root / clean_project_name(project_name))
        if not project_path.exists():
            return
        memory = self.ensure_project_memory(project_path)
        try:
            text = memory.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        meta = self.load_project_meta(project_path)
        lines = ["<!-- AIOS_TODOS_START -->", "## Dashboard To-Dos"]
        if self.is_dashboard_todo(meta):
            due = str(meta.get("due", "")).strip()
            suffix = f" ({self.due_countdown(due)})" if due else ""
            lines.append(f"- {self.project_display_title(project_path, meta)}{suffix}")
        else:
            lines.append("- Not pinned to dashboard.")
        lines.append("<!-- AIOS_TODOS_END -->")
        block = "\n".join(lines)
        pattern = r"<!-- AIOS_TODOS_START -->.*?<!-- AIOS_TODOS_END -->"
        if re.search(pattern, text, flags=re.DOTALL):
            text = re.sub(pattern, block, text, flags=re.DOTALL)
        else:
            text = text.rstrip() + "\n\n" + block + "\n"
        try:
            memory.write_text(text, encoding="utf-8")
        except OSError:
            pass

    def render_projects(self):
        head = tk.Frame(self.page, bg=self.c("panel"))
        head.pack(fill="x", pady=(0, 12))
        tk.Label(head, text="Projects", bg=self.c("panel"), fg=self.c("text"), font=self.font(18, "bold")).pack(
            side="left"
        )
        self.header_btn(head, "+", self._toggle_create_menu, hint="New To-Do or Project").pack(side="right")

        scroll = ScrollFrame(self.page, self.c("panel"))
        scroll.pack(fill="both", expand=True)
        projects = self.projects()
        if not projects:
            self.muted(scroll.inner, "No projects found.").pack(anchor="w", padx=6)
            return
        for project in projects:
            self.project_row(scroll.inner, project).pack(fill="x", pady=(0, 8))

    def _detail_header(self, title, subtitle="", *, back_command=None, right_buttons=None):
        head = tk.Frame(self.page, bg=self.c("panel"))
        head.pack(fill="x", pady=(0, 10))
        self.button(head, "Back", back_command or self.close_detail, compact=True).pack(side="left")
        title_frame = tk.Frame(head, bg=self.c("panel"))
        title_frame.pack(side="left", fill="x", expand=True, padx=(12, 0))
        tk.Label(
            title_frame,
            text=title,
            bg=self.c("panel"),
            fg=self.c("text"),
            font=self.font(18, "bold"),
            anchor="w",
        ).pack(fill="x")
        if subtitle:
            self.muted(title_frame, subtitle).pack(fill="x")
        if right_buttons:
            actions = tk.Frame(head, bg=self.c("panel"))
            actions.pack(side="right")
            for label, command in right_buttons:
                self.header_chip(actions, label, command).pack(side="right", padx=(4, 0))

    def _schedule_project_autosave(self, project_path, delay=650):
        if not project_path:
            return
        self._autosave_project_path = Path(project_path)
        if self._autosave_after:
            try:
                self.root.after_cancel(self._autosave_after)
            except tk.TclError:
                pass
        self._autosave_after = self.root.after(delay, self._run_project_autosave)

    def _run_project_autosave(self):
        self._autosave_after = None
        path = self._autosave_project_path
        if not path or not self.page_view:
            return
        kind = self.page_view[0]
        if kind == "project":
            self._autosave_project_main(path)
        elif kind == "project_settings":
            self._autosave_project_settings(path)

    def _bind_autosave_text(self, widget, project_path, field):
        def trigger(_event=None):
            self._schedule_project_autosave(project_path)

        widget.bind("<KeyRelease>", trigger, add="+")
        widget.bind("<FocusOut>", trigger, add="+")

    def _bind_autosave_var(self, var, project_path):
        var.trace_add("write", lambda *_args: self._schedule_project_autosave(project_path))

    def _autosave_project_main(self, project_path):
        if not hasattr(self, "detail_summary_box"):
            return
        text = self.detail_summary_box.get("1.0", "end").strip()
        self.save_project_summary(project_path, text + ("\n" if text and not text.endswith("\n") else ""))

    def _autosave_project_settings(self, project_path):
        if not hasattr(self, "detail_title_entry"):
            return
        tags_raw = self.detail_tags_entry.get("1.0", "end").strip()
        tags = [part.strip() for part in tags_raw.split(",") if part.strip()]
        due = self.get_due_value()
        pinned = bool(self.detail_tracked_var.get()) if hasattr(self, "detail_tracked_var") else False
        meta = self.load_project_meta(project_path)
        meta.update(
            {
                "title": self.detail_title_entry.get("1.0", "end").strip(),
                "status": self.detail_status_var.get().strip() or "active",
                "priority": self.detail_priority_var.get().strip() or "normal",
                "due": due,
                "tags": tags,
                "notes": self.detail_notes.get("1.0", "end").strip(),
                "tracked_as_todo": pinned,
                "done": False if pinned else meta.get("done", False),
            }
        )
        if meta.get("status") == "done" and pinned:
            meta["status"] = "active"
        self.save_project_meta(project_path, meta)

    def open_project_item(self, full_path):
        try:
            os.startfile(str(full_path))
        except OSError as exc:
            self.local_reply(self._error_message(exc))

    def open_project_item_with(self, full_path, app_key):
        path = Path(full_path)
        if path.is_dir():
            try:
                os.startfile(str(path))
            except OSError as exc:
                self.local_reply(self._error_message(exc))
            return
        if app_key == "default":
            self.open_project_item(path)
            return
        if app_key == "notepad":
            run_detached(["notepad.exe", str(path)])
            return
        if app_key == "firefox":
            firefox = find_firefox()
            if not firefox:
                self.local_reply("Mozilla Firefox was not found on this machine.")
                return
            run_detached([firefox, str(path)])
            return
        self.open_project_item(path)

    def _close_open_with_popup(self):
        popup = getattr(self, "_open_with_popup", None)
        if popup is not None:
            try:
                popup.destroy()
            except tk.TclError:
                pass
        self._open_with_popup = None
        self._open_with_target = None
        bind_after = getattr(self, "_open_with_bind_after", None)
        if bind_after:
            try:
                self.root.after_cancel(bind_after)
            except tk.TclError:
                pass
        self._open_with_bind_after = None
        bind = getattr(self, "_open_with_outside_bind", None)
        if bind:
            try:
                self.root.unbind("<Button-1>", bind)
            except tk.TclError:
                pass
        self._open_with_outside_bind = None

    def _show_open_with_popup(self, anchor, full_path):
        self._close_open_with_popup()
        popup = tk.Toplevel(self.root)
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.configure(bg=self.c("panel"))
        x = anchor.winfo_rootx()
        y = anchor.winfo_rooty() + anchor.winfo_height() + 2
        popup.geometry(f"+{x}+{y}")
        self._open_with_popup = popup
        self._open_with_target = str(full_path)
        inner = tk.Frame(popup, bg=self.c("panel"), highlightthickness=1, highlightbackground=self._header_border_color())
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        for label, key in OPEN_WITH_APPS:
            self.header_chip(
                inner,
                label,
                lambda k=key, fp=full_path: (self.open_project_item_with(fp, k), self._close_open_with_popup()),
            ).pack(fill="x", padx=6, pady=3)
        popup.bind("<Escape>", lambda _e: self._close_open_with_popup())

        def outside_click(event):
            if not self._open_with_popup:
                return
            widget = event.widget
            current = widget
            while current is not None:
                if current == popup or current == anchor:
                    return
                current = getattr(current, "master", None)
            self._close_open_with_popup()

        def bind_outside():
            self._open_with_outside_bind = self.root.bind("<Button-1>", outside_click, add="+")

        self._open_with_bind_after = self.root.after(120, bind_outside)

    def _toggle_open_with_menu(self, anchor, full_path):
        if self._open_with_popup and self._open_with_target == str(full_path):
            self._close_open_with_popup()
        else:
            self._show_open_with_popup(anchor, full_path)

    def file_open_with_button(self, parent, full_path):
        holder = tk.Frame(parent, bg=parent.cget("bg"))
        btn = tk.Button(
            holder,
            text="Open with ▾",
            command=lambda: self._toggle_open_with_menu(btn, full_path),
            bg=self.c("surface2"),
            fg=self.c("muted"),
            activebackground=self.blend_color(self.c("surface2"), self.c("accent"), 0.18),
            activeforeground=self.c("text"),
            relief="flat",
            bd=0,
            padx=8,
            pady=4,
            cursor="hand2",
            font=self.font(8),
            highlightthickness=1,
            highlightbackground=self._header_border_color(),
        )
        btn.pack(side="left")
        return holder

    def _tree_storage_key(self, project_path, rel):
        return f"{Path(project_path).resolve()}|{rel}"

    def _toggle_tree_folder(self, project_path, rel):
        key = self._tree_storage_key(project_path, rel)
        if key in self._file_tree_expanded:
            self._file_tree_expanded.discard(key)
        else:
            self._file_tree_expanded.add(key)
        self.render_project_detail(project_path)

    def render_project_file_tree(self, parent, project_path, folder_path, rel="", depth=0):
        project_path = Path(project_path)
        folder_path = Path(folder_path)
        try:
            entries = sorted(
                folder_path.iterdir(),
                key=lambda p: (not p.is_dir(), p.name.casefold()),
            )
        except OSError:
            return
        pad = 12 + depth * 16
        for entry in entries:
            if entry.name.startswith(".") or entry.name in ("__pycache__", "node_modules", ".git"):
                continue
            child_rel = f"{rel}/{entry.name}" if rel else entry.name
            row = tk.Frame(parent, bg=self.c("surface2"))
            row.pack(fill="x", pady=(0, 2))
            indent = tk.Frame(row, bg=self.c("surface2"), width=pad)
            indent.pack(side="left")
            indent.pack_propagate(False)
            if entry.is_dir():
                expanded = self._tree_storage_key(project_path, child_rel) in self._file_tree_expanded
                arrow = "▼" if expanded else "▶"
                tk.Button(
                    row,
                    text=arrow,
                    command=lambda p=project_path, r=child_rel: self._toggle_tree_folder(p, r),
                    bg=self.c("surface2"),
                    fg=self.c("muted"),
                    activebackground=self.c("surface2"),
                    activeforeground=self.c("accent"),
                    relief="flat",
                    bd=0,
                    width=2,
                    cursor="hand2",
                    font=self.font(8, "bold"),
                ).pack(side="left")
                tk.Button(
                    row,
                    text=entry.name + "/",
                    command=lambda fp=entry: self.open_project_item(fp),
                    bg=self.c("surface2"),
                    fg=self.c("text"),
                    activebackground=self.c("surface2"),
                    activeforeground=self.c("accent"),
                    relief="flat",
                    bd=0,
                    anchor="w",
                    cursor="hand2",
                    font=self.font(9, "bold"),
                ).pack(side="left", fill="x", expand=True, padx=(2, 4), pady=4)
                if expanded:
                    child_holder = tk.Frame(parent, bg=self.c("surface"))
                    child_holder.pack(fill="x")
                    self.render_project_file_tree(child_holder, project_path, entry, child_rel, depth + 1)
            else:
                tk.Label(row, text=" ", bg=self.c("surface2"), width=2).pack(side="left")
                tk.Button(
                    row,
                    text=entry.name,
                    command=lambda fp=entry: self.open_project_item(fp),
                    bg=self.c("surface2"),
                    fg=self.c("text"),
                    activebackground=self.c("surface2"),
                    activeforeground=self.c("accent"),
                    relief="flat",
                    bd=0,
                    anchor="w",
                    cursor="hand2",
                    font=("Consolas", max(8, int(self.c("font_size")) - 1)),
                ).pack(side="left", fill="x", expand=True, padx=(2, 4), pady=4)
                actions = tk.Frame(row, bg=self.c("surface2"))
                actions.pack(side="right", padx=(0, 4), pady=2)
                self.button(actions, "Open", lambda fp=entry: self.open_project_item(fp), compact=True).pack(
                    side="left", padx=(0, 2)
                )
                self.file_open_with_button(actions, entry).pack(side="left")

    def render_project_detail(self, project_path):
        project_path = Path(project_path)
        self._editing_project_path = project_path
        self.ensure_project_files(project_path)
        meta = self.load_project_meta(project_path)
        due = str(meta.get("due", "")).strip()
        self._detail_header(
            self.project_display_title(project_path, meta),
            str(project_path),
            right_buttons=[
                ("Settings", lambda p=project_path: self.open_project_settings(p)),
                ("Codex", lambda p=project_path: self.codex_task_for_project(p)),
            ],
        )

        scroll = ScrollFrame(self.page, self.c("panel"))
        scroll.pack(fill="both", expand=True)
        body = scroll.inner

        if due:
            due_card = self.card(body)
            due_card.pack(fill="x", pady=(0, 10))
            due_row = tk.Frame(due_card, bg=self.c("surface"))
            due_row.pack(fill="x", padx=12, pady=10)
            tk.Label(
                due_row,
                text="Deadline",
                bg=self.c("surface"),
                fg=self.c("muted"),
                font=self.font(9, "bold"),
            ).pack(side="left")
            tk.Label(
                due_row,
                text=self.due_countdown(due),
                bg=self.c("surface"),
                fg=self.c("accent"),
                font=self.font(12, "bold"),
            ).pack(side="left", padx=(10, 0))
            parsed = parse_due_datetime(due)
            if parsed:
                self.muted(due_row, parsed.strftime("%a %b %d, %Y · %H:%M")).pack(side="left", padx=(10, 0))

        summary_card = self.card(body)
        summary_card.pack(fill="x", pady=(0, 10))
        self.section(summary_card, "Summary")
        self.detail_summary_box = tk.Text(
            summary_card,
            height=3,
            bg=self.c("panel2"),
            fg=self.c("text"),
            insertbackground=self.c("text"),
            selectbackground="#29415d",
            relief="flat",
            bd=0,
            padx=12,
            pady=10,
            wrap="word",
            font=self.font(10),
        )
        self.detail_summary_box.pack(fill="x", padx=12, pady=(0, 12))
        self.detail_summary_box.insert("1.0", self.load_project_summary(project_path))
        self._bind_autosave_text(self.detail_summary_box, project_path, "summary")
        scroll.bind_wheel_forward(self.detail_summary_box)

        files_card = self.card(body)
        files_card.pack(fill="x", pady=(0, 10))
        files_head = tk.Frame(files_card, bg=self.c("surface"))
        files_head.pack(fill="x", padx=12, pady=(8, 4))
        self.section(files_head, "Files")
        self.button(files_head, "Open folder", lambda p=project_path: os.startfile(str(p)), compact=True).pack(
            side="right"
        )
        entries = self.list_project_entries(project_path)
        if not entries:
            self.muted(files_card, "No files found.").pack(anchor="w", padx=12, pady=(0, 12))
        else:
            list_frame = tk.Frame(files_card, bg=self.c("surface"))
            list_frame.pack(fill="x", padx=12, pady=(0, 12))
            self.render_project_file_tree(list_frame, project_path, project_path)
            self.muted(list_frame, f"{len(entries)} item(s)").pack(anchor="w", pady=(6, 0))

        actions = tk.Frame(body, bg=self.c("panel"))
        actions.pack(fill="x", pady=(4, 0))
        self.button(actions, "Remove from aiOS", lambda p=project_path: self.remove_from_aios(p), compact=True).pack(
            side="left"
        )

    def render_project_settings(self, project_path):
        project_path = Path(project_path)
        self._editing_project_path = project_path
        meta = self.load_project_meta(project_path)
        on_dashboard = self.is_dashboard_todo(meta)
        self._detail_header(
            "Project Settings",
            self.project_display_title(project_path, meta),
            back_command=lambda p=project_path: self.open_project_detail(p),
        )

        scroll = ScrollFrame(self.page, self.c("panel"))
        scroll.pack(fill="both", expand=True)
        body = scroll.inner

        card = self.card(body)
        card.pack(fill="x", pady=(0, 10))
        self.section(card, "Details")
        form = tk.Frame(card, bg=self.c("surface"))
        form.pack(fill="x", padx=12, pady=(0, 12))

        tk.Label(form, text="Display name", bg=self.c("surface"), fg=self.c("muted"), font=self.font(9, "bold")).pack(anchor="w")
        self.detail_title_entry = self.single_line(form, self.project_display_title(project_path, meta))
        self.detail_title_entry.pack(fill="x", pady=(4, 8))
        self._bind_autosave_text(self.detail_title_entry, project_path, "title")

        self.detail_status_var = tk.StringVar(value=str(meta.get("status", "active")))
        self.detail_priority_var = tk.StringVar(value=str(meta.get("priority", "normal")))
        row_a = tk.Frame(form, bg=self.c("surface"))
        row_a.pack(fill="x", pady=(0, 6))
        tk.Label(row_a, text="Status", bg=self.c("surface"), fg=self.c("muted"), font=self.font(9, "bold")).pack(side="left")
        status_menu = tk.OptionMenu(row_a, self.detail_status_var, "active", "paused", "done", "archived")
        self.style_option(status_menu)
        status_menu.pack(side="left", padx=(8, 16))
        tk.Label(row_a, text="Priority", bg=self.c("surface"), fg=self.c("muted"), font=self.font(9, "bold")).pack(side="left")
        priority_menu = tk.OptionMenu(row_a, self.detail_priority_var, "low", "normal", "high")
        self.style_option(priority_menu)
        priority_menu.pack(side="left", padx=(8, 0))
        self._bind_autosave_var(self.detail_status_var, project_path)
        self._bind_autosave_var(self.detail_priority_var, project_path)

        tk.Label(form, text="Deadline", bg=self.c("surface"), fg=self.c("muted"), font=self.font(9, "bold")).pack(anchor="w", pady=(6, 0))
        self._build_form_due_picker(form, required=False, default_iso=str(meta.get("due", "")))

        tk.Label(form, text="Tags (comma separated)", bg=self.c("surface"), fg=self.c("muted"), font=self.font(9, "bold")).pack(anchor="w")
        self.detail_tags_entry = self.single_line(form, ", ".join(meta.get("tags", [])))
        self.detail_tags_entry.pack(fill="x", pady=(4, 8))
        self._bind_autosave_text(self.detail_tags_entry, project_path, "tags")

        tk.Label(form, text="Notes", bg=self.c("surface"), fg=self.c("muted"), font=self.font(9, "bold")).pack(anchor="w")
        self.detail_notes = tk.Text(
            form,
            height=4,
            bg=self.c("panel2"),
            fg=self.c("text"),
            insertbackground=self.c("text"),
            selectbackground="#29415d",
            relief="flat",
            bd=0,
            padx=10,
            pady=8,
            wrap="word",
            font=self.font(9),
        )
        self.detail_notes.pack(fill="x", pady=(4, 8))
        self.detail_notes.insert("1.0", str(meta.get("notes", "")))
        self._bind_autosave_text(self.detail_notes, project_path, "notes")
        scroll.bind_wheel_forward(self.detail_notes)

        self.detail_tracked_var = tk.BooleanVar(value=on_dashboard)
        tk.Checkbutton(
            form,
            text="Show on dashboard (to-do)",
            variable=self.detail_tracked_var,
            command=lambda: self._schedule_project_autosave(project_path),
            bg=self.c("surface"),
            activebackground=self.c("surface"),
            selectcolor=self.c("panel2"),
            fg=self.c("text"),
            font=self.font(9),
        ).pack(anchor="w", pady=(0, 4))

        info_card = self.card(body)
        info_card.pack(fill="x")
        self.section(info_card, "Info")
        try:
            mtime = datetime.fromtimestamp(project_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        except OSError:
            mtime = "-"
        for line in (f"Path: {project_path}", f"Modified: {mtime}"):
            self.muted(info_card, line).pack(anchor="w", padx=12, pady=(0, 4))
        self.muted(info_card, "Changes save automatically.").pack(anchor="w", padx=12, pady=(0, 12))

    def render_code(self):
        """Native overview for persistent Codex, Claude, and Cursor jobs."""
        self.code_view_token += 1
        # This tab owns a new widget tree on every render. The previous tree's
        # signature must not suppress drawing into the new session list.
        self._code_sessions_signature = None
        self.code_log_size = 0
        self.code_last_rendered_kind = ""
        self.code_activity_cards = {}
        self.code_create_attachments = []
        self.code_followup_attachments = []
        token = self.code_view_token

        head = tk.Frame(self.page, bg=self.c("panel"))
        head.pack(fill="x", pady=(0, 8))
        tk.Label(head, text="CODE", bg=self.c("panel"), fg=self.c("text"), font=self.font(18, "bold")).pack(side="left")
        tk.Label(
            head,
            text="Codex · Claude · Cursor",
            bg=self.c("panel"),
            fg=self.c("muted"),
            font=self.font(9),
        ).pack(side="left", padx=(10, 0))
        self.header_btn(head, "↗", self._code_open_web, hint="Open the full phone/web CODE dashboard").pack(side="right")
        self.header_btn(head, "↻", lambda: self._code_refresh_all(force=True), hint="Refresh agents and sessions").pack(side="right", padx=(0, 6))

        overview = tk.Frame(self.page, bg=self.c("panel"))
        overview.pack(fill="x", pady=(0, 8))
        self.code_active_var = tk.StringVar(value="0 active")
        self.code_waiting_var = tk.StringVar(value="0 need you")
        self.code_done_var = tk.StringVar(value="0 finished")
        for index, (variable, color) in enumerate((
            (self.code_active_var, self.c("success")),
            (self.code_waiting_var, "#f0c85f"),
            (self.code_done_var, self.c("muted")),
        )):
            tk.Label(
                overview,
                textvariable=variable,
                bg=self.c("surface"),
                fg=color,
                font=self.font(9, "bold"),
                padx=10,
                pady=6,
            ).pack(side="left", padx=(0 if index == 0 else 6, 0))
        self.code_health_var = tk.StringVar(value="Checking local agent logins and models…")
        self.code_speak_var = tk.BooleanVar(value=bool(self.config.get("code_speak_notifications", True)))
        self.code_setup_button = self.button(overview, "Set up agent", self._code_setup_provider, compact=True)
        self.code_setup_button.pack(side="right", padx=(8, 0))
        tk.Checkbutton(
            overview,
            text="Speak milestones",
            variable=self.code_speak_var,
            command=self._code_set_speaking,
            bg=self.c("panel"),
            activebackground=self.c("panel"),
            selectcolor=self.c("panel2"),
            fg=self.c("muted"),
            font=self.font(7),
        ).pack(side="right", padx=(8, 0))
        tk.Label(
            overview,
            textvariable=self.code_health_var,
            bg=self.c("panel"),
            fg=self.c("muted"),
            font=self.font(8),
            anchor="e",
        ).pack(side="right", fill="x", expand=True)

        creator = self.card(self.page)
        creator.pack(fill="x", pady=(0, 8))
        row = tk.Frame(creator, bg=self.c("surface"))
        row.pack(fill="x", padx=10, pady=(9, 5))
        self.code_provider_var = tk.StringVar(value="codex")
        self.code_model_var = tk.StringVar(value="gpt-5.6-sol")
        self.code_reasoning_var = tk.StringVar(value="medium")
        self.code_fast_var = tk.BooleanVar(value=False)
        self.code_project_var = tk.StringVar(value=str(self.active_project or self.default_project_path()))
        tk.Label(row, text="Agent", bg=self.c("surface"), fg=self.c("muted"), font=self.font(8, "bold")).pack(side="left", padx=(0, 6))
        self._code_build_provider_picker(row)
        for label, variable, values, width in (
            ("Model", self.code_model_var, ("gpt-5.6-sol",), 18),
            ("Intelligence", self.code_reasoning_var, ("medium",), 9),
        ):
            tk.Label(row, text=label, bg=self.c("surface"), fg=self.c("muted"), font=self.font(8, "bold")).pack(side="left", padx=(0, 5))
            option = tk.OptionMenu(row, variable, *values)
            self.style_option(option)
            option.configure(width=width)
            option.pack(side="left", padx=(0, 9))
            if label == "Model":
                self.code_model_menu = option
            else:
                self.code_reasoning_menu = option
        self.code_fast_check = tk.Checkbutton(
            row,
            text="Fast",
            variable=self.code_fast_var,
            bg=self.c("surface"),
            activebackground=self.c("surface"),
            selectcolor=self.c("panel2"),
            fg=self.c("text"),
            font=self.font(8, "bold"),
        )
        self.code_fast_check.pack(side="left")

        folder_row = tk.Frame(creator, bg=self.c("surface"))
        folder_row.pack(fill="x", padx=10, pady=(0, 5))
        tk.Label(folder_row, text="Folder", bg=self.c("surface"), fg=self.c("muted"), font=self.font(8, "bold")).pack(side="left", padx=(0, 6))
        self.code_project_entry = tk.Entry(
            folder_row,
            textvariable=self.code_project_var,
            bg=self.c("panel2"),
            fg=self.c("text"),
            insertbackground=self.c("text"),
            relief="flat",
            font=self.font(9),
        )
        self.code_project_entry.pack(side="left", fill="x", expand=True, ipady=6)
        self.button(folder_row, "Browse", self._code_browse_project, compact=True).pack(side="right", padx=(7, 0))

        brief_row = tk.Frame(creator, bg=self.c("surface"))
        brief_row.pack(fill="x", padx=10, pady=(0, 9))
        self.code_brief = tk.Text(
            brief_row,
            height=3,
            bg=self.c("panel2"),
            fg=self.c("text"),
            insertbackground=self.c("text"),
            selectbackground="#29415d",
            relief="flat",
            padx=9,
            pady=7,
            wrap="word",
            font=self.font(9),
        )
        self.code_brief.pack(side="left", fill="both", expand=True)
        actions = tk.Frame(brief_row, bg=self.c("surface"))
        actions.pack(side="right", fill="y", padx=(7, 0))
        self.code_attach_button = self.button(actions, "Attach", self._code_attach_create, compact=True)
        self.code_attach_button.pack(fill="x", pady=(0, 5))
        self.button(actions, "Launch", self._code_start_job, compact=True, active=True).pack(fill="x")

        split = tk.PanedWindow(self.page, orient="horizontal", bg=self.c("panel"), sashwidth=5, bd=0, relief="flat")
        split.pack(fill="both", expand=True)
        left = self.card(split)
        right = self.card(split)
        split.add(left, minsize=190, width=220)
        split.add(right, minsize=280)

        list_head = tk.Frame(left, bg=self.c("surface"))
        list_head.pack(fill="x", padx=9, pady=(8, 5))
        tk.Label(list_head, text="SESSIONS", bg=self.c("surface"), fg=self.c("muted"), font=self.font(8, "bold")).pack(side="left")
        self.code_sessions_frame = ScrollFrame(left, self.c("surface"))
        self.code_sessions_frame.pack(fill="both", expand=True, padx=4, pady=(0, 6))

        detail_head = tk.Frame(right, bg=self.c("surface"))
        detail_head.pack(fill="x", padx=10, pady=(8, 5))
        self.code_detail_title_var = tk.StringVar(value="Select a session")
        self.code_detail_meta_var = tk.StringVar(value="Current conversation, questions, and tool outputs appear here.")
        title_wrap = tk.Frame(detail_head, bg=self.c("surface"))
        title_wrap.pack(side="left", fill="x", expand=True)
        tk.Label(title_wrap, textvariable=self.code_detail_title_var, bg=self.c("surface"), fg=self.c("text"), font=self.font(10, "bold"), anchor="w").pack(fill="x")
        tk.Label(title_wrap, textvariable=self.code_detail_meta_var, bg=self.c("surface"), fg=self.c("muted"), font=self.font(7), anchor="w").pack(fill="x")
        self.code_stop_button = self.button(detail_head, "Stop", self._code_stop_job, compact=True)
        self.code_stop_button.pack(side="right", padx=(5, 0))
        self.code_delete_button = self.button(detail_head, "Delete", self._code_delete_job, compact=True)
        self.code_delete_button.pack(side="right")

        output_wrap = tk.Frame(right, bg=self.CODE_CHAT_BG)
        output_wrap.pack(fill="both", expand=True, padx=9)
        chat_scroll = tk.Scrollbar(output_wrap, orient="vertical", width=10)
        chat_scroll.pack(side="right", fill="y")
        self.code_chat = ScrollFrame(output_wrap, self.CODE_CHAT_BG)
        self.code_chat.pack(side="left", fill="both", expand=True)
        self.code_chat.canvas.configure(yscrollcommand=chat_scroll.set)
        self.code_chat.on_user_scroll = self._code_chat_user_scrolled
        chat_scroll.configure(command=self._code_chat_scrollbar)
        self.code_chat_inner = self.code_chat.inner
        self.code_chat.canvas.bind("<Configure>", self._code_chat_on_resize, add="+")
        self._code_chat_reset()

        compose = tk.Frame(right, bg=self.c("surface"))
        compose.pack(fill="x", padx=9, pady=(6, 9))
        self.code_followup = tk.Text(
            compose,
            height=2,
            bg=self.c("panel2"),
            fg=self.c("text"),
            insertbackground=self.c("text"),
            relief="flat",
            padx=8,
            pady=6,
            wrap="word",
            font=self.font(9),
        )
        self.code_followup.pack(side="left", fill="x", expand=True)
        follow_actions = tk.Frame(compose, bg=self.c("surface"))
        follow_actions.pack(side="right", padx=(6, 0))
        self.code_urgent_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            follow_actions,
            text="Urgent",
            variable=self.code_urgent_var,
            bg=self.c("surface"),
            activebackground=self.c("surface"),
            selectcolor=self.c("panel2"),
            fg=self.c("muted"),
            font=self.font(7),
        ).pack(anchor="w")
        self.code_follow_attach_button = self.button(follow_actions, "+ File", self._code_attach_followup, compact=True)
        self.code_follow_attach_button.pack(side="left", padx=(0, 5))
        self.button(follow_actions, "Send", self._code_send_followup, compact=True, active=True).pack(side="left")

        self.code_provider_var.trace_add("write", lambda *_args: self._code_on_provider_changed())
        self.code_model_var.trace_add("write", lambda *_args: self._code_refresh_reasoning())
        self._code_sync_provider_buttons()
        self._code_render_summary()
        self._code_render_sessions()
        self._code_refresh_all(force=False, token=token)

    def render_codex(self):
        """Compatibility alias for commands saved before the CODE rename."""
        self.render_code()

    def _code_build_provider_picker(self, parent):
        """Three logo buttons for Codex / Claude / Cursor instead of a dropdown."""
        picker = tk.Frame(parent, bg=self.c("surface"))
        picker.pack(side="left", padx=(0, 10))
        icons = self._code_ensure_provider_icons()
        self.code_provider_buttons = {}
        for provider, _filename, label in CODE_PROVIDER_CHOICES:
            shell = tk.Frame(
                picker,
                bg=self.c("panel2"),
                highlightthickness=2,
                highlightbackground=self.c("panel2"),
                highlightcolor=self.c("accent"),
                cursor="hand2",
            )
            shell.pack(side="left", padx=(0, 5))
            image = icons.get(provider)
            if image is not None:
                face = tk.Label(
                    shell,
                    image=image,
                    bg=self.c("panel2"),
                    bd=0,
                    padx=4,
                    pady=4,
                    cursor="hand2",
                )
            else:
                face = tk.Label(
                    shell,
                    text=label.split()[0][:1],
                    bg=self.c("panel2"),
                    fg=self.c("text"),
                    font=self.font(10, "bold"),
                    width=3,
                    padx=4,
                    pady=4,
                    cursor="hand2",
                )
            face.pack()
            for widget in (shell, face):
                widget.bind("<Button-1>", lambda _event, name=provider: self._code_select_provider(name))
                self._bind_header_hint(widget, label)
            self.code_provider_buttons[provider] = {"shell": shell, "face": face}

    def _code_ensure_provider_icons(self):
        if self.code_provider_images:
            return self.code_provider_images
        try:
            from PIL import Image, ImageTk
        except Exception:
            return self.code_provider_images
        size = max(24, min(36, int(self.c("font_size")) + 18))
        for provider, filename, _label in CODE_PROVIDER_CHOICES:
            path = CODE_PROVIDER_ICON_DIR / filename
            hi = CODE_PROVIDER_ICON_DIR / filename.replace(".png", "@2x.png")
            source = hi if hi.exists() else path
            if not source.exists():
                continue
            try:
                img = Image.open(source).convert("RGBA")
                img = img.resize((size, size), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(img)
                self.code_provider_images[provider] = photo
            except Exception:
                continue
        return self.code_provider_images

    def _code_select_provider(self, provider):
        if self.code_provider_var.get() == provider:
            self._code_sync_provider_buttons()
            return
        self.code_provider_var.set(provider)

    def _code_on_provider_changed(self):
        self._code_sync_provider_buttons()
        self._code_refresh_selectors()

    def _code_sync_provider_buttons(self):
        selected = (self.code_provider_var.get() or "codex").strip().lower()
        accent = self.c("accent")
        idle = self.blend_color(self.c("panel2"), self.c("muted"), 0.22)
        face_idle = self.c("panel2")
        face_active = self.blend_color(self.c("panel2"), accent, 0.18)
        for provider, widgets in (self.code_provider_buttons or {}).items():
            active = provider == selected
            shell = widgets.get("shell")
            face = widgets.get("face")
            if shell is None:
                continue
            try:
                shell.configure(
                    highlightbackground=accent if active else idle,
                    highlightcolor=accent if active else idle,
                    bg=face_active if active else face_idle,
                )
                if face is not None:
                    face.configure(bg=face_active if active else face_idle)
            except tk.TclError:
                pass

    def _code_api(self, path, method="GET", payload=None, timeout=20):
        port = int(os.environ.get("AIOS_PHONE_BRIDGE_PORT", "5000"))
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"ok": False, "error": raw or str(exc)}
        except Exception as exc:
            return {"ok": False, "error": f"CODE backend unavailable: {exc}"}

    def _poll_code_notifications(self):
        """Relay CODE milestones even when the CODE tab is not open."""
        if self.code_notify_busy:
            self.root.after(2500, self._poll_code_notifications)
            return
        self.code_notify_busy = True
        offsets = dict(self.code_notify_offsets)
        started_at = float(self.code_notify_started_at)

        def worker():
            listing = self._code_api("/api/code/jobs?limit=250", timeout=12)
            notices = []
            if listing.get("ok"):
                for job in listing.get("jobs") or []:
                    job_id = str(job.get("id") or "")
                    if not job_id:
                        continue
                    known = job_id in offsets and int(offsets[job_id]) >= 0
                    recent = float(job.get("updated_at") or 0) >= started_at - 5
                    active = job.get("status") in {"queued", "running", "waiting_user"}
                    if not known and not recent and not active:
                        offsets[job_id] = -1
                        continue
                    since = max(0, int(offsets.get(job_id, 0)))
                    log = self._code_api(f"/api/code/jobs/{job_id}/log?since={since}", timeout=12)
                    if not log.get("ok"):
                        continue
                    offsets[job_id] = int(log.get("size") or since)
                    for event in log.get("events") or []:
                        if not event.get("notify"):
                            continue
                        if not known and float(event.get("ts") or 0) < started_at - 3:
                            continue
                        notices.append((job, event))
            jobs = listing.get("jobs") or [] if listing.get("ok") else None
            self.root.after(0, lambda: self._code_apply_notifications(offsets, notices, jobs))

        threading.Thread(target=worker, daemon=True, name="aios-code-notifications").start()

    def _code_apply_notifications(self, offsets, notices, jobs=None):
        self.code_notify_busy = False
        self.code_notify_offsets = offsets
        # The notification poll doubles as a cheap session prefetch, so CODE
        # can paint saved sessions immediately when the tab opens.
        if jobs is not None and self.active_tab != "CODE":
            self.code_jobs = jobs
        for job, event in notices:
            kind = str(event.get("kind") or "status")
            provider = str(job.get("provider") or "CODE").title()
            titles = {
                "result": f"{provider} finished",
                "question": f"{provider} needs your answer",
                "error": f"{provider} failed",
                "warning": f"{provider} is still working",
                "status": f"{provider} CODE update",
            }
            if kind not in titles:
                continue
            message = str(event.get("text") or kind)
            self._tray_notify(titles[kind], message, "error" if kind == "error" else "warning" if kind in {"question", "warning"} else "info")
            if self.config.get("code_speak_notifications", True):
                speaker = getattr(self, "agent_operator_tts", None)
                if speaker:
                    try:
                        speaker.speak((titles[kind] + ". " + message)[:700])
                    except Exception:
                        pass
        self.root.after(3000, self._poll_code_notifications)

    def _code_open_web(self):
        port = int(os.environ.get("AIOS_PHONE_BRIDGE_PORT", "5000"))
        os.startfile(f"http://127.0.0.1:{port}/code")

    def _code_setup_provider(self):
        provider = self.code_provider_var.get().strip().lower()
        self.code_health_var.set(f"Opening {provider.title()} sign-in…")

        def worker():
            result = self._code_api(f"/api/code/providers/{provider}/setup", "POST", {}, timeout=20)
            self.root.after(0, lambda: self._code_setup_result(result))

        threading.Thread(target=worker, daemon=True, name="aios-code-setup").start()

    def _code_setup_result(self, result):
        if not result.get("ok"):
            messagebox.showerror("CODE setup", result.get("error") or "Could not open provider sign-in.")
            return
        messagebox.showinfo("CODE setup", result.get("message") or "Sign-in opened. Refresh CODE when it is complete.")

    def _code_browse_project(self):
        selected = filedialog.askdirectory(initialdir=self.code_project_var.get() or str(self.project_root))
        if selected:
            self.code_project_var.set(selected)

    def _code_attach_create(self):
        paths = list(filedialog.askopenfilenames(title="Attach files or images to the CODE job"))
        if paths:
            self.code_create_attachments.extend(path for path in paths if path not in self.code_create_attachments)
        self.code_attach_button.configure(text=f"Attach ({len(self.code_create_attachments)})" if self.code_create_attachments else "Attach")

    def _code_attach_followup(self):
        paths = list(filedialog.askopenfilenames(title="Attach files or images to the follow-up"))
        if paths:
            self.code_followup_attachments.extend(path for path in paths if path not in self.code_followup_attachments)
        self.code_follow_attach_button.configure(text=f"+ File ({len(self.code_followup_attachments)})" if self.code_followup_attachments else "+ File")

    def _code_refresh_all(self, force=False, token=None):
        if self.active_tab != "CODE":
            return
        token = self.code_view_token if token is None else token
        selected = self.code_selected_id
        since = self.code_log_size
        scope = (token, selected, since)
        inflight = getattr(self, "_code_refresh_inflight", None)
        if not force and inflight and inflight.get("scope") == scope:
            return

        self._code_refresh_seq = getattr(self, "_code_refresh_seq", 0) + 1
        request_id = self._code_refresh_seq
        self._code_refresh_inflight = {"id": request_id, "scope": scope}

        # Provider CLI discovery can take tens of seconds on a cold start. It
        # must never sit in front of the tiny session-list request.
        if force or not self.code_capabilities.get("providers"):
            self._code_refresh_capabilities(force=force, token=token)

        def worker():
            listing = self._code_api("/api/code/jobs?limit=250", timeout=10)
            log = self._code_api(f"/api/code/jobs/{selected}/log?since={since}", timeout=10) if selected else None
            self.root.after(
                0,
                lambda: self._code_apply_refresh(
                    token, request_id, selected, listing, log, since
                ),
            )

        threading.Thread(target=worker, daemon=True, name="aios-code-refresh").start()

    def _code_refresh_capabilities(self, *, force=False, token=None):
        if getattr(self, "_code_capabilities_busy", False):
            return
        self._code_capabilities_busy = True
        token = self.code_view_token if token is None else token

        def worker():
            suffix = "?refresh=1" if force else ""
            caps = self._code_api(f"/api/code/capabilities{suffix}", timeout=45)
            self.root.after(0, lambda: self._code_apply_capabilities(token, caps))

        threading.Thread(target=worker, daemon=True, name="aios-code-capabilities").start()

    def _code_apply_capabilities(self, token, caps):
        self._code_capabilities_busy = False
        if self.active_tab != "CODE" or token != self.code_view_token:
            return
        if caps.get("ok"):
            self.code_capabilities = caps
            self._code_refresh_selectors()
            self._code_render_summary()

    def _code_apply_refresh(self, token, request_id, selected, listing, log, requested_since):
        inflight = getattr(self, "_code_refresh_inflight", None)
        if not inflight or inflight.get("id") != request_id:
            return
        self._code_refresh_inflight = None
        if self.active_tab != "CODE" or token != self.code_view_token:
            return
        if listing.get("ok"):
            self.code_jobs = listing.get("jobs") or []
            self._code_render_summary()
            self._code_render_sessions()
            current = next((job for job in self.code_jobs if job.get("id") == self.code_selected_id), None)
            if current:
                self._code_render_detail_meta(current)
        else:
            self.code_health_var.set(listing.get("error") or "CODE backend unavailable")
        # A click can select another session while this request is in flight.
        # Never append the old session's log to the newly selected chat.
        if log and log.get("ok") and selected and selected == self.code_selected_id:
            initial_render = bool(log.get("reset") or requested_since == 0)
            if initial_render:
                self._code_chat_reset()
                self.code_auto_follow = False
            for event in log.get("events") or []:
                self._code_render_event(event, live=requested_since > 0)
            if initial_render:
                self.code_auto_follow = True
                self._code_chat_scroll_end(force=True)
            self.code_log_size = int(log.get("size") or self.code_log_size)
            if log.get("job"):
                self._code_render_detail_meta(log["job"])
        self.code_poll_after = self.root.after(1500, self._code_refresh_all)

    def _code_render_summary(self):
        active = sum(job.get("status") in {"queued", "running"} for job in self.code_jobs)
        waiting = sum(job.get("status") == "waiting_user" for job in self.code_jobs)
        done = sum(job.get("status") == "completed" for job in self.code_jobs)
        self.code_active_var.set(f"{active} active")
        self.code_waiting_var.set(f"{waiting} need you")
        self.code_done_var.set(f"{done} finished")
        health = []
        for provider in self.code_capabilities.get("providers") or []:
            health.append(f"{provider.get('provider', '').title()} {'ready' if provider.get('ready') else 'setup needed'}")
        self.code_health_var.set("  ·  ".join(health) or "Loading agent capabilities…")

    def _code_render_sessions(self):
        if not hasattr(self, "code_sessions_frame"):
            return
        signature = tuple((job.get("id"), job.get("title"), job.get("status"), job.get("updated_at")) for job in self.code_jobs)
        if signature == getattr(self, "_code_sessions_signature", None):
            return
        self._code_sessions_signature = signature
        self.clear(self.code_sessions_frame.inner)
        if not self.code_jobs:
            self.muted(self.code_sessions_frame.inner, "No sessions yet. Launch one here or ask the voice agent.").pack(fill="x", padx=8, pady=20)
            return
        colors = {"codex": "#65b8ff", "claude": "#dc795a", "cursor": "#b892ff"}
        for job in self.code_jobs:
            selected = job.get("id") == self.code_selected_id
            item = tk.Frame(
                self.code_sessions_frame.inner,
                bg=self.c("panel2") if selected else self.c("surface"),
                highlightthickness=1 if selected else 0,
                highlightbackground=self.c("accent"),
                cursor="hand2",
            )
            item.pack(fill="x", padx=4, pady=3)
            dot = tk.Label(item, text="●", bg=item.cget("bg"), fg=colors.get(job.get("provider"), self.c("muted")), font=self.font(7))
            dot.pack(side="left", padx=(7, 5), pady=8)
            copy = tk.Frame(item, bg=item.cget("bg"))
            copy.pack(side="left", fill="x", expand=True, pady=6)
            title = tk.Label(copy, text=str(job.get("title") or "CODE job"), bg=item.cget("bg"), fg=self.c("text"), font=self.font(8, "bold"), anchor="w")
            title.pack(fill="x")
            meta = tk.Label(copy, text=f"{job.get('project_name', '')} · {job.get('status', '')}", bg=item.cget("bg"), fg=self.c("muted"), font=self.font(7), anchor="w")
            meta.pack(fill="x")
            for widget in (item, dot, copy, title, meta):
                widget.bind("<Button-1>", lambda _event, value=job.get("id"): self._code_select_job(value))

    def _code_select_job(self, job_id):
        self.code_selected_id = str(job_id or "")
        self.code_log_size = 0
        self._code_sessions_signature = None
        self._code_chat_reset()
        self._code_render_sessions()
        self._code_refresh_all()

    def _code_render_detail_meta(self, job):
        self.code_detail_title_var.set(str(job.get("title") or "CODE job"))
        fast = " · fast" if job.get("fast") else ""
        self.code_detail_meta_var.set(
            f"{str(job.get('provider') or '').title()} · {job.get('status')} · {job.get('model')} / {job.get('reasoning')}{fast}\n{job.get('cwd', '')}"
        )
        active = job.get("status") in {"queued", "running", "waiting_user"}
        self.code_stop_button.configure(state="normal" if active else "disabled")
        self.code_delete_button.configure(state="disabled" if active else "normal")
        self._code_set_busy(
            job.get("status") in {"queued", "running"},
            f"{str(job.get('provider') or 'Agent').title()} is working",
        )

    # ---- CODE session chat -------------------------------------------------

    CODE_CHAT_BG = "#080c0e"

    def _code_chat_live(self):
        inner = getattr(self, "code_chat_inner", None)
        try:
            return inner is not None and inner.winfo_exists()
        except tk.TclError:
            return False

    def _code_chat_reset(self):
        self.code_last_rendered_kind = ""
        self.code_activity_cards = {}
        self.code_stream = None
        self.code_turn_assistant_text = ""
        self.code_busy_row = None
        self.code_busy_label = None
        self.code_auto_follow = True
        if not self._code_chat_live():
            return
        for child in list(self.code_chat_inner.winfo_children()):
            try:
                child.destroy()
            except tk.TclError:
                pass
        self._code_spin_start()

    def _code_chat_width(self):
        try:
            width = int(self.code_chat.canvas.winfo_width())
        except (AttributeError, tk.TclError, TypeError, ValueError):
            width = 0
        return max(220, (width or 520) - 26)

    def _code_chat_on_resize(self, _event=None):
        """Re-wrap every message when the pane width changes."""
        if not self._code_chat_live():
            return
        for widget in self._code_chat_text_widgets():
            self._code_fit_text(widget)

    def _code_chat_text_widgets(self, parent=None):
        found = []
        parent = parent if parent is not None else self.code_chat_inner
        try:
            children = parent.winfo_children()
        except tk.TclError:
            return found
        for child in children:
            if isinstance(child, tk.Text) and getattr(child, "_code_autofit", False):
                found.append(child)
            found.extend(self._code_chat_text_widgets(child))
        return found

    def _code_chat_near_bottom(self):
        if not self._code_chat_live():
            return True
        try:
            _first, last = self.code_chat.canvas.yview()
            return float(last) >= 0.999
        except (tk.TclError, TypeError, ValueError):
            return True

    def _code_chat_user_scrolled(self, wheel_delta=None):
        # Freeze following immediately; otherwise a streaming event arriving
        # before after_idle can snap the user back to the bottom.
        self.code_auto_follow = False
        if wheel_delta is not None and float(wheel_delta or 0) > 0:
            return

        def update():
            self.code_auto_follow = self._code_chat_near_bottom()
        try:
            self.root.after_idle(update)
        except tk.TclError:
            pass

    def _code_chat_scrollbar(self, *args):
        try:
            self.code_chat.canvas.yview(*args)
        finally:
            self._code_chat_user_scrolled()

    def _code_chat_scroll_end(self, force=False):
        if not self._code_chat_live() or (not force and not getattr(self, "code_auto_follow", True)):
            return

        def apply():
            try:
                if not force and not getattr(self, "code_auto_follow", True):
                    return
                self.code_chat.canvas.update_idletasks()
                bounds = self.code_chat.canvas.bbox("all")
                if bounds:
                    self.code_chat.canvas.configure(scrollregion=bounds)
                self.code_chat.canvas.yview_moveto(1.0)
                self.code_auto_follow = True
            except tk.TclError:
                pass

        apply()
        try:
            self.root.after_idle(apply)
        except tk.TclError:
            pass

    def _code_chat_row(self):
        """One transcript row, always kept above the live spinner line."""
        row = tk.Frame(self.code_chat_inner, bg=self.CODE_CHAT_BG)
        row.pack(fill="x", padx=8, pady=(6, 0))
        busy = getattr(self, "code_busy_row", None)
        try:
            if busy is not None and busy.winfo_exists():
                busy.pack_forget()
                busy.pack(fill="x", padx=8, pady=(6, 0))
        except tk.TclError:
            pass
        return row

    def _code_fit_text(self, widget):
        """Size a read-only Text to its wrapped content so rows never scroll.

        Tk only knows how many display lines the text needs once the widget has
        a real width, so the fit also runs from ``<Configure>``; before the
        first layout the width is 1 char and the count is meaningless.
        """
        def apply(_event=None):
            if getattr(widget, "_code_fitting", False):
                return
            widget._code_fitting = True
            try:
                def measure(what):
                    value = widget.count("1.0", "end", what)
                    if isinstance(value, tuple):
                        value = value[0] if value else 0
                    return max(0, int(value or 0))

                if not widget.winfo_exists() or widget.winfo_width() <= 1:
                    return
                # Tk lays out only the lines that currently fit, so start from
                # the wrapped line count and then top up in pixels: headings and
                # code blocks are taller than the widget's base line height.
                height = max(1, measure("displaylines"))
                if int(widget.cget("height")) != height:
                    widget.configure(height=height)
                    widget.update_idletasks()
                for _ in range(12):
                    if measure("ypixels") <= widget.winfo_height():
                        break
                    height += 1
                    widget.configure(height=height)
                    widget.update_idletasks()
            except (tk.TclError, TypeError, ValueError):
                pass
            finally:
                widget._code_fitting = False

        if not getattr(widget, "_code_fit_bound", False):
            widget._code_fit_bound = True
            widget.bind("<Configure>", apply, add="+")
        apply()
        try:
            self.root.after_idle(apply)
        except tk.TclError:
            pass

    def _code_text_widget(self, parent, *, bg, fg, mono=False):
        body_font = self.font(9)
        size = max(8, int(body_font[1]))
        widget = tk.Text(
            parent,
            bg=bg,
            fg=fg,
            insertbackground=fg,
            selectbackground="#29415d",
            relief="flat",
            bd=0,
            padx=0,
            pady=0,
            height=1,
            wrap="word",
            cursor="arrow",
            font=("Cascadia Code", max(7, size - 1)) if mono else body_font,
        )
        widget._code_autofit = True
        mono_font = ("Cascadia Code", max(7, size - 1))
        widget.tag_configure("bold", font=self.font(9, "bold"))
        widget.tag_configure("italic", font=(body_font[0], size, "italic"))
        widget.tag_configure("code", font=mono_font, background=self.blend_color(bg, "#ffffff", 0.09), foreground="#d7e6ff")
        widget.tag_configure("codeblock", font=mono_font, background=self.blend_color(bg, "#ffffff", 0.06), foreground="#d7e6ff", lmargin1=10, lmargin2=10, spacing1=3, spacing3=3)
        widget.tag_configure("link", foreground=self.c("accent"), underline=True)
        widget.tag_configure("muted", foreground=self.c("muted"))
        widget.tag_configure("h1", font=self.font(12, "bold"), spacing1=6, spacing3=3)
        widget.tag_configure("h2", font=self.font(11, "bold"), spacing1=5, spacing3=2)
        widget.tag_configure("h3", font=self.font(10, "bold"), spacing1=4, spacing3=2)
        widget.tag_configure("quote", foreground=self.c("muted"), lmargin1=12, lmargin2=12)
        widget.tag_configure("bullet", lmargin1=6, lmargin2=20)
        widget.tag_configure("table", font=mono_font, background=self.blend_color(bg, "#ffffff", 0.045), foreground="#dbe4e8", lmargin1=6, lmargin2=6, spacing1=3, spacing3=3)
        return widget

    @staticmethod
    def _code_table_text(rows):
        if not rows:
            return ""
        column_count = max(len(row) for row in rows)
        normalized = [(list(row) + [""] * column_count)[:column_count] for row in rows]
        widths = [min(26, max(3, max(len(str(row[column])) for row in normalized))) for column in range(column_count)]

        def cell(value, width):
            value = str(value)
            if len(value) > width:
                value = value[: max(1, width - 1)] + "…"
            return value.ljust(width)

        def row_text(row):
            return "│ " + " │ ".join(cell(row[column], widths[column]) for column in range(column_count)) + " │"

        separator = "├─" + "─┼─".join("─" * width for width in widths) + "─┤"
        return "\n".join([row_text(normalized[0]), separator, *(row_text(row) for row in normalized[1:])])

    def _code_open_markdown_link(self, target):
        value = str(target or "").strip()
        try:
            parsed = urllib.parse.urlparse(value)
            if parsed.scheme in {"http", "https"}:
                os.startfile(value)
                return
            if parsed.scheme == "file":
                path = urllib.parse.unquote(parsed.path)
                match = re.match(r"^/mnt/([a-zA-Z])/(.*)$", path)
                if match:
                    path = f"{match.group(1).upper()}:\\{match.group(2).replace('/', os.sep)}"
                os.startfile(path)
        except OSError:
            pass

    def _code_write_markdown(self, widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        blocks = code_markdown_blocks(text)
        for index, block in enumerate(blocks):
            kind = block["type"]
            if kind == "blank":
                if index:
                    widget.insert("end", "\n")
                continue
            if index:
                widget.insert("end", "\n")
            if kind == "rule":
                widget.insert("end", "─" * 24, "muted")
                continue
            if kind == "code":
                widget.insert("end", block["spans"][0][0], "codeblock")
                continue
            if kind == "table":
                widget.insert("end", self._code_table_text(block.get("rows") or []), "table")
                continue
            prefix_tag = kind if kind in {"h1", "h2", "h3", "quote"} else ""
            indent = "  " * int(block.get("indent") or 0)
            if kind == "bullet":
                widget.insert("end", f"{indent}•  ", "bullet")
            elif kind == "task":
                widget.insert("end", f"{indent}{'☑' if block.get('checked') else '☐'}  ", "bullet")
            elif kind == "number":
                widget.insert("end", f"{indent}{block.get('marker', '1.')}  ", "bullet")
            elif indent:
                widget.insert("end", indent)
            for span in block["spans"]:
                span_text, style = span[:2]
                if style == "link" and len(span) > 2:
                    target = span[2]
                    style = f"link_{abs(hash(target))}"
                    widget.tag_configure(style, foreground=self.c("accent"), underline=True)
                    widget.tag_bind(style, "<Button-1>", lambda _event, url=target: self._code_open_markdown_link(url))
                    widget.tag_bind(style, "<Enter>", lambda _event, item=widget: item.configure(cursor="hand2"))
                    widget.tag_bind(style, "<Leave>", lambda _event, item=widget: item.configure(cursor="arrow"))
                tags = tuple(tag for tag in (prefix_tag, style, "bullet" if kind in {"bullet", "number", "task"} else "") if tag)
                widget.insert("end", span_text, tags or None)
        widget.configure(state="disabled")
        self._code_fit_text(widget)

    def _code_add_user_bubble(self, text):
        row = self._code_chat_row()
        bg = self.blend_color(self.CODE_CHAT_BG, self.c("accent"), 0.26)
        bubble = tk.Frame(row, bg=bg, highlightthickness=1, highlightbackground=self.blend_color(bg, "#ffffff", 0.12))
        bubble.pack(side="right", anchor="e", padx=(60, 2))
        tk.Label(
            bubble,
            text=str(text or "").strip(),
            bg=bg,
            fg=self.c("text"),
            font=self.font(9),
            justify="left",
            anchor="w",
            wraplength=max(160, int(self._code_chat_width() * 0.72)),
            padx=11,
            pady=8,
        ).pack(anchor="w")
        self._code_chat_scroll_end()

    def _code_add_message(self, text, kind):
        """Agent prose: no bubble, just readable markdown with a soft label."""
        row = self._code_chat_row()
        accents = {
            "error": self.c("danger"),
            "question": "#f0c85f",
            "warning": "#f0c85f",
        }
        holder = tk.Frame(row, bg=self.CODE_CHAT_BG)
        holder.pack(side="left", anchor="w", fill="x", expand=True, padx=(2, 40))
        if kind in accents:
            labels = {"error": "ERROR", "question": "QUESTION", "warning": "WARNING"}
            tk.Label(
                holder,
                text=labels.get(kind, kind.upper()),
                bg=self.CODE_CHAT_BG,
                fg=accents[kind],
                font=self.font(7, "bold"),
                anchor="w",
            ).pack(fill="x", pady=(0, 3))
        # The label already carries the colour; a whole report in accent green
        # is hard to read, so only errors and questions tint their body text.
        widget = self._code_text_widget(
            holder,
            bg=self.CODE_CHAT_BG,
            fg=accents.get(kind, self.c("text")) if kind in {"error", "question", "warning"} else self.c("text"),
        )
        widget.pack(fill="x")
        self._code_write_markdown(widget, text)
        self._code_chat_scroll_end()
        return widget

    def _code_add_status(self, text):
        row = self._code_chat_row()
        tk.Label(
            row,
            text=str(text or "").strip(),
            bg=self.CODE_CHAT_BG,
            fg=self.c("muted"),
            font=self.font(7),
            anchor="center",
            wraplength=self._code_chat_width(),
        ).pack(fill="x")
        self._code_chat_scroll_end()

    def _code_add_handoff(self, event):
        row = self._code_chat_row()
        bg = self.blend_color(self.CODE_CHAT_BG, "#8f72d8", 0.16)
        card = tk.Frame(
            row,
            bg=bg,
            highlightthickness=1,
            highlightbackground=self.blend_color(bg, "#b892ff", 0.34),
        )
        card.pack(fill="x", padx=(2, 20))
        tk.Label(
            card,
            text="PROVIDER HANDOFF",
            bg=bg,
            fg="#d9c6ff",
            font=self.font(7, "bold"),
            anchor="w",
        ).pack(fill="x", padx=11, pady=(8, 2))
        tk.Label(
            card,
            text=str(event.get("text") or "CODE provider switched"),
            bg=bg,
            fg=self.c("text"),
            font=self.font(9, "bold"),
            anchor="w",
            justify="left",
            wraplength=self._code_chat_width() - 70,
        ).pack(fill="x", padx=11)
        tk.Label(
            card,
            text="New native provider session · aiOS transferred context and working-tree state",
            bg=bg,
            fg=self.c("muted"),
            font=self.font(7),
            anchor="w",
            justify="left",
            wraplength=self._code_chat_width() - 70,
        ).pack(fill="x", padx=11, pady=(2, 8))
        self._code_chat_scroll_end()

    def _code_stream_assistant(self, delta):
        """Codex streams word-sized deltas; keep one growing markdown block."""
        stream = getattr(self, "code_stream", None)
        if stream is None or not stream["widget"].winfo_exists():
            stream = {"widget": self._code_add_message("", "assistant"), "text": ""}
            self.code_stream = stream
        addition = str(delta or "")
        stream["text"] += addition
        self.code_turn_assistant_text = f"{getattr(self, 'code_turn_assistant_text', '')}{addition}"
        self._code_write_markdown(stream["widget"], stream["text"])
        self._code_chat_scroll_end()

    def _code_close_stream(self):
        self.code_stream = None

    # ---- activity cards ----------------------------------------------------

    def _code_activity_from_event(self, event):
        """Normalize legacy tool/thinking rows into the same card shape."""
        kind = str(event.get("kind") or "tool")
        if kind == "activity":
            return dict(event)
        raw = str(event.get("text") or "Working").strip()
        activity_type = "thinking" if kind == "thinking" else "tool"
        if raw.startswith("$ ") or event.get("tool") == "command":
            activity_type = "command"
        elif re.match(r"^Edited\b", raw, flags=re.I) or event.get("tool") == "files":
            activity_type = "files"
        titles = {"thinking": "Thought through the approach", "command": "Ran command", "files": raw.split("\n")[0]}
        return {
            "kind": "activity",
            "activity_id": code_activity_key(event),
            "activity_type": activity_type,
            "phase": "completed",
            "title": titles.get(activity_type, "Approved permission" if kind == "approval" else raw.split(":")[0][:80]),
            "detail": "" if activity_type == "command" else raw,
            "command": re.sub(r"^\$\s*", "", raw) if activity_type == "command" else "",
            "summary": raw if activity_type == "thinking" else "",
            "ts": event.get("ts"),
        }

    def _code_upsert_activity(self, event):
        key = code_activity_key(event)
        cards = getattr(self, "code_activity_cards", None)
        if cards is None:
            cards = self.code_activity_cards = {}
        card = cards.get(key)
        if card is None or not self._card_alive(card):
            card = self._code_build_activity_card(key)
            cards[key] = card
        state = card["state"]
        phase = str(event.get("phase") or state.get("phase") or "started").casefold()
        state["phase"] = {
            "complete": "completed", "success": "completed", "succeeded": "completed", "done": "completed",
            "error": "failed", "declined": "failed", "cancelled": "failed", "canceled": "failed",
            "in_progress": "started", "running": "started", "pending": "started",
        }.get(phase, phase if phase in {"started", "update", "completed", "failed"} else "completed")
        for field in ("activity_type", "title", "detail", "command", "cwd", "output", "summary", "diff", "error"):
            value = event.get(field)
            if value not in (None, ""):
                state[field] = str(value)
        if event.get("delta"):
            stream = str(event.get("stream") or "output")
            target = {"summary": "summary", "plan": "detail"}.get(stream, "output")
            state[target] = f"{state.get(target, '')}{event['delta']}"
        for field in ("files", "steps"):
            if isinstance(event.get(field), list) and event[field]:
                state[field] = event[field]
        if isinstance(event.get("changes"), list) and event["changes"]:
            state["files"] = [str(change.get("path") or "") for change in event["changes"] if change.get("path")]
            state["diff"] = "\n".join(str(change.get("diff") or "") for change in event["changes"] if change.get("diff"))
        if event.get("exit_code") is not None:
            state["exit_code"] = event["exit_code"]
        if event.get("duration_ms") is not None:
            state["duration_ms"] = event["duration_ms"]
        self._code_paint_activity(card)
        self._code_chat_scroll_end()
        return card

    def _card_alive(self, card):
        try:
            return bool(card.get("frame")) and card["frame"].winfo_exists()
        except (AttributeError, tk.TclError):
            return False

    def _code_build_activity_card(self, key):
        row = self._code_chat_row()
        bg = self.blend_color(self.CODE_CHAT_BG, "#ffffff", 0.05)
        frame = tk.Frame(row, bg=bg, highlightthickness=1, highlightbackground=self.blend_color(bg, "#ffffff", 0.09))
        frame.pack(fill="x", anchor="w", padx=(2, 40))
        header = tk.Frame(frame, bg=bg, cursor="hand2")
        header.pack(fill="x", padx=9, pady=7)
        icon_var = tk.StringVar(master=self.root, value=CODE_SPINNER_FRAMES[0])
        title_var = tk.StringVar(master=self.root, value="Working")
        preview_var = tk.StringVar(master=self.root, value="")
        meta_var = tk.StringVar(master=self.root, value="")
        chevron = tk.Label(header, text="▸", bg=bg, fg=self.c("muted"), font=self.font(8, "bold"), cursor="hand2")
        chevron.pack(side="left")
        icon = tk.Label(header, textvariable=icon_var, bg=bg, fg=self.c("muted"), font=self.font(9, "bold"), width=2, cursor="hand2")
        icon.pack(side="left", padx=(5, 6))
        copy = tk.Frame(header, bg=bg, cursor="hand2")
        copy.pack(side="left", fill="x", expand=True)
        title = tk.Label(copy, textvariable=title_var, bg=bg, fg=self.c("text"), font=self.font(8, "bold"), anchor="w", cursor="hand2")
        title.pack(fill="x")
        preview = tk.Label(copy, textvariable=preview_var, bg=bg, fg=self.c("muted"), font=("Cascadia Code", max(7, int(self.c("font_size")) - 3)), anchor="w", cursor="hand2")
        preview.pack(fill="x")
        meta = tk.Label(header, textvariable=meta_var, bg=bg, fg=self.c("muted"), font=self.font(7), cursor="hand2")
        meta.pack(side="right")
        body = tk.Frame(frame, bg=bg)
        card = {
            "key": key,
            "frame": frame,
            "header": header,
            "bg": bg,
            "icon": icon,
            "icon_var": icon_var,
            "title_var": title_var,
            "preview_var": preview_var,
            "meta_var": meta_var,
            "chevron": chevron,
            "body": body,
            "open": False,
            "state": {"phase": "started", "activity_type": "tool", "title": "Working", "files": [], "steps": []},
        }
        for widget in (header, chevron, icon, copy, title, preview, meta):
            widget.bind("<Button-1>", lambda _event, item=card: self._code_toggle_activity(item))
        return card

    def _code_toggle_activity(self, card):
        try:
            self.code_chat.canvas.update_idletasks()
            bounds = self.code_chat.canvas.bbox("all")
            old_top = float(self.code_chat.canvas.canvasy(0)) - float(bounds[1] if bounds else 0)
        except (tk.TclError, TypeError, ValueError):
            old_top = 0.0
        card["open"] = not card["open"]
        card["chevron"].configure(text="▾" if card["open"] else "▸")
        if card["open"]:
            self._code_fill_activity_body(card)
            card["body"].pack(fill="x", padx=9, pady=(0, 8))
        else:
            card["body"].pack_forget()
        # Expanding details is a reading action. Preserve the viewport instead
        # of snapping to the newest output at the bottom.
        try:
            self.code_chat.canvas.update_idletasks()
            bounds = self.code_chat.canvas.bbox("all")
            height = max(1.0, float(bounds[3] - bounds[1])) if bounds else 1.0
            self.code_chat.canvas.configure(scrollregion=bounds)
            self.code_chat.canvas.yview_moveto(max(0.0, min(1.0, old_top / height)))
            self.code_auto_follow = self._code_chat_near_bottom()
        except (tk.TclError, TypeError, ValueError):
            pass

    def _code_paint_activity(self, card):
        state = card["state"]
        phase = state.get("phase", "started")
        colors = {"completed": self.c("success"), "failed": self.c("danger")}
        card["icon"].configure(fg=colors.get(phase, self.c("accent")))
        if phase == "completed":
            card["icon_var"].set("✓")
        elif phase == "failed":
            card["icon_var"].set("×")
        card["title_var"].set(state.get("title") or "Working")
        preview = state.get("command") or state.get("detail") or ", ".join(state.get("files") or []) or state.get("summary") or ""
        preview = re.sub(r"\s+", " ", str(preview)).strip()
        card["preview_var"].set(preview[:110] + ("…" if len(preview) > 110 else ""))
        label = {"command": "terminal", "files": "file edit", "thinking": "reasoning"}.get(state.get("activity_type"), state.get("activity_type") or "tool")
        bits = []
        if state.get("duration_ms") is not None:
            bits.append(f"{max(0, int(state['duration_ms'])) / 1000:.1f}s")
        if state.get("exit_code") is not None:
            bits.append(f"exit {state['exit_code']}")
        bits.append(label)
        card["meta_var"].set(" · ".join(bits))
        if card["open"]:
            self._code_fill_activity_body(card)

    def _code_fill_activity_body(self, card):
        body = card["body"]
        for child in list(body.winfo_children()):
            try:
                child.destroy()
            except tk.TclError:
                pass
        state = card["state"]
        bg = card["bg"]
        sections = [
            ("Command", state.get("command"), True),
            ("Folder", state.get("cwd"), True),
            ("Files", "\n".join(state.get("files") or []), True),
            ("Reasoning", state.get("summary") or (state.get("detail") if state.get("activity_type") == "thinking" else ""), False),
            ("Error" if state.get("phase") == "failed" else "Output", state.get("output"), True),
            ("Error detail", state.get("error"), True),
            ("Diff", state.get("diff"), True),
        ]
        shown = 0
        for label, value, mono in sections:
            text = str(value or "").strip()
            if not text:
                continue
            shown += 1
            tk.Label(body, text=label.upper(), bg=bg, fg=self.c("muted"), font=self.font(7, "bold"), anchor="w").pack(fill="x", pady=(6, 2))
            if len(text) > 8000:
                text = "…\n" + text[-8000:]
            block_bg = self.blend_color(bg, "#000000", 0.35)
            widget = self._code_text_widget(body, bg=block_bg, fg="#d3dee6" if mono else self.c("text"), mono=mono)
            widget.configure(padx=8, pady=6)
            widget.pack(fill="x")
            if mono:
                widget.configure(state="normal")
                widget.insert("1.0", text)
                widget.configure(state="disabled")
                self._code_fit_text(widget)
            else:
                self._code_write_markdown(widget, text)
        for step in state.get("steps") or []:
            if not shown:
                tk.Label(body, text="PLAN", bg=bg, fg=self.c("muted"), font=self.font(7, "bold"), anchor="w").pack(fill="x", pady=(6, 2))
                shown += 1
            status = str(step.get("status") or "pending") if isinstance(step, dict) else "pending"
            glyph = {"completed": "✓", "in_progress": "◌", "inProgress": "◌"}.get(status, "○")
            label = str(step.get("step") or step.get("text") or "") if isinstance(step, dict) else str(step)
            tk.Label(body, text=f"{glyph}  {label}", bg=bg, fg=self.c("muted"), font=self.font(8), anchor="w", justify="left", wraplength=self._code_chat_width() - 60).pack(fill="x")
        if not shown and not (state.get("steps") or []):
            tk.Label(
                body,
                text="Live details will appear here." if state.get("phase") in {"started", "update"} else "No additional output.",
                bg=bg,
                fg=self.c("muted"),
                font=self.font(7),
                anchor="w",
            ).pack(fill="x", pady=(4, 2))

    # ---- live spinner ------------------------------------------------------

    def _code_set_busy(self, active, label="Working"):
        if not self._code_chat_live():
            return
        row = getattr(self, "code_busy_row", None)
        alive = False
        try:
            alive = row is not None and row.winfo_exists()
        except tk.TclError:
            alive = False
        if not active:
            if alive:
                row.destroy()
            self.code_busy_row = None
            self.code_busy_label = None
            return
        if not alive:
            row = tk.Frame(self.code_chat_inner, bg=self.CODE_CHAT_BG)
            row.pack(fill="x", padx=8, pady=(6, 8))
            self.code_busy_row = row
            self.code_busy_icon = tk.StringVar(master=self.root, value=CODE_SPINNER_FRAMES[0])
            tk.Label(row, textvariable=self.code_busy_icon, bg=self.CODE_CHAT_BG, fg=self.c("accent"), font=self.font(9, "bold")).pack(side="left", padx=(2, 7))
            self.code_busy_label = tk.Label(row, text=label, bg=self.CODE_CHAT_BG, fg=self.c("muted"), font=self.font(8, "italic"), anchor="w")
            self.code_busy_label.pack(side="left")
            self._code_spin_start()
        else:
            try:
                self.code_busy_label.configure(text=label)
            except (AttributeError, tk.TclError):
                pass

    def _code_spin_start(self):
        if getattr(self, "code_spin_after", None) is not None:
            return
        self.code_spin_step = 0
        self._code_spin()

    def _code_spin(self):
        self.code_spin_after = None
        if not self._code_chat_live():
            return
        self.code_spin_step = (getattr(self, "code_spin_step", 0) + 1) % len(CODE_SPINNER_FRAMES)
        glyph = CODE_SPINNER_FRAMES[self.code_spin_step]
        try:
            if getattr(self, "code_busy_row", None) is not None and self.code_busy_row.winfo_exists():
                self.code_busy_icon.set(glyph)
        except (AttributeError, tk.TclError):
            pass
        for card in list(getattr(self, "code_activity_cards", {}).values()):
            if card["state"].get("phase") in {"started", "update"} and self._card_alive(card):
                try:
                    card["icon_var"].set(glyph)
                except tk.TclError:
                    pass
        try:
            self.code_spin_after = self.root.after(110, self._code_spin)
        except tk.TclError:
            self.code_spin_after = None

    def _code_render_event(self, event, live=False):
        if not self._code_chat_live():
            return
        kind = str(event.get("kind") or "status")
        text = str(event.get("text") or kind)

        if kind == "result":
            assistant_text = re.sub(r"\s+", "", getattr(self, "code_turn_assistant_text", ""))
            result_text = re.sub(r"\s+", "", text)
            if assistant_text and result_text == assistant_text:
                self._code_close_stream()
                self.code_last_rendered_kind = "result"
                return
            # Some providers only emit a result. Show it as ordinary agent prose,
            # never as a second, specially labelled final-report card.
            kind = "assistant"

        if kind in {"activity", "tool", "thinking", "approval"}:
            self._code_close_stream()
            self._code_upsert_activity(self._code_activity_from_event(event))
            self.code_last_rendered_kind = "activity"
            return
        if kind == "provider_switch":
            self._code_close_stream()
            self._code_add_handoff(event)
            self.code_last_rendered_kind = kind
            return
        if kind in {"assistant", "assistant_delta"}:
            self._code_stream_assistant(str(event.get("delta") or text))
            self.code_last_rendered_kind = "assistant"
            return

        self._code_close_stream()
        if kind == "user":
            self.code_turn_assistant_text = ""
            self._code_add_user_bubble(text)
        elif kind == "status":
            self._code_add_status(text)
        else:
            self._code_add_message(text, kind)
        self.code_last_rendered_kind = kind

    def _code_set_speaking(self):
        self.config["code_speak_notifications"] = bool(self.code_speak_var.get())
        save_config(self.config)

    def _code_refresh_selectors(self):
        if not hasattr(self, "code_model_menu"):
            return
        provider = self.code_provider_var.get()
        info = next((row for row in self.code_capabilities.get("providers") or [] if row.get("provider") == provider), {})
        models = info.get("models") or []
        ids = [str(model.get("id")) for model in models if model.get("id")]
        if not ids:
            ids = {"codex": ["gpt-5.6-sol"], "claude": ["sonnet"], "cursor": ["composer-2.5"]}.get(provider, [])
        menu = self.code_model_menu["menu"]
        menu.delete(0, "end")
        for model_id in ids:
            menu.add_command(label=model_id, command=tk._setit(self.code_model_var, model_id))
        if self.code_model_var.get() not in ids and ids:
            self.code_model_var.set(ids[0])
        self._code_refresh_reasoning()

    def _code_refresh_reasoning(self):
        if not hasattr(self, "code_reasoning_menu"):
            return
        provider = self.code_provider_var.get()
        info = next((row for row in self.code_capabilities.get("providers") or [] if row.get("provider") == provider), {})
        model = next((row for row in info.get("models") or [] if row.get("id") == self.code_model_var.get()), {})
        efforts = [str(value) for value in model.get("reasoning") or ["medium"]]
        menu = self.code_reasoning_menu["menu"]
        menu.delete(0, "end")
        for effort in efforts:
            menu.add_command(label=effort, command=tk._setit(self.code_reasoning_var, effort))
        preferred = str(model.get("default_reasoning") or "medium")
        if self.code_reasoning_var.get() not in efforts:
            self.code_reasoning_var.set(preferred if preferred in efforts else efforts[0])
        self.code_fast_check.configure(state="normal" if model.get("fast") else "disabled")
        if not model.get("fast"):
            self.code_fast_var.set(False)
        if hasattr(self, "code_setup_button"):
            self.code_setup_button.configure(
                text="Ready" if info.get("ready") else f"Sign in {provider.title()}",
                state="disabled" if info.get("ready") else "normal",
            )

    def _code_start_job(self):
        brief = self.code_brief.get("1.0", "end").strip()
        if not brief:
            messagebox.showinfo("CODE", "Describe what the agent should build first.")
            return
        provider = self.code_provider_var.get()
        info = next((row for row in self.code_capabilities.get("providers") or [] if row.get("provider") == provider), {})
        if info and not info.get("ready"):
            messagebox.showerror("CODE", info.get("message") or f"{provider.title()} is not ready.")
            return
        attachments = [{"path": path, "label": Path(path).name} for path in self.code_create_attachments]
        attachments.extend({"url": url, "label": url} for url in re.findall(r"https?://[^\s)]+", brief))
        payload = {
            "provider": provider,
            "cwd": self.code_project_var.get().strip(),
            "brief": brief,
            "model": self.code_model_var.get(),
            "reasoning": self.code_reasoning_var.get(),
            "fast": self.code_fast_var.get(),
            "attachments": attachments,
        }
        self.code_health_var.set(f"Launching {provider.title()}…")

        def worker():
            result = self._code_api("/api/code/jobs", "POST", payload, timeout=40)
            self.root.after(0, lambda: self._code_started(result))

        threading.Thread(target=worker, daemon=True, name="aios-code-start").start()

    def _code_started(self, result):
        if not result.get("ok"):
            messagebox.showerror("CODE", result.get("error") or "Could not start the CODE job.")
            return
        self.code_brief.delete("1.0", "end")
        self.code_create_attachments = []
        self.code_attach_button.configure(text="Attach")
        self.code_selected_id = str((result.get("job") or {}).get("id") or "")
        self.code_log_size = 0
        self._code_sessions_signature = None
        self._code_refresh_all()

    def _code_send_followup(self):
        if not self.code_selected_id:
            messagebox.showinfo("CODE", "Select a session first.")
            return
        text = self.code_followup.get("1.0", "end").strip()
        if not text:
            return
        payload = {
            "text": text,
            "urgent": self.code_urgent_var.get(),
            "attachments": [{"path": path, "label": Path(path).name} for path in self.code_followup_attachments],
        }
        self.code_followup.delete("1.0", "end")
        self.code_followup_attachments = []
        self.code_follow_attach_button.configure(text="+ File")
        self.code_urgent_var.set(False)
        job_id = self.code_selected_id

        def worker():
            result = self._code_api(f"/api/code/jobs/{job_id}/messages", "POST", payload)
            if not result.get("ok"):
                self.root.after(0, lambda: messagebox.showerror("CODE", result.get("error") or "Could not send the follow-up."))

        threading.Thread(target=worker, daemon=True, name="aios-code-followup").start()

    def _code_stop_job(self):
        if not self.code_selected_id:
            return
        job_id = self.code_selected_id
        threading.Thread(target=lambda: self._code_api(f"/api/code/jobs/{job_id}/stop", "POST", {}), daemon=True).start()

    def _code_delete_job(self):
        if not self.code_selected_id or not messagebox.askyesno("Delete CODE session", "Delete this session and transcript? Project files stay untouched."):
            return
        job_id = self.code_selected_id

        def worker():
            result = self._code_api(f"/api/code/jobs/{job_id}", "DELETE", {"confirm": job_id})
            def finish():
                if not result.get("ok"):
                    messagebox.showerror("CODE", result.get("error") or "Could not delete the session.")
                    return
                if self.code_selected_id == job_id:
                    self.code_selected_id = ""
                    self.code_log_size = 0
                self._code_refresh_all()

            self.root.after(0, finish)

        threading.Thread(target=worker, daemon=True, name="aios-code-delete").start()

    def render_apps(self):
        self.page_title("Apps")
        search = self.card(self.page)
        search.pack(fill="x", pady=(0, 12))
        self.app_search = self.entry(search)
        self.app_search.pack(side="left", fill="x", expand=True, padx=12, pady=12)
        self.app_search.bind("<KeyRelease>", lambda _event: self.refresh_app_results())
        self.button(search, "Search", self.refresh_app_results, compact=True).pack(side="right", padx=12, pady=12)

        self.apps_area = ScrollFrame(self.page, self.c("panel"))
        self.apps_area.pack(fill="both", expand=True)
        self.refresh_app_results()

    def render_drop(self):
        self.page_title("Drop")
        self.drop_zone(self.page).pack(fill="x", pady=(0, 12))

        card = self.card(self.page)
        card.pack(fill="x", pady=(0, 12))
        self.section(card, "Import")
        row = tk.Frame(card, bg=self.c("surface"))
        row.pack(fill="x", padx=12, pady=(0, 12))
        self.drop_project_name = self.single_line(row, self.drop_default_project_name())
        self.drop_project_name.pack(side="left", fill="x", expand=True)
        self.button(row, "New From Drop", self.new_project_from_drop, compact=True).pack(side="right", padx=(8, 0))

        existing = self.card(self.page)
        existing.pack(fill="both", expand=True)
        self.section(existing, "Add To Existing Project")
        row2 = tk.Frame(existing, bg=self.c("surface"))
        row2.pack(fill="x", padx=12, pady=(0, 10))
        self.drop_existing_entry = self.single_line(row2, self.default_project_path().name if self.default_project_path() else "")
        self.drop_existing_entry.pack(side="left", fill="x", expand=True)
        self.button(row2, "Add", self.add_drop_to_existing_project, compact=True).pack(side="right", padx=(8, 0))
        paths = "\n".join(str(path) for path in self.dropped_paths) or "Drop files or folders anywhere on this window."
        tk.Label(
            existing,
            text=paths,
            bg=self.c("surface"),
            fg=self.c("muted"),
            justify="left",
            anchor="nw",
            font=self.font(9),
        ).pack(fill="both", expand=True, padx=12, pady=(0, 12))

    def render_ai_operator(self):
        if not self.agent_operator_booted and not self.agent_operator_error:
            self.render_ai_operator_boot()
            return

        head = tk.Frame(self.page, bg=self.c("panel"))
        head.pack(fill="x", pady=(0, 12))
        tk.Label(
            head,
            text="OPERATOR",
            bg=self.c("panel"),
            fg=self.c("text"),
            font=self.brand_font(18),
        ).pack(side="left")

        if not self._ensure_agent_operator():
            card = self.card(self.page)
            card.pack(fill="x")
            self.section(card, "Unavailable")
            error = self.agent_operator_error or "Agent Clicker could not be loaded."
            tk.Label(
                card,
                text=f"{error}\n{self.agent_clicker_dir}",
                bg=self.c("surface"),
                fg=self.c("danger"),
                justify="left",
                anchor="w",
                font=self.font(9),
            ).pack(fill="x", padx=12, pady=(0, 12))
            return

        self.agent_operator_monitors = self._agent_operator_list_monitors()
        physical_monitors = self._agent_operator_physical_monitors()
        selected = self.agent_operator_monitor_var.get() if self.agent_operator_monitor_var else str(self.agent_operator_settings.get("monitor", ""))
        if not selected and physical_monitors:
            selected = physical_monitors[0].label

        self.agent_operator_status_var = tk.StringVar(value=self._agent_operator_status_text())
        self.agent_operator_step_var = tk.StringVar(value="")
        self.agent_operator_monitor_var = tk.StringVar(value=selected)
        self.agent_operator_model_var = tk.StringVar(value=self.agent_operator_model_var.get() if self.agent_operator_model_var else str(self.agent_operator_settings.get("model") or self.agent_operator_default_model))
        self.agent_operator_planner_model_var = tk.StringVar(value=self.agent_operator_planner_model_var.get() if self.agent_operator_planner_model_var else str(self.agent_operator_settings.get("planner_model") or "gpt-5.6-sol"))
        self.agent_operator_reason_var = tk.StringVar(value=self.agent_operator_reason_var.get() if self.agent_operator_reason_var else str(self.agent_operator_settings.get("reasoning") or "low"))
        self.agent_operator_steps_var = tk.StringVar(value=self.agent_operator_steps_var.get() if self.agent_operator_steps_var else str(self.agent_operator_settings.get("steps") or "25"))
        self.agent_operator_delay_var = tk.StringVar(value=self.agent_operator_delay_var.get() if self.agent_operator_delay_var else str(self.agent_operator_settings.get("delay") or "0.20"))
        self.agent_operator_tts_var = tk.BooleanVar(value=self.agent_operator_tts_var.get() if self.agent_operator_tts_var else bool(self.agent_operator_settings.get("tts", False)))
        self.agent_operator_voice_var = tk.StringVar(value=self.agent_operator_voice_var.get() if self.agent_operator_voice_var else str(self.agent_operator_settings.get("voice") or self.agent_operator_default_voice))
        self.agent_operator_shell_var = tk.BooleanVar(value=self.agent_operator_shell_var.get() if self.agent_operator_shell_var else bool(self.agent_operator_settings.get("shell", False)))
        self.agent_operator_codex_var = tk.BooleanVar(value=self.agent_operator_codex_var.get() if self.agent_operator_codex_var else bool(self.agent_operator_settings.get("codex_auth", False)))

        controls = self.card(self.page)
        controls.pack(fill="x", pady=(0, 12))

        model_row = tk.Frame(controls, bg=self.c("surface"))
        model_row.pack(fill="x", padx=12, pady=(12, 10))
        tk.Label(model_row, text="Model", bg=self.c("surface"), fg=self.c("text"), font=self.font(10, "bold")).pack(side="left")
        model_choices = ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"]
        if self.agent_operator_model_var.get() not in model_choices:
            model_choices.insert(0, self.agent_operator_model_var.get())
        model_menu = tk.OptionMenu(
            model_row,
            self.agent_operator_model_var,
            *model_choices,
            command=lambda _value: self.save_agent_operator_settings(),
        )
        self.style_option(model_menu)
        model_menu.pack(side="left", fill="x", expand=True, padx=(10, 10))
        self.agent_operator_model_entry = None
        self.agent_operator_advanced_btn = self.button(
            model_row,
            "Advanced ▾" if self.agent_operator_advanced_open else "Advanced ▸",
            self.toggle_agent_operator_advanced,
            compact=True,
            active=self.agent_operator_advanced_open,
        )
        self.agent_operator_advanced_btn.pack(side="right")

        self.agent_operator_advanced_frame = tk.Frame(controls, bg=self.c("surface"))
        if self.agent_operator_advanced_open:
            self.agent_operator_advanced_frame.pack(fill="x", padx=12, pady=(0, 8))

        row1 = tk.Frame(self.agent_operator_advanced_frame, bg=self.c("surface"))
        row1.pack(fill="x", pady=(0, 6))
        tk.Label(row1, text="Monitor", bg=self.c("surface"), fg=self.c("muted"), font=self.font(9, "bold")).pack(side="left")
        monitor_labels = [m.label for m in physical_monitors] or ["No monitors"]
        if self.agent_operator_monitor_var.get() not in monitor_labels:
            self.agent_operator_monitor_var.set(monitor_labels[0])
        monitor_menu = tk.OptionMenu(row1, self.agent_operator_monitor_var, *monitor_labels)
        self.style_option(monitor_menu)
        monitor_menu.pack(side="left", fill="x", expand=True, padx=(8, 8))
        self.agent_operator_monitor_var.trace_add("write", lambda *_args: self.save_agent_operator_settings())
        monitor_actions = tk.Frame(row1, bg=self.c("surface"))
        monitor_actions.pack(side="right")
        self.button(monitor_actions, "Preview", self.agent_operator_preview_monitor, compact=True).pack(side="left", padx=(6, 0))
        self.button(monitor_actions, "Test Cursor", self.agent_operator_test_cursor, compact=True).pack(side="left", padx=(6, 0))

        planner_row = tk.Frame(self.agent_operator_advanced_frame, bg=self.c("surface"))
        planner_row.pack(fill="x", pady=(0, 6))
        tk.Label(planner_row, text="Planning model", bg=self.c("surface"), fg=self.c("muted"), font=self.font(9, "bold")).pack(side="left")
        planner_choices = ["off", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"]
        if self.agent_operator_planner_model_var.get() not in planner_choices:
            planner_choices.insert(0, self.agent_operator_planner_model_var.get())
        planner_menu = tk.OptionMenu(
            planner_row,
            self.agent_operator_planner_model_var,
            *planner_choices,
            command=lambda _value: self.save_agent_operator_settings(),
        )
        self.style_option(planner_menu)
        planner_menu.pack(side="left", fill="x", expand=True, padx=(8, 0))

        row2 = tk.Frame(self.agent_operator_advanced_frame, bg=self.c("surface"))
        row2.pack(fill="x", pady=(0, 6))
        tk.Label(row2, text="Reasoning", bg=self.c("surface"), fg=self.c("muted"), font=self.font(9, "bold")).pack(side="left")
        reason_menu = tk.OptionMenu(
            row2,
            self.agent_operator_reason_var,
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
            command=lambda _value: self.save_agent_operator_settings(),
        )
        self.style_option(reason_menu)
        reason_menu.pack(side="left", padx=(8, 12))
        self.agent_operator_reason_var.trace_add("write", lambda *_args: self.save_agent_operator_settings())
        tk.Label(row2, text="Steps", bg=self.c("surface"), fg=self.c("muted"), font=self.font(9, "bold")).pack(side="left")
        tk.Spinbox(
            row2,
            from_=1,
            to=200,
            textvariable=self.agent_operator_steps_var,
            width=5,
            bg=self.c("panel2"),
            fg=self.c("text"),
            insertbackground=self.c("text"),
            buttonbackground=self.c("surface2"),
            relief="flat",
            bd=0,
            font=self.font(9),
        ).pack(side="left", padx=(8, 12))
        self.agent_operator_steps_var.trace_add("write", lambda *_args: self.save_agent_operator_settings())
        tk.Label(row2, text="Delay", bg=self.c("surface"), fg=self.c("muted"), font=self.font(9, "bold")).pack(side="left")
        tk.Spinbox(
            row2,
            from_=0.0,
            to=3.0,
            increment=0.05,
            textvariable=self.agent_operator_delay_var,
            width=5,
            format="%.2f",
            bg=self.c("panel2"),
            fg=self.c("text"),
            insertbackground=self.c("text"),
            buttonbackground=self.c("surface2"),
            relief="flat",
            bd=0,
            font=self.font(9),
        ).pack(side="left", padx=(8, 0))
        self.agent_operator_delay_var.trace_add("write", lambda *_args: self.save_agent_operator_settings())

        row2b = tk.Frame(self.agent_operator_advanced_frame, bg=self.c("surface"))
        row2b.pack(fill="x", pady=(0, 6))
        tts_check = tk.Checkbutton(
            row2b,
            text="TTS",
            variable=self.agent_operator_tts_var,
            command=self.agent_operator_toggle_tts,
            bg=self.c("surface"),
            activebackground=self.c("surface"),
            selectcolor=self.c("panel2"),
            fg=self.c("text"),
            activeforeground=self.c("text"),
            font=self.font(9, "bold"),
        )
        tts_check.pack(side="left")
        tk.Label(row2b, text="Voice", bg=self.c("surface"), fg=self.c("muted"), font=self.font(9, "bold")).pack(side="left", padx=(14, 8))
        voice_menu = tk.OptionMenu(
            row2b,
            self.agent_operator_voice_var,
            "nova",
            "coral",
            "shimmer",
            "sage",
            "alloy",
            "echo",
            "fable",
            "onyx",
            "ash",
            "ballad",
            "verse",
            command=lambda _value: self.agent_operator_set_voice(),
        )
        self.style_option(voice_menu)
        voice_menu.pack(side="left")
        if self.agent_operator_tts_error:
            tk.Label(
                row2b,
                text=self.agent_operator_tts_error,
                bg=self.c("surface"),
                fg=self.c("danger"),
                font=self.font(8),
            ).pack(side="left", padx=(12, 0))
        shell_check = tk.Checkbutton(
            row2b,
            text="Shell",
            variable=self.agent_operator_shell_var,
            command=self.agent_operator_toggle_shell,
            bg=self.c("surface"),
            activebackground=self.c("surface"),
            selectcolor=self.c("panel2"),
            fg=self.c("text"),
            activeforeground=self.c("text"),
            font=self.font(9, "bold"),
        )
        shell_check.pack(side="left", padx=(18, 0))
        codex_check = tk.Checkbutton(
            row2b,
            text="Codex Auth",
            variable=self.agent_operator_codex_var,
            command=self.agent_operator_toggle_codex,
            bg=self.c("surface"),
            activebackground=self.c("surface"),
            selectcolor=self.c("panel2"),
            fg=self.c("text"),
            activeforeground=self.c("text"),
            font=self.font(9, "bold"),
        )
        codex_check.pack(side="left", padx=(18, 0))
        if not self.agent_operator_codex_available:
            codex_check.configure(state="disabled", fg=self.c("muted"))
            if self.agent_operator_codex_var.get():
                self.agent_operator_codex_var.set(False)
                self.save_agent_operator_settings()
        login_text = "Switch account" if self.agent_operator_codex_available else "Sign in with Codex"
        self.agent_operator_codex_login_btn = self.button(
            row2b,
            login_text,
            self.agent_operator_start_codex_login,
            compact=True,
        )
        self.agent_operator_codex_login_btn.pack(side="left", padx=(8, 0))
        self.agent_operator_codex_status_label = tk.Label(
            row2b,
            text=self._agent_operator_codex_status_text(),
            bg=self.c("surface"),
            fg=self.c("success") if self.agent_operator_codex_available else self.c("muted"),
            font=self.font(8),
        )
        self.agent_operator_codex_status_label.pack(side="left", padx=(8, 0))

        task_panel = tk.Frame(controls, bg=self.c("panel2"), highlightbackground="#2a3a50", highlightthickness=1, bd=0)
        self.agent_operator_task_panel = task_panel
        task_panel.pack(fill="x", padx=12, pady=(4, 12))
        task_head = tk.Frame(task_panel, bg=self.c("panel2"))
        task_head.pack(fill="x", padx=10, pady=(8, 0))
        tk.Label(task_head, text="Task", bg=self.c("panel2"), fg=self.c("text"), font=self.font(10, "bold")).pack(side="left")
        self.button(task_head, "Attach", self.agent_operator_attach_files, compact=True).pack(side="right", padx=(6, 0))
        self.button(task_head, "Clear", self.agent_operator_clear_input, compact=True).pack(side="right", padx=(6, 0))
        actions = tk.Frame(task_head, bg=self.c("panel2"))
        actions.pack(side="right", padx=(10, 0))
        self.agent_operator_run_btn = self.button(actions, "Run", self.agent_operator_run, compact=True)
        self.agent_operator_run_btn.pack(side="left", padx=(0, 6))
        self.agent_operator_followup_btn = self.button(actions, "Send follow-up", self.agent_operator_send_followup, compact=True)
        self.agent_operator_followup_btn.pack(side="left", padx=(0, 6))
        self.agent_operator_pause_btn = self.button(actions, "Pause", self.agent_operator_toggle_pause, compact=True)
        self.agent_operator_pause_btn.pack(side="left", padx=(0, 6))
        self.agent_operator_stop_btn = self.button(actions, "Stop", self.agent_operator_stop, compact=True)
        self.agent_operator_stop_btn.pack(side="left")
        self.agent_operator_clear_run_btn = self.button(actions, "Clear operator", self.agent_operator_clear_run, compact=True)
        self.agent_operator_clear_run_btn.pack(side="left", padx=(6, 0))
        self.agent_operator_task = tk.Text(
            task_panel,
            height=4,
            bg="#080d14",
            fg=self.c("text"),
            insertbackground=self.c("text"),
            selectbackground="#29415d",
            relief="flat",
            bd=0,
            padx=10,
            pady=8,
            wrap="word",
            undo=True,
            font=self.font(10),
        )
        self.agent_operator_task.pack(fill="x", padx=10, pady=(8, 6))
        self.agent_operator_task.bind("<Return>", self.agent_operator_task_enter)
        self.agent_operator_task.bind("<Shift-Return>", lambda _event: None)
        self.agent_operator_task.bind("<Control-Return>", self.agent_operator_task_enter)
        self.agent_operator_task.bind("<Control-v>", self.agent_operator_task_paste)
        self.agent_operator_task.bind("<Control-V>", self.agent_operator_task_paste)
        self.agent_operator_attach_strip = tk.Frame(task_panel, bg=self.c("panel2"))
        self.agent_operator_attach_strip.pack(fill="x", padx=8, pady=(0, 8))
        self._agent_operator_render_attachments()

        prompt_panel = tk.Frame(self.agent_operator_advanced_frame, bg=self.c("surface"))
        prompt_panel.pack(fill="x", pady=(6, 4))
        prompt_head = tk.Frame(prompt_panel, bg=self.c("surface"))
        prompt_head.pack(fill="x", pady=(0, 6))
        tk.Label(prompt_head, text="Prompts", bg=self.c("surface"), fg=self.c("text"), font=self.font(10, "bold")).pack(side="left")
        self.agent_operator_context_status_var = tk.StringVar(value="")
        tk.Label(prompt_head, textvariable=self.agent_operator_context_status_var, bg=self.c("surface"), fg=self.c("muted"), font=self.font(8)).pack(side="left", padx=(10, 0))
        self.button(prompt_head, "New", self.agent_operator_context_new, compact=True).pack(side="right", padx=(6, 0))
        self.button(prompt_head, "Rename", self.agent_operator_context_rename, compact=True).pack(side="right", padx=(6, 0))
        self.button(prompt_head, "Delete", self.agent_operator_context_delete, compact=True).pack(side="right", padx=(6, 0))
        prompt_body = tk.Frame(prompt_panel, bg=self.c("surface"))
        prompt_body.pack(fill="x")
        self.agent_operator_context_list = tk.Listbox(
            prompt_body,
            height=5,
            bg="#080d14",
            fg=self.c("text"),
            selectbackground=self.c("accent"),
            selectforeground="#061018",
            borderwidth=0,
            highlightthickness=0,
            activestyle="none",
            font=self.font(9),
        )
        self.agent_operator_context_list.pack(side="left", fill="y", padx=(0, 8))
        self.agent_operator_context_list.bind("<<ListboxSelect>>", self.agent_operator_context_select)
        editor_wrap = tk.Frame(prompt_body, bg="#080d14", highlightbackground="#1c2b3d", highlightthickness=1, bd=0)
        editor_wrap.pack(side="left", fill="both", expand=True)
        self.agent_operator_context_file_var = tk.StringVar(value="")
        tk.Label(editor_wrap, textvariable=self.agent_operator_context_file_var, bg="#080d14", fg=self.c("muted"), font=self.font(8, "bold")).pack(anchor="w", padx=8, pady=(6, 0))
        self.agent_operator_context_editor = tk.Text(
            editor_wrap,
            height=5,
            bg="#080d14",
            fg=self.c("text"),
            insertbackground=self.c("text"),
            selectbackground="#29415d",
            relief="flat",
            bd=0,
            padx=8,
            pady=6,
            wrap="word",
            undo=True,
            font=self.font(9),
        )
        self.agent_operator_context_editor.pack(fill="both", expand=True)
        self.agent_operator_context_editor.bind("<<Modified>>", self.agent_operator_context_modified)
        self._agent_operator_context_refresh()

        split = tk.Frame(self.page, bg=self.c("panel"))
        split.pack(fill="both", expand=True)
        preview_card = self.card(split)
        preview_card.pack(side="left", fill="both", expand=True, padx=(0, 6))
        preview_head = tk.Frame(preview_card, bg=self.c("surface"))
        preview_head.pack(fill="x", padx=12, pady=(10, 6))
        tk.Label(preview_head, text="Screen", bg=self.c("surface"), fg=self.c("text"), font=self.font(10, "bold")).pack(side="left")
        tk.Label(preview_head, textvariable=self.agent_operator_step_var, bg=self.c("surface"), fg=self.c("muted"), font=self.font(8)).pack(side="right")
        self.agent_operator_canvas = tk.Canvas(preview_card, bg="#080d14", highlightthickness=0, bd=0)
        self.agent_operator_canvas.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.agent_operator_canvas.bind("<Configure>", lambda _event: self._agent_operator_redraw_preview())

        log_card = self.card(split)
        log_card.pack(side="right", fill="both", padx=(6, 0))
        log_card.configure(width=430)
        log_card.pack_propagate(False)
        log_head = tk.Frame(log_card, bg=self.c("surface"))
        log_head.pack(fill="x", padx=12, pady=(10, 6))
        tk.Label(log_head, text="Activity", bg=self.c("surface"), fg=self.c("text"), font=self.font(10, "bold")).pack(side="left")
        self.button(log_head, "Clear", self.agent_operator_clear_log, compact=True).pack(side="right")
        self.agent_operator_log = tk.Text(
            log_card,
            bg="#080d14",
            fg=self.c("text"),
            insertbackground=self.c("text"),
            selectbackground="#29415d",
            relief="flat",
            bd=0,
            padx=12,
            pady=10,
            wrap="word",
            font=("Consolas", max(8, int(self.c("font_size")) - 1)),
            state="disabled",
        )
        self.agent_operator_log.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        for tag, color in (
            ("ts", self.c("muted")),
            ("thought", "#9ec3ff"),
            ("action", "#c8e69a"),
            ("ok", self.c("success")),
            ("err", self.c("danger")),
            ("status", "#ffd479"),
            ("step", self.c("accent")),
            ("dim", self.c("muted")),
        ):
            self.agent_operator_log.tag_configure(tag, foreground=color)
        self._agent_operator_render_log_buffer()
        status = tk.Label(
            log_card,
            textvariable=self.agent_operator_status_var,
            bg=self.c("surface"),
            fg=self.c("muted"),
            font=self.font(8),
            anchor="w",
        )
        status.pack(fill="x", padx=12, pady=(0, 10))
        self._agent_operator_sync_buttons()
        self._agent_operator_redraw_preview()

    def render_ai_operator_boot(self):
        boot = tk.Frame(self.page, bg=self.c("panel"))
        boot.pack(fill="both", expand=True)
        center = tk.Frame(boot, bg=self.c("panel"))
        center.place(relx=0.5, rely=0.42, anchor="center")
        tk.Label(
            center,
            text="OPERATOR",
            bg=self.c("panel"),
            fg=self.c("text"),
            font=self.brand_font(26),
        ).pack(anchor="center", pady=(0, 10))
        tk.Label(
            center,
            text="Preparing desktop controls…",
            bg=self.c("panel"),
            fg=self.c("muted"),
            font=self.font(9),
        ).pack(anchor="center")
        self._start_agent_operator_load()

    def _start_agent_operator_load(self):
        if self.agent_operator_imported or self.agent_operator_booting or self.agent_operator_error:
            return
        self.agent_operator_booting = True

        def worker():
            ok = self._ensure_agent_operator()
            self.agent_operator_event_q.put({"type": "operator_boot_ready", "ok": ok})

        threading.Thread(target=worker, daemon=True, name="aiOS-operator-loader").start()

    def _ensure_agent_operator(self):
        if self.agent_operator_imported:
            return True
        if self.agent_operator_error:
            return False
        if threading.current_thread().name != "aiOS-operator-loader":
            self._start_agent_operator_load()
            return False
        if not self.agent_clicker_dir.exists():
            self.agent_operator_error = "Agent Clicker folder is missing."
            return False
        try:
            self._load_agent_operator_env()
            path = str(self.agent_clicker_dir)
            if path not in sys.path:
                sys.path.insert(0, path)
            from agent.config import MODEL as default_model
            from desktop_agent import loop as agent_loop_module
            from desktop_agent.loop import AgentLoop
            from desktop_agent.screen import capture as raw_capture, list_monitors
            from desktop_agent.actions import execute as exec_action, release_all
            from PIL import Image, ImageDraw, ImageTk, ImageGrab
            self.agent_operator_AgentLoop = AgentLoop
            self.agent_operator_loop_module = agent_loop_module
            self.agent_operator_raw_capture = raw_capture
            self.agent_operator_capture = self._agent_operator_capture_clean
            self.agent_operator_exec_action = exec_action
            self.agent_operator_release_all = release_all
            self.agent_operator_list_monitors_fn = list_monitors
            self.agent_operator_Image = Image
            self.agent_operator_ImageDraw = ImageDraw
            self.agent_operator_ImageTk = ImageTk
            self.agent_operator_ImageGrab = ImageGrab
            self.agent_operator_default_model = default_model
            agent_loop_module.capture = self._agent_operator_capture_clean
            self.agent_operator_loop = AgentLoop(self._agent_operator_enqueue)
            try:
                from desktop_agent.tts import DEFAULT_VOICE, TTSPlayer
                self.agent_operator_default_voice = DEFAULT_VOICE
                self.agent_operator_tts = TTSPlayer(
                    on_log=lambda _level, message: self._agent_operator_enqueue({"type": "log", "msg": message})
                )
                self.agent_operator_tts.enable(False)
                self.agent_operator_tts_error = None
            except Exception as exc:
                self.agent_operator_tts = None
                self.agent_operator_tts_error = f"TTS unavailable: {exc}"
            self.agent_operator_imported = True
            return True
        except Exception as exc:
            self.agent_operator_error = str(exc)
            return False

    def _load_agent_operator_env(self):
        env_path = self.agent_clicker_dir / ".env"
        if env_path.exists():
            for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and value and not os.environ.get(key):
                    os.environ[key] = value
        self._sync_agent_operator_api_key()

    def _sync_agent_operator_api_key(self, *, force_config=False):
        """Refresh the live OpenAI client after the phone changes its key."""
        key = (
            str(self.config.get("openai_api_key") or "").strip()
            if force_config
            else self.get_openai_api_key()
        )
        if key:
            os.environ["OPENAI_API_KEY"] = key
        else:
            os.environ.pop("OPENAI_API_KEY", None)
        try:
            from agent import config as agent_config
            from agent import vlm
            agent_config.OPENAI_API_KEY = key
            vlm._client = None
        except Exception:
            pass

    def _refresh_agent_operator_codex_auth(self):
        try:
            from agent.codex_backend import auth_available
            ok, message = auth_available()
        except Exception as exc:
            ok, message = False, str(exc)
        self.agent_operator_codex_available = bool(ok)
        if ok:
            _signed_in, account_label = codex_auth_info()
            self.agent_operator_codex_message = account_label
        else:
            self.agent_operator_codex_message = message
        if not ok and self.agent_operator_codex_var:
            try:
                self.agent_operator_codex_var.set(False)
            except tk.TclError:
                pass
        return ok, message

    def _agent_operator_codex_status_text(self):
        if self.agent_operator_codex_available:
            return f"Codex: {self.agent_operator_codex_message or 'signed in'}"
        return self.agent_operator_codex_message or "Codex sign-in required"

    def _update_agent_operator_codex_controls(self):
        if self.agent_operator_codex_login_btn:
            try:
                text = "Switch account" if self.agent_operator_codex_available else "Sign in with Codex"
                self.agent_operator_codex_login_btn.configure(text=text, state="normal")
            except tk.TclError:
                pass
        if self.agent_operator_codex_status_label:
            try:
                self.agent_operator_codex_status_label.configure(
                    text=self._agent_operator_codex_status_text(),
                    fg=self.c("success") if self.agent_operator_codex_available else self.c("muted"),
                )
            except tk.TclError:
                pass

    def agent_operator_start_codex_login(self):
        auth_path = CODEX_HOME / "auth.json"
        try:
            self.agent_operator_codex_login_started_at = auth_path.stat().st_mtime
        except OSError:
            self.agent_operator_codex_login_started_at = 0.0
        if not launch_codex_login():
            self.agent_operator_codex_message = "Codex is not installed or could not be started"
            self._update_agent_operator_codex_controls()
            if self.agent_operator_status_var:
                self.agent_operator_status_var.set(self.agent_operator_codex_message)
            return
        if self.agent_operator_codex_login_btn:
            self.agent_operator_codex_login_btn.configure(text="Waiting for browser...", state="disabled")
        if self.agent_operator_status_var:
            self.agent_operator_status_var.set("Complete Codex sign-in in your browser")
        self._poll_agent_operator_codex_login(120)

    def _poll_agent_operator_codex_login(self, attempts_left):
        auth_path = CODEX_HOME / "auth.json"
        try:
            changed = auth_path.stat().st_mtime > self.agent_operator_codex_login_started_at
        except OSError:
            changed = False
        if changed:
            ok, message = self._refresh_agent_operator_codex_auth()
            if ok:
                if self.agent_operator_codex_var:
                    self.agent_operator_codex_var.set(True)
                self.save_agent_operator_settings()
                self._update_agent_operator_codex_controls()
                if self.agent_operator_status_var:
                    self.agent_operator_status_var.set("Codex signed in and enabled for OPERATOR")
                self.refresh_chat_account()
                self.agent_operator_codex_login_after = None
                return
            self.agent_operator_codex_message = message
        if attempts_left <= 0:
            self._refresh_agent_operator_codex_auth()
            self._update_agent_operator_codex_controls()
            if self.agent_operator_status_var:
                self.agent_operator_status_var.set("Codex sign-in was not completed")
            self.agent_operator_codex_login_after = None
            return
        self.agent_operator_codex_login_after = self.root.after(
            1500,
            lambda: self._poll_agent_operator_codex_login(attempts_left - 1),
        )

    def _agent_operator_list_monitors(self):
        try:
            return list(self.agent_operator_list_monitors_fn())
        except Exception as exc:
            self.agent_operator_error = str(exc)
            return []

    def _agent_operator_physical_monitors(self):
        physical = [monitor for monitor in self.agent_operator_monitors if getattr(monitor, "index", 0) != 0]
        return physical or list(self.agent_operator_monitors)

    def _agent_operator_selected_monitor(self):
        wanted = self.agent_operator_monitor_var.get() if self.agent_operator_monitor_var else ""
        monitors = self._agent_operator_physical_monitors()
        for monitor in monitors:
            if monitor.label == wanted:
                return monitor
        if monitors:
            return monitors[0]
        return None

    def _agent_operator_status_text(self):
        if self.agent_operator_loop and self.agent_operator_loop.is_running():
            return "Running"
        return "Idle"

    def _agent_operator_ui_sync(self, fn, timeout=0.5):
        if threading.current_thread() is threading.main_thread():
            fn()
            return True
        done = threading.Event()

        def run():
            try:
                fn()
            finally:
                done.set()

        self._ui_queue.put(run)
        return done.wait(timeout)

    def _aios_top_level_windows(self):
        if not sys.platform.startswith("win"):
            return []
        user32 = ctypes.windll.user32
        process_id = ctypes.windll.kernel32.GetCurrentProcessId()
        windows = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd, _lparam):
            owner_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
            if owner_pid.value == process_id:
                windows.append(hwnd)
            return True

        callback_ref = callback_type(callback)
        user32.EnumWindows(callback_ref, 0)
        return windows

    def _install_root_hit_test_passthrough(self):
        if not sys.platform.startswith("win") or self._root_native_wndproc is not None:
            return
        try:
            self.root.update_idletasks()
            user32 = ctypes.windll.user32
            child_hwnd = self.root.winfo_id()
            hwnd = user32.GetAncestor(child_hwnd, GA_ROOT) or child_hwnd
            wndproc_type = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t,
                wintypes.HWND,
                wintypes.UINT,
                ctypes.c_size_t,
                ctypes.c_ssize_t,
            )
            user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.GetWindowLongPtrW.restype = ctypes.c_void_p
            user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
            user32.SetWindowLongPtrW.restype = ctypes.c_void_p
            user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
            user32.CallWindowProcW.restype = ctypes.c_ssize_t
            user32.GetMessageExtraInfo.restype = ctypes.c_ssize_t
            original = user32.GetWindowLongPtrW(hwnd, GWLP_WNDPROC)

            def wndproc(window, message, wparam, lparam):
                if message == WM_NCHITTEST and self._operator_input_passthrough:
                    # Only OPERATOR's tagged SendInput packets pass through.
                    # Real mouse/touch input continues to reach aiOS normally.
                    if int(user32.GetMessageExtraInfo()) == OPERATOR_INPUT_TAG:
                        return HTTRANSPARENT
                return user32.CallWindowProcW(original, window, message, wparam, lparam)

            callback = wndproc_type(wndproc)
            if not user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, ctypes.cast(callback, ctypes.c_void_p)):
                return
            self._root_native_hwnd = hwnd
            self._root_original_wndproc = original
            self._root_native_wndproc = callback
        except (AttributeError, OSError, tk.TclError, ValueError):
            self._root_native_wndproc = None

    def _set_aios_agent_capture_hidden(self, hidden):
        """Toggle capture affinity only while aiOS itself grabs an agent frame."""
        if not sys.platform.startswith("win"):
            return False
        user32 = ctypes.windll.user32
        applied = False
        affinity = WDA_EXCLUDEFROMCAPTURE if hidden else WDA_NONE
        for hwnd in self._aios_top_level_windows():
            try:
                changed = bool(user32.SetWindowDisplayAffinity(hwnd, affinity))
                if hidden and not changed:
                    changed = bool(user32.SetWindowDisplayAffinity(hwnd, WDA_MONITOR))
                applied = applied or changed
            except (OSError, ValueError):
                continue
        self._operator_capture_exclusion_ok = applied if hidden else False
        return applied

    def _show_aios_windows_in_normal_capture(self, passive=False):
        """Keep aiOS visible to screenshots/sharing; optionally pass agent input through."""
        visible = self._set_aios_agent_capture_hidden(False)
        if passive and sys.platform.startswith("win"):
            user32 = ctypes.windll.user32
            for hwnd in self._aios_top_level_windows():
                try:
                    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                    self._operator_passive_window_styles.setdefault(int(hwnd), int(style))
                    user32.SetWindowLongW(
                        hwnd,
                        GWL_EXSTYLE,
                        style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
                    )
                except (OSError, ValueError):
                    continue
        return visible

    def _agent_capture_affinity_begin(self, token):
        self._agent_capture_affinity_tokens.add(str(token))
        return self._set_aios_agent_capture_hidden(True)

    def _agent_capture_affinity_end(self, token):
        self._agent_capture_affinity_tokens.discard(str(token))
        if not self._agent_capture_affinity_tokens:
            self._set_aios_agent_capture_hidden(False)

    def _remote_agent_capture_begin(self, token):
        """Start a short remote capture lease with automatic crash cleanup."""
        token = str(token)
        self._agent_capture_affinity_begin(token)
        self.root.after(5000, lambda value=token: self._agent_capture_affinity_end(value))

    def _restore_aios_window_input(self):
        if not sys.platform.startswith("win"):
            return
        user32 = ctypes.windll.user32
        for hwnd, style in list(self._operator_passive_window_styles.items()):
            try:
                if user32.IsWindow(hwnd):
                    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
            except (OSError, ValueError):
                pass
        self._operator_passive_window_styles.clear()

    def _agent_operator_capture_clean(self, monitor):
        # Scope WDA_EXCLUDEFROMCAPTURE to this frame only. Normal screenshots
        # and screen sharing see aiOS before and immediately after the grab.
        token = f"operator:{threading.get_ident()}:{time.time_ns()}"
        protected = [False]

        def prepare():
            protected[0] = self._agent_capture_affinity_begin(token)
            if not protected[0]:
                self._agent_operator_control_hide(temporary=True)
                try:
                    self.root.withdraw()
                except tk.TclError:
                    pass

        self._agent_operator_ui_sync(prepare, timeout=0.35)
        time.sleep(0.045)
        try:
            return self.agent_operator_raw_capture(monitor)
        finally:
            def restore():
                self._agent_capture_affinity_end(token)
                if not protected[0] and self.agent_operator_loop and self.agent_operator_loop.is_running():
                    self.root.deiconify()
                    self._agent_operator_control_show(monitor)

            self._agent_operator_ui_sync(restore, timeout=0.1)

    def _agent_operator_model(self):
        return self.agent_operator_model_var.get().strip() or self.agent_operator_default_model

    def _agent_operator_planner_model(self):
        if not self.agent_operator_planner_model_var:
            return str(self.agent_operator_settings.get("planner_model") or "")
        value = self.agent_operator_planner_model_var.get().strip()
        return "" if value.lower() in {"", "off", "none", "disabled"} else value

    def toggle_agent_operator_advanced(self):
        self.agent_operator_advanced_open = not self.agent_operator_advanced_open
        frame = self.agent_operator_advanced_frame
        if frame:
            if self.agent_operator_advanced_open:
                pack_options = {"fill": "x", "padx": 12, "pady": (0, 8)}
                if self.agent_operator_task_panel:
                    pack_options["before"] = self.agent_operator_task_panel
                frame.pack(**pack_options)
            else:
                frame.pack_forget()
        if self.agent_operator_advanced_btn:
            try:
                self.agent_operator_advanced_btn.configure(
                    text="Advanced ▾" if self.agent_operator_advanced_open else "Advanced ▸",
                    bg=self.c("accent") if self.agent_operator_advanced_open else self.c("panel2"),
                    fg="#061018" if self.agent_operator_advanced_open else self.c("text"),
                )
            except tk.TclError:
                pass

    def save_agent_operator_settings(self):
        settings = dict(self.config.get("ai_operator") or DEFAULT_CONFIG["ai_operator"])
        if self.agent_operator_monitor_var:
            settings["monitor"] = self.agent_operator_monitor_var.get()
        if self.agent_operator_model_var:
            settings["model"] = self._agent_operator_model()
        if self.agent_operator_planner_model_var:
            settings["planner_model"] = self.agent_operator_planner_model_var.get().strip() or "off"
        if self.agent_operator_reason_var:
            settings["reasoning"] = self.agent_operator_reason_var.get()
        if self.agent_operator_steps_var:
            settings["steps"] = self.agent_operator_steps_var.get()
        if self.agent_operator_delay_var:
            settings["delay"] = self.agent_operator_delay_var.get()
        if self.agent_operator_tts_var:
            settings["tts"] = bool(self.agent_operator_tts_var.get())
        if self.agent_operator_voice_var:
            settings["voice"] = self.agent_operator_voice_var.get()
        if self.agent_operator_shell_var:
            settings["shell"] = bool(self.agent_operator_shell_var.get())
        if self.agent_operator_codex_var:
            codex_enabled = bool(self.agent_operator_codex_var.get())
            settings["codex_auth"] = codex_enabled
            current_mode = str(settings.get("provider_mode") or "").strip().lower()
            if not codex_enabled:
                settings["provider_mode"] = "api"
            elif current_mode not in {"codex", "codex_api_fallback"}:
                settings["provider_mode"] = "codex"
        self.config["ai_operator"] = merge_dict(DEFAULT_CONFIG["ai_operator"], settings)
        self.agent_operator_settings = self.config["ai_operator"]
        save_config(self.config)

    def agent_operator_task_enter(self, event):
        if event and (event.state & 0x0001):
            return None
        loop = self.agent_operator_loop
        if loop and loop.is_running():
            return self.agent_operator_send_followup()
        return self.agent_operator_run()

    def agent_operator_task_paste(self, _event):
        if not self.agent_operator_ImageGrab:
            return None
        try:
            grabbed = self.agent_operator_ImageGrab.grabclipboard()
        except Exception:
            grabbed = None
        if self.agent_operator_Image and isinstance(grabbed, self.agent_operator_Image.Image):
            name = f"pasted_{datetime.now().strftime('%H%M%S')}.png"
            self._agent_operator_add_image_attachment(grabbed.copy(), name)
            return "break"
        if isinstance(grabbed, list):
            attached = False
            for path in grabbed:
                if isinstance(path, str) and os.path.isfile(path):
                    self._agent_operator_add_file_attachment(path)
                    attached = True
            if attached:
                return "break"
        return None

    def agent_operator_attach_files(self):
        paths = filedialog.askopenfilenames(
            title="Attach files",
            filetypes=[
                ("Images", "*.png *.jpg *.jpeg *.gif *.bmp *.webp"),
                ("Text", "*.txt *.md *.json *.csv *.log *.py *.js *.html *.xml *.yaml *.yml"),
                ("All", "*.*"),
            ],
        )
        for path in paths:
            self._agent_operator_add_file_attachment(path)

    def agent_operator_clear_input(self):
        if self.agent_operator_task:
            try:
                self.agent_operator_task.delete("1.0", "end")
            except tk.TclError:
                pass
        self.agent_operator_attachments.clear()
        self._agent_operator_render_attachments()

    def _agent_operator_add_file_attachment(self, path):
        if not self.agent_operator_Image:
            return
        name = os.path.basename(path)
        ext = os.path.splitext(path)[1].lower()
        image_exts = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
        try:
            if ext in image_exts:
                image = self.agent_operator_Image.open(path).convert("RGB")
                self._agent_operator_add_image_attachment(image, name)
            else:
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    self._agent_operator_add_text_attachment(handle.read(), name)
        except Exception as exc:
            messagebox.showerror("Attach failed", f"{name}: {exc}")

    def _agent_operator_add_image_attachment(self, image, name):
        self.agent_operator_attachments.append({"kind": "image", "image": image, "text": None, "name": name})
        self._agent_operator_render_attachments()

    def _agent_operator_add_text_attachment(self, text, name):
        self.agent_operator_attachments.append({"kind": "text", "image": None, "text": text, "name": name})
        self._agent_operator_render_attachments()

    def _agent_operator_remove_attachment(self, attachment):
        if attachment in self.agent_operator_attachments:
            self.agent_operator_attachments.remove(attachment)
        self._agent_operator_render_attachments()

    def _agent_operator_render_attachments(self):
        strip = self.agent_operator_attach_strip
        if not strip:
            return
        for child in strip.winfo_children():
            try:
                child.destroy()
            except tk.TclError:
                pass
        if not self.agent_operator_attachments:
            self.agent_operator_attach_placeholder = tk.Label(
                strip,
                text="No attachments",
                bg=self.c("panel2"),
                fg=self.c("muted"),
                font=self.font(8),
            )
            self.agent_operator_attach_placeholder.pack(side="left", padx=4, pady=4)
            return
        for attachment in self.agent_operator_attachments:
            self._agent_operator_attachment_chip(strip, attachment)

    def _agent_operator_attachment_chip(self, parent, attachment):
        chip_bg = "#182536"
        chip = tk.Frame(parent, bg=chip_bg, highlightbackground="#31445d", highlightthickness=1, bd=0)
        chip.pack(side="left", padx=4, pady=4)
        kind = attachment.get("kind")
        if kind == "image" and attachment.get("image") is not None and self.agent_operator_ImageTk:
            thumb = attachment["image"].copy()
            thumb.thumbnail((54, 54), self.agent_operator_Image.LANCZOS)
            photo = self.agent_operator_ImageTk.PhotoImage(thumb)
            attachment["thumb"] = photo
            tk.Label(chip, image=photo, bg=chip_bg, borderwidth=0).pack(side="left", padx=(5, 5), pady=5)
            sub = f"{attachment['image'].width}x{attachment['image'].height}"
        else:
            tk.Label(chip, text="TXT", bg=chip_bg, fg=self.c("accent"), font=self.font(9, "bold")).pack(side="left", padx=(8, 6), pady=8)
            text = attachment.get("text") or ""
            sub = f"{text.count(chr(10)) + 1} lines"
        meta = tk.Frame(chip, bg=chip_bg)
        meta.pack(side="left", padx=(0, 8), pady=5)
        name = str(attachment.get("name") or "attached")
        display_name = name if len(name) <= 24 else name[:21] + "..."
        tk.Label(meta, text=display_name, bg=chip_bg, fg=self.c("text"), font=self.font(8, "bold")).pack(anchor="w")
        tk.Label(meta, text=sub, bg=chip_bg, fg=self.c("muted"), font=self.font(8)).pack(anchor="w")
        remove = tk.Label(chip, text="x", bg=chip_bg, fg=self.c("danger"), font=self.font(10, "bold"), cursor="hand2", padx=8)
        remove.pack(side="right", fill="y")
        remove.bind("<Button-1>", lambda _event, item=attachment: self._agent_operator_remove_attachment(item))

    def _agent_operator_attachment_snapshot(self):
        snapshot = []
        for attachment in self.agent_operator_attachments:
            item = {"kind": attachment.get("kind"), "name": attachment.get("name") or "attached"}
            if attachment.get("kind") == "image" and attachment.get("image") is not None:
                item["image"] = attachment["image"].copy()
            elif attachment.get("kind") == "text":
                item["text"] = attachment.get("text") or ""
            snapshot.append(item)
        return snapshot

    def _agent_operator_context_dir(self):
        return Path(self.agent_clicker_dir) / "user_context"

    def _agent_operator_context_files(self):
        folder = self._agent_operator_context_dir()
        if not folder.exists():
            return []
        return sorted(
            path.name for path in folder.iterdir()
            if path.is_file() and not path.name.startswith(".")
        )

    def _agent_operator_context_path(self, name):
        folder = self._agent_operator_context_dir()
        safe = os.path.basename(str(name or "").strip())
        if not safe or safe.startswith("."):
            raise ValueError("Bad file name")
        return folder / safe

    def _agent_operator_context_read(self, name):
        try:
            return self._agent_operator_context_path(name).read_text(encoding="utf-8")
        except Exception:
            return ""

    def _agent_operator_context_write(self, name, text):
        folder = self._agent_operator_context_dir()
        folder.mkdir(parents=True, exist_ok=True)
        self._agent_operator_context_path(name).write_text(text, encoding="utf-8")

    def _agent_operator_seed_context_files(self):
        folder = self._agent_operator_context_dir()
        if self._agent_operator_context_files():
            return
        external = Path(r"C:\Claude code\Agent Clicker\user_context")
        if external.exists() and external.resolve() != folder.resolve():
            for source in external.iterdir():
                if source.is_file() and not source.name.startswith("."):
                    target = folder / source.name
                    if not target.exists():
                        try:
                            shutil.copy2(source, target)
                        except OSError:
                            pass
        if not self._agent_operator_context_files():
            self._agent_operator_context_write("human.md", "")

    def _agent_operator_context_refresh(self, select=None):
        if not self.agent_operator_context_list or not self.agent_operator_context_editor:
            return
        folder = self._agent_operator_context_dir()
        folder.mkdir(parents=True, exist_ok=True)
        self._agent_operator_seed_context_files()
        files = self._agent_operator_context_files()
        current = select or self.agent_operator_context_current
        self.agent_operator_context_list.delete(0, "end")
        for name in files:
            self.agent_operator_context_list.insert("end", name)
        if current not in files:
            current = files[0] if files else None
        if current:
            index = files.index(current)
            self.agent_operator_context_list.selection_clear(0, "end")
            self.agent_operator_context_list.selection_set(index)
            self.agent_operator_context_list.see(index)
            self._agent_operator_context_load(current)
        else:
            self.agent_operator_context_current = None
            self.agent_operator_context_file_var.set("")
            self.agent_operator_context_loading = True
            self.agent_operator_context_editor.delete("1.0", "end")
            self.agent_operator_context_loading = False

    def _agent_operator_context_load(self, name):
        self.agent_operator_context_current = name
        self.agent_operator_context_file_var.set(name)
        if self.agent_operator_context_status_var:
            self.agent_operator_context_status_var.set("")
        self.agent_operator_context_loading = True
        self.agent_operator_context_editor.delete("1.0", "end")
        self.agent_operator_context_editor.insert("1.0", self._agent_operator_context_read(name))
        self.agent_operator_context_editor.edit_modified(False)
        self.agent_operator_context_loading = False

    def agent_operator_context_select(self, _event=None):
        if not self.agent_operator_context_list:
            return
        selection = self.agent_operator_context_list.curselection()
        if not selection:
            return
        name = self.agent_operator_context_list.get(selection[0])
        if name != self.agent_operator_context_current:
            self._agent_operator_context_flush()
            self._agent_operator_context_load(name)

    def agent_operator_context_modified(self, _event=None):
        editor = self.agent_operator_context_editor
        if not editor:
            return
        if self.agent_operator_context_loading or not self.agent_operator_context_current:
            editor.edit_modified(False)
            return
        if self.agent_operator_context_status_var:
            self.agent_operator_context_status_var.set("editing")
        if self.agent_operator_context_save_after is not None:
            try:
                self.root.after_cancel(self.agent_operator_context_save_after)
            except tk.TclError:
                pass
        self.agent_operator_context_save_after = self.root.after(400, self._agent_operator_context_flush)
        editor.edit_modified(False)

    def _agent_operator_context_flush(self):
        self.agent_operator_context_save_after = None
        if not self.agent_operator_context_current or not self.agent_operator_context_editor:
            return
        text = self.agent_operator_context_editor.get("1.0", "end-1c")
        try:
            self._agent_operator_context_write(self.agent_operator_context_current, text)
            if self.agent_operator_context_status_var:
                self.agent_operator_context_status_var.set(f"saved {datetime.now().strftime('%H:%M:%S')}")
        except Exception as exc:
            if self.agent_operator_context_status_var:
                self.agent_operator_context_status_var.set(f"save failed: {exc}")

    def agent_operator_context_new(self):
        name = simpledialog.askstring("New prompt", "File name:", parent=self.root)
        if not name:
            return
        name = name.strip()
        if not name:
            return
        if "/" in name or "\\" in name or name.startswith("."):
            messagebox.showerror("Bad name", "Use a simple file name.")
            return
        if "." not in name:
            name += ".md"
        try:
            path = self._agent_operator_context_path(name)
            if path.exists():
                messagebox.showerror("Exists", f"{name} already exists.")
                return
            self._agent_operator_context_write(name, "")
            self._agent_operator_context_refresh(select=name)
        except Exception as exc:
            messagebox.showerror("Create failed", str(exc))

    def agent_operator_context_rename(self):
        current = self.agent_operator_context_current
        if not current:
            return
        new = simpledialog.askstring("Rename prompt", "File name:", initialvalue=current, parent=self.root)
        if not new or new == current:
            return
        new = new.strip()
        if "/" in new or "\\" in new or new.startswith("."):
            messagebox.showerror("Bad name", "Use a simple file name.")
            return
        if "." not in new:
            new += ".md"
        try:
            self._agent_operator_context_flush()
            src = self._agent_operator_context_path(current)
            dst = self._agent_operator_context_path(new)
            if dst.exists():
                messagebox.showerror("Exists", f"{new} already exists.")
                return
            src.rename(dst)
            self.agent_operator_context_current = new
            self._agent_operator_context_refresh(select=new)
        except Exception as exc:
            messagebox.showerror("Rename failed", str(exc))

    def agent_operator_context_delete(self):
        current = self.agent_operator_context_current
        if not current:
            return
        if not messagebox.askyesno("Delete prompt", f"Delete {current}?"):
            return
        try:
            self._agent_operator_context_path(current).unlink()
            self.agent_operator_context_current = None
            if self.agent_operator_context_save_after is not None:
                try:
                    self.root.after_cancel(self.agent_operator_context_save_after)
                except tk.TclError:
                    pass
                self.agent_operator_context_save_after = None
            self._agent_operator_context_refresh()
        except Exception as exc:
            messagebox.showerror("Delete failed", str(exc))

    def _agent_operator_user_context_text(self):
        self._agent_operator_context_flush()
        chunks = []
        for name in self._agent_operator_context_files():
            text = self._agent_operator_context_read(name).strip()
            if text:
                chunks.append(f"# {name}\n{text}")
        return "\n\n".join(chunks)

    def agent_operator_preview_monitor(self):
        if not self._ensure_agent_operator():
            return
        monitor = self._agent_operator_selected_monitor()
        if not monitor:
            self.agent_operator_status_var.set("No monitor")
            return
        try:
            self.agent_operator_current_image = self.agent_operator_capture(monitor)
            self.agent_operator_last_clicks.clear()
            self._agent_operator_redraw_preview()
            self._agent_operator_log_line("dim", f"preview {monitor.label}\n")
        except Exception as exc:
            self._agent_operator_log_line("err", f"capture failed: {exc}\n")
            self.agent_operator_status_var.set("Capture failed")

    def agent_operator_test_cursor(self):
        if not self._ensure_agent_operator():
            return
        monitor = self._agent_operator_selected_monitor()
        if not monitor:
            self.agent_operator_status_var.set("No monitor")
            return
        try:
            self.agent_operator_current_image = self.agent_operator_capture(monitor)
            self.agent_operator_last_clicks.clear()
            self._agent_operator_redraw_preview()
        except Exception as exc:
            self._agent_operator_log_line("err", f"capture failed: {exc}\n")
            self.agent_operator_status_var.set("Capture failed")
            return

        cx, cy = monitor.width // 2, monitor.height // 2
        self._agent_operator_log_line("step", f"[{self._ts()}] TEST CURSOR\n")
        self._agent_operator_log_line(
            "dim",
            f"local center=({cx},{cy}) virtual=({monitor.left + cx},{monitor.top + cy})\n",
        )

        def worker():
            try:
                result = self.agent_operator_exec_action(
                    {"type": "move", "x": cx, "y": cy, "duration": 0.6},
                    monitor,
                )
                self._agent_operator_enqueue({"type": "log", "msg": f"move -> {result.detail} ({result.elapsed_ms}ms)"})
            except Exception as exc:
                self._agent_operator_enqueue({"type": "log", "msg": f"test cursor failed: {exc}"})

        threading.Thread(target=worker, daemon=True).start()

    def agent_operator_run(self):
        if not self._ensure_agent_operator():
            return "break"
        if self.agent_operator_loop.is_running():
            self.agent_operator_status_var.set("Already running")
            self._phone_mirror_write({
                "type": "error", "ts": time.time(), "title": "Already running",
                "message": "A task is already in progress — send this as a follow-up, or stop the run first.",
            })
            # Also repairs a phone that thought the run had ended.
            self._phone_mirror_set_running()
            return "break"
        task = self.agent_operator_task.get("1.0", "end").strip() if self.agent_operator_task else ""
        if not task:
            self.agent_operator_status_var.set("Task missing")
            self._phone_mirror_fail("The task text never reached this PC.", "task_missing")
            return "break"
        monitor = self._agent_operator_selected_monitor()
        if not monitor:
            self.agent_operator_status_var.set("No monitor")
            self._phone_mirror_fail("No display is available to control.", "no_monitor")
            return "break"
        self._phone_mirror_monitor = {
            "left": int(getattr(monitor, "left", 0)),
            "top": int(getattr(monitor, "top", 0)),
            "width": int(getattr(monitor, "width", 0)),
            "height": int(getattr(monitor, "height", 0)),
        }
        try:
            steps = max(1, min(200, int(float(self.agent_operator_steps_var.get()))))
        except (TypeError, ValueError):
            steps = 25
            self.agent_operator_steps_var.set(str(steps))
        try:
            delay = max(0.0, min(3.0, float(self.agent_operator_delay_var.get())))
        except (TypeError, ValueError):
            delay = 0.20
            self.agent_operator_delay_var.set(f"{delay:.2f}")
        self.agent_operator_current_task = task
        self.agent_operator_clear_log()
        self.agent_operator_overlay_step = 0
        self.agent_operator_overlay_thought = "Starting OPERATOR..."
        self.agent_operator_overlay_action = ""
        self.agent_operator_last_clicks.clear()
        model = self._agent_operator_model()
        planner_model = self._agent_operator_planner_model()
        reasoning = (self.agent_operator_reason_var.get() if self.agent_operator_reason_var else "low").strip().lower() or None
        attachments = self._agent_operator_attachment_snapshot()
        user_context = self._agent_operator_user_context_text()
        context_count = len([name for name in self._agent_operator_context_files() if self._agent_operator_context_read(name).strip()])
        shell_enabled = bool(self.agent_operator_shell_var and self.agent_operator_shell_var.get())
        self.save_agent_operator_settings()
        provider_mode = str(self.agent_operator_settings.get("provider_mode") or "").strip().lower()
        if provider_mode not in {"codex", "api", "codex_api_fallback"}:
            provider_mode = "codex" if self.agent_operator_settings.get("codex_auth") else "api"
        key_available = bool(self.get_openai_api_key())
        codex_available, codex_message = self._refresh_agent_operator_codex_auth()
        if provider_mode == "codex":
            if not codex_available:
                self._agent_operator_log_line("err", f"Codex auth unavailable: {codex_message}\n")
                self.agent_operator_status_var.set("Codex auth unavailable")
                self._phone_mirror_fail(f"Codex auth unavailable: {codex_message}", "codex_unavailable")
                return "break"
            backend = "codex"
        elif provider_mode == "api":
            if not key_available:
                self._agent_operator_log_line("err", "OpenAI API key is not configured.\n")
                self.agent_operator_status_var.set("OpenAI API key required")
                self._phone_mirror_fail(
                    "No OpenAI API key is saved on this PC. Add one in Settings → AI provider.",
                    "no_api_key")
                return "break"
            backend = "api"
        else:
            if codex_available:
                backend = "codex_fallback"
            elif key_available:
                backend = "api"
                self._agent_operator_log_line("dim", "Codex unavailable; starting with the API fallback.\n")
            else:
                self._agent_operator_log_line("err", "Codex is unavailable and no API fallback is configured.\n")
                self.agent_operator_status_var.set("No AI provider available")
                self._phone_mirror_fail(
                    "Codex is unavailable and no API key fallback is configured.",
                    "no_provider")
                return "break"
        self._agent_operator_log_line("step", f"[{self._ts()}] START\n")
        self._agent_operator_log_line(
            "dim",
            f"task={task!r} monitor={monitor.label} planner={planner_model or 'off'} model={model} reasoning={reasoning} max_steps={steps} attachments={len(attachments)} prompts={context_count} shell={'on' if shell_enabled else 'off'} backend={backend}\n\n",
        )
        self.agent_operator_status_var.set("Running")
        self.agent_operator_stop_requested = False
        self._agent_operator_sync_buttons()
        try:
            self._agent_operator_control_show(monitor)
            self.agent_operator_loop.start(
                task,
                monitor,
                model=model,
                max_steps=steps,
                action_delay=delay,
                shell_enabled=shell_enabled,
                backend=backend,
                reasoning_effort=reasoning,
                mid_screenshots="key",
                attachments=attachments,
                user_context=user_context,
                planner_model=planner_model,
            )
            self._agent_operator_sync_buttons()
        except TypeError:
            try:
                self._agent_operator_control_show(monitor)
                self.agent_operator_loop.start(
                    task,
                    monitor,
                    model=model,
                    max_steps=steps,
                    action_delay=delay,
                    shell_enabled=shell_enabled,
                    backend=backend,
                    reasoning_effort=reasoning,
                    mid_screenshots="key",
                    attachments=attachments,
                    planner_model=planner_model,
                )
                self._agent_operator_sync_buttons()
            except TypeError:
                self._agent_operator_control_show(monitor)
                self.agent_operator_loop.start(task, monitor, model=model, max_steps=steps, action_delay=delay)
                self._agent_operator_sync_buttons()
        except Exception as exc:
            self.agent_operator_stop_requested = False
            self._agent_operator_control_stop()
            self._agent_operator_log_line("err", f"start failed: {exc}\n")
            self.agent_operator_status_var.set("Start failed")
            self._phone_mirror_fail(f"Start failed: {exc}")
            self._agent_operator_sync_buttons()
        if self.agent_operator_loop and self.agent_operator_loop.is_running():
            self._phone_mirror_set_running()
        return "break"

    def agent_operator_stop(self):
        if self.agent_operator_stop_requested:
            if self.agent_operator_status_var:
                self.agent_operator_status_var.set("Safety stop already requested")
            return "break"
        self.agent_operator_stop_requested = True
        self._phone_mirror_set_idle("stop_requested")
        self._agent_operator_log_line("ts", f"\n[{self._ts()}] ")
        self._agent_operator_log_line("err", "SAFETY STOP confirmed. Further aiOPERATOR inputs are blocked.\n")
        if self.agent_operator_loop:
            self.agent_operator_loop.stop()
            if self.agent_operator_release_all:
                try:
                    self.agent_operator_release_all()
                except Exception as exc:
                    self._agent_operator_log_line("err", f"input release failed: {exc}\n")
            self._agent_operator_tts_clear()
            self._agent_operator_control_stop()
            if self.agent_operator_status_var:
                self.agent_operator_status_var.set("Safety stop requested")
        self._agent_operator_sync_buttons()
        return "break"

    def agent_operator_toggle_pause(self):
        if not self.agent_operator_loop or not self.agent_operator_loop.is_running():
            return
        if self.agent_operator_loop.is_paused():
            self.agent_operator_loop.resume()
            self.agent_operator_status_var.set("Running")
        else:
            self.agent_operator_loop.pause()
            self.agent_operator_status_var.set("Paused")
        self._agent_operator_sync_buttons()

    def agent_operator_toggle_tts(self):
        enabled = bool(self.agent_operator_tts_var and self.agent_operator_tts_var.get())
        if not self.agent_operator_tts:
            if self.agent_operator_status_var:
                self.agent_operator_status_var.set(self.agent_operator_tts_error or "TTS unavailable")
            if self.agent_operator_tts_var:
                self.agent_operator_tts_var.set(False)
            return
        self.agent_operator_tts.enable(enabled)
        self.agent_operator_set_voice()
        if self.agent_operator_status_var:
            self.agent_operator_status_var.set(f"TTS {'on' if enabled else 'off'} ({self.agent_operator_voice_var.get()})")
        if enabled:
            self._agent_operator_tts_speak("text to speech is on")
        self.save_agent_operator_settings()

    def agent_operator_toggle_shell(self):
        enabled = bool(self.agent_operator_shell_var and self.agent_operator_shell_var.get())
        if self.agent_operator_status_var:
            self.agent_operator_status_var.set(f"PowerShell {'enabled' if enabled else 'disabled'}")
        self.save_agent_operator_settings()

    def agent_operator_toggle_codex(self):
        enabled = bool(self.agent_operator_codex_var and self.agent_operator_codex_var.get())
        if enabled:
            ok, message = self._refresh_agent_operator_codex_auth()
            if not ok:
                if self.agent_operator_codex_var:
                    self.agent_operator_codex_var.set(False)
                if self.agent_operator_status_var:
                    self.agent_operator_status_var.set(f"Codex auth unavailable: {message}")
                self._agent_operator_log_line("err", f"Codex auth unavailable: {message}\n")
                self.save_agent_operator_settings()
                return
        if self.agent_operator_status_var:
            self.agent_operator_status_var.set(
                "Backend: Codex auth" if enabled else "Backend: OpenAI API"
            )
        self.save_agent_operator_settings()

    def agent_operator_set_voice(self):
        if self.agent_operator_tts and self.agent_operator_voice_var:
            self.agent_operator_tts.set_voice(self.agent_operator_voice_var.get())
        self.save_agent_operator_settings()

    def _agent_operator_tts_speak(self, text):
        if self.agent_operator_tts and self.agent_operator_tts_var and self.agent_operator_tts_var.get():
            self.agent_operator_tts.speak(text)

    def _agent_operator_tts_clear(self):
        if self.agent_operator_tts:
            self.agent_operator_tts.clear()

    def agent_operator_clear_log(self):
        self.agent_operator_log_buffer.clear()
        if not self.agent_operator_log:
            return
        try:
            self.agent_operator_log.configure(state="normal")
            self.agent_operator_log.delete("1.0", "end")
            self.agent_operator_log.configure(state="disabled")
        except tk.TclError:
            pass
        try:
            self._phone_mirror_reset()
        except Exception:
            pass

    def _phone_mirror_dir(self):
        path = BASE_DIR / "phone_operator_events"
        try:
            path.mkdir(exist_ok=True)
            (path / "frames").mkdir(exist_ok=True)
        except OSError:
            pass
        return path

    def _phone_mirror_reset(self):
        root = self._phone_mirror_dir()
        events = root / "events.jsonl"
        frames = root / "frames"
        try:
            events.write_bytes(b"")
        except OSError:
            pass
        try:
            for child in frames.glob("frame-*.jpg"):
                try:
                    child.unlink()
                except OSError:
                    pass
        except OSError:
            pass
        self._phone_mirror_seq = 0
        self._phone_mirror_last_frame = 0
        # Marker event so the phone knows a fresh run started
        self._phone_mirror_write({"type": "run_start", "ts": time.time(),
                                  "task": self.agent_operator_current_task})
        # The run is only "running" once the loop actually starts. Claiming it
        # here left the phone stuck on a phantom run whenever the start aborted
        # (no monitor, no provider), which turned the next prompt into a
        # follow-up for a run that was never alive.
        self._phone_mirror_write_status(running=False)

    def _phone_mirror_write_status(self, *, running, asking=False, last_question="", reason=""):
        state = {
            "ts": time.time(),
            "running": bool(running),
            "asking": bool(asking),
            "last_question": last_question or "",
            "task": self.agent_operator_current_task,
        }
        if reason:
            state["reason"] = reason
        try:
            (self._phone_mirror_dir() / "status.json").write_text(
                json.dumps(state, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def _phone_mirror_set_running(self):
        self._phone_mirror_write_status(running=True)

    def _phone_mirror_fail(self, message, reason="start_failed"):
        """Surface an aborted start on the phone instead of only in the GUI log."""
        try:
            self._phone_mirror_write({"type": "error", "ts": time.time(),
                                      "title": "OPERATOR could not start",
                                      "message": str(message)})
        except Exception:
            pass
        try:
            self._phone_mirror_write_status(running=False, reason=reason)
        except Exception:
            pass

    def _phone_mirror_write(self, payload):
        root = self._phone_mirror_dir()
        line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
        try:
            with (root / "events.jsonl").open("ab") as fh:
                fh.write(line.encode("utf-8", "ignore"))
        except OSError:
            pass

    def _phone_mirror_bounds(self):
        """Capture rectangle of the running monitor, so the phone can place a
        click marker on the exact screenshot the model was looking at."""
        bounds = getattr(self, "_phone_mirror_monitor", None)
        return dict(bounds) if isinstance(bounds, dict) else {}

    def _phone_mirror_save_frame(self, image):
        if image is None:
            return None
        try:
            seq = getattr(self, "_phone_mirror_seq", 0) + 1
            self._phone_mirror_seq = seq
            path = self._phone_mirror_dir() / "frames" / f"frame-{seq}.jpg"
            img = image
            try:
                if img.mode != "RGB":
                    img = img.convert("RGB")
            except Exception:
                pass
            # Cap dimension so frames stay servable
            try:
                w, h = img.size
                cap = 1800
                if max(w, h) > cap:
                    if w >= h:
                        img = img.resize((cap, int(h * cap / w)))
                    else:
                        img = img.resize((int(w * cap / h), cap))
            except Exception:
                pass
            img.save(path, format="JPEG", quality=80, optimize=True)
            self._phone_mirror_last_frame = seq
            return seq
        except Exception:
            return None

    def _phone_mirror_event(self, event):
        kind = event.get("type") if isinstance(event, dict) else None
        if not kind:
            return
        record = {"type": kind, "ts": time.time()}
        if kind == "step_begin":
            record["n"] = event.get("n")
        elif kind == "screenshot":
            seq = self._phone_mirror_save_frame(event.get("image"))
            if seq is None:
                return
            record["frame"] = seq
            record["n"] = event.get("n")
            size = event.get("size") or []
            if len(size) == 2:
                record["shot_w"], record["shot_h"] = int(size[0]), int(size[1])
            record.update(self._phone_mirror_bounds())
        elif kind == "thought":
            record["thought"] = (event.get("thought") or "").strip()
            record["say"] = (event.get("say") or "").strip()
            record["message"] = (event.get("message") or "").strip()
            record["actions"] = len(event.get("actions") or [])
            record["status"] = event.get("status")
            record["elapsed_ms"] = event.get("elapsed_ms")
        elif kind == "planning_begin":
            record["model"] = event.get("model", "")
        elif kind == "plan":
            record["model"] = event.get("model", "")
            record["plan"] = (event.get("plan") or "")[:8000]
            record["todo"] = [str(item)[:240] for item in (event.get("todo") or [])][:12]
            record["done_when"] = [str(item)[:200] for item in (event.get("done_when") or [])][:8]
        elif kind == "verify_begin":
            record["model"] = event.get("model", "")
        elif kind == "verified":
            record["verdict"] = event.get("verdict", "")
            record["reason"] = (event.get("reason") or "")[:400]
            record["missing"] = [str(item)[:200] for item in (event.get("missing") or [])][:6]
        elif kind == "action_done":
            result = event.get("result") or {}
            atype = (result.get("action") or {}).get("type") or "?"
            record["ok"] = bool(result.get("ok"))
            record["action"] = atype
            record["detail"] = (result.get("detail") or "")[:600]
            record["elapsed_ms"] = result.get("elapsed_ms")
            output = (result.get("output") or "")[:1200]
            if output:
                record["output"] = output
        elif kind == "click_fx":
            record["x"] = event.get("x")
            record["y"] = event.get("y")
            record["button"] = event.get("button", "left")
            # Point at the screenshot this click was decided from so the phone
            # can show exactly where OPERATOR clicked.
            frame = getattr(self, "_phone_mirror_last_frame", 0)
            if frame:
                record["frame"] = frame
            record.update(self._phone_mirror_bounds())
        elif kind == "step_end":
            r = event.get("record") or {}
            record["n"] = r.get("n")
            record["think_ms"] = r.get("think_ms")
            record["act_ms"] = r.get("act_ms")
            record["actions"] = len(r.get("results") or [])
        elif kind == "done":
            record["ok"] = bool(event.get("ok"))
            record["steps"] = event.get("steps")
            record["message"] = event.get("message", "")
            record["verified"] = bool(event.get("verified"))
            record["usage"] = event.get("usage") or {}
            try:
                record["cost"] = model_pricing.estimate_cost(
                    record["usage"], self._agent_operator_model())
            except Exception:
                pass
        elif kind == "ask":
            record["message"] = event.get("message", "")
        elif kind == "max_steps":
            record["message"] = event.get("message", "")
            record["steps"] = event.get("steps")
        elif kind == "follow_up_received":
            record["text"] = event.get("text", "")
            record["answering_ask"] = bool(event.get("answering_ask"))
        elif kind == "log":
            record["msg"] = event.get("msg", "")
        else:
            return
        self._phone_mirror_write(record)

    def _agent_operator_enqueue(self, event):
        self.agent_operator_event_q.put(event)
        try:
            self._phone_mirror_event(event)
        except Exception:
            pass
        try:
            self._phone_mirror_state(event)
        except Exception:
            pass

    def _phone_mirror_state(self, event):
        kind = event.get("type") if isinstance(event, dict) else None
        if kind not in {"step_begin", "done", "ask", "max_steps", "follow_up_received", "log"}:
            return
        loop = self.agent_operator_loop
        state = {
            "ts": time.time(),
            "running": bool(loop and loop.is_running()),
            "asking": bool(loop and loop.is_awaiting_answer()),
            "last_question": "",
            "task": self.agent_operator_current_task,
        }
        if kind in {"ask", "max_steps"}:
            state["last_question"] = event.get("message", "")
            state["asking"] = True
        if kind == "done":
            state["running"] = False
            state["asking"] = False
        try:
            path = self._phone_mirror_dir() / "status.json"
            path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def _poll_agent_operator_events(self):
        try:
            while True:
                event = self.agent_operator_event_q.get_nowait()
                self._handle_agent_operator_event(event)
        except queue.Empty:
            pass
        try:
            self.root.after(40, self._poll_agent_operator_events)
        except tk.TclError:
            pass

    def _handle_agent_operator_event(self, event):
        kind = event.get("type")
        if kind == "operator_boot_ready":
            self.agent_operator_booting = False
            self.agent_operator_booted = bool(event.get("ok"))
            if self.agent_operator_booted:
                self._refresh_agent_operator_codex_auth()
            if self.active_tab == "AI Operator":
                self.render_tab("AI Operator")
        elif kind == "step_begin":
            self.agent_operator_overlay_step = int(event.get("n") or 0)
            self.agent_operator_overlay_thought = "Looking at the latest screen..."
            self.agent_operator_overlay_action = ""
            if self.agent_operator_step_var:
                self.agent_operator_step_var.set(f"step {event.get('n')}")
            self.agent_operator_last_clicks.clear()
            self._agent_operator_log_line("step", f"\n[{self._ts()}] Step {event.get('n')}\n")
        elif kind == "planning_begin":
            self.agent_operator_overlay_thought = "Planning the task..."
            if self.agent_operator_status_var:
                self.agent_operator_status_var.set("Planning")
            self._agent_operator_log_line("step", f"\n[{self._ts()}] Planning with {event.get('model', 'planner')}\n")
        elif kind == "plan":
            self.agent_operator_overlay_thought = str(event.get("plan") or "Plan ready")
            self._agent_operator_log_line("status", (event.get("plan") or "").rstrip() + "\n")
            todo = event.get("todo") or []
            if todo:
                self._agent_operator_log_line("status", "TODO:\n")
                for index, item in enumerate(todo, 1):
                    self._agent_operator_log_line("dim", f"  {index}. {item}\n")
            for item in event.get("done_when") or []:
                self._agent_operator_log_line("dim", f"  done when: {item}\n")
        elif kind == "verify_begin":
            self.agent_operator_overlay_thought = "Checking that the requested result is actually complete..."
            if self.agent_operator_status_var:
                self.agent_operator_status_var.set("Checking the result")
            self._agent_operator_log_line("step", f"\n[{self._ts()}] Checking whether the task is really done\n")
        elif kind == "verified":
            passed = str(event.get("verdict")) == "pass"
            self.agent_operator_overlay_action = (
                f"Completion check {'passed' if passed else 'needs more work'}: "
                f"{event.get('reason', '')}"
            ).strip()
            self._agent_operator_log_line(
                "ok" if passed else "err",
                f"CHECK {'passed' if passed else 'failed'}: {event.get('reason', '')}\n")
            for item in event.get("missing") or []:
                self._agent_operator_log_line("err", f"  still missing: {item}\n")
        elif kind == "screenshot":
            self.agent_operator_current_image = event.get("image")
            self._agent_operator_redraw_preview()
        elif kind == "thought":
            self.agent_operator_overlay_thought = str(
                event.get("thought") or event.get("message") or "Thinking..."
            )
            self._agent_operator_log_line("ts", f"[{self._ts()}] ")
            self._agent_operator_log_line("thought", "thought: ")
            self._agent_operator_log_line("thought", (event.get("thought") or "").rstrip() + "\n")
            say = (event.get("say") or "").strip()
            if say:
                self._agent_operator_log_line("ts", f"[{self._ts()}] ")
                self._agent_operator_log_line("status", f"say: {say}\n")
                self._agent_operator_tts_speak(say)
            if event.get("message"):
                self._agent_operator_log_line("status", f"msg: {event.get('message')}\n")
            self._agent_operator_log_line(
                "dim",
                f"plan: {len(event.get('actions') or [])} action(s), status={event.get('status')}, think={event.get('elapsed_ms')}ms\n",
            )
        elif kind == "action_done":
            result = event.get("result") or {}
            tag = "ok" if result.get("ok") else "err"
            atype = (result.get("action") or {}).get("type") or "?"
            detail = str(result.get("detail") or "").strip()
            self.agent_operator_overlay_action = f"{atype}: {detail}" if detail else str(atype)
            self._agent_operator_log_line("ts", f"[{self._ts()}] ")
            self._agent_operator_log_line(tag, "ok " if result.get("ok") else "fail ")
            self._agent_operator_log_line("action", f"{atype:<12}")
            self._agent_operator_log_line("dim", f" {result.get('detail', '')} ({result.get('elapsed_ms')}ms)\n")
            output = result.get("output") or ""
            if output:
                clipped = output if len(output) <= 1200 else output[:1200] + "\n...[clipped]"
                for line in clipped.splitlines():
                    self._agent_operator_log_line("dim", f"  | {line}\n")
        elif kind == "click_fx":
            self._agent_operator_flash_click(event.get("x", 0), event.get("y", 0), event.get("button", "left"))
            monitor = self._agent_operator_selected_monitor()
            if monitor:
                self.agent_operator_last_clicks.append((event.get("x", 0) - monitor.left, event.get("y", 0) - monitor.top, event.get("button", "left")))
                self._agent_operator_redraw_preview()
        elif kind == "step_end":
            record = event.get("record") or {}
            self._agent_operator_log_line(
                "dim",
                f"step {record.get('n')} totals: think {record.get('think_ms')}ms, act {record.get('act_ms')}ms, {len(record.get('results') or [])} actions\n",
            )
        elif kind == "done":
            ok = bool(event.get("ok"))
            tag = "ok" if ok else "err"
            self._agent_operator_control_stop()
            self._agent_operator_log_line("ts", f"\n[{self._ts()}] ")
            self._agent_operator_log_line(tag, f"DONE ok={ok} steps={event.get('steps')} message={event.get('message', '')}\n")
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
            if usage:
                counts = model_pricing.token_breakdown(usage)
                self._agent_operator_log_line(
                    "status",
                    f"TOKENS {counts['input_tokens']:,} input "
                    f"({counts['fresh_input_tokens']:,} fresh, "
                    f"{counts['cached_input_tokens']:,} cached, "
                    f"{counts['cache_write_input_tokens']:,} cache-write) + "
                    f"{counts['output_tokens']:,} output = {counts['total_tokens']:,} total "
                    f"across {counts['requests']} model call(s)\n",
                )
                try:
                    cost = model_pricing.estimate_cost(usage, self._agent_operator_model())
                    cost_line = model_pricing.describe_cost(usage, self._agent_operator_model())
                except Exception:
                    cost = {}
                    cost_line = ""
                if cost_line:
                    self._agent_operator_log_line("status", f"COST {cost_line}\n")
            if self.agent_operator_stop_requested:
                self._agent_operator_log_line("err", "SAFETY STOP loop exited. aiOPERATOR is no longer controlling input.\n")
                self.agent_operator_stop_requested = False
            if self.agent_operator_status_var:
                stopped = "stop" in str(event.get("message", "")).lower()
                if stopped:
                    status = "Stopped"
                elif usage:
                    status = (
                        f"Done · {counts['total_tokens']:,} tokens · "
                        f"{model_pricing.format_usd_exact(cost)} API"
                    )
                else:
                    status = f"Done. {event.get('message', '')}"
                self.agent_operator_status_var.set(status)
            self._agent_operator_tts_speak("done" if ok else ("stopped" if "stop" in str(event.get("message", "")).lower() else "failed"))
            self._agent_operator_sync_buttons()
        elif kind == "ask":
            self.agent_operator_overlay_thought = str(event.get("message") or "Waiting for your answer")
            if self.agent_operator_status_var:
                self.agent_operator_status_var.set("Agent asks: " + event.get("message", ""))
            self._agent_operator_log_line("status", f"ASK: {event.get('message', '')}\n")
            self._agent_operator_sync_buttons()
        elif kind == "max_steps":
            msg = event.get("message") or "Step budget used — continue for another batch?"
            self.agent_operator_overlay_thought = str(msg)
            if self.agent_operator_status_var:
                self.agent_operator_status_var.set("Out of steps — send Continue")
            self._agent_operator_log_line("status", f"MAX STEPS: {msg}\n")
            self._agent_operator_sync_buttons()
        elif kind == "log":
            self._agent_operator_log_line("dim", event.get("msg", "") + "\n")

    def _agent_operator_sync_buttons(self):
        running = bool(self.agent_operator_loop and self.agent_operator_loop.is_running())
        paused = bool(self.agent_operator_loop and self.agent_operator_loop.is_paused())
        asking = bool(self.agent_operator_loop and self.agent_operator_loop.is_awaiting_answer())
        stopping = bool(self.agent_operator_stop_requested and running)
        for button, enabled in (
            (self.agent_operator_run_btn, not running),
            (self.agent_operator_followup_btn, running),
            (self.agent_operator_pause_btn, running and not stopping),
            (self.agent_operator_stop_btn, running),
            (self.agent_operator_clear_run_btn, True),
        ):
            if not button:
                continue
            try:
                button.configure(state="normal" if enabled else "disabled")
            except tk.TclError:
                pass
        if self.agent_operator_pause_btn:
            try:
                label = "Resume" if (paused and not asking) else "Pause"
                self.agent_operator_pause_btn.configure(text=label)
            except tk.TclError:
                pass
        if self.agent_operator_followup_btn:
            try:
                more_steps = bool(self.agent_operator_loop and getattr(
                    self.agent_operator_loop, "_awaiting_more_steps", False))
                label = ("Continue" if more_steps
                         else "Answer" if asking
                         else "Send follow-up")
                self.agent_operator_followup_btn.configure(text=label)
            except tk.TclError:
                pass

    def agent_operator_send_followup(self):
        if not self.agent_operator_task:
            return "break"
        text = self.agent_operator_task.get("1.0", "end").strip()
        if not text:
            return "break"
        loop = self.agent_operator_loop
        if not loop or not loop.is_running():
            return self.agent_operator_run()
        extra_steps = None
        if loop.is_awaiting_answer() and getattr(loop, "_awaiting_more_steps", False):
            try:
                extra_steps = max(1, min(200, int(float(self.agent_operator_steps_var.get()))))
            except (TypeError, ValueError, AttributeError):
                extra_steps = 25
        if loop.add_follow_up(text, extra_steps=extra_steps):
            self._agent_operator_log_line("ts", f"\n[{self._ts()}] ")
            self._agent_operator_log_line("status", f"FOLLOW-UP: {text}\n")
            self._phone_mirror_write({"type": "follow_up", "ts": time.time(),
                                       "text": text,
                                       "answering_ask": bool(loop.is_awaiting_answer())})
            self.agent_operator_task.delete("1.0", "end")
            if self.agent_operator_status_var:
                self.agent_operator_status_var.set("Follow-up sent")
        return "break"

    def agent_operator_clear_run(self):
        self._remote_operator_clear()
        return "break"

    def _agent_operator_log_line(self, tag, text):
        self.agent_operator_log_buffer.append((tag, text))
        if len(self.agent_operator_log_buffer) > 500:
            self.agent_operator_log_buffer = self.agent_operator_log_buffer[-500:]
        if self.agent_operator_log:
            try:
                self.agent_operator_log.configure(state="normal")
                self.agent_operator_log.insert("end", text, tag)
                self.agent_operator_log.configure(state="disabled")
                self.agent_operator_log.see("end")
            except tk.TclError:
                pass
        # Phone/headless runs do not necessarily have the full Operator tab
        # mounted. The native HUD must still receive every thought and action.
        self._agent_operator_control_update_log()

    def _agent_operator_render_log_buffer(self):
        if not self.agent_operator_log:
            return
        try:
            self.agent_operator_log.configure(state="normal")
            self.agent_operator_log.delete("1.0", "end")
            for tag, text in self.agent_operator_log_buffer:
                self.agent_operator_log.insert("end", text, tag)
            self.agent_operator_log.configure(state="disabled")
            self.agent_operator_log.see("end")
        except tk.TclError:
            pass

    def _agent_operator_control_show(self, monitor):
        if not monitor:
            return
        # aiOS remains visible/clickable and appears in normal screen sharing.
        # Only OPERATOR's own frame grabs temporarily exclude it.
        self._operator_input_passthrough = True
        self._show_aios_windows_in_normal_capture(passive=False)
        self.agent_operator_control_monitor = monitor
        self.agent_operator_control_visible = True
        if self.agent_operator_native_overlay:
            self.agent_operator_native_overlay.show(monitor)
            self._agent_operator_control_update_log()
        self._agent_operator_control_draw()
        if self.agent_operator_control_after is None:
            self._agent_operator_control_tick()

    def _agent_operator_control_create_windows(self):
        windows = {}
        for name in ("top", "bottom", "left", "right"):
            win = tk.Toplevel(self.root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.configure(bg=self.c("accent"))
            try:
                win.attributes("-alpha", 0.45)
            except tk.TclError:
                pass
            self._agent_operator_control_make_passive(win)
            windows[name] = win

        label = tk.Toplevel(self.root)
        label.overrideredirect(True)
        label.attributes("-topmost", True)
        label.configure(bg=self.c("panel"))
        try:
            label.attributes("-alpha", 0.50)
        except tk.TclError:
            pass
        text = tk.Label(
            label,
            text="aiOPERATOR controlling computer",
            bg=self.c("panel"),
            fg=self.c("text"),
            font=self.brand_font(12),
            padx=14,
            pady=5,
        )
        text.pack(fill="both", expand=True)
        label._aios_text_label = text
        self._agent_operator_control_make_passive(label)
        windows["label"] = label
        return windows

    def _agent_operator_control_place_windows(self, monitor):
        windows = self.agent_operator_control_overlay
        if not windows:
            return
        border = 4
        inset = 8
        left = int(monitor.left)
        top = int(monitor.top)
        width = int(monitor.width)
        height = int(monitor.height)
        windows["top"].geometry(f"{max(1, width - inset * 2)}x{border}+{left + inset}+{top + inset}")
        windows["bottom"].geometry(
            f"{max(1, width - inset * 2)}x{border}+{left + inset}+{top + height - inset - border}"
        )
        windows["left"].geometry(f"{border}x{max(1, height - inset * 2)}+{left + inset}+{top + inset}")
        windows["right"].geometry(
            f"{border}x{max(1, height - inset * 2)}+{left + width - inset - border}+{top + inset}"
        )
        label_w = min(620, max(260, width - 80))
        label_h = 34
        label_x = left + max(20, (width - label_w) // 2)
        label_y = top + inset + 12
        windows["label"].geometry(f"{label_w}x{label_h}+{label_x}+{label_y}")

    def _agent_operator_control_make_passive(self, win):
        if not win or not sys.platform.startswith("win"):
            return
        try:
            win.update_idletasks()
            hwnd = win.winfo_id()
            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(
                hwnd,
                GWL_EXSTYLE,
                style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            )
            top_hwnd = user32.GetAncestor(hwnd, GA_ROOT) or hwnd
            user32.SetWindowDisplayAffinity(top_hwnd, WDA_NONE)
        except (tk.TclError, OSError):
            pass

    def _agent_operator_control_tick(self):
        self.agent_operator_control_after = None
        if not self.agent_operator_control_visible:
            return
        self.agent_operator_control_pulse = (self.agent_operator_control_pulse + 1) % 80
        self._agent_operator_control_draw()
        self.agent_operator_control_after = self.root.after(110, self._agent_operator_control_tick)

    def _agent_operator_control_draw(self):
        monitor = self.agent_operator_control_monitor
        if not monitor or not self.agent_operator_native_overlay:
            return
        phase = self.agent_operator_control_pulse
        wave = abs(40 - phase) / 40
        self.agent_operator_native_overlay.update(wave)

    def _agent_operator_control_update_log(self):
        if not self.agent_operator_native_overlay or not self.agent_operator_control_visible:
            return
        step = int(getattr(self, "agent_operator_overlay_step", 0) or 0)
        title = f"OPERATOR · STEP {step}" if step else "OPERATOR · STARTING"
        self.agent_operator_native_overlay.set_log(
            self._agent_operator_overlay_log_text(),
            title=title,
        )

    def _agent_operator_overlay_log_text(self):
        def clean(value, limit=260):
            text = " ".join(str(value or "").strip().split())
            return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

        thought = clean(getattr(self, "agent_operator_overlay_thought", ""))
        action = clean(getattr(self, "agent_operator_overlay_action", ""))
        rows = []
        if thought:
            rows.extend(("THINKING", thought))
        if action:
            rows.extend(("DID", action))
        return "\n".join(rows) or "THINKING\nLooking at the screen..."

    def _agent_operator_control_hide(self, temporary=False):
        self.agent_operator_control_visible = False
        if self.agent_operator_native_overlay:
            self.agent_operator_native_overlay.hide()
        if not temporary and self.agent_operator_control_after is not None:
            try:
                self.root.after_cancel(self.agent_operator_control_after)
            except tk.TclError:
                pass
            self.agent_operator_control_after = None

    def _agent_operator_control_stop(self):
        self._agent_operator_control_hide(temporary=False)
        self._operator_input_passthrough = False
        self._restore_aios_window_input()
        self._agent_capture_affinity_tokens.clear()
        self._show_aios_windows_in_normal_capture(passive=False)

    def _agent_operator_redraw_preview(self):
        if self.agent_operator_current_image is None or self.agent_operator_canvas is None:
            return
        try:
            cw = self.agent_operator_canvas.winfo_width()
            ch = self.agent_operator_canvas.winfo_height()
        except tk.TclError:
            return
        if cw < 10 or ch < 10:
            return
        iw, ih = self.agent_operator_current_image.size
        scale = min(cw / iw, ch / ih)
        nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
        img = self.agent_operator_current_image.resize((nw, nh)).copy()
        draw = self.agent_operator_ImageDraw.Draw(img, "RGBA")
        for lx, ly, button in self.agent_operator_last_clicks:
            cx, cy = int(lx * scale), int(ly * scale)
            color = {"left": (255, 80, 80, 255), "right": (80, 120, 255, 255), "middle": (80, 255, 140, 255)}.get(button, (255, 80, 80, 255))
            radius = 14
            draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], outline=color, width=3)
            draw.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=color)
        self.agent_operator_tk_preview = self.agent_operator_ImageTk.PhotoImage(img)
        ox, oy = (cw - nw) // 2, (ch - nh) // 2
        self.agent_operator_preview_scale = scale
        self.agent_operator_preview_origin = (ox, oy)
        try:
            self.agent_operator_canvas.delete("all")
            self.agent_operator_canvas.create_image(ox, oy, anchor="nw", image=self.agent_operator_tk_preview)
        except tk.TclError:
            pass

    def _agent_operator_flash_click(self, x, y, button="left"):
        # A temporary Tk click-ring would itself become a hit target and a
        # capture candidate. The native OPERATOR frame/log remains visible.
        if self.agent_operator_loop and self.agent_operator_loop.is_running():
            return
        try:
            win = tk.Toplevel(self.root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            try:
                win.attributes("-transparentcolor", "white")
            except tk.TclError:
                pass
            radius = 36
            win.geometry(f"{radius * 2}x{radius * 2}+{int(x) - radius}+{int(y) - radius}")
            canvas = tk.Canvas(win, width=radius * 2, height=radius * 2, bg="white", highlightthickness=0)
            canvas.pack()
            color = {"left": "#ff5050", "right": "#5080ff", "middle": "#50ff8c"}.get(button, "#ff5050")
            canvas.create_oval(4, 4, radius * 2 - 4, radius * 2 - 4, outline=color, width=4)
            canvas.create_oval(radius - 4, radius - 4, radius + 4, radius + 4, fill=color, outline=color)
            self._agent_operator_control_make_passive(win)

            def fade(step=0):
                try:
                    win.attributes("-alpha", max(0.0, 1.0 - step * 0.08))
                except tk.TclError:
                    return
                if step < 14:
                    win.after(50, lambda: fade(step + 1))
                else:
                    try:
                        win.destroy()
                    except tk.TclError:
                        pass

            fade()
        except tk.TclError:
            pass

    def _ts(self):
        return datetime.now().strftime("%H:%M:%S.%f")[:-3]

    # Sub-pages of the Settings tab. One screen per concern, so the page stops
    # being a single scroll of unrelated cards.
    SETTINGS_PAGES = (
        ("General", "Project folder, mobile remote and updates."),
        ("Appearance", "Colors, sizing and how the window behaves."),
        ("Voice", "Dictation keys, microphone and transcription quality."),
        ("Voice agent", "What the agent you talk to is allowed to do."),
        ("OPERATOR", "The agent that drives your mouse and keyboard."),
        ("Models", "Codex, quick chat and API keys."),
        ("Macro pad", "The buttons that drive aiOS from your macro keyboard."),
    )

    # ------------------------------------------------------------ settings shell

    def render_settings(self):
        self.page_title("Settings")
        names = [name for name, _hint in self.SETTINGS_PAGES]
        if getattr(self, "settings_page", None) not in names:
            self.settings_page = names[0]

        rail = tk.Frame(self.page, bg=self.c("panel"))
        rail.pack(fill="x", pady=(0, 2))
        for name in names:
            self.button(
                rail,
                name,
                lambda choice=name: self.open_settings_page(choice),
                compact=True,
                active=(name == self.settings_page),
            ).pack(side="left", padx=(0, 6))

        status = tk.Frame(self.page, bg=self.c("panel"))
        status.pack(fill="x", pady=(0, 10))
        hint = dict(self.SETTINGS_PAGES)[self.settings_page]
        tk.Label(
            status, text=hint, bg=self.c("panel"), fg=self.c("muted"), font=self.font(8), anchor="w"
        ).pack(side="left")
        self.settings_status_var = tk.StringVar(value="")
        tk.Label(
            status,
            textvariable=self.settings_status_var,
            bg=self.c("panel"),
            fg=self.c("success"),
            font=self.font(8, "bold"),
            anchor="e",
        ).pack(side="right")

        scroll = ScrollFrame(self.page, self.c("panel"))
        scroll.pack(fill="both", expand=True)
        body = scroll.inner
        {
            "General": self._settings_general,
            "Appearance": self._settings_appearance,
            "Voice": self._settings_voice,
            "Voice agent": self._settings_voice_agent,
            "OPERATOR": self._settings_operator,
            "Models": self._settings_models,
            "Macro pad": self._settings_macro_pad,
        }[self.settings_page](body)

    def open_settings_page(self, name):
        self.settings_page = name
        self.render_tab("Settings")

    # ------------------------------------------------------- settings building blocks

    def settings_group(self, parent, title, hint=""):
        """A titled card. Returns the frame to drop fields into."""
        card = self.card(parent)
        card.pack(fill="x", pady=(0, 12))
        tk.Label(
            card, text=title, bg=self.c("surface"), fg=self.c("text"), font=self.font(11, "bold")
        ).pack(anchor="w", padx=14, pady=(12, 2))
        if hint:
            tk.Label(
                card,
                text=hint,
                bg=self.c("surface"),
                fg=self.c("muted"),
                font=self.font(8),
                anchor="w",
                justify="left",
                wraplength=640,
            ).pack(fill="x", padx=14, pady=(0, 6))
        body = tk.Frame(card, bg=self.c("surface"))
        body.pack(fill="x", padx=14, pady=(4, 12))
        return body

    def settings_field(self, parent, label, hint=""):
        """One labelled row. Returns the frame the control goes in."""
        wrap = tk.Frame(parent, bg=self.c("surface"))
        wrap.pack(fill="x", pady=(0, 9))
        head = tk.Frame(wrap, bg=self.c("surface"))
        head.pack(fill="x")
        tk.Label(
            head,
            text=label,
            bg=self.c("surface"),
            fg=self.c("text"),
            font=self.font(9, "bold"),
            width=18,
            anchor="w",
        ).pack(side="left")
        control = tk.Frame(head, bg=self.c("surface"))
        control.pack(side="left", fill="x", expand=True)
        if hint:
            tk.Label(
                wrap,
                text=hint,
                bg=self.c("surface"),
                fg=self.c("muted"),
                font=self.font(8),
                anchor="w",
                justify="left",
                wraplength=640,
            ).pack(fill="x", padx=(4, 0), pady=(3, 0))
        return control

    def settings_saved(self, label=""):
        """Flash the autosave confirmation next to the page description."""
        var = getattr(self, "settings_status_var", None)
        if var is None:
            return
        var.set(f"Saved · {label}" if label else "Saved")
        pending = getattr(self, "_settings_status_after", None)
        if pending:
            try:
                self.root.after_cancel(pending)
            except tk.TclError:
                pass
        self._settings_status_after = self.root.after(2200, lambda: var.set(""))

    def settings_problem(self, message):
        var = getattr(self, "settings_status_var", None)
        if var is not None:
            var.set(message)

    def auto_entry(self, parent, label, value, on_commit, hint="", placeholder=""):
        """Text field that saves when you leave it or press Enter.

        Committing on blur rather than on every keystroke matters here: these
        fields hold paths and model names, and saving half-typed values would
        persist nonsense (or, for the project root, create nonsense folders).
        """
        control = self.settings_field(parent, label, hint)
        entry = self.single_line(control, value)
        entry.pack(side="left", fill="x", expand=True)
        if placeholder and not str(value).strip():
            entry.insert("1.0", "")
        state = {"last": str(value).strip()}

        def commit(_event=None):
            text = entry.get("1.0", "end").strip()
            if text == state["last"]:
                return None
            try:
                on_commit(text)
            except Exception as exc:
                self.settings_problem(f"{label}: {exc}")
                return None
            state["last"] = text
            self.settings_saved(label)
            return None

        entry.bind("<FocusOut>", commit)
        # Return would otherwise insert a newline into a one-line Text widget.
        entry.bind("<Return>", lambda event: (commit(), "break")[1])
        return entry

    def auto_toggle(self, parent, label, value, on_change, hint=""):
        control = self.settings_field(parent, label, hint)
        var = tk.BooleanVar(value=bool(value))

        def changed():
            on_change(var.get())
            self.settings_saved(label)

        tk.Checkbutton(
            control,
            variable=var,
            command=changed,
            bg=self.c("surface"),
            activebackground=self.c("surface"),
            fg=self.c("text"),
            selectcolor=self.c("panel2"),
            highlightthickness=0,
            bd=0,
        ).pack(side="left")
        return var

    def auto_scale(self, parent, label, start, end, value, on_change, hint="", suffix="", resolution=1):
        """Slider with a live readout that persists once you stop dragging."""
        control = self.settings_field(parent, label, hint)
        readout = tk.StringVar(value=f"{value:g}{suffix}" if isinstance(value, float) else f"{value}{suffix}")
        key = "_scale_after_" + re.sub(r"\W+", "_", label).lower()

        def changed(raw):
            readout.set(f"{float(raw):g}{suffix}")
            pending = getattr(self, key, None)
            if pending:
                try:
                    self.root.after_cancel(pending)
                except tk.TclError:
                    pass
            # Dragging fires this per pixel; only the settled value is written.
            setattr(self, key, self.root.after(220, lambda: self._commit_scale(key, on_change, raw)))

        scale = tk.Scale(
            control,
            from_=start,
            to=end,
            orient="horizontal",
            resolution=resolution,
            showvalue=0,
            bg=self.c("surface"),
            fg=self.c("text"),
            troughcolor=self.c("panel2"),
            highlightthickness=0,
            activebackground=self.c("accent"),
            sliderrelief="flat",
            bd=0,
        )
        scale.set(value)
        # Attached only after the initial value is in place — otherwise merely
        # opening the page would write to disk and flash "Saved".
        scale.configure(command=changed)
        scale.pack(side="left", fill="x", expand=True)
        tk.Label(
            control,
            textvariable=readout,
            bg=self.c("surface"),
            fg=self.c("muted"),
            font=self.font(9),
            width=6,
            anchor="e",
        ).pack(side="left", padx=(8, 0))
        return scale

    def _commit_scale(self, key, on_change, raw):
        setattr(self, key, None)
        on_change(raw)
        self.settings_saved(key.replace("_scale_after_", "").replace("_", " ").strip())

    def auto_option(self, parent, label, var, choices, on_change, hint=""):
        control = self.settings_field(parent, label, hint)
        menu = tk.OptionMenu(
            control,
            var,
            *choices,
            command=lambda _value: (on_change(), self.settings_saved(label)),
        )
        self.style_option(menu)
        menu.pack(side="left")
        return menu

    # ------------------------------------------------------------ settings pages

    def _settings_general(self, body):
        group = self.settings_group(
            body, "Project folder", "Where aiOS looks for your markdown projects."
        )
        self.root_entry = self.auto_entry(
            group,
            "Location",
            str(self.project_root),
            self._commit_project_root,
            hint="Saved when you leave the field or press Enter. The folder is created if missing.",
        )

        relay_cfg = self.config.get("phone_relay") or {}
        paired = bool(relay_cfg.get("machine_token"))
        group = self.settings_group(
            body,
            "Mobile remote",
            "Control OPERATOR from the aiOS phone app. Use the same private code on every PC.",
        )
        self.mobile_remote_url_entry = self.auto_entry(
            group, "Remote URL", relay_cfg.get("url", ""), lambda text: self._commit_relay("url", text)
        )
        self.mobile_remote_code_entry = self.auto_entry(
            group,
            "Private code",
            "",
            lambda _text: None,
            hint="Typed once to pair. Not stored in settings — press Connect after entering it.",
        )
        self.mobile_remote_name_entry = self.auto_entry(
            group,
            "Computer name",
            relay_cfg.get("machine_name") or os.environ.get("COMPUTERNAME", "My computer"),
            lambda text: self._commit_relay("machine_name", text),
        )
        self.mobile_remote_status_var = tk.StringVar(
            value=(f"Connected as {relay_cfg.get('machine_name') or 'this PC'}" if paired else "Not connected")
        )
        actions = tk.Frame(group, bg=self.c("surface"))
        actions.pack(fill="x", pady=(4, 0))
        self.button(actions, "Connect", self.pair_mobile_remote, compact=True).pack(side="left")
        self.button(actions, "Open remote", self.open_mobile_remote, compact=True).pack(side="left", padx=(8, 0))
        tk.Label(
            actions,
            textvariable=self.mobile_remote_status_var,
            bg=self.c("surface"),
            fg=self.c("muted"),
            font=self.font(8),
        ).pack(side="left", padx=(12, 0))

        self._render_update_card(body)

    def _settings_appearance(self, body):
        group = self.settings_group(body, "Window", "Applies as you drag.")
        self.auto_scale(
            group, "Opacity", 75, 100, int(float(self.c("opacity")) * 100), self.set_opacity, suffix="%"
        )
        self.auto_scale(group, "Text size", 8, 15, int(self.c("font_size")), self.set_font_size)
        self.auto_scale(group, "Corner radius", 12, 40, int(self.c("radius")), self.set_radius)
        self.auto_toggle(
            group,
            "Always on top",
            bool(self.c("always_on_top")),
            self.set_always_on_top,
            hint="Keeps aiOS above other windows.",
        )

        group = self.settings_group(body, "Thinking dots", "The pulse shown while a model is working.")
        self.auto_scale(
            group,
            "Base opacity",
            0,
            100,
            int(self.c("thinking_base_opacity")),
            lambda value: self.set_theme_int("thinking_base_opacity", value),
            suffix="%",
        )
        self.auto_scale(
            group,
            "Pulse opacity",
            0,
            100,
            int(self.c("thinking_pulse_opacity")),
            lambda value: self.set_theme_int("thinking_pulse_opacity", value),
            suffix="%",
        )

        self.settings_color_rows = {}
        group = self.settings_group(body, "Colors", "Click a swatch to change it.")
        for key, label in (
            ("accent", "Accent"),
            ("panel", "Panel"),
            ("surface", "Cards"),
            ("surface2", "Cards 2"),
            ("panel2", "Inputs"),
            ("text", "Text"),
            ("muted", "Muted"),
            ("success", "Success"),
            ("danger", "Danger"),
            ("thinking_base", "Dot base"),
            ("thinking_pulse", "Dot pulse"),
        ):
            self.color_row(group, key, label)

    def _settings_voice(self, body):
        voice_cfg = self._voice_cfg()
        separate = bool(voice_cfg.get("separate_hotkeys"))
        voice_ahk = voice_hotkey_to_ahk(voice_cfg.get("voice_hotkey") or "Insert")
        voice_key = voice_hotkey_label(voice_ahk)
        aios_ahk = voice_hotkey_to_ahk(voice_cfg.get("aios_hotkey") or "Insert")
        aios_key = voice_hotkey_label(aios_ahk)

        if separate:
            summary = (
                f"Quick press {voice_key} toggles dictation; holding it ≥0.6 s stops on release. "
                f"{aios_key} opens and closes aiOS."
            )
        else:
            summary = (
                f"Short press {voice_key} opens aiOS. Hold {voice_key} to dictate — release to stop and type."
            )
        group = self.settings_group(body, "Keys", summary)
        self.auto_toggle(
            group,
            "Separate keys",
            separate,
            self.set_voice_separate_hotkeys,
            hint="One key for dictation, another for opening aiOS. Recommended with a macro pad.",
        )
        key_control = self.settings_field(
            group,
            "Dictation key" if separate else "Shared key",
            "F13–F24 are macro-pad keys. Side mouse buttons work too. AutoHotkey picks changes up within ~2 s.",
        )
        self.voice_hotkey_var = tk.StringVar(value=voice_key)
        key_menu = tk.OptionMenu(
            key_control,
            self.voice_hotkey_var,
            *VOICE_HOTKEY_OPTIONS,
            command=lambda name: self._on_hotkey_choice("voice_hotkey", name),
        )
        self.style_option(key_menu)
        key_menu.pack(side="left", padx=(0, 8))
        self.button(key_control, "Capture…", lambda: self._capture_hotkey("voice_hotkey"), compact=True).pack(
            side="left"
        )

        if separate:
            open_control = self.settings_field(group, "Open aiOS key", "Immediate open/close — no hold wait.")
            self.aios_hotkey_var = tk.StringVar(value=aios_key)
            open_menu = tk.OptionMenu(
                open_control,
                self.aios_hotkey_var,
                *VOICE_HOTKEY_OPTIONS,
                command=lambda name: self._on_hotkey_choice("aios_hotkey", name),
            )
            self.style_option(open_menu)
            open_menu.pack(side="left", padx=(0, 8))
            self.button(
                open_control, "Capture…", lambda: self._capture_hotkey("aios_hotkey"), compact=True
            ).pack(side="left")
        else:
            self.auto_scale(
                group,
                "Hold threshold",
                150,
                800,
                int(voice_cfg.get("hold_ms", 280)),
                self.set_voice_hold_ms,
                hint="Held longer than this means dictate; shorter means open aiOS.",
                suffix=" ms",
            )

        group = self.settings_group(body, "Microphone", "What aiOS listens to, and how hard it listens.")
        self.voice_input_device_entry = self.auto_entry(
            group,
            "Device",
            voice_cfg.get("input_device", ""),
            lambda text: self._commit_voice_text("input_device", text),
            hint="Part of the device name, e.g. 'Yeti'. Leave empty to follow the Windows default.",
        )
        self.auto_scale(
            group,
            "Sensitivity",
            1,
            50,
            max(1, min(50, int(float(voice_cfg.get("silence_rms", 0.006)) * 10000))),
            self.set_voice_mic_sensitivity,
            hint="Lower picks up quieter speech but also more room noise.",
        )

        group = self.settings_group(
            body, "Transcription", "Whisper runs locally. Bigger models are slower but hear you better."
        )
        self.voice_model_settings_entry = self.auto_entry(
            group,
            "Model",
            voice_cfg.get("whisper_model", "small"),
            lambda text: self._commit_voice_text("whisper_model", text),
            hint="large-v3-turbo is the best pick on a GPU: near-large accuracy at small speed. "
            "Also: tiny, base, small, medium, large-v3, distil-large-v3, and .en variants. "
            "Changes preload in the background; large models can take about a minute to become ready.",
        )
        self.voice_language_var = tk.StringVar(value=voice_cfg.get("language", "auto"))
        self.auto_option(
            group,
            "Language",
            self.voice_language_var,
            WHISPER_LANGUAGES,
            lambda: self._commit_voice_var("language", self.voice_language_var),
            hint="Auto and Swedish need a multilingual model (small, not small.en).",
        )
        self.voice_compute_var = tk.StringVar(value=voice_cfg.get("compute_type", "int8"))
        self.auto_option(
            group,
            "Compute",
            self.voice_compute_var,
            COMPUTE_TYPES,
            lambda: self._commit_voice_var("compute_type", self.voice_compute_var),
            hint="float16 on a GPU, int8 on CPU.",
        )
        self.voice_vocabulary_entry = self.auto_entry(
            group,
            "Vocabulary",
            ", ".join(voice_cfg.get("vocabulary") or []),
            lambda text: self._commit_voice_text("vocabulary", text),
            hint="Names Whisper has never heard, comma separated. Biases the decoder toward them.",
        )
        self.voice_replacements_entry = self.auto_entry(
            group,
            "Fix words",
            ", ".join(f"{key}={value}" for key, value in (voice_cfg.get("replacements") or {}).items()),
            lambda text: self._commit_voice_text("replacements", text),
            hint="wrong=right pairs applied to every transcript, e.g. ayos=aiOS, operator=OPERATOR.",
        )
        self.auto_toggle(
            group,
            "Filter silence junk",
            bool(voice_cfg.get("hallucination_filter", True)),
            self.set_voice_hallucination_filter,
            hint="Drops the stock phrases Whisper invents on silence instead of typing them.",
        )
        self.auto_toggle(
            group,
            "Keep transcript log",
            bool(voice_cfg.get("transcript_history", True)),
            self.set_voice_transcript_history,
            hint="Appends every finished turn to voice-transcripts.jsonl (git-ignored).",
        )

        group = self.settings_group(body, "Output", "What happens when a turn finishes.")
        self.auto_scale(
            group,
            "Overlay opacity",
            20,
            100,
            int(voice_cfg.get("overlay_opacity", 85)),
            self.set_voice_overlay_opacity,
            hint="Background of the dictation pill and the agent chat panel.",
            suffix="%",
        )
        self.auto_scale(
            group,
            "Typing delay",
            0,
            50,
            int(voice_cfg.get("typing_delay_ms", 0)),
            self.set_voice_typing_delay_ms,
            hint="Raise this only if an app drops characters when text is typed quickly.",
            suffix=" ms",
        )
        self.auto_toggle(
            group,
            "Speak replies",
            bool(voice_cfg.get("agent_tts_enabled", True)),
            self.set_voice_agent_tts,
            hint="Read the agent's answers out loud.",
        )
        self.auto_toggle(
            group,
            "Stop on new speech",
            bool(voice_cfg.get("barge_in", True)),
            self.set_voice_barge_in,
            hint="Cuts the spoken reply the moment you press to talk, so it never talks into your mic.",
        )

        group = self.settings_group(body, "Discord", "Mute yourself in Discord while dictating.")
        self.auto_toggle(
            group,
            "Mute while dictating",
            bool(voice_cfg.get("discord_mute_enabled")),
            self.set_voice_discord_mute_enabled,
        )
        self.voice_discord_hotkey_entry = self.auto_entry(
            group,
            "Mute key",
            voice_cfg.get("discord_mute_hotkey", ""),
            lambda text: self._commit_voice_text("discord_mute_hotkey", text),
            hint="Match Discord → Keybinds → Toggle Mute. Combos work: Alt+M, Ctrl+Shift+M, F8. "
            "Applies after AutoHotkey reloads.",
        )

    def _settings_voice_agent(self, body):
        voice_cfg = self._voice_cfg()
        group = self.settings_group(
            body,
            "Tools",
            "Each switch adds or removes a capability. Off means the tool is never offered to the model.",
        )
        for key, label, hint in (
            ("agent_web_search", "Web search", "Look things up online."),
            ("agent_open_apps", "Open apps & URLs", "Launch Start Menu apps and web pages."),
            ("agent_shell", "PowerShell", "Run local commands for facts and small changes."),
            ("agent_operator", "OPERATOR", "Hand multi-step GUI work to the computer-use agent."),
            ("agent_clipboard_read", "Read clipboard", "Use what you just copied as context."),
            ("agent_screen", "Read screen", "Look at the monitor and answer questions about it."),
            ("agent_files", "Files", "Read and write text files in your allowed folders."),
            ("agent_media", "Volume & media", "Set the volume, play/pause, skip tracks."),
            ("agent_timers", "Reminders", "Schedule reminders that are spoken out loud."),
            ("agent_windows", "Windows", "List, focus and close open windows."),
            ("agent_remember", "Long-term memory", "Remember facts about you across restarts."),
        ):
            self.auto_toggle(
                group,
                label,
                bool(voice_cfg.get(key, True)),
                lambda value, name=key: self.set_voice_agent_tool(name, value),
                hint=hint,
            )

        group = self.settings_group(
            body,
            "Shell safety",
            "The agent's input is dictated speech, so a mis-heard sentence must not be able to do damage.",
        )
        self.auto_toggle(
            group,
            "Block destructive",
            bool(voice_cfg.get("agent_shell_guard", True)),
            lambda value: self.set_voice_agent_tool("agent_shell_guard", value),
            hint="Refuses recursive deletes, formats, shutdowns and download-and-run outright.",
        )
        self.auto_toggle(
            group,
            "Confirm changes",
            bool(voice_cfg.get("agent_shell_confirm", True)),
            lambda value: self.set_voice_agent_tool("agent_shell_confirm", value),
            hint="Anything that changes state is read back to you and waits for a spoken yes.",
        )

        group = self.settings_group(body, "Conversation", "How much the agent carries between turns.")
        self.auto_toggle(
            group,
            "Remember chat",
            bool(voice_cfg.get("agent_persist_memory", True)),
            lambda value: self.set_voice_agent_tool("agent_persist_memory", value),
            hint="Keeps the conversation across a restart of the dictation process.",
        )
        self.auto_scale(
            group,
            "Forget after",
            0,
            120,
            int(voice_cfg.get("agent_memory_minutes", 10)),
            lambda value: self._commit_voice_number("agent_memory_minutes", value),
            hint="Minutes of silence before the conversation resets. 0 never forgets.",
            suffix=" min",
        )
        self.auto_scale(
            group,
            "Tool rounds",
            1,
            12,
            int(voice_cfg.get("agent_max_rounds", 6)),
            lambda value: self._commit_voice_number("agent_max_rounds", value),
            hint="How many times the agent may call tools before it must answer.",
        )

    def _settings_operator(self, body):
        settings = self.config.get("ai_operator") or dict(DEFAULT_CONFIG["ai_operator"])
        # Bound to the same variables the OPERATOR tab uses, so the two screens
        # can never disagree about what is configured.
        if not self.agent_operator_model_var:
            self.agent_operator_model_var = tk.StringVar(
                value=str(settings.get("model") or self.agent_operator_default_model)
            )
        if not self.agent_operator_planner_model_var:
            self.agent_operator_planner_model_var = tk.StringVar(value=str(settings.get("planner_model") or "off"))
        if not self.agent_operator_reason_var:
            self.agent_operator_reason_var = tk.StringVar(value=str(settings.get("reasoning") or "low"))
        if not self.agent_operator_steps_var:
            self.agent_operator_steps_var = tk.StringVar(value=str(settings.get("steps") or "25"))
        if not self.agent_operator_delay_var:
            self.agent_operator_delay_var = tk.StringVar(value=str(settings.get("delay") or "0.20"))
        if not self.agent_operator_tts_var:
            self.agent_operator_tts_var = tk.BooleanVar(value=bool(settings.get("tts", False)))
        if not self.agent_operator_voice_var:
            self.agent_operator_voice_var = tk.StringVar(
                value=str(settings.get("voice") or self.agent_operator_default_voice)
            )
        if not self.agent_operator_shell_var:
            self.agent_operator_shell_var = tk.BooleanVar(value=bool(settings.get("shell", False)))
        if not self.agent_operator_codex_var:
            self.agent_operator_codex_var = tk.BooleanVar(value=bool(settings.get("codex_auth", False)))

        save = self.save_agent_operator_settings
        group = self.settings_group(body, "Models", "Which models plan and drive the run.")
        model_choices = ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"]
        if self.agent_operator_model_var.get() not in model_choices:
            model_choices.insert(0, self.agent_operator_model_var.get())
        self.auto_option(
            group,
            "Acting model",
            self.agent_operator_model_var,
            model_choices,
            save,
            hint="Looks at the screen and decides the next click.",
        )
        planner_choices = ["off", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"]
        if self.agent_operator_planner_model_var.get() not in planner_choices:
            planner_choices.insert(0, self.agent_operator_planner_model_var.get())
        self.auto_option(
            group,
            "Planning model",
            self.agent_operator_planner_model_var,
            planner_choices,
            save,
            hint="Optional second model that writes the plan before OPERATOR starts.",
        )
        self.auto_option(
            group,
            "Reasoning",
            self.agent_operator_reason_var,
            ("minimal", "low", "medium", "high", "xhigh", "max"),
            save,
            hint="Higher thinks longer per step. Costs more and runs slower.",
        )

        group = self.settings_group(body, "Run limits", "Guard rails for a run that goes wrong.")
        self.auto_scale(
            group,
            "Max steps",
            1,
            200,
            int(float(self.agent_operator_steps_var.get() or 25)),
            lambda value: self._commit_operator_var(self.agent_operator_steps_var, int(float(value))),
            hint="OPERATOR stops and asks once it has used this many actions.",
        )
        self.auto_scale(
            group,
            "Step delay",
            0.0,
            3.0,
            float(self.agent_operator_delay_var.get() or 0.20),
            lambda value: self._commit_operator_var(self.agent_operator_delay_var, f"{float(value):.2f}"),
            hint="Pause between actions. Raise it if apps cannot keep up.",
            suffix=" s",
            resolution=0.05,
        )

        group = self.settings_group(body, "Behaviour", "What OPERATOR may do and how it reports back.")
        self.auto_toggle(
            group,
            "Speak progress",
            bool(self.agent_operator_tts_var.get()),
            lambda value: self._commit_operator_flag(self.agent_operator_tts_var, value),
            hint="Narrates what it is doing out loud.",
        )
        self.auto_toggle(
            group,
            "Allow shell",
            bool(self.agent_operator_shell_var.get()),
            lambda value: self._commit_operator_flag(self.agent_operator_shell_var, value),
            hint="Lets OPERATOR run commands instead of only clicking.",
        )
        self.auto_toggle(
            group,
            "Use Codex auth",
            bool(self.agent_operator_codex_var.get()),
            lambda value: self._commit_operator_flag(self.agent_operator_codex_var, value),
            hint="Bills through your Codex sign-in instead of the API key.",
        )
        tk.Label(
            group,
            text="Monitor choice, preview and cursor test live on the OPERATOR tab, next to the run controls.",
            bg=self.c("surface"),
            fg=self.c("muted"),
            font=self.font(8),
            anchor="w",
            justify="left",
            wraplength=560,
        ).pack(fill="x", pady=(2, 0))

    def _settings_models(self, body):
        group = self.settings_group(body, "Codex", "The coding agent behind the Codex tab.")
        self.codex_model_settings_entry = self.auto_entry(
            group, "Model", self.config["codex_model"], lambda text: self._commit_config("codex_model", text)
        )
        self.settings_reasoning_var = tk.StringVar(value=self.config["codex_reasoning"])
        self.auto_option(
            group,
            "Thinking",
            self.settings_reasoning_var,
            ("none", "low", "medium", "high", "xhigh"),
            lambda: self._commit_config("codex_reasoning", self.settings_reasoning_var.get()),
        )

        group = self.settings_group(body, "Quick chat", "The faster model used for short questions.")
        self.quick_model_settings_entry = self.auto_entry(
            group,
            "Model",
            self.config["quick_codex_model"],
            lambda text: self._commit_config("quick_codex_model", text),
        )
        self.quick_reasoning_var = tk.StringVar(value=self.config["quick_codex_reasoning"])
        self.auto_option(
            group,
            "Thinking",
            self.quick_reasoning_var,
            ("none", "low", "medium", "high", "xhigh"),
            lambda: self._commit_config("quick_codex_reasoning", self.quick_reasoning_var.get()),
        )

        env_key = get_setting("OPENAI_API_KEY", "")
        stored_key = self.config.get("openai_api_key", "")
        group = self.settings_group(body, "OpenAI", "Used by the side chat, the voice agent and OPERATOR.")
        self.chat_model_settings_entry = self.auto_entry(
            group,
            "Chat model",
            self.config.get("chat_model", DEFAULT_CHAT_MODEL),
            lambda text: self._commit_config("chat_model", text),
        )
        self.openai_key_settings_entry = self.auto_entry(
            group,
            "API key",
            stored_key,
            self._commit_openai_key,
            hint=(
                "Currently using the OPENAI_API_KEY environment variable — leave empty to keep it."
                if env_key and not stored_key
                else "Stored in helper_config.json, which is git-ignored."
            ),
        )

    def _settings_macro_pad(self, body):
        group = self.settings_group(
            body,
            "How it works today",
            "Macro-pad buttons talk to aiOS over a local socket by running one of these files. "
            "Bind each one in your macro software.",
        )
        for filename, description in (
            ("voice_ptt_down.bat", "Button down: start dictating."),
            ("voice_ptt_up.bat", "Button release: stop and send."),
            ("voice_target_cursor.bat", "Send the transcript to whatever window is focused."),
            ("voice_target_clipboard.bat", "Copy the transcript instead of typing it."),
            ("voice_target_agent.bat", "Send the transcript to the voice agent."),
            ("voice_cancel.bat", "Throw the turn away without sending it."),
            ("voice_stop_agent.bat", "Panic button: stop the reply, the turn and any OPERATOR job."),
        ):
            row = tk.Frame(group, bg=self.c("surface"))
            row.pack(fill="x", pady=(0, 6))
            exists = (BASE_DIR / filename).exists()
            tk.Label(
                row,
                text=filename,
                bg=self.c("surface"),
                fg=self.c("text") if exists else self.c("danger"),
                font=self.font(9, "bold"),
                width=26,
                anchor="w",
            ).pack(side="left")
            tk.Label(
                row,
                text=description if exists else f"{description}  (missing)",
                bg=self.c("surface"),
                fg=self.c("muted"),
                font=self.font(8),
                anchor="w",
                justify="left",
                wraplength=420,
            ).pack(side="left", fill="x", expand=True)

        actions = tk.Frame(group, bg=self.c("surface"))
        actions.pack(fill="x", pady=(6, 0))
        self.button(
            actions,
            "Open folder",
            lambda: os.startfile(str(BASE_DIR)),
            compact=True,
        ).pack(side="left")

        group = self.settings_group(
            body,
            "Planned",
            "Macro-pad configuration is moving into aiOS so buttons can be bound here instead of "
            "through .bat files and external macro software.",
        )
        for line in (
            "Bind a button to any aiOS action from this page.",
            "Per-profile layouts that follow the focused app.",
            "Actions beyond voice: switch tabs, start an OPERATOR task, run a saved prompt.",
            "Live button feedback while a run is in progress.",
        ):
            tk.Label(
                group,
                text=f"·  {line}",
                bg=self.c("surface"),
                fg=self.c("muted"),
                font=self.font(9),
                anchor="w",
                justify="left",
                wraplength=600,
            ).pack(fill="x", pady=(0, 4))

    # -------------------------------------------------------- settings commit helpers

    def _commit_config(self, key, value):
        self.config[key] = value
        save_config(self.config)

    def _commit_openai_key(self, text):
        if text.startswith("(using OPENAI_API_KEY"):
            return
        self.config["openai_api_key"] = text
        save_config(self.config)
        self.refresh_chat_account()

    def _commit_project_root(self, text):
        if not text:
            return
        path = Path(text)
        path.mkdir(parents=True, exist_ok=True)
        self.project_root = path
        self.config["project_root"] = str(path)
        save_config(self.config)

    def _commit_relay(self, key, value):
        relay = self.config.setdefault("phone_relay", {})
        relay[key] = value
        save_config(self.config)

    def _commit_voice_text(self, key, text):
        """Settings-UI text field → voice config. merge parses the list forms."""
        self._voice_cfg()[key] = text
        self._save_voice_cfg()
        self._reload_voice_dictation()

    def _commit_voice_var(self, key, var):
        self._voice_cfg()[key] = var.get()
        self._save_voice_cfg()
        self._reload_voice_dictation()

    def _commit_voice_number(self, key, value):
        self._voice_cfg()[key] = int(float(value))
        self._save_voice_cfg()
        self._reload_voice_dictation()

    def _commit_operator_var(self, var, value):
        var.set(str(value))
        self.save_agent_operator_settings()

    def _commit_operator_flag(self, var, value):
        var.set(bool(value))
        self.save_agent_operator_settings()

    def _ensure_phone_relay(self):
        relay = self.config.get("phone_relay") or {}
        if not relay.get("enabled") or not relay.get("machine_token"):
            return
        try:
            subprocess.Popen(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(BASE_DIR / "start-phone-bridge.ps1"),
                ],
                cwd=str(BASE_DIR),
                creationflags=CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass

    def _phone_mirror_set_idle(self, reason=""):
        self._phone_mirror_write_status(running=False, reason=reason)

    def pair_mobile_remote(self):
        url = self.mobile_remote_url_entry.get("1.0", "end").strip()
        code = self.mobile_remote_code_entry.get("1.0", "end").strip()
        name = self.mobile_remote_name_entry.get("1.0", "end").strip() or os.environ.get("COMPUTERNAME", "My computer")
        if not url or not code:
            self.mobile_remote_status_var.set("Enter the remote URL and private code")
            return
        self.mobile_remote_status_var.set("Connecting securely…")

        def worker():
            try:
                from phone_relay import pair

                relay = pair(url, code, name)
                self.config = load_config()
                self.root.after(0, lambda: self._mobile_remote_paired(relay))
            except Exception as exc:
                self.root.after(0, lambda message=str(exc): self.mobile_remote_status_var.set(f"Could not connect: {message}"))

        threading.Thread(target=worker, daemon=True).start()

    def _mobile_remote_paired(self, relay):
        self.mobile_remote_code_entry.delete("1.0", "end")
        self.mobile_remote_status_var.set(f"Connected as {relay.get('machine_name') or 'this PC'}")
        self._ensure_phone_relay()

    def open_mobile_remote(self):
        relay = self.config.get("phone_relay") or {}
        url = self.mobile_remote_url_entry.get("1.0", "end").strip() if getattr(self, "mobile_remote_url_entry", None) else relay.get("url")
        if not url:
            if getattr(self, "mobile_remote_status_var", None):
                self.mobile_remote_status_var.set("Enter your remote URL first")
            return
        try:
            os.startfile(url)
        except OSError as exc:
            if getattr(self, "mobile_remote_status_var", None):
                self.mobile_remote_status_var.set(str(exc))

    def drop_zone(self, parent):
        zone = tk.Frame(parent, bg=self.c("surface2"), highlightbackground=self.c("accent"), highlightthickness=1, bd=0)
        title = "Drop files here"
        if self.dropped_paths:
            title = f"{len(self.dropped_paths)} item(s) ready"
        tk.Label(
            zone,
            text=title,
            bg=self.c("surface2"),
            fg=self.c("text"),
            font=self.font(13, "bold"),
        ).pack(anchor="w", padx=14, pady=(12, 2))
        text = "Create a new project from the drop, or add files to an existing project."
        tk.Label(zone, text=text, bg=self.c("surface2"), fg=self.c("muted"), font=self.font(9)).pack(anchor="w", padx=14, pady=(0, 12))
        return zone

    def color_row(self, parent, key, label):
        row = tk.Frame(parent, bg=self.c("surface"))
        row.pack(fill="x", padx=12, pady=(0, 8))
        tk.Label(row, text=label, bg=self.c("surface"), fg=self.c("muted"), font=self.font(9, "bold"), width=12, anchor="w").pack(side="left")
        swatch = tk.Label(row, text=" ", bg=self.c(key), width=4)
        swatch.pack(side="left", padx=(0, 8))
        entry = self.single_line(row, self.c(key))
        entry.pack(side="left", fill="x", expand=True)
        self.settings_color_rows[key] = entry
        self.button(row, "Pick", lambda value=key: self.pick_color(value), compact=True).pack(side="right", padx=(8, 0))
        self.button(row, "Apply", lambda value=key: self.apply_color(value), compact=True).pack(side="right", padx=(8, 0))

    def create_from_entry(self):
        self.open_create_project()

    def create_from_projects_tab(self):
        self.active_tab = "Projects"
        self.open_create_project()

    def new_project_from_drop(self):
        if not self.dropped_paths:
            self.local_reply("Drop files or folders first.")
            return
        name = self.drop_project_name.get("1.0", "end").strip()
        project_path, message = self.create_project_path(name)
        copied = self.copy_paths_to_project(project_path, self.dropped_paths)
        self._launch_codex_app(project_path)
        self.local_reply(f"{message}\nImported {copied} item(s).")
        self.render_tab("Drop")

    def add_drop_to_existing_project(self):
        if not self.dropped_paths:
            self.local_reply("Drop files or folders first.")
            return
        raw = self.drop_existing_entry.get("1.0", "end").strip()
        project = self._resolve_project(raw)
        if not project:
            self.local_reply(f"I could not find that project in {self.project_root}.")
            return
        copied = self.copy_paths_to_project(project, self.dropped_paths)
        self.local_reply(f"Imported {copied} item(s) into:\n{project}")
        self.render_tab("Drop")

    def copy_paths_to_project(self, project_path, paths):
        dest_root = project_path / "assets" / "imported"
        dest_root.mkdir(parents=True, exist_ok=True)
        count = 0
        for source in paths:
            source = Path(source)
            if not source.exists():
                continue
            dest = dest_root / source.name
            if dest.exists():
                dest = dest_root / f"{source.stem}-{int(time.time())}{source.suffix}"
            if source.is_dir():
                shutil.copytree(source, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(source, dest)
            count += 1
        return count

    def handle_drop(self, paths):
        self.dropped_paths = [Path(path) for path in paths]
        self.local_reply(f"Drop received: {len(self.dropped_paths)} item(s).")
        self.render_tab("Drop")
        self.show()

    def drop_default_project_name(self):
        if not self.dropped_paths:
            return "New Project"
        first = self.dropped_paths[0]
        return clean_project_name(first.stem if first.is_file() else first.name) or "New Project"

    def refresh_app_results(self):
        if not hasattr(self, "apps_area"):
            return
        self.clear(self.apps_area.inner)
        query = ""
        if hasattr(self, "app_search"):
            query = self.app_search.get("1.0", "end").strip().casefold()

        grouped = {}
        for app in self.apps:
            if query and query not in app["name"].casefold():
                continue
            grouped.setdefault(app["category"], []).append(app)

        if not grouped:
            self.muted(self.apps_area.inner, "No apps found.").pack(anchor="w", padx=6)
            return

        used = self.most_used_apps()
        if used and not query:
            self.section(self.apps_area.inner, "Most Used")
            for app in used[:6]:
                self.app_row(self.apps_area.inner, app).pack(fill="x", pady=(0, 8))

        for category in KNOWN_APPS:
            apps = grouped.get(category, [])
            if not apps:
                continue
            self.section(self.apps_area.inner, category)
            grid = tk.Frame(self.apps_area.inner, bg=self.c("panel"))
            grid.pack(fill="x", pady=(0, 10))
            for index, app in enumerate(apps):
                item = self.app_tile(grid, app)
                item.grid(row=index // 3, column=index % 3, sticky="ew", padx=4, pady=4)
            for column in range(3):
                grid.columnconfigure(column, weight=1)

    def _discover_apps(self):
        apps = []
        seen = set()
        for category, names in KNOWN_APPS.items():
            for name in names:
                app = self._resolve_app(name, category)
                if not app:
                    continue
                key = app["name"].casefold()
                if key in seen:
                    continue
                seen.add(key)
                apps.append(app)
        return apps

    def _resolve_app(self, name, category=None):
        if name in SYSTEM_APPS:
            kind, value = SYSTEM_APPS[name]
            return {"name": name, "category": category or "System", "kind": kind, "value": value}

        if name == "Codex":
            codex = find_codex()
            if codex:
                return {"name": "Codex", "category": category or "AI", "kind": "codex", "value": codex}

        key = normalize_name(name)
        candidates = [key, key.replace("visual studio code", "code"), key.replace("google chrome", "chrome")]
        for candidate in candidates:
            shortcut = self.shortcuts.get(candidate)
            if shortcut:
                return {"name": name, "category": category or "Apps", "kind": "shortcut", "value": str(shortcut)}

        for shortcut_key, shortcut in self.shortcuts.items():
            if key and (key in shortcut_key or shortcut_key in key):
                return {"name": name, "category": category or "Apps", "kind": "shortcut", "value": str(shortcut)}
        return None

    def launch_app(self, app):
        try:
            kind = app["kind"]
            value = app["value"]
            if kind in {"shortcut", "path", "uri"}:
                os.startfile(value)
            elif kind == "command":
                run_detached(value)
            elif kind == "codex":
                run_detached([value, "app", str(self.project_root)], cwd=self.project_root, env=codex_env())
            self.bump_app_usage(app["name"])
            self.local_reply(f"Opened {app['name']}.")
        except Exception as exc:
            self.local_reply(self._error_message(exc))

    def bump_app_usage(self, name):
        usage = self.config.setdefault("app_usage", {})
        usage[name] = int(usage.get(name, 0)) + 1
        save_config(self.config)

    def most_used_apps(self):
        usage = self.config.get("app_usage", {})
        ranked = sorted(
            self.apps,
            key=lambda app: (int(usage.get(app["name"], 0)), app["name"].casefold()),
            reverse=True,
        )
        return [app for app in ranked if usage.get(app["name"], 0)] or self.apps[:8]

    def projects(self):
        hidden = {self._project_key(p) for p in self.config.get("hidden_projects", [])}
        seen = set()
        rows = []
        if self.project_root.exists():
            for path in self.project_root.iterdir():
                if not path.is_dir():
                    continue
                key = self._project_key(path)
                if key in hidden or key in seen:
                    continue
                seen.add(key)
                rows.append(path)
        for raw in self.config.get("linked_projects", []):
            path = Path(raw)
            if not path.is_dir():
                continue
            key = self._project_key(path)
            if key in hidden or key in seen:
                continue
            seen.add(key)
            rows.append(path)
        return sorted(rows, key=lambda path: path.stat().st_mtime, reverse=True)

    def _project_key(self, path):
        try:
            return str(Path(path).resolve()).casefold()
        except OSError:
            return str(path).casefold()

    def open_add_folder(self):
        self._close_create_menu()
        folder = filedialog.askdirectory(parent=self.root, title="Select existing project folder")
        if folder:
            self.register_existing_project(folder)

    def register_existing_project(self, raw_path):
        path = Path(raw_path).resolve()
        if not path.is_dir():
            self.local_reply("That path is not a folder.")
            return None
        try:
            if path == self.project_root.resolve():
                self.local_reply("Pick a project folder, not the projects root.")
                return None
        except OSError:
            pass
        hidden = self.config.setdefault("hidden_projects", [])
        self.config["hidden_projects"] = [h for h in hidden if self._project_key(h) != self._project_key(path)]
        try:
            if path.parent != self.project_root.resolve():
                linked = self.config.setdefault("linked_projects", [])
                path_str = str(path)
                if path_str not in linked:
                    linked.append(path_str)
        except OSError:
            pass
        self.ensure_project_scaffold(path)
        save_config(self.config)
        self.open_project_detail(path)
        return path

    def remove_from_aios(self, project_path):
        project_path = Path(project_path).resolve()
        if not messagebox.askyesno(
            "Remove from aiOS",
            f"Remove '{project_path.name}' from aiOS?\n\nThe folder stays on disk:\n{project_path}",
        ):
            return
        hidden = self.config.setdefault("hidden_projects", [])
        path_str = str(project_path)
        keys = {self._project_key(h) for h in hidden}
        if self._project_key(project_path) not in keys:
            hidden.append(path_str)
        linked = self.config.get("linked_projects", [])
        self.config["linked_projects"] = [p for p in linked if self._project_key(p) != self._project_key(project_path)]
        save_config(self.config)
        self.close_detail()
        self.render_tab(self.active_tab)
        self.local_reply(f"Removed {project_path.name} from aiOS. Files were kept on disk.")

    def allocate_project_path(self, raw_name):
        base = clean_project_name(raw_name) or "New Project"
        candidate = self.project_root / base
        if not candidate.exists():
            return candidate
        counter = 2
        while (self.project_root / f"{base} ({counter})").exists():
            counter += 1
        return self.project_root / f"{base} ({counter})"

    def ensure_project_scaffold(self, project_path):
        project_path = Path(project_path)
        project_path.mkdir(parents=True, exist_ok=True)
        for folder in ("src", "docs", "assets", "notes"):
            (project_path / folder).mkdir(exist_ok=True)
        readme = project_path / "README.md"
        if not readme.exists():
            readme.write_text(f"# {project_path.name}\n\n", encoding="utf-8")
        agents = project_path / "AGENTS.md"
        if not agents.exists():
            agents.write_text(
                (
                    "# Project Instructions\n\n"
                    "- Keep changes focused and practical.\n"
                    "- Check implementations when it is quick and easy.\n"
                    "- Keep UI minimal, clean, and intuitive.\n"
                ),
                encoding="utf-8",
            )
        self.ensure_project_files(project_path)
        return project_path

    def list_project_entries(self, project_path, limit=500):
        project_path = Path(project_path)
        rows = []
        try:
            for path in sorted(project_path.rglob("*"), key=lambda p: (not p.is_dir(), str(p).casefold())):
                rel = path.relative_to(project_path).as_posix()
                if not rel:
                    continue
                try:
                    size = 0 if path.is_dir() else path.stat().st_size
                except OSError:
                    size = 0
                rows.append((rel, size, path.is_dir(), path))
                if len(rows) >= limit:
                    break
        except OSError:
            return []
        return rows

    def project_row(self, parent, project):
        row = self.card(parent)
        left = tk.Frame(row, bg=self.c("surface"))
        left.pack(side="left", fill="both", expand=True, padx=12, pady=10)
        tk.Button(
            left,
            text=project.name,
            command=lambda p=project: self.open_project_detail(p),
            bg=self.c("surface"),
            fg=self.c("text"),
            activebackground=self.c("surface"),
            activeforeground=self.c("accent"),
            relief="flat",
            bd=0,
            anchor="w",
            cursor="hand2",
            font=self.font(10, "bold"),
        ).pack(anchor="w")
        meta = self.load_project_meta(project)
        due = str(meta.get("due", "")).strip()
        if self.is_dashboard_todo(meta):
            meta_bits = [self.due_countdown(due) if due else "on dashboard"]
        else:
            meta_bits = [str(meta.get("status", "active"))]
            if due:
                meta_bits.append(self.due_countdown(due))
        self.muted(left, "  |  ".join(meta_bits)).pack(anchor="w")

        actions = tk.Frame(row, bg=self.c("surface"))
        actions.pack(side="right", padx=10, pady=10)
        self.button(actions, "Open", lambda p=project: self.open_project_detail(p), compact=True).pack(side="left", padx=3)
        self.button(actions, "Codex", lambda p=project: self.open_project_path(p), compact=True).pack(side="left", padx=3)
        self.button(actions, "Task", lambda p=project: self.codex_task_for_project(p), compact=True).pack(side="left", padx=3)
        self.button(actions, "Code", lambda p=project: self.open_code(p), compact=True).pack(side="left", padx=3)
        self.button(actions, "Memory", lambda p=project: os.startfile(str(self.ensure_project_memory(p))), compact=True).pack(side="left", padx=3)
        self.button(actions, "Files", lambda p=project: os.startfile(str(p)), compact=True).pack(side="left", padx=3)
        return row

    def app_row(self, parent, app):
        row = self.card(parent)
        tk.Label(row, text=app["name"], bg=self.c("surface"), fg=self.c("text"), font=self.font(10, "bold")).pack(side="left", padx=12, pady=10)
        self.button(row, "Open", lambda value=app: self.launch_app(value), compact=True).pack(side="right", padx=10, pady=8)
        return row

    def app_tile(self, parent, app):
        return tk.Button(
            parent,
            text=app["name"],
            command=lambda value=app: self.launch_app(value),
            bg=self.c("surface"),
            fg=self.c("text"),
            activebackground=self.c("panel2"),
            activeforeground=self.c("text"),
            relief="flat",
            bd=0,
            padx=10,
            pady=12,
            cursor="hand2",
            anchor="w",
            font=self.font(9, "bold"),
        )

    def open_project_path(self, project_path):
        self._launch_codex_app(project_path)
        self.local_reply(f"Opened Codex Desktop:\n{project_path}")

    def codex_task_for_project(self, project_path):
        self.active_project = project_path
        self.render_tab("CODE")

    def open_code(self, project_path):
        code = shutil.which("code") or shutil.which("code.cmd")
        if not code:
            self.local_reply("VS Code command was not found.")
            return
        run_detached([code, str(project_path)], cwd=project_path)
        self.local_reply(f"Opened VS Code:\n{project_path}")

    def disk_free(self, drive):
        try:
            free = shutil.disk_usage(drive).free / (1024**3)
            return f"{free:.0f} GB"
        except OSError:
            return "-"

    def _handle_local_command(self, prompt):
        text = prompt.strip()
        lower = text.casefold()

        if lower in {"dashboard", "projects", "code", "codex", "apps", "drop", "ai operator", "aioperator", "operator", "settings"}:
            tab = "AI Operator" if lower in {"ai operator", "aioperator", "operator"} else "CODE" if lower in {"code", "codex"} else lower.title()
            self.root.after(0, lambda value=tab: self.render_tab(value))
            return f"Switched to {tab}."

        if lower in {"commands", "help", "what can you do"}:
            return (
                "new project NAME\n"
                "open project NAME\n"
                "open app NAME\n"
                "ask codex in PROJECT to TASK\n"
                "codex task PROJECT: TASK"
            )

        if lower in {"list projects", "show projects"}:
            return self._list_projects()

        task_match = (
            re.match(r"^ask\s+codex\s+in\s+(.+?)\s+to\s+(.+)$", text, flags=re.IGNORECASE)
            or re.match(r"^codex\s+task\s+(.+?)\s*:\s*(.+)$", text, flags=re.IGNORECASE)
        )
        if task_match:
            project = self._resolve_project(task_match.group(1))
            if not project:
                return f"I could not find that project in {self.project_root}."
            self.root.after(0, lambda p=project, task=task_match.group(2): self.run_codex_task(p, task))
            return f"Started task in {project.name}."

        new_match = re.match(
            r"^(new|create|make)\s+(?:a\s+)?(?:codex\s+)?project(?:\s+(called|named))?\s+(.+)$",
            text,
            flags=re.IGNORECASE,
        )
        if new_match:
            reply = self._create_project(new_match.group(3))
            self.root.after(0, lambda: self.render_tab(self.active_tab))
            return reply

        open_match = (
            re.match(r"^(open|start|launch)\s+(?:codex\s+)?project\s+(.+)$", text, flags=re.IGNORECASE)
            or re.match(r"^open\s+(.+?)\s+in\s+codex$", text, flags=re.IGNORECASE)
            or re.match(r"^start\s+codex\s+(.+)$", text, flags=re.IGNORECASE)
            or re.match(r"^codex\s+(.+)$", text, flags=re.IGNORECASE)
        )
        if open_match:
            return self._open_project(open_match.group(open_match.lastindex))

        open_app = re.match(r"^(open|launch|start)\s+(?:app\s+)?(.+)$", text, re.IGNORECASE)
        if open_app:
            app = self.find_app(open_app.group(2))
            if app:
                self.root.after(0, lambda value=app: self.launch_app(value))
                return f"Opening {app['name']}."

        return ""

    def find_app(self, raw_name):
        query = clean_project_name(raw_name).casefold()
        exact = [app for app in self.apps if app["name"].casefold() == query]
        if exact:
            return exact[0]
        matches = [app for app in self.apps if query in app["name"].casefold()]
        return matches[0] if len(matches) == 1 else None

    def _list_projects(self):
        projects = self.projects()
        if not projects:
            return f"No projects found in {self.project_root}"
        names = "\n".join(path.name for path in projects[:12])
        return f"Recent projects:\n{names}"

    def _create_project(self, raw_name):
        project_path, message = self.create_project_path(raw_name)
        self._launch_codex_app(project_path)
        return message + f"\nOpened Codex Desktop:\n{project_path}"

    def create_project_path(self, raw_name):
        name = clean_project_name(raw_name)
        if not name:
            name = "New Project"
        project_path = self.project_root / name
        project_path.mkdir(parents=True, exist_ok=True)
        for folder in ("src", "docs", "assets", "notes"):
            (project_path / folder).mkdir(exist_ok=True)
        readme = project_path / "README.md"
        if not readme.exists():
            readme.write_text(f"# {name}\n\n", encoding="utf-8")
        agents = project_path / "AGENTS.md"
        if not agents.exists():
            agents.write_text(
                (
                    "# Project Instructions\n\n"
                    "- Keep changes focused and practical.\n"
                    "- Check implementations when it is quick and easy.\n"
                    "- Keep UI minimal, clean, and intuitive.\n"
                ),
                encoding="utf-8",
            )
        self.ensure_project_files(project_path)
        return project_path, f"Created project:\n{project_path}"

    def ensure_project_memory(self, project_path):
        memory = Path(project_path) / "aios-memory.md"
        if not memory.exists():
            memory.write_text(
                (
                    "# aiOS Project Memory\n\n"
                    "## Purpose\n"
                    "- Not set yet.\n\n"
                    "## Decisions\n"
                    "- None yet.\n\n"
                    "## Current State\n"
                    "- New project.\n\n"
                    "## Next Actions\n"
                    "- Define the first useful task.\n\n"
                    "## Notes\n"
                    "- Keep this file short and current.\n"
                ),
                encoding="utf-8",
            )
        return memory

    def read_project_memory(self, project_path):
        memory = self.ensure_project_memory(project_path)
        try:
            text = memory.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""
        return text[:2000]

    def ensure_project_files(self, project_path):
        project_path = Path(project_path)
        self.ensure_project_memory(project_path)
        summary = project_path / PROJECT_SUMMARY_FILE
        if not summary.exists():
            summary.write_text(
                f"# {project_path.name}\n\nShort summary of what this project is for.\n",
                encoding="utf-8",
            )
        meta = project_path / PROJECT_META_FILE
        if not meta.exists():
            self.save_project_meta(project_path, dict(DEFAULT_PROJECT_META))
        return summary, meta

    def load_project_meta(self, project_path):
        self.ensure_project_files(project_path)
        meta_path = Path(project_path) / PROJECT_META_FILE
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        merged = dict(DEFAULT_PROJECT_META)
        if isinstance(data, dict):
            merged.update(data)
        if not isinstance(merged.get("tags"), list):
            merged["tags"] = []
        merged["tracked_as_todo"] = bool(merged.get("tracked_as_todo"))
        merged["done"] = bool(merged.get("done"))
        merged["due"] = str(merged.get("due", "")).strip()
        merged["title"] = str(merged.get("title", "")).strip()
        merged["created"] = str(merged.get("created", "")).strip()
        return merged

    def save_project_meta(self, project_path, meta):
        project_path = Path(project_path)
        project_path.mkdir(parents=True, exist_ok=True)
        payload = dict(DEFAULT_PROJECT_META)
        payload.update(meta or {})
        if not isinstance(payload.get("tags"), list):
            payload["tags"] = [t.strip() for t in str(payload.get("tags", "")).split(",") if t.strip()]
        payload["tracked_as_todo"] = bool(payload.get("tracked_as_todo"))
        payload["done"] = bool(payload.get("done"))
        payload["due"] = str(payload.get("due", "")).strip()
        payload["title"] = str(payload.get("title", "")).strip()
        payload["created"] = str(payload.get("created", "")).strip() or datetime.now().isoformat(timespec="seconds")
        meta_path = project_path / PROJECT_META_FILE
        meta_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self.sync_project_todos_memory(project_path.name)

    def load_project_summary(self, project_path):
        summary_path = self.ensure_project_files(project_path)[0]
        try:
            return summary_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def save_project_summary(self, project_path, text):
        summary_path = self.ensure_project_files(project_path)[0]
        summary_path.write_text(str(text), encoding="utf-8")

    def list_project_files(self, project_path, limit=400):
        project_path = Path(project_path)
        rows = []
        try:
            for path in sorted(project_path.rglob("*")):
                if path.is_file():
                    rel = path.relative_to(project_path).as_posix()
                    try:
                        size = path.stat().st_size
                    except OSError:
                        size = 0
                    rows.append((rel, size))
                if len(rows) >= limit:
                    break
        except OSError:
            return []
        return rows

    def format_file_size(self, size):
        if size < 1024:
            return f"{size} B"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        return f"{size / (1024 * 1024):.1f} MB"

    def _find_todo(self, todo_id):
        for item in self.config.get("todos", []):
            if str(item.get("id")) == str(todo_id):
                return item
        return None

    def project_has_active_todo(self, project_name):
        project_path = self._resolve_project(project_name)
        if not project_path:
            return False
        return self.is_dashboard_todo(self.load_project_meta(project_path))

    def add_project_as_todo(self, project_path, quiet=False):
        self.pin_project_todo(project_path)
        if not quiet:
            self.local_reply(f"Pinned {Path(project_path).name} to the dashboard.")
        if self.page_view and self.page_view[0] == "project":
            self._render_detail()

    def set_project_tracked(self, project_path, tracked):
        meta = self.load_project_meta(project_path)
        meta["tracked_as_todo"] = bool(tracked)
        self.save_project_meta(project_path, meta)
        if self.page_view and self.page_view[0] == "project":
            self._render_detail()

    def _open_project(self, raw_name):
        project_path = self._resolve_project(raw_name)
        if not project_path:
            return f"I could not find that project in {self.project_root}."
        self.ensure_project_memory(project_path)
        self._launch_codex_app(project_path)
        return f"Opened Codex Desktop:\n{project_path}"

    def _resolve_project(self, raw_name):
        name = clean_project_name(raw_name)
        if not name or not self.project_root.exists():
            return None
        exact = self.project_root / name
        if exact.is_dir():
            return exact
        lowered = name.casefold()
        projects = self.projects()
        for path in projects:
            if path.name.casefold() == lowered:
                return path
        matches = [path for path in projects if lowered in path.name.casefold()]
        if len(matches) == 1:
            return matches[0]
        return None

    def _launch_codex_app(self, project_path):
        codex = find_codex()
        if not codex:
            raise RuntimeError("I could not find codex.exe.")
        run_detached([codex, "app", str(project_path)], cwd=project_path, env=codex_env())

    def default_project_path(self):
        projects = self.projects()
        if self.active_project and Path(self.active_project).exists():
            return Path(self.active_project)
        return projects[0] if projects else self.project_root

    def run_codex_from_tab(self):
        if self.active_tab == "CODE" and hasattr(self, "code_brief"):
            self._code_start_job()

    def codex_open_desktop(self):
        project = Path(self.code_project_var.get().strip() if hasattr(self, "code_project_var") else self.default_project_path())
        self._launch_codex_app(project)
        self.local_reply(f"Opened Codex Desktop:\n{project}")

    def save_codex_settings(self):
        if hasattr(self, "codex_model_entry"):
            self.config["codex_model"] = self.codex_model_entry.get("1.0", "end").strip() or DEFAULT_CHAT_MODEL
        if hasattr(self, "codex_reasoning_var"):
            self.config["codex_reasoning"] = self.codex_reasoning_var.get() or "none"
        save_config(self.config)
        self.subtitle.configure(text=self._status_subtitle())

    def run_codex_task(self, project_path, prompt):
        project_path = Path(project_path)
        project_path.mkdir(parents=True, exist_ok=True)
        self.ensure_project_memory(project_path)
        self.active_project = project_path
        self.render_tab("CODE")
        model = str(self.config.get("codex_model") or "gpt-5.6-sol")
        reasoning = str(self.config.get("codex_reasoning") or "medium")
        payload = {
            "provider": "codex",
            "cwd": str(project_path),
            "brief": str(prompt),
            "model": model,
            "reasoning": reasoning,
            "fast": bool(self.config.get("codex_fast", False)),
        }

        def worker():
            result = self._code_api("/api/code/jobs", "POST", payload, timeout=40)
            self.root.after(0, lambda: self._code_started(result))

        threading.Thread(target=worker, daemon=True, name="aios-code-legacy-start").start()

    def _run_codex_worker(self, project_path, prompt):
        codex = find_codex()
        if not codex:
            self.root.after(0, lambda: self.append_codex_output("codex.exe was not found.\n"))
            return

        output_file = Path(tempfile.gettempdir()) / f"aios-codex-{int(time.time())}.txt"
        reasoning = self.config.get("codex_reasoning", "none")
        model = self.config.get("codex_model", DEFAULT_CHAT_MODEL)
        args = [
            codex,
            "exec",
            "-C",
            str(project_path),
            "--skip-git-repo-check",
            "--sandbox",
            self.config.get("codex_sandbox", "workspace-write"),
            "-m",
            model,
            "-c",
            f'model_reasoning_effort="{reasoning}"',
            "--color",
            "never",
            "-o",
            str(output_file),
            prompt,
        ]
        self.root.after(0, lambda: self.append_codex_output(f"Running: codex exec -m {model} reasoning={reasoning}\n\n"))
        try:
            self.codex_process = subprocess.Popen(
                args,
                cwd=str(project_path),
                env=codex_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=CREATE_NO_WINDOW,
            )
            assert self.codex_process.stdout is not None
            for line in self.codex_process.stdout:
                self.root.after(0, lambda text=line: self.append_codex_output(text))
            code = self.codex_process.wait()
            if output_file.exists():
                final = output_file.read_text(encoding="utf-8", errors="replace").strip()
                if final:
                    self.root.after(0, lambda text=final: self.append_codex_output(f"\nFinal:\n{text}\n"))
            self.root.after(0, lambda: self.append_codex_output(f"\nCodex exited with code {code}.\n"))
        except Exception as exc:
            self.root.after(0, lambda err=exc: self.append_codex_output(self._error_message(err) + "\n"))

    def stop_codex(self):
        if self.codex_process and self.codex_process.poll() is None:
            self.codex_process.terminate()
            self.append_codex_output("Stopping Codex...\n")
        else:
            self.append_codex_output("Codex is not running.\n")

    def append_codex_output(self, text):
        self.codex_log.append(text)
        if hasattr(self, "codex_output"):
            self.codex_output.configure(state="normal")
            self.codex_output.insert("end", text)
            self.codex_output.configure(state="disabled")
            self.codex_output.see("end")

    def refresh_codex_output(self):
        if not hasattr(self, "codex_output"):
            return
        self.codex_output.configure(state="normal")
        self.codex_output.delete("1.0", "end")
        self.codex_output.insert("end", "".join(self.codex_log))
        self.codex_output.configure(state="disabled")
        self.codex_output.see("end")

    def _poll_ui_queue(self):
        try:
            while True:
                work = self._ui_queue.get_nowait()
                try:
                    work()
                except Exception:
                    pass
        except queue.Empty:
            pass
        self.root.after(30, self._poll_ui_queue)

    def _ui_async(self, fn, *args, **kwargs):
        def run():
            fn(*args, **kwargs)

        if threading.current_thread() is threading.main_thread():
            run()
        else:
            self._ui_queue.put(run)

    def _status_subtitle(self):
        chat_model = self.config.get("chat_model") or DEFAULT_CHAT_MODEL
        return f"{self.project_root}  |  Chat {chat_model}  |  Codex {self.config['codex_model']}"

    def get_openai_api_key(self):
        key = (self.config.get("openai_api_key") or "").strip()
        if not key:
            key = get_setting("OPENAI_API_KEY", "").strip()
        return key

    def chat_app_context(self):
        now = datetime.now()
        ctx = {
            "active_tab": self.active_tab,
            "project_root": str(self.project_root),
            "today": now.strftime("%Y-%m-%d"),
            "tomorrow": (now + timedelta(days=1)).strftime("%Y-%m-%d"),
            "weekday": now.strftime("%A"),
            "now": now.isoformat(timespec="minutes"),
        }
        projects = []
        for project_path in self.projects():
            try:
                meta = self.load_project_meta(project_path)
            except Exception:
                continue
            projects.append({
                "name": project_path.name,
                "title": self.project_display_title(project_path, meta),
                "due": meta.get("due", ""),
                "status": meta.get("status", "active"),
                "priority": meta.get("priority", "normal"),
                "pinned": self.is_dashboard_todo(meta),
            })
            if len(projects) >= 60:
                break
        ctx["projects"] = projects
        if self.page_view:
            ctx["page"] = self.page_view[0]
            if len(self.page_view) > 1:
                project_path = Path(self.page_view[1])
                try:
                    meta = self.load_project_meta(project_path)
                    ctx["active_project"] = {
                        "name": project_path.name,
                        "path": str(project_path),
                        "title": self.project_display_title(project_path, meta),
                        "due": meta.get("due", ""),
                        "status": meta.get("status", ""),
                        "priority": meta.get("priority", ""),
                        "pinned": self.is_dashboard_todo(meta),
                    }
                except Exception:
                    pass
        return ctx

    def build_chat_messages(self, prompt):
        messages = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}]
        ctx = self.chat_app_context()
        messages.append({"role": "system", "content": f"Current aiOS context:\n{json.dumps(ctx, indent=2)}"})
        for item in self.history[-12:-1]:
            if isinstance(item, (list, tuple)):
                role, text = item[0], item[1]
            else:
                role, text = item.get("role", ""), item.get("text", "")
            if str(role) == "User":
                messages.append({"role": "user", "content": str(text)})
            else:
                messages.append({"role": "assistant", "content": str(text)})
        messages.append({"role": "user", "content": prompt})
        return messages

    def execute_chat_tool(self, name, args):
        args = args or {}

        def active_project_path():
            project = str(args.get("project", "") or "").strip()
            if project:
                return self._resolve_project(project)
            if self.page_view and len(self.page_view) > 1:
                return Path(self.page_view[1])
            return None

        if name == "list_projects":
            rows = []
            for project_path in self.projects():
                meta = self.load_project_meta(project_path)
                rows.append(
                    {
                        "name": project_path.name,
                        "title": self.project_display_title(project_path, meta),
                        "due": meta.get("due", ""),
                        "pinned": self.is_dashboard_todo(meta),
                        "status": meta.get("status", "active"),
                    }
                )
            return {"projects": rows}

        if name == "open_project":
            path = self._resolve_project(args.get("name", ""))
            if not path:
                return {"ok": False, "error": f"Project not found: {args.get('name')}"}
            self._ui_async(self.open_project_detail, path)
            return {"ok": True, "opened": path.name}

        if name == "switch_tab":
            tab = str(args.get("tab", "Dashboard") or "Dashboard")
            if tab.casefold() in {"ai operator", "aioperator", "operator"}:
                tab = "AI Operator"
            self._ui_async(self.render_tab, tab)
            return {"ok": True, "tab": tab}

        if name == "set_project_summary":
            summary = str(args.get("summary", "") or "")
            path = active_project_path()
            if not path or not path.exists():
                return {"ok": False, "error": "No project found"}
            if summary and not summary.endswith("\n"):
                summary += "\n"
            self.save_project_summary(path, summary)
            self._ui_async(self._refresh_detail_if_needed)
            return {"ok": True, "project": path.name}

        if name == "set_project_due":
            due = args.get("due", "")
            if due is None:
                due = ""
            due = str(due).strip()
            path = active_project_path()
            if not path:
                return {"ok": False, "error": "No project found"}
            meta = self.load_project_meta(path)
            meta["due"] = due
            self.save_project_meta(path, meta)
            self._ui_async(self._refresh_after_project_meta_change)
            return {"ok": True, "project": path.name, "due": due}

        if name == "update_project":
            path = active_project_path()
            if not path:
                return {"ok": False, "error": "No project found"}
            meta = self.load_project_meta(path)
            for key in ("title", "status", "priority", "notes"):
                if key in args and args[key] is not None:
                    meta[key] = args[key]
            if "pinned" in args:
                meta["tracked_as_todo"] = bool(args["pinned"])
                if meta["tracked_as_todo"]:
                    meta["done"] = False
                    if meta.get("status") == "done":
                        meta["status"] = "active"
            self.save_project_meta(path, meta)
            self._ui_async(self._refresh_after_project_meta_change)
            return {
                "ok": True,
                "project": path.name,
                "title": meta.get("title"),
                "due": meta.get("due", ""),
                "status": meta.get("status"),
                "pinned": self.is_dashboard_todo(meta),
            }

        if name == "create_todo":
            title = str(args.get("title", "") or "").strip()
            if not title:
                return {"ok": False, "error": "title required"}
            due = str(args.get("due", "") or "").strip()
            priority = args.get("priority", "normal")
            path = self.create_todo_project(title, due, priority)
            self._ui_async(self.open_project_detail, path)
            self._ui_async(self._refresh_after_project_meta_change)
            return {"ok": True, "project": path.name, "path": str(path)}

        if name == "create_project":
            title = str(args.get("title", "") or "").strip()
            if not title:
                return {"ok": False, "error": "title required"}
            due = str(args.get("due", "") or "").strip()
            path = self.create_plain_project(title, due)
            self._ui_async(self.open_project_detail, path)
            self._ui_async(self._refresh_after_project_meta_change)
            return {"ok": True, "project": path.name, "path": str(path)}

        if name == "get_project_context":
            path = active_project_path()
            if not path or not path.exists():
                return {"ok": False, "error": "Project not found"}
            meta = self.load_project_meta(path)
            try:
                summary = self.load_project_summary(path)
            except Exception:
                summary = ""
            memory = self.read_project_memory(path)
            files = [
                {"path": rel, "size": size}
                for rel, size in self.list_project_files(path, limit=80)
            ]
            return {
                "ok": True,
                "project": path.name,
                "path": str(path),
                "title": meta.get("title") or path.name,
                "due": meta.get("due", ""),
                "status": meta.get("status", "active"),
                "priority": meta.get("priority", "normal"),
                "pinned": self.is_dashboard_todo(meta),
                "notes": meta.get("notes", ""),
                "summary": (summary or "")[:4000],
                "memory": (memory or "")[:2000],
                "files": files,
            }

        if name == "list_project_files":
            path = active_project_path()
            if not path or not path.exists():
                return {"ok": False, "error": "Project not found"}
            limit = int(args.get("limit") or 80)
            files = [
                {"path": rel, "size": size}
                for rel, size in self.list_project_files(path, limit=max(1, min(limit, 400)))
            ]
            return {"ok": True, "project": path.name, "files": files}

        if name == "read_project_file":
            path = active_project_path()
            if not path or not path.exists():
                return {"ok": False, "error": "Project not found"}
            rel = str(args.get("path", "") or "")
            try:
                target = safe_join_project(self.project_root, path, rel)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            if not target.exists() or not target.is_file():
                return {"ok": False, "error": "File does not exist."}
            if target.suffix.lower() not in SAFE_PROJECT_TEXT_EXTS:
                return {"ok": False, "error": f"Refusing to read {target.suffix or 'binary'} files."}
            try:
                text = target.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                return {"ok": False, "error": str(exc)}
            limit = 12000
            return {
                "ok": True,
                "project": path.name,
                "path": rel.replace("\\", "/"),
                "content": text[:limit],
                "truncated": len(text) > limit,
                "size": len(text),
            }

        if name == "write_project_file":
            path = active_project_path()
            if not path or not path.exists():
                return {"ok": False, "error": "Project not found"}
            rel = str(args.get("path", "") or "")
            content = args.get("content")
            if content is None:
                return {"ok": False, "error": "content required"}
            try:
                target = safe_join_project(self.project_root, path, rel)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            if target.suffix.lower() not in SAFE_PROJECT_TEXT_EXTS:
                return {"ok": False, "error": f"Refusing to write {target.suffix or 'binary'} files."}
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(str(content), encoding="utf-8")
            except OSError as exc:
                return {"ok": False, "error": str(exc)}
            self._ui_async(self._refresh_detail_if_needed)
            return {
                "ok": True,
                "project": path.name,
                "path": rel.replace("\\", "/"),
                "bytes": target.stat().st_size,
            }

        if name == "append_project_notes":
            path = active_project_path()
            if not path or not path.exists():
                return {"ok": False, "error": "Project not found"}
            text = str(args.get("text", "") or "").strip()
            if not text:
                return {"ok": False, "error": "text required"}
            meta = self.load_project_meta(path)
            existing = str(meta.get("notes", "") or "").rstrip()
            meta["notes"] = (existing + "\n" + text).strip() if existing else text
            self.save_project_meta(path, meta)
            self._ui_async(self._refresh_after_project_meta_change)
            return {"ok": True, "project": path.name, "notes": meta["notes"]}

        if name == "rename_project":
            project_arg = str(args.get("project", "") or "").strip()
            new_name = clean_project_name(str(args.get("new_name", "") or ""))
            if not new_name:
                return {"ok": False, "error": "new_name required"}
            path = self._resolve_project(project_arg) if project_arg else active_project_path()
            if not path or not path.exists():
                return {"ok": False, "error": "Project not found"}
            target = self.project_root / new_name
            if target.exists():
                return {"ok": False, "error": f"A project named '{new_name}' already exists."}
            try:
                path.rename(target)
            except OSError as exc:
                return {"ok": False, "error": str(exc)}
            meta = self.load_project_meta(target)
            if not meta.get("title") or meta.get("title") == path.name:
                meta["title"] = new_name
                self.save_project_meta(target, meta)
            if self.active_project and Path(self.active_project) == path:
                self.active_project = target
            if self.page_view and isinstance(self.page_view, tuple) and len(self.page_view) > 1 and Path(self.page_view[1]) == path:
                self.page_view = (self.page_view[0], target)
            self._ui_async(self._refresh_after_project_meta_change)
            return {"ok": True, "old_name": path.name, "new_name": target.name, "path": str(target)}

        if name == "search_projects":
            query = str(args.get("query", "") or "").strip().casefold()
            if not query:
                return {"ok": False, "error": "query required"}
            hits = []
            for project_path in self.projects():
                meta = self.load_project_meta(project_path)
                haystack = " ".join([
                    project_path.name,
                    str(meta.get("title", "")),
                    str(meta.get("notes", "")),
                    str(meta.get("status", "")),
                ]).casefold()
                summary_text = ""
                try:
                    summary_text = self.load_project_summary(project_path).casefold()
                except Exception:
                    pass
                if query in haystack or query in summary_text:
                    hits.append({
                        "name": project_path.name,
                        "title": self.project_display_title(project_path, meta),
                        "due": meta.get("due", ""),
                        "status": meta.get("status", "active"),
                    })
                if len(hits) >= 20:
                    break
            return {"ok": True, "query": args.get("query", ""), "matches": hits}

        if name == "open_project_folder":
            path = active_project_path()
            if not path or not path.exists():
                return {"ok": False, "error": "Project not found"}
            try:
                run_detached(["explorer.exe", str(path)], visible=True)
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "project": path.name, "path": str(path)}

        if name == "get_now":
            now = datetime.now()
            return {
                "now": now.isoformat(timespec="seconds"),
                "date": now.strftime("%Y-%m-%d"),
                "time": now.strftime("%H:%M"),
                "weekday": now.strftime("%A"),
                "tomorrow": (now + timedelta(days=1)).strftime("%Y-%m-%d"),
            }

        return {"ok": False, "error": f"Unknown tool: {name}"}

    def _refresh_detail_if_needed(self):
        if self.page_view:
            self._render_detail()

    def _refresh_after_project_meta_change(self):
        if self.page_view:
            self._render_detail()
        elif self.active_tab in ("Dashboard", "Projects"):
            self.render_tab(self.active_tab)

    def _openai_chat_reply(self, prompt, tools_used=None):
        api_key = self.get_openai_api_key()
        if not api_key:
            codex_ok, _label = codex_auth_info()
            if codex_ok and find_codex():
                return self._quick_codex_reply(prompt)
            return "Set your OpenAI API key in Settings, set OPENAI_API_KEY, or sign in with Codex."

        if tools_used is None:
            tools_used = []
        model = (self.config.get("chat_model") or DEFAULT_CHAT_MODEL).strip()
        messages = self.build_chat_messages(prompt)

        for _round in range(8):
            self._ui_async(self.set_thinking_status, "thinking")
            payload = openai_chat_payload(model, messages, AIOS_CHAT_TOOLS)
            data = openai_request(api_key, payload, timeout=90)
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            tool_calls = message.get("tool_calls") or []

            if tool_calls:
                messages.append(normalize_assistant_tool_message(message))
                for tool_call in tool_calls:
                    fn = tool_call.get("function") or {}
                    fn_name = fn.get("name", "")
                    tool_id = tool_call.get("id")
                    if not tool_id:
                        return "The model returned an invalid tool call. Try again."
                    try:
                        fn_args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        fn_args = {}
                    self._ui_async(self.set_thinking_status, f"using {fn_name}…")
                    result = self.execute_chat_tool(fn_name, fn_args)
                    tools_used.append({"name": fn_name, "args": fn_args, "ok": bool(result.get("ok", True)) if isinstance(result, dict) else True})
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_id,
                            "content": json.dumps(result),
                        }
                    )
                continue

            content = (message.get("content") or "").strip()
            if content:
                return content
            return "Done."

        return "Reached the tool call limit. Check aiOS for any changes and ask again if needed."

    def send(self):
        if self.busy:
            return "break"
        prompt = self.input.get("1.0", "end").strip()
        if not prompt:
            return "break"
        sent_at = time.perf_counter()
        self.input.delete("1.0", "end")
        self.append("You", prompt, "user")
        self.add_history("User", prompt)

        try:
            local_reply = self._handle_local_command(prompt)
        except Exception as exc:
            local_reply = self._error_message(exc)
        if local_reply:
            self.local_reply(local_reply, elapsed=time.perf_counter() - sent_at)
            return "break"

        if not self.get_openai_api_key():
            self.local_reply(
                "Add your OpenAI API key in Settings (same key the dictation agent uses).",
                elapsed=time.perf_counter() - sent_at,
            )
            return "break"

        self.busy = True
        self.chat_busy_since = time.perf_counter()
        self.chat_run_id += 1
        run_id = self.chat_run_id
        self.subtitle.configure(text="thinking")
        self.send_button.configure(state="disabled")
        self.show_thinking()
        # Hand off to the dictation voice agent so typed + spoken share one brain.
        threading.Thread(target=self._ask_voice_agent, args=(prompt, sent_at, run_id), daemon=True).start()
        return "break"

    def _ask_voice_agent(self, prompt, sent_at, run_id):
        """Send a typed turn to voice_dictation's VoiceAgent; reply arrives via voice_log."""
        self._ensure_voice_server()
        # Give a freshly spawned server a moment to bind.
        deadline = time.perf_counter() + 2.5
        delivered = False
        while time.perf_counter() < deadline:
            if self._send_voice_ask(prompt, echo_user=False):
                delivered = True
                break
            time.sleep(0.12)
        if delivered:
            # voice_log finishes the turn (hide thinking + append reply).
            return
        # Fallback: run the agent in-process if the voice server is unavailable.
        try:
            from voice_agent import VoiceAgent

            def on_event(kind, payload):
                if kind == "status":
                    status = payload if isinstance(payload, str) else "Thinking"
                    self._ui_async(self.set_thinking_status, status)
                elif kind == "tool_start":
                    detail = payload if isinstance(payload, dict) else {}
                    self._ui_async(
                        self.set_thinking_status,
                        detail.get("label") or detail.get("name") or "Running tool",
                    )
                elif kind == "tool_done":
                    detail = payload if isinstance(payload, dict) else {}
                    if detail:
                        self._ui_async(self._embed_tool_card, detail, before_thinking=True)
                    self._ui_async(self.set_thinking_status, "Thinking")
                elif kind == "reply_start":
                    self._ui_async(self._start_stream_reply)
                elif kind == "reply_delta":
                    self._ui_async(self._append_stream_reply, str(payload or ""))
                elif kind == "reply_done":
                    self._ui_async(self._set_stream_reply, str(payload or ""))

            if getattr(self, "_gui_voice_agent", None) is None:
                self._gui_voice_agent = VoiceAgent(on_event=on_event)
            else:
                self._gui_voice_agent.on_event = on_event
            result = self._gui_voice_agent.run(prompt)
            reply = (result.reply or result.error or "No reply.").strip()
            details = list(getattr(result, "tool_details", None) or [])
        except Exception as exc:
            reply = self._error_message(exc)
            details = []

        def finish():
            if run_id != self.chat_run_id:
                return
            had_live = bool(self._agent_turn_active)
            live_tools = self._live_tool_count
            self.hide_thinking()
            meta = self.format_elapsed(time.perf_counter() - sent_at)
            stream_frame = getattr(self, "_stream_reply_frame", None)
            try:
                stream_ok = stream_frame is not None and stream_frame.winfo_exists()
            except tk.TclError:
                stream_ok = False
            if stream_ok:
                if live_tools <= 0 and details:
                    for detail in details:
                        self._embed_tool_card(detail, before_thinking=False)
                self._finalize_stream_reply(reply, meta=meta)
            elif had_live:
                if live_tools <= 0 and details:
                    for detail in details:
                        self._embed_tool_card(detail, before_thinking=False)
                self._append_assistant_body(reply, meta=meta)
            else:
                self.append_assistant_message(reply, tools=details or None, meta=meta)
            self.add_history("Assistant", reply, tools=details or None)
            self.busy = False
            self.chat_busy_since = 0.0
            self._live_tool_count = 0
            if hasattr(self, "send_button"):
                try:
                    self.send_button.configure(state="normal")
                except tk.TclError:
                    pass
            try:
                self.subtitle.configure(text=self._status_subtitle())
            except tk.TclError:
                pass

        self._ui_async(finish)

    def _send_voice_ask(self, text, echo_user=True):
        payload = json.dumps({"cmd": "ask", "text": str(text or ""), "echo_user": bool(echo_user)})
        try:
            with socket.create_connection(("127.0.0.1", 48737), timeout=1.5) as client:
                client.sendall(payload.encode("utf-8"))
            return True
        except OSError:
            return False

    def _send_voice_reset_agent(self):
        for payload in (
            json.dumps({"cmd": "reset_agent"}),
            "reset_agent",
        ):
            try:
                with socket.create_connection(("127.0.0.1", 48737), timeout=0.4) as client:
                    client.sendall(payload.encode("utf-8"))
                return True
            except OSError:
                continue
        return False

    def should_use_quick_codex(self, prompt):
        lower = prompt.casefold().strip()
        if lower.startswith(("codex:", "ai:", "do:", "edit:", "fix:", "build:", "create:")):
            return True
        work_words = (
            "code",
            "codex",
            "file",
            "folder",
            "project",
            "app",
            "script",
            "implement",
            "fix",
            "debug",
            "create",
            "make",
            "build",
            "change",
            "update",
            "add",
            "remove",
            "delete",
            "rename",
            "move",
            "run",
            "test",
            "install",
            "open",
            "launch",
        )
        return any(word in lower for word in work_words)

    def _ask_ai(self, prompt, sent_at, run_id):
        reply = ""
        tools_used = []
        try:
            reply = self._openai_chat_reply(prompt, tools_used=tools_used)
        except Exception as exc:
            reply = self._error_message(exc)
        if tools_used:
            tool_lines = []
            for entry in tools_used:
                marker = "✓" if entry.get("ok", True) else "✗"
                tool_lines.append(f"  {marker} {entry['name']}{self._format_tool_args(entry.get('args') or {})}")
            footer = "Tools used:\n" + "\n".join(tool_lines)
            reply = (reply.rstrip() + "\n\n" + footer).strip() if reply else footer
        elapsed = time.perf_counter() - sent_at

        def finish():
            try:
                self._finish_reply(reply, elapsed, run_id)
            except Exception as exc:
                self._recover_stuck_chat(self._error_message(exc))

        self._ui_async(finish)

    def _format_tool_args(self, args):
        if not args:
            return ""
        bits = []
        for key, value in args.items():
            if value is None:
                continue
            text = str(value).replace("\n", " ").strip()
            if len(text) > 60:
                text = text[:57] + "…"
            bits.append(f"{key}={text}")
            if len(bits) >= 3:
                break
        return f" ({', '.join(bits)})" if bits else ""

    def _quick_codex_reply(self, prompt):
        codex = find_codex()
        if not codex:
            return "codex.exe was not found."

        prompt = re.sub(r"^(codex|ai|do|edit|fix|build|create)\s*:\s*", "", prompt, flags=re.IGNORECASE).strip()
        project_path = self.default_project_path()
        project_path.mkdir(parents=True, exist_ok=True)
        output_file = Path(tempfile.gettempdir()) / f"aios-quick-codex-{int(time.time())}.txt"
        model = (self.config.get("quick_codex_model") or "").strip()
        reasoning = self.config.get("quick_codex_reasoning", "none")
        context_lines = []
        for item in self.history[-5:-1]:
            if isinstance(item, (list, tuple)):
                role, text = item[0], item[1]
            else:
                role, text = item.get("role", ""), item.get("text", "")
            snippet = str(text).strip().replace("\n", " ")
            if len(snippet) > 240:
                snippet = snippet[:240] + "..."
            context_lines.append(f"{role}: {snippet}")
        context_block = "\n".join(context_lines)
        task = (
            "You are the aiOS side chat. Reply concisely in plain prose. "
            "Only touch files if the user explicitly asks. "
            f"Workspace: {project_path}\n"
        )
        if context_block:
            task += f"\nRecent conversation:\n{context_block}\n"
        task += f"\nUser: {prompt}\nAssistant:"
        args = [
            codex,
            "exec",
            "-C",
            str(project_path),
            "--skip-git-repo-check",
            "--sandbox",
            self.config.get("codex_sandbox", "workspace-write"),
        ]
        if model:
            args += ["-m", model]
        if reasoning and reasoning != "none":
            args += ["-c", f'model_reasoning_effort="{reasoning}"']
        args += ["--color", "never", "-o", str(output_file), task]

        process = subprocess.Popen(
            args,
            cwd=str(project_path),
            env=codex_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        self.quick_process = process
        try:
            stdout, _stderr = process.communicate(timeout=600)
            lines = [stdout] if stdout else []
            code = process.returncode
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
            code = -1
            lines = ["Codex request timed out after 10 minutes."]
        finally:
            if self.quick_process is process:
                self.quick_process = None
        final = ""
        if output_file.exists():
            final = output_file.read_text(encoding="utf-8", errors="replace").strip()
        output = self.sanitize_codex_output(final or "".join(lines))
        diff = self.git_diff_summary(project_path)
        if diff:
            output = (output + "\n\nChanged files\n" + diff).strip()
        friendly_markers = (
            "Codex hit",
            "Codex could not",
            "Codex says",
            "Codex rejected",
            "Your Codex session",
            "Codex error",
        )
        if output.startswith(friendly_markers):
            return output
        if code != 0:
            prefix = f"Codex exited with code {code}."
            return f"{prefix}\n{output}" if output else prefix
        return output or "Codex finished without a text response."

    def sanitize_codex_output(self, text):
        if not text:
            return ""
        raw = str(text).replace("\r\n", "\n").replace("\r", "\n")
        lower = raw.casefold()
        if "usage limit" in lower or "too many requests" in lower or "429" in lower:
            return "Codex hit its usage limit. Try again later or upgrade your plan."
        if "exceeded retry limit" in lower:
            return "Codex could not reach the model (retry limit exceeded). Check your network or try again."
        if "not logged in" in lower or "please login" in lower or "please log in" in lower or "unauthorized" in lower or "401" in lower or "no auth" in lower:
            return "Codex says you are not signed in. Click Login above (or run `codex login`)."
        if "model_not_found" in lower or "model not found" in lower or "unknown model" in lower or "invalid model" in lower:
            return "Codex rejected the configured model. Open Settings and set a Quick Model you have access to (or leave it blank)."
        if "session expired" in lower or "token expired" in lower or "refresh failed" in lower:
            return "Your Codex session expired. Click Login above to refresh it."
        message_match = re.search(r'"message"\s*:\s*"([^"]+)"', raw)
        if message_match:
            message = message_match.group(1).encode("utf-8", errors="ignore").decode("unicode_escape", errors="ignore")
            return f"Codex error: {message}"
        if "<html" in lower or "<svg" in lower or "cdn-cgi/challenge" in lower or "cloudflare" in lower:
            return "Codex hit a remote plugin/auth sync error. I hid the raw HTML."

        clean_lines = []
        skip_prefixes = (
            "Reading additional input from stdin",
            "OpenAI Codex",
            "--------",
            "workdir:",
            "model:",
            "provider:",
            "approval:",
            "sandbox:",
            "reasoning effort:",
            "reasoning summaries:",
            "session id:",
        )
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped:
                if clean_lines and clean_lines[-1]:
                    clean_lines.append("")
                continue
            if " WARN " in stripped or stripped.startswith("WARN "):
                continue
            if any(stripped.startswith(prefix) for prefix in skip_prefixes):
                continue
            if stripped in {"user", "assistant", "User:", "Assistant:"}:
                continue
            if "You are responding inside the aiOS Quick Codex side chat" in stripped:
                continue
            if "You are the aiOS side chat" in stripped:
                continue
            if stripped.startswith(
                (
                    "Current workspace:",
                    "Project memory:",
                    "Current user request:",
                    "Workspace:",
                    "Recent conversation:",
                    "User:",
                    "Assistant:",
                )
            ):
                continue
            clean_lines.append(line)
        output = "\n".join(clean_lines).strip()
        return output[:5000]

    def should_show_quick_line(self, line):
        text = line.strip()
        if not text:
            return False
        blocked = (
            "WARN ",
            "remote installed plugin",
            "<html>",
            "<svg",
            "<path",
            "Reading additional input from stdin",
        )
        if any(item in text for item in blocked):
            return False
        if len(text) > 220:
            return False
        interesting = (
            "exec",
            "apply_patch",
            "python ",
            "node ",
            "npm ",
            "pnpm ",
            "git ",
            "created",
            "updated",
            "modified",
            "error:",
        )
        return any(item in text.casefold() for item in interesting)

    def _finish_reply(self, reply, elapsed=None, run_id=None):
        stale = run_id is not None and run_id != self.chat_run_id
        if stale:
            return
        self.hide_thinking()
        if reply:
            self.local_reply(reply, elapsed=elapsed)
        self.busy = False
        self.chat_busy_since = 0.0
        self.set_thinking_status("thinking")
        try:
            self.subtitle.configure(text=self._status_subtitle())
        except tk.TclError:
            pass
        if hasattr(self, "send_button"):
            try:
                self.send_button.configure(state="normal")
            except tk.TclError:
                pass
        if hasattr(self, "input"):
            try:
                self.input.focus_set()
            except tk.TclError:
                pass

    def local_reply(self, text, elapsed=None):
        meta = self.format_elapsed(elapsed) if elapsed is not None else None
        self.append_assistant_message(text, meta=meta)
        self.add_history("Assistant", text)

    def format_elapsed(self, elapsed):
        if elapsed < 1:
            return f"{elapsed * 1000:.0f} ms"
        return f"{elapsed:.1f} s"

    def _chat_on_inner_configure(self, _event=None):
        try:
            self.chat_canvas.configure(scrollregion=self.chat_canvas.bbox("all"))
        except tk.TclError:
            pass

    def _chat_on_canvas_configure(self, event):
        try:
            self.chat_canvas.itemconfigure(self._chat_canvas_window, width=event.width)
        except tk.TclError:
            pass

    def _chat_on_mousewheel(self, event):
        try:
            self.chat_canvas.yview_scroll(int(-event.delta / 120), "units")
        except tk.TclError:
            pass

    def _chat_scroll_end(self):
        if not hasattr(self, "chat_canvas"):
            return

        def apply():
            self._chat_scroll_after = None
            if not hasattr(self, "chat_canvas"):
                return
            try:
                self.chat_canvas.update_idletasks()
                bounds = self.chat_canvas.bbox("all")
                if bounds:
                    self.chat_canvas.configure(scrollregion=bounds)
                self.chat_canvas.yview_moveto(1.0)
            except tk.TclError:
                pass

        # Do it now for normal appends, then once more after Tk has completed
        # the new bubble's geometry. Without the idle pass, long transcripts
        # could retain the old scrollregion and stay parked on the first turns.
        try:
            pending = getattr(self, "_chat_scroll_after", None)
            if pending is not None:
                self.root.after_cancel(pending)
            apply()
            self._chat_scroll_after = self.root.after_idle(apply)
        except tk.TclError:
            pass

    def _chat_clear_view(self):
        if not hasattr(self, "chat_inner"):
            return
        for child in list(self.chat_inner.winfo_children()):
            try:
                child.destroy()
            except tk.TclError:
                pass
        self._chat_embeds = []
        self._live_turn_col = None
        self._live_tools_box = None
        self.thinking_frame = None
        self.thinking_canvas = None
        self.thinking_label = None
        self._stream_reply_frame = None
        self._stream_reply_var = None
        self._stream_reply_text = ""
        self._stream_reply_meta = None

    def _chat_bubble_width(self):
        try:
            width = int(self.chat_canvas.winfo_width()) - 28
        except (tk.TclError, TypeError, ValueError):
            width = 0
        if width < 160:
            try:
                width = max(160, int(self.config.get("chat_width", 380)) - 56)
            except (TypeError, ValueError):
                width = 160
        # Tk raises "bad screen distance" if wraplength is not a positive int.
        return max(120, int(width * 0.82))

    def _make_bubble(self, parent, text, *, user=False, meta=None, textvariable=None):
        surface = self.c("surface")
        if user:
            bg = getattr(self, "_user_bubble_bg", self.blend_color(surface, self.c("accent"), 0.22))
            fg = self.c("text")
        else:
            bg = getattr(self, "_assistant_bg", self.blend_color(surface, self.c("text"), 0.08))
            fg = self.c("text")
        wrap = self._chat_bubble_width()
        bubble = tk.Frame(
            parent,
            bg=bg,
            highlightthickness=1,
            highlightbackground=self.blend_color(bg, self.c("text"), 0.08),
        )
        label_options = {
            "bg": bg,
            "fg": fg,
            "font": self.font(9),
            "justify": "left",
            "anchor": "w",
            "wraplength": max(120, int(wrap or 120)),
            "padx": 10,
            "pady": 8,
        }
        if textvariable is not None:
            label_options["textvariable"] = textvariable
        else:
            label_options["text"] = str(text or "").strip()
        tk.Label(bubble, **label_options).pack(anchor="w")
        if meta:
            meta_label = tk.Label(
                bubble,
                text=meta,
                bg=bg,
                fg=self.c("muted"),
                font=self.font(7, "italic"),
                anchor="e",
                padx=10,
                pady=0,
            )
            # A widget's ``pady`` is one Tk screen distance, while ``pack``
            # accepts a (top, bottom) pair. Passing the pair to Label raised
            # ``bad screen distance \"0 6\"`` after every timed agent reply.
            meta_label.pack(anchor="e", pady=(0, 6))
        return bubble

    def _add_user_bubble(self, text):
        if not hasattr(self, "chat_inner"):
            return
        row = tk.Frame(self.chat_inner, bg=self.c("surface"))
        row.pack(fill="x", padx=10, pady=(8, 2))
        bubble = self._make_bubble(row, text, user=True)
        bubble.pack(side="right", anchor="e")
        self._chat_embeds.append(row)
        self._chat_scroll_end()

    def _add_agent_column(self):
        row = tk.Frame(self.chat_inner, bg=self.c("surface"))
        row.pack(fill="x", padx=10, pady=(8, 2))
        col = tk.Frame(row, bg=self.c("surface"))
        col.pack(side="left", anchor="w", fill="x", expand=True)
        tools = tk.Frame(col, bg=self.c("surface"))
        tools.pack(fill="x", anchor="w")
        self._chat_embeds.append(row)
        return col, tools

    def show_thinking(self, status="Thinking"):
        if not hasattr(self, "chat_inner"):
            return
        self.hide_thinking()
        self._live_tool_count = 0
        self._live_tool_call_ids = set()
        self._agent_turn_active = True
        self._stream_reply_frame = None
        self._stream_reply_var = None
        self._stream_reply_text = ""
        self._stream_reply_meta = None
        status = self._pretty_agent_status(status)
        self.thinking_status_text = status
        col, tools = self._add_agent_column()
        self._live_turn_col = col
        self._live_tools_box = tools
        bg = getattr(self, "_assistant_bg", self.c("surface"))
        frame = tk.Frame(
            col,
            bg=bg,
            highlightthickness=1,
            highlightbackground=self.blend_color(bg, self.c("text"), 0.08),
        )
        frame.pack(anchor="w", pady=(0, 2))
        row = tk.Frame(frame, bg=bg)
        row.pack(fill="x", padx=10, pady=8)
        canvas = tk.Canvas(row, width=22, height=10, bg=bg, highlightthickness=0, bd=0)
        canvas.pack(side="left")
        label = tk.Label(
            row,
            text=status,
            bg=bg,
            fg=self.c("muted"),
            font=self.font(8, "italic"),
            anchor="w",
        )
        label.pack(side="left", padx=(8, 0))
        self.thinking_frame = frame
        self.thinking_canvas = canvas
        self.thinking_label = label
        self._chat_scroll_end()
        self.animate_thinking()

    def set_thinking_status(self, text):
        status = self._pretty_agent_status(text)
        self.thinking_status_text = status
        label = getattr(self, "thinking_label", None)
        if label is not None:
            try:
                if label.winfo_exists():
                    label.configure(text=status)
            except tk.TclError:
                pass

    def _pretty_agent_status(self, text):
        raw = str(text or "").strip() or "Thinking"
        lowered = raw.casefold()
        if lowered in {"thinking", "thinking...", "thinking…"}:
            return "Thinking…"
        if lowered.endswith("..."):
            raw = raw[:-3] + "…"
        elif not raw.endswith("…"):
            raw = raw[:1].upper() + raw[1:]
            if not raw.endswith((".", "!", "?", "…")):
                raw += "…"
        return raw

    def hide_thinking(self):
        if self.thinking_after:
            try:
                self.root.after_cancel(self.thinking_after)
            except tk.TclError:
                pass
        self.thinking_after = None
        frame = getattr(self, "thinking_frame", None)
        if frame is not None:
            try:
                frame.destroy()
            except tk.TclError:
                pass
        self.thinking_canvas = None
        self.thinking_frame = None
        self.thinking_label = None
        self._agent_turn_active = False

    def _start_stream_reply(self):
        """Replace the spinner with one assistant bubble that grows per delta."""
        if not hasattr(self, "chat_inner"):
            return
        frame = getattr(self, "_stream_reply_frame", None)
        try:
            if frame is not None and frame.winfo_exists():
                return
        except tk.TclError:
            pass
        self._ensure_agent_turn_ui("Writing")
        parent = self._live_turn_col
        if parent is None:
            parent, tools = self._add_agent_column()
            self._live_turn_col = parent
            self._live_tools_box = tools
        self.hide_thinking()
        self._agent_turn_active = True
        self._stream_reply_text = ""
        self._stream_reply_var = tk.StringVar(master=self.root, value="")
        self._stream_reply_frame = self._make_bubble(
            parent,
            "",
            user=False,
            textvariable=self._stream_reply_var,
        )
        self._stream_reply_frame.pack(anchor="w", pady=(0, 2))
        self._stream_reply_meta = None
        self._chat_scroll_end()

    def _append_stream_reply(self, delta):
        delta = str(delta or "")
        if not delta:
            return
        self._start_stream_reply()
        self._stream_reply_text += delta
        try:
            self._stream_reply_var.set(self._stream_reply_text)
        except (AttributeError, tk.TclError):
            return
        self._chat_scroll_end()

    def _set_stream_reply(self, text):
        self._start_stream_reply()
        self._stream_reply_text = str(text or "")
        try:
            self._stream_reply_var.set(self._stream_reply_text)
        except (AttributeError, tk.TclError):
            pass
        self._chat_scroll_end()

    def _finalize_stream_reply(self, text, meta=None):
        frame = getattr(self, "_stream_reply_frame", None)
        try:
            if frame is None or not frame.winfo_exists():
                return False
        except tk.TclError:
            return False
        self._set_stream_reply(str(text or "").strip())
        if meta and self._stream_reply_meta is None:
            bg = str(frame.cget("bg"))
            self._stream_reply_meta = tk.Label(
                frame,
                text=meta,
                bg=bg,
                fg=self.c("muted"),
                font=self.font(7, "italic"),
                anchor="e",
                padx=10,
                pady=0,
            )
            self._stream_reply_meta.pack(anchor="e", pady=(0, 6))
        self._agent_turn_active = False
        self._live_turn_col = None
        self._live_tools_box = None
        self._stream_reply_frame = None
        self._stream_reply_var = None
        self._stream_reply_text = ""
        self._stream_reply_meta = None
        self._chat_scroll_end()
        return True

    def animate_thinking(self):
        canvas = getattr(self, "thinking_canvas", None)
        if not self.busy or canvas is None:
            return
        try:
            if not canvas.winfo_exists():
                return
            canvas.delete("all")
        except tk.TclError:
            return
        canvas_bg = getattr(self, "_assistant_bg", self.c("surface"))
        base = self.blend_color(canvas_bg, self.c("thinking_base"), int(self.c("thinking_base_opacity")) / 100)
        pulse = self.blend_color(canvas_bg, self.c("thinking_pulse"), int(self.c("thinking_pulse_opacity")) / 100)
        step = self.thinking_step
        for col in range(3):
            phase = (step - col * 2) % 12
            strength = max(0, 1 - abs(phase - 3) / 3)
            radius = 1.2 + strength * 1.4
            x = 4 + col * 7
            y = 5
            color = self.blend_color(base, pulse, strength)
            canvas.create_oval(x - radius, y - radius, x + radius, y + radius, fill=color, outline="")
        self.thinking_step = (self.thinking_step + 1) % 12
        self.thinking_after = self.root.after(90, self.animate_thinking)

    def _chat_card_width(self):
        return max(140, self._chat_bubble_width())

    def _embed_tool_card(self, detail, *, before_thinking=False):
        """Insert an expandable tool-call card into the agent chat transcript."""
        if not hasattr(self, "chat_inner") or not isinstance(detail, dict):
            return
        call_id = str(detail.get("call_id") or "").strip()
        if before_thinking and call_id:
            seen = getattr(self, "_live_tool_call_ids", None)
            if not isinstance(seen, set):
                seen = set()
                self._live_tool_call_ids = seen
            if call_id in seen:
                return
            seen.add(call_id)
        name = str(detail.get("name") or "tool")
        label = str(detail.get("label") or name)
        summary = str(detail.get("summary") or label)
        ok = bool(detail.get("ok", True))
        arguments = detail.get("arguments") if isinstance(detail.get("arguments"), dict) else {}
        output = str(detail.get("output") or "")
        bg = self.blend_color(self.c("surface"), self.c("text"), 0.07)
        border = self.blend_color(bg, self.c("success") if ok else self.c("danger"), 0.45)
        accent = self.c("success") if ok else self.c("danger")
        width = self._chat_card_width()
        parent = None
        if before_thinking and getattr(self, "_live_tools_box", None) is not None:
            parent = self._live_tools_box
        elif getattr(self, "_live_turn_col", None) is not None:
            parent = self._live_turn_col
        else:
            col, tools = self._add_agent_column()
            parent = tools
            self._live_turn_col = col
            self._live_tools_box = tools

        card = tk.Frame(
            parent,
            bg=bg,
            highlightthickness=1,
            highlightbackground=border,
        )
        card.pack(fill="x", anchor="w", pady=(0, 4))
        header = tk.Frame(card, bg=bg, cursor="hand2")
        header.pack(fill="x", padx=8, pady=6)
        chevron = tk.Label(header, text="▸", bg=bg, fg=self.c("muted"), font=self.font(8, "bold"), cursor="hand2")
        chevron.pack(side="left")
        title = tk.Label(
            header,
            text=name.replace("_", " "),
            bg=bg,
            fg=self.c("text"),
            font=self.font(8, "bold"),
            anchor="w",
            cursor="hand2",
        )
        title.pack(side="left", padx=(6, 8))
        preview = label if label.casefold() != name.casefold() else summary
        preview = preview if len(preview) <= 42 else preview[:41] + "…"
        preview_label = tk.Label(
            header,
            text=preview,
            bg=bg,
            fg=self.c("muted"),
            font=self.font(8),
            anchor="w",
            cursor="hand2",
        )
        preview_label.pack(side="left", fill="x", expand=True)
        status = tk.Label(
            header,
            text="OK" if ok else "ERR",
            bg=self.blend_color(bg, accent, 0.18),
            fg=accent,
            font=self.font(7, "bold"),
            padx=6,
            pady=1,
            cursor="hand2",
        )
        status.pack(side="right")

        body = tk.Frame(card, bg=bg)
        body_inner = tk.Frame(body, bg=bg)
        body_inner.pack(fill="x", padx=10, pady=(0, 8))

        def detail_view(title_text, value, top_pad=0):
            tk.Label(
                body_inner,
                text=title_text,
                bg=bg,
                fg=self.c("muted"),
                font=self.font(7, "bold"),
                anchor="w",
            ).pack(fill="x", pady=(top_pad, 2))
            text = str(value or "")
            holder = tk.Frame(body_inner, bg=bg)
            holder.pack(fill="x")
            holder.grid_columnconfigure(0, weight=1)
            holder.grid_rowconfigure(0, weight=1)
            box = tk.Text(
                holder,
                height=min(14, max(3, text.count("\n") + 1)),
                bg=self.blend_color(bg, "#000000", 0.28),
                fg="#d6e2ff",
                relief="flat",
                bd=0,
                padx=8,
                pady=6,
                wrap="none",
                font=("Consolas", max(8, int(self.c("font_size")) - 1)),
            )
            y_scroll = tk.Scrollbar(holder, orient="vertical", command=box.yview, width=8)
            x_scroll = tk.Scrollbar(holder, orient="horizontal", command=box.xview, width=8)
            box.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
            box.grid(row=0, column=0, sticky="nsew")
            y_scroll.grid(row=0, column=1, sticky="ns")
            x_scroll.grid(row=1, column=0, sticky="ew")
            box.insert("1.0", text or "(none)")
            box.configure(state="disabled")

            def scroll_detail(event):
                try:
                    box.yview_scroll(int(-event.delta / 120), "units")
                except tk.TclError:
                    pass
                return "break"

            box.bind("<MouseWheel>", scroll_detail)
            return box

        metadata = {key: value for key, value in detail.items() if key not in {"arguments", "output"}}
        detail_view(
            "Call details",
            json.dumps(metadata, ensure_ascii=False, indent=2, default=str),
        )
        detail_view(
            "Input",
            json.dumps(arguments, ensure_ascii=False, indent=2, default=str),
            top_pad=8,
        )
        detail_view("Result", output or "(no output)", top_pad=8)

        state = {"open": False}

        def toggle(_event=None):
            state["open"] = not state["open"]
            if state["open"]:
                body.pack(fill="x")
                chevron.configure(text="▾")
                preview_label.configure(text="")
            else:
                body.pack_forget()
                chevron.configure(text="▸")
                preview_label.configure(text=preview)
            self._chat_scroll_end()

        for widget in (header, chevron, title, preview_label, status):
            widget.bind("<Button-1>", toggle)

        self._chat_embeds.append(card)
        self._live_tool_count += 1
        self._chat_scroll_end()

    def _finalize_agent_turn_ui(self):
        self.hide_thinking()
        self.busy = False
        self.chat_busy_since = 0.0
        self._agent_turn_active = False
        self._live_turn_col = None
        self._live_tools_box = None
        self.set_thinking_status("Thinking")
        if hasattr(self, "send_button"):
            try:
                self.send_button.configure(state="normal")
            except tk.TclError:
                pass
        try:
            self.subtitle.configure(text=self._status_subtitle())
        except tk.TclError:
            pass

    def blend_color(self, background, foreground, amount):
        amount = max(0.0, min(1.0, float(amount)))
        try:
            br, bg, bb = self.root.winfo_rgb(background)
            fr, fg, fb = self.root.winfo_rgb(foreground)
        except tk.TclError:
            return foreground
        values = []
        for base, top in ((br, fr), (bg, fg), (bb, fb)):
            values.append(int((base + (top - base) * amount) / 256))
        return f"#{values[0]:02x}{values[1]:02x}{values[2]:02x}"

    def codex_login(self):
        if launch_codex_login():
            self.local_reply(
                "Opened a terminal to run `codex login`. Finish the browser flow, then come back and try again. "
                "I'll refresh the account label automatically."
            )
            self.root.after(2000, self.refresh_chat_account)
            self.root.after(8000, self.refresh_chat_account)
            self.root.after(20000, self.refresh_chat_account)
        else:
            self.local_reply("I could not launch `codex login`. Open a terminal and run it manually.")

    def refresh_chat_account(self):
        if not hasattr(self, "chat_account_label"):
            return
        voice_cfg = self.config.get("voice_dictation") or {}
        model_label = voice_cfg.get("agent_model") or DEFAULT_VOICE_DICTATION.get("agent_model") or "gpt-5.6-luna"
        key_ok = bool(self.get_openai_api_key())
        auth_label = "API key ready" if key_ok else "No API key"
        try:
            self.chat_account_label.configure(
                text=f"{model_label} · {auth_label}",
                fg=self.c("success") if key_ok else self.c("danger"),
            )
        except tk.TclError:
            pass

    def reset_chat(self):
        if self.quick_process and self.quick_process.poll() is None:
            try:
                self.quick_process.terminate()
                self.quick_process.wait(timeout=2)
            except Exception:
                try:
                    self.quick_process.kill()
                except Exception:
                    pass
        self.quick_process = None
        self.busy = False
        self.chat_busy_since = 0.0
        self.chat_run_id += 1
        self.hide_thinking()
        self.history = []
        self.config["chat_history"] = []
        save_config(self.config)
        agent = getattr(self, "_gui_voice_agent", None)
        if agent is not None:
            try:
                agent.clear()
            except Exception:
                pass
        self._send_voice_reset_agent()
        if hasattr(self, "send_button"):
            self.send_button.configure(state="normal")
        self.render_chat_history()

    def append_system_message(self, text):
        self.append("Codex", text, "muted")

    def append_stream_line(self, text):
        if not hasattr(self, "chat_inner"):
            return
        stripped = text.rstrip("\n")
        if not stripped:
            return
        row = tk.Frame(self.chat_inner, bg=self.c("surface"))
        row.pack(fill="x", padx=10, pady=2)
        tk.Label(
            row,
            text=stripped,
            bg=self.c("surface"),
            fg=self.c("muted"),
            font=self.font(8),
            anchor="w",
            justify="left",
            wraplength=self._chat_bubble_width(),
        ).pack(anchor="w")
        self._chat_scroll_end()

    def git_diff_summary(self, project_path):
        git = shutil.which("git")
        if not git:
            return ""
        try:
            result = subprocess.run(
                [git, "diff", "--stat", "--", "."],
                cwd=str(project_path),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception:
            return ""
        return result.stdout.strip()

    def add_history(self, role, text, tools=None):
        entry = {"role": role, "text": text}
        if tools:
            entry["tools"] = tools
        self.history.append(entry)
        self.history = self.history[-40:]
        self.config["chat_history"] = list(self.history)
        save_config(self.config)

    def load_chat_history(self):
        history = []
        for item in self.config.get("chat_history", [])[-40:]:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                role, text = str(item[0]), str(item[1])
                tools = None
            elif isinstance(item, dict) and item.get("role") and item.get("text") is not None:
                role = str(item["role"])
                text = str(item["text"])
                tools = item.get("tools") if isinstance(item.get("tools"), list) else None
            else:
                continue
            if role.casefold() != "user":
                text = self.sanitize_codex_output(text)
            if text and len(text) < 8000:
                entry = {"role": role, "text": text}
                if tools:
                    entry["tools"] = tools
                history.append(entry)
        self.config["chat_history"] = list(history)
        save_config(self.config)
        return history

    def render_chat_history(self):
        if not hasattr(self, "chat_inner"):
            return
        self.hide_thinking()
        self._chat_clear_view()
        self._live_tool_count = 0
        self._agent_turn_active = False
        for item in self.history[-24:]:
            if isinstance(item, (list, tuple)):
                role, text, tools = item[0], item[1], None
            else:
                role = item.get("role", "")
                text = item.get("text", "")
                tools = item.get("tools") if isinstance(item.get("tools"), list) else None
            is_user = str(role).casefold() == "user"
            if is_user:
                self.append("You", text, "user", trim=False)
            else:
                self.append_assistant_message(text, tools=tools, trim=False)
        self._trim_chat_display()

    def append(self, label, text, tag, meta=None, trim=True):
        if not hasattr(self, "chat_inner"):
            return
        body = str(text or "").strip()
        if not body and tag != "muted":
            return
        if tag in {"user"}:
            self._add_user_bubble(body)
        elif tag in {"assistant"}:
            self.append_assistant_message(body, meta=meta, trim=False)
        else:
            row = tk.Frame(self.chat_inner, bg=self.c("surface"))
            row.pack(fill="x", padx=10, pady=2)
            tk.Label(
                row,
                text=body or str(label or ""),
                bg=self.c("surface"),
                fg=self.c("muted"),
                font=self.font(8),
                anchor="w",
                justify="left",
                wraplength=self._chat_bubble_width(),
            ).pack(anchor="w")
            self._chat_scroll_end()
        if trim:
            self._trim_chat_display()

    def append_assistant_message(self, text, tools=None, meta=None, trim=True):
        if not hasattr(self, "chat_inner"):
            return
        col, tools_box = self._add_agent_column()
        prev_col, prev_tools = self._live_turn_col, self._live_tools_box
        self._live_turn_col = col
        self._live_tools_box = tools_box
        for detail in tools or []:
            if isinstance(detail, dict):
                self._embed_tool_card(detail, before_thinking=False)
        body = str(text or "").strip()
        if body or meta:
            bubble = self._make_bubble(col, body or "", user=False, meta=meta)
            bubble.pack(anchor="w", pady=(0, 2))
        self._live_turn_col, self._live_tools_box = prev_col, prev_tools
        self._chat_scroll_end()
        if trim:
            self._trim_chat_display()

    def _append_assistant_body(self, text, meta=None, trim=True):
        """Continue the open aiOS block after live tools (no second label)."""
        if not hasattr(self, "chat_inner"):
            return
        body = str(text or "").strip()
        parent = self._live_turn_col
        if parent is None:
            self.append_assistant_message(body, meta=meta, trim=trim)
            return
        if body or meta:
            bubble = self._make_bubble(parent, body or "", user=False, meta=meta)
            bubble.pack(anchor="w", pady=(0, 2))
        self._live_turn_col = None
        self._live_tools_box = None
        self._chat_scroll_end()
        if trim:
            self._trim_chat_display()

    def insert_formatted_text(self, text, default_tag):
        # Bubble UI renders plain text; keep helper for any legacy callers.
        _ = default_tag
        if str(text or "").strip():
            self.append_assistant_message(text)

    def _error_message(self, exc):
        message = str(exc).strip() or exc.__class__.__name__
        return f"Action failed: {message}"

    def _voice_cfg(self):
        self.config["voice_dictation"] = merge_voice_dictation(self.config.get("voice_dictation"))
        return self.config["voice_dictation"]

    def _save_voice_cfg(self):
        self.config["voice_dictation"] = merge_voice_dictation(self.config.get("voice_dictation"))
        save_config(self.config)

    def set_voice_hold_ms(self, value):
        self._voice_cfg()["hold_ms"] = int(float(value))
        self._save_voice_cfg()

    def set_voice_separate_hotkeys(self, value):
        voice = self._voice_cfg()
        voice["separate_hotkeys"] = bool(value)
        if voice["separate_hotkeys"] and voice_hotkey_to_ahk(voice.get("aios_hotkey")) == voice_hotkey_to_ahk(
            voice.get("voice_hotkey")
        ):
            voice["aios_hotkey"] = "Insert" if voice_hotkey_to_ahk(voice.get("voice_hotkey")) != "Insert" else "Home"
        self._save_voice_cfg()
        if self.active_tab == "Settings":
            self.render_tab("Settings")

    def _apply_hotkey_setting(self, field, ahk_key, *, display_name=None):
        if field not in ("voice_hotkey", "aios_hotkey"):
            return
        ahk = voice_hotkey_to_ahk(ahk_key)
        label = display_name or voice_hotkey_label(ahk)
        voice = self._voice_cfg()
        voice[field] = ahk
        if voice.get("separate_hotkeys"):
            other = "aios_hotkey" if field == "voice_hotkey" else "voice_hotkey"
            if voice_hotkey_to_ahk(voice.get(other)) == ahk:
                voice[other] = "Insert" if ahk != "Insert" else "Home"
        self._save_voice_cfg()
        try:
            if field == "voice_hotkey":
                self.voice_hotkey_var.set(label)
            elif hasattr(self, "aios_hotkey_var"):
                self.aios_hotkey_var.set(label)
        except Exception:
            pass

    def _on_hotkey_choice(self, field, display_name):
        ahk = SAFE_HOTKEYS.get(display_name, voice_hotkey_to_ahk(display_name))
        self._apply_hotkey_setting(field, ahk, display_name=display_name)
        if self.active_tab == "Settings":
            self.render_tab("Settings")

    def _capture_hotkey(self, field="voice_hotkey"):
        """Modal that grabs the next key and saves it as voice or open-aiOS hotkey."""
        title = "Set dictation key" if field == "voice_hotkey" else "Set open aiOS key"
        top = tk.Toplevel(self.root)
        top.title(title)
        top.configure(bg=self.c("panel"))
        top.transient(self.root)
        top.grab_set()
        try:
            top.attributes("-topmost", True)
        except tk.TclError:
            pass
        try:
            w, h = 420, 180
            x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
            y = self.root.winfo_rooty() + (self.root.winfo_height() - h) // 2
            top.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
        except tk.TclError:
            pass

        tk.Label(top, text="Press the key you want to use",
                 bg=self.c("panel"), fg=self.c("text"),
                 font=self.font(11, "bold")).pack(pady=(22, 6))
        info = tk.StringVar(value="F13–F24 = Macro Deck · side mouse buttons · Insert · F1–F12")
        tk.Label(top, textvariable=info, bg=self.c("panel"),
                 fg=self.c("muted"), font=self.font(8),
                 wraplength=380, justify="center").pack(pady=(0, 14))
        result = tk.StringVar(value="(waiting…)")
        tk.Label(top, textvariable=result,
                 bg="#080d14", fg=self.c("text"),
                 font=self.font(10, "bold"),
                 padx=18, pady=6).pack(pady=(0, 6))

        actions = tk.Frame(top, bg=self.c("panel"))
        actions.pack(pady=(8, 0))

        keysym_to_ahk = {
            "Insert": "Insert", "Delete": "Delete", "Home": "Home", "End": "End",
            "Prior": "PgUp", "Next": "PgDn", "Pause": "Pause",
            "Scroll_Lock": "ScrollLock", "Menu": "AppsKey",
        }
        for i in range(1, 25):
            keysym_to_ahk[f"F{i}"] = f"F{i}"
        mouse_button_to_ahk = {8: "XButton1", 9: "XButton2"}

        chosen = {"key": None}
        bindings = []

        def resolve_key(event):
            ahk = keysym_to_ahk.get(event.keysym)
            if not ahk and event.keycode:
                if 112 <= event.keycode <= 135:
                    ahk = f"F{event.keycode - 111}"
            return voice_hotkey_to_ahk(ahk) if ahk else ""

        def on_key(event):
            ahk = resolve_key(event)
            unknown_fell_back = (
                ahk == "Insert"
                and event.keysym not in keysym_to_ahk
                and not (event.keycode and 112 <= event.keycode <= 135)
            )
            if not ahk or unknown_fell_back:
                info.set(f"'{event.keysym or event.keycode}' is not safe — "
                          "pick a function key, Insert, side mouse button, etc.")
                result.set("(waiting…)")
                return "break"
            label = voice_hotkey_label(ahk)
            chosen["key"] = ahk
            result.set(label)
            info.set("Press OK to save, or pick another key.")
            return "break"

        def on_button(event):
            ahk = mouse_button_to_ahk.get(event.num)
            if not ahk:
                info.set("Use Mouse 4 / Mouse 5 (side buttons) or a keyboard key.")
                return "break"
            label = voice_hotkey_label(ahk)
            chosen["key"] = ahk
            result.set(label)
            info.set("Press OK to save, or pick another key.")
            return "break"

        for sequence, handler in (("<KeyPress>", on_key), ("<ButtonPress>", on_button)):
            top.bind_all(sequence, handler, add="+")
            bindings.append(sequence)

        def cleanup_bindings():
            for sequence in bindings:
                try:
                    top.unbind_all(sequence)
                except tk.TclError:
                    pass

        top.focus_set()

        def commit():
            if not chosen["key"]:
                return
            self._apply_hotkey_setting(field, chosen["key"])
            cleanup_bindings()
            top.destroy()
            if self.active_tab == "Settings":
                self.render_tab("Settings")

        def cancel():
            cleanup_bindings()
            top.destroy()

        top.protocol("WM_DELETE_WINDOW", cancel)
        self.button(actions, "OK", commit, compact=True).pack(side="left", padx=(0, 8))
        self.button(actions, "Cancel", cancel, compact=True).pack(side="left")

    def set_voice_mic_sensitivity(self, value):
        self._voice_cfg()["silence_rms"] = round(max(1, min(50, int(float(value)))) / 10000.0, 4)
        self._save_voice_cfg()

    def set_voice_overlay_opacity(self, value):
        self._voice_cfg()["overlay_opacity"] = max(20, min(100, int(float(value))))
        self._save_voice_cfg()
        # Live-apply on the running dictation HUD (no full restart needed).
        try:
            with socket.create_connection(("127.0.0.1", 48737), timeout=0.3) as client:
                client.sendall(b"reload")
        except OSError:
            pass

    def _reload_voice_dictation(self):
        """Nudge the running dictation process to re-read helper_config.json."""
        try:
            with socket.create_connection(("127.0.0.1", 48737), timeout=0.3) as client:
                client.sendall(b"reload")
        except OSError:
            pass

    def set_voice_agent_tts(self, value):
        self._voice_cfg()["agent_tts_enabled"] = bool(value)
        self._save_voice_cfg()
        self._reload_voice_dictation()

    def set_voice_barge_in(self, value):
        self._voice_cfg()["barge_in"] = bool(value)
        self._save_voice_cfg()
        self._reload_voice_dictation()

    def set_voice_hallucination_filter(self, value):
        self._voice_cfg()["hallucination_filter"] = bool(value)
        self._save_voice_cfg()
        self._reload_voice_dictation()

    def set_voice_transcript_history(self, value):
        self._voice_cfg()["transcript_history"] = bool(value)
        self._save_voice_cfg()
        self._reload_voice_dictation()

    def set_voice_agent_tool(self, name, value):
        """Toggle one agent capability. Read fresh on the agent's next turn."""
        self._voice_cfg()[name] = bool(value)
        self._save_voice_cfg()
        self._reload_voice_dictation()

    def set_voice_typing_delay_ms(self, value):
        self._voice_cfg()["typing_delay_ms"] = int(float(value))
        self._save_voice_cfg()

    def set_voice_discord_mute_enabled(self, value):
        self._voice_cfg()["discord_mute_enabled"] = bool(value)
        self._save_voice_cfg()

    def _render_update_card(self, parent):
        try:
            import aios_updater
        except Exception as exc:
            err = self.card(parent)
            err.pack(fill="x", pady=(0, 12))
            self.section(err, "Update aiOS")
            tk.Label(err, text=f"updater unavailable: {exc}",
                     bg=self.c("surface"), fg=self.c("danger"),
                     font=self.font(9)).pack(anchor="w", padx=12, pady=(0, 12))
            return
        card = self.card(parent)
        card.pack(fill="x", pady=(0, 12))
        self.section(card, "Update aiOS")

        src = aios_updater.load_source()
        current = aios_updater.get_current_sha() or "(unknown)"
        info_var = tk.StringVar(value=f"current {current}  ·  press Check to compare with GitHub")
        status_var = tk.StringVar(value="")
        log_var = tk.StringVar(value="")
        owner_var = tk.StringVar(value=src["owner"])
        repo_var = tk.StringVar(value=src["repo"])
        branch_var = tk.StringVar(value=src["branch"])
        self._update_state = {
            "info_var": info_var, "status_var": status_var, "log_var": log_var,
            "owner_var": owner_var, "repo_var": repo_var,
            "branch_var": branch_var,
            "busy": False, "last_check": None,
        }

        tk.Label(card, textvariable=info_var, bg=self.c("surface"),
                 fg=self.c("text"), font=self.font(9), anchor="w",
                 justify="left", wraplength=620).pack(fill="x", padx=12, pady=(0, 4))
        tk.Label(card, textvariable=status_var, bg=self.c("surface"),
                 fg=self.c("muted"), font=self.font(8), anchor="w",
                 justify="left", wraplength=620).pack(fill="x", padx=12, pady=(0, 4))

        # Source config grid: owner / repo / branch / token.
        grid = tk.Frame(card, bg=self.c("surface"))
        grid.pack(fill="x", padx=12, pady=(6, 6))

        def add_row(row_idx, label, var, *, masked=False, hint=""):
            tk.Label(grid, text=label, bg=self.c("surface"), fg=self.c("muted"),
                     font=self.font(8, "bold"), width=10, anchor="w").grid(
                row=row_idx, column=0, sticky="w", pady=2)
            entry = tk.Entry(
                grid, textvariable=var,
                bg="#080d14", fg=self.c("text"),
                insertbackground=self.c("text"),
                relief="flat", bd=0, font=self.font(9),
                show="*" if masked else "",
            )
            entry.grid(row=row_idx, column=1, sticky="ew", padx=(8, 0), pady=2, ipady=3, ipadx=6)
            if hint:
                tk.Label(grid, text=hint, bg=self.c("surface"),
                         fg=self.c("muted"), font=self.font(7)).grid(
                    row=row_idx, column=2, sticky="w", padx=(8, 0))
            return entry

        grid.columnconfigure(1, weight=1)
        add_row(0, "Owner", owner_var, hint="GitHub user/org")
        add_row(1, "Repo", repo_var)
        add_row(2, "Branch", branch_var, hint="usually main")

        log_frame = tk.Frame(card, bg="#080d14")
        log_frame.pack(fill="x", padx=12, pady=(4, 8))
        log_label = tk.Label(log_frame, textvariable=log_var, bg="#080d14",
                              fg=self.c("muted"), font=self.font(8),
                              anchor="w", justify="left", wraplength=620,
                              padx=8, pady=6)
        log_label.pack(fill="x")

        actions = tk.Frame(card, bg=self.c("surface"))
        actions.pack(fill="x", padx=12, pady=(0, 12))
        save_btn = self.button(actions, "Save source",
                                lambda: self._updater_save_source(), compact=True)
        save_btn.pack(side="left")
        check_btn = self.button(actions, "Check for updates",
                                 lambda: self._updater_check(), compact=True)
        check_btn.pack(side="left", padx=(8, 0))
        update_btn = self.button(actions, "Update & restart",
                                  lambda: self._updater_update_and_restart(), compact=True)
        update_btn.pack(side="left", padx=(8, 0))
        update_btn.configure(state="disabled")
        self._update_state["check_btn"] = check_btn
        self._update_state["update_btn"] = update_btn
        self._update_state["save_btn"] = save_btn

        # Kick off an initial check in the background so the user sees status
        # without having to click.
        self._updater_check(silent=True)

    def _updater_save_source(self):
        import aios_updater
        state = self._update_state
        owner = state["owner_var"].get().strip()
        repo = state["repo_var"].get().strip()
        branch = state["branch_var"].get().strip() or "main"
        if not owner or not repo:
            state["status_var"].set("Owner and Repo are required")
            return
        ok = aios_updater.save_source(owner, repo, branch)
        if ok:
            state["status_var"].set("source saved · re-checking…")
            self._updater_check(silent=False)
        else:
            state["status_var"].set("could not save (check helper_config.json)")

    def _updater_set_buttons(self, busy: bool, can_update: bool):
        state = self._update_state
        try:
            state["check_btn"].configure(
                state="disabled" if busy else "normal")
        except Exception:
            pass
        try:
            state["update_btn"].configure(
                state="normal" if (not busy and can_update) else "disabled")
        except Exception:
            pass

    def _updater_log(self, text):
        state = self._update_state
        prev = state["log_var"].get()
        line = text.strip()
        if not line:
            return
        lines = (prev + "\n" + line).splitlines() if prev else [line]
        state["log_var"].set("\n".join(lines[-6:]))

    def _updater_check(self, silent: bool = False):
        import aios_updater
        state = self._update_state
        if state["busy"]:
            return
        state["busy"] = True
        self._updater_set_buttons(busy=True, can_update=False)
        if not silent:
            state["status_var"].set("checking GitHub…")

        def worker():
            result = aios_updater.check_for_update()
            self.root.after(0, lambda: self._updater_check_done(result))

        threading.Thread(target=worker, daemon=True).start()

    def _updater_check_done(self, result):
        state = self._update_state
        state["busy"] = False
        state["last_check"] = result
        if not result.get("ok"):
            state["status_var"].set(result.get("error") or "could not check for updates")
            self._updater_set_buttons(busy=False, can_update=False)
            return
        current = result.get("current") or "(unknown)"
        latest = result.get("latest") or "?"
        behind = bool(result.get("behind"))
        msg = (result.get("message") or "").strip()
        author = (result.get("author") or "").strip()
        if behind:
            state["info_var"].set(
                f"update available · current {current} → latest {latest}"
                + (f"\n“{msg}” — {author}" if msg else "")
            )
            state["status_var"].set("press Update & restart to install")
        else:
            state["info_var"].set(f"up to date · {current}")
            state["status_var"].set("")
        self._updater_set_buttons(busy=False, can_update=behind)

    def _updater_update_and_restart(self):
        import aios_updater
        state = self._update_state
        if state["busy"]:
            return
        if not (state.get("last_check") and state["last_check"].get("behind")):
            return
        state["busy"] = True
        self._updater_set_buttons(busy=True, can_update=False)
        state["status_var"].set("updating…")
        state["log_var"].set("")

        def emit(msg):
            self.root.after(0, lambda m=msg: self._updater_log(m))

        def worker():
            result = aios_updater.perform_update(progress=emit)
            self.root.after(0, lambda: self._updater_update_done(result))

        threading.Thread(target=worker, daemon=True).start()

    def _updater_update_done(self, result):
        import aios_updater
        state = self._update_state
        state["busy"] = False
        if not result.get("ok"):
            state["status_var"].set("update failed: " + (result.get("message") or "?"))
            self._updater_set_buttons(busy=False, can_update=True)
            return
        staged = bool(result.get("staged"))
        if staged:
            state["status_var"].set(
                "files staged · aiOS will close and reopen with the new version")
            self._updater_log("closing aiOS to apply staged files…")
        else:
            state["status_var"].set("updated · restarting in 1s…")
            self._updater_log("restarting aiOS…")
        self._updater_set_buttons(busy=False, can_update=False)
        # Stop any operator run first so we don't leave child threads.
        try:
            if self.agent_operator_loop and self.agent_operator_loop.is_running():
                self.agent_operator_loop.stop()
        except Exception:
            pass
        self.root.after(900, lambda: aios_updater.restart_aios())

    def pick_color(self, key):
        color = colorchooser.askcolor(color=self.c(key))[1]
        if color:
            self.theme[key] = color
            save_config(self.config)
            self.rebuild_shell()

    def apply_color(self, key):
        entry = self.settings_color_rows.get(key)
        if not entry:
            return
        color = entry.get("1.0", "end").strip()
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            self.local_reply("Use colors like #61dafb.")
            return
        self.theme[key] = color
        save_config(self.config)
        self.rebuild_shell()

    def set_opacity(self, value):
        opacity = max(0.75, min(1.0, int(float(value)) / 100))
        self.theme["opacity"] = opacity
        self.root.attributes("-alpha", opacity)
        save_config(self.config)

    def set_font_size(self, value):
        size = max(8, min(15, int(float(value))))
        if int(self.theme.get("font_size", 10)) == size:
            return
        self.theme["font_size"] = size
        save_config(self.config)
        self.rebuild_shell()

    def set_radius(self, value):
        radius = max(12, min(40, int(float(value))))
        self.theme["radius"] = radius
        save_config(self.config)
        self._redraw_shell()

    def set_theme_int(self, key, value):
        self.theme[key] = max(0, min(100, int(float(value))))
        save_config(self.config)

    def set_always_on_top(self, value):
        self.theme["always_on_top"] = bool(value)
        self.root.attributes("-topmost", bool(value))
        save_config(self.config)

    def refresh_all(self):
        self.shortcuts = start_menu_shortcuts()
        self.apps = self._discover_apps()
        self.project_root.mkdir(parents=True, exist_ok=True)
        if self.page_view:
            self._render_detail()
        else:
            self.render_tab(self.active_tab)
        self.update_usage_badges()

    def _stop_background_work(self):
        self._agent_operator_context_flush()
        if self.screen_record_process and self.screen_record_process.poll() is None:
            self.stop_screen_recording()
        if self.quick_process and self.quick_process.poll() is None:
            try:
                self.quick_process.kill()
            except Exception:
                pass
        self.quick_process = None
        if self.codex_process and self.codex_process.poll() is None:
            try:
                self.codex_process.terminate()
            except Exception:
                pass
        self.codex_process = None
        if self.agent_operator_loop and self.agent_operator_loop.is_running():
            try:
                self.agent_operator_loop.stop()
            except Exception:
                pass
        self._agent_operator_control_stop()
        if self.agent_operator_tts:
            try:
                self.agent_operator_tts.shutdown()
            except Exception:
                pass
        self.busy = False
        self.chat_busy_since = 0.0
        self.hide_thinking()

    def restart_application(self):
        self._dash_flush_notes()
        self._stop_background_work()
        self._stop_voice_server()
        self.config["window"] = self.root.geometry()
        save_config(self.config)
        script = Path(__file__).resolve()
        try:
            subprocess.Popen(
                [sys.executable, str(script)],
                cwd=str(script.parent),
                close_fds=True,
            )
        except OSError as exc:
            self.local_reply(f"Could not restart aiOS: {exc}")
            return
        self._delete_tray_icon()
        self.root.quit()
        os._exit(0)

    def _stop_voice_server(self):
        script = Path(__file__).resolve().parent / "voice_dictation.py"
        python = Path(sys.executable)
        try:
            subprocess.run(
                [str(python), str(script), "--quit"],
                cwd=str(script.parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                timeout=2,
                creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        deadline = time.perf_counter() + 1.5
        while time.perf_counter() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", 48737), timeout=0.15):
                    time.sleep(0.1)
                    continue
            except OSError:
                return
        self._force_stop_voice_processes()

    def _force_stop_voice_processes(self):
        if not sys.platform.startswith("win"):
            return
        command = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.CommandLine -match 'voice_dictation.py' -and $_.Name -match '^pythonw?\\.exe$' } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
        )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                cwd=str(Path(__file__).resolve().parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                timeout=4,
                creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    def rebuild_shell(self):
        self._dash_flush_notes()
        if self.quick_process and self.quick_process.poll() is None:
            try:
                self.quick_process.terminate()
            except Exception:
                pass
        self.quick_process = None
        self.busy = False
        self.chat_busy_since = 0.0
        self.config["window"] = self.root.geometry()
        save_config(self.config)
        for child in self.root.winfo_children():
            child.destroy()
        self.root.attributes("-alpha", float(self.c("opacity")))
        self.root.attributes("-topmost", bool(self.c("always_on_top")))
        self._build_ui()
        self._bind_keys()
        self.root.after(20, self._redraw_shell)
        self.root.after(50, self.update_usage_badges)

    def _schedule_usage_refresh(self):
        self.update_usage_badges()
        self.root.after(10000, self._schedule_usage_refresh)

    def _schedule_chat_watchdog(self):
        self._chat_watchdog()
        self.root.after(5000, self._schedule_chat_watchdog)

    def _schedule_self_health_heartbeat(self):
        try:
            HELPER_HEARTBEAT_PATH.touch()
        except OSError:
            pass
        self.root.after(5000, self._schedule_self_health_heartbeat)

    def _chat_watchdog(self):
        if self.busy and self.chat_busy_since:
            if time.perf_counter() - self.chat_busy_since > 1900:
                self._recover_stuck_chat("The agent took too long and was reset. Try again or click Reset.")
                return
        if hasattr(self, "send_button"):
            try:
                if not self.busy and str(self.send_button.cget("state")) == "disabled":
                    self.send_button.configure(state="normal")
                    self.subtitle.configure(text=self._status_subtitle())
            except tk.TclError:
                pass

    def _recover_stuck_chat(self, message):
        if self.quick_process and self.quick_process.poll() is None:
            try:
                self.quick_process.kill()
            except Exception:
                pass
        self.quick_process = None
        self.busy = False
        self.chat_busy_since = 0.0
        self.chat_run_id += 1
        self.hide_thinking()
        try:
            self.send_button.configure(state="normal")
        except tk.TclError:
            pass
        try:
            self.subtitle.configure(text=self._status_subtitle())
        except tk.TclError:
            pass
        if message:
            self.local_reply(message)

    def _trim_chat_display(self, max_rows=40):
        if not hasattr(self, "chat_inner"):
            return
        children = list(self.chat_inner.winfo_children())
        overflow = len(children) - max_rows
        if overflow <= 0:
            return
        for child in children[:overflow]:
            try:
                child.destroy()
            except tk.TclError:
                pass

    def update_usage_badges(self):
        if not hasattr(self, "usage_account_labels"):
            return
        usage = codex_usage.codex_usage_payload(CODEX_HOME)
        accounts = usage.get("accounts") or []
        for index, label in enumerate(self.usage_account_labels):
            account = accounts[index] if index < len(accounts) else {}
            label.configure(text=codex_usage.desktop_account_text(account))
        active = next((account for account in accounts if account.get("active")), {})
        primary = active.get("primary") or {}
        secondary = active.get("secondary") or {}
        primary_reset = primary.get("reset")
        secondary_reset = secondary.get("reset")
        tooltip = []
        if primary_reset:
            tooltip.append("5H reset " + datetime.fromtimestamp(float(primary_reset)).strftime("%H:%M"))
        if secondary_reset:
            tooltip.append("Weekly reset " + datetime.fromtimestamp(float(secondary_reset)).strftime("%a %H:%M"))
        if tooltip:
            self.subtitle.configure(text=self._status_subtitle() + "  |  " + " · ".join(tooltip))

    def show(self):
        self.root.overrideredirect(True)
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        if hasattr(self, "input"):
            self.input.focus_set()

    def _apply_window_icon(self):
        if not APP_ICON_PATH.exists():
            return
        try:
            self.root.iconbitmap(default=str(APP_ICON_PATH))
        except tk.TclError:
            pass

    def _init_tray_icon(self):
        if not sys.platform.startswith("win"):
            return
        try:
            user32 = ctypes.windll.user32
            shell32 = ctypes.windll.shell32
            user32.LoadImageW.argtypes = [
                wintypes.HINSTANCE,
                wintypes.LPCWSTR,
                wintypes.UINT,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.UINT,
            ]
            user32.LoadImageW.restype = wintypes.HANDLE
            shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATA)]
            shell32.Shell_NotifyIconW.restype = wintypes.BOOL
            self.root.update_idletasks()
            hwnd = self.root.winfo_id()
            icon = None
            if TRAY_ICON_PATH.exists():
                icon = user32.LoadImageW(
                    None, str(TRAY_ICON_PATH), IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE
                )
            if not icon:
                icon = user32.LoadIconW(None, IDI_APPLICATION)
            self._tray_icon = icon
            nid = NOTIFYICONDATA()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
            nid.hWnd = hwnd
            nid.uID = 1
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_TRAYICON
            nid.hIcon = icon
            nid.szTip = "aiOS"
            self._subclass_tray_window(hwnd)
            if shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
                self._tray_added = True
                self._tray_nid = nid
        except (AttributeError, OSError, tk.TclError):
            pass

    def _tray_notify(self, title, message, level="info"):
        if not self._tray_added or self._tray_nid is None or not sys.platform.startswith("win"):
            return
        try:
            nid = self._tray_nid
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_INFO
            nid.szInfoTitle = str(title or "aiOS CODE")[:63]
            nid.szInfo = re.sub(r"\s+", " ", str(message or ""))[:255]
            nid.dwInfoFlags = {
                "error": NIIF_ERROR,
                "warning": NIIF_WARNING,
            }.get(level, NIIF_INFO)
            ctypes.windll.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        except (AttributeError, OSError, ValueError):
            pass

    def _subclass_tray_window(self, hwnd):
        user32 = ctypes.windll.user32
        lresult = ctypes.c_ssize_t
        wndproc_type = ctypes.WINFUNCTYPE(lresult, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        user32.SetWindowLongPtrW.restype = ctypes.c_void_p
        user32.CallWindowProcW.argtypes = [
            ctypes.c_void_p,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.CallWindowProcW.restype = lresult

        def wndproc(window, message, wparam, lparam):
            if message == WM_TRAYICON and wparam == 1:
                if lparam == WM_RBUTTONUP:
                    self._show_tray_menu()
                    return 0
                if lparam == WM_LBUTTONDBLCLK:
                    self.root.after(0, self.show)
                    return 0
            return user32.CallWindowProcW(self._tray_old_wndproc, window, message, wparam, lparam)

        self._tray_wndproc = wndproc_type(wndproc)
        self._tray_old_wndproc = user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, self._tray_wndproc)

    def _show_tray_menu(self):
        try:
            user32 = ctypes.windll.user32
            user32.CreatePopupMenu.restype = wintypes.HMENU
            user32.TrackPopupMenu.restype = wintypes.UINT
            user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
            menu = user32.CreatePopupMenu()
            user32.AppendMenuW(menu, MF_STRING, TRAY_SHOW, "Show aiOS")
            user32.AppendMenuW(menu, MF_STRING, TRAY_HIDE, "Hide aiOS")
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, TRAY_START_VOICE, "Start Voice")
            user32.AppendMenuW(menu, MF_STRING, TRAY_RESTART_VOICE, "Restart Voice")
            user32.AppendMenuW(menu, MF_STRING, TRAY_RESTART_MACROS, "Restart Macros")
            user32.AppendMenuW(menu, MF_STRING, TRAY_RESTART_APP, "Restart aiOS")
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, TRAY_QUIT, "Quit")
            point = POINT()
            user32.GetCursorPos(ctypes.byref(point))
            user32.SetForegroundWindow(self.root.winfo_id())
            command = user32.TrackPopupMenu(
                menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, point.x, point.y, 0, self.root.winfo_id(), None
            )
            user32.DestroyMenu(menu)
            if command:
                self.root.after(0, lambda cmd=command: self._handle_tray_command(cmd))
        except (AttributeError, OSError, tk.TclError):
            pass

    def _handle_tray_command(self, command):
        if command == TRAY_SHOW:
            self.show()
        elif command == TRAY_HIDE:
            self.hide()
        elif command == TRAY_START_VOICE:
            self._ensure_voice_server()
        elif command == TRAY_RESTART_VOICE:
            self._stop_voice_server()
            self.root.after(150, self._ensure_voice_server)
        elif command == TRAY_RESTART_MACROS:
            self._restart_hotkeys()
        elif command == TRAY_RESTART_APP:
            self.restart_application()
        elif command == TRAY_QUIT:
            self._delete_tray_icon()
            self.root.destroy()

    def _delete_tray_icon(self):
        if not self._tray_added or self._tray_nid is None or not sys.platform.startswith("win"):
            return
        try:
            ctypes.windll.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._tray_nid))
        except (AttributeError, OSError):
            pass
        self._tray_added = False

    def _on_root_destroy(self, event):
        if event.widget is self.root:
            self._agent_operator_context_flush()
            if self.agent_operator_loop and self.agent_operator_loop.is_running():
                try:
                    self.agent_operator_loop.stop()
                except Exception:
                    pass
            self._agent_operator_control_stop()
            if self.agent_operator_tts:
                try:
                    self.agent_operator_tts.shutdown()
                except Exception:
                    pass
            self._delete_tray_icon()

    def show_startup_screen(self):
        frame_paths = sorted(STARTUP_FRAME_DIR.glob("frame_*.png"))
        if not frame_paths:
            self.show()
            return
        self._launch_startup_splash()
        self.root.after(2600, self.show)

    def _launch_startup_splash(self):
        script = BASE_DIR / "startup_splash.py"
        pythonw = self._find_pythonw() or sys.executable
        try:
            subprocess.Popen(
                [pythonw, str(script)],
                cwd=str(BASE_DIR),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError:
            pass

    def _show_layered_startup_screen(self, frame_paths):
        try:
            from PIL import Image
        except ImportError:
            return False
        try:
            frames = [Image.open(path).convert("RGBA") for path in frame_paths]
        except OSError:
            return False
        if not frames:
            return False
        width, height = frames[0].size
        splash = None
        try:
            splash = tk.Toplevel(self.root)
            splash.withdraw()
            splash.overrideredirect(True)
            splash.attributes("-topmost", True)
            x = (splash.winfo_screenwidth() - width) // 2
            y = (splash.winfo_screenheight() - height) // 2
            splash.geometry(f"{width}x{height}+{x}+{y}")
            splash.update_idletasks()
            hwnd = splash.winfo_id()
            ctypes.windll.user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
            ctypes.windll.user32.GetWindowLongW.restype = wintypes.LONG
            ctypes.windll.user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
            ctypes.windll.user32.SetWindowLongW.restype = wintypes.LONG
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)
            self._startup_frames = frames
            self._startup_splash = splash
            self._update_layered_window(hwnd, frames[0], x, y)
            splash.deiconify()
            splash.lift()

            def finish():
                try:
                    splash.destroy()
                except tk.TclError:
                    pass
                self.show()

            def step(index=1):
                if index >= len(frames):
                    self.root.after(180, finish)
                    return
                try:
                    self._update_layered_window(hwnd, frames[index], x, y)
                except (OSError, tk.TclError):
                    finish()
                    return
                self.root.after(30, lambda: step(index + 1))

            step()
            return True
        except (AttributeError, OSError, tk.TclError):
            if splash is not None:
                try:
                    splash.destroy()
                except tk.TclError:
                    pass
            return False

    def _update_layered_window(self, hwnd, image, x, y):
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongW.restype = wintypes.LONG
        user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
        user32.SetWindowLongW.restype = wintypes.LONG
        user32.GetDC.argtypes = [wintypes.HWND]
        user32.GetDC.restype = wintypes.HDC
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        user32.ReleaseDC.restype = ctypes.c_int
        user32.UpdateLayeredWindow.argtypes = [
            wintypes.HWND,
            wintypes.HDC,
            ctypes.POINTER(POINT),
            ctypes.POINTER(SIZE),
            wintypes.HDC,
            ctypes.POINTER(POINT),
            wintypes.COLORREF,
            ctypes.POINTER(BLENDFUNCTION),
            wintypes.DWORD,
        ]
        user32.UpdateLayeredWindow.restype = wintypes.BOOL
        gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
        gdi32.DeleteDC.argtypes = [wintypes.HDC]
        gdi32.DeleteDC.restype = wintypes.BOOL
        gdi32.CreateDIBSection.argtypes = [
            wintypes.HDC,
            ctypes.POINTER(BITMAPINFO),
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p),
            wintypes.HANDLE,
            wintypes.DWORD,
        ]
        gdi32.CreateDIBSection.restype = ctypes.c_void_p
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        gdi32.SelectObject.restype = ctypes.c_void_p
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.DeleteObject.restype = wintypes.BOOL
        width, height = image.size
        rgba = image.tobytes("raw", "RGBA")
        bgra = bytearray(len(rgba))
        for index in range(0, len(rgba), 4):
            r, g, b, a = rgba[index:index + 4]
            bgra[index] = (b * a) // 255
            bgra[index + 1] = (g * a) // 255
            bgra[index + 2] = (r * a) // 255
            bgra[index + 3] = a

        screen_dc = user32.GetDC(0)
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        bits = ctypes.c_void_p()
        bitmap_info = BITMAPINFO()
        bitmap_info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bitmap_info.bmiHeader.biWidth = width
        bitmap_info.bmiHeader.biHeight = -height
        bitmap_info.bmiHeader.biPlanes = 1
        bitmap_info.bmiHeader.biBitCount = 32
        bitmap_info.bmiHeader.biCompression = BI_RGB
        bitmap = gdi32.CreateDIBSection(
            screen_dc, ctypes.byref(bitmap_info), 0, ctypes.byref(bits), None, 0
        )
        if not bitmap or not bits:
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(0, screen_dc)
            raise OSError("Could not create splash bitmap")
        old_bitmap = gdi32.SelectObject(mem_dc, bitmap)
        try:
            ctypes.memmove(bits, bytes(bgra), len(bgra))
            dst = POINT(x, y)
            size = SIZE(width, height)
            src = POINT(0, 0)
            blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
            updated = user32.UpdateLayeredWindow(
                hwnd,
                screen_dc,
                ctypes.byref(dst),
                ctypes.byref(size),
                mem_dc,
                ctypes.byref(src),
                0,
                ctypes.byref(blend),
                ULW_ALPHA,
            )
            if not updated:
                raise OSError("UpdateLayeredWindow failed")
        finally:
            gdi32.SelectObject(mem_dc, old_bitmap)
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(0, screen_dc)

    def hide(self):
        self._dash_flush_notes()
        self.config["window"] = self.root.geometry()
        save_config(self.config)
        self.root.withdraw()

    def toggle(self):
        if self.root.state() == "withdrawn":
            self.show()
        else:
            self.hide()

    def _bind_keys(self):
        self.root.bind("<Escape>", lambda _event: self.hide())
        self.root.bind("<Control-v>", self._dashboard_clipboard_paste)
        self.root.bind("<Control-V>", self._dashboard_clipboard_paste)
        if hasattr(self, "input"):
            self.input.bind("<Return>", self._return_key)
            self.input.bind("<Control-Return>", lambda _event: self.send())
            self.input.bind("<Shift-Return>", lambda _event: None)

    def _dashboard_clipboard_paste(self, event):
        if self.active_tab != "Dashboard":
            return None
        focused = event.widget
        if isinstance(focused, (tk.Text, tk.Entry)):
            return None
        self.save_clipboard_image()
        return "break"

    def _return_key(self, event):
        if event.state & 0x0001:
            return None
        self.send()
        return "break"

    def _start_command_server(self):
        threading.Thread(target=self._command_server, daemon=True).start()

    def _find_autohotkey(self):
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        candidates = [
            Path(r"C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe"),
            local / "Programs" / "AutoHotkey" / "v2" / "AutoHotkey64.exe",
            local / "Programs" / "AutoHotkey" / "v2" / "AutoHotkey32.exe",
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        return shutil.which("AutoHotkey64.exe") or shutil.which("AutoHotkey.exe") or ""

    def _hotkeys_script(self):
        return Path(__file__).resolve().parent / "autocorrect.ahk"

    def _hotkeys_heartbeat_path(self):
        return Path(__file__).resolve().parent / ".aios-ahk-heartbeat"

    def _hotkeys_running(self):
        if not sys.platform.startswith("win"):
            return False
        script = str(self._hotkeys_script())
        needle = script.replace("'", "''")
        try:
            completed = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    (
                        f"$needle = '{needle}'; "
                        "(Get-CimInstance Win32_Process | Where-Object { "
                        "$_.Name -like 'AutoHotkey*.exe' -and $_.CommandLine -like ('*' + $needle + '*') "
                        "} | Measure-Object).Count"
                    ),
                ],
                capture_output=True,
                text=True,
                timeout=3,
                creationflags=CREATE_NO_WINDOW,
            )
            return int((completed.stdout or "0").strip() or "0") > 0
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return False

    def _hotkeys_healthy(self, max_age_sec=20):
        """True when the AHK process is up and still writing its heartbeat."""
        if not self._hotkeys_running():
            return False
        path = self._hotkeys_heartbeat_path()
        try:
            age = time.time() - path.stat().st_mtime
            return age <= float(max_age_sec)
        except OSError:
            return False

    def _stop_hotkeys(self):
        if not sys.platform.startswith("win"):
            return
        script = str(self._hotkeys_script()).replace("'", "''")
        try:
            subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    (
                        f"$needle = '{script}'; "
                        "Get-CimInstance Win32_Process | Where-Object { "
                        "$_.Name -like 'AutoHotkey*.exe' -and $_.CommandLine -like ('*' + $needle + '*') "
                        "} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
                    ),
                ],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _ensure_hotkeys(self):
        """Spawn autocorrect.ahk (macro / dictate hotkeys) if it is not healthy."""
        if not self._hotkeys_script().exists():
            return False
        if self._hotkeys_healthy():
            return True
        if self._hotkeys_running():
            # Process exists but heartbeat is stale — hard restart.
            self._stop_hotkeys()
            time.sleep(0.25)
        return self._start_hotkeys(wait_healthy=True)

    def _start_hotkeys(self, wait_healthy=False):
        script = self._hotkeys_script()
        ahk = self._find_autohotkey()
        if not ahk or not script.exists():
            return False
        try:
            # Never CREATE_NO_WINDOW for AutoHotkey — that breaks its message loop /
            # tray / hotkeys. Detach only so it outlives the launcher.
            creationflags = 0x00000008 if sys.platform.startswith("win") else 0  # DETACHED_PROCESS
            subprocess.Popen(
                [ahk, str(script)],
                cwd=str(script.parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
            )
        except OSError:
            return False
        if not wait_healthy:
            return True
        deadline = time.perf_counter() + 4.0
        while time.perf_counter() < deadline:
            if self._hotkeys_healthy(max_age_sec=30):
                return True
            time.sleep(0.2)
        return self._hotkeys_running()

    def _restart_hotkeys(self):
        """Reload AutoHotkey macros (tray / dashboard Pad)."""
        script = self._hotkeys_script()
        ahk = self._find_autohotkey()
        if not ahk or not script.exists():
            try:
                self.local_reply("AutoHotkey v2 / autocorrect.ahk not found.")
            except Exception:
                pass
            return False
        self._stop_hotkeys()
        time.sleep(0.2)
        ok = self._start_hotkeys(wait_healthy=True)
        try:
            self.local_reply("Macropad running." if ok else "Macropad failed to start.")
        except Exception:
            pass
        return ok

    def _ensure_voice_server(self):
        """Spawn voice_dictation.py if it's not already listening on port 48737."""
        voice_port = 48737
        try:
            with socket.create_connection(("127.0.0.1", voice_port), timeout=0.15):
                return  # already running
        except OSError:
            pass
        script = Path(__file__).resolve().parent / "voice_dictation.py"
        if not script.exists():
            return
        pythonw = self._find_pythonw()
        if not pythonw:
            return
        env = os.environ.copy()
        env.setdefault("VOICE_PRELOAD", "1")
        try:
            stdout_log = open(script.parent / "voice-out.log", "a", encoding="utf-8")
            stderr_log = open(script.parent / "voice-err.log", "a", encoding="utf-8")
            creationflags = CREATE_NO_WINDOW
            if sys.platform.startswith("win"):
                # DETACHED_PROCESS so the voice server survives without a console
                creationflags |= 0x00000008
            subprocess.Popen(
                [pythonw, str(script)],
                cwd=str(script.parent),
                env=env,
                stdout=stdout_log,
                stderr=stderr_log,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
            )
        except OSError:
            pass
        finally:
            for handle in (locals().get("stdout_log"), locals().get("stderr_log")):
                try:
                    handle.close()
                except Exception:
                    pass

    def _find_pythonw(self):
        candidates = [
            r"C:\Python313\pythonw.exe",
            r"C:\Python312\pythonw.exe",
            r"C:\Python311\pythonw.exe",
            shutil.which("pythonw.exe"),
            shutil.which("pythonw"),
        ]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return candidate
        # Fall back to whichever python the helper is running with
        py = Path(sys.executable)
        if py.name.lower() == "python.exe":
            alt = py.with_name("pythonw.exe")
            if alt.exists():
                return str(alt)
        return str(py)

    def _command_server(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((HOST, PORT))
            server.listen()
            while True:
                conn, _addr = server.accept()
                with conn:
                    chunks = []
                    while True:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        chunks.append(chunk)
                        if sum(len(part) for part in chunks) > MAX_COMMAND_BYTES:
                            break
                    command = b"".join(chunks).decode("utf-8", "ignore").strip()
                self._handle_remote_command(command)

    def _handle_remote_command(self, command):
        if not command:
            return
        if command == "toggle":
            self.root.after(0, self.toggle)
            return
        if command == "show":
            self.root.after(0, self.show)
            return
        if command == "hide":
            self.root.after(0, self.hide)
            return
        if command == "quit":
            self.root.after(0, self.root.destroy)
            return
        try:
            payload = json.loads(command)
        except json.JSONDecodeError:
            return
        action = str(payload.get("action") or "").strip().lower()
        text = str(payload.get("text") or "").strip()
        options = payload.get("options") if isinstance(payload.get("options"), dict) else None
        if not text and action not in {
            "phone_start", "phone_stop", "reload_operator_settings",
            "operator_stop", "operator_clear", "operator_clear_attachments",
            "operator_followup", "voice_event", "voice_log",
            "agent_capture_begin", "agent_capture_end",
        }:
            return
        if action == "chat":
            self.root.after(0, lambda value=text: self._remote_submit_chat(value))
        elif action == "operator":
            self.root.after(0, lambda value=text, opts=options: self._remote_submit_operator(value, opts))
        elif action == "phone_start":
            self.root.after(0, lambda value=text: self._phone_control_show(value))
        elif action == "phone_stop":
            self.root.after(0, self._phone_control_hide)
        elif action == "reload_operator_settings":
            self.root.after(0, lambda opts=options: self._remote_apply_operator_options(opts or {}))
        elif action == "operator_stop":
            self.root.after(0, self._remote_operator_stop)
        elif action == "operator_followup":
            self.root.after(0, lambda value=text, opts=options: self._remote_operator_followup(value, opts))
        elif action == "operator_clear":
            self.root.after(0, self._remote_operator_clear)
        elif action == "operator_attach":
            self.root.after(0, lambda value=text: self._remote_operator_attach(value))
        elif action == "operator_clear_attachments":
            self.root.after(0, self._remote_operator_clear_attachments)
        elif action == "voice_event":
            self.root.after(0, lambda value=text, opts=options: self._remote_voice_event(value, opts or {}))
        elif action == "voice_log":
            self.root.after(0, lambda value=text, opts=options: self._remote_voice_log(value, opts or {}))
        elif action == "agent_capture_begin":
            token = text or str((options or {}).get("token") or "voice-agent")
            self.root.after(0, lambda value=token: self._remote_agent_capture_begin(value))
        elif action == "agent_capture_end":
            token = text or str((options or {}).get("token") or "voice-agent")
            self.root.after(0, lambda value=token: self._agent_capture_affinity_end(value))

    def _ensure_agent_turn_ui(self, status="Thinking"):
        if self._agent_turn_active:
            self.set_thinking_status(status)
            return
        self.busy = True
        if not self.chat_busy_since:
            self.chat_busy_since = time.perf_counter()
        if hasattr(self, "send_button"):
            try:
                self.send_button.configure(state="disabled")
            except tk.TclError:
                pass
        try:
            self.subtitle.configure(text="thinking")
        except tk.TclError:
            pass
        self.show_thinking(status)

    def _remote_voice_event(self, text, options):
        """Live agent progress: thinking status + expandable tool cards."""
        kind = str(options.get("kind") or "").strip().lower()
        echo_user = bool(options.get("echo_user", True))
        if kind == "turn_start":
            if echo_user and text:
                # Avoid duplicating a user bubble if the event is replayed.
                last = self.history[-1] if self.history else None
                last_text = ""
                if isinstance(last, dict):
                    last_text = str(last.get("text") or "")
                elif isinstance(last, (list, tuple)) and len(last) >= 2:
                    last_text = str(last[1])
                if last_text.strip() != str(text).strip():
                    self.append("You", text, "user")
                    self.add_history("User", text)
            self._ensure_agent_turn_ui("Thinking")
            return
        if kind == "status":
            self._ensure_agent_turn_ui(options.get("status") or text or "Thinking")
            return
        if kind == "tool_start":
            tool = options.get("tool") if isinstance(options.get("tool"), dict) else {}
            label = tool.get("label") or tool.get("name") or text or "Running tool"
            self._ensure_agent_turn_ui(label)
            return
        if kind == "tool_done":
            tool = options.get("tool") if isinstance(options.get("tool"), dict) else {}
            if not tool and text:
                tool = {"name": text, "label": text, "summary": text, "output": "", "ok": True}
            label = tool.get("label") or tool.get("name") or "Tool"
            self._ensure_agent_turn_ui(label)
            if tool:
                self._embed_tool_card(tool, before_thinking=True)
            self.set_thinking_status("Thinking")
            return
        if kind == "reply_start":
            self._start_stream_reply()
            return
        if kind == "reply_delta":
            self._append_stream_reply(text)
            return
        if kind == "reply_done":
            self._set_stream_reply(text)
            return

    def _remote_voice_log(self, text, options):
        """Mirror a voice-agent turn into the chat panel. The answer already
        exists — this only records it, it must not re-run the model."""
        try:
            options = options or {}
            reply = str(options.get("reply") or "").strip()
            error = str(options.get("error") or "").strip()
            tools = [str(name) for name in (options.get("tools") or [])]
            details = [item for item in (options.get("tool_details") or []) if isinstance(item, dict)]
            if not details and tools:
                details = [
                    {
                        "name": name,
                        "label": name,
                        "summary": name,
                        "arguments": {},
                        "output": "",
                        "ok": True,
                    }
                    for name in dict.fromkeys(tools)
                ]
            echo_user = bool(options.get("echo_user", True))
            elapsed = options.get("elapsed")
            meta = None
            try:
                if elapsed is not None:
                    meta = self.format_elapsed(float(elapsed))
            except (TypeError, ValueError):
                meta = None
            body = (error or reply).strip() or "(no reply)"

            # Spoken turns: add the user bubble if turn_start didn't already.
            append_user = False
            if echo_user and text:
                last = self.history[-1] if self.history else None
                last_text = ""
                last_role = ""
                if isinstance(last, dict):
                    last_text = str(last.get("text") or "")
                    last_role = str(last.get("role") or "")
                elif isinstance(last, (list, tuple)) and len(last) >= 2:
                    last_role, last_text = str(last[0]), str(last[1])
                if last_role.casefold() != "user" or last_text.strip() != str(text).strip():
                    self.add_history("User", text)
                    append_user = True

            # Save the completed answer before touching presentation widgets.
            # A Tk rendering failure must never discard a reply the agent has
            # already produced.
            self.add_history("Assistant", body, tools=details or None)
            if append_user:
                self.append("You", text, "user")

            # Close the spinner. If live tool cards were already embedded during
            # tool_done events, keep that column and only append the reply —
            # re-embedding tool_details here was duplicating operator cards.
            live_col = getattr(self, "_live_turn_col", None)
            live_tools = getattr(self, "_live_tools_box", None)
            live_count = int(getattr(self, "_live_tool_count", 0) or 0)
            stream_frame = getattr(self, "_stream_reply_frame", None)
            try:
                live_ok = live_col is not None and live_col.winfo_exists()
            except tk.TclError:
                live_ok = False
            try:
                stream_ok = stream_frame is not None and stream_frame.winfo_exists()
            except tk.TclError:
                stream_ok = False
            self.hide_thinking()
            self._agent_turn_active = False

            if not hasattr(self, "chat_inner"):
                self._live_turn_col = None
                self._live_tools_box = None
                self._live_tool_count = 0
            elif stream_ok:
                self._live_turn_col = live_col
                self._live_tools_box = live_tools
                if details and live_count <= 0:
                    for detail in details:
                        if isinstance(detail, dict):
                            self._embed_tool_card(detail, before_thinking=False)
                self._finalize_stream_reply(body, meta=meta)
            elif live_ok:
                self._live_turn_col = live_col
                self._live_tools_box = live_tools
                if details and live_count <= 0:
                    for detail in details:
                        if isinstance(detail, dict):
                            self._embed_tool_card(detail, before_thinking=False)
                self._append_assistant_body(body, meta=meta, trim=False)
            else:
                self._live_turn_col = None
                self._live_tools_box = None
                self._live_tool_count = 0
                self._stream_reply_frame = None
                self._stream_reply_var = None
                self._stream_reply_text = ""
                self._stream_reply_meta = None
                self.append_assistant_message(body, tools=details or None, meta=meta, trim=False)

            self.busy = False
            self.chat_busy_since = 0.0
            if hasattr(self, "send_button"):
                try:
                    self.send_button.configure(state="normal")
                except tk.TclError:
                    pass
            try:
                self.subtitle.configure(text=self._status_subtitle())
            except tk.TclError:
                pass
            try:
                self._chat_scroll_end()
            except tk.TclError:
                pass
        except Exception:
            try:
                self.hide_thinking()
                self.busy = False
                self._agent_turn_active = False
                self._live_turn_col = None
                self._live_tools_box = None
                self._live_tool_count = 0
                # Rebuild from durable history without the ephemeral timing
                # footer. This keeps the real response visible even if another
                # presentation-only regression slips through.
                self.render_chat_history()
            except Exception:
                pass

    def _remote_submit_chat(self, text):
        self._phone_control_show("AIOS")
        self.show()
        if hasattr(self, "input"):
            self.input.delete("1.0", "end")
            self.input.insert("1.0", text)
            self.send()

    def _remote_submit_operator(self, text, options=None):
        # Build the hidden Operator widgets needed by the runner, but do not
        # restore or focus the full desktop GUI for a phone-launched task.
        # The compact always-on-top HUD is shown when the run actually starts.
        self._phone_control_hide()
        self.render_tab("AI Operator")
        self._remote_submit_operator_attempt(text, options, attempts_left=80)

    def _show_operator_workspace(self):
        self.show()
        try:
            self.root.state("normal")
            self.root.update_idletasks()
            self.root.lift()
            self.root.focus_force()
            if sys.platform.startswith("win"):
                user32 = ctypes.windll.user32
                hwnd = user32.GetAncestor(self.root.winfo_id(), GA_ROOT) or self.root.winfo_id()
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
        except (AttributeError, OSError, tk.TclError):
            pass

    def _remote_submit_operator_attempt(self, text, options, attempts_left):
        ready = False
        try:
            imported = bool(self._ensure_agent_operator())
            if imported and not getattr(self, "agent_operator_task", None):
                # OPERATOR finished loading while another tab was on screen, so
                # its widgets — the ones the runner needs — were never built.
                # Without this the phone's task waited for a page that would
                # never render and was dropped without a word.
                self.agent_operator_booting = False
                self.agent_operator_booted = True
                self.render_tab("AI Operator")
            ready = imported and bool(getattr(self, "agent_operator_task", None))
        except Exception as exc:
            ready = False
            self.agent_operator_error = self.agent_operator_error or str(exc)
        if not ready and attempts_left > 0:
            self.root.after(150, lambda: self._remote_submit_operator_attempt(text, options, attempts_left - 1))
            return
        if not ready:
            # Never fail quietly: the phone has no other way of learning that
            # the task it sent was dropped on this PC.
            self._phone_mirror_fail(
                self.agent_operator_error or "OPERATOR did not finish loading on this PC.",
                "operator_unavailable")
            return
        # Phone asked for a NEW task. If a previous run is still winding down
        # (or stuck "running"), stop it and retry — do not reject with
        # "Already running" and leave the phone on a forever Thinking row.
        loop = getattr(self, "agent_operator_loop", None)
        if loop is not None and loop.is_running():
            try:
                if not getattr(self, "agent_operator_stop_requested", False):
                    self.agent_operator_stop()
                else:
                    loop.stop()
            except Exception:
                try:
                    loop.stop()
                except Exception:
                    pass
            if attempts_left > 0:
                self.root.after(
                    150,
                    lambda: self._remote_submit_operator_attempt(text, options, attempts_left - 1),
                )
                return
            self._phone_mirror_fail(
                "Could not stop the previous run to start your new task. Tap Stop, then try again.",
                "still_running",
            )
            return
        try:
            if options:
                self._remote_apply_operator_options(options, run=False)
            self.agent_operator_task.delete("1.0", "end")
            self.agent_operator_task.insert("1.0", text)
            self.agent_operator_run()
        except Exception as exc:
            self._phone_mirror_fail(f"Could not hand the task to OPERATOR: {exc}", "handoff_failed")

    def _remote_operator_stop(self):
        try:
            if self.agent_operator_loop and self.agent_operator_loop.is_running():
                self.agent_operator_stop()
            else:
                self._phone_mirror_set_idle("remote_stop")
        except Exception:
            pass

    def _remote_operator_followup(self, text, options=None):
        text = (text or "").strip() or "Continue"
        extra_steps = None
        if isinstance(options, dict) and options.get("steps") not in (None, ""):
            try:
                extra_steps = max(1, min(200, int(float(options.get("steps")))))
            except (TypeError, ValueError):
                extra_steps = None
        try:
            if not self._ensure_agent_operator():
                self._phone_mirror_fail(
                    "OPERATOR is not available on this PC.", "operator_unavailable")
                return
        except Exception as exc:
            self._phone_mirror_fail(
                f"OPERATOR is not available: {exc}", "operator_unavailable")
            return
        loop = self.agent_operator_loop
        if not loop or not loop.is_running():
            # Nothing running → treat as a fresh task (keep options).
            self._remote_submit_operator(text, options)
            return
        try:
            loop.add_follow_up(text, extra_steps=extra_steps)
            self._agent_operator_log_line("ts", f"\n[{self._ts()}] ")
            self._agent_operator_log_line("status", (
                f"FOLLOW-UP: {text}"
                + (f" (+{extra_steps} steps)\n" if extra_steps else "\n")
            ))
            self._phone_mirror_write({"type": "follow_up", "ts": time.time(),
                                       "text": text,
                                       "answering_ask": bool(loop.is_awaiting_answer())})
            if self.agent_operator_status_var:
                self.agent_operator_status_var.set("Follow-up received")
        except Exception as exc:
            self._agent_operator_log_line("err", f"follow-up failed: {exc}\n")
            try:
                self._phone_mirror_fail(f"Follow-up failed: {exc}", "followup_failed")
            except Exception:
                pass

    def _remote_operator_attach(self, payload_text):
        try:
            self._ensure_agent_operator()
        except Exception:
            return
        if not self.agent_operator_Image:
            return
        try:
            payload = json.loads(payload_text or "{}")
        except json.JSONDecodeError:
            return
        paths = payload.get("paths") if isinstance(payload, dict) else None
        if not isinstance(paths, list):
            return
        loop = self.agent_operator_loop
        running = bool(loop and loop.is_running())
        image_exts = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
        images = []
        texts = []
        for path in paths:
            name = os.path.basename(path)
            try:
                if os.path.splitext(path)[1].lower() in image_exts:
                    images.append((name, self.agent_operator_Image.open(path).convert("RGBA")))
                else:
                    with open(path, "r", encoding="utf-8", errors="replace") as handle:
                        texts.append((name, handle.read(PHONE_TEXT_ATTACHMENT_LIMIT)))
            except Exception:
                continue
        if not images and not texts:
            return
        if running:
            # Live run: inject as a follow-up (adds context to the task in flight).
            note = "\n\n".join(
                f"[attached file: {name}]\n{body[:8000]}"
                + ("\n…[truncated]" if len(body) > 8000 else "")
                for name, body in texts
            )
            loop.add_follow_up(note, images=[image for _, image in images])
            for name, _ in images:
                self._agent_operator_log_line("status", f"FOLLOW-UP IMAGE: {name}\n")
            for name, _ in texts:
                self._agent_operator_log_line("status", f"FOLLOW-UP FILE: {name}\n")
        else:
            # Idle: queue for the next Run() call.
            for name, image in images:
                self._agent_operator_add_image_attachment(image.convert("RGB"), name)
            for name, body in texts:
                self._agent_operator_add_text_attachment(body, name)

    def _remote_operator_clear_attachments(self):
        try:
            self.agent_operator_attachments.clear()
            self._agent_operator_render_attachments()
        except Exception:
            pass

    def _remote_operator_clear(self):
        try:
            self._ensure_agent_operator()
        except Exception:
            return
        loop = self.agent_operator_loop
        try:
            if loop and loop.is_running():
                self.agent_operator_stop_requested = True
                loop.stop()
        except Exception:
            pass
        try:
            if self.agent_operator_task:
                self.agent_operator_task.delete("1.0", "end")
        except Exception:
            pass
        try:
            self.agent_operator_clear_log()
        except Exception:
            pass
        self.agent_operator_attachments = []
        try:
            self._agent_operator_render_attachments()
        except Exception:
            pass
        self._phone_mirror_write({"type": "cleared", "ts": time.time()})
        if self.agent_operator_status_var:
            self.agent_operator_status_var.set("Cleared")
        try:
            self._agent_operator_sync_buttons()
        except Exception:
            pass

    def _remote_apply_operator_options(self, options, run=False):
        if not isinstance(options, dict):
            return
        try:
            self._ensure_agent_operator()
        except Exception:
            return
        settings = dict(self.config.get("ai_operator") or DEFAULT_CONFIG["ai_operator"])
        mapping = {
            "monitor": ("agent_operator_monitor_var", str),
            "model": ("agent_operator_model_var", str),
            "planner_model": ("agent_operator_planner_model_var", str),
            "reasoning": ("agent_operator_reason_var", str),
            "steps": ("agent_operator_steps_var", str),
            "delay": ("agent_operator_delay_var", str),
            "tts": ("agent_operator_tts_var", bool),
            "voice": ("agent_operator_voice_var", str),
            "shell": ("agent_operator_shell_var", bool),
            "codex_auth": ("agent_operator_codex_var", bool),
            "provider_mode": (None, str),
        }
        for key, (attr, cast) in mapping.items():
            if key not in options:
                continue
            value = options[key]
            try:
                value = cast(value) if cast is not bool else bool(value)
            except Exception:
                continue
            settings[key] = value
            var = getattr(self, attr, None) if attr else None
            if var is not None:
                try:
                    var.set(value)
                except Exception:
                    pass
        # Pick up root-level settings (especially the API key) written by the
        # phone bridge before saving the live Operator fields back.
        disk_config = load_config()
        if "openai_api_key" in disk_config:
            self.config["openai_api_key"] = disk_config.get("openai_api_key") or ""
        self.config["ai_operator"] = merge_dict(DEFAULT_CONFIG["ai_operator"], settings)
        self._sync_agent_operator_api_key(force_config=True)
        try:
            self.agent_operator_settings = self.config["ai_operator"]
        except Exception:
            pass
        try:
            save_config(self.config)
        except Exception:
            pass

    def _phone_control_monitor(self):
        try:
            if self._ensure_agent_operator():
                monitor = self._agent_operator_selected_monitor()
                if monitor:
                    return monitor
        except Exception:
            pass

        class MonitorBounds:
            pass

        monitor = MonitorBounds()
        monitor.left = 0
        monitor.top = 0
        monitor.width = max(1, self.root.winfo_screenwidth())
        monitor.height = max(1, self.root.winfo_screenheight())
        return monitor

    def _phone_control_show(self, target=""):
        target = str(target or "").strip().upper()
        if target:
            self.phone_control_log = f"iPhone connected\nTarget: {target}"
        else:
            self.phone_control_log = "iPhone connected"
        self.phone_control_deadline = time.perf_counter() + 18.0
        self.phone_control_visible = True
        monitor = self._phone_control_monitor()
        if self.phone_control_native_overlay:
            self.phone_control_native_overlay.show(monitor)
            self.phone_control_native_overlay.set_log(self.phone_control_log)
        if self.phone_control_after is None:
            self._phone_control_tick()

    def _phone_control_tick(self):
        self.phone_control_after = None
        if not self.phone_control_visible:
            return
        if time.perf_counter() > self.phone_control_deadline:
            self._phone_control_hide()
            return
        self.phone_control_pulse = (self.phone_control_pulse + 1) % 80
        wave = abs(40 - self.phone_control_pulse) / 40
        if self.phone_control_native_overlay:
            self.phone_control_native_overlay.update(wave)
            self.phone_control_native_overlay.set_log(self.phone_control_log)
        self.phone_control_after = self.root.after(110, self._phone_control_tick)

    def _phone_control_hide(self):
        self.phone_control_visible = False
        if self.phone_control_native_overlay:
            self.phone_control_native_overlay.hide()
        if self.phone_control_after is not None:
            try:
                self.root.after_cancel(self.phone_control_after)
            except tk.TclError:
                pass
            self.phone_control_after = None

    def _enable_file_drop(self):
        if os.name != "nt":
            return
        try:
            self.root.update_idletasks()
            hwnd = self.root.winfo_id()
            user32 = ctypes.windll.user32
            shell32 = ctypes.windll.shell32
            if ctypes.sizeof(ctypes.c_void_p) == 8:
                set_wnd_proc = user32.SetWindowLongPtrW
                set_wnd_proc.restype = ctypes.c_void_p
            else:
                set_wnd_proc = user32.SetWindowLongW
                set_wnd_proc.restype = ctypes.c_long
            set_wnd_proc.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
            user32.CallWindowProcW.argtypes = [
                ctypes.c_void_p,
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ]
            user32.CallWindowProcW.restype = ctypes.c_longlong
            shell32.DragAcceptFiles(hwnd, True)
            self._drop_wndproc = ctypes.WINFUNCTYPE(
                ctypes.c_longlong,
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )(self._drop_window_proc)
            self._old_wndproc = set_wnd_proc(hwnd, -4, self._drop_wndproc)
        except Exception:
            self._old_wndproc = None

    def _drop_window_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_DROPFILES:
            paths = self.read_drop_paths(wparam)
            ctypes.windll.shell32.DragFinish(wparam)
            self.root.after(0, lambda p=paths: self.handle_drop(p))
            return 0
        if getattr(self, "_old_wndproc", None):
            return ctypes.windll.user32.CallWindowProcW(self._old_wndproc, hwnd, msg, wparam, lparam)
        return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def read_drop_paths(self, hdrop):
        shell32 = ctypes.windll.shell32
        shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
        shell32.DragQueryFileW.restype = wintypes.UINT
        count = shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
        paths = []
        for index in range(count):
            length = shell32.DragQueryFileW(hdrop, index, None, 0)
            buffer = ctypes.create_unicode_buffer(length + 1)
            shell32.DragQueryFileW(hdrop, index, buffer, length + 1)
            paths.append(buffer.value)
        return paths

    def _redraw_shell(self, _event=None):
        width = self.shell.winfo_width()
        height = self.shell.winfo_height()
        if width <= 1 or height <= 1:
            return
        radius = int(self.c("radius"))
        self.shell.delete("shell")
        rounded_rect(self.shell, 8, 8, width - 8, height - 8, radius + 2, fill="#060a11", outline="", tags="shell")
        rounded_rect(
            self.shell,
            10,
            10,
            width - 10,
            height - 10,
            radius,
            fill=self.c("panel"),
            outline="#26384f",
            width=1,
            tags="shell",
        )
        self.shell.create_line(44, 13, width - 44, 13, fill=self.c("accent"), width=1, tags="shell")
        self.shell.tag_lower("shell")
        self.shell.coords(self.panel_window, 18, 18)
        self.shell.itemconfigure(self.panel_window, width=max(1, width - 36), height=max(1, height - 36))

    def _start_move(self, event):
        self.drag_x = event.x_root - self.root.winfo_x()
        self.drag_y = event.y_root - self.root.winfo_y()

    def _drag_move(self, event):
        x = event.x_root - self.drag_x
        y = event.y_root - self.drag_y
        self.root.geometry(f"+{x}+{y}")

    def _start_resize(self, event):
        self.resize_start_x = event.x_root
        self.resize_start_y = event.y_root
        self.resize_start_w = self.root.winfo_width()
        self.resize_start_h = self.root.winfo_height()

    def _drag_resize(self, event):
        width = max(860, self.resize_start_w + event.x_root - self.resize_start_x)
        height = max(560, self.resize_start_h + event.y_root - self.resize_start_y)
        self.root.geometry(f"{width}x{height}")

    def _start_chat_resize(self, event):
        self.chat_resize_start_x = event.x_root
        self.chat_resize_start_w = self.chat_panel.winfo_width()

    def _drag_chat_resize(self, event):
        delta = self.chat_resize_start_x - event.x_root
        width = max(260, min(720, self.chat_resize_start_w + delta))
        self.chat_panel.configure(width=width)
        self.config["chat_width"] = width
        save_config(self.config)

    def _clamp_window_to_screen(self):
        self.root.update_idletasks()
        raw_size = (self.config.get("window") or DEFAULT_CONFIG["window"]).split("+", 1)[0]
        try:
            raw_width, raw_height = raw_size.lower().split("x", 1)
            current_width = int(raw_width)
            current_height = int(raw_height)
        except (TypeError, ValueError):
            current_width, current_height = 1100, 760
        width = min(max(860, current_width), max(860, self.root.winfo_screenwidth() - 80))
        height = min(max(560, current_height), max(560, self.root.winfo_screenheight() - 80))
        self.root.geometry(f"{width}x{height}")

    def bind_drag(self, widget):
        widget.bind("<ButtonPress-1>", self._start_move)
        widget.bind("<B1-Motion>", self._drag_move)

    def page_title(self, text):
        tk.Label(self.page, text=text, bg=self.c("panel"), fg=self.c("text"), font=self.font(18, "bold")).pack(anchor="w", pady=(0, 12))

    def section(self, parent, text):
        tk.Label(parent, text=text, bg=parent.cget("bg"), fg=self.c("muted"), font=self.font(9, "bold")).pack(anchor="w", padx=4, pady=(4, 8))

    def stat_card(self, parent, label, value):
        card = self.card(parent)
        tk.Label(card, text=label, bg=self.c("surface"), fg=self.c("muted"), font=self.font(9)).pack(anchor="w", padx=12, pady=(10, 0))
        tk.Label(card, text=value, bg=self.c("surface"), fg=self.c("text"), font=self.font(17, "bold")).pack(anchor="w", padx=12, pady=(0, 10))
        return card

    def card(self, parent):
        return tk.Frame(parent, bg=self.c("surface"), highlightbackground="#223247", highlightthickness=1, bd=0)

    def entry(self, parent):
        return tk.Text(
            parent,
            height=1,
            bg=self.c("panel2"),
            fg=self.c("text"),
            insertbackground=self.c("text"),
            selectbackground="#29415d",
            relief="flat",
            bd=0,
            padx=10,
            pady=8,
            wrap="none",
            font=self.font(10),
        )

    def single_line(self, parent, value=""):
        item = self.entry(parent)
        item.insert("1.0", str(value))
        return item

    def muted(self, parent, text):
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=self.c("muted"), font=self.font(8), anchor="w")

    def _header_border_color(self):
        return self.blend_color(self.c("panel"), self.c("muted"), 0.22)

    def _build_header_toolbar(self):
        toolbar = tk.Frame(self.header, bg=self.c("panel"))
        toolbar.pack(side="right")

        stats = tk.Frame(toolbar, bg=self.c("panel"))
        stats.pack(side="left", padx=(0, 10))
        self.usage_account_labels = []
        for index, text in enumerate(("calle --", "contact --")):
            label = self.header_stat(stats, text)
            label.pack(side="left", padx=(0, 6 if index == 0 else 0))
            self.usage_account_labels.append(label)

        tk.Frame(toolbar, bg=self._header_border_color(), width=1).pack(side="left", fill="y", padx=(0, 10), pady=8)

        actions = tk.Frame(toolbar, bg=self.c("panel"))
        actions.pack(side="left")
        self.restart_button = self.header_btn(actions, "\u27f2", self.restart_application, hint="Restart app")
        self.restart_button.pack(side="left", padx=(0, 4))
        self.refresh_button = self.header_btn(actions, "\u21bb", self.refresh_all, hint="Refresh data")
        self.refresh_button.pack(side="left", padx=(0, 4))
        self.hide_button = self.header_btn(actions, "\u2013", self.hide, hint="Hide window")
        self.hide_button.pack(side="left")

    def header_stat(self, parent, text):
        border = self._header_border_color()
        return tk.Label(
            parent,
            text=text,
            bg=self.c("surface2"),
            fg=self.c("muted"),
            padx=9,
            pady=3,
            font=self.font(8),
            highlightthickness=1,
            highlightbackground=border,
            highlightcolor=border,
        )

    def header_btn(self, parent, text, command, hint=""):
        border = self._header_border_color()
        btn = tk.Button(
            parent,
            text=text,
            command=command,
            bg=self.c("surface2"),
            fg=self.c("muted"),
            activebackground=self.blend_color(self.c("surface2"), self.c("accent"), 0.18),
            activeforeground=self.c("text"),
            relief="flat",
            bd=0,
            width=3,
            padx=0,
            pady=2,
            cursor="hand2",
            font=("Segoe UI Symbol", max(9, int(self.c("font_size")))),
            highlightthickness=1,
            highlightbackground=border,
            highlightcolor=border,
        )
        if hint:
            self._bind_header_hint(btn, hint)
        return btn

    def header_chip(self, parent, text, command, hint=""):
        border = self._header_border_color()
        btn = tk.Button(
            parent,
            text=text,
            command=command,
            bg=self.c("surface2"),
            fg=self.c("muted"),
            activebackground=self.blend_color(self.c("surface2"), self.c("accent"), 0.18),
            activeforeground=self.c("text"),
            relief="flat",
            bd=0,
            padx=10,
            pady=3,
            cursor="hand2",
            font=self.font(8),
            highlightthickness=1,
            highlightbackground=border,
            highlightcolor=border,
        )
        if hint:
            self._bind_header_hint(btn, hint)
        return btn

    def quick_tool_chip(self, parent, text, command, hint=""):
        border = self.blend_color(self._header_border_color(), self.c("text"), 0.22)
        btn = tk.Button(
            parent,
            text=text,
            command=command,
            bg=self.c("surface2"),
            fg=self.c("text"),
            activebackground=self.blend_color(self.c("surface2"), self.c("accent"), 0.26),
            activeforeground=self.c("text"),
            relief="flat",
            bd=0,
            padx=14,
            pady=6,
            cursor="hand2",
            font=self.font(9, "bold"),
            highlightthickness=1,
            highlightbackground=border,
            highlightcolor=border,
        )
        if hint:
            self._bind_header_hint(btn, hint)
        return btn

    def quick_tools_category(self, parent, title, description, accent_color):
        bg = self.blend_color(self.c("surface"), self.c("panel"), 0.42)
        border = self.blend_color(self._header_border_color(), accent_color, 0.18)
        group = tk.Frame(
            parent,
            bg=bg,
            highlightthickness=1,
            highlightbackground=border,
            padx=10,
            pady=9,
        )
        header = tk.Frame(group, bg=bg)
        header.pack(fill="x", pady=(0, 8))
        marker = tk.Frame(header, bg=accent_color, width=3, height=24)
        marker.pack(side="left", fill="y", padx=(0, 8))
        marker.pack_propagate(False)
        heading = tk.Frame(header, bg=bg)
        heading.pack(side="left", fill="x", expand=True)
        tk.Label(
            heading,
            text=title,
            bg=bg,
            fg=self.c("text"),
            anchor="w",
            font=self.font(8, "bold"),
        ).pack(fill="x")
        tk.Label(
            heading,
            text=description,
            bg=bg,
            fg=self.blend_color(self.c("muted"), self.c("text"), 0.28),
            anchor="w",
            font=self.font(8),
        ).pack(fill="x")
        cards = tk.Frame(group, bg=bg)
        cards.pack(fill="both", expand=True)
        return group, cards

    def quick_tool_card(
        self,
        parent,
        icon,
        title,
        description,
        command,
        *,
        accent_color=None,
        featured=False,
    ):
        accent_color = accent_color or self.c("accent")
        base_bg = self.blend_color(self.c("surface2"), self.c("panel"), 0.22)
        hover_bg = self.blend_color(base_bg, accent_color, 0.10 if not featured else 0.16)
        border = self.blend_color(
            self._header_border_color(),
            accent_color,
            0.34 if featured else 0.20,
        )
        card = tk.Frame(
            parent,
            bg=base_bg,
            highlightthickness=1,
            highlightbackground=border,
            highlightcolor=accent_color,
            padx=10,
            pady=9,
            cursor="hand2",
        )
        icon_bg = self.blend_color(
            self.c("surface2"),
            accent_color,
            0.38 if featured else 0.28,
        )
        icon_box = tk.Frame(card, bg=icon_bg, width=40, height=40, cursor="hand2")
        icon_box.pack(anchor="w")
        icon_box.pack_propagate(False)
        icon_label = tk.Label(
            icon_box,
            text=icon,
            bg=icon_bg,
            fg=accent_color,
            font=self.font(15, "bold"),
            cursor="hand2",
        )
        icon_label.place(relx=0.5, rely=0.5, anchor="center")
        title_label = tk.Label(
            card,
            text=title,
            bg=base_bg,
            fg=self.c("text"),
            anchor="w",
            font=self.font(9, "bold"),
            cursor="hand2",
        )
        title_label.pack(fill="x", pady=(8, 2))
        description_label = tk.Label(
            card,
            text=description,
            bg=base_bg,
            fg=self.blend_color(self.c("muted"), self.c("text"), 0.18),
            anchor="nw",
            justify="left",
            wraplength=140,
            font=self.font(8),
            cursor="hand2",
        )
        description_label.pack(fill="both", expand=True)

        card._quick_card_title = title_label
        card._quick_card_icon_box = icon_box
        card._quick_card_icon = icon_label
        card._quick_card_description = description_label
        card._quick_card_accent = accent_color

        widgets = (card, icon_box, icon_label, title_label, description_label)

        def set_hover(active):
            background = hover_bg if active else base_bg
            card.configure(
                bg=background,
                highlightbackground=accent_color if active else border,
            )
            title_label.configure(bg=background)
            description_label.configure(bg=background)

        for widget in widgets:
            widget.bind("<Button-1>", lambda _event, action=command: action(), add="+")
            widget.bind("<Enter>", lambda _event: set_hover(True), add="+")
            widget.bind("<Leave>", lambda _event: set_hover(False), add="+")
        return card

    def record_stop_chip(self, parent, text, command, hint=""):
        btn = tk.Button(
            parent,
            text=text,
            command=command,
            bg=self.c("danger"),
            fg="#ffffff",
            activebackground=self.c("danger"),
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            padx=14,
            pady=6,
            cursor="hand2",
            font=self.font(9, "bold"),
            highlightthickness=1,
            highlightbackground=self.c("danger"),
            highlightcolor=self.c("danger"),
        )
        if hint:
            self._bind_header_hint(btn, hint)
        return btn

    def _bind_header_hint(self, widget, hint):
        def show(_event=None):
            if hasattr(self, "subtitle"):
                try:
                    self._header_hint_prev = self.subtitle.cget("text")
                    self.subtitle.configure(text=hint, fg=self.c("muted"))
                except tk.TclError:
                    pass

        def restore(_event=None):
            if hasattr(self, "subtitle") and hasattr(self, "_header_hint_prev"):
                try:
                    self.subtitle.configure(text=self._header_hint_prev, fg=self.c("muted"))
                except tk.TclError:
                    pass

        widget.bind("<Enter>", show)
        widget.bind("<Leave>", restore)

    def button(self, parent, text, command, compact=False, active=False):
        bg = self.c("accent") if active else self.c("panel2")
        fg = "#061018" if active else self.c("text")
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=self.c("accent"),
            activeforeground="#061018",
            relief="flat",
            bd=0,
            padx=12 if compact else 14,
            pady=6 if compact else 10,
            cursor="hand2",
            anchor="w",
            font=self.font(9, "bold"),
        )

    def usage_pill(self, parent, text):
        return self.header_stat(parent, text)

    def style_option(self, option):
        option.configure(
            bg=self.c("panel2"),
            fg=self.c("text"),
            activebackground=self.c("accent"),
            activeforeground="#061018",
            relief="flat",
            bd=0,
            highlightthickness=0,
            font=self.font(9),
        )
        option["menu"].configure(bg=self.c("panel2"), fg=self.c("text"), activebackground=self.c("accent"))

    def _init_brand_font(self):
        if os.name == "nt" and BRAND_FONT_PATH.exists():
            try:
                ctypes.windll.gdi32.AddFontResourceExW(str(BRAND_FONT_PATH), FR_PRIVATE, 0)
            except (AttributeError, OSError):
                pass
        try:
            families = {family.casefold() for family in tkfont.families(self.root)}
        except tk.TclError:
            families = set()
        if BRAND_FONT_FAMILY.casefold() in families:
            return BRAND_FONT_FAMILY
        return "Segoe UI"

    def brand_font(self, size=None, weight="bold"):
        base = int(self.c("font_size"))
        actual = size if size is not None else base
        if size is not None and size <= 10:
            actual = max(7, base + (size - 10))
        return (getattr(self, "brand_font_family", "Segoe UI"), actual, weight)

    def font(self, size=None, weight="normal"):
        base = int(self.c("font_size"))
        actual = size if size is not None else base
        if size is not None and size <= 10:
            actual = max(7, base + (size - 10))
        return ("Segoe UI", actual, weight)

    def c(self, key):
        return self.theme.get(key, DEFAULT_CONFIG["theme"][key])

    def clear(self, widget):
        for child in widget.winfo_children():
            child.destroy()

    def run(self):
        self.root.mainloop()


def send_command(command):
    try:
        with socket.create_connection((HOST, PORT), timeout=0.15) as client:
            client.sendall(command.encode("utf-8"))
        return True
    except OSError:
        return False


def main():
    enable_per_monitor_dpi_awareness()
    set_windows_app_id()
    parser = argparse.ArgumentParser()
    parser.add_argument("--toggle", action="store_true")
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--hide", action="store_true")
    parser.add_argument("--quit", action="store_true")
    parser.add_argument("--background", action="store_true")
    args = parser.parse_args()

    if args.quit:
        send_command("quit")
        return
    if args.hide:
        send_command("hide")
        return
    if args.show and send_command("show"):
        return
    if args.toggle and send_command("toggle"):
        return

    if not claim_single_instance():
        send_command("show" if args.show else "toggle")
        return

    app = HelperOverlay(background=args.background)
    app.run()


if __name__ == "__main__":
    main()
