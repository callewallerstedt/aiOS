"""The phone went quiet when the bridge advanced its log cursor before a
successful upload. These tests lock that contract: collect is read-only for the
durable cursor; only commit_event_cursor moves it, and only after a post."""

import pytest

import phone_relay


@pytest.fixture
def bridge_events(tmp_path, monkeypatch):
    monkeypatch.setattr(phone_relay, "STATE_PATH", tmp_path / "phone-relay-state.json")
    monkeypatch.setattr(phone_relay, "LOCAL_EVENTS_PATH", tmp_path / "events.jsonl")
    monkeypatch.setattr(phone_relay, "LOCAL_VOICE_EVENTS_PATH", tmp_path / "voice-events.jsonl")
    bridge = phone_relay.Bridge({
        "url": "https://relay.example",
        "machine_id": "machine-1",
        "machine_token": "token-1",
    })
    events = [
        {"type": "thought", "ts": 1.0, "thought": "looking"},
        {"type": "step_begin", "ts": 1.5, "n": 2},
    ]
    monkeypatch.setattr(
        bridge, "local_json",
        lambda path, **kwargs: {"ok": True, "events": events, "size": 42, "reset": False},
    )
    return bridge, events


def test_collect_events_does_not_advance_the_durable_cursor(bridge_events):
    bridge, events = bridge_events
    assert bridge.log_cursor == 0

    out = bridge.collect_events()

    assert [item["type"] for item in out] == ["thought", "step_begin"]
    assert bridge.log_cursor == 0, "cursor must stay put until the relay ack"
    assert bridge._events_next_cursor == 42


def test_commit_only_after_a_successful_flush(bridge_events, monkeypatch):
    bridge, events = bridge_events
    posts = []

    def ok_post(**kwargs):
        posts.append(kwargs)

    monkeypatch.setattr(bridge, "post_events", ok_post)
    monkeypatch.setattr(bridge, "collect_status", lambda: {"state": "running"})

    bridge.flush_events(force_status=True)

    assert bridge.log_cursor == 42
    assert posts and posts[0]["events"][0]["type"] == "thought"


def test_a_failed_post_leaves_the_cursor_so_events_retry(bridge_events, monkeypatch):
    bridge, events = bridge_events

    def boom(**kwargs):
        raise RuntimeError("relay blip")

    monkeypatch.setattr(bridge, "post_events", boom)
    monkeypatch.setattr(bridge, "collect_status", lambda: None)

    try:
        bridge.flush_events()
    except RuntimeError:
        pass

    assert bridge.log_cursor == 0, "a failed upload must not skip this slice"


def test_clarify_runs_off_the_event_pump(bridge_events, monkeypatch):
    bridge, _ = bridge_events
    started = []

    monkeypatch.setattr(
        bridge, "remote_json",
        lambda path, **kwargs: {"commands": [{
            "id": 7, "type": "clarify",
            "payload": {"request_id": "r1", "draft": "open the browser please"},
        }]} if path.endswith("/commands") else {},
    )
    monkeypatch.setattr(bridge, "flush_events", lambda **kwargs: started.append("flush"))
    monkeypatch.setattr(bridge, "reload_pairing", lambda: False)
    monkeypatch.setattr(bridge, "refresh_monitors", lambda: None)

    def capture_async(command, *, is_clarify):
        started.append(("async", command.get("id"), is_clarify))

    monkeypatch.setattr(bridge, "_run_command_async", capture_async)
    monkeypatch.setattr(bridge, "_complete_command", lambda *a, **k: started.append("sync"))

    bridge.tick()

    assert ("async", 7, True) in started
    assert "sync" not in started, "clarify must not block the tick on a model call"
    assert started[0] == "flush", "operator activity is pushed before commands"
