"""Focused regressions for the Director's Linux operator input/progress loop."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from director.operator import atspi_click, loop, x11


def test_click_targets_are_marked_and_navigation_inputs_wait_a_full_second():
    assert loop.click_marker({"type": "click", "x": 40, "y": 50}) == {
        "x": 40, "y": 50, "kind": "click", "button": "left", "clicks": 1,
    }
    assert loop.click_marker({"type": "type", "x": 12, "y": 18, "text": "x"})[
        "kind"] == "type-focus"
    assert loop.click_marker({"type": "scroll", "x": 40, "y": 50, "dy": 4}) is None
    assert loop.post_action_settle_seconds([{"type": "click", "x": 1, "y": 2}]) == 1.0
    assert loop.post_action_settle_seconds([{"type": "key", "key": "Return"}]) == 1.0
    assert loop.post_action_settle_seconds([{"type": "hotkey", "keys": ["ctrl", "enter"]}]) == 1.0
    assert loop.post_action_settle_seconds([{"type": "open_url", "url": "https://example.com"}]) == 1.0
    assert loop.post_action_settle_seconds([{"type": "scroll", "dy": 4}]) == 0.25
    assert loop.post_action_settle_seconds([
        {"type": "click", "x": 1, "y": 2}, {"type": "key", "key": "Enter"},
    ]) == 0.25


def test_alternating_desktop_cycle_stops_before_a_fifth_action(monkeypatch):
    """A -> B -> A -> B was the production loop that escaped same-screen guards."""
    events: list[tuple[str, dict]] = []
    actions: list[dict] = []
    model_calls = 0
    captures = 0

    async def ready(*_args, **_kwargs):
        return {"ready": True, "display": ":0"}

    async def capture(*_args, **_kwargs):
        nonlocal captures
        captures += 1
        return b"A" if captures % 2 else b"B"

    async def complete(**_kwargs):
        nonlocal model_calls
        model_calls += 1
        # Keep each click distinct so the cycle guard, rather than the
        # same-action guard, is what ends this run.
        coordinate = 10 + model_calls * 10
        return {
            "tool_calls": [{
                "name": "click",
                "arguments": '{"x": %d, "y": 50}' % coordinate,
            }],
            "reasoning": "try another visible route",
            "model": "test",
        }

    async def execute(action, _settings):
        actions.append(action)
        return loop.ActionResult("click issued")

    async def emit(kind, payload):
        events.append((kind, payload))

    async def no_controls(*_args, **_kwargs):
        return []

    async def browser_window(*_args, **_kwargs):
        return ["Browser"]

    async def release_all(*_args, **_kwargs):
        return None

    monkeypatch.setattr(loop.display_mod, "ensure_running", ready)
    monkeypatch.setattr(loop.x11, "screen_size", lambda *_args, **_kwargs:
                        asyncio.sleep(0, result=(100, 100)))
    monkeypatch.setattr(loop.x11, "capture", capture)
    monkeypatch.setattr(loop.x11, "encode_jpeg",
                        lambda data: (f"data:image/png;base64,{data.decode()}", 100, 100))
    monkeypatch.setattr(loop.x11, "image_signature",
                        lambda data: bytes([0 if data == b"A" else 15]) * (64 * 36))
    monkeypatch.setattr(loop.x11, "window_list", browser_window)
    monkeypatch.setattr(loop.x11, "accessible_controls", no_controls)
    monkeypatch.setattr(loop.x11, "release_all", release_all)
    monkeypatch.setattr(loop.models, "complete", complete)
    monkeypatch.setattr(loop, "execute", execute)
    monkeypatch.setattr(loop, "background", lambda: "")
    monkeypatch.setattr(loop, "POST_ACTION_SETTLE", 0)

    result = asyncio.run(loop.run_task(
        "leave the alternating screens", emit=emit,
        settings={"operator": {
            "review_every": 30,
            "max_no_postcondition_actions": 20,
            "max_distinct_actions_on_screen": 30,
        }},
    ))

    assert result["status"] == "stopped"
    assert result["steps"] == 5
    assert "2-state cycle" in result["summary"]
    assert model_calls == len(actions) == 4
    assert events[-1][0] == "operator.stuck"
    assert events[-1][1]["cycle"] == [1, 2]


def test_typing_progress_requires_a_semantic_character_count_change():
    action = {"type": "type", "x": 50, "y": 30, "text": "secret"}
    before = {
        "id": "field-1", "role": "entry", "x": 0, "y": 0,
        "width": 100, "height": 60, "focused": False, "text_length": 2,
    }

    focus_only = [{**before, "focused": True}]
    length_changed = [{**before, "focused": True, "text_length": 8}]

    assert loop.typed_text_postcondition(action, before, focus_only) is False
    assert loop.typed_text_postcondition(action, before, length_changed) is True
    assert loop.typed_text_postcondition(action, None, length_changed) is False
    same_length_replace = {**action, "replace": True, "text": "xx"}
    assert loop.typed_text_postcondition(
        same_length_replace, before, [{**before, "text_length": 2}]) is False
    assert "secret" not in str(length_changed)


def test_done_is_refused_after_the_previous_action_had_no_postcondition(monkeypatch):
    events: list[tuple[str, dict]] = []
    calls = 0

    async def ready(*_args, **_kwargs):
        return {"ready": True, "display": ":0"}

    async def complete(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"tool_calls": [{"name": "click",
                                     "arguments": '{"x":20,"y":20}'}]}
        return {"tool_calls": [{"name": "finish",
                                 "arguments": '{"status":"done","message":"done"}'}]}

    async def emit(kind, payload):
        events.append((kind, payload))

    async def empty(*_args, **_kwargs):
        return []

    monkeypatch.setattr(loop.display_mod, "ensure_running", ready)
    monkeypatch.setattr(loop.x11, "screen_size", lambda *_args, **_kwargs:
                        asyncio.sleep(0, result=(100, 100)))
    monkeypatch.setattr(loop.x11, "capture", lambda *_args, **_kwargs:
                        asyncio.sleep(0, result=b"same"))
    monkeypatch.setattr(loop.x11, "encode_jpeg",
                        lambda _data: ("data:image/png;base64,x", 100, 100))
    monkeypatch.setattr(loop.x11, "image_signature", lambda _data: bytes([4]) * (64 * 36))
    monkeypatch.setattr(loop.x11, "window_list", empty)
    monkeypatch.setattr(loop.x11, "accessible_controls", empty)
    monkeypatch.setattr(loop.x11, "release_all", empty)
    monkeypatch.setattr(loop.models, "complete", complete)
    monkeypatch.setattr(loop, "execute", lambda *_args, **_kwargs:
                        asyncio.sleep(0, result=loop.ActionResult("click issued")))
    monkeypatch.setattr(loop, "background", lambda: "")
    monkeypatch.setattr(loop, "POST_ACTION_SETTLE", 0)

    result = asyncio.run(loop.run_task(
        "click and verify", emit=emit,
        settings={"operator": {"max_no_postcondition_actions": 10}},
    ))

    assert result["status"] == "stopped"
    assert result["steps"] == 2
    assert "no verified postcondition" in result["summary"]
    assert events[-1][0] == "operator.stuck"


def test_atspi_delivery_is_not_mistaken_for_a_click_postcondition(monkeypatch):
    async def semantic_click(*_args, **_kwargs):
        return {"handled": True, "semantic": True, "window": "200"}

    monkeypatch.setattr(loop.x11, "click", semantic_click)

    result = asyncio.run(loop.execute({"type": "click", "x": 20, "y": 30}, {}))

    assert result.ok and result.issued
    assert result.verified is False


def test_focus_does_not_accept_a_different_window_with_the_same_pid(monkeypatch):
    calls: list[tuple[str, ...]] = []

    async def fake_xdotool(*args, settings=None):
        del settings
        calls.append(tuple(args))
        if args == ("getmouselocation", "--shell"):
            return 0, "X=40\nY=50\nWINDOW=200"
        if args in {("getactivewindow",), ("getwindowfocus",)}:
            return 0, "100"
        if args[0] == "getwindowpid":
            return 0, "4242"
        if args[:2] in {("windowactivate", "--sync"),
                        ("windowfocus", "--sync")}:
            return 1, "activation denied"
        raise AssertionError(args)

    monkeypatch.setattr(x11, "xdotool", fake_xdotool)

    assert asyncio.run(x11.window_pid("100", {})) == "4242"
    assert asyncio.run(x11.window_pid("200", {})) == "4242"
    with pytest.raises(RuntimeError, match="could not focus pointer window 200"):
        asyncio.run(x11.focus_pointer_window({}))

    assert ("windowactivate", "--sync", "200") in calls
    assert ("windowfocus", "--sync", "200") in calls


def test_click_targets_pointer_window_when_popup_rejects_activation(monkeypatch):
    calls: list[tuple[str, ...]] = []

    async def move(_x, _y, _settings=None):
        return None

    async def pointer_window(_settings=None):
        return "200"

    async def focus(_settings=None):
        raise RuntimeError("popup rejected activation")

    async def window_name(_window, _settings=None):
        return "Saved login"

    async def accessible(_x, _y, _title="", _settings=None):
        return {"handled": False}

    async def checked(*args, settings=None):
        del settings
        calls.append(tuple(args))

    monkeypatch.setattr(x11, "move", move)
    monkeypatch.setattr(x11, "pointer_window", pointer_window)
    monkeypatch.setattr(x11, "focus_pointer_window", focus)
    monkeypatch.setattr(x11, "window_name", window_name)
    monkeypatch.setattr(x11, "accessible_click", accessible)
    monkeypatch.setattr(x11, "_checked_xdotool", checked)

    result = asyncio.run(x11.click(40, 50, settings={}))

    assert result == {"handled": True, "semantic": False, "window": "200"}
    assert calls == [("click", "--window", "200", "--repeat", "1", "--delay", "80", "1")]


def test_mouse_down_targets_pointer_window_when_activation_fails(monkeypatch):
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(x11, "move", lambda *_args, **_kwargs: asyncio.sleep(0))
    monkeypatch.setattr(x11, "pointer_window", lambda *_args, **_kwargs:
                        asyncio.sleep(0, result="200"))

    async def focus(*_args, **_kwargs):
        raise RuntimeError("popup rejected activation")

    async def checked(*args, settings=None):
        del settings
        calls.append(tuple(args))

    monkeypatch.setattr(x11, "focus_pointer_window", focus)
    monkeypatch.setattr(x11, "_checked_xdotool", checked)

    target = asyncio.run(x11.mouse_down(20, 30, settings={}))

    assert target == "200"
    assert calls == [("mousedown", "--window", "200", "1")]


@pytest.mark.parametrize("gesture", ["drag", "stroke"])
def test_failed_gesture_still_releases_the_mouse_button(monkeypatch, gesture):
    calls: list[tuple] = []

    async def mouse_down(*args, **_kwargs):
        calls.append(("down", *args))
        return "200"

    async def move(*args, **_kwargs):
        calls.append(("move", *args))
        raise RuntimeError("pointer movement failed")

    async def mouse_up(*args, **kwargs):
        calls.append(("up", *args, kwargs.get("target_window")))

    monkeypatch.setattr(x11, "mouse_down", mouse_down)
    monkeypatch.setattr(x11, "move", move)
    monkeypatch.setattr(x11, "mouse_up", mouse_up)

    with pytest.raises(RuntimeError, match="pointer movement failed"):
        if gesture == "drag":
            asyncio.run(x11.drag((10, 20), (50, 60), steps=2, settings={}))
        else:
            asyncio.run(x11.stroke([[10, 20], [50, 60]], settings={}))

    assert calls[-1] == ("up", None, None, "left", {}, "200")


def test_clipboard_paste_fails_closed_when_no_selection_request_arrives(monkeypatch):
    seen = {}

    class Stdout:
        def __init__(self):
            self.lines = asyncio.Queue()

        async def readline(self):
            return await self.lines.get()

        def feed(self, line):
            self.lines.put_nowait(line)

    class Stdin:
        def write(self, _data):
            return None

        async def drain(self):
            return None

        def close(self):
            return None

    class Proc:
        def __init__(self):
            self.stdin = Stdin()
            self.stdout = Stdout()
            self.returncode = None
            self.done = asyncio.Event()

        async def wait(self):
            await self.done.wait()
            return self.returncode

        def terminate(self):
            seen["terminated"] = True
            self.returncode = -15
            self.stdout.feed(b"")
            self.done.set()

        def kill(self):
            self.terminate()

    async def create(*_args, **_kwargs):
        proc = Proc()
        proc.stdout.feed(b"  Waiting for selection request number 1\n")
        return proc

    monkeypatch.setattr(x11.shutil, "which", lambda _name: "/usr/bin/xclip")
    monkeypatch.setattr(x11.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(x11, "focus_pointer_window", lambda *_args, **_kwargs:
                        asyncio.sleep(0, result="200"))
    monkeypatch.setattr(x11, "hotkey", lambda *_args, **_kwargs: asyncio.sleep(0))
    monkeypatch.setattr(x11, "CLIPBOARD_REQUEST_TIMEOUT", 0.01)
    monkeypatch.setattr(x11, "CLIPBOARD_OWNER_QUIET_PERIOD", 0.001)
    monkeypatch.setattr(x11, "CLIPBOARD_OWNER_SETTLE_TIMEOUT", 0.005)

    with pytest.raises(RuntimeError, match="did not request the clipboard paste"):
        asyncio.run(x11.type_text("hello", {}))

    assert seen["terminated"] is True


def test_clipboard_manager_request_is_not_mistaken_for_target_paste(monkeypatch):
    seen = {}

    class Stdin:
        def write(self, _data):
            return None

        async def drain(self):
            return None

        def close(self):
            return None

    class Stdout:
        def __init__(self):
            self.lines = asyncio.Queue()

        async def readline(self):
            return await self.lines.get()

        def feed(self, line):
            self.lines.put_nowait(line)

    class Proc:
        def __init__(self):
            self.stdin = Stdin()
            self.stdout = Stdout()
            self.returncode = None
            self.done = asyncio.Event()

        async def wait(self):
            await self.done.wait()
            return self.returncode

        def terminate(self):
            self.returncode = -15
            self.stdout.feed(b"")
            self.done.set()

        def kill(self):
            self.terminate()

    async def create(*_args, **_kwargs):
        proc = Proc()
        seen["proc"] = proc
        # Request 1 is GNOME persisting the new clipboard selection before the
        # focused application receives Ctrl+V.
        proc.stdout.feed(b"  Waiting for selection request number 1\n")
        proc.stdout.feed(b"  Waiting for selection request number 2\n")
        return proc

    async def hotkey(*_args, **_kwargs):
        seen["hotkey_baseline_reached"] = True
        seen["proc"].stdout.feed(
            b"  Waiting for selection request number 3\n")

    monkeypatch.setattr(x11.shutil, "which", lambda _name: "/usr/bin/xclip")
    monkeypatch.setattr(x11.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(x11, "focus_pointer_window", lambda *_args, **_kwargs:
                        asyncio.sleep(0, result="200"))
    monkeypatch.setattr(x11, "hotkey", hotkey)
    monkeypatch.setattr(x11, "CLIPBOARD_OWNER_QUIET_PERIOD", 0.001)

    asyncio.run(x11.type_text("hello", {}))

    assert seen["hotkey_baseline_reached"] is True


def test_atspi_focus_only_widget_is_not_reported_as_a_click():
    roles = SimpleNamespace(
        ROLE_ALERT=1, ROLE_DIALOG=2, ROLE_CHECK_BOX=3, ROLE_COMBO_BOX=4,
        ROLE_LINK=5, ROLE_LIST_BOX=6, ROLE_MENU_ITEM=7, ROLE_PAGE_TAB=8,
        ROLE_PUSH_BUTTON=9, ROLE_RADIO_BUTTON=10, ROLE_SLIDER=11,
        ROLE_SPIN_BUTTON=12, ROLE_TOGGLE_BUTTON=13, ROLE_ENTRY=14,
    )

    class FocusOnlyEntry:
        def __iter__(self):
            return iter(())

        def getRole(self):
            return roles.ROLE_ENTRY

        def queryAction(self):
            raise RuntimeError("there is no semantic click action")

    assert atspi_click._deepest_action(
        FocusOnlyEntry(), 10, 20, 0, roles) is None


@pytest.fixture()
def director_store(tmp_path, monkeypatch):
    monkeypatch.setenv("AIOS_DIRECTOR_HOME", str(tmp_path / "director-home"))
    from director import config, store

    store.close()
    config.load_settings(refresh=True)
    yield store
    store.close()


def test_cancelling_operator_job_emits_terminal_event_before_job_finished(director_store):
    from director import runtime

    async def scenario():
        agent = director_store.create_agent(name="Operator owner")
        thread = director_store.create_thread(agent["id"])
        job = director_store.create_job(
            kind="operator", request={"task": "browse"},
            thread_id=thread["id"], agent_id=agent["id"], status="running")
        hub = runtime.Runtime()

        async def no_report(*_args, **_kwargs):
            return None

        hub._report_job = no_report
        started = asyncio.Event()

        async def work():
            started.set()
            await asyncio.Event().wait()

        hub.start_job(job, work)
        await started.wait()
        await hub.stop_job(job["id"])
        return job, director_store.list_events(thread_id=thread["id"], limit=20)

    job, events = asyncio.run(scenario())
    kinds = [event["kind"] for event in events]

    assert kinds[-2:] == ["operator.stopped", "job.finished"]
    stopped = events[-2]
    assert stopped["payload"]["job_id"] == job["id"]
    assert director_store.get_job(job["id"])["status"] == "stopped"


def test_operator_tool_start_is_typed_before_the_job_result(director_store, monkeypatch):
    from director import runtime, tools

    class FakeTool:
        destructive = False
        approval_summary = None

        async def run(self, _ctx, **_args):
            return tools.ToolResult(output="started")

    async def scenario():
        agent = director_store.create_agent(name="Director")
        thread = director_store.create_thread(agent["id"])
        hub = runtime.Runtime()
        monkeypatch.setattr(runtime.tools_mod, "get", lambda _name: FakeTool())
        ctx = SimpleNamespace(settings={}, source="")
        await hub._run_tool_call(
            thread["id"], agent, ctx,
            {"name": "operator", "call_id": "call-1",
             "arguments": '{"task":"check Spotify"}'})
        return director_store.list_events(thread_id=thread["id"], limit=20)

    events = asyncio.run(scenario())
    started = next(event for event in events if event["kind"] == "tool.start")

    assert started["payload"]["card"]["job_kind"] == "operator"
    assert started["payload"]["card"]["preview"] == "check Spotify"


def test_restart_persists_operator_terminal_event_and_returns_it_for_reporting(director_store):
    from director import server

    agent = director_store.create_agent(name="Director")
    thread = director_store.create_thread(agent["id"])
    job = director_store.create_job(
        kind="operator", request={"task": "unfinished"},
        thread_id=thread["id"], agent_id=agent["id"], status="running")

    released = server._release_orphaned_jobs()

    assert [row[0]["id"] for row in released] == [job["id"]]
    assert director_store.get_job(job["id"])["status"] == "stopped"
    events = director_store.list_events(thread_id=thread["id"], limit=20)
    assert [event["kind"] for event in events] == ["operator.stopped", "job.finished"]
    assert events[0]["payload"]["job_id"] == job["id"]


def test_thread_reload_rehydrates_original_operator_card_terminal_status(director_store):
    from director import server

    agent = director_store.create_agent(name="Director")
    thread = director_store.create_thread(agent["id"])
    job = director_store.create_job(
        kind="operator", request={"task": "browse"},
        thread_id=thread["id"], agent_id=agent["id"], status="running")
    director_store.add_message(thread["id"], "tool_result", "", {
        "call_id": "call-1", "name": "operator", "output": "started",
        "card": {"job_id": job["id"], "job_kind": "operator",
                 "meta": "running", "tone": "accent"},
    })
    director_store.update_job(
        job["id"], status="stopped",
        result={"status": "stopped", "summary": "Director restarted"})

    payload = server._thread_payload(thread["id"])
    card = next(message["meta"]["card"] for message in payload["messages"]
                if message["role"] == "tool_result")

    assert card["job_kind"] == "operator"
    assert card["job_status"] == "stopped"
    assert card["meta"] == "stopped"
    assert card["tone"] == "danger"
