"""Input-target and progress invariants for the desktop operator."""
from __future__ import annotations

import asyncio
import inspect

import pytest

from director.operator import display as display_mod
from director.operator import loop, x11


class FakeXdotool:
    def __init__(self, *, pointer="200", active="100", activation_fails=False):
        self.pointer = pointer
        self.active = active
        self.activation_fails = activation_fails
        self.calls = []

    async def __call__(self, *args, settings=None):
        self.calls.append(args)
        if args[:2] == ("getmouselocation", "--shell"):
            return 0, f"X=10\nY=20\nWINDOW={self.pointer}"
        if args == ("getactivewindow",):
            return 0, self.active
        if args[:2] == ("windowactivate", "--sync"):
            if self.activation_fails:
                return 1, "activation denied"
            self.active = args[2]
            return 0, ""
        if args[:2] == ("windowfocus", "--sync"):
            return 1, "focus denied"
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
    assert typed[1] == len("private value")
    assert "private value" not in str(typed)


def test_operator_loop_has_a_bounded_unchanged_action_budget():
    source = inspect.getsource(loop.run_task)
    assert "MAX_REPEATED_NO_EFFECT_ROUNDS" in source
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


def test_operator_does_not_close_windows_by_matching_titles():
    source = inspect.getsource(display_mod) + inspect.getsource(loop.run_task)
    assert "NAG_TITLES" not in source
    assert "dismiss_stray_dialogs" not in source
