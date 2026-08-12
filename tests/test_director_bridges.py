"""Tests for the Windows side of Director: the sidebar link, the voice agent
stand-in, and the CODE bridge in director_client.

All offline — the network calls are stubbed, because what these need to get
right is the translation between Director's event log and the shapes the aiOS
desktop already renders."""
import json

import pytest


# ---------------- aios_ui.director_link ----------------

@pytest.fixture()
def link(tmp_path, monkeypatch):
    from aios_ui import director_link

    config = tmp_path / "helper_config.json"
    config.write_text(json.dumps({"director": {
        "enabled": True, "url": "https://example.invalid/director",
        "token": "t0ken", "agent_id": "agt_director"}}), encoding="utf-8")
    monkeypatch.setattr(director_link, "CONFIG_PATH", config)
    director_link._STATE.update({"thread_id": "", "agent_id": "", "checked": 0.0})
    return director_link


def test_link_is_off_without_configuration(tmp_path, monkeypatch):
    from aios_ui import director_link

    empty = tmp_path / "helper_config.json"
    empty.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(director_link, "CONFIG_PATH", empty)
    assert director_link.enabled() is False


def test_link_is_off_without_a_token(tmp_path, monkeypatch):
    from aios_ui import director_link

    config = tmp_path / "helper_config.json"
    config.write_text(json.dumps({"director": {"enabled": True,
                                               "url": "https://example.invalid"}}),
                      encoding="utf-8")
    monkeypatch.setattr(director_link, "CONFIG_PATH", config)
    assert director_link.enabled() is False


def test_link_enabled_when_configured(link):
    assert link.enabled() is True


def test_translate_covers_the_kinds_the_panel_renders(link):
    cases = [
        ({"kind": "message.user", "payload": {"text": "hi"}}, "turn_start", "hi"),
        ({"kind": "message.delta", "payload": {"text": "par"}}, "reply_delta", "par"),
        ({"kind": "message.assistant", "payload": {"text": "done"}}, "turn_done", "done"),
        ({"kind": "tool.start", "payload": {"name": "shell"}}, "tool_start", "shell…"),
    ]
    for event, kind, text in cases:
        got = link.translate(event)
        assert got["type"] == kind and got["text"] == text


def test_translate_marks_a_failed_tool_not_ok(link):
    got = link.translate({"kind": "tool.done", "payload": {
        "name": "shell", "card": {"title": "shell", "preview": "rm -rf", "tone": "danger"}}})
    assert got["type"] == "tool_done" and got["ok"] is False


def test_translate_reports_a_turn_error_as_a_finished_turn(link):
    """The panel clears its spinner on turn_done. An error that never produced
    one would leave the sidebar spinning forever."""
    got = link.translate({"kind": "thread.error", "payload": {"error": "boom"}})
    assert got["type"] == "turn_done" and got["error"] is True


def test_translate_ignores_kinds_with_no_home_in_the_panel(link):
    assert link.translate({"kind": "reasoning.delta", "payload": {"text": "…"}}) is None
    assert link.translate({"kind": "thread.status", "payload": {"status": "idle"}}) is None


def test_approval_and_question_surface_as_visible_rows(link):
    """The desktop panel cannot render cards yet, so these must at least appear
    — silently waiting on an approval the user cannot see is the bad case."""
    approval = link.translate({"kind": "approval", "payload": {"summary": "delete x"}})
    question = link.translate({"kind": "question", "payload": {"question": "which one?"}})
    assert "delete x" in approval["text"]
    assert "which one?" in question["text"]


def test_read_events_advances_the_cursor(link, monkeypatch):
    calls = {}

    def fake_request(path, payload=None, method=""):
        calls["path"] = path
        if "/thread" in path:
            return {"ok": True, "thread": {"id": "thr_1"}, "cursor": 0}
        return {"ok": True, "cursor": 42, "events": [
            {"id": 41, "kind": "message.user", "payload": {"text": "hi"}},
            {"id": 42, "kind": "message.assistant", "payload": {"text": "yo"}},
        ]}

    monkeypatch.setattr(link, "_request", fake_request)
    got = link.read_events(40)
    assert got["ok"] and got["size"] == 42
    assert [event["type"] for event in got["events"]] == ["turn_start", "turn_done"]


def test_read_events_survives_an_unreachable_director(link, monkeypatch):
    monkeypatch.setattr(link, "_request",
                        lambda *a, **k: {"ok": False, "error": "Director unreachable"})
    got = link.read_events(0)
    assert got["ok"] is False and got["events"] == []


def test_agent_api_falls_back_when_director_is_off(tmp_path, monkeypatch):
    """A misconfigured or unreachable Director must not take the sidebar down."""
    from aios_ui import agent_api, director_link

    monkeypatch.setattr(director_link, "enabled", lambda: False)
    monkeypatch.setattr(agent_api, "ensure_voice_server", lambda: False)
    got = agent_api.dispatch("/api/agent/send", "POST", {}, {"text": "hello"})
    assert got["ok"] is False and "voice agent" in got["error"]


# ---------------- director_voice ----------------

@pytest.fixture()
def voice(tmp_path, monkeypatch):
    import director_voice

    config = tmp_path / "helper_config.json"
    config.write_text(json.dumps({"director": {
        "enabled": True, "voice": True, "url": "https://example.invalid/director",
        "token": "t0ken", "agent_id": "agt_director"}}), encoding="utf-8")
    monkeypatch.setattr(director_voice, "CONFIG_PATH", config)
    monkeypatch.setattr(director_voice, "POLL_SECONDS", 0.01)
    return director_voice


def test_voice_off_when_voice_flag_is_false(voice, tmp_path, monkeypatch):
    config = tmp_path / "off.json"
    config.write_text(json.dumps({"director": {"enabled": True, "voice": False,
                                               "url": "u", "token": "t"}}), encoding="utf-8")
    monkeypatch.setattr(voice, "CONFIG_PATH", config)
    assert voice.enabled() is False


def test_voice_agent_returns_the_reply_and_tools(voice, monkeypatch):
    events = [
        {"id": 1, "kind": "thread.status", "payload": {"status": "running"}},
        {"id": 2, "kind": "tool.start", "payload": {"name": "shell"}},
        {"id": 3, "kind": "tool.done", "payload": {
            "name": "shell", "card": {"title": "shell", "preview": "uptime", "tone": "ok"}}},
        {"id": 4, "kind": "message.assistant", "payload": {"text": "load is low"}},
        {"id": 5, "kind": "thread.status", "payload": {"status": "idle"}},
    ]

    agent = voice.DirectorVoiceAgent()

    def fake_request(path, payload=None):
        if "/thread" in path:
            return {"ok": True, "thread": {"id": "thr_1"}, "cursor": 0}
        if "/messages" in path:
            return {"ok": True}
        return {"ok": True, "events": events}

    monkeypatch.setattr(agent, "_request", fake_request)
    result = agent.run("how busy is the box?")
    assert result.reply == "load is low"
    assert result.tools == ["shell"]
    assert result.error == ""
    assert ("user", "how busy is the box?") in agent.history()


def test_voice_agent_reports_an_unreachable_director_instead_of_hanging(voice, monkeypatch):
    agent = voice.DirectorVoiceAgent()
    monkeypatch.setattr(agent, "_request", lambda *a, **k: {"ok": False, "error": "nope"})
    result = agent.run("hello")
    assert result.reply == "" and "not reachable" in result.error


def test_voice_agent_exposes_the_surface_voice_dictation_calls():
    """voice_dictation calls run/cancel/history/history_before_current/clear on
    whatever _ensure_agent returns. A missing one is an AttributeError mid-turn."""
    import director_voice
    from voice_agent import VoiceAgent

    for name in ("run", "cancel", "history", "history_before_current", "clear"):
        assert hasattr(director_voice.DirectorVoiceAgent, name), name
        assert hasattr(VoiceAgent, name), name


# ---------------- director_client (the CODE bridge) ----------------

def test_client_config_requires_url_and_token(tmp_path, monkeypatch):
    import director_client

    config = tmp_path / "aios_director_client.json"
    config.write_text(json.dumps({"url": "https://example.invalid"}), encoding="utf-8")
    monkeypatch.setattr(director_client, "CONFIG_PATH", config)
    with pytest.raises(SystemExit):
        director_client.load_config()


def test_code_bridge_rejects_an_unknown_provider(monkeypatch):
    import director_client

    bridge = director_client.CodeBridge()

    class FakeJobs:
        PROVIDERS = ("codex", "claude")
        DEFAULT_MODELS = {"codex": "gpt-5.6-sol"}

    monkeypatch.setattr(bridge, "harness", lambda: FakeJobs)
    got = bridge.start({"task": "do a thing", "provider": "nonsense"})
    assert got["ok"] is False and "provider must be" in got["error"]


def test_code_bridge_passes_the_harness_defaults(tmp_path, monkeypatch):
    """create_job requires an explicit model and reasoning; omitting either is
    rejected by the harness, so the bridge must fill them in when no saved
    CODE configuration is available."""
    import director_client

    seen = {}

    class FakeJobs:
        PROVIDERS = ("codex",)
        DEFAULT_MODELS = {"codex": "gpt-5.6-sol"}

        @staticmethod
        def create_job(**kwargs):
            seen.update(kwargs)
            return {"id": "sess_1"}

    bridge = director_client.CodeBridge()
    monkeypatch.setattr(bridge, "harness", lambda: FakeJobs)
    monkeypatch.setattr(bridge, "list_configs", lambda: {
        "ok": True, "configs": [],
        "default_id": director_client.DEFAULT_CODE_CONFIG_ID,
        "default_name": director_client.DEFAULT_CODE_CONFIG_NAME,
    })
    got = bridge.start({"task": "fix the bug", "project": str(tmp_path)})
    assert got["ok"] and got["session_id"] == "sess_1"
    assert seen["model"] == "gpt-5.6-sol"
    assert seen["reasoning"] == "medium"
    assert seen["brief"] == "fix the bug"


def test_code_bridge_defaults_to_balanced_engineering(tmp_path, monkeypatch):
    import director_client

    seen = {}

    class FakeJobs:
        PROVIDERS = ("codex", "openrouter")
        DEFAULT_MODELS = {"codex": "gpt-5.6-sol", "openrouter": "fallback"}

        @staticmethod
        def create_job(**kwargs):
            seen.update(kwargs)
            return {"id": "sess_bal"}

    bridge = director_client.CodeBridge()
    monkeypatch.setattr(bridge, "harness", lambda: FakeJobs)
    monkeypatch.setattr(bridge, "list_configs", lambda: {
        "ok": True,
        "configs": [{
            "id": "harness-balanced-engineering",
            "name": "Balanced Engineering",
            "provider": "openrouter",
            "strategy": "auto",
            "review_fix": False,
            "roles": {
                "coder": {
                    "role": "coder", "enabled": True,
                    "model": "deepseek/deepseek-v4-flash-0731",
                    "reasoning": "low", "fast": True,
                },
            },
        }],
        "default_id": "harness-balanced-engineering",
        "default_name": "Balanced Engineering",
    })
    got = bridge.start({"task": "ship it", "project": str(tmp_path)})
    assert got["ok"] and got["config_id"] == "harness-balanced-engineering"
    assert seen["provider"] == "openrouter"
    assert seen["model"] == "deepseek/deepseek-v4-flash-0731"
    assert seen["reasoning"] == "low"
    assert seen["fast"] is True
    assert seen["config_name"] == "Balanced Engineering"


def test_code_bridge_honours_explicit_config_id(tmp_path, monkeypatch):
    import director_client

    seen = {}

    class FakeJobs:
        PROVIDERS = ("openrouter",)
        DEFAULT_MODELS = {"openrouter": "fallback"}

        @staticmethod
        def create_job(**kwargs):
            seen.update(kwargs)
            return {"id": "sess_cfg"}

    bridge = director_client.CodeBridge()
    monkeypatch.setattr(bridge, "harness", lambda: FakeJobs)
    monkeypatch.setattr(bridge, "list_configs", lambda: {
        "ok": True,
        "configs": [{
            "id": "fast-one",
            "name": "Fast",
            "provider": "openrouter",
            "strategy": "auto",
            "roles": {
                "coder": {
                    "role": "coder", "enabled": True,
                    "model": "deepseek/deepseek-v4-flash",
                    "reasoning": "off", "fast": True,
                },
            },
        }],
    })
    got = bridge.start({
        "task": "quick edit", "project": str(tmp_path),
        "config_id": "fast-one",
    })
    assert got["ok"] and got["config_id"] == "fast-one"
    assert seen["model"] == "deepseek/deepseek-v4-flash"
    assert seen["reasoning"] == "off"


def test_code_bridge_refuses_a_missing_project(monkeypatch):
    import director_client

    class FakeJobs:
        PROVIDERS = ("codex",)
        DEFAULT_MODELS = {"codex": "gpt-5.6-sol"}

    bridge = director_client.CodeBridge()
    monkeypatch.setattr(bridge, "harness", lambda: FakeJobs)
    got = bridge.start({"task": "x", "project": "Z:/definitely/not/here"})
    assert got["ok"] is False and "no such project" in got["error"]


def test_terminal_states_split_success_from_failure():
    import director_client

    assert "done" in director_client.TERMINAL_OK
    assert "failed" in director_client.TERMINAL_BAD
    assert not (director_client.TERMINAL_OK & director_client.TERMINAL_BAD)
