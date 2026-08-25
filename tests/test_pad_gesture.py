"""Macropad aiOS-button short/hold gesture."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aios_ui.pad_gesture import PadGesture  # noqa: E402


class FakeMain:
    def __init__(self):
        self.actions = []

    def hide(self):
        self.actions.append("hide")

    def show(self):
        self.actions.append("show")

    def toggle(self):
        self.actions.append("toggle")


class FakeQt:
    def __init__(self):
        self.open = False
        self.actions = []

    def is_open(self):
        return self.open

    def show(self):
        self.open = True
        self.actions.append("show")

    def hide(self):
        self.open = False
        self.actions.append("hide")


def test_short_press_toggles_main_only_after_release():
    main, qt = FakeMain(), FakeQt()
    pad = PadGesture(main, qt, hold_ms=80)
    pad.down()
    assert main.actions == []
    assert qt.actions == []
    result = pad.up()
    assert result["phase"] == "tap"
    assert main.actions == ["toggle"]
    assert qt.actions == []


def test_hold_opens_quick_tools_and_hides_main():
    main, qt = FakeMain(), FakeQt()
    pad = PadGesture(main, qt, hold_ms=50)
    pad.down()
    time.sleep(0.12)
    assert main.actions == ["hide"]
    assert qt.actions == ["show"]
    assert pad.up()["phase"] == "hold"
    assert "toggle" not in main.actions


def test_short_press_closes_open_quick_tools():
    main, qt = FakeMain(), FakeQt()
    qt.open = True
    pad = PadGesture(main, qt, hold_ms=80)
    pad.down()
    assert pad.up()["phase"] == "closed_quick_tools"
    assert qt.actions == ["hide"]
    assert main.actions == []


def test_oneshot_toggle_opens_main_not_quick_tools():
    main, qt = FakeMain(), FakeQt()
    pad = PadGesture(main, qt, hold_ms=80)
    assert pad.toggle_main()["phase"] == "tap"
    assert main.actions == ["toggle"]
    assert qt.actions == []
