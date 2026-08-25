"""Focused regression coverage for Director <-> CODE lifecycle recovery."""
from __future__ import annotations

import asyncio
import queue
import threading

import pytest


@pytest.fixture(autouse=True)
def isolated_continuation_receipts(tmp_path, monkeypatch):
    import director_client

    monkeypatch.setattr(
        director_client, "CONTINUATION_RECEIPTS_PATH",
        tmp_path / "continuation-receipts.json")


@pytest.fixture()
def director_store(tmp_path, monkeypatch):
    monkeypatch.setenv("AIOS_DIRECTOR_HOME", str(tmp_path / "home"))
    import director.config as config
    import director.store as store

    store.close()
    config.load_settings(refresh=True)
    yield store
    store.close()


def _context(thread_id, hub):
    from director.tools import ToolContext

    async def emit(_kind, _payload):
        return None

    async def unused(*_args, **_kwargs):
        return ""

    return ToolContext(
        agent={"id": "agt_test"}, thread_id=thread_id, settings={}, emit=emit,
        request_approval=unused, ask_user=unused, cancel=asyncio.Event(), hub=hub,
    )


def test_continuation_retries_idempotently_and_uses_a_fresh_tracking_result(
        director_store):
    from director.tools import code

    prior = director_store.create_job(
        kind="code", request={"task": "first"}, thread_id="thr_a",
        agent_id="agt_test", machine_id="mch_1", status="done")
    director_store.update_job(prior["id"], result={
        "session_id": "sess_1", "summary": "finished", "code_status": "completed",
        "completed_at": 123, "provider": "codex",
    })

    class Hub:
        def __init__(self):
            self.calls = []
            self.started = []

        def live_jobs(self, *_args, **_kwargs):
            return []

        async def call_machine(self, machine_id, action, payload, *, timeout):
            self.calls.append((machine_id, action, dict(payload), timeout))
            if len(self.calls) == 1:
                return {"ok": False, "error": "machine did not answer within 20s"}
            return {"ok": True, "event_cursor": 44, "queued": True}

        def start_job(self, job, factory):
            self.started.append((job, factory))

    hub = Hub()
    result = asyncio.run(code.code_continue(
        _context("thr_a", hub), text="now add tests", job_id=prior["id"]))

    assert not result.error
    assert len(hub.calls) == 2
    assert hub.calls[0][2]["continuation_id"] == hub.calls[1][2]["continuation_id"]
    tracking = director_store.list_jobs(thread_id="thr_a", limit=1)[0]
    assert tracking["id"] != prior["id"]
    assert tracking["result"]["session_id"] == "sess_1"
    assert tracking["result"]["event_cursor"] == 44
    assert "code_status" not in tracking["result"]
    assert "completed_at" not in tracking["result"]
    assert len(hub.started) == 1


def test_a_stale_running_code_row_gets_a_new_reporter_job(director_store):
    from director.tools import code

    prior = director_store.create_job(
        kind="code", request={"task": "first"}, thread_id="thr_a",
        agent_id="agt_test", machine_id="mch_1", status="running")
    director_store.update_job(prior["id"], result={
        "session_id": "sess_1", "summary": "running"})

    class Hub:
        def __init__(self):
            self.started = []

        def live_jobs(self, *_args, **_kwargs):
            return []

        async def call_machine(self, _machine, _action, payload, *, timeout):
            return {"ok": True, "event_cursor": 8, "queued": True}

        def start_job(self, job, factory):
            self.started.append((job, factory))

    hub = Hub()
    result = asyncio.run(code.code_continue(
        _context("thr_a", hub), text="continue safely", job_id=prior["id"]))
    newest = director_store.list_jobs(thread_id="thr_a", limit=1)[0]

    assert not result.error
    assert newest["id"] != prior["id"]
    assert newest["request"]["parent_job_id"] == prior["id"]
    stale = director_store.get_job(prior["id"])
    assert stale["status"] == "incomplete"
    assert stale["result"]["superseded_by"] == newest["id"]
    assert len(hub.started) == 1


def test_active_continuations_get_distinct_receipts_and_keep_delivered_cursor(
        director_store):
    from director.tools import code

    job = director_store.create_job(
        kind="code", request={}, thread_id="thr_a", agent_id="agt_test",
        machine_id="mch_1", status="running")
    director_store.update_job(job["id"], result={
        "session_id": "sess_1", "summary": "running", "event_cursor": 7})

    class Hub:
        def __init__(self):
            self.calls = []

        def live_jobs(self, *_args, **_kwargs):
            return [director_store.get_job(job["id"])]

        async def call_machine(self, _machine, _action, payload, *, timeout):
            self.calls.append(dict(payload))
            return {"ok": True, "event_cursor": 99, "queued": True}

        def start_job(self, *_args):
            return True

    hub = Hub()
    asyncio.run(code.code_continue(_context("thr_a", hub), text="first follow-up"))
    asyncio.run(code.code_continue(_context("thr_a", hub), text="second follow-up"))

    assert hub.calls[0]["continuation_id"] != hub.calls[1]["continuation_id"]
    assert [call["follow_since"] for call in hub.calls] == [7, 7]
    assert director_store.get_job(job["id"])["result"]["event_cursor"] == 7


def test_unknown_continuation_retry_reuses_durable_key_and_tracking_job(
        director_store, monkeypatch):
    from director.tools import code

    prior = director_store.create_job(
        kind="code", request={}, thread_id="thr_a", agent_id="agt_test",
        machine_id="mch_1", status="done")
    director_store.update_job(prior["id"], result={"session_id": "sess_1"})

    class Hub:
        def __init__(self):
            self.calls = []

        def live_jobs(self, *_args, **_kwargs):
            return []

        async def call_machine(self, _machine, _action, payload, *, timeout):
            self.calls.append(dict(payload))
            return {"ok": False, "error": "machine did not answer within 20s"}

        def start_job(self, *_args):
            return True

    async def no_wait(_delay):
        return None

    monkeypatch.setattr(code.asyncio, "sleep", no_wait)
    hub = Hub()
    first = asyncio.run(code.code_continue(
        _context("thr_a", hub), text="deliver exactly once", job_id=prior["id"]))
    tracking = director_store.list_jobs(thread_id="thr_a", limit=1)[0]
    landed = dict(tracking["result"])
    landed.update({"delivery_uncertain": False, "summary": "completed"})
    director_store.update_job(tracking["id"], status="done", result=landed)
    second = asyncio.run(code.code_continue(
        _context("thr_a", hub), text="deliver exactly once", job_id=prior["id"]))

    assert first.error and second.error
    assert len({call["continuation_id"] for call in hub.calls}) == 1
    assert len({call["job_id"] for call in hub.calls}) == 1
    assert len(director_store.list_jobs(thread_id="thr_a", limit=20)) == 2


def test_explicit_code_jobs_cannot_cross_conversations(director_store):
    from director.tools import code

    job = director_store.create_job(
        kind="code", request={}, thread_id="thr_private", machine_id="mch_1",
        status="done")
    director_store.update_job(job["id"], result={"session_id": "sess_private"})
    assert code._latest_code_job("thr_other", job["id"]) is None


def test_stopped_harness_session_is_rearmed_before_enqueue(monkeypatch):
    import director_client

    class Live:
        def __init__(self):
            self.turn_lock = threading.Lock()
            self._worker_running = False
            self.process = None
            self.rpc = None
            self._messages = queue.Queue()
            self._stop_event = threading.Event()
            self._stop_event.set()
            self.stop_requested = True
            self.interrupt_requested = False
            self.queued = 3

        def save(self, **updates):
            for key, value in updates.items():
                setattr(self, key, value)
            return updates

    live = Live()

    class Jobs:
        @staticmethod
        def get_job(_session):
            return {"status": "stopped", "provider": "codex"}

        @staticmethod
        def _get_job(_session):
            return live

        @staticmethod
        def read_events(_session, _since):
            return {"size": 7}

        @staticmethod
        def send_message(_session, _text, **_kwargs):
            assert live.stop_requested is False
            assert live.interrupt_requested is False
            assert live._stop_event.is_set() is False
            assert live.queued == 0
            return {"ok": True, "queued": True, "job": {"status": "queued"}}

    bridge = director_client.CodeBridge()
    monkeypatch.setattr(bridge, "harness", lambda: Jobs)
    result = bridge.continue_session("sess_1", "do more")

    assert result["ok"] is True
    assert result["event_cursor"] == 7


def test_code_answer_refuses_when_there_is_no_pending_question(monkeypatch):
    import director_client

    class Jobs:
        sent = False

        @staticmethod
        def get_job(_session):
            return {"status": "running", "pending_question": ""}

        @classmethod
        def send_message(cls, *_args, **_kwargs):
            cls.sent = True
            return {"ok": True}

    bridge = director_client.CodeBridge()
    monkeypatch.setattr(bridge, "harness", lambda: Jobs)
    result = bridge.answer("sess_1", "yes")

    assert result["ok"] is False
    assert Jobs.sent is False


def test_client_deduplicates_a_continuation_receipt(monkeypatch):
    import director_client

    client = director_client.DirectorClient({
        "url": "https://example.invalid", "token": "t", "name": "pc"})
    calls = []
    monkeypatch.setattr(client.code, "available", lambda: (True, ""))
    monkeypatch.setattr(client.code, "events", lambda _session, _since: {
        "ok": True, "events": [], "size": 0, "reset": False})

    def continue_once(session_id, text, urgent):
        calls.append((session_id, text, urgent))
        return {"ok": True, "session_id": session_id, "event_cursor": 3}

    monkeypatch.setattr(client.code, "continue_session", continue_once)

    def no_follower(session_id, *_args, **_kwargs):
        done = asyncio.get_running_loop().create_future()
        done.set_result(None)
        client._code_followers[session_id] = done
        return done

    monkeypatch.setattr(client, "_start_code_follower", no_follower)
    payload = {
        "session_id": "sess_1", "job_id": "job_1", "text": "same instruction",
        "continuation_id": "cont_stable", "replace_follower": True,
    }

    async def scenario():
        first = await client.do_code_continue(payload)
        second = await client.do_code_continue(payload)
        return first, second

    first, second = asyncio.run(scenario())
    assert first == second
    assert calls == [("sess_1", "same instruction", False)]


def test_client_receipt_survives_restart_and_is_bound_to_the_request(monkeypatch):
    import director_client

    config = {"url": "https://example.invalid", "token": "t", "name": "pc"}
    payload = {
        "session_id": "sess_1", "job_id": "job_1", "text": "durable request",
        "continuation_id": "cont_durable", "replace_follower": True,
    }

    def prepare(client):
        monkeypatch.setattr(client.code, "available", lambda: (True, ""))
        monkeypatch.setattr(client.code, "events", lambda *_args: {
            "ok": True, "events": [], "size": 4, "reset": False})
        monkeypatch.setattr(client, "_start_code_follower", lambda *_args, **_kwargs: None)

    first = director_client.DirectorClient(config)
    prepare(first)
    calls = []
    monkeypatch.setattr(first.code, "continue_session", lambda session, text, urgent: (
        calls.append((session, text, urgent))
        or {"ok": True, "session_id": session, "event_cursor": 4}))
    asyncio.run(first.do_code_continue(payload))

    restarted = director_client.DirectorClient(config)
    prepare(restarted)
    monkeypatch.setattr(
        restarted.code, "continue_session",
        lambda *_args: pytest.fail("durable receipt was sent twice"))
    replay = asyncio.run(restarted.do_code_continue(payload))
    collision = asyncio.run(restarted.do_code_continue({**payload, "text": "different"}))

    assert replay["ok"] is True
    assert calls == [("sess_1", "durable request", False)]
    assert collision["ok"] is False
    assert "different CODE request" in collision["error"]


def test_client_recovers_an_inflight_receipt_from_the_user_event(monkeypatch):
    import director_client

    config = {"url": "https://example.invalid", "token": "t", "name": "pc"}
    payload = {
        "session_id": "sess_1", "job_id": "job_1", "text": "already appended",
        "continuation_id": "cont_inflight", "replace_follower": True,
    }
    first = director_client.DirectorClient(config)
    request_hash = first._continuation_request_hash(payload)
    first._continuation_receipts[payload["continuation_id"]] = {
        "state": "inflight", "request_hash": request_hash,
        "before_cursor": 12, "saved_at": 1,
    }
    first._persist_continuation_receipts()

    restarted = director_client.DirectorClient(config)
    monkeypatch.setattr(restarted.code, "available", lambda: (True, ""))
    monkeypatch.setattr(restarted.code, "continuation_seen", lambda *_args: {
        "ok": True, "seen": True, "event_cursor": 30})
    monkeypatch.setattr(restarted.code, "status", lambda *_args: {
        "ok": True, "status": "running", "provider": "codex"})
    monkeypatch.setattr(
        restarted.code, "continue_session",
        lambda *_args: pytest.fail("recovered inflight request was sent twice"))
    monkeypatch.setattr(restarted, "_start_code_follower", lambda *_args, **_kwargs: None)

    result = asyncio.run(restarted.do_code_continue(payload))
    assert result["ok"] is True
    assert result["recovered"] is True
    assert result["event_cursor"] == 12
    assert result["continuation_id"] == "cont_inflight"


def test_follower_retries_events_and_preserves_incomplete_status(monkeypatch):
    import director_client

    client = director_client.DirectorClient({
        "url": "https://example.invalid", "token": "t", "name": "pc"})
    event_offsets = []

    def events(_session, since):
        event_offsets.append(since)
        if since == 0:
            return {"ok": True, "events": [{"kind": "delta", "text": "x"}], "size": 9}
        return {"ok": True, "events": [], "size": 9}

    monkeypatch.setattr(client.code, "events", events)
    monkeypatch.setattr(client.code, "status", lambda _session: {
        "ok": True, "status": "incomplete", "summary": "budget ended"})
    emits = []

    async def emit(_job, kind, payload):
        emits.append((kind, payload))
        return len(emits) > 1

    reports = []

    async def report(_job, status, payload):
        reports.append((status, payload))
        return len(reports) > 1

    monkeypatch.setattr(client, "emit", emit)
    monkeypatch.setattr(client, "report_job", report)
    original_sleep = asyncio.sleep

    async def fast_sleep(_delay):
        await original_sleep(0)

    monkeypatch.setattr(director_client.asyncio, "sleep", fast_sleep)
    asyncio.run(client.follow_session("sess_1", "job_1"))

    assert event_offsets[:2] == [0, 0]
    assert reports[0][0] == reports[1][0] == "incomplete"
    assert reports[-1][1]["event_cursor"] == 9


def test_restart_and_machine_reconnect_restore_code_reporting(director_store):
    from director import server

    code_job = director_store.create_job(
        kind="code", request={}, thread_id="thr_a", agent_id="agt_a",
        machine_id="mch_1", status="running")
    director_store.update_job(code_job["id"], result={
        "session_id": "sess_1", "summary": "running", "event_cursor": 12})
    operator_job = director_store.create_job(
        kind="operator", request={}, thread_id="thr_a", agent_id="agt_a",
        status="running")
    server._release_orphaned_jobs()

    assert director_store.get_job(code_job["id"])["status"] == "recovering"
    assert director_store.get_job(operator_job["id"])["status"] == "stopped"

    class Runtime:
        def __init__(self):
            self.calls = []
            self.started = []

        async def call_machine(self, machine_id, action, payload, *, timeout):
            self.calls.append((machine_id, action, payload, timeout))
            return {"ok": True, "status": "running"}

        def live_jobs(self, *_args, **_kwargs):
            return []

        def start_job(self, job, factory):
            self.started.append((job, factory))

    runtime = Runtime()
    asyncio.run(server._reattach_code_jobs(runtime, {"id": "mch_1"}))

    assert runtime.calls[0][1] == "code.follow"
    assert runtime.calls[0][2]["since"] == 12
    assert director_store.get_job(code_job["id"])["status"] == "running"
    assert len(runtime.started) == 1


def test_restart_recovery_does_not_lose_active_code_behind_terminal_history(
        director_store):
    from director import server

    active = director_store.create_job(
        kind="code", request={}, thread_id="thr_a", agent_id="agt_a",
        machine_id="mch_1", status="running")
    director_store.update_job(active["id"], result={
        "session_id": "sess_old_live", "event_cursor": 5})
    for index in range(205):
        row = director_store.create_job(
            kind="code", request={}, thread_id="thr_a", agent_id="agt_a",
            machine_id="mch_1", status="done")
        director_store.update_job(row["id"], result={"session_id": f"terminal_{index}"})

    class Runtime:
        def __init__(self):
            self.calls = []

        async def call_machine(self, _machine, action, payload, *, timeout):
            self.calls.append((action, payload))
            return {"ok": True, "status": "running"}

        def start_job(self, *_args):
            return True

    runtime = Runtime()
    asyncio.run(server._reattach_code_jobs(runtime, {"id": "mch_1"}))

    assert [payload["session_id"] for _, payload in runtime.calls] == ["sess_old_live"]
    assert director_store.get_job(active["id"])["status"] == "running"


def test_code_generation_rejects_a_stale_follower(director_store):
    from director import server

    job = director_store.create_job(kind="code", request={}, status="running")
    director_store.update_job(job["id"], result={"continuation_id": "cont_new"})
    job = director_store.get_job(job["id"])

    assert server._code_generation_matches(job, {"continuation_id": "cont_new"})
    assert not server._code_generation_matches(job, {"continuation_id": "cont_old"})
    assert not server._code_generation_matches(job, {})


def test_code_waiter_creation_is_owned_atomically(director_store):
    from director import server

    job = director_store.create_job(kind="code", request={}, status="running")

    class Runtime:
        def __init__(self):
            self.started = False
            self.calls = 0

        def start_job(self, _job, _factory):
            self.calls += 1
            if self.started:
                return False
            self.started = True
            return True

    runtime = Runtime()
    assert server._ensure_code_waiter(runtime, job) is True
    assert server._ensure_code_waiter(runtime, job) is False
    assert runtime.calls == 2


def test_delivered_code_event_cursor_is_monotonic_until_an_explicit_reset(
        director_store):
    from director import server

    job = director_store.create_job(kind="code", request={}, status="running")
    director_store.update_job(job["id"], result={"event_cursor": 10, "summary": "running"})
    job = director_store.get_job(job["id"])

    assert server._persist_code_event_cursor(job, {"size": 7}) == 10
    assert server._persist_code_event_cursor(
        director_store.get_job(job["id"]), {"size": 14}) == 14
    assert server._persist_code_event_cursor(
        director_store.get_job(job["id"]), {"size": 4, "reset": True}) == 4
    assert director_store.get_job(job["id"])["result"]["event_cursor"] == 4


def test_code_status_reconciles_the_harness_and_reporter_state(director_store):
    from director.tools import code

    job = director_store.create_job(
        kind="code", request={}, thread_id="thr_a", machine_id="mch_1",
        status="running")
    director_store.update_job(job["id"], result={
        "session_id": "sess_1", "summary": "stale running"})

    class Hub:
        def live_jobs(self, *_args, **_kwargs):
            return []

        async def call_machine(self, _machine, action, payload, *, timeout):
            assert action == "code.status"
            return {"ok": True, "status": "completed", "summary": "shipped"}

    result = asyncio.run(code.code_status(
        _context("thr_a", Hub()), job_id=job["id"]))

    assert not result.error
    assert "Harness session sess_1: completed" in result.output
    assert "Director reporter: not attached" in result.output
    assert director_store.get_job(job["id"])["status"] == "done"


def test_code_status_repairs_a_missing_live_reporter(director_store):
    from director.tools import code

    job = director_store.create_job(
        kind="code", request={}, thread_id="thr_a", machine_id="mch_1",
        status="running")
    director_store.update_job(job["id"], result={
        "session_id": "sess_1", "summary": "working", "event_cursor": 21})

    class Hub:
        def __init__(self):
            self.calls = []
            self.started = []

        def live_jobs(self, *_args, **_kwargs):
            return []

        async def call_machine(self, _machine, action, payload, *, timeout):
            self.calls.append((action, payload))
            if action == "code.status":
                return {"ok": True, "status": "running", "summary": "working"}
            return {"ok": True, "status": "running"}

        def start_job(self, row, factory):
            self.started.append((row, factory))

    hub = Hub()
    result = asyncio.run(code.code_status(
        _context("thr_a", hub), job_id=job["id"]))

    assert [call[0] for call in hub.calls] == ["code.status", "code.follow"]
    assert hub.calls[1][1]["since"] == 21
    assert "Director reporter: attached" in result.output
    assert len(hub.started) == 1


def test_code_status_repairs_machine_follower_even_with_a_live_waiter(
        director_store):
    from director.tools import code

    job = director_store.create_job(
        kind="code", request={}, thread_id="thr_a", machine_id="mch_1",
        status="running")
    director_store.update_job(job["id"], result={
        "session_id": "sess_1", "event_cursor": 11,
        "continuation_id": "cont_current"})

    class Hub:
        def __init__(self):
            self.calls = []

        def live_jobs(self, *_args, **_kwargs):
            return [director_store.get_job(job["id"])]

        async def call_machine(self, _machine, action, payload, *, timeout):
            self.calls.append((action, payload))
            return ({"ok": True, "status": "running", "summary": "working"}
                    if action == "code.status" else {"ok": True})

        def start_job(self, *_args):
            return False

    hub = Hub()
    result = asyncio.run(code.code_status(
        _context("thr_a", hub), job_id=job["id"]))

    assert not result.error
    assert [action for action, _ in hub.calls] == ["code.status", "code.follow"]
    assert hub.calls[-1][1]["continuation_id"] == "cont_current"


def test_reply_reattaches_a_terminal_tracking_row_when_harness_is_waiting(
        director_store):
    from director.tools import code

    job = director_store.create_job(
        kind="code", request={}, thread_id="thr_a", machine_id="mch_1",
        status="incomplete")
    director_store.update_job(job["id"], result={
        "session_id": "sess_1", "pending_question": "Proceed?", "event_cursor": 9})

    class Hub:
        def __init__(self):
            self.calls = []
            self.started = []

        async def call_machine(self, _machine, action, payload, *, timeout):
            self.calls.append((action, payload))
            if action == "code.answer":
                return {"ok": True, "question": "Proceed?"}
            return {"ok": True, "status": "running"}

        def start_job(self, row, factory):
            self.started.append((row, factory))
            return True

    hub = Hub()
    result = asyncio.run(code.code_reply(
        _context("thr_a", hub), job_id=job["id"], text="yes"))

    assert not result.error
    assert [action for action, _ in hub.calls] == ["code.answer", "code.follow"]
    assert director_store.get_job(job["id"])["status"] == "running"
    assert len(hub.started) == 1


def test_transient_reattach_failure_pauses_instead_of_ghosting(
        director_store, monkeypatch):
    from director import server

    job = director_store.create_job(
        kind="code", request={}, thread_id="thr_a", machine_id="mch_1",
        status="recovering")
    director_store.update_job(job["id"], result={"session_id": "sess_1"})

    class Runtime:
        def __init__(self):
            self.calls = 0
            self.started = []

        async def call_machine(self, *_args, **_kwargs):
            self.calls += 1
            return {"ok": False, "error": "temporary bridge error"}

        def live_jobs(self, *_args, **_kwargs):
            return []

        def start_job(self, row, factory):
            self.started.append((row, factory))

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(server.asyncio, "sleep", no_wait)
    runtime = Runtime()
    asyncio.run(server._reattach_code_jobs(runtime, {"id": "mch_1"}))

    row = director_store.get_job(job["id"])
    assert runtime.calls == 2
    assert row["status"] == "incomplete"
    assert "after two reattachment attempts" in row["result"]["summary"]
    assert len(runtime.started) == 1
