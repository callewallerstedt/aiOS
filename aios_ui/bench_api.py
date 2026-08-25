"""Backend for the BENCH page.

Thin on purpose: every decision lives in the `bench` package, which the CLI uses
too, so the page and `python -m bench.run_bench` can never disagree about what a
score means. This module is HTTP shape and push, nothing else.

Note the one thing it deliberately does *not* do: read benchmark sessions
through `code_jobs`. This process's `code_jobs` points at the real session
store, and a benchmark session never lands there -- `bench.runs` reads the run's
own jobs folder directly, with the same cursor protocol, so the CODE transcript
renderer drives it unchanged.
"""

from __future__ import annotations

import json
import time
from typing import Any

from bench import adapters, custom, project_campaigns, runs, scoring, suites

# The run list and the open run are cheap to fingerprint and change rarely; the
# transcript is the thing that has to feel live.
STATE_TICK = 0.5
EVENT_TICK = 0.25
HEARTBEAT = 15.0

ACTIVE_RUN_STATES = {"starting", "running", "stopping"}
ACTIVE_TASK_STATES = {"pending", "running", "verifying"}

CAMPAIGN_DEFAULT_COUNTS = {
    "tweak": 1,
    "bugfix": 1,
    "feature": 0,
    "precision": 0,
    "hard": 0,
    "humaneval": 1,
    "aider_polyglot": 1,
}
MAX_CAMPAIGN_REPETITIONS = 3


def _int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _first(params: dict, name: str, fallback: str = "") -> str:
    return str((params.get(name) or [fallback])[0])


# ------------------------------------------------------------------- request


def dispatch(route: str, method: str, params: dict, data: dict) -> Any:
    """Return None for anything this module does not own."""
    if route == "/api/bench/meta" and method == "GET":
        harnesses = adapters.catalogue()
        return {
            "ok": True,
            "suites": suites.suite_catalogue(),
            "harnesses": harnesses,
            # Kept as an alias for older preview builds of the campaign page.
            "adapters": harnesses,
            "defaults": {
                "counts": dict(suites.DEFAULT_COUNTS),
                "concurrency": 3,
                "timeout": 600,
                "custom_timeout": 3600,
            },
            "campaign_defaults": {
                "counts": dict(CAMPAIGN_DEFAULT_COUNTS),
                "concurrency": 4,
                "max_cost_usd": 0.75,
                "native_max_cost_usd": 0.45,
                "timeout": 600,
                "repetitions": 1,
            },
            "limits": {
                "concurrency": runs.MAX_CONCURRENCY,
                "custom_models": 8,
                "timeout": int(runs.MAX_TIMEOUT),
                "custom_timeout": int(runs.CUSTOM_MAX_TIMEOUT),
                "max_cost_usd": runs.MAX_COST_CEILING_USD,
                "repetitions": MAX_CAMPAIGN_REPETITIONS,
            },
            "scoring": {
                "weights": dict(scoring.WEIGHTS),
                "reference": {
                    "tokens_per_pass": scoring.TOKEN_REFERENCE,
                    "seconds_per_pass": scoring.SECONDS_REFERENCE,
                },
            },
        }

    if route == "/api/bench/runs":
        if method == "GET":
            return {"ok": True, "runs": runs.list_runs(_int(_first(params, "limit", "60"), 60))}
        if method == "POST":
            return runs.create_run(
                data.get("config") or data,
                str(data.get("label") or ""),
                saved_config_id=str(data.get("saved_config_id") or ""),
                saved_config_name=str(data.get("saved_config_name") or ""),
                saved_config_roles=data.get("saved_config_roles") if isinstance(data.get("saved_config_roles"), dict) else None,
            )

    if route == "/api/bench/groups":
        if method == "GET":
            return {"ok": True, "groups": runs.list_run_groups(_int(_first(params, "limit", "60"), 60))}
        if method == "POST":
            return runs.create_run_group(
                data.get("config") or {},
                data.get("configurations") if isinstance(data.get("configurations"), list) else [],
                str(data.get("label") or ""),
            )

    if route == "/api/bench/project-campaign":
        if method == "POST":
            return project_campaigns.create_campaign(
                data.get("config") if isinstance(data.get("config"), dict) else data,
                data.get("configurations") if isinstance(data.get("configurations"), list) else [],
                str(data.get("label") or ""),
            )
        if method == "GET":
            campaign = project_campaigns.get_campaign(_first(params, "id"))
            return {"ok": True, "campaign": campaign} if campaign else {"ok": False, "error": "unknown project campaign"}

    if route == "/api/bench/project/apply-preview" and method == "POST":
        return project_campaigns.preview_apply(
            str(data.get("run_id") or ""), str(data.get("task_id") or ""),
        )

    if route == "/api/bench/project/apply-confirm" and method == "POST":
        return project_campaigns.confirm_apply(
            str(data.get("preview_id") or ""),
            allow_deletions=bool(data.get("allow_deletions")),
        )

    if route == "/api/bench/group" and method == "GET":
        group = runs.get_run_group(_first(params, "id"))
        return {"ok": True, "group": group} if group else {"ok": False, "error": "unknown run group"}

    if route == "/api/bench/run" and method == "GET":
        run = runs.get_run(_first(params, "id"))
        return {"ok": True, "run": run} if run else {"ok": False, "error": "unknown run"}

    if route == "/api/bench/stop" and method == "POST":
        return runs.stop_target(str(data.get("id") or ""))

    if route == "/api/bench/continue" and method == "POST":
        return runs.continue_run(
            str(data.get("id") or ""),
            task_id=str(data.get("task") or ""),
            extra_seconds=(float(data["extra_seconds"]) if data.get("extra_seconds") is not None else None),
            instruction=str(data.get("instruction") or ""),
        )

    if route == "/api/bench/parallel-continue" and method == "POST":
        return runs.continue_run_group_parallel(
            str(data.get("id") or ""),
            concurrency=_int(data.get("concurrency"), 4),
        )

    if route == "/api/bench/delete" and method == "POST":
        return runs.delete_run(str(data.get("id") or ""))

    if route == "/api/bench/runs-by-config" and method == "GET":
        config_id = _first(params, "saved_config_id", "")
        if not config_id:
            return {"ok": False, "error": "missing saved_config_id"}
        all_runs = runs.list_runs(500)
        filtered = [r for r in all_runs if str(r.get("saved_config_id") or "") == str(config_id)]
        return {"ok": True, "runs": filtered}

    if route == "/api/bench/runs-by-custom" and method == "GET":
        custom_id = _first(params, "custom_id", "")
        if not custom_id:
            return {"ok": False, "error": "missing custom_id"}
        all_runs = runs.list_runs(500)
        filtered = [r for r in all_runs if str(r.get("custom_id") or (r.get("config") or {}).get("custom_id") or "") == str(custom_id)]
        return {"ok": True, "runs": filtered}

    if route == "/api/bench/custom":
        if method == "GET":
            return {"ok": True, "definitions": custom.list_definitions(_int(_first(params, "limit", "100"), 100))}
        if method == "POST":
            return custom.create_definition(data)

    if route == "/api/bench/custom/get" and method == "GET":
        definition = custom.get_definition(_first(params, "id"))
        return {"ok": True, "definition": definition} if definition else {"ok": False, "error": "unknown custom test"}

    if route == "/api/bench/custom/update" and method == "POST":
        return custom.update_definition(str(data.get("id") or ""), data)

    if route == "/api/bench/custom/delete" and method == "POST":
        return custom.delete_definition(str(data.get("id") or ""))

    # Task ids carry a slash (`bugfix/rounding`), so they travel as a query
    # parameter rather than a path segment. Escaping them into the path means
    # every proxy and every http.server in the chain has to agree about %2F.
    if route == "/api/bench/events" and method == "GET":
        return runs.read_task_events(
            _first(params, "run"), _first(params, "task"), _int(_first(params, "since", "0"), 0)
        )

    return None


# --------------------------------------------------------------------- push


def _state(run_id: str, group_id: str = "") -> dict:
    payload: dict[str, Any] = {"runs": runs.list_run_groups(60)}
    if run_id:
        payload["run"] = runs.get_run(run_id)
    if group_id:
        payload["group"] = runs.get_run_group(group_id)
    return payload


def stream_state(handler, params: dict, sse) -> None:
    """Push the run list and the open run whenever either actually changes."""
    run_id = _first(params, "run")
    group_id = _first(params, "group")
    signature = None
    last_beat = time.monotonic()
    while not handler.server.stopping:
        try:
            payload = _state(run_id, group_id)
        except Exception as exc:  # a half-written run.json must not kill the stream
            sse(handler, "error", {"error": str(exc)})
            time.sleep(2.0)
            continue
        fingerprint = json.dumps(payload, default=str, sort_keys=True)
        if fingerprint != signature:
            signature = fingerprint
            sse(handler, "state", payload)
        now = time.monotonic()
        if now - last_beat > HEARTBEAT:
            last_beat = now
            sse(handler, "ping", {"t": now})
        run = payload.get("run") or {}
        group = payload.get("group") or {}
        busy = (str(run.get("status") or "") in ACTIVE_RUN_STATES
                or str(group.get("status") or "") in ACTIVE_RUN_STATES
                or any(str(row.get("status")) in ACTIVE_TASK_STATES for row in (run.get("tasks") or [])))
        time.sleep(STATE_TICK if busy else 1.5)


def stream_events(handler, params: dict, sse) -> None:
    """Push one benchmark task's transcript, exactly like a CODE session's."""
    run_id = _first(params, "run")
    task_id = _first(params, "task")
    since = _int(_first(params, "since", "0"), 0)
    if not run_id or not task_id:
        sse(handler, "error", {"error": "missing run or task"})
        return

    last_status = None
    last_beat = time.monotonic()
    while not handler.server.stopping:
        result = runs.read_task_events(run_id, task_id, since)
        if not result.get("ok"):
            sse(handler, "error", {"error": result.get("error") or "unknown benchmark task"})
            return
        if result.get("reset"):
            sse(handler, "reset", {})
            since = 0
        events = result.get("events") or []
        if events:
            sse(handler, "events", {"events": events, "size": result.get("size") or since,
                                    "job": result.get("job") or {}, "task": result.get("task") or {}})
            since = _int(result.get("size"), since)
        task = result.get("task") or {}
        status = str(task.get("status") or "")
        if status != last_status:
            last_status = status
            sse(handler, "task", {"task": task, "job": result.get("job") or {}})
        now = time.monotonic()
        if now - last_beat > HEARTBEAT:
            last_beat = now
            sse(handler, "ping", {"t": now})
        time.sleep(EVENT_TICK if status in ACTIVE_TASK_STATES else 1.0)
