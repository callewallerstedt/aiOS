"""Provider-neutral context manifests for aiOS CODE session handoffs.

Native Claude Code, Codex, and Cursor session ids are intentionally never
treated as interchangeable.  This module builds a bounded, versioned snapshot
that can seed a fresh native session while the aiOS logical job id remains
stable.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Iterable


SCHEMA = "aios.code-handoff"
SCHEMA_VERSION = 1
MAX_RECENT_EVENTS = 40
MAX_RECENT_OUTPUTS = 8
MAX_CHANGED_FILES = 250
MAX_TEXT_CHARS = 4000


def _text(value: Any, limit: int = MAX_TEXT_CHARS) -> str:
    value = str(value or "").strip()
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)].rstrip() + "..."


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple, set)) else [value]
    result: list[str] = []
    for item in values:
        text = _text(item, 800)
        if text and text not in result:
            result.append(text)
    return result


def _candidate_lines(values: Iterable[Any]) -> list[str]:
    lines: list[str] = []
    for value in values:
        for line in str(value or "").splitlines():
            clean = re.sub(r"^[\s>*#\-\d.)]+", "", line).strip()
            if 8 <= len(clean) <= 800 and clean not in lines:
                lines.append(clean)
    return lines


def _extract_constraints(meta: dict, events: list[dict]) -> list[str]:
    explicit = _string_list(meta.get("constraints"))
    sources = [meta.get("brief")]
    sources.extend(event.get("text") for event in events if event.get("kind") == "user")
    pattern = re.compile(
        r"\b(must|must not|should|do not|don't|never|avoid|preserve|prefer|required|"
        r"requirement|constraint|acceptance|only|without)\b",
        re.IGNORECASE,
    )
    for line in _candidate_lines(sources):
        if pattern.search(line) and line not in explicit:
            explicit.append(line)
    return explicit[:40]


def _extract_decisions(meta: dict, events: list[dict]) -> list[str]:
    explicit = _string_list(meta.get("decisions"))
    sources = [meta.get("brief")]
    sources.extend(
        event.get("text")
        for event in events
        if event.get("kind") in {"user", "assistant", "result"}
    )
    pattern = re.compile(
        r"\b(decided|decision|chosen|choose|selected|use|using|keep|prefer|architecture|approach)\b",
        re.IGNORECASE,
    )
    for line in _candidate_lines(sources):
        if pattern.search(line) and line not in explicit:
            explicit.append(line)
    return explicit[:40]


def collect_worktree_changes(cwd: str | Path) -> list[dict]:
    """Return bounded git working-tree paths without reading their contents."""
    project = Path(cwd).expanduser().resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(project), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    records = result.stdout.decode("utf-8", "replace").split("\0")
    changes: list[dict] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        status = record[:2].strip() or "?"
        path = record[3:] if len(record) > 3 else record
        # Porcelain v1 emits a second NUL record for the original rename path.
        if "R" in record[:2] or "C" in record[:2]:
            old_path = records[index] if index < len(records) else ""
            index += 1
        else:
            old_path = ""
        item = {"path": path, "status": status, "source": "git"}
        if old_path:
            item["previous_path"] = old_path
        changes.append(item)
        if len(changes) >= MAX_CHANGED_FILES:
            break
    return changes


def _event_files(events: list[dict]) -> list[dict]:
    found: list[dict] = []
    known: set[str] = set()
    for event in events:
        values: list[Any] = []
        files = event.get("files") or []
        values.extend(files if isinstance(files, (list, tuple, set)) else [files])
        changes = event.get("changes") or []
        for change in changes if isinstance(changes, (list, tuple)) else [changes]:
            if isinstance(change, dict):
                values.append(change.get("path"))
        for value in values:
            path = str(value or "").strip()
            if path and path not in known:
                known.add(path)
                found.append({"path": path, "status": "touched", "source": "agent_event"})
    return found


def _merge_changed_files(worktree: list[dict], events: list[dict]) -> tuple[list[dict], bool]:
    merged: list[dict] = []
    positions: dict[str, int] = {}
    for item in [*_event_files(events), *worktree]:
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        if path in positions:
            merged[positions[path]].update({key: value for key, value in item.items() if value})
        else:
            positions[path] = len(merged)
            merged.append(dict(item))
    truncated = len(merged) > MAX_CHANGED_FILES
    return merged[:MAX_CHANGED_FILES], truncated


def _safe_recent_events(events: list[dict]) -> list[dict]:
    safe: list[dict] = []
    for event in events[-MAX_RECENT_EVENTS:]:
        kind = str(event.get("kind") or "")
        if kind not in {
            "user", "assistant", "result", "question", "status", "warning",
            "error", "activity", "provider_switch",
        }:
            continue
        row = {
            "ts": event.get("ts"),
            "kind": kind,
            "text": _text(event.get("text") or event.get("title"), 1200),
        }
        for key in ("activity_type", "phase", "title", "state", "from_provider", "to_provider"):
            if event.get(key) not in (None, ""):
                row[key] = event.get(key)
        safe.append(row)
    return safe


def _recent_agent_output(events: list[dict]) -> list[str]:
    outputs: list[str] = []
    for event in reversed(events):
        if event.get("kind") not in {"assistant", "result"}:
            continue
        value = _text(event.get("text") or event.get("delta"), 2400)
        if value and value not in outputs:
            outputs.append(value)
        if len(outputs) >= MAX_RECENT_OUTPUTS:
            break
    outputs.reverse()
    return outputs


def build_manifest(
    meta: dict,
    events: list[dict],
    *,
    target_provider: str,
    target_model: str,
    target_reasoning: str,
    target_fast: bool,
    instruction: str = "",
    worktree_changes: list[dict] | None = None,
    handoff_id: str = "",
) -> dict:
    """Build a JSON-serializable, provider-neutral continuation snapshot."""
    handoff_id = handoff_id or uuid.uuid4().hex[:16]
    changed_files, files_truncated = _merge_changed_files(worktree_changes or [], events)
    pending = _string_list(meta.get("pending_questions"))
    current_question = _text(meta.get("pending_question"), 1600)
    if current_question and current_question not in pending:
        pending.append(current_question)
    last_summary = _text(meta.get("last_summary"), 2400)
    brief = _text(meta.get("brief"), 8000)
    manifest = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "handoff_id": handoff_id,
        "created_at": round(time.time(), 3),
        "logical_session": {
            "id": str(meta.get("id") or ""),
            "title": str(meta.get("title") or ""),
            "cwd": str(meta.get("cwd") or ""),
        },
        "source": {
            "provider": str(meta.get("provider") or ""),
            "model": str(meta.get("model") or ""),
            "reasoning": str(meta.get("reasoning") or ""),
            "fast": bool(meta.get("fast")),
            "native_session_id": str(meta.get("native_session_id") or ""),
        },
        "target": {
            "provider": str(target_provider or ""),
            "model": str(target_model or ""),
            "reasoning": str(target_reasoning or ""),
            "fast": bool(target_fast),
            "native_session_id": None,
        },
        "context": {
            "task_summary": brief,
            "conversation_summary": last_summary or brief,
            "constraints": _extract_constraints(meta, events),
            "decisions": _extract_decisions(meta, events),
            "pending_questions": pending,
            "files_changed": changed_files,
            "files_changed_truncated": files_truncated,
            "recent_agent_output": _recent_agent_output(events),
            "recent_events": _safe_recent_events(events),
        },
        "continuation_instruction": _text(
            instruction or "Continue the current task from the transferred context and working tree state.",
            4000,
        ),
        "native_continuation": False,
        "limitations": [
            "The target provider receives a new native session id.",
            "Native provider transcripts, hidden reasoning, and provider-specific tool state are not transferred.",
            "The working directory and files remain shared and must be inspected before further edits.",
        ],
    }
    validate_manifest(manifest)
    return manifest


def validate_manifest(manifest: dict) -> None:
    if not isinstance(manifest, dict):
        raise ValueError("handoff manifest must be an object")
    if manifest.get("schema") != SCHEMA or manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported handoff manifest schema")
    for key in ("handoff_id", "logical_session", "source", "target", "context"):
        if not manifest.get(key):
            raise ValueError(f"handoff manifest is missing {key}")
    if manifest["source"].get("provider") == manifest["target"].get("provider"):
        raise ValueError("provider handoff requires a different target provider")
    if not manifest["logical_session"].get("cwd"):
        raise ValueError("handoff manifest is missing cwd")


def serialize_manifest(manifest: dict) -> str:
    validate_manifest(manifest)
    return json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)


def deserialize_manifest(payload: str | bytes) -> dict:
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    manifest = json.loads(payload)
    validate_manifest(manifest)
    return manifest


def bridge_prompt(manifest: dict) -> str:
    """Create the first user input for the fresh target provider session."""
    serialized = serialize_manifest(manifest)
    return (
        "You are continuing an aiOS CODE logical session after a provider handoff. "
        "This is a new native provider session, not a native transcript resume.\n\n"
        "Use the provider-neutral manifest below as continuity context. Treat the current working "
        "directory and on-disk files as the source of truth, inspect them before editing, preserve "
        "the listed constraints and decisions, address pending questions when possible, and continue "
        "naturally from the recent output. Do not claim access to the source provider's hidden state "
        "or full native transcript.\n\n"
        "<aios-code-handoff-manifest>\n"
        f"{serialized}\n"
        "</aios-code-handoff-manifest>\n\n"
        f"Continuation request: {manifest.get('continuation_instruction')}"
    )
