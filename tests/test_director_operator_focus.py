"""Input-target and progress invariants for the desktop operator."""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from director.operator import display as display_mod
from director.operator import loop, x11


class FakeXdotool:
    def __init__(self, *, pointer="200", active="100", activation_fails=False,
                 activation_no_effect=False, focus_fails=True,
                 active_api_fails=False):
        self.pointer = pointer
        self.active = active
        self.activation_fails = activation_fails
        self.activation_no_effect = activation_no_effect
        self.focus_fails = focus_fails
        self.active_api_fails = active_api_fails
        self.x = 10
        self.y = 20
        self.calls = []

    async def __call__(self, *args, settings=None):
        self.calls.append(args)
        if args[:2] == ("getmouselocation", "--shell"):
            return 0, f"X={self.x}\nY={self.y}\nWINDOW={self.pointer}"
        if args and args[0] == "mousemove":
            self.x, self.y = int(args[1]), int(args[2])
            return 0, ""
        if args == ("getactivewindow",):
            if self.active_api_fails:
                return 1, "_NET_ACTIVE_WINDOW unavailable"
            return 0, self.active
        if args == ("getwindowfocus",):
            return 0, self.active
        if args[:2] == ("windowactivate", "--sync"):
            if self.activation_fails:
                return 1, "activation denied"
            if not self.activation_no_effect:
                self.active = args[2]
            return 0, ""
        if args[:2] == ("windowfocus", "--sync"):
            if self.focus_fails:
                return 1, "focus denied"
            self.active = args[2]
            return 0, ""
        if args and args[0] == "getwindowname":
            return 0, "Browser"
        return 0, ""


async def _handled(value):
    return {"handled": value}


def test_pointer_window_is_activated_before_click(monkeypatch):
    fake = FakeXdotool()
    monkeypatch.setattr(x11, "xdotool", fake)
    monkeypatch.setattr(x11, "accessible_click", lambda *args, **kwargs: _handled(False))

    asyncio.run(x11.click(40, 50, settings={}))

    activate = fake.calls.index(("windowactivate", "--sync", "200"))
    click = next(index for index, call in enumerate(fake.calls) if call[0] == "click")
    assert activate < click
    assert fake.active == "200"


def test_keyboard_action_refuses_to_claim_success_without_focus(monkeypatch):
    fake = FakeXdotool(activation_fails=True)
    monkeypatch.setattr(x11, "xdotool", fake)

    with pytest.raises(RuntimeError, match="could not focus pointer window"):
        asyncio.run(x11.press("tab", settings={}))
    assert not any(call and call[0] == "key" for call in fake.calls)


def test_keyboard_action_keeps_an_already_focused_target(monkeypatch):
    fake = FakeXdotool(pointer="200", active="200")
    monkeypatch.setattr(x11, "xdotool", fake)

    asyncio.run(x11.hotkey(["ctrl", "l"], settings={}))

    assert not any(call and call[0] in {"windowactivate", "windowfocus"} for call in fake.calls)
    assert ("key", "--clearmodifiers", "ctrl+l") in fake.calls


def test_server_input_focus_is_used_when_desktop_active_property_is_missing(monkeypatch):
    fake = FakeXdotool(pointer="200", active="200", active_api_fails=True)
    monkeypatch.setattr(x11, "xdotool", fake)

    assert asyncio.run(x11.active_window({})) == "200"


def test_focus_falls_back_when_window_activation_reports_success_without_effect(monkeypatch):
    fake = FakeXdotool(
        activation_no_effect=True, focus_fails=False, active_api_fails=True,
    )
    monkeypatch.setattr(x11, "xdotool", fake)

    focused = asyncio.run(x11.focus_pointer_window({}))

    assert focused == "200"
    assert ("windowactivate", "--sync", "200") in fake.calls
    assert ("windowfocus", "--sync", "200") in fake.calls
    assert fake.active == "200"


def test_screen_signature_ignores_tiny_change_but_detects_page_change():
    previous = bytes([4] * 100)
    tiny = bytes([8] + [4] * 99)
    different = bytes([8] * 100)

    assert x11.image_change_ratio(previous, tiny) == pytest.approx(0.01)
    assert x11.image_change_ratio(previous, different) == pytest.approx(1.0)


def test_action_signatures_group_same_effect_without_exposing_typed_text():
    first = loop.action_signature({"type": "click", "x": 100, "y": 100})
    nearby = loop.action_signature({"type": "click", "x": 108, "y": 111})
    typed = loop.action_signature({"type": "type", "text": "private value"})

    assert first == nearby
    assert typed[3] == len("private value")
    assert "private value" not in str(typed)


def test_typed_text_signatures_include_the_target_control():
    first = loop.action_signature({"type": "type", "x": 100, "y": 100,
                                   "text": "same"})
    other_field = loop.action_signature({"type": "type", "x": 100, "y": 200,
                                         "text": "same"})

    assert first != other_field


def test_type_text_focuses_the_requested_control_before_paste(monkeypatch):
    calls = []

    async def fake_click(x, y, *args, **kwargs):
        calls.append(("click", x, y))

    async def fake_focus(*args, **kwargs):
        calls.append(("focus",))
        return "200"

    async def fake_checked(*args, **kwargs):
        calls.append(args)

    monkeypatch.setattr(x11, "click", fake_click)
    monkeypatch.setattr(x11, "focus_pointer_window", fake_focus)
    monkeypatch.setattr(x11, "_checked_xdotool", fake_checked)
    monkeypatch.setattr(x11.shutil, "which", lambda _name: None)

    asyncio.run(x11.type_text("hello", {}, x=320, y=240))

    assert calls[0] == ("click", 320, 240)
    assert calls[1] == ("focus",)
    assert calls[2][-1] == "hello"


def test_kernel_keyboard_chords_hold_modifiers_until_the_key_is_released():
    assert x11.kernel_key_events(["ctrl", "a"]) == [
        [29, 1], [30, 1], [30, 0], [29, 0],
    ]


def test_operator_loop_has_a_bounded_unchanged_action_budget():
    source = inspect.getsource(loop.run_task)
    assert "MAX_NO_POSTCONDITION_ACTIONS" in source
    assert "MAX_CYCLE_STRIKES" in source
    assert "MAX_DISTINCT_ACTIONS_ON_SCREEN" in source
    assert "actions_on_screen" in source
    assert "operator.stuck" in source


def test_unchanged_loop_executes_once_then_stops(monkeypatch):
    events = []
    model_calls = []
    executed = []

    async def ready(*_args, **_kwargs):
        return {"ready": True, "display": ":0"}

    async def size(*_args, **_kwargs):
        return 100, 100

    async def capture(*_args, **_kwargs):
        return b"same-screen"

    async def windows(*_args, **_kwargs):
        return ["Browser"]

    async def controls(*_args, **_kwargs):
        return []

    async def complete(**_kwargs):
        model_calls.append(True)
        return {
            "tool_calls": [{
                "name": "click",
                "arguments": '{"x": 48, "y": 48}',
            }],
            "reasoning": "try the visible control",
            "model": "test",
        }

    async def execute(action, _settings):
        executed.append(action)
        return "click (48,48)"

    async def emit(kind, payload):
        events.append((kind, payload))

    monkeypatch.setattr(loop.display_mod, "ensure_running", ready)
    monkeypatch.setattr(loop.x11, "screen_size", size)
    monkeypatch.setattr(loop.x11, "capture", capture)
    monkeypatch.setattr(loop.x11, "encode_jpeg", lambda _data: ("data:image/jpeg;base64,x", 100, 100))
    monkeypatch.setattr(loop.x11, "image_signature", lambda _data: bytes([5] * 100))
    monkeypatch.setattr(loop.x11, "window_list", windows)
    monkeypatch.setattr(loop.x11, "accessible_controls", controls)
    monkeypatch.setattr(loop.models, "complete", complete)
    monkeypatch.setattr(loop, "execute", execute)
    monkeypatch.setattr(loop, "background", lambda: "")

    result = asyncio.run(loop.run_task(
        "click the control", emit=emit,
        settings={"operator": {"review_every": 30}},
    ))

    assert result["status"] == "stopped"
    assert result["steps"] == loop.MAX_REPEATED_NO_EFFECT_ROUNDS + 2
    assert len(executed) == 1
    assert len(model_calls) == loop.MAX_REPEATED_NO_EFFECT_ROUNDS + 1
    assert events[-1][0] == "operator.stuck"


def test_chrome_prevents_restore_bubbles_at_startup():
    source = inspect.getsource(display_mod)
    assert "--hide-crash-restore-bubble" in source
    assert "--disable-session-crashed-bubble" in source


def test_chrome_exposes_real_control_bounds_to_the_operator():
    argv = display_mod.chrome_argv("", {"operator": {"width": 1280, "height": 720}})
    assert "--force-renderer-accessibility" in argv
    service = (Path(__file__).parents[1]
               / "director/deploy/aios-director-chrome.service").read_text()
    assert "--force-renderer-accessibility" in service


def test_deployment_installs_a_kernel_level_keyboard_driver():
    installer = (Path(__file__).parents[1]
                 / "director/deploy/install.sh").read_text()
    assert "uinput_keyboard.py" in installer
    assert "aios-director-keyboard.service" in installer


def test_operator_does_not_close_windows_by_matching_titles():
    source = inspect.getsource(display_mod) + inspect.getsource(loop.run_task)
    assert "NAG_TITLES" not in source
    assert "dismiss_stray_dialogs" not in source
