from pathlib import Path
from types import SimpleNamespace

from aios_ui import server


ROOT = Path(__file__).resolve().parent.parent
CODE_JS = (ROOT / "aios_ui" / "web" / "js" / "code.js").read_text(encoding="utf-8")
CODE_CSS = (ROOT / "aios_ui" / "web" / "css" / "code.css").read_text(encoding="utf-8")
MODELS_JS = (ROOT / "aios_ui" / "web" / "js" / "models.js").read_text(encoding="utf-8")
BENCH_JS = (ROOT / "aios_ui" / "web" / "js" / "bench.js").read_text(encoding="utf-8")


def test_composer_offers_the_four_exact_backend_strategy_values():
    expected = {
        "auto": "AUTO",
        "direct": "DIRECT",
        "planned": "PLAN",
        "distributed": "TEAM",
    }

    for value, label in expected.items():
        assert f'data-strategy="{value}"' in CODE_JS
        assert f">{label}</button>" in CODE_JS
    assert 'this.turnStrategy = "auto";' in CODE_JS
    assert 'aria-label="Turn strategy"' in CODE_JS


def test_strategy_is_sent_for_launch_and_follow_up_then_reset_after_success():
    # Launch, follow-up, and the Config modal all receive the same selected mode.
    assert CODE_JS.count("strategy: this.turnStrategy,") == 3
    assert 'api("/api/code/jobs", {' in CODE_JS
    assert '/messages`, {' in CODE_JS
    assert 'this.setTurnStrategy("auto");' in CODE_JS


def test_strategy_control_is_compact_and_has_a_selected_state():
    assert ".composer-strategy" in CODE_CSS
    assert ".composer-strategy-btn.selected" in CODE_CSS
    assert '.composer-strategy-btn[aria-pressed="true"]' in CODE_CSS


def test_active_session_ui_separates_steering_from_queued_followups():
    assert 'Steer now &#9656;' in CODE_JS
    assert ">Queue next</button>" in CODE_JS
    assert 'urgent: deliveryMode === "steer_now"' in CODE_JS
    assert 'deliveryMode === "queue_next"' in CODE_JS
    assert "delivery-receipt" in CODE_JS


def test_lifecycle_banner_uses_backend_truth_and_current_turn_clock():
    assert "export function deriveSessionState" in CODE_JS
    assert 'source.turn_started_at || source.started_at' in CODE_JS
    for label in ("Waiting for you", "Done with warning", "Done", "Queued"):
        assert label in CODE_JS
    assert "Stopped by you. Review the transcript for any unfinished checks." in CODE_JS
    assert 'label: status === "interrupted" ? "Interrupted" : "Stopped"' in CODE_JS
    assert ".code-session-state" in CODE_CSS


def test_completed_session_surfaces_unverified_telemetry_without_downgrading_backend_status():
    completed = CODE_JS.split('if (status === "completed") {', 1)[1].split("\n  }", 1)[0]
    assert 'key: "warning"' in completed
    assert 'label: "Done · unverified"' in completed
    assert "verification.reason" in completed
    assert 'job.status === "completed"' in CODE_JS
    assert '`completed · ${verificationState}`' in CODE_JS
    assert "verificationWarning" not in CODE_JS
    assert 'cell("verification", "VERIFY"' not in CODE_JS


def test_saved_config_round_trips_and_applies_its_execution_mode():
    assert 'data-models="config-strategy"' in MODELS_JS
    assert "strategy: this.configStrategy" in MODELS_JS
    assert "strategy: cfg.strategy || \"auto\"" in MODELS_JS
    assert 'this.setTurnStrategy(config.strategy || "auto");' in CODE_JS


def test_config_and_bench_cards_disclose_adaptive_review():
    assert 'adaptiveReview ? "review adaptive" : "review off"' in MODELS_JS
    # Both BENCH selectors share one identity renderer, so the reviewer label
    # cannot drift between Fair comparison and Project test.
    assert BENCH_JS.count('adaptiveReview ? "review adaptive" : "review off"') == 1
    assert BENCH_JS.count("return renderBenchLaneChoice(row, checked") == 2


def test_ui_server_forwards_strategy_on_create_and_message(monkeypatch):
    calls = []

    def create_job(*args, **kwargs):
        calls.append(("create", args, kwargs))
        return {"ok": True, "job": {"id": "job-1"}}

    def send_message(*args, **kwargs):
        calls.append(("message", args, kwargs))
        return {"ok": True}

    fake_jobs = SimpleNamespace(create_job=create_job, send_message=send_message)
    monkeypatch.setattr(server.BRIDGE, "_code_jobs", fake_jobs)

    created = server.dispatch(
        "/api/code/jobs",
        "POST",
        {},
        {"provider": "openrouter", "cwd": "C:/repo", "brief": "fix", "strategy": "distributed"},
    )
    continued = server.dispatch(
        "/api/code/jobs/job-1/messages",
        "POST",
        {},
        {"text": "continue", "strategy": "planned"},
    )

    assert created["ok"] and continued["ok"]
    assert calls[0][2]["strategy"] == "distributed"
    assert calls[1][2]["strategy"] == "planned"


def test_ui_server_defaults_missing_strategy_to_auto(monkeypatch):
    calls = []
    fake_jobs = SimpleNamespace(
        create_job=lambda *args, **kwargs: calls.append(kwargs) or {"ok": True},
    )
    monkeypatch.setattr(server.BRIDGE, "_code_jobs", fake_jobs)

    result = server.dispatch(
        "/api/code/jobs",
        "POST",
        {},
        {"provider": "openrouter", "cwd": "C:/repo", "brief": "fix"},
    )

    assert result["ok"]
    assert calls[0]["strategy"] == "auto"


def test_ollama_keeps_the_reasoning_level_it_was_configured_with():
    """A local model must not have "off" silently upgraded to "medium".

    The ollama provider row carries no model catalogue, so the effort list in
    configurationCoderChoice collapsed to ["medium"], "off" was not in it, and
    every local session started with thinking on no matter what the preset
    said. Ollama's think flag is a boolean; there is no scale to snap to.
    """
    body = CODE_JS.split("configurationCoderChoice(", 1)[1].split("roleSupportsFast", 1)[0]

    assert 'provider === "openrouter" || provider === "ollama"' in body,         "ollama must take the branch that preserves the configured reasoning"
    assert 'reasoning: role.reasoning || "off"' in body

    # And the fallback path must never be able to veto an explicit "off".
    assert 'String(role.reasoning || "") === "off" && !efforts.includes("off")' in body


def test_saved_provider_config_never_borrows_another_providers_default_while_discovery_loads():
    """A cache-first config selection must remain a provider/model pair.

    Capability discovery runs in the background. During that window Codex,
    Claude, and Cursor used to fall through to the global ``code_default_model``
    (which can be an Ollama/OpenRouter id) instead of the selected config's
    coder model.
    """
    body = CODE_JS.split("configurationCoderChoice(", 1)[1].split("roleSupportsFast", 1)[0]

    assert "const configured = models.find" in body
    assert "if (role.model && !models.length)" in body
    assert "model: String(preferred.id || fallback)" in body
    assert "this._preferredModel" not in CODE_JS
