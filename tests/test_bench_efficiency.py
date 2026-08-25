"""The benchmark trajectory summary must describe calls the agent really made."""

from __future__ import annotations

import sys
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bench import efficiency, runs, suites  # noqa: E402
from tests.test_bench_run import REFERENCE, _run_one  # noqa: E402


def activity(
    activity_id: str,
    tool: str,
    activity_type: str,
    phase: str,
    ts: float,
    arguments: dict | None = None,
) -> dict:
    return {
        "kind": "activity",
        "activity_id": activity_id,
        "tool": tool,
        "activity_type": activity_type,
        "phase": phase,
        "ts": ts,
        "arguments": arguments or {},
    }


def test_trace_is_chronological_and_counts_waste_from_real_tool_events_only():
    events = [
        {"kind": "activity", "activity_id": "stage", "activity_type": "stage",
         "stage": "coder", "phase": "started", "ts": 99.0},
        activity("search", "search_text", "search", "started", 100.0,
                 {"query": "sortSessions", "relative_path": "web/js/sessions.js"}),
        activity("search", "search_text", "search", "completed", 100.1,
                 {"query": "sortSessions", "relative_path": "web/js/sessions.js"}),
        activity("read-1", "read_file", "read", "completed", 101.0,
                 {"relative_path": "web/js/sessions.js", "start_line": 1, "max_lines": 20}),
        activity("read-2", "read_file", "read", "completed", 102.0,
                 {"relative_path": "web/js/sessions.js", "start_line": 10, "max_lines": 21}),
        activity("edit", "edit_file", "files", "completed", 104.0,
                 {"relative_path": "web/js/sessions.js", "old_text": "old", "new_text": "new"}),
        activity("read-3", "read_file", "read", "completed", 105.0,
                 {"relative_path": "web/js/sessions.js", "start_line": 15, "max_lines": 4}),
        activity("shell-1", "run_shell", "command", "failed", 106.0,
                 {"command": "npm test -- --runInBand"}),
        activity("shell-2", "run_shell", "command", "completed", 107.0,
                 {"command": "npm test -- --runInBand"}),
        {"kind": "activity", "activity_id": "review", "activity_type": "review",
         "phase": "completed", "ts": 108.0},
    ]

    trace = efficiency.build_efficiency_trace(events, task_started_at=100.0)

    assert [row["tool"] for row in trace["sequence"]] == [
        "search_text", "read_file", "read_file", "edit_file", "read_file", "run_shell", "run_shell",
    ]
    assert trace["total_calls"] == 7
    assert trace["first_edit_call"] == 4
    assert trace["time_to_first_edit_seconds"] == 4.0
    assert trace["calls_before_first_edit"] == 3
    assert trace["calls_after_first_edit"] == 3
    assert trace["failed_calls"] == 1
    assert trace["duplicate_calls"] == 1
    assert trace["retry_calls"] == 1
    assert trace["overlapping_read_calls"] == 1
    assert trace["post_edit_inspection_calls"] == 1
    assert trace["post_edit_revalidation_calls"] == 0
    assert trace["tools_by_role"] == {"coder": 7}
    assert trace["tools_by_type"] == {"read": 3, "command": 2, "files": 1, "search": 1}
    assert trace["tools_by_name"]["read_file"] == 3
    assert trace["sequence"][2]["overlaps_with"] == [2]
    assert trace["sequence"][4]["overlaps_with"] == []
    assert trace["sequence"][4]["post_edit_inspection"] is True
    assert trace["sequence"][4]["post_edit_revalidation"] is False
    assert trace["sequence"][6]["duplicate_of"] == 6
    assert trace["sequence"][6]["retry_of"] == 6


def test_inspection_duplicates_and_overlaps_reset_only_after_a_relevant_mutation():
    search = {"query": "renderPanel", "relative_path": "src"}
    read = {"relative_path": "src/panel.py", "start_line": 1, "max_lines": 20}
    events = [
        activity("search-1", "search_text", "search", "completed", 1.0, search),
        activity("read-1", "read_file", "read", "completed", 2.0, read),
        activity("search-2", "search_text", "search", "completed", 3.0, search),
        activity("edit", "edit_file", "files", "completed", 4.0,
                 {"relative_path": "src/panel.py", "old_text": "old", "new_text": "new"}),
        activity("search-3", "search_text", "search", "completed", 5.0, search),
        activity("read-2", "read_file", "read", "completed", 6.0, read),
        activity("search-4", "search_text", "search", "completed", 7.0, search),
        activity("edit-other", "edit_file", "files", "completed", 8.0,
                 {"relative_path": "tests/other.py", "old_text": "old", "new_text": "new"}),
        activity("search-5", "search_text", "search", "completed", 9.0, search),
    ]

    trace = efficiency.build_efficiency_trace(events)

    assert trace["duplicate_calls"] == 3
    assert trace["overlapping_read_calls"] == 0
    assert trace["post_edit_inspection_calls"] == 4
    assert trace["post_edit_revalidation_calls"] == 2
    assert trace["sequence"][2]["duplicate_of"] == 1
    assert trace["sequence"][4]["duplicate_of"] is None
    assert trace["sequence"][4]["post_edit_revalidation"] is True
    assert trace["sequence"][5]["duplicate_of"] is None
    assert trace["sequence"][5]["overlaps_with"] == []
    assert trace["sequence"][5]["post_edit_revalidation"] is True
    assert trace["sequence"][6]["duplicate_of"] == 5
    assert trace["sequence"][6]["post_edit_revalidation"] is False
    assert trace["sequence"][8]["duplicate_of"] == 7
    assert trace["sequence"][8]["post_edit_revalidation"] is False


def test_path_prefix_correction_is_a_retry_but_same_basename_elsewhere_is_not():
    trace = efficiency.build_efficiency_trace([
        activity("bad-root", "read_file", "read", "failed", 1.0,
                 {"relative_path": "C:/work/root/src/app.py"}),
        activity("relative", "read_file", "read", "completed", 2.0,
                 {"relative_path": "src/app.py"}),
        activity("wrong-tree", "read_file", "read", "failed", 3.0,
                 {"relative_path": "src/config.py"}),
        activity("other-tree", "read_file", "read", "completed", 4.0,
                 {"relative_path": "tests/config.py"}),
    ])

    assert trace["retry_calls"] == 1
    assert trace["sequence"][1]["retry_of"] == 1
    assert trace["sequence"][3]["retry_of"] is None


def test_failed_edit_attempt_does_not_create_a_mutation_epoch():
    search = {"query": "renderPanel", "relative_path": "src"}
    trace = efficiency.build_efficiency_trace([
        activity("search-1", "search_text", "search", "completed", 1.0, search),
        activity("edit", "edit_file", "files", "failed", 2.0,
                 {"relative_path": "src/panel.py", "old_text": "missing", "new_text": "new"}),
        activity("search-2", "search_text", "search", "completed", 3.0, search),
    ])

    assert trace["sequence"][2]["duplicate_of"] == 1
    assert trace["sequence"][2]["post_edit_inspection"] is False
    assert trace["sequence"][2]["post_edit_revalidation"] is False
    assert trace["post_edit_revalidation_calls"] == 0


def test_trace_is_bounded_without_losing_complete_summary_counts():
    events = [
        activity(
            f"read-{index}",
            "read_file",
            "read",
            "completed",
            float(index),
            {"relative_path": f"src/file_{index}.py", "note": "x" * 1000},
        )
        for index in range(efficiency.MAX_TRACE_CALLS + 6)
    ]

    trace = efficiency.build_efficiency_trace(events, default_role="native")

    assert trace["total_calls"] == efficiency.MAX_TRACE_CALLS + 6
    assert trace["shown_calls"] == efficiency.MAX_TRACE_CALLS
    assert trace["omitted_calls"] == 6
    assert trace["tools_by_name"] == {"read_file": efficiency.MAX_TRACE_CALLS + 6}
    assert trace["tools_by_role"] == {"native": efficiency.MAX_TRACE_CALLS + 6}
    assert all(len(row["argument_preview"]) <= efficiency.MAX_ARGUMENT_PREVIEW for row in trace["sequence"])


def test_an_event_tool_is_a_call_even_when_its_display_type_is_plan():
    trace = efficiency.build_efficiency_trace([
        activity("plan-tool", "update_plan", "plan", "completed", 1.0, {"step": "verify"}),
        {"kind": "activity", "activity_id": "plan-card", "activity_type": "plan", "ts": 2.0},
    ])

    assert trace["total_calls"] == 1
    assert trace["sequence"][0]["tool"] == "update_plan"


def test_runner_persists_the_trace_in_the_task_result(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    task = suites.select({"bugfix": 1})[0]

    run = _run_one(tmp_path / "runs", task, REFERENCE[task.id])
    trace = run["tasks"][0]["efficiency_trace"]
    on_disk = json.loads((runs.run_dir(run["id"]) / "run.json").read_text(encoding="utf-8"))

    assert trace["total_calls"] == 1
    assert trace["sequence"][0]["tool"] == "run_shell"
    assert on_disk["tasks"][0]["efficiency_trace"] == trace


def test_malformed_optional_timing_and_range_metadata_cannot_break_a_run():
    event = activity(
        "read", "read_file", "read", "completed", float("nan"),
        {"relative_path": "src/app.py", "start_line": "all", "max_lines": "many"},
    )

    trace = efficiency.build_efficiency_trace([event], task_started_at=float("inf"))

    assert trace["total_calls"] == 1
    assert trace["time_origin"] == "first_tool_call"
    assert trace["sequence"][0]["elapsed_seconds"] is None
    assert trace["sequence"][0]["read_ranges"] == [
        {"path": "src/app.py", "start_line": 1, "end_line": 1},
    ]
