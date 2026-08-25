"""Harness efficiency: what the agent is *shown*, and what it is charged for.

Both rules here come from one observed failure. Asked to start a CODE session on
OpenRouter, the agent called code_capabilities four times and still answered
"OpenRouter isn't showing as available on this PC". The provider was ready the
whole time -- its one line sat 99% of the way through a 7,000 token payload that
was 88% Cursor's 194 models.

So: a tool result is evidence, not a database dump, and an identical read
repeated inside one turn is a loop to break rather than a question to re-answer.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import openrouter_client  # noqa: E402
import voice_agent  # noqa: E402


def capabilities_payload(cursor_models=194):
    """The real shape: one huge provider and one tiny one, tiny one last."""
    def models(prefix, count):
        return [
            {"id": f"{prefix}-{i}", "label": f"{prefix} {i}",
             "reasoning": ["off", "low", "medium", "high"],
             "default_reasoning": "medium", "fast": False, "default": i == 0}
            for i in range(count)
        ]
    return {
        "ok": True,
        "providers": [
            {"provider": "cursor", "ready": True, "message": "Cursor is ready",
             "models": models("cursor", cursor_models)},
            {"provider": "openrouter", "ready": True, "message": "OpenRouter ready",
             "models": models("deepseek/deepseek-v4-flash", 1)},
        ],
    }


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setattr(voice_agent, "agent_settings", lambda: dict(voice_agent.DEFAULT_AGENT_SETTINGS))
    instance = voice_agent.VoiceAgent(on_event=lambda *_: None)
    monkeypatch.setattr(
        voice_agent.VoiceAgent, "_code_request",
        lambda self, path, *a, **k: capabilities_payload(),
    )
    return instance


# ------------------------------------------------------- compact evidence


def test_readiness_survives_without_reading_the_whole_payload(agent):
    """The first line answers "what can I use right now"."""
    result = json.loads(agent._tool_code_capabilities({}))
    assert "openrouter: ready" in result["summary"]
    assert "cursor: ready" in result["summary"]


def test_a_survey_does_not_enumerate_two_hundred_models(agent):
    result = json.loads(agent._tool_code_capabilities({}))
    cursor = next(p for p in result["providers"] if p["provider"] == "cursor")
    assert len(cursor["models"]) <= agent.CAPABILITY_MODEL_PREVIEW
    # Truthful about what was withheld, and how to get it.
    assert cursor["model_count"] == 194
    assert cursor["models_omitted"] == 194 - len(cursor["models"])
    assert "provider=" in cursor["more"]


def test_a_small_provider_is_never_crowded_out(agent):
    """The bug: OpenRouter's single line lost behind Cursor's 194."""
    raw = agent._tool_code_capabilities({})
    result = json.loads(raw)
    openrouter = next(p for p in result["providers"] if p["provider"] == "openrouter")
    assert len(openrouter["models"]) == 1
    assert len(raw) < len(json.dumps(capabilities_payload())) / 3


def test_the_default_model_is_never_the_one_truncated_away(agent):
    result = json.loads(agent._tool_code_capabilities({}))
    cursor = next(p for p in result["providers"] if p["provider"] == "cursor")
    assert any(model["default"] for model in cursor["models"])


def test_naming_a_provider_returns_its_complete_list(agent):
    result = json.loads(agent._tool_code_capabilities({"provider": "cursor"}))
    assert [p["provider"] for p in result["providers"]] == ["cursor"]
    assert len(result["providers"][0]["models"]) == 194
    assert "models_omitted" not in result["providers"][0]


def test_an_unknown_provider_says_what_is_known(agent):
    result = json.loads(agent._tool_code_capabilities({"provider": "gpt9"}))
    assert result["ok"] is False
    assert "cursor" in result["error"] and "openrouter" in result["error"]


def test_model_labels_are_kept_so_the_agent_can_name_them(agent):
    result = json.loads(agent._tool_code_capabilities({"provider": "openrouter"}))
    assert result["providers"][0]["models"][0]["label"]


# ---------------------------------------------------------- loop breaking


def test_an_identical_read_is_answered_once_per_turn(agent):
    calls = []
    agent._tool_list_files = lambda arguments: calls.append(arguments) or "one file"
    first = agent._execute("list_files", {"path": "."})
    second = agent._execute("list_files", {"path": "."})
    assert len(calls) == 1
    assert "one file" in second
    assert "identical to your earlier call" in second


def test_different_arguments_are_a_different_question(agent):
    calls = []
    agent._tool_list_files = lambda arguments: calls.append(arguments) or "listing"
    agent._execute("list_files", {"path": "a"})
    agent._execute("list_files", {"path": "b"})
    assert len(calls) == 2


def test_live_state_is_never_served_from_cache(agent):
    """Re-reading the clipboard or the screen is legitimate, not a loop."""
    for tool in ("read_clipboard", "read_screen", "system_status", "code_status", "list_timers"):
        assert tool not in agent.REPEATABLE_READS


def test_a_new_turn_may_reread_what_the_last_turn_read(agent, monkeypatch):
    calls = []
    agent._tool_list_files = lambda arguments: calls.append(arguments) or "listing"
    agent._execute("list_files", {"path": "."})
    monkeypatch.setattr(
        voice_agent.VoiceAgent, "_run_ollama_locked",
        lambda self, transcript, settings, started: None,
    )
    monkeypatch.setattr(
        voice_agent, "agent_settings",
        lambda: {**voice_agent.DEFAULT_AGENT_SETTINGS, "agent_model": "ollama:x"},
    )
    agent._run_locked("next turn")
    agent._execute("list_files", {"path": "."})
    assert len(calls) == 2


def test_a_failing_tool_is_not_cached_as_an_answer(agent):
    """A transient failure must not be replayed for the rest of the turn."""
    def boom(arguments):
        raise RuntimeError("disk busy")

    agent._tool_list_files = boom
    assert "tool failed" in agent._execute("list_files", {"path": "."})
    agent._tool_list_files = lambda arguments: "it worked"
    assert agent._execute("list_files", {"path": "."}) == "it worked"


# ------------------------------------------- summaries on the other tools


def test_code_list_leads_with_what_needs_the_user(agent, monkeypatch):
    monkeypatch.setattr(voice_agent.VoiceAgent, "_code_request", lambda self, *a, **k: {
        "ok": True,
        "jobs": [
            {"id": "1", "title": "alpha", "status": "waiting_user", "provider": "codex",
             "pending_question": "which branch?"},
            {"id": "2", "title": "beta", "status": "completed", "provider": "codex",
             "last_summary": "x" * 800},
        ],
    })
    result = json.loads(agent._tool_code_list({"limit": 20}))
    assert "1 waiting on you" in result["summary"]
    assert "alpha" in result["summary"]
    # Prose is capped; a 500-char summary per row is what made the list fat.
    assert len(result["jobs"][1]["last_summary"]) <= 161


def test_code_status_answers_before_it_quotes(agent, monkeypatch):
    monkeypatch.setattr(voice_agent.VoiceAgent, "_code_request", lambda self, *a, **k: {
        "ok": True,
        "job": {"status": "waiting_user", "provider": "claude", "pending_question": "overwrite?"},
        "events": (
            [{"kind": "thinking", "text": "pondering"} for _ in range(30)]
            + [{"kind": "assistant", "text": "y" * 900}]
        ),
    })
    result = json.loads(agent._tool_code_status({"job_id": "1"}))
    assert "waiting_user" in result["summary"]
    assert "overwrite?" in result["summary"]
    kinds = {event["kind"] for event in result["recent_events"]}
    assert "thinking" not in kinds          # says nothing the status line does not
    assert len(result["recent_events"][-1]["text"]) <= 301


def test_short_keeps_everything_that_fits():
    assert voice_agent._short("hello", 10) == "hello"
    assert voice_agent._short("hello world", 8).endswith("…")
    assert len(voice_agent._short("x" * 500, 100)) == 100


# ------------------------------------------------------------ cheap models


def test_scout_models_are_declared_and_real():
    assert openrouter_client.SCOUT_MODELS
    catalog = {row["id"] for row in openrouter_client.MODEL_CATALOG}
    assert set(openrouter_client.SCOUT_MODELS) <= catalog


def test_scouts_are_cheap_fast_and_never_the_default():
    for row in openrouter_client.MODEL_CATALOG:
        if not row.get("scout"):
            continue
        assert row["fast"] is True
        assert row["default"] is False
