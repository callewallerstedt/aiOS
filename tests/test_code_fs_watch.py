from __future__ import annotations

import os
from pathlib import Path
import time

import pytest

import code_fs_watch


@pytest.mark.skipif(os.name != "nt", reason="ReadDirectoryChangesW is Windows-only")
def test_windows_tracker_reports_recursive_file_changes_without_tree_scan(tmp_path: Path):
    nested = tmp_path / "large" / "nested"
    nested.mkdir(parents=True)
    existing = nested / "existing.lua"
    existing.write_text("return 1\n", encoding="utf-8")
    tracker = code_fs_watch.start_directory_tracker(tmp_path)
    assert tracker is not None

    existing.write_text("return 2\n", encoding="utf-8")
    created = nested / "temporary.lua"
    created.write_text("return true\n", encoding="utf-8")
    created.unlink()
    time.sleep(0.15)
    result = tracker.stop()

    records = {row.relative_path.replace("\\", "/"): row for row in result["records"]}
    assert result["engine"] == "read_directory_changes_w"
    assert result["overflow"] is False
    assert result["error"] == ""
    assert "large/nested/existing.lua" in records
    assert "modified" in records["large/nested/existing.lua"].actions
    assert records["large/nested/temporary.lua"].previous_revision == "deleted"
    assert "removed" in records["large/nested/temporary.lua"].actions


def test_unavailable_tracker_fails_closed_without_starting_a_thread(tmp_path: Path, monkeypatch):
    tracker = code_fs_watch.WindowsDirectoryTracker(tmp_path)
    monkeypatch.setattr(code_fs_watch.os, "name", "posix")

    assert tracker.start() is False
    assert tracker.available is False
    assert tracker.stop()["records"] == []
