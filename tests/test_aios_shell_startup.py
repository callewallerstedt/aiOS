"""Regression coverage for WebView2 restart freezes."""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import launch_aios  # noqa: E402
from aios_ui.app import (  # noqa: E402
    _profile_in_use,
    _visible_window_position,
    _window_geometry,
)
from aios_ui.api import NativeApi  # noqa: E402


@pytest.mark.skipif(os.name != "nt", reason="WebView2 profile locks are Windows-only")
def test_webview_profile_lock_is_detected_with_windows_sharing_rules(tmp_path):
    lock = tmp_path / "EBWebView" / "lockfile"
    lock.parent.mkdir(parents=True)
    lock.write_bytes(b"")
    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(lock), 0x80000000, 0, None, 3, 0, None,
    )
    assert handle != ctypes.c_void_p(-1).value
    try:
        assert _profile_in_use(tmp_path) is True
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)
    assert _profile_in_use(tmp_path) is False


def test_always_on_top_never_marshals_back_through_winforms():
    """A JS bridge worker assigning window.on_top deadlocks with the UI thread."""
    import inspect

    source = inspect.getsource(NativeApi.set_always_on_top)
    assert "SetWindowPos" in source
    assert "self._window.on_top =" not in source


def test_main_shell_is_a_normal_snap_and_alt_tab_window():
    app = (ROOT / "aios_ui" / "app.py").read_text(encoding="utf-8")
    web = (ROOT / "aios_ui" / "web" / "js" / "app.js").read_text(encoding="utf-8")
    defaults = (ROOT / "helper_overlay.py").read_text(encoding="utf-8")

    main_window = app.split('window = webview.create_window(', 1)[1].split('qt_window =', 1)[0]
    assert "frameless=False" in main_window
    assert "resizable=True" in main_window
    assert "on_top=False" in main_window
    assert 'theme.always_on_top === true' in web
    assert '"always_on_top": False' in defaults


def test_aios_window_geometry_restores_its_own_last_position(monkeypatch):
    import helper_overlay

    monkeypatch.setattr(
        helper_overlay,
        "load_config",
        lambda: {"aios_window": "1280x720-1440+96", "window": "900x600+1+2"},
    )
    monkeypatch.setattr("aios_ui.app._monitor_work_areas", lambda: [(-1920, 0, 0, 1080)])
    assert _window_geometry() == (1280, 720, -1440, 96)

    app = (ROOT / "aios_ui" / "app.py").read_text(encoding="utf-8")
    assert 'config["aios_window"]' in app
    assert "window.events.moved += on_window_moved" in app
    assert "window.events.resized += on_window_resized" in app


def test_offscreen_geometry_moves_only_position_and_preserves_size():
    areas = [(0, 0, 2560, 1392), (275, -1080, 2195, -48)]
    assert _visible_window_position(1936, 1048, 267, -1088, areas) == (267, -1088)
    assert _visible_window_position(1936, 1048, -32000, -32000, areas) == (0, 0)


def test_toggle_uses_real_foreground_state_and_show_never_restores_normal_window():
    source = (ROOT / "aios_ui" / "api.py").read_text(encoding="utf-8")
    show = source.split("    def show(self) -> None:", 1)[1].split("    def toggle(self) -> None:", 1)[0]
    toggle = source.split("    def toggle(self) -> None:", 1)[1].split("    def refresh(self) -> None:", 1)[0]
    assert "GetForegroundWindow" in toggle
    assert "SetForegroundWindow" in show
    assert "NOSIZE | NOMOVE" in show
    assert "self._window.restore()" not in show


def test_geometry_persistence_rejects_windows_hidden_position_sentinel():
    app = (ROOT / "aios_ui" / "app.py").read_text(encoding="utf-8")
    assert "not api.is_geometry_stable()" in app
    assert "int(new_x) <= -30000" in app


def test_fullscreen_uses_the_native_webview_window():
    class Window:
        def __init__(self):
            self.calls = 0

        def toggle_fullscreen(self):
            self.calls += 1

    window = Window()
    api = NativeApi()
    api.attach(window)
    assert api.toggle_fullscreen() is True
    assert window.calls == 1


def test_web_shell_has_balance_fullscreen_and_mouse_page_history():
    index = (ROOT / "aios_ui" / "web" / "index.html").read_text(encoding="utf-8")
    app = (ROOT / "aios_ui" / "web" / "js" / "app.js").read_text(encoding="utf-8")
    server = (ROOT / "aios_ui" / "server.py").read_text(encoding="utf-8")
    assert 'id="openrouter-balance"' in index
    assert 'data-action="toggle_fullscreen"' in index
    assert '"mousedown", "mouseup", "auxclick"' in app
    assert "event.button !== 3 && event.button !== 4" in app
    assert "this.goPageHistory(event.button === 3 ? -1 : 1)" in app
    assert 'route == "/api/openrouter/balance"' in server


def test_web_boot_times_out_backend_calls_and_still_builds_chrome():
    bridge = (ROOT / "aios_ui" / "web" / "js" / "bridge.js").read_text(encoding="utf-8")
    app = (ROOT / "aios_ui" / "web" / "js" / "app.js").read_text(encoding="utf-8")
    assert "AbortController" in bridge and "controller.abort()" in bridge
    assert 'api("/api/config", { timeout: 5000 })' in app
    assert "for (const step of [this.buildNav" in app


def test_shell_persists_zoom_and_sidebar_preferences_across_profiles():
    app = (ROOT / "aios_ui" / "web" / "js" / "app.js").read_text(encoding="utf-8")
    index = (ROOT / "aios_ui" / "web" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "aios_ui" / "web" / "css" / "shell.css").read_text(encoding="utf-8")
    assert "ui_preferences" in app
    assert 'body: { patch: { ui_preferences: this.uiPreferences } }' in app
    assert "this.uiPreferences.zoom = this.zoomLevel" in app
    assert 'aria-controls="nav"' in index and 'aria-controls="chat-panel"' in index
    assert ".collapse-glyph" in css and ".sr-only" in css
