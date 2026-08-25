"""Deterministic efficiency summaries for one benchmark task trajectory.

The transcript is the source of truth.  A row only becomes a tool call when it
has ``event.tool``; stage, thinking, plan, and review activity cards therefore
cannot inflate these counters.  The saved sequence is deliberately bounded so
a pathological agent cannot make ``run.json`` grow without limit, while all
summary counters still cover the complete event stream.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from typing import Any


TRACE_SCHEMA = 1
MAX_TRACE_CALLS = 64
MAX_BUCKETS = 32
MAX_PATHS_PER_CALL = 8
MAX_ARGUMENT_PREVIEW = 240
MAX_TARGET = 180

_TERMINAL_PHASES = {"completed", "failed", "incomplete", "stopped", "cancelled"}
_EDIT_TOOL_WORDS = ("edit", "write", "patch", "replace", "restore")
_PATH_KEYS = ("relative_path", "file_path", "path", "target_file", "notebook_path")
_TARGET_KEYS = ("query", "pattern", "name", "symbol", "command")
_RANGED_PATH = re.compile(r"^(.*):(\d+)(?:-(\d+))?$")


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _integer(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return fallback


def _text(value: Any, limit: int) -> str:
    clean = " ".join(str(value or "").replace("\x00", "").split())
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 1)].rstrip() + "…"


def _normal_path(value: Any) -> str:
    path = str(value or "").strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path.rstrip("/") or "."


def _path_and_range(value: Any) -> tuple[str, int | None, int | None]:
    raw = _normal_path(value)
    match = _RANGED_PATH.fullmatch(raw)
    if not match:
        return raw, None, None
    path = _normal_path(match.group(1))
    start = int(match.group(2))
    end = int(match.group(3) or match.group(2))
    return path, min(start, end), max(start, end)


def _argument_preview(arguments: Any) -> str:
    if not isinstance(arguments, dict) or not arguments:
        return ""
    try:
        raw = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        raw = repr(arguments)
    return _text(raw, MAX_ARGUMENT_PREVIEW)


def _signature(tool: str, arguments: Any, files: list[str], detail: str) -> str:
    payload = {
        "tool": tool.casefold(),
        "arguments": arguments if isinstance(arguments, dict) else {},
        "files": files,
        "detail": detail if not isinstance(arguments, dict) or not arguments else "",
    }
    try:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        raw = repr(payload)
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]


def _retry_scope(tool: str, arguments: dict[str, Any], paths: list[str], detail: str) -> str:
    """A stable operation target used only to link recovery after a failure.

    Exact duplicates use the full signature above.  Retries may adjust an
    argument, so they share a narrower scope: tool + paths when paths exist,
    otherwise tool + the bounded beginning of the command/query target.
    """
    if paths:
        target = "|".join(sorted(path.casefold() for path in paths))
    else:
        candidate = next(
            (arguments.get(key) for key in _TARGET_KEYS if str(arguments.get(key) or "").strip()),
            detail,
        )
        target = _text(candidate, 64).casefold()
    return f"{tool.casefold()}|{target}"


def _path_suffix_equivalent(left: str, right: str) -> bool:
    """Return whether two paths differ only by an added/removed root prefix."""
    left_key = _normal_path(left).casefold().lstrip("/")
    right_key = _normal_path(right).casefold().lstrip("/")
    if left_key == right_key:
        return True
    return left_key.endswith("/" + right_key) or right_key.endswith("/" + left_key)


def _is_path_prefix_correction(failed_paths: list[str], current_paths: list[str]) -> bool:
    """Match a failed path with a retry that removed or added a wrong root.

    Matching is one-to-one and requires at least one real prefix correction, so
    unrelated files that merely share a basename do not become retries.
    """
    if not failed_paths or len(failed_paths) != len(current_paths):
        return False
    remaining = list(failed_paths)
    corrected = False
    for current in current_paths:
        match_index = next(
            (index for index, failed in enumerate(remaining) if _path_suffix_equivalent(failed, current)),
            None,
        )
        if match_index is None:
            return False
        failed = remaining.pop(match_index)
        corrected = corrected or _normal_path(failed).casefold() != _normal_path(current).casefold()
    return corrected


def _relevant_mutation_epoch(
    inspection_paths: list[str],
    mutated_paths: dict[str, int],
    unknown_mutation_epoch: int,
    current_epoch: int,
) -> int:
    """Return the newest mutation that can affect an inspection's scope."""
    if not inspection_paths:
        # A search/read without an explicit scope is project-wide.
        return current_epoch
    relevant = unknown_mutation_epoch
    for mutated_path, epoch in mutated_paths.items():
        if any(
            _scope_contains(scope, mutated_path) or _scope_contains(mutated_path, scope)
            for scope in inspection_paths
        ):
            relevant = max(relevant, epoch)
    return relevant


def _event_paths(event: dict[str, Any], arguments: dict[str, Any]) -> list[tuple[str, int | None, int | None]]:
    raw_paths: list[Any] = []
    for key in _PATH_KEYS:
        if arguments.get(key):
            raw_paths.append(arguments[key])
    argument_files = arguments.get("files")
    if isinstance(argument_files, list):
        raw_paths.extend(argument_files)
    event_files = event.get("files")
    if isinstance(event_files, list):
        raw_paths.extend(event_files)

    found: list[tuple[str, int | None, int | None]] = []
    seen: set[tuple[str, int | None, int | None]] = set()
    for value in raw_paths:
        if not str(value or "").strip():
            continue
        spec = _path_and_range(value)
        if spec not in seen:
            seen.add(spec)
            found.append(spec)
        if len(found) >= MAX_PATHS_PER_CALL:
            break

    # aiOS read_file carries its range beside the path rather than in it.
    if found and arguments.get("start_line") is not None:
        start = max(1, _integer(arguments.get("start_line"), 1))
        if arguments.get("end_line") is not None:
            end = max(start, _integer(arguments.get("end_line"), start))
        elif arguments.get("max_lines") is not None:
            end = start + max(1, _integer(arguments.get("max_lines"), 1)) - 1
        else:
            end = start
        found[0] = (found[0][0], start, end)
    return found


def _target(arguments: dict[str, Any], specs: list[tuple[str, int | None, int | None]], detail: str) -> str:
    if specs:
        labels = []
        for path, start, end in specs[:2]:
            labels.append(f"{path}:{start}-{end}" if start is not None else path)
        return _text(", ".join(labels), MAX_TARGET)
    for key in _TARGET_KEYS:
        if arguments.get(key) is not None and str(arguments.get(key) or "").strip():
            return _text(arguments[key], MAX_TARGET)
    return _text(detail, MAX_TARGET)


def _is_edit(call: dict[str, Any]) -> bool:
    if str(call.get("type") or "").casefold() == "files":
        return True
    tool = str(call.get("tool") or "").casefold()
    return any(word in tool for word in _EDIT_TOOL_WORDS)


def _is_inspection(call: dict[str, Any]) -> bool:
    activity_type = str(call.get("type") or "").casefold()
    tool = str(call.get("tool") or "").casefold()
    return activity_type in {"read", "search"} or any(word in tool for word in ("read", "search", "find", "grep"))


def _ranges_overlap(left: tuple[int | None, int | None], right: tuple[int | None, int | None]) -> bool:
    if left[0] is None or right[0] is None:
        return True
    return left[0] <= right[1] and right[0] <= left[1]


def _scope_contains(scope: str, path: str) -> bool:
    scope = _normal_path(scope).casefold()
    path = _normal_path(path).casefold()
    return scope == "." or scope == path or path.startswith(scope + "/")


def _bounded_counts(values: list[str]) -> dict[str, int]:
    rows = sorted(Counter(value or "unattributed" for value in values).items(), key=lambda row: (-row[1], row[0]))
    visible = rows[:MAX_BUCKETS]
    omitted = sum(count for _name, count in rows[MAX_BUCKETS:])
    result = {name: count for name, count in visible}
    if omitted:
        result["other"] = omitted
    return result


def build_efficiency_trace(
    events: list[dict[str, Any]],
    *,
    task_started_at: float | None = None,
    default_role: str = "",
) -> dict[str, Any]:
    """Summarise real, chronological tool calls from a task event stream."""
    calls: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    active_role = ""

    for event_index, source in enumerate(events or []):
        if not isinstance(source, dict):
            continue
        event = source
        if str(event.get("kind") or "") == "activity" and str(event.get("activity_type") or "") == "stage":
            stage = str(event.get("stage") or event.get("title") or "").strip().casefold()
            phase = str(event.get("phase") or "").strip().casefold()
            if stage and phase not in _TERMINAL_PHASES:
                active_role = stage
            elif phase in _TERMINAL_PHASES and (not stage or active_role == stage):
                active_role = ""
            continue

        tool = str(event.get("tool") or "").strip()
        if str(event.get("kind") or "") != "activity" or not tool:
            continue
        activity_id = str(event.get("activity_id") or "").strip() or f"event-{event_index + 1}"
        arguments = event.get("arguments") if isinstance(event.get("arguments"), dict) else {}
        specs = _event_paths(event, arguments)
        paths = [path for path, _start, _end in specs]
        detail = str(event.get("detail") or event.get("command") or "")
        timestamp = _number(event.get("ts"))
        phase = str(event.get("phase") or "").strip().casefold() or "completed"
        explicit_role = str(event.get("stage") or "").strip().casefold()
        event_role = str(event.get("role") or "").strip().casefold()
        if not explicit_role and event_role not in {"", "status", "assistant", "result", "activity"}:
            explicit_role = event_role

        call = by_id.get(activity_id)
        if call is None:
            call = {
                "_order": len(calls) + 1,
                "_first_event": event_index,
                "_last_event": event_index,
                "_started": timestamp,
                "_finished": timestamp if phase in _TERMINAL_PHASES else None,
                "_arguments": dict(arguments),
                "_specs": list(specs),
                "_detail": detail,
                "tool": tool,
                "type": str(event.get("activity_type") or "tool").strip().casefold() or "tool",
                "role": explicit_role or active_role or str(default_role or "unattributed"),
                "outcome": "failed" if phase == "failed" else ("completed" if phase == "completed" else "incomplete"),
            }
            by_id[activity_id] = call
            calls.append(call)
        else:
            call["_last_event"] = event_index
            if call.get("_started") is None or (timestamp is not None and timestamp < call["_started"]):
                call["_started"] = timestamp
            if phase in _TERMINAL_PHASES:
                call["_finished"] = timestamp
                call["outcome"] = "failed" if phase == "failed" else ("completed" if phase == "completed" else "incomplete")
            if arguments and not call.get("_arguments"):
                call["_arguments"] = dict(arguments)
            if specs:
                combined = list(call.get("_specs") or [])
                for spec in specs:
                    if spec not in combined and len(combined) < MAX_PATHS_PER_CALL:
                        combined.append(spec)
                call["_specs"] = combined
            if detail and not call.get("_detail"):
                call["_detail"] = detail
            if explicit_role and str(call.get("role") or "") in {"", "unattributed"}:
                call["role"] = explicit_role

    first_timestamp = next((call.get("_started") for call in calls if call.get("_started") is not None), None)
    origin = _number(task_started_at)
    time_origin = "task_started_at" if origin is not None and origin > 0 else "first_tool_call"
    if origin is None or origin <= 0:
        origin = first_timestamp

    prior_signatures: dict[tuple[str, int], dict[str, Any]] = {}
    signature_history: dict[str, dict[str, Any]] = {}
    failed_operations: list[dict[str, Any]] = []
    prior_reads: list[dict[str, Any]] = []
    mutation_epoch = 0
    mutated_paths: dict[str, int] = {}
    unknown_mutation_epoch = 0
    first_edit_index: int | None = None

    for call in calls:
        arguments = call.get("_arguments") if isinstance(call.get("_arguments"), dict) else {}
        specs = list(call.get("_specs") or [])
        paths = list(dict.fromkeys(path for path, _start, _end in specs))[:MAX_PATHS_PER_CALL]
        signature_paths = [
            f"{path}:{start}-{end}" if start is not None else path
            for path, start, end in specs
        ]
        signature = _signature(
            str(call.get("tool") or ""), arguments, signature_paths, str(call.get("_detail") or ""),
        )
        inspection = _is_inspection(call)
        inspection_epoch = _relevant_mutation_epoch(
            paths, mutated_paths, unknown_mutation_epoch, mutation_epoch,
        ) if inspection else 0
        prior = prior_signatures.get((signature, inspection_epoch))
        historical = signature_history.get(signature)
        call["signature"] = signature
        call["_mutation_epoch"] = mutation_epoch
        call["_inspection_epoch"] = inspection_epoch
        call["paths"] = paths
        call["duplicate_of"] = int(prior["_order"]) if prior else None
        call["post_edit_revalidation"] = bool(
            inspection
            and prior is None
            and historical is not None
            and inspection_epoch > int(historical.get("_inspection_epoch") or 0)
        )
        retry_scope = _retry_scope(
            str(call.get("tool") or ""), arguments, paths, str(call.get("_detail") or ""),
        )
        failed = next(
            (candidate for candidate in reversed(failed_operations)
             if candidate.get("_retry_scope") == retry_scope),
            None,
        )
        if failed is None and paths:
            failed = next(
                (
                    candidate
                    for candidate in reversed(failed_operations)
                    if str(candidate.get("tool") or "").casefold() == str(call.get("tool") or "").casefold()
                    and int(candidate.get("_mutation_epoch") or 0) == mutation_epoch
                    and _is_path_prefix_correction(list(candidate.get("paths") or []), paths)
                ),
                None,
            )
        call["retry_of"] = int(failed["_order"]) if failed else None
        call["_retry_scope"] = retry_scope
        if call.get("outcome") == "failed":
            if failed in failed_operations:
                failed_operations.remove(failed)
            failed_operations = [
                candidate for candidate in failed_operations
                if candidate.get("_retry_scope") != retry_scope
            ]
            failed_operations.append(call)
        elif failed:
            failed_operations.remove(failed)
        prior_signatures[(signature, inspection_epoch)] = call
        signature_history[signature] = call

        overlap_with: list[int] = []
        if _is_inspection(call) and str(call.get("type") or "") == "read":
            for previous in prior_reads:
                if int(previous.get("_inspection_epoch") or 0) != inspection_epoch:
                    continue
                for path, start, end in specs:
                    for old_path, old_start, old_end in previous.get("_specs") or []:
                        if path.casefold() == old_path.casefold() and _ranges_overlap((start, end), (old_start, old_end)):
                            overlap_with.append(int(previous["_order"]))
                            break
                    if overlap_with and overlap_with[-1] == int(previous["_order"]):
                        break
            prior_reads.append(call)
        call["overlaps_with"] = list(dict.fromkeys(overlap_with))[:8]

        post_edit = bool(inspection and inspection_epoch > 0)
        call["post_edit_inspection"] = post_edit

        if _is_edit(call):
            if first_edit_index is None:
                first_edit_index = int(call["_order"])
            if call.get("outcome") == "completed":
                mutation_epoch += 1
                if paths:
                    for path in paths:
                        mutated_paths[_normal_path(path)] = mutation_epoch
                else:
                    unknown_mutation_epoch = mutation_epoch

        started = call.get("_started")
        finished = call.get("_finished")
        call["elapsed_seconds"] = round(max(0.0, started - origin), 3) if started is not None and origin is not None else None
        call["duration_seconds"] = round(max(0.0, finished - started), 3) if started is not None and finished is not None else None
        call["paths"] = paths
        call["read_ranges"] = [
            {"path": path, "start_line": start, "end_line": end}
            for path, start, end in specs
        ] if str(call.get("type") or "") == "read" else []
        call["target"] = _target(arguments, specs, str(call.get("_detail") or ""))
        call["argument_preview"] = _argument_preview(arguments)

    first_edit = calls[first_edit_index - 1] if first_edit_index else None
    sequence = []
    for call in calls[:MAX_TRACE_CALLS]:
        row = {
            "index": int(call["_order"]),
            "tool": str(call.get("tool") or ""),
            "type": str(call.get("type") or "tool"),
            "role": str(call.get("role") or "unattributed"),
            "outcome": str(call.get("outcome") or "incomplete"),
            "elapsed_seconds": call.get("elapsed_seconds"),
            "duration_seconds": call.get("duration_seconds"),
            "target": str(call.get("target") or ""),
            "paths": list(call.get("paths") or []),
            "read_ranges": list(call.get("read_ranges") or []),
            "argument_preview": str(call.get("argument_preview") or ""),
            "signature": str(call.get("signature") or ""),
            "duplicate_of": call.get("duplicate_of"),
            "retry_of": call.get("retry_of"),
            "overlaps_with": list(call.get("overlaps_with") or []),
            "post_edit_inspection": bool(call.get("post_edit_inspection")),
            "post_edit_revalidation": bool(call.get("post_edit_revalidation")),
        }
        sequence.append(row)

    total = len(calls)
    return {
        "schema": TRACE_SCHEMA,
        "total_calls": total,
        "shown_calls": len(sequence),
        "omitted_calls": max(0, total - len(sequence)),
        "failed_calls": sum(call.get("outcome") == "failed" for call in calls),
        "duplicate_calls": sum(call.get("duplicate_of") is not None for call in calls),
        "retry_calls": sum(call.get("retry_of") is not None for call in calls),
        "overlapping_read_calls": sum(bool(call.get("overlaps_with")) for call in calls),
        "post_edit_inspection_calls": sum(bool(call.get("post_edit_inspection")) for call in calls),
        "post_edit_revalidation_calls": sum(bool(call.get("post_edit_revalidation")) for call in calls),
        "first_edit_call": first_edit_index,
        "time_to_first_edit_seconds": first_edit.get("elapsed_seconds") if first_edit else None,
        "time_origin": time_origin if calls else "unavailable",
        "calls_before_first_edit": first_edit_index - 1 if first_edit_index else None,
        "calls_after_first_edit": total - first_edit_index if first_edit_index else None,
        "tools_by_name": _bounded_counts([str(call.get("tool") or "") for call in calls]),
        "tools_by_type": _bounded_counts([str(call.get("type") or "tool") for call in calls]),
        "tools_by_role": _bounded_counts([str(call.get("role") or "unattributed") for call in calls]),
        "sequence": sequence,
    }
