from __future__ import annotations

import pytest

from code_harness_policy import (
    DISTRIBUTED_CONTEXT_TOKENS,
    DIRECT_CONTEXT_TOKENS,
    MIN_OUTPUT_RESERVE_TOKENS,
    PLANNED_CONTEXT_TOKENS,
    classify_task,
    context_budget,
    estimate_tokens,
    named_file_references,
    resolve_model_profile,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Fix the typo in README.md", "direct"),
        ("Change the label in aios_ui/web/js/code.js", "direct"),
        ("Fix the login bug", "planned"),
        ("Refactor authentication across multiple files", "planned"),
        ("Research OMP and Hermes in depth and overhaul the harness", "distributed"),
        ("Migrate every package in the whole repository in parallel", "distributed"),
    ],
)
def test_classify_task_routes_by_scope(text: str, expected: str) -> None:
    strategy = classify_task(text)

    assert strategy.name == expected
    assert strategy.reasons


def test_detailed_single_file_acceptance_criteria_stay_direct() -> None:
    brief = (
        "In the sidebar the session dots are wrong in two small ways.\n\n"
        "The dot for a session you have already read should be hollow -- no fill at all,\n"
        "just a thin 1.5px border in the same grey it uses now. And the unread dot's grey\n"
        "is far too light against the dark background; darken it noticeably.\n\n"
        "Both live in `web/css/app.css`. Leave the running, waiting and error dots exactly\n"
        "as they are."
    )

    strategy = classify_task(brief)

    assert len(brief) > 320
    assert strategy.name == "direct"
    assert "specific file identified" in strategy.reasons


def test_long_vague_styling_request_does_not_become_direct_from_length_alone() -> None:
    brief = "Change the CSS styling so the interface feels better. " + ("Be thoughtful. " * 30)

    assert classify_task(brief).name == "planned"


def test_feature_discovery_with_state_and_permissions_routes_through_discovery() -> None:
    prompt = (
        "In this repository, find the homepage press-to-talk microphone button. "
        "Make it smaller and change it to tap-to-toggle while preserving microphone "
        "permissions, state indicators, accessibility, and styling conventions."
    )

    strategy = classify_task(prompt)

    assert strategy.name == "planned"
    assert strategy.use_scout is True
    assert strategy.use_planner is True


@pytest.mark.parametrize(
    "brief",
    [
        "Center the icon in `web/css/app.css`.",
        "The primary button should be blue in `web/css/app.css`.",
        "Sort sessions by created_at in `web/js/sessions.js`, newest first.",
        "Show whole seconds under a minute in `console/status.py`.",
        "Hide the empty label in `web/index.html`.",
        "Set the card padding to 12px in `web/css/cards.css`.",
        "Move the heading above the actions in `web/index.html`.",
        "Reduce the empty-state heading opacity.",
        "Set the card padding to 12px.",
        "Change the heading text to Create project.",
        "Change the race car color in `web/css/cars.css`.",
    ],
)
def test_common_bounded_edits_route_direct(brief: str) -> None:
    assert classify_task(brief).name == "direct"


@pytest.mark.parametrize(
    "brief",
    [
        "Fix the authentication protocol in `src/auth.py`.",
        "Update the streaming state machine in `src/stream.py`.",
        "Change permission checks in `src/access.py`.",
        "Fix the concurrency race in `src/worker.py`.",
        "Implement transaction rollback in `src/store.py`.",
        "Update the serializer protocol in `src/wire.py`.",
        "Preserve backward compatibility while changing `src/api.py`.",
        "Fix the state_machine transition in `src/stream.py`.",
        "Make `src/api.py` backward-compatible with v1.",
        "Fix the race-condition in `src/worker.py`.",
    ],
)
def test_risky_named_file_changes_stay_planned(brief: str) -> None:
    strategy = classify_task(brief)

    assert strategy.name == "planned"
    assert strategy.use_planner is True
    assert "risk-sensitive" in strategy.reasons[0]


def test_existing_session_operational_followup_skips_replanning() -> None:
    strategy = classify_task(
        "and can you switch it right now to the headphones please",
        continuation=True,
    )

    assert strategy.name == "direct"
    assert strategy.use_scout is False
    assert strategy.use_planner is False
    assert "operational follow-up" in strategy.reasons[0]


def test_short_vague_initial_change_gets_discovery_without_a_restatement_plan() -> None:
    strategy = classify_task(
        "Make the VR audio toggle restore my headphones when Assetto Corsa quits normally"
    )

    assert strategy.name == "planned"
    assert strategy.use_scout is True
    assert strategy.use_planner is False
    assert "scoped discovery" in strategy.reasons[0]


@pytest.mark.parametrize(
    "brief",
    [
        "actually make it blue instead",
        "also change that heading text",
        "and make sure it is centered",
    ],
)
def test_short_context_dependent_followups_reuse_the_session_map(brief: str) -> None:
    strategy = classify_task(brief, continuation=True)

    assert strategy.name == "direct"
    assert strategy.use_scout is False
    assert strategy.use_planner is False


@pytest.mark.parametrize(
    "brief",
    [
        "deploy it to production right now",
        "actually update the script and restart it now",
        "drop the database right now",
    ],
)
def test_high_impact_or_code_changing_followups_do_not_take_operational_shortcut(brief: str) -> None:
    strategy = classify_task(brief, continuation=True)

    assert "operational follow-up" not in " ".join(strategy.reasons)


def test_scoped_exercise_with_read_only_references_stays_direct() -> None:
    brief = (
        "Complete the pinned exercise in `phone_number.py`. Read "
        "`.docs/instructions.md` and `.docs/instructions.append.md`, run "
        "`python -m unittest phone_number_test.py`, and do not edit the test. "
        + ("The referenced files are the exact specification. " * 10)
    )

    strategy = classify_task(brief)

    assert len(brief) > 320
    assert strategy.name == "direct"
    assert strategy.use_scout is False
    assert strategy.use_planner is False


def test_planned_task_with_named_files_skips_redundant_scout() -> None:
    strategy = classify_task(
        "Refactor the authentication design in `src/auth.py` and `tests/test_auth.py`."
    )

    assert strategy.name == "planned"
    assert strategy.use_scout is False
    assert strategy.use_planner is True


def test_project_wide_symbol_rename_routes_planned() -> None:
    brief = (
        "Rename `transform` to `apply_stage` everywhere it is defined and everywhere it is called. "
        "The old name must not appear anywhere in Python source or `plugins.json`."
    )

    strategy = classify_task(brief)

    assert strategy.name == "planned"
    assert any("cross-file symbol migration" in reason for reason in strategy.reasons)


@pytest.mark.parametrize("name", ["direct", "planned", "distributed"])
def test_explicit_strategy_override_wins(name: str) -> None:
    strategy = classify_task(f"[{name}] overhaul the entire repository")

    assert strategy.name == name
    assert "override" in strategy.reasons[0]


def test_strategy_capabilities_and_context_caps() -> None:
    direct = classify_task("Fix a typo in README.md")
    planned = classify_task("Implement an uncertain feature across multiple files")
    distributed = classify_task("Overhaul the repo with parallel workstreams")

    assert (direct.use_scout, direct.use_planner, direct.allow_subagents) == (False, False, False)
    assert (planned.use_scout, planned.use_planner, planned.allow_subagents) == (True, True, False)
    assert (distributed.use_scout, distributed.use_planner, distributed.allow_subagents) == (True, True, True)
    assert direct.working_context_tokens == DIRECT_CONTEXT_TOKENS
    assert planned.working_context_tokens == PLANNED_CONTEXT_TOKENS
    assert distributed.working_context_tokens == DISTRIBUTED_CONTEXT_TOKENS


@pytest.mark.parametrize(
    "model",
    [
        "deepseek/deepseek-v4-flash",
        "xiaomi/mimo-v2",
        "moonshot/kimi-k2",
        "stepfun/step-3.7-flash",
    ],
)
def test_edit_mode_is_adapted_for_replacement_models(model: str) -> None:
    assert resolve_model_profile(model).edit_mode == "robust_replace"


def test_gemini_uses_inline_tool_descriptors() -> None:
    profile = resolve_model_profile("google/gemini-3-flash", context_tokens=1_000_000)

    assert profile.tool_schema_mode == "inline_tool_descriptors"
    assert profile.context_tokens == 1_000_000
    assert profile.conservative is False


@pytest.mark.parametrize("model", ["deepseek/deepseek-chat", "local/qwen", "ollama/codestral"])
def test_deepseek_and_local_models_use_append_only_context(model: str) -> None:
    assert resolve_model_profile(model).context_mode == "append_only_context"


def test_unknown_model_gets_conservative_defaults() -> None:
    profile = resolve_model_profile("vendor/new-model")

    assert profile.edit_mode == "exact_replace"
    assert profile.tool_schema_mode == "native_tool_schemas"
    assert profile.context_mode == "bounded_context"
    assert profile.conservative is True


def test_estimate_tokens_is_empty_safe_and_monotonic() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("short") > 0
    assert estimate_tokens("short " * 100) > estimate_tokens("short")
    assert estimate_tokens("\u6f22\u5b57\u3068\u30b3\u30fc\u30c9") > 0


@pytest.mark.parametrize("name", ["direct", "planned", "distributed"])
def test_known_model_window_is_not_artificially_capped_by_task_strategy(name: str) -> None:
    strategy = classify_task(f"[{name}] do the task")
    budget = context_budget(strategy, 200_000)

    assert budget.output_reserve_tokens == 30_000
    assert budget.working_tokens == 170_000
    assert budget.working_tokens + budget.output_reserve_tokens == budget.window_tokens


def test_context_budget_uses_strategy_cap_when_window_is_unknown() -> None:
    strategy = classify_task("[planned] implement this")
    budget = context_budget(strategy, 0)

    assert budget.window_tokens == 0
    assert budget.output_reserve_tokens == 0
    assert budget.working_tokens == strategy.working_context_tokens


def test_context_budget_handles_small_known_windows_without_overflow() -> None:
    """A small window must still leave room to work in.

    The flat 16k output reserve is sized for cloud models; applied to a small
    window it claimed the whole thing and left no history at all, which is why
    a local 32k model compacted on every single round.
    """
    budget = context_budget("direct", 8_000)

    assert budget.output_reserve_tokens <= 8_000, "reserve must never exceed the window"
    assert budget.output_reserve_tokens == 2_000
    assert budget.working_tokens == 6_000

    tight = context_budget("direct", 32_768)
    assert tight.output_reserve_tokens == 8_192
    assert tight.working_tokens == 24_576


def test_named_file_references_preserve_order_and_deduplicate() -> None:
    assert named_file_references(
        "Change `web/js/app.js`, keep web/css/app.css stable, then recheck web/js/app.js."
    ) == ["web/js/app.js", "web/css/app.css"]


def test_real_ollama_tags_are_recognised_as_local() -> None:
    """Local models are named `qwen3:14b` or `hf.co/...`, never `ollama/x`.

    The prefix check only knew the latter shape, so every locally served model
    was treated as an unknown hosted one: no append-only context and, worse, no
    fuzzy replace -- which a quantized model needs most, since a stray space in
    `old_text` costs it an edit and another read it can barely afford at 32k.
    """
    for model in ("qwen3:14b", "qwen3.6-agent:27b", "qwen3-vl:30b",
                  "hf.co/huihui-ai/Huihui-Qwen3.8-27B-abliterated-GGUF:UD-Q2_K_XL"):
        profile = resolve_model_profile(model)
        assert profile.context_mode == "append_only_context", model
        assert profile.edit_mode == "robust_replace", model
        assert profile.conservative is False, model


def test_hosted_model_ids_are_not_mistaken_for_local_ones() -> None:
    """A vendor-prefixed id must keep its hosted handling."""
    for model in ("openai/gpt-5.6-luna", "google/gemini-3-flash",
                  "z-ai/glm-5.2", "qwen/qwen3.7-flash", "stealth/ox-alpha"):
        profile = resolve_model_profile(model)
        assert profile.context_mode == "bounded_context", model
        assert profile.edit_mode == "exact_replace", model
