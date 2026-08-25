"""Entry point for the WebView2-backed aiOS shell."""

from __future__ import annotations

import argparse
import ctypes
import os
import re
import sys
import threading
from pathlib import Path

# WebView2 locks its user-data folder for the lifetime of the process. Sharing
# the default one means a restart races its own dying instance ("the requested
# resource is in use") and aiOS fights any other pywebview app on the machine.
# Must be set before webview is imported.
_PROFILE_ROOT = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "aiOS"


def _profile_in_use(profile: Path) -> bool:
    """Ask Windows for exclusive access to WebView2's real lock file.

    Python's normal ``open()`` uses permissive Windows sharing flags and can
    open this file even while Edge owns it. That false negative was the reason
    some restarts froze with a blank window.
    """
    lock = profile / "EBWebView" / "lockfile"
    if not lock.exists() or os.name != "nt":
        return False
    GENERIC_READ = 0x80000000
    OPEN_EXISTING = 3
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(lock), GENERIC_READ, 0, None, OPEN_EXISTING, 0, None,
    )
    if handle == INVALID_HANDLE_VALUE:
        return True
    close_handle = ctypes.windll.kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    close_handle(handle)
    return False


def _profile_dir() -> Path:
    """Pick a WebView2 profile we can actually open.

    A hard kill leaves the lockfile held by an orphaned msedgewebview2.exe, and
    WebView2 then refuses to start at all ("the requested resource is in use").
    Falling back to a sibling profile means a crash costs you some cached state,
    not a shell that will not open.
    """
    primary = _PROFILE_ROOT / "webview2"
    fallback = _PROFILE_ROOT / "webview2-recover"
    for candidate in (primary, fallback):
        candidate.mkdir(parents=True, exist_ok=True)
        if not _profile_in_use(candidate):
            return candidate
    # Two overlapping crashed instances are rare, but the third launch must
    # still open. A PID-scoped profile cannot collide and is only used for this
    # recovery launch.
    emergency = _PROFILE_ROOT / f"webview2-recover-{os.getpid()}"
    emergency.mkdir(parents=True, exist_ok=True)
    return emergency


os.environ.setdefault("WEBVIEW2_USER_DATA_FOLDER", str(_profile_dir()))

import webview  # noqa: E402 - must follow the env var above

from .api import NativeApi, QuickToolsApi
from .pad_gesture import PadGesture
from .server import start_server


def _panel_colour() -> str:
    """Paint the frame in the user's own panel colour so there is no flash."""
    try:
        from helper_overlay import load_config

        value = str((load_config().get("theme") or {}).get("panel") or "")
        return value if value.startswith("#") and len(value) == 7 else "#101722"
    except Exception:
        return "#101722"


def _monitor_work_areas() -> list[tuple[int, int, int, int]]:
    """Return current monitor work areas with the primary display first."""
    if os.name != "nt":
        return []
    try:
        import ctypes
        import ctypes.wintypes

        class MonitorInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.wintypes.DWORD),
                ("rcMonitor", ctypes.wintypes.RECT),
                ("rcWork", ctypes.wintypes.RECT),
                ("dwFlags", ctypes.wintypes.DWORD),
            ]

        found: list[tuple[bool, tuple[int, int, int, int]]] = []
        callback_type = ctypes.WINFUNCTYPE(
            ctypes.c_int,
            ctypes.wintypes.HMONITOR,
            ctypes.wintypes.HDC,
            ctypes.POINTER(ctypes.wintypes.RECT),
            ctypes.wintypes.LPARAM,
        )

        @callback_type
        def collect(monitor, _dc, _rect, _data):
            info = MonitorInfo()
            info.cbSize = ctypes.sizeof(info)
            if ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                work = info.rcWork
                found.append(
                    (
                        bool(info.dwFlags & 1),
                        (work.left, work.top, work.right, work.bottom),
                    )
                )
            return 1

        ctypes.windll.user32.EnumDisplayMonitors(0, None, collect, 0)
        found.sort(key=lambda item: not item[0])
        return [area for _primary, area in found]
    except Exception:
        return []


def _visible_window_position(
    width: int,
    height: int,
    x: int | None,
    y: int | None,
    work_areas: list[tuple[int, int, int, int]] | None = None,
) -> tuple[int | None, int | None]:
    """Keep valid positions, or move an off-screen window without resizing it."""
    if x is None or y is None:
        return x, y
    areas = _monitor_work_areas() if work_areas is None else work_areas
    if not areas:
        return x, y
    for left, top, right, bottom in areas:
        visible_width = max(0, min(x + width, right) - max(x, left))
        visible_height = max(0, min(y + height, bottom) - max(y, top))
        if visible_width >= 96 and visible_height >= 32:
            return x, y
    # The saved monitor disappeared or Windows supplied its -32000 sentinel.
    # Relocate onto the primary work area, preserving width and height exactly.
    left, top, _right, _bottom = areas[0]
    return left, top


def _window_geometry() -> tuple[int, int, int | None, int | None]:
    """Restore aiOS's own size and position without sharing Tk's geometry."""
    try:
        from helper_overlay import DEFAULT_CONFIG, load_config

        config = load_config()
        raw = str(config.get("aios_window") or config.get("window") or DEFAULT_CONFIG["window"])
        match = re.fullmatch(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)", raw)
        if match:
            width, height, x, y = map(int, match.groups())
            width, height = max(860, width), max(560, height)
            x, y = _visible_window_position(width, height, x, y)
            return width, height, x, y
        size = raw.split("+", 1)[0]
        width, _, height = size.partition("x")
        return max(860, int(width)), max(560, int(height)), None, None
    except Exception:
        return 1180, 760, None, None


_MUTEX_HANDLE = None
# Deliberately NOT helper_overlay's APP_MUTEX_NAME: while the port is in
# progress the Tk build and this one have to run side by side so you can
# compare them. Switch this to the shared name once the Tk build is retired.
_MUTEX_NAME = "Local\\aiOS.Desktop.Shell.WebView2.Singleton"


def _claim_single_instance() -> bool:
    """One WebView2 shell at a time -- two would fight over the same profile."""
    global _MUTEX_HANDLE
    if os.name != "nt":
        return True
    try:
        import ctypes

        handle = ctypes.windll.kernel32.CreateMutexW(None, True, _MUTEX_NAME)
        if not handle:
            return True
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            ctypes.windll.kernel32.CloseHandle(handle)
            return False
        _MUTEX_HANDLE = handle
        return True
    except Exception:
        return True


def run(*, debug: bool = False, quick_tools: bool = False) -> None:
    if not _claim_single_instance():
        # A second launch is a hotkey/show request, not an error — nudge the
        # live shell and exit quietly (no terminal message for AHK callers).
        from .control import send_command

        command = "quick_tools" if quick_tools else "toggle"
        if not send_command(command) and not send_command("show"):
            # Mutex is held but nothing is listening — recover from a hung shell.
            try:
                import aios_updater

                extra = ["--fast-start"]
                if quick_tools:
                    extra.append("--quick-tools")
                aios_updater.spawn_relaunch(parent_pid=0, extra_args=extra)
            except Exception:
                pass
        return
    server = start_server()
    api = NativeApi()
    qt_api = QuickToolsApi()
    width, height, x, y = _window_geometry()
    panel = _panel_colour()

    window = webview.create_window(
        "aiOS",
        server.url,
        js_api=api,
        width=width,
        height=height,
        x=x,
        y=y,
        min_size=(860, 560),
        # Use native Windows chrome so aiOS behaves like an ordinary app: it is
        # represented in Alt+Tab/taskbar and supports drag-to-edge Snap layouts.
        frameless=False,
        easy_drag=False,
        resizable=True,
        transparent=False,
        on_top=False,
        hidden=bool(quick_tools),
        background_color=panel,
    )
    api.attach(window)
    if quick_tools:
        api._hidden = True
    window.events.shown += api.style_window

    geometry = {"width": width, "height": height, "x": x, "y": y}
    geometry_lock = threading.Lock()
    geometry_timer: list[threading.Timer | None] = [None]

    def persist_geometry() -> None:
        if not api.is_geometry_stable():
            return
        with geometry_lock:
            state = dict(geometry)
            geometry_timer[0] = None
        if state["x"] is None or state["y"] is None:
            return
        try:
            from helper_overlay import load_config, save_config

            config = load_config()
            config["aios_window"] = (
                f'{int(state["width"])}x{int(state["height"])}'
                f'{int(state["x"]):+d}{int(state["y"]):+d}'
            )
            save_config(config)
        except Exception:
            pass

    def schedule_geometry_save() -> None:
        with geometry_lock:
            if geometry_timer[0] is not None:
                geometry_timer[0].cancel()
            timer = threading.Timer(0.4, persist_geometry)
            timer.daemon = True
            geometry_timer[0] = timer
            timer.start()

    def on_window_moved(new_x: int, new_y: int) -> None:
        if int(new_x) <= -30000 or int(new_y) <= -30000 or not api.is_geometry_stable():
            return
        with geometry_lock:
            geometry.update(x=int(new_x), y=int(new_y))
        schedule_geometry_save()

    def on_window_resized(new_width: int, new_height: int) -> None:
        if not api.is_geometry_stable():
            return
        with geometry_lock:
            geometry.update(width=max(860, int(new_width)), height=max(560, int(new_height)))
        schedule_geometry_save()

    window.events.moved += on_window_moved
    window.events.resized += on_window_resized
    window.events.closing += persist_geometry

    qt_window = webview.create_window(
        "aiOS Quick Tools",
        server.quick_tools_url,
        js_api=qt_api,
        width=360,
        height=400,
        min_size=(320, 360),
        frameless=True,
        easy_drag=True,
        resizable=False,
        on_top=True,
        hidden=not bool(quick_tools),
        focus=bool(quick_tools),
        background_color=panel,
    )
    qt_api.attach(qt_window, api)
    pad = PadGesture(api, qt_api)

    def on_qt_shown() -> None:
        qt_api._visible = True
        qt_api.style_window()
        if quick_tools:
            qt_api._center()

    qt_window.events.shown += on_qt_shown

    # Hotkey control sockets. WEBVIEW_PORT is ours even when the legacy Tk
    # helper still owns 48736; we also claim 48736 when it is free.
    from .control import PORT, WEBVIEW_PORT, ControlServer

    def on_control(command: str) -> None:
        if command == "pad_down":
            pad.down()
        elif command == "pad_up":
            pad.up()
        elif command == "pad_cancel":
            pad.cancel()
        elif command in {"pad_tap", "toggle"}:
            # Short / one-shot → full aiOS. Hold uses pad_down/pad_up → Quick Tools.
            pad.toggle_main()
        elif command in {"quick_tools", "quicktools"}:
            pad.open_quick_tools()
        elif command in {"quick_tools_hide", "quicktools_hide"}:
            qt_api.hide()
        elif command.startswith("qt_key:"):
            try:
                number = int(command.split(":", 1)[1])
            except ValueError:
                return
            qt_api.trigger_key(number)
        elif command.startswith("qt:"):
            qt_api.trigger_tool(command.split(":", 1)[1])
        elif command == "show":
            qt_api.hide()
            api.show()
        elif command == "hide":
            qt_api.hide()
            api.hide()
        elif command in {"quit", "exit"}:
            try:
                if api._window is not None:
                    api._window.destroy()
            except Exception:
                os._exit(0)
        elif command == "restart":
            api.restart()

    controls = [
        ControlServer(on_control, port=WEBVIEW_PORT),
        ControlServer(on_control, port=PORT),
    ]
    for control in controls:
        control.start()

    try:
        # EdgeChromium is WebView2. Naming it explicitly avoids falling back to
        # the legacy MSHTML renderer on machines where both are present.
        webview.start(gui="edgechromium", debug=debug, private_mode=False)
    finally:
        try:
            from . import screen_recording

            screen_recording.shutdown()
        except Exception:
            pass
        for control in controls:
            control.stop()
        server.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aios-ui", description="aiOS desktop shell (WebView2)")
    parser.add_argument("--debug", action="store_true", help="open devtools and log to stdout")
    parser.add_argument(
        "--quick-tools",
        action="store_true",
        help="open the compact Quick Tools palette (macropad hold)",
    )
    args = parser.parse_args(argv)
    run(debug=args.debug, quick_tools=args.quick_tools)
    return 0


if __name__ == "__main__":
    sys.exit(main())
