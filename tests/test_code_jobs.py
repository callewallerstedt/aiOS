import json
import threading
import time
from pathlib import Path

import pytest

import code_jobs


@pytest.fixture()
def isolated_jobs(tmp_path, monkeypatch):
    jobs_dir = tmp_path / "code_jobs"
    jobs_dir.mkdir()
    monkeypatch.setattr(code_jobs, "JOBS_DIR", jobs_dir)
    monkeypatch.setattr(code_jobs, "CAPABILITIES_CACHE", jobs_dir / "capabilities.json")
    monkeypatch.setattr(code_jobs, "provider_status", lambda provider: (True, f"{provider} ready"))
    monkeypatch.setattr(
        code_jobs,
        "capabilities",
        lambda force=False: {
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
        },
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
