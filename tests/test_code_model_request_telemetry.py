from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import code_jobs  # noqa: E402


def _job(tmp_path: Path) -> code_jobs.CodeJob:
    job = code_jobs.CodeJob("request-telemetry", tmp_path / "job")
    job.save(
        id=job.id,
        provider="openrouter",
        model="test/model",
        created_at=1.0,
        pipeline_stages={"planner": {"phase": "started", "started_at": 2.0}},
    )
    return job


def test_local_model_requests_persist_exact_count_role_usage_and_stop_reason(tmp_path):
    job = _job(tmp_path)
    first = job._begin_model_request("openrouter", "test/model", round_index=1)
    job._finish_model_request(
        first,
        usage={"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
        generation_id="gen-123",
        stop_reason="tool_calls",
    )
    second = job._begin_model_request("openrouter", "test/model", round_index=1, attempt=2)
    job._finish_model_request(second, status="failed", stop_reason="error", error="provider timeout")

    meta = json.loads(job.meta_path.read_text(encoding="utf-8"))
    assert meta["model_request_count"] == 2
    assert meta["model_request_count_source"] == "aios_local_provider_loop"
    assert [row["sequence"] for row in meta["model_request_rounds"]] == [1, 2]
    assert meta["model_request_rounds"][0]["role"] == "planner"
    assert meta["model_request_rounds"][0]["usage"]["total_tokens"] == 15
    assert meta["model_request_rounds"][0]["generation_id"] == "gen-123"
    assert meta["model_request_rounds"][0]["stop_reason"] == "tool_calls"
    assert meta["model_request_rounds"][1]["attempt"] == 2
    assert meta["model_request_rounds"][1]["status"] == "failed"
    assert meta["model_request_rounds"][1]["error"] == "provider timeout"


def test_local_model_request_rows_are_bounded_without_losing_exact_total(tmp_path, monkeypatch):
    monkeypatch.setattr(code_jobs, "MODEL_REQUEST_ROUND_LIMIT", 2)
    job = _job(tmp_path)
    for round_index in range(1, 4):
        sequence = job._begin_model_request("ollama", "local/model", round_index=round_index)
        job._finish_model_request(sequence, usage={"total_tokens": round_index}, stop_reason="stop")

    meta = job.load()
    assert meta["model_request_count"] == 3
    assert meta["model_request_rounds_omitted"] == 1
    assert [row["sequence"] for row in meta["model_request_rounds"]] == [2, 3]


def test_local_model_request_persists_reasoning_effort(tmp_path):
    job = _job(tmp_path)

    job._begin_model_request(
        "openrouter", "stealth/ox-alpha", round_index=1, role="coder", reasoning="max",
    )

    assert job.load()["model_request_rounds"][-1]["reasoning"] == "max"


def test_local_model_usage_trips_resumable_turn_token_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(code_jobs, "TURN_MODEL_TOKEN_BUDGET", 10)
    monkeypatch.setattr(code_jobs, "LARGE_TURN_MODEL_TOKEN_BUDGET", 20)
    job = _job(tmp_path)
    job.reset_turn_discipline("standard")

    sequence = job._begin_model_request("openrouter", "test/model", round_index=1)
    job._finish_model_request(
        sequence,
        usage={"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10},
        stop_reason="tool_calls",
    )

    assert job._turn_model_tokens == 10
    assert job._turn_force_finalize is True
    assert "10-token model budget" in job._turn_finalize_reason
