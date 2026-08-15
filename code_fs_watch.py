"""Bounded filesystem change tracking for shell tools on large Windows trees.

Git repositories already provide a cheap authoritative change index.  A
non-git project used to require two recursive ``os.walk`` scans around every
shell command, which is catastrophically slow for game installs and other
large folders.  Windows exposes the changes directly through
``ReadDirectoryChangesW``; this module keeps that platform detail outside the
agent loop and never installs a dependency at runtime.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import os
from pathlib import Path
import threading
import time
from typing import Any


_FILE_LIST_DIRECTORY = 0x0001
_FILE_SHARE_ALL = 0x0001 | 0x0002 | 0x0004
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_NOTIFY_FILTER = 0x0001 | 0x0008 | 0x0010 | 0x0040
_ERROR_OPERATION_ABORTED = 995
_ACTION_NAMES = {
    1: "added",
    2: "removed",
    3: "modified",
    4: "renamed_from",
    5: "renamed_to",
}


@dataclass(frozen=True)
class ChangeRecord:
    relative_path: str
    actions: tuple[str, ...]
    previous_revision: str | None


class WindowsDirectoryTracker:
    """Collect changed file names recursively without walking the tree."""

    def __init__(self, root: str | Path, *, max_paths: int = 20_000) -> None:
        self.root = Path(root).resolve()
        self.max_paths = max(1, min(int(max_paths or 20_000), 100_000))
        self._handle: Any = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._events: dict[str, dict[str, Any]] = {}
        self._overflow = False
        self._error = ""
        self._started_at = 0.0
        self._stopped = False

    @property
    def available(self) -> bool:
        return self._handle is not None and not self._error

    def start(self) -> bool:
        if os.name != "nt" or not self.root.is_dir():
            return False
        try:
            import win32file  # type: ignore[import-not-found]

            self._handle = win32file.CreateFile(
                str(self.root),
                _FILE_LIST_DIRECTORY,
                _FILE_SHARE_ALL,
                None,
                _OPEN_EXISTING,
                _FILE_FLAG_BACKUP_SEMANTICS,
                None,
            )
        except (ImportError, OSError, RuntimeError) as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            self._handle = None
            return False
        self._started_at = time.monotonic()
        self._thread = threading.Thread(
            target=self._watch,
            daemon=True,
            name=f"aios-fs-watch-{self.root.name[:24]}",
        )
        self._thread.start()
        self._ready.wait(timeout=1.0)
        return self.available

    def _watch(self) -> None:
        try:
            import win32file  # type: ignore[import-not-found]

            self._ready.set()
            while True:
                rows = win32file.ReadDirectoryChangesW(
                    self._handle,
                    65_536,
                    True,
                    _NOTIFY_FILTER,
                    None,
                    None,
                )
                with self._lock:
                    for action, raw_path in rows:
                        relative = str(raw_path or "").replace("\\", "/").strip("/")
                        if not relative or relative.startswith("../") or "/../" in relative:
                            continue
                        key = relative.casefold()
                        row = self._events.get(key)
                        if row is None:
                            if len(self._events) >= self.max_paths:
                                self._overflow = True
                                continue
                            row = {"relative_path": relative, "actions": []}
                            self._events[key] = row
                        name = _ACTION_NAMES.get(int(action), f"action_{int(action)}")
                        if not row["actions"] or row["actions"][-1] != name:
                            row["actions"].append(name)
        except Exception as exc:  # pywin32 raises its own OSError subclass
            code = int(getattr(exc, "winerror", 0) or 0)
            if code != _ERROR_OPERATION_ABORTED and not self._stopped:
                self._error = f"{type(exc).__name__}: {exc}"
        finally:
            self._ready.set()

    def stop(self) -> dict[str, Any]:
        if self._stopped:
            return self._result()
        self._stopped = True
        handle = self._handle
        if handle is not None:
            try:
                ctypes.windll.kernel32.CancelIoEx(int(handle), None)
            except (AttributeError, OSError, TypeError, ValueError):
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if handle is not None:
            try:
                import win32file  # type: ignore[import-not-found]

                win32file.CloseHandle(handle)
            except (ImportError, OSError, RuntimeError):
                pass
        self._handle = None
        return self._result()

    def _result(self) -> dict[str, Any]:
        with self._lock:
            rows = [dict(row) for row in self._events.values()]
        records: list[ChangeRecord] = []
        for row in sorted(rows, key=lambda item: str(item["relative_path"]).casefold()):
            actions = tuple(str(action) for action in row.get("actions") or ())
            # Only an observed creation proves the path did not exist before
            # the command. Replacement/rename sequences remain unknown.
            previous = "deleted" if actions and actions[0] == "added" else None
            records.append(ChangeRecord(str(row["relative_path"]), actions, previous))
        return {
            "engine": "read_directory_changes_w",
            "records": records,
            "path_count": len(records),
            "overflow": self._overflow,
            "error": self._error,
            "elapsed_seconds": round(max(0.0, time.monotonic() - self._started_at), 3)
            if self._started_at
            else 0.0,
        }


def start_directory_tracker(root: str | Path) -> WindowsDirectoryTracker | None:
    tracker = WindowsDirectoryTracker(root)
    return tracker if tracker.start() else None


__all__ = ["ChangeRecord", "WindowsDirectoryTracker", "start_directory_tracker"]
