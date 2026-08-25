"""Screen-recording engine for the WebView2 aiOS Quick Tools menu.

The deprecated Tk application used to own both the recorder process and its
picker widgets.  The WebView2 UI now owns the picker; this module only exposes
validated monitor/window targets and manages the FFmpeg process lifecycle.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any


RECORDINGS_FOLDER_NAME = "aiOS recordings"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_LOCK = threading.RLock()
_PROCESS: subprocess.Popen[bytes] | None = None
_PATH: Path | None = None
_STARTED_AT = 0.0
_LABEL = ""
_LAST_MESSAGE = "Ready"
_LAST_OK = True


def recordings_dir() -> Path:
    path = Path.home() / "Videos" / RECORDINGS_FOLDER_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _unique_recording_path() -> Path:
    folder = recordings_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = folder / f"aios-recording-{stamp}.mp4"
    if not base.exists():
        return base
    for index in range(2, 1000):
        candidate = folder / f"aios-recording-{stamp}-{index}.mp4"
        if not candidate.exists():
            return candidate
    raise FileExistsError("Could not allocate a recording filename.")


def ffmpeg_path() -> str:
    candidates = [
        shutil.which("ffmpeg"),
        r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return ""


def virtual_screen_bounds() -> dict[str, int]:
    if os.name == "nt":
        user32 = ctypes.windll.user32
        return {
            "left": int(user32.GetSystemMetrics(76)),
            "top": int(user32.GetSystemMetrics(77)),
            "width": int(user32.GetSystemMetrics(78)),
            "height": int(user32.GetSystemMetrics(79)),
        }
    return {"left": 0, "top": 0, "width": 1920, "height": 1080}


def _contains_origin(bounds: dict[str, int]) -> bool:
    return (
        bounds["left"] <= 0 < bounds["left"] + bounds["width"]
        and bounds["top"] <= 0 < bounds["top"] + bounds["height"]
    )


def list_monitors() -> list[dict[str, Any]]:
    if os.name != "nt":
        bounds = virtual_screen_bounds()
        return [{**bounds, "id": "0", "label": f"Monitor 1 {bounds['width']}x{bounds['height']}"}]

    monitors: list[dict[str, Any]] = []

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(RECT),
        wintypes.LPARAM,
    )

    def callback(_monitor: int, _dc: int, rect_pointer: Any, _data: int) -> bool:
        rect = rect_pointer.contents
        monitors.append(
            {
                "left": int(rect.left),
                "top": int(rect.top),
                "width": int(rect.right - rect.left),
                "height": int(rect.bottom - rect.top),
            }
        )
        return True

    ctypes.windll.user32.EnumDisplayMonitors(0, 0, callback_type(callback), 0)
    monitors.sort(key=lambda item: (not _contains_origin(item), item["left"], item["top"]))
    for index, monitor in enumerate(monitors):
        primary = " Primary" if _contains_origin(monitor) else ""
        monitor["id"] = str(index)
        monitor["label"] = f"Monitor {index + 1}{primary} {monitor['width']}x{monitor['height']}"
    return monitors


def _window_bounds(hwnd: int) -> dict[str, int] | None:
    if os.name != "nt":
        return None
    rect = wintypes.RECT()
    got_rect = False
    try:
        got_rect = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd), 9, ctypes.byref(rect), ctypes.sizeof(rect)
        ) == 0
    except (AttributeError, OSError):
        pass
    if not got_rect and not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)
    if width <= 0 or height <= 0:
        return None
    return {"left": int(rect.left), "top": int(rect.top), "width": width, "height": height}


def list_windows() -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    user32 = ctypes.windll.user32
    windows: list[dict[str, Any]] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd: int, _data: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value.strip()
        if not title or title == "aiOS":
            return True
        bounds = _window_bounds(int(hwnd))
        if not bounds or bounds["width"] < 120 or bounds["height"] < 80:
            return True
        windows.append({"id": str(int(hwnd)), "title": title, "bounds": bounds})
        return True

    user32.EnumWindows(callback_type(callback), 0)
    windows.sort(key=lambda item: item["title"].casefold())
    used: dict[str, int] = {}
    for item in windows[:80]:
        title = item["title"]
        compact = title if len(title) <= 58 else title[:55] + "..."
        count = used.get(compact, 0) + 1
        used[compact] = count
        item["label"] = compact if count == 1 else f"{compact} ({count})"
    return windows[:80]


def _validated_area(raw: Any) -> dict[str, int] | None:
    if not isinstance(raw, dict):
        return None
    try:
        area = {key: int(raw[key]) for key in ("left", "top", "width", "height")}
    except (KeyError, TypeError, ValueError):
        return None
    if area["width"] < 24 or area["height"] < 24:
        return None
    virtual = virtual_screen_bounds()
    right = min(area["left"] + area["width"], virtual["left"] + virtual["width"])
    bottom = min(area["top"] + area["height"], virtual["top"] + virtual["height"])
    area["left"] = max(area["left"], virtual["left"])
    area["top"] = max(area["top"], virtual["top"])
    area["width"] = right - area["left"]
    area["height"] = bottom - area["top"]
    return area if area["width"] >= 24 and area["height"] >= 24 else None


def _finalize_locked(process: subprocess.Popen[bytes], *, stopped: bool) -> None:
    global _PROCESS, _PATH, _STARTED_AT, _LABEL, _LAST_MESSAGE, _LAST_OK
    if process is not _PROCESS:
        return
    stderr = ""
    try:
        if process.stderr:
            stderr = process.stderr.read().decode("utf-8", "replace").strip()
    except OSError:
        pass
    path = _PATH
    _PROCESS = None
    _PATH = None
    _STARTED_AT = 0.0
    _LABEL = ""
    if path and path.exists() and path.stat().st_size > 0:
        _LAST_MESSAGE = f"Saved {path.name}"
        _LAST_OK = True
        return
    if path and path.exists():
        try:
            path.unlink()
        except OSError:
            pass
    _LAST_MESSAGE = "Recording stopped" if stopped else "Recording failed"
    if stderr:
        _LAST_MESSAGE = stderr.splitlines()[-1][:140]
    _LAST_OK = False


def _reap_locked() -> None:
    if _PROCESS is not None and _PROCESS.poll() is not None:
        _finalize_locked(_PROCESS, stopped=False)


def state(*, include_options: bool = False) -> dict[str, Any]:
    with _LOCK:
        _reap_locked()
        active = _PROCESS is not None
        payload: dict[str, Any] = {
            "ok": True,
            "active": active,
            "available": bool(ffmpeg_path()),
            "label": _LABEL,
            "elapsed": max(0, int(time.perf_counter() - _STARTED_AT)) if active else 0,
            "path": str(_PATH) if _PATH else "",
            "message": f"Recording {_LABEL}" if active else _LAST_MESSAGE,
            "message_ok": True if active else _LAST_OK,
        }
    if include_options:
        payload["monitors"] = list_monitors()
        payload["windows"] = list_windows()
    return payload


def start(data: dict[str, Any]) -> dict[str, Any]:
    global _PROCESS, _PATH, _STARTED_AT, _LABEL, _LAST_MESSAGE, _LAST_OK
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        return {"ok": False, "error": "FFmpeg not found."}

    source = str(data.get("source") or "").lower()
    bounds: dict[str, int] | None = None
    label = ""
    if source == "area":
        bounds = _validated_area(data.get("bounds"))
        label = "selected area"
    elif source == "monitor":
        monitor = next((item for item in list_monitors() if item["id"] == str(data.get("id"))), None)
        if monitor:
            bounds = {key: int(monitor[key]) for key in ("left", "top", "width", "height")}
            label = str(monitor["label"])
    elif source == "window":
        window = next((item for item in list_windows() if item["id"] == str(data.get("id"))), None)
        if window:
            bounds = _window_bounds(int(window["id"]))
            label = str(window["title"])
    if not bounds:
        return {"ok": False, "error": "That recording target is no longer available."}

    width = max(2, int(bounds["width"]) // 2 * 2)
    height = max(2, int(bounds["height"]) // 2 * 2)
    target = _unique_recording_path()
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
        str(int(bounds["left"])),
        "-offset_y",
        str(int(bounds["top"])),
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

    with _LOCK:
        _reap_locked()
        if _PROCESS is not None:
            return {"ok": False, "error": "A screen recording is already running."}
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                cwd=str(recordings_dir()),
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        _PROCESS = process
        _PATH = target
        _STARTED_AT = time.perf_counter()
        _LABEL = label
        _LAST_MESSAGE = f"Recording {label}"
        _LAST_OK = True
    return state()


def stop() -> dict[str, Any]:
    with _LOCK:
        _reap_locked()
        process = _PROCESS
    if process is None:
        return state()
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
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
    with _LOCK:
        _finalize_locked(process, stopped=True)
    return state()


def shutdown() -> None:
    stop()
