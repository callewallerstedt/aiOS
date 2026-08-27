import inspect
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import code_jobs
import code_roles

_REAL_GENERATE_TITLE = code_jobs._generate_title


@pytest.fixture()
def isolated_jobs(tmp_path, monkeypatch):
    jobs_dir = tmp_path / "code_jobs"
    jobs_dir.mkdir()
    monkeypatch.setattr(code_jobs, "JOBS_DIR", jobs_dir)
    monkeypatch.setattr(code_jobs, "CAPABILITIES_CACHE", jobs_dir / "capabilities.json")
    config_path = tmp_path / "helper_config.json"
    config_path.write_text(json.dumps({"code_roles": {
        "scout": {"enabled": False},
        "planner": {"enabled": False},
        "reviewer": {"enabled": False},
    }}), encoding="utf-8")
    monkeypatch.setattr(code_jobs, "CONFIG_PATH", config_path)
    monkeypatch.setattr(code_roles, "CONFIG_PATH", config_path)
    monkeypatch.setattr(code_jobs, "provider_status", lambda provider: (True, f"{provider} ready"))
    capability_snapshot = {
        "ok": True,
        "providers": [
                {
                    "provider": "codex",
                    "ready": True,
                    "models": [
                        {
                            "id": "gpt-test",
                            "reasoning": ["low", "high"],
                            "default_reasoning": "low",
                            "fast": True,
                        }
                    ],
                },
                {
                    "provider": "claude",
                    "ready": True,
                    "models": [
                        {
                            "id": "sonnet",
                            "reasoning": ["low", "high"],
                            "default_reasoning": "high",
                            "fast": False,
                        }
                    ],
                },
        ],
    }
    monkeypatch.setattr(code_jobs, "capabilities", lambda force=False: capability_snapshot)
    monkeypatch.setattr(
        code_jobs,
        "_selection_capabilities",
        lambda provider: next(
            (row for row in capability_snapshot["providers"] if row["provider"] == provider),
            None,
        ),
    )
    monkeypatch.setattr(code_jobs, "_generate_title", lambda _job_id: None)
    with code_jobs._REGISTRY_LOCK:
        code_jobs._LIVE.clear()
    yield jobs_dir
    for job in list(code_jobs._LIVE.values()):
        try:
            job.stop()
        except Exception:
            pass
    with code_jobs._REGISTRY_LOCK:
        code_jobs._LIVE.clear()


def wait_for(job_id, state="completed", timeout=3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        meta = code_jobs.get_job(job_id) or {}
        if meta.get("status") == state:
            return meta
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not reach {state}: {code_jobs.get_job(job_id)}")


def _accept_without_worker(self, text, **_kwargs):
    self.append("user", text)
    return {"ok": True, "queued": True, "job": self.load()}


def test_requires_exact_provider_model_and_reasoning(isolated_jobs, tmp_path):
    assert not code_jobs.create_job("", str(tmp_path), "build it", "gpt-test", "low")["ok"]
    missing = code_jobs.create_job("codex", str(tmp_path), "build it", "", "low")
    assert missing["needs"] == ["model"]
    invalid = code_jobs.create_job("codex", str(tmp_path), "build it", "invented", "low")
    assert invalid["needs"] == ["model"]
    assert invalid["choices"] == ["gpt-test"]
    invalid_effort = code_jobs.create_job("codex", str(tmp_path), "build it", "gpt-test", "ultra")
    assert invalid_effort["needs"] == ["reasoning"]
    no_fast = code_jobs.create_job("claude", str(tmp_path), "build it", "sonnet", "high", fast=True)
    assert no_fast["needs"] == ["fast"]


def test_openrouter_selection_uses_only_cached_provider_catalog(monkeypatch):
    import openrouter_client

    monkeypatch.setattr(
        code_jobs,
        "capabilities",
        lambda force=False: pytest.fail("job validation must not probe every provider"),
    )
    monkeypatch.setattr(openrouter_client, "provider_status", lambda **_kwargs: (True, "ready"))
    refreshes = []
    monkeypatch.setattr(
        openrouter_client,
        "list_enabled_models",
        lambda *, refresh=False: refreshes.append(refresh) or [{
            "id": "vendor/model",
            "reasoning": ["off", "low"],
            "fast": True,
        }],
    )

    assert code_jobs.selection_error("openrouter", "vendor/model", "low", False) is None
    assert refreshes == [False]


def test_session_role_snapshot_does_not_drift_with_global_defaults(isolated_jobs, tmp_path, monkeypatch):
    monkeypatch.setattr(code_jobs.CodeJob, "send", _accept_without_worker)
    roles = code_roles.save_roles({
        "planner": {"enabled": True, "model": "planner/at-launch"},
        "reviewer": {"enabled": True, "model": "reviewer/at-launch"},
    }, {})
    created = code_jobs.create_job(
        "codex", str(tmp_path), "snapshot roles", "gpt-test", "low", role_config=roles,
    )
    job = code_jobs._get_job(created["job"]["id"])
    assert job.configured_role("planner")["model"] == "planner/at-launch"

    code_roles.CONFIG_PATH.write_text(json.dumps({"code_roles": {
        "planner": {"enabled": False, "model": "planner/later"},
    }}), encoding="utf-8")
    assert job.configured_role("planner")["model"] == "planner/at-launch"
    assert job.load()["role_config"] == roles


def test_same_coder_configuration_updates_roles_without_native_handoff(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("cfgsame")
    job.directory.mkdir(parents=True)
    roles = code_roles.save_roles({}, {})
    job.save(
        id=job.id, provider="codex", cwd=str(tmp_path), model="gpt-test",
        reasoning="low", fast=False, status="completed", role_config=roles,
    )
    updated = code_roles.save_roles({"reviewer": {"model": "reviewer/new"}}, {})
    result = job.apply_configuration(
        "codex", "gpt-test", "low", False, updated,
        config_id="quality", config_name="Quality",
    )
    assert result["ok"] is True and result["handoff"] is False
    assert result["job"]["config_id"] == "quality"
    assert result["job"]["role_config"]["reviewer"]["model"] == "reviewer/new"
    events = code_jobs.read_events(job.id)["events"]
    assert events[-1]["kind"] == "configuration_switch"


def test_changed_coder_configuration_uses_provider_handoff(isolated_jobs, tmp_path, monkeypatch):
    job = code_jobs.CodeJob("cfgswitch")
    job.directory.mkdir(parents=True)
    job.save(
        id=job.id, provider="codex", cwd=str(tmp_path), model="gpt-test",
        reasoning="low", fast=False, status="completed", role_config=code_roles.save_roles({}, {}),
    )
    seen = {}

    def handoff(provider, model, reasoning, fast, instruction, **kwargs):
        seen.update(provider=provider, model=model, reasoning=reasoning, fast=fast,
                    instruction=instruction, kwargs=kwargs)
        return {"ok": True, "handoff": {"id": "h1"}, "job": job.load()}

    monkeypatch.setattr(job, "handoff", handoff)
    roles = code_roles.save_roles({"coder": {"model": "sonnet", "reasoning": "high"}}, {})
    result = job.apply_configuration(
        "claude", "sonnet", "high", False, roles,
        config_id="claude-quality", config_name="Claude quality",
    )
    assert result["ok"] is True
    assert seen["provider"] == "claude" and seen["model"] == "sonnet"
    assert seen["kwargs"]["role_config"] == roles
    assert "Claude quality" in seen["instruction"]


def test_self_review_is_a_normal_session_with_complete_timestamped_dossier(isolated_jobs, tmp_path, monkeypatch):
    monkeypatch.setattr(code_jobs.CodeJob, "send", _accept_without_worker)
    source = code_jobs.CodeJob("source123")
    source.directory.mkdir(parents=True)
    roles = code_roles.save_roles({"reviewer": {"enabled": True}}, {})
    source.save(
        id=source.id, title="Original run", provider="codex", cwd=str(tmp_path),
        project_name=tmp_path.name, brief="fix it", model="gpt-test", reasoning="low",
        fast=False, status="completed", created_at=100.0, updated_at=102.0,
        role_config=roles, edited_files=["app.py"], provider_sessions=[], handoffs=[],
    )
    source.events_path.touch()
    source.append(
        "activity", "Ran command", activity_id="cmd-1", activity_type="command",
        phase="completed", command="pytest -q", output="1 passed", arguments={"command": "pytest -q"},
    )
    (source.directory / "openrouter_messages.json").write_text(
        json.dumps([
            {"role": "user", "content": "full original prompt"},
            {"role": "assistant", "content": None, "reasoning": "provider-returned trace"},
        ]), encoding="utf-8",
    )
    with code_jobs._REGISTRY_LOCK:
        code_jobs._LIVE[source.id] = source

    result = code_jobs.create_session_review(
        source.id, "codex", "gpt-test", "low", False, roles,
        config_id="review-quality", config_name="Review quality",
    )
    assert result["ok"] is True
    review = result["job"]
    assert review["session_kind"] == "review"
    assert review["source_job_id"] == source.id
    assert review["sidebar_group"] == "Session Reviews"
    directory = code_jobs.review_jobs_dir() / review["id"]
    assert directory.is_dir() and directory.parent.name == "reviews"
    dossier = json.loads((directory / "session-review-dossier.json").read_text(encoding="utf-8"))
    event = dossier["events"][0]
    assert event["sequence"] == 1 and event["ts_iso"]
    assert event["command"] == "pytest -q" and event["arguments"]["command"] == "pytest -q"
    assert dossier["provider_and_handoff_artifacts"]["openrouter_messages.json"][0]["content"] == "full original prompt"
    assert any("Provider-returned reasoning is included" in note for note in dossier["notes"])
    assert not any("not available to aiOS" in note for note in dossier["notes"])
    tool_names = {row["name"] for row in dossier["harness"]["tools"]}
    assert "run_shell" in tool_names and "read_file" in tool_names
    assert str(directory / "session-review-dossier.json") in review["brief"]
    assert any(row["id"] == review["id"] for row in code_jobs.list_jobs())


def test_followups_are_fifo_and_keep_native_session(isolated_jobs, tmp_path, monkeypatch):
    seen = []

    def fake_codex(self, payload, attachments):
        meta = self.load()
        if not meta.get("native_session_id"):
            self.save(native_session_id="native-123")
        seen.append(payload)
        time.sleep(0.04)
        return "completed", f"done {payload}"

    monkeypatch.setattr(code_jobs.CodeJob, "_run_codex", fake_codex)
    created = code_jobs.create_job("codex", str(tmp_path), "first", "gpt-test", "low")
    job_id = created["job"]["id"]
    code_jobs.send_message(job_id, "second")
    code_jobs.send_message(job_id, "third")
    wait_for(job_id)
    deadline = time.time() + 2
    while len(seen) < 3 and time.time() < deadline:
        time.sleep(0.02)
    assert seen == ["first", "second", "third"]
    assert code_jobs.get_job(job_id)["native_session_id"] == "native-123"


def test_codex_question_pauses_and_answer_returns_to_same_turn(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("questionjob")
    job.directory.mkdir()
    job.meta_path.write_text(
        json.dumps(
            {
                "id": job.id,
                "provider": "codex",
                "cwd": str(tmp_path),
                "model": "gpt-test",
                "reasoning": "low",
                "fast": False,
                "status": "running",
                "pending_question": "",
            }
        ),
        encoding="utf-8",
    )
    job.events_path.touch()
    code_jobs._LIVE[job.id] = job
    response = {}

    def request_thread():
        response.update(
            job._codex_server_request(
                {
                    "method": "item/tool/requestUserInput",
                    "params": {
                        "questions": [
                            {
                                "id": "choice",
                                "question": "Which database?",
                                "options": [{"label": "Postgres"}, {"label": "SQLite"}],
                            }
                        ]
                    },
                }
            )
        )

    thread = threading.Thread(target=request_thread)
    thread.start()
    deadline = time.time() + 1
    while job.question_waiter is None and time.time() < deadline:
        time.sleep(0.01)
    assert job.load()["status"] == "waiting_user"
    result = job.send("Postgres")
    assert result["answered"] is True
    thread.join(timeout=1)
    assert response == {"answers": {"choice": {"answers": ["Postgres"]}}}
    assert job.load()["pending_question"] == ""


def test_event_offsets_and_restart_recovery(isolated_jobs):
    job = code_jobs.CodeJob("restartjob")
    job.directory.mkdir()
    job.meta_path.write_text(
        json.dumps({"id": job.id, "status": "running", "updated_at": time.time()}),
        encoding="utf-8",
    )
    job.events_path.touch()
    job.append("status", "one")
    first = code_jobs.read_events(job.id, 0)
    job.append("result", "two")
    second = code_jobs.read_events(job.id, first["size"])
    assert [event["text"] for event in second["events"]] == ["two"]
    with code_jobs._REGISTRY_LOCK:
        code_jobs._LIVE.clear()
    code_jobs.recover_interrupted()
    assert code_jobs.get_job(job.id)["status"] == "interrupted"
    assert code_jobs.read_events(job.id, 0)["events"][-1]["state"] == "interrupted"


def test_event_reader_retries_partial_jsonl_tail(isolated_jobs):
    job = code_jobs.CodeJob("partialevent")
    job.save(id=job.id, status="running")
    job.append("status", "complete")
    complete_size = job.events_path.stat().st_size
    with job.events_path.open("ab") as handle:
        handle.write(b'{"kind":"status","text":"later"')

    first = code_jobs.read_events(job.id, 0)
    assert first["size"] == complete_size
    assert [event["text"] for event in first["events"]] == ["complete"]

    with job.events_path.open("ab") as handle:
        handle.write(b'}\n')
    second = code_jobs.read_events(job.id, first["size"])
    assert [event["text"] for event in second["events"]] == ["later"]


def test_windows_path_conversion():
    assert code_jobs.windows_to_wsl(r"C:\aiOS\project") == "/mnt/c/aiOS/project"


def test_codex_activity_stream_keeps_command_output_and_diffs(isolated_jobs):
    job = code_jobs.CodeJob("codexactivity")
    job.save(id=job.id, status="running")
    job._handle_codex_item(
        "item/started",
        {"item": {"id": "cmd-1", "type": "commandExecution", "command": "pytest -q", "cwd": r"C:\aiOS", "status": "inProgress"}},
    )
    assert job._handle_codex_progress(
        "item/commandExecution/outputDelta",
        {"itemId": "cmd-1", "delta": "test_one PASSED\n"},
    )
    job._handle_codex_item(
        "item/completed",
        {"item": {"id": "cmd-1", "type": "commandExecution", "command": "pytest -q", "status": "completed", "aggregatedOutput": "1 passed", "exitCode": 0, "durationMs": 812}},
    )
    job._handle_codex_item(
        "item/completed",
        {"item": {"id": "files-1", "type": "fileChange", "status": "completed", "changes": [{"path": "app.py", "kind": "update", "diff": "@@\n-old\n+new"}]}},
    )
    assert job._handle_codex_progress(
        "turn/diff/updated",
        {"turnId": "turn-1", "diff": "diff --git a/app.py b/app.py\n-old\n+new"},
    )

    events = code_jobs.read_events(job.id, 0)["events"]
    command = [event for event in events if event.get("activity_id") == "cmd-1"]
    assert [event["phase"] for event in command] == ["started", "update", "completed"]
    assert command[1]["delta"] == "test_one PASSED\n"
    assert command[-1]["exit_code"] == 0
    files = next(event for event in events if event.get("activity_id") == "files-1")
    assert files["activity_type"] == "files"
    assert files["changes"][0]["diff"].endswith("+new")
    assert any(event.get("activity_type") == "diff" for event in events)


def test_native_command_without_session_cwd_never_scans_the_repository_root(isolated_jobs, monkeypatch):
    job = code_jobs.CodeJob("nativecommandnocwd")
    job.save(id=job.id, status="running")
    scanned = []
    monkeypatch.setattr(job, "_shell_workspace_snapshot", lambda project: scanned.append(project) or {})

    started = time.monotonic()
    job._begin_native_command("cmd-1", "pytest -q")
    job._finish_native_command("cmd-1", exit_code=0, output="1 passed", elapsed_seconds=0.1)

    assert scanned == []
    assert time.monotonic() - started < 1.0
    evidence = (job.load().get("verification") or {}).get("evidence") or []
    assert evidence[-1]["command"] == "pytest -q"
    assert evidence[-1]["status"] == "passed"


def test_normal_followup_keeps_live_turn_running_and_exposes_queue_count(isolated_jobs):
    job = code_jobs.CodeJob("livefollowupqueue")
    job.directory.mkdir(parents=True, exist_ok=True)
    job.save(id=job.id, status="running", queued=0)
    job._worker_running = True
    job.turn_lock.acquire()
    try:
        job._queue_payload("do this after the current work", planned=True, strategy="auto")
    finally:
        job.turn_lock.release()

    meta = job.load()
    assert meta["status"] == "running"
    assert meta["queued"] == 1


def test_local_provider_followups_enter_the_next_model_round_fifo(
    isolated_jobs, monkeypatch,
):
    monkeypatch.setattr(code_jobs, "selection_error", lambda *_args, **_kwargs: None)
    job = code_jobs.CodeJob("inturnfollowups")
    job.directory.mkdir(parents=True, exist_ok=True)
    job.save(
        id=job.id,
        provider="openrouter",
        model="vendor/model",
        reasoning="off",
        status="running",
        queued=0,
        user_turns=0,
    )
    job._begin_in_turn_followups()

    first = job.send("add the API test")
    second = job.send("and keep the old behavior compatible")

    assert first["injected_next_round"] is True
    assert second["injected_next_round"] is True
    assert job._messages.empty()
    assert job.load()["queued"] == 2

    history = [{"role": "assistant", "content": "current round"}]
    assert job._inject_in_turn_followups(history, "OpenRouter") is True
    assert history[-2:] == [
        {"role": "user", "content": "add the API test"},
        {"role": "user", "content": "and keep the old behavior compatible"},
    ]
    assert job.load()["status"] == "running"
    assert job.load()["queued"] == 0


def test_closed_model_boundary_rejects_late_in_turn_enqueue(isolated_jobs):
    job = code_jobs.CodeJob("closedfollowups")
    job.directory.mkdir(parents=True, exist_ok=True)
    job.save(id=job.id, status="running", queued=0)
    job._begin_in_turn_followups()

    assert job._inject_in_turn_followups([], "OpenRouter", close_if_empty=True) is False
    assert job._queue_in_turn_followup(
        "too late for this turn", [], planned=True, strategy="auto",
    ) is False


def test_openrouter_live_followup_is_used_by_the_next_request(
    isolated_jobs, tmp_path, monkeypatch,
):
    import openrouter_client

    monkeypatch.setattr(code_jobs, "selection_error", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(openrouter_client, "provider_status", lambda **_kwargs: (True, "ready"))
    requests = []
    job = code_jobs.CodeJob("openrouterlivefollowup")
    job.directory.mkdir(parents=True, exist_ok=True)
    job.save(
        id=job.id,
        provider="openrouter",
        cwd=str(tmp_path),
        model="vendor/model",
        reasoning="off",
        fast=False,
        status="running",
        queued=0,
        user_turns=1,
        role_config={},
    )
    job.reset_turn_discipline("direct")
    job._begin_in_turn_followups()

    def stream_chat(messages, *_args, **_kwargs):
        requests.append(json.loads(json.dumps(messages)))
        if len(requests) == 1:
            delivered = job.send("also cover the retry path")
            assert delivered["injected_next_round"] is True
            answer = "I was about to finish."
        else:
            answer = "Implemented both requests."
        yield {
            "done": True,
            "message": {"role": "assistant", "content": answer, "finish_reason": "stop"},
            "finish_reason": "stop",
            "stream_complete": True,
            "usage": {},
        }

    monkeypatch.setattr(openrouter_client, "stream_chat", stream_chat)

    outcome, summary = job._run_openrouter("make the change", [])

    assert outcome == "completed"
    assert summary == "Implemented both requests."
    assert len(requests) == 2
    assert requests[1][-2:] == [
        {"role": "assistant", "content": "I was about to finish."},
        {"role": "user", "content": "also cover the retry path"},
    ]
    assert job.load()["queued"] == 0


def test_native_command_with_session_cwd_still_tracks_shell_mutations(isolated_jobs, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    job = code_jobs.CodeJob("nativecommandworkspace")
    job.save(id=job.id, status="running", cwd=str(workspace))

    job._begin_native_command("cmd-1", "python generate.py")
    (workspace / "generated.py").write_text("answer = 42\n", encoding="utf-8")
    job._finish_native_command("cmd-1", exit_code=0, output="done", elapsed_seconds=0.1)

    verification = job.load().get("verification") or {}
    assert list(verification.get("changed_path_hashes") or {}) == ["generated.py"]
    assert verification["generation"] == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows directory watcher integration")
def test_non_git_shell_mutation_tracking_does_not_walk_large_workspace(
    isolated_jobs, tmp_path, monkeypatch
):
    workspace = tmp_path / "large-non-git"
    workspace.mkdir()
    target = workspace / "settings.lua"
    target.write_text("return 1\n", encoding="utf-8")
    job = code_jobs.CodeJob("shellwatchlarge")
    job.directory.mkdir(parents=True, exist_ok=True)
    job.save(id=job.id, cwd=str(workspace), status="running")
    job.reset_turn_discipline("direct")

    def forbidden_walk(*_args, **_kwargs):
        raise AssertionError("healthy ReadDirectoryChangesW tracking must not scan the tree")

    monkeypatch.setattr(code_jobs.os, "walk", forbidden_walk)
    result = json.loads(job._ollama_run_tool(workspace, "run_shell", {
        "command": "Set-Content -LiteralPath settings.lua -Value 'return 2' -Encoding utf8",
    }))

    assert result["exit_code"] == 0
    assert result["mutated_paths"] == ["settings.lua"]
    assert result["mutation_tracking_engine"] == "read_directory_changes_w"
    assert result["mutation_event_count"] >= 1
    assert result["mutation_tracking_seconds"] < 2.0
    assert 0 <= result["process_seconds"] <= result["elapsed_seconds"]
    assert job._verification_ledger.generation == 1


def test_git_shell_snapshot_uses_event_tracking_without_scanning_runtime_cache(
    isolated_jobs, tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    job = code_jobs.CodeJob("shellruntimecache")
    job.directory.mkdir(parents=True, exist_ok=True)

    (workspace / "generated.py").write_text("answer = 42\n", encoding="utf-8")
    cache = workspace / "__pycache__"
    cache.mkdir()
    (cache / "generated.cpython-314.pyc").write_bytes(b"runtime cache")

    snapshot = job._shell_workspace_snapshot(workspace)

    assert snapshot["kind"] == "watch"
    assert snapshot["tracker"] is not None


def test_run_shell_redirects_python_bytecode_out_of_workspace(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("shellpycache")
    job.directory.mkdir(parents=True, exist_ok=True)
    (tmp_path / "sample.py").write_text("answer = 42\n", encoding="utf-8")

    result = json.loads(job._ollama_run_tool(
        tmp_path, "run_shell", {"command": "python -m py_compile sample.py"},
    ))

    assert result["exit_code"] == 0
    assert not (tmp_path / "__pycache__").exists()
    assert (job.directory / ".python-cache").is_dir()


def test_direct_scratch_check_cleanup_preserves_net_source_verification(isolated_jobs, tmp_path):
    workspace = tmp_path / "scratch-cleanup"
    workspace.mkdir()
    app = workspace / "app.py"
    app.write_text("answer = 41\n", encoding="utf-8")
    job = code_jobs.CodeJob("scratch-cleanup")
    job.directory.mkdir(parents=True, exist_ok=True)
    job.save(
        id=job.id,
        cwd=str(workspace),
        provider="openrouter",
        status="running",
        explicit_verification_commands=["python scratch_test.py"],
        task_strategy={"name": "direct"},
    )
    job.reset_turn_discipline("direct")
    job._task_strategy = code_jobs.code_harness_policy.classify_task(
        "[direct] update app.py and run `python scratch_test.py`"
    )
    job._turn_explicit_verification_commands = frozenset({
        code_jobs._verification_command_key("python scratch_test.py")
    })

    observed = json.loads(job._ollama_run_tool(workspace, "read_file", {
        "relative_path": "app.py",
    }))
    edited = json.loads(job._ollama_run_tool(workspace, "edit_file", {
        "relative_path": "app.py",
        "old_text": "answer = 41",
        "new_text": "answer = 42",
        "expected_revision": observed["revision"],
    }))
    created = json.loads(job._ollama_run_tool(workspace, "write_file", {
        "relative_path": "scratch_test.py",
        "content": "import app\nassert app.answer == 42\n",
    }))
    checked = json.loads(job._ollama_run_tool(workspace, "run_shell", {
        "command": "python scratch_test.py",
    }))
    generation = job._verification_ledger.generation
    removed = json.loads(job._ollama_run_tool(workspace, "run_shell", {
        "command": "Remove-Item -LiteralPath scratch_test.py",
    }))
    gate = job._completion_verification_gate(workspace)
    verification = job._verification_ledger.snapshot()

    assert edited["ok"] is True
    assert created["ok"] is True
    assert checked["exit_code"] == 0
    assert removed["exit_code"] == 0
    assert "scratch_test.py" in removed["mutated_paths"]
    assert not (workspace / "scratch_test.py").exists()
    assert job._verification_ledger.generation == generation
    assert verification["changed_path_hashes"] == {"app.py": edited["revision"]}
    assert verification["evidence"][-2]["command"] == "python scratch_test.py"
    assert verification["evidence"][-2]["status"] == "passed"
    assert gate["allowed"] is True
    assert gate["continuation"] is False
    assert gate["attempt"] == 0


def test_claude_activity_stream_normalizes_thinking_and_tool_results(isolated_jobs):
    job = code_jobs.CodeJob("claudeactivity")
    job.save(id=job.id, status="running")
    job._handle_claude_event({"type": "stream_event", "event": {"type": "message_start", "message": {"id": "msg-1"}}})
    job._handle_claude_event({"type": "stream_event", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}}})
    job._handle_claude_event({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Checking tests"}}})
    job._handle_claude_event({"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}})
    job._handle_claude_event({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"command": "pytest -q"}}]}})
    job._handle_claude_event({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "2 passed"}]}})

    events = code_jobs.read_events(job.id, 0)["events"]
    thinking = [event for event in events if event.get("activity_id") == "claude-msg-1-0"]
    assert [event["phase"] for event in thinking] == ["started", "update", "completed"]
    tool = [event for event in events if event.get("activity_id") == "tool-1"]
    assert [event["phase"] for event in tool] == ["started", "completed"]
    assert tool[0]["activity_type"] == tool[1]["activity_type"] == "command"
    assert tool[1]["output"] == "2 passed"


def test_thinking_stream_removes_provider_token_gaps_without_flattening_paragraphs(isolated_jobs):
    job = code_jobs.CodeJob("thinkingwhitespace")
    job.save(id=job.id, status="running")

    thinking = job.activity_delta(
        "thought-1",
        "thinking",
        "Thinking",
        "one\n\n\n\n two\n\nnormal paragraph",
        stream="summary",
    )
    command = job.activity_delta(
        "command-1",
        "command",
        "Running command",
        "one\n\n\n\n two",
    )

    assert thinking["delta"] == "one two\n\nnormal paragraph"
    assert command["delta"] == "one\n\n\n\n two"


def test_cursor_activity_stream_normalizes_tool_lifecycle(isolated_jobs):
    job = code_jobs.CodeJob("cursoractivity")
    job.save(id=job.id, status="running")
    job._handle_cursor_event({
        "type": "tool_call",
        "id": "cursor-tool-1",
        "tool_call": {"writeToolCall": {"args": {"path": "src/main.ts"}}},
    })
    job._handle_cursor_event({
        "type": "tool_result",
        "tool_call_id": "cursor-tool-1",
        "result": "updated src/main.ts",
    })

    events = code_jobs.read_events(job.id, 0)["events"]
    activity = [event for event in events if event.get("activity_id") == "cursor-tool-1"]
    assert [event["phase"] for event in activity] == ["started", "completed"]
    assert activity[0]["activity_type"] == activity[1]["activity_type"] == "files"
    assert activity[1]["output"] == "updated src/main.ts"


def test_cursor_uses_exact_discovered_model_without_synthetic_modifiers(isolated_jobs, tmp_path, monkeypatch):
    job = code_jobs.CodeJob("cursorexactmodel")
    job.save(
        id=job.id,
        provider="cursor",
        cwd=str(tmp_path),
        model="composer-2.5",
        reasoning="medium",
        fast=True,
        native_session_id="",
    )
    captured = {}

    def fake_stream(command, cwd, provider):
        captured.update(command=command, cwd=cwd, provider=provider)
        return "completed", "CURSOR_OK"

    monkeypatch.setattr(job, "_run_stream_process", fake_stream)

    assert job._run_cursor("Reply OK") == ("completed", "CURSOR_OK")
    model_index = captured["command"].index("--model") + 1
    assert captured["command"][model_index] == "composer-2.5"
    assert "[effort=" not in " ".join(captured["command"])
    assert captured["provider"] == "cursor"


def test_cursor_live_fragments_coalesce_and_thinking_completes(isolated_jobs):
    job = code_jobs.CodeJob("cursorpartials")
    job.save(id=job.id, status="running")
    for text in ("CUR", "SOR", "_OK"):
        job._handle_cursor_event({
            "type": "assistant",
            "timestamp_ms": 123,
            "message": {"content": [{"type": "text", "text": text}]},
        })
    job._handle_cursor_event({
        "type": "assistant",
        "timestamp_ms": 124,
        "message": {"content": [{"type": "text", "text": "CURSOR_OK"}]},
    })
    job._handle_cursor_event({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "CURSOR_OK"}]},
    })
    job._handle_cursor_event({"type": "thinking", "subtype": "delta", "text": "Checking"})
    job._handle_cursor_event({"type": "thinking", "subtype": "completed"})

    events = code_jobs.read_events(job.id, 0)["events"]
    assistant = [event for event in events if event["kind"] == "assistant"]
    assert "".join(event["text"] for event in assistant) == "CURSOR_OK"
    thinking = [event for event in events if event.get("activity_id") == "cursor-thinking"]
    assert [event["phase"] for event in thinking] == ["update", "completed"]


def test_cursor_idless_tool_events_update_one_activity_card(isolated_jobs):
    job = code_jobs.CodeJob("cursoridlesstool")
    job.save(id=job.id, status="running")
    arguments = {"globPattern": "**/*", "targetDirectory": "/mnt/c/project"}
    job._handle_cursor_event({
        "type": "tool_call",
        "tool_call": {"globToolCall": {"args": arguments}},
    })
    job._handle_cursor_event({
        "type": "tool_call",
        "tool_call": {"globToolCall": {"args": arguments, "result": {"files": ["app.py"]}}},
    })

    events = [event for event in code_jobs.read_events(job.id, 0)["events"] if event["kind"] == "activity"]
    assert len(events) == 2
    assert events[0]["activity_id"] == events[1]["activity_id"]
    assert [event["phase"] for event in events] == ["started", "completed"]


def test_cursor_model_parser_preserves_exact_runnable_ids():
    models = code_jobs._parse_cursor_models(
        "Available models\n"
        "auto - Auto (default)\n"
        "composer-2.5 - Composer 2.5\n"
        "gpt-5.6-sol-high-fast - GPT-5.6 Sol High Fast\n"
    )

    assert [row["id"] for row in models] == ["auto", "composer-2.5", "gpt-5.6-sol-high-fast"]
    assert models[0]["default"] is True
    assert models[1]["reasoning"] == ["auto"]
    assert models[1]["fast"] is False
    assert models[2]["reasoning"] == ["high"]
    assert models[2]["intrinsic_fast"] is True


def test_failed_provider_turn_emits_visible_terminal_error(isolated_jobs, tmp_path, monkeypatch):
    job = code_jobs.CodeJob("cursorfailure")
    job.directory.mkdir()
    job.save(
        id=job.id,
        provider="cursor",
        cwd=str(tmp_path),
        model="composer-2.5",
        reasoning="auto",
        fast=False,
        status="queued",
    )
    job.events_path.touch()
    monkeypatch.setattr(job, "_run_cursor", lambda _payload: ("failed", "Cursor could not start."))

    job._run_locked("test")

    assert job.load()["status"] == "failed"
    errors = [event for event in code_jobs.read_events(job.id, 0)["events"] if event["kind"] == "error"]
    assert errors[-1]["text"] == "Cursor could not start."
    assert errors[-1]["state"] == "failed"


def test_cursor_model_errors_are_short_and_actionable():
    error = code_jobs._friendly_provider_error(
        "cursor",
        "Cannot use this model: composer-2.5[effort=medium]. Available models: auto, composer-2.5, many-more",
    )
    assert error == (
        "Cannot use this model: composer-2.5[effort=medium]. "
        "Refresh CODE and choose one of Cursor's currently discovered model ids."
    )


def test_token_events_are_coalesced_before_reaching_any_ui():
    events = [
        {"kind": "assistant_delta", "role": "status", "delta": str(index), "text": str(index), "ts": index}
        for index in range(500)
    ]
    events += [
        {"kind": "activity", "activity_id": "thinking", "phase": "update", "stream": "summary", "delta": "a"},
        {"kind": "activity", "activity_id": "thinking", "phase": "update", "stream": "summary", "delta": "b"},
        {"kind": "activity", "activity_id": "thinking", "phase": "completed", "title": "Thought"},
    ]

    coalesced = code_jobs.coalesce_events(events)

    assert len(coalesced) == 3
    assert coalesced[0]["kind"] == coalesced[0]["role"] == "assistant"
    assert coalesced[0]["text"] == "".join(str(index) for index in range(500))
    assert coalesced[1]["delta"] == "ab"
    assert "_coalesce" not in coalesced[0]


def test_openrouter_assistant_chunks_are_coalesced_before_reaching_any_ui():
    coalesced = code_jobs.coalesce_events([
        {"kind": "assistant", "role": "assistant", "text": "One", "ts": 1},
        {"kind": "assistant", "role": "assistant", "text": " smooth", "ts": 2},
        {"kind": "assistant", "role": "assistant", "text": " message", "ts": 3},
    ])

    assert len(coalesced) == 1
    assert coalesced[0]["text"] == "One smooth message"


def test_local_file_tool_honors_offsets_and_reports_progress(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("offsetreader")
    target = tmp_path / "large.txt"
    target.write_text("0123456789\nline two\nline three\n", encoding="utf-8")

    by_chars = json.loads(job._ollama_run_tool(tmp_path, "read_file", {
        "relative_path": "large.txt", "offset": 5, "max_chars": 500,
    }))
    by_lines = json.loads(job._ollama_run_tool(tmp_path, "read_file", {
        "relative_path": "large.txt", "start_line": 2, "max_lines": 1, "max_chars": 500,
    }))

    assert by_chars["content"].startswith("56789")
    assert by_chars["offset"] == 5
    assert by_chars["next_offset"] > by_chars["offset"]
    assert by_lines["content"] == "line two\n"
    assert by_lines["next_line"] == 3


def test_local_tools_find_files_and_preserve_exact_utf8_bytes(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("smarttools")
    (tmp_path / "src").mkdir()
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "root.py").write_text("print('root')\n", encoding="utf-8")
    target = tmp_path / "src" / "app.py"
    target.write_bytes(b"\xef\xbb\xbffirst\r\nsecond\r\n")
    (tmp_path / "node_modules" / "ignored.py").write_text("ignored", encoding="utf-8")

    found = json.loads(job._ollama_run_tool(tmp_path, "find_files", {"pattern": "**/*.py"}))
    edited = json.loads(job._ollama_run_tool(tmp_path, "edit_file", {
        "relative_path": "src/app.py", "old_text": "second", "new_text": "changed",
    }))

    assert found["files"] == ["root.py", "src/app.py"]
    assert edited["ok"] is True
    assert target.read_bytes() == b"\xef\xbb\xbffirst\r\nchanged\r\n"


def test_local_edits_create_recoverable_checkpoints_and_diff_stats(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("checkpoints")
    job.save(id=job.id, cwd=str(tmp_path), provider_sessions=[])
    target = tmp_path / "app.py"
    target.write_text("old\n", encoding="utf-8")

    edited = json.loads(job._ollama_run_tool(tmp_path, "edit_file", {
        "relative_path": "app.py", "old_text": "old", "new_text": "new",
    }))
    job._record_local_tool_result(
        "edit-1", "files", "Edit file", tmp_path, "edit_file",
        {"relative_path": "app.py"}, json.dumps(edited),
    )
    restored = json.loads(job._ollama_run_tool(tmp_path, "restore_checkpoint", {
        "checkpoint_id": edited["checkpoint_id"],
    }))

    assert edited["ok"] is True
    assert "+new" in edited["diff"] and "-old" in edited["diff"]
    assert job.load()["files_edited"] == 1
    assert job.load()["lines_added"] == 1
    assert job.load()["lines_deleted"] == 1
    assert restored["ok"] is True
    assert target.read_text(encoding="utf-8") == "old\n"


def test_undo_job_restores_all_session_file_baselines(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("undoall")
    job.save(id=job.id, cwd=str(tmp_path), status="completed", provider_sessions=[])
    existing = tmp_path / "app.py"
    existing.write_text("original\n", encoding="utf-8")
    created = tmp_path / "new_file.py"

    first = json.loads(job._ollama_run_tool(tmp_path, "edit_file", {
        "relative_path": "app.py", "old_text": "original", "new_text": "changed-once",
    }))
    job._record_local_tool_result(
        "edit-1", "files", "Edit file", tmp_path, "edit_file",
        {"relative_path": "app.py"}, json.dumps(first),
    )
    second = json.loads(job._ollama_run_tool(tmp_path, "edit_file", {
        "relative_path": "app.py", "old_text": "changed-once", "new_text": "changed-twice",
    }))
    job._record_local_tool_result(
        "edit-2", "files", "Edit file", tmp_path, "edit_file",
        {"relative_path": "app.py"}, json.dumps(second),
    )
    written = json.loads(job._ollama_run_tool(tmp_path, "write_file", {
        "relative_path": "new_file.py", "content": "brand new\n",
    }))
    job._record_local_tool_result(
        "write-1", "files", "Write file", tmp_path, "write_file",
        {"relative_path": "new_file.py"}, json.dumps(written),
    )

    assert existing.read_text(encoding="utf-8") == "changed-twice\n"
    assert created.read_text(encoding="utf-8") == "brand new\n"
    assert job.load()["undoable_files"] == 2

    refused = code_jobs.undo_job(job.id, confirmed=False)
    assert refused["ok"] is False
    assert refused["needs_confirmation"] is True

    result = code_jobs.undo_job(job.id, confirmed=True)
    assert result["ok"] is True
    assert result["restored_count"] == 2
    assert existing.read_text(encoding="utf-8") == "original\n"
    assert not created.exists()
    meta = job.load()
    assert meta["undoable_files"] == 0
    assert meta["files_edited"] == 0
    assert meta["edited_files"] == []


def test_undo_job_refuses_active_sessions(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("undoactive")
    job.save(id=job.id, cwd=str(tmp_path), status="running", provider_sessions=[])
    target = tmp_path / "app.py"
    target.write_text("keep\n", encoding="utf-8")
    edited = json.loads(job._ollama_run_tool(tmp_path, "edit_file", {
        "relative_path": "app.py", "old_text": "keep", "new_text": "changed",
    }))
    assert edited["ok"] is True

    result = code_jobs.undo_job(job.id, confirmed=True)
    assert result["ok"] is False
    assert result["active"] is True
    assert target.read_text(encoding="utf-8") == "changed\n"


def test_provider_tool_capture_persists_undo_checkpoint(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("providerundo")
    job.save(id=job.id, cwd=str(tmp_path), status="completed", provider_sessions=[])
    target = tmp_path / "lib.py"
    target.write_text("before\n", encoding="utf-8")

    job.capture_tool_files("tool-1", ["lib.py"])
    target.write_text("after\n", encoding="utf-8")
    job.finalize_tool_files("tool-1")

    assert job.load()["undoable_files"] == 1
    result = code_jobs.undo_job(job.id, confirmed=True)
    assert result["ok"] is True
    assert target.read_text(encoding="utf-8") == "before\n"


def test_absolute_paths_outside_project_are_readable(isolated_jobs, tmp_path):
    project = tmp_path / "project"
    outside = tmp_path / "shared" / "notes.txt"
    project.mkdir()
    outside.parent.mkdir()
    outside.write_text("shared context", encoding="utf-8")
    job = code_jobs.CodeJob("outsidepath")

    result = json.loads(job._ollama_run_tool(project, "read_file", {
        "relative_path": str(outside), "max_chars": 500,
    }))

    assert result["content"] == "shared context"
    assert result["path"] == str(outside)


def test_usage_is_accumulated_across_provider_segments(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("telemetry")
    job.save(
        id=job.id,
        cwd=str(tmp_path),
        provider="openrouter",
        provider_sessions=[{"provider": "openrouter", "model": "test"}],
    )
    job.record_usage({"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.01}, tokens_per_second=10)
    job.record_usage({"prompt_tokens": 40, "completion_tokens": 10, "cost": 0.005}, tokens_per_second=5)

    meta = job.load()
    assert meta["usage"]["input_tokens"] == 140
    assert meta["usage"]["output_tokens"] == 30
    assert meta["usage"]["total_tokens"] == 170
    assert meta["estimated_cost_usd"] == pytest.approx(0.015)
    assert meta["tokens_per_second"] == 5


def test_completed_zero_cost_stage_does_not_absorb_later_role_usage(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("skippedstage")
    job.save(
        id=job.id,
        cwd=str(tmp_path),
        provider="openrouter",
        model="test/model",
        created_at=time.time(),
        provider_sessions=[{"provider": "openrouter", "model": "test/model"}],
    )

    job.pipeline_stage("scout", "completed", "Skipped - local map is sufficient")
    job.pipeline_stage("planner", "started", "Planning")
    job.record_usage({"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.01})
    job.pipeline_stage("planner", "completed", "Planned")

    roles = job.load()["role_usage"]
    assert roles["scout"]["usage"]["total_tokens"] == 0
    assert roles["scout"]["phase"] == "completed"
    assert roles["planner"]["usage"]["total_tokens"] == 120


def test_large_tool_history_compacts_without_losing_recent_request():
    history = [{"role": "system", "content": "rules"}]
    for index in range(20):
        history.extend([
            {"role": "user", "content": f"request {index}"},
            {"role": "assistant", "content": "working", "tool_calls": [{"id": f"t{index}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"t{index}", "content": "x" * 5000},
        ])

    compacted = code_jobs.CodeJob._compact_local_history(history, 20000)

    assert compacted[0]["role"] == "system"
    assert any("compacted" in str(row.get("content")).casefold() for row in compacted)
    assert any("request 19" in str(row.get("content")) for row in compacted)
    assert compacted[-1]["role"] == "tool"


def test_repeated_compaction_preserves_supplied_code_and_successful_edits():
    supplied = '"use client";\n' + ("const exactComponent = true;\n" * 360)
    edit_call = {
        "id": "edit-1", "type": "function",
        "function": {
            "name": "edit_file",
            "arguments": json.dumps({"relative_path": "web/js/question.js", "old_text": "old", "new_text": "new"}),
        },
    }
    history = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": supplied},
        {"role": "assistant", "content": "", "tool_calls": [edit_call]},
        {"role": "tool", "tool_call_id": "edit-1", "content": json.dumps({
            "ok": True, "changed": True, "path": "web/js/question.js", "lines_added": 20, "lines_deleted": 2,
        })},
    ]
    for index in range(24):
        call_id = f"read-{index}"
        history.extend([
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": call_id, "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"relative_path": f"file-{index}.py"})},
            }]},
            {"role": "tool", "tool_call_id": call_id, "content": json.dumps({
                "path": f"file-{index}.py", "start_line": 1, "next_line": 200, "content": "x" * 5000,
            })},
        ])

    once = code_jobs.CodeJob._compact_local_history(history, 36_000)
    continued = list(once)
    for index in range(24, 32):
        call_id = f"read-{index}"
        continued.extend([
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": call_id, "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"relative_path": f"file-{index}.py"})},
            }]},
            {"role": "tool", "tool_call_id": call_id, "content": json.dumps({
                "path": f"file-{index}.py", "start_line": 1, "next_line": 200, "content": "y" * 5000,
            })},
        ])
    twice = code_jobs.CodeJob._compact_local_history(continued, 28_000)
    summary = next(row["content"] for row in twice if str(row.get("content", "")).startswith("Compacted working state"))
    state = json.loads(summary.split("\n", 1)[1])
    assert supplied.strip() in state["active_user_requests"]
    assert state["active_user_requests"].count(supplied.strip()) == 1
    assert any("edit_file web/js/question.js" in fact for fact in state["durable_state"])


def test_compaction_does_not_retain_a_second_shrunken_user_request():
    supplied = "Implement this exact component:\n" + ("const important = true;\n" * 260)
    history = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": supplied},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "read-1",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"relative_path": "app.py"}),
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "read-1",
            "content": json.dumps({"path": "app.py", "content": "x" * 20_000}),
        },
    ]

    once = code_jobs.CodeJob._compact_local_history(history, 14_000)
    assert not any(row.get("role") == "user" and "older message compacted" in str(row.get("content")) for row in once)

    twice = code_jobs.CodeJob._compact_local_history(
        once + [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "read-2",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"relative_path": "other.py"}),
                    },
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "read-2",
                "content": json.dumps({"path": "other.py", "content": "y" * 20_000}),
            },
        ],
        12_000,
    )
    summary = next(row["content"] for row in twice if str(row.get("content", "")).startswith("Compacted working state"))
    state = json.loads(summary.split("\n", 1)[1])

    assert state["active_user_requests"] == [supplied.strip()]


def test_compaction_keeps_source_inspection_with_runtime_churn_as_evidence():
    call = {
        "id": "shell-1",
        "type": "function",
        "function": {
            "name": "run_shell",
            "arguments": json.dumps({"command": "rg -n needle ."}),
        },
    }
    tool_message = {
        "role": "tool",
        "tool_call_id": "shell-1",
        "content": json.dumps({
            "exit_code": 0,
            "output": "app.py:12:needle",
            "mutated_paths": ["aios-watchdog.log"],
        }),
    }

    category, fact = code_jobs.CodeJob._compacted_tool_fact(call, tool_message)

    assert category == "EVIDENCE"
    assert "rg -n needle" in fact


def test_historical_tool_argument_compaction_preserves_real_schema():
    raw = json.dumps({
        "relative_path": "scratch/check.py",
        "content": "x" * 8_000,
        "overwrite": True,
    })

    compacted = code_jobs.CodeJob._compact_historical_tool_arguments(raw)
    parsed = json.loads(compacted)

    assert parsed["relative_path"] == "scratch/check.py"
    assert parsed["overwrite"] is True
    assert "historical value compacted" in parsed["content"]
    assert "compacted" not in parsed


def test_auto_compaction_releases_read_evidence_no_longer_visible(
    isolated_jobs, tmp_path, monkeypatch,
):
    job = code_jobs.CodeJob("compactevidence")
    job.save(id=job.id, cwd=str(tmp_path), provider="openrouter", model="deepseek/test")
    job.reset_turn_discipline()
    job._seen_read_signatures.add("read_file:old")
    job._read_coverage["app.py"] = [{"kind": "char", "start": 0, "end": 9000, "total": 9000}]
    job._search_history.append({"query": "needle", "terms": ("needle",), "scope": "."})
    job._pending_evidence_notes["read_file:old"] = "old evidence"
    job._semantic_overlap_calls = 3
    monkeypatch.setattr(
        job,
        "_managed_context_budget",
        lambda provider: code_jobs.code_harness_policy.ContextBudget(32_000, 4_000, 16_000),
    )
    job._model_profile = code_jobs.code_harness_policy.resolve_model_profile("deepseek/test", 32_000)
    # Several reads, so compaction has older groups it can genuinely drop.
    # The newest group keeps its payload; the evidence that goes away is an
    # older one, and that is what releases the reuse guards.
    history = [{'role': 'system', 'content': 'rules'}]
    for index in range(4):
        history.append({'role': 'assistant', 'content': '', 'tool_calls': [{
            'id': f'read-{index}',
            'type': 'function',
            'function': {'name': 'read_file',
                         'arguments': json.dumps({'relative_path': f'app{index}.py'})},
        }]})
        history.append({'role': 'tool', 'tool_call_id': f'read-{index}',
                        'content': 'source\n' * 8_000})

    compacted = job._auto_compact_local_history(history, "openrouter", [])

    assert len(json.dumps(compacted)) < len(json.dumps(history))
    assert job._seen_read_signatures == set()
    assert job._read_coverage == {}
    assert job._search_history == []
    assert job._pending_evidence_notes == {}
    assert job._semantic_overlap_calls == 0


def test_local_history_compaction_is_a_hard_character_bound():
    history = [
        {"role": "system", "content": "rules\n" + ("x" * 60_000)},
        {"role": "user", "content": "keep the task goal"},
    ]

    compacted = code_jobs.CodeJob._compact_local_history(history, 12_000)

    assert len(json.dumps(compacted, ensure_ascii=False)) <= 12_000
    assert compacted[0]["role"] == "system"


def test_context_snapshot_reports_fill_for_openrouter_history(isolated_jobs, tmp_path, monkeypatch):
    monkeypatch.setattr(
        code_jobs.CodeJob,
        "_model_context_tokens",
        staticmethod(lambda provider, model: 1_000_000),
    )
    job = code_jobs.CodeJob("ctxsnap")
    job.save(
        id=job.id,
        cwd=str(tmp_path),
        provider="openrouter",
        model="deepseek/deepseek-v4-flash",
        status="waiting_user",
        provider_sessions=[{"provider": "openrouter", "model": "deepseek/deepseek-v4-flash"}],
    )
    with code_jobs._REGISTRY_LOCK:
        code_jobs._LIVE[job.id] = job
    history = [{"role": "system", "content": "rules"}]
    history.extend([
        {"role": "user", "content": "please fix the defaults"},
        {"role": "assistant", "content": "ok", "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "y" * 8000},
    ])
    job._save_openrouter_history(history)
    job._context_cache = None
    snap = job.context_snapshot()
    assert snap["usable"] is True
    assert snap["messages"] == 4
    assert snap["used_chars"] > 8000
    assert snap["budget_chars"] == 850_000 * 4
    assert snap["working_tokens"] == 850_000
    assert snap["window_tokens"] == 1_000_000
    assert snap["output_reserve_tokens"] == 150_000
    assert 0 <= snap["percent"] <= 100
    detail = code_jobs.get_job(job.id)
    assert detail["context"]["used_chars"] == snap["used_chars"]


def test_manual_compact_preserves_system_and_recent_turns(isolated_jobs, tmp_path, monkeypatch):
    job = code_jobs.CodeJob("ctxcompact")
    job.save(
        id=job.id,
        cwd=str(tmp_path),
        provider="openrouter",
        model="deepseek/deepseek-v4-flash",
        status="waiting_user",
        brief="build a thing",
        edited_files=["helper_overlay.py"],
        provider_sessions=[{"provider": "openrouter", "model": "deepseek/deepseek-v4-flash"}],
    )
    with code_jobs._REGISTRY_LOCK:
        code_jobs._LIVE[job.id] = job
    history = [{"role": "system", "content": "system rules stay"}]
    for index in range(16):
        history.extend([
            {"role": "user", "content": f"step {index}"},
            {"role": "assistant", "content": "working", "tool_calls": [{"id": f"t{index}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"t{index}", "content": ("blob " * 2000)},
        ])
    job._save_openrouter_history(history)
    monkeypatch.setattr(
        code_jobs,
        "_llm_compact_continuity",
        lambda messages, meta: (
            "GOAL\nbuild a thing\nCONSTRAINTS\nnone\nDONE\n- helper_overlay.py\n"
            "OPEN\n- finish\nFACTS\nnone\nDECISIONS\nnone\n"
        ),
    )

    before = job.context_snapshot()["used_chars"]
    result = code_jobs.compact_job_context(job.id, force=True)
    assert result["ok"] is True
    after_history = job._load_openrouter_history()
    assert after_history[0]["content"] == "system rules stay"
    assert any("Compacted session continuity" in str(row.get("content")) for row in after_history)
    assert any("step 15" in str(row.get("content")) for row in after_history)
    assert job.context_snapshot()["used_chars"] < before


def test_local_tool_result_emits_one_structured_card_without_raw_json(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("structuredtool")
    job.save(id=job.id, status="running")
    job._record_local_tool_result(
        "shell-1", "command", "Run command", tmp_path, "run_shell",
        {"command": "pytest -q"}, json.dumps({"exit_code": 0, "output": "2 passed"}),
    )

    events = code_jobs.read_events(job.id, 0)["events"]
    assert len(events) == 1
    assert events[0]["kind"] == "activity"
    assert events[0]["title"] == "Ran command"
    assert events[0]["command"] == "pytest -q"
    assert events[0]["exit_code"] == 0
    assert events[0]["output"] == "2 passed"


def test_historical_cursor_tool_phases_share_one_activity_id():
    arguments = {"globPattern": "**/*", "targetDirectory": "/mnt/c/project"}
    coalesced = code_jobs.coalesce_events([
        {"kind": "activity", "activity_id": "old-start", "activity_type": "search", "phase": "started", "tool": "globToolCall", "arguments": arguments, "title": "Searching the codebase"},
        {"kind": "activity", "activity_id": "old-finish", "activity_type": "search", "phase": "completed", "tool": "globToolCall", "arguments": arguments, "title": "Searched the codebase"},
    ])

    assert len(coalesced) == 2
    assert coalesced[0]["activity_id"] == coalesced[1]["activity_id"] == "old-start"


def test_historical_cursor_assembled_snapshot_is_not_repeated():
    coalesced = code_jobs.coalesce_events([
        {"kind": "assistant_delta", "delta": "Hello"},
        {"kind": "assistant_delta", "delta": " world"},
        {"kind": "assistant_delta", "delta": "Hello world"},
    ])
    assert len(coalesced) == 1
    assert coalesced[0]["text"] == "Hello world"


def test_raw_provider_deltas_coalesce_only_with_the_same_request_and_stream():
    coalesced = code_jobs.coalesce_events([
        {"kind": "raw_model_delta", "request_id": "ollama:job:1", "raw_stream": "content", "delta": "plain "},
        {"kind": "raw_model_delta", "request_id": "ollama:job:1", "raw_stream": "content", "delta": "tokens"},
        {"kind": "raw_model_delta", "request_id": "ollama:job:1", "raw_stream": "thinking", "delta": "reasoning"},
        {"kind": "raw_model_delta", "request_id": "ollama:job:2", "raw_stream": "content", "delta": "next"},
    ])

    assert [(row["request_id"], row["raw_stream"], row["text"]) for row in coalesced] == [
        ("ollama:job:1", "content", "plain tokens"),
        ("ollama:job:1", "thinking", "reasoning"),
        ("ollama:job:2", "content", "next"),
    ]


def test_historical_local_tool_json_is_folded_into_clean_activity_card():
    events = [
        {
            "kind": "activity", "activity_id": "shell-old", "tool": "run_shell",
            "activity_type": "command", "phase": "started", "title": "Shell",
            "detail": 'powershell -Command "pytest -q"',
        },
        {
            "kind": "activity", "activity_id": "shell-old", "tool": "run_shell",
            "activity_type": "command", "phase": "completed", "title": "Shell",
            "detail": '{"exit_code": 1, "output": "one failed"}',
            "output": '{"exit_code": 1, "output": "one failed"}',
        },
        {
            "kind": "tool", "activity_id": "shell-old", "tool": "run_shell",
            "text": '{"exit_code": 1, "output": "one failed"}',
        },
    ]

    coalesced = code_jobs.coalesce_events(events)

    assert len(coalesced) == 2
    assert coalesced[-1]["title"] == "Command failed"
    assert coalesced[-1]["command"] == 'powershell -Command "pytest -q"'
    assert coalesced[-1]["exit_code"] == 1
    assert coalesced[-1]["output"] == "one failed"
    assert all(event["kind"] == "activity" for event in coalesced)


def test_provider_setup_does_not_launch_when_already_ready(isolated_jobs, monkeypatch):
    monkeypatch.setattr(code_jobs, "provider_status", lambda provider: (True, f"{provider} ready"))
    monkeypatch.setattr(code_jobs.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("setup must not launch"))
    result = code_jobs.setup_provider("codex")
    assert result == {"ok": True, "provider": "codex", "launched": False, "message": "codex ready"}


@pytest.mark.parametrize(
    "provider, expected",
    [
        ("claude", 'call "C:\\tools\\claude.CMD" auth login'),
        ("codex", 'call "C:\\tools\\codex.exe" login'),
        ("cursor", "wsl.exe -d %s -- %s login" % (code_jobs.WSL_DISTRO, code_jobs.CURSOR_AGENT)),
    ],
)
def test_provider_setup_launches_a_command_line_cmd_can_parse(monkeypatch, provider, expected):
    """cmd.exe cannot read the escaped quotes Python puts in argument lists."""
    monkeypatch.setattr(code_jobs.os, "name", "nt")
    monkeypatch.setattr(code_jobs, "provider_status", lambda name: (False, f"{name} needs sign-in"))
    monkeypatch.setattr(code_jobs, "find_claude", lambda: "C:\\tools\\claude.CMD")
    monkeypatch.setattr(code_jobs, "find_codex", lambda: "C:\\tools\\codex.exe")
    launched = []
    monkeypatch.setattr(code_jobs.subprocess, "Popen", lambda command, **_kwargs: launched.append(command))

    result = code_jobs.setup_provider(provider)

    assert result["launched"] is True
    assert len(launched) == 1
    command_line = launched[0]
    assert isinstance(command_line, str)
    assert command_line.startswith("cmd.exe /d /k title aiOS ")
    assert command_line.endswith(expected)
    assert '\\"' not in command_line


def test_import_does_not_recover_jobs_owned_by_another_process(tmp_path):
    jobs_dir = tmp_path / "live-jobs"
    job_dir = jobs_dir / "livejob"
    job_dir.mkdir(parents=True)
    job_file = job_dir / "job.json"
    job_file.write_text(json.dumps({"id": "livejob", "status": "running"}), encoding="utf-8")
    script = "import code_jobs; print(code_jobs.get_job('livejob')['status'])"
    env = dict(code_jobs.os.environ)
    env["AIOS_CODE_JOBS_DIR"] = str(jobs_dir)
    result = code_jobs.subprocess.run(
        [code_jobs.sys.executable, "-c", script],
        cwd=str(code_jobs.ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "running"
    assert json.loads(job_file.read_text(encoding="utf-8"))["status"] == "running"


def test_delete_waits_for_worker_and_does_not_leave_ghost(isolated_jobs, tmp_path, monkeypatch):
    release = threading.Event()

    def fake_codex(self, _payload, _attachments):
        release.wait(timeout=2)
        return "stopped", "Stopped."

    monkeypatch.setattr(code_jobs.CodeJob, "_run_codex", fake_codex)
    created = code_jobs.create_job("codex", str(tmp_path), "working", "gpt-test", "low")
    job_id = created["job"]["id"]
    deadline = time.time() + 1
    while (code_jobs.get_job(job_id) or {}).get("status") != "running" and time.time() < deadline:
        time.sleep(0.01)

    stopped = code_jobs.stop_job(job_id)
    assert stopped["job"]["status"] == "stopped"
    result = {}
    thread = threading.Thread(target=lambda: result.update(code_jobs.delete_job(job_id, confirmed=True)))
    thread.start()
    time.sleep(0.03)
    release.set()
    thread.join(timeout=2)

    assert result.get("ok") is True
    assert result.get("recoverable") is True
    assert not (isolated_jobs / job_id).exists()
    assert (isolated_jobs / ".trash" / result["trash_id"] / "job.json").exists()
    assert all(job.get("id") != job_id for job in code_jobs.list_jobs())


def test_delete_requires_confirmation_and_refuses_active_job(isolated_jobs, tmp_path, monkeypatch):
    release = threading.Event()

    def fake_codex(self, _payload, _attachments):
        release.wait(timeout=2)
        return "completed", "Done."

    monkeypatch.setattr(code_jobs.CodeJob, "_run_codex", fake_codex)
    created = code_jobs.create_job("codex", str(tmp_path), "working", "gpt-test", "low")
    job_id = created["job"]["id"]
    deadline = time.time() + 1
    while (code_jobs.get_job(job_id) or {}).get("status") != "running" and time.time() < deadline:
        time.sleep(0.01)

    unconfirmed = code_jobs.delete_job(job_id)
    assert unconfirmed.get("needs_confirmation") is True
    assert (isolated_jobs / job_id / "job.json").exists()

    active = code_jobs.delete_job(job_id, confirmed=True)
    assert active.get("active") is True
    assert (isolated_jobs / job_id / "job.json").exists()

    release.set()
    assert wait_for(job_id)["status"] == "completed"


def test_edit_file_tolerates_line_ending_mismatch(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("crlfedit")
    target = tmp_path / "app.py"
    target.write_bytes(b"first\r\nsecond line\r\nthird\r\n")

    edited = json.loads(job._ollama_run_tool(tmp_path, "edit_file", {
        "relative_path": "app.py",
        "old_text": "second line\nthird\n",
        "new_text": "changed line\nthird\n",
    }))

    assert edited["ok"] is True
    assert target.read_bytes() == b"first\r\nchanged line\r\nthird\r\n"


def test_edit_file_noop_explains_that_retrying_cannot_create_progress(isolated_jobs, tmp_path):
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    job = code_jobs.CodeJob("noopedit")

    result = json.loads(job._ollama_run_tool(tmp_path, "edit_file", {
        "relative_path": "app.py",
        "old_text": "value = 1",
        "new_text": "value = 1",
    }))

    assert result["ok"] is True and result["changed"] is False
    assert result["reason"] == "no_content_change"
    assert "Do not retry the same edit" in result["message"]


def test_edit_file_missing_match_points_at_nearest_line(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("nearmiss")
    target = tmp_path / "app.py"
    target.write_text("alpha\ndef compute_total(items):\nomega\n", encoding="utf-8")

    result = json.loads(job._ollama_run_tool(tmp_path, "edit_file", {
        "relative_path": "app.py",
        "old_text": "def compute_total(item):",
        "new_text": "def compute_total(rows):",
    }))

    assert "was not found" in result["error"]
    assert "line 2" in result["error"]


def test_openrouter_loop_repairs_missing_tool_call_ids(isolated_jobs, tmp_path, monkeypatch):
    import openrouter_client

    (tmp_path / "hello.txt").write_text("marker\n", encoding="utf-8")
    job = code_jobs.CodeJob("orids")
    job.directory.mkdir(parents=True, exist_ok=True)
    job.events_path.touch()
    job.save(id=job.id, provider="openrouter", cwd=str(tmp_path), model="test/model", provider_sessions=[])

    calls = {"count": 0}

    def fake_stream(messages, model, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            yield {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": json.dumps({"relative_path": "hello.txt"})},
                    }],
                },
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                "finish_reason": "tool_calls",
                "done": True,
            }
        else:
            yield {
                "message": {"role": "assistant", "content": "All done."},
                "usage": {},
                "finish_reason": "stop",
                "done": True,
            }

    monkeypatch.setattr(openrouter_client, "provider_status", lambda **kwargs: (True, "ready"))
    monkeypatch.setattr(openrouter_client, "stream_chat", fake_stream)

    outcome, summary = job._run_openrouter("read the file", [])

    assert outcome == "completed"
    assert summary == "All done."
    history = json.loads(job._openrouter_history_path().read_text(encoding="utf-8"))
    assistant = next(m for m in history if m.get("tool_calls"))
    tool_reply = next(m for m in history if m.get("role") == "tool")
    assert assistant["tool_calls"][0]["id"]
    assert tool_reply["tool_call_id"] == assistant["tool_calls"][0]["id"]
    assert job.load()["model_request_count"] == 2
    raw_events = [row for row in code_jobs.read_events(job.id, 0)["events"] if row["kind"].startswith("raw_model_")]
    assert [row["kind"] for row in raw_events] == ["raw_model_tool", "raw_model_delta"]
    assert raw_events[-1]["text"] == "All done."


def test_openrouter_loop_retries_transient_stream_errors(isolated_jobs, tmp_path, monkeypatch):
    import openrouter_client

    job = code_jobs.CodeJob("orretry")
    job.directory.mkdir(parents=True, exist_ok=True)
    job.events_path.touch()
    job.save(id=job.id, provider="openrouter", cwd=str(tmp_path), model="test/model", provider_sessions=[])

    calls = {"count": 0}

    def flaky_stream(messages, model, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("429 rate limit exceeded")
        yield {
            "message": {"role": "assistant", "content": "Recovered."},
            "usage": {},
            "finish_reason": "stop",
            "done": True,
        }

    monkeypatch.setattr(openrouter_client, "provider_status", lambda **kwargs: (True, "ready"))
    monkeypatch.setattr(openrouter_client, "stream_chat", flaky_stream)
    monkeypatch.setattr(code_jobs.time, "sleep", lambda _s: None)

    outcome, summary = job._run_openrouter("hi", [])

    assert outcome == "completed"
    assert summary == "Recovered."
    assert calls["count"] == 2
    telemetry = job.load()
    assert telemetry["model_request_count"] == 2
    assert telemetry["model_request_count_source"] == "aios_local_provider_loop"
    assert [row["status"] for row in telemetry["model_request_rounds"]] == ["failed", "completed"]
    assert [row["attempt"] for row in telemetry["model_request_rounds"]] == [1, 2]


def test_tool_aliases_accept_other_harness_spellings():
    normalize = code_jobs.CodeJob._normalize_tool_call

    assert normalize("grep", {"pattern": "openrouter", "path": "helper_overlay.py"}) == (
        "search_text", {"query": "openrouter", "relative_path": "helper_overlay.py"})
    assert normalize("str_replace", {"file_path": "a.py", "old_str": "x", "new_str": "y"}) == (
        "edit_file", {"relative_path": "a.py", "old_text": "x", "new_text": "y"})
    assert normalize("bash", {"cmd": "pytest -q"}) == ("run_shell", {"command": "pytest -q"})
    assert normalize("glob", {"query": "**/*.py"}) == ("find_files", {"pattern": "**/*.py"})
    assert normalize("cat", {"file": "notes.md"}) == ("read_file", {"relative_path": "notes.md"})


def test_search_text_accepts_pattern_instead_of_query(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("aliassearch")
    (tmp_path / "a.py").write_text("openrouter_client = 1\n", encoding="utf-8")

    result = json.loads(job._ollama_run_tool(tmp_path, "search_text", {"pattern": "openrouter"}))

    assert "error" not in result
    assert result["matches"][0]["path"] == "a.py"


def test_coder_led_prompt_starts_from_a_named_entrypoint_and_separates_path_from_content_search(
    isolated_jobs, tmp_path,
):
    job = code_jobs.CodeJob("namedentrypoint")
    job.save(id=job.id, cwd=str(tmp_path), strategy="auto")
    job._turn_request = "Change src/index.html and the bundle it loads"
    job._turn_policy_active = True
    job._task_strategy = code_jobs._coder_led_strategy()

    prompt = job._openrouter_system_prompt(tmp_path)
    tools = {
        item["function"]["name"]: item["function"]
        for item in job._ollama_tools(job._turn_request)
    }

    assert "paths named by the operator as established scope" in prompt
    assert "first inspection target that file" in prompt
    assert "does not need a plan" in prompt
    assert "Do not inspect git status, diff, or history" in prompt
    assert "searches names/paths only, never file contents" in tools["find_files"]["description"]
    assert "not a content regex" in tools["find_files"]["parameters"]["properties"]["pattern"]["description"]


def test_search_text_recognizes_unambiguous_regex_without_a_mode_flag(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("autoregexsearch")
    (tmp_path / "events.js").write_text(
        "node.addEventListener('pointerdown', start)\n"
        "node.addEventListener('touchstart', start)\n",
        encoding="utf-8",
    )

    result = json.loads(job._ollama_run_tool(
        tmp_path,
        "search_text",
        {"query": "pointerdown|touchstart", "relative_path": "events.js"},
    ))

    assert result["query_mode"] == "regex"
    assert [match["line"] for match in result["matches"]] == [1, 2]


def test_search_text_explicit_literal_mode_overrides_regex_inference(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("literalpipesearch")
    (tmp_path / "notes.txt").write_text("alpha|beta\nalpha only\n", encoding="utf-8")

    result = json.loads(job._ollama_run_tool(
        tmp_path,
        "search_text",
        {"query": "alpha|beta", "relative_path": "notes.txt", "is_regex": False},
    ))

    assert result["query_mode"] == "literal"
    assert [match["line"] for match in result["matches"]] == [1]


def test_search_text_file_scope_keeps_real_path_line_and_revision(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("filescopedsearch")
    target = tmp_path / "web" / "css" / "app.css"
    target.parent.mkdir(parents=True)
    target.write_text(".before {}\n.session-row .dot.read { background: #8a8f98; }\n", encoding="utf-8")

    result = json.loads(job._ollama_run_tool(
        tmp_path,
        "search_text",
        {"query": "session-row", "relative_path": "web/css/app.css"},
    ))

    if result.get("engine") != "ripgrep":
        pytest.skip("ripgrep is unavailable")
    assert result["matches"] == [{
        "path": "web/css/app.css",
        "line": 2,
        "text": ".session-row .dot.read { background: #8a8f98; }",
        "text_truncated": False,
    }]
    assert result["file_lines"] == {"web/css/app.css": 2}
    assert result["file_revisions"]["web/css/app.css"]


def test_search_text_file_scope_does_not_silently_stop_at_ten_matches(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("filescopedmany")
    target = tmp_path / "many.css"
    target.write_text("".join(f".match-{index} {{ color: red; }}\n" for index in range(25)), encoding="utf-8")

    result = json.loads(job._ollama_run_tool(
        tmp_path,
        "search_text",
        {"query": "color: red", "relative_path": "many.css", "max_results": 80},
    ))

    if result.get("engine") != "ripgrep":
        pytest.skip("ripgrep is unavailable")
    assert len(result["matches"]) == 25
    assert result["matches"][-1]["line"] == 25
    assert result["truncated"] is False
    assert result["per_file_limited"] is False


def test_search_text_directory_reports_per_file_limit(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("directorymany")
    target = tmp_path / "styles"
    target.mkdir()
    (target / "many.css").write_text(
        "".join(f".match-{index} {{ color: red; }}\n" for index in range(25)),
        encoding="utf-8",
    )

    result = json.loads(job._ollama_run_tool(
        tmp_path,
        "search_text",
        {"query": "color: red", "relative_path": "styles", "max_results": 80},
    ))

    if result.get("engine") != "ripgrep":
        pytest.skip("ripgrep is unavailable")
    assert len(result["matches"]) == 10
    assert result["truncated"] is False
    assert result["per_file_limited"] is True
    assert result["per_file_limit"] == 10
    assert result["per_file_limited_paths"] == ["styles/many.css"]


def test_file_tools_report_missing_path_instead_of_none(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("nopath")

    result = json.loads(job._ollama_run_tool(tmp_path, "edit_file", {"old_text": "a", "new_text": "b"}))

    assert "relative_path is required" in result["error"]


def test_run_shell_propagates_exit_codes(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("shellexit")
    job.directory.mkdir(parents=True, exist_ok=True)

    ok = json.loads(job._ollama_run_tool(tmp_path, "run_shell", {"command": "Write-Output ready"}))
    bad = json.loads(job._ollama_run_tool(tmp_path, "run_shell", {"command": "exit 7"}))

    assert ok["exit_code"] == 0 and "ready" in ok["output"]
    assert bad["exit_code"] == 7


@pytest.mark.skipif(os.name != "nt", reason="PowerShell wrapper is Windows-specific")
def test_run_shell_missing_command_cannot_false_pass(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("shellmissing")
    job.directory.mkdir(parents=True, exist_ok=True)

    result = json.loads(job._ollama_run_tool(
        tmp_path,
        "run_shell",
        {"command": "aios_command_that_does_not_exist_7f3c"},
    ))

    assert result["exit_code"] != 0
    assert "aios_command_that_does_not_exist_7f3c" in result["output"]


def test_benchmark_identity_is_system_context_and_shell_environment(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("benchidentity")
    job.directory.mkdir(parents=True, exist_ok=True)
    job.save(
        id=job.id,
        cwd=str(tmp_path),
        system_context="You are Agent #347. Use port 24347.",
        runtime_env={"AIOS_PREVIEW_PORT": "24347", "AIOS_PREVIEW_URL": "http://127.0.0.1:24347"},
    )
    assert "Agent #347" in job._openrouter_system_prompt(tmp_path)
    result = json.loads(job._ollama_run_tool(
        tmp_path, "run_shell", {"command": "Write-Output $env:AIOS_PREVIEW_PORT"},
    ))
    assert result["exit_code"] == 0
    assert "24347" in result["output"]


@pytest.mark.skipif(os.name != "nt", reason="Windows Start-Process regression")
def test_background_preview_does_not_hold_the_shell_capture_open(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("shellpreview")
    job.directory.mkdir(parents=True, exist_ok=True)
    executable = sys.executable.replace("'", "''")
    command = (
        f"Start-Process -WindowStyle Hidden -FilePath '{executable}' "
        "-ArgumentList @('-m','http.server','0','--bind','127.0.0.1')"
    )
    started = time.perf_counter()
    try:
        result = json.loads(job._ollama_run_tool(
            tmp_path, "run_shell", {"command": command, "timeout_seconds": 8},
        ))
        assert result["exit_code"] == 0
        assert time.perf_counter() - started < 6
        assert job._background_shell_processes
    finally:
        job._cleanup_background_shell_processes()


def test_edit_diffs_are_kept_for_the_ui_but_trimmed_out_of_model_history(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("difftrim")
    job.save(id=job.id, cwd=str(tmp_path), provider_sessions=[])
    target = tmp_path / "big.py"
    target.write_text("\n".join(f"line {i}" for i in range(200)) + "\n", encoding="utf-8")

    raw = job._ollama_run_tool(tmp_path, "edit_file", {
        "relative_path": "big.py", "old_text": "line 5\n", "new_text": "line five\n",
    })
    trimmed = json.loads(job._tool_result_for_model(raw))

    assert "diff" in json.loads(raw)          # the activity card still gets it
    assert "diff" not in trimmed              # the model does not pay for it twice
    assert trimmed["lines_added"] == 1
    assert trimmed["lines_deleted"] == 1
    assert trimmed["ok"] is True
    assert len(job._tool_result_for_model(raw)) < len(raw)


def test_usage_window_sums_only_reported_usage(isolated_jobs, tmp_path):
    now = code_jobs._now()
    rows = [
        ("recent1", "openrouter", now - 86400, {"input_tokens": 100, "output_tokens": 20,
                                                "total_tokens": 120, "cost_usd": 0.01}),
        ("recent2", "codex", now - 5 * 86400, {"input_tokens": 300, "output_tokens": 40,
                                               "total_tokens": 340, "cost_usd": 0.05}),
        ("silent", "cursor", now - 2 * 86400, {}),
        ("stale", "openrouter", now - 40 * 86400, {"input_tokens": 999999, "output_tokens": 9999,
                                                   "total_tokens": 1009998, "cost_usd": 99.0}),
    ]
    for job_id, provider, stamp, usage in rows:
        job = code_jobs.CodeJob(job_id)
        job.directory.mkdir(parents=True, exist_ok=True)
        job.save(id=job_id, provider=provider, cwd=str(tmp_path), usage=usage,
                 status="completed", updated_at=stamp, completed_at=stamp, created_at=stamp)

    window = code_jobs.usage_window(28)

    assert window["sessions"] == 3                 # the 40-day-old session is outside
    assert window["sessions_with_usage"] == 2
    assert window["sessions_without_usage"] == 1   # counted, never estimated
    assert window["usage"]["total_tokens"] == 460
    assert window["usage"]["cost_usd"] == pytest.approx(0.06)
    assert set(window["by_provider"]) == {"openrouter", "codex"}
    assert window["by_provider"]["codex"]["usage"]["total_tokens"] == 340


def test_stored_usage_rows_keep_their_cost_when_renormalized():
    stored = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12, "cost_usd": 0.25}

    assert code_jobs._normalized_usage(stored)["cost_usd"] == pytest.approx(0.25)
    assert code_jobs._normalized_usage({"cost": 0.5})["cost_usd"] == pytest.approx(0.5)


def test_ask_user_blocks_the_turn_until_an_answer_arrives(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("askuser")
    job.directory.mkdir(parents=True, exist_ok=True)
    job.events_path.touch()
    job.save(id=job.id, provider="openrouter", cwd=str(tmp_path), status="running",
             model="test/model", provider_sessions=[])
    result = {}

    def run():
        result["raw"] = job._ollama_run_tool(tmp_path, "ask_user", {
            "question": "Which app did you mean?",
            "options": ["the aiOS overlay", "agent_clicker"],
        })

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.time() + 3
    while job.load().get("status") != "waiting_user" and time.time() < deadline:
        time.sleep(0.01)

    assert job.load()["status"] == "waiting_user"
    assert job.load()["pending_question"] == "Which app did you mean?"
    assert job.question_waiter is not None

    job.question_waiter.put_nowait("the aiOS overlay")
    thread.join(timeout=3)

    assert json.loads(result["raw"])["answer"] == "the aiOS overlay"
    assert job.load()["status"] == "running"
    assert job.load()["pending_question"] == ""


def test_ask_user_structured_questions_round_trip_through_normal_message_path(isolated_jobs, tmp_path, monkeypatch):
    job = code_jobs.CodeJob("askstructured")
    job.directory.mkdir(parents=True, exist_ok=True)
    job.events_path.touch()
    job.save(id=job.id, provider="openrouter", cwd=str(tmp_path), status="running",
             model="test/model", provider_sessions=[])
    monkeypatch.setattr(code_jobs, "selection_error", lambda *args, **kwargs: None)
    result = {}
    questions = [
        {"id": "flavors", "q": "How many flavors?", "type": "radio", "options": ["Three", "Five"]},
        {"id": "mixins", "q": "Which mix-ins?", "type": "check", "options": ["Chips", "Sprinkles"]},
    ]

    thread = threading.Thread(
        target=lambda: result.update(raw=job._ollama_run_tool(tmp_path, "ask_user", {"questions": questions})),
        daemon=True,
    )
    thread.start()
    deadline = time.time() + 3
    while job.question_waiter is None and time.time() < deadline:
        time.sleep(0.01)

    event = next(event for event in code_jobs.read_events(job.id)["events"] if event["kind"] == "question")
    assert event["questions"][0]["q"] == "How many flavors?"
    assert event["questions"][1]["type"] == "check"
    answer_text = "How many flavors?: Three\nWhich mix-ins?: Chips, Sprinkles"
    sent = job.send(answer_text, question_answers={"flavors": ["Three"], "mixins": ["Chips", "Sprinkles"]})
    assert sent["answered"] is True
    thread.join(timeout=3)

    payload = json.loads(result["raw"])
    assert payload["answer"] == answer_text
    assert payload["answers"] == {"flavors": ["Three"], "mixins": ["Chips", "Sprinkles"]}
    user_event = next(event for event in code_jobs.read_events(job.id)["events"] if event["kind"] == "user")
    assert user_event["answer_to_question"] == event["question_id"]


def test_lookup_tools_are_offered_so_the_model_can_verify_instead_of_guessing(isolated_jobs):
    job = code_jobs.CodeJob("lookuptools")
    tools = job._ollama_tools()
    names = {tool["function"]["name"] for tool in tools}
    selector = next(tool["function"] for tool in tools if tool["function"]["name"] == "select_tools")
    available = set(selector["parameters"]["properties"]["names"]["items"]["enum"])

    assert {"ask_user", "select_tools"} <= names
    assert {"fetch_url", "web_search"} <= available


def test_fetch_url_rejects_non_http_targets(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("fetchguard")

    result = json.loads(job._ollama_run_tool(tmp_path, "fetch_url", {"url": "file:///C:/Windows/win.ini"}))

    assert "http" in result["error"]


def test_html_is_reduced_to_readable_text():
    html = "<html><head><style>a{}</style></head><body><h1>Title</h1><p>One &amp; two</p>"
    html += "<script>ignored()</script><li>item</li></body></html>"

    text = code_jobs.CodeJob._html_to_text(html)

    assert "ignored()" not in text and "a{}" not in text
    assert "One & two" in text
    assert "Title" in text and "item" in text


def test_independent_reads_run_in_parallel_but_edits_stay_ordered(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("parallel")
    job.directory.mkdir(parents=True, exist_ok=True)
    job.events_path.touch()
    job.save(id=job.id, cwd=str(tmp_path), provider_sessions=[])
    for index in range(4):
        (tmp_path / f"f{index}.txt").write_text(f"content {index}\n", encoding="utf-8")
    (tmp_path / "target.txt").write_text("before\n", encoding="utf-8")

    order = []
    real = code_jobs.CodeJob._ollama_run_tool

    def traced(self, project, name, args, activity_id=""):
        order.append(("start", name))
        if name == "read_file":
            time.sleep(0.15)
        result = real(self, project, name, args, activity_id)
        order.append(("end", name))
        return result

    calls = [
        {"id": f"c{i}", "function": {"name": "read_file",
                                     "arguments": json.dumps({"relative_path": f"f{i}.txt"})}}
        for i in range(4)
    ] + [
        {"id": "edit", "function": {"name": "edit_file", "arguments": json.dumps(
            {"relative_path": "target.txt", "old_text": "before", "new_text": "after"})}},
    ]

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(code_jobs.CodeJob, "_ollama_run_tool", traced)
        started = time.monotonic()
        results = job._execute_tool_calls(tmp_path, calls, "test")
        elapsed = time.monotonic() - started

    # Four 0.15s reads overlap instead of costing 0.6s in series.
    assert elapsed < 0.45
    assert [item["name"] for item in results] == ["read_file"] * 4 + ["edit_file"]
    # The edit only begins once every read has finished.
    edit_start = order.index(("start", "edit_file"))
    assert all(entry != ("end", "read_file") for entry in order[edit_start:])
    assert (tmp_path / "target.txt").read_text(encoding="utf-8") == "after\n"


def test_same_file_edits_in_one_batch_chain_fresh_revisions(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("batch-edit-revisions")
    job.directory.mkdir(parents=True, exist_ok=True)
    job.save(id=job.id, cwd=str(tmp_path), provider_sessions=[])
    target = tmp_path / "target.py"
    target.write_text("alpha = 1\nbeta = 2\n", encoding="utf-8")
    observed = json.loads(job._ollama_run_tool(tmp_path, "read_file", {
        "relative_path": "target.py",
    }))
    revision = observed["revision"]
    calls = [
        {
            "id": "edit-alpha",
            "function": {
                "name": "edit_file",
                "arguments": json.dumps({
                    "relative_path": "target.py",
                    "old_text": "alpha = 1",
                    "new_text": "alpha = 10",
                    "expected_revision": revision,
                }),
            },
        },
        {
            "id": "edit-beta",
            "function": {
                "name": "edit_file",
                "arguments": json.dumps({
                    "relative_path": "./target.py",
                    "old_text": "beta = 2",
                    "new_text": "beta = 20",
                    "expected_revision": revision,
                }),
            },
        },
    ]

    results = job._execute_tool_calls(tmp_path, calls, "test")
    payloads = [json.loads(item["result"]) for item in results]

    assert [payload["ok"] for payload in payloads] == [True, True]
    assert payloads[1]["batch_revision_handoff"] is True
    assert payloads[1]["revision_before"] == payloads[0]["revision"]
    assert target.read_text(encoding="utf-8") == "alpha = 10\nbeta = 20\n"


def test_subagents_are_read_only_and_cannot_nest(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("subguard")
    job.directory.mkdir(parents=True, exist_ok=True)
    job.events_path.touch()
    job.save(id=job.id, cwd=str(tmp_path), provider="openrouter", provider_sessions=[])

    assert "edit_file" not in code_jobs.SUBAGENT_TOOLS
    assert "write_file" not in code_jobs.SUBAGENT_TOOLS
    assert "run_shell" not in code_jobs.SUBAGENT_TOOLS
    assert "ask_user" not in code_jobs.SUBAGENT_TOOLS
    assert "spawn_agent" not in code_jobs.SUBAGENT_TOOLS

    job._subagent_local.depth = 1
    nested = json.loads(job._spawn_agent_tool(tmp_path, {"objective": "recurse"}))
    assert "cannot spawn" in nested["error"]


def test_subagent_reports_stream_onto_one_card(isolated_jobs, tmp_path, monkeypatch):
    job = code_jobs.CodeJob("subcard")
    job.directory.mkdir(parents=True, exist_ok=True)
    job.events_path.touch()
    job.save(id=job.id, cwd=str(tmp_path), provider="openrouter", provider_sessions=[])
    (tmp_path / "a.py").write_text("def go():\n    return 1\n", encoding="utf-8")

    rounds = iter([
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "s1", "function": {"name": "read_file",
                                      "arguments": json.dumps({"relative_path": "a.py"})}}]},
        {"role": "assistant", "content": "MAP\na.py:1 - go()\nANSWER\nIt returns 1.\nGAPS\nnone"},
    ])
    monkeypatch.setattr(code_jobs.CodeJob, "_subagent_round",
                        lambda self, provider, history, model, tools: next(rounds))

    raw = json.loads(job._spawn_agent_tool(tmp_path, {"objective": "what does a.py do"}, "card-1"))
    events = [e for e in (code_jobs.read_events(job.id, 0).get("events") or [])
              if e.get("activity_id") == "card-1"]

    assert raw["report"].startswith("MAP")
    assert raw["agent"].startswith("Scout-")
    # One card id carries the whole lifecycle, so the UI shows a single row.
    assert {e["phase"] for e in events} == {"started", "update", "completed"}
    assert all(e.get("activity_type") == "subagent" for e in events)
    assert "read_file" in str(events[-1].get("output"))


def test_subagent_gets_one_tool_free_final_round_at_limit(isolated_jobs, tmp_path, monkeypatch):
    job = code_jobs.CodeJob("subfinal")
    job.directory.mkdir(parents=True, exist_ok=True)
    job.events_path.touch()
    job.save(id=job.id, cwd=str(tmp_path), provider="openrouter", provider_sessions=[])
    (tmp_path / "a.py").write_text("def go():\n    return 1\n", encoding="utf-8")
    seen_tool_counts = []

    def fake_round(self, provider, history, model, tools):
        seen_tool_counts.append(len(tools))
        if tools:
            return {"role": "assistant", "content": "", "tool_calls": [{
                "id": "s1",
                "function": {"name": "read_file", "arguments": json.dumps({"relative_path": "a.py"})},
            }]}
        return {"role": "assistant", "content": "MAP\na.py:1 - go\nANSWER\nLocated it.\nGAPS\nnone"}

    monkeypatch.setattr(code_jobs, "SUBAGENT_MAX_ROUNDS", 1)
    monkeypatch.setattr(code_jobs.CodeJob, "_subagent_round", fake_round)

    result = json.loads(job._spawn_agent_tool(tmp_path, {"objective": "locate go"}))

    assert seen_tool_counts[0] > 0
    assert seen_tool_counts[1] == 0
    assert result["report"].startswith("MAP")


def test_small_concrete_project_map_replaces_paid_scout():
    survey = "app.py\n    def run():\ntests/test_app.py\n    def test_run():"

    assert code_jobs.CodeJob._survey_is_small_and_concrete(survey) is True
    oversized = "\n".join(f"file_{index}.py" for index in range(code_jobs.PLAN_LOCAL_MAP_FILE_LIMIT + 1))
    assert code_jobs.CodeJob._survey_is_small_and_concrete(oversized) is False


def test_precise_small_repo_brief_skips_legacy_paid_planning_stages(
    isolated_jobs, tmp_path, monkeypatch,
):
    (tmp_path / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_app.py").write_text("# smoke\n", encoding="utf-8")
    job = code_jobs.CodeJob("localplan")
    job.save(
        id=job.id,
        cwd=str(tmp_path),
        provider="openrouter",
        model="test/model",
        created_at=time.time(),
        provider_sessions=[{"provider": "openrouter", "model": "test/model"}],
        role_config={
            "scout": {"enabled": True, "model": "test/model", "reasoning": "low", "fast": False},
            "planner": {"enabled": True, "model": "test/model", "reasoning": "low", "fast": False},
            "coder": {"enabled": True, "model": "test/model", "reasoning": "low", "fast": False},
            "reviewer": {"enabled": False, "model": "test/model", "reasoning": "low", "fast": False},
        },
    )
    brief = (
        "Finish the bounded settings panel exactly as specified.\n\n"
        "- Existing preference keys and public component props stay unchanged.\n"
        "- The Save button remains disabled until a visible value changes.\n"
        "- Cancel restores the initial values without writing persistence.\n"
        "- A successful save shows one status message and closes the panel.\n"
        "- Keyboard focus returns to the button that opened the panel.\n"
        "- Keep the visible test untouched and use current dependencies only.\n\n"
        + ("Every requested behavior has a concrete visible acceptance condition. " * 8)
        + "\nRun `python -m unittest tests/test_app.py` while you work."
    )
    selected = code_jobs.code_harness_policy.classify_task(brief)
    assert selected.name == "planned"
    monkeypatch.setattr(
        job,
        "_run_plan_stage",
        lambda *args, **kwargs: pytest.fail("paid planner should have been skipped"),
    )

    prepared = job._with_plan(brief, strategy=selected)

    assert prepared == brief


def test_risky_precise_brief_keeps_planner():
    brief = (
        "Finish this streaming protocol and thread-safe state machine.\n"
        + "\n".join(f"- Contract condition {index} must hold." for index in range(8))
        + ("\nAll malformed frames must fail without mutating committed state." * 12)
        + "\nRun `python -m unittest tests/test_protocol.py`."
    )
    selected = code_jobs.code_harness_policy.classify_task(brief)

    assert selected.name == "planned"
    assert code_jobs.CodeJob._is_precise_execution_brief(brief, selected) is False


def test_three_part_acceptance_contract_with_exact_check_is_already_a_plan():
    brief = (
        "Rename the bounded stage entry point everywhere it is defined and called.\n"
        "- The old name must disappear from Python and JSON source.\n"
        "- Every dynamically loaded stage must remain reachable.\n"
        "- The keyword-only signature must be used at each call site.\n"
        + ("This is a mechanical rename with explicit acceptance conditions. " * 10)
        + "\nRun `python -m unittest tests/test_stages.py`."
    )
    selected = code_jobs.code_harness_policy.classify_task(brief)

    assert selected.name == "planned"
    assert code_jobs.CodeJob._is_precise_execution_brief(brief, selected) is True


def test_find_symbol_locates_definitions_without_reading_whole_files(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("symbols")
    (tmp_path / "app.py").write_text(
        "import os\n\n\ndef helper():\n    pass\n\n\nclass Widget:\n    def draw(self):\n        helper()\n",
        encoding="utf-8")
    (tmp_path / "other.py").write_text("from app import helper\nhelper()\n", encoding="utf-8")

    found = json.loads(job._ollama_run_tool(tmp_path, "find_symbol",
                                            {"name": "helper", "include_references": True}))

    if found.get("error"):
        pytest.skip(found["error"])
    definitions = {(row["path"], row["line"]) for row in found["definitions"]}
    assert ("app.py", 4) in definitions
    assert any(row["path"] == "other.py" for row in found.get("references") or [])
    assert len(json.dumps(found)) < 2000


def test_find_symbol_file_scope_does_not_search_sibling_files(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("scopedsymbol")
    (tmp_path / "app.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("def helper():\n    return 2\n", encoding="utf-8")

    found = json.loads(job._ollama_run_tool(
        tmp_path,
        "find_symbol",
        {"name": "helper", "relative_path": "app.py", "include_references": True},
    ))

    if found.get("error"):
        pytest.skip(found["error"])
    assert {(row["path"], row["line"]) for row in found["definitions"]} == {("app.py", 1)}
    assert all(row["path"] == "app.py" for row in found.get("references") or [])
    assert set(found["file_revisions"]) == {"app.py"}


def test_find_symbol_can_return_complete_ast_backed_python_source(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("symbol-source-python")
    target = tmp_path / "app.py"
    target.write_bytes(
        b"def trace(func):\r\n"
        b"    return func\r\n\r\n"
        b"@trace\r\n"
        b"async def transform(value):\r\n"
        b"    nested = value + 1\r\n"
        b"    return nested\r\n\r\n"
        b"def after():\r\n"
        b"    return 0\r\n"
    )

    found = json.loads(job._ollama_run_tool(tmp_path, "find_symbol", {
        "name": "transform",
        "relative_path": "app.py",
        "include_source": True,
    }))

    if found.get("error"):
        pytest.skip(found["error"])
    definition = found["definitions"][0]
    assert definition["source_range_method"] == "python_ast"
    assert definition["source_start_line"] == 4
    assert definition["source_end_line"] == definition["definition_end_line"] == 7
    assert definition["source_truncated"] is False
    assert definition["source_next_range"] is None
    assert definition["source"].startswith("@trace\nasync def transform")
    assert "def after" not in definition["source"]
    assert definition["revision"] == found["file_revisions"]["app.py"]


def test_find_symbol_source_uses_bounded_language_neutral_fallback(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("symbol-source-js")
    (tmp_path / "app.js").write_text(
        "export function calculate(\n"
        "  value,\n"
        ") {\n"
        "  const marker = '{not structure}';\n"
        "  if (value) {\n"
        "    return value + 1;\n"
        "  }\n"
        "}\n\n"
        "export function after() { return 0; }\n",
        encoding="utf-8",
    )

    found = json.loads(job._ollama_run_tool(tmp_path, "find_symbol", {
        "name": "calculate",
        "relative_path": "app.js",
        "include_source": True,
    }))

    if found.get("error"):
        pytest.skip(found["error"])
    definition = found["definitions"][0]
    assert definition["source_range_method"] == "bounded_lexical_fallback"
    assert definition["source_truncated"] is False
    assert definition["source"].rstrip().endswith("}")
    assert "function after" not in definition["source"]


def test_find_symbol_source_has_hard_combined_line_and_character_limits(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("symbol-source-limits")
    for index in range(3):
        body = "".join(f"    item_{line} = {line}\n" for line in range(140))
        (tmp_path / f"part_{index}.py").write_text(
            f"def shared():\n{body}    return item_139\n",
            encoding="utf-8",
        )

    found = json.loads(job._ollama_run_tool(tmp_path, "find_symbol", {
        "name": "shared",
        "include_source": True,
        "max_lines": 999,
    }))

    if found.get("error"):
        pytest.skip(found["error"])
    assert len(found["definitions"]) == 3
    limits = found["source_limits"]
    sources = [str(row.get("source") or "") for row in found["definitions"]]
    assert limits["max_lines_per_definition"] == code_jobs.FIND_SYMBOL_SOURCE_MAX_LINES
    assert limits["lines_returned"] <= code_jobs.FIND_SYMBOL_SOURCE_MAX_LINES
    assert limits["chars_returned"] == sum(map(len, sources))
    assert limits["chars_returned"] <= code_jobs.FIND_SYMBOL_SOURCE_MAX_CHARS
    assert limits["truncated_definitions"] >= 2
    assert all("revision" in row for row in found["definitions"])
    assert any(row.get("source_next_range") for row in found["definitions"])


def test_find_symbol_source_character_truncation_returns_offset_continuation(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("symbol-source-char-limit")
    (tmp_path / "large.js").write_text(
        "function huge() { const payload = \"" + ("x" * 30_000) + "\"; return payload; }\n",
        encoding="utf-8",
    )

    raw = job._ollama_run_tool(tmp_path, "find_symbol", {
        "name": "huge",
        "relative_path": "large.js",
        "include_source": True,
        "max_lines": 250,
    })
    found = json.loads(raw)

    if found.get("error"):
        pytest.skip(found["error"])
    definition = found["definitions"][0]
    assert len(definition["source"]) == code_jobs.FIND_SYMBOL_SOURCE_MAX_CHARS
    assert definition["source_truncated"] is True
    assert definition["source_next_range"]["offset"] == code_jobs.FIND_SYMBOL_SOURCE_MAX_CHARS
    assert found["source_limits"]["chars_returned"] == code_jobs.FIND_SYMBOL_SOURCE_MAX_CHARS
    assert len(raw) <= code_jobs.TOOL_OUTPUT_PREVIEW_CHARS * 2
    assert job._externalize_large_tool_result("find_symbol", raw) == raw


def test_find_symbol_source_rejects_invalid_utf8_instead_of_replacing_it(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("symbol-source-utf8")
    (tmp_path / "bad.py").write_bytes(b"def helper():\n    return \xff\n")
    definitions = [{"path": "bad.py", "line": 1, "text": "def helper():"}]

    revisions, limits = job._attach_symbol_sources(tmp_path, "helper", definitions, 20)

    assert revisions == {}
    assert limits["chars_returned"] == 0
    assert definitions[0]["source_error"] == "File is binary or is not valid UTF-8."
    assert "source" not in definitions[0]


def test_find_symbol_source_counts_as_read_evidence_for_duplicate_guard(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("symbol-source-evidence")
    (tmp_path / "app.py").write_text(
        "def helper(value):\n    changed = value + 1\n    return changed\n",
        encoding="utf-8",
    )
    job.reset_turn_discipline()
    args = {
        "name": "helper",
        "relative_path": "app.py",
        "include_source": True,
    }
    result = job._ollama_run_tool(tmp_path, "find_symbol", args)
    job._guard_after_tool("find_symbol", args, result)

    duplicate = json.loads(job._guard_before_tool("read_file", {
        "relative_path": "app.py",
        "start_line": 1,
        "max_lines": 3,
    }))

    assert duplicate["guardrail"] == "evidence_reuse"
    assert duplicate["reused"] is True


def test_outline_file_returns_structure_without_bodies(isolated_jobs, tmp_path):
    job = code_jobs.CodeJob("outline")
    (tmp_path / "big.py").write_text(
        "class Alpha:\n    def one(self):\n        return " + "1 + " * 200 + "1\n\ndef beta():\n    pass\n",
        encoding="utf-8")

    outline = json.loads(job._ollama_run_tool(tmp_path, "outline_file", {"relative_path": "big.py"}))
    signatures = [row["signature"] for row in outline["symbols"]]

    assert "class Alpha" in signatures[0]
    assert any("def one" in text for text in signatures)
    assert any("def beta" in text for text in signatures)
    assert "1 + 1 + 1" not in json.dumps(outline)   # bodies never travel


def test_ripgrep_is_found_even_when_it_is_not_on_path(monkeypatch):
    """A bundled editor copy still counts; falling back to the Python scanner
    turns a 0.1s repository search into a 30s one."""
    monkeypatch.setattr(code_jobs.shutil, "which", lambda _name: None)
    monkeypatch.setattr(code_jobs, "_RIPGREP_RESOLVED", False)
    monkeypatch.setattr(code_jobs, "_RIPGREP_CACHE", None)

    found = code_jobs.ripgrep_path()

    if not found:
        pytest.skip("no ripgrep installed anywhere on this machine")
    assert Path(found).is_file()
    assert Path(found).stem == "rg"


def test_search_ignores_cover_heavy_hidden_directories():
    args = code_jobs._rg_ignore_args()
    joined = " ".join(args)

    assert "!.git/**" in joined
    assert "!node_modules/**" in joined
    assert "!.venv-*/**" in joined      # multi-GB training venvs
    assert "!.tools/**" in joined
    assert args.count("-g") == len(code_jobs.SEARCH_IGNORE_GLOBS)


def test_a_session_left_running_by_a_restart_settles_to_interrupted(isolated_jobs, tmp_path):
    """The overlay hosts local turns in-process, so closing it strands them."""
    job = code_jobs.CodeJob("stalejob")
    job.directory.mkdir(parents=True, exist_ok=True)
    job.events_path.touch()
    job.save(id=job.id, provider="openrouter", cwd=str(tmp_path), status="running",
             queued=0, provider_sessions=[])
    with code_jobs._REGISTRY_LOCK:
        code_jobs._LIVE.pop("stalejob", None)

    listed = next(row for row in code_jobs.list_jobs() if row["id"] == "stalejob")

    assert listed["status"] == "interrupted"
    # Reading never rewrites a job another process may still own.
    assert code_jobs.get_job("stalejob")["status"] == "running"
    assert json.loads(job.meta_path.read_text(encoding="utf-8"))["status"] == "running"

    # The owner settles it at startup, and then both views agree.
    code_jobs.recover_interrupted()
    assert code_jobs.get_job("stalejob")["status"] == "interrupted"
    assert json.loads(job.meta_path.read_text(encoding="utf-8"))["status"] == "interrupted"


def test_a_live_in_process_turn_is_never_reported_as_interrupted(isolated_jobs, tmp_path):
    """OpenRouter and Ollama turns own no subprocess; only the worker thread
    flag distinguishes them from a stranded session."""
    job = code_jobs.CodeJob("liveturn")
    job.directory.mkdir(parents=True, exist_ok=True)
    job.events_path.touch()
    job.save(id=job.id, provider="openrouter", cwd=str(tmp_path), status="running",
             queued=0, provider_sessions=[])
    with code_jobs._REGISTRY_LOCK:
        code_jobs._LIVE[job.id] = job
    job._worker_running = True
    try:
        assert code_jobs.get_job("liveturn")["status"] == "running"
        code_jobs.recover_interrupted()
        assert code_jobs.get_job("liveturn")["status"] == "running"
    finally:
        job._worker_running = False


def test_recovery_settles_stranded_sessions_on_startup(isolated_jobs, tmp_path):
    for name, status in (("alive", "completed"), ("stranded", "running"), ("queuedone", "queued")):
        job = code_jobs.CodeJob(name)
        job.directory.mkdir(parents=True, exist_ok=True)
        job.events_path.touch()
        job.save(id=name, provider="openrouter", cwd=str(tmp_path), status=status, provider_sessions=[])
    with code_jobs._REGISTRY_LOCK:
        code_jobs._LIVE.clear()

    code_jobs.recover_interrupted()

    statuses = {row["id"]: row["status"] for row in code_jobs.list_jobs()}
    assert statuses["stranded"] == "interrupted"
    assert statuses["queuedone"] == "interrupted"
    assert statuses["alive"] == "completed"


def test_two_agents_starting_at_once_do_not_race_on_the_projects_file(isolated_jobs, tmp_path):
    """A shared ".tmp" name is a race, not a detail.

    Every writer used to build `projects.json.tmp`, so launching several
    sessions in the same moment had them overwriting each other's temp file and
    the losers died with "the process cannot access the file because it is
    being used by another process". A benchmark running three agents at once
    hit it every time; two sessions a second apart hit it rarely, which is the
    worse failure because nobody could reproduce it.
    """
    from concurrent.futures import ThreadPoolExecutor

    target = tmp_path / "projects.json"
    errors = []

    def write(index):
        try:
            code_jobs._atomic_json(target, {"projects": [{"id": index}]})
        except OSError as exc:
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(64)))

    assert not errors, errors[:3]
    # Whole, parseable, and one of the payloads that really was written.
    assert json.loads(target.read_text(encoding="utf-8"))["projects"][0]["id"] in range(64)
    assert not list(tmp_path.glob("*.tmp")), "temp files have to be cleaned up"


def test_project_registry_follows_current_jobs_dir(isolated_jobs, tmp_path, monkeypatch):
    project = tmp_path / "registered-project"
    project.mkdir()
    writes = []
    atomic_json = code_jobs._atomic_json

    def capture_write(path, payload):
        writes.append(Path(path))
        atomic_json(path, payload)

    monkeypatch.setattr(code_jobs, "_atomic_json", capture_write)

    result = code_jobs.add_project(str(project), "Isolated project")

    registry = isolated_jobs / "projects.json"
    assert result["ok"] is True
    assert code_jobs.projects_path() == registry
    assert writes == [registry]
    assert json.loads(registry.read_text(encoding="utf-8"))["projects"][0]["path"] == str(project)


def test_a_review_with_concerns_is_reported_and_left_alone_by_default(isolated_jobs, tmp_path, monkeypatch):
    """The reviewer is a second opinion, not a boss.

    Acting on it automatically spends your tokens on its confidence and can undo
    work you wanted, so nothing is queued unless you ask for it.
    """
    monkeypatch.setattr(code_jobs, "review_fix_enabled", lambda: False)
    job = code_jobs.CodeJob("quiet")
    job.directory.mkdir(parents=True, exist_ok=True)
    job.events_path.touch()
    job.save(id="quiet", provider="openrouter", cwd=str(tmp_path), status="running")

    queued = []
    monkeypatch.setattr(job, "_queue_payload", lambda payload, attachments=None, **kwargs: queued.append(payload))
    review = {"verdict": "concerns", "findings": [{"severity": "high", "file": "a.py", "issue": "off by one"}],
              "unmet": [], "model": "reviewer"}

    assert job._maybe_queue_review_fix(review) is False
    assert queued == []
    assert job.review_fix_used is False
    # The findings still reach you: the text of the review event is the report.
    assert "off by one" in job._review_text(review)


def test_the_review_fix_prompt_leaves_room_for_the_reviewer_being_wrong(isolated_jobs):
    """An instruction to "fix these" gets a change for every point, including
    the mistaken ones. That is how a review loop makes a good change worse."""
    prompt = code_jobs.CodeJob._review_fix_prompt({
        "verdict": "concerns",
        "findings": [{"severity": "high", "file": "report.py", "issue": "--top defaults to None, not 5"}],
        "unmet": ["--top must default to 5"],
    })
    assert "--top defaults to None, not 5" in prompt
    assert "[UNMET] --top must default to 5" in prompt
    # The escape hatch, and the fence.
    assert "mistaken" in prompt
    assert "do not start work beyond these points" in prompt.lower()


def test_review_concerns_never_auto_queue_a_coder_pass(isolated_jobs, tmp_path, monkeypatch):
    monkeypatch.setattr(code_jobs, "review_fix_enabled", lambda: True)
    job = code_jobs.CodeJob("loop")
    job.directory.mkdir(parents=True, exist_ok=True)
    job.events_path.touch()
    job.save(id="loop", provider="openrouter", cwd=str(tmp_path), status="running")

    queued = []
    monkeypatch.setattr(job, "_queue_payload", lambda payload, attachments=None, **kwargs: queued.append(payload))
    review = {"verdict": "concerns", "unmet": ["--top must default to 5"],
              "findings": [{"severity": "high", "file": "report.py", "issue": "the default is None"}]}

    assert job._maybe_queue_review_fix(review) is False
    assert queued == []


def test_review_fix_is_off_unless_configured(isolated_jobs, monkeypatch, tmp_path):
    """Default off, and the environment cannot be the only thing that says so."""
    config = tmp_path / "helper_config.json"
    monkeypatch.setattr(code_jobs, "CONFIG_PATH", config)

    config.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(code_jobs, "REVIEW_FIX_DEFAULT", False)
    assert code_jobs.review_fix_enabled() is False

    config.write_text(json.dumps({"code_review_fix_enabled": True}), encoding="utf-8")
    assert code_jobs.review_fix_enabled() is True

    # A missing or broken config file must not turn it on.
    config.write_text("{ not json", encoding="utf-8")
    assert code_jobs.review_fix_enabled() is False


def test_job_review_fix_is_stored_on_the_session(isolated_jobs, tmp_path, monkeypatch):
    monkeypatch.setattr(code_jobs, "review_fix_enabled", lambda: False)
    job = code_jobs.CodeJob("rf")
    job.directory.mkdir(parents=True, exist_ok=True)
    job.events_path.touch()
    job.save(id="rf", provider="openrouter", cwd=str(tmp_path), status="running", review_fix=True)
    assert job.uses_review_fix() is True
    job.save(review_fix=False)
    assert job.uses_review_fix() is False


def test_own_repo_prompt_points_at_web_gui_not_tk(tmp_path, monkeypatch):
    """aiOS GUI work must land in aios_ui, never the deprecated Tk overlay."""
    text = code_jobs.SELF_LOCATION
    assert "aios_ui" in text
    assert "aios_shell.py" in text
    assert "helper_overlay.py" in text
    assert "deprecated" in text.lower() or "Never edit" in text
    assert "agent_clicker" in text
    assert "phone_site/index.html" in text
    assert "Once that wiring establishes the owner, stay in that product" in text

    job = code_jobs.CodeJob("gui-map")
    monkeypatch.setattr(code_jobs, "ROOT", Path(r"C:\aiOS"))
    prompt = job._openrouter_system_prompt(Path(r"C:\aiOS"))
    assert "aios_ui" in prompt
    assert "helper_overlay.py" in prompt


def test_session_title_uses_subagent_openrouter_model(isolated_jobs, tmp_path, monkeypatch):
    created = code_jobs.create_job("codex", str(tmp_path), "fix the login button styling", "gpt-test", "low")
    job_id = created["job"]["id"]
    monkeypatch.setattr(code_jobs, "title_model_default", lambda: "nex-agi/nex-n2-mini")
    seen = {}

    def fake_chat(messages, model):
        seen["model"] = model
        return {
            "choices": [{"message": {"content": "Login button styling"}}],
            "usage": {
                "prompt_tokens": 40,
                "completion_tokens": 4,
                "total_tokens": 44,
                "cost": 0.0002,
            },
        }

    monkeypatch.setattr(code_jobs, "_title_chat", fake_chat)
    _REAL_GENERATE_TITLE(job_id)
    assert seen["model"] == "nex-agi/nex-n2-mini"
    meta = code_jobs.get_job(job_id)
    assert meta["title"] == "Login button styling"
    assert meta["title_source"] == "nex-agi/nex-n2-mini"
    assert meta["support_requests"][-1]["role"] == "title"
    assert meta["support_requests"][-1]["provider"] == "openrouter"
    assert meta["support_usage"]["total_tokens"] == 44
    assert meta["usage"]["total_tokens"] == 44
    assert not meta["provider_sessions"][0].get("usage")


def test_refresh_session_titles_skips_already_titled(isolated_jobs, tmp_path, monkeypatch):
    created = code_jobs.create_job("codex", str(tmp_path), "first task", "gpt-test", "low")
    job_id = created["job"]["id"]
    model = code_jobs.title_model_default()
    job = code_jobs._get_job(job_id)
    job.save(title="Already named", title_source=model)
    started = []

    def fake_generate(jid):
        started.append(jid)

    monkeypatch.setattr(code_jobs, "_generate_title", fake_generate)
    result = code_jobs.refresh_session_titles(limit=50)
    assert result["ok"]
    assert result["queued"] == 0
    assert started == []


def test_refresh_session_titles_filters_aios_project(isolated_jobs, tmp_path, monkeypatch):
    created = code_jobs.create_job("codex", str(tmp_path), "other repo task", "gpt-test", "low")
    job_id = created["job"]["id"]
    calls = []

    def fake_generate(jid):
        calls.append(jid)

    monkeypatch.setattr(code_jobs, "_generate_title", fake_generate)
    monkeypatch.setattr(code_jobs, "ROOT", tmp_path)
    result = code_jobs.refresh_session_titles(limit=50, project="aios", wait=True)
    assert result["ok"]
    assert result["queued"] == 1
    assert calls == [job_id]
def test_prompt_verification_commands_are_exact_safe_executables_only():
    prompt = """
When done, `python run.py sample.json` must preserve its output.
The symbol `transform` is being renamed. Do not trust `python -m pytest --collect-only`.
Never accept `python bad.py && exit 0`, `README.md`, or a fenced block:
```
python hidden.py
```
"""

    assert code_jobs._extract_prompt_verification_commands(prompt) == [
        "python run.py sample.json"
    ]
    assert code_jobs._verification_command_key("PYTHON   run.py  sample.json") == (
        code_jobs._verification_command_key("python run.py sample.json")
    )


def test_prompt_verification_commands_respect_negative_instructions():
    prompt = """
Run `python tests/test_pricing.py` when done.
Do not run `pytest -q`.
Never execute `npm test`.
Avoid using `node session.test.js`.
Skip `python test_slow.py`.
Finish without running `python test_network.py`.
There is no need to run `python test_duplicate.py`.
`python test_postposed.py` must not be run.
`npm run expensive` should never be executed.
The command `python test_context.py` is mentioned only as context.
"""

    assert code_jobs._extract_prompt_verification_commands(prompt) == [
        "python tests/test_pricing.py"
    ]


def test_prompt_verification_commands_preserve_multiple_affirmative_checks_in_order():
    prompt = (
        "Run `python test_one.py`, then verify with `python test_two.py`. "
        "Do not run `pytest -q`."
    )

    assert code_jobs._extract_prompt_verification_commands(prompt) == [
        "python test_one.py",
        "python test_two.py",
    ]


def test_completion_gate_runs_exact_requested_test_without_another_model_round(isolated_jobs, tmp_path):
    workspace = tmp_path / "auto-verify"
    workspace.mkdir()
    (workspace / "app.py").write_text("answer = 42\n", encoding="utf-8")
    (workspace / "test_app.py").write_text(
        "import unittest\nimport app\n\n"
        "class TestApp(unittest.TestCase):\n"
        "    def test_answer(self): self.assertEqual(app.answer, 42)\n",
        encoding="utf-8",
    )
    job = code_jobs.CodeJob("auto-verification")
    job.directory.mkdir(parents=True, exist_ok=True)
    job.save(
        id=job.id,
        cwd=str(workspace),
        provider="openrouter",
        status="running",
        explicit_verification_commands=["python -m unittest test_app.py"],
        task_strategy={"name": "planned"},
    )
    job.reset_turn_discipline("planned")
    job._record_mutation_state(workspace, workspace / "app.py")

    gate = job._completion_verification_gate(workspace)

    assert gate["allowed"] is True
    assert gate["continuation"] is False
    assert gate["attempt"] == 0
    assert gate["automatic_verification"]["command"] == "python -m unittest test_app.py"
    evidence = job._verification_ledger.snapshot()["evidence"]
    assert evidence[-1]["status"] == "passed"
    assert evidence[-1]["generation"] == job._verification_ledger.snapshot()["generation"]


def test_direct_completion_runs_exact_requested_test_despite_passing_diagnostic(isolated_jobs, tmp_path):
    workspace = tmp_path / "direct-auto-verification"
    workspace.mkdir()
    (workspace / "app.py").write_text("answer = 42\n", encoding="utf-8")
    (workspace / "test_app.py").write_text(
        "import unittest\nimport app\n\n"
        "class TestApp(unittest.TestCase):\n"
        "    def test_answer(self): self.assertEqual(app.answer, 42)\n\n"
        "if __name__ == '__main__': unittest.main()\n",
        encoding="utf-8",
    )
    job = code_jobs.CodeJob("direct-auto-verification")
    job.directory.mkdir(parents=True, exist_ok=True)
    job.save(
        id=job.id,
        cwd=str(workspace),
        provider="openrouter",
        status="running",
        explicit_verification_commands=["python test_app.py"],
        task_strategy={"name": "direct"},
    )
    job.reset_turn_discipline("direct")
    job._task_strategy = code_jobs.code_harness_policy.classify_task(
        "[direct] update app.py and run the named test"
    )
    job._record_mutation_state(workspace, workspace / "app.py")

    before = job._verification_ledger.decision("direct")
    gate = job._completion_verification_gate(workspace)
    second = job._completion_verification_gate(workspace)

    assert before["allowed"] is True
    assert gate["allowed"] is True
    assert gate["automatic_verification"]["command"] == "python test_app.py"
    assert "automatic_verification" not in second
    evidence = job._verification_ledger.snapshot()["evidence"]
    assert [row["command"] for row in evidence] == ["python test_app.py"]


def test_completion_gate_runs_all_requested_recognized_checks_once(
    isolated_jobs, tmp_path, monkeypatch,
):
    workspace = tmp_path / "multiple-auto-verification"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("answer = 42\n", encoding="utf-8")
    commands = ["python test_one.py", "python test_two.py"]
    job = code_jobs.CodeJob("multiple-auto-verification")
    job.directory.mkdir(parents=True, exist_ok=True)
    job.save(
        id=job.id,
        cwd=str(workspace),
        provider="openrouter",
        status="running",
        explicit_verification_commands=commands,
        task_strategy={"name": "direct"},
    )
    job.reset_turn_discipline("direct")
    job._task_strategy = code_jobs.code_harness_policy.classify_task("[direct] update app.py")
    job._record_mutation_state(workspace, target)
    executed_commands = []

    def fake_execute(_project, calls, _role):
        command = json.loads(calls[0]["function"]["arguments"])["command"]
        executed_commands.append(command)
        job._verification_ledger.record_command(command, 0, "passed")
        return [{"result": json.dumps({"exit_code": 0, "output": "passed"})}]

    monkeypatch.setattr(job, "_execute_tool_calls", fake_execute)

    gate = job._completion_verification_gate(workspace)
    second = job._completion_verification_gate(workspace)

    assert executed_commands == commands
    assert gate["allowed"] is True
    assert [row["command"] for row in gate["automatic_verifications"]] == commands
    assert gate["requested_verification_passed_count"] == 2
    assert "automatic_verifications" not in second


def test_completion_gate_skips_custom_then_runs_later_recognized_check(
    isolated_jobs, tmp_path, monkeypatch,
):
    workspace = tmp_path / "mixed-auto-verification"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("answer = 42\n", encoding="utf-8")
    job = code_jobs.CodeJob("mixed-auto-verification")
    job.directory.mkdir(parents=True, exist_ok=True)
    job.save(
        id=job.id,
        cwd=str(workspace),
        provider="openrouter",
        status="running",
        explicit_verification_commands=["python run_server.py", "python test_app.py"],
        task_strategy={"name": "direct"},
    )
    job.reset_turn_discipline("direct")
    job._task_strategy = code_jobs.code_harness_policy.classify_task("[direct] update app.py")
    job._record_mutation_state(workspace, target)
    executed_commands = []

    def fake_execute(_project, calls, _role):
        command = json.loads(calls[0]["function"]["arguments"])["command"]
        executed_commands.append(command)
        job._verification_ledger.record_command(command, 0, "passed")
        return [{"result": json.dumps({"exit_code": 0, "output": "passed"})}]

    monkeypatch.setattr(job, "_execute_tool_calls", fake_execute)

    gate = job._completion_verification_gate(workspace)

    assert executed_commands == ["python test_app.py"]
    assert gate["allowed"] is False
    assert gate["continuation"] is True
    assert gate["outstanding_verification_commands"] == ["python run_server.py"]


def test_completion_gate_stops_requested_batch_on_first_failure(
    isolated_jobs, tmp_path, monkeypatch,
):
    workspace = tmp_path / "failed-auto-verification"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("answer = 42\n", encoding="utf-8")
    commands = ["python test_one.py", "python test_two.py"]
    job = code_jobs.CodeJob("failed-auto-verification")
    job.directory.mkdir(parents=True, exist_ok=True)
    job.save(
        id=job.id,
        cwd=str(workspace),
        provider="openrouter",
        status="running",
        explicit_verification_commands=commands,
        task_strategy={"name": "direct"},
    )
    job.reset_turn_discipline("direct")
    job._task_strategy = code_jobs.code_harness_policy.classify_task("[direct] update app.py")
    job._record_mutation_state(workspace, target)
    executed_commands = []

    def fake_execute(_project, calls, _role):
        command = json.loads(calls[0]["function"]["arguments"])["command"]
        executed_commands.append(command)
        job._verification_ledger.record_command(command, 1, "failed")
        return [{"result": json.dumps({"exit_code": 1, "output": "failed"})}]

    monkeypatch.setattr(job, "_execute_tool_calls", fake_execute)

    gate = job._completion_verification_gate(workspace)

    assert executed_commands == [commands[0]]
    assert gate["allowed"] is False
    assert gate["failing_evidence_count"] == 1
    assert gate["outstanding_verification_commands"] == [commands[1]]


def test_direct_custom_requested_check_must_be_observed_before_completion(
    isolated_jobs, tmp_path, monkeypatch,
):
    workspace = tmp_path / "custom-required-verification"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("answer = 42\n", encoding="utf-8")
    command = "python run.py sample.json"
    job = code_jobs.CodeJob("custom-required-verification")
    job.directory.mkdir(parents=True, exist_ok=True)
    job.save(
        id=job.id,
        cwd=str(workspace),
        provider="openrouter",
        status="running",
        explicit_verification_commands=[command],
        task_strategy={"name": "direct"},
    )
    job.reset_turn_discipline("direct")
    job._task_strategy = code_jobs.code_harness_policy.classify_task("[direct] update app.py")
    job._record_mutation_state(workspace, target)
    monkeypatch.setattr(
        job,
        "_execute_tool_calls",
        lambda *_args, **_kwargs: pytest.fail("custom commands must not be auto-launched"),
    )

    missing = job._completion_verification_gate(workspace)
    job._verification_ledger.record_command(command, 0, "ok", explicit_verification=True)
    complete = job._completion_verification_gate(workspace)

    assert missing["allowed"] is False
    assert missing["outstanding_verification_commands"] == [command]
    assert complete["allowed"] is True


def test_direct_javascript_completion_uses_fresh_edit_diagnostic(isolated_jobs, tmp_path, monkeypatch):
    workspace = tmp_path / "direct-js-diagnostic"
    workspace.mkdir()
    target = workspace / "app.js"
    target.write_text("export const answer = 42;\n", encoding="utf-8")
    job = code_jobs.CodeJob("direct-js-diagnostic")
    job.directory.mkdir(parents=True, exist_ok=True)
    job.save(
        id=job.id,
        cwd=str(workspace),
        provider="openrouter",
        status="running",
        explicit_verification_commands=[],
        task_strategy={"name": "direct"},
    )
    job.reset_turn_discipline("direct")
    job._task_strategy = code_jobs.code_harness_policy.classify_task(
        "[direct] update app.js"
    )
    job._record_mutation_state(workspace, target)
    monkeypatch.setattr(
        job, "_execute_tool_calls",
        lambda *_args, **_kwargs: pytest.fail("DIRECT must not invent a shell verifier"),
    )

    gate = job._completion_verification_gate(workspace)

    assert gate["allowed"] is True
    assert gate["continuation"] is False
    assert gate["automatic_diagnostics_passed"] is True


def test_completion_gate_does_not_auto_run_non_verification_prompt_commands(isolated_jobs, tmp_path, monkeypatch):
    workspace = tmp_path / "no-auto-server"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("answer = 42\n", encoding="utf-8")
    job = code_jobs.CodeJob("no-auto-server")
    job.directory.mkdir(parents=True, exist_ok=True)
    job.save(
        id=job.id,
        cwd=str(workspace),
        provider="openrouter",
        status="running",
        explicit_verification_commands=["python run_server.py"],
        task_strategy={"name": "planned"},
    )
    job.reset_turn_discipline("planned")
    job._record_mutation_state(workspace, target)
    monkeypatch.setattr(
        job, "_execute_tool_calls",
        lambda *_args, **_kwargs: pytest.fail("a server command must not be auto-run"),
    )

    gate = job._completion_verification_gate(workspace)

    assert gate["allowed"] is False
    assert gate["continuation"] is True
    assert "automatic_verification" not in gate


def test_completion_gate_prompt_gives_state_specific_next_action():
    syntax = code_jobs.CodeJob._completion_gate_prompt({
        "reason": "Fresh diagnostics failed.",
        "failed_diagnostic_paths": ["src/app.js"],
    })
    planned = code_jobs.CodeJob._completion_gate_prompt({
        "reason": "Explicit verification is missing.",
        "requires_explicit_verification": True,
        "automatic_diagnostics_passed": True,
    })
    coverage = code_jobs.CodeJob._completion_gate_prompt({
        "reason": "Coverage is incomplete.",
        "source_paths": ["src/a.py", "src/b.py"],
        "verification_covered_paths": ["src/a.py"],
        "ignored_passing_evidence_count": 1,
    })

    assert "another check cannot unlock" in syntax
    assert "do not run another syntax or version probe" in planned
    assert "src/b.py" in coverage and "src/a.py" not in coverage.split("still-unverified paths:", 1)[1]


def test_compaction_keeps_the_newest_tool_output_readable() -> None:
    """A blanked receipt is indistinguishable from an empty file.

    When the model cannot tell the difference it answers by calling the same
    tool again, which is how a tight context budget became a read loop.
    """
    body = "<!doctype html>\n" + ("<div class='row'>content</div>\n" * 400)
    history = [{"role": "system", "content": "S" * 3_000},
               {"role": "user", "content": "fix the layout"}]
    for index in range(8):
        history.append({"role": "assistant", "tool_calls": [{
            "id": f"c{index}",
            "function": {"name": "read_file",
                         "arguments": {"relative_path": "page.html"}},
        }]})
        history.append({"role": "tool", "tool_call_id": f"c{index}", "content": body})

    compacted = code_jobs.CodeJob._compact_local_history(history, 12_000)

    assert len(json.dumps(compacted)) <= 12_000
    results = [item for item in compacted if item.get("role") == "tool"]
    newest = results[-1]["content"]
    assert not newest.startswith(code_jobs.BLANKED_TOOL_RECEIPT[:24])
    assert "<div" in newest, "the model must still read what it just fetched"
    # Older receipts are still surrendered; only the working tail is protected.
    assert any(item["content"].startswith(code_jobs.BLANKED_TOOL_RECEIPT[:24])
               for item in results[:-1])


def test_clipping_alone_does_not_release_read_reuse() -> None:
    """Reuse is only invalid once the evidence is actually gone.

    Releasing it on any rewrite is what let the model ask for the same file
    forever: compaction blanked the answer, then lifted the guard that would
    have stopped the repeat.
    """
    before = [{"role": "tool", "tool_call_id": "a", "content": "x" * 5_000}]
    clipped = [{"role": "tool", "tool_call_id": "a", "content": "x" * 2_000 + "[clipped]"}]
    blanked = [{"role": "tool", "tool_call_id": "a",
                "content": code_jobs.BLANKED_TOOL_RECEIPT}]

    assert code_jobs._evidence_left_history(before, clipped) is False
    assert code_jobs._evidence_left_history(before, blanked) is True
    assert code_jobs._evidence_left_history(before, []) is True
    assert code_jobs._evidence_left_history(before, before) is False


def test_protected_tail_walks_whole_tool_groups() -> None:
    """A protected result must never be split from the call it answers."""
    body = [{"role": "assistant", "tool_calls": [{"id": "a"}]},
            {"role": "tool", "tool_call_id": "a"},
            {"role": "tool", "tool_call_id": "a"},
            {"role": "assistant", "tool_calls": [{"id": "b"}]},
            {"role": "tool", "tool_call_id": "b"}]

    assert code_jobs.CodeJob._protected_tail_start(body, 1) == 3
    assert code_jobs.CodeJob._protected_tail_start(body, 2) == 0
    assert code_jobs.CodeJob._protected_tail_start(body, 0) == len(body)


def test_shell_timeout_tells_the_model_how_to_run_a_server() -> None:
    """A blocking server times out, and a bare timeout reads as a broken command.

    Without an alternative the model retries the same foreground command; the
    observed failure was `python -m http.server` attempted four times in a row.
    """
    hint = code_jobs.SERVER_TIMEOUT_HINT
    assert "detached" in hint
    assert "Start-Process" in hint, "Windows needs the concrete recipe, not just advice"
    assert "Do not retry it unchanged" in hint

    run_shell = next(t["function"] for t in code_jobs.CodeJob._local_tool_schema()
                     if t["function"]["name"] == "run_shell")
    # The first attempt should already be right, not just the retry.
    assert "detached" in run_shell["description"]
    assert run_shell["parameters"]["properties"]["timeout_seconds"].get("description"),         "an undocumented timeout is why the model guessed 20s for a server"


def test_compaction_leaves_headroom_on_a_small_window() -> None:
    """One compaction must buy more than a single tool result.

    A 4k-token tool schema against a 32k window pushed both the trigger and the
    target onto their floors: compaction fired at 3,028 tokens and compacted to
    3,000, so every round compacted and the model could never hold a whole file.
    """
    import code_harness_policy

    allocation = code_harness_policy.context_budget("direct", 32_768)
    # The flat 16k output reserve must not claim half a small window.
    assert allocation.output_reserve_tokens <= 32_768 // 4
    assert allocation.working_tokens >= 24_000

    schema = [t for t in code_jobs.CodeJob._local_tool_schema()
              if t["function"]["name"] not in ("spawn_agent", "consult")]
    schema_tokens = code_harness_policy.estimate_tokens(
        json.dumps(schema, ensure_ascii=False, separators=(",", ":")))
    threshold = code_jobs.AUTO_COMPACT_THRESHOLD
    ratio = min(code_jobs.COMPACT_TARGET_RATIO, threshold * 0.7)

    trigger = max(3_000, int(allocation.working_tokens * threshold) - schema_tokens - 1_024)
    target = max(3_000, min(trigger, int(allocation.working_tokens * ratio) - schema_tokens - 1_024))
    target = min(target, max(1_000, int(trigger * code_jobs.COMPACT_HEADROOM_RATIO)))

    headroom = trigger - target
    assert headroom > 2_000, f"only {headroom} tokens between compactions"


def test_large_context_windows_keep_their_output_reserve() -> None:
    """The small-window cap must not shrink the reserve cloud models rely on."""
    import code_harness_policy

    for window, expected in ((200_000, 30_000), (1_000_000, 150_000)):
        allocation = code_harness_policy.context_budget("direct", window)
        assert allocation.output_reserve_tokens == expected, window


def test_repeated_compaction_never_empties_the_working_state() -> None:
    """The state message is JSON, and half a JSON object parses as nothing.

    The generic long-message truncation cut it mid-object, the next pass failed
    to parse it and treated it as no state at all, and the model was handed
    `active_user_requests: []` beside "continue, do not rediscover".
    """
    marker = code_jobs.COMPACTED_STATE_MARKER
    request = "set up a website to stream f1 for free"
    state = {
        "active_user_requests": [request],
        "recent_dialogue": ["assistant: " + "d" * 4_000],
        "durable_state": ["EDIT app.py"],
        "recent_evidence": ["READ app.py"],
        "next_action": "Continue from this state.",
    }
    history = [{"role": "system", "content": "S" * 3_000},
               {"role": "user", "content": marker + chr(10) + json.dumps(state, indent=2)}]
    for index in range(6):
        history.append({"role": "assistant", "tool_calls": [{
            "id": f"c{index}",
            "function": {"name": "read_file", "arguments": {"relative_path": "page.html"}},
        }]})
        history.append({"role": "tool", "tool_call_id": f"c{index}", "content": "x" * 3_000})

    limit = 12_000
    for _ in range(7):
        history = code_jobs.CodeJob._compact_local_history(history, limit)
        assert len(json.dumps(history, ensure_ascii=False)) <= limit

        carried = next((m for m in history
                        if str(m.get("content") or "").startswith(marker)), None)
        assert carried is not None, "the working state disappeared entirely"
        body = str(carried["content"])[len(marker):].strip()
        parsed = json.loads(body)   # must never be half an object
        joined = " ".join(str(r) for r in (parsed.get("active_user_requests") or []))
        assert request in joined, "the operator's actual request was compacted away"
        limit = max(1_000, int(limit * 0.72))


def test_damaged_working_state_is_salvaged_not_discarded() -> None:
    """Unparseable state must not be silently downgraded to no state."""
    marker = code_jobs.COMPACTED_STATE_MARKER
    damaged = marker + chr(10) + '{"active_user_requests": ["stream f1"], "recent_'
    body = [{"role": "user", "content": damaged}]

    summary = code_jobs.CodeJob._compacted_working_summary(body, [])

    text = json.dumps(summary)
    assert "stream f1" in text, "the damaged state was thrown away instead of salvaged"


def test_reasoning_only_overrun_is_recoverable_not_terminal() -> None:
    """A turn that spends its whole response reasoning must not end the turn.

    Measured: the same history and tools produced 22,993 characters of thinking
    and zero tool calls with thinking on, and a clean tool call with it off.
    `done_reason=length` used to be terminal, so nothing recovered and nothing
    even noticed for eight minutes -- the round cap counts rounds, not time.
    """
    source = inspect.getsource(code_jobs.CodeJob._run_ollama)

    # The overrun is detected and separated from the unsafe replay cases.
    assert "overran_on_thinking" in source
    assert "retryable_eof = overran_on_thinking or (" in source

    # Replay is only safe with nothing emitted: no content, no tool call.
    detection = source.split("overran_on_thinking = (", 1)[1][:600]
    assert "not candidate_tools" in detection
    assert "not candidate_content" in detection
    assert 'round_stop_reason.strip().casefold() == "length"' in detection

    # And the retry must actually drop thinking, or it just overruns again.
    assert 'reasoning="off" if thinking_ran_away else request_reasoning' in source
    assert "thinking_ran_away = True" in source


def test_normal_local_turns_do_not_have_an_artificial_output_cap() -> None:
    """Long local responses use the model/context limit, not a harness allowance."""
    source = inspect.getsource(code_jobs.CodeJob._run_ollama)
    options = source.split("request_options = {", 1)[1].split("}", 1)[0]
    assert "num_predict" not in options
    assert "num_predict" not in source


def test_tool_cards_say_what_was_read_and_how_much_is_left() -> None:
    """A bare path makes four reads of one file look identical.

    With the range on the card, progress through a file is distinguishable from
    re-reading the same region, which is the thing that was impossible to see.
    """
    line = code_jobs.CodeJob._tool_detail_line

    detail = line("read_file", {"relative_path": "f1-stream.html", "start_line": 240},
                  {"path": "f1-stream.html", "start_line": 240, "next_line": 300,
                   "total_lines": 299})
    assert "f1-stream.html" in detail and "240" in detail and "299" in detail

    # A partial read must advertise that there is more.
    partial = line("read_file", {"relative_path": "f1-stream.html", "start_line": 1},
                   {"path": "f1-stream.html", "start_line": 1, "next_line": 121,
                    "total_lines": 299, "truncated": True})
    assert "more follows" in partial

    # Character-addressed reads carry their own counters.
    chars = line("read_file", {"relative_path": "a.html"},
                 {"path": "a.html", "offset": 0, "next_offset": 12_000,
                  "total_chars": 13_210, "truncated": True})
    assert "13,210" in chars

    assert line("search_text", {"query": "f1"}, {"matches": []}) == '"f1" · no matches'
    many = line("search_text", {"query": "f1"},
                {"matches": [{"path": "a"}, {"path": "a"}, {"path": "b"}]})
    assert "3 matches in 2 files" in many
    assert "1 match in 1 file" in line("search_text", {"query": "x"}, {"matches": [{"path": "a"}]})
    assert "14 entries" in line("list_dir", {"relative_path": "."},
                                {"entries": [{"path": str(i)} for i in range(14)]})
    assert "1 entry" in line("list_dir", {"relative_path": "."}, {"entries": [{"path": "a"}]})

    # A failing command should say so on the card itself.
    assert "exit 1" in line("run_shell", {"command": "pytest -q"}, {"exit_code": 1})
    assert line("run_shell", {"command": "pytest -q"}, {"exit_code": 0}) == "pytest -q"

    # Unknown tools still fall back to something rather than an empty card.
    assert line("mystery_tool", {"relative_path": "x.py"}, {}) == "x.py"
    assert line("read_file", {}, {}) == "."


def test_turn_rate_is_weighted_by_tokens_not_wall_clock() -> None:
    """Dividing output by elapsed folds tool time in and understates the model.

    The turn figure also has to be token-weighted: a five-token round finishing
    fast must not drag the average away from what the model actually sustains.
    """
    rates = code_jobs.CodeJob._stage_generation_rates
    meta = {"model_request_rounds": [
        {"started_at": 100.0, "finished_at": 110.0, "tokens_per_second": 50.0,
         "usage": {"output_tokens": 500}},
        {"started_at": 120.0, "finished_at": 121.0, "tokens_per_second": 10.0,
         "usage": {"output_tokens": 10}},
    ]}
    last, mean = rates(meta, 50.0)
    assert last == 10.0, "the latest round is the one that just finished"
    # 510 tokens over 10s + 1s -> ~46.4, not the flat mean of 30.
    assert 46.0 < mean < 47.0, mean

    # Rounds from an earlier turn are excluded.
    last_only, mean_only = rates(meta, 115.0)
    assert last_only == 10.0 and mean_only == 10.0

    # Nothing measurable must not fabricate a number.
    assert rates({"model_request_rounds": []}, 0.0) == (None, None)
    assert rates({"model_request_rounds": [
        {"started_at": 1.0, "tokens_per_second": 0, "usage": {"output_tokens": 5}}]}, 0.0) == (None, None)


def test_truncated_tool_call_is_recovered_not_fatal() -> None:
    """Ollama answers a tool call cut off mid-JSON with a 500, not a length stop.

    The generic handler raised RuntimeError and killed the turn, even though
    nothing had been executed and nothing streamed. Bounding the output made
    this reachable, so it has to be recoverable.
    """
    import io
    from urllib.error import HTTPError

    def http500(body: str) -> HTTPError:
        return HTTPError("http://localhost:11434/api/chat", 500, "Internal Server Error",
                         {}, io.BytesIO(body.encode()))

    real = http500('{"error":"llama-server returned invalid tool call arguments for '
                   '\\"find_symbol\\": unexpected end of JSON input"}')
    assert code_jobs._is_truncated_tool_call_error(real) is True

    # A genuine server fault must still be fatal.
    assert code_jobs._is_truncated_tool_call_error(http500('{"error":"out of memory"}')) is False
    assert code_jobs._is_truncated_tool_call_error(RuntimeError("connection reset")) is False
    # A body that cannot be read must not raise from inside the check.
    class Unreadable(Exception):
        def read(self):
            raise OSError("stream already consumed")
    assert code_jobs._is_truncated_tool_call_error(Unreadable()) is False

    source = inspect.getsource(code_jobs.CodeJob._run_ollama)
    assert "_is_truncated_tool_call_error(exc)" in source
    assert "incomplete_retries < PROVIDER_INCOMPLETE_STREAM_RETRIES" in source
