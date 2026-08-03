import json
import time
from pathlib import Path

import code_handoff
import code_jobs


def base_meta(tmp_path):
    return {
        "id": "logical123",
        "title": "Provider bridge",
        "provider": "claude",
        "model": "sonnet",
        "reasoning": "high",
        "fast": False,
        "native_session_id": "claude-native-1",
        "cwd": str(tmp_path),
        "brief": (
            "Implement the session bridge.\n"
            "Constraint: preserve existing APIs.\n"
            "Decision: use a provider-neutral manifest."
        ),
        "last_summary": "The serializer is complete and backend wiring is next.",
        "pending_question": "Should the switch interrupt the active turn?",
        "created_at": 10.0,
    }


def sample_events():
    return [
        {"ts": 11.0, "kind": "user", "text": "Keep the same aiOS job id."},
        {"ts": 12.0, "kind": "assistant", "text": "I changed the serializer and will wire the API next."},
        {
            "ts": 13.0,
            "kind": "activity",
            "activity_type": "files",
            "phase": "completed",
            "title": "Edited files",
            "files": ["code_handoff.py"],
        },
    ]


def test_handoff_manifest_serialization_round_trip(tmp_path):
    manifest = code_handoff.build_manifest(
        base_meta(tmp_path),
        sample_events(),
        target_provider="codex",
        target_model="gpt-test",
        target_reasoning="high",
        target_fast=True,
        instruction="Continue with API wiring.",
        worktree_changes=[{"path": "server.py", "status": "M", "source": "git"}],
        handoff_id="handoff-test-1",
    )

    payload = code_handoff.serialize_manifest(manifest)
    restored = code_handoff.deserialize_manifest(payload)

    assert restored == manifest
    assert restored["schema"] == "aios.code-handoff"
    assert restored["schema_version"] == 1
    assert restored["native_continuation"] is False


def test_handoff_context_transfers_required_fields(tmp_path):
    manifest = code_handoff.build_manifest(
        base_meta(tmp_path),
        sample_events(),
        target_provider="cursor",
        target_model="composer-test",
        target_reasoning="auto",
        target_fast=False,
        worktree_changes=[{"path": "server.py", "status": "M", "source": "git"}],
    )

    context = manifest["context"]
    assert manifest["logical_session"]["cwd"] == str(tmp_path)
    assert "Implement the session bridge" in context["task_summary"]
    assert "backend wiring is next" in context["conversation_summary"]
    assert any("preserve existing APIs" in item for item in context["constraints"])
    assert any("provider-neutral manifest" in item for item in context["decisions"])
    assert context["pending_questions"] == ["Should the switch interrupt the active turn?"]
    assert {item["path"] for item in context["files_changed"]} == {"code_handoff.py", "server.py"}
    assert context["recent_agent_output"] == ["I changed the serializer and will wire the API next."]


def test_provider_switch_keeps_logical_job_and_starts_fresh_native_session(tmp_path, monkeypatch):
    jobs_dir = tmp_path / "jobs"
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
                {"provider": "codex", "ready": True, "models": [
                    {"id": "gpt-test", "reasoning": ["high"], "fast": True}
                ]},
                {"provider": "claude", "ready": True, "models": [
                    {"id": "sonnet", "reasoning": ["high"], "fast": False}
                ]},
            ],
        },
    )
    monkeypatch.setattr(
        code_handoff,
        "collect_worktree_changes",
        lambda _cwd: [{"path": "changed.py", "status": "M", "source": "git"}],
    )
    with code_jobs._REGISTRY_LOCK:
        code_jobs._LIVE.clear()

    job = code_jobs.CodeJob("logical123")
    job.directory.mkdir()
    meta = {
        **base_meta(tmp_path),
        "status": "completed",
        "queued": 0,
        "provider_sessions": [{
            "provider": "claude",
            "model": "sonnet",
            "reasoning": "high",
            "fast": False,
            "native_session_id": "claude-native-1",
            "started_at": 10.0,
        }],
        "handoffs": [],
    }
    job.meta_path.write_text(json.dumps(meta), encoding="utf-8")
    job.events_path.touch()
    for event in sample_events():
        job.append(event.pop("kind"), event.pop("text", ""), **event)
    code_jobs._LIVE[job.id] = job
    queued = []
    monkeypatch.setattr(job, "_queue_payload", lambda payload, attachments=None: queued.append(payload))

    result = code_jobs.handoff_job(
        job.id,
        "codex",
        "gpt-test",
        "high",
        True,
        "Finish the API and tests.",
    )

    assert result["ok"] is True
    assert result["job"]["id"] == "logical123"
    assert result["job"]["provider"] == "codex"
    assert result["job"]["model"] == "gpt-test"
    assert result["job"]["native_session_id"] == ""
    assert result["job"]["provider_sessions"][0]["native_session_id"] == "claude-native-1"
    assert result["job"]["provider_sessions"][0]["ended_at"]
    assert result["job"]["provider_sessions"][1]["provider"] == "codex"
    assert len(queued) == 1
    assert "new native provider session" in queued[0]
    assert "claude-native-1" in queued[0]
    assert "Finish the API and tests." in queued[0]

    events = code_jobs.read_events(job.id, 0)["events"]
    switch = next(event for event in events if event["kind"] == "provider_switch")
    assert switch["from_provider"] == "claude"
    assert switch["to_provider"] == "codex"
    assert switch["to_model"] == "gpt-test"
    assert switch["native_continuation"] is False
    manifest = code_handoff.deserialize_manifest(Path(result["handoff"]["manifest"]).read_text(encoding="utf-8"))
    assert "changed.py" in {item["path"] for item in manifest["context"]["files_changed"]}


def test_active_handoff_interrupts_source_before_queuing_target(tmp_path, monkeypatch):
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    monkeypatch.setattr(code_jobs, "JOBS_DIR", jobs_dir)
    monkeypatch.setattr(code_jobs, "provider_status", lambda _provider: (True, "ready"))
    monkeypatch.setattr(
        code_jobs,
        "selection_error",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(code_handoff, "collect_worktree_changes", lambda _cwd: [])
    with code_jobs._REGISTRY_LOCK:
        code_jobs._LIVE.clear()
    job = code_jobs.CodeJob("active123")
    job.directory.mkdir()
    meta = {
        **base_meta(tmp_path),
        "id": job.id,
        "status": "running",
        "queued": 0,
        "provider_sessions": [],
        "handoffs": [],
    }
    job.meta_path.write_text(json.dumps(meta), encoding="utf-8")
    job.events_path.touch()
    code_jobs._LIVE[job.id] = job
    order = []

    def fake_stop(*, interrupted=False):
        order.append(("stop", interrupted))
        job.save(status="interrupted", queued=0)
        return {"ok": True}

    monkeypatch.setattr(job, "stop", fake_stop)
    monkeypatch.setattr(job, "_queue_payload", lambda payload, attachments=None: order.append(("queue", payload)))

    result = job.handoff("cursor", "composer-test", "auto", False)

    assert result["ok"] is True
    assert order[0] == ("stop", True)
    assert order[1][0] == "queue"
    assert job.load()["provider"] == "cursor"


def test_handoff_bridge_runs_as_first_target_turn_and_records_new_native_id(tmp_path, monkeypatch):
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    monkeypatch.setattr(code_jobs, "JOBS_DIR", jobs_dir)
    monkeypatch.setattr(code_jobs, "provider_status", lambda _provider: (True, "ready"))
    monkeypatch.setattr(code_jobs, "selection_error", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(code_handoff, "collect_worktree_changes", lambda _cwd: [])
    with code_jobs._REGISTRY_LOCK:
        code_jobs._LIVE.clear()
    job = code_jobs.CodeJob("continue123")
    job.directory.mkdir()
    job.meta_path.write_text(json.dumps({
        **base_meta(tmp_path),
        "id": job.id,
        "status": "completed",
        "queued": 0,
        "provider_sessions": [],
        "handoffs": [],
    }), encoding="utf-8")
    job.events_path.touch()
    job.append("assistant", "Source provider completed the serializer.")
    code_jobs._LIVE[job.id] = job
    target_payloads = []

    def fake_codex(self, payload, attachments):
        target_payloads.append(payload)
        self.record_native_session("codex-native-2")
        return "completed", "Target provider continued successfully."

    monkeypatch.setattr(code_jobs.CodeJob, "_run_codex", fake_codex)

    result = job.handoff("codex", "gpt-test", "high", True, "Continue with backend wiring.")
    assert result["ok"] is True
    deadline = time.time() + 3
    while job.load().get("status") != "completed" and time.time() < deadline:
        time.sleep(0.02)

    meta = job.load()
    assert meta["status"] == "completed"
    assert meta["native_session_id"] == "codex-native-2"
    assert meta["provider_sessions"][-1]["native_session_id"] == "codex-native-2"
    assert len(target_payloads) == 1
    assert "Source provider completed the serializer." in target_payloads[0]
    assert "Continue with backend wiring." in target_payloads[0]
    kinds = [event["kind"] for event in code_jobs.read_events(job.id, 0)["events"]]
    assert kinds.index("provider_switch") < kinds.index("result")
