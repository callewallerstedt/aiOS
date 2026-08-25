"""The BENCH page's contract with the backend, and with the CODE tab.

Two things are worth pinning beyond the routes. First, that BENCH renders
benchmark sessions with the *same* transcript engine as CODE rather than a
second copy that will drift. Second, that opening BENCH does not become the tab
aiOS reopens on -- it is a tool for the harness, not a place you live in.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aios_ui import bench_api  # noqa: E402
from bench import runs, scoring, suites  # noqa: E402

WEB = ROOT / "aios_ui" / "web"
CODE_JS = (WEB / "js" / "code.js").read_text(encoding="utf-8")
BENCH_JS = (WEB / "js" / "bench.js").read_text(encoding="utf-8")
BENCH_CSS = (WEB / "css" / "bench.css").read_text(encoding="utf-8")
APP_JS = (WEB / "js" / "app.js").read_text(encoding="utf-8")


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(runs.subprocess, "Popen", lambda *a, **k: type("P", (), {"pid": 1})())
    return tmp_path


# ------------------------------------------------------------------- routing


def test_dispatch_returns_none_for_routes_it_does_not_own():
    assert bench_api.dispatch("/api/code/jobs", "GET", {}, {}) is None
    assert bench_api.dispatch("/api/bench/nope", "POST", {}, {}) is None


def test_the_ui_server_hands_bench_routes_to_this_module(monkeypatch):
    from aios_ui import server

    monkeypatch.setattr(bench_api, "dispatch", lambda *a: {"ok": True, "mine": True})
    assert server.dispatch("/api/bench/runs", "GET", {}, {})["mine"] is True


def test_meta_tells_the_form_what_it_may_offer(monkeypatch):
    monkeypatch.setattr(bench_api.adapters, "catalogue", lambda: [])
    meta = bench_api.dispatch("/api/bench/meta", "GET", {}, {})
    assert meta["ok"]
    assert {entry["id"] for entry in meta["suites"]} == set(suites.SUITES)
    for entry in meta["suites"]:
        assert entry["max"] >= entry["default"] >= 0
        assert entry["detail"], "every suite has to say what it measures"
    # The score is only defensible if the page can show its workings.
    assert meta["scoring"]["weights"] == scoring.WEIGHTS
    assert meta["scoring"]["reference"]["tokens_per_pass"] == scoring.TOKEN_REFERENCE
    assert meta["limits"]["concurrency"] == runs.MAX_CONCURRENCY


def test_meta_exposes_the_native_adapter_catalogue(monkeypatch):
    catalogue = [
        {"id": "aios", "ready": True, "cost_provenance": "provider_reported"},
        {"id": "claude", "ready": False, "cost_provenance": "provider_reported"},
    ]
    monkeypatch.setattr(bench_api.adapters, "catalogue", lambda: catalogue)

    meta = bench_api.dispatch("/api/bench/meta", "GET", {}, {})

    assert meta["adapters"] == catalogue
    assert meta["harnesses"] == catalogue


def test_meta_exposes_campaign_defaults_and_backend_limits(monkeypatch):
    monkeypatch.setattr(bench_api.adapters, "catalogue", lambda: [])

    meta = bench_api.dispatch("/api/bench/meta", "GET", {}, {})

    assert meta["campaign_defaults"] == {
        "counts": {
            "tweak": 1,
            "bugfix": 1,
            "feature": 0,
            "precision": 0,
            "hard": 0,
            "humaneval": 1,
            "aider_polyglot": 1,
        },
        "concurrency": 4,
        "max_cost_usd": 0.75,
        "native_max_cost_usd": 0.45,
        "timeout": 600,
        "repetitions": 1,
    }
    assert meta["limits"]["max_cost_usd"] == runs.MAX_COST_CEILING_USD
    assert meta["limits"]["repetitions"] == 3


def test_parallel_continuation_route_forwards_group_and_concurrency(monkeypatch):
    captured = {}

    def continue_group(group_id, concurrency=4):
        captured.update(group_id=group_id, concurrency=concurrency)
        return {"ok": True, "group": {"id": "new-group"}}

    monkeypatch.setattr(runs, "continue_run_group_parallel", continue_group)
    result = bench_api.dispatch(
        "/api/bench/parallel-continue", "POST", {},
        {"id": "old-group", "concurrency": 7},
    )

    assert result["ok"] is True
    assert captured == {"group_id": "old-group", "concurrency": 7}


def test_creating_a_run_rejects_a_config_the_form_should_have_caught(store):
    result = bench_api.dispatch("/api/bench/runs", "POST", {}, {"config": {"provider": "openrouter"}})
    assert result["ok"] is False
    assert "model" in result["error"]


def test_the_run_routes_walk_a_run_from_start_to_deletion(store):
    created = bench_api.dispatch("/api/bench/runs", "POST", {}, {
        "label": "smoke",
        "config": {"provider": "openrouter", "model": "m", "reasoning": "off",
                   "counts": {"bugfix": 1}, "concurrency": 1, "timeout": 120},
    })
    assert created["ok"], created
    run_id = created["run"]["id"]

    listing = bench_api.dispatch("/api/bench/runs", "GET", {}, {})
    assert [row["id"] for row in listing["runs"]] == [run_id]
    assert listing["runs"][0]["label"] == "smoke"

    opened = bench_api.dispatch("/api/bench/run", "GET", {"id": [run_id]}, {})
    assert opened["run"]["tasks"][0]["suite"] == "bugfix"
    assert bench_api.dispatch("/api/bench/run", "GET", {"id": ["ghost"]}, {})["ok"] is False

    assert bench_api.dispatch("/api/bench/stop", "POST", {}, {"id": run_id})["ok"] is True
    # Deleting a live run is refused; the sessions are still running.
    assert bench_api.dispatch("/api/bench/delete", "POST", {}, {"id": run_id})["ok"] is False


def test_task_ids_travel_as_a_query_parameter_not_a_path(store):
    """`bugfix/rounding` has a slash in it, which a path segment cannot carry."""
    created = bench_api.dispatch("/api/bench/runs", "POST", {}, {
        "config": {"provider": "openrouter", "model": "m", "reasoning": "off",
                   "counts": {"bugfix": 1}},
    })
    run_id = created["run"]["id"]
    task_id = created["run"]["tasks"][0]["id"]
    assert "/" in task_id

    result = bench_api.dispatch(
        "/api/bench/events", "GET", {"run": [run_id], "task": [task_id], "since": ["0"]}, {}
    )
    assert result["ok"] is True
    assert result["events"] == []  # nothing has run yet, but the task is known


# ------------------------------------------------------------------ the page


def test_code_offers_a_bench_button_that_opens_the_page():
    assert 'data-code="bench"' in CODE_JS
    assert '>BENCH<' in CODE_JS
    assert 'this.shell.show("BENCH")' in CODE_JS


def test_the_shell_mounts_the_bench_page():
    assert "BenchTab" in APP_JS
    assert 'name === "BENCH"' in APP_JS


def test_saved_model_config_prefills_the_real_bench_page():
    models_js = (WEB / "js" / "models.js").read_text(encoding="utf-8")
    assert "pendingBenchConfig" in models_js
    assert 'this.shell.show("BENCH")' in models_js
    assert "window.__app" not in models_js
    assert "saved_config_roles" in BENCH_JS
    assert "Saved configuration:" in BENCH_JS
    assert 'data-bench-config="' in BENCH_JS
    assert "MODEL CONFIGURATION" in BENCH_JS


def test_custom_bench_uses_saved_full_configurations_not_model_rows():
    custom_view = BENCH_JS.split("async showCustom(", 1)[1].split("\n  newCustom()", 1)[0]
    assert 'data-bench="edit-configs"' in custom_view
    assert 'data-bench="custom-configs"' in custom_view
    assert "Saved tasks" not in custom_view
    assert 'data-bench="model-rows"' not in custom_view
    assert "configurations: selectedConfigs" in BENCH_JS
    assert "configurations: selectedConfigs" in BENCH_JS


def test_multi_config_custom_benchmark_is_one_live_comparison_group():
    assert 'api("/api/bench/groups"' in BENCH_JS
    assert "openGroup(result.group.id)" in BENCH_JS
    assert 'data-bench="group-pane"' in BENCH_JS
    for metric in ["total_tokens", "cost_usd", "tool_calls", "files_edited"]:
        assert metric in BENCH_JS


def test_bench_releases_its_state_stream_while_fetching_details():
    request = BENCH_JS.split("  async request(path, options = {}) {", 1)[1].split(
        "\n  // ------------------------------------------------------------- runs rail", 1
    )[0]
    boot = BENCH_JS.split("  async boot() {", 1)[1].split("\n  connect() {", 1)[0]

    assert "this.stateStream.close()" in request
    assert "return await api(path, options)" in request
    assert "if (!this.destroyed) this.connect()" in request
    assert 'const listing = await api("/api/bench/groups")' in boot
    assert boot.index('const listing = await api("/api/bench/groups")') < boot.index("await this.openGroup")
    assert 'await this.request(`/api/bench/group?id=' in BENCH_JS
    assert 'await this.request(`/api/bench/run?id=' in BENCH_JS


def test_native_primary_and_auxiliary_models_are_rendered_separately():
    assert "task.native_primary_model || task.model" in BENCH_JS
    assert "task.native_models_used.filter" in BENCH_JS
    assert "auxiliary:" in BENCH_JS


def test_comparison_group_is_live_and_can_stop_every_configuration():
    assert '"starting", "running", "stopping"' in BENCH_JS
    assert 'data-bench="stop"${active && !stopping' in BENCH_JS
    assert "Stop all ${children.length}" in BENCH_JS
    assert "this.runId || this.groupId" in BENCH_JS
    assert 'class="bench-group-overview" aria-live="polite"' in BENCH_JS
    assert "bench-group-progress" in BENCH_JS
    assert "Agent #${String(agentId).padStart(3" in BENCH_JS
    assert "Preview port ${previewPort}" in BENCH_JS
    assert 'data-bench="parallel-continue"' in BENCH_JS
    assert 'this.request("/api/bench/parallel-continue"' in BENCH_JS
    assert "completed results merged" in BENCH_JS


def test_campaign_ui_enforces_selection_and_suite_count_invariants():
    campaign = BENCH_JS.split("  showCampaign() {", 1)[1].split("\n  collectCampaignSetup()", 1)[0]
    collector = BENCH_JS.split("  collectCampaignSetup() {", 1)[1].split("\n  saveCampaignSetup()", 1)[0]
    renderer = BENCH_JS.split("export function renderBenchLaneChoice(", 1)[1].split("\n}\n\nexport class BenchTab", 1)[0]

    assert "const active = !!checked && identity.ready;" in renderer
    assert '${active ? " selected" : ""}' in renderer
    assert '${active ? " checked" : ""}' in renderer
    assert '${checked ? " selected" : ""}' not in campaign
    assert 'return renderBenchLaneChoice(row, checked, "campaign")' in campaign
    assert "suiteMax.has(suiteId)" in collector
    assert "Math.min(limit, Math.trunc(Number(input.value) || 0))" in collector
    assert "input.value = String(count);" in collector
    assert 'data-campaign-field="concurrency"' in campaign
    assert "config.concurrency" in BENCH_JS


def test_campaign_native_rows_use_catalogue_defaults_and_openrouter_copy():
    campaign = BENCH_JS.split("  showCampaign() {", 1)[1].split("\n  collectCampaignSetup()", 1)[0]
    starter = BENCH_JS.split("  async startCampaign() {", 1)[1].split(
        "\n  // ------------------------------------------------------------- custom", 1
    )[0]

    assert 'selected = ["harness-balanced-engineering", "codex", "claude"]' in campaign
    assert "Raw Codex, Claude, OMP, and Hermes" in campaign
    assert "including aiOS, OMP, and Hermes" in campaign
    assert "Non-OpenRouter native ceiling" in campaign
    assert "provider: row.default_provider || row.provider || row.id" in starter
    assert 'model: row.default_model || row.model || ""' in starter
    assert 'reasoning: row.default_reasoning || row.reasoning || "medium"' in starter
    assert "provider: row.id" not in starter


def test_project_lane_renderer_keeps_native_identity_and_disabled_state_honest():
    script = f"""
globalThis.location = {{ search: "" }};
const {{ BenchTab, renderBenchLaneChoice }} = await import({json.dumps((WEB / "js" / "bench.js").as_uri())});
const catalogue = BenchTab.prototype.campaignHarnesses.call({{
  savedConfigs: [{{ id: "saved", name: "Saved engineering", strategy: "planned",
    roles: {{ reviewer: {{ enabled: true }}, coder: {{ model: "model/exact" }} }} }}],
  meta: {{ harnesses: [
    {{ id: "aios", version: "worktree.123" }},
    {{ id: "codex", label: "Codex CLI (raw)", ready: true, version: "codex 1",
      default_reasoning: "high", tool_profile: "built-in Codex CLI defaults" }},
    {{ id: "claude", label: "Claude Code (raw)", ready: true, version: "claude 2",
      default_reasoning: "high", tool_profile: "Bash,Edit,Read,Write,Glob,Grep" }},
    {{ id: "omp", label: "Oh My Pi (raw)", ready: true, version: "omp/17.2.11",
      default_model: "openrouter/deepseek/test", default_reasoning: "high",
      tool_profile: "read,bash,edit", cost_provenance: "provider_reported" }},
    {{ id: "hermes", label: "Hermes Agent (raw)", ready: false, version: "Hermes 0.20",
      default_reasoning: "high", tool_profile: "file,terminal", reason: "not installed" }},
  ] }},
}});
const natives = Object.fromEntries(catalogue.filter((row) => row.lane_type === "native")
  .map((row) => [row.id, renderBenchLaneChoice({{
    ...row, roles: {{ reviewer: {{ enabled: true }} }},
  }}, true, "project")]));
const native = natives.omp;
const unavailable = natives.hermes;
const saved = renderBenchLaneChoice({{
  id: "saved", kind: "config", lane_type: "aios", name: "Saved engineering",
  ready: true, strategy: "planned", version: "worktree.123",
  roles: {{ reviewer: {{ enabled: true }} }}, model: "model/exact",
}}, true, "project");
console.log(JSON.stringify({{ native, unavailable, saved, catalogue, natives }}));
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True,
    )
    rendered = json.loads(completed.stdout)

    native = rendered["native"]
    assert "Oh My Pi (raw) · reasoning high" in native
    assert "omp/17.2.11 · tools: read,bash,edit · provider reported" in native
    assert 'data-harness-engine="omp"' in native
    assert " checked" in native
    assert "aiOS" not in native and "review adaptive" not in native

    catalogue = rendered["catalogue"]
    assert next(row for row in catalogue if row["id"] == "saved")["lane_type"] == "aios"
    assert {row["id"] for row in catalogue if row["lane_type"] == "native"} == {
        "codex", "claude", "omp", "hermes",
    }
    for engine, html in rendered["natives"].items():
        assert f'data-harness-engine="{engine}"' in html
        assert "reasoning high" in html
        assert "tools:" in html
        assert "aiOS" not in html and "review adaptive" not in html

    unavailable = rendered["unavailable"]
    assert 'aria-disabled="true"' in unavailable
    assert " disabled" in unavailable
    assert " checked" not in unavailable
    assert 'class="bench-campaign-harness selected' not in unavailable

    saved = rendered["saved"]
    assert "aiOS · planned · review adaptive" in saved
    assert 'data-lane-type="aios"' in saved


def test_campaign_report_hides_partial_medians_until_attempts_finish():
    report = BENCH_JS.split("  groupReport(group) {", 1)[1].split(
        "\n  // -------------------------------------------------------------- open run", 1
    )[0]

    assert "const metricsReady = Number(harness.attempt_count || 0) > 0" in report
    assert "Number(harness.pending_count || 0) === 0" in report
    assert report.count("!metricsReady") >= 3


def test_review_requires_an_explicit_fix_click_and_continues_the_same_task():
    transcript_js = (ROOT / "aios_ui" / "web" / "js" / "transcript.js").read_text(encoding="utf-8")
    assert "data-review-fix" in transcript_js
    assert ">FIX</button>" in transcript_js
    assert "onReviewFix" in transcript_js
    assert 'this.request("/api/bench/continue"' in BENCH_JS
    assert "task: this.taskId, instruction" in BENCH_JS
    assert "Auto-fix review" not in (ROOT / "aios_ui" / "web" / "js" / "models.js").read_text(encoding="utf-8")


def test_bench_is_never_the_tab_aios_reopens_on():
    """It is reached from CODE; booting into it would strand you there.

    HARNESS joined it later, so the exclusion is a set both belong to rather
    than a comparison against one name.
    """
    body = APP_JS.split("show(name) {")[1].split("\n  }")[0]
    assert "BENCH" in APP_JS.split("CODE_PAGES = new Set(")[1].split(")")[0]
    assert "!this.phoneMirror && !CODE_PAGES.has(name)" in body
    assert 'api("/api/config", { method: "POST"' in body
    assert "active_tab" in body


def test_bench_reuses_the_code_transcript_rather_than_copying_it():
    assert 'from "./transcript.js"' in BENCH_JS
    assert "new Transcript(" in BENCH_JS
    assert 'from "./transcript.js"' in CODE_JS
    # The engine lives in exactly one place now.
    for method in ["paintActivity(", "streamAssistant(", "advanceAssistant("]:
        assert method not in BENCH_JS
        assert method not in CODE_JS


def test_the_page_shows_exact_token_counts_not_only_rounded_ones():
    """`13.2K tokens` is not an answer to "what did that cost me"."""
    assert "toLocaleString()" in BENCH_JS
    for field in ["input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "total_tokens"]:
        assert field in BENCH_JS, field


def test_each_benchmark_configuration_shows_per_role_usage_live():
    assert "roleMetrics(task" in BENCH_JS
    assert "task.role_usage" in BENCH_JS
    assert "run.saved_config_roles" in BENCH_JS
    for role in ("scout", "planner", "coder", "reviewer"):
        assert role in BENCH_JS
    assert ".bench-role-metric" in BENCH_CSS


def test_task_detail_exposes_the_bounded_efficiency_trace():
    assert 'data-bench="task-efficiency"' in BENCH_JS
    assert "efficiencyTrace(task)" in BENCH_JS
    for field in (
        "total_calls", "time_to_first_edit_seconds", "calls_before_first_edit",
        "calls_after_first_edit", "failed_calls", "duplicate_calls", "retry_calls",
        "overlapping_read_calls", "post_edit_inspection_calls", "tools_by_role",
        "tools_by_type", "tools_by_name", "sequence", "omitted_calls",
    ):
        assert field in BENCH_JS, field
    assert ".bench-efficiency-trace" in BENCH_CSS
    assert ".bench-tool-sequence" in BENCH_CSS
    for field in (
        "model_request_count", "model_request_count_source", "model_request_rounds",
        "model_request_rounds_omitted", "stop_reason",
    ):
        assert field in BENCH_JS, field
    assert ".bench-model-rounds" in BENCH_CSS


def test_the_score_can_always_be_explained():
    assert 'data-bench="score-help"' in BENCH_JS
    assert "How the score works" in BENCH_JS


def test_the_page_ships_its_stylesheet():
    assert 'href="css/bench.css"' in (WEB / "index.html").read_text(encoding="utf-8")
    assert (WEB / "css" / "bench.css").exists()
