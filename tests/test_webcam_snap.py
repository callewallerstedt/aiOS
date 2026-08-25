"""Webcam snap Quick Tool — decode + clipboard/paste dispatch."""

from __future__ import annotations

import base64
import sys
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aios_ui import settings_api, webcam_snap  # noqa: E402


def _png_data_url(width: int = 40, height: int = 30, color=(12, 34, 56)) -> str:
    image = Image.new("RGB", (width, height), color)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def test_decode_image_data_url_returns_rgb():
    image = webcam_snap.decode_image_data_url(_png_data_url(16, 12))
    assert image.size == (16, 12)
    assert image.mode == "RGB"


def test_decode_rejects_non_image_payload():
    with pytest.raises(ValueError):
        webcam_snap.decode_image_data_url("not-a-data-url")


def test_orient_flips_upside_down():
    image = Image.new("RGB", (4, 2), (0, 0, 0))
    image.putpixel((0, 0), (255, 0, 0))
    flipped = webcam_snap._orient(image)
    assert flipped.size == (4, 2)
    assert flipped.getpixel((3, 1)) == (255, 0, 0)
    assert flipped.getpixel((0, 0)) == (0, 0, 0)


def test_preferred_device_picks_lenovo_rgb():
    names = [
        "Rift S Sensor",
        "Lenovo 500 RGB Camera",
        "Meta Quest 3",
        "Lenovo 500 IR Camera",
    ]
    assert webcam_snap.preferred_device_index(names) == 1


def test_preferred_device_skips_virtual_when_no_lenovo():
    names = ["Meta Quest 3", "USB Camera", "Rift S Sensor"]
    assert webcam_snap.preferred_device_index(names) == 1


def test_handle_copy_puts_image_on_clipboard(monkeypatch):
    seen = {}

    def fake_copy(image):
        seen["size"] = image.size
        seen["mode"] = image.mode

    monkeypatch.setattr(webcam_snap, "copy_image_to_clipboard", fake_copy)
    result = webcam_snap.handle({"action": "copy", "image": _png_data_url(22, 18)})
    assert result["ok"] is True
    assert result["width"] == 22
    assert result["height"] == 18
    assert seen == {"size": (22, 18), "mode": "RGB"}


def test_handle_copy_without_image_uses_camera(monkeypatch):
    seen = {}
    fake_image = Image.new("RGB", (10, 8), (1, 2, 3))

    class FakeSession:
        active = True
        _name = "Lenovo 500 RGB Camera"

        def start(self, device=0):
            return None

        def capture_image(self):
            return fake_image

        def _opencv_image(self):
            return fake_image

    monkeypatch.setattr(webcam_snap, "camera", lambda: FakeSession())
    monkeypatch.setattr(
        webcam_snap,
        "copy_image_to_clipboard",
        lambda image: seen.update(size=image.size),
    )
    result = webcam_snap.handle({"action": "copy", "fast": True})
    assert result["ok"] is True
    assert seen["size"] == (10, 8)


def test_instant_snap_uses_ready_dib(monkeypatch):
    seen = {}

    class FakeSession:
        active = True
        _name = "Lenovo 500 RGB Camera"

        def start(self, device="auto"):
            return None

        def take_ready_dib(self, wait_s: float = 0.75):
            return b"DIBDATA"

        def touch(self):
            return None

    monkeypatch.setattr(webcam_snap, "camera", lambda: FakeSession())
    monkeypatch.setattr(
        webcam_snap,
        "copy_dib_to_clipboard",
        lambda dib: seen.update(dib=dib),
    )
    result = webcam_snap.instant_snap_to_clipboard()
    assert result["ok"] is True
    assert seen == {"dib": b"DIBDATA"}


def test_bgr_frame_to_dib_has_bitmap_header():
    import numpy as np

    frame = np.zeros((4, 6, 3), dtype=np.uint8)
    frame[0, 0] = (1, 2, 3)
    dib = webcam_snap.bgr_frame_to_dib(frame)
    assert dib[:4] == (40).to_bytes(4, "little")
    assert len(dib) > 40


def test_handle_paste_sends_ctrl_v(monkeypatch):
    calls = []
    monkeypatch.setattr(webcam_snap, "paste_clipboard", lambda: calls.append("paste"))
    monkeypatch.setattr(webcam_snap.time, "sleep", lambda _seconds: None)
    result = webcam_snap.handle({"action": "paste"})
    assert result["ok"] is True
    assert calls == ["paste"]


def test_handle_start_and_stop(monkeypatch):
    calls = []

    class FakeSession:
        _index = 1
        _name = "Lenovo 500 RGB Camera"
        active = False

        def list_devices(self):
            return [
                {"id": "auto", "label": "Auto"},
                {"id": "1", "label": "Lenovo 500 RGB Camera"},
            ]

        def start(self, device="auto"):
            calls.append(("start", str(device)))

        def stop(self):
            calls.append(("stop",))

    monkeypatch.setattr(webcam_snap, "camera", lambda: FakeSession())
    started = webcam_snap.handle({"action": "start", "device": "auto"})
    stopped = webcam_snap.handle({"action": "stop"})
    assert started["ok"] is True
    assert started["name"] == "Lenovo 500 RGB Camera"
    assert stopped["ok"] is True
    assert calls == [("start", "auto"), ("stop",)]


def test_run_tool_routes_webcam_snap(monkeypatch):
    monkeypatch.setattr(webcam_snap, "handle", lambda data: {"ok": True, "echo": data})
    result = settings_api.run_tool("webcam_snap", {"action": "paste"})
    assert result == {"ok": True, "echo": {"action": "paste"}}
