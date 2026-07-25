import json

import pytest

import phone_relay


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    monkeypatch.setattr(phone_relay, "STATE_PATH", tmp_path / "phone-relay-state.json")
    monkeypatch.setattr(phone_relay, "LOCAL_EVENTS_PATH", tmp_path / "events.jsonl")
    bridge = phone_relay.Bridge({
        "url": "https://relay.example",
        "machine_id": "machine-1",
        "machine_token": "token-1",
    })
    uploaded = []
    monkeypatch.setattr(bridge.shot_uploads, "submit", lambda fn: uploaded.append(fn))
    bridge.uploaded = uploaded
    return bridge


def feed(bridge, monkeypatch, events):
    monkeypatch.setattr(
        bridge, "local_json",
        lambda path, **kwargs: {"ok": True, "events": events, "size": 10, "reset": False},
    )
    return bridge.collect_events()


def test_step_screenshot_is_published_for_the_phone(bridge, monkeypatch):
    events = feed(bridge, monkeypatch, [
        {"type": "run_start", "ts": 1.0, "task": "open mail"},
        {"type": "screenshot", "ts": 2.0, "frame": 1, "n": 1, "width": 2560, "height": 1440},
    ])

    shot = events[1]["payload"]["shot"]
    assert shot == "shot-1-1"
    assert len(bridge.uploaded) == 1


def test_a_click_reuses_the_screenshot_it_was_decided_from(bridge, monkeypatch):
    events = feed(bridge, monkeypatch, [
        {"type": "screenshot", "ts": 2.0, "frame": 4, "n": 2},
        {"type": "click_fx", "ts": 2.5, "frame": 4, "x": 120, "y": 340, "button": "left"},
    ])

    assert events[0]["payload"]["shot"] == events[1]["payload"]["shot"]
    assert len(bridge.uploaded) == 1, "the same frame must not upload twice"


def test_a_new_run_cannot_overwrite_the_previous_run_screenshots(bridge, monkeypatch):
    first = feed(bridge, monkeypatch, [
        {"type": "run_start", "ts": 1.0},
        {"type": "screenshot", "ts": 2.0, "frame": 1},
    ])
    second = feed(bridge, monkeypatch, [
        {"type": "run_start", "ts": 3.0},
        {"type": "screenshot", "ts": 4.0, "frame": 1},
    ])

    assert first[1]["payload"]["shot"] != second[1]["payload"]["shot"]


def test_the_rotating_slot_survives_a_bridge_restart(bridge, monkeypatch):
    feed(bridge, monkeypatch, [{"type": "run_start", "ts": 1.0}])
    saved = json.loads(phone_relay.STATE_PATH.read_text(encoding="utf-8"))

    assert saved["machines"]["machine-1"]["shot_slot"] == bridge.shot_slot
    assert phone_relay.Bridge({
        "url": "https://relay.example",
        "machine_id": "machine-1",
        "machine_token": "token-1",
    }).shot_slot == bridge.shot_slot


def test_events_without_a_frame_are_untouched(bridge, monkeypatch):
    events = feed(bridge, monkeypatch, [{"type": "thought", "ts": 1.0, "thought": "looking"}])

    assert "shot" not in events[0]["payload"]
    assert bridge.uploaded == []
