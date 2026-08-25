"""Custom benchmarks: saved prompts you re-run against chosen models."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aios_ui import bench_api  # noqa: E402
from bench import custom, runs  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(custom, "CUSTOM_DIR", tmp_path / "custom")
    monkeypatch.setattr(runs.subprocess, "Popen", lambda *a, **k: type("P", (), {"pid": 1})())
    return tmp_path


def test_a_custom_definition_round_trips(store):
    created = custom.create_definition({
        "name": "Gearbox site",
        "prompt": "Build a cycloidal gearbox designer.",
        "notes": "compare models",
    })
    assert created["ok"], created
    custom_id = created["definition"]["id"]
    assert custom.get_definition(custom_id)["prompt"].startswith("Build a")

    updated = custom.update_definition(custom_id, {"prompt": "Build it with SVG export."})
    assert updated["ok"]
    assert custom.get_definition(custom_id)["prompt"] == "Build it with SVG export."

    listing = custom.list_definitions()
    assert listing[0]["id"] == custom_id
    assert custom.delete_definition(custom_id)["ok"]
    assert custom.get_definition(custom_id) is None


def test_custom_run_builds_one_task_per_model(store):
    created = custom.create_definition({
        "name": "Site",
        "prompt": "Make a website.",
    })
    custom_id = created["definition"]["id"]
    result = runs.create_run({
        "kind": "custom",
        "custom_id": custom_id,
        "models": [
            {"provider": "openrouter", "model": "deepseek/deepseek-v4-flash", "reasoning": "off"},
            {"provider": "claude", "model": "sonnet", "reasoning": "medium"},
        ],
        "concurrency": 2,
        "timeout": 120,
    }, label="Site")
    assert result["ok"], result
    run = result["run"]
    assert run["config"]["kind"] == "custom"
    assert run["config"]["prompt"] == "Make a website."
    assert len(run["tasks"]) == 2
    assert {task["provider"] for task in run["tasks"]} == {"openrouter", "claude"}
    assert all(task["suite"] == "custom" for task in run["tasks"])

    tasks = runs.select_tasks(run["config"])
    assert len(tasks) == 2
    assert all(task.brief == "Make a website." for task in tasks)
    assert "README.md" in tasks[0].files


def test_saved_custom_tasks_run_against_every_selected_model(store):
    created = custom.create_definition({
        "name": "UI checks",
        "title": "UI quality",
        "info": "Two repeatable interface jobs",
        "prompt": "",
        "tasks": [
            {"title": "Header", "prompt": "Build the header."},
            {"title": "Settings", "prompt": "Build settings."},
        ],
    })
    assert created["ok"], created
    config, error = runs.normalise_config({
        "kind": "custom",
        "custom_id": created["definition"]["id"],
        "models": [
            {"provider": "openrouter", "model": "a/model", "reasoning": "off"},
            {"provider": "openrouter", "model": "b/model", "reasoning": "low"},
        ],
    })
    assert not error
    tasks = runs.select_tasks(config)
    assert len(tasks) == 4
    assert {task.brief for task in tasks} == {"Build the header.", "Build settings."}
    assert config["custom_title"] == "UI quality"


def test_custom_api_routes(store):
    created = bench_api.dispatch("/api/bench/custom", "POST", {}, {
        "name": "API test",
        "prompt": "Do the thing.",
    })
    assert created["ok"], created
    custom_id = created["definition"]["id"]

    listing = bench_api.dispatch("/api/bench/custom", "GET", {}, {})
    assert any(row["id"] == custom_id for row in listing["definitions"])

    opened = bench_api.dispatch("/api/bench/custom/get", "GET", {"id": [custom_id]}, {})
    assert opened["definition"]["name"] == "API test"

    run = bench_api.dispatch("/api/bench/runs", "POST", {}, {
        "label": "API test",
        "config": {
            "kind": "custom",
            "custom_id": custom_id,
            "models": [{"provider": "openrouter", "model": "m", "reasoning": "off"}],
        },
    })
    assert run["ok"], run
    assert run["run"]["tasks"][0]["suite"] == "custom"

    summary = runs.summarise_run(run["run"])
    assert summary["kind"] == "custom"
    assert summary["custom_id"] == custom_id


def test_custom_config_needs_a_prompt_and_a_model():
    assert runs.normalise_config({"kind": "custom"})[1]
    assert "model" in runs.normalise_config({
        "kind": "custom", "prompt": "hi", "models": [],
    })[1].lower() or "model" in runs.normalise_config({
        "kind": "custom", "prompt": "hi",
    })[1].lower()


def test_ui_mentions_custom_tests():
    root = Path(__file__).resolve().parent.parent
    bench_js = (root / "aios_ui" / "web" / "js" / "bench.js").read_text(encoding="utf-8")
    assert "Custom tests" in bench_js
    assert "start-custom" in bench_js
    assert 'kind: "custom"' in bench_js
    assert "/api/bench/custom" in bench_js
