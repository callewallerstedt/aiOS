"""Four roles, four models, and a plan that reaches the coder.

A CODE session is not one model's work. Scouts sweep, a planner decides, a coder
writes, a reviewer checks -- and the right model is a different one for each,
because the economics are different. The planner sees a few thousand tokens once
and should be the smartest model in the run; the coder re-sends its whole
context on every round and should be the cheapest one that can do the job.

Two properties matter more than the wiring:

  * a role's configuration can never be invalid. The file is hand-editable and
    survives upgrades, so every read forces model, reasoning and enabled back
    into range rather than trusting what it finds;
  * the planner can never break a session. It is an optional stage in front of
    work the operator asked for -- if it is off, unreachable, or returns
    nothing, the turn must proceed exactly as it would have without it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import code_jobs  # noqa: E402
import code_roles  # noqa: E402
import openrouter_client  # noqa: E402


@pytest.fixture
def config(tmp_path, monkeypatch):
    """A private helper_config.json for both modules that read one."""
    path = tmp_path / "helper_config.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(code_roles, "CONFIG_PATH", path)
    monkeypatch.setattr(code_jobs, "CONFIG_PATH", path)
    return path


@pytest.fixture
def job(tmp_path, monkeypatch):
    monkeypatch.setattr(code_jobs, "JOBS_DIR", tmp_path / "jobs")
    instance = code_jobs.CodeJob("roles-1")
    instance.directory.mkdir(parents=True, exist_ok=True)
    instance.save(cwd=str(tmp_path), provider="openrouter")
    return instance


# --------------------------------------------------------------- the config


def test_every_role_is_present_and_complete(config):
    roles = code_roles.load_roles()
    assert set(roles) == set(code_roles.ROLE_KEYS)
    for row in roles.values():
        assert row["model"] and row["reasoning"] in code_roles.VALID_REASONING
        assert isinstance(row["enabled"], bool) and isinstance(row["fast"], bool)


def test_the_roles_do_not_all_default_to_one_model(config):
    """Shipping four roles pointed at the same model would make the split
    decorative -- the whole point is that they get different ones."""
    roles = code_roles.load_roles()
    assert len({row["model"] for row in roles.values()}) >= 3


def test_the_planner_is_off_until_asked_for(config):
    """It costs a call and adds latency in front of every turn."""
    assert code_roles.load_roles()["planner"]["enabled"] is False


def test_the_coder_cannot_be_switched_off(config):
    config.write_text(json.dumps({"code_roles": {"coder": {"enabled": False}}}), encoding="utf-8")
    assert code_roles.load_roles()["coder"]["enabled"] is True


def test_a_nonsense_reasoning_level_falls_back(config):
    config.write_text(json.dumps({"code_roles": {"coder": {"reasoning": "turbo"}}}), encoding="utf-8")
    assert code_roles.load_roles()["coder"]["reasoning"] == code_roles.DEFAULT_ROLES["coder"]["reasoning"]


@pytest.mark.parametrize("reasoning", ["minimal", "max", "ultra"])
def test_discovered_provider_reasoning_levels_survive_config_round_trip(config, reasoning):
    config.write_text(json.dumps({"code_roles": {"coder": {"reasoning": reasoning}}}), encoding="utf-8")
    assert code_roles.load_roles()["coder"]["reasoning"] == reasoning


def test_an_empty_model_falls_back_rather_than_launching_blank(config):
    config.write_text(json.dumps({"code_roles": {"coder": {"model": "   "}}}), encoding="utf-8")
    assert code_roles.load_roles()["coder"]["model"] == code_roles.DEFAULT_ROLES["coder"]["model"]


def test_an_upgrade_keeps_the_models_already_configured(config):
    """Roles did not exist before. An install that had chosen its models must
    not silently switch them on first read."""
    config.write_text(json.dumps({
        "code_default_model": "moonshotai/kimi-k3",
        "code_subagent_model": "google/gemini-3.5-flash-lite",
        "code_review_model": "qwen/qwen3.8-max",
        "code_review_enabled": False,
    }), encoding="utf-8")
    roles = code_roles.load_roles()
    assert roles["coder"]["model"] == "moonshotai/kimi-k3"
    assert roles["scout"]["model"] == "google/gemini-3.5-flash-lite"
    assert roles["reviewer"]["model"] == "qwen/qwen3.8-max"
    assert roles["reviewer"]["enabled"] is False


def test_explicit_roles_win_over_the_legacy_keys(config):
    config.write_text(json.dumps({
        "code_default_model": "moonshotai/kimi-k3",
        "code_roles": {"coder": {"model": "qwen/qwen3.8-max"}},
    }), encoding="utf-8")
    assert code_roles.load_roles()["coder"]["model"] == "qwen/qwen3.8-max"


def test_saving_one_field_leaves_the_others_alone(config):
    merged = code_roles.save_roles({"planner": {"enabled": True}})
    assert merged["planner"]["enabled"] is True
    assert merged["planner"]["model"] == code_roles.DEFAULT_ROLES["planner"]["model"]
    assert merged["coder"] == code_roles.load_roles()["coder"]


def test_saved_model_configs_are_complete_clean_snapshots(config):
    rows = code_roles.save_model_config({
        "name": "  Cheap UI work  ",
        "description": "  Fast changes  ",
        "provider": "codex",
        "roles": {"coder": {"model": "gpt-5.6-sol", "reasoning": "off", "enabled": False}},
    }, {})
    saved = rows[0]
    assert saved["name"] == "Cheap UI work"
    assert saved["description"] == "Fast changes"
    assert saved["provider"] == "codex"
    assert saved["strategy"] == "auto"
    assert saved["review_fix"] is False
    assert saved["show_in_composer"] is True
    assert saved["origin"] == "user"
    assert saved["show_in_composer_explicit"] is False
    assert set(saved["roles"]) == set(code_roles.ROLE_KEYS)
    assert saved["roles"]["coder"]["model"] == "gpt-5.6-sol"
    assert saved["roles"]["coder"]["enabled"] is True


def test_model_config_review_fix_round_trips(config):
    rows = code_roles.save_model_config({
        "name": "With review loop",
        "review_fix": True,
        "roles": {},
    }, {})
    assert rows[0]["review_fix"] is True
    loaded = code_roles.load_model_configs({"model_configs": rows})
    assert loaded[0]["review_fix"] is True


def test_model_config_composer_visibility_round_trips(config):
    rows = code_roles.save_model_config({
        "name": "Bench only",
        "show_in_composer": False,
        "roles": {},
    }, {})
    assert rows[0]["show_in_composer"] is False
    assert rows[0]["show_in_composer_explicit"] is True
    loaded = code_roles.load_model_configs({"model_configs": rows})
    assert loaded[0]["show_in_composer"] is False


def test_model_config_strategy_is_cleaned_and_preserved_on_partial_update(config):
    data = {}
    rows = code_roles.save_model_config({
        "id": "team-build",
        "name": "Team Build",
        "strategy": "team",
        "roles": {},
    }, data)
    assert rows[0]["strategy"] == "distributed"

    data["model_configs"] = rows
    updated = code_roles.save_model_config({
        "id": "team-build",
        "name": "Team Build renamed",
        "roles": rows[0]["roles"],
    }, data)
    assert updated[0]["strategy"] == "distributed"

    invalid = code_roles.save_model_config({"name": "Safe fallback", "strategy": "reckless", "roles": {}}, {})
    assert invalid[0]["strategy"] == "auto"


def test_saving_without_an_id_duplicates_a_model_config(config):
    data = {}
    first = code_roles.save_model_config({"name": "Daily", "roles": {}}, data)
    data["model_configs"] = first
    rows = code_roles.save_model_config({
        "name": "Daily copy",
        "provider": first[0]["provider"],
        "roles": first[0]["roles"],
        "show_in_composer": first[0]["show_in_composer"],
    }, data)
    assert len(rows) == 2
    assert rows[0]["id"] != rows[1]["id"]
    assert rows[0]["roles"] == rows[1]["roles"]


def test_bench_runs_restore_missing_saved_model_configs(config, monkeypatch):
    monkeypatch.setattr(
        code_roles,
        "_bench_recovered_configs",
        lambda existing: [{
            "id": "abc123",
            "name": "Test 1",
            "description": "Restored from benchmark history",
            "origin": "benchmark_history",
            "show_in_composer": False,
            "show_in_composer_explicit": False,
            "provider": "openrouter",
            "review_fix": True,
            "roles": code_roles.save_roles({}, {}),
            "created_at": 100.0,
            "updated_at": 100.0,
        }] if not existing else [],
    )
    rows = code_roles.load_model_configs({})
    assert len(rows) == 1
    assert rows[0]["id"] == "abc123"
    assert rows[0]["review_fix"] is True
    assert rows[0]["origin"] == "benchmark_history"
    assert rows[0]["show_in_composer"] is False


def test_scanned_bench_configs_default_to_history_only(tmp_path, monkeypatch):
    import bench.runs

    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "20260823-test"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({
        "saved_config_id": "bench-loser",
        "saved_config_name": "Benchmark candidate",
        "saved_config_roles": code_roles.save_roles({}, {}),
        "provider": "openrouter",
        "config": {"strategy": "direct"},
        "created_at": 10,
        "updated_at": 20,
    }), encoding="utf-8")
    monkeypatch.setattr(bench.runs, "RUNS_DIR", runs_dir)

    rows = code_roles._scan_bench_model_configs()

    assert len(rows) == 1
    assert rows[0]["origin"] == "benchmark_history"
    assert rows[0]["show_in_composer"] is False
    assert rows[0]["show_in_composer_explicit"] is False


def test_legacy_recovered_config_migrates_hidden_until_user_promotes_it(config, monkeypatch):
    monkeypatch.setattr(code_roles, "_bench_recovered_configs", lambda _existing: [])
    data = {"model_configs": [{
        "id": "legacy-bench",
        "name": "Old benchmark candidate",
        "description": "Restored from benchmark history",
        # This True was synthesized by the old missing-means-visible cleaner;
        # there was no explicit visibility provenance.
        "show_in_composer": True,
        "roles": {},
        "created_at": 10,
        "updated_at": 20,
    }]}

    loaded = code_roles.load_model_configs(data)
    assert len(loaded) == 1
    assert loaded[0]["origin"] == "benchmark_history"
    assert loaded[0]["show_in_composer"] is False
    assert loaded[0]["show_in_composer_explicit"] is False

    assert code_roles.merge_recovered_model_configs(data) is True
    assert data["model_configs"][0]["show_in_composer"] is False
    assert code_roles.merge_recovered_model_configs(data) is False

    promoted = code_roles.save_model_config({
        "id": "legacy-bench",
        "name": "Adopted benchmark candidate",
        "description": "Restored from benchmark history",
        "show_in_composer": True,
        "roles": loaded[0]["roles"],
    }, data)[0]
    assert promoted["origin"] == "user"
    assert promoted["show_in_composer"] is True
    assert promoted["show_in_composer_explicit"] is True


def test_saved_model_config_requires_a_name(config):
    with pytest.raises(ValueError, match="name"):
        code_roles.save_model_config({"name": "   ", "roles": {}}, {})


def test_the_harness_reads_its_models_from_the_roles(config):
    config.write_text(json.dumps({"code_roles": {
        "reviewer": {"model": "qwen/qwen3.8-max", "reasoning": "high", "enabled": True},
        "scout": {"model": "qwen/qwen3.7-flash"},
    }}), encoding="utf-8")
    assert code_jobs.review_model_default() == "qwen/qwen3.8-max"
    assert code_jobs.review_reasoning_default() == "high"
    assert code_jobs.subagent_model_default() == "qwen/qwen3.7-flash"
    assert code_jobs.review_enabled() is True


def test_fast_mode_overrides_the_reviewers_reasoning(config):
    config.write_text(json.dumps({"code_roles": {
        "reviewer": {"model": "x/y", "reasoning": "high", "fast": True},
    }}), encoding="utf-8")
    assert code_jobs.review_reasoning_default() == "off"


@pytest.mark.parametrize(
    ("configured", "fast", "runtime"),
    [
        ("off", False, "off"),
        ("low", False, "off"),
        ("medium", False, "medium"),
        ("high", False, "high"),
        ("xhigh", False, "xhigh"),
        ("high", True, "off"),
    ],
)
def test_adaptive_review_reasoning_preserves_only_explicit_medium_or_higher(
    configured, fast, runtime,
):
    assert code_jobs.adaptive_review_reasoning({
        "reasoning": configured,
        "fast": fast,
    }) == runtime


def test_every_role_has_a_card_to_render(config):
    cards = {row["role"]: row for row in code_roles.catalogue()}
    assert set(cards) == set(code_roles.ROLE_KEYS)
    for row in cards.values():
        assert row["label"] and row["tagline"] and row["detail"]
    assert cards["coder"]["optional"] is False
    assert cards["planner"]["optional"] is True


def test_code_prompt_bar_exposes_saved_config_picker_and_settings():
    source = (ROOT / "aios_ui" / "web" / "js" / "code.js").read_text(encoding="utf-8")
    assert 'data-code="config-menu-list"' in source
    assert 'data-code="prompt-config"' in source
    assert "data-config-pill" in source
    assert "refreshModelConfigs()" in source
    assert '>Settings</strong>' in source
    assert "launchReviewFix" in source
    assert 'this.openModels();' in source
    assert 'data-code="review-fix"' not in source
    assert 'data-code="urgent"' not in source


def test_code_prompt_bar_removes_the_old_global_fast_toggle_and_manual_handoff_strip():
    source = (ROOT / "aios_ui" / "web" / "js" / "code.js").read_text(encoding="utf-8")
    assert "toggleFastMode()" in source and "roleSupportsFast" in source
    assert 'data-code="fast-mode"' not in source
    assert 'class="prompt-shell"' in source
    assert "CONTINUE WITH" not in source
    assert 'data-code="switch-provider"' not in source


def test_config_selection_auto_handoffs_and_self_review_is_a_separate_session():
    source = (ROOT / "aios_ui" / "web" / "js" / "code.js").read_text(encoding="utf-8")
    assert "/configuration`" in source
    assert 'data-code="self-review"' in source
    assert "/review`" in source
    assert "Session Reviews" in source


def test_models_window_has_provider_selector():
    source = (ROOT / "aios_ui" / "web" / "js" / "models.js").read_text(encoding="utf-8")
    assert 'data-models="provider"' in source
    assert "provider: this.provider" in source
    assert 'data-models="review-fix"' not in source
    assert "Auto-fix review" not in source
    assert "review_fix: false" in source


def test_models_window_can_edit_an_existing_saved_configuration():
    source = (ROOT / "aios_ui" / "web" / "js" / "models.js").read_text(encoding="utf-8")
    assert 'data-config-action="edit"' in source
    assert "editSavedConfig(configId)" in source
    assert "this.editingConfigId || undefined" in source


def test_models_window_is_cache_first_and_refreshes_slow_data_in_background():
    source = (ROOT / "aios_ui" / "web" / "js" / "models.js").read_text(encoding="utf-8")
    initial = source.index('api("/api/code/models")')
    background = source.index('api("/api/code/models?refresh=1")')
    assert initial < background
    assert "void this.refreshBackgroundData(loadId)" in source
    assert 'api("/api/bench/runs?limit=1000").then' in source
    assert 'api("/api/code/capabilities").then' in source


def test_saved_config_cards_duplicate_toggle_visibility_and_hide_disabled_roles():
    source = (ROOT / "aios_ui" / "web" / "js" / "models.js").read_text(encoding="utf-8")
    assert 'data-config-action="duplicate"' in source
    assert 'data-config-action="visibility"' in source
    assert "duplicateSavedConfig(configId)" in source
    assert "toggleConfigVisibility(configId)" in source
    assert "v.enabled !== false" in source


def test_saved_config_visibility_filters_composer_pills_and_removes_new_session_text():
    source = (ROOT / "aios_ui" / "web" / "js" / "code.js").read_text(encoding="utf-8")
    models = (ROOT / "aios_ui" / "web" / "js" / "models.js").read_text(encoding="utf-8")
    assert 'config.show_in_composer !== false' in source
    assert 'data-models="config-visible"' in models
    assert "show_in_composer: this.configShowInComposer" in models
    assert 'data-code="new"' not in source
    assert ">New session<" not in source


def test_unmatched_role_setup_is_not_labeled_as_the_first_saved_preset():
    source = (ROOT / "aios_ui" / "web" / "js" / "code.js").read_text(encoding="utf-8")
    assert 'visible.length ? visible[0].name' not in source
    assert 'Custom · ${shortText(this.coderChoice().model' in source


def test_fresh_session_resets_the_model_inherited_from_a_viewed_session():
    source = (ROOT / "aios_ui" / "web" / "js" / "code.js").read_text(encoding="utf-8")
    assert "this.defaultRoles = JSON.parse(JSON.stringify(result.roles || {}))" in source
    assert "if (!jobId) {\n      this.resetLaunchConfiguration();" in source
    assert "resetLaunchConfiguration()" in source


def test_model_picker_shows_live_rank_strengths_and_average_tps():
    source = (ROOT / "aios_ui" / "web" / "js" / "models.js").read_text(encoding="utf-8")
    assert "popularity_rank" in source and "#${popularity} weekly" in source
    assert "good_for" in source
    assert "openrouter_average_tps" in source


# -------------------------------------------------------------- model specs


def test_prices_are_dollars_per_million_not_per_token():
    """OpenRouter quotes per token. Nobody can read that, and rendering it raw
    put "$0.0000001" in a column meant for comparison."""
    assert openrouter_client._per_million("0.0000005") == 0.5
    assert openrouter_client._per_million("0.000002") == 2.0


def test_a_missing_price_is_blank_not_free():
    """A wrong number gets budgeted against; a blank is obviously missing."""
    assert openrouter_client._per_million(None) is None
    assert openrouter_client._per_million("") is None
    assert openrouter_client._per_million("not-a-number") is None


def test_specs_carry_what_a_choice_needs():
    rows = {row["id"]: row for row in openrouter_client.model_specs()}
    row = rows.get("deepseek/deepseek-v4-flash")
    assert row, "the default model is missing from the picker"
    for field in ("price_in", "price_out", "context_length", "reasoning", "fast", "enabled"):
        assert field in row


def test_a_catalog_refresh_cannot_erase_curated_flags(monkeypatch):
    """Live rows own price and context; the curated entry owns the judgements.
    Overwriting wholesale silently disabled fast mode on every model."""
    monkeypatch.setattr(openrouter_client, "_MODEL_CACHE", [{
        "id": "deepseek/deepseek-v4-flash",
        "label": "whatever upstream calls it",
        "context_length": 1048576,
        "reasoning": ["off"],
        "fast": False,
        "pricing": {"prompt": "0.0000001", "completion": "0.0000002"},
    }])
    monkeypatch.setattr(openrouter_client, "_MODEL_CACHE_AT", 9e18)
    row = next(r for r in openrouter_client.catalog_models() if r["id"] == "deepseek/deepseek-v4-flash")
    assert row["fast"] is True
    assert row["pricing"]["prompt"] == "0.0000001"


def test_measured_speed_is_observed_never_estimated(job, monkeypatch):
    """Vendors publish throughput. This is what happened on this machine."""
    monkeypatch.setattr(code_jobs, "list_jobs", lambda limit=400: [
        {"model": "a/b", "tokens_per_second": 50.0, "updated_at": code_jobs._now()},
        {"model": "a/b", "tokens_per_second": 70.0, "updated_at": code_jobs._now()},
        {"model": "a/b", "tokens_per_second": 60.0, "updated_at": code_jobs._now()},
        {"model": "c/d", "tokens_per_second": 0, "updated_at": code_jobs._now()},
        {"model": "e/f", "updated_at": code_jobs._now()},
    ])
    speed = code_jobs.measured_model_speed()
    assert speed["a/b"] == {"tokens_per_second": 60.0, "sessions": 3}
    assert "c/d" not in speed and "e/f" not in speed


def test_an_old_session_does_not_count_as_current_speed(job, monkeypatch):
    monkeypatch.setattr(code_jobs, "list_jobs", lambda limit=400: [
        {"model": "a/b", "tokens_per_second": 50.0, "updated_at": code_jobs._now() - 90 * 86400},
    ])
    assert code_jobs.measured_model_speed(days=28) == {}


# ------------------------------------------------------------- the planner


def test_a_disabled_planner_leaves_the_turn_untouched(job, config):
    config.write_text(json.dumps({"code_roles": {
        "scout": {"enabled": False},
        "planner": {"enabled": False},
    }}), encoding="utf-8")
    assert job._with_plan("darken the grey") == "darken the grey"


def test_the_plan_reaches_the_coder(job, config, monkeypatch):
    config.write_text(json.dumps({"code_roles": {"planner": {"enabled": True}}}), encoding="utf-8")
    monkeypatch.setattr(code_jobs.CodeJob, "_run_plan_stage", lambda self, request, role: "1. Edit app.css")
    payload = job._with_plan("Plan and redesign the settings workflow")
    assert "Plan and redesign the settings workflow" in payload
    assert "1. Edit app.css" in payload
    assert "<plan>" in payload


def test_the_coder_may_overrule_a_wrong_plan(job, config, monkeypatch):
    """A plan written without file contents can be wrong. Told to follow it
    blindly, the coder would implement the mistake and report success."""
    config.write_text(json.dumps({"code_roles": {"planner": {"enabled": True}}}), encoding="utf-8")
    monkeypatch.setattr(code_jobs.CodeJob, "_run_plan_stage", lambda self, request, role: "step")
    payload = job._with_plan("Plan the architecture and implement it")
    assert "do what is actually correct" in payload


def test_a_broken_planner_does_not_block_the_work(job, config, monkeypatch):
    config.write_text(json.dumps({"code_roles": {"planner": {"enabled": True}}}), encoding="utf-8")

    def boom(self, request, role):
        raise RuntimeError("no API key")

    monkeypatch.setattr(code_jobs.CodeJob, "_run_plan_stage", boom)
    assert job._with_plan("darken the grey") == "darken the grey"


def test_auto_route_preserves_raw_request_for_primary_coder(job, config, monkeypatch, tmp_path):
    config.write_text(json.dumps({"code_roles": {
        "scout": {"enabled": True},
        "consultant": {"enabled": True},
    }}), encoding="utf-8")

    monkeypatch.setattr(
        code_jobs.CodeJob,
        "_run_plan_stage",
        lambda *args, **kwargs: pytest.fail("consultant must not be repurposed as an automatic planner"),
    )
    monkeypatch.setattr(
        code_jobs.CodeJob,
        "_run_scout_stage",
        lambda *args, **kwargs: pytest.fail("automatic model scout must not rewrite the request"),
    )
    (tmp_path / "app.py").write_text("def login():\n    pass\n", encoding="utf-8")
    job.save(cwd=str(tmp_path))
    selected = code_jobs.code_harness_policy.classify_task("Fix the login state and permissions")

    payload = job._with_plan("Fix the login state and permissions", strategy=selected)

    assert payload == "Fix the login state and permissions"
    assert "<project_map>" not in payload
    assert "<plan>" not in payload
    assert job.load()["planning_mode"] == "coder_led"


def test_an_empty_plan_is_not_pasted_into_the_turn(job, config, monkeypatch):
    config.write_text(json.dumps({"code_roles": {"planner": {"enabled": True}}}), encoding="utf-8")
    monkeypatch.setattr(code_jobs.CodeJob, "_run_plan_stage", lambda self, request, role: "   ")
    assert job._with_plan("darken the grey") == "darken the grey"


def test_the_survey_is_names_and_symbols_not_file_bodies(job, tmp_path):
    """The planner's value is that its input is small. Feeding it file contents
    would make it just an expensive coder."""
    (tmp_path / "app.py").write_text(
        "SECRET_BODY_TEXT = 1\n\n\ndef handler(request):\n    return SECRET_BODY_TEXT\n", encoding="utf-8")
    survey = job._plan_survey(tmp_path, "handler")
    assert "app.py" in survey
    assert "SECRET_BODY_TEXT = 1" not in survey


def test_the_survey_is_capped(job, tmp_path, monkeypatch):
    monkeypatch.setattr(code_jobs, "PLAN_SURVEY_CHARS", 120)
    for index in range(60):
        (tmp_path / f"mod{index}.py").write_text(f"def thing{index}():\n    pass\n", encoding="utf-8")
    assert len(job._plan_survey(tmp_path, "thing")) <= 120


def test_a_disabled_scout_skips_the_sweep(job, config):
    config.write_text(json.dumps({"code_roles": {"scout": {"enabled": False}}}), encoding="utf-8")
    assert job._plan_scout(Path(str(job.load()["cwd"])), "anything") == ""


def test_the_scout_runs_on_openrouter_whatever_the_session_uses(job, config, monkeypatch):
    """A Codex session's scout still uses an OpenRouter model id, so it has to
    be posted to OpenRouter rather than to whatever the session is running."""
    job.save(provider="codex")
    seen = {}

    def fake_spawn(self, project, args, activity_id=""):
        seen.update(args)
        return json.dumps({"report": "web/css/app.css:161 - the dot rules"})

    monkeypatch.setattr(code_jobs.CodeJob, "_spawn_agent_tool", fake_spawn)
    report = job._plan_scout(Path(str(job.load()["cwd"])), "darken the grey")
    assert seen["provider"] == "openrouter"
    assert seen["model"] == code_roles.load_roles()["scout"]["model"]
    assert "app.css:161" in report


def test_the_planner_is_told_not_to_write_the_code():
    assert "Do not write the code" in code_jobs.PLANNER_CONTRACT
    assert "no tools" in code_jobs.PLANNER_CONTRACT.casefold()
    assert all(heading in code_jobs.PLANNER_CONTRACT for heading in (
        "PATHS", "CONTRACT TRAPS", "STEPS", "VERIFY",
    ))


def test_the_planner_is_told_to_size_the_plan_to_the_request():
    """Otherwise a one-line change comes back with four phases and a risk
    register, which is the failure this whole harness has been fighting."""
    assert "Size it to the request" in code_jobs.PLANNER_CONTRACT


def test_the_planner_must_not_invent_acceptance_values():
    contract = code_jobs.PLANNER_CONTRACT.casefold()
    assert "never weaken or reinterpret" in contract
    assert "never invent a default" in contract
    assert "original request remains authoritative" in contract


def test_planner_budget_scales_by_strategy_reasoning_and_model_profile():
    planned = code_jobs._planner_limits(
        "planned",
        {"reasoning": "off", "fast": False},
        "deepseek/deepseek-v4-flash",
    )
    distributed = code_jobs._planner_limits(
        "distributed",
        {"reasoning": "high", "fast": False},
        "vendor/unknown-planner",
    )

    assert planned == {
        "strategy": "planned",
        "target_words": code_jobs.PLAN_WORDS_PLANNED,
        "hard_max_words": code_jobs._planner_validation_limit(
            code_jobs.PLAN_WORDS_PLANNED,
            code_jobs.PLAN_VALIDATION_WORD_PERCENT,
        ),
        "target_bullets": code_jobs.PLAN_BULLETS_PLANNED,
        "hard_max_bullets": code_jobs._planner_validation_limit(
            code_jobs.PLAN_BULLETS_PLANNED,
            code_jobs.PLAN_VALIDATION_BULLET_PERCENT,
        ),
        "max_completion_tokens": None,
        "requested_reasoning": "off",
        "reasoning": "off",
        "model_profile": "known",
    }
    assert distributed["target_words"] == code_jobs.PLAN_WORDS_DISTRIBUTED
    assert distributed["hard_max_words"] > distributed["target_words"]
    assert distributed["target_bullets"] == code_jobs.PLAN_BULLETS_DISTRIBUTED
    assert distributed["hard_max_bullets"] > distributed["target_bullets"]
    assert distributed["max_completion_tokens"] is None
    assert distributed["requested_reasoning"] == "high"
    assert distributed["reasoning"] == "high"
    assert distributed["model_profile"] == "conservative"


def test_low_or_fast_planning_disables_hidden_reasoning_without_a_response_cap():
    low = code_jobs._planner_limits(
        "planned",
        {"reasoning": "low", "fast": False},
        "deepseek/deepseek-v4-flash",
    )
    fast_high = code_jobs._planner_limits(
        "planned",
        {"reasoning": "high", "fast": True},
        "deepseek/deepseek-v4-flash",
    )

    assert low["requested_reasoning"] == "low"
    assert low["reasoning"] == "off"
    assert low["max_completion_tokens"] is None
    assert fast_high["requested_reasoning"] == "high"
    assert fast_high["reasoning"] == "off"
    assert fast_high["max_completion_tokens"] is None


def test_planner_output_gate_requires_a_compact_complete_execution_map():
    limits = code_jobs._planner_limits(
        "planned",
        {"reasoning": "off", "fast": False},
        "deepseek/deepseek-v4-flash",
    )
    valid = """PATHS
- app.py :: handler :: adjust behavior
CONTRACT TRAPS
- Keep the operator's one-based error invariant exact.
STEPS
- Inspect the named symbol, then make the smallest compatible change.
VERIFY
- python -m unittest tests/test_app.py"""

    assert code_jobs._planner_output_issue(valid, limits) == ""
    assert "missing VERIFY" in code_jobs._planner_output_issue(valid.rsplit("VERIFY", 1)[0], limits)
    assert "code or pseudocode" in code_jobs._planner_output_issue(valid + "\n```python\npass\n```", limits)
    add_words = lambda total: valid + "\n" + " ".join(
        ["detail"] * max(0, total - len(valid.split()))
    )
    small_overshoot = add_words(limits["target_words"] + 5)
    assert len(small_overshoot.split()) == limits["target_words"] + 5
    assert code_jobs._planner_output_issue(small_overshoot, limits) == ""
    at_guard = add_words(limits["hard_max_words"])
    assert code_jobs._planner_output_issue(at_guard, limits) == ""
    over_guard = add_words(limits["hard_max_words"] + 1)
    assert code_jobs._planner_output_issue(over_guard, limits) == ""
    assert "length limit" in code_jobs._planner_output_issue(valid, limits, "length")


def test_plan_stage_propagates_its_compact_provider_budget(job, config, monkeypatch):
    captured = {}
    plan = """PATHS
- app.py :: handler :: adjust behavior
CONTRACT TRAPS
- Preserve the exact acceptance contract; inspect unstated values.
STEPS
- Inspect the symbol and implement the smallest compatible change.
VERIFY
- python -m unittest tests/test_app.py"""

    def stream(messages, model, **kwargs):
        captured.update({"messages": messages, "model": model, **kwargs})
        yield {
            "done": True,
            "message": {"content": plan},
            "finish_reason": "stop",
            "usage": {"prompt_tokens": 30, "completion_tokens": 40, "total_tokens": 70},
        }

    monkeypatch.setattr(openrouter_client, "provider_status", lambda **_kwargs: (True, "ready"))
    monkeypatch.setattr(openrouter_client, "stream_chat", stream)

    result = job._run_plan_stage("Plan a multi-file feature", {
        "model": "deepseek/deepseek-v4-flash",
        "reasoning": "low",
        "fast": False,
        "_use_scout": False,
        "_strategy": "planned",
    })

    assert result == plan
    assert captured["reasoning"] == "off"
    assert captured["max_completion_tokens"] is None
    assert f"Aim for about {code_jobs.PLAN_WORDS_PLANNED} words" in captured["messages"][1]["content"]
    meta = job.load()
    assert meta["planner_budget"]["target_words"] == code_jobs.PLAN_WORDS_PLANNED
    assert meta["planner_budget"]["hard_max_words"] > code_jobs.PLAN_WORDS_PLANNED
    assert meta["planner_budget"]["max_completion_tokens"] is None
    planner_round = next(row for row in meta["model_request_rounds"] if row["role"] == "planner")
    assert planner_round["reasoning"] == "off"
    assert planner_round["max_completion_tokens"] is None


def test_switching_scouts_off_withdraws_the_tool(job, config):
    """Offering a tool the operator disabled is worse than not having it: the
    model calls it, gets refused, and spends a round finding out."""
    available = lambda: set(next(
        tool["function"]["parameters"]["properties"]["names"]["items"]["enum"]
        for tool in job._ollama_tools()
        if tool["function"]["name"] == "select_tools"
    ))
    assert "spawn_agent" in available()
    config.write_text(json.dumps({"code_roles": {"scout": {"enabled": False}}}), encoding="utf-8")
    assert "spawn_agent" not in available()


def test_the_other_tools_survive_scouts_being_off(job, config):
    config.write_text(json.dumps({"code_roles": {"scout": {"enabled": False}}}), encoding="utf-8")
    names = {tool["function"]["name"] for tool in job._ollama_tools()}
    assert {"read_file", "edit_file", "search_text", "run_shell"} <= names


def test_a_review_fix_pass_is_not_replanned(job, config, monkeypatch):
    """It continues work the planner already planned. Re-planning would scout
    the repo again and write a second plan for a job in progress."""
    config.write_text(json.dumps({"code_roles": {"planner": {"enabled": True}}}), encoding="utf-8")
    monkeypatch.setattr(code_jobs.CodeJob, "_run_plan_stage",
                        lambda self, request, role: pytest.fail("planned a harness-written turn"))
    monkeypatch.setattr(code_jobs.CodeJob, "_run_openrouter", lambda self, payload, a: ("completed", payload))
    monkeypatch.setattr(code_jobs.CodeJob, "_review_completed_change", lambda self: {})
    job._queue_payload("apply the reviewer's findings", planned=False)
    job._messages.join()


def test_a_broad_operator_message_stays_primary_coder_led(job, config, monkeypatch):
    config.write_text(json.dumps({"code_roles": {"planner": {"enabled": True}}}), encoding="utf-8")
    seen = []
    monkeypatch.setattr(code_jobs.CodeJob, "_run_plan_stage",
                        lambda *args, **kwargs: pytest.fail("automatic planner rewrote the operator request"))
    monkeypatch.setattr(code_jobs.CodeJob, "_run_openrouter",
                        lambda self, payload, a: seen.append(payload) or ("completed", payload))
    monkeypatch.setattr(code_jobs.CodeJob, "_review_completed_change", lambda self: {})
    job._queue_payload("Plan and redesign the settings workflow")
    job._messages.join()
    assert seen == ["Plan and redesign the settings workflow"]


def test_an_enabled_planner_runs_even_for_a_small_operator_message(job, config, monkeypatch):
    config.write_text(json.dumps({"code_roles": {"planner": {"enabled": True}}}), encoding="utf-8")
    seen = []
    monkeypatch.setattr(code_jobs.CodeJob, "_run_plan_stage",
                        lambda self, request, role: seen.append(request) or "small edit plan")
    monkeypatch.setattr(code_jobs.CodeJob, "_run_openrouter", lambda self, payload, a: ("completed", payload))
    monkeypatch.setattr(code_jobs.CodeJob, "_review_completed_change", lambda self: {})
    job._queue_payload("darken the grey")
    job._messages.join()
    assert seen == ["darken the grey"]


def test_an_enabled_scout_runs_without_a_planner(job, config, monkeypatch):
    config.write_text(json.dumps({"code_roles": {
        "scout": {"enabled": True},
        "planner": {"enabled": False},
    }}), encoding="utf-8")
    monkeypatch.setattr(code_jobs.CodeJob, "_run_scout_stage",
                        lambda self, request: "app.css:12 - theme colour")
    payload = job._with_plan("darken the grey")
    assert "<scout_report>" in payload
    assert "app.css:12" in payload


def test_a_handoff_briefing_is_not_replanned(job, config, monkeypatch):
    """Same reason as the fix pass: it continues planned work."""
    config.write_text(json.dumps({"code_roles": {"planner": {"enabled": True}}}), encoding="utf-8")
    queued = []
    monkeypatch.setattr(job, "_queue_payload",
                        lambda payload, attachments=None, **kwargs: queued.append(kwargs.get("planned", True)))
    job._queue_payload("briefing for the next provider", [], planned=False)
    assert queued == [False]
