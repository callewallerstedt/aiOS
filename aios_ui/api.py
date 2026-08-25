"""Native operations the web layer cannot do for itself.

Deliberately small. Anything that is really backend work goes through the HTTP
server instead; this is only for the window and OS-level dialogs.
"""

from __future__ import annotations

import ctypes.wintypes
import os
import threading
import time
from pathlib import Path

import webview


class NativeApi:
    def __init__(self) -> None:
        self._window: webview.Window | None = None
        self._resize_origin: tuple[int, int] | None = None
        self._hidden = False

    def attach(self, window: webview.Window) -> None:
        self._window = window
        self._hidden = False

    def _native_handle(self) -> int:
        if os.name != "nt" or not self._window:
            return 0
        try:
            return int(self._window.native.Handle)
        except Exception:
            return 0

    def is_geometry_stable(self) -> bool:
        """True only while Windows exposes a real, persistable window rect."""
        handle = self._native_handle()
        if not handle:
            return True
        try:
            user32 = ctypes.windll.user32
            rect = ctypes.wintypes.RECT()
            if not user32.IsWindowVisible(handle) or user32.IsIconic(handle):
                return False
            if not user32.GetWindowRect(handle, ctypes.byref(rect)):
                return False
            # WinForms parks hidden windows at this Windows sentinel. It is not
            # a user move and must never become the next startup position.
            return rect.left > -30000 and rect.top > -30000
        except Exception:
            return True

    def style_window(self) -> None:
        """Round the frameless window's corners using DWM.

        A frameless window is a bare rectangle, which is what produced the hard
        square edge. DWMWA_WINDOW_CORNER_PREFERENCE = ROUND gives the same
        antialiased corner Windows 11 puts on every other app, with no
        transparency layer to go wrong.
        """
        if os.name != "nt" or not self._window:
            return
        try:
            import ctypes

            handle = int(self._window.native.Handle)
            # 33 = DWMWA_WINDOW_CORNER_PREFERENCE, 2 = DWMWCP_ROUND
            preference = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.wintypes.HWND(handle), 33, ctypes.byref(preference), ctypes.sizeof(preference)
            )
        except Exception:
            pass  # pre-22H2 Windows simply keeps square corners

    # ---------------------------------------------------------- window chrome

    def hide(self) -> None:
        if not self._window:
            return
        handle = self._native_handle()
        if handle:
            try:
                ctypes.windll.user32.ShowWindowAsync(handle, 0)  # SW_HIDE
                self._hidden = True
                return
            except Exception:
                pass
        self._window.hide()
        self._hidden = True

    def show(self) -> None:
        if not self._window:
            return
        handle = self._native_handle()
        if handle:
            try:
                user32 = ctypes.windll.user32
                # Restore only an actually minimized window. For a normal or
                # hidden window, SW_SHOW plus NOMOVE|NOSIZE preserves its exact
                # rectangle while bringing it forward.
                user32.ShowWindowAsync(handle, 9 if user32.IsIconic(handle) else 5)
                user32.SetWindowPos(
                    handle,
                    0,  # HWND_TOP, never TOPMOST
                    0,
                    0,
                    0,
                    0,
                    0x0001 | 0x0002 | 0x0040,  # NOSIZE | NOMOVE | SHOWWINDOW
                )
                user32.BringWindowToTop(handle)
                user32.SetForegroundWindow(handle)
                self._hidden = False
                return
            except Exception:
                pass
        self._window.show()
        self._hidden = False

    def toggle(self) -> None:
        handle = self._native_handle()
        if handle:
            try:
                if int(ctypes.windll.user32.GetForegroundWindow()) == handle:
                    self.hide()
                else:
                    self.show()
                return
            except Exception:
                pass
        if self._hidden:
            self.show()
        else:
            self.hide()

    def refresh(self) -> None:
        if self._window:
            self._window.evaluate_js("location.reload()")

    def toggle_fullscreen(self) -> bool:
        if not self._window:
            return False
        self._window.toggle_fullscreen()
        return True

    def restart(self) -> None:
        """Hand off a complete stack restart, then exit this shell."""
        def worker() -> None:
            try:
                from . import screen_recording

                screen_recording.shutdown()
            except Exception:
                pass
            import aios_updater

            aios_updater.restart_aios()

        threading.Thread(target=worker, daemon=True, name="aios-full-restart").start()

    def set_opacity(self, percent: float) -> bool:
        """Settings -> Appearance -> Opacity, applied to the real window.

        CSS cannot make a window translucent, so this is the Win32 layered-window
        route: the same visual result the Tk build got from `-alpha`.
        """
        if os.name != "nt" or not self._window:
            return False
        try:
            import ctypes

            handle = ctypes.wintypes.HWND(int(self._window.native.Handle))
            user32 = ctypes.windll.user32
            gwl_exstyle, ws_ex_layered, lwa_alpha = -20, 0x00080000, 0x00000002
            style = user32.GetWindowLongW(handle, gwl_exstyle)
            if not style & ws_ex_layered:
                user32.SetWindowLongW(handle, gwl_exstyle, style | ws_ex_layered)
            alpha = max(75, min(100, int(float(percent))))
            user32.SetLayeredWindowAttributes(handle, 0, int(alpha * 255 / 100), lwa_alpha)
            return True
        except Exception:
            return False

    def set_always_on_top(self, value: bool) -> bool:
        if os.name != "nt" or not self._window:
            return False
        try:
            # Do not assign ``self._window.on_top`` from a pywebview JS-API
            # callback.  On WinForms that setter marshals back to the UI
            # thread, while the UI thread is synchronously waiting for this
            # callback to finish: a classic startup deadlock that leaves both
            # the window and the local HTTP server apparently frozen.
            #
            # SetWindowPos is thread-safe for this window-level flag and does
            # not need the WinForms dispatcher, so it is safe from the bridge.
            handle = ctypes.wintypes.HWND(int(self._window.native.Handle))
            insert_after = ctypes.wintypes.HWND(-1 if value else -2)  # TOPMOST / NOTOPMOST
            flags = 0x0001 | 0x0002 | 0x0010  # NOSIZE | NOMOVE | NOACTIVATE
            ok = ctypes.windll.user32.SetWindowPos(
                handle, insert_after, 0, 0, 0, 0, flags,
            )
            if ok:
                self._window._on_top = bool(value)
            return bool(ok)
        except Exception:
            return False

    def resize_window(self, width: float, height: float) -> None:
        if not self._window:
            return
        self._window.resize(max(860, int(width)), max(560, int(height)))

    def move_window(self, x: float, y: float) -> None:
        if self._window:
            self._window.move(int(x), int(y))

    # -------------------------------------------------------------- dialogs

    def pick_folder(self) -> str:
        if not self._window:
            return ""
        result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        return str(result[0]) if result else ""

    def pick_files(self) -> list[str]:
        if not self._window:
            return []
        result = self._window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=True)
        return [str(item) for item in (result or [])]

    def pick_screen_area(self) -> dict[str, int]:
        """Hide aiOS and let the user drag a rectangle across the desktop."""
        if os.name != "nt":
            return {}
        from .screen_recording import virtual_screen_bounds

        bounds = virtual_screen_bounds()
        selection: dict[str, int] = {}
        root = None
        self.hide()
        try:
            import tkinter as tk

            root = tk.Tk()
            root.overrideredirect(True)
            root.attributes("-topmost", True)
            root.attributes("-alpha", 0.28)
            root.configure(bg="#000000")
            geometry = (
                f"{bounds['width']}x{bounds['height']}"
                f"{bounds['left']:+d}{bounds['top']:+d}"
            )
            root.geometry(geometry)
            canvas = tk.Canvas(root, bg="#000000", highlightthickness=0, cursor="crosshair")
            canvas.pack(fill="both", expand=True)
            canvas.create_text(
                bounds["width"] // 2,
                38,
                text="Drag to select the recording area   |   Esc to cancel",
                fill="#ffffff",
                font=("Segoe UI", 14, "bold"),
            )
            drag: dict[str, object] = {"start": None, "rect": None}

            def close() -> None:
                if root is not None:
                    root.quit()

            def begin(event: tk.Event) -> None:
                drag["start"] = (event.x, event.y)
                if drag["rect"] is not None:
                    canvas.delete(drag["rect"])
                drag["rect"] = canvas.create_rectangle(
                    event.x, event.y, event.x, event.y, outline="#ffffff", width=2
                )

            def move(event: tk.Event) -> None:
                if drag["start"] is None or drag["rect"] is None:
                    return
                x0, y0 = drag["start"]
                canvas.coords(drag["rect"], x0, y0, event.x, event.y)

            def finish(event: tk.Event) -> None:
                if drag["start"] is None:
                    close()
                    return
                x0, y0 = drag["start"]
                width = abs(event.x - x0)
                height = abs(event.y - y0)
                if width >= 24 and height >= 24:
                    selection.update(
                        left=bounds["left"] + min(x0, event.x),
                        top=bounds["top"] + min(y0, event.y),
                        width=width,
                        height=height,
                    )
                close()

            canvas.bind("<ButtonPress-1>", begin)
            canvas.bind("<B1-Motion>", move)
            canvas.bind("<ButtonRelease-1>", finish)
            root.bind("<Escape>", lambda _event: close())
            root.focus_force()
            root.mainloop()
        except Exception:
            selection = {}
        finally:
            if root is not None:
                try:
                    root.destroy()
                except Exception:
                    pass
            self.show()
        return selection

    def open_path(self, target: str) -> bool:
        """Reveal a file or folder in Explorer."""
        path = Path(str(target or ""))
        if not path.exists():
            return False
        os.startfile(str(path))  # noqa: S606 - Windows shell open, same as the Tk build
        return True


# Must match aios_ui/web/js/quick_tools.js TOOLS order (row-major 3×3).
QUICK_TOOL_IDS = (
    "webcam_snap",
    "phone_photos",
    "paste_image",
    "record_screen",
    "open_aios",
    "recordings",
    "downloads",
    "close",
    "open_code",
)


class QuickToolsApi:
    """Tiny always-on-top 3×3 palette driven by the macropad aiOS hold."""

    def __init__(self) -> None:
        self._window: webview.Window | None = None
        self._main: NativeApi | None = None
        self._visible = False

    def attach(self, window: webview.Window, main: NativeApi) -> None:
        self._window = window
        self._main = main
        self._visible = False

    def is_open(self) -> bool:
        return bool(self._visible)

    def trigger_key(self, number: int) -> bool:
        """Fire the Quick Tool in the same cell as macropad key 1–9."""
        if number < 1 or number > len(QUICK_TOOL_IDS):
            return False
        return self.trigger_tool(QUICK_TOOL_IDS[number - 1])

    def trigger_tool(self, tool_id: str) -> bool:
        """Run a palette action from the macropad (or a TCP `qt:` command)."""
        tid = str(tool_id or "").strip().lower()
        if not tid or not self._visible:
            return False
        # Webcam is one-shot from Python so the pad never waits on a preview UI.
        if tid == "webcam_snap":
            return self.webcam_snap_now()
        # Prefer the live page so status / busy / flash stay in sync.
        try:
            if self._window is not None:
                ran = self._window.evaluate_js(
                    "(function(){"
                    f"if (!window.aiosQt || !window.aiosQt.run) return false;"
                    f"return !!window.aiosQt.run({tid!r});"
                    "})()"
                )
                if ran:
                    return True
        except Exception:
            pass
        return self._trigger_tool_native(tid)

    def _trigger_tool_native(self, tid: str) -> bool:
        if tid == "close":
            self.hide()
            return True
        if tid == "open_aios":
            return self.open_main("")
        if tid == "open_code":
            return self.open_main("CODE")
        if tid == "webcam_snap":
            return self.webcam_snap_now()
        if tid == "record_screen":
            return self.open_tool(tid)
        if tid in {"downloads", "recordings", "paste_image", "phone_photos"}:
            from .settings_api import run_tool

            result = run_tool(tid, {})
            if isinstance(result, dict) and result.get("ok") is False:
                return False
            self.hide()
            return True
        return False

    def webcam_snap_now(self) -> bool:
        """Pad/Quick Tools Webcam: grab → clipboard → close palette → paste."""
        from . import webcam_snap

        result = webcam_snap.instant_snap_to_clipboard()
        self.hide()
        if not isinstance(result, dict) or result.get("ok") is False:
            return False
        try:
            time.sleep(0.03)
            webcam_snap.paste_clipboard()
        except Exception:
            return False
        return True

    def warm_webcam(self) -> None:
        """Start camera warm-up (safe to call from pad-down before QT opens)."""
        threading.Thread(
            target=self._warm_webcam_bg, daemon=True, name="aios-webcam-warm"
        ).start()

    def _warm_webcam_bg(self) -> None:
        try:
            from . import webcam_snap

            webcam_snap.warm_camera()
        except Exception:
            pass

    def style_window(self) -> None:
        if os.name != "nt" or not self._window:
            return
        try:
            import ctypes

            handle = int(self._window.native.Handle)
            preference = ctypes.c_int(2)  # DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.wintypes.HWND(handle), 33, ctypes.byref(preference), ctypes.sizeof(preference)
            )
        except Exception:
            pass

    def _center(self) -> None:
        if not self._window or os.name != "nt":
            return
        try:
            import ctypes

            width = int(getattr(self._window, "width", 360) or 360)
            height = int(getattr(self._window, "height", 400) or 400)
            user32 = ctypes.windll.user32
            screen_w = int(user32.GetSystemMetrics(0))
            screen_h = int(user32.GetSystemMetrics(1))
            self._window.move(max(0, (screen_w - width) // 2), max(0, (screen_h - height) // 2))
        except Exception:
            pass

    def show(self) -> None:
        if not self._window:
            return
        # Hold-open path must never leave the full shell sitting behind the palette.
        if self._main is not None:
            try:
                self._main.hide()
            except Exception:
                pass
        self._center()
        self._window.show()
        self._visible = True
        try:
            self._window.restore()
        except Exception:
            pass
        self.style_window()
        # Warm again if pad-down didn't already (keeps a live DIB ready).
        self.warm_webcam()

    def hide(self) -> None:
        if not self._window:
            return
        self._window.hide()
        self._visible = False
        # Keep the camera hot briefly so a second snap is still instant.
        try:
            from . import webcam_snap

            webcam_snap.schedule_idle_cool()
        except Exception:
            pass

    def toggle(self) -> None:
        if self._visible:
            self.hide()
        else:
            self.show()

    # --------------------------------------------------------------- js bridge

    def close(self) -> bool:
        self.hide()
        return True

    def open_main(self, tab: str = "") -> bool:
        self.hide()
        if not self._main:
            return False
        self._main.show()
        name = str(tab or "").strip()
        if name and self._main._window is not None:
            try:
                self._main._window.evaluate_js(
                    f"window.aios && window.aios.show({name!r})"
                )
            except Exception:
                pass
        return True

    def open_tool(self, name: str) -> bool:
        """Close the palette, raise aiOS, and open a Quick Tool that needs UI."""
        self.hide()
        if not self._main or self._main._window is None:
            return False
        self._main.show()
        key = str(name or "").strip()
        scripts = {
            "webcam_snap": "window.aios && window.aios.openWebcamSnap && window.aios.openWebcamSnap()",
            "record_screen": (
                "window.aios && window.aios.openScreenRecorder && "
                "window.aios.openScreenRecorder()"
            ),
        }
        script = scripts.get(key)
        if not script:
            return False

        def run() -> None:
            time.sleep(0.08)
            try:
                self._main._window.evaluate_js(script)
            except Exception:
                pass

        threading.Thread(target=run, daemon=True, name="aios-qt-tool").start()
        return True
