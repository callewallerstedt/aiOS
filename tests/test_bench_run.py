"""How a benchmark run behaves, and the one property it must never lose.

The load-bearing property is isolation: a benchmark session lives in the run's
own directory and never appears in the CODE session list. It is easy to lose --
one import of `code_jobs` in the wrong process and fifty throwaway sessions land
in the list you actually use -- so it is pinned from both ends here, at the
process boundary that creates it and at the guard that refuses to run without
it.
"""

from __future__ import annotations

import json
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench import adapters, runner, runs, scoring, suites  # noqa: E402
from tests.test_bench_suites import REFERENCE  # noqa: E402


def test_cache_hit_rate_uses_provider_comparable_prompt_denominator():
    omp = scoring.summarise([{
        "passed": True,
        "suite": "tweak",
        "seconds": 1,
        "tool_calls": 1,
        "usage": {
            "input_tokens": 100,
            "cached_input_tokens": 300,
            "canonical_prompt_tokens": 400,
            "output_tokens": 10,
            "total_tokens": 410,
        },
    }])
    aios = scoring.summarise([{
        "passed": True,
        "suite": "tweak",
        "seconds": 1,
        "tool_calls": 1,
        "usage": {
            "input_tokens": 400,
            "cached_input_tokens": 300,
            "output_tokens": 10,
            "total_tokens": 410,
        },
    }])

    assert omp["canonical_prompt_tokens"] == 400
    assert omp["cache_hit_rate"] == 0.75
    assert aios["cache_hit_rate"] == 0.75
    assert 0 <= omp["cache_hit_rate"] <= 1


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A runs directory of our own, so tests never touch bench/runs."""
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    # Group/run integration tests exercise storage and process orchestration,
    # not live CLI/model discovery.  Keep those external preflights explicit
    # in their focused tests below.
    monkeypatch.setattr(runs, "_resolved_harness_version", lambda engine, supplied="": str(supplied or "test-harness"))
    monkeypatch.setattr(runs, "_aios_selection_error", lambda provider, model, reasoning, fast: None)
    return tmp_path / "runs"


CONFIG = {
    "provider": "openrouter",
    "model": "deepseek/deepseek-v4-flash",
    "reasoning": "off",
    "counts": {"bugfix": 1},
    "concurrency": 2,
    "timeout": 120,
}


# ------------------------------------------------------------------ isolation


def test_the_runner_is_started_pointed_at_its_own_jobs_directory(store, monkeypatch):
    captured = {}

    class FakeProcess:
        pid = 4242

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs.get("env") or {}
        return FakeProcess()

    monkeypatch.setattr(runs.subprocess, "Popen", fake_popen)
    created = runs.create_run(CONFIG, label="isolated")
    assert created["ok"], created

    run_id = created["run"]["id"]
    expected = str(runs.run_dir(run_id) / "jobs")
    assert captured["env"]["AIOS_CODE_JOBS_DIR"] == expected
    assert "bench.runner" in captured["command"]
    # code_jobs does JOBS_DIR.mkdir(exist_ok=True) with no parents, so the
    # directory has to exist before the child imports it.
    assert Path(expected).is_dir()


def test_direct_fixture_preparation_error_creates_no_run(store, monkeypatch):
    spawned = []

    def fail_preparation(_config):
        raise RuntimeError("pinned Aider fixture SHA-256 mismatch")

    monkeypatch.setattr(runs, "select_tasks", fail_preparation)
    monkeypatch.setattr(runs.subprocess, "Popen", lambda *a, **k: spawned.append((a, k)))

    result = runs.create_run(CONFIG)

    assert result == {
        "ok": False,
        "error": "could not prepare tasks: pinned Aider fixture SHA-256 mismatch",
    }
    assert spawned == []
    assert not store.exists()


def test_running_task_reads_live_usage_and_work_counters(store, monkeypatch):
    monkeypatch.setattr(runs.subprocess, "Popen", lambda *a, **k: type("P", (), {"pid": 7})())
    created = runs.create_run(CONFIG)
    run = created["run"]
    task = run["tasks"][0]
    task.update({"status": "running", "job_id": "live-job", "started_at": time.time() - 12})
    runs.write_run(run)
    directory = runs.run_dir(run["id"]) / "jobs" / "live-job"
    directory.mkdir(parents=True)
    (directory / "job.json").write_text(json.dumps({
        "status": "running",
        "usage": {"total_tokens": 12345, "input_tokens": 12000, "output_tokens": 345},
        "estimated_cost_usd": 0.0123,
        "files_edited": 2,
        "lines_added": 40,
        "lines_deleted": 3,
    }), encoding="utf-8")
    (directory / "events.jsonl").write_text(
        json.dumps({"kind": "activity", "activity_type": "stage", "activity_id": "stage-1"}) + "\n"
        + json.dumps({"kind": "activity", "activity_type": "command", "activity_id": "tool-1",
                      "tool": "run_shell"}) + "\n",
        encoding="utf-8",
    )
    live = runs.get_run(run["id"])
    assert live["summary"]["usage"]["total_tokens"] == 12345
    assert live["summary"]["cost_usd"] == 0.0123
    assert live["summary"]["tool_calls"] == 1
    assert live["tasks"][0]["files_edited"] == 2
    assert live["tasks"][0]["seconds"] >= 12


def test_multiple_saved_configs_are_one_visible_run_group(store, monkeypatch):
    monkeypatch.setattr(runs.subprocess, "Popen", lambda *a, **k: type("P", (), {"pid": 8})())
    result = runs.create_run_group(
        {"kind": "custom", "prompt": "Build the same thing.", "timeout": 120},
        [
            {"id": "cheap", "name": "Cheap", "roles": {"coder": {"model": "a/model"}}},
            {"id": "smart", "name": "Smart", "roles": {"coder": {"model": "b/model"}}},
        ],
        "Comparison",
    )
    assert result["ok"], result
    group = result["group"]
    assert group["is_group"] is True
    assert group["label"] == "Comparison"
    assert len(group["runs"]) == 2
    assert {run["saved_config_name"] for run in group["runs"]} == {"Cheap", "Smart"}
    assert len({run["agent_id"] for run in group["runs"]}) == 2
    for child in group["runs"]:
        assert 100 <= child["agent_id"] <= 999
        assert child["preview_port"] == runs.AGENT_PORT_BASE + child["agent_id"]
    assert len(runs.list_run_groups()) == 1


def test_group_preserves_each_saved_provider_and_review_loop(store, monkeypatch):
    monkeypatch.setattr(runs.subprocess, "Popen", lambda *a, **k: type("P", (), {"pid": 9})())
    result = runs.create_run_group(
        {"kind": "custom", "prompt": "Build the same thing.", "timeout": 120},
        [{
            "id": "claude-config",
            "name": "Claude config",
            "provider": "claude",
            "review_fix": True,
            "roles": {"coder": {"model": "anthropic/test-coder"}},
        }],
        "Comparison",
    )
    child = result["group"]["runs"][0]
    assert child["config"]["provider"] == "claude"
    assert child["config"]["models"][0]["provider"] == "claude"
    assert child["config"]["review_fix"] is True


def test_group_preserves_each_saved_execution_strategy(store, monkeypatch):
    monkeypatch.setattr(runs.subprocess, "Popen", lambda *a, **k: type("P", (), {"pid": 10})())
    result = runs.create_run_group(
        {"kind": "custom", "prompt": "Build the same thing.", "timeout": 120},
        [
            {
                "id": "direct-config",
                "name": "Direct config",
                "strategy": "direct",
                "roles": {"coder": {"model": "a/model"}},
            },
            {
                "id": "team-config",
                "name": "Team config",
                "strategy": "distributed",
                "roles": {"coder": {"model": "b/model"}},
            },
        ],
        "Comparison",
    )
    assert result["ok"], result
    strategies = {
        child["saved_config_id"]: child["config"]["strategy"]
        for child in result["group"]["runs"]
    }
    assert strategies == {"direct-config": "direct", "team-config": "distributed"}


def test_fixed_campaign_reuses_exact_tasks_tracks_repetitions_and_splits_only_billable_caps(store, monkeypatch):
    monkeypatch.setattr(runs.subprocess, "Popen", lambda *a, **k: type("P", (), {"pid": 11})())
    monkeypatch.setattr(adapters, "catalogue", lambda: [
        {"id": "codex", "ready": True, "auth": "authenticated"},
        {"id": "claude", "ready": True, "auth": "authenticated"},
    ])
    real_select_tasks = runs.select_tasks
    selections = []

    def select_once(config):
        selections.append(config)
        if len(selections) > 1:
            raise RuntimeError("public fixture changed after the first child started")
        return real_select_tasks(config)

    monkeypatch.setattr(runs, "select_tasks", select_once)
    result = runs.create_run_group(
        {
            "kind": "suite", "counts": {"bugfix": 1, "tweak": 1},
            "timeout": 120, "repetitions": 2, "max_cost_usd": 0.8,
            "native_max_cost_usd": 0.5,
        },
        [
            {"id": "balanced", "name": "Balanced", "provider": "openrouter",
             "roles": {"coder": {"model": "same/model", "reasoning": "off"}}},
            {"id": "codex", "name": "Codex", "engine": "codex", "provider": "codex",
             "model": "codex/test-model", "cost_provenance": "unavailable"},
            {"id": "claude", "name": "Claude", "engine": "claude", "provider": "claude",
             "model": "claude/test-model", "cost_provenance": "api_equivalent"},
        ],
        "Proof",
    )
    assert result["ok"], result
    assert len(selections) == 1
    group = result["group"]
    assert len(group["runs"]) == 6
    assert group["comparable"] is True
    assert len(group["task_set_hashes"]) == 1
    repetitions = {}
    for child in group["runs"]:
        repetitions.setdefault(child["saved_config_id"], []).append(child["repetition"])
        assert child["config"]["kind"] == "suite"
        assert len(child["tasks"]) == 2
        assert child["task_hash_schema"] == runs.TASK_SET_HASH_SCHEMA
        assert all(task["fixture_hash"] for task in child["tasks"])
        if child["saved_config_id"] == "codex":
            assert child["config"]["model"] == "codex/test-model"
        if child["saved_config_id"] == "claude":
            assert child["config"]["model"] == "claude/test-model"
    assert {key: sorted(value) for key, value in repetitions.items()} == {
        "balanced": [1, 2], "claude": [1, 2], "codex": [1, 2],
    }
    by_harness = {}
    for child in group["runs"]:
        by_harness.setdefault(child["saved_config_id"], set()).add(child["config"]["max_cost_usd"])
    assert by_harness == {"balanced": {0.4}, "claude": {0.5}, "codex": {0.0}}


def test_fixed_campaign_fixture_error_starts_no_partial_children(store, monkeypatch):
    spawned = []

    def fail_preparation(_config):
        raise RuntimeError("pinned Aider fixture SHA-256 mismatch")

    monkeypatch.setattr(runs, "select_tasks", fail_preparation)
    monkeypatch.setattr(runs.subprocess, "Popen", lambda *a, **k: spawned.append((a, k)))

    result = runs.create_run_group(
        {"kind": "suite", "counts": {"aider_polyglot": 1}, "timeout": 120},
        [
            {"id": "first", "name": "First", "roles": {"coder": {"model": "same/model"}}},
            {"id": "second", "name": "Second", "roles": {"coder": {"model": "same/model"}}},
        ],
        "Integrity preflight",
    )

    assert result == {
        "ok": False,
        "error": "could not prepare tasks: pinned Aider fixture SHA-256 mismatch",
    }
    assert spawned == []
    assert not store.exists()


def test_task_hash_covers_protected_grader_and_pinned_source_provenance():
    common = {
        "id": "audit/task",
        "suite": "bugfix",
        "title": "Audit",
        "brief": "Fix it.",
        "files": {"app.py": "VALUE = 1\n"},
        "checks": '@case("works")\ndef _():\n    assert True\n',
        "source": "https://example.test/repo/tree/commit-a",
        "provenance": {"commit": "commit-a", "task_id": "one"},
    }
    reference = suites.Task(**common, protected=("tests.py",))
    changed_protected = suites.Task(**common, protected=())
    changed_source = suites.Task(**{**common, "source": "https://example.test/repo/tree/commit-b"}, protected=("tests.py",))
    changed_provenance = suites.Task(
        **{**common, "provenance": {"commit": "commit-b", "task_id": "one"}},
        protected=("tests.py",),
    )
    hashes = {
        runs._task_set_hash([reference]),
        runs._task_set_hash([changed_protected]),
        runs._task_set_hash([changed_source]),
        runs._task_set_hash([changed_provenance]),
    }

    assert len(hashes) == 4
    assert runs._task_set_hash([reference, changed_source]) == runs._task_set_hash([changed_source, reference])
    assert runs._task_fixture_hash(reference) != runs._task_fixture_hash(changed_protected)


def test_shared_openrouter_cap_split_is_residual_safe(store, monkeypatch):
    monkeypatch.setattr(runs.subprocess, "Popen", lambda *a, **k: type("P", (), {"pid": 12})())
    result = runs.create_run_group(
        {
            "kind": "suite",
            "counts": {"bugfix": 1},
            "timeout": 120,
            "max_cost_usd": 0.8,
        },
        [
            {"id": f"config-{index}", "name": f"Config {index}", "roles": {"coder": {"model": "same/model"}}}
            for index in range(3)
        ],
        "Residual-safe",
    )

    assert result["ok"], result
    caps = [child["config"]["max_cost_usd"] for child in result["group"]["runs"]]
    assert sorted(caps) == [0.266666, 0.266667, 0.266667]
    assert sum(round(value * 1_000_000) for value in caps) == 800_000


def test_openrouter_cap_is_shared_by_aios_omp_and_hermes_only(store, monkeypatch):
    monkeypatch.setattr(runs.subprocess, "Popen", lambda *a, **k: type("P", (), {"pid": 13})())
    monkeypatch.setattr(adapters, "catalogue", lambda: [
        {
            "id": "omp", "ready": True, "auth": "openrouter_configured",
            "default_provider": "openrouter", "default_model": "openrouter/deepseek/model",
            "default_reasoning": "high", "cost_provenance": "provider_reported",
        },
        {
            "id": "hermes", "ready": True, "auth": "openrouter_configured",
            "default_provider": "openrouter", "default_model": "deepseek/model",
            "default_reasoning": "high", "cost_provenance": "model_pricing_estimate",
        },
        {
            "id": "claude", "ready": True, "auth": "authenticated",
            "default_provider": "claude", "default_model": "claude/exact",
            "default_reasoning": "high", "cost_provenance": "api_equivalent",
        },
        {
            "id": "codex", "ready": True, "auth": "authenticated",
            "default_provider": "codex", "default_model": "codex/exact",
            "default_reasoning": "high", "cost_provenance": "unavailable",
        },
    ])
    result = runs.create_run_group(
        {
            "kind": "suite", "counts": {"bugfix": 1}, "timeout": 120,
            "repetitions": 2, "max_cost_usd": 0.8, "native_max_cost_usd": 0.5,
        },
        [
            {"id": "aios", "name": "aiOS", "roles": {"coder": {"model": "same/model"}}},
            {"id": "omp", "name": "OMP", "engine": "omp"},
            {"id": "hermes", "name": "Hermes", "engine": "hermes"},
            {"id": "claude", "name": "Claude", "engine": "claude"},
            {"id": "codex", "name": "Codex", "engine": "codex"},
        ],
        "Provider budgets",
    )

    assert result["ok"], result
    children = result["group"]["runs"]
    openrouter = [child for child in children if child["config"]["provider"] == "openrouter"]
    assert {child["saved_config_id"] for child in openrouter} == {"aios", "omp", "hermes"}
    assert sorted(child["config"]["max_cost_usd"] for child in openrouter) == [
        0.133333, 0.133333, 0.133333, 0.133333, 0.133334, 0.133334,
    ]
    assert sum(round(child["config"]["max_cost_usd"] * 1_000_000) for child in openrouter) == 800_000
    by_harness = {}
    for child in children:
        by_harness.setdefault(child["saved_config_id"], set()).add(child["config"]["max_cost_usd"])
    assert by_harness["claude"] == {0.5}
    assert by_harness["codex"] == {0.0}
    assert next(child for child in children if child["saved_config_id"] == "omp")["config"]["model"] == "openrouter/deepseek/model"
    assert next(child for child in children if child["saved_config_id"] == "hermes")["config"]["model"] == "deepseek/model"


def test_campaign_rejects_unready_native_adapter_and_missing_exact_model(store, monkeypatch):
    monkeypatch.setattr(runs.subprocess, "Popen", lambda *a, **k: type("P", (), {"pid": 14})())
    base = {"kind": "suite", "counts": {"bugfix": 1}, "timeout": 120}
    aios = {"id": "aios", "name": "aiOS", "roles": {"coder": {"model": "same/model"}}}
    monkeypatch.setattr(adapters, "catalogue", lambda: [
        {"id": "codex", "ready": False, "auth": "not_authenticated"},
    ])

    unready = runs.create_run_group(base, [
        aios,
        {"id": "codex", "name": "Codex", "engine": "codex", "model": "codex/test-model"},
    ])

    assert unready["ok"] is False
    assert "not ready" in unready["error"]
    monkeypatch.setattr(adapters, "catalogue", lambda: [
        {"id": "codex", "ready": True, "auth": "authenticated"},
    ])
    missing_model = runs.create_run_group(base, [
        aios,
        {"id": "codex", "name": "Codex", "engine": "codex"},
    ])
    assert missing_model["ok"] is False
    assert missing_model["error"] == "Codex requires an exact model"
    monkeypatch.setattr(adapters, "catalogue", lambda: [
        {"id": "claude", "ready": True, "auth": "authenticated"},
    ])
    missing_claude_model = runs.create_run_group(base, [
        aios,
        {"id": "claude", "name": "Claude", "engine": "claude"},
    ])
    assert missing_claude_model["ok"] is False
    assert missing_claude_model["error"] == "Claude requires an exact model"
    monkeypatch.setattr(adapters, "catalogue", lambda: [
        {"id": "omp", "ready": True, "auth": "environment"},
    ])
    missing_omp_model = runs.create_run_group(base, [
        aios,
        {"id": "omp", "name": "OMP", "engine": "omp"},
    ])
    assert missing_omp_model["ok"] is False
    assert missing_omp_model["error"] == "OMP requires an exact model"
    monkeypatch.setattr(adapters, "catalogue", lambda: [
        {"id": "hermes", "ready": True, "auth": "environment"},
    ])
    missing_hermes_model = runs.create_run_group(base, [
        aios,
        {"id": "hermes", "name": "Hermes", "engine": "hermes"},
    ])
    assert missing_hermes_model["ok"] is False
    assert missing_hermes_model["error"] == "Hermes requires an exact model"
    monkeypatch.setattr(adapters, "catalogue", lambda: [{
        "id": "omp", "ready": True, "auth": "openrouter_configured",
        "default_provider": "openrouter", "default_model": "openrouter/deepseek/exact",
        "default_reasoning": "high", "cost_provenance": "provider_reported",
    }])
    supported = runs.create_run_group(base, [
        aios,
        {"id": "omp", "name": "OMP", "engine": "omp"},
    ])
    assert supported["ok"], supported
    omp = next(child for child in supported["group"]["runs"] if child["saved_config_id"] == "omp")
    assert omp["config"]["provider"] == "openrouter"
    assert omp["config"]["model"] == "openrouter/deepseek/exact"
    assert omp["config"]["reasoning"] == "high"


def test_group_preflights_every_aios_selection_before_creating_children(store, monkeypatch):
    checked = []
    spawned = []

    def validate(provider, model, reasoning, fast):
        checked.append((provider, model, reasoning, fast))
        if model == "disabled/model":
            return {
                "ok": False,
                "error": "'disabled/model' is not a current openrouter model. Choose an exact discovered model.",
                "needs": ["model"],
            }
        return None

    monkeypatch.setattr(runs, "_aios_selection_error", validate)
    monkeypatch.setattr(runs.subprocess, "Popen", lambda *args, **kwargs: spawned.append((args, kwargs)))
    result = runs.create_run_group(
        {"kind": "suite", "counts": {"bugfix": 1}, "timeout": 120},
        [
            {
                "id": "enabled", "name": "Enabled", "provider": "openrouter",
                "model": "enabled/model", "reasoning": "off", "roles": {},
            },
            {
                "id": "disabled", "name": "Disabled", "provider": "openrouter",
                "model": "disabled/model", "reasoning": "high", "fast": True, "roles": {},
            },
        ],
    )

    assert result["ok"] is False
    assert result["error"].startswith("Disabled is not runnable:")
    assert "not a current openrouter model" in result["error"]
    assert checked == [
        ("openrouter", "enabled/model", "off", True),
        ("openrouter", "disabled/model", "high", True),
    ]
    assert spawned == []
    assert not store.exists()


def test_fixed_campaign_requires_two_harnesses(store):
    result = runs.create_run_group(
        {"kind": "suite", "counts": {"bugfix": 1}},
        [{"id": "only", "name": "Only", "roles": {"coder": {"model": "a/model"}}}],
    )
    assert result["ok"] is False
    assert "at least two" in result["error"]


def test_saved_role_config_isolated_and_passed_to_the_runner(store, monkeypatch):
    captured = {}

    class FakeProcess:
        pid = 4243

    def fake_popen(command, **kwargs):
        captured["env"] = kwargs["env"]
        return FakeProcess()

    monkeypatch.setattr(runs.subprocess, "Popen", fake_popen)
    created = runs.create_run(
        CONFIG,
        saved_config_id="ui-fast",
        saved_config_name="UI fast",
        saved_config_roles={"scout": {"model": "cheap/scout"}, "coder": {"model": "cheap/coder"}},
    )
    assert created["ok"], created
    path = Path(captured["env"]["AIOS_CODE_CONFIG_PATH"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["code_roles"]["scout"]["model"] == "cheap/scout"
    assert payload["code_roles"]["coder"]["model"] == "cheap/coder"
    assert created["run"]["saved_config_id"] == "ui-fast"


def test_a_benchmark_session_is_nowhere_near_the_real_session_store(store, monkeypatch):
    monkeypatch.setattr(runs.subprocess, "Popen", lambda *a, **k: type("P", (), {"pid": 1})())
    created = runs.create_run(CONFIG)
    import code_jobs

    bench_jobs = runs.run_dir(created["run"]["id"]) / "jobs"
    assert code_jobs.JOBS_DIR.resolve() not in bench_jobs.resolve().parents
    assert bench_jobs.resolve() != code_jobs.JOBS_DIR.resolve()


def test_the_runner_refuses_to_run_in_the_wrong_store(store, monkeypatch, tmp_path):
    """The guard, not the caller, is what makes the isolation trustworthy."""
    monkeypatch.setattr(runs.subprocess, "Popen", lambda *a, **k: type("P", (), {"pid": 1})())
    created = runs.create_run(CONFIG)
    directory = runs.run_dir(created["run"]["id"])

    import code_jobs

    monkeypatch.setattr(code_jobs, "JOBS_DIR", tmp_path / "somewhere-else")
    assert runner.main(["--run", str(directory)]) == 2

    settled = runs.get_run(created["run"]["id"])
    assert settled["status"] == "failed"
    assert "refusing to run" in settled["error"]


# --------------------------------------------------------------------- config


def test_a_run_needs_a_harness_and_at_least_one_task():
    assert runs.normalise_config({})[1] == "pick a provider"
    assert runs.normalise_config({"provider": "openrouter"})[1] == "pick an exact model"
    assert runs.normalise_config({"provider": "openrouter", "model": "m"})[1] == "pick a reasoning level"
    assert runs.normalise_config(
        {"provider": "openrouter", "model": "m", "reasoning": "off", "counts": {}}
    )[1] == "pick at least one task"


def test_config_numbers_are_clamped_to_something_survivable():
    config, error = runs.normalise_config({
        "provider": "openrouter", "model": "m", "reasoning": "off",
        "counts": {"bugfix": 99, "humaneval": 5}, "concurrency": 400, "timeout": 5,
    })
    assert not error
    assert config["counts"]["bugfix"] == suites.SUITES["bugfix"]["max"]
    assert config["concurrency"] == runs.MAX_CONCURRENCY
    assert config["timeout"] == 60.0


def test_aios_run_persists_a_worktree_harness_version(monkeypatch):
    monkeypatch.setattr(adapters, "_git_version", lambda: "abc123+worktree.deadbeef00")

    config, error = runs.normalise_config({
        "provider": "openrouter", "model": "m", "reasoning": "off",
        "counts": {"bugfix": 1},
    })

    assert not error
    assert config["harness_version"] == "abc123+worktree.deadbeef00"


def test_observed_cost_ceiling_does_not_silently_disable_parallelism():
    config, error = runs.normalise_config({
        "provider": "openrouter", "model": "m", "reasoning": "off",
        "counts": {"bugfix": 1}, "concurrency": 6, "max_cost_usd": 0.5,
    })
    assert not error
    assert config["concurrency"] == 6
    assert config["max_cost_usd"] == 0.5


def test_native_config_uses_the_cli_default_model_without_inventing_an_id():
    config, error = runs.normalise_config({
        "engine": "codex", "provider": "codex", "counts": {"bugfix": 1},
    })
    assert not error
    assert config["model"] == ""
    assert config["reasoning"] == "auto"

    kimi, kimi_error = runs.normalise_config({
        "engine": "kimi", "counts": {"bugfix": 1},
    })
    assert not kimi_error
    assert kimi["provider"] == "openrouter"
    assert kimi["model"] == ""
    assert kimi["reasoning"] == "auto"
    assert kimi["cost_provenance"] == "provider_reported"


def test_a_run_id_cannot_escape_the_runs_directory():
    for bad in ["../../etc", "a/b", ""]:
        with pytest.raises(ValueError):
            runs.run_dir(bad)


# ---------------------------------------------------------------- one task


class FakeJobs:
    """A CODE harness that solves the task instantly, or does nothing at all."""

    TERMINAL_STATES = {"completed", "failed", "interrupted", "stopped"}

    def __init__(self, jobs_dir: Path, solution: dict | None, usage: dict):
        self.jobs_dir = jobs_dir
        self.solution = solution
        self.usage = usage
        self.stopped = []
        self.created_strategy = ""

    def create_job(self, provider, cwd, brief, model, reasoning, fast, title="", strategy="auto"):
        self.created_strategy = strategy
        if self.solution:
            for name, body in self.solution.items():
                (Path(cwd) / name).write_text(body, encoding="utf-8")
        directory = self.jobs_dir / "job1"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "job.json").write_text(json.dumps(self._meta()), encoding="utf-8")
        (directory / "events.jsonl").write_text(
            json.dumps({"kind": "activity", "activity_type": "stage", "activity_id": "s1"}) + "\n"
            + json.dumps({"kind": "activity", "activity_type": "command", "activity_id": "a1",
                          "tool": "run_shell"}) + "\n"
            + json.dumps({"kind": "activity", "activity_type": "thinking", "activity_id": "t1"}) + "\n",
            encoding="utf-8",
        )
        return {"ok": True, "job": {"id": "job1"}}

    def _meta(self):
        return {"id": "job1", "status": "completed", "usage": self.usage,
                "review": {"verdict": "pass"}, "files_edited": 1,
                "lines_added": 12, "lines_deleted": 3,
                "role_usage": {
                    "coder": {"stage": "coder", "phase": "completed", "model": "test/model",
                              "seconds": 1.5, "attempts": 1, "usage": self.usage},
                },
                "pipeline_stages": {},
                "model_request_count": 2,
                "model_request_count_source": "aios_local_provider_loop",
                "model_request_rounds": [
                    {"sequence": 1, "role": "coder", "model": "test/model",
                     "status": "completed", "usage": {"total_tokens": 6000}, "stop_reason": "tool_calls"},
                    {"sequence": 2, "role": "coder", "model": "test/model",
                     "status": "completed", "usage": {"total_tokens": 6800}, "stop_reason": "stop"},
                ],
                "model_request_rounds_omitted": 0}

    def get_job(self, job_id):
        return self._meta()

    def stop_job(self, job_id):
        self.stopped.append(job_id)
        return {"ok": True}


def _run_one(store, task, solution, usage=None, *, strategy="auto", return_jobs=False):
    """Drive one task through the runner without spawning a real process."""
    run_id = f"test-{int(time.time() * 1000)}"
    directory = runs.run_dir(run_id)
    (directory / "jobs").mkdir(parents=True, exist_ok=True)
    document = {
        "id": run_id, "label": "", "status": "running", "created_at": time.time(),
        "updated_at": time.time(), "pid": 0, "error": "",
        "config": {**CONFIG, "counts": {task.suite: 1}, "strategy": strategy},
        "tasks": [{"id": task.id, "suite": task.suite, "title": task.title, "status": "pending",
                   "job_id": "", "passed": None, "error": "", "seconds": 0.0, "usage": {},
                   "tool_calls": 0, "checks": [], "review": "", "started_at": 0.0, "finished_at": 0.0}],
        "summary": scoring.summarise([]),
    }
    runs.write_run(document)

    run = runner.Run(directory)
    jobs = FakeJobs(directory / "jobs", solution, usage or {
        "input_tokens": 12000, "cached_input_tokens": 6000, "output_tokens": 800,
        "reasoning_tokens": 200, "total_tokens": 12800, "cost_usd": 0.0009,
    })
    runner.run_task(run, jobs, task)
    result = runs.get_run(run_id)
    return (result, jobs) if return_jobs else result


def test_a_solved_task_is_recorded_with_exact_tokens_and_named_checks(store):
    task = suites.select({"bugfix": 1})[0]
    run = _run_one(store, task, REFERENCE[task.id])

    row = run["tasks"][0]
    assert row["passed"] is True
    assert row["status"] == "passed"
    assert row["usage"]["total_tokens"] == 12800
    assert row["usage"]["cached_input_tokens"] == 6000
    assert row["role_usage"]["coder"]["usage"]["total_tokens"] == 12800
    assert row["model_request_count"] == 2
    assert row["model_request_count_source"] == "aios_local_provider_loop"
    assert [request["stop_reason"] for request in row["model_request_rounds"]] == ["tool_calls", "stop"]
    assert row["review"] == "pass"
    assert row["checks"] and all(check["passed"] for check in row["checks"])
    # Thinking is not a tool call; one real activity means one.
    assert row["tool_calls"] == 1
    assert run["summary"]["passed"] == 1
    assert run["summary"]["score"] > 0


def test_runner_passes_the_run_strategy_to_the_code_job(store):
    task = suites.select({"bugfix": 1})[0]
    run, jobs = _run_one(
        store,
        task,
        REFERENCE[task.id],
        strategy="distributed",
        return_jobs=True,
    )

    assert run["tasks"][0]["passed"] is True
    assert jobs.created_strategy == "distributed"


def test_native_adapter_uses_the_same_workspace_hidden_grader_and_live_job(store, monkeypatch):
    task = suites.select({"bugfix": 1})[0]
    run_id = f"native-{int(time.time() * 1000)}"
    directory = runs.run_dir(run_id)
    (directory / "jobs").mkdir(parents=True, exist_ok=True)
    document = {
        "id": run_id, "label": "", "status": "running", "created_at": time.time(),
        "updated_at": time.time(), "pid": 0, "error": "",
        "config": {
            **CONFIG, "engine": "codex", "provider": "codex", "model": "",
            "reasoning": "auto", "counts": {"bugfix": 1}, "max_cost_usd": 0.5,
            "cost_provenance": "unavailable",
        },
        "tasks": [{
            "id": task.id, "suite": task.suite, "title": task.title, "status": "pending",
            "job_id": "", "passed": None, "error": "", "seconds": 0.0, "usage": {},
            "cost_provenance": "unavailable", "tool_calls": 0, "checks": [], "review": "",
            "started_at": 0.0, "finished_at": 0.0,
        }],
        "summary": scoring.summarise([]),
    }
    runs.write_run(document)
    captured = {}

    def fake_native(engine, workspace, prompt, model, reasoning, job_dir, timeout,
                    max_cost_usd=0, should_stop=None):
        captured.update({
            "engine": engine, "workspace": Path(workspace), "prompt": prompt,
            "model": model, "reasoning": reasoning, "budget": max_cost_usd,
        })
        for name, body in REFERENCE[task.id].items():
            (Path(workspace) / name).write_text(body, encoding="utf-8")
        Path(job_dir).mkdir(parents=True, exist_ok=True)
        (Path(job_dir) / "job.json").write_text(json.dumps({
            "id": Path(job_dir).name, "status": "completed",
            "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        }), encoding="utf-8")
        (Path(job_dir) / "events.jsonl").write_text(json.dumps({
            "kind": "activity", "activity_type": "files", "activity_id": "edit-1",
        }) + "\n", encoding="utf-8")
        return {
            "status": "completed", "usage": {
                "input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 20,
                "reasoning_tokens": 0, "total_tokens": 120, "cost_usd": 0.0,
            },
            "error": "", "tool_calls": 1, "files_edited": 1, "lines_added": 2,
            "lines_deleted": 1, "cost_provenance": "unavailable", "timed_out": False,
            "model": "codex-primary", "primary_model": "codex-primary",
            "models_used": ["codex-primary", "codex-helper"],
            "model_request_count": None, "model_request_count_source": "unavailable",
            "model_request_rounds": [], "model_request_rounds_omitted": 0,
        }

    monkeypatch.setattr(runner.adapters, "run_native", fake_native)
    runner.run_task(runner.Run(directory), object(), task)

    result = runs.get_run(run_id)
    row = result["tasks"][0]
    assert row["passed"] is True
    assert row["status"] == "passed"
    assert row["job_id"].startswith("native-codex-")
    assert row["tool_calls"] == 1
    assert row["usage"]["total_tokens"] == 120
    assert row["model"] == "codex-primary"
    assert row["native_primary_model"] == "codex-primary"
    assert row["native_models_used"] == ["codex-primary", "codex-helper"]
    assert row["model_request_count"] is None
    assert row["model_request_count_source"] == "unavailable"
    assert row["cost_provenance"] == "unavailable"
    assert result["summary"]["cost_usd"] is None
    assert captured["engine"] == "codex"
    assert captured["workspace"] == runs.workspace_of(run_id, task.id)
    assert captured["model"] == ""
    assert captured["reasoning"] == "auto"
    assert captured["budget"] == pytest.approx(0.5)


def test_native_adapter_failure_keeps_the_real_error_instead_of_only_the_hidden_check(store, monkeypatch):
    task = suites.select({"bugfix": 1})[0]
    run_id = f"native-failure-{int(time.time() * 1000)}"
    directory = runs.run_dir(run_id)
    (directory / "jobs").mkdir(parents=True, exist_ok=True)
    runs.write_run({
        "id": run_id, "label": "", "status": "running", "created_at": time.time(),
        "updated_at": time.time(), "pid": 0, "error": "",
        "config": {
            **CONFIG, "engine": "codex", "provider": "codex", "model": "gpt-test",
            "reasoning": "high", "counts": {"bugfix": 1}, "max_cost_usd": 0.0,
            "cost_provenance": "unavailable",
        },
        "tasks": [{
            "id": task.id, "suite": task.suite, "title": task.title, "status": "pending",
            "job_id": "", "passed": None, "error": "", "seconds": 0.0, "usage": {},
            "cost_provenance": "unavailable", "tool_calls": 0, "checks": [], "review": "",
            "started_at": 0.0, "finished_at": 0.0,
        }],
        "summary": scoring.summarise([]),
    })

    monkeypatch.setattr(runner.adapters, "run_native", lambda *args, **kwargs: {
        "status": "failed",
        "error": "unexpected argument '--ignore-user-config' found",
        "usage": {
            "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
            "reasoning_tokens": 0, "total_tokens": 0, "cost_usd": 0.0,
        },
        "tool_calls": 0, "files_edited": 0, "lines_added": 0,
        "lines_deleted": 0, "cost_provenance": "unavailable", "timed_out": False,
    })

    runner.run_task(runner.Run(directory), object(), task)

    row = runs.get_run(run_id)["tasks"][0]
    assert row["passed"] is False
    assert row["status"] == "failed"
    assert row["job_status"] == "failed"
    assert row["agent_error"] == "unexpected argument '--ignore-user-config' found"
    assert row["error"].startswith("native adapter failed: unexpected argument")
    assert "hidden grader" in row["error"]


def test_a_task_the_agent_ignored_fails_with_the_check_that_caught_it(store):
    task = suites.select({"bugfix": 1})[0]
    run = _run_one(store, task, None)

    row = run["tasks"][0]
    assert row["passed"] is False
    assert row["status"] == "failed"
    assert any(not check["passed"] for check in row["checks"])
    assert row["error"]
    # Tokens are still counted. A failed attempt is not a free one.
    assert row["usage"]["total_tokens"] == 12800
    assert run["summary"]["score"] == 0.0


def test_deleting_a_run_removes_gits_read_only_objects(store):
    """Deleting a run used to fail on Windows and leave the run behind.

    Each task workspace is a git repo so the UI can show a diff, and git marks
    everything under `.git/objects` read-only, which plain rmtree refuses to
    remove.
    """
    run_id = f"delete-{int(time.time() * 1000)}"
    directory = runs.run_dir(run_id)
    objects = directory / "work" / "task" / ".git" / "objects" / "02"
    objects.mkdir(parents=True, exist_ok=True)
    blob = objects / "e0fab0ff44f560a28242ad5f683d23b3f507e3"
    blob.write_bytes(b"x")
    blob.chmod(stat.S_IREAD)
    runs.write_run({
        "id": run_id, "label": "", "status": "completed", "created_at": time.time(),
        "updated_at": time.time(), "pid": 0, "error": "", "config": CONFIG,
        "tasks": [], "summary": scoring.summarise([]),
    })

    assert runs.delete_run(run_id)["ok"] is True
    assert not directory.exists()


def test_a_task_that_crashes_is_recorded_rather_than_left_running(store):
    """The bug that made a run lie about itself.

    A task that raised was left saying "running" with `passed` still null, and
    scoring skips anything unfinished -- so a real run where one of two tasks
    crashed on a file lock reported a score of 100. A crash is a failure of
    that task and has to be counted as one.
    """
    task = suites.select({"bugfix": 1})[0]

    class Exploding(FakeJobs):
        def create_job(self, *args, **kwargs):
            raise OSError("the process cannot access the file")

    run_id = f"crash-{int(time.time() * 1000)}"
    directory = runs.run_dir(run_id)
    (directory / "jobs").mkdir(parents=True, exist_ok=True)
    runs.write_run({
        "id": run_id, "label": "", "status": "running", "created_at": time.time(),
        "updated_at": time.time(), "pid": 0, "error": "",
        "config": {**CONFIG, "counts": {"bugfix": 1}},
        "tasks": [{"id": task.id, "suite": task.suite, "title": task.title, "status": "pending",
                   "job_id": "", "passed": None, "error": "", "seconds": 0.0, "usage": {},
                   "tool_calls": 0, "checks": [], "review": "", "started_at": 0.0, "finished_at": 0.0}],
        "summary": scoring.summarise([]),
    })
    runner.run_task(runner.Run(directory), Exploding(directory / "jobs", None, {}), task)

    row = runs.get_run(run_id)["tasks"][0]
    assert row["status"] == "failed"
    assert row["passed"] is False
    assert "crashed" in row["error"]
    summary = runs.get_run(run_id)["summary"]
    assert summary["finished"] == 1 and summary["passed"] == 0
    assert summary["score"] == 0.0


def test_editing_a_frozen_file_fails_the_task_even_with_a_working_fix(store):
    task = suites.select({"bugfix": 1})[0]
    solution = dict(REFERENCE[task.id])
    solution[task.protected[0]] = "# I rewrote the test\n"
    run = _run_one(store, task, solution)

    row = run["tasks"][0]
    assert row["passed"] is False
    assert any(task.protected[0] in check["name"] and not check["passed"] for check in row["checks"])


# ----------------------------------------------------------------- transcript


def test_the_transcript_reader_uses_the_same_cursor_protocol_as_code(store):
    task = suites.select({"bugfix": 1})[0]
    run = _run_one(store, task, REFERENCE[task.id])
    run_id = run["id"]

    first = runs.read_task_events(run_id, task.id, 0)
    assert first["ok"] and first["events"] and first["size"] > 0
    # Reading from the cursor returns nothing new, exactly like code_jobs.
    assert runs.read_task_events(run_id, task.id, first["size"])["events"] == []
    # A cursor past the end means the file was replaced: rewind and say so.
    beyond = runs.read_task_events(run_id, task.id, first["size"] + 10_000)
    assert beyond["reset"] is True

    assert runs.read_task_events(run_id, "nope/nope", 0)["ok"] is False
    assert runs.read_task_events("nosuchrun", task.id, 0)["ok"] is False


# ------------------------------------------------------------------ lifecycle


def test_stopping_asks_the_runner_rather_than_killing_it(store, monkeypatch):
    monkeypatch.setattr(runs.subprocess, "Popen", lambda *a, **k: type("P", (), {"pid": 1})())
    created = runs.create_run(CONFIG)
    run_id = created["run"]["id"]

    assert runs.stop_run(run_id)["stopping"] is True
    # Results already paid for survive: the runner writes its final summary.
    assert (runs.run_dir(run_id) / "STOP").exists()
    assert runs.get_run(run_id)["status"] == "running"


def test_stopping_a_group_asks_every_child_runner_to_stop(store, monkeypatch):
    monkeypatch.setattr(runs.subprocess, "Popen", lambda *a, **k: type("P", (), {"pid": 2})())
    result = runs.create_run_group(
        {"kind": "custom", "prompt": "Build it.", "timeout": 120},
        [
            {"id": "one", "name": "One", "roles": {"coder": {"model": "a/model"}}},
            {"id": "two", "name": "Two", "roles": {"coder": {"model": "b/model"}}},
        ],
        "Comparison",
    )
    group = result["group"]
    stopped = runs.stop_target(group["id"])
    assert stopped["ok"] and stopped["stopping"]
    assert set(stopped["run_ids"]) == set(group["run_ids"])
    assert all((runs.run_dir(run_id) / "STOP").exists() for run_id in group["run_ids"])
    assert runs.get_run_group(group["id"])["status"] == "stopping"


def test_parallel_group_continuation_merges_only_matching_passes_and_runs_the_rest(store, monkeypatch):
    pid = iter(range(100, 110))
    monkeypatch.setattr(
        runs.subprocess, "Popen",
        lambda *a, **k: type("P", (), {"pid": next(pid)})(),
    )
    created = runs.create_run_group(
        {
            "kind": "suite", "counts": {"tweak": 1, "bugfix": 1},
            "concurrency": 1, "timeout": 120, "max_cost_usd": 0.8,
        },
        [
            {"id": "one", "name": "One", "roles": {"coder": {"model": "same/model"}}},
            {"id": "two", "name": "Two", "roles": {"coder": {"model": "same/model"}}},
        ],
        "Interrupted proof",
    )
    assert created["ok"], created
    source = created["group"]
    for child in source["runs"]:
        child["status"] = "stopped"
        child["tasks"][0].update({
            "status": "passed", "passed": True, "seconds": 12.5,
            "job_id": "old-job", "usage": {"total_tokens": 1234, "cost_usd": 0.01},
        })
        child["tasks"][1].update({"status": "failed", "passed": False, "error": "interrupted"})
        runs.write_run(child)

    continued = runs.continue_run_group_parallel(source["id"], concurrency=6)

    assert continued["ok"], continued
    assert continued["continued_from_group"] == source["id"]
    assert continued["seeded_results"] == 2
    assert continued["remaining_tasks"] == 2
    assert continued["concurrency"] == 6
    merged = continued["group"]
    assert merged["id"] != source["id"]
    assert merged["continued_from_group"] == source["id"]
    assert merged["seeded_task_count"] == 2
    assert merged["task_set_hash"] == source["task_set_hash"]
    assert merged["finished"] == 2
    assert merged["passed"] == 2
    for child in merged["runs"]:
        assert child["config"]["concurrency"] == 6
        assert child["config"]["task_ids"] == [child["tasks"][1]["id"]]
        assert child["continued_from_run"]
        seeded, pending = child["tasks"]
        assert seeded["seeded"] is True
        assert seeded["passed"] is True
        assert seeded["job_id"] == ""
        assert seeded["seeded_job_id"] == "old-job"
        assert seeded["usage"]["total_tokens"] == 1234
        assert pending["passed"] is None
        assert pending["status"] == "pending"
    # The audit source remains exactly where it was; continuation is additive.
    original = runs.get_run_group(source["id"])
    assert original["status"] == "stopped"
    assert all(run["tasks"][1]["passed"] is False for run in original["runs"])


def test_manual_review_fix_continues_the_completed_task_with_explicit_instruction(store, monkeypatch):
    commands = []

    def fake_popen(command, **kwargs):
        commands.append(command)
        return type("P", (), {"pid": 77})()

    monkeypatch.setattr(runs.subprocess, "Popen", fake_popen)
    created = runs.create_run({
        "kind": "custom",
        "prompt": "Build it.",
        "models": [{"provider": "openrouter", "model": "a/model", "reasoning": "off"}],
        "timeout": 120,
    })
    run = created["run"]
    run["status"] = "completed"
    run["tasks"][0].update(status="passed", passed=True, job_id="job-1")
    runs.write_run(run)

    result = runs.continue_run(
        run["id"], task_id=run["tasks"][0]["id"], instruction="Fix the review finding.",
    )
    assert result["ok"] and result["manual_fix"] is True
    command = commands[-1]
    assert "--instruction-file" in command
    path = Path(command[command.index("--instruction-file") + 1])
    assert path.read_text(encoding="utf-8") == "Fix the review finding."


def test_a_run_that_lost_its_process_is_reported_as_interrupted(store, monkeypatch):
    monkeypatch.setattr(runs.subprocess, "Popen", lambda *a, **k: type("P", (), {"pid": 1})())
    created = runs.create_run(CONFIG)
    run_id = created["run"]["id"]

    document = runs.get_run(run_id)
    document["updated_at"] = time.time() - runs.STALE_AFTER - 10
    runs._atomic_json(runs.run_dir(run_id) / "run.json", document)
    monkeypatch.setattr(runs, "_process_alive", lambda pid: False)

    settled = runs.get_run(run_id)
    assert settled["status"] == "interrupted"
    assert settled["tasks"][0]["status"] == "interrupted"
    # Reading must not rewrite the file; the runner owns it.
    on_disk = json.loads((runs.run_dir(run_id) / "run.json").read_text(encoding="utf-8"))
    assert on_disk["status"] == "running"


def test_a_running_run_cannot_be_deleted(store, monkeypatch):
    monkeypatch.setattr(runs.subprocess, "Popen", lambda *a, **k: type("P", (), {"pid": 1})())
    created = runs.create_run(CONFIG)
    run_id = created["run"]["id"]

    assert runs.delete_run(run_id)["ok"] is False
    document = runs.get_run(run_id)
    document["status"] = "completed"
    runs._atomic_json(runs.run_dir(run_id) / "run.json", document)
    assert runs.delete_run(run_id)["ok"] is True
    assert not runs.run_dir(run_id).exists()


def test_the_run_list_is_newest_first_and_carries_the_headline_numbers(store, monkeypatch):
    monkeypatch.setattr(runs.subprocess, "Popen", lambda *a, **k: type("P", (), {"pid": 1})())
    first = runs.create_run(CONFIG, label="one")["run"]["id"]
    time.sleep(0.01)
    second = runs.create_run(CONFIG, label="two")["run"]["id"]

    listing = runs.list_runs()
    assert [row["label"] for row in listing][:2] == ["two", "one"]
    assert {row["id"] for row in listing} == {first, second}
    assert set(listing[0]) >= {"score", "grade", "cost_usd", "total_tokens", "tokens_per_pass"}


# -------------------------------------------------------------------- the CLI


def test_the_cli_and_the_page_share_one_definition_of_a_run():
    from bench import run_bench

    assert run_bench.parse_counts("") == suites.DEFAULT_COUNTS
    assert run_bench.parse_counts("humaneval=6,bugfix=2") == {"humaneval": 6, "bugfix": 2}
    with pytest.raises(SystemExit):
        run_bench.parse_counts("nonsense=1")


def test_the_runner_module_starts_without_a_display():
    """It runs as a detached subprocess; an import-time failure is invisible."""
    result = subprocess.run(
        [sys.executable, "-c", "import bench.runner, bench.run_bench, bench.suites"],
        cwd=str(Path(__file__).resolve().parent.parent), capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
