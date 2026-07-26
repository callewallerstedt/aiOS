"""The run must always reach an end.

A model call that never returns used to freeze a run outright: no error, no
`done`, nothing in the transcript after the screenshot, and a UI that spun
forever. These cover the ways out.
"""

import json
import threading
import time

import pytest

from test_agent_loop_guardrails import (  # noqa: F401 — reuses the fake desktop/model
    MONITOR,
    FakeDesktop,
    FakeModel,
    agent_loop,
    AgentLoop,
    final,
    run_loop,
    _noop,
)


def _wire(monkeypatch, tmp_path, chat_raw):
    monkeypatch.setattr(agent_loop, "DEBUG_ROOT", str(tmp_path / "runs"))
    monkeypatch.setattr(agent_loop.vlm, "chat_raw", chat_raw)
    monkeypatch.setattr(agent_loop.vlm, "take_last_usage", lambda: {"requests": 1, "model": "m"})
    monkeypatch.setattr(agent_loop, "capture", FakeDesktop().capture)
    monkeypatch.setattr(agent_loop, "execute", lambda action, monitor, **kw: agent_loop.ExecResult(
        action=action, ok=True, detail="ok", elapsed_ms=1, output=""))
    monkeypatch.setattr(agent_loop, "any_button_held", lambda: False)
    monkeypatch.setattr(agent_loop, "any_key_held", lambda: False)
    monkeypatch.setattr(agent_loop, "release_all", _noop)


def test_a_model_call_that_never_returns_does_not_freeze_the_run(monkeypatch, tmp_path):
    """The step is abandoned and the run carries on instead of hanging."""
    monkeypatch.setattr(agent_loop, "MODEL_CALL_TIMEOUT", 0.3)
    released = threading.Event()
    calls = []

    def chat_raw(system, messages, **kwargs):
        calls.append(system)
        if len(calls) == 1:
            released.wait(30)  # the hang: never answers in time
            return json.dumps({"status": "continue", "actions": []})
        return json.dumps({"thought": "recovered", "status": "done", "message": "ok"})

    _wire(monkeypatch, tmp_path, chat_raw)
    events = []
    agent = AgentLoop(events.append)
    agent.start("Do the thing", MONITOR, model="clicker", max_steps=4,
                action_delay=0.0, settle_after_step=0.0, planner_model="")
    agent._thread.join(timeout=30)
    released.set()

    assert not agent.is_running(), "the loop never finished"
    assert final(events)["ok"] is True, "the run should recover on the next step"
    # The abandoned step is reported, not swallowed.
    errors = [e for e in events if e["type"] == "step_end" and e["record"].get("error")]
    assert errors, "the timed-out step should be recorded as an error"
    assert "exceeded" in errors[0]["record"]["error"]


def test_a_wedged_run_is_ended_by_the_stall_watchdog(monkeypatch, tmp_path):
    """Nothing else got us out, so the watchdog reports a stuck run."""
    monkeypatch.setattr(agent_loop, "MODEL_CALL_TIMEOUT", 30.0)
    monkeypatch.setattr(agent_loop, "STALL_ABORT_SEC", 0.5)
    released = threading.Event()

    def chat_raw(system, messages, **kwargs):
        released.wait(30)
        return json.dumps({"status": "done", "message": "too late"})

    _wire(monkeypatch, tmp_path, chat_raw)
    events = []
    agent = AgentLoop(events.append)
    agent.start("Do the thing", MONITOR, model="clicker", max_steps=4,
                action_delay=0.0, settle_after_step=0.0, planner_model="")

    deadline = time.time() + 20
    while time.time() < deadline and not any(e["type"] == "done" for e in events):
        time.sleep(0.05)
    released.set()

    done = final(events)
    assert done["ok"] is False
    assert "stopped responding" in done["message"]
    # And the user can start another run rather than being locked out forever.
    assert not agent.is_running()


def test_only_one_done_reaches_the_ui(monkeypatch, tmp_path):
    """The watchdog and the loop can both decide it is over; the UI sees one end."""
    # Leave the model call wedged long enough that the stall watchdog ends the
    # run; releasing afterward must not produce a second `done`.
    monkeypatch.setattr(agent_loop, "STALL_ABORT_SEC", 0.4)
    monkeypatch.setattr(agent_loop, "MODEL_CALL_TIMEOUT", 30.0)
    released = threading.Event()

    def chat_raw(system, messages, **kwargs):
        released.wait(30)
        return json.dumps({"status": "done", "message": "late"})

    _wire(monkeypatch, tmp_path, chat_raw)
    events = []
    agent = AgentLoop(events.append)
    agent.start("Do the thing", MONITOR, model="clicker", max_steps=3,
                action_delay=0.0, settle_after_step=0.0, planner_model="")
    deadline = time.time() + 20
    while time.time() < deadline and not any(e["type"] == "done" for e in events):
        time.sleep(0.05)
    released.set()
    time.sleep(0.4)

    assert len([e for e in events if e["type"] == "done"]) == 1
    assert not agent.is_running()


def test_an_answer_that_arrives_first_still_wakes_the_ask(monkeypatch, tmp_path):
    """The reply landing before the loop armed its wait must not deadlock it.

    add_follow_up used to check `_awaiting_answer` after queueing, so an answer
    delivered in that window was stored and never woken — the run sat waiting
    with the reply already in hand.
    """
    asked = threading.Event()
    events = []
    calls = []

    def chat_raw(system, messages, **kwargs):
        calls.append(system)
        if len(calls) == 1:
            return json.dumps({"thought": "need input", "status": "ask",
                               "message": "which one?"})
        return json.dumps({"thought": "got it", "status": "done", "message": "ok"})

    _wire(monkeypatch, tmp_path, chat_raw)

    agent = AgentLoop(events.append)

    def on_event(event):
        events.append(event)
        # Answer from inside the emit, i.e. before `_wait_unpaused` is reached.
        if event.get("type") == "ask" and not asked.is_set():
            asked.set()
            agent.add_follow_up("the second one")

    agent.on_event = on_event
    agent.start("Do the thing", MONITOR, model="clicker", max_steps=4,
                action_delay=0.0, settle_after_step=0.0, planner_model="")
    agent._thread.join(timeout=15)

    assert asked.is_set(), "the run never asked"
    assert not agent.is_running(), "the loop deadlocked waiting for an answer it had"
    assert final(events)["ok"] is True


def test_a_superseded_run_goes_quiet(monkeypatch, tmp_path):
    """A run the watchdog abandoned must not emit into the run that replaced it."""
    monkeypatch.setattr(agent_loop, "STALL_ABORT_SEC", 0.4)
    monkeypatch.setattr(agent_loop, "MODEL_CALL_TIMEOUT", 30.0)
    released = threading.Event()

    def chat_raw(system, messages, **kwargs):
        released.wait(30)
        return json.dumps({"status": "done", "message": "zombie speaking"})

    _wire(monkeypatch, tmp_path, chat_raw)
    events = []
    agent = AgentLoop(events.append)
    agent.start("First", MONITOR, model="clicker", max_steps=3,
                action_delay=0.0, settle_after_step=0.0, planner_model="")

    deadline = time.time() + 20
    while time.time() < deadline and not any(e["type"] == "done" for e in events):
        time.sleep(0.05)
    assert not agent.is_running()

    agent._generation += 1  # stand in for the next run claiming the loop
    before = len(events)
    released.set()
    time.sleep(0.5)

    assert len(events) == before, "the abandoned run kept talking"
