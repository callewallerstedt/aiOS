"""Focused coverage for the WebView2 Quick Tools screen recorder."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aios_ui import screen_recording, settings_api  # noqa: E402


@pytest.fixture(autouse=True)
def clean_recorder_state(monkeypatch):
    monkeypatch.setattr(screen_recording, "_PROCESS", None)
    monkeypatch.setattr(screen_recording, "_PATH", None)
    monkeypatch.setattr(screen_recording, "_STARTED_AT", 0.0)
    monkeypatch.setattr(screen_recording, "_LABEL", "")
    monkeypatch.setattr(screen_recording, "_LAST_MESSAGE", "Ready")
    monkeypatch.setattr(screen_recording, "_LAST_OK", True)


def test_quick_tool_dispatches_recorder_actions(monkeypatch):
    calls = []

    def fake_state(*, include_options=False):
        calls.append(("state", include_options))
        return {"ok": True, "active": False}

    monkeypatch.setattr(screen_recording, "state", fake_state)
    monkeypatch.setattr(screen_recording, "stop", lambda: {"ok": True, "active": False, "stopped": True})

    assert settings_api.run_tool("record_screen", {})["ok"] is True
    assert calls == [("state", True)]
    assert settings_api.run_tool("record_screen", {"action": "status"})["active"] is False
    assert settings_api.run_tool("record_screen", {"action": "stop"})["stopped"] is True
    unknown = settings_api.run_tool("record_screen", {"action": "explode"})
    assert unknown["ok"] is False
    assert "explode" in unknown["error"]


def test_monitor_recording_starts_ffmpeg_and_stops_cleanly(monkeypatch, tmp_path):
    target = tmp_path / "recording.mp4"
    launched = {}

    class FakeProcess:
        def __init__(self):
            self.stdin = io.BytesIO()
            self.stderr = io.BytesIO()
            self.running = True

        def poll(self):
            return None if self.running else 0

        def wait(self, timeout=None):
            self.running = False
            target.write_bytes(b"finished mp4")
            return 0

        def terminate(self):
            self.running = False

        def kill(self):
            self.running = False

    def fake_popen(command, **kwargs):
        launched["command"] = command
        launched["kwargs"] = kwargs
        launched["process"] = FakeProcess()
        return launched["process"]

    monitor = {
        "id": "0",
        "label": "Monitor 1 Primary 1920x1080",
        "left": 0,
        "top": 0,
        "width": 1920,
        "height": 1080,
    }
    monkeypatch.setattr(screen_recording, "ffmpeg_path", lambda: r"C:\ffmpeg.exe")
    monkeypatch.setattr(screen_recording, "recordings_dir", lambda: tmp_path)
    monkeypatch.setattr(screen_recording, "_unique_recording_path", lambda: target)
    monkeypatch.setattr(screen_recording, "list_monitors", lambda: [monitor])
    monkeypatch.setattr(screen_recording.subprocess, "Popen", fake_popen)

    started = screen_recording.start({"source": "monitor", "id": "0"})
    assert started["ok"] is True
    assert started["active"] is True
    assert started["label"] == monitor["label"]
    assert "gdigrab" in launched["command"]
    assert "1920x1080" in launched["command"]
    assert launched["kwargs"]["stdin"] is screen_recording.subprocess.PIPE

    stopped = screen_recording.stop()
    assert stopped["active"] is False
    assert stopped["message"] == "Saved recording.mp4"
    assert launched["process"].stdin.getvalue() == b"q\n"


def test_area_is_clamped_to_the_virtual_desktop(monkeypatch):
    monkeypatch.setattr(
        screen_recording,
        "virtual_screen_bounds",
        lambda: {"left": -100, "top": 0, "width": 300, "height": 200},
    )
    assert screen_recording._validated_area(
        {"left": -150, "top": -20, "width": 200, "height": 100}
    ) == {"left": -100, "top": 0, "width": 150, "height": 80}
    assert screen_recording._validated_area({"left": 0, "top": 0, "width": 10, "height": 10}) is None


def test_web_ui_contains_new_recorder_picker():
    app_js = (Path(__file__).resolve().parent.parent / "aios_ui" / "web" / "js" / "app.js").read_text(
        encoding="utf-8"
    )
    assert "showScreenRecorder" in app_js
    assert 'data-record-source="area"' in app_js
    assert 'body: { action: "stop" }' in app_js
    assert "still runs on the Tk window" not in app_js
