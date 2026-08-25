"""The right-hand agent chat in the WebView2 shell.

The panel is a view of the agent voice_dictation already runs -- the same one the
voice overlay and the phone talk to -- so these tests pin the two things that
would silently break that: the byte-cursor contract on the shared event log, and
the wiring that mounts the panel and reaches the agent's tools.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aios_ui import agent_api  # noqa: E402

WEB = ROOT / "aios_ui" / "web"


def write_log(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


@pytest.fixture
def log(tmp_path, monkeypatch):
    path = tmp_path / "phone_voice_events" / "events.jsonl"
    monkeypatch.setattr(agent_api, "EVENTS_PATH", path)
    return path


# ------------------------------------------------------------------- the log


def test_only_rendered_event_kinds_reach_the_panel(log):
    write_log(log, [
        {"ts": 1, "type": "turn_start", "text": "hello"},
        {"ts": 2, "type": "reply_start", "text": ""},
        {"ts": 3, "type": "reply_delta", "text": "hi"},
        # reply_done repeats the whole answer the deltas already carried; both
        # would render the reply twice.
        {"ts": 4, "type": "reply_done", "text": "hi"},
        {"ts": 5, "type": "turn_done", "text": "hi"},
    ])
    kinds = [event["type"] for event in agent_api.read_events(0)["events"]]
    assert kinds == ["turn_start", "reply_delta", "turn_done"]


def test_the_cursor_reads_forward_without_replaying(log):
    write_log(log, [{"ts": 1, "type": "turn_start", "text": "first"}])
    first = agent_api.read_events(0)
    assert [e["text"] for e in first["events"]] == ["first"]

    with log.open("a", encoding="utf-8") as file:
        file.write(json.dumps({"ts": 2, "type": "turn_done", "text": "second"}) + "\n")

    second = agent_api.read_events(first["size"])
    assert [e["text"] for e in second["events"]] == ["second"]
    assert second["size"] > first["size"]


def test_a_trimmed_log_is_a_reset_not_a_silent_skip(log):
    """voice_dictation trims the file when it grows; the reader must re-sync.

    Holding a cursor past EOF and reading nothing is how a panel goes quiet
    while the agent keeps answering.
    """
    write_log(log, [{"ts": 1, "type": "turn_start", "text": "kept"}])
    result = agent_api.read_events(999_999)
    assert result["reset"] is True
    assert [e["text"] for e in result["events"]] == ["kept"]


def test_a_missing_log_is_empty_not_an_error(log):
    result = agent_api.read_events(0)
    assert result["ok"] is True
    assert result["events"] == []
    assert result["running"] is False


def test_running_follows_the_last_lifecycle_event(log):
    import time

    write_log(log, [{"ts": time.time(), "type": "turn_start", "text": "working"}])
    assert agent_api.read_events(0)["running"] is True

    with log.open("a", encoding="utf-8") as file:
        file.write(json.dumps({"ts": time.time(), "type": "turn_done", "text": "done"}) + "\n")
    assert agent_api.read_events(0)["running"] is False


def test_an_abandoned_turn_does_not_pin_the_spinner_forever(log):
    write_log(log, [{"ts": 1, "type": "turn_start", "text": "ancient"}])
    assert agent_api.read_events(0)["running"] is False


# ------------------------------------------------------------------- routing


def test_blank_messages_never_reach_the_agent(monkeypatch):
    monkeypatch.setattr(agent_api, "ensure_voice_server", lambda: pytest.fail("should not spawn"))
    assert agent_api.send("   ")["ok"] is False


def test_send_forwards_the_ask_with_a_valid_reasoning_level(monkeypatch):
    sent = {}
    monkeypatch.setattr(agent_api, "ensure_voice_server", lambda: True)
    monkeypatch.setattr(agent_api, "_send", lambda payload, timeout=1.5: sent.update(payload) or {"ok": True})

    agent_api.send("do the thing", "high")
    assert sent["cmd"] == "ask"
    assert sent["text"] == "do the thing"
    assert sent["reasoning"] == "high"

    sent.clear()
    agent_api.send("do the thing", "turbo")
    assert "reasoning" not in sent  # an invalid level falls back to the agent's own


def test_dispatch_claims_only_its_own_routes(log):
    assert agent_api.dispatch("/api/agent/log", "GET", {"since": ["0"]}, {})["ok"] is True
    assert agent_api.dispatch("/api/code/jobs", "GET", {}, {}) is None
    assert agent_api.dispatch("/api/agent/log", "POST", {}, {}) is None


def test_the_server_exposes_the_agent_routes_and_stream():
    source = (ROOT / "aios_ui" / "server.py").read_text(encoding="utf-8")
    assert "/api/agent/" in source
    assert "/sse/agent/events" in source
    assert "def stream_agent" in source


# --------------------------------------------------------------------- the UI


def test_the_panel_is_mounted_outside_the_tab_lifecycle():
    """A tab switch must not tear down a conversation mid-answer."""
    app = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    assert "mountChat" in app
    assert "ChatPanel" in app
    assert "bindChatResize" in app
    # show() swaps only the page area; the chat must not be rebuilt there.
    page_swap = app.split("show(name)")[1]
    assert "ChatPanel" not in page_swap


def test_code_module_avoids_optional_chain_assignment_unsupported_by_webview():
    code = (WEB / "js" / "code.js").read_text(encoding="utf-8")
    assert "?.textContent =" not in code


def test_the_shell_loads_the_chat_stylesheet():
    assert 'href="css/chat.css"' in (WEB / "index.html").read_text(encoding="utf-8")


def test_the_panel_streams_and_never_depends_on_a_frame_arriving():
    chat = (WEB / "js" / "chat.js").read_text(encoding="utf-8")
    transcript = (WEB / "js" / "transcript.js").read_text(encoding="utf-8")
    assert "/sse/agent/events" in chat
    assert "reply_delta" in chat
    assert 'import { Transcript } from "./transcript.js"' in chat
    assert "this.fallback = setTimeout" in transcript
    # The echoed turn must not double the optimistic one.
    assert "pendingText" in chat


def test_the_panel_does_not_fight_the_users_scrolling():
    chat = (WEB / "js" / "chat.js").read_text(encoding="utf-8")
    transcript = (WEB / "js" / "transcript.js").read_text(encoding="utf-8")
    assert "new Transcript(this.log" in chat
    assert "scrollHeld" in transcript
    assert "scrollHoldUntil" in transcript
    assert "this.follow" in transcript


def test_sidebar_agent_model_and_reasoning_are_quick_controls():
    chat = (WEB / "js" / "chat.js").read_text(encoding="utf-8")
    shared = (WEB / "js" / "chat_components.js").read_text(encoding="utf-8")

    assert "promptShellMarkup" in chat and "promptConfigRowMarkup" in chat
    assert "promptShellMarkup" in (WEB / "js" / "code.js").read_text(encoding="utf-8")
    assert 'id="chat-model-btn"' in chat
    assert 'id="chat-model-list"' in chat and 'id="chat-reasoning-list"' in chat
    assert 'api("/api/settings/meta")' in chat
    assert 'api("/api/settings/voice"' in chat
    assert "agent_model" in chat and "agent_reasoning" in chat
    assert 'typeof raw === "string"' in chat
    assert 'class="prompt-config-row' in shared
    assert 'placeholder: "Write a message&hellip;"' in chat


def test_sidebar_tools_and_churning_use_the_real_code_components():
    chat = (WEB / "js" / "chat.js").read_text(encoding="utf-8")
    transcript = (WEB / "js" / "transcript.js").read_text(encoding="utf-8")
    assert "this.view.push([this.toolEvent" in chat
    assert "this.view.setWorking" in chat
    assert 'className = "working-sentinel live row-new"' in transcript
    assert 'class="working-pixels"' in transcript
    assert "loadingPixelsMarkup()" in transcript
    assert '/update_plan|\\bplan\\b/i.test(tool) ? "plan"' in chat
    assert "Array.isArray(event.steps) ? event.steps" in chat


def test_resident_agent_has_plan_search_and_openrouter_responses_support():
    agent = (ROOT / "voice_agent.py").read_text(encoding="utf-8")
    voice = (ROOT / "voice_dictation.py").read_text(encoding="utf-8")
    assert '"name": "update_plan"' in agent
    assert '"type": "openrouter:web_search"' in agent
    assert 'model.startswith("openrouter:")' in agent
    assert 'settings["api_base"] = openrouter_client.API_BASE' in agent
    assert 'extra["steps"]' in voice


def test_sidebar_inherits_the_exact_code_conversation_tokens_and_autosizing():
    beautiful = (WEB / "css" / "code-beautiful.css").read_text(encoding="utf-8")
    chat_css = (WEB / "css" / "chat.css").read_text(encoding="utf-8")
    chat = (WEB / "js" / "chat.js").read_text(encoding="utf-8")
    code = (WEB / "js" / "code.js").read_text(encoding="utf-8")
    shared = (WEB / "js" / "chat_components.js").read_text(encoding="utf-8")
    assert ":root {\n  --bui-canvas:" in beautiful
    assert "background: var(--bui-canvas)" in chat_css
    assert "background: var(--bui-surface)" in chat_css
    assert "autosizePromptShell" in shared
    assert "autosizePromptShell" in chat and "autosizePromptShell" in code


def test_agent_send_and_stream_read_use_the_same_director_or_local_route():
    server = (ROOT / "aios_ui" / "server.py").read_text(encoding="utf-8")
    stream_body = server.split("def stream_agent", 1)[1].split("def stream_reload", 1)[0]
    assert "agent_api.dispatch(" in stream_body
    assert '\"/api/agent/log\", \"GET\"' in stream_body
