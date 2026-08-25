from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench import project_campaigns, runs  # noqa: E402
from aios_ui import bench_api  # noqa: E402


def _snapshot(monkeypatch, tmp_path: Path):
    store = tmp_path / "campaigns"
    monkeypatch.setattr(project_campaigns, "CAMPAIGNS_DIR", store)
    source = tmp_path / "real-project"
    source.mkdir()
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "README.md").write_text("# Demo\n", encoding="utf-8")
    result = project_campaigns.create_snapshot(source, "Change VALUE to 2.")
    assert result["ok"], result
    return source, result["campaign"]


def _run(campaign: dict, workspace: Path) -> dict:
    return {
        "id": "lane-1",
        "status": "completed",
        "config": {
            "kind": "project",
            "project_campaign_id": campaign["id"],
            "project_snapshot_hash": campaign["snapshot_hash"],
        },
        "tasks": [{"id": "project/workspace", "workspace": str(workspace)}],
    }


def test_project_snapshot_builds_an_isolated_git_workspace(monkeypatch, tmp_path):
    source, campaign = _snapshot(monkeypatch, tmp_path)
    config = {
        "kind": "project",
        "project_campaign_id": campaign["id"],
        "project_snapshot_hash": campaign["snapshot_hash"],
        "provider": "openrouter",
        "model": "example/model",
        "reasoning": "off",
    }
    normalised, error = runs.normalise_config(config)
    assert not error
    task = runs.select_tasks(normalised)[0]
    workspace = tmp_path / "lane"
    task.build(workspace)

    (workspace / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert (source / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    result = project_campaigns.workspace_diff(campaign["id"], workspace)
    assert result["modified"] == 1
    assert result["changes"][0]["path"] == "app.py"


def test_project_snapshot_validation_is_a_user_error_not_an_exception(monkeypatch, tmp_path):
    monkeypatch.setattr(project_campaigns, "CAMPAIGNS_DIR", tmp_path / "campaigns")
    missing = project_campaigns.create_snapshot("relative/project", "Do work")
    assert missing == {"ok": False, "error": "project folder must be an absolute path"}


def test_lane_files_inside_bench_run_storage_are_included_in_result(monkeypatch, tmp_path):
    bench_dir = tmp_path / "bench"
    monkeypatch.setattr(project_campaigns, "BENCH_DIR", bench_dir)
    monkeypatch.setattr(project_campaigns, "CAMPAIGNS_DIR", bench_dir / "project_campaigns")
    source = tmp_path / "real"
    source.mkdir()
    (source / "app.txt").write_text("before", encoding="utf-8")
    campaign = project_campaigns.create_snapshot(source, "Change it")["campaign"]
    workspace = bench_dir / "runs" / "lane" / "work" / "project-workspace"
    project_campaigns.task_for_config({
        "project_campaign_id": campaign["id"],
        "project_snapshot_hash": campaign["snapshot_hash"],
        "prompt": campaign["prompt"],
    }).build(workspace)
    (workspace / "app.txt").write_text("after", encoding="utf-8")
    assert project_campaigns.workspace_diff(campaign["id"], workspace)["modified"] == 1


def test_apply_requires_preview_and_creates_recoverable_checkpoint(monkeypatch, tmp_path):
    source, campaign = _snapshot(monkeypatch, tmp_path)
    workspace = tmp_path / "lane"
    project_campaigns.task_for_config({
        "project_campaign_id": campaign["id"],
        "project_snapshot_hash": campaign["snapshot_hash"],
        "prompt": campaign["prompt"],
    }).build(workspace)
    (workspace / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    monkeypatch.setattr(runs, "get_run", lambda _run_id: _run(campaign, workspace))

    preview = project_campaigns.preview_apply("lane-1")
    assert preview["ok"]
    assert preview["preview"]["ready"] == 1
    applied = project_campaigns.confirm_apply(preview["preview"]["id"])

    assert applied["ok"]
    assert (source / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    checkpoint = Path(applied["checkpoint"])
    assert (checkpoint / "files" / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert json.loads((checkpoint / "checkpoint.json").read_text(encoding="utf-8"))["source_path"] == str(source)


def test_apply_rejects_real_project_drift(monkeypatch, tmp_path):
    source, campaign = _snapshot(monkeypatch, tmp_path)
    workspace = tmp_path / "lane"
    project_campaigns.task_for_config({
        "project_campaign_id": campaign["id"],
        "project_snapshot_hash": campaign["snapshot_hash"],
        "prompt": campaign["prompt"],
    }).build(workspace)
    (workspace / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    monkeypatch.setattr(runs, "get_run", lambda _run_id: _run(campaign, workspace))

    preview = project_campaigns.preview_apply("lane-1")
    assert preview["preview"]["conflicts"] == 0
    (source / "app.py").write_text("VALUE = 99\n", encoding="utf-8")
    refused = project_campaigns.confirm_apply(preview["preview"]["id"])

    assert not refused["ok"]
    assert refused["conflicts"] == ["app.py"]
    assert (source / "app.py").read_text(encoding="utf-8") == "VALUE = 99\n"


def test_deletions_need_an_explicit_second_confirmation(monkeypatch, tmp_path):
    source, campaign = _snapshot(monkeypatch, tmp_path)
    workspace = tmp_path / "lane"
    project_campaigns.task_for_config({
        "project_campaign_id": campaign["id"],
        "project_snapshot_hash": campaign["snapshot_hash"],
        "prompt": campaign["prompt"],
    }).build(workspace)
    (workspace / "README.md").unlink()
    monkeypatch.setattr(runs, "get_run", lambda _run_id: _run(campaign, workspace))

    preview = project_campaigns.preview_apply("lane-1")
    refused = project_campaigns.confirm_apply(preview["preview"]["id"])
    assert not refused["ok"]
    assert refused["deletions"] == ["README.md"]
    applied = project_campaigns.confirm_apply(preview["preview"]["id"], allow_deletions=True)
    assert applied["ok"]
    assert not (source / "README.md").exists()


def test_project_campaign_api_snapshots_before_starting_lanes(monkeypatch, tmp_path):
    source, campaign = _snapshot(monkeypatch, tmp_path)
    captured = {}

    def fake_group(config, configurations, label=""):
        captured.update(config=config, configurations=configurations, label=label)
        return {"ok": True, "group": {"id": "group-test"}}

    monkeypatch.setattr(project_campaigns, "create_snapshot", lambda *_args: {"ok": True, "campaign": campaign})
    monkeypatch.setattr(runs, "create_run_group", fake_group)
    result = bench_api.dispatch("/api/bench/project-campaign", "POST", {}, {
        "config": {"source_path": str(source), "prompt": "Change it."},
        "configurations": [{"id": "cfg", "name": "Auto"}],
        "label": "Real task",
    })

    assert result["ok"]
    assert captured["config"]["kind"] == "project"
    assert captured["config"]["project_snapshot_hash"] == campaign["snapshot_hash"]
    stored = project_campaigns.get_campaign(campaign["id"])
    assert stored["group_id"] == "group-test"


def test_full_project_campaign_creates_one_comparable_isolated_run_per_lane(monkeypatch, tmp_path):
    monkeypatch.setattr(project_campaigns, "CAMPAIGNS_DIR", tmp_path / "campaigns")
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(runs, "_spawn", lambda *args, **kwargs: {"ok": True, "pid": 42})
    source = tmp_path / "real"
    source.mkdir()
    (source / "main.js").write_text("export const value = 1;\n", encoding="utf-8")

    created = project_campaigns.create_campaign({
        "source_path": str(source), "prompt": "Change value to 2.", "timeout": 120,
    }, [
        {"id": "direct", "name": "Direct", "strategy": "direct", "roles": {"coder": {"model": "test/model"}}},
        {"id": "auto", "name": "Auto", "strategy": "auto", "roles": {"coder": {"model": "test/model"}}},
    ], "Project proof")

    assert created["ok"], created
    group = created["group"]
    assert len(group["runs"]) == 2
    assert group["comparable"] is True
    assert len({run["task_set_hash"] for run in group["runs"]}) == 1
    assert all(run["config"]["kind"] == "project" for run in group["runs"])
    assert all(run["tasks"][0]["suite"] == "project" for run in group["runs"])


def test_project_benchmark_controls_and_apply_flow_are_visible_in_ui():
    bench_js = (ROOT / "aios_ui" / "web" / "js" / "bench.js").read_text(encoding="utf-8")
    bench_css = (ROOT / "aios_ui" / "web" / "css" / "bench.css").read_text(encoding="utf-8")
    assert 'data-bench="setup-project"' in bench_js
    assert 'data-bench="pick-project-folder"' in bench_js
    assert "/api/bench/project-campaign" in bench_js
    assert "/api/bench/project/apply-preview" in bench_js
    assert "/api/bench/project/apply-confirm" in bench_js
    assert "bench-project-change-list" in bench_css
