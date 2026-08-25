"""Repository-shaped engineering scenarios for harness stress testing.

These are deliberately larger and more adversarial than the quick regression
fixtures.  Each repository has a public contract, a plausible incomplete
implementation, a small visible smoke test, and hidden checks outside the
agent workspace.  They model production work without pretending to be an
official leaderboard.
"""

from __future__ import annotations

from .suites import Task


_ARCHIVE_BRIEF = """\
Harden the backup restore path in this repository. The current implementation
uses `ZipFile.extractall`, trusts the manifest, and can partially overwrite a
working installation before it notices a bad archive.

Preserve the public API `backup.restore_backup(archive_path, destination,
*, max_bytes=16777216) -> list[str]` and the `RestoreError` exception.

The archive contract is:

* `MANIFEST.json` has `{"version": 1, "files": [...]}`. Every file row has
  exactly a relative POSIX `path`, integer `size`, and SHA-256 `sha256`.
* Every non-directory archive member except `MANIFEST.json` must occur exactly
  once in the manifest, and every manifest file must occur exactly once in the
  archive. Return the normalized paths sorted lexicographically.
* Reject absolute, drive-qualified, UNC, backslash-containing, empty, `.`, or
  `..` paths; NULs; case-insensitive duplicate paths; symlinks and other
  non-regular entries; malformed manifests; size/hash mismatches; and archives
  whose declared or actual total uncompressed bytes exceed `max_bytes`.
* Validate the complete archive before replacing anything. On any error the
  destination must remain byte-for-byte as it was. On success the destination
  is replaced by exactly the files in the manifest; `MANIFEST.json` is not
  restored. Temporary or rollback directories must be cleaned up.
* Use only the Python standard library and keep the CLI working on Windows.

Run `python -m unittest tests/test_restore.py` while you work. Do not edit the
visible test. Add focused tests if useful, but the implementation—not a test
workaround—is the task.
"""


_ARCHIVE_FILES = {
    "README.md": """\
# backup restore

`python restore_cli.py backup.zip restored` restores a manifest-backed backup.
The security and transaction contract is in the task brief.
""",
    "backup/__init__.py": '''\
from .errors import RestoreError
from .restore import restore_backup

__all__ = ["RestoreError", "restore_backup"]
''',
    "backup/errors.py": '''\
class RestoreError(ValueError):
    """The backup could not be validated or restored safely."""
''',
    "backup/manifest.py": '''\
"""Manifest parsing shared by the CLI and restore engine."""

import json

from .errors import RestoreError


def load_manifest(payload):
    try:
        return json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise RestoreError(f"invalid manifest: {exc}") from exc
''',
    "backup/restore.py": '''\
"""Restore a manifest-backed zip archive."""

from pathlib import Path
from zipfile import ZipFile

from .manifest import load_manifest


def restore_backup(archive_path, destination, *, max_bytes=16 * 1024 * 1024):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with ZipFile(archive_path) as archive:
        manifest = load_manifest(archive.read("MANIFEST.json"))
        archive.extractall(destination)
    return sorted(row["path"] for row in manifest["files"])
''',
    "restore_cli.py": '''\
import sys

from backup import RestoreError, restore_backup


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: restore_cli.py ARCHIVE DESTINATION", file=sys.stderr)
        return 2
    try:
        restored = restore_backup(args[0], args[1])
    except RestoreError as exc:
        print(f"restore failed: {exc}", file=sys.stderr)
        return 1
    for path in restored:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
''',
    "tests/test_restore.py": '''\
import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from backup import restore_backup


class RestoreSmokeTest(unittest.TestCase):
    def test_a_valid_archive_restores_a_nested_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / "backup.zip"
            payload = b'{"enabled": true}\\n'
            manifest = {"version": 1, "files": [{
                "path": "config/app.json",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }]}
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("MANIFEST.json", json.dumps(manifest))
                handle.writestr("config/app.json", payload)

            restored = restore_backup(archive, root / "restored")

            self.assertEqual(restored, ["config/app.json"])
            self.assertEqual((root / "restored/config/app.json").read_bytes(), payload)


if __name__ == "__main__":
    unittest.main()
''',
}


_ARCHIVE_CHECKS = r'''
import hashlib
import json
import stat
import tempfile
import zipfile
from pathlib import Path

from backup import RestoreError, restore_backup


def manifest_for(files):
    return {"version": 1, "files": [
        {"path": path, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        for path, payload in files
    ]}


def write_archive(path, files, *, manifest=None, extras=(), symlink=""):
    rows = list(files)
    document = manifest if manifest is not None else manifest_for(rows)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("MANIFEST.json", json.dumps(document))
        for name, payload in rows:
            archive.writestr(name, payload)
        for name, payload in extras:
            archive.writestr(name, payload)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "target")


def rejected(archive, destination, **kwargs):
    try:
        restore_backup(archive, destination, **kwargs)
    except RestoreError:
        return
    raise AssertionError("unsafe archive was accepted")


@case("valid archives replace the destination with exactly manifested files")
def _():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        target = root / "live"
        target.mkdir()
        (target / "obsolete.txt").write_text("old", encoding="utf-8")
        archive = root / "ok.zip"
        files = [("a.txt", b"a"), ("nested/b.bin", b"\x00\x01")]
        write_archive(archive, files)
        assert restore_backup(archive, target) == ["a.txt", "nested/b.bin"]
        assert (target / "a.txt").read_bytes() == b"a"
        assert (target / "nested/b.bin").read_bytes() == b"\x00\x01"
        assert not (target / "obsolete.txt").exists()
        assert not (target / "MANIFEST.json").exists()


@case("traversal, absolute, drive, UNC, and backslash paths are rejected")
def _():
    bad_names = ("../escape", "/absolute", "C:/drive", "//server/share", "a\\..\\escape")
    for index, name in enumerate(bad_names):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / f"bad-{index}.zip"
            write_archive(archive, [(name, b"bad")])
            rejected(archive, root / "live")
            assert not (root / "escape").exists()


@case("symlinks and case-insensitive duplicate paths are rejected")
def _():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        link_archive = root / "link.zip"
        write_archive(link_archive, [], symlink="link")
        rejected(link_archive, root / "one")

        duplicate = root / "duplicate.zip"
        files = [("Config/App.json", b"one"), ("config/app.json", b"two")]
        write_archive(duplicate, files)
        rejected(duplicate, root / "two")


@case("manifest and archive member sets must match exactly")
def _():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        extra = root / "extra.zip"
        write_archive(extra, [("one.txt", b"1")], extras=[("surprise.txt", b"2")])
        rejected(extra, root / "extra")

        missing = root / "missing.zip"
        document = manifest_for([("one.txt", b"1"), ("missing.txt", b"2")])
        write_archive(missing, [("one.txt", b"1")], manifest=document)
        rejected(missing, root / "missing")


@case("size, digest, version, and total-byte limits are enforced")
def _():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        for label, mutate in (
            ("size", lambda row: row.update(size=99)),
            ("hash", lambda row: row.update(sha256="0" * 64)),
        ):
            archive = root / f"{label}.zip"
            document = manifest_for([("data.bin", b"1234")])
            mutate(document["files"][0])
            write_archive(archive, [("data.bin", b"1234")], manifest=document)
            rejected(archive, root / label)
        version = root / "version.zip"
        document = manifest_for([("data.bin", b"1234")])
        document["version"] = 2
        write_archive(version, [("data.bin", b"1234")], manifest=document)
        rejected(version, root / "version")
        limited = root / "limited.zip"
        write_archive(limited, [("data.bin", b"1234")])
        rejected(limited, root / "limited", max_bytes=3)


@case("a late validation failure leaves an existing destination untouched")
def _():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        target = root / "live"
        target.mkdir()
        (target / "keep.txt").write_bytes(b"original")
        archive = root / "bad.zip"
        document = manifest_for([("first.txt", b"ok"), ("second.txt", b"bad")])
        document["files"][1]["sha256"] = "f" * 64
        write_archive(archive, [("first.txt", b"ok"), ("second.txt", b"bad")], manifest=document)
        rejected(archive, target)
        assert (target / "keep.txt").read_bytes() == b"original"
        assert sorted(path.name for path in target.iterdir()) == ["keep.txt"]
'''


_STREAM_BRIEF = """\
Finish the streaming telemetry protocol in this repository. Real devices split
UTF-8 and JSON at arbitrary byte boundaries, reconnect, retry events, and send
more than one frame per network read; the current code only handles a complete
ASCII line.

Preserve these public APIs:

* `EventDecoder(max_frame_bytes=65536).feed(chunk: bytes) -> list[dict]`
* `EventDecoder.finish() -> list[dict]` and `EventDecoder.reset() -> None`
* `TelemetrySession.apply(event: dict) -> bool` and `.snapshot() -> dict`
* `ProtocolError`, a `ValueError` with integer `frame_number`.

Decoder contract:

* Frames are UTF-8 JSON objects delimited by `\\n`; accept `\\r\\n`, ignore blank
  lines, and handle any fragmentation/coalescing without losing bytes.
* Count the raw bytes in one frame. Reject a frame as soon as it exceeds
  `max_frame_bytes`, malformed UTF-8/JSON, or a non-object JSON value. The
  exception identifies the one-based nonblank frame number. `reset()` fully
  recovers after an error.
* `finish()` processes a final newline-terminated frame normally, ignores only
  trailing ASCII whitespace, and rejects any other unterminated data.
* Reject non-bytes chunks with `ProtocolError` (not `TypeError`) rather than
  guessing an encoding, so every rejected decoder condition carries
  `frame_number`.

Session contract:

* Events have exactly a non-empty string `device`, non-negative integer `seq`
  (booleans are invalid), kind `update` or `reset`, and object `data`.
* An update merges `data` into that device's state. The first update must have
  seq 0; later updates must be exactly previous seq + 1. Only an exact retry of
  the most recently committed event returns `False`; any older sequence is
  rejected even when its payload matches. Reusing the latest sequence with
  different content or skipping a sequence raises `ProtocolError` without
  changing state. This retry rule applies to both updates and resets.
* A reset may arrive at any time but must use seq 0; it replaces device state
  and is itself the first committed event of the restarted sequence. Therefore
  the next new update for that device must use seq 1. `snapshot()` returns a
  deep copy safe for the caller to mutate. `apply` and `snapshot` must be
  thread-safe.

Run `python -m unittest tests/test_protocol.py` while you work. Do not edit the
visible test. Use only the Python standard library.
"""


_STREAM_FILES = {
    "README.md": """\
# telemetry protocol

Newline-delimited JSON events are decoded by `EventDecoder` and reduced into
per-device state by `TelemetrySession`. The full wire contract is in the task.
""",
    "telemetry/__init__.py": '''\
from .errors import ProtocolError
from .protocol import EventDecoder
from .session import TelemetrySession

__all__ = ["EventDecoder", "ProtocolError", "TelemetrySession"]
''',
    "telemetry/errors.py": '''\
class ProtocolError(ValueError):
    def __init__(self, message, frame_number=0):
        super().__init__(message)
        self.frame_number = int(frame_number)
''',
    "telemetry/protocol.py": '''\
"""Incremental newline-delimited JSON decoding."""

import json


class EventDecoder:
    def __init__(self, max_frame_bytes=65536):
        self.max_frame_bytes = max_frame_bytes
        self.buffer = b""

    def feed(self, chunk):
        # The production socket usually gives us one whole line. Tests do too.
        return [json.loads(chunk.decode("utf-8"))]

    def finish(self):
        return []

    def reset(self):
        self.buffer = b""
''',
    "telemetry/session.py": '''\
"""Reduce decoded events into the latest state per device."""


class TelemetrySession:
    def __init__(self):
        self.devices = {}

    def apply(self, event):
        self.devices[event["device"]] = dict(event["data"])
        return True

    def snapshot(self):
        return dict(self.devices)
''',
    "tests/test_protocol.py": '''\
import unittest

from telemetry import EventDecoder, TelemetrySession


class ProtocolSmokeTest(unittest.TestCase):
    def test_one_complete_event_updates_the_session(self):
        decoder = EventDecoder()
        events = decoder.feed(b'{"device":"car","seq":0,"kind":"update","data":{"speed":7}}\\n')
        session = TelemetrySession()
        self.assertTrue(session.apply(events[0]))
        self.assertEqual(session.snapshot(), {"car": {"speed": 7}})


if __name__ == "__main__":
    unittest.main()
''',
}


_STREAM_CHECKS = r'''
import json

from telemetry import EventDecoder, ProtocolError, TelemetrySession


def encoded(event):
    return json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def update(device="car", seq=0, data=None, kind="update"):
    return {"device": device, "seq": seq, "kind": kind, "data": dict(data or {})}


@case("every byte boundary including split UTF-8 decodes exactly once")
def _():
    event = update(data={"city": "G\u00f6teborg", "speed": 7})
    payload = encoded(event)
    for split in range(len(payload) + 1):
        decoder = EventDecoder()
        got = decoder.feed(payload[:split]) + decoder.feed(payload[split:]) + decoder.finish()
        assert got == [event], (split, got)


@case("coalesced CRLF frames and blank lines retain their order")
def _():
    first = update("a", 0, {"x": 1})
    second = update("b", 0, {"y": 2})
    payload = b"\r\n" + encoded(first).replace(b"\n", b"\r\n") + b"  \n" + encoded(second)
    decoder = EventDecoder()
    assert decoder.feed(payload) == [first, second]
    assert decoder.finish() == []


@case("malformed, non-object, oversized, and non-bytes frames fail closed")
def _():
    bad = (
        (EventDecoder(), b"{bad}\n"),
        (EventDecoder(), b"[1,2]\n"),
        (EventDecoder(max_frame_bytes=3), b"1234"),
        (EventDecoder(), "not bytes"),
    )
    for decoder, payload in bad:
        try:
            decoder.feed(payload)
        except ProtocolError as exc:
            assert exc.frame_number >= 1
        else:
            raise AssertionError(f"bad payload was accepted: {payload!r}")


@case("finish rejects unterminated data and reset recovers completely")
def _():
    decoder = EventDecoder()
    decoder.feed(b'{"device":"car"')
    try:
        decoder.finish()
    except ProtocolError as exc:
        assert exc.frame_number == 1
    else:
        raise AssertionError("unterminated data was accepted")
    decoder.reset()
    event = update(data={"ok": True})
    assert decoder.feed(encoded(event)) == [event]
    assert decoder.finish() == []


@case("session ordering, retries, gaps, and sequence reuse are strict")
def _():
    session = TelemetrySession()
    zero = update(data={"speed": 1})
    one = update(seq=1, data={"rpm": 2})
    assert session.apply(zero) is True
    assert session.apply(zero) is False
    assert session.apply(one) is True
    assert session.snapshot() == {"car": {"speed": 1, "rpm": 2}}
    for bad in (
        update(seq=1, data={"rpm": 3}),
        update(seq=3, data={"x": 1}),
        zero,
    ):
        try:
            session.apply(bad)
        except ProtocolError:
            pass
        else:
            raise AssertionError(f"out-of-order event was accepted: {bad}")
    assert session.snapshot() == {"car": {"speed": 1, "rpm": 2}}


@case("reset restarts one device and snapshots are deep copies")
def _():
    session = TelemetrySession()
    session.apply(update("a", 0, {"nested": {"value": 1}}))
    session.apply(update("b", 0, {"value": 2}))
    reset = update("a", 0, {"fresh": True}, kind="reset")
    assert session.apply(reset) is True
    assert session.apply(reset) is False
    session.apply(update("a", 1, {"next": 3}))
    snapshot = session.snapshot()
    assert snapshot == {"a": {"fresh": True, "next": 3}, "b": {"value": 2}}
    snapshot["a"]["next"] = 99
    assert session.snapshot()["a"]["next"] == 3


@case("invalid event shapes never mutate session state")
def _():
    invalid = (
        {},
        update(device="", seq=0),
        update(seq=True),
        update(seq=-1),
        update(kind="other"),
        {"device": "a", "seq": 1, "kind": "update", "data": {}, "extra": 1},
    )
    session = TelemetrySession()
    assert session.apply(update("stable", 0, {"value": 7})) is True
    baseline = session.snapshot()
    for event in invalid:
        try:
            session.apply(event)
        except ProtocolError:
            pass
        else:
            raise AssertionError(f"invalid event was accepted: {event!r}")
        assert session.snapshot() == baseline
'''


ENGINEERING_FIXTURES: tuple[Task, ...] = (
    Task(
        id="engineering/archive-restore",
        suite="engineering",
        title="Transactional, traversal-safe backup restore",
        brief=_ARCHIVE_BRIEF,
        files=_ARCHIVE_FILES,
        checks=_ARCHIVE_CHECKS,
        protected=("tests/test_restore.py",),
    ),
    Task(
        id="engineering/telemetry-stream",
        suite="engineering",
        title="Fragmented telemetry with retries and resets",
        brief=_STREAM_BRIEF,
        files=_STREAM_FILES,
        checks=_STREAM_CHECKS,
        protected=("tests/test_protocol.py",),
    ),
)


__all__ = ["ENGINEERING_FIXTURES"]
