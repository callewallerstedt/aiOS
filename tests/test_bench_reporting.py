"""Offline comparison reports for completed and live BENCH groups."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench import reporting  # noqa: E402


def task(task_id, suite, *, model="same/model", passed=True, status="passed",
         tokens=100, cost=0.01, seconds=10, tools=2, error="", checks=None):
    usage = {"total_tokens": tokens}
    if cost is not ...:
        usage["cost_usd"] = cost
    return {
        "id": task_id,
        "suite": suite,
        "title": task_id,
        "model": model,
        "status": status,
        "passed": passed,
        "usage": usage,
        "seconds": seconds,
        "tool_calls": tools,
        "error": error,
        "checks": checks or [],
    }


def run(
    run_id,
    harness_id,
    tasks,
    *,
    model="same/model",
    repetition=1,
    engine="aios",
    roles=None,
    task_set_hash=None,
):
    rows = []
    for row in tasks:
        item = dict(row)
        item.setdefault("model", model)
        rows.append(item)
    descriptor = sorted(
        ({
            "id": str(row.get("id") or ""),
            "suite": str(row.get("suite") or ""),
            "fixture_hash": str(row.get("fixture_hash") or ""),
        } for row in rows),
        key=lambda row: (row["suite"], row["id"], row["fixture_hash"]),
    )
    authoritative_hash = task_set_hash or hashlib.sha256(
        json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    payload = {
        "id": run_id,
        "status": "completed",
        "saved_config_id": harness_id,
        "saved_config_name": harness_id.title(),
        "repetition": repetition,
        "task_set_hash": authoritative_hash,
        "task_hash_schema": reporting.AUTHORITATIVE_TASK_HASH_SCHEMA,
        "config": {
            "engine": engine,
            "provider": "openrouter" if engine == "aios" else engine,
            "model": model,
            "strategy": "auto",
            "profile": "lean",
        },
        "tasks": rows,
    }
    if roles is not None:
        payload["saved_config_roles"] = roles
    return payload


def group(*runs):
    return {"id": "group-1", "status": "completed", "runs": list(runs)}


def harness(report, harness_id):
    return next(row for row in report["harnesses"] if row["id"] == harness_id)


def test_mixed_task_sets_are_not_comparable_and_hash_is_order_independent():
    alpha = run("a1", "alpha", [task("tweak/a", "tweak"), task("bugfix/b", "bugfix")])
    beta = run("b1", "beta", [task("tweak/a", "tweak")])

    first = reporting.analyze_group(group(alpha, beta))
    reversed_report = reporting.analyze_group(group(beta, alpha))

    assert first["comparable"] is False
    assert "task sets differ across harnesses or repetitions" in first["comparability_reasons"]
    assert first["task_set_hash"] == reversed_report["task_set_hash"]
    assert first["lane"] == "harness-only"
    assert reporting.dumps_report(group(alpha, beta)) == reporting.dumps_report(group(beta, alpha))


def test_model_request_aggregation_uses_only_exact_available_counts():
    exact_one = task("bugfix/a", "bugfix") | {
        "model_request_count": 3,
        "model_request_count_source": "aios_local_provider_loop",
    }
    exact_two = task("bugfix/a", "bugfix") | {
        "model_request_count": 5,
        "model_request_count_source": "omp_unique_assistant_message_end",
    }
    unavailable = task("bugfix/a", "bugfix") | {
        "model_request_count": None,
        "model_request_count_source": "unavailable",
    }
    report = reporting.analyze_group(group(
        run("a1", "alpha", [exact_one], repetition=1),
        run("a2", "alpha", [unavailable], repetition=2),
        run("b1", "beta", [exact_two], repetition=1),
        run("b2", "beta", [unavailable], repetition=2),
    ))

    alpha = harness(report, "alpha")
    beta = harness(report, "beta")
    assert alpha["model_request_count_total"] == 3
    assert alpha["median_model_request_count"] == 3
    assert alpha["model_request_count_available"] == 1
    assert alpha["model_request_count_unavailable"] == 1
    assert beta["model_request_count_total"] == 5


def test_persisted_hash_mismatch_overrides_matching_task_ids():
    same_task = [task("bugfix/a", "bugfix")]
    alpha = run("a1", "alpha", same_task, task_set_hash="a" * 64)
    beta = run("b1", "beta", same_task, task_set_hash="b" * 64)

    report = reporting.analyze_group(group(alpha, beta))

    assert report["comparable"] is False
    assert report["task_set_authority"] == "persisted"
    assert "task sets differ across harnesses or repetitions" in report["comparability_reasons"]


def test_legacy_task_fallback_is_explicitly_non_authoritative():
    alpha = run("a1", "alpha", [task("bugfix/a", "bugfix")])
    beta = run("b1", "beta", [task("bugfix/a", "bugfix")])
    for row in (alpha, beta):
        row.pop("task_set_hash")
        row.pop("task_hash_schema")

    report = reporting.analyze_group(group(alpha, beta))

    assert report["comparable"] is False
    assert report["task_set_authority"] == "legacy-fallback"
    assert report["task_hash_schema"] == 0
    assert "legacy task identity is non-authoritative" in report["comparability_reasons"]
    assert report["schema_version"] == 2


def test_repetition_identities_must_be_unique_contiguous_and_equal():
    rows = [task("bugfix/a", "bugfix")]
    duplicate = reporting.analyze_group(group(
        run("a1", "alpha", rows, repetition=1),
        run("a2", "alpha", rows, repetition=1),
        run("b1", "beta", rows, repetition=1),
        run("b2", "beta", rows, repetition=2),
    ))
    gap = reporting.analyze_group(group(
        run("a1", "alpha", rows, repetition=1),
        run("a3", "alpha", rows, repetition=3),
        run("b1", "beta", rows, repetition=1),
        run("b3", "beta", rows, repetition=3),
    ))
    fractional = reporting.analyze_group(group(
        run("a1", "alpha", rows, repetition=1.5),
        run("b1", "beta", rows, repetition=1.5),
    ))

    assert duplicate["comparable"] is False
    assert "duplicate repetition identities in alpha" in duplicate["comparability_reasons"]
    assert "repetition identities differ across harnesses" in duplicate["comparability_reasons"]
    assert gap["comparable"] is False
    assert "repetition identities are not contiguous from 1 in alpha" in gap["comparability_reasons"]
    assert "repetition identities are not contiguous from 1 in beta" in gap["comparability_reasons"]
    assert fractional["comparable"] is False
    assert "missing or invalid repetition identity in alpha" in fractional["comparability_reasons"]


def test_lane_signature_covers_all_active_roles_and_never_calls_native_harness_only():
    rows = [task("bugfix/a", "bugfix")]
    first_roles = {
        "scout": {"enabled": True, "model": "scout/a", "reasoning": "off", "fast": True},
        "coder": {"enabled": True, "model": "same/model", "reasoning": "off", "fast": False},
    }
    second_roles = {
        **first_roles,
        "scout": {"enabled": True, "model": "scout/b", "reasoning": "off", "fast": True},
    }
    role_report = reporting.analyze_group(group(
        run("a1", "alpha", rows, roles=first_roles),
        run("b1", "beta", rows, roles=second_roles),
    ))
    same_role_report = reporting.analyze_group(group(
        run("a1", "alpha", rows, roles=first_roles),
        run("b1", "beta", rows, roles=first_roles),
    ))
    native_report = reporting.analyze_group(group(
        run("a1", "alpha", rows, roles=first_roles),
        run("b1", "codex", rows, engine="codex"),
    ))

    assert role_report["lane"] == "harness+model"
    assert harness(role_report, "alpha")["models"] == ["same/model", "scout/a"]
    assert same_role_report["lane"] == "harness-only"
    assert native_report["lane"] == "harness+model"
    assert native_report["lane_reason"] == "Native or mixed-engine comparisons measure harness and model together."


def test_missing_cost_stays_null_and_marks_the_harness_unavailable():
    alpha = run("a1", "alpha", [task("bugfix/a", "bugfix", cost=...)])
    beta = run("b1", "beta", [task("bugfix/a", "bugfix", cost=0.02)])

    report = reporting.analyze_group(group(alpha, beta))
    row = harness(report, "alpha")

    assert row["attempts"][0]["usage"]["cost_usd"] is None
    assert row["median_cost_usd"] is None
    assert row["cost_status"] == "unavailable"
    assert row["cost_reported_attempts"] == 0
    assert json.loads(reporting.dumps_report(group(alpha, beta)))["harnesses"][0]["median_cost_usd"] is None


def test_unavailable_cost_provenance_never_turns_zero_into_a_free_harness():
    codex_task = task("bugfix/a", "bugfix", cost=0.0)
    codex_task["cost_provenance"] = "unavailable"
    codex = run("a1", "codex", [codex_task])
    codex["config"]["cost_provenance"] = "unavailable"
    paid = run("b1", "paid", [task("bugfix/a", "bugfix", cost=0.02)])

    report = reporting.analyze_group(group(codex, paid))
    row = harness(report, "codex")

    assert row["attempts"][0]["usage"]["cost_usd"] is None
    assert row["cost_status"] == "unavailable"
    assert row["median_cost_usd"] is None


def test_per_harness_medians_and_capability_matrix_use_exact_attempts():
    alpha_tasks = [
        task("bugfix/a", "bugfix", tokens=10, cost=0.1, seconds=30, tools=5),
        task("bugfix/b", "bugfix", tokens=100, cost=0.3, seconds=10, tools=1, passed=False, status="failed"),
        task("bugfix/c", "bugfix", tokens=50, cost=0.2, seconds=20, tools=3),
    ]
    beta_tasks = [
        task("bugfix/a", "bugfix", model="other/model"),
        task("bugfix/b", "bugfix", model="other/model"),
        task("bugfix/c", "bugfix", model="other/model"),
    ]
    report = reporting.analyze_group(group(
        run("a1", "alpha", alpha_tasks),
        run("b1", "beta", beta_tasks, model="other/model"),
    ))
    row = harness(report, "alpha")

    assert report["comparable"] is True
    assert report["lane"] == "harness+model"
    assert row["pass_rate"] == 0.666667
    assert row["median_tokens"] == 50
    assert row["median_cost_usd"] == 0.2
    assert row["median_seconds"] == 20
    assert row["median_tool_calls"] == 3
    assert [attempt["task_id"] for attempt in row["attempts"]] == ["bugfix/a", "bugfix/b", "bugfix/c"]
    bugfix = next(item for item in report["by_suite"] if item["suite"] == "bugfix")
    assert bugfix["harnesses"]["alpha"]["passed_count"] == 2


def test_recommendations_cover_capability_and_efficiency_signals():
    bad = [
        task(
            "bugfix/a", "bugfix", tokens=1000, cost=0.1, passed=False, status="failed",
            checks=[{"name": "regression", "passed": False}],
        ),
        task(
            "precision/a", "precision", tokens=1200, cost=0.12, passed=False,
            status="timeout", error="timed out after 60s",
        ),
        task(
            "hard/a", "hard", tokens=1300, cost=0.13, passed=False,
            status="failed", error="tool failed: run_shell",
        ),
    ]
    good = [
        task("bugfix/a", "bugfix", tokens=100, cost=0.01),
        task("precision/a", "precision", tokens=100, cost=0.01),
        task("hard/a", "hard", tokens=100, cost=0.01),
    ]
    report = reporting.analyze_group(group(run("a1", "alpha", bad), run("b1", "beta", good)))
    keys = [row["key"] for row in report["recommendations"]]

    assert report["failure_categories"]["counts"]["verification"] == 1
    assert report["failure_categories"]["counts"]["timeout"] == 1
    assert report["failure_categories"]["counts"]["tool-error"] == 1
    assert {"timeout", "tool-error", "bugfix", "precision", "hard", "token", "cost"} <= set(keys)
    assert keys[:2] == ["timeout", "tool-error"]


def test_native_report_uses_primary_model_instead_of_auxiliary_usage_model():
    native_task = task("hard/a", "hard", model="requested-alias")
    native_task["native_primary_model"] = "primary/exact"
    native_task["native_models_used"] = ["primary/exact", "auxiliary/helper"]

    report = reporting.analyze_group(group(
        run("a1", "native", [native_task], engine="omp"),
        run("b1", "aios", [task("hard/a", "hard", model="same/base")]),
    ))

    assert harness(report, "native")["models"] == ["primary/exact"]


def test_native_adapter_setup_failure_outranks_untouched_fixture_checks():
    row = task(
        "bugfix/a", "bugfix", passed=False, status="failed",
        error="native adapter failed: unexpected argument '--flag' found",
        checks=[{"name": "hidden behavior", "passed": False}],
    )

    assert reporting._failure_category(row) == "setup-error"
