"""The loop's own safety nets, driven with a fake model and a fake desktop."""

import json
import sys
import time
import types
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent_clicker"))


def _stub(name, **attrs):
    """Stand in for a Windows/desktop module that cannot import off-Windows."""
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def _noop(*args, **kwargs):
    return None


for name in ("mss", "pyautogui", "pyperclip"):
    try:  # pragma: no cover - present on a real operator machine
        __import__(name)
    except Exception:
        sys.modules[name] = _stub(name, FAILSAFE=True, PAUSE=0.0, mss=_noop,
                                  size=lambda: (1920, 1080), position=lambda: (0, 0))
try:  # pragma: no cover - Windows only
    import desktop_agent.winput  # noqa: F401
except Exception:
    sys.modules["desktop_agent.winput"] = _stub(
        "desktop_agent.winput", OPERATOR_INPUT_TAG=0,
        move_to=_noop, click=_noop, mouse_down=_noop, mouse_up=_noop,
        move_rel=_noop, key_down=_noop, key_up=_noop, scroll=_noop,
        any_button_held=lambda: False, any_key_held=lambda: False, release_all=_noop,
    )

from desktop_agent import loop as agent_loop  # noqa: E402
from desktop_agent.loop import AgentLoop  # noqa: E402
from desktop_agent.screen import Monitor  # noqa: E402


MONITOR = Monitor(index=1, left=0, top=0, width=800, height=600, label="Monitor 1  800x600")


class FakeDesktop:
    """A screen that only changes when the agent does something that works."""

    def __init__(self):
        self.marks = 0

    def capture(self, _monitor):
        image = Image.new("RGB", (800, 600), (18, 20, 26))
        draw = ImageDraw.Draw(image)
        for index in range(self.marks):
            draw.rectangle([20 + index * 40, 20, 50 + index * 40, 60], fill=(240, 240, 240))
        return image


class FakeModel:
    """Replies from a script; records every request it was sent."""

    def __init__(self, replies, verifier=None):
        self.replies = list(replies)
        self.verifier = list(verifier or [])
        self.requests = []
        self.step_requests = []
        self.systems = []

    def chat_raw(self, system, messages, **kwargs):
        self.systems.append(system)
        self.requests.append(messages)
        if "pre-run planner" not in system and "actually did" not in system:
            self.step_requests.append(messages)
        if "completion checker" in system or "actually did" in system:
            reply = self.verifier.pop(0) if self.verifier else {"verdict": "pass", "reason": "looks done"}
        elif "pre-run planner" in system:
            reply = {"plan": "Do the thing.", "todo": ["Open the app", "Do the thing"],
                     "done_when": ["The thing is on screen"]}
        else:
            reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return json.dumps(reply)


def run_loop(model, desktop, monkeypatch, tmp_path, *, max_steps=12, planner="planner-model",
             backend="api", usage=None):
    monkeypatch.setattr(agent_loop, "DEBUG_ROOT", str(tmp_path / "runs"))
    monkeypatch.setattr(agent_loop.vlm, "chat_raw", model.chat_raw)
    usage = usage or {"requests": 1, "model": "m"}
    monkeypatch.setattr(agent_loop.vlm, "take_last_usage", lambda: dict(usage))
    monkeypatch.setattr(agent_loop, "capture", desktop.capture)

    def fake_execute(action, monitor, **kwargs):
        return agent_loop.ExecResult(action=action, ok=True, detail="ok", elapsed_ms=1, output="")

    monkeypatch.setattr(agent_loop, "execute", fake_execute)
    monkeypatch.setattr(agent_loop, "any_button_held", lambda: False)
    monkeypatch.setattr(agent_loop, "any_key_held", lambda: False)
    monkeypatch.setattr(agent_loop, "release_all", _noop)

    events = []
    agent = AgentLoop(events.append)

    def on_event(event):
        events.append(event)
        # Cap hit now pauses for Continue instead of ending — stop so helpers
        # that only care about progress during the budget still finish.
        if event.get("type") == "max_steps":
            agent.stop()

    agent.on_event = on_event
    agent.start("Do the thing", MONITOR, model="clicker", max_steps=max_steps,
                action_delay=0.0, settle_after_step=0.0, planner_model=planner,
                backend=backend)
    agent._thread.join(timeout=30)
    assert not agent.is_running(), "the loop did not finish"
    return events


def final(events):
    return [event for event in events if event["type"] == "done"][-1]


def test_a_dead_click_repeated_ends_the_run_instead_of_burning_every_step(monkeypatch, tmp_path):
    """The reported symptom: it loops for ages and never gets anywhere."""
    model = FakeModel([{"thought": "clicking the button", "status": "continue",
                        "actions": [{"type": "click", "x": 100, "y": 100}]}])
    events = run_loop(model, FakeDesktop(), monkeypatch, tmp_path, max_steps=30)

    ended = final(events)
    steps = max(event.get("n", 0) for event in events if event["type"] == "step_begin")
    assert ended["ok"] is False
    assert "no progress" in ended["message"].lower()
    assert steps < 12, f"gave up after {steps} steps — should notice much sooner"
    assert any("nudged" in str(event.get("msg", "")) for event in events if event["type"] == "log"), \
        "the model should be warned before the run is pulled"


def test_a_run_that_keeps_changing_the_screen_is_left_alone(monkeypatch, tmp_path):
    desktop = FakeDesktop()

    class Progressing(FakeModel):
        def chat_raw(self, system, messages, **kwargs):
            if "planner" not in system and "actually did" not in system:
                desktop.marks += 1
            return super().chat_raw(system, messages, **kwargs)

    model = Progressing([{"thought": "scrolling on", "status": "continue",
                          "actions": [{"type": "key", "key": "pagedown"}]}])
    events = run_loop(model, desktop, monkeypatch, tmp_path, max_steps=8)

    assert "no progress" not in final(events)["message"].lower()


def test_running_out_of_steps_does_not_invoke_the_completion_checker(monkeypatch, tmp_path):
    desktop = FakeDesktop()

    class Progressing(FakeModel):
        def chat_raw(self, system, messages, **kwargs):
            if "planner" not in system and "actually did" not in system:
                desktop.marks += 1
            return super().chat_raw(system, messages, **kwargs)

    model = Progressing([{"thought": "still working", "status": "continue",
                          "actions": [{"type": "key", "key": "pagedown"}]}])
    events = []
    agent = AgentLoop(events.append)
    monkeypatch.setattr(agent_loop, "DEBUG_ROOT", str(tmp_path / "runs"))
    monkeypatch.setattr(agent_loop.vlm, "chat_raw", model.chat_raw)
    monkeypatch.setattr(agent_loop.vlm, "take_last_usage", lambda: {"requests": 1, "model": "m"})
    monkeypatch.setattr(agent_loop, "capture", desktop.capture)
    monkeypatch.setattr(agent_loop, "execute",
                        lambda action, monitor, **kwargs: agent_loop.ExecResult(
                            action=action, ok=True, detail="ok", elapsed_ms=1, output=""))
    monkeypatch.setattr(agent_loop, "any_button_held", lambda: False)
    monkeypatch.setattr(agent_loop, "any_key_held", lambda: False)
    monkeypatch.setattr(agent_loop, "release_all", _noop)
    agent.start("Do the thing", MONITOR, model="clicker", max_steps=2,
                action_delay=0.0, settle_after_step=0.0, planner_model="")
    # Cap hit pauses for Continue — decline by stopping instead of granting more.
    deadline = time.time() + 5
    while time.time() < deadline and not any(e["type"] == "max_steps" for e in events):
        time.sleep(0.02)
    assert any(e["type"] == "max_steps" for e in events)
    assert not [event for event in events if event["type"] in {"verify_begin", "verified"}]
    agent.stop()
    agent._thread.join(timeout=10)
    assert final(events)["ok"] is False


def test_continue_after_max_steps_grants_another_batch(monkeypatch, tmp_path):
    desktop = FakeDesktop()
    replies = [
        {"thought": "working", "status": "continue",
         "actions": [{"type": "key", "key": "pagedown"}]},
        {"thought": "working", "status": "continue",
         "actions": [{"type": "key", "key": "pagedown"}]},
        {"thought": "finished", "status": "done", "message": "All done."},
    ]

    class Scripted(FakeModel):
        def chat_raw(self, system, messages, **kwargs):
            if "planner" not in system and "actually did" not in system:
                desktop.marks += 1
            return super().chat_raw(system, messages, **kwargs)

    model = Scripted(replies, verifier=[{"verdict": "pass", "reason": "ok"}])
    events = []
    agent = AgentLoop(events.append)
    monkeypatch.setattr(agent_loop, "DEBUG_ROOT", str(tmp_path / "runs"))
    monkeypatch.setattr(agent_loop.vlm, "chat_raw", model.chat_raw)
    monkeypatch.setattr(agent_loop.vlm, "take_last_usage", lambda: {"requests": 1, "model": "m"})
    monkeypatch.setattr(agent_loop, "capture", desktop.capture)
    monkeypatch.setattr(agent_loop, "execute",
                        lambda action, monitor, **kwargs: agent_loop.ExecResult(
                            action=action, ok=True, detail="ok", elapsed_ms=1, output=""))
    monkeypatch.setattr(agent_loop, "any_button_held", lambda: False)
    monkeypatch.setattr(agent_loop, "any_key_held", lambda: False)
    monkeypatch.setattr(agent_loop, "release_all", _noop)
    agent.start("Do the thing", MONITOR, model="clicker", max_steps=2,
                action_delay=0.0, settle_after_step=0.0, planner_model="")
    deadline = time.time() + 5
    while time.time() < deadline and not any(e["type"] == "max_steps" for e in events):
        time.sleep(0.02)
    assert any(e["type"] == "max_steps" for e in events)
    assert agent.is_awaiting_answer()
    assert agent.add_follow_up("Continue", extra_steps=5)
    agent._thread.join(timeout=15)
    assert final(events)["ok"] is True
    assert max(e.get("n", 0) for e in events if e["type"] == "step_begin") >= 3


def test_shell_steps_skip_the_next_screenshot_unless_requested(monkeypatch, tmp_path):
    model = FakeModel([
        {"thought": "run a command", "status": "continue", "need_screen": False,
         "actions": [{"type": "shell", "command": "Get-Date"}]},
        {"thought": "read the output", "status": "done", "need_screen": False,
         "message": "done from shell"},
    ], verifier=[{"verdict": "pass", "reason": "ok"}])
    captures = {"n": 0}
    desktop = FakeDesktop()

    def counting_capture(monitor):
        captures["n"] += 1
        return desktop.capture(monitor)

    events = []
    agent = AgentLoop(events.append)
    monkeypatch.setattr(agent_loop, "DEBUG_ROOT", str(tmp_path / "runs"))
    monkeypatch.setattr(agent_loop.vlm, "chat_raw", model.chat_raw)
    monkeypatch.setattr(agent_loop.vlm, "take_last_usage", lambda: {"requests": 1, "model": "m"})
    monkeypatch.setattr(agent_loop, "capture", counting_capture)
    monkeypatch.setattr(
        agent_loop, "execute",
        lambda action, monitor, **kwargs: agent_loop.ExecResult(
            action=action, ok=True, detail="ok", elapsed_ms=1, output="Monday"))
    monkeypatch.setattr(agent_loop, "any_button_held", lambda: False)
    monkeypatch.setattr(agent_loop, "any_key_held", lambda: False)
    monkeypatch.setattr(agent_loop, "release_all", _noop)
    agent.start("What day is it", MONITOR, model="clicker", max_steps=5,
                action_delay=0.0, settle_after_step=0.0, planner_model="",
                shell_enabled=True)
    agent._thread.join(timeout=30)
    # Step 1 capture + optional completion-check capture — not a capture per step.
    assert captures["n"] <= 2
    assert len([e for e in events if e["type"] == "screenshot"]) == 1


def test_saying_done_is_checked_and_can_be_rejected(monkeypatch, tmp_path):
    model = FakeModel(
        replies=[{"thought": "sent it", "status": "done", "message": "Sent."},
                 {"thought": "attaching the file", "status": "continue",
                  "actions": [{"type": "click", "x": 40, "y": 40}]},
                 {"thought": "now really sent", "status": "done", "message": "Sent for real."}],
        verifier=[{"verdict": "fail", "reason": "still a draft", "missing": ["press Send"]},
                  {"verdict": "pass", "reason": "the message is in Sent Items"}],
    )
    events = run_loop(model, FakeDesktop(), monkeypatch, tmp_path, max_steps=10)

    verdicts = [event["verdict"] for event in events if event["type"] == "verified"]
    ended = final(events)
    assert verdicts == ["fail", "pass"]
    assert ended["ok"] is True and ended["verified"] is True
    assert ended["steps"] > 1, "the rejected done must not end the run"


def test_finished_codex_run_carries_measured_plan_percentage(monkeypatch, tmp_path):
    from agent import codex_backend

    reset_at = int(time.time()) + 3600
    monkeypatch.setattr(codex_backend, "latest_plan_usage", lambda: {
        "used_percent": 10,
        "reset_at": reset_at,
        "window_minutes": 10080,
        "plan_type": "plus",
        "updated_at": int(time.time()),
    })
    usage = {
        "requests": 1,
        "model": "gpt-5.6-luna",
        "backend": "codex",
        "total_tokens": 1000,
        "plan_usage": {
            "used_percent": 11,
            "reset_at": reset_at,
            "window_minutes": 10080,
            "plan_type": "plus",
        },
    }
    model = FakeModel([{"thought": "done", "status": "done", "message": "Finished."}])

    events = run_loop(
        model, FakeDesktop(), monkeypatch, tmp_path,
        max_steps=2, planner="", backend="codex", usage=usage,
    )

    plan = final(events)["usage"]["plan_usage"]
    assert plan["measured"] is True
    assert plan["used_percent_delta"] == 1
    assert plan["end_used_percent"] == 11


def test_a_stubborn_claim_of_done_is_not_looped_forever(monkeypatch, tmp_path):
    model = FakeModel(
        replies=[{"thought": "done", "status": "done", "message": "Finished."}],
        verifier=[{"verdict": "fail", "reason": "nope", "missing": ["everything"]}] * 6,
    )
    events = run_loop(model, FakeDesktop(), monkeypatch, tmp_path, max_steps=20)

    ended = final(events)
    assert len([event for event in events if event["type"] == "verified"]) <= 4
    assert "completion check still disagrees" in ended["message"]


def test_the_task_and_todo_list_survive_a_long_run(monkeypatch, tmp_path):
    """History used to be trimmed to the last 16 messages, which threw away the
    plan exactly when a long run needed it."""
    desktop = FakeDesktop()

    class Progressing(FakeModel):
        def chat_raw(self, system, messages, **kwargs):
            if "planner" not in system and "actually did" not in system:
                desktop.marks = (desktop.marks + 1) % 7
            return super().chat_raw(system, messages, **kwargs)

    model = Progressing([{"thought": "working", "status": "continue",
                          "actions": [{"type": "click", "x": 30, "y": 30}]},
                         {"thought": "working", "status": "continue",
                          "actions": [{"type": "type", "text": "hello"}]}])
    run_loop(model, desktop, monkeypatch, tmp_path, max_steps=14)

    flat = json.dumps(model.step_requests[-1], default=str)
    assert "Do the thing" in flat, "the task fell out of the conversation"
    assert "TODO LIST" in flat, "the plan fell out of the conversation"
    assert "Open the app" in flat


def test_every_step_carries_the_real_date_and_time(monkeypatch, tmp_path):
    from datetime import datetime

    model = FakeModel([{"thought": "done", "status": "done", "message": "ok"}])
    run_loop(model, FakeDesktop(), monkeypatch, tmp_path, max_steps=3, planner="")

    today = datetime.now().strftime("%Y-%m-%d")
    step_request = json.dumps(model.step_requests[0], default=str)
    assert "CONTEXT:" in step_request
    assert today in step_request, "the model was not told today's date"
    assert today in model.systems[0], "the system prompt lost its clock"


def test_the_screen_not_moving_is_reported_back_to_the_model(monkeypatch, tmp_path):
    model = FakeModel([{"thought": "clicking", "status": "continue",
                        "actions": [{"type": "click", "x": 10, "y": 10}]}])
    run_loop(model, FakeDesktop(), monkeypatch, tmp_path, max_steps=4)

    flat = json.dumps(model.step_requests[-1], default=str)
    assert "screen did NOT change" in flat


def test_planner_off_never_reaches_the_api_as_a_model_name(monkeypatch, tmp_path):
    seen = []

    model = FakeModel([{"thought": "done", "status": "done", "message": "ok"}])
    original = model.chat_raw

    def spy(system, messages, **kwargs):
        seen.append(kwargs.get("model"))
        return original(system, messages, **kwargs)

    model.chat_raw = spy
    run_loop(model, FakeDesktop(), monkeypatch, tmp_path, max_steps=3, planner="off")

    assert "off" not in seen and seen, seen
