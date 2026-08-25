"""Run storage. One run, one directory, nothing shared.

    bench/runs/<run id>/
        run.json        config, per-task state, score -- the whole run
        jobs/           AIOS_CODE_JOBS_DIR for this run only
        work/<task>/    the git repository the agent was given
        runner.log      stdout of the runner process
        STOP            sentinel; its presence asks the runner to wind down

The `jobs/` folder is the reason a run is a subprocess rather than a thread.
`code_jobs.JOBS_DIR` is read from the environment at import time, so the only
way to point a run at its own session store is to start a process with
`AIOS_CODE_JOBS_DIR` set. That isolation is the point: benchmark sessions are
invisible to the CODE tab, cannot be deleted by tidying up sessions, and cannot
drag the real session list to 200 rows every time you measure something.

Ownership is equally deliberate. The runner owns `run.json` and is the only
writer; the UI only ever reads it, or drops the STOP sentinel next to it. Two
processes editing the same file is a bug waiting for a slow disk.
"""

from __future__ import annotations

import json
import hashlib
import copy
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from . import ROOT, RUNS_DIR
from . import custom, project_campaigns, scoring, suites

import code_roles

# A run whose runner has not written anything for this long, and whose process
# is gone, is stranded rather than slow.
STALE_AFTER = 90.0

MAX_CONCURRENCY = 8
# Suite tasks are short; custom prompt builds (whole apps) often need longer.
MAX_TIMEOUT = 3600.0
CUSTOM_MAX_TIMEOUT = 7200.0
MAX_COST_CEILING_USD = 25.0
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
AGENT_PORT_BASE = 24000
TASK_SET_HASH_SCHEMA = 2
_SUPPORTED_ENGINES = frozenset({"aios", "codex", "claude", "omp", "hermes", "kimi"})


def _default_provider(engine: str) -> str:
    if engine == "aios":
        return ""
    return "openrouter" if engine == "kimi" else engine


def _default_cost_provenance(engine: str) -> str:
    if engine == "codex":
        return "unavailable"
    return "api_equivalent" if engine == "claude" else "provider_reported"


def _aios_selection_error(provider: str, model: str, reasoning: str, fast: bool) -> dict | None:
    """Use CODE's exact cached selection contract without importing it at module load."""
    from code_jobs import selection_error

    return selection_error(provider, model, reasoning, fast)


def _resolved_harness_version(engine: str, supplied: Any = "") -> str:
    """Persist an exact aiOS code/worktree identity when callers omit one."""
    value = str(supplied or "").strip()[:120]
    if value or str(engine or "").casefold() != "aios":
        return value
    from . import adapters
    return adapters._git_version()[:120]


def _benchmark_identity() -> tuple[int, int]:
    """Return an unused Agent #xxx whose port ends in the same three digits."""
    used = set()
    for directory in RUNS_DIR.glob("*") if RUNS_DIR.exists() else []:
        payload = _read(directory / "run.json")
        if str(payload.get("status") or "") in {"starting", "running", "stopping"}:
            used.add(int(payload.get("agent_id") or 0))
    start = 100 + int.from_bytes(os.urandom(2), "big") % 900
    for offset in range(900):
        agent_id = 100 + ((start - 100 + offset) % 900)
        if agent_id in used:
            continue
        port = AGENT_PORT_BASE + agent_id
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            continue
        finally:
            probe.close()
        return agent_id, port
    raise RuntimeError("no free benchmark agent ports are available")
_LIVE_TOOL_CACHE: dict[tuple[str, str], dict[str, Any]] = {}


# ------------------------------------------------------------------- storage


def _atomic_json(path: Path, payload: dict) -> None:
    """Write run.json whole. The UI reads it constantly; it never sees a half.

    Unique temp name for the same reason code_jobs uses one: a shared ".tmp" is
    a race between writers, and on Windows the loser's replace() fails outright.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f".{os.getpid()}-{uuid.uuid4().hex[:8]}.tmp")
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        for attempt in range(3):
            try:
                temp.replace(path)
                return
            except OSError:
                if attempt == 2:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        temp.unlink(missing_ok=True)


def run_dir(run_id: str) -> Path:
    """Resolve a run id to its directory, refusing anything path-shaped."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "", str(run_id or ""))
    if not safe or safe != str(run_id):
        raise ValueError("bad run id")
    return RUNS_DIR / safe


def write_run(run: dict) -> dict:
    run["updated_at"] = round(time.time(), 3)
    _atomic_json(run_dir(str(run["id"])) / "run.json", run)
    return run


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _money(value: Any, fallback: float = 0.0) -> float:
    try:
        return max(0.0, float(value if value is not None else fallback))
    except (TypeError, ValueError):
        return max(0.0, float(fallback))


def _task_ids(value: Any) -> list[str]:
    """Return a bounded, de-duplicated execution subset from persisted input."""
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in value[:256]:
        task_id = str(raw or "").strip()[:240]
        if task_id and task_id not in seen:
            seen.add(task_id)
            cleaned.append(task_id)
    return cleaned


def _task_manifest(task: Any) -> dict[str, Any]:
    """The complete, grading-relevant identity of one frozen task."""
    files = getattr(task, "files", {}) if isinstance(getattr(task, "files", {}), dict) else {}
    provenance = getattr(task, "provenance", {})
    if not isinstance(provenance, dict):
        provenance = {}
    return {
        "id": str(getattr(task, "id", "")),
        "suite": str(getattr(task, "suite", "")),
        "brief": str(getattr(task, "brief", "")),
        "files": {
            str(name): hashlib.sha256(str(body).encode("utf-8")).hexdigest()
            for name, body in sorted(files.items())
        },
        "verifier": hashlib.sha256(str(getattr(task, "verifier", "")).encode("utf-8")).hexdigest(),
        # protected_checks() contributes to the external verdict even though it
        # is not embedded in Task.verifier, so omitting this made two different
        # graders share a fingerprint.
        "protected": sorted(str(path) for path in (getattr(task, "protected", ()) or ())),
        # Public benchmark source/commit/task provenance pins the exact upstream
        # case.  It is part of reproducibility, not decorative report metadata.
        "source": str(getattr(task, "source", "") or ""),
        "provenance": provenance,
    }


def _task_fixture_hash(task: Any) -> str:
    raw = json.dumps(
        {"schema": TASK_SET_HASH_SCHEMA, "task": _task_manifest(task)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _task_set_hash(tasks: list) -> str:
    """Fingerprint exact prompts, fixtures, source pins, and external graders."""
    manifest = sorted(
        (_task_manifest(task) for task in tasks),
        key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )
    raw = json.dumps(
        {"schema": TASK_SET_HASH_SCHEMA, "tasks": manifest},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _valid_task_set_hash(value: Any) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", str(value or "").strip().casefold()))


def _split_cost_cap(total_usd: float, count: int) -> list[float]:
    """Split a six-decimal cap exactly; equal rounding must never exceed it."""
    count = max(0, int(count or 0))
    if not count:
        return []
    units = max(0, int(round(_money(total_usd) * 1_000_000)))
    base, residual = divmod(units, count)
    return [round((base + (1 if index < residual else 0)) / 1_000_000, 6) for index in range(count)]


def _budget_snapshot(run: dict, summary: dict | None = None) -> dict:
    config = run.get("config") if isinstance(run.get("config"), dict) else {}
    cap = _money(config.get("max_cost_usd"))
    rolled = summary if isinstance(summary, dict) else (run.get("summary") or {})
    spent = _money(rolled.get("cost_usd"))
    return {
        "cap_usd": round(cap, 6),
        "spent_usd": round(spent, 6),
        "remaining_usd": round(max(0.0, cap - spent), 6) if cap else None,
        "exhausted": bool(cap and spent >= cap),
        "enforcement": "observed-soft" if cap else "none",
        "note": ("Stops active work after reported spend reaches the ceiling; one in-flight provider "
                 "request can overshoot." if cap else "No run cost ceiling configured."),
    }


def _process_alive(pid: Any) -> bool:
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=10, creationflags=CREATE_NO_WINDOW,
        )
        return str(pid) in (result.stdout or "")
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _settle(run: dict) -> dict:
    """Report a runner that died mid-run as interrupted, not as still running.

    Checked on read rather than written back, because the reader is the UI and
    the UI is not allowed to write run.json -- see the module docstring.
    """
    if str(run.get("status")) != "running":
        return run
    if time.time() - float(run.get("updated_at") or 0) < STALE_AFTER:
        return run
    if _process_alive(run.get("pid")):
        return run
    run = dict(run)
    run["status"] = "interrupted"
    run["error"] = run.get("error") or "the runner stopped without finishing"
    for task in run.get("tasks") or []:
        if task.get("status") in {"pending", "running", "verifying"}:
            task["status"] = "interrupted"
    return run


def get_run(run_id: str) -> dict | None:
    try:
        payload = _read(run_dir(run_id) / "run.json")
    except ValueError:
        return None
    return _with_live_metrics(_settle(payload)) if payload.get("id") else None


def _live_tool_calls(run_id: str, job_id: str) -> int:
    path = run_dir(run_id) / "jobs" / job_id / "events.jsonl"
    key = (run_id, job_id)
    cached = _LIVE_TOOL_CACHE.get(key) or {"offset": 0, "seen": set()}
    seen = set(cached["seen"])
    offset = int(cached["offset"])
    try:
        size = path.stat().st_size
        if size < offset:
            offset, seen = 0, set()
        if size == offset:
            return len(seen)
        with path.open("rb") as handle:
            handle.seek(offset)
            raw = handle.read()
        newline = raw.rfind(b"\n")
        complete = raw[: newline + 1] if newline >= 0 else b""
        for line in complete.decode("utf-8", "replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (str(event.get("kind")) == "activity"
                    and str(event.get("tool") or "").strip()):
                seen.add(str(event.get("activity_id") or event.get("ts")))
    except OSError:
        return len(seen)
    _LIVE_TOOL_CACHE[key] = {"offset": offset + len(complete), "seen": seen}
    return len(seen)


def _with_live_metrics(run: dict) -> dict:
    """Overlay the running job's counters without ever writing run.json.

    The runner commits final counters when a task ends. While it is working,
    code_jobs already updates job.json; reading that snapshot is what makes the
    BENCH counters genuinely live instead of showing zero until completion.
    """
    if str(run.get("status")) not in {"starting", "running"}:
        result = dict(run)
        result["budget"] = _budget_snapshot(result, result.get("summary") or {})
        return result
    result = dict(run)
    result["stop_requested"] = (run_dir(str(run["id"])) / "STOP").exists()
    tasks = []
    now = time.time()
    for stored in run.get("tasks") or []:
        task = dict(stored)
        job_id = str(task.get("job_id") or "")
        if job_id and str(task.get("status")) in {"pending", "running", "verifying"}:
            meta = _read(run_dir(str(run["id"])) / "jobs" / job_id / "job.json")
            usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else {}
            usage = dict(usage)
            if not usage.get("cost_usd"):
                usage["cost_usd"] = meta.get("estimated_cost_usd") or 0.0
            task["usage"] = usage
            task["role_usage"] = dict(meta.get("role_usage") or {})
            task["pipeline_stages"] = dict(meta.get("pipeline_stages") or {})
            started = float(task.get("started_at") or 0)
            if started:
                task["seconds"] = round(max(0.0, now - started), 1)
            for field in ("files_edited", "lines_added", "lines_deleted"):
                task[field] = int(meta.get(field) or 0)
            task["model_request_count"] = meta.get("model_request_count")
            task["model_request_count_source"] = str(meta.get("model_request_count_source") or "unavailable")
            task["model_request_rounds"] = list(meta.get("model_request_rounds") or [])[:128]
            task["model_request_rounds_omitted"] = int(meta.get("model_request_rounds_omitted") or 0)
            task["tool_calls"] = _live_tool_calls(str(run["id"]), job_id)
            task["job_status"] = str(meta.get("status") or "")
        tasks.append(task)
    result["tasks"] = tasks
    result["summary"] = scoring.summarise(tasks)
    result["budget"] = _budget_snapshot(result, result["summary"])
    return result


def summarise_run(run: dict) -> dict:
    """The compact shape the run list needs -- no task detail, no briefs."""
    config = run.get("config") or {}
    summary = run.get("summary") or {}
    tasks = run.get("tasks") or []
    models = config.get("models") if isinstance(config.get("models"), list) else []
    model_label = config.get("model")
    if config.get("kind") == "custom" and models:
        names = [str(row.get("model") or "") for row in models if isinstance(row, dict)]
        model_label = ", ".join(name for name in names if name) or model_label
    return {
        "id": run.get("id"),
        "label": run.get("label") or "",
        "saved_config_id": run.get("saved_config_id") or "",
        "saved_config_name": run.get("saved_config_name") or "",
        "agent_id": int(run.get("agent_id") or 0),
        "preview_port": int(run.get("preview_port") or 0),
        "group_id": run.get("group_id") or "",
        "group_label": run.get("group_label") or "",
        "group_index": int(run.get("group_index") or 0),
        "group_size": int(run.get("group_size") or 0),
        "continued_from_group": run.get("continued_from_group") or "",
        "continued_from_run": run.get("continued_from_run") or "",
        "continuation_mode": run.get("continuation_mode") or "",
        "seeded_task_count": int(run.get("seeded_task_count") or 0),
        "kind": config.get("kind") or "suite",
        "engine": config.get("engine") or "aios",
        "harness_label": config.get("harness_label") or run.get("saved_config_name") or "aiOS",
        "harness_version": config.get("harness_version") or "",
        "cost_provenance": config.get("cost_provenance") or "provider_reported",
        "task_set_hash": run.get("task_set_hash") or "",
        "task_hash_schema": int(run.get("task_hash_schema") or 0),
        "custom_id": config.get("custom_id") or "",
        "project_campaign_id": config.get("project_campaign_id") or "",
        "project_snapshot_hash": config.get("project_snapshot_hash") or "",
        "project_source_name": config.get("project_source_name") or "",
        "status": run.get("status"),
        "stop_requested": bool(run.get("stop_requested")),
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
        "provider": config.get("provider") if config.get("kind") != "custom" else (
            models[0].get("provider") if models and isinstance(models[0], dict) else config.get("provider")
        ),
        "model": model_label,
        "reasoning": config.get("reasoning"),
        "fast": bool(config.get("fast")),
        "concurrency": config.get("concurrency"),
        "tasks": len(tasks),
        "finished": sum(1 for task in tasks if task.get("passed") is not None),
        "passed": sum(1 for task in tasks if task.get("passed")),
        "score": summary.get("score"),
        "grade": summary.get("grade"),
        "total_tokens": ((summary.get("usage") or {}).get("total_tokens")),
        "cost_usd": summary.get("cost_usd"),
        "total_seconds": summary.get("total_seconds"),
        "tool_calls": summary.get("tool_calls") or 0,
        "files_edited": sum(int(task.get("files_edited") or 0) for task in tasks),
        "lines_added": sum(int(task.get("lines_added") or 0) for task in tasks),
        "lines_deleted": sum(int(task.get("lines_deleted") or 0) for task in tasks),
        "seconds_per_pass": summary.get("seconds_per_pass"),
        "tokens_per_pass": summary.get("tokens_per_pass"),
        "budget": _budget_snapshot(run, summary),
    }


def list_runs(limit: int = 60) -> list[dict]:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for directory in RUNS_DIR.iterdir():
        if not directory.is_dir():
            continue
        payload = _read(directory / "run.json")
        if payload.get("id"):
            rows.append(summarise_run(_with_live_metrics(_settle(payload))))
    rows.sort(key=lambda row: float(row.get("created_at") or 0), reverse=True)
    return rows[: max(1, min(int(limit or 60), 1000))]


def _inferred_group_id(row: dict) -> str:
    explicit = str(row.get("group_id") or "")
    if explicit:
        return explicit
    if str(row.get("kind")) == "custom" and row.get("saved_config_id"):
        stamp = int(float(row.get("created_at") or 0) * 1000)
        custom_id = re.sub(r"[^A-Za-z0-9._-]+", "-", str(row.get("custom_id") or "custom"))
        return f"legacy-{custom_id}-{stamp}"
    return str(row.get("id") or "")


def summarise_run_group(rows: list[dict]) -> dict:
    children = sorted(rows, key=lambda row: int(row.get("group_index") or 0))
    first = children[0]
    active = any(str(row.get("status")) in {"starting", "running"} for row in children)
    stop_requested = active and any(bool(row.get("stop_requested")) for row in children)
    statuses = {str(row.get("status") or "") for row in children}
    status = "stopping" if stop_requested else (
        "running" if active else (statuses.pop() if len(statuses) == 1 else "completed")
    )
    group_id = _inferred_group_id(first)
    label = str(first.get("group_label") or "").strip()
    if not label:
        label = str(first.get("label") or group_id)
        saved_name = str(first.get("saved_config_name") or "")
        if saved_name and label.endswith(f" · {saved_name}"):
            label = label[: -(len(saved_name) + 3)].strip()
    hashes = sorted({str(row.get("task_set_hash") or "") for row in children if row.get("task_set_hash")})
    hash_schemas = sorted({int(row.get("task_hash_schema") or 0) for row in children})
    authoritative_hashes = bool(children) and all(
        _valid_task_set_hash(row.get("task_set_hash"))
        and int(row.get("task_hash_schema") or 0) == TASK_SET_HASH_SCHEMA
        for row in children
    )
    budgets = [row.get("budget") or {} for row in children]
    cost_provenances = sorted({str(row.get("cost_provenance") or "provider_reported") for row in children})
    cap = sum(_money(row.get("cap_usd")) for row in budgets)
    spent = sum(_money(row.get("spent_usd")) for row in budgets)
    continued_from = sorted({
        str(row.get("continued_from_group") or "") for row in children
        if row.get("continued_from_group")
    })
    return {
        "id": group_id if len(children) > 1 or first.get("group_id") else first.get("id"),
        "is_group": len(children) > 1 or bool(first.get("group_id")),
        "run_ids": [row.get("id") for row in children],
        "label": label,
        "kind": first.get("kind") or "suite",
        "task_set_hash": hashes[0] if len(hashes) == 1 else "",
        "task_set_hashes": hashes,
        "task_hash_schema": TASK_SET_HASH_SCHEMA if hash_schemas == [TASK_SET_HASH_SCHEMA] else 0,
        "task_hash_schemas": hash_schemas,
        "task_set_authority": "persisted" if authoritative_hashes else "legacy-fallback",
        "comparable": authoritative_hashes and len(hashes) == 1,
        "custom_id": first.get("custom_id") or "",
        "project_campaign_id": first.get("project_campaign_id") or "",
        "project_snapshot_hash": first.get("project_snapshot_hash") or "",
        "project_source_name": first.get("project_source_name") or "",
        "status": status,
        "stop_requested": stop_requested,
        "continued_from_group": continued_from[0] if len(continued_from) == 1 else "",
        "seeded_task_count": sum(int(row.get("seeded_task_count") or 0) for row in children),
        "created_at": min(float(row.get("created_at") or 0) for row in children),
        "updated_at": max(float(row.get("updated_at") or 0) for row in children),
        "tasks": sum(int(row.get("tasks") or 0) for row in children),
        "finished": sum(int(row.get("finished") or 0) for row in children),
        "passed": sum(int(row.get("passed") or 0) for row in children),
        "total_tokens": sum(int(row.get("total_tokens") or 0) for row in children),
        "cost_usd": round(sum(float(row.get("cost_usd") or 0) for row in children), 6),
        "cost_available": all(row.get("cost_usd") is not None for row in children),
        "cost_provenances": cost_provenances,
        "cost_comparable": len([row for row in cost_provenances if row != "unavailable"]) <= 1,
        "total_seconds": max((float(row.get("total_seconds") or 0) for row in children), default=0),
        "tool_calls": sum(int(row.get("tool_calls") or 0) for row in children),
        "files_edited": sum(int(row.get("files_edited") or 0) for row in children),
        "lines_added": sum(int(row.get("lines_added") or 0) for row in children),
        "lines_deleted": sum(int(row.get("lines_deleted") or 0) for row in children),
        "budget": {
            "cap_usd": round(cap, 6),
            "spent_usd": round(spent, 6),
            "remaining_usd": round(max(0.0, cap - spent), 6) if cap else None,
            "exhausted": any(bool(row.get("exhausted")) for row in budgets),
            "enforcement": "observed-soft" if cap else "none",
        },
        "configurations": children,
    }


def list_run_groups(limit: int = 60) -> list[dict]:
    buckets: dict[str, list[dict]] = {}
    for row in list_runs(1000):
        buckets.setdefault(_inferred_group_id(row), []).append(row)
    groups = [summarise_run_group(rows) for rows in buckets.values()]
    groups.sort(key=lambda row: float(row.get("created_at") or 0), reverse=True)
    return groups[: max(1, min(int(limit or 60), 1000))]


def get_run_group(group_id: str) -> dict | None:
    summary = next((row for row in list_run_groups(1000) if str(row.get("id")) == str(group_id)), None)
    if not summary:
        return None
    children = [run for run_id in summary.get("run_ids") or [] if (run := get_run(str(run_id)))]
    group = {**summary, "runs": children}
    try:
        from . import reporting

        group["report"] = reporting.analyze_group(group)
    except Exception as exc:
        group["report"] = {"error": f"could not analyze benchmark group: {exc}"}
    return group


# -------------------------------------------------------------------- config


def normalise_config(raw: dict) -> tuple[dict, str]:
    """Validate what the UI sent. Returns (config, error)."""
    data = raw if isinstance(raw, dict) else {}
    if str(data.get("kind") or "").strip().lower() == "custom":
        return _normalise_custom_config(data)
    if str(data.get("kind") or "").strip().lower() == "project":
        return _normalise_project_config(data)

    engine = str(data.get("engine") or "aios").strip().casefold()
    if engine not in _SUPPORTED_ENGINES:
        return {}, "pick a supported harness"
    provider = str(data.get("provider") or _default_provider(engine)).strip().lower()
    model = str(data.get("model") or "").strip()
    reasoning = str(data.get("reasoning") or ("auto" if engine != "aios" else "")).strip().lower()
    if not provider:
        return {}, "pick a provider"
    if engine == "aios" and not model:
        return {}, "pick an exact model"
    if not reasoning:
        return {}, "pick a reasoning level"

    counts = {}
    raw_counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
    for name, meta in suites.SUITES.items():
        counts[name] = max(0, min(int(meta["max"]), int(raw_counts.get(name) or 0)))
    if not sum(counts.values()):
        return {}, "pick at least one task"

    max_cost = min(MAX_COST_CEILING_USD, _money(data.get("max_cost_usd")))
    config = {
        "kind": "suite",
        "engine": engine,
        "harness_label": str(data.get("harness_label") or ("aiOS" if engine == "aios" else engine.title())).strip()[:80],
        "harness_version": _resolved_harness_version(engine, data.get("harness_version")),
        "cost_provenance": str(data.get("cost_provenance") or _default_cost_provenance(engine)).strip()[:40],
        "provider": provider,
        "model": model,
        "reasoning": reasoning,
        "fast": bool(data.get("fast")),
        "counts": counts,
        # More than a handful of agents at once mostly buys you provider rate
        # limits, and every one of them is writing to the same disk.
        "concurrency": max(1, min(MAX_CONCURRENCY, int(data.get("concurrency") or 3))),
        "timeout": max(60.0, min(MAX_TIMEOUT, float(data.get("timeout") or 600.0))),
        "max_cost_usd": round(max_cost, 6),
        "attempt": max(1, min(3, int(data.get("attempt") or 1))),
        "repetitions": max(1, min(3, int(data.get("repetitions") or 1))),
        # Hand the reviewer's findings back to the agent for one fix pass. It is
        # a property of the harness rather than of the task, and the whole point
        # of having a benchmark is that you can answer "does it help?" with two
        # runs instead of an opinion.
        "review_fix": bool(data.get("review_fix")),
        "strategy": code_roles._clean_strategy(data.get("strategy")),
        # "legacy" restores the pre-2026-08 prompt and verification rules, so
        # "did the rewrite help?" is two runs rather than an opinion.
        "profile": "legacy" if str(data.get("profile") or "").strip().casefold() == "legacy" else "lean",
    }
    if "task_ids" in data:
        config["task_ids"] = _task_ids(data.get("task_ids"))
    return config, ""


def _normalise_project_config(data: dict) -> tuple[dict, str]:
    """A real-project run always points at an immutable campaign snapshot."""
    project, error = project_campaigns.normalise_config(data)
    if error:
        return {}, error
    engine = str(data.get("engine") or "aios").strip().casefold()
    if engine not in _SUPPORTED_ENGINES:
        return {}, "pick a supported harness"
    provider = str(data.get("provider") or _default_provider(engine)).strip().lower()
    model = str(data.get("model") or "").strip()
    reasoning = str(data.get("reasoning") or ("auto" if engine != "aios" else "off")).strip().lower()
    if not provider or not model:
        return {}, "pick an exact provider and model"
    max_cost = min(MAX_COST_CEILING_USD, _money(data.get("max_cost_usd")))
    config = {
        "kind": "project",
        "engine": engine,
        "harness_label": str(data.get("harness_label") or ("aiOS" if engine == "aios" else engine.title())).strip()[:80],
        "harness_version": _resolved_harness_version(engine, data.get("harness_version")),
        "cost_provenance": str(data.get("cost_provenance") or _default_cost_provenance(engine)).strip()[:40],
        "provider": provider,
        "model": model,
        "reasoning": reasoning,
        "fast": bool(data.get("fast")),
        "counts": {},
        "concurrency": 1,
        "timeout": max(60.0, min(CUSTOM_MAX_TIMEOUT, float(data.get("timeout") or 3600.0))),
        "max_cost_usd": round(max_cost, 6),
        "attempt": max(1, min(3, int(data.get("attempt") or 1))),
        "repetitions": max(1, min(3, int(data.get("repetitions") or 1))),
        "review_fix": bool(data.get("review_fix")),
        "strategy": code_roles._clean_strategy(data.get("strategy")),
        "profile": "legacy" if str(data.get("profile") or "").strip().casefold() == "legacy" else "lean",
        **project,
    }
    return config, ""


def _normalise_custom_config(data: dict) -> tuple[dict, str]:
    """A custom run is a saved prompt pointed at one or more models."""
    custom_id = str(data.get("custom_id") or "").strip()
    definition = custom.get_definition(custom_id) if custom_id else None
    # Prefer the live definition so "run again" picks up edits; fall back to a
    # prompt snapshot only when the caller is replaying without the id.
    prompt = str((definition or {}).get("prompt") or data.get("prompt") or "").strip()
    custom_tasks = ((definition or {}).get("tasks")
                    if isinstance((definition or {}).get("tasks"), list)
                    else data.get("custom_tasks"))
    name = str((definition or {}).get("name") or data.get("custom_name") or custom_id or "custom").strip()[:80]
    if not prompt and not custom_tasks:
        return {}, "write a prompt, add a task, or pick a saved custom test"
    if not custom_id and not definition:
        # Anonymous one-shot is allowed: the prompt travels in the run config.
        custom_id = ""

    models, error = custom.normalise_models(data.get("models"))
    if error:
        # Single-model form fields still work for a quick custom attempt.
        provider = str(data.get("provider") or "").strip().lower()
        model = str(data.get("model") or "").strip()
        reasoning = str(data.get("reasoning") or "").strip().lower()
        if provider and model and reasoning:
            models = [{
                "provider": provider,
                "model": model,
                "reasoning": reasoning,
                "fast": bool(data.get("fast")),
            }]
        else:
            return {}, error

    engine = str(data.get("engine") or "aios").strip().casefold()
    if engine not in _SUPPORTED_ENGINES:
        return {}, "pick a supported harness"
    primary = models[0]
    max_cost = min(MAX_COST_CEILING_USD, _money(data.get("max_cost_usd")))
    config = {
        "kind": "custom",
        "engine": engine,
        "harness_label": str(data.get("harness_label") or ("aiOS" if engine == "aios" else engine.title())).strip()[:80],
        "harness_version": _resolved_harness_version(engine, data.get("harness_version")),
        "cost_provenance": str(data.get("cost_provenance") or _default_cost_provenance(engine)).strip()[:40],
        "custom_id": custom_id,
        "custom_name": name,
        "prompt": prompt,
        "custom_title": str((definition or {}).get("title") or name).strip()[:120],
        "custom_info": str((definition or {}).get("info") or "").strip()[:2000],
        "custom_tasks": custom._normalise_tasks(custom_tasks),
        "models": models,
        # Kept for summarise_run / older UI paths that expect one harness.
        "provider": primary["provider"],
        "model": primary["model"],
        "reasoning": primary["reasoning"],
        "fast": bool(primary.get("fast")),
        "counts": {},
        "concurrency": max(1, min(MAX_CONCURRENCY, int(data.get("concurrency") or 3))),
        "timeout": max(60.0, min(CUSTOM_MAX_TIMEOUT, float(data.get("timeout") or 3600.0))),
        "max_cost_usd": round(max_cost, 6),
        "attempt": max(1, min(3, int(data.get("attempt") or 1))),
        "repetitions": max(1, min(3, int(data.get("repetitions") or 1))),
        "review_fix": bool(data.get("review_fix")),
        "strategy": code_roles._clean_strategy(data.get("strategy")),
        "profile": "legacy" if str(data.get("profile") or "").strip().casefold() == "legacy" else "lean",
    }
    if "task_ids" in data:
        config["task_ids"] = _task_ids(data.get("task_ids"))
    return config, ""


def select_tasks(config: dict) -> list:
    """Resolve the Task objects a run will execute."""
    if config.get("kind") == "custom":
        selected = custom.tasks_for_config(config)
    elif config.get("kind") == "project":
        selected = [project_campaigns.task_for_config(config)]
    else:
        selected = suites.select(config.get("counts") or {})
    if "task_ids" not in config:
        return selected
    by_id = {str(task.id): task for task in selected}
    requested = _task_ids(config.get("task_ids"))
    missing = [task_id for task_id in requested if task_id not in by_id]
    if missing:
        raise ValueError(f"unknown task id in continuation: {missing[0]}")
    return [by_id[task_id] for task_id in requested]


# ------------------------------------------------------------------ lifecycle


def create_run(
    raw_config: dict,
    label: str = "",
    saved_config_id: str = "",
    saved_config_name: str = "",
    saved_config_roles: dict | None = None,
    *,
    group_id: str = "",
    group_label: str = "",
    group_index: int = 0,
    group_size: int = 0,
    prepared_tasks: list | None = None,
    seeded_results: dict[str, dict] | None = None,
    continued_from_group: str = "",
    continued_from_run: str = "",
) -> dict:
    config, error = normalise_config(raw_config)
    if error:
        return {"ok": False, "error": error}

    if prepared_tasks is None:
        try:
            tasks = select_tasks(config)
        except Exception as exc:  # a missing public fixture must say so plainly
            return {"ok": False, "error": f"could not prepare tasks: {exc}"}
    else:
        # A fixed-suite group resolves public fixtures once before starting any
        # child.  Copy the snapshot so callers cannot mutate this run's view.
        tasks = list(prepared_tasks)
    if not tasks:
        return {"ok": False, "error": "no tasks matched that selection"}

    fixture_hashes = {str(task.id): _task_fixture_hash(task) for task in tasks}
    seeds: dict[str, dict] = {}
    for task_id, result in (seeded_results or {}).items():
        task_id = str(task_id)
        if task_id not in fixture_hashes:
            return {"ok": False, "error": f"seeded result has unknown task id: {task_id}"}
        if not isinstance(result, dict) or result.get("passed") is not True:
            return {"ok": False, "error": f"only passing results can be carried forward: {task_id}"}
        if str(result.get("fixture_hash") or "") != fixture_hashes[task_id]:
            return {"ok": False, "error": f"seeded result fixture changed: {task_id}"}
        seeds[task_id] = result

    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    directory = run_dir(run_id)
    # code_jobs does JOBS_DIR.mkdir(exist_ok=True) with no parents, so the
    # parent has to exist before the runner imports it.
    (directory / "jobs").mkdir(parents=True, exist_ok=True)
    (directory / "work").mkdir(parents=True, exist_ok=True)

    if config.get("kind") in {"custom", "project"} and not label:
        label = str(config.get("custom_name") or config.get("project_source_name") or "custom")[:60]

    agent_id = preview_port = 0
    if config.get("kind") in {"custom", "project"}:
        try:
            agent_id, preview_port = _benchmark_identity()
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc)}

    roles = code_roles.save_roles(saved_config_roles, {}) if isinstance(saved_config_roles, dict) else {}
    run = {
        "id": run_id,
        "label": str(label or "").strip()[:60],
        "saved_config_id": str(saved_config_id or "").strip(),
        "saved_config_name": str(saved_config_name or "").strip()[:60] if saved_config_name else "",
        "saved_config_roles": roles,
        "agent_id": agent_id,
        "preview_port": preview_port,
        "group_id": str(group_id or "").strip(),
        "group_label": str(group_label or "").strip()[:80],
        "group_index": max(0, int(group_index or 0)),
        "group_size": max(0, int(group_size or 0)),
        "continued_from_group": str(continued_from_group or "").strip(),
        "continued_from_run": str(continued_from_run or "").strip(),
        "continuation_mode": "parallel_unfinished" if continued_from_group else "",
        "seeded_task_count": len(seeds),
        "repetition": max(1, int(config.get("attempt") or 1)),
        "status": "starting",
        "created_at": round(time.time(), 3),
        "updated_at": round(time.time(), 3),
        "pid": 0,
        "error": "",
        "task_set_hash": _task_set_hash(tasks),
        "task_hash_schema": TASK_SET_HASH_SCHEMA,
        "config": config,
        "tasks": [
            {
                "id": task.id,
                "suite": task.suite,
                "title": task.title,
                "fixture_hash": _task_fixture_hash(task),
                "fixture_hash_schema": TASK_SET_HASH_SCHEMA,
                "engine": config.get("engine") or "aios",
                "cost_provenance": config.get("cost_provenance") or "provider_reported",
                "benchmark_origin": str((getattr(task, "provenance", {}) or {}).get("benchmark") or "aiOS"),
                "source_url": str(getattr(task, "source", "") or ""),
                "official": bool(getattr(task, "source", "")),
                "leaderboard_comparable": bool((getattr(task, "provenance", {}) or {}).get("leaderboard_comparable", False)),
                "language": str((getattr(task, "provenance", {}) or {}).get("language") or ""),
                "provenance": dict(getattr(task, "provenance", {}) or {}),
                "status": "pending",
                "job_id": "",
                "agent_id": agent_id,
                "preview_port": preview_port,
                "passed": None,
                "error": "",
                "seconds": 0.0,
                "usage": {},
                "role_usage": {},
                "pipeline_stages": {},
                "tool_calls": 0,
                "efficiency_trace": {},
                "model_request_count": None,
                "model_request_count_source": "unavailable",
                "model_request_rounds": [],
                "model_request_rounds_omitted": 0,
                "checks": [],
                "review": "",
                "started_at": 0.0,
                "finished_at": 0.0,
                "provider": task.provider or config.get("provider") or "",
                "model": task.model or config.get("model") or "",
                "native_primary_model": "",
                "native_models_used": [],
                "reasoning": task.reasoning or config.get("reasoning") or "",
                "fast": bool(task.fast if task.provider else config.get("fast")),
            }
            for task in tasks
        ],
        "summary": scoring.summarise([]),
    }
    carried_fields = {
        "seconds", "usage", "role_usage", "pipeline_stages", "tool_calls", "efficiency_trace",
        "model_request_count", "model_request_count_source", "model_request_rounds",
        "model_request_rounds_omitted",
        "checks", "review", "started_at", "finished_at", "provider", "model",
        "native_primary_model", "native_models_used", "reasoning", "fast",
        "cost_provenance", "job_status", "agent_error", "files_edited",
        "lines_added", "lines_deleted", "events", "error",
    }
    for task in run["tasks"]:
        source = seeds.get(str(task["id"]))
        if not source:
            continue
        for field in carried_fields:
            if field in source:
                task[field] = copy.deepcopy(source[field])
        task.update({
            "status": "passed",
            "passed": True,
            # The transcript stays in the immutable source run. Reusing its
            # job id here would point the UI at a job directory that does not
            # exist in this continuation.
            "job_id": "",
            "seeded": True,
            "seeded_from_run": str(continued_from_run or ""),
            "seeded_job_id": str(source.get("job_id") or ""),
        })
    run["summary"] = scoring.summarise(run["tasks"])
    run["budget"] = _budget_snapshot(run, run["summary"])
    write_run(run)

    started = _spawn(directory, config, roles=roles)
    if not started.get("ok"):
        run["status"] = "failed"
        run["error"] = started.get("error") or "could not start the runner"
        write_run(run)
        return {"ok": False, "error": run["error"], "run": run}

    run["pid"] = started["pid"]
    run["status"] = "running"
    write_run(run)
    return {"ok": True, "run": run}


def create_run_group(raw_config: dict, configurations: list[dict], label: str = "") -> dict:
    """Start a paired custom or fixed-suite campaign in isolated child runs."""
    # Keep native discovery lazy: adapters imports pc_cli_runner and probes the
    # installed CLIs, while ordinary aiOS-only runs should not pay for either.
    from . import adapters

    base = dict(raw_config if isinstance(raw_config, dict) else {})
    requested_kind = str(base.get("kind") or "").casefold()
    kind = requested_kind if requested_kind in {"custom", "project"} else "suite"
    rows = configurations if isinstance(configurations, list) else []
    has_native = any(
        isinstance(row, dict) and str(row.get("engine") or "aios").strip().casefold() != "aios"
        for row in rows[:24]
    )
    native_catalogue = ({
        str(row.get("id") or "").strip().casefold(): row
        for row in adapters.catalogue()
        if isinstance(row, dict) and str(row.get("id") or "").strip()
    } if has_native else {})
    cleaned = []
    for raw in rows[:24]:
        if not isinstance(raw, dict):
            continue
        engine = str(raw.get("engine") or "aios").strip().casefold()
        if engine not in _SUPPORTED_ENGINES:
            continue
        roles = (code_roles.save_roles(
            raw.get("roles") if isinstance(raw.get("roles"), dict) else {}, {}
        ) if engine == "aios" else {})
        config_id = str(raw.get("id") or "").strip()
        name = str(raw.get("name") or "").strip()[:80]
        if config_id and name:
            native = {}
            if engine != "aios":
                native = native_catalogue.get(engine) or {}
                if not native.get("ready"):
                    auth = str(native.get("auth") or "not available").replace("_", " ")
                    return {"ok": False, "error": f"{name} is not ready ({auth})"}
            coder = roles.get("coder") if isinstance(roles, dict) else {}
            cleaned.append({
                "id": config_id,
                "name": name,
                "engine": engine,
                "provider": str(raw.get("provider") or native.get("default_provider") or (
                    "openrouter" if engine == "aios" else engine
                )).strip().casefold(),
                "model": str(raw.get("model") or native.get("default_model") or
                             (coder or {}).get("model") or "").strip(),
                "reasoning": str(raw.get("reasoning") or native.get("default_reasoning") or
                                 (coder or {}).get("reasoning") or (
                    "auto" if engine != "aios" else "off"
                )).strip().casefold(),
                "fast": bool(raw.get("fast") if "fast" in raw else (coder or {}).get("fast")),
                "review_fix": bool(raw.get("review_fix")),
                "strategy": code_roles._clean_strategy(raw.get("strategy")),
                "harness_version": _resolved_harness_version(
                    engine, raw.get("harness_version") or native.get("version")
                ),
                "cost_provenance": str(
                    raw.get("cost_provenance")
                    or native.get("cost_provenance")
                    or _default_cost_provenance(engine)
                ).strip()[:40],
                "roles": roles,
            })
    if not cleaned:
        return {"ok": False, "error": "select at least one benchmark harness"}
    if kind == "suite" and len(cleaned) < 2:
        return {"ok": False, "error": "select at least two harnesses for a fair comparison"}
    for row in cleaned:
        if row["engine"] == "aios" and not row["model"]:
            return {"ok": False, "error": f"{row['name']} has no model configured"}
        if row["engine"] != "aios" and not row["model"]:
            return {"ok": False, "error": f"{row['name']} requires an exact model"}
    # Preflight every aiOS selection before fixture preparation or the first
    # child is created.  The runner ultimately calls code_jobs.create_job,
    # which enforces this same contract, but discovering a stale/disabled
    # model there turns one bad campaign choice into N failed task processes.
    for row in cleaned:
        if row["engine"] != "aios":
            continue
        try:
            invalid = _aios_selection_error(
                row["provider"], row["model"], row["reasoning"], bool(row["fast"]),
            )
        except Exception as exc:
            return {"ok": False, "error": f"Could not validate {row['name']}: {exc}"}
        if invalid:
            detail = str(invalid.get("error") or "the selected model configuration is unavailable")
            return {"ok": False, "error": f"{row['name']} is not runnable: {detail}"}

    repetitions = max(1, min(3, int(base.get("repetitions") or 1)))
    attempts = [(saved, repeat) for repeat in range(1, repetitions + 1) for saved in cleaned]
    total = len(attempts)
    group_cap = min(MAX_COST_CEILING_USD, _money(base.get("max_cost_usd")))
    openrouter_indices = [
        index for index, (saved, _repeat) in enumerate(attempts)
        if saved["provider"] == "openrouter"
    ]
    openrouter_caps = dict(zip(
        openrouter_indices,
        _split_cost_cap(group_cap, len(openrouter_indices)),
    ))
    native_cap = min(MAX_COST_CEILING_USD, _money(base.get("native_max_cost_usd")))

    def attempt_config(index: int, saved: dict, repeat: int) -> dict:
        config = dict(base)
        config.update({
            "kind": kind,
            "engine": saved["engine"],
            "harness_label": saved["name"],
            "harness_version": saved["harness_version"],
            "cost_provenance": saved["cost_provenance"],
            "provider": saved["provider"],
            "model": saved["model"],
            "reasoning": saved["reasoning"],
            "fast": saved["fast"],
            "concurrency": max(1, min(MAX_CONCURRENCY, int(base.get("concurrency") or 3))),
            "review_fix": saved["review_fix"],
            "strategy": saved["strategy"],
            "max_cost_usd": (openrouter_caps.get(index, 0.0)
                             if saved["provider"] == "openrouter"
                             else native_cap if saved["engine"] != "aios"
                             and saved["cost_provenance"] != "unavailable" else 0.0),
            "repetitions": repetitions,
            "attempt": repeat,
        })
        if kind == "custom":
            config["models"] = [{
                "provider": saved["provider"],
                "model": saved["model"],
                "reasoning": saved["reasoning"],
                "fast": saved["fast"],
            }]
        return config

    # Public fixture preparation may download and verify pinned artifacts.  Do
    # it once, before spawning any child, so an integrity error cannot leave a
    # half-started comparison group.  Custom tasks remain per-harness because
    # their Task objects intentionally carry the selected model/provider.
    prepared_tasks = None
    if kind == "suite":
        preflight_config, error = normalise_config(attempt_config(0, *attempts[0]))
        if error:
            return {"ok": False, "error": error}
        try:
            prepared_tasks = select_tasks(preflight_config)
        except Exception as exc:
            return {"ok": False, "error": f"could not prepare tasks: {exc}"}
        if not prepared_tasks:
            return {"ok": False, "error": "no tasks matched that selection"}

    group_id = f"group-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    default_label = ("Custom benchmark" if kind == "custom" else
                     "Project benchmark" if kind == "project" else "Fair benchmark campaign")
    group_label = str(label or default_label).strip()[:80]
    created = []
    errors = []
    for index, (saved, repeat) in enumerate(attempts):
        config = attempt_config(index, saved, repeat)
        attempt_name = saved["name"] if repetitions == 1 else f"{saved['name']} r{repeat}"
        result = create_run(
            config,
            label=f"{group_label} · {attempt_name}",
            saved_config_id=saved["id"],
            saved_config_name=attempt_name,
            saved_config_roles=saved["roles"] if saved["engine"] == "aios" else None,
            group_id=group_id,
            group_label=group_label,
            group_index=index,
            group_size=total,
            prepared_tasks=prepared_tasks,
        )
        if result.get("ok"):
            created.append(result["run"])
        else:
            errors.append({"configuration": attempt_name, "error": result.get("error") or "could not start"})
    if not created:
        return {"ok": False, "error": errors[0]["error"] if errors else "could not start the group", "errors": errors}
    return {"ok": True, "group": get_run_group(group_id), "errors": errors}


def continue_run_group_parallel(group_id: str, concurrency: int = 4) -> dict:
    """Create an auditable merged group that runs only unfinished suite tasks.

    Passing results are copied into the new report only when their persisted
    fixture fingerprint still matches the freshly prepared task. The source
    group remains untouched, including failed/interrupted attempts and their
    spend, while every unfinished task gets a fresh isolated workspace.
    """
    source_group = get_run_group(str(group_id or ""))
    if not source_group:
        return {"ok": False, "error": "unknown run group"}
    if str(source_group.get("status") or "") in {"starting", "running", "stopping"}:
        return {"ok": False, "error": "stop the active group before continuing it"}
    if str(source_group.get("kind") or "") != "suite":
        return {"ok": False, "error": "parallel continuation is for fixed-suite groups"}
    if source_group.get("comparable") is not True:
        return {"ok": False, "error": "cannot merge a group without one authoritative task fingerprint"}

    sources = source_group.get("runs") if isinstance(source_group.get("runs"), list) else []
    if not sources:
        return {"ok": False, "error": "the group has no child runs"}
    full_config = dict(sources[0].get("config") or {})
    full_config.pop("task_ids", None)
    try:
        prepared_tasks = select_tasks(full_config)
    except Exception as exc:
        return {"ok": False, "error": f"could not prepare tasks: {exc}"}
    if not prepared_tasks:
        return {"ok": False, "error": "the source group has no tasks"}
    fixture_hashes = {str(task.id): _task_fixture_hash(task) for task in prepared_tasks}
    expected_hash = _task_set_hash(prepared_tasks)
    if expected_hash != str(source_group.get("task_set_hash") or ""):
        return {"ok": False, "error": "task fixtures changed since the source campaign"}

    workers = max(1, min(MAX_CONCURRENCY, int(concurrency or 4)))
    prepared: list[tuple[dict, dict[str, dict], list[str]]] = []
    total_remaining = 0
    for source in sources:
        if str(source.get("task_set_hash") or "") != expected_hash:
            return {"ok": False, "error": f"source task fingerprint changed: {source.get('id') or '?'}"}
        rows = {str(row.get("id") or ""): row for row in (source.get("tasks") or [])}
        if set(rows) != set(fixture_hashes):
            return {"ok": False, "error": f"source task inventory changed: {source.get('id') or '?'}"}
        seeds: dict[str, dict] = {}
        remaining: list[str] = []
        for task in prepared_tasks:
            task_id = str(task.id)
            row = rows[task_id]
            if str(row.get("fixture_hash") or "") != fixture_hashes[task_id]:
                return {"ok": False, "error": f"source fixture changed: {task_id}"}
            if row.get("passed") is True:
                seeds[task_id] = row
            else:
                remaining.append(task_id)
        prepared.append((source, seeds, remaining))
        total_remaining += len(remaining)
    if not total_remaining:
        return {"ok": False, "error": "every task in that group already passed"}

    new_group_id = f"group-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    source_label = str(source_group.get("label") or "Benchmark campaign").strip()
    suffix = " - parallel continuation"
    new_label = f"{source_label[:max(1, 80 - len(suffix))]}{suffix}"[:80]
    created = []
    errors = []
    for index, (source, seeds, remaining) in enumerate(prepared):
        config = dict(source.get("config") or {})
        config.update({"concurrency": workers, "task_ids": remaining})
        saved_name = str(source.get("saved_config_name") or source.get("label") or f"Harness {index + 1}")
        result = create_run(
            config,
            label=f"{new_label} - {saved_name}",
            saved_config_id=str(source.get("saved_config_id") or ""),
            saved_config_name=saved_name,
            saved_config_roles=(source.get("saved_config_roles")
                                if isinstance(source.get("saved_config_roles"), dict) else None),
            group_id=new_group_id,
            group_label=new_label,
            group_index=index,
            group_size=len(prepared),
            prepared_tasks=prepared_tasks,
            seeded_results=seeds,
            continued_from_group=str(source_group.get("id") or ""),
            continued_from_run=str(source.get("id") or ""),
        )
        if result.get("ok"):
            created.append(result["run"])
        else:
            errors.append({
                "configuration": saved_name,
                "error": result.get("error") or "could not start continuation",
            })
    if not created:
        return {"ok": False, "error": errors[0]["error"] if errors else "could not continue the group"}
    return {
        "ok": True,
        "group": get_run_group(new_group_id),
        "errors": errors,
        "continued_from_group": str(source_group.get("id") or ""),
        "seeded_results": sum(len(seeds) for _source, seeds, _remaining in prepared),
        "remaining_tasks": total_remaining,
        "concurrency": workers,
    }


def _spawn(directory: Path, config: dict, *, roles: dict | None = None, command: list[str] | None = None) -> dict:
    environment = os.environ.copy()
    # The whole isolation story in one line.
    environment["AIOS_CODE_JOBS_DIR"] = str(directory / "jobs")
    if roles:
        config_path = directory / "harness-config.json"
        _atomic_json(config_path, {"code_roles": roles})
        environment["AIOS_CODE_CONFIG_PATH"] = str(config_path)
    # The log is a file, not a console, so Python would otherwise encode it with
    # the Windows locale codec and make the run log unreadable at the first
    # middle dot.
    environment["PYTHONIOENCODING"] = "utf-8"
    # Set both ways round, so a run always measures what its own config says
    # rather than inheriting whatever this machine happens to be set to.
    environment["AIOS_CODE_REVIEW_FIX"] = "1" if config.get("review_fix") else "0"
    environment["AIOS_CODE_PROMPT_PROFILE"] = str(config.get("profile") or "lean")
    log = (directory / "runner.log").open("ab")
    argv = command or [sys.executable, "-u", "-m", "bench.runner", "--run", str(directory)]
    try:
        process = subprocess.Popen(
            argv,
            cwd=str(ROOT),
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
    except OSError as exc:
        log.close()
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "pid": process.pid}


def continue_run(run_id: str, *, task_id: str = "", extra_seconds: float | None = None,
                 instruction: str = "") -> dict:
    """Pick up a timed-out/stopped custom task in its existing workspace."""
    run = get_run(run_id)
    if not run:
        return {"ok": False, "error": "unknown run"}
    if str(run.get("status")) in {"running", "starting"}:
        return {"ok": False, "error": "that run is already active"}
    if (run.get("config") or {}).get("kind") != "custom":
        return {"ok": False, "error": "only custom runs can be continued"}

    tasks = run.get("tasks") or []
    target = None
    if task_id:
        target = task_of(run, task_id)
    else:
        for task in tasks:
            if task.get("job_id") and str(task.get("status")) in {"timeout", "stopped", "failed", "interrupted"}:
                target = task
                break
    if not target or not target.get("job_id"):
        return {"ok": False, "error": "no stopped custom task to continue"}

    budget = float(extra_seconds if extra_seconds is not None else CUSTOM_MAX_TIMEOUT)
    budget = max(60.0, min(CUSTOM_MAX_TIMEOUT, budget))
    directory = run_dir(run_id)
    stop_path = directory / "STOP"
    stop_path.unlink(missing_ok=True)

    config = dict(run.get("config") or {})
    config["timeout"] = max(float(config.get("timeout") or 0), budget)
    run["config"] = config
    run["status"] = "starting"
    run["error"] = ""
    run["finished_at"] = 0.0
    for task in run.get("tasks") or []:
        if str(task.get("id")) == str(target["id"]):
            task["status"] = "pending"
            task["passed"] = None
            task["error"] = ""
            task["finished_at"] = 0.0
    write_run(run)

    instruction_path: Path | None = None
    manual_instruction = str(instruction or "").strip()[:16_000]
    if manual_instruction:
        instruction_path = directory / f"continue-{uuid.uuid4().hex[:10]}.txt"
        instruction_path.write_text(manual_instruction, encoding="utf-8")

    command = [
        sys.executable, "-u", "-m", "bench.continue_task",
        "--run", str(directory),
        "--task", str(target["id"]),
        "--extra-seconds", str(budget),
    ]
    if instruction_path is not None:
        command.extend(["--instruction-file", str(instruction_path)])

    started = _spawn(
        directory,
        config,
        roles=run.get("saved_config_roles") if isinstance(run.get("saved_config_roles"), dict) else None,
        command=command,
    )
    if not started.get("ok"):
        if instruction_path is not None:
            instruction_path.unlink(missing_ok=True)
        run["status"] = "failed"
        run["error"] = started.get("error") or "could not start the continuer"
        write_run(run)
        return {"ok": False, "error": run["error"], "run": run}

    run["pid"] = started["pid"]
    run["status"] = "running"
    write_run(run)
    return {"ok": True, "run": run, "task_id": target["id"], "extra_seconds": budget,
            "manual_fix": bool(manual_instruction)}


def stop_run(run_id: str) -> dict:
    run = get_run(run_id)
    if not run:
        return {"ok": False, "error": "unknown run"}
    if str(run.get("status")) not in {"running", "starting"}:
        return {"ok": True, "run": run}
    # Ask first. The runner stops its agents, records what it has, and writes a
    # final summary -- a killed process would leave the run looking crashed and
    # throw away results that were already paid for.
    (run_dir(run_id) / "STOP").write_text(str(time.time()), encoding="utf-8")
    return {"ok": True, "stopping": True}


def stop_target(target_id: str) -> dict:
    """Stop one run or every child in a visible comparison group."""
    if get_run(target_id):
        return stop_run(target_id)
    group = get_run_group(target_id)
    if not group:
        return {"ok": False, "error": "unknown run or run group"}
    results = [stop_run(str(run_id)) for run_id in group.get("run_ids") or []]
    errors = [row.get("error") for row in results if not row.get("ok")]
    if errors:
        return {"ok": False, "error": str(errors[0])}
    return {
        "ok": True,
        "stopping": any(bool(row.get("stopping")) for row in results),
        "run_ids": list(group.get("run_ids") or []),
    }


def delete_run(run_id: str) -> dict:
    run = get_run(run_id)
    if not run:
        return {"ok": False, "error": "unknown run"}
    if str(run.get("status")) in {"running", "starting"}:
        return {"ok": False, "error": "stop the run before deleting it"}
    try:
        shutil.rmtree(run_dir(run_id), onexc=_force_remove)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True}


def _force_remove(func, path, _exc) -> None:
    """Every workspace is a git repo, and git marks its objects read-only.

    On Windows that makes plain rmtree fail with "access is denied" on the
    first `.git/objects` entry, so deleting a run from the UI never worked.
    Clear the read-only bit and try the one operation again.
    """
    os.chmod(path, stat.S_IWRITE)
    func(path)


# ----------------------------------------------------------------- monitoring


def task_of(run: dict, task_id: str) -> dict | None:
    for task in run.get("tasks") or []:
        if str(task.get("id")) == str(task_id):
            return task
    return None


def read_task_events(run_id: str, task_id: str, since: int = 0) -> dict:
    """The selected benchmark task's transcript, byte-for-byte like CODE's.

    Same cursor protocol and the same coalescing pass as
    `code_jobs.read_events`, so the CODE transcript renderer drives a benchmark
    session without knowing it is one. The read is direct rather than through
    code_jobs because this process's code_jobs points at the *real* session
    store, and the whole point is that a benchmark never lands there.
    """
    run = get_run(run_id)
    if not run:
        return {"ok": False, "error": "unknown run", "events": [], "size": 0}
    task = task_of(run, task_id)
    if not task:
        return {"ok": False, "error": "unknown task", "events": [], "size": 0}
    job_id = str(task.get("job_id") or "")
    if not job_id:
        return {"ok": True, "events": [], "size": 0, "reset": False, "job": {}, "task": task}

    directory = run_dir(run_id) / "jobs" / job_id
    meta = _read(directory / "job.json")
    events_path = directory / "events.jsonl"
    events: list[dict] = []
    size = 0
    reset = False
    try:
        file_size = events_path.stat().st_size
        if since > file_size:
            since = 0
            reset = True
        start = max(0, since)
        with events_path.open("rb") as handle:
            handle.seek(start)
            raw = handle.read()
        # The runner may be mid-write on the last line; only advance the cursor
        # through complete records so the next read retries the partial tail.
        newline = raw.rfind(b"\n")
        complete = raw[: newline + 1] if newline >= 0 else b""
        size = start + len(complete)
        for line in complete.decode("utf-8", "replace").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    except OSError:
        pass

    try:
        import code_jobs

        events = code_jobs.coalesce_events(events)
    except Exception:
        pass
    return {"ok": True, "events": events, "size": size, "reset": reset,
            "job": meta, "task": task}


def workspace_of(run_id: str, task_id: str) -> Path:
    """Where the agent's repository for one task lives, safe for a shell open."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", str(task_id or ""))
    return run_dir(run_id) / "work" / safe
