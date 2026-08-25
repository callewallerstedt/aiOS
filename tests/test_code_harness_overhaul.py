from __future__ import annotations

import json
from pathlib import Path

import pytest

import code_harness_policy
import code_jobs
import code_roles


@pytest.fixture
def job(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"
    config = tmp_path / "helper_config.json"
    config.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(code_jobs, "JOBS_DIR", jobs)
    monkeypatch.setattr(code_jobs, "CONFIG_PATH", config)
    monkeypatch.setattr(code_roles, "CONFIG_PATH", config)
    instance = code_jobs.CodeJob("overhaul")
    instance.directory.mkdir(parents=True, exist_ok=True)
    instance.events_path.touch()
    instance.save(
        id=instance.id,
        cwd=str(tmp_path),
        provider="openrouter",
        model="deepseek/deepseek-v4-flash",
        provider_sessions=[],
    )
    return instance


def _set_strategy(job, name: str) -> None:
    strategy = code_harness_policy.classify_task(f"[{name}] test")
    job._task_strategy = strategy
    job.save(task_strategy={
        "name": strategy.name,
        "reasons": strategy.reasons,
        "score": strategy.score,
        "use_scout": strategy.use_scout,
        "use_planner": strategy.use_planner,
        "allow_subagents": strategy.allow_subagents,
        "working_context_tokens": strategy.working_context_tokens,
    })
    job.reset_turn_discipline(name)


def _call(name: str, **arguments):
    return {
        "id": f"call-{name}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def test_revision_bound_robust_edit_records_diagnostics_and_rejects_stale_snapshot(job, tmp_path):
    _set_strategy(job, "direct")
    target = tmp_path / "app.py"
    target.write_bytes(b"def compute_total(items):\r\n    return len(items)\r\n")

    read = json.loads(job._ollama_run_tool(tmp_path, "read_file", {
        "relative_path": "app.py", "start_line": 1, "max_lines": 2,
    }))
    assert len(read["revision"]) == 16
    assert "\r" not in read["content"]

    target.write_bytes(target.read_bytes() + b"# external change\r\n")
    stale = json.loads(job._ollama_run_tool(tmp_path, "edit_file", {
        "relative_path": "app.py",
        "old_text": "def compute_total(item):",
        "new_text": "def compute_total(rows):",
        "expected_revision": read["revision"],
    }))
    assert stale["reason"] == "stale_revision"

    read = json.loads(job._ollama_run_tool(tmp_path, "read_file", {
        "relative_path": "app.py", "start_line": 1, "max_lines": 3,
    }))
    edited = json.loads(job._ollama_run_tool(tmp_path, "edit_file", {
        "relative_path": "app.py",
        "old_text": "def compute_total(item):",
        "new_text": "def compute_total(rows):",
        "expected_revision": read["revision"],
    }))
    assert edited["ok"] is True
    assert edited["match_kind"] == "fuzzy"
    assert edited["confidence"] >= 0.95
    assert edited["diagnostic"]["status"] == "passed"
    assert job._verification_ledger.decision("direct")["allowed"] is True


def test_line_reads_page_losslessly_when_the_char_budget_truncates(job, tmp_path):
    """``next_line`` must describe the lines actually returned.

    A window that fits ``max_lines`` but not ``max_chars`` used to report the
    whole window as delivered.  The reader then resumed past content it never
    saw, and the turn's read coverage marked those lines as already read, so
    every later attempt to fetch them was refused as a duplicate.
    """
    _set_strategy(job, "direct")
    target = tmp_path / "wide.js"
    body = "".join(f"// line {index:04d} {'x' * 200}\n" for index in range(400))
    target.write_text(body, encoding="utf-8")

    page = json.loads(job._ollama_run_tool(tmp_path, "read_file", {
        "relative_path": "wide.js", "start_line": 1, "max_lines": 400,
    }))
    assert page["truncated"] is True
    assert page["total_lines"] == 400
    # The char budget stops this well short of 400 lines.
    assert page["next_line"] - page["start_line"] < 400
    assert page["content"].count("\n") == page["next_line"] - page["start_line"]
    assert page["content"].endswith("\n")

    pages = [page["content"]]
    while page["next_line"] <= page["total_lines"]:
        page = json.loads(job._ollama_run_tool(tmp_path, "read_file", {
            "relative_path": "wide.js",
            "start_line": page["next_line"],
            "max_lines": 400,
        }))
        pages.append(page["content"])
    assert "".join(pages) == body

    # Coverage is only claimed for delivered lines, so an unseen range is
    # still readable rather than refused as evidence the model already holds.
    job.reset_turn_discipline("direct")
    first = json.loads(job._execute_guarded_tool(tmp_path, {
        "id": "read-1", "name": "read_file",
        "args": {"relative_path": "wide.js", "start_line": 1, "max_lines": 400},
    }))
    tail = json.loads(job._execute_guarded_tool(tmp_path, {
        "id": "read-2", "name": "read_file",
        "args": {"relative_path": "wide.js", "start_line": 396, "max_lines": 10},
    }))
    assert not first.get("reused")
    assert not tail.get("reused")
    # 1-based line 400 holds the last body line, "// line 0399".
    assert "// line 0399" in tail["content"]
    assert tail["next_line"] == 401


def test_guardrail_refusals_surface_their_reason_on_the_activity_card(job, tmp_path):
    """A blocked call must not render as an empty "Read file failed" card."""
    _set_strategy(job, "direct")
    (tmp_path / "present.txt").write_text("evidence", encoding="utf-8")
    job._record_local_tool_result(
        "call-1", "read", "Read file", tmp_path, "read_file",
        {"relative_path": "present.txt"},
        code_jobs.CodeJob._guardrail_result("Inspection is paused.", "no_progress_transition"),
    )
    event = json.loads(job.events_path.read_text(encoding="utf-8").splitlines()[-1])
    assert event["phase"] == "failed"
    assert event["error"] == "Inspection is paused."
    assert event["output"] == "Inspection is paused."


def test_planned_change_requires_current_generation_explicit_verification(job, tmp_path):
    _set_strategy(job, "planned")
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")

    edited = json.loads(job._ollama_run_tool(tmp_path, "edit_file", {
        "relative_path": "app.py", "old_text": "value = 1", "new_text": "value = 2",
    }))
    assert edited["diagnostic"]["status"] == "passed"
    decision = job._verification_ledger.decision("planned")
    assert decision["allowed"] is False
    assert decision["requires_explicit_verification"] is True

    checked = json.loads(job._ollama_run_tool(tmp_path, "run_shell", {
        "command": "python -m py_compile app.py", "timeout_seconds": 30,
    }))
    assert checked["exit_code"] == 0
    assert checked["verification"]["status"] == "passed"
    assert job._verification_ledger.decision("planned")["allowed"] is True

    changed_again = json.loads(job._ollama_run_tool(tmp_path, "edit_file", {
        "relative_path": "app.py", "old_text": "value = 2", "new_text": "value = 3",
    }))
    assert changed_again["ok"] is True
    after_second_edit = job._verification_ledger.decision("planned")
    assert after_second_edit["state"] == "unverified"
    assert "Fresh syntax diagnostics passed" in after_second_edit["reason"]


def test_completion_gate_is_bounded_and_then_exhausts(job, tmp_path):
    _set_strategy(job, "planned")
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    result = json.loads(job._ollama_run_tool(tmp_path, "edit_file", {
        "relative_path": "app.py", "old_text": "value = 1", "new_text": "value = 2",
    }))
    assert result["ok"] is True

    gates = [
        job._completion_verification_gate()
        for _ in range(code_jobs.MAX_COMPLETION_VERIFICATION_BLOCKS + 2)
    ]
    assert sum(bool(gate["continuation"]) for gate in gates) == code_jobs.MAX_COMPLETION_VERIFICATION_BLOCKS
    assert gates[code_jobs.MAX_COMPLETION_VERIFICATION_BLOCKS]["exhausted"] is True
    assert gates[code_jobs.MAX_COMPLETION_VERIFICATION_BLOCKS]["allowed"] is False


def test_auto_compaction_leaves_headroom_below_its_own_trigger(job, monkeypatch):
    """Compacting back to the trigger makes the next tool result re-trigger it.

    A long turn then compacts on nearly every round, and each pass clears the
    read and search caches, so the model re-fetches ranges it just had.
    """
    captured = {}

    class _Allocation:
        working_tokens = 100_000

    monkeypatch.setattr(job, "_managed_context_budget", lambda _p: _Allocation())
    monkeypatch.setattr(
        code_jobs.code_harness_policy, "estimate_tokens",
        lambda text: len(str(text)) // 4,
    )

    def fake_compact(messages, char_limit):
        captured["char_limit"] = char_limit
        return [{"role": "user", "content": "x" * char_limit}]

    monkeypatch.setattr(job, "_compact_local_history", fake_compact)
    # Comfortably over the 50% trigger for a 100k working budget.
    messages = [{"role": "user", "content": "x" * 400_000}]
    job._auto_compact_local_history(messages, "openrouter", [])

    trigger_tokens = int(_Allocation.working_tokens * code_jobs.AUTO_COMPACT_THRESHOLD)
    resulting_tokens = captured["char_limit"] // 4
    assert resulting_tokens < trigger_tokens * 0.8, (
        f"compacted to {resulting_tokens} against a {trigger_tokens} trigger: too little headroom"
    )


def test_no_progress_review_keeps_inspection_available(job, tmp_path):
    _set_strategy(job, "direct")
    (tmp_path / "present.txt").write_text("evidence", encoding="utf-8")
    # The first no-match result is new evidence; only its identical replays
    # count toward the no-progress review threshold.
    for _ in range(code_jobs.NO_PROGRESS_BLOCK_CALLS + 1):
        job._execute_tool_calls(
            tmp_path,
            [_call("search_text", query="repeated-identical-probe", relative_path=".")],
            "progress",
        )
    assert job.load()["progress"]["state"] == "review"
    followup = json.loads(job._execute_tool_calls(
        tmp_path, [_call("read_file", relative_path="present.txt")], "progress",
    )[0]["result"])
    assert followup.get("guardrail") is None
    assert followup["content"] == "evidence"


def test_a_search_that_finds_nothing_still_counts_as_evidence(job, tmp_path):
    """Proving a symbol is absent is how an agent decides to write it."""
    _set_strategy(job, "direct")
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    # Unrelated names, so the overlapping-search guard is not what is measured.
    absent = [
        "recordTurnDiff", "flushPendingWidgets", "hydrateSidebar", "parseManifest",
        "buildLegendRow", "scheduleRepaint", "collapseGutter", "emitBadgeState",
        "resolveThemeToken",
    ]
    assert len(absent) > code_jobs.NO_PROGRESS_BLOCK_CALLS
    for query in absent:
        result = json.loads(job._execute_tool_calls(
            tmp_path,
            [_call("search_text", query=query, relative_path=".")],
            "progress",
        )[0]["result"])
        assert result.get("guardrail") is None, query
    assert job.load()["progress"]["state"] == "working"


def test_shell_spellings_of_a_read_are_recognized_as_inspection(job):
    """The pause must not be evadable by rephrasing the same read."""
    inspection = code_jobs.CodeJob._shell_is_source_inspection
    for command in (
        r'cd C:\repo; $l=Get-Content src\app.js; $l[10..40]',
        r'cd C:\repo; findstr /n /i "handler" src\app.js',
        r'cd C:\repo; $l=Get-Content src\app.js; $l | Select-String -Pattern "handler"',
        r'(Get-Content src\app.js)[0..20]',
    ):
        assert inspection({"command": command}) is True, command
    assert inspection({"command": 'echo "hello"'}) is False


def test_an_edit_carrying_its_revision_recovers_a_dropped_path(job, tmp_path):
    """A revision is a content hash of exactly one observed file."""
    _set_strategy(job, "direct")
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    read = json.loads(job._ollama_run_tool(tmp_path, "read_file", {
        "relative_path": "app.py", "start_line": 1, "max_lines": 5,
    }))

    edited = json.loads(job._ollama_run_tool(tmp_path, "edit_file", {
        "old_text": "value = 1", "new_text": "value = 2",
        "expected_revision": read["revision"],
    }))
    assert edited["ok"] is True
    assert target.read_text(encoding="utf-8") == "value = 2\n"

    # An unknown revision still has to ask rather than guess.
    missing = json.loads(job._ollama_run_tool(tmp_path, "edit_file", {
        "old_text": "value = 2", "new_text": "value = 3",
        "expected_revision": "0" * 16,
    }))
    assert "relative_path is required" in missing["error"]


def test_local_edits_report_changes_so_the_run_card_and_summary_update(job, tmp_path):
    """`changes` drives the run card rollup and the turn diff summary.

    Provider-native edits always emitted it; local tool edits did not, so the
    operator saw "Edited file" while the card and the summary stayed empty.
    """
    _set_strategy(job, "direct")
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    result = job._ollama_run_tool(tmp_path, "edit_file", {
        "relative_path": "app.py", "old_text": "value = 1", "new_text": "value = 2",
    })
    job._record_local_tool_result(
        "call-edit", "files", "Edited file", tmp_path, "edit_file",
        {"relative_path": "app.py"}, result,
    )
    event = json.loads(job.events_path.read_text(encoding="utf-8").splitlines()[-1])
    assert event["phase"] == "completed"
    assert [change["path"] for change in event["changes"]] == ["app.py"]
    assert event["changes"][0]["diff"]
    assert event["lines_added"] >= 1


def test_truncated_openrouter_tool_calls_are_never_executed(job, tmp_path, monkeypatch):
    import openrouter_client

    monkeypatch.setattr(openrouter_client, "provider_status", lambda **kwargs: (True, "ready"))
    monkeypatch.setattr(
        job,
        "_execute_tool_calls",
        lambda *args, **kwargs: pytest.fail("truncated tool call executed"),
    )

    def fake_stream(messages, model, **kwargs):
        yield {
            "done": True,
            "finish_reason": "length",
            "message": {
                "role": "assistant",
                "content": "",
                "finish_reason": "length",
                "tool_calls": [_call("write_file", relative_path="bad.txt", content="partial")],
            },
            "usage": {},
        }

    monkeypatch.setattr(openrouter_client, "stream_chat", fake_stream)
    outcome, summary = job._run_openrouter("Create bad.txt", [])
    assert outcome == "incomplete"
    assert "ended unsafely on two" in summary
    assert not (tmp_path / "bad.txt").exists()
    history = json.loads(job._openrouter_history_path().read_text(encoding="utf-8"))
    assert sum("truncated_tool_call" in str(row.get("content")) for row in history) == 2


def test_task_plan_persists_a_dag_with_only_one_active_step(job, tmp_path):
    result = json.loads(job._ollama_run_tool(tmp_path, "update_plan", {
        "explanation": "Implement and verify",
        "steps": [
            {"id": "edit", "step": "Edit engine", "status": "in_progress", "owner": "coder"},
            {"id": "test", "step": "Run tests", "status": "in_progress", "depends_on": ["edit"]},
        ],
    }))
    assert result["active"] == "edit"
    saved = job.load()["task_plan"]
    assert [row["status"] for row in saved["steps"]] == ["in_progress", "pending"]
    assert saved["steps"][1]["depends_on"] == ["edit"]


def test_gemini_moves_tool_descriptions_into_the_stable_system_prefix(job, tmp_path):
    job.save(model="google/gemini-3.1-pro-preview")
    tools = job._ollama_tools("Fix app.py typo")
    assert tools
    assert all("description" not in json.dumps(tool) for tool in tools)
    prompt = job._openrouter_system_prompt(tmp_path)
    assert "Gemini tool guidance (stable prefix)" in prompt
    assert "- edit_file:" in prompt


def test_gemini_keeps_descriptions_on_dynamically_loaded_tools(job):
    job.save(model="google/gemini-3.1-pro-preview")
    initial = job._ollama_tools("Research an external API before editing app.py")
    assert "web_search" not in {tool["function"]["name"] for tool in initial}

    receipt = json.loads(job._select_tools({"names": ["web_search"]}))
    assert receipt["loaded"] == ["web_search"]
    expanded = {
        tool["function"]["name"]: tool["function"]
        for tool in job._ollama_tools("Research an external API before editing app.py")
    }

    assert "description" in expanded["web_search"]
    assert "description" not in expanded["read_file"]


def test_large_typed_tool_result_is_externalized(job):
    raw = json.dumps({"ok": True, "content": "x" * (code_jobs.TOOL_OUTPUT_PREVIEW_CHARS * 3)})
    preview = json.loads(job._externalize_large_tool_result("read_file", raw))
    assert preview["artifact_note"]
    artifact = Path(preview["artifact"]["path"])
    assert artifact.is_file()
    assert len(preview["content"]) < len(json.loads(raw)["content"])


def test_write_file_preserves_existing_utf8_bom_and_crlf(job, tmp_path):
    target = tmp_path / "notes.txt"
    target.write_bytes(b"\xef\xbb\xbffirst\r\nsecond\r\n")

    result = json.loads(job._ollama_run_tool(tmp_path, "write_file", {
        "relative_path": "notes.txt",
        "content": "first\nupdated\n",
    }))

    assert result["ok"] is True
    assert target.read_bytes() == b"\xef\xbb\xbffirst\r\nupdated\r\n"


def test_write_file_builds_long_files_through_revision_bound_appends(job, tmp_path):
    first = json.loads(job._ollama_run_tool(tmp_path, "write_file", {
        "relative_path": "long.html",
        "content": "<main>\n",
        "mode": "overwrite",
    }))
    second = json.loads(job._ollama_run_tool(tmp_path, "write_file", {
        "relative_path": "long.html",
        "content": "  <h1>Calculator</h1>\n</main>\n",
        "mode": "append",
        "expected_revision": first["revision"],
    }))

    assert first["mode"] == "overwrite"
    assert second["mode"] == "append"
    assert second["chunk_bytes"] == len("  <h1>Calculator</h1>\n</main>\n".encode("utf-8"))
    assert second["bytes"] > second["chunk_bytes"]
    assert second["revision"] != first["revision"]
    assert (tmp_path / "long.html").read_text(encoding="utf-8") == (
        "<main>\n  <h1>Calculator</h1>\n</main>\n"
    )


def test_invalid_utf8_is_rejected_instead_of_exposing_replacement_text(job, tmp_path):
    (tmp_path / "binary.dat").write_bytes(b"ok\xffbad")

    result = json.loads(job._ollama_run_tool(tmp_path, "read_file", {
        "relative_path": "binary.dat",
    }))

    assert result["reason"] == "binary_or_invalid_utf8"


def test_native_provider_events_share_the_verification_ledger(job, tmp_path):
    _set_strategy(job, "planned")
    target = tmp_path / "native.py"
    target.write_text("value = 1\n", encoding="utf-8")

    job._handle_codex_item("item/completed", {
        "item": {
            "id": "native-edit",
            "type": "fileChange",
            "status": "completed",
            "changes": [{"path": "native.py", "kind": "update", "diff": "+value = 1"}],
        },
    })
    assert job._verification_ledger.decision("planned")["allowed"] is False

    command = "python -m py_compile native.py"
    job._handle_codex_item("item/started", {
        "item": {"id": "native-check", "type": "commandExecution", "command": command},
    })
    job._handle_codex_item("item/completed", {
        "item": {
            "id": "native-check",
            "type": "commandExecution",
            "status": "completed",
            "command": command,
            "exitCode": 0,
            "aggregatedOutput": "",
        },
    })

    assert job._verification_ledger.decision("planned")["allowed"] is True


def test_prior_unverified_change_is_visible_but_does_not_block_an_unrelated_followup(job, tmp_path):
    _set_strategy(job, "planned")
    target = tmp_path / "followup.py"
    target.write_text("value = 1\n", encoding="utf-8")
    job._record_mutation_state(tmp_path, target)
    generation = job._verification_ledger.generation

    job.reset_turn_discipline("planned", restore_verification=True)

    decision = job._verification_ledger.decision("planned")
    assert decision["generation"] == generation
    assert decision["allowed"] is True
    assert decision["source_paths"] == []
    assert decision["carried_source_paths"] == ["followup.py"]
    snapshot = job._verification_ledger.snapshot()
    assert snapshot["changed_path_hashes"] == {"followup.py": snapshot["carried_path_hashes"]["followup.py"]}
    assert snapshot["current_changed_path_hashes"] == {}


def test_shell_written_source_invalidates_prior_verification(job, tmp_path):
    _set_strategy(job, "planned")
    target = tmp_path / "shell_edit.py"
    target.write_text("value = 1\n", encoding="utf-8")
    job._record_mutation_state(tmp_path, target)
    job._verification_ledger.record_command(
        "python -m py_compile shell_edit.py", 0, ""
    )
    assert job._verification_ledger.decision("planned")["allowed"] is True

    result = json.loads(job._ollama_run_tool(tmp_path, "run_shell", {
        "command": "Set-Content -LiteralPath shell_edit.py -Value 'value = 2' -Encoding utf8",
    }))

    assert result["exit_code"] == 0
    assert result["mutated_paths"] == ["shell_edit.py"]
    assert job._verification_ledger.decision("planned")["allowed"] is False


def test_incomplete_shell_mutation_tracking_fails_closed_without_retry_loop(job, tmp_path):
    _set_strategy(job, "direct")
    job._mutation_tracking_incomplete = True

    gate = job._completion_verification_gate(tmp_path)

    assert gate["allowed"] is False
    assert gate["mutation_tracking_incomplete"] is True
    assert gate["continuation"] is False
    assert gate["exhausted"] is True


def test_fast_mode_trims_reasoning_but_never_discards_an_explicit_level():
    """Fast is a latency preference, not a reset of the operator's choice.

    A session configured for medium ran with reasoning off because Fast forced
    it, and the model is also told not to write prose between tool calls -- so
    the turn had no channel left in which to decide anything and simply kept
    calling tools.
    """
    resolve = code_jobs.resolve_turn_reasoning
    for level in ("medium", "high", "xhigh", "max"):
        assert resolve(level, True) == level, level
        assert resolve(level, False) == level, level
    # "low" is as deliberate a choice as "high"; Fast rewrites neither.
    assert resolve("low", True) == "low"
    assert resolve("low", False) == "low"
    # Only an absent or explicitly-off level means off.
    assert resolve("off", True) == "off"
    assert resolve(None, True) == "off"
    assert resolve("", True) == "off"
