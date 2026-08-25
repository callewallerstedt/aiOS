"""Focused regressions for Director's durable tool and follow-up lifecycle."""
from __future__ import annotations

import asyncio
import json
import time

import pytest


@pytest.fixture()
def director(tmp_path, monkeypatch):
    monkeypatch.setenv("AIOS_DIRECTOR_HOME", str(tmp_path / "home"))
    from director import config, store

    store.close()
    config.load_settings(refresh=True)
    yield store
    store.close()


def _reply(*, text: str = "", calls: list[dict] | None = None) -> dict:
    return {
        "text": text,
        "tool_calls": list(calls or []),
        "usage": {},
        "backend": "test",
        "model": "test-model",
    }


def _call(name: str, arguments: dict, number: int) -> dict:
    return {
        "call_id": f"call-{number}",
        "name": name,
        "arguments": json.dumps(arguments, separators=(",", ":")),
    }


def _register_tool(name: str, run, *, destructive: bool = False) -> None:
    from director import tools

    tools.register(tools.Tool(
        name=name,
        description=f"test tool {name}",
        parameters={"type": "object", "properties": {}},
        run=run,
        destructive=destructive,
    ))


def _make_agent(store, *, tools: list[str] | None = None, name: str = "Test Director") -> dict:
    return store.create_agent(
        name=name,
        backend="test",
        model="test-model",
        tools=list(tools or []),
    )


async def _quiet_notify(*_args, **_kwargs) -> None:
    return None


def _bulk_messages(store, thread_id: str, rows: list[tuple[str, str, dict]]) -> None:
    """Insert large transcript fixtures in one transaction to keep tests quick."""
    now = time.time()
    connection = store.connect()
    connection.executemany(
        "INSERT INTO messages (id, thread_id, role, content, meta, created_at) "
        "VALUES (?,?,?,?,?,?)",
        [
            (
                f"bulk-{thread_id}-{index}",
                thread_id,
                role,
                content,
                json.dumps(meta),
                now + index / 1_000_000,
            )
            for index, (role, content, meta) in enumerate(rows)
        ],
    )
    connection.commit()


def test_live_operator_receives_each_fresh_followup_exactly_and_only_from_its_thread(
        director, monkeypatch):
    from director import models, runtime, tools

    agent = _make_agent(director, tools=["operator_say"])
    owner_thread = director.create_thread(agent["id"])
    other_thread = director.create_thread(agent["id"])

    initial = director.add_message(owner_thread["id"], "user", "Open Spotify.")
    initial_sequence = director.list_messages(owner_thread["id"])[-1]["sequence"]
    director.add_message(owner_thread["id"], "assistant", "Started.", {
        "input_through": initial_sequence,
    })
    first = director.add_message(owner_thread["id"], "user", "Use the email login.")
    second = director.add_message(owner_thread["id"], "user", "Then check WALLERSTEDT.")
    outsider = director.add_message(other_thread["id"], "user", "Do not route this elsewhere.")

    job = director.create_job(
        kind="operator",
        request={"task": "Open Spotify"},
        thread_id=owner_thread["id"],
        agent_id=agent["id"],
        status="running",
    )
    model_calls: list[dict] = []

    async def fake_complete(**kwargs):
        model_calls.append(kwargs)
        return _reply(text="The update was delivered.")

    monkeypatch.setattr(models, "complete", fake_complete)
    hub = runtime.Runtime()
    hub.notify = _quiet_notify

    async def run() -> None:
        never = asyncio.Event()
        live_task = asyncio.create_task(never.wait())
        hub._jobs[job["id"]] = live_task
        try:
            settings = {"safety": {"confirm_destructive": True}}

            async def approval(**_kwargs):
                return {"status": "approved"}

            async def ask(_question, **_kwargs):
                return ""

            async def emit(_kind, _payload):
                return None

            ctx = tools.ToolContext(
                agent=agent,
                thread_id=owner_thread["id"],
                settings=settings,
                emit=emit,
                request_approval=approval,
                ask_user=ask,
                cancel=asyncio.Event(),
                hub=hub,
            )
            await hub._tool_rounds(
                owner_thread["id"], agent, ctx, tools.schemas(["operator_say"]),
                settings, ctx.cancel, consumed_through=initial_sequence,
                attempted_through=[initial_sequence],
            )

            other_ctx = tools.ToolContext(
                agent=agent,
                thread_id=other_thread["id"],
                settings=settings,
                emit=emit,
                request_approval=approval,
                ask_user=ask,
                cancel=asyncio.Event(),
                hub=hub,
            )
            await hub._tool_rounds(
                other_thread["id"], agent, other_ctx, tools.schemas(["operator_say"]),
                settings, other_ctx.cancel, consumed_through=0,
                attempted_through=[0],
            )
        finally:
            live_task.cancel()
            await asyncio.gather(live_task, return_exceptions=True)
            hub._jobs.pop(job["id"], None)

    asyncio.run(run())

    assert hub.take_job_notes(job["id"]) == [
        "Use the email login.",
        "Then check WALLERSTEDT.",
    ]
    owner_calls = [
        row for row in director.list_messages(owner_thread["id"])
        if row["role"] == "tool_call" and row["meta"].get("name") == "operator_say"
    ]
    assert [json.loads(row["meta"]["arguments"])["text"] for row in owner_calls] == [
        first["content"], second["content"],
    ]
    assert [row["meta"]["source_message_ids"] for row in owner_calls] == [
        [first["id"]], [second["id"]],
    ]
    assert not [
        row for row in director.list_messages(other_thread["id"])
        if row["role"] == "tool_call" and row["meta"].get("name") == "operator_say"
    ]
    assert outsider["content"] in json.dumps(model_calls[-1]["items"])
    assert all(
        "operator_say" not in {schema["name"] for schema in call["tools"]}
        for call in model_calls[:1]
    )


def test_live_operator_explicit_cancel_uses_stop_control_not_a_queued_note(
        director, monkeypatch):
    from director import models, runtime, tools

    agent = _make_agent(director, tools=["operator_say", "operator_stop"])
    thread = director.create_thread(agent["id"])
    director.add_message(thread["id"], "user", "Open Spotify.")
    initial_sequence = director.latest_message_sequence(thread["id"])
    director.add_message(thread["id"], "assistant", "Started.", {
        "input_through": initial_sequence,
    })
    stop_message = director.add_message(
        thread["id"], "user", "Cancel the Operator run now."
    )
    job = director.create_job(
        kind="operator",
        request={"task": "Open Spotify"},
        thread_id=thread["id"],
        agent_id=agent["id"],
        status="running",
    )
    stop_calls: list[str] = []

    async def provider_must_not_run(**_kwargs):
        raise AssertionError("an exact stop-only turn must not reach the model")

    monkeypatch.setattr(models, "complete", provider_must_not_run)
    hub = runtime.Runtime()
    hub.notify = _quiet_notify

    async def run() -> None:
        live_task = asyncio.create_task(asyncio.Event().wait())
        hub._jobs[job["id"]] = live_task

        async def stop_job(job_id: str) -> dict:
            stop_calls.append(job_id)
            task = hub._jobs.pop(job_id, None)
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            director.update_job(
                job_id, status="stopped",
                result={"status": "stopped", "summary": "Stopped by request"},
            )
            return {"ok": True, "job_id": job_id}

        hub.stop_job = stop_job
        settings = {"safety": {"confirm_destructive": True}}

        async def approval(**_kwargs):
            return {"status": "approved"}

        async def ask(_question, **_kwargs):
            return ""

        async def emit(_kind, _payload):
            return None

        ctx = tools.ToolContext(
            agent=agent,
            thread_id=thread["id"],
            settings=settings,
            emit=emit,
            request_approval=approval,
            ask_user=ask,
            cancel=asyncio.Event(),
            hub=hub,
        )
        try:
            await hub._tool_rounds(
                thread["id"], agent, ctx,
                tools.schemas(["operator_say", "operator_stop"]),
                settings, ctx.cancel, consumed_through=initial_sequence,
                attempted_through=[initial_sequence],
            )
        finally:
            if not live_task.done():
                live_task.cancel()
                await asyncio.gather(live_task, return_exceptions=True)
            hub._jobs.pop(job["id"], None)

    asyncio.run(run())

    assert stop_calls == [job["id"]]
    assert hub.take_job_notes(job["id"]) == []
    rows = director.list_messages(thread["id"])
    calls = [row for row in rows if row["role"] == "tool_call"]
    assert [row["meta"]["name"] for row in calls] == ["operator_stop"]
    assert json.loads(calls[0]["meta"]["arguments"]) == {}
    assert calls[0]["meta"]["source_message_ids"] == [stop_message["id"]]
    assert not [row for row in rows if row["role"] == "tool_call"
                and row["meta"].get("name") == "operator_say"]
    notices = [row for row in rows if row["role"] == "assistant"
               and row["meta"].get("kind") == "operator.control"]
    assert len(notices) == 1
    assert "stopped" in notices[0]["content"].lower()


def test_live_operator_sensitive_input_is_blocked_without_tool_or_event_copy(
        director, monkeypatch):
    from director import models, runtime, tools

    agent = _make_agent(director, tools=["operator_say", "operator_stop"])
    thread = director.create_thread(agent["id"])
    director.add_message(thread["id"], "user", "Open the account page.")
    initial_sequence = director.latest_message_sequence(thread["id"])
    director.add_message(thread["id"], "assistant", "Started.", {
        "input_through": initial_sequence,
    })
    secret = "correct-horse-private-battery"
    director.add_message(thread["id"], "user", f"Password: {secret}")
    job = director.create_job(
        kind="operator",
        request={"task": "Open the account page"},
        thread_id=thread["id"],
        agent_id=agent["id"],
        status="running",
    )

    async def provider_must_not_run(**_kwargs):
        raise AssertionError("sensitive Operator input must not reach a provider")

    monkeypatch.setattr(models, "complete", provider_must_not_run)
    hub = runtime.Runtime()
    hub.notify = _quiet_notify

    async def run() -> None:
        live_task = asyncio.create_task(asyncio.Event().wait())
        hub._jobs[job["id"]] = live_task
        settings = {"safety": {"confirm_destructive": True}}

        async def approval(**_kwargs):
            return {"status": "approved"}

        async def ask(_question, **_kwargs):
            return ""

        async def emit(_kind, _payload):
            return None

        ctx = tools.ToolContext(
            agent=agent,
            thread_id=thread["id"],
            settings=settings,
            emit=emit,
            request_approval=approval,
            ask_user=ask,
            cancel=asyncio.Event(),
            hub=hub,
        )
        try:
            await hub._tool_rounds(
                thread["id"], agent, ctx,
                tools.schemas(["operator_say", "operator_stop"]),
                settings, ctx.cancel, consumed_through=initial_sequence,
                attempted_through=[initial_sequence],
            )
        finally:
            live_task.cancel()
            await asyncio.gather(live_task, return_exceptions=True)
            hub._jobs.pop(job["id"], None)

    asyncio.run(run())

    rows = director.list_messages(thread["id"])
    assert not [row for row in rows if row["role"] in {"tool_call", "tool_result"}]
    assert hub.take_job_notes(job["id"]) == []
    notices = [row for row in rows if row["role"] == "assistant"
               and row["meta"].get("kind") == "operator.sensitive_handoff"]
    assert len(notices) == 1
    assert "take over" in notices[0]["content"].lower()
    assert secret not in notices[0]["content"]
    assert secret not in json.dumps(
        director.list_events(thread_id=thread["id"]), ensure_ascii=False
    )


def test_operator_control_and_sensitive_classification_is_narrow(director):
    from director import runtime

    assert runtime._operator_stop_intent("cancel the run") is True
    assert runtime._operator_stop_intent("don't stop") is False
    assert runtime._operator_stop_intent("cancel the subscription") is False
    assert runtime._sensitive_operator_input("Password: private-value") is True
    assert runtime._sensitive_operator_input("Use card 4242 4242 4242 4242") is True
    assert runtime._sensitive_operator_input("Use the saved email login.") is False


def test_provider_failure_after_side_effect_is_acknowledged_without_replay(
        director, monkeypatch):
    from director import models, runtime, tools

    tool_name = "test_runtime_once_before_provider_failure"
    effects: list[str] = []

    async def side_effect(_ctx, value: str = ""):
        effects.append(value)
        return tools.ToolResult(output="external side effect completed")

    _register_tool(tool_name, side_effect)
    agent = _make_agent(director, tools=[tool_name])
    thread = director.create_thread(agent["id"])
    user = director.add_message(thread["id"], "user", "Do it once.")
    responses = {"count": 0}

    async def fake_complete(**_kwargs):
        responses["count"] += 1
        if responses["count"] == 1:
            return _reply(calls=[_call(tool_name, {"value": "only-once"}, 1)])
        raise models.ModelError("provider disconnected after the tool result")

    monkeypatch.setattr(models, "complete", fake_complete)
    hub = runtime.Runtime()
    hub.notify = _quiet_notify
    asyncio.run(hub._turn(thread["id"], "user"))

    assert effects == ["only-once"]
    assert responses["count"] == 2
    assert not hub.busy(thread["id"])
    rows = director.list_messages(thread["id"])
    acknowledgements = [row for row in rows if row["role"] == "runtime_ack"]
    assert len(acknowledgements) == 1
    assert acknowledgements[0]["meta"]["input_through"] >= next(
        row["sequence"] for row in rows if row["id"] == user["id"]
    )
    assert len([row for row in rows if row["role"] == "tool_result"]) == 1
    blockers = [row for row in rows if row["role"] == "assistant"
                and row["meta"].get("kind") == "runtime.model_failure"]
    assert len(blockers) == 1
    assert blockers[0]["meta"]["runtime_generated"] is True
    assert "did not retry automatically" in blockers[0]["content"]
    assert "provider disconnected" in blockers[0]["content"]


def test_third_unchanged_action_is_blocked_and_omitted_default_matches_explicit_default(
        director, monkeypatch):
    from director import models, runtime, tools

    tool_name = "test_runtime_optional_default_guard"
    executions: list[bool] = []

    async def harmless(_ctx, urgent: bool = False):
        executions.append(urgent)
        return tools.ToolResult(output=f"attempt {len(executions)} did not change state")

    _register_tool(tool_name, harmless)
    agent = _make_agent(director, tools=[tool_name])
    thread = director.create_thread(agent["id"])
    director.add_message(thread["id"], "user", "Try it, but do not loop.")
    provider_round = {"count": 0}

    async def fake_complete(**kwargs):
        if not kwargs.get("tools"):
            return _reply(text="I stopped after the unchanged approach repeated.")
        provider_round["count"] += 1
        arguments = {} if provider_round["count"] == 1 else {"urgent": False}
        return _reply(calls=[_call(tool_name, arguments, provider_round["count"])])

    monkeypatch.setattr(models, "complete", fake_complete)
    hub = runtime.Runtime()
    hub.notify = _quiet_notify
    asyncio.run(hub._turn(thread["id"], "user"))

    assert executions == [False, False]
    rows = director.list_messages(thread["id"])
    results = [row for row in rows if row["role"] == "tool_result"]
    assert len(results) == 3
    assert results[-1]["meta"]["card"]["meta"] == "loop guard"
    assistants = [row for row in rows if row["role"] == "assistant"]
    assert assistants[-1]["meta"].get("loop_guard") is True


@pytest.mark.parametrize("mode", ["error", "declined"])
def test_destructive_error_or_decline_is_never_retried(director, monkeypatch, mode):
    from director import models, runtime, tools

    tool_name = f"test_runtime_destructive_{mode}"
    executions: list[str] = []
    approvals: list[str] = []

    async def destructive(_ctx, target: str = ""):
        executions.append(target)
        return tools.ToolResult(error="delivery outcome is unknown")

    _register_tool(tool_name, destructive, destructive=True)
    agent = _make_agent(director, tools=[tool_name])
    thread = director.create_thread(agent["id"])
    director.add_message(thread["id"], "user", "Perform this at most once.")

    async def fake_complete(**kwargs):
        if not kwargs.get("tools"):
            return _reply(text="I paused the side-effecting action.")
        return _reply(calls=[_call(tool_name, {"target": "external"}, 1)])

    monkeypatch.setattr(models, "complete", fake_complete)
    hub = runtime.Runtime()
    hub.notify = _quiet_notify

    async def run() -> None:
        settings = {
            "safety": {
                "confirm_destructive": mode == "declined",
                "approve_all": mode == "error",
            }
        }

        async def approval(**kwargs):
            approvals.append(kwargs["tool"])
            return {"status": "declined", "note": "not now"}

        async def ask(_question, **_kwargs):
            return ""

        async def emit(_kind, _payload):
            return None

        ctx = tools.ToolContext(
            agent=agent,
            thread_id=thread["id"],
            settings=settings,
            emit=emit,
            request_approval=approval,
            ask_user=ask,
            cancel=asyncio.Event(),
            hub=hub,
        )
        await hub._tool_rounds(
            thread["id"], agent, ctx, tools.schemas([tool_name]), settings,
            ctx.cancel, consumed_through=0, attempted_through=[0],
        )

    asyncio.run(run())

    if mode == "error":
        assert executions == ["external"]
        assert approvals == []
    else:
        assert executions == []
        assert approvals == [tool_name]
    rows = director.list_messages(thread["id"])
    assert len([row for row in rows if row["role"] == "tool_call"]) == 1
    assert len([row for row in rows if row["role"] == "tool_result"]) == 1
    assert [row for row in rows if row["role"] == "assistant"][-1]["meta"].get(
        "loop_guard"
    ) is True


def test_varying_group_tool_loop_ends_with_visible_fallback(director, monkeypatch):
    from director import models, runtime, tools

    tool_name = "test_runtime_group_varying_tool"
    executions: list[int] = []

    async def varying(_ctx, value: int = 0):
        executions.append(value)
        return tools.ToolResult(output=f"still working on variation {value}")

    _register_tool(tool_name, varying)
    member = _make_agent(director, tools=[tool_name], name="Looping Member")
    group = director.create_agent(
        name="Test Group",
        kind="group",
        members=[member["id"]],
    )
    thread = director.create_thread(group["id"])
    director.add_message(thread["id"], "user", "Finish this without disappearing.")
    rounds = {"count": 0}

    async def fake_complete(**_kwargs):
        rounds["count"] += 1
        return _reply(calls=[_call(tool_name, {"value": rounds["count"]}, rounds["count"])])

    monkeypatch.setattr(models, "complete", fake_complete)
    hub = runtime.Runtime()
    hub.notify = _quiet_notify
    asyncio.run(hub._group_member_turn(thread["id"], group, member))

    assert executions == list(range(1, 9))
    assistants = [row for row in director.list_messages(thread["id"])
                  if row["role"] == "assistant"]
    assert len(assistants) == 1
    assert assistants[0]["meta"]["loop_guard"] is True
    assert "eight tool rounds" in assistants[0]["content"]


def test_active_input_older_than_newest_5000_is_in_model_snapshot_before_ack(
        director, monkeypatch):
    from director import models, runtime

    agent = _make_agent(director)
    thread = director.create_thread(agent["id"])
    active = director.add_message(
        thread["id"], "user", "EARLY ACTIVE INPUT THAT MUST NOT BE SKIPPED"
    )
    _bulk_messages(
        director,
        thread["id"],
        [("reaction", "👍", {}) for _ in range(5000)],
    )
    high_watermark = director.latest_message_sequence(thread["id"])
    captured: list[dict] = []

    async def fake_complete(**kwargs):
        captured.extend(kwargs["items"])
        return _reply(text="I saw the active input.")

    monkeypatch.setattr(models, "complete", fake_complete)
    hub = runtime.Runtime()
    hub.notify = _quiet_notify
    asyncio.run(hub._turn(thread["id"], "user"))

    assert active["content"] in json.dumps(captured)
    assistant = [
        row for row in director.list_messages(thread["id"], newest=True)
        if row["role"] == "assistant"
    ][-1]
    assert assistant["meta"]["input_through"] == high_watermark


def test_compaction_advances_when_call_result_is_after_first_5000_row_batch(
        director, monkeypatch):
    from director import models, runtime

    agent = _make_agent(director)
    thread = director.create_thread(agent["id"])
    director.add_message(thread["id"], "tool_call", "", {
        "call_id": "long-running-call",
        "name": "long_tool",
        "arguments": "{}",
    })
    _bulk_messages(
        director,
        thread["id"],
        [("assistant", "small filler", {}) for _ in range(4999)],
    )
    result = director.add_message(thread["id"], "tool_result", "", {
        "call_id": "long-running-call",
        "name": "long_tool",
        "output": "finished after the first compaction page",
    })
    result_sequence = director.latest_message_sequence(thread["id"])

    async def fake_complete(**_kwargs):
        return _reply(text="durable compacted summary")

    monkeypatch.setattr(models, "complete", fake_complete)
    hub = runtime.Runtime()
    hub.notify = _quiet_notify

    async def run() -> tuple[int, int]:
        assert await hub.compact_thread(thread["id"]) is True
        first = int(director.get_thread(thread["id"])["compacted_through"])
        assert await hub.compact_thread(thread["id"]) is True
        second = int(director.get_thread(thread["id"])["compacted_through"])
        return first, second

    first_boundary, second_boundary = asyncio.run(run())
    assert 0 < first_boundary < result_sequence
    assert second_boundary == result_sequence
    assert result["meta"]["output"] == "finished after the first compaction page"


def test_compaction_does_not_stall_inside_a_long_call_result_interleaving(
        director, monkeypatch):
    from director import models, runtime

    agent = _make_agent(director)
    thread = director.create_thread(agent["id"])
    director.add_message(thread["id"], "tool_call", "", {
        "call_id": "wide-call",
        "name": "wide_tool",
        "arguments": "{}",
    })
    _bulk_messages(
        director,
        thread["id"],
        [("assistant", f"chunk {index} " + ("x" * 6000), {}) for index in range(24)],
    )
    director.add_message(thread["id"], "tool_result", "", {
        "call_id": "wide-call",
        "name": "wide_tool",
        "output": "the delayed result still survives",
    })
    latest = director.latest_message_sequence(thread["id"])

    async def fake_complete(**_kwargs):
        return _reply(text="durable compacted summary")

    monkeypatch.setattr(models, "complete", fake_complete)
    hub = runtime.Runtime()
    boundaries: list[int] = []

    async def run() -> None:
        for _ in range(4):
            if int(director.get_thread(thread["id"])["compacted_through"]) >= latest:
                return
            assert await hub.compact_thread(thread["id"]) is True
            boundaries.append(int(director.get_thread(thread["id"])["compacted_through"]))

    asyncio.run(run())
    assert boundaries == sorted(set(boundaries))
    assert boundaries[0] > 0
    assert boundaries[-1] == latest


def test_thread_payload_counts_compacted_rows_older_than_newest_5000(director):
    from director import server

    agent = _make_agent(director)
    thread = director.create_thread(agent["id"])
    director.add_message(thread["id"], "user", "old compacted input")
    compacted_through = director.latest_message_sequence(thread["id"])
    _bulk_messages(
        director,
        thread["id"],
        [("assistant", "newer row", {}) for _ in range(5000)],
    )
    director.save_compaction(thread["id"], "summary", compacted_through)

    payload = server._thread_payload(thread["id"])

    assert len(payload["messages"]) == 5000
    assert all(int(row["sequence"]) > compacted_through
               for row in payload["messages"])
    assert payload["thread"]["hidden_count"] == 1
