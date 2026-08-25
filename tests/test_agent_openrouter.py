"""The voice agent on OpenRouter models (DeepSeek, Kimi).

OpenRouter speaks Chat Completions; the OpenAI path uses the Responses API. The
shapes differ in exactly the places that fail silently -- tool schemas, tool
results, and the id that pairs a result to its call -- so those are what these
tests pin. The tool set itself must stay whole: the point of choosing DeepSeek
is a cheaper agent, not a smaller one.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import voice_agent  # noqa: E402


@pytest.fixture
def agent(monkeypatch):
    settings = dict(voice_agent.DEFAULT_AGENT_SETTINGS)
    settings["agent_model"] = "openrouter:deepseek/deepseek-v4-flash"
    settings["api_key"] = ""
    monkeypatch.setattr(voice_agent, "agent_settings", lambda: dict(settings))
    instance = voice_agent.VoiceAgent(on_event=lambda *_: None)
    instance._settings = settings
    return instance


def test_an_openrouter_model_routes_off_the_responses_api(agent, monkeypatch):
    """`_run_locked` must never hand an OpenRouter model to the OpenAI client."""
    taken = {}
    monkeypatch.setattr(
        voice_agent.VoiceAgent, "_run_openrouter_locked",
        lambda self, transcript, settings, started: taken.setdefault("routed", True),
    )
    monkeypatch.setattr(
        voice_agent.VoiceAgent, "_ensure_client",
        lambda self, key: pytest.fail("the OpenAI client must not be built"),
    )
    agent._run_locked("hello")
    assert taken.get("routed") is True


def test_the_full_tool_set_survives_the_conversion(agent):
    tools = agent._openrouter_tools(agent._settings)
    names = {tool["function"]["name"] for tool in tools}
    # Every CODE tool, so the agent can still start and steer sessions and pick
    # provider, model and reasoning for them.
    assert {"code_start", "code_continue", "code_handoff", "code_capabilities"} <= names
    assert all(tool["type"] == "function" for tool in tools)
    # Chat Completions nests the schema under "function"; a flat name is the
    # Responses shape and OpenRouter ignores it.
    assert all("parameters" in tool["function"] for tool in tools)


def test_the_local_ollama_tool_set_stays_lean(agent):
    """The reduction is for local models only -- it must not leak here."""
    assert len(agent._openrouter_tools(agent._settings)) > len(agent._ollama_tools(agent._settings))


def test_local_models_keep_the_complete_code_controller(agent):
    code_names = {tool["name"] for tool in agent._code_tools()}
    local_names = {tool["function"]["name"] for tool in agent._ollama_tools(agent._settings)}
    assert code_names <= local_names
    assert {"code_projects", "code_configs", "code_continue", "code_handoff"} <= local_names


def test_a_tool_result_carries_the_call_id(agent, monkeypatch):
    """Chat Completions pairs results to calls by id; without it the round derails."""
    sent = []

    class FakeClient:
        DEFAULT_MODEL = "deepseek/deepseek-v4-flash"

        @staticmethod
        def provider_status():
            return True, "ready"

        @staticmethod
        def stream_chat(history, model, **kwargs):
            sent.append([dict(item) for item in history])
            if len(sent) == 1:
                yield {"done": True, "message": {
                    "role": "assistant", "content": "",
                    "tool_calls": [{
                        "id": "call_abc",
                        "type": "function",
                        "function": {"name": "system_status", "arguments": "{}"},
                    }],
                }}
            else:
                yield {"delta": {"content": "all good"}, "done": False}
                yield {"done": True, "message": {"role": "assistant", "content": "all good"}}

    monkeypatch.setitem(sys.modules, "openrouter_client", FakeClient)
    monkeypatch.setattr(voice_agent.VoiceAgent, "_execute", lambda self, name, args: "cpu 5%")

    result = agent._run_openrouter_locked("how is the pc", dict(agent._settings), 0.0)

    assert result.error == ""
    assert result.reply == "all good"
    assert result.tools == ["system_status"]
    tool_message = [m for m in sent[1] if m.get("role") == "tool"][0]
    assert tool_message["tool_call_id"] == "call_abc"
    assert "cpu 5%" in tool_message["content"]


def test_malformed_tool_arguments_do_not_kill_the_turn(agent, monkeypatch):
    captured = {}

    class FakeClient:
        DEFAULT_MODEL = "deepseek/deepseek-v4-flash"

        @staticmethod
        def provider_status():
            return True, "ready"

        @staticmethod
        def stream_chat(history, model, **kwargs):
            if not captured:
                captured["called"] = True
                yield {"done": True, "message": {
                    "role": "assistant", "content": "",
                    "tool_calls": [{"id": "c1", "function": {"name": "system_status", "arguments": "{not json"}}],
                }}
            else:
                yield {"done": True, "message": {"role": "assistant", "content": "done"}}

    monkeypatch.setitem(sys.modules, "openrouter_client", FakeClient)
    monkeypatch.setattr(
        voice_agent.VoiceAgent, "_execute",
        lambda self, name, args: captured.setdefault("args", args) or "ok",
    )

    result = agent._run_openrouter_locked("go", dict(agent._settings), 0.0)
    assert result.error == ""
    assert captured["args"] == {}


def test_a_dead_provider_is_reported_not_raised(agent, monkeypatch):
    class FakeClient:
        DEFAULT_MODEL = "x"

        @staticmethod
        def provider_status():
            return False, "OpenRouter API key is missing."

    monkeypatch.setitem(sys.modules, "openrouter_client", FakeClient)
    result = agent._run_openrouter_locked("hi", dict(agent._settings), 0.0)
    assert "key is missing" in result.error


def test_openrouter_models_are_offered_as_agent_models(monkeypatch):
    from aios_ui import settings_api

    fake = type("M", (), {"list_enabled_models": staticmethod(
        lambda: [{"id": "deepseek/deepseek-v4-flash", "description": "fast"}])})
    monkeypatch.setitem(sys.modules, "openrouter_client", fake)
    ids = [row["id"] for row in settings_api._openrouter_agent_models()]
    # The prefix is what routes the turn; a bare id would hit the OpenAI path.
    assert ids == ["openrouter:deepseek/deepseek-v4-flash"]


def test_warmup_needs_no_openai_key_for_openrouter(agent, monkeypatch):
    monkeypatch.setattr(
        voice_agent.VoiceAgent, "_ensure_client",
        lambda self, key: pytest.fail("no OpenAI key should be required"),
    )
    fake = type("M", (), {"provider_status": staticmethod(lambda: (True, "ready"))})
    monkeypatch.setitem(sys.modules, "openrouter_client", fake)
    assert agent.warmup() is True
