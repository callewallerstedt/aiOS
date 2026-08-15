"""One screen, one run, steerable while it goes.

On 2026-08-15 two operator jobs were dispatched three minutes apart and both
sat at "running" — they were driving the same mouse on the same display,
clicking through each other. Nothing stopped the second from starting, and
there was no way to correct the first without killing it.
"""
import asyncio

import pytest


class FakeHub:
    def __init__(self, live=None):
        self._live = live or []
        self.notes = {}
        self.started = []

    def live_jobs(self, kind=""):
        return [j for j in self._live if not kind or j.get("kind") == kind]

    def note_job(self, job_id, text):
        if job_id not in {j["id"] for j in self._live}:
            return False
        self.notes.setdefault(job_id, []).append(text)
        return True

    def take_job_notes(self, job_id):
        return self.notes.pop(job_id, [])

    def start_job(self, job, factory):
        self.started.append(job)


def _ctx(hub, settings=None):
    events = []

    class Context:
        agent = {"id": "agt_x"}
        thread_id = "thr_x"
        depth = 0
        source = ""

    ctx = Context()
    ctx.settings = settings or {"operator": {"review_every": 30}}
    ctx.hub = hub
    ctx.emit = lambda kind, payload: events.append((kind, payload)) or asyncio.sleep(0)
    ctx.ask_user = None
    ctx.request_approval = None
    ctx.cancel = asyncio.Event()
    ctx.events = events
    return ctx


@pytest.fixture()
def director(tmp_path, monkeypatch):
    monkeypatch.setenv("AIOS_DIRECTOR_HOME", str(tmp_path / "home"))
    import director.config as config
    import director.store as store
    store.close()
    config.load_settings(refresh=True)
    yield store
    store.close()


# ---------------- one screen ----------------

def test_a_second_operator_run_is_refused_while_one_is_going(director):
    from director.tools import operator

    hub = FakeHub(live=[{"id": "job_1", "kind": "operator",
                         "request": {"task": "log in to Spotify"}}])
    result = asyncio.run(operator.operator(_ctx(hub), task="check the artist page"))
    assert result.error
    assert "already running job job_1" in result.error
    assert "operator_say" in result.error
    assert not hub.started, "nothing should have been dispatched"


def test_the_refusal_names_what_is_running(director):
    from director.tools import operator

    hub = FakeHub(live=[{"id": "job_1", "kind": "operator",
                         "request": {"task": "log in to Spotify"}}])
    result = asyncio.run(operator.operator(_ctx(hub), task="something else"))
    assert "log in to Spotify" in result.error


def test_a_code_job_does_not_block_the_screen(director):
    """Only the operator owns the mouse; a CODE session is unrelated."""
    from director.tools import operator

    hub = FakeHub(live=[{"id": "job_c", "kind": "code", "request": {"task": "fix a bug"}}])
    result = asyncio.run(operator.operator(_ctx(hub), task="open the page"))
    assert not result.error and hub.started


def test_the_first_run_starts_normally(director):
    from director.tools import operator

    hub = FakeHub()
    result = asyncio.run(operator.operator(_ctx(hub), task="open the page"))
    assert not result.error
    assert hub.started and hub.started[0]["kind"] == "operator"
    assert "operator_say" in result.output


# ---------------- steering it ----------------

def test_a_follow_up_reaches_the_running_run(director):
    from director.tools import operator

    hub = FakeHub(live=[{"id": "job_1", "kind": "operator", "request": {"task": "t"}}])
    result = asyncio.run(operator.operator_say(_ctx(hub), text="use the WALLERSTEDT artist"))
    assert not result.error
    assert hub.notes["job_1"] == ["use the WALLERSTEDT artist"]


def test_a_follow_up_finds_the_run_without_being_given_an_id(director):
    from director.tools import operator

    hub = FakeHub(live=[{"id": "job_9", "kind": "operator", "request": {"task": "t"}}])
    asyncio.run(operator.operator_say(_ctx(hub), text="scroll down first"))
    assert hub.notes["job_9"] == ["scroll down first"]


def test_a_follow_up_with_nothing_running_says_so(director):
    from director.tools import operator

    result = asyncio.run(operator.operator_say(_ctx(FakeHub()), text="hello"))
    assert result.error and "no operator run is going" in result.error


def test_an_empty_follow_up_is_refused(director):
    from director.tools import operator

    hub = FakeHub(live=[{"id": "job_1", "kind": "operator", "request": {"task": "t"}}])
    result = asyncio.run(operator.operator_say(_ctx(hub), text="   "))
    assert result.error


# ---------------- the runtime side ----------------

def test_live_jobs_ignores_rows_left_running_by_a_restart(director):
    """Two of these were sitting at "running" with nothing behind them. Treating
    a dead row as a live run would block the screen forever."""
    from director import runtime

    hub = runtime.Runtime()
    director.create_job(kind="operator", request={"task": "ghost"}, thread_id="t",
                        agent_id="a", status="running")
    assert hub.live_jobs("operator") == []


def test_live_jobs_sees_a_real_one(director):
    from director import runtime

    async def scenario():
        hub = runtime.Runtime()
        job = director.create_job(kind="operator", request={"task": "real"},
                                  thread_id="t", agent_id="a", status="running")
        hub._jobs[job["id"]] = asyncio.create_task(asyncio.sleep(0.2))
        live = hub.live_jobs("operator")
        hub._jobs[job["id"]].cancel()
        return live

    live = asyncio.run(scenario())
    assert len(live) == 1 and live[0]["request"]["task"] == "real"


def test_notes_are_delivered_once_and_only_to_a_live_job(director):
    from director import runtime

    async def scenario():
        hub = runtime.Runtime()
        hub._jobs["job_1"] = asyncio.create_task(asyncio.sleep(0.2))
        assert hub.note_job("job_1", "first") is True
        assert hub.note_job("job_1", "second") is True
        assert hub.note_job("job_gone", "nope") is False
        first = hub.take_job_notes("job_1")
        second = hub.take_job_notes("job_1")
        hub._jobs["job_1"].cancel()
        return first, second

    first, second = asyncio.run(scenario())
    assert first == ["first", "second"]
    assert second == [], "a note must not be replayed on the next step"


# ---------------- the loop reads them ----------------

def test_the_prompt_puts_a_follow_up_last_and_calls_it_an_instruction():
    from director.operator import prompts

    message = prompts.task_message("original task", 1280, 720, "history",
                                   ["Chrome"], "", notes=["actually use the other one"],
                                   step=4)
    assert "CALLE JUST SAID" in message
    assert message.index("CALLE JUST SAID") > message.index("HISTORY")
    assert "actually use the other one" in message


def test_the_prompt_is_unchanged_when_nothing_was_added():
    from director.operator import prompts

    assert "CALLE JUST SAID" not in prompts.task_message("t", 1280, 720, "h")


def test_the_operator_is_told_to_act_on_the_newest_instruction():
    from director.operator import prompts

    assert "CALLE JUST SAID" in prompts.SYSTEM_PROMPT
    assert "corrects or replaces" in prompts.SYSTEM_PROMPT


def test_the_operator_is_told_to_reason_before_it_clicks():
    from director.operator import prompts

    for phrase in ("Where am I?", "What am I aiming at?", "How will I know it worked?"):
        assert phrase in prompts.SYSTEM_PROMPT
    assert "Clicking to see what happens" in prompts.SYSTEM_PROMPT


def test_done_requires_seeing_the_result():
    from director.operator import prompts

    assert "Nothing is done because you clicked the button" in prompts.SYSTEM_PROMPT


# ---------------- reach ----------------

def test_the_operator_can_drag_and_replace_a_field():
    """The loop could always drag; the model had no way to ask for one, which
    put sliders, reordering and canvas work out of reach."""
    from director.operator import loop, prompts

    names = {tool["name"] for tool in prompts.ACTION_TOOLS}
    assert {"drag", "select_all_text"} <= names
    assert loop.TOOL_ACTIONS["drag"] == "drag"
    assert loop.TOOL_ACTIONS["select_all_text"] == "select_all"


def test_a_drag_call_becomes_a_drag_action():
    from director.operator import loop

    decision = loop.tool_decision({"tool_calls": [
        {"name": "drag", "arguments": '{"from": [10, 20], "to": [30, 40]}'}]})
    assert decision["actions"] == [{"type": "drag", "from": [10, 20], "to": [30, 40]}]


def test_every_operator_tool_the_model_can_call_is_executable():
    from director.operator import loop, prompts

    for tool in prompts.ACTION_TOOLS:
        name = tool["name"]
        if name == "finish":
            continue
        assert name in loop.TOOL_ACTIONS, f"{name} has no action mapping"


def test_agents_have_the_operator_and_machine_reach():
    from director import agents, tools

    tools.load_all()
    for name in ("operator_say", "machine_shell", "machine_read"):
        assert name in agents.DIRECTOR_TOOLS
    assert not tools.missing(agents.DIRECTOR_TOOLS)


def test_running_a_command_on_the_windows_box_needs_approval():
    from director import tools

    tools.load_all()
    assert tools.get("machine_shell").destructive is True
    assert tools.get("machine_read").destructive is False


def test_agents_are_told_there_is_only_one_screen():
    from director import agents

    assert "one** screen" in agents.DIRECTOR_PROMPT
    assert "operator_say" in agents.DIRECTOR_PROMPT
