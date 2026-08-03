"""The phone PWA's channel to the resident voice agent.

Covers the mirror the PC writes and the endpoints the phone reads, without
needing a running agent, a microphone or a phone.
"""

import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agent_clicker"))
sys.path.insert(0, str(REPO / "agent_clicker" / "app"))

import voice_dictation

PHONE_JS = REPO / "phone_site" / "phone.js"
PHONE_HTML = REPO / "phone_site" / "index.html"
PHONE_CSS = REPO / "phone_site" / "phone.css"


@pytest.fixture
def mirror(monkeypatch, tmp_path):
    events = tmp_path / "events.jsonl"
    monkeypatch.setattr(voice_dictation, "PHONE_VOICE_DIR", tmp_path)
    monkeypatch.setattr(voice_dictation, "PHONE_VOICE_EVENTS", events)
    return events


def read(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ------------------------------------------------------------------- mirroring


def test_a_turn_is_mirrored_in_order(mirror):
    voice_dictation.mirror_phone_event("turn_start", "what is my cpu doing", {"source": "voice"})
    voice_dictation.mirror_phone_event("reply_start")
    voice_dictation.mirror_phone_event("reply_delta", "CPU is at ")
    voice_dictation.mirror_phone_event("reply_delta", "12 percent.")
    voice_dictation.mirror_phone_event("turn_done", "CPU is at 12 percent.", {"error": False})

    kinds = [event["type"] for event in read(mirror)]
    assert kinds == ["turn_start", "reply_start", "reply_delta", "reply_delta", "turn_done"]
    assert read(mirror)[0]["source"] == "voice"


def test_every_event_carries_a_timestamp(mirror):
    before = time.time()
    voice_dictation.mirror_phone_event("status", "thinking")
    event = read(mirror)[0]
    assert event["ts"] >= before


def test_the_mirror_trims_itself_instead_of_growing_without_bound(mirror, monkeypatch):
    monkeypatch.setattr(voice_dictation, "PHONE_VOICE_MAX_BYTES", 2000)
    for index in range(600):
        voice_dictation.mirror_phone_event("reply_delta", f"chunk {index} " + "x" * 40)
    assert mirror.stat().st_size < 200_000
    # Trimming keeps the newest events, which is what the phone needs.
    assert "599" in mirror.read_text(encoding="utf-8")


def test_mirroring_never_raises_when_the_directory_is_unwritable(monkeypatch, tmp_path):
    blocked = tmp_path / "nope" / "events.jsonl"
    monkeypatch.setattr(voice_dictation, "PHONE_VOICE_DIR", blocked.parent)
    monkeypatch.setattr(voice_dictation, "PHONE_VOICE_EVENTS", blocked)

    def refuse(*args, **kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(voice_dictation.Path, "mkdir", refuse)
    # A spoken turn must never fail because the phone mirror could not be written.
    voice_dictation.mirror_phone_event("turn_start", "hello")


# ------------------------------------------------------------------- endpoints


@pytest.fixture
def client(monkeypatch, tmp_path):
    import server

    events = tmp_path / "events.jsonl"
    monkeypatch.setattr(server, "VOICE_EVENTS_FILE", events)
    server.app.config["TESTING"] = True
    return server.app.test_client(), events, server


def test_the_log_returns_only_finished_turns(client):
    http, events, _server = client
    events.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"ts": 1, "type": "turn_start", "text": "hello"},
                {"ts": 2, "type": "reply_delta", "text": "hi "},
                {"ts": 3, "type": "reply_delta", "text": "there"},
                {"ts": 4, "type": "turn_done", "text": "hi there", "error": False},
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    response = http.get("/api/phone/voice/log")
    payload = response.get_json()
    assert response.status_code == 200
    assert [event["type"] for event in payload["events"]] == ["turn_start", "turn_done"]
    assert payload["size"] == events.stat().st_size


def test_the_log_is_empty_and_calm_before_any_turn(client):
    http, _events, _server = client
    payload = http.get("/api/phone/voice/log").get_json()
    assert payload["events"] == []
    assert payload["size"] == 0


def test_the_relay_can_resume_the_full_voice_stream_by_byte_cursor(client):
    http, events, _server = client
    first = json.dumps({"ts": 1, "type": "turn_start", "text": "hello"}) + "\n"
    second = json.dumps({"ts": 2, "type": "reply_delta", "text": "hi"}) + "\n"
    events.write_bytes((first + second).encode("utf-8"))

    payload = http.get(f"/api/phone/voice/log?since={len(first.encode('utf-8'))}").get_json()

    assert [event["type"] for event in payload["events"]] == ["reply_delta"]
    assert payload["reset"] is False
    assert payload["size"] == len((first + second).encode("utf-8"))


def test_send_routes_the_voice_target_to_the_agent_port(client, monkeypatch):
    http, _events, server = client
    sent = {}

    def fake_forward(payload):
        sent.update(payload)
        return {"ok": True, "sent": True}

    monkeypatch.setattr(server, "forward_voice", fake_forward)
    response = http.post("/api/phone/send", json={"target": "voice", "text": "set a timer for ten minutes"})
    assert response.status_code == 200
    assert sent == {
        "cmd": "ask",
        "text": "set a timer for ten minutes",
        "echo_user": True,
        "speak_reply": False,
    }


def test_phone_agent_is_a_one_turn_agent_override(client, monkeypatch):
    http, _events, server = client
    sent = {}
    monkeypatch.setattr(server, "forward_voice", lambda payload: sent.update(payload) or {"ok": True})

    response = http.post("/api/phone/send", json={
        "target": "agent",
        "text": "compare these options",
        "options": {"reasoning": "high", "speak_reply": False},
    })

    assert response.status_code == 200
    assert sent["reasoning"] == "high"
    assert sent["speak_reply"] is False


def test_send_reports_when_the_agent_is_not_running(client, monkeypatch):
    http, _events, server = client
    monkeypatch.setattr(
        server, "forward_voice", lambda payload: {"ok": False, "sent": False, "error": "not listening"}
    )
    response = http.post("/api/phone/send", json={"target": "voice", "text": "hello"})
    assert response.status_code == 503
    assert response.get_json()["ok"] is False


def test_other_targets_still_go_to_the_helper(client, monkeypatch):
    http, _events, server = client
    seen = {}
    monkeypatch.setattr(
        server,
        "forward_helper",
        lambda action, text="", options=None: seen.update({"action": action, "text": text}) or {"ok": True},
    )
    http.post("/api/phone/send", json={"target": "chat", "text": "hi"})
    assert seen["action"] == "chat"


def test_forward_voice_speaks_the_agent_protocol(monkeypatch):
    """It must send the exact JSON the dictation server's `ask` command expects."""
    import server

    received = []
    ready = threading.Event()

    def listener():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            monkeypatch.setattr(server, "VOICE_PORT", srv.getsockname()[1])
            ready.set()
            conn, _ = srv.accept()
            with conn:
                received.append(conn.recv(65536).decode("utf-8"))

    thread = threading.Thread(target=listener, daemon=True)
    thread.start()
    ready.wait(2)
    result = server.forward_voice({"cmd": "ask", "text": "hej", "echo_user": True})
    thread.join(timeout=2)

    assert result["ok"] is True
    assert json.loads(received[0]) == {"cmd": "ask", "text": "hej", "echo_user": True}


def test_stop_reports_cleanly_when_nothing_is_listening(client, monkeypatch):
    http, _events, server = client
    # Port 1 is never a live agent.
    monkeypatch.setattr(server, "VOICE_PORT", 1)
    response = http.post("/api/phone/voice/stop")
    assert response.status_code == 503
    assert response.get_json()["ok"] is False


def test_hosted_pwa_streams_agent_fragments_into_one_reply_row():
    source = PHONE_JS.read_text(encoding="utf-8")
    describe = source[source.index("function describe(event)"):source.index("function bubbleText(")]
    add_event = source[source.index("function addEvent(event)"):source.index("function currentThread(")]

    assert 'type === "agent_reply_delta"' in describe
    assert 'kind: "agent-stream"' in describe
    assert 'info.kind === "agent-stream"' in add_event
    assert '.entry.agent-reply.streaming' in add_event


def test_voice_mode_uses_a_short_lived_realtime_credential(client, monkeypatch):
    http, _events, server = client
    seen = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"value": "ek_test_short_lived", "expires_at": 123456}

    monkeypatch.setattr(server, "_load_helper_config", lambda: {"openai_api_key": "sk-test-private-key-long-enough"})
    monkeypatch.setattr(
        server.httpx,
        "post",
        lambda url, **kwargs: seen.update({"url": url, **kwargs}) or FakeResponse(),
    )

    response = http.post("/api/phone/realtime/token")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["value"] == "ek_test_short_lived"
    assert seen["url"].endswith("/v1/realtime/client_secrets")
    assert seen["headers"]["Authorization"].startswith("Bearer sk-test")
    realtime = seen["json"]["session"]
    assert realtime["model"] == "gpt-realtime-2.1"
    assert realtime["audio"]["output"]["voice"] == "marin"
    assert realtime["tools"][0]["name"] == "ask_aios_agent"
    assert "resident aiOS agent" in realtime["instructions"]


def test_voice_mode_refuses_to_expose_a_missing_api_key(client, monkeypatch):
    http, _events, server = client
    monkeypatch.setattr(server, "_load_helper_config", lambda: {})
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    response = http.post("/api/phone/realtime/token")

    assert response.status_code == 409
    assert "OpenAI API key" in response.get_json()["error"]


def test_pwa_has_one_agent_and_a_real_webrtc_voice_mode():
    html = PHONE_HTML.read_text(encoding="utf-8")
    js = PHONE_JS.read_text(encoding="utf-8")

    assert "Voice Mode" in html
    assert "Fast Agent" not in html
    assert "Think Agent" not in html
    assert "Control PC" not in html
    assert "new RTCPeerConnection()" in js
    assert "/v1/realtime/calls" in js
    assert 'sendCommand("realtime_token"' in js
    assert 'sendCommand("agent"' in js
    assert 'return "low"' in js


def test_shipped_phone_sources_are_strict_utf8_without_mojibake():
    bad = ("â", "Ã", "Â", "ð", "€")
    for path in (PHONE_HTML, PHONE_JS, PHONE_CSS):
        text = path.read_bytes().decode("utf-8", errors="strict")
        assert not any(marker in text for marker in bad), f"mojibake in {path.name}"
