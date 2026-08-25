"""Macropad → Quick Tools grid routing."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aios_ui.api import QUICK_TOOL_IDS, QuickToolsApi  # noqa: E402


def test_quick_tool_ids_are_nine_cells():
    assert len(QUICK_TOOL_IDS) == 9
    assert QUICK_TOOL_IDS[0] == "webcam_snap"
    assert QUICK_TOOL_IDS[7] == "close"
    assert QUICK_TOOL_IDS[8] == "open_code"


def test_trigger_key_maps_to_tool_id():
    qt = QuickToolsApi()
    qt._visible = True
    qt.trigger_tool = MagicMock(return_value=True)

    assert qt.trigger_key(1) is True
    qt.trigger_tool.assert_called_once_with("webcam_snap")

    qt.trigger_tool.reset_mock()
    assert qt.trigger_key(8) is True
    qt.trigger_tool.assert_called_once_with("close")


def test_trigger_tool_native_close_hides():
    qt = QuickToolsApi()
    qt._visible = True
    qt.hide = MagicMock()

    assert qt._trigger_tool_native("close") is True
    qt.hide.assert_called_once_with()


def test_trigger_webcam_uses_instant_path(monkeypatch):
    qt = QuickToolsApi()
    qt._visible = True
    qt.webcam_snap_now = MagicMock(return_value=True)
    qt._window = MagicMock()

    assert qt.trigger_tool("webcam_snap") is True
    qt.webcam_snap_now.assert_called_once_with()
    qt._window.evaluate_js.assert_not_called()


def test_trigger_tool_ignored_when_closed():
    qt = QuickToolsApi()
    qt._visible = False
    qt._window = MagicMock()

    assert qt.trigger_tool("webcam_snap") is False
    qt._window.evaluate_js.assert_not_called()
