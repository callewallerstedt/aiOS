"""The production-shaped BENCH fixtures reject starters and accept references."""

from __future__ import annotations

import sys
import subprocess
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench import runner, suites  # noqa: E402


ARCHIVE_SOLUTION = {
    "backup/manifest.py": '''\
import json

from .errors import RestoreError


def load_manifest(payload):
    try:
        document = json.loads(payload)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise RestoreError(f"invalid manifest: {exc}") from exc
    if not isinstance(document, dict) or set(document) != {"version", "files"}:
        raise RestoreError("manifest must contain only version and files")
    if document["version"] != 1 or not isinstance(document["files"], list):
        raise RestoreError("unsupported manifest")
    return document
''',
    "backup/restore.py": '''\
import hashlib
import os
import re
import shutil
import stat
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

from .errors import RestoreError
from .manifest import load_manifest


def _path(value):
    if not isinstance(value, str) or not value or "\\0" in value or "\\\\" in value:
        raise RestoreError("invalid member path")
    if value.startswith(("/", "//")) or re.match(r"^[A-Za-z]:", value):
        raise RestoreError("absolute member path")
    parts = PurePosixPath(value).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise RestoreError("unsafe member path")
    normalized = PurePosixPath(*parts).as_posix()
    if normalized != value.rstrip("/"):
        raise RestoreError("non-canonical member path")
    return normalized


def _remove(path):
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def restore_backup(archive_path, destination, *, max_bytes=16 * 1024 * 1024):
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise RestoreError("max_bytes must be a non-negative integer")
    try:
        with ZipFile(archive_path) as archive:
            infos = archive.infolist()
            manifests = [info for info in infos if info.filename == "MANIFEST.json"]
            if len(manifests) != 1:
                raise RestoreError("archive needs one manifest")
            document = load_manifest(archive.read(manifests[0]))

            rows = {}
            order = []
            declared_total = 0
            for raw in document["files"]:
                if not isinstance(raw, dict) or set(raw) != {"path", "size", "sha256"}:
                    raise RestoreError("invalid file row")
                name = _path(raw["path"])
                key = name.casefold()
                size = raw["size"]
                digest = raw["sha256"]
                if key in rows or isinstance(size, bool) or not isinstance(size, int) or size < 0:
                    raise RestoreError("duplicate path or invalid size")
                if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
                    raise RestoreError("invalid digest")
                declared_total += size
                if declared_total > max_bytes:
                    raise RestoreError("archive is too large")
                rows[key] = (name, size, digest.casefold())
                order.append(name)

            members = {}
            member_total = 0
            seen_paths = set()
            for info in infos:
                if info.filename == "MANIFEST.json":
                    continue
                name = _path(info.filename)
                key = name.casefold()
                if key in seen_paths:
                    raise RestoreError("duplicate archive path")
                seen_paths.add(key)
                mode = (info.external_attr >> 16) & 0xFFFF
                kind = stat.S_IFMT(mode)
                if info.is_dir():
                    continue
                if kind not in {0, stat.S_IFREG}:
                    raise RestoreError("non-regular archive entry")
                members[key] = (name, info)
                member_total += info.file_size
                if member_total > max_bytes:
                    raise RestoreError("archive is too large")
            if set(rows) != set(members):
                raise RestoreError("manifest does not match archive")

            payloads = {}
            for key, (name, size, digest) in rows.items():
                member_name, info = members[key]
                if member_name != name or info.file_size != size:
                    raise RestoreError("member size or spelling mismatch")
                payload = archive.read(info)
                if len(payload) != size or hashlib.sha256(payload).hexdigest() != digest:
                    raise RestoreError("member digest mismatch")
                payloads[name] = payload
    except RestoreError:
        raise
    except (BadZipFile, KeyError, OSError, ValueError) as exc:
        raise RestoreError(f"invalid archive: {exc}") from exc

    target = Path(destination)
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.restore-", dir=parent))
    backup = parent / f".{target.name}.rollback-{uuid.uuid4().hex}"
    moved = False
    try:
        for name, payload in payloads.items():
            output = stage.joinpath(*PurePosixPath(name).parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(payload)
        if target.exists() or target.is_symlink():
            os.replace(target, backup)
            moved = True
        os.replace(stage, target)
        if moved:
            _remove(backup)
    except Exception as exc:
        try:
            if target.exists() or target.is_symlink():
                _remove(target)
            if moved and backup.exists():
                os.replace(backup, target)
        finally:
            _remove(stage)
        raise RestoreError(f"restore transaction failed: {exc}") from exc
    finally:
        _remove(stage)
        if backup.exists():
            _remove(backup)
    return sorted(order)
''',
}


STREAM_SOLUTION = {
    "telemetry/protocol.py": '''\
import json

from .errors import ProtocolError


class EventDecoder:
    def __init__(self, max_frame_bytes=65536):
        if isinstance(max_frame_bytes, bool) or not isinstance(max_frame_bytes, int) or max_frame_bytes < 1:
            raise ValueError("max_frame_bytes must be positive")
        self.max_frame_bytes = max_frame_bytes
        self.buffer = bytearray()
        self.frame_number = 0

    def _error(self, message):
        return ProtocolError(message, self.frame_number + 1)

    def feed(self, chunk):
        if not isinstance(chunk, bytes):
            raise self._error("chunk must be bytes")
        self.buffer.extend(chunk)
        result = []
        while True:
            try:
                boundary = self.buffer.index(10)
            except ValueError:
                break
            raw = bytes(self.buffer[:boundary])
            del self.buffer[:boundary + 1]
            if raw.endswith(b"\\r"):
                raw = raw[:-1]
            if not raw.strip(b" \\t\\r"):
                continue
            self.frame_number += 1
            if len(raw) > self.max_frame_bytes:
                raise ProtocolError("frame too large", self.frame_number)
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeError, ValueError) as exc:
                raise ProtocolError(f"invalid frame: {exc}", self.frame_number) from exc
            if not isinstance(value, dict):
                raise ProtocolError("frame must be an object", self.frame_number)
            result.append(value)
        if len(self.buffer) > self.max_frame_bytes:
            number = self.frame_number + 1
            self.buffer.clear()
            raise ProtocolError("frame too large", number)
        return result

    def finish(self):
        if self.buffer.strip(b" \\t\\r"):
            number = self.frame_number + 1
            self.buffer.clear()
            raise ProtocolError("unterminated frame", number)
        self.buffer.clear()
        return []

    def reset(self):
        self.buffer.clear()
        self.frame_number = 0
''',
    "telemetry/session.py": '''\
import copy
import threading

from .errors import ProtocolError


class TelemetrySession:
    def __init__(self):
        self.devices = {}
        self.sequences = {}
        self.last_events = {}
        self.lock = threading.RLock()

    @staticmethod
    def _validated(event):
        if not isinstance(event, dict) or set(event) != {"device", "seq", "kind", "data"}:
            raise ProtocolError("invalid event shape")
        device, seq, kind, data = event["device"], event["seq"], event["kind"], event["data"]
        if not isinstance(device, str) or not device or isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise ProtocolError("invalid device or sequence")
        if kind not in {"update", "reset"} or not isinstance(data, dict):
            raise ProtocolError("invalid kind or data")
        return device, seq, kind, copy.deepcopy(data)

    def apply(self, event):
        device, seq, kind, data = self._validated(event)
        canonical = {"device": device, "seq": seq, "kind": kind, "data": data}
        with self.lock:
            previous_event = self.last_events.get(device)
            if previous_event == canonical:
                return False
            previous = self.sequences.get(device)
            if kind == "reset":
                if seq != 0:
                    raise ProtocolError("reset sequence must be zero")
                next_state = data
            else:
                expected = 0 if previous is None else previous + 1
                if seq != expected:
                    if previous is not None and seq == previous:
                        raise ProtocolError("sequence was reused with different content")
                    raise ProtocolError("sequence gap")
                next_state = copy.deepcopy(self.devices.get(device, {}))
                next_state.update(data)
            self.devices[device] = next_state
            self.sequences[device] = seq
            self.last_events[device] = canonical
            return True

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.devices)
''',
}


@pytest.mark.parametrize("task", suites.select({"engineering": 99}), ids=lambda task: task.id)
def test_engineering_starters_fail_and_reference_solutions_pass(task, tmp_path):
    workspace = tmp_path / "work"
    task.build(workspace)

    visible_test = next(path for path in task.protected if path.startswith("tests/"))
    visible = subprocess.run(
        [sys.executable, "-m", "unittest", visible_test],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    assert visible.returncode == 0, visible.stdout + visible.stderr

    untouched = runner.verify(task, workspace, tmp_path / "verify-untouched")
    assert untouched["passed"] is False

    solution = ARCHIVE_SOLUTION if task.id.endswith("archive-restore") else STREAM_SOLUTION
    for relative, content in solution.items():
        (workspace / relative).write_text(content, encoding="utf-8")

    solved = runner.verify(task, workspace, tmp_path / "verify-solved")
    assert solved["passed"] is True, solved


def test_engineering_suite_is_exposed_and_bounded():
    entry = next(row for row in suites.suite_catalogue() if row["id"] == "engineering")
    assert entry["max"] == 2
    assert entry["official"] is False
    assert [task.id for task in suites.select({"engineering": 99})] == [
        "engineering/archive-restore",
        "engineering/telemetry-stream",
    ]


def test_telemetry_hidden_exception_contract_is_explicit_in_the_brief():
    task = next(
        task for task in suites.select({"engineering": 2})
        if task.id == "engineering/telemetry-stream"
    )
    assert "with `ProtocolError` (not `TypeError`)" in task.brief
    assert "every rejected decoder condition carries" in task.brief
    assert "next new update for that device must use seq 1" in task.brief
    assert "most recently committed event" in task.brief
    assert "retry rule applies to both updates and resets" in task.brief
