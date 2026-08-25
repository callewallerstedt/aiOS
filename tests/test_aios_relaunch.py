import os
import re
import subprocess
import sys
import time
from pathlib import Path

import aios_relaunch
import aios_updater
import launch_aios
from aios_ui import api as native_api
from aios_ui import screen_recording


def test_process_alive_self():
    assert aios_relaunch.process_alive(os.getpid()) is True
    assert aios_relaunch.process_alive(0) is False
    assert aios_relaunch.process_alive(-1) is False


def test_wait_for_exit_returns_quickly_for_dead_pid():
    started = time.perf_counter()
    assert aios_relaunch.wait_for_exit(999_999_999, timeout=1.0) is True
    assert time.perf_counter() - started < 0.5


def test_spawn_relaunch_launches_waiter(tmp_path, monkeypatch):
    calls = []

    class FakePopen:
        def __init__(self, args, **kwargs):
            calls.append((list(args), kwargs))

    monkeypatch.setattr(aios_updater.subprocess, "Popen", FakePopen)
    assert aios_updater.spawn_relaunch(12345, ["--fast-start"]) is True
    assert calls
    args, kwargs = calls[0]
    assert str(aios_updater.BASE_DIR / "aios_relaunch.py") in args
    assert "12345" in args
    assert "--fast-start" in args
    if os.name == "nt":
        assert kwargs.get("creationflags")


def test_relaunch_main_starts_complete_stack_after_parent_gone(monkeypatch):
    launched = []
    stopped = []

    monkeypatch.setattr(aios_relaunch, "wait_for_exit", lambda pid, timeout=30.0: True)
    monkeypatch.setattr(aios_relaunch.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        aios_relaunch,
        "stop_managed_processes",
        lambda *, exclude=None: stopped.append(set(exclude or set())) or [11, 12],
    )
    monkeypatch.setattr(aios_relaunch, "clear_stale_heartbeats", lambda: None)
    monkeypatch.setattr(aios_relaunch, "launch_helper", lambda extra=None: launched.append(list(extra or [])))
    monkeypatch.setattr(aios_relaunch, "SHELL_PATH", Path(__file__))

    assert aios_relaunch.main(["4242", "--fast-start"]) == 0
    assert launched == [["--fast-start"]]
    assert stopped == [{os.getpid()}]


def test_full_restart_uses_one_taskkill_for_all_managed_trees(monkeypatch):
    calls = []
    monkeypatch.setattr(aios_relaunch, "managed_process_pids", lambda *, exclude=None: [101, 202, 303])
    monkeypatch.setattr(aios_relaunch.subprocess, "run", lambda args, **kwargs: calls.append(list(args)))

    assert aios_relaunch.stop_managed_processes(exclude={999}) == [101, 202, 303]
    assert calls == [[
        "taskkill", "/T", "/F", "/PID", "101", "/PID", "202", "/PID", "303",
    ]]


def test_managed_selector_matches_python_module_mirror_process():
    pattern = re.compile("|".join(aios_relaunch.MANAGED_COMMAND_PATTERNS), re.IGNORECASE)

    assert pattern.search(r'"C:\Program Files\Python314\pythonw.exe" -m aios_ui.mirror')
    assert pattern.search(r'C:\Python\python.exe -m aios_ui.mirror --serve')


def test_managed_selector_rejects_similarly_named_modules():
    pattern = re.compile("|".join(aios_relaunch.MANAGED_COMMAND_PATTERNS), re.IGNORECASE)

    assert not pattern.search(r'C:\Python\pythonw.exe -m aios_ui.mirror_tools')
    assert not pattern.search(r'C:\Python\pythonw.exe -m third_party.aios_ui.mirror')


def test_launch_stack_fast_starts_the_watchdog(monkeypatch):
    calls = []
    monkeypatch.setattr(aios_relaunch, "WATCHDOG_PATH", Path(__file__))
    monkeypatch.setattr(aios_relaunch, "find_pythonw", lambda: r"C:\Python\pythonw.exe")
    monkeypatch.setattr(
        aios_relaunch.subprocess,
        "Popen",
        lambda args, **kwargs: calls.append((list(args), kwargs)),
    )

    aios_relaunch.launch_stack()
    assert calls[0][0] == [r"C:\Python\pythonw.exe", str(Path(__file__)), "--fast-start"]


def test_desktop_launcher_hands_off_to_full_restart(monkeypatch):
    calls = []
    monkeypatch.setattr(launch_aios, "_create_desktop_shortcut", lambda: "aiOS.lnk")
    monkeypatch.setattr(
        aios_updater,
        "spawn_relaunch",
        lambda parent_pid=None, extra_args=None: calls.append((parent_pid, list(extra_args or []))) or True,
    )

    assert launch_aios.main() == 0
    assert calls == [(0, ["--fast-start"])]


def test_desktop_shortcut_targets_the_restart_launcher(monkeypatch, tmp_path):
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    captured = []

    def fake_run(args, **_kwargs):
        captured.append(list(args))
        (desktop / "aiOS.lnk").touch()

    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(launch_aios, "_find_pythonw", lambda: r"C:\Python\pythonw.exe")
    monkeypatch.setattr(launch_aios.subprocess, "run", fake_run)

    assert launch_aios._create_desktop_shortcut() == str(desktop / "aiOS.lnk")
    powershell = captured[0][-1]
    assert str(Path(launch_aios.__file__).resolve()) in powershell
    assert "aios_shell.py" not in powershell


def test_webview_restart_button_uses_the_same_full_restart(monkeypatch):
    calls = []

    class ImmediateThread:
        def __init__(self, target, **_kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(native_api.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(screen_recording, "shutdown", lambda: calls.append("recording stopped"))
    monkeypatch.setattr(aios_updater, "restart_aios", lambda: calls.append("full restart"))

    native_api.NativeApi().restart()
    assert calls == ["recording stopped", "full restart"]


def test_webview_control_socket_accepts_restart():
    source = (Path(__file__).resolve().parent.parent / "aios_ui" / "app.py").read_text(encoding="utf-8")
    assert 'elif command == "restart":' in source
    assert "api.restart()" in source


def test_macropad_triple_tap_restart_is_wired():
    source = (Path(__file__).resolve().parent.parent / "autocorrect.ahk").read_text(encoding="utf-8")
    assert "AiosButtonTap()" in source
    assert "AiosRestartNow()" in source
    assert 'SendToHelper("restart")' in source


def test_macropad_aios_hold_opens_quick_tools():
    source = (Path(__file__).resolve().parent.parent / "autocorrect.ahk").read_text(encoding="utf-8")
    assert "AiosQuickToolsMs := 200" in source
    assert 'SendToHelper("pad_down")' in source
    assert 'SendToHelper("pad_up")' in source
    assert "CombinedHotkeyUp" in source
    assert "SeparateAiosUp" in source
    assert (Path(__file__).resolve().parent.parent / "aios_pad_down.bat").is_file()
    assert (Path(__file__).resolve().parent.parent / "aios_pad_up.bat").is_file()


def test_quick_tools_overlay_is_wired_into_shell():
    root = Path(__file__).resolve().parent.parent
    app = (root / "aios_ui" / "app.py").read_text(encoding="utf-8")
    assert "quick_tools" in app
    assert "QuickToolsApi" in app
    assert "WEBVIEW_PORT" in app
    assert (root / "aios_ui" / "web" / "quick_tools.html").is_file()
    assert (root / "aios_ui" / "web" / "js" / "quick_tools.js").is_file()
    assert (root / "aios_ui" / "web" / "css" / "quick_tools.css").is_file()
    control = (root / "aios_ui" / "control.py").read_text(encoding="utf-8")
    assert "WEBVIEW_PORT = 48739" in control
    ahk = (root / "autocorrect.ahk").read_text(encoding="utf-8")
    assert "SendToPort(msg, 48739)" in ahk


def test_tray_exposes_the_complete_restart_action():
    source = (Path(__file__).resolve().parent.parent / "helper_overlay.py").read_text(encoding="utf-8")
    assert '"Restart aiOS (GUI + backend)"' in source
    assert "elif command == TRAY_RESTART_APP:" in source
    assert "self.restart_application()" in source


def test_restart_aios_uses_spawn_relaunch(monkeypatch):
    calls = []
    monkeypatch.setattr(aios_updater, "STAGING_DIR", Path("definitely-missing-staging-dir"))
    monkeypatch.setattr(aios_updater, "spawn_relaunch", lambda pid, args=None: calls.append((pid, list(args or []))) or True)
    monkeypatch.setattr(aios_updater.threading, "Timer", lambda delay, fn: type("T", (), {"start": lambda self: None})())
    aios_updater.restart_aios()
    assert calls
    assert "--fast-start" in calls[0][1]
