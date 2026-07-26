"""Files the phone attaches must reach OPERATOR before the run starts."""

import base64
import json

import pytest

import phone_relay


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    monkeypatch.setattr(phone_relay, "STATE_PATH", tmp_path / "phone-relay-state.json")
    monkeypatch.setattr(phone_relay, "LOCAL_EVENTS_PATH", tmp_path / "events.jsonl")
    return phone_relay.Bridge({
        "url": "https://relay.example",
        "machine_id": "machine-1",
        "machine_token": "token-1",
    })


@pytest.fixture
def wired(bridge, monkeypatch):
    """Record every relay download/delete and the local multipart upload."""
    calls = {"fetched": [], "deleted": [], "uploaded": [], "sent": []}

    def fake_request_bytes(url, *, method="GET", data=None, headers=None, timeout=12):
        assert headers.get("X-aiOS-Machine-Token") == "token-1"
        if method == "DELETE":
            calls["deleted"].append(url)
            return b""
        calls["fetched"].append(url)
        return b"\x89PNG-bytes"

    def fake_post(url, *, files, timeout):
        calls["uploaded"].append((url, files))
        return FakeResponse({"ok": True, "attachments": [
            {"id": f"local{index}", "name": name} for index, (_, (name, _, _)) in enumerate(files)
        ]})

    monkeypatch.setattr(phone_relay, "request_bytes", fake_request_bytes)
    monkeypatch.setattr(phone_relay.httpx, "post", fake_post)
    monkeypatch.setattr(
        bridge, "local_json",
        lambda path, **kwargs: calls["sent"].append((path, kwargs.get("payload"))) or {"ok": True},
    )
    bridge.calls = calls
    return bridge


def test_relay_hosted_files_are_downloaded_then_released(wired):
    ids = wired.fetch_attachments([{"key": "abc123", "name": "statement.jpg", "type": "image/jpeg"}])

    assert ids == ["local0"]
    assert wired.calls["fetched"] == ["https://relay.example/api/agent/uploads/abc123"]
    url, files = wired.calls["uploaded"][0]
    assert url.endswith("/api/phone/operator/upload")
    assert files[0] == ("files", ("statement.jpg", b"\x89PNG-bytes", "image/jpeg"))
    # The relay is a courier, not a store: drop the copy once it landed.
    assert wired.calls["deleted"] == ["https://relay.example/api/agent/uploads/abc123"]


def test_inline_files_need_no_relay_round_trip(wired):
    payload = base64.b64encode(b"hello receipts").decode("ascii")

    ids = wired.fetch_attachments([{"name": "notes.txt", "type": "text/plain", "data": payload}])

    assert ids == ["local0"]
    assert wired.calls["fetched"] == []
    assert wired.calls["uploaded"][0][1][0][1][1] == b"hello receipts"


def test_attachment_ids_cannot_escape_their_folder(wired):
    wired.fetch_attachments([{"key": "../../frames/other", "name": "x.png"}])

    assert wired.calls["fetched"] == ["https://relay.example/api/agent/uploads/framesother"]


def test_a_file_name_cannot_walk_the_disk(wired):
    wired.fetch_attachments([{"key": "abc", "name": "../../evil.png"}])

    assert wired.calls["uploaded"][0][1][0][1][0] == "evil.png"


def test_only_a_handful_of_files_travel_per_message(wired):
    items = [{"key": f"k{index}", "name": f"{index}.png"} for index in range(12)]

    ids = wired.fetch_attachments(items)

    assert len(ids) == phone_relay.MAX_ATTACHMENTS


def test_an_oversized_file_stops_the_run_instead_of_starting_it_blind(bridge, monkeypatch):
    monkeypatch.setattr(
        phone_relay, "request_bytes",
        lambda *args, **kwargs: b"x" * (phone_relay.MAX_ATTACHMENT_BYTES + 1),
    )

    with pytest.raises(RuntimeError, match="15 MB"):
        bridge.fetch_attachments([{"key": "abc", "name": "huge.png"}])


def test_a_prompt_carries_its_attachment_ids_to_aios(wired):
    wired.execute({"type": "prompt", "payload": {
        "prompt": "find the receipts in this",
        "model": "luna",
        "attachments": [{"key": "abc123", "name": "bank.jpg", "type": "image/jpeg"}],
    }})

    path, payload = wired.calls["sent"][0]
    assert path == "/api/phone/send"
    assert payload["text"] == "find the receipts in this"
    assert payload["intent"] == "new"
    assert payload["attachments"] == ["local0"]


def test_a_plain_prompt_still_sends_no_attachment_key(wired):
    wired.execute({"type": "prompt", "payload": {"prompt": "open my email"}})

    _, payload = wired.calls["sent"][0]
    assert "attachments" not in payload
    assert wired.calls["uploaded"] == []


def test_a_follow_up_keeps_its_files(wired):
    wired.execute({"type": "followup", "payload": {
        "prompt": "and this one too",
        "attachments": [{"key": "def456", "name": "receipt.jpg"}],
    }})

    _, payload = wired.calls["sent"][0]
    assert payload["intent"] == "followup"
    assert payload["attachments"] == ["local0"]
