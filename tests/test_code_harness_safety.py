from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import code_jobs


@pytest.fixture
def harness_job(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    job = code_jobs.CodeJob("harness-safety", tmp_path / "jobs" / "harness-safety")
    job.directory.mkdir(parents=True, exist_ok=True)
    job.events_path.touch()
    job.save(
        id=job.id,
        cwd=str(project),
        provider="openrouter",
        model="test/tool-model",
        reasoning="off",
        fast=False,
        provider_sessions=[],
        session_kind="code",
    )
    monkeypatch.setattr(job, "_model_context_tokens", lambda _provider, _model: 128_000)
    monkeypatch.setattr(
        job,
        "configured_role",
        lambda name, meta=None: {
            "enabled": name == "scout",
            "model": "test/scout-model",
            "reasoning": "off",
            "fast": True,
        },
    )
    return job, project


def _call(name: str, **arguments):
    return {
        "id": f"call-{name}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _terminal_message(
    content: str,
    *,
    finish_reason: str = "stop",
    stream_complete: bool = True,
    tool_calls: list[dict] | None = None,
):
    message = {
        "role": "assistant",
        "content": content,
        "finish_reason": finish_reason,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "done": True,
        "message": message,
        "usage": {},
        "finish_reason": finish_reason,
        "stream_complete": stream_complete,
    }


def _tool_names(tools: list[dict]) -> set[str]:
    return {
        str((tool.get("function") or {}).get("name") or "")
        for tool in tools
    }


def test_optional_tools_are_loaded_through_one_authoritative_gateway(harness_job):
    job, project = harness_job
    job._configure_turn_policy("Implement the requested repository change", strategy="auto")
    job.reset_turn_discipline("coder_led")

    initial_tools = job._ollama_tools("Implement the requested repository change")
    initial = _tool_names(initial_tools)
    selector = next(
        tool["function"] for tool in initial_tools
        if tool["function"]["name"] == code_jobs.TOOL_SELECTOR_NAME
    )
    available = set(selector["parameters"]["properties"]["names"]["items"]["enum"])

    assert {"read_file", "search_text", "edit_file", "run_shell", "select_tools"} <= initial
    assert {"web_search", "repo_map", "spawn_agent"}.isdisjoint(initial)
    assert {"web_search", "repo_map", "spawn_agent"} <= available
    assert "consult" not in available
    denied = json.loads(job._guard_before_tool("web_search", {"query": "docs"}))
    assert denied["guardrail"] == "tool_not_enabled"

    receipt = job._execute_tool_calls(
        project,
        [_call("select_tools", names=["web_search", "repo_map"])],
        "test",
    )[0]
    assert json.loads(receipt["result"])["loaded"] == ["web_search", "repo_map"]

    expanded = _tool_names(job._ollama_tools("Implement the requested repository change"))
    assert {"web_search", "repo_map"} <= expanded
    assert job._guard_before_tool("web_search", {"query": "docs"}) == ""


def test_dynamic_tool_loading_has_a_full_schema_ablation_switch(harness_job, monkeypatch):
    job, _project = harness_job
    job._configure_turn_policy("Implement the requested repository change", strategy="auto")
    job.reset_turn_discipline("coder_led")
    monkeypatch.setattr(code_jobs, "DYNAMIC_TOOL_LOADING", False)

    names = _tool_names(job._ollama_tools("Implement the requested repository change"))

    assert {"web_search", "repo_map", "spawn_agent"} <= names
    assert "select_tools" not in names


def test_openrouter_refreshes_schemas_after_select_tools(harness_job, monkeypatch):
    import openrouter_client

    job, _project = harness_job
    job._configure_turn_policy("Research the implementation docs", strategy="auto")
    job.reset_turn_discipline("coder_led")
    monkeypatch.setattr(openrouter_client, "provider_status", lambda **_kwargs: (True, "ready"))
    offered = []

    def fake_stream(_messages, _model, **kwargs):
        names = _tool_names(kwargs.get("tools") or [])
        offered.append(names)
        if len(offered) == 1:
            yield _terminal_message(
                "",
                finish_reason="tool_calls",
                tool_calls=[_call("select_tools", names=["web_search"])],
            )
        else:
            yield _terminal_message("Capability loaded.")

    monkeypatch.setattr(openrouter_client, "stream_chat", fake_stream)
    outcome, summary = job._run_openrouter("Research the implementation docs", [])

    assert outcome == "completed"
    assert summary == "Capability loaded."
    assert "web_search" not in offered[0]
    assert "select_tools" in offered[0]
    assert "web_search" in offered[1]


def test_dense_mutating_turn_gets_one_same_coder_acceptance_audit(harness_job, monkeypatch):
    import openrouter_client

    job, _project = harness_job
    job._edits_applied = 1
    monkeypatch.setattr(openrouter_client, "provider_status", lambda **_kwargs: (True, "ready"))
    requests = []
    request = (
        "Build the release artifact exactly as specified.\n\nInputs:\n\n"
        "* Read package metadata from the existing project manifest.\n"
        "* Keep source ordering deterministic across operating systems.\n"
        "* Report invalid configuration through the documented exception.\n"
        "* Isolate concurrent build directories from one another.\n"
        "* Run the focused packaging check and leave its fixture unchanged.\n\n"
        + "The resulting archive format and command output are part of the public interface. " * 8
    )

    def fake_stream(messages, _model, **_kwargs):
        requests.append([dict(row) for row in messages])
        yield _terminal_message("Initial conclusion." if len(requests) == 1 else "Audited and fixed.")

    monkeypatch.setattr(openrouter_client, "stream_chat", fake_stream)
    outcome, summary = job._run_openrouter(request, [])

    assert outcome == "completed"
    assert summary == "Audited and fixed."
    assert len(requests) == 2
    assert "one allowed acceptance audit" in requests[1][-1]["content"]
    assert sum(
        "one allowed acceptance audit" in str(row.get("content") or "")
        for row in requests[1]
    ) == 1


def test_small_edit_does_not_pay_for_acceptance_audit(harness_job, monkeypatch):
    import openrouter_client

    job, _project = harness_job
    job._edits_applied = 1
    monkeypatch.setattr(openrouter_client, "provider_status", lambda **_kwargs: (True, "ready"))
    calls = 0

    def fake_stream(_messages, _model, **_kwargs):
        nonlocal calls
        calls += 1
        yield _terminal_message("Done.")

    monkeypatch.setattr(openrouter_client, "stream_chat", fake_stream)
    outcome, summary = job._run_openrouter("Change one CSS colour in app.css.", [])

    assert (outcome, summary) == ("completed", "Done.")
    assert calls == 1


def test_acceptance_audit_sees_tracked_shell_mutations(harness_job):
    job, _project = harness_job
    request = (
        "Implement the release workflow.\n\nInputs:\n"
        "1. Read the declared package version.\n"
        "2. Produce a deterministic archive.\n"
        "3. Reject an invalid destination.\n"
        "4. Preserve executable metadata.\n\n"
        + "The command output and archive layout are public behavior. " * 10
    )
    job._edits_applied = 0
    job._verification_ledger.mark_mutation("build.ps1", "after", previous_hash="before")
    history = []

    assert job._queue_acceptance_audit(history, request) is True
    assert history[-1]["role"] == "user"


def test_acceptance_audit_eligibility_uses_pre_injection_operator_request(harness_job):
    job, _project = harness_job
    operator_request = "Change the status label in the named template. Do not run tests or probes."
    job._configure_turn_policy(operator_request, strategy="auto")
    job.reset_turn_discipline("coder_led")
    job._edits_applied = 1
    injected_payload = (
        operator_request
        + "\n\n<plan>\nInputs:\n"
        + "\n".join(f"- Generated navigation clause {index}." for index in range(12))
        + "\n"
        + "Generated plan detail that is not an operator acceptance clause. " * 12
        + "\n</plan>"
    )

    assert job._acceptance_contract_density(injected_payload)["dense"] is True
    assert job._queue_acceptance_audit([], injected_payload) is False


def test_acceptance_audit_quotes_and_enforces_original_operator_constraints(harness_job):
    job, _project = harness_job
    operator_request = (
        "Implement the bounded release workflow.\n\nAcceptance:\n"
        "1. Read the existing manifest without replacing it.\n"
        "2. Preserve deterministic source ordering.\n"
        "3. Reject invalid destinations through the documented exception.\n"
        "4. Keep concurrent output directories isolated.\n"
        "5. Leave all fixtures byte-for-byte unchanged.\n\n"
        "Do not run or create any tests, checks, or probes during this task. "
        + "Use only the implementation and evidence already supplied by the operator. " * 8
    )
    job._configure_turn_policy(operator_request, strategy="auto")
    job.reset_turn_discipline("coder_led")
    job._edits_applied = 1
    history = []

    assert job._queue_acceptance_audit(
        history,
        operator_request + "\n\n<named_file_metadata>\n- generated-only.py\n</named_file_metadata>",
    ) is True

    instruction = history[-1]["content"]
    assert operator_request in instruction
    assert "Do not run or create tests, checks, probes" in instruction
    assert "use only the changed implementation and evidence already available" in instruction
    assert "run the smallest missing probes" not in instruction


def test_acceptance_audit_has_real_round_and_tool_boundaries(harness_job, monkeypatch):
    job, _project = harness_job
    job.reset_turn_discipline("coder_led")
    job._edits_applied = 1
    monkeypatch.setattr(code_jobs, "ACCEPTANCE_AUDIT_MAX_ROUNDS", 1)
    monkeypatch.setattr(code_jobs, "ACCEPTANCE_AUDIT_MAX_TOOL_CALLS", 1)
    request = (
        "Implement the release workflow.\n\nInputs:\n"
        "1. Read the package version.\n2. Build a deterministic archive.\n"
        "3. Reject invalid output.\n4. Preserve executable metadata.\n\n"
        + "The archive and command output are public behavior. " * 12
    )

    assert job._queue_acceptance_audit([], request) is True
    job._begin_acceptance_audit_round()
    assert job._turn_force_finalize is False
    job._begin_acceptance_audit_round()
    assert job._turn_force_finalize is True

    job._turn_force_finalize = False
    assert job._guard_before_tool("read_file", {"relative_path": "one.py"}) == ""
    blocked = json.loads(job._guard_before_tool("read_file", {"relative_path": "two.py"}))
    assert blocked["guardrail"] == "acceptance_audit_tool_cap"
    assert job._turn_force_finalize is True


@pytest.mark.parametrize("terminal_outcome", ["incomplete", "stopped"])
def test_noncompleted_turn_persists_final_harness_counters(
    harness_job,
    monkeypatch,
    terminal_outcome,
):
    job, _project = harness_job
    monkeypatch.setattr(job, "_runtime_warnings", lambda *_args: None)

    def fake_run(_payload, _attachments):
        job._turn_model_tokens = 500
        job._turn_model_token_budget = 500
        job._turn_tool_calls = 9
        job._no_progress_calls = 3
        job._progress_state = "blocked"
        job._progress_blocked_reason = "bounded test stop"
        job._completion_acceptance_audit_rounds = 2
        return terminal_outcome, "Bounded handoff."

    monkeypatch.setattr(job, "_run_openrouter", fake_run)

    job._run_locked("Implement the requested bounded change.", planned=False, strategy="direct")

    meta = job.load()
    assert meta["status"] == terminal_outcome
    assert meta["progress"] == {
        "state": "blocked",
        "no_progress_calls": 3,
        "productive_calls": 0,
        "objective_progress_calls": 0,
        "tool_calls": 9,
        "empty_search_run": 0,
        "redirects": 0,
        "blocked_reason": "bounded test stop",
        "model_tokens": 500,
        "model_token_budget": 500,
        "acceptance_audit_rounds": 2,
    }


def test_openrouter_text_length_is_never_reported_complete(harness_job, monkeypatch):
    import openrouter_client

    job, _project = harness_job
    monkeypatch.setattr(openrouter_client, "provider_status", lambda **_kwargs: (True, "ready"))

    def fake_stream(_messages, _model, **_kwargs):
        yield _terminal_message("Partial answer", finish_reason="length")

    monkeypatch.setattr(openrouter_client, "stream_chat", fake_stream)

    outcome, summary = job._run_openrouter("Explain the result", [])

    assert outcome == "incomplete"
    assert "Partial answer" in summary
    assert "finish_reason=length" in summary


def test_openrouter_eof_never_executes_an_assembled_tool_call(harness_job, monkeypatch):
    import openrouter_client

    job, project = harness_job
    monkeypatch.setattr(openrouter_client, "provider_status", lambda **_kwargs: (True, "ready"))
    monkeypatch.setattr(openrouter_client, "get_api_key", lambda: "test-key")
    monkeypatch.setattr(code_jobs, "OPENROUTER_MAX_TOOL_ROUNDS", 1)
    monkeypatch.setattr(code_jobs, "LARGE_MAX_TOOL_ROUNDS", 1)

    requests = []

    class TruncatedResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            chunk = {
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "id": "call-eof-write",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps({
                                    "relative_path": "must-not-exist.txt",
                                    "content": "unsafe",
                                }),
                            },
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
            }
            yield f"data: {json.dumps(chunk)}\n".encode("utf-8")
            # Deliberately no `data: [DONE]`: the transport ended early.

    def fake_urlopen(request, **_kwargs):
        requests.append(request)
        return TruncatedResponse()

    executed = []
    original_execute = job._execute_tool_calls

    def record_execution(*args, **kwargs):
        executed.append((args, kwargs))
        return original_execute(*args, **kwargs)

    monkeypatch.setattr(openrouter_client, "urlopen", fake_urlopen)
    monkeypatch.setattr(job, "_execute_tool_calls", record_execution)

    outcome, _summary = job._run_openrouter("Write must-not-exist.txt", [])

    assert outcome == "incomplete"
    assert requests
    assert executed == []
    assert not (project / "must-not-exist.txt").exists()


def test_openrouter_missing_finish_reason_never_executes_tool_call(harness_job, monkeypatch):
    import openrouter_client

    job, project = harness_job
    monkeypatch.setattr(openrouter_client, "provider_status", lambda **_kwargs: (True, "ready"))
    calls = 0

    def fake_stream(_messages, _model, **_kwargs):
        nonlocal calls
        calls += 1
        yield {
            "done": True,
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [_call(
                    "write_file",
                    relative_path="must-not-exist-missing-finish.txt",
                    content="unsafe",
                )],
            },
            "usage": {},
            "stream_complete": True,
            # Deliberately missing finish_reason.
        }

    monkeypatch.setattr(openrouter_client, "stream_chat", fake_stream)
    executed = []
    original_execute = job._execute_tool_calls

    def record_execution(*args, **kwargs):
        executed.append((args, kwargs))
        return original_execute(*args, **kwargs)

    monkeypatch.setattr(job, "_execute_tool_calls", record_execution)

    outcome, summary = job._run_openrouter("Write the file safely", [])

    assert outcome == "incomplete"
    assert "No partial call was executed" in summary
    assert calls == 2
    assert executed == []
    assert not (project / "must-not-exist-missing-finish.txt").exists()


def test_openrouter_reasoning_only_eof_retries_then_executes_complete_edit(
    harness_job, monkeypatch,
):
    import openrouter_client

    job, project = harness_job
    monkeypatch.setattr(openrouter_client, "provider_status", lambda **_kwargs: (True, "ready"))
    monkeypatch.setattr(code_jobs, "PROVIDER_INCOMPLETE_STREAM_RETRIES", 1)
    monkeypatch.setattr(code_jobs.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(job, "_completion_verification_gate", lambda _project: {"allowed": True})
    requests: list[list[dict]] = []

    def fake_stream(messages, _model, **_kwargs):
        requests.append(json.loads(json.dumps(messages)))
        if len(requests) == 1:
            yield {
                "done": False,
                "delta": {
                    "content": "",
                    "reasoning": "I located the archive restore branch and know the required edit.",
                    "tool_calls": [],
                },
            }
            yield {
                "done": True,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning": "I located the archive restore branch and know the required edit.",
                },
                "usage": {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
                "finish_reason": "",
                "stream_complete": False,
            }
        elif len(requests) == 2:
            yield _terminal_message(
                "",
                finish_reason="tool_calls",
                tool_calls=[_call(
                    "write_file",
                    relative_path="restored.txt",
                    content="restored\n",
                )],
            )
        else:
            yield _terminal_message("Implemented and verified the restore path.")

    monkeypatch.setattr(openrouter_client, "stream_chat", fake_stream)

    outcome, summary = job._run_openrouter("Create restored.txt with the restored value", [])

    assert outcome == "completed"
    assert summary == "Implemented and verified the restore path."
    assert (project / "restored.txt").read_text(encoding="utf-8") == "restored\n"
    assert len(requests) == 3
    assert "previous provider stream ended" in requests[1][-1]["content"]
    assert "previous provider stream ended" not in json.dumps(requests[2])
    rounds = job.load()["model_request_rounds"]
    assert [(row["round"], row["attempt"], row["status"]) for row in rounds] == [
        (1, 1, "incomplete"),
        (1, 2, "completed"),
        (2, 1, "completed"),
    ]
    assert rounds[0]["stop_reason"] == "eof"
    events = [json.loads(line) for line in job.events_path.read_text(encoding="utf-8").splitlines()]
    assert any("OpenRouter stream ended" in event.get("text", "") for event in events)


def test_openrouter_terminal_answer_is_not_replaced_by_a_verification_round(
    harness_job, monkeypatch,
):
    import openrouter_client

    job, _project = harness_job
    monkeypatch.setattr(openrouter_client, "provider_status", lambda **_kwargs: (True, "ready"))
    monkeypatch.setattr(
        job,
        "_completion_verification_gate",
        lambda _project: pytest.fail("terminal answers must not enter a verification model round"),
    )
    requests = []

    def fake_stream(messages, _model, **_kwargs):
        requests.append(json.loads(json.dumps(messages)))
        yield _terminal_message("Here is the requested design.")

    monkeypatch.setattr(openrouter_client, "stream_chat", fake_stream)

    outcome, summary = job._run_openrouter("Brainstorm the design", [])

    assert outcome == "completed"
    assert summary == "Here is the requested design."
    assert len(requests) == 1
    assert "completion_gate" not in json.dumps(requests)


def test_openrouter_repeated_reasoning_only_eof_is_bounded_incomplete(
    harness_job, monkeypatch,
):
    import openrouter_client

    job, project = harness_job
    monkeypatch.setattr(openrouter_client, "provider_status", lambda **_kwargs: (True, "ready"))
    monkeypatch.setattr(code_jobs, "PROVIDER_INCOMPLETE_STREAM_RETRIES", 1)
    monkeypatch.setattr(code_jobs.time, "sleep", lambda _seconds: None)
    requests = 0

    def fake_stream(_messages, _model, **_kwargs):
        nonlocal requests
        requests += 1
        yield {
            "done": False,
            "delta": {"content": "", "reasoning": f"partial reasoning {requests}", "tool_calls": []},
        }
        yield {
            "done": True,
            "message": {"role": "assistant", "content": "", "reasoning": "partial reasoning"},
            "usage": {},
            "finish_reason": "",
            "stream_complete": False,
        }

    monkeypatch.setattr(openrouter_client, "stream_chat", fake_stream)

    outcome, summary = job._run_openrouter("Create restored.txt safely", [])

    assert outcome == "incomplete"
    assert "terminal marker" in summary
    assert requests == 2
    assert not (project / "restored.txt").exists()
    rounds = job.load()["model_request_rounds"]
    assert [(row["attempt"], row["status"], row["stop_reason"]) for row in rounds] == [
        (1, "incomplete", "eof"),
        (2, "incomplete", "eof"),
    ]


def test_ollama_reasoning_only_eof_retries_then_executes_complete_edit(
    harness_job, monkeypatch,
):
    import ollama_client

    job, project = harness_job
    job.save(provider="ollama", model="test/local-model")
    monkeypatch.setattr(ollama_client, "provider_status", lambda **_kwargs: (True, "ready"))
    monkeypatch.setattr(code_jobs, "PROVIDER_INCOMPLETE_STREAM_RETRIES", 1)
    monkeypatch.setattr(code_jobs.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(job, "_completion_verification_gate", lambda _project: {"allowed": True})
    requests: list[list[dict]] = []

    def fake_stream(messages, _model, **_kwargs):
        requests.append(json.loads(json.dumps(messages)))
        if len(requests) == 1:
            yield {"message": {"thinking": "I found the exact file and edit."}, "done": False}
        elif len(requests) == 2:
            yield {
                "message": {
                    "content": "",
                    "tool_calls": [_call(
                        "write_file",
                        relative_path="local-restored.txt",
                        content="restored locally\n",
                    )],
                },
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 12,
                "eval_count": 4,
            }
        else:
            yield {
                "message": {"content": "Implemented the local restore path."},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 10,
                "eval_count": 3,
            }

    monkeypatch.setattr(ollama_client, "stream_chat", fake_stream)

    outcome, summary = job._run_ollama("Create local-restored.txt", [])

    assert outcome == "completed"
    assert summary == "Implemented the local restore path."
    assert (project / "local-restored.txt").read_text(encoding="utf-8") == "restored locally\n"
    assert len(requests) == 3
    assert "previous provider stream ended" in requests[1][-1]["content"]
    rounds = job.load()["model_request_rounds"]
    assert [(row["round"], row["attempt"], row["status"]) for row in rounds] == [
        (1, 1, "incomplete"),
        (1, 2, "completed"),
        (2, 1, "completed"),
    ]
    events = [json.loads(line) for line in job.events_path.read_text(encoding="utf-8").splitlines()]
    assert any("Ollama stream ended" in event.get("text", "") for event in events)


def test_ollama_narration_then_eof_retries_before_any_tool_executes(
    harness_job, monkeypatch,
):
    import ollama_client

    job, project = harness_job
    job.save(provider="ollama", model="test/local-model")
    monkeypatch.setattr(ollama_client, "provider_status", lambda **_kwargs: (True, "ready"))
    monkeypatch.setattr(code_jobs, "PROVIDER_INCOMPLETE_STREAM_RETRIES", 1)
    monkeypatch.setattr(code_jobs.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(job, "_completion_verification_gate", lambda _project: {"allowed": True})
    requests: list[list[dict]] = []

    def fake_stream(messages, _model, **_kwargs):
        requests.append(json.loads(json.dumps(messages)))
        if len(requests) == 1:
            yield {
                "message": {"content": "I'll build the full calculator now."},
                "done": False,
            }
        elif len(requests) == 2:
            yield {
                "message": {
                    "content": "",
                    "tool_calls": [_call(
                        "write_file",
                        relative_path="calculator.html",
                        content="<main>ready</main>\n",
                        mode="overwrite",
                    )],
                },
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 12,
                "eval_count": 4,
            }
        else:
            yield {
                "message": {"content": "Created the calculator."},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 10,
                "eval_count": 3,
            }

    monkeypatch.setattr(ollama_client, "stream_chat", fake_stream)

    outcome, summary = job._run_ollama("Create calculator.html", [])

    assert outcome == "completed"
    assert summary == "Created the calculator."
    assert (project / "calculator.html").read_text(encoding="utf-8") == "<main>ready</main>\n"
    assert len(requests) == 3
    assert "before a complete tool call" in requests[1][-1]["content"]
    assert "mode=append" in requests[1][-1]["content"]


def test_ollama_terminal_answer_is_not_replaced_by_a_verification_round(
    harness_job, monkeypatch,
):
    import ollama_client

    job, _project = harness_job
    job.save(provider="ollama", model="test/local-model")
    monkeypatch.setattr(ollama_client, "provider_status", lambda **_kwargs: (True, "ready"))
    monkeypatch.setattr(
        job,
        "_completion_verification_gate",
        lambda _project: pytest.fail("terminal answers must not enter a verification model round"),
    )
    requests = []

    def fake_stream(messages, _model, **_kwargs):
        requests.append(json.loads(json.dumps(messages)))
        yield {
            "message": {"content": "Here is the requested local design."},
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 10,
            "eval_count": 3,
        }

    monkeypatch.setattr(ollama_client, "stream_chat", fake_stream)

    outcome, summary = job._run_ollama("Brainstorm the design", [])

    assert outcome == "completed"
    assert summary == "Here is the requested local design."
    assert len(requests) == 1
    assert "completion_gate" not in json.dumps(requests)


def test_ollama_missing_finish_reason_never_executes_tool_call(harness_job, monkeypatch):
    import ollama_client

    job, project = harness_job
    job.save(provider="ollama", model="test/local-model")
    monkeypatch.setattr(ollama_client, "provider_status", lambda **_kwargs: (True, "ready"))

    def fake_stream(_messages, _model, **_kwargs):
        yield {
            "message": {
                "content": "",
                "tool_calls": [_call(
                    "write_file",
                    relative_path="must-not-exist-local.txt",
                    content="unsafe",
                )],
            },
            "done": True,
            # Deliberately missing done_reason: transport completion alone is
            # not authority to mutate the project.
        }

    monkeypatch.setattr(ollama_client, "stream_chat", fake_stream)
    executed = []
    original_execute = job._execute_tool_calls

    def record_execution(*args, **kwargs):
        executed.append((args, kwargs))
        return original_execute(*args, **kwargs)

    monkeypatch.setattr(job, "_execute_tool_calls", record_execution)

    outcome, summary = job._run_ollama("Write must-not-exist-local.txt", [])

    assert outcome == "incomplete"
    assert "done_reason=missing" in summary
    assert executed == []
    assert not (project / "must-not-exist-local.txt").exists()


def test_review_session_denies_write_tools_at_schema_and_runtime(harness_job):
    job, project = harness_job
    job.save(session_kind="review")
    job.reset_turn_discipline("review")
    job._turn_enabled_tools = frozenset(code_jobs.REVIEW_TOOL_NAMES)

    offered = _tool_names(job._ollama_tools("Review the current change"))
    assert "write_file" not in offered
    assert "edit_file" not in offered
    assert "run_shell" not in offered

    result = job._execute_tool_calls(
        project,
        [_call("write_file", relative_path="review-write.txt", content="unsafe")],
        "review",
    )[0]
    payload = json.loads(result["result"])

    assert payload["blocked"] is True
    assert payload["guardrail"] == "review_tool_denied"
    assert not (project / "review-write.txt").exists()


def test_review_session_can_read_its_own_dossier_but_not_checkpoints(harness_job):
    job, project = harness_job
    job.save(session_kind="review")
    dossier = job.directory / "session-review-dossier.json"
    harness = job.directory / "HARNESS_CONTEXT.md"
    checkpoint = job.directory / "checkpoints" / "abc" / "checkpoint.json"
    checkpoint.parent.mkdir(parents=True)
    dossier.write_text('{"ok": true}', encoding="utf-8")
    harness.write_text("# harness\n", encoding="utf-8")
    checkpoint.write_text('{"id": "abc"}', encoding="utf-8")

    dossier_read = json.loads(job._ollama_run_tool(
        project, "read_file", {"relative_path": str(dossier)},
    ))
    harness_read = json.loads(job._ollama_run_tool(
        project, "read_file", {"relative_path": str(harness)},
    ))
    listed = json.loads(job._ollama_run_tool(
        project, "list_dir", {"relative_path": str(job.directory)},
    ))
    blocked = json.loads(job._ollama_run_tool(
        project, "read_file", {"relative_path": str(checkpoint)},
    ))

    assert '{"ok": true}' in dossier_read["content"]
    assert "# harness" in harness_read["content"]
    assert any(row.get("path", "").endswith("session-review-dossier.json") or "session-review-dossier.json" in row.get("path", "") for row in listed.get("entries") or [])
    assert "protected from coding file tools" in str(blocked.get("error") or "")


def test_normal_session_still_cannot_read_its_session_storage(harness_job):
    job, project = harness_job
    job.save(session_kind="code")
    secret = job.directory / "openrouter_messages.json"
    secret.write_text("[]", encoding="utf-8")

    result = json.loads(job._ollama_run_tool(
        project, "read_file", {"relative_path": str(secret)},
    ))
    assert "protected from coding file tools" in str(result.get("error") or "")


def test_file_tools_allow_explicit_cross_project_paths(harness_job, tmp_path):
    job, project = harness_job
    cases = [
        ("../outside-relative.txt", tmp_path / "outside-relative.txt"),
        (str(tmp_path / "outside-absolute.txt"), tmp_path / "outside-absolute.txt"),
    ]

    for requested_path, outside_path in cases:
        result = json.loads(job._ollama_run_tool(
            project,
            "write_file",
            {"relative_path": requested_path, "content": "cross-project"},
        ))
        assert result["ok"] is True
        assert outside_path.read_text(encoding="utf-8") == "cross-project"


def test_file_tools_still_protect_git_internals(harness_job, tmp_path):
    job, project = harness_job
    target = tmp_path / "other" / ".git" / "config"

    result = json.loads(job._ollama_run_tool(
        project,
        "write_file",
        {"relative_path": str(target), "content": "unsafe"},
    ))

    assert "Git internals are protected" in result["error"]
    assert not target.exists()


def test_explicit_direct_and_distributed_strategies_control_tool_availability(harness_job):
    job, _project = harness_job

    job._configure_turn_policy("Fix app.py typo", strategy="direct")
    direct = _tool_names(job._ollama_tools("Fix app.py typo"))
    assert {"read_file", "edit_file", "write_file", "run_shell"} <= direct
    assert {"repo_map", "update_plan", "spawn_agent"}.isdisjoint(direct)

    job._configure_turn_policy("Implement the requested change", strategy="distributed")
    distributed = _tool_names(job._ollama_tools("Implement the requested change"))
    assert {"update_plan", "spawn_agent", "select_tools"} <= distributed
    assert "repo_map" not in distributed


def test_subagent_cannot_execute_a_write_even_if_provider_emits_one(harness_job, monkeypatch):
    job, project = harness_job
    rounds = []

    def fake_round(_provider, _history, _model, tools):
        rounds.append(_tool_names(tools))
        if len(rounds) == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    _call("write_file", relative_path="subagent-write.txt", content="unsafe")
                ],
            }
        return {"role": "assistant", "content": "Read-only investigation complete."}

    monkeypatch.setattr(job, "_subagent_round", fake_round)

    job._spawn_agent_tool(project, {"objective": "Inspect the implementation"})

    assert rounds
    assert all(names <= code_jobs.SUBAGENT_TOOLS for names in rounds)
    assert not (project / "subagent-write.txt").exists()


def test_parallel_sibling_subagents_do_not_trip_the_recursion_guard(harness_job, monkeypatch):
    job, project = harness_job
    first_entered = threading.Event()
    both_entered = threading.Event()
    count_lock = threading.Lock()
    entered = 0

    def fake_round(_provider, _history, _model, _tools):
        nonlocal entered
        with count_lock:
            entered += 1
            first_entered.set()
            if entered == 2:
                both_entered.set()
        both_entered.wait(timeout=1.0)
        return {"role": "assistant", "content": "Independent report."}

    monkeypatch.setattr(job, "_subagent_round", fake_round)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(job._spawn_agent_tool, project, {"objective": "Inspect module A"})
        assert first_entered.wait(timeout=1.0)
        second = pool.submit(job._spawn_agent_tool, project, {"objective": "Inspect module B"})
        both_entered.wait(timeout=1.0)
        results = [json.loads(first.result(timeout=2.0)), json.loads(second.result(timeout=2.0))]

    assert entered == 2
    assert all("error" not in result for result in results)


def test_repeated_empty_shell_results_request_a_review_without_blocking(harness_job):
    job, _project = harness_job
    job.reset_turn_discipline("direct")

    for index in range(code_jobs.NO_PROGRESS_BLOCK_CALLS):
        job._semantic_progress_result(
            "run_shell",
            json.dumps({"exit_code": 0, "output": "", "elapsed_seconds": index / 10}),
        )

    progress = job.load()["progress"]
    assert progress["state"] == "review"
    assert progress["no_progress_calls"] == code_jobs.NO_PROGRESS_BLOCK_CALLS


def test_repeated_identical_plan_results_request_a_review_without_blocking(harness_job):
    job, _project = harness_job
    job.reset_turn_discipline("planned")
    unchanged = json.dumps({"ok": True, "steps": 2, "completed": 0, "active": "inspect"})

    # The first plan is new state; each identical replay after it is not.
    for _index in range(code_jobs.NO_PROGRESS_BLOCK_CALLS + 1):
        job._semantic_progress_result("update_plan", unchanged)

    progress = job.load()["progress"]
    assert progress["state"] == "review"
    assert progress["no_progress_calls"] == code_jobs.NO_PROGRESS_BLOCK_CALLS


def test_distributed_strategy_uses_the_large_round_limit(harness_job, monkeypatch):
    import openrouter_client

    job, project = harness_job
    (project / "evidence.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    job._configure_turn_policy("Inspect independent workstreams", strategy="distributed")
    monkeypatch.setattr(code_jobs, "OPENROUTER_MAX_TOOL_ROUNDS", 1)
    monkeypatch.setattr(code_jobs, "LARGE_MAX_TOOL_ROUNDS", 3)
    monkeypatch.setattr(openrouter_client, "provider_status", lambda **_kwargs: (True, "ready"))
    offered = []

    def fake_stream(_messages, _model, **kwargs):
        has_tools = bool(kwargs.get("tools"))
        offered.append(has_tools)
        if has_tools:
            yield _terminal_message(
                "",
                finish_reason="tool_calls",
                tool_calls=[_call(
                    "read_file",
                    relative_path="evidence.txt",
                    start_line=len(offered),
                    max_lines=1,
                )],
            )
        else:
            yield _terminal_message("Verified incomplete handoff.")

    monkeypatch.setattr(openrouter_client, "stream_chat", fake_stream)

    outcome, summary = job._run_openrouter("Inspect independent workstreams", [])

    assert outcome == "incomplete"
    assert summary == "Verified incomplete handoff."
    assert offered == [True, True, True, False]


def test_reviewer_does_not_clobber_coder_progress_or_verification(harness_job, monkeypatch):
    import openrouter_client

    job, project = harness_job
    job._configure_turn_policy("Implement a multi-file feature", strategy="planned")
    job.reset_turn_discipline("planned")
    job._verification_ledger.mark_mutation("app.py", "content-hash", "passed", "python-ast")
    job._verification_ledger.record_command("python -m pytest -q tests/test_app.py", 0, "1 passed")
    job._semantic_progress_result(
        "run_shell",
        json.dumps({
            "exit_code": 0,
            "output": "1 passed",
            "verification": {"kind": "test", "status": "passed"},
        }),
    )
    verification_before = job._verification_ledger.snapshot()
    progress_before = dict(job.load()["progress"])

    monkeypatch.setattr(openrouter_client, "provider_status", lambda **_kwargs: (True, "ready"))

    def fake_review_stream(_messages, _model, **_kwargs):
        yield _terminal_message(json.dumps({
            "verdict": "pass",
            "summary": "Looks correct.",
            "findings": [],
            "unmet": [],
            "suggestions": [],
        }))

    monkeypatch.setattr(openrouter_client, "stream_chat", fake_review_stream)
    result = code_jobs.review_change(
        "Implement a multi-file feature",
        {
            "available": True,
            "files": ["app.py"],
            "untracked": [],
            "diff": "diff --git a/app.py b/app.py",
            "diff_truncated": False,
        },
        model="test/reviewer",
        runner=job,
        project=project,
    )

    assert result["verdict"] == "pass"
    assert job._verification_ledger.snapshot() == verification_before
    assert job.load()["progress"] == progress_before


@pytest.mark.parametrize("command", [
    "git reset --hard HEAD",
    "git clean -fdx",
    "git checkout -- src/app.py",
    "git checkout src/app.py",
    "git restore src/app.py",
    "git restore --staged src/app.py",
    "git stash push -u",
    "git stash",
    "git stash pop",
    "git push --force-with-lease origin main",
    "git branch -D recovery",
    "git switch --discard-changes main",
    "git add .",
    "git commit -am unsafe",
])
def test_destructive_git_commands_are_classified(command):
    assert code_jobs._destructive_git_operation(command)


@pytest.mark.parametrize("command", [
    "git status --short",
    "git log -3 --oneline",
    "git add src/app.py",
    "git commit -m safe",
    "git push origin main",
    "git worktree add ../publish origin/main",
    "git stash list",
    "git stash show stash@{0}",
])
def test_non_destructive_git_commands_remain_available(command):
    assert code_jobs._destructive_git_operation(command) == ""


def test_runtime_blocks_destructive_git_before_shell_execution(harness_job, monkeypatch):
    job, project = harness_job
    monkeypatch.setattr(code_jobs, "lean_harness", lambda: False)
    called = []
    original = job._ollama_run_tool

    def record(*args, **kwargs):
        called.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(job, "_ollama_run_tool", record)
    result = job._execute_tool_calls(
        project,
        [_call("run_shell", command="git reset --hard HEAD")],
        "protect the worktree",
    )[0]
    payload = json.loads(result["result"])

    assert payload["blocked"] is True
    assert payload["guardrail"] == "destructive_git_denied"
    assert called == [], "the shell executor must never receive the command"


def test_shell_output_reader_keeps_head_and_tail_without_loading_everything(tmp_path):
    output = tmp_path / "huge.stdout"
    output.write_bytes(b"HEAD" + (b"x" * 10_000) + b"TAIL")

    text, omitted = code_jobs._read_bounded_text(output, max_bytes=1_024)

    assert text.startswith("HEAD")
    assert text.endswith("TAIL")
    assert omitted > 0
    assert "output bytes omitted by aiOS" in text
    assert len(text) < 1_200


def test_run_shell_stops_a_process_that_exceeds_the_raw_output_budget(
    harness_job, monkeypatch,
):
    job, project = harness_job
    monkeypatch.setattr(code_jobs, "MAX_SHELL_RAW_OUTPUT_BYTES", 4_096)

    payload = json.loads(job._ollama_run_tool(
        project,
        "run_shell",
        {
            "command": "python -",
            "stdin": "import sys\nsys.stdout.write('x' * 100_000)\n",
            "timeout_seconds": 20,
        },
    ))

    assert payload["exit_code"] == 125
    assert payload["output_limit_exceeded"] is True
    assert "output limit" in payload["output"]


def test_artifacts_are_bounded_even_when_a_tool_returns_huge_text(
    harness_job, monkeypatch,
):
    job, _project = harness_job
    monkeypatch.setattr(code_jobs, "TOOL_OUTPUT_PREVIEW_CHARS", 100)
    monkeypatch.setattr(code_jobs, "MAX_TOOL_ARTIFACT_CHARS", 1_000)

    row = job._persist_tool_artifact("test", "a" * 10_000)

    assert row["omitted_chars"] == 9_000
    assert Path(row["path"]).stat().st_size < 1_100
