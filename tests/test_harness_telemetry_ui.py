from pathlib import Path

from aios_ui import harness_api


ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "aios_ui" / "web"


def test_harness_meta_describes_each_live_telemetry_group():
    payload = harness_api.dispatch("/api/harness/meta", "GET", {}, {})

    assert payload and payload["ok"] is True
    assert [row["id"] for row in payload["telemetry"]] == [
        "strategy",
        "profile",
        "verification",
        "progress",
        "tokens",
    ]
    assert all(row["name"] and row["fields"] and row["detail"] for row in payload["telemetry"])


def test_completion_verification_is_visible_in_the_harness_flow():
    payload = harness_api.dispatch("/api/harness/meta", "GET", {}, {})
    steps = [row["step"] for row in payload["flow"]]

    assert steps.index("Diff") < steps.index("Verify") < steps.index("Review")


def test_code_detail_has_a_live_telemetry_strip_and_renderer():
    source = (WEB / "js" / "code.js").read_text(encoding="utf-8")

    assert 'data-code="telemetry"' in source
    assert "renderHarnessTelemetry(job)" in source
    assert "this.renderHarnessTelemetry(job);" in source
    assert "normalizeHarnessTelemetry" in source


def test_code_telemetry_reads_the_fixed_backend_contract():
    source = (WEB / "js" / "code.js").read_text(encoding="utf-8")
    fields = {
        "task_strategy",
        "model_profile",
        "edit_mode",
        "tool_schema_mode",
        "context_mode",
        "context_budget",
        "verification",
        "passing_evidence_count",
        "failing_evidence_count",
        "completion_blocks",
        "progress",
        "no_progress_calls",
        "productive_calls",
        "redirects",
        "task_plan",
        "artifacts",
        "actual_input_tokens",
        "cached_input_tokens",
        "output_reserve_tokens",
        "compactions",
        "artifact_count",
    }

    missing = sorted(field for field in fields if field not in source)
    assert not missing, f"telemetry fields not rendered: {missing}"


def test_context_panel_distinguishes_actual_from_estimated_tokens():
    source = (WEB / "js" / "code.js").read_text(encoding="utf-8")

    for label in (
        "Current actual",
        "Current estimate",
        "Working budget",
        "Model window",
        "Output reserve",
        "Cached input",
        "Compactions",
        "Artifacts",
    ):
        assert label in source
    assert "A leading ~ marks aiOS's current estimate." in source


def test_telemetry_styles_are_compact_and_state_aware():
    css = (WEB / "css" / "code.css").read_text(encoding="utf-8")

    assert ".code-telemetry" in css
    assert ".code-telemetry-cell" in css
    for state in ("passed", "failed", "stale", "unverified", "no_progress", "hot"):
        assert f'data-state="{state}"' in css


def test_harness_page_exposes_the_telemetry_reference_section():
    source = (WEB / "js" / "harness.js").read_text(encoding="utf-8")
    css = (WEB / "css" / "harness.css").read_text(encoding="utf-8")

    assert '["telemetry", "Session telemetry"]' in source
    assert "renderTelemetry()" in source
    assert ".harness-telemetry-grid" in css
