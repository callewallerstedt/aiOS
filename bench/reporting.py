"""Deterministic, offline analysis of a BENCH comparison group.

The analyzer consumes the dictionary returned by ``bench.runs.get_run_group``.
It deliberately has no runner or provider imports: historical and live snapshots
produce the same report shape without starting work or looking up mutable state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 2
AUTHORITATIVE_TASK_HASH_SCHEMA = 2
SUITE_ORDER = (
    "tweak",
    "bugfix",
    "feature",
    "precision",
    "hard",
    "humaneval",
    "aider-polyglot",
    "aider-refactor",
)
FAILURE_CATEGORIES = (
    "timeout",
    "tool-error",
    "provider-error",
    "setup-error",
    "stopped",
    "verification",
    "failed",
)

SUITE_GUIDANCE = {
    "tweak": "Use the direct path and tighten file localization; small edits should not pay for broad exploration.",
    "bugfix": "Reproduce the failure first, isolate the root cause, and rerun the narrow regression test after editing.",
    "feature": "Strengthen cross-file planning and verify the complete integration path, not only the new function.",
    "precision": "Extract every contract edge case before editing and verify the result against that checklist.",
    "hard": "Improve iterative tool use, state tracking, and evidence-based verification for long dependency chains.",
    "humaneval": "Add compact algorithmic edge-case checks before declaring the implementation complete.",
    "aider-polyglot": "Detect the repository language and use its native build and test commands before completion.",
    "aider-refactor": "Preserve complete method bodies during large-file extraction and verify AST structure before completion.",
}

_ACTIVE_STATUSES = frozenset({"starting", "queued", "pending", "running", "verifying", "stopping"})
_SUITE_ALIASES = {
    "human-eval": "humaneval",
    "human_eval": "humaneval",
    "aider_polyglot": "aider-polyglot",
    "aiderpolyglot": "aider-polyglot",
    "aider_refactor": "aider-refactor",
    "aiderrefactor": "aider-refactor",
}


def _suite(value: Any) -> str:
    name = str(value or "unknown").strip().casefold().replace(" ", "-") or "unknown"
    return _SUITE_ALIASES.get(name, name)


def _number(value: Any, *, integer: bool = False) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in {float("inf"), float("-inf")}:
        return None
    if integer:
        return int(result)
    return result


def _median(values: list[int | float]) -> int | float | None:
    if not values:
        return None
    value = statistics.median(values)
    if float(value).is_integer():
        return int(value)
    return round(float(value), 10)


def _stable_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _harness_id(run: Mapping[str, Any], index: int) -> str:
    config = run.get("config") if isinstance(run.get("config"), Mapping) else {}
    for value in (
        run.get("harness_id"),
        run.get("saved_config_id"),
        config.get("harness_id"),
        run.get("saved_config_name"),
        config.get("config_id"),
        run.get("id"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return f"harness-{index + 1}"


def _harness_name(run: Mapping[str, Any], harness_id: str) -> str:
    return str(
        run.get("saved_config_name")
        or run.get("harness_name")
        or run.get("label")
        or harness_id
    ).strip()


def _custom_task_definition(run: Mapping[str, Any], index: int) -> tuple[str, str]:
    config = run.get("config") if isinstance(run.get("config"), Mapping) else {}
    definitions = config.get("custom_tasks") if isinstance(config.get("custom_tasks"), list) else []
    definitions = [row for row in definitions if isinstance(row, Mapping)]
    if definitions:
        row = definitions[index % len(definitions)]
        task_id = str(row.get("id") or f"task-{index % len(definitions) + 1}").strip()
        return f"custom/{task_id}", str(row.get("prompt") or "")
    return "custom/prompt", str(config.get("prompt") or "")


def _task_identity(run: Mapping[str, Any], task: Mapping[str, Any], index: int) -> tuple[str, dict[str, str]]:
    suite = _suite(task.get("suite"))
    for field in ("logical_task_id", "benchmark_task_id", "source_task_id", "case_id"):
        value = str(task.get(field) or "").strip()
        if value:
            logical_id = value
            break
    else:
        if suite == "custom":
            logical_id, prompt = _custom_task_definition(run, index)
            descriptor = {"id": logical_id, "suite": suite}
            if prompt:
                descriptor["prompt_sha256"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            return logical_id, descriptor
        logical_id = str(task.get("id") or f"{suite}/{index + 1}").strip()

    descriptor = {"id": logical_id, "suite": suite}
    fingerprint = str(task.get("fixture_hash") or task.get("brief_hash") or "").strip()
    if fingerprint:
        descriptor["fixture_hash"] = fingerprint
    return logical_id, descriptor


def _model_for(run: Mapping[str, Any], task: Mapping[str, Any]) -> str:
    value = str(task.get("native_primary_model") or task.get("model") or "").strip()
    if value:
        return value
    roles = run.get("saved_config_roles") if isinstance(run.get("saved_config_roles"), Mapping) else {}
    coder = roles.get("coder") if isinstance(roles.get("coder"), Mapping) else {}
    value = str(coder.get("model") or "").strip()
    if value:
        return value
    config = run.get("config") if isinstance(run.get("config"), Mapping) else {}
    return str(config.get("model") or run.get("model") or "").strip()


def _engine_for(run: Mapping[str, Any]) -> str:
    config = run.get("config") if isinstance(run.get("config"), Mapping) else {}
    return str(config.get("engine") or "aios").strip().casefold() or "aios"


def _active_aios_role_signature(run: Mapping[str, Any]) -> tuple[tuple[str, str, str, str, bool], ...]:
    """Every enabled role setting that can change an aiOS campaign result."""
    if _engine_for(run) != "aios":
        return ()
    config = run.get("config") if isinstance(run.get("config"), Mapping) else {}
    provider = str(config.get("provider") or "").strip()
    roles = run.get("saved_config_roles") if isinstance(run.get("saved_config_roles"), Mapping) else {}
    signature = []
    for name in sorted(roles):
        row = roles.get(name) if isinstance(roles.get(name), Mapping) else {}
        if row.get("enabled") is False:
            continue
        signature.append((
            str(name),
            str(row.get("provider") or provider),
            str(row.get("model") or ""),
            str(row.get("reasoning") or ""),
            bool(row.get("fast")),
        ))
    if signature:
        return tuple(signature)
    # Legacy/synthetic run snapshots predate saved_config_roles.  Preserve a
    # useful coder-only signature, but only for an all-aiOS comparison.
    return ((
        "coder",
        provider,
        str(config.get("model") or run.get("model") or ""),
        str(config.get("reasoning") or ""),
        bool(config.get("fast")),
    ),)


def _failure_category(task: Mapping[str, Any]) -> str | None:
    passed = task.get("passed")
    status = str(task.get("status") or "").strip().casefold()
    if passed is True or (passed is None and status in _ACTIVE_STATUSES):
        return None
    text = f"{status} {task.get('error') or ''}".casefold()
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if any(token in text for token in (
        "tool error", "tool failed", "tool call failed", "shell error",
        "command failed", "run_shell", "apply_patch",
    )):
        return "tool-error"
    if any(token in text for token in (
        "provider error", "api error", "rate limit", "429", "unauthorized",
        "authentication", "service unavailable", "bad gateway",
    )):
        return "provider-error"
    if any(token in text for token in (
        "could not build", "could not prepare", "could not create",
        "harness refused", "missing workspace", "native adapter failed",
    )):
        return "setup-error"
    if status in {"stopped", "interrupted", "skipped", "cancelled", "canceled"}:
        return "stopped"
    checks = task.get("checks") if isinstance(task.get("checks"), list) else []
    if any(isinstance(row, Mapping) and row.get("passed") is False for row in checks):
        return "verification"
    if "verification" in text or re.search(r"failed\s+\d+\s+of\s+\d+\s+checks", text):
        return "verification"
    if passed is False or status not in _ACTIVE_STATUSES:
        return "failed"
    return None


def _usage_row(task: Mapping[str, Any]) -> dict[str, int | float | None]:
    usage = task.get("usage") if isinstance(task.get("usage"), Mapping) else {}
    cost = _number(usage.get("cost_usd")) if "cost_usd" in usage else None
    if cost is None and "cost_usd" in task:
        cost = _number(task.get("cost_usd"))
    return {
        "input_tokens": _number(usage.get("input_tokens"), integer=True),
        "cached_input_tokens": _number(usage.get("cached_input_tokens"), integer=True),
        "canonical_prompt_tokens": _number(usage.get("canonical_prompt_tokens"), integer=True),
        "output_tokens": _number(usage.get("output_tokens"), integer=True),
        "reasoning_tokens": _number(usage.get("reasoning_tokens"), integer=True),
        "total_tokens": _number(usage.get("total_tokens"), integer=True),
        "cost_usd": cost,
    }


def _attempt(run: Mapping[str, Any], task: Mapping[str, Any], index: int, harness_id: str) -> dict[str, Any]:
    logical_id, _descriptor = _task_identity(run, task, index)
    passed = task.get("passed") if isinstance(task.get("passed"), bool) else None
    usage = _usage_row(task)
    config = run.get("config") if isinstance(run.get("config"), Mapping) else {}
    cost_provenance = str(
        task.get("cost_provenance") or config.get("cost_provenance") or "provider_reported"
    ).strip()
    if cost_provenance == "unavailable":
        usage["cost_usd"] = None
    return {
        "harness_id": harness_id,
        "run_id": str(run.get("id") or ""),
        "repetition": _number(run.get("repetition"), integer=True),
        "logical_task_id": logical_id,
        "task_id": str(task.get("id") or logical_id),
        "suite": _suite(task.get("suite")),
        "title": str(task.get("title") or ""),
        "status": str(task.get("status") or "unknown"),
        "passed": passed,
        "provider": str(task.get("provider") or (run.get("config") or {}).get("provider") or ""),
        "model": _model_for(run, task),
        "reasoning": str(task.get("reasoning") or (run.get("config") or {}).get("reasoning") or ""),
        "fast": bool(task.get("fast")),
        "usage": usage,
        "cost_provenance": cost_provenance,
        "seconds": _number(task.get("seconds")),
        "tool_calls": _number(task.get("tool_calls"), integer=True),
        "model_request_count": (
            _number(task.get("model_request_count"), integer=True)
            if task.get("model_request_count") is not None else None
        ),
        "model_request_count_source": str(task.get("model_request_count_source") or "unavailable"),
        "files_edited": _number(task.get("files_edited"), integer=True),
        "lines_added": _number(task.get("lines_added"), integer=True),
        "lines_deleted": _number(task.get("lines_deleted"), integer=True),
        "failure_category": _failure_category(task),
        "error": str(task.get("error") or ""),
        "role_usage": task.get("role_usage") if isinstance(task.get("role_usage"), Mapping) else {},
    }


def _summarize_attempts(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    evaluated = [row for row in attempts if isinstance(row.get("passed"), bool)]
    passed = sum(row["passed"] is True for row in evaluated)
    costs = [row["usage"]["cost_usd"] for row in attempts if row["usage"]["cost_usd"] is not None]
    tokens = [row["usage"]["total_tokens"] for row in attempts if row["usage"]["total_tokens"] is not None]
    seconds = [row["seconds"] for row in attempts if row["seconds"] is not None]
    tools = [row["tool_calls"] for row in attempts if row["tool_calls"] is not None]
    model_requests = [
        row["model_request_count"] for row in attempts
        if row.get("model_request_count") is not None
        and row.get("model_request_count_source") != "unavailable"
    ]
    model_request_sources = sorted({
        str(row.get("model_request_count_source")) for row in attempts
        if row.get("model_request_count") is not None
        and row.get("model_request_count_source") != "unavailable"
    })
    failures = Counter(row["failure_category"] for row in attempts if row.get("failure_category"))
    cost_complete = bool(attempts) and len(costs) == len(attempts)
    return {
        "attempt_count": len(attempts),
        "evaluated_count": len(evaluated),
        "pending_count": len(attempts) - len(evaluated),
        "passed_count": passed,
        "pass_rate": round(passed / len(evaluated), 6) if evaluated else None,
        "median_tokens": _median(tokens),
        "median_cost_usd": _median(costs) if cost_complete else None,
        "median_seconds": _median(seconds),
        "median_tool_calls": _median(tools),
        "model_request_count_total": sum(model_requests) if model_requests else None,
        "median_model_request_count": _median(model_requests),
        "model_request_count_available": len(model_requests),
        "model_request_count_unavailable": len(attempts) - len(model_requests),
        "model_request_count_sources": model_request_sources,
        "cost_status": "available" if cost_complete else ("unavailable" if attempts else "no-attempts"),
        "cost_reported_attempts": len(costs),
        "failure_categories": {name: failures.get(name, 0) for name in FAILURE_CATEGORIES},
    }


def _run_task_set(run: Mapping[str, Any]) -> tuple[list[dict[str, str]], bool]:
    descriptors = []
    logical_ids = []
    for index, raw in enumerate(run.get("tasks") or []):
        if not isinstance(raw, Mapping):
            continue
        logical_id, descriptor = _task_identity(run, raw, index)
        logical_ids.append(logical_id)
        descriptors.append(descriptor)
    descriptors.sort(key=lambda row: (row["suite"], row["id"], row.get("prompt_sha256", "")))
    return descriptors, len(logical_ids) != len(set(logical_ids))


def _comparability(
    runs: list[Mapping[str, Any]],
    harness_for_run: dict[int, str],
) -> tuple[str, list[str], list[str], str, int]:
    observed: list[tuple[str, list[dict[str, str]]]] = []
    reasons: list[str] = []
    repetitions: dict[str, list[int]] = defaultdict(list)
    persisted_hashes: list[str] = []
    authoritative = bool(runs)
    for index, run in enumerate(runs):
        harness_id = harness_for_run[index]
        task_set, duplicate = _run_task_set(run)
        observed.append((harness_id, task_set))
        if duplicate:
            reasons.append(f"duplicate logical tasks in {harness_id}/{run.get('id') or index + 1}")

        repetition_value = _number(run.get("repetition"))
        if (
            repetition_value is None
            or repetition_value < 1
            or not float(repetition_value).is_integer()
        ):
            reasons.append(f"missing or invalid repetition identity in {harness_id}")
        else:
            repetitions[harness_id].append(int(repetition_value))

        persisted = str(run.get("task_set_hash") or "").strip().casefold()
        schema = _number(run.get("task_hash_schema"), integer=True)
        if not re.fullmatch(r"[0-9a-f]{64}", persisted) or schema != AUTHORITATIVE_TASK_HASH_SCHEMA:
            authoritative = False
        else:
            persisted_hashes.append(persisted)

    unique_sets = sorted({json.dumps(row, sort_keys=True, separators=(",", ":")) for _harness, row in observed})
    fallback_payload: Any = json.loads(unique_sets[0]) if len(unique_sets) == 1 else [json.loads(row) for row in unique_sets]
    fallback_hash = _stable_hash(fallback_payload)
    unique_hashes = sorted(set(persisted_hashes))
    if authoritative and len(persisted_hashes) == len(runs):
        task_set_hash = unique_hashes[0] if len(unique_hashes) == 1 else _stable_hash(unique_hashes)
        authority = "persisted"
        hash_schema = AUTHORITATIVE_TASK_HASH_SCHEMA
    else:
        task_set_hash = fallback_hash
        authority = "legacy-fallback"
        hash_schema = 0
        reasons.append("legacy task identity is non-authoritative")
    task_keys = sorted({row["id"] for _harness, task_set in observed for row in task_set})

    harnesses = sorted(set(harness_for_run.values()))
    if len(harnesses) < 2:
        reasons.append("requires at least two harnesses")
    if not task_keys:
        reasons.append("no benchmark tasks")
    if len(unique_sets) > 1 or len(unique_hashes) > 1:
        reasons.append("task sets differ across harnesses or repetitions")
    repetition_sets = []
    for harness_id in harnesses:
        values = repetitions.get(harness_id) or []
        unique = sorted(set(values))
        if len(unique) != len(values):
            reasons.append(f"duplicate repetition identities in {harness_id}")
        if unique and unique != list(range(1, max(unique) + 1)):
            reasons.append(f"repetition identities are not contiguous from 1 in {harness_id}")
        repetition_sets.append(tuple(unique))
    if len(set(repetition_sets)) > 1:
        reasons.append("repetition identities differ across harnesses")
    return task_set_hash, sorted(set(reasons)), task_keys, authority, hash_schema


def _suite_sort_key(name: str) -> tuple[int, str]:
    try:
        return SUITE_ORDER.index(name), name
    except ValueError:
        return len(SUITE_ORDER), name


def _recommendations(harnesses: list[dict[str, Any]], comparable: bool) -> list[dict[str, Any]]:
    attempts = [row for harness in harnesses for row in harness["attempts"]]
    names = {str(harness.get("id") or ""): str(harness.get("name") or harness.get("id") or "harness")
             for harness in harnesses}
    candidates: list[tuple[int, float, int, dict[str, Any]]] = []
    key_order = {name: index for index, name in enumerate(("timeout", "tool-error", *SUITE_ORDER, "token", "cost"))}

    timeout_count = sum(row.get("failure_category") == "timeout" for row in attempts)
    if timeout_count:
        candidates.append((1, -float(timeout_count), key_order["timeout"], {
            "priority": 1,
            "key": "timeout",
            "signal": f"{timeout_count} timeout failure{'s' if timeout_count != 1 else ''}",
            "action": "Bound exploration earlier, checkpoint progress, and reserve enough time for verification.",
        }))

    tool_errors = sum(row.get("failure_category") == "tool-error" for row in attempts)
    if tool_errors:
        candidates.append((1, -float(tool_errors), key_order["tool-error"], {
            "priority": 1,
            "key": "tool-error",
            "signal": f"{tool_errors} tool error{'s' if tool_errors != 1 else ''}",
            "action": "Harden tool error recovery and require a verified fallback before retrying the same operation.",
        }))

    by_suite: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in attempts:
        by_suite[row["suite"]].append(row)
    for suite in SUITE_ORDER:
        rows = [row for row in by_suite.get(suite, []) if isinstance(row.get("passed"), bool)]
        failed = sum(row["passed"] is False for row in rows)
        if not failed:
            continue
        rate = failed / len(rows)
        candidates.append((2, -rate, key_order[suite], {
            "priority": 2,
            "key": suite,
            "signal": f"{failed}/{len(rows)} evaluated attempts failed",
            "action": SUITE_GUIDANCE[suite],
        }))

    unavailable_cost = [harness["id"] for harness in harnesses if harness["cost_status"] == "unavailable"]
    if unavailable_cost:
        candidates.append((2, -float(len(unavailable_cost)), key_order["cost"], {
            "priority": 2,
            "key": "cost",
            "signal": f"cost unavailable for {len(unavailable_cost)} harness{'es' if len(unavailable_cost) != 1 else ''}",
            "action": "Capture provider-reported cost for every attempt before ranking harness spend.",
        }))

    def relative_metric(field: str, key: str, label: str, action: str) -> None:
        if not comparable:
            return
        measured = [(row["id"], row.get(field)) for row in harnesses if row.get(field) is not None]
        if len(measured) < 2:
            return
        best_id, best = min(measured, key=lambda item: (float(item[1]), item[0]))
        worst_id, worst = max(measured, key=lambda item: (float(item[1]), item[0]))
        if float(worst) <= float(best):
            return
        ratio = float("inf") if float(best) == 0 else float(worst) / float(best)
        if ratio < 1.5:
            return
        ratio_text = ">99x" if ratio == float("inf") or ratio > 99 else f"{ratio:.1f}x"
        candidates.append((3, -min(ratio, 999.0), key_order[key], {
            "priority": 3,
            "key": key,
            "signal": f"{names.get(worst_id, worst_id)} median {label} is {ratio_text} {names.get(best_id, best_id)}",
            "action": action,
        }))

    relative_metric(
        "median_tokens",
        "token",
        "tokens",
        "Reduce repeated context, redundant scouting, and no-progress tool rounds in the least efficient harness.",
    )
    if not unavailable_cost:
        relative_metric(
            "median_cost_usd",
            "cost",
            "cost",
            "Move expensive reasoning to bounded planning or review stages and keep the iterative coder economical.",
        )

    candidates.sort(key=lambda row: (row[0], row[1], row[2], row[3]["signal"]))
    return [row[3] for row in candidates[:8]]


def analyze_group(group: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deterministic JSON-compatible report for one BENCH group."""
    payload = group if isinstance(group, Mapping) else {}
    raw_runs = payload.get("runs") if isinstance(payload.get("runs"), list) else []
    runs = [row for row in raw_runs if isinstance(row, Mapping)]
    runs.sort(key=lambda row: (str(row.get("saved_config_id") or ""), str(row.get("id") or "")))

    harness_for_run: dict[int, str] = {}
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    names: dict[str, str] = {}
    for index, run in enumerate(runs):
        harness_id = _harness_id(run, index)
        harness_for_run[index] = harness_id
        grouped[harness_id].append(run)
        names.setdefault(harness_id, _harness_name(run, harness_id))

    task_set_hash, reasons, task_keys, task_set_authority, task_hash_schema = _comparability(
        runs, harness_for_run
    )
    comparable = not reasons
    harness_rows: list[dict[str, Any]] = []
    comparison_signatures: list[tuple[Any, ...]] = []
    comparison_signatures_complete: list[bool] = []
    campaign_engines: set[str] = set()

    for harness_id in sorted(grouped):
        child_runs = sorted(grouped[harness_id], key=lambda row: str(row.get("id") or ""))
        attempts = []
        strategies = set()
        profiles = set()
        providers = set()
        engines = set()
        cost_provenances = set()
        role_signatures = set()
        for child in child_runs:
            config = child.get("config") if isinstance(child.get("config"), Mapping) else {}
            engine = _engine_for(child)
            engines.add(engine)
            campaign_engines.add(engine)
            if engine == "aios":
                role_signatures.add(_active_aios_role_signature(child))
            strategies.add(str(config.get("strategy") or "auto"))
            profiles.add(str(config.get("profile") or "lean"))
            provider = str(config.get("provider") or "").strip()
            if provider:
                providers.add(provider)
            for index, task in enumerate(child.get("tasks") or []):
                if isinstance(task, Mapping):
                    attempt = _attempt(child, task, index, harness_id)
                    attempts.append(attempt)
                    cost_provenances.add(attempt["cost_provenance"])
        attempts.sort(key=lambda row: (
            row["logical_task_id"],
            -1 if row["repetition"] is None else row["repetition"],
            row["run_id"],
            row["task_id"],
        ))
        task_models = {row["model"] for row in attempts if row["model"]}
        role_models = {
            role[2]
            for signature in role_signatures
            for role in signature
            if role[2]
        }
        models = sorted(task_models | role_models)
        comparison_signatures.append((tuple(sorted(role_signatures)), tuple(models)))
        comparison_signatures_complete.append(
            bool(role_signatures)
            and bool(models)
            and all(role[2] for signature in role_signatures for role in signature)
        )
        metrics = _summarize_attempts(attempts)
        harness_rows.append({
            "id": harness_id,
            "name": names[harness_id],
            "run_ids": sorted(str(row.get("id") or "") for row in child_runs),
            "statuses": sorted({str(row.get("status") or "unknown") for row in child_runs}),
            "engines": sorted(engines),
            "providers": sorted(providers),
            "models": models,
            "strategies": sorted(strategies),
            "profiles": sorted(profiles),
            "cost_provenances": sorted(cost_provenances),
            **metrics,
            "attempts": attempts,
        })

    all_aios = campaign_engines == {"aios"}
    exact_same_aios_roles = (
        all_aios
        and len(comparison_signatures) >= 2
        and all(comparison_signatures_complete)
        and len(set(comparison_signatures)) == 1
    )
    lane = "harness-only" if exact_same_aios_roles else "harness+model"
    if exact_same_aios_roles:
        lane_reason = "Every aiOS harness used identical active role model settings."
    elif not all_aios:
        lane_reason = "Native or mixed-engine comparisons measure harness and model together."
    else:
        lane_reason = "aiOS active role model settings differ or exact settings are unavailable."

    suite_names = sorted(
        {attempt["suite"] for harness in harness_rows for attempt in harness["attempts"]},
        key=_suite_sort_key,
    )
    by_suite = []
    for suite in suite_names:
        cells = {}
        for harness in harness_rows:
            rows = [row for row in harness["attempts"] if row["suite"] == suite]
            cells[harness["id"]] = _summarize_attempts(rows)
        by_suite.append({"suite": suite, "harnesses": cells})

    total_failures = Counter(
        row["failure_category"]
        for harness in harness_rows
        for row in harness["attempts"]
        if row.get("failure_category")
    )
    failure_categories = {
        "total": sum(total_failures.values()),
        "counts": {name: total_failures.get(name, 0) for name in FAILURE_CATEGORIES},
        "by_harness": {
            harness["id"]: dict(harness["failure_categories"])
            for harness in harness_rows
        },
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "group_id": str(payload.get("id") or ""),
        "group_status": str(payload.get("status") or "unknown"),
        "task_set_hash": task_set_hash,
        "task_set_authority": task_set_authority,
        "task_hash_schema": task_hash_schema,
        "task_keys": task_keys,
        "comparable": comparable,
        "comparability_reasons": reasons,
        "lane": lane,
        "lane_reason": lane_reason,
        "harnesses": harness_rows,
        "by_suite": by_suite,
        "failure_categories": failure_categories,
        "recommendations": _recommendations(harness_rows, comparable),
    }


def dumps_report(group: Mapping[str, Any], *, indent: int | None = 2) -> str:
    """Serialize a report byte-for-byte deterministically."""
    return json.dumps(analyze_group(group), ensure_ascii=True, indent=indent, sort_keys=True)


# British spelling for the rest of the BENCH package's vocabulary.
analyse_group = analyze_group


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze an aiOS BENCH group JSON snapshot.")
    parser.add_argument("path", type=Path, help="JSON file containing a group with child runs and tasks")
    args = parser.parse_args(argv)
    payload = json.loads(args.path.read_text(encoding="utf-8"))
    print(dumps_report(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
