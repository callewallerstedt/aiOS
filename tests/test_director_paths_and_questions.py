"""Director finding its way around, and never leaving a CODE session hanging.

Both of these are regressions from one live session on 2026-08-15:

  * Two CODE jobs died on "no such project directory" because the agent
    invented the project names `aiOS Director` and `aiOS` instead of looking
    for the folder. Calle had to type `C:\\aiOS` himself.
  * A third session asked "How should I scope the commit before pushing to
    main?" and sat waiting. The question streamed to the phone as a raw event,
    the agent was never woken, and the job showed "running" for hours.

The dispatch bug in the same session — a job that failed instantly while the
tool had already reported "dispatched" — is pinned here too.
"""
import asyncio

import pytest


# ---------------- looking around the Windows filesystem ----------------

@pytest.fixture()
def tree(tmp_path):
    """A little pretend Windows disk with one real repo in it."""
    (tmp_path / "aiOS" / ".git").mkdir(parents=True)
    (tmp_path / "aiOS" / "director").mkdir()
    (tmp_path / "Downloads" / "aios-backup").mkdir(parents=True)
    (tmp_path / "Downloads" / "node_modules" / "aios-thing").mkdir(parents=True)
    (tmp_path / "empty").mkdir()
    return tmp_path


def test_find_paths_locates_a_repo_by_bare_name(tree):
    import director_client

    hits = director_client.find_paths("aios", [str(tree)])
    paths = [hit["path"] for hit in hits]
    assert str(tree / "aiOS") in paths


def test_find_paths_ranks_a_real_project_first(tree):
    import director_client

    hits = director_client.find_paths("aios", [str(tree)])
    assert hits[0]["path"] == str(tree / "aiOS")
    assert hits[0]["project"] is True


def test_find_paths_never_walks_into_node_modules(tree):
    import director_client

    hits = director_client.find_paths("aios", [str(tree)])
    assert not any("node_modules" in hit["path"] for hit in hits)


def test_resolve_project_takes_an_absolute_path_as_is(tree):
    import director_client

    resolved = director_client.resolve_project(str(tree / "aiOS"))
    assert resolved["ok"] and resolved["path"] == str(tree / "aiOS")


def test_resolve_project_finds_a_bare_name(tree, monkeypatch):
    import director_client

    monkeypatch.setattr(director_client, "search_roots", lambda: [tree])
    resolved = director_client.resolve_project("aiOS")
    assert resolved["ok"] and resolved["path"] == str(tree / "aiOS")


def test_resolve_project_recovers_from_a_label(tree, monkeypatch):
    """"aiOS Director" is what Calle calls the app, not a folder on disk."""
    import director_client

    monkeypatch.setattr(director_client, "search_roots", lambda: [tree])
    resolved = director_client.resolve_project("aiOS Director")
    assert resolved["ok"] and resolved["path"] == str(tree / "aiOS")


def test_resolve_project_says_so_when_there_is_nothing(tree, monkeypatch):
    import director_client

    monkeypatch.setattr(director_client, "search_roots", lambda: [tree])
    resolved = director_client.resolve_project("nothing-like-this")
    assert resolved["ok"] is False and resolved["candidates"] == []


# ---------------- the tools that reach the machine ----------------

class FakeHub:
    """Stands in for the server hub: one online machine, scripted answers."""

    def __init__(self, answers=None, machines=None):
        self.answers = answers or {}
        self.calls = []
        self.jobs = []
        self._machines = machines if machines is not None else [
            {"id": "mch_1", "name": "calle-windows", "platform": "windows",
             "online": True, "caps": {"code": True, "files": True}}]

    def online_machines(self):
        return list(self._machines)

    async def call_machine(self, machine_id, action, payload, timeout=30.0):
        self.calls.append((action, payload))
        answer = self.answers.get(action, {"ok": True})
        return answer(payload) if callable(answer) else answer

    def start_job(self, job, factory):
        self.jobs.append(job)


def _ctx(hub, **kw):
    class Context:
        agent = {"id": "agt_x"}
        thread_id = "thr_x"
        settings = {}
        depth = 0
        source = ""

    ctx = Context()
    ctx.hub = hub
    ctx.emit = kw.get("emit") or (lambda kind, payload: asyncio.sleep(0))
    ctx.ask_user = kw.get("ask_user")
    ctx.request_approval = kw.get("request_approval")
    ctx.cancel = asyncio.Event()
    return ctx


def test_machine_find_returns_absolute_paths():
    from director.tools import machine

    hub = FakeHub({"find_paths": {"ok": True, "matches": [
        {"path": "C:\\aiOS", "name": "aiOS", "project": True}]}})
    result = asyncio.run(machine.machine_find(_ctx(hub), name="aiOS"))
    assert "C:\\aiOS" in result.output
    assert hub.calls[0][0] == "find_paths"


def test_machine_find_is_honest_about_no_match():
    from director.tools import machine

    hub = FakeHub({"find_paths": {"ok": True, "matches": []}})
    result = asyncio.run(machine.machine_find(_ctx(hub), name="ghost"))
    assert "nothing matching" in result.output


def test_machine_dirs_lists_the_roots_when_no_path_is_given():
    from director.tools import machine

    hub = FakeHub({"list_dir": {"ok": True, "path": "", "entries": [
        {"name": "C:\\", "path": "C:\\", "dir": True}]}})
    asyncio.run(machine.machine_dirs(_ctx(hub)))
    assert hub.calls[0][1]["path"] == ""


def test_machine_tools_say_when_nothing_is_paired():
    from director.tools import machine

    hub = FakeHub(machines=[])
    result = asyncio.run(machine.machine_find(_ctx(hub), name="aiOS"))
    assert result.error and "no machine" in result.error


def test_every_agent_can_look_at_the_machine():
    from director import agents, tools

    tools.load_all()
    assert "machine_find" in agents.DIRECTOR_TOOLS
    assert "machine_dirs" in agents.DIRECTOR_TOOLS
    assert not tools.missing(agents.DIRECTOR_TOOLS)


# ---------------- dispatch that cannot lie ----------------

@pytest.fixture()
def director(tmp_path, monkeypatch):
    monkeypatch.setenv("AIOS_DIRECTOR_HOME", str(tmp_path / "home"))
    import director.config as config
    import director.store as store
    store.close()
    config.load_settings(refresh=True)
    yield store
    store.close()


def test_a_bad_project_is_refused_before_anything_is_dispatched(director):
    from director.tools import code

    hub = FakeHub({"resolve_project": {"ok": False,
                                       "error": "no directory matching 'aiOS Director'"}})
    result = asyncio.run(code.code_session(
        _ctx(hub), task="fix the header", project="aiOS Director"))
    assert result.error
    assert "machine_find" in result.error
    assert not hub.jobs
    assert not any(action == "code.start" for action, _ in hub.calls)


def test_a_resolvable_project_is_dispatched_with_the_real_path(director):
    from director.tools import code

    hub = FakeHub({
        "resolve_project": {"ok": True, "path": "C:\\aiOS", "candidates": []},
        "code.start": {"ok": True, "session_id": "sess_1"},
    })
    result = asyncio.run(code.code_session(_ctx(hub), task="fix it", project="aiOS"))
    start = next(payload for action, payload in hub.calls if action == "code.start")
    assert start["project"] == "C:\\aiOS"
    assert not result.error and hub.jobs


def test_a_refused_start_is_this_tools_error_not_a_background_surprise(director):
    """The live failure: the tool said "dispatched", the job died seconds later,
    and the agent told Calle work was running that never started."""
    from director.tools import code

    hub = FakeHub({"code.start": {"ok": False, "error": "harness unavailable"}})
    result = asyncio.run(code.code_session(_ctx(hub), task="fix it"))
    assert result.error and "harness unavailable" in result.error
    assert "do not tell Calle this job was dispatched" in result.error
    assert not hub.jobs


def test_a_refused_start_marks_the_job_failed(director):
    from director.tools import code

    hub = FakeHub({"code.start": {"ok": False, "error": "nope"}})
    asyncio.run(code.code_session(_ctx(hub), task="fix it"))
    rows = director.list_jobs(thread_id="thr_x", limit=5)
    assert rows and rows[0]["status"] == "fail"


# ---------------- answering a waiting session ----------------

def test_code_reply_reaches_the_waiting_session(director):
    from director.tools import code

    job = director.create_job(kind="code", request={}, thread_id="thr_x",
                              agent_id="agt_x", machine_id="mch_1", status="running")
    director.update_job(job["id"], result={"session_id": "sess_1"})
    hub = FakeHub({"code.answer": {"ok": True, "question": "Commit scope?"}})
    result = asyncio.run(code.code_reply(_ctx(hub), job_id=job["id"],
                                         text="commit only your own hunks"))
    action, payload = hub.calls[0]
    assert action == "code.answer"
    assert payload == {"session_id": "sess_1", "text": "commit only your own hunks"}
    assert not result.error


def test_code_reply_finds_the_live_job_without_being_told_its_id(director):
    from director.tools import code

    job = director.create_job(kind="code", request={}, thread_id="thr_x",
                              agent_id="agt_x", machine_id="mch_1", status="running")
    director.update_job(job["id"], result={"session_id": "sess_1"})
    hub = FakeHub({"code.answer": {"ok": True}})
    result = asyncio.run(code.code_reply(_ctx(hub), text="yes"))
    assert not result.error and hub.calls[0][0] == "code.answer"


def test_code_reply_says_so_when_there_is_nothing_waiting(director):
    from director.tools import code

    result = asyncio.run(code.code_reply(_ctx(FakeHub()), text="yes"))
    assert result.error and "no running CODE job" in result.error


def test_agents_are_told_how_to_handle_a_waiting_session():
    from director import agents

    assert "code_reply" in agents.DIRECTOR_TOOLS
    assert "code_reply" in agents.DIRECTOR_PROMPT
    assert "ask_yes_no" in agents.DIRECTOR_PROMPT


def test_agents_are_told_not_to_guess_paths():
    from director import agents

    assert "machine_find" in agents.BASE_PROMPT
    assert "Never guess a file path" in agents.BASE_PROMPT


def test_an_agent_made_last_month_gets_this_months_tools(director):
    """Live proof this matters: Björn was created before `machine_find`
    existed, so he could not have looked a path up if he had wanted to."""
    from director import agents

    old = director.create_agent(name="Björn", kind="custom",
                                tools=["ask_user", "code_session", "shell"])
    agents.ensure_seeded()
    assert sorted(director.get_agent(old["id"])["tools"]) == sorted(agents.DIRECTOR_TOOLS)


def test_a_group_row_is_left_without_tools(director):
    from director import agents

    a = director.create_agent(name="A", kind="custom", tools=agents.DIRECTOR_TOOLS)
    b = director.create_agent(name="B", kind="custom", tools=agents.DIRECTOR_TOOLS)
    group = director.create_agent(name="Group1", kind="group", tools=[],
                                  members=[a["id"], b["id"]])
    agents.ensure_seeded()
    assert director.get_agent(group["id"])["tools"] == []


def test_an_archived_agent_is_not_revived_with_new_tools(director):
    from director import agents

    gone = director.create_agent(name="Old", kind="custom", tools=["ask_user"])
    director.update_agent(gone["id"], {"archived": True})
    agents.ensure_seeded()
    assert director.get_agent(gone["id"])["tools"] == ["ask_user"]


# ---------------- the client end of the same two fixes ----------------

def test_the_client_reports_a_pending_question_in_its_status(monkeypatch):
    import director_client

    class Harness:
        @staticmethod
        def get_job(session_id):
            return {"status": "waiting_user", "title": "Implement yes/no box",
                    "pending_question": "How should I scope the commit?"}

    bridge = director_client.CodeBridge()
    monkeypatch.setattr(bridge, "harness", lambda: Harness)
    assert bridge.status("sess_1")["pending_question"] == "How should I scope the commit?"


def test_the_client_answers_a_waiting_session(monkeypatch):
    import director_client

    sent = {}

    class Harness:
        @staticmethod
        def get_job(session_id):
            return {"status": "waiting_user", "pending_question": "Commit scope?"}

        @staticmethod
        def send_message(session_id, text):
            sent.update({"session": session_id, "text": text})
            return {"ok": True}

    bridge = director_client.CodeBridge()
    monkeypatch.setattr(bridge, "harness", lambda: Harness)
    result = bridge.answer("sess_1", "your hunks only")
    assert result["ok"] and result["question"] == "Commit scope?"
    assert sent == {"session": "sess_1", "text": "your hunks only"}
