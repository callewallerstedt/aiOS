import os
import sys
from pathlib import Path


os.environ.setdefault("AIOS_SKIP_WHISPER_PRELOAD", "1")
APP_DIR = Path(__file__).resolve().parents[1] / "agent_clicker" / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import server  # noqa: E402


def test_code_routes_expose_unified_contract(monkeypatch):
    import code_jobs

    job = {
        "id": "abc123",
        "provider": "codex",
        "cwd": r"C:\project",
        "model": "gpt-test",
        "reasoning": "high",
        "fast": True,
        "status": "queued",
    }
    monkeypatch.setattr(code_jobs, "list_jobs", lambda limit=100: [job])
    monkeypatch.setattr(code_jobs, "create_job", lambda *args, **kwargs: {"ok": True, "job": job})
    monkeypatch.setattr(code_jobs, "get_job", lambda job_id: job if job_id == "abc123" else None)
    monkeypatch.setattr(code_jobs, "send_message", lambda job_id, text, **kwargs: {"ok": True, "job": job, "queued": True})
    handoff_calls = []
    monkeypatch.setattr(
        code_jobs,
        "handoff_job",
        lambda job_id, provider, model, reasoning, fast=False, instruction="": handoff_calls.append(
            (job_id, provider, model, reasoning, fast, instruction)
        ) or {"ok": True, "job": {**job, "provider": provider, "model": model}, "handoff": {"to_provider": provider}},
    )
    monkeypatch.setattr(code_jobs, "stop_job", lambda job_id: {"ok": True, "job": {**job, "status": "stopped"}})
    delete_calls = []
    monkeypatch.setattr(
        code_jobs,
        "delete_job",
        lambda job_id, confirmed=False: delete_calls.append((job_id, confirmed)) or {"ok": confirmed},
    )
    monkeypatch.setattr(code_jobs, "setup_provider", lambda provider: {"ok": True, "provider": provider, "launched": True})

    client = server.app.test_client()
    assert client.get("/code").status_code == 200
    listing = client.get("/api/code/jobs")
    assert listing.status_code == 200
    assert listing.get_json()["jobs"][0]["provider"] == "codex"
    created = client.post(
        "/api/code/jobs",
        json={
            "provider": "codex",
            "cwd": r"C:\project",
            "brief": "build it",
            "model": "gpt-test",
            "reasoning": "high",
            "fast": True,
        },
    )
    assert created.status_code == 201
    assert created.get_json()["job"]["id"] == "abc123"
    continued = client.post("/api/code/jobs/abc123/messages", json={"text": "continue", "urgent": True})
    assert continued.status_code == 200
    assert continued.get_json()["queued"] is True
    handed_off = client.post(
        "/api/code/jobs/abc123/handoff",
        json={
            "provider": "cursor",
            "model": "composer-test",
            "reasoning": "auto",
            "fast": False,
            "instruction": "Continue from the bridge.",
        },
    )
    assert handed_off.status_code == 200
    assert handed_off.get_json()["job"]["provider"] == "cursor"
    assert handoff_calls == [("abc123", "cursor", "composer-test", "auto", False, "Continue from the bridge.")]
    stopped = client.post("/api/code/jobs/abc123/stop")
    assert stopped.get_json()["job"]["status"] == "stopped"
    rejected_delete = client.delete("/api/code/jobs/abc123")
    assert rejected_delete.status_code == 400
    confirmed_delete = client.delete("/api/code/jobs/abc123", json={"confirm": "abc123"})
    assert confirmed_delete.status_code == 200
    assert delete_calls == [("abc123", False), ("abc123", True)]
    setup = client.post("/api/code/providers/claude/setup")
    assert setup.get_json() == {"ok": True, "provider": "claude", "launched": True}


def test_phone_aliases_use_same_jobs(monkeypatch):
    import code_jobs

    monkeypatch.setattr(code_jobs, "list_jobs", lambda limit=100: [{"id": "phone1", "provider": "cursor"}])
    client = server.app.test_client()
    payload = client.get("/api/phone/code/jobs").get_json()
    assert payload["jobs"] == [{"id": "phone1", "provider": "cursor"}]
