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


def test_send_routes_the_voice_target_to_the_agent_port(client, monkeypatch):
    http, _events, server = client
    sent = {}

    def fake_forward(payload):
        sent.update(payload)
        return {"ok": True, "sent": True}

    monkeypatch.setattr(server, "forward_voice", fake_forward)
    response = http.post("/api/phone/send", json={"target": "voice", "text": "set a timer for ten minutes"})
    assert response.status_code == 200
    assert sent == {"cmd": "ask", "text": "set a timer for ten minutes", "echo_user": True}


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
