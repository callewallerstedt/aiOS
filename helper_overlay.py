import argparse
import base64
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
import urllib.request
from tkinter import colorchooser, filedialog, messagebox, simpledialog

import codex_usage
from voice_settings import (
    COMPUTE_TYPES,
    DEFAULT_VOICE_DICTATION,
    LANGUAGE_LABELS,
    WHISPER_LANGUAGES,
    WHISPER_MODELS,
    load_voice_dictation_settings,
    merge_voice_dictation,
    resolve_transcribe_language,
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
CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
TRANSPARENT = "#010203"
WM_DROPFILES = 0x0233
BRAND_FONT_PATH = BASE_DIR / "assets" / "fonts" / "Michroma-Regular.ttf"
BRAND_FONT_FAMILY = "Michroma"
STARTUP_FRAME_DIR = BASE_DIR / "assets" / "startup" / "aios-logo-reveal-frames"
STARTUP_SOUND_PATH = BASE_DIR / "assets" / "startup" / "aios-startup.wav"
OPERATOR_SOUND_PATH = BASE_DIR / "assets" / "startup" / "aios-operator.wav"
APP_ICON_PATH = BASE_DIR / "assets" / "aios-logo.ico"
TRAY_ICON_PATH = BASE_DIR / "assets" / "rectangle-logo.ico"
APP_USER_MODEL_ID = "aiOS.Desktop.Helper"
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
LWA_ALPHA = 0x00000002
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
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203
NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
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
TRAY_RESTART_APP = 1005
TRAY_QUIT = 1006


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

    def __init__(self, owner, title_text="aiOPERATOR controlling computer", log_title="aiOPERATOR LOG", class_name=None):
        self.owner = owner
        self.class_name = class_name or self.CLASS_NAME
        self.windows = {}
        self.labels = {}
        self.log_text = ""
        self.title_text = title_text
        self.log_title = log_title
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

        wndproc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t)

        def wndproc(hwnd, message, wparam, lparam):
            if message == 0x000F:
                return self._paint(hwnd)
            if message == 0x0021:
                return 3
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
        for name in ("top", "bottom", "left", "right", "label", "log"):
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
            self.user32.SetLayeredWindowAttributes(hwnd, 0, 210 if name == "log" else 190, LWA_ALPHA)

    def show(self, monitor):
        self.ensure()
        if not self.windows:
            return
        border = 8
        inset = 9
        left, top = int(monitor.left), int(monitor.top)
        width, height = int(monitor.width), int(monitor.height)
        label_w = min(560, max(280, width - 120))
        log_w = min(580, max(360, int(width * 0.34)))
        log_h = min(190, max(120, int(height * 0.20)))
        placements = {
            "top": (left + inset, top + inset, max(1, width - inset * 2), border),
            "bottom": (left + inset, top + height - inset - border, max(1, width - inset * 2), border),
            "left": (left + inset, top + inset, border, max(1, height - inset * 2)),
            "right": (left + width - inset - border, top + inset, border, max(1, height - inset * 2)),
            "label": (left + max(20, (width - label_w) // 2), top + inset + 18, label_w, 38),
            "log": (left + width - inset - log_w - 12, top + height - inset - log_h - 22, log_w, log_h),
        }
        for name, hwnd in self.windows.items():
            x, y, w, h = placements[name]
            self.user32.MoveWindow(hwnd, x, y, w, h, True)
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
        log_alpha = int(220 + wave * 30)
        for name, hwnd in self.windows.items():
            value = log_alpha if name == "log" else label_alpha if name == "label" else alpha
            self.user32.SetLayeredWindowAttributes(hwnd, 0, max(0, min(255, value)), LWA_ALPHA)
            self.user32.InvalidateRect(hwnd, None, True)

    def set_log(self, text):
        self.log_text = text or ""
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

        text_rect = wintypes.RECT(rect.left + 12, rect.top + 34, rect.right - 12, rect.bottom - 10)
        self.gdi32.SetTextColor(hdc, self._colorref(self.owner.c("text")))
        font = self.gdi32.CreateFontW(-12, 0, 0, 0, 400, 0, 0, 0, 0, 0, 0, 5, 0, "Cascadia Code")
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


class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
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


DEFAULT_CONFIG = {
    "project_root": get_setting("COMPUTER_HELPER_PROJECT_ROOT", r"D:\Projects"),
    "assistant_model": get_setting("COMPUTER_HELPER_MODEL", "gpt-4.1-nano"),
    "codex_model": "gpt-4.1-nano",
    "codex_reasoning": "none",
    "quick_codex_model": "gpt-5-mini",
    "quick_codex_reasoning": "none",
    "chat_model": "gpt-5-mini",
    "openai_api_key": "",
    "codex_sandbox": "workspace-write",
    "window": "1120x720+520+90",
    "chat_width": 330,
    "app_usage": {},
    "chat_history": [],
    "todos": [],
    "linked_projects": [],
    "hidden_projects": [],
    "ai_operator": {
        "monitor": "",
        "model": "gpt-5.5",
        "reasoning": "medium",
        "steps": "25",
        "delay": "0.20",
        "tts": False,
        "voice": "nova",
        "shell": False,
        "codex_auth": False,
    },
    "voice_dictation": dict(DEFAULT_VOICE_DICTATION),
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
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as file:
                config = merge_dict(config, json.load(file))
        except (OSError, json.JSONDecodeError):
            pass

    if str(config.get("project_root", "")).casefold() == r"c:\codex":
        config["project_root"] = r"D:\Projects"
    config.setdefault("assistant_model", "gpt-4.1-nano")
    config.setdefault("codex_model", "gpt-4.1-nano")
    config.setdefault("codex_reasoning", "none")
    config.setdefault("quick_codex_model", "gpt-5-mini")
    if config.get("quick_codex_model") in {"gpt-4.1-nano", "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.5"}:
        config["quick_codex_model"] = "gpt-5-mini"
    config.setdefault("quick_codex_reasoning", "none")
    config.setdefault("chat_model", "gpt-5-mini")
    if config.get("chat_model") in {"gpt-4o-mini", "gpt-4.1-nano", "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.5"}:
        config["chat_model"] = "gpt-5-mini"
    config.setdefault("openai_api_key", "")
    config.setdefault("codex_sandbox", "workspace-write")
    config.setdefault("todos", [])
    config.setdefault("linked_projects", [])
    config.setdefault("hidden_projects", [])
    config["ai_operator"] = merge_dict(DEFAULT_CONFIG["ai_operator"], config.get("ai_operator") or {})
    config["voice_dictation"] = merge_voice_dictation(config.get("voice_dictation"))
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
            "description": "Switch aiOS tab: Dashboard, Projects, Codex, Apps, Drop, AI Operator, or Settings.",
            "parameters": {
                "type": "object",
                "properties": {"tab": {"type": "string", "enum": ["Dashboard", "Projects", "Codex", "Apps", "Drop", "AI Operator", "Settings"]}},
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
    found = shutil.which("codex") or shutil.which("codex.exe")
    if found:
        return found

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
    return ""


def codex_env():
    env = os.environ.copy()
    env.pop("OPENAI_API_KEY", None)
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
    codex = find_codex()
    if not codex:
        return False
    try:
        subprocess.Popen(
            ["cmd.exe", "/c", "start", "", "cmd.exe", "/k", f'"{codex}" login'],
            env=codex_env(),
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


class ScrollFrame(tk.Frame):
    _instances = []

    def __init__(self, parent, bg):
        super().__init__(parent, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self.window = self.canvas.create_window(0, 0, anchor="nw", window=self.inner)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._update_width)
        ScrollFrame._instances.append(self)

    def _on_inner_configure(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._bind_wheel_tree(self.inner)

    def _update_width(self, event):
        self.canvas.itemconfigure(self.window, width=event.width)

    def _bind_wheel_tree(self, widget):
        try:
            widget.bind("<MouseWheel>", self._mousewheel, add="+")
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

    def _deepest_scroll(self, widget):
        matches = [frame for frame in ScrollFrame._instances if frame._contains_widget(widget) and frame.winfo_ismapped()]
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


class HelperOverlay:
    def __init__(self):
        self.config = load_config()
        self.theme = self.config["theme"]
        self.project_root = Path(self.config["project_root"])
        self.project_root.mkdir(parents=True, exist_ok=True)
        self.shortcuts = start_menu_shortcuts()
        self.apps = self._discover_apps()
        self.active_tab = "Dashboard"
        self.active_project = None
        self.page_view = None
        self.busy = False
        self.chat_busy_since = 0.0
        self.history = self.load_chat_history()
        self.dropped_paths = []
        self.codex_process = None
        self.quick_process = None
        self.codex_log = []
        self.chat_run_id = 0
        self._ui_queue = queue.Queue()
        self.thinking_step = 0
        self.thinking_after = None
        self.drag_x = 0
        self.drag_y = 0
        self.chat_resize_start_x = 0
        self.chat_resize_start_w = 0
        self._create_menu_popup = None
        self._autosave_after = None
        self._autosave_project_path = None
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
        self._tray_current_h = 10
        self._tray_open = False
        self._tray_target_h = 66
        self._tray_peek_h = 10
        self._operator_sound_last_at = 0.0
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
        self.screen_record_monitors = []
        self.screen_record_windows = []
        self.screen_record_monitor_var = None
        self.screen_record_window_var = None
        self.agent_clicker_dir = AGENT_CLICKER_DIR
        self.agent_operator_imported = False
        self.agent_operator_error = None
        self.agent_operator_loop = None
        self.agent_operator_monitors = []
        self.agent_operator_event_q = queue.Queue()
        self.agent_operator_current_image = None
        self.agent_operator_tk_preview = None
        self.agent_operator_last_clicks = []
        self.agent_operator_log_buffer = []
        self.agent_operator_booted = False
        self.agent_operator_booting = False
        self.agent_operator_boot_progress = 0
        self.agent_operator_boot_after = None
        self.agent_operator_boot_label = None
        self.agent_operator_boot_status = None
        self.agent_operator_boot_canvas = None
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
        self.agent_operator_default_model = "gpt-5.5"
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
        self.agent_operator_settings = self.config.get("ai_operator") or dict(DEFAULT_CONFIG["ai_operator"])
        self.agent_operator_canvas = None
        self.agent_operator_log = None
        self.agent_operator_run_btn = None
        self.agent_operator_pause_btn = None
        self.agent_operator_stop_btn = None
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
        self.agent_operator_native_overlay = NativeOperatorOverlay(self)
        self.phone_control_native_overlay = NativeOperatorOverlay(
            self,
            "Phone Control",
            "PHONE CONTROL",
            class_name="aiOSPhoneControlOverlay",
        )
        self._init_tray_icon()

        self._clamp_window_to_screen()
        self._build_ui()
        self._bind_keys()
        self._enable_file_drop()
        self._start_command_server()
        self._schedule_usage_refresh()
        self._schedule_chat_watchdog()
        self._poll_ui_queue()
        self._poll_agent_operator_events()
        self._ensure_voice_server()
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
        for tab in ("Dashboard", "Projects", "Codex", "Apps", "Drop", "AI Operator", "Settings"):
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
            text="Assistant",
            bg=self.c("surface"),
            fg=self.c("text"),
            font=self.font(10, "bold"),
        ).pack(anchor="w")
        model_label = self.config.get("chat_model") or "gpt-5-mini"
        key_ok = bool(self.get_openai_api_key())
        codex_ok, codex_label = codex_auth_info()
        if key_ok:
            auth_label = "API key ready"
        elif codex_ok:
            auth_label = f"Codex {codex_label}"
        else:
            auth_label = "No auth"
        meta = f"{model_label} · {auth_label}"
        self.chat_account_label = tk.Label(
            head_left,
            text=meta,
            bg=self.c("surface"),
            fg=self.c("success") if (key_ok or codex_ok) else self.c("danger"),
            font=self.font(8),
        )
        self.chat_account_label.pack(anchor="w")
        chat_actions = tk.Frame(chat_head, bg=self.c("surface"))
        chat_actions.pack(side="right")
        self.header_chip(chat_actions, "Reset", self.reset_chat, hint="Clear chat").pack(side="right", padx=(4, 0))
        self.header_chip(chat_actions, "Login", self.codex_login, hint="Sign in to Codex").pack(side="right", padx=(4, 0))
        self.header_chip(chat_actions, "Settings", lambda: self.render_tab("Settings"), hint="OpenAI key & model").pack(
            side="right"
        )

        chat_box = tk.Frame(chat_content, bg=self.c("surface"))
        chat_box.pack(fill="both", expand=True, padx=(2, 0), pady=(0, 6))
        self.chat = tk.Text(
            chat_box,
            bg=self.c("surface"),
            fg=self.c("text"),
            insertbackground=self.c("text"),
            selectbackground="#29415d",
            relief="flat",
            bd=0,
            padx=14,
            pady=8,
            wrap="word",
            font=self.font(9),
            state="disabled",
        )
        self.chat_scroll = tk.Scrollbar(
            chat_box,
            orient="vertical",
            command=self.chat.yview,
            width=8,
            bd=0,
            relief="flat",
            troughcolor=self.c("surface"),
            bg=self.c("surface2"),
            activebackground=self.c("accent"),
            highlightthickness=0,
        )
        self.chat.configure(yscrollcommand=self.chat_scroll.set)
        self.chat_scroll.pack(side="right", fill="y", padx=(0, 2))
        self.chat.pack(side="left", fill="both", expand=True)
        self.chat.tag_configure(
            "user_label",
            foreground="#95c7ff",
            font=self.font(8, "bold"),
            spacing1=8,
            spacing3=2,
        )
        self.chat.tag_configure("user", foreground=self.c("text"), spacing3=2, lmargin1=12, lmargin2=12)
        assistant_bg = self.blend_color(self.c("surface"), self.c("text"), 0.08)
        assistant_label_kwargs = dict(
            foreground=self.c("accent"),
            background=assistant_bg,
            font=self.brand_font(8),
            spacing1=10,
            spacing3=4,
            lmargin1=14,
            lmargin2=14,
            rmargin=14,
        )
        assistant_kwargs = dict(
            foreground=self.c("text"),
            background=assistant_bg,
            spacing1=2,
            spacing3=10,
            lmargin1=14,
            lmargin2=14,
            rmargin=14,
            font=self.font(9),
        )
        for key in ("lmargincolor", "rmargincolor"):
            assistant_label_kwargs[key] = assistant_bg
            assistant_kwargs[key] = assistant_bg
        try:
            self.chat.tag_configure("assistant_label", **assistant_label_kwargs)
        except tk.TclError:
            assistant_label_kwargs.pop("lmargincolor", None)
            assistant_label_kwargs.pop("rmargincolor", None)
            self.chat.tag_configure("assistant_label", **assistant_label_kwargs)
        try:
            self.chat.tag_configure("assistant", **assistant_kwargs)
        except tk.TclError:
            assistant_kwargs.pop("lmargincolor", None)
            assistant_kwargs.pop("rmargincolor", None)
            self.chat.tag_configure("assistant", **assistant_kwargs)
        self.chat.tag_configure("heading", foreground=self.c("accent"), font=self.font(10, "bold"), spacing3=4)
        self.chat.tag_configure("code", foreground="#d6e2ff", background="#080d14", font=("Consolas", max(8, int(self.c("font_size")) - 1)))
        self.chat.tag_configure("command", foreground=self.c("success"), font=("Consolas", max(8, int(self.c("font_size")) - 1), "bold"))
        self.chat.tag_configure("diff_add", foreground="#7ee787", font=("Consolas", max(8, int(self.c("font_size")) - 1)))
        self.chat.tag_configure("diff_del", foreground="#ff7b72", font=("Consolas", max(8, int(self.c("font_size")) - 1)))
        self.chat.tag_configure(
            "muted",
            foreground=self.c("muted"),
            background=self.c("surface"),
            font=self.font(8),
        )
        meta_kwargs = dict(
            foreground=self.c("muted"),
            background=assistant_bg,
            font=self.font(8, "italic"),
            spacing3=10,
            lmargin1=14,
            lmargin2=14,
            rmargin=14,
        )
        for key in ("lmargincolor", "rmargincolor"):
            meta_kwargs[key] = assistant_bg
        try:
            self.chat.tag_configure("assistant_meta", **meta_kwargs)
        except tk.TclError:
            meta_kwargs.pop("lmargincolor", None)
            meta_kwargs.pop("rmargincolor", None)
            self.chat.tag_configure("assistant_meta", **meta_kwargs)

        self.thinking_canvas = None
        self._assistant_bg = assistant_bg
        self.thinking_status_text = "thinking"

        bottom = tk.Frame(chat_content, bg=self.c("surface"))
        bottom.pack(fill="x", padx=12, pady=(0, 12))
        self.input = tk.Text(
            bottom,
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
        self.input.pack(side="left", fill="both", expand=True)
        self.send_button = self.button(bottom, "Send", self.send, compact=True)
        self.send_button.pack(side="right", fill="y", padx=(8, 0))

    def render_tab(self, tab):
        previous_tab = self.active_tab
        self.active_tab = tab
        self.page_view = None
        self._build_nav()
        self.clear(self.page)
        if tab == "Dashboard":
            self.render_dashboard()
        elif tab == "Projects":
            self.render_projects()
        elif tab == "Codex":
            self.render_codex()
        elif tab == "Apps":
            self.render_apps()
        elif tab == "Drop":
            self.render_drop()
        elif tab == "AI Operator":
            if previous_tab != "AI Operator":
                self._play_operator_sound()
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
        head = tk.Frame(self.page, bg=self.c("panel"))
        head.pack(fill="x", pady=(0, 12))
        tk.Label(head, text="Dashboard", bg=self.c("panel"), fg=self.c("text"), font=self.font(18, "bold")).pack(
            side="left"
        )
        self.header_btn(head, "+", self._toggle_create_menu, hint="New To-Do or Project").pack(side="right")

        scroll = ScrollFrame(self.page, self.c("panel"))
        scroll.pack(fill="both", expand=True)
        body = scroll.inner

        todos = self.dashboard_todos()
        todo_card = self.card(body)
        todo_card.pack(fill="x", pady=(0, 12))
        self.section(todo_card, "To-Dos")
        if not todos:
            self.muted(todo_card, "No active to-dos. Tap + to add one with a deadline.").pack(
                anchor="w", padx=14, pady=(0, 14)
            )
        else:
            list_frame = tk.Frame(todo_card, bg=self.c("surface"))
            list_frame.pack(fill="x", padx=12, pady=(0, 12))
            for project_path, meta in todos:
                self.dashboard_todo_row(list_frame, project_path, meta).pack(fill="x", pady=(0, 6))

        recent_names = {p.name for p, _m in todos}
        recent = [p for p in self.projects() if p.name not in recent_names][:6]
        recent_card = self.card(body)
        recent_card.pack(fill="x")
        self.section(recent_card, "Recent Projects")
        if not recent:
            self.muted(recent_card, "No other projects yet.").pack(anchor="w", padx=14, pady=(0, 14))
        else:
            for project in recent:
                self.recent_project_row(recent_card, project).pack(fill="x", padx=12, pady=(0, 6))

    def _build_bottom_tray(self):
        tray_bg = self.blend_color(self.c("panel"), self.c("surface2"), 0.62)
        border = self._header_border_color()
        accent_line = self.blend_color(border, self.c("accent"), 0.45)

        self._bottom_tray = tk.Frame(
            self.panel,
            bg=tray_bg,
            highlightthickness=1,
            highlightbackground=border,
        )

        accent = tk.Frame(self._bottom_tray, bg=accent_line, height=1)
        accent.pack(fill="x")

        handle_row = tk.Frame(self._bottom_tray, bg=tray_bg, height=12)
        handle_row.pack(fill="x")
        handle_row.pack_propagate(False)
        handle = tk.Frame(handle_row, bg=self.blend_color(self.c("muted"), self.c("text"), 0.35), width=46, height=3)
        handle.place(relx=0.5, rely=0.5, anchor="center")

        content = tk.Frame(self._bottom_tray, bg=tray_bg)
        content.pack(fill="x", padx=18, pady=(2, 12))

        actions = tk.Frame(content, bg=tray_bg)
        actions.pack(side="left")
        paste_btn = self.quick_tool_chip(
            actions,
            "Paste Image",
            self.save_clipboard_image,
            hint="Save clipboard image to Downloads",
        )
        paste_btn.pack(side="left")
        downloads_btn = self.quick_tool_chip(
            actions,
            "Downloads",
            self.open_downloads_folder,
            hint="Open Downloads folder",
        )
        downloads_btn.pack(side="left", padx=(8, 0))
        record_btn = self.quick_tool_chip(
            actions,
            "Record",
            self.open_screen_recorder_menu,
            hint="Record screen, monitor, area, or window",
        )
        record_btn.pack(side="left", padx=(8, 0))
        self.screen_record_quick_btn = record_btn
        recordings_btn = self.quick_tool_chip(
            actions,
            "Recordings",
            self.open_recordings_folder,
            hint="Open screen recordings folder",
        )
        recordings_btn.pack(side="left", padx=(8, 0))

        self.quick_tools_status = tk.Label(
            content,
            text="Quick tools",
            bg=tray_bg,
            fg=self.blend_color(self.c("muted"), self.c("text"), 0.45),
            anchor="e",
            font=self.font(9, "bold"),
        )
        self.quick_tools_status.pack(side="right", fill="x", expand=True, padx=(12, 0))

        self._bottom_tray.place(relx=0, rely=1, relwidth=1, anchor="sw", height=self._tray_peek_h)
        self._bottom_tray.lift()
        self._tray_current_h = self._tray_peek_h

        tray_widgets = {
            self._bottom_tray,
            accent,
            handle_row,
            handle,
            content,
            actions,
            paste_btn,
            downloads_btn,
            record_btn,
            recordings_btn,
        }
        self._tray_widget_ids = {id(widget) for widget in tray_widgets}
        self._tray_widget_ids.add(id(self.quick_tools_status))

        for widget in tray_widgets:
            widget.bind("<Enter>", self._tray_show, add="+")
            widget.bind("<Leave>", self._tray_schedule_hide, add="+")
        self.quick_tools_status.bind("<Enter>", self._tray_show, add="+")
        self.quick_tools_status.bind("<Leave>", self._tray_schedule_hide, add="+")

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
            panel_h = self.panel.winfo_height()
            if panel_h <= 1:
                return
            y_in_panel = self.root.winfo_pointery() - self.panel.winfo_rooty()
        except tk.TclError:
            return

        hot_zone = max(18, self._tray_peek_h + 8)
        if y_in_panel >= panel_h - hot_zone:
            self._tray_show()
        elif self._tray_open and y_in_panel < panel_h - self._tray_target_h - 6:
            self._tray_schedule_hide()

    def _tray_show(self, _event=None):
        if self._bottom_tray is None:
            return
        if self._tray_hide_job is not None:
            self.root.after_cancel(self._tray_hide_job)
            self._tray_hide_job = None
        self._tray_animate_to(self._tray_target_h)

    def _tray_schedule_hide(self, _event=None):
        if self._bottom_tray is None:
            return
        if self._tray_hide_job is not None:
            self.root.after_cancel(self._tray_hide_job)

        def hide():
            self._tray_hide_job = None
            try:
                widget = self.root.winfo_containing(self.root.winfo_pointerx(), self.root.winfo_pointery())
            except tk.TclError:
                widget = None
            if widget is not None and self._tray_widget_contains(widget):
                return
            try:
                panel_h = self.panel.winfo_height()
                y_in_panel = self.root.winfo_pointery() - self.panel.winfo_rooty()
                if y_in_panel >= panel_h - max(18, self._tray_peek_h + 8):
                    return
            except tk.TclError:
                pass
            self._tray_animate_to(self._tray_peek_h)

        self._tray_hide_job = self.root.after(280, hide)

    def _tray_animate_to(self, target_h):
        if self._bottom_tray is None:
            return
        if self._tray_anim_job is not None:
            self.root.after_cancel(self._tray_anim_job)
            self._tray_anim_job = None

        current = self._tray_current_h
        if abs(current - target_h) <= 1:
            self._tray_current_h = target_h
            self._tray_open = target_h > self._tray_peek_h + 2
            try:
                self._bottom_tray.place_configure(height=int(target_h))
            except tk.TclError:
                pass
            if target_h <= self._tray_peek_h + 1 and self.quick_tools_status is not None:
                try:
                    self.quick_tools_status.configure(
                        text="Quick tools",
                        fg=self.blend_color(self.c("muted"), self.c("text"), 0.45),
                    )
                except tk.TclError:
                    pass
            return

        diff = target_h - current
        step = max(2, min(10, int(abs(diff) * 0.38)))
        next_h = current + step if diff > 0 else current - step
        if diff > 0 and next_h > target_h:
            next_h = target_h
        elif diff < 0 and next_h < target_h:
            next_h = target_h

        self._tray_current_h = next_h
        try:
            self._bottom_tray.place_configure(height=int(next_h))
        except tk.TclError:
            return
        self._tray_anim_job = self.root.after(12, lambda value=target_h: self._tray_animate_to(value))

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
            self._tray_hide_job = self.root.after(2200, lambda: self._tray_animate_to(self._tray_peek_h))
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

    def render_codex(self):
        self.page_title("Codex")
        top = self.card(self.page)
        top.pack(fill="x", pady=(0, 10))

        self.codex_project_var = tk.StringVar(value=str(self.active_project or self.default_project_path()))
        self.codex_model_var = tk.StringVar(value=self.config["codex_model"])
        self.codex_reasoning_var = tk.StringVar(value=self.config["codex_reasoning"])

        row1 = tk.Frame(top, bg=self.c("surface"))
        row1.pack(fill="x", padx=12, pady=(12, 6))
        tk.Label(row1, text="Project", bg=self.c("surface"), fg=self.c("muted"), font=self.font(9, "bold")).pack(side="left")
        self.codex_project_entry = self.single_line(row1, self.codex_project_var.get())
        self.codex_project_entry.pack(side="left", fill="x", expand=True, padx=(8, 8))
        self.button(row1, "Open App", self.codex_open_desktop, compact=True).pack(side="right", padx=(6, 0))

        row2 = tk.Frame(top, bg=self.c("surface"))
        row2.pack(fill="x", padx=12, pady=(0, 12))
        tk.Label(row2, text="Model", bg=self.c("surface"), fg=self.c("muted"), font=self.font(9, "bold")).pack(side="left")
        self.codex_model_entry = self.single_line(row2, self.config["codex_model"])
        self.codex_model_entry.pack(side="left", fill="x", expand=True, padx=(8, 8))
        tk.Label(row2, text="Thinking", bg=self.c("surface"), fg=self.c("muted"), font=self.font(9, "bold")).pack(side="left")
        self.codex_reason = tk.OptionMenu(row2, self.codex_reasoning_var, "none", "low", "medium", "high", "xhigh")
        self.style_option(self.codex_reason)
        self.codex_reason.pack(side="left", padx=(8, 8))
        self.button(row2, "Save", self.save_codex_settings, compact=True).pack(side="right")

        prompt_card = self.card(self.page)
        prompt_card.pack(fill="x", pady=(0, 10))
        self.codex_prompt = tk.Text(
            prompt_card,
            height=5,
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
        self.codex_prompt.pack(side="left", fill="both", expand=True, padx=12, pady=12)
        actions = tk.Frame(prompt_card, bg=self.c("surface"))
        actions.pack(side="right", fill="y", padx=(0, 12), pady=12)
        self.button(actions, "Run Codex", self.run_codex_from_tab, compact=True).pack(fill="x", pady=(0, 8))
        self.button(actions, "Stop", self.stop_codex, compact=True).pack(fill="x")

        output_card = self.card(self.page)
        output_card.pack(fill="both", expand=True)
        self.codex_output = tk.Text(
            output_card,
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
        self.codex_output.pack(fill="both", expand=True, padx=10, pady=10)
        self.refresh_codex_output()

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
        self.agent_operator_reason_var = tk.StringVar(value=self.agent_operator_reason_var.get() if self.agent_operator_reason_var else str(self.agent_operator_settings.get("reasoning") or "medium"))
        self.agent_operator_steps_var = tk.StringVar(value=self.agent_operator_steps_var.get() if self.agent_operator_steps_var else str(self.agent_operator_settings.get("steps") or "25"))
        self.agent_operator_delay_var = tk.StringVar(value=self.agent_operator_delay_var.get() if self.agent_operator_delay_var else str(self.agent_operator_settings.get("delay") or "0.20"))
        self.agent_operator_tts_var = tk.BooleanVar(value=self.agent_operator_tts_var.get() if self.agent_operator_tts_var else bool(self.agent_operator_settings.get("tts", False)))
        self.agent_operator_voice_var = tk.StringVar(value=self.agent_operator_voice_var.get() if self.agent_operator_voice_var else str(self.agent_operator_settings.get("voice") or self.agent_operator_default_voice))
        self.agent_operator_shell_var = tk.BooleanVar(value=self.agent_operator_shell_var.get() if self.agent_operator_shell_var else bool(self.agent_operator_settings.get("shell", False)))
        self.agent_operator_codex_var = tk.BooleanVar(value=self.agent_operator_codex_var.get() if self.agent_operator_codex_var else bool(self.agent_operator_settings.get("codex_auth", False)))

        controls = self.card(self.page)
        controls.pack(fill="x", pady=(0, 12))

        row1 = tk.Frame(controls, bg=self.c("surface"))
        row1.pack(fill="x", padx=12, pady=(12, 6))
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

        row2 = tk.Frame(controls, bg=self.c("surface"))
        row2.pack(fill="x", padx=12, pady=(0, 6))
        tk.Label(row2, text="Model", bg=self.c("surface"), fg=self.c("muted"), font=self.font(9, "bold")).pack(side="left")
        self.agent_operator_model_entry = self.single_line(row2, self.agent_operator_model_var.get())
        self.agent_operator_model_entry.pack(side="left", fill="x", expand=True, padx=(8, 8))
        self.agent_operator_model_entry.bind("<FocusOut>", lambda _event: self.save_agent_operator_settings(), add="+")
        tk.Label(row2, text="Reasoning", bg=self.c("surface"), fg=self.c("muted"), font=self.font(9, "bold")).pack(side="left")
        reason_menu = tk.OptionMenu(
            row2,
            self.agent_operator_reason_var,
            "minimal",
            "low",
            "medium",
            "high",
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

        row2b = tk.Frame(controls, bg=self.c("surface"))
        row2b.pack(fill="x", padx=12, pady=(0, 6))
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
        if self.agent_operator_codex_message and not self.agent_operator_codex_available:
            tk.Label(
                row2b,
                text=self.agent_operator_codex_message,
                bg=self.c("surface"),
                fg=self.c("muted"),
                font=self.font(8),
            ).pack(side="left", padx=(8, 0))

        task_panel = tk.Frame(controls, bg=self.c("panel2"), highlightbackground="#2a3a50", highlightthickness=1, bd=0)
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
        self.agent_operator_pause_btn = self.button(actions, "Pause", self.agent_operator_toggle_pause, compact=True)
        self.agent_operator_pause_btn.pack(side="left", padx=(0, 6))
        self.agent_operator_stop_btn = self.button(actions, "Stop", self.agent_operator_stop, compact=True)
        self.agent_operator_stop_btn.pack(side="left")
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

        prompt_panel = tk.Frame(controls, bg=self.c("surface"))
        prompt_panel.pack(fill="x", padx=12, pady=(0, 12))
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
        center.place(relx=0.5, rely=0.44, anchor="center", relwidth=0.72)

        self.agent_operator_boot_label = tk.Label(
            center,
            text="OPERATOR",
            bg=self.c("panel"),
            fg=self.blend_color(self.c("panel"), self.c("text"), 0.18),
            font=self.brand_font(28),
        )
        self.agent_operator_boot_label.pack(anchor="center", pady=(0, 18))

        bar_shell = tk.Frame(
            center,
            bg=self.c("surface"),
            highlightthickness=1,
            highlightbackground=self._header_border_color(),
            height=12,
        )
        bar_shell.pack(fill="x", padx=28)
        bar_shell.pack_propagate(False)
        self.agent_operator_boot_canvas = tk.Canvas(bar_shell, bg=self.c("surface"), highlightthickness=0, bd=0, height=10)
        self.agent_operator_boot_canvas.pack(fill="both", expand=True, padx=1, pady=1)

        self.agent_operator_boot_status = tk.Label(
            center,
            text="initializing",
            bg=self.c("panel"),
            fg=self.c("muted"),
            font=self.font(9),
        )
        self.agent_operator_boot_status.pack(anchor="center", pady=(12, 0))

        if not self.agent_operator_booting:
            self.agent_operator_booting = True
            self.agent_operator_boot_progress = 0
            self._play_startup_sound()
            self._agent_operator_boot_tick()
        else:
            self._agent_operator_draw_boot()

    def _agent_operator_boot_tick(self):
        if self.active_tab != "AI Operator":
            self.agent_operator_boot_after = self.root.after(120, self._agent_operator_boot_tick)
            return

        if self.agent_operator_boot_progress < 42:
            self.agent_operator_boot_progress += 5
        elif self.agent_operator_boot_progress < 68:
            if not self.agent_operator_imported and not self.agent_operator_error:
                self._ensure_agent_operator()
            self.agent_operator_boot_progress += 4
        elif self.agent_operator_boot_progress < 92:
            if self.agent_operator_imported and not self.agent_operator_monitors:
                self.agent_operator_monitors = self._agent_operator_list_monitors()
            self.agent_operator_boot_progress += 4
        else:
            self.agent_operator_boot_progress += 3

        self.agent_operator_boot_progress = min(100, self.agent_operator_boot_progress)
        self._agent_operator_draw_boot()

        if self.agent_operator_boot_progress >= 100:
            self.agent_operator_booting = False
            self.agent_operator_booted = True
            self.agent_operator_boot_after = None
            if self.active_tab == "AI Operator":
                self.root.after(160, lambda: self.render_tab("AI Operator"))
            return

        self.agent_operator_boot_after = self.root.after(90, self._agent_operator_boot_tick)

    def _agent_operator_draw_boot(self):
        progress = self.agent_operator_boot_progress
        status = "loading agent core"
        if progress >= 42:
            status = "linking desktop controls"
        if progress >= 68:
            status = "scanning monitors"
        if progress >= 92:
            status = "ready"
        if self.agent_operator_error:
            status = "boot failed"

        if self.agent_operator_boot_status:
            try:
                self.agent_operator_boot_status.configure(
                    text=f"{status}  {progress}%",
                    fg=self.c("danger") if self.agent_operator_error else self.c("muted"),
                )
            except tk.TclError:
                pass

        if self.agent_operator_boot_label:
            try:
                fade = min(1.0, 0.18 + (progress / 100) * 0.82)
                color = self.blend_color(self.c("panel"), self.c("text"), fade)
                self.agent_operator_boot_label.configure(fg=color)
            except tk.TclError:
                pass

        if self.agent_operator_boot_canvas:
            try:
                canvas = self.agent_operator_boot_canvas
                canvas.delete("all")
                width = max(1, canvas.winfo_width())
                height = max(1, canvas.winfo_height())
                fill_w = int(width * (progress / 100))
                canvas.create_rectangle(0, 0, width, height, fill=self.c("surface"), width=0)
                canvas.create_rectangle(0, 0, fill_w, height, fill=self.c("accent"), width=0)
                pulse_x = min(width - 1, max(0, fill_w - 24))
                canvas.create_rectangle(pulse_x, 0, fill_w, height, fill=self.blend_color(self.c("accent"), self.c("text"), 0.35), width=0)
            except tk.TclError:
                pass

    def _ensure_agent_operator(self):
        if self.agent_operator_imported:
            return True
        if self.agent_operator_error:
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
            self._refresh_agent_operator_codex_auth()
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
                if key:
                    os.environ.setdefault(key, value)
        key = self.get_openai_api_key()
        if key:
            os.environ.setdefault("OPENAI_API_KEY", key)

    def _refresh_agent_operator_codex_auth(self):
        try:
            from agent.codex_backend import auth_available
            ok, message = auth_available()
        except Exception as exc:
            ok, message = False, str(exc)
        self.agent_operator_codex_available = bool(ok)
        self.agent_operator_codex_message = "" if ok else message
        if not ok and self.agent_operator_codex_var:
            try:
                self.agent_operator_codex_var.set(False)
            except tk.TclError:
                pass
        return ok, message

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

    def _agent_operator_capture_clean(self, monitor):
        self._agent_operator_ui_sync(lambda: self._agent_operator_control_hide(temporary=True), timeout=0.25)
        time.sleep(0.035)
        try:
            return self.agent_operator_raw_capture(monitor)
        finally:
            if self.agent_operator_loop and self.agent_operator_loop.is_running():
                self._agent_operator_ui_sync(
                    lambda m=monitor: self._agent_operator_control_show(m),
                    timeout=0.05,
                )

    def _agent_operator_model(self):
        if hasattr(self, "agent_operator_model_entry"):
            text = self.agent_operator_model_entry.get("1.0", "end").strip()
            if text:
                self.agent_operator_model_var.set(text)
        return self.agent_operator_model_var.get().strip() or self.agent_operator_default_model

    def save_agent_operator_settings(self):
        settings = dict(self.config.get("ai_operator") or DEFAULT_CONFIG["ai_operator"])
        if self.agent_operator_monitor_var:
            settings["monitor"] = self.agent_operator_monitor_var.get()
        if self.agent_operator_model_var:
            settings["model"] = self._agent_operator_model()
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
            settings["codex_auth"] = bool(self.agent_operator_codex_var.get())
        self.config["ai_operator"] = merge_dict(DEFAULT_CONFIG["ai_operator"], settings)
        self.agent_operator_settings = self.config["ai_operator"]
        save_config(self.config)

    def agent_operator_task_enter(self, event):
        if event and (event.state & 0x0001):
            return None
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
            return "break"
        task = self.agent_operator_task.get("1.0", "end").strip() if self.agent_operator_task else ""
        if not task:
            self.agent_operator_status_var.set("Task missing")
            return "break"
        monitor = self._agent_operator_selected_monitor()
        if not monitor:
            self.agent_operator_status_var.set("No monitor")
            return "break"
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
        self.agent_operator_clear_log()
        self.agent_operator_last_clicks.clear()
        model = self._agent_operator_model()
        reasoning = (self.agent_operator_reason_var.get() if self.agent_operator_reason_var else "medium").strip().lower() or None
        attachments = self._agent_operator_attachment_snapshot()
        user_context = self._agent_operator_user_context_text()
        context_count = len([name for name in self._agent_operator_context_files() if self._agent_operator_context_read(name).strip()])
        shell_enabled = bool(self.agent_operator_shell_var and self.agent_operator_shell_var.get())
        codex_enabled = bool(self.agent_operator_codex_var and self.agent_operator_codex_var.get())
        if codex_enabled:
            ok, message = self._refresh_agent_operator_codex_auth()
            if not ok:
                self._agent_operator_log_line("err", f"Codex auth unavailable: {message}\n")
                self.agent_operator_status_var.set("Codex auth unavailable")
                return "break"
        backend = "codex" if codex_enabled else "api"
        self.save_agent_operator_settings()
        self._agent_operator_log_line("step", f"[{self._ts()}] START\n")
        self._agent_operator_log_line(
            "dim",
            f"task={task!r} monitor={monitor.label} model={model} reasoning={reasoning} max_steps={steps} attachments={len(attachments)} prompts={context_count} shell={'on' if shell_enabled else 'off'} backend={backend}\n\n",
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
            self._agent_operator_sync_buttons()
        return "break"

    def agent_operator_stop(self):
        if self.agent_operator_stop_requested:
            if self.agent_operator_status_var:
                self.agent_operator_status_var.set("Safety stop already requested")
            return "break"
        self.agent_operator_stop_requested = True
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
        # Marker event so the phone knows a fresh run started
        self._phone_mirror_write({"type": "run_start", "ts": time.time()})

    def _phone_mirror_write(self, payload):
        root = self._phone_mirror_dir()
        line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
        try:
            with (root / "events.jsonl").open("ab") as fh:
                fh.write(line.encode("utf-8", "ignore"))
        except OSError:
            pass

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
        elif kind == "thought":
            record["thought"] = (event.get("thought") or "").strip()
            record["say"] = (event.get("say") or "").strip()
            record["message"] = (event.get("message") or "").strip()
            record["actions"] = len(event.get("actions") or [])
            record["status"] = event.get("status")
            record["elapsed_ms"] = event.get("elapsed_ms")
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
        elif kind == "ask":
            record["message"] = event.get("message", "")
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
        if kind == "step_begin":
            if self.agent_operator_step_var:
                self.agent_operator_step_var.set(f"step {event.get('n')}")
            self.agent_operator_last_clicks.clear()
            self._agent_operator_log_line("step", f"\n[{self._ts()}] Step {event.get('n')}\n")
        elif kind == "screenshot":
            self.agent_operator_current_image = event.get("image")
            self._agent_operator_redraw_preview()
        elif kind == "thought":
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
            if self.agent_operator_stop_requested:
                self._agent_operator_log_line("err", "SAFETY STOP loop exited. aiOPERATOR is no longer controlling input.\n")
                self.agent_operator_stop_requested = False
            if self.agent_operator_status_var:
                self.agent_operator_status_var.set("Stopped" if "stop" in str(event.get("message", "")).lower() else f"Done. {event.get('message', '')}")
            self._agent_operator_tts_speak("done" if ok else ("stopped" if "stop" in str(event.get("message", "")).lower() else "failed"))
            self._agent_operator_sync_buttons()
        elif kind == "ask":
            if self.agent_operator_status_var:
                self.agent_operator_status_var.set("Agent asks: " + event.get("message", ""))
            self._agent_operator_log_line("status", f"ASK: {event.get('message', '')}\n")
            self._agent_operator_sync_buttons()
        elif kind == "log":
            self._agent_operator_log_line("dim", event.get("msg", "") + "\n")

    def _agent_operator_sync_buttons(self):
        running = bool(self.agent_operator_loop and self.agent_operator_loop.is_running())
        paused = bool(self.agent_operator_loop and self.agent_operator_loop.is_paused())
        stopping = bool(self.agent_operator_stop_requested and running)
        for button, enabled in (
            (self.agent_operator_run_btn, not running),
            (self.agent_operator_pause_btn, running and not stopping),
            (self.agent_operator_stop_btn, running),
        ):
            if not button:
                continue
            try:
                button.configure(state="normal" if enabled else "disabled")
            except tk.TclError:
                pass
        if self.agent_operator_pause_btn:
            try:
                self.agent_operator_pause_btn.configure(text="Resume" if paused else "Pause")
            except tk.TclError:
                pass

    def _agent_operator_log_line(self, tag, text):
        self.agent_operator_log_buffer.append((tag, text))
        if len(self.agent_operator_log_buffer) > 500:
            self.agent_operator_log_buffer = self.agent_operator_log_buffer[-500:]
        if not self.agent_operator_log:
            return
        try:
            self.agent_operator_log.configure(state="normal")
            self.agent_operator_log.insert("end", text, tag)
            self.agent_operator_log.configure(state="disabled")
            self.agent_operator_log.see("end")
        except tk.TclError:
            pass
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
                style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW,
            )
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
        self.agent_operator_native_overlay.set_log(self._agent_operator_overlay_log_text())

    def _agent_operator_overlay_log_text(self):
        chunks = "".join(text for _tag, text in self.agent_operator_log_buffer[-120:])
        lines = []
        for line in chunks.splitlines():
            line = " ".join(line.strip().split())
            if not line:
                continue
            if len(line) > 88:
                line = line[:85] + "..."
            lines.append(line)
        return "\n".join(lines[-7:])

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

    def render_settings(self):
        self.page_title("Settings")
        scroll = ScrollFrame(self.page, self.c("panel"))
        scroll.pack(fill="both", expand=True)

        self.settings_color_rows = {}
        colors = self.card(scroll.inner)
        colors.pack(fill="x", pady=(0, 12))
        self.section(colors, "Colors")
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
            ("thinking_base", "Dot Base"),
            ("thinking_pulse", "Dot Pulse"),
        ):
            self.color_row(colors, key, label)

        visual = self.card(scroll.inner)
        visual.pack(fill="x", pady=(0, 12))
        self.section(visual, "Visual")
        self.scale_row(visual, "Opacity", 75, 100, int(float(self.c("opacity")) * 100), self.set_opacity)
        self.scale_row(visual, "Text Size", 8, 15, int(self.c("font_size")), self.set_font_size)
        self.scale_row(visual, "Corner Radius", 12, 40, int(self.c("radius")), self.set_radius)
        self.scale_row(visual, "Dot Base Opacity", 0, 100, int(self.c("thinking_base_opacity")), lambda value: self.set_theme_int("thinking_base_opacity", value))
        self.scale_row(visual, "Dot Pulse Opacity", 0, 100, int(self.c("thinking_pulse_opacity")), lambda value: self.set_theme_int("thinking_pulse_opacity", value))
        self.toggle_row(visual, "Always On Top", bool(self.c("always_on_top")), self.set_always_on_top)

        models = self.card(scroll.inner)
        models.pack(fill="x", pady=(0, 12))
        self.section(models, "Models")
        self.codex_model_settings_entry = self.setting_entry(models, "Codex Model", self.config["codex_model"])
        self.quick_model_settings_entry = self.setting_entry(models, "Quick Model", self.config["quick_codex_model"])
        row = tk.Frame(models, bg=self.c("surface"))
        row.pack(fill="x", padx=12, pady=(0, 6))
        tk.Label(row, text="Codex Thinking", bg=self.c("surface"), fg=self.c("muted"), font=self.font(9, "bold"), width=16, anchor="w").pack(side="left")
        self.settings_reasoning_var = tk.StringVar(value=self.config["codex_reasoning"])
        option = tk.OptionMenu(row, self.settings_reasoning_var, "none", "low", "medium", "high", "xhigh")
        self.style_option(option)
        option.pack(side="left")
        row2 = tk.Frame(models, bg=self.c("surface"))
        row2.pack(fill="x", padx=12, pady=(0, 12))
        tk.Label(row2, text="Quick Thinking", bg=self.c("surface"), fg=self.c("muted"), font=self.font(9, "bold"), width=16, anchor="w").pack(side="left")
        self.quick_reasoning_var = tk.StringVar(value=self.config["quick_codex_reasoning"])
        quick_option = tk.OptionMenu(row2, self.quick_reasoning_var, "none", "low", "medium", "high", "xhigh")
        self.style_option(quick_option)
        quick_option.pack(side="left")
        tk.Label(models, text="Side Chat (OpenAI)", bg=self.c("surface"), fg=self.c("muted"), font=self.font(9, "bold")).pack(
            anchor="w", padx=12, pady=(8, 0)
        )
        self.chat_model_settings_entry = self.setting_entry(models, "Chat Model", self.config.get("chat_model", "gpt-5-mini"))
        env_key = get_setting("OPENAI_API_KEY", "")
        key_hint = "(using OPENAI_API_KEY env)" if env_key and not self.config.get("openai_api_key") else ""
        self.openai_key_settings_entry = self.setting_entry(
            models,
            "OpenAI API Key",
            self.config.get("openai_api_key", "") or key_hint,
        )
        self.button(row2, "Save Models", self.save_model_settings, compact=True).pack(side="right")

        root_card = self.card(scroll.inner)
        root_card.pack(fill="x", pady=(0, 12))
        self.section(root_card, "Project Root")
        row = tk.Frame(root_card, bg=self.c("surface"))
        row.pack(fill="x", padx=12, pady=(0, 12))
        self.root_entry = self.single_line(row, str(self.project_root))
        self.root_entry.pack(side="left", fill="x", expand=True)
        self.button(row, "Save", self.save_project_root, compact=True).pack(side="right", padx=(8, 0))

        voice = self.card(scroll.inner)
        voice.pack(fill="x", pady=(0, 12))
        self.section(voice, "Voice Dictation")
        self.muted(
            voice,
            "Short Insert opens aiOS on release. Hold Insert to dictate — release to stop and type.",
        ).pack(anchor="w", padx=12, pady=(0, 8))
        voice_cfg = self.config.get("voice_dictation") or dict(DEFAULT_VOICE_DICTATION)
        hold_ms = int(voice_cfg.get("hold_ms", voice_cfg.get("double_press_ms", 280)))
        self.scale_row(
            voice,
            "Hold threshold (ms)",
            150,
            800,
            hold_ms,
            self.set_voice_hold_ms,
        )
        self.scale_row(
            voice,
            "Mic sensitivity",
            1,
            50,
            max(1, min(50, int(float(voice_cfg.get("silence_rms", 0.006)) * 10000))),
            self.set_voice_mic_sensitivity,
        )
        self.voice_model_settings_entry = self.setting_entry(
            voice, "Whisper model", voice_cfg.get("whisper_model", "small")
        )
        lang_row = tk.Frame(voice, bg=self.c("surface"))
        lang_row.pack(fill="x", padx=12, pady=(0, 8))
        tk.Label(lang_row, text="Language", bg=self.c("surface"), fg=self.c("muted"), font=self.font(9, "bold"), width=16, anchor="w").pack(
            side="left"
        )
        self.voice_language_var = tk.StringVar(value=voice_cfg.get("language", "auto"))
        lang_menu = tk.OptionMenu(
            lang_row,
            self.voice_language_var,
            *WHISPER_LANGUAGES,
        )
        self.style_option(lang_menu)
        lang_menu.pack(side="left")
        self.muted(lang_row, "Use Auto or Swedish with multilingual models (small, not small.en).").pack(
            side="left", padx=(10, 0)
        )
        row = tk.Frame(voice, bg=self.c("surface"))
        row.pack(fill="x", padx=12, pady=(0, 8))
        tk.Label(row, text="Compute", bg=self.c("surface"), fg=self.c("muted"), font=self.font(9, "bold"), width=16, anchor="w").pack(
            side="left"
        )
        self.voice_compute_var = tk.StringVar(value=voice_cfg.get("compute_type", "int8"))
        compute_menu = tk.OptionMenu(row, self.voice_compute_var, *COMPUTE_TYPES)
        self.style_option(compute_menu)
        compute_menu.pack(side="left")
        self.scale_row(
            voice,
            "Typing delay (ms)",
            0,
            50,
            int(voice_cfg.get("typing_delay_ms", 0)),
            self.set_voice_typing_delay_ms,
        )
        self.toggle_row(
            voice,
            "Discord mute while dictating",
            bool(voice_cfg.get("discord_mute_enabled")),
            self.set_voice_discord_mute_enabled,
        )
        self.voice_discord_hotkey_entry = self.setting_entry(
            voice,
            "Discord mute key",
            voice_cfg.get("discord_mute_hotkey", ""),
        )
        self.muted(
            voice,
            "Match Discord → Keybinds → Toggle Mute. Combos OK: Alt+M, Ctrl+Shift+M, F8, etc.",
        ).pack(anchor="w", padx=12, pady=(0, 8))
        voice_actions = tk.Frame(voice, bg=self.c("surface"))
        voice_actions.pack(fill="x", padx=12, pady=(0, 12))
        self.button(voice_actions, "Save Voice", self.save_voice_settings, compact=True).pack(side="left")
        self.muted(voice_actions, "Sliders save instantly. Model/compute need Save Voice.").pack(side="left", padx=(10, 0))

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

    def scale_row(self, parent, label, start, end, value, command):
        row = tk.Frame(parent, bg=self.c("surface"))
        row.pack(fill="x", padx=12, pady=(0, 10))
        tk.Label(row, text=label, bg=self.c("surface"), fg=self.c("muted"), font=self.font(9, "bold"), width=16, anchor="w").pack(side="left")
        scale = tk.Scale(
            row,
            from_=start,
            to=end,
            orient="horizontal",
            bg=self.c("surface"),
            fg=self.c("text"),
            troughcolor=self.c("panel2"),
            highlightthickness=0,
            activebackground=self.c("accent"),
            command=command,
        )
        scale.set(value)
        scale.pack(side="left", fill="x", expand=True)

    def toggle_row(self, parent, label, value, command):
        row = tk.Frame(parent, bg=self.c("surface"))
        row.pack(fill="x", padx=12, pady=(0, 10))
        tk.Label(row, text=label, bg=self.c("surface"), fg=self.c("muted"), font=self.font(9, "bold"), width=16, anchor="w").pack(side="left")
        var = tk.BooleanVar(value=value)
        check = tk.Checkbutton(
            row,
            variable=var,
            command=lambda: command(var.get()),
            bg=self.c("surface"),
            activebackground=self.c("surface"),
            fg=self.c("text"),
            selectcolor=self.c("panel2"),
        )
        check.pack(side="left")

    def setting_entry(self, parent, label, value):
        row = tk.Frame(parent, bg=self.c("surface"))
        row.pack(fill="x", padx=12, pady=(0, 8))
        tk.Label(row, text=label, bg=self.c("surface"), fg=self.c("muted"), font=self.font(9, "bold"), width=16, anchor="w").pack(side="left")
        entry = self.single_line(row, value)
        entry.pack(side="left", fill="x", expand=True)
        return entry

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
        self.render_tab("Codex")

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

        if lower in {"dashboard", "projects", "codex", "apps", "drop", "ai operator", "aioperator", "operator", "settings"}:
            tab = "AI Operator" if lower in {"ai operator", "aioperator", "operator"} else lower.title()
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
        project = Path(self.codex_project_entry.get("1.0", "end").strip())
        prompt = self.codex_prompt.get("1.0", "end").strip()
        if not prompt:
            self.append_codex_output("Enter a Codex task first.\n")
            return
        self.save_codex_settings()
        self.run_codex_task(project, prompt)

    def codex_open_desktop(self):
        project = Path(self.codex_project_entry.get("1.0", "end").strip())
        self._launch_codex_app(project)
        self.local_reply(f"Opened Codex Desktop:\n{project}")

    def save_codex_settings(self):
        if hasattr(self, "codex_model_entry"):
            self.config["codex_model"] = self.codex_model_entry.get("1.0", "end").strip() or "gpt-4.1-nano"
        if hasattr(self, "codex_reasoning_var"):
            self.config["codex_reasoning"] = self.codex_reasoning_var.get() or "none"
        save_config(self.config)
        self.subtitle.configure(text=self._status_subtitle())

    def run_codex_task(self, project_path, prompt):
        project_path = Path(project_path)
        project_path.mkdir(parents=True, exist_ok=True)
        self.ensure_project_memory(project_path)
        self.active_project = project_path
        self.render_tab("Codex")
        if self.codex_process and self.codex_process.poll() is None:
            self.append_codex_output("Codex is already running. Stop it first.\n")
            return

        self.codex_log = []
        self.refresh_codex_output()
        self.append_codex_output(f"> {prompt}\n\n")
        threading.Thread(target=self._run_codex_worker, args=(project_path, prompt), daemon=True).start()

    def _run_codex_worker(self, project_path, prompt):
        codex = find_codex()
        if not codex:
            self.root.after(0, lambda: self.append_codex_output("codex.exe was not found.\n"))
            return

        output_file = Path(tempfile.gettempdir()) / f"aios-codex-{int(time.time())}.txt"
        reasoning = self.config.get("codex_reasoning", "none")
        model = self.config.get("codex_model", "gpt-4.1-nano")
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
        chat_model = self.config.get("chat_model") or "gpt-5-mini"
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
        for role, text in self.history[-12:-1]:
            if role == "User":
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
        model = (self.config.get("chat_model") or "gpt-5-mini").strip()
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

        codex_ok, _codex_label = codex_auth_info()
        if not self.get_openai_api_key() and not codex_ok:
            self.local_reply(
                "Add your OpenAI API key in Settings, set OPENAI_API_KEY, or click Login for Codex.",
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
        threading.Thread(target=self._ask_ai, args=(prompt, sent_at, run_id), daemon=True).start()
        return "break"

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
        for role, text in self.history[-5:-1]:
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
        self.append("aiOS", text, "assistant", meta=meta)
        self.add_history("Assistant", text)

    def format_elapsed(self, elapsed):
        if elapsed < 1:
            return f"{elapsed * 1000:.0f} ms"
        return f"{elapsed:.1f} s"

    def show_thinking(self):
        if not hasattr(self, "chat"):
            return
        bg = getattr(self, "_assistant_bg", self.c("surface"))
        try:
            if self.thinking_canvas is None or not self.thinking_canvas.winfo_exists():
                self.thinking_canvas = tk.Canvas(
                    self.chat, width=22, height=8, bg=bg, highlightthickness=0, bd=0
                )
            self.chat.configure(state="normal")
            self.chat.mark_set("thinking_start", "end-1c")
            self.chat.mark_gravity("thinking_start", "left")
            self.chat.insert("end", "aiOS\n", "assistant_label")
            self.chat.window_create("end", window=self.thinking_canvas, padx=12, pady=2)
            self.chat.insert("end", "\n\n", "assistant")
        except tk.TclError:
            pass
        finally:
            try:
                self.chat.configure(state="disabled")
                self.chat.see("end")
            except tk.TclError:
                pass
        self.animate_thinking()

    def set_thinking_status(self, text):
        self.thinking_status_text = text or "thinking"

    def hide_thinking(self):
        if self.thinking_after:
            try:
                self.root.after_cancel(self.thinking_after)
            except tk.TclError:
                pass
        self.thinking_after = None
        if hasattr(self, "chat"):
            try:
                self.chat.configure(state="normal")
                if "thinking_start" in self.chat.mark_names():
                    self.chat.delete("thinking_start", "end")
                    self.chat.mark_unset("thinking_start")
            except tk.TclError:
                pass
            finally:
                try:
                    self.chat.configure(state="disabled")
                except tk.TclError:
                    pass
        self.thinking_canvas = None

    def animate_thinking(self):
        if not self.busy or not hasattr(self, "thinking_canvas"):
            return
        self.thinking_canvas.delete("all")
        canvas_bg = getattr(self, "_assistant_bg", self.c("surface"))
        base = self.blend_color(canvas_bg, self.c("thinking_base"), int(self.c("thinking_base_opacity")) / 100)
        pulse = self.blend_color(canvas_bg, self.c("thinking_pulse"), int(self.c("thinking_pulse_opacity")) / 100)
        step = self.thinking_step
        for col in range(3):
            phase = (step - col * 2) % 12
            strength = max(0, 1 - abs(phase - 3) / 3)
            radius = 1 + strength * 1.2
            x = 4 + col * 7
            y = 4
            color = self.blend_color(base, pulse, strength)
            self.thinking_canvas.create_oval(x - radius, y - radius, x + radius, y + radius, fill=color, outline="")
        self.thinking_step = (self.thinking_step + 1) % 12
        self.thinking_after = self.root.after(90, self.animate_thinking)

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
        model_label = self.config.get("chat_model") or "gpt-5-mini"
        key_ok = bool(self.get_openai_api_key())
        codex_ok, codex_label = codex_auth_info()
        if key_ok:
            auth_label = "API key ready"
        elif codex_ok:
            auth_label = f"Codex {codex_label}"
        else:
            auth_label = "No auth"
        meta = f"{model_label} · {auth_label}"
        try:
            self.chat_account_label.configure(
                text=meta,
                fg=self.c("success") if (key_ok or codex_ok) else self.c("danger"),
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
        if hasattr(self, "send_button"):
            self.send_button.configure(state="normal")
        self.render_chat_history()

    def append_system_message(self, text):
        self.append("Codex", text, "muted")

    def append_stream_line(self, text):
        if not hasattr(self, "chat"):
            return
        stripped = text.rstrip("\n")
        if not stripped:
            return
        tag = "command" if stripped.startswith(("exec", "$", ">")) else "muted"
        self.chat.configure(state="normal")
        self.chat.insert("end", stripped + "\n", tag)
        self.chat.configure(state="disabled")
        self.chat.see("end")

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

    def add_history(self, role, text):
        self.history.append((role, text))
        self.history = self.history[-40:]
        self.config["chat_history"] = [{"role": item[0], "text": item[1]} for item in self.history]
        save_config(self.config)

    def load_chat_history(self):
        history = []
        for item in self.config.get("chat_history", [])[-40:]:
            if isinstance(item, dict) and item.get("role") and item.get("text"):
                role = str(item["role"])
                text = str(item["text"])
                if role.casefold() != "user":
                    text = self.sanitize_codex_output(text)
                if text and len(text) < 5000:
                    history.append((role, text))
        self.config["chat_history"] = [{"role": item[0], "text": item[1]} for item in history]
        save_config(self.config)
        return history

    def render_chat_history(self):
        if not hasattr(self, "chat"):
            return
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        self.chat.configure(state="disabled")
        for role, text in self.history[-16:]:
            is_user = role.casefold() == "user"
            self.append("You" if is_user else "aiOS", text, "user" if is_user else "assistant", trim=False)
        self._trim_chat_display()

    def append(self, label, text, tag, meta=None, trim=True):
        if not hasattr(self, "chat"):
            return
        label_tag = f"{tag}_label" if f"{tag}_label" in self.chat.tag_names() else tag
        self.chat.configure(state="normal")
        self.chat.insert("end", f"{label}\n", label_tag)
        self.insert_formatted_text(text.strip(), tag)
        if meta and tag == "assistant":
            self.chat.insert("end", meta + "\n", "assistant_meta")
        elif meta:
            self.chat.insert("end", "  " + meta + "\n", "muted")
        self.chat.insert("end", "\n")
        self.chat.configure(state="disabled")
        self.chat.see("end")
        if trim:
            self._trim_chat_display()

    def insert_formatted_text(self, text, default_tag):
        in_code = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("```"):
                in_code = not in_code
                continue
            if in_code:
                tag = "code"
            elif stripped.startswith("### "):
                line = stripped[4:]
                tag = "heading"
            elif stripped.startswith("## "):
                line = stripped[3:]
                tag = "heading"
            elif stripped.startswith("# "):
                line = stripped[2:]
                tag = "heading"
            elif line.startswith("+") and not line.startswith("+++"):
                tag = "diff_add"
            elif line.startswith("-") and not line.startswith("---"):
                tag = "diff_del"
            elif line.startswith((">", "$", "codex ", "git ", "python ", "npm ", "pnpm ")):
                tag = "command"
            else:
                tag = default_tag
            self.chat.insert("end", line + "\n", tag)

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

    def set_voice_mic_sensitivity(self, value):
        self._voice_cfg()["silence_rms"] = round(max(1, min(50, int(float(value)))) / 10000.0, 4)
        self._save_voice_cfg()

    def set_voice_typing_delay_ms(self, value):
        self._voice_cfg()["typing_delay_ms"] = int(float(value))
        self._save_voice_cfg()

    def set_voice_discord_mute_enabled(self, value):
        self._voice_cfg()["discord_mute_enabled"] = bool(value)
        self._save_voice_cfg()

    def save_voice_settings(self):
        voice = self._voice_cfg()
        voice["whisper_model"] = self.voice_model_settings_entry.get("1.0", "end").strip() or "small"
        voice["language"] = self.voice_language_var.get() or "auto"
        voice["compute_type"] = self.voice_compute_var.get() or "int8"
        voice["discord_mute_hotkey"] = self.voice_discord_hotkey_entry.get("1.0", "end").strip()
        self.config["voice_dictation"] = merge_voice_dictation(voice)
        save_config(self.config)
        merged = self.config["voice_dictation"]
        self.local_reply(
            f"Voice settings saved ({LANGUAGE_LABELS.get(merged['language'], merged['language'])}, "
            f"{merged['whisper_model']}). "
            "Restart voice dictation to load a new model. Discord mute applies after reloading AHK."
        )

    def save_project_root(self):
        raw = self.root_entry.get("1.0", "end").strip()
        if not raw:
            return
        self.project_root = Path(raw)
        self.project_root.mkdir(parents=True, exist_ok=True)
        self.config["project_root"] = str(self.project_root)
        save_config(self.config)
        self.refresh_all()

    def save_model_settings(self):
        self.config["codex_model"] = self.codex_model_settings_entry.get("1.0", "end").strip() or "gpt-4.1-nano"
        self.config["codex_reasoning"] = self.settings_reasoning_var.get() or "none"
        self.config["quick_codex_model"] = self.quick_model_settings_entry.get("1.0", "end").strip() or "gpt-5-mini"
        self.config["quick_codex_reasoning"] = self.quick_reasoning_var.get() or "none"
        self.config["chat_model"] = self.chat_model_settings_entry.get("1.0", "end").strip() or "gpt-5-mini"
        raw_key = self.openai_key_settings_entry.get("1.0", "end").strip()
        if raw_key and not raw_key.startswith("(using OPENAI_API_KEY"):
            self.config["openai_api_key"] = raw_key
        save_config(self.config)
        self.refresh_chat_account()
        self.rebuild_shell()

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

    def _chat_watchdog(self):
        if self.busy and self.chat_busy_since:
            if time.perf_counter() - self.chat_busy_since > 300:
                self._recover_stuck_chat("The assistant took too long and was reset. Try again or click Reset.")
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

    def _trim_chat_display(self, max_lines=320):
        if not hasattr(self, "chat"):
            return
        try:
            line_count = int(self.chat.index("end-1c").split(".")[0])
        except (tk.TclError, ValueError):
            return
        if line_count <= max_lines:
            return
        try:
            self.chat.configure(state="normal")
            self.chat.delete("1.0", f"{line_count - max_lines}.0")
            self.chat.configure(state="disabled")
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
        self._play_startup_sound()
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

    def _play_startup_sound(self):
        self._play_wav_async(STARTUP_SOUND_PATH)

    def _play_operator_sound(self):
        now = time.perf_counter()
        if now - self._operator_sound_last_at < 1.5:
            return
        self._operator_sound_last_at = now
        self._play_wav_async(OPERATOR_SOUND_PATH)

    def _play_wav_async(self, path):
        if not path.exists() or not sys.platform.startswith("win"):
            return
        try:
            escaped = str(path).replace("'", "''")
            command = (
                f"$p = New-Object System.Media.SoundPlayer -ArgumentList '{escaped}'; "
                "$p.PlaySync()"
            )
            encoded = base64.b64encode(command.encode("utf-16le")).decode("ascii")
            subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
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
                        if sum(len(part) for part in chunks) > 65536:
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
        if not text and action not in {"phone_start", "phone_stop", "reload_operator_settings"}:
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

    def _remote_submit_chat(self, text):
        self._phone_control_show("AIOS")
        self.show()
        if hasattr(self, "input"):
            self.input.delete("1.0", "end")
            self.input.insert("1.0", text)
            self.send()

    def _remote_submit_operator(self, text, options=None):
        self._phone_control_show("OPERATOR")
        self.show()
        self.render_tab("AI Operator")
        self._remote_submit_operator_attempt(text, options, attempts_left=80)

    def _remote_submit_operator_attempt(self, text, options, attempts_left):
        ready = False
        try:
            ready = bool(self._ensure_agent_operator()) and bool(getattr(self, "agent_operator_task", None))
        except Exception:
            ready = False
        if not ready and attempts_left > 0:
            self.root.after(150, lambda: self._remote_submit_operator_attempt(text, options, attempts_left - 1))
            return
        if not ready:
            return
        try:
            if options:
                self._remote_apply_operator_options(options, run=False)
            self.agent_operator_task.delete("1.0", "end")
            self.agent_operator_task.insert("1.0", text)
            self.agent_operator_run()
        except Exception:
            pass

    def _remote_operator_stop(self):
        try:
            if self.agent_operator_loop and self.agent_operator_loop.is_running():
                self.agent_operator_stop()
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
            "reasoning": ("agent_operator_reason_var", str),
            "steps": ("agent_operator_steps_var", str),
            "delay": ("agent_operator_delay_var", str),
            "tts": ("agent_operator_tts_var", bool),
            "voice": ("agent_operator_voice_var", str),
            "shell": ("agent_operator_shell_var", bool),
            "codex_auth": ("agent_operator_codex_var", bool),
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
            var = getattr(self, attr, None)
            if var is not None:
                try:
                    var.set(value)
                except Exception:
                    pass
        self.config["ai_operator"] = merge_dict(DEFAULT_CONFIG["ai_operator"], settings)
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
    set_windows_app_id()
    parser = argparse.ArgumentParser()
    parser.add_argument("--toggle", action="store_true")
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--hide", action="store_true")
    parser.add_argument("--quit", action="store_true")
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

    app = HelperOverlay()
    app.run()


if __name__ == "__main__":
    main()
