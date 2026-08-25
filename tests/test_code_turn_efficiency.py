"""What a CODE turn is allowed to spend.

Three real sessions motivated this file. Asked to darken a grey, pulse a dot,
and add one toggle, the harness spent 40, 47 and 172 tool rounds and 8.6M input
tokens -- and the operator killed the longest one. The waste was not in the
model's judgement about the change; it was in everything around it:

  * whole-file reads and repeated searches before a one-line CSS edit;
  * a tail of "verification" that ran the full test suite three times and
    py_compile against a .css file;
  * five git commands to confirm an edit the harness had just applied;
  * a stored result that opened with "Let me look at the theme variables"
    because every round's narration was concatenated into it;
  * a reviewer round-trip in front of the answer, on a four-line diff.

Each test below pins one of those shut.
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


@pytest.fixture
def job(tmp_path, monkeypatch):
    monkeypatch.setattr(code_jobs, "JOBS_DIR", tmp_path / "jobs")
    config_path = tmp_path / "helper_config.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(code_jobs, "CONFIG_PATH", config_path)
    monkeypatch.setattr(code_roles, "CONFIG_PATH", config_path)
    instance = code_jobs.CodeJob("t1")
    instance.directory.mkdir(parents=True, exist_ok=True)
    return instance


# ------------------------------------------------- the effort budget is stated


@pytest.mark.parametrize("builder", ["_openrouter_system_prompt", "_ollama_system_prompt"])
def test_both_local_providers_get_the_same_effort_budget(job, tmp_path, builder):
    prompt = getattr(job, builder)(tmp_path)
    assert "Size the effort to the request" in prompt
    assert "SMALL" in prompt and "MEDIUM" in prompt and "LARGE" in prompt


def test_the_budget_is_read_before_the_deep_reasoning_discipline(job, tmp_path):
    """A model that meets "reason from first principles" first will do that to a
    one-line colour change. Order is the whole point of the block."""
    prompt = job._openrouter_system_prompt(tmp_path)
    assert prompt.index("Size the effort") < prompt.index("first principles")


def test_narration_between_tool_calls_is_forbidden(job, tmp_path):
    prompt = job._openrouter_system_prompt(tmp_path)
    assert "Do not narrate" in prompt


def test_verification_is_told_to_stay_proportional(job, tmp_path):
    prompt = job._openrouter_system_prompt(tmp_path)
    assert "Never run the whole suite" in prompt
    assert "Do not run git diff" in prompt
    assert "Do not create a verifier script" in prompt
    assert "Stop after the edit" in prompt


def test_web_search_is_scoped_to_third_party_apis(job, tmp_path):
    """Asked for a darker grey, one session searched the web. The evidence rule
    is about external services, never about files sitting on disk."""
    prompt = job._openrouter_system_prompt(tmp_path)
    assert "never web_search for this repository's own code" in prompt.casefold()


# -------------------------------------------- task-sized tool schema


def _tool_names(tools):
    return {tool["function"]["name"] for tool in tools}


def test_small_presentation_task_gets_exact_compact_edit_tools(job):
    tools = job._ollama_tools("Make the settings button slightly darker")
    names = _tool_names(tools)
    assert names == set(code_jobs.CORE_CONTENT_TOOL_NAMES) | {code_jobs.TOOL_SELECTOR_NAME}
    assert not ({
        "run_shell", "list_dir", "find_files", "find_symbol", "outline_file",
        "repo_map", "code_intelligence", "spawn_agent", "update_plan",
    } & names)
    assert {"web_search", "fetch_url"}.isdisjoint(names)


@pytest.mark.parametrize(
    "prompt",
    [
        "Change one CSS colour in web/css/app.css",
        "Fix the typo in README.md",
        "Update the heading copy and aria-label on the settings button",
        "Adjust the fill colour in assets/settings.svg",
    ],
)
def test_localized_content_edits_share_one_generic_compact_profile(job, prompt):
    profile, enabled = job._tool_profile(prompt)

    assert profile == "direct-content"
    assert enabled == code_jobs.DIRECT_CONTENT_TOOL_NAMES


@pytest.mark.parametrize(
    "prompt",
    [
        "Fix app.py typo",
        "[direct] Fix the submit behavior in index.html",
        "[direct] Create a new stylesheet theme.css",
        "Find which file styles the settings button and make it darker",
        "[direct] Replace assets/logo.png",
    ],
)
def test_source_behavior_new_file_and_uncertain_direct_tasks_keep_escape_hatches(job, prompt):
    profile, enabled = job._tool_profile(prompt)

    assert profile == "direct"
    assert enabled == code_jobs.STANDARD_TOOL_NAMES


def test_named_target_existence_distinguishes_edit_from_unannounced_creation(job, tmp_path):
    existing = tmp_path / "web" / "css" / "app.css"
    existing.parent.mkdir(parents=True)
    existing.write_text("body {}\n", encoding="utf-8")
    job.save(cwd=str(tmp_path))

    assert job._tool_profile("Change web/css/app.css colour")[0] == "direct-content"
    assert job._tool_profile("Add web/css/theme.css")[1] == code_jobs.STANDARD_TOOL_NAMES


def test_compact_profile_reduces_native_schema_bytes_by_at_least_forty_percent(job):
    compact = job._ollama_tools("Change one CSS colour in web/css/app.css")
    source = job._ollama_tools("Fix app.py typo")
    encoded = lambda tools: len(json.dumps(tools, separators=(",", ":")).encode("utf-8"))

    assert encoded(compact) < encoded(source) * 0.6


def test_compact_schema_and_runtime_authorization_use_the_same_tool_set(job):
    prompt = "Change one CSS colour in web/css/app.css"
    job._configure_turn_policy(prompt, strategy="direct")
    _profile, enabled = job._tool_profile(prompt)
    job._turn_enabled_tools = enabled

    denied = json.loads(job._guard_before_tool("run_shell", {"command": "git status"}))
    assert denied["guardrail"] == "tool_not_enabled"
    assert job._guard_before_tool("edit_file", {"relative_path": "web/css/app.css"}) == ""


def test_gemini_inline_descriptors_match_the_active_compact_schema(job, tmp_path):
    prompt = "Change one CSS colour in web/css/app.css"
    job.save(model="google/gemini-3.1-pro-preview")
    job._configure_turn_policy(prompt, strategy="direct")

    system = job._openrouter_system_prompt(tmp_path)

    assert "- edit_file:" in system
    assert "- search_text:" in system
    assert "- run_shell:" not in system
    assert "- find_files:" not in system


def test_small_task_prompt_allows_safe_search_to_edit_without_duplicate_read():
    assert "edit from its revision immediately" in code_jobs.EFFORT_RULES
    assert "Do not read again when a search result already contains every current line" in code_jobs.EFFORT_RULES
    assert "search_text return revisions" in code_jobs.ANTI_HALLUCINATION_RULES


def test_auto_followup_keeps_the_same_coder_led_loop(job):
    job.save(
        provider="openrouter",
        model="deepseek/deepseek-v4-flash-0731",
        user_turns=2,
    )

    job._configure_turn_policy(
        "and can you switch it right now to the headphones please",
        strategy="auto",
    )

    assert job._task_strategy.name == "coder_led"
    assert job._task_strategy.use_scout is False
    assert job._task_strategy.use_planner is False


def test_incidental_risk_words_do_not_change_the_auto_harness_shape(job):
    job.save(provider="openrouter", model="deepseek/deepseek-v4-flash-0731")

    job._configure_turn_policy(
        "Make the mic smaller but preserve existing microphone permissions",
        strategy="auto",
    )

    profile, enabled = job._tool_profile("ignored once policy is active")
    assert job._task_strategy.name == "coder_led"
    assert profile == "coder_led"
    assert {"spawn_agent", "consult", "edit_file", "run_shell"} <= enabled


def test_larger_followup_support_roles_receive_a_bounded_continuity_manifest(job):
    job.save(
        user_turns=3,
        edited_files=["apps/hud.lua", "audio-output-toggle.ps1"],
        last_summary="Added the quit hook; runtime quit behavior still needs proof.",
        verification={"state": "unverified", "reason": "Quit behavior was not exercised."},
    )

    manifest = job._continuity_manifest()

    assert "<session_continuity>" in manifest
    assert "apps/hud.lua" in manifest
    assert "audio-output-toggle.ps1" in manifest
    assert "runtime quit behavior still needs proof" in manifest
    assert len(manifest) <= 1800


def test_direct_system_prompt_is_compact_and_route_specific(job, tmp_path):
    job._task_strategy = code_jobs.code_harness_policy.classify_task(
        "Change the colour in web/css/app.css"
    )
    job._turn_policy_active = True

    direct = job._openrouter_system_prompt(tmp_path)
    planned = code_jobs._SHARED_AGENT_PROMPT(tmp_path, "planned")

    assert "This is a DIRECT task" in direct
    assert "call edit_file immediately with that revision" in direct
    assert "MEDIUM" not in direct and "LARGE" not in direct
    assert len(direct) < len(planned) * 0.5
    assert direct.endswith(f"Project: {tmp_path}")


def test_planned_system_prompt_keeps_scope_with_the_primary_coder(job, tmp_path):
    job._task_strategy = code_jobs.code_harness_policy.classify_task(
        "Fix the login state and permissions"
    )
    job._turn_policy_active = True

    system = job._openrouter_system_prompt(tmp_path)

    assert "primary Coder" in system
    assert "operator request reaches you unchanged" in system
    assert "follow the active entrypoint" in system
    assert "imagined old state" in system
    assert "implement the remaining delta" in system
    assert "There is no automatic planner or scout stage" not in system


def test_direct_source_prompt_teaches_symbol_source_and_serial_edit_batch(job, tmp_path):
    direct = code_jobs._DIRECT_AGENT_PROMPT(tmp_path)

    assert "find_symbol once with include_source=true" in direct
    assert "adjacent edit_file calls in one response" in direct
    assert "same observed revision" in direct
    assert "serialized, not atomic" in direct


def test_source_tool_schema_exposes_bounded_symbol_body_and_revision_handoff(job):
    tools = job._ollama_tools("Fix the helper function in app.py")
    functions = {tool["function"]["name"]: tool["function"] for tool in tools}
    symbol_properties = functions["find_symbol"]["parameters"]["properties"]

    assert symbol_properties["include_source"]["type"] == "boolean"
    assert symbol_properties["max_lines"]["default"] == code_jobs.FIND_SYMBOL_SOURCE_DEFAULT_LINES
    assert symbol_properties["max_lines"]["maximum"] == code_jobs.FIND_SYMBOL_SOURCE_MAX_LINES
    assert "bounded definition body and its revision" in functions["find_symbol"]["description"]
    assert "serializes them and forwards fresh revisions" in functions["edit_file"]["description"]
    assert "not atomically" in functions["edit_file"]["description"]


def test_read_file_schema_has_one_line_based_pagination_contract(job, tmp_path):
    schema = next(tool for tool in job._ollama_tools() if tool["function"]["name"] == "read_file")
    properties = schema["function"]["parameters"]["properties"]
    assert "offset" not in properties
    assert properties["start_line"]["description"].startswith("One-based line")

    source = tmp_path / "numbered.txt"
    source.write_text("".join(f"line {index}\n" for index in range(1, 101)), encoding="utf-8")
    result = json.loads(job._ollama_run_tool(tmp_path, "read_file", {
        "relative_path": "numbered.txt",
        "offset": 80,
        "max_lines": 2,
    }))
    assert result["start_line"] == 80
    assert result["content"] == "line 80\nline 81\n"


def test_direct_system_prompt_keeps_dynamic_project_after_stable_policy(tmp_path):
    first = code_jobs._DIRECT_AGENT_PROMPT(tmp_path / "one")
    second = code_jobs._DIRECT_AGENT_PROMPT(tmp_path / "two")

    assert first.rsplit("\nProject: ", 1)[0] == second.rsplit("\nProject: ", 1)[0]


def test_aios_context_requires_mapping_the_exact_user_visible_target():
    context = code_jobs.SELF_LOCATION

    assert "user-visible location and state qualifier" in context
    assert "exact markup, handler, and styles" in context
    assert "similar label, icon, or behavior is not the target" in context
    assert "same visible surface" in context


def test_named_file_metadata_exposes_size_without_claiming_content(job, tmp_path):
    target = tmp_path / "web" / "js" / "sessions.js"
    target.parent.mkdir(parents=True)
    target.write_text("x" * 20_000, encoding="utf-8")
    job.save(cwd=str(tmp_path))

    result = job._with_named_file_metadata(
        "Change the sort.",
        "Change the sort in `web/js/sessions.js` and leave missing.js alone.",
    )

    assert "web/js/sessions.js · 20,000 bytes" in result
    assert "missing.js" not in result
    assert "contents are not inspected" in result


def test_research_turn_gets_research_and_large_task_tools(job):
    tools = job._ollama_tools(
        "Research the latest Hermes agentic harness architecture and refactor ours"
    )
    names = _tool_names(tools)
    selector = next(tool["function"] for tool in tools if tool["function"]["name"] == "select_tools")
    available = set(selector["parameters"]["properties"]["names"]["items"]["enum"])
    assert {"spawn_agent", "update_plan", "select_tools"} <= names
    assert {"repo_map", "web_search", "fetch_url"} <= available


def test_every_code_schema_keeps_web_tools_discoverable(job):
    tools = job._ollama_tools("Change one CSS colour in web/css/app.css")
    names = _tool_names(tools)
    selector = next(tool["function"] for tool in tools if tool["function"]["name"] == "select_tools")
    available = set(selector["parameters"]["properties"]["names"]["items"]["enum"])
    assert {"web_search", "fetch_url"}.isdisjoint(names)
    assert {"web_search", "fetch_url"} <= available


def test_repo_map_ranks_source_content_above_generated_artifacts(job, tmp_path):
    generated = tmp_path / "app" / "build" / "intermediates"
    generated.mkdir(parents=True)
    (generated / "microphone-button.class").write_bytes(b"generated")
    phone = tmp_path / "phone_site"
    phone.mkdir()
    (phone / "director.js").write_text(
        "function toggleMic() { /* microphone recording permissions */ }\n",
        encoding="utf-8",
    )
    (tmp_path / "unrelated.py").write_text("def helper(): pass\n", encoding="utf-8")

    mapped = job._repo_map(
        tmp_path,
        tmp_path,
        "find the homepage microphone recording permissions control",
        10,
        4000,
    )
    paths = [row["path"].replace("\\", "/") for row in mapped["files"]]

    assert paths[0] == "phone_site/director.js"
    assert not any("/build/" in f"/{path}/" for path in paths)


def test_repo_map_keeps_a_web_entrypoint_with_the_assets_it_loads(job, tmp_path):
    phone = tmp_path / "phone_site"
    phone.mkdir()
    (phone / "index.html").write_text(
        '<link rel="stylesheet" href="/director.css">\n'
        '<button id="btn-mic" aria-label="Voice"></button>\n'
        '<script src="/director.js" type="module"></script>\n',
        encoding="utf-8",
    )
    (phone / "director.js").write_text(
        'function toggleMic() { /* microphone recording permissions */ }\n',
        encoding="utf-8",
    )
    (phone / "director.css").write_text(".pill-btn { width: 38px; }\n", encoding="utf-8")
    (phone / "phone.js").write_text(
        'function oldMic() { /* microphone recording permissions */ }\n',
        encoding="utf-8",
    )

    mapped = job._repo_map(
        tmp_path,
        tmp_path,
        "find the homepage microphone recording permissions control",
        6,
        4000,
    )
    paths = [row["path"].replace("\\", "/") for row in mapped["files"]]

    assert paths.index("phone_site/index.html") < paths.index("phone_site/phone.js")
    assert paths.index("phone_site/director.css") < paths.index("phone_site/phone.js")


def test_aios_request_from_another_project_exposes_harness_location(job, tmp_path):
    job._turn_request = "Go edit the aiOS harness and fix its web search tool"

    prompt = job._openrouter_system_prompt(tmp_path / "SimRig")

    assert str(code_jobs.ROOT / "code_jobs.py") in prompt
    assert "absolute paths and parent traversal are available" in prompt
    assert "newest operator message is the active objective" in prompt


def test_aios_request_from_another_project_routes_planner_to_harness(job, tmp_path):
    simrig = tmp_path / "SimRig"

    assert job._planning_project(simrig, "Edit the aiOS harness") == code_jobs.ROOT
    assert job._planning_project(simrig, "Edit the paddle script") == simrig


def test_model_work_has_resumable_default_safety_boundaries():
    assert code_jobs.OLLAMA_MAX_TOOL_ROUNDS == 48
    assert code_jobs.OPENROUTER_MAX_TOOL_ROUNDS == 48
    assert code_jobs.MAX_TOOL_CALLS_PER_TURN == 0
    assert code_jobs.LARGE_MAX_TOOL_ROUNDS == 80
    assert code_jobs.LARGE_MAX_TOOL_CALLS == 0
    assert code_jobs.TURN_MODEL_TOKEN_BUDGET == 600_000
    assert code_jobs.LARGE_TURN_MODEL_TOKEN_BUDGET == 1_200_000
    assert code_jobs.MAX_WEB_SEARCHES_PER_TURN == 0
    assert code_jobs.MAX_SUBAGENTS_PER_TURN == 0
    assert code_jobs.SUBAGENT_MAX_ROUNDS == 6


def test_every_task_profile_keeps_tools_open_but_budgets_model_tokens(job):
    job.reset_turn_discipline("standard")
    assert job._turn_tool_call_limit == 0
    assert job._turn_model_token_budget == 600_000
    job.reset_turn_discipline("distributed")
    assert job._turn_tool_call_limit == 0
    assert job._turn_model_token_budget == 1_200_000


# ------------------------------------------------- checks that cannot succeed


@pytest.mark.parametrize(
    "command",
    [
        "python -m py_compile aios_ui/web/css/code.css",
        "python -m py_compile aios_ui/web/css/code.css 2>&1",
        "node --check aios_ui/web/css/code.css",
    ],
)
def test_a_syntax_check_on_the_wrong_language_is_refused(command):
    assert code_jobs._pointless_check_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "python -m py_compile aios_ui/server.py",
        "node --check aios_ui/web/js/code.js",
        "python -m py_compile app.py && node --check app.js",
        "python -m pytest tests/test_code_review.py -q",
        "git diff --stat",
        "python -m py_compile $target",
    ],
)
def test_real_checks_are_left_alone(command):
    assert code_jobs._pointless_check_command(command) == ""


def test_the_refusal_says_what_to_do_instead():
    message = code_jobs._pointless_check_command("python -m py_compile theme.css")
    assert "theme.css" in message
    assert "Skip this check" in message


# ------------------------------------------------- edits prove themselves


def test_an_edit_reports_where_it_landed(job, tmp_path):
    """Without this the model reads the file back or shells out to git diff --
    a whole round to learn something the harness already knew."""
    target = tmp_path / "code.css"
    target.write_text("a {}\nb {}\n.dot { color: green; }\n", encoding="utf-8")
    result = json.loads(job._ollama_run_tool(
        tmp_path, "edit_file",
        {"relative_path": "code.css", "old_text": "color: green", "new_text": "color: white"},
        "call-1",
    ))
    assert result["ok"] is True
    assert result["applied_at_line"] == 3


def test_the_landing_line_survives_into_model_history(job, tmp_path):
    """The diff is stripped before the result reaches the model; the proof the
    edit applied must not be stripped with it."""
    target = tmp_path / "code.css"
    target.write_text("x {}\n.dot { color: green; }\n", encoding="utf-8")
    raw = job._ollama_run_tool(
        tmp_path, "edit_file",
        {"relative_path": "code.css", "old_text": "green", "new_text": "white"},
        "call-1",
    )
    trimmed = json.loads(code_jobs.CodeJob._tool_result_for_model(raw))
    assert "diff" not in trimmed
    assert trimmed["applied_at_line"] == 2


# ------------------------------------------------- the reviewer earns its turn


def _snapshot(added_lines: int) -> dict:
    body = "\n".join(f"+line {i}" for i in range(added_lines))
    return {"c1": {"files": ["a.css"], "diff": f"--- a.css\n+++ a.css\n@@ -1 +1 @@\n{body}"}}


def _set_review_strategy(job, name: str) -> None:
    job._task_strategy = code_jobs.code_harness_policy.classify_task(f"[{name}] review test")
    job.save(task_strategy={"name": name})


def test_an_enabled_reviewer_checks_even_a_four_line_planned_change(job, tmp_path, monkeypatch):
    monkeypatch.setattr(
        code_jobs, "review_change",
        lambda *a, **k: {"ok": True, "verdict": "pass", "summary": "", "findings": [], "unmet": [], "suggestions": []},
    )
    _set_review_strategy(job, "planned")
    job.save(cwd=str(tmp_path), diff_snapshots=_snapshot(4))
    assert job._review_completed_change().get("verdict") == "pass"


def test_direct_change_skips_the_second_model_and_records_why(job, tmp_path, monkeypatch):
    monkeypatch.setattr(code_jobs, "review_change", lambda *a, **k: pytest.fail("Direct task paid for review"))
    _set_review_strategy(job, "direct")
    job._pipeline_turn_key = "direct-review"
    job.save(cwd=str(tmp_path), diff_snapshots=_snapshot(40))

    assert job._review_completed_change() == {}

    meta = job.load()
    expected_policy = {
        "mode": "adaptive",
        "enabled": True,
        "strategy": "direct",
        "run": False,
        "runtime_reasoning": "medium",
        "reason": "Direct tasks skip the second model to keep small edits fast.",
    }
    assert expected_policy.items() <= meta["review_policy"].items()
    stage = meta["pipeline_stages"]["stage-direct-review-reviewer"]
    assert stage["phase"] == "completed"
    assert "Direct strategy" in stage["detail"]


def test_no_session_edit_never_reviews_the_ambient_dirty_tree(job, tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "unrelated.py").write_text("dirty = True\n", encoding="utf-8")
    monkeypatch.setattr(code_jobs, "review_change", lambda *a, **k: pytest.fail("reviewed unrelated work"))
    _set_review_strategy(job, "planned")
    job.save(cwd=str(tmp_path), edited_files=[], diff_snapshots={})
    assert job._review_completed_change() == {}


def test_a_real_change_is_still_reviewed(job, tmp_path, monkeypatch):
    seen = {}

    def fake_review(brief, change, **kwargs):
        seen["diff"] = change.get("diff")
        return {"ok": True, "verdict": "pass", "summary": "fine", "findings": [], "unmet": [], "suggestions": []}

    monkeypatch.setattr(code_jobs, "review_change", fake_review)
    _set_review_strategy(job, "planned")
    job.save(cwd=str(tmp_path), brief="do the thing", diff_snapshots=_snapshot(40))
    result = job._review_completed_change()
    assert result["verdict"] == "pass"
    assert "line 39" in seen["diff"]


# ------------------------------------------------- the loop is told to end


def _look(job, name="search_text", **payload):
    return json.loads(job._turn_discipline_note(name, json.dumps(payload)))


def test_a_few_empty_searches_are_left_alone(job):
    job.reset_turn_discipline()
    for _ in range(2):
        assert "note" not in _look(job, matches=[])


def test_a_run_of_empty_searches_is_called_out(job):
    job.reset_turn_discipline()
    for _ in range(3):
        result = _look(job, matches=[])
    assert "not in this project" in result["note"]


def test_one_hit_clears_the_empty_run(job):
    job.reset_turn_discipline()
    _look(job, matches=[])
    _look(job, matches=[])
    _look(job, matches=[{"path": "a.py", "line": 1, "text": "x"}])
    assert "note" not in _look(job, matches=[])


def test_looking_around_after_the_edit_is_named(job):
    """The measured failure: the edit landed on call two, fourteen followed."""
    job.reset_turn_discipline()
    job._turn_discipline_note("edit_file", json.dumps({"ok": True, "applied_at_line": 3}))
    for _ in range(4):
        result = _look(job, "read_file", content="...")
    assert "stop and answer now" in result["note"]
    assert job._progress_state == "working"


def test_progress_state_never_removes_inspection_tools(job):
    """Progress telemetry must not turn into a permanent tool deadlock."""
    job.reset_turn_discipline()
    job._progress_state = "blocked"

    inspection = [
        ("run_shell", {"command": "Get-Content app.py | Select-Object -First 20"}),
        ("run_shell", {"command": r"cd C:\repo; $l=Get-Content app.py; $l[0..20]"}),
        ("run_shell", {"command": r'cd C:\repo; findstr /n /i "handler" app.py'}),
        ("read_file", {"relative_path": "unseen.py"}),
        ("search_text", {"query": "handler", "relative_path": "."}),
        ("spawn_agent", {"objective": "read app.py and report the handler"}),
    ]
    for name, args in inspection:
        assert job._guard_before_tool(name, args) == "", (name, args)

    assert job._guard_before_tool("run_shell", {"command": "python -m pytest -q tests/test_app.py"}) == ""
    assert job._guard_before_tool("edit_file", {"relative_path": "app.py"}) == ""
    assert job._guard_before_tool("write_file", {"relative_path": "new_page.html"}) == ""
    assert job._guard_before_tool("ask_user", {"question": "which layout?"}) == ""


def test_thirty_step_review_redirects_without_removing_tools(job):
    job.reset_turn_discipline()
    job._turn_tool_calls = code_jobs.PROGRESS_REVIEW_CALLS
    result = json.loads(job._semantic_progress_result(
        "run_shell", json.dumps({"exit_code": 0, "output": ""}),
    ))
    assert "no objective progress was made" in result["note"]
    assert job._turn_force_finalize is False
    assert job._progress_state == "review"


def test_thirty_step_review_continues_after_real_progress(job):
    job.reset_turn_discipline()
    job._turn_tool_calls = code_jobs.PROGRESS_REVIEW_CALLS
    result = json.loads(job._semantic_progress_result(
        "edit_file", json.dumps({"ok": True, "changed": True}),
        {"relative_path": "app.py", "patch": "..."},
    ))
    assert "progress was made" in result["note"]
    assert job._turn_force_finalize is False
    assert job._progress_state == "working"


def test_thirty_fresh_reads_do_not_masquerade_as_objective_progress(job):
    job.reset_turn_discipline()
    for index in range(code_jobs.PROGRESS_REVIEW_CALLS):
        job._turn_tool_calls = index + 1
        result = json.loads(job._semantic_progress_result(
            "read_file", json.dumps({"content": f"new evidence {index}"}),
            {"relative_path": f"file_{index}.py"},
        ))
    assert "no objective progress was made" in result["note"]
    assert job._productive_calls == code_jobs.PROGRESS_REVIEW_CALLS
    assert job._objective_progress_calls == 0
    assert job._turn_force_finalize is False
    assert job._progress_state == "review"


def test_source_inspection_ignores_unrelated_watcher_mutations_for_progress(job):
    job.reset_turn_discipline()
    job._turn_tool_calls = code_jobs.PROGRESS_REVIEW_CALLS
    result = json.loads(job._semantic_progress_result(
        "run_shell",
        json.dumps({
            "exit_code": 0,
            "output": "app.py:12:needle",
            "mutated_paths": ["aios-watchdog.log"],
            "verification": False,
        }),
        {"command": "rg -n needle ."},
    ))

    assert "no objective progress was made" in result["note"]
    assert job._productive_calls == 1
    assert job._objective_progress_calls == 0


def test_pre_edit_exploration_gets_a_soft_commitment_nudge(job):
    job.reset_turn_discipline()
    for _ in range(code_jobs.COMMITMENT_NUDGE_STANDARD - 1):
        result = _look(job, "read_file", content="...")
        assert "note" not in result
    result = _look(job, "read_file", content="...")
    assert "Progress check" in result["note"]
    assert job._turn_force_finalize is False


def test_external_research_gets_the_same_progress_feedback(job):
    job.reset_turn_discipline()
    result = {}
    for index in range(code_jobs.COMMITMENT_NUDGE_STANDARD):
        name = "web_search" if index % 2 == 0 else "fetch_url"
        payload = {"results": [{"url": f"https://example.com/{index}"}]} if name == "web_search" else {
            "url": f"https://example.com/{index}", "content": "reference"
        }
        result = _look(job, name, **payload)

    assert "Progress check" in result["note"]
    assert job._turn_force_finalize is False


def test_non_mutating_shell_research_gets_progress_feedback(job):
    job.reset_turn_discipline()
    result = {}
    for index in range(code_jobs.COMMITMENT_NUDGE_STANDARD):
        result = _look(job, "run_shell", exit_code=0, output=f"download {index}")

    assert "Progress check" in result["note"]


def test_shell_mutation_is_not_mistaken_for_more_exploration(job):
    job.reset_turn_discipline()
    for index in range(code_jobs.COMMITMENT_NUDGE_STANDARD):
        result = _look(
            job,
            "run_shell",
            exit_code=0,
            output=f"changed {index}",
            mutated_paths=["app.py"],
        )

    assert "note" not in result


def test_empty_web_searches_are_recognized_as_fruitless(job):
    job.reset_turn_discipline()
    for _ in range(3):
        result = _look(job, "web_search", results=[])

    assert "not in this project" in result["note"]


def test_blocked_guardrail_result_is_reported_as_a_failed_tool_activity():
    result = json.dumps({
        "ok": False,
        "blocked": True,
        "guardrail": "tool_not_enabled",
        "message": "web_search is not enabled",
    })

    assert code_jobs.CodeJob._result_failed(result, "web_search") is True


def test_orchestrated_profile_is_not_mistaken_for_a_large_task(job):
    job.reset_turn_discipline("orchestrated")
    for _ in range(code_jobs.COMMITMENT_NUDGE_STANDARD - 1):
        result = _look(job, "read_file", content="...")
        assert "note" not in result
    result = _look(job, "read_file", content="...")
    assert "Progress check" in result["note"]
    assert job._turn_force_finalize is False


def test_a_second_edit_resets_the_drift_count(job):
    job.reset_turn_discipline()
    job._turn_discipline_note("edit_file", json.dumps({"ok": True}))
    for _ in range(3):
        _look(job, "read_file", content="...")
    job._turn_discipline_note("edit_file", json.dumps({"ok": True}))
    for _ in range(3):
        result = _look(job, "read_file", content="...")
    assert "note" not in result


def test_a_failed_edit_does_not_count_as_progress(job):
    job.reset_turn_discipline()
    job._turn_discipline_note("edit_file", json.dumps({"error": "old_text was not found"}))
    for _ in range(6):
        result = _look(job, "read_file", content="...")
    assert "note" not in result


def test_the_legacy_profile_gets_no_notes(job, monkeypatch):
    monkeypatch.setenv("AIOS_CODE_PROMPT_PROFILE", "legacy")
    job.reset_turn_discipline()
    job._turn_discipline_note("edit_file", json.dumps({"ok": True}))
    for _ in range(8):
        result = _look(job, "read_file", content="...")
    assert "note" not in result


def test_a_search_reports_how_big_the_files_are(job, tmp_path):
    """So "can I just open this?" costs no round trip."""
    (tmp_path / "small.css").write_text("a {}\nb {}\n", encoding="utf-8")
    result = json.loads(job._ollama_run_tool(tmp_path, "search_text", {"query": "a {"}, "c1"))
    assert result["file_lines"]["small.css"] == 2


def test_a_search_with_no_matches_is_not_a_tool_failure(job):
    result = json.dumps({"matches": [], "file_lines": {}, "exit_code": 1})

    assert job._result_failed(result, "search_text") is False


def test_repeated_empty_searches_do_not_force_finalize(job):
    job.reset_turn_discipline()
    for _ in range(3):
        result = json.dumps({"matches": [], "file_lines": {}, "exit_code": 1})
        assert job._guard_after_tool("search_text", {"query": "missing"}, result) == result

    assert job._turn_force_finalize is False


# ---------------------------------------------- runtime circuit breakers


def _raw_call(name, **arguments):
    return {"id": f"call-{name}", "function": {"name": name, "arguments": arguments}}


def test_duplicate_reads_execute_only_once_even_in_one_batch(job, tmp_path, monkeypatch):
    calls = []

    def fake_run(project, name, arguments, tool_id):
        calls.append((name, arguments))
        return json.dumps({"matches": [{"path": "a.py", "line": 1}]})

    monkeypatch.setattr(job, "_ollama_run_tool", fake_run)
    job.reset_turn_discipline()
    results = job._execute_tool_calls(
        tmp_path,
        [_raw_call("search_text", query="needle"), _raw_call("search_text", query="needle")],
        "test",
    )
    assert len(calls) == 1
    reused = [json.loads(item["result"]) for item in results if json.loads(item["result"]).get("reused")]
    assert reused and reused[0]["guardrail"] == "evidence_reuse"


def test_a_fully_covered_line_read_reuses_turn_evidence(job, tmp_path, monkeypatch):
    target = tmp_path / "shell.css"
    target.write_text("".join(f"line {index}\n" for index in range(1, 301)), encoding="utf-8")
    real_run = job._ollama_run_tool
    calls = []

    def counted(project, name, arguments, tool_id):
        calls.append((name, dict(arguments)))
        return real_run(project, name, arguments, tool_id)

    monkeypatch.setattr(job, "_ollama_run_tool", counted)
    job.reset_turn_discipline()
    first = job._execute_tool_calls(
        tmp_path, [_raw_call("read_file", relative_path="shell.css", start_line=120, max_lines=80)], "test"
    )
    second = job._execute_tool_calls(
        tmp_path, [_raw_call("read_file", relative_path="shell.css", start_line=160, max_lines=40)], "test"
    )

    assert json.loads(first[0]["result"])["start_line"] == 120
    reused = json.loads(second[0]["result"])
    assert reused["reused"] is True
    assert reused["coverage"]["start"] == 120
    assert [name for name, _args in calls] == ["read_file"]


def test_a_partial_read_overlap_runs_but_names_the_reuse(job, tmp_path):
    target = tmp_path / "shell.css"
    target.write_text("".join(f"line {index}\n" for index in range(1, 301)), encoding="utf-8")
    job.reset_turn_discipline()
    job._execute_tool_calls(
        tmp_path, [_raw_call("read_file", relative_path="shell.css", start_line=120, max_lines=80)], "test"
    )
    result = job._execute_tool_calls(
        tmp_path, [_raw_call("read_file", relative_path="shell.css", start_line=180, max_lines=40)], "test"
    )
    payload = json.loads(result[0]["result"])
    assert payload.get("reused") is not True
    assert "Evidence overlap" in payload["note"]


def test_a_mutation_invalidates_overlapping_read_evidence(job, tmp_path):
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    job.reset_turn_discipline()
    read = _raw_call("read_file", relative_path="app.py", start_line=1, max_lines=1)
    assert not json.loads(job._execute_tool_calls(tmp_path, [read], "test")[0]["result"]).get("reused")
    job._execute_tool_calls(
        tmp_path,
        [_raw_call("edit_file", relative_path="app.py", old_text="value = 1", new_text="value = 2")],
        "test",
    )
    assert not json.loads(job._execute_tool_calls(tmp_path, [read], "test")[0]["result"]).get("reused")


def test_a_rephrased_search_gets_guidance_without_being_blocked(job, tmp_path, monkeypatch):
    calls = []

    def fake_run(project, name, arguments, tool_id):
        calls.append(arguments["query"])
        return json.dumps({"matches": [{"path": "app.js", "line": 10, "text": "collapse"}]})

    monkeypatch.setattr(job, "_ollama_run_tool", fake_run)
    job.reset_turn_discipline()
    job._execute_tool_calls(
        tmp_path,
        [_raw_call("search_text", query="chat-collapse|nav-collapse|collapsed", relative_path="aios_ui/web/js/app.js")],
        "test",
    )
    result = job._execute_tool_calls(
        tmp_path,
        [_raw_call("search_text", query="chat-collapse|nav-collapse", relative_path="aios_ui/web/js")],
        "test",
    )
    payload = json.loads(result[0]["result"])
    assert len(calls) == 2
    assert payload.get("blocked") is not True
    assert "substantially overlaps" in payload["note"]


def test_forensic_sidebar_sequence_is_steered_before_the_six_minute_loop(job, tmp_path, monkeypatch):
    """Replay the first eight retained stage-4 inspections from f1dfc34ec2fc."""
    def fake_run(project, name, arguments, tool_id):
        if name == "search_text":
            return json.dumps({"matches": [{"path": "aios_ui/web/js/app.js", "line": 183, "text": "collapse"}]})
        start = int(arguments.get("start_line") or 1)
        maximum = int(arguments.get("max_lines") or 300)
        return json.dumps({
            "path": arguments["relative_path"], "content": "x\n" * maximum,
            "start_line": start, "next_line": start + maximum, "total_lines": 1000, "truncated": True,
        })

    monkeypatch.setattr(job, "_ollama_run_tool", fake_run)
    job.reset_turn_discipline()
    calls = [
        _raw_call("read_file", relative_path="aios_ui/web/index.html", start_line=40, max_lines=30),
        _raw_call("read_file", relative_path="aios_ui/web/js/app.js", start_line=150, max_lines=60),
        _raw_call("search_text", query="chat-collapse|nav-collapse|collapsed", relative_path="aios_ui/web/js/app.js"),
        _raw_call("search_text", query="chat-collapse|nav-collapse", relative_path="aios_ui/web/js"),
        _raw_call("search_text", query="collapsed|chat-panel|classList", relative_path="aios_ui/web"),
        _raw_call("search_text", query="collapse", relative_path="aios_ui/web/js"),
        _raw_call("read_file", relative_path="aios_ui/web/js/app.js", start_line=183, max_lines=30),
        _raw_call("search_text", query="chat-panel", relative_path="aios_ui/web/css/shell.css"),
    ]
    results = []
    for call in calls:
        results.extend(job._execute_tool_calls(tmp_path, [call], "forensic"))

    payloads = [json.loads(item["result"]) for item in results]
    assert any(
        payload.get("reused") or "overlap" in str(payload.get("note") or "").casefold()
        for payload in payloads
    )
    assert any("Progress check" in str(payload.get("note") or "") for payload in payloads)
    assert job._turn_force_finalize is False


def test_a_successful_edit_makes_the_same_read_fresh_again(job, tmp_path, monkeypatch):
    calls = []

    def fake_run(project, name, arguments, tool_id):
        calls.append(name)
        if name == "edit_file":
            return json.dumps({"ok": True, "path": "a.py", "applied_at_line": 1})
        return json.dumps({"path": "a.py", "content": "value"})

    monkeypatch.setattr(job, "_ollama_run_tool", fake_run)
    job.reset_turn_discipline()
    for call in (
        _raw_call("read_file", relative_path="a.py"),
        _raw_call("edit_file", relative_path="a.py", old_text="x", new_text="y"),
        _raw_call("read_file", relative_path="a.py"),
    ):
        result = job._execute_tool_calls(tmp_path, [call], "test")
        assert not json.loads(result[0]["result"]).get("blocked")
    assert calls == ["read_file", "edit_file", "read_file"]


def test_identical_failures_are_blocked_before_a_third_execution(job, tmp_path, monkeypatch):
    calls = []

    def failing(project, name, arguments, tool_id):
        calls.append(name)
        return json.dumps({"error": "missing"})

    monkeypatch.setattr(job, "_ollama_run_tool", failing)
    job.reset_turn_discipline()
    results = []
    for _ in range(3):
        results = job._execute_tool_calls(tmp_path, [_raw_call("read_file", relative_path="x")], "test")
    assert len(calls) == 2
    assert json.loads(results[0]["result"])["guardrail"] == "repeated_exact_failure"


def test_web_search_cap_ends_the_tool_loop_cleanly(job, tmp_path, monkeypatch):
    monkeypatch.setattr(code_jobs, "MAX_WEB_SEARCHES_PER_TURN", 2)
    monkeypatch.setattr(
        job,
        "_ollama_run_tool",
        lambda *args, **kwargs: json.dumps({"results": [{"url": "https://example.com"}]}),
    )
    job.reset_turn_discipline()
    for query in ("one", "two", "three"):
        result = job._execute_tool_calls(tmp_path, [_raw_call("web_search", query=query)], "test")
    assert json.loads(result[0]["result"])["guardrail"] == "web_search_cap"
    assert job._turn_force_finalize is True


def test_round_cap_uses_one_tool_free_closing_round(job, tmp_path, monkeypatch):
    import openrouter_client

    (tmp_path / "hello.txt").write_text("hello\n", encoding="utf-8")
    job.events_path.touch()
    job.save(provider="openrouter", cwd=str(tmp_path), model="test/model", provider_sessions=[])
    job._save_openrouter_history([
        {"role": "system", "content": "stale system"},
        {"role": "user", "content": "old context " + ("x" * 30_000)},
        {"role": "assistant", "content": "old result " + ("y" * 30_000)},
    ])
    monkeypatch.setattr(code_jobs, "OPENROUTER_MAX_TOOL_ROUNDS", 1)
    monkeypatch.setattr(code_jobs, "LARGE_MAX_TOOL_ROUNDS", 1)
    monkeypatch.setattr(openrouter_client, "provider_status", lambda **kwargs: (True, "ready"))
    requests = []

    def fake_stream(messages, model, **kwargs):
        requests.append((json.loads(json.dumps(messages)), dict(kwargs)))
        if len(requests) == 1:
            yield {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [_raw_call("read_file", relative_path="hello.txt")],
                },
                "usage": {},
                "finish_reason": "tool_calls",
                "stream_complete": True,
                "done": True,
            }
        else:
            yield {
                "message": {"role": "assistant", "content": "Best verified result."},
                "usage": {},
                "finish_reason": "stop",
                "stream_complete": True,
                "done": True,
            }

    monkeypatch.setattr(openrouter_client, "stream_chat", fake_stream)
    outcome, summary = job._run_openrouter("Read the file", [])
    assert outcome == "incomplete"
    assert summary == "Best verified result."
    assert len(requests[0][1].get("tools") or []) > 0
    closing_messages, closing_options = requests[1]
    assert closing_options["tools"] == []
    assert closing_options["reasoning"] == "off"
    assert closing_options["max_completion_tokens"] == code_jobs.FORCED_HANDOFF_MAX_TOKENS
    assert len(closing_messages) == 2
    assert closing_messages[0]["role"] == "system"
    assert "Stop reason: The turn reached its 1-round provider-step limit." in closing_messages[0]["content"]
    assert "The task is incomplete" in closing_messages[0]["content"]
    assert "verification occurred" in closing_messages[0]["content"]
    assert len(json.dumps(closing_messages)) < code_jobs.FORCED_HANDOFF_CONTEXT_CHARS + 3_000
    assert "x" * 5_000 not in json.dumps(closing_messages)


def test_token_cap_suppresses_reasoning_only_eof_retry(job, tmp_path, monkeypatch):
    import openrouter_client

    job.events_path.touch()
    job.save(provider="openrouter", cwd=str(tmp_path), model="test/model", provider_sessions=[])
    monkeypatch.setattr(code_jobs, "TURN_MODEL_TOKEN_BUDGET", 10)
    monkeypatch.setattr(code_jobs, "LARGE_TURN_MODEL_TOKEN_BUDGET", 20)
    monkeypatch.setattr(code_jobs, "PROVIDER_INCOMPLETE_STREAM_RETRIES", 2)
    monkeypatch.setattr(openrouter_client, "provider_status", lambda **kwargs: (True, "ready"))
    job.reset_turn_discipline("standard")
    requests = []

    def fake_stream(messages, model, **kwargs):
        requests.append((messages, kwargs))
        yield {
            "done": True,
            "message": {"role": "assistant", "content": "", "reasoning": "unfinished reasoning"},
            "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10},
            "finish_reason": "",
            "stream_complete": False,
        }

    monkeypatch.setattr(openrouter_client, "stream_chat", fake_stream)
    outcome, summary = job._run_openrouter("Inspect safely", [])

    assert outcome == "incomplete"
    assert "10-token model budget" in summary
    assert "no further verification was performed" in summary
    assert len(requests) == 1
    assert job._turn_force_finalize is True
    assert [(row["attempt"], row["status"]) for row in job.load()["model_request_rounds"]] == [
        (1, "incomplete"),
    ]


def test_exhausted_token_budget_uses_local_handoff_without_a_closing_request(
    job, tmp_path, monkeypatch,
):
    import openrouter_client

    (tmp_path / "hello.txt").write_text("hello\n", encoding="utf-8")
    job.events_path.touch()
    job.save(provider="openrouter", cwd=str(tmp_path), model="test/model", provider_sessions=[])
    monkeypatch.setattr(code_jobs, "TURN_MODEL_TOKEN_BUDGET", 10)
    monkeypatch.setattr(code_jobs, "LARGE_TURN_MODEL_TOKEN_BUDGET", 20)
    monkeypatch.setattr(openrouter_client, "provider_status", lambda **kwargs: (True, "ready"))
    job.reset_turn_discipline("standard")
    requests = 0

    def fake_stream(messages, model, **kwargs):
        nonlocal requests
        requests += 1
        if requests > 1:
            pytest.fail("an exhausted token budget must not fund a closing request")
        yield {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [_raw_call("read_file", relative_path="hello.txt")],
            },
            "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10},
            "finish_reason": "tool_calls",
            "stream_complete": True,
            "done": True,
        }

    monkeypatch.setattr(openrouter_client, "stream_chat", fake_stream)
    outcome, summary = job._run_openrouter("Read the file", [])

    assert outcome == "incomplete"
    assert "10-token model budget" in summary
    assert requests == 1
    assert any(
        "returning a local truthful incomplete handoff" in event.get("text", "")
        for event in (
            json.loads(line)
            for line in job.events_path.read_text(encoding="utf-8").splitlines()
        )
    )


def test_bounded_closing_eof_is_not_retried(job, tmp_path, monkeypatch):
    import openrouter_client

    (tmp_path / "hello.txt").write_text("hello\n", encoding="utf-8")
    job.events_path.touch()
    job.save(provider="openrouter", cwd=str(tmp_path), model="test/model", provider_sessions=[])
    monkeypatch.setattr(code_jobs, "OPENROUTER_MAX_TOOL_ROUNDS", 1)
    monkeypatch.setattr(code_jobs, "LARGE_MAX_TOOL_ROUNDS", 1)
    monkeypatch.setattr(code_jobs, "PROVIDER_INCOMPLETE_STREAM_RETRIES", 2)
    monkeypatch.setattr(openrouter_client, "provider_status", lambda **kwargs: (True, "ready"))
    requests = []

    def fake_stream(messages, model, **kwargs):
        requests.append((messages, kwargs))
        if len(requests) == 1:
            yield {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [_raw_call("read_file", relative_path="hello.txt")],
                },
                "usage": {},
                "finish_reason": "tool_calls",
                "stream_complete": True,
                "done": True,
            }
        else:
            yield {
                "message": {"role": "assistant", "content": "", "reasoning": "partial close"},
                "usage": {},
                "finish_reason": "",
                "stream_complete": False,
                "done": True,
            }

    monkeypatch.setattr(openrouter_client, "stream_chat", fake_stream)
    outcome, summary = job._run_openrouter("Read the file", [])

    assert outcome == "incomplete"
    assert "1-round provider-step limit" in summary
    assert len(requests) == 2
    assert requests[1][1]["max_completion_tokens"] == code_jobs.FORCED_HANDOFF_MAX_TOKENS


def test_ollama_round_cap_uses_a_tool_free_handoff_without_an_output_cap(job, tmp_path, monkeypatch):
    import ollama_client

    (tmp_path / "hello.txt").write_text("hello\n", encoding="utf-8")
    job.events_path.touch()
    job.save(provider="ollama", cwd=str(tmp_path), model="test/local-model", provider_sessions=[])
    monkeypatch.setattr(code_jobs, "OLLAMA_MAX_TOOL_ROUNDS", 1)
    monkeypatch.setattr(code_jobs, "LARGE_MAX_TOOL_ROUNDS", 1)
    monkeypatch.setattr(ollama_client, "provider_status", lambda **kwargs: (True, "ready"))
    requests = []

    def fake_stream(messages, model, **kwargs):
        requests.append((json.loads(json.dumps(messages)), dict(kwargs)))
        if len(requests) == 1:
            yield {
                "message": {
                    "content": "",
                    "tool_calls": [_raw_call("read_file", relative_path="hello.txt")],
                },
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 8,
                "eval_count": 2,
            }
        else:
            yield {
                "message": {"content": "Truthful local incomplete handoff."},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 8,
                "eval_count": 2,
            }

    monkeypatch.setattr(ollama_client, "stream_chat", fake_stream)
    outcome, summary = job._run_ollama("Read the file", [])

    assert outcome == "incomplete"
    assert summary == "Truthful local incomplete handoff."
    assert len(requests) == 2
    closing_messages, closing_options = requests[1]
    assert closing_options["tools"] == []
    assert closing_options["reasoning"] == "off"
    assert "num_predict" not in closing_options["options"]
    assert len(closing_messages) == 2
    assert "1-round provider-step limit" in closing_messages[0]["content"]
    assert "The task is incomplete" in closing_messages[0]["content"]
    raw_events = [row for row in code_jobs.read_events(job.id, 0)["events"] if row["kind"].startswith("raw_model_")]
    assert [row["kind"] for row in raw_events] == ["raw_model_tool", "raw_model_delta"]
    assert raw_events[-1]["text"] == "Truthful local incomplete handoff."


def test_incomplete_turn_is_terminal_but_never_reviewed(job, tmp_path, monkeypatch):
    job.events_path.touch()
    job.save(provider="openrouter", cwd=str(tmp_path), model="test/model", status="queued")
    monkeypatch.setattr(job, "_run_openrouter", lambda payload, attachments: ("incomplete", "More work remains."))
    monkeypatch.setattr(job, "_review_completed_change", lambda: pytest.fail("reviewed incomplete work"))

    job._run_locked("Implement a large benchmark feature", planned=False)

    meta = job.load()
    assert meta["status"] == "incomplete"
    assert meta["last_summary"] == "More work remains."
    events = [json.loads(line) for line in job.events_path.read_text(encoding="utf-8").splitlines()]
    assert any(event.get("kind") == "result" and event.get("state") == "incomplete" for event in events)
    coder = [event for event in events if event.get("activity_type") == "stage" and event.get("stage") == "coder"]
    assert [event["phase"] for event in coder] == ["started", "incomplete"]


def test_unverified_telemetry_does_not_downgrade_a_completed_turn(job, tmp_path, monkeypatch):
    job.events_path.touch()
    job.save(provider="openrouter", cwd=str(tmp_path), model="test/model", status="queued")

    def complete_with_unverified_change(payload, attachments):
        job._verification_ledger.mark_mutation("app.py", "changed")
        return "completed", "The useful final answer."

    monkeypatch.setattr(job, "_run_openrouter", complete_with_unverified_change)
    monkeypatch.setattr(job, "_review_completed_change", lambda: None)

    job._run_locked("Explain the design", planned=False)

    meta = job.load()
    assert meta["status"] == "completed"
    assert meta["last_summary"] == "The useful final answer."
    assert meta["verification"]["state"] == "unverified"
    events = [json.loads(line) for line in job.events_path.read_text(encoding="utf-8").splitlines()]
    assert not any("Verification gate" in str(event.get("text") or "") for event in events)
    assert any(
        event.get("kind") == "result"
        and event.get("state") == "completed"
        and event.get("text") == "The useful final answer."
        for event in events
    )


@pytest.mark.parametrize(
    "name",
    [
        ".aios-helper-heartbeat",
        ".aios-health.json",
        ".aios-health.json.tmp",
        ".aios-watchdog.log",
        ".aios-watchdog.log.old",
        ".git/index",
        ".hg/dirstate",
        ".svn/wc.db",
    ],
)
def test_aios_runtime_state_is_not_treated_as_source_mutation(name):
    assert code_jobs.CodeJob._untracked_runtime_artifact(name) is True


def test_generic_scout_provider_error_retries_then_uses_catalog_fallback(job, monkeypatch):
    import openrouter_client

    monkeypatch.setattr(openrouter_client, "SCOUT_MODELS", ("primary/scout", "fallback/scout"))
    monkeypatch.setattr(code_jobs.time, "sleep", lambda _seconds: None)
    models = []

    def fake_round(provider, history, model, tools):
        models.append(model)
        if model == "primary/scout":
            raise RuntimeError("Provider returned error")
        return {"role": "assistant", "content": "mapped"}

    monkeypatch.setattr(job, "_subagent_round_once", fake_round)
    result = job._subagent_round("openrouter", [], "primary/scout", [])
    assert result["content"] == "mapped"
    assert models == ["primary/scout", "primary/scout", "fallback/scout"]


def test_scout_finishes_before_planner_stage_starts(job, tmp_path, monkeypatch):
    import openrouter_client

    job.events_path.touch()
    job.save(cwd=str(tmp_path))
    job._pipeline_turn_key = "ordered"
    real_role = code_jobs.code_roles.role
    monkeypatch.setattr(code_jobs.code_roles, "role", lambda name: {"enabled": True} if name == "scout" else real_role(name))
    monkeypatch.setattr(openrouter_client, "provider_status", lambda **kwargs: (True, "ready"))
    monkeypatch.setattr(job, "_plan_survey", lambda project, request: "app.py")
    # This test proves role ordering when Scout is actually required. Small
    # local maps now skip the paid Scout by design, which is covered separately.
    monkeypatch.setattr(job, "_survey_is_small_and_concrete", lambda survey: False)
    monkeypatch.setattr(job, "_plan_scout", lambda project, request: "app.py:1 - entry point")

    expected_plan = """PATHS
- app.py :: entry point :: clarify UI behavior
CONTRACT TRAPS
- Preserve adjacent behavior.
STEPS
- Inspect the mapped entry point and make the bounded change.
VERIFY
- Run the focused UI check."""

    def fake_stream(*args, **kwargs):
        yield {"done": True, "message": {"content": expected_plan}, "usage": {}}

    monkeypatch.setattr(openrouter_client, "stream_chat", fake_stream)
    plan = job._run_plan_stage("Make the UI clearer", {"model": "planner/model", "reasoning": "high"})
    assert plan == expected_plan
    events = [json.loads(line) for line in job.events_path.read_text(encoding="utf-8").splitlines()]
    stages = [
        (event.get("stage"), event.get("phase"))
        for event in events if event.get("activity_type") == "stage"
    ]
    assert stages == [
        ("scout", "started"), ("scout", "completed"),
        ("planner", "started"), ("planner", "completed"),
    ]
    planner_done = next(
        event for event in events
        if event.get("activity_type") == "planner" and event.get("phase") == "completed"
    )
    assert planner_done["title"] == "Plan sent to Coder"
    assert planner_done["output"] == plan
    assert planner_done["summary"] == plan


def test_planner_can_skip_scout_when_operator_named_the_files(job, tmp_path, monkeypatch):
    import openrouter_client

    job.events_path.touch()
    job.save(cwd=str(tmp_path))
    job._pipeline_turn_key = "scoped"
    monkeypatch.setattr(openrouter_client, "provider_status", lambda **kwargs: (True, "ready"))
    monkeypatch.setattr(job, "_plan_survey", lambda project, request: "src/auth.py")
    monkeypatch.setattr(
        job,
        "_run_scout_stage",
        lambda request: pytest.fail("scout ran despite exact file scope"),
    )

    expected_plan = """PATHS
- src/auth.py :: coder must locate symbol :: refactor bounded behavior
CONTRACT TRAPS
- Preserve the operator's exact contract.
STEPS
- Inspect the named file and make the compatible change.
VERIFY
- Run the smallest relevant auth check."""

    def fake_stream(*args, **kwargs):
        yield {"done": True, "message": {"content": expected_plan}, "usage": {}}

    monkeypatch.setattr(openrouter_client, "stream_chat", fake_stream)
    plan = job._run_plan_stage(
        "Refactor `src/auth.py`",
        {"model": "planner/model", "reasoning": "high", "_use_scout": False},
    )

    assert plan == expected_plan
    events = [json.loads(line) for line in job.events_path.read_text(encoding="utf-8").splitlines()]
    stages = [
        (event.get("stage"), event.get("phase"))
        for event in events if event.get("activity_type") == "stage"
    ]
    assert stages == [("planner", "started"), ("planner", "completed")]


def test_each_pipeline_role_persists_its_exact_usage(job):
    job.events_path.touch()
    job.save(
        provider="openrouter", model="coder/model", usage={}, provider_sessions=[],
        pipeline_stages={}, role_usage={},
    )
    job._pipeline_turn_key = "metered"

    job.pipeline_stage("scout", "started", "Mapping")
    job.record_usage({
        "input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 20,
        "reasoning_tokens": 5, "total_tokens": 120, "cost_usd": 0.001,
    })
    job.pipeline_stage("scout", "completed", "Mapped")

    job.pipeline_stage("planner", "started", "Planning")
    job.record_usage({
        "input_tokens": 300, "cached_input_tokens": 200, "output_tokens": 50,
        "reasoning_tokens": 10, "total_tokens": 350, "cost_usd": 0.004,
    })
    job.pipeline_stage("planner", "completed", "Planned")

    roles = job.load()["role_usage"]
    assert roles["scout"]["usage"] == {
        "input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 20,
        "reasoning_tokens": 5, "total_tokens": 120, "cost_usd": 0.001,
    }
    assert roles["planner"]["usage"]["total_tokens"] == 350
    assert roles["planner"]["usage"]["cost_usd"] == 0.004
    assert roles["scout"]["phase"] == roles["planner"]["phase"] == "completed"


def test_job_is_not_terminal_until_reviewer_finishes(job, tmp_path, monkeypatch):
    job.events_path.touch()
    job.save(
        provider="openrouter", cwd=str(tmp_path), model="coder/model", status="queued",
        usage={}, provider_sessions=[], pipeline_stages={}, role_usage={},
    )
    monkeypatch.setattr(job, "_run_openrouter", lambda payload, attachments: ("completed", "Done."))

    def review_after_coder():
        assert job.load()["status"] == "running"
        job.pipeline_stage("reviewer", "started", "Reviewing")
        job.record_usage({"input_tokens": 80, "output_tokens": 10, "total_tokens": 90, "cost_usd": 0.002})
        job.pipeline_stage("reviewer", "completed", "Review passed")
        return {"verdict": "pass"}

    monkeypatch.setattr(job, "_review_completed_change", review_after_coder)
    job._run_locked("Build it", planned=False)

    assert job.load()["status"] == "completed"
    events = [json.loads(line) for line in job.events_path.read_text(encoding="utf-8").splitlines()]
    reviewer_done = next(i for i, event in enumerate(events)
                         if event.get("stage") == "reviewer" and event.get("phase") == "completed")
    result = next(i for i, event in enumerate(events) if event.get("kind") == "result")
    assert reviewer_done < result
    assert job.load()["role_usage"]["reviewer"]["usage"]["total_tokens"] == 90


def test_textual_dsml_is_recovered_as_a_real_tool_call(job):
    markup = (
        '<｜DSML｜tool_calls><｜DSML｜invoke name="read_file">'
        '<｜DSML｜parameter name="max_lines" string="false">30</｜DSML｜parameter>'
        '<｜DSML｜parameter name="relative_path" string="true">app.py</｜DSML｜parameter>'
        '</｜DSML｜invoke></｜DSML｜tool_calls>'
    )
    calls, cleaned = job._textual_tool_calls(markup)
    assert cleaned == ""
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "read_file"
    assert json.loads(calls[0]["function"]["arguments"]) == {
        "max_lines": 30,
        "relative_path": "app.py",
    }


def test_openrouter_does_not_complete_on_textual_dsml(job, tmp_path, monkeypatch):
    import openrouter_client

    (tmp_path / "hello.txt").write_text("hello\n", encoding="utf-8")
    job.events_path.touch()
    job.save(provider="openrouter", cwd=str(tmp_path), model="test/model", provider_sessions=[])
    monkeypatch.setattr(openrouter_client, "provider_status", lambda **kwargs: (True, "ready"))
    rounds = []
    markup = (
        '<｜DSML｜tool_calls><｜DSML｜invoke name="read_file">'
        '<｜DSML｜parameter name="relative_path" string="true">hello.txt</｜DSML｜parameter>'
        '</｜DSML｜invoke></｜DSML｜tool_calls>'
    )

    def fake_stream(messages, model, **kwargs):
        rounds.append(messages)
        content = markup if len(rounds) == 1 else "Finished after reading the file."
        yield {
            "done": True,
            "message": {"role": "assistant", "content": content},
            "usage": {},
            "finish_reason": "stop",
            "stream_complete": True,
        }

    monkeypatch.setattr(openrouter_client, "stream_chat", fake_stream)
    outcome, summary = job._run_openrouter("Read hello.txt", [])
    assert outcome == "completed"
    assert summary == "Finished after reading the file."
    assert len(rounds) == 2
    saved = json.loads(job._openrouter_history_path().read_text(encoding="utf-8"))
    assert saved[2]["tool_calls"][0]["function"]["name"] == "read_file"
    assert any(message.get("role") == "tool" for message in saved)


def test_tool_call_narration_is_preserved_in_append_only_openrouter_context(job, tmp_path, monkeypatch):
    import openrouter_client

    (tmp_path / "hello.txt").write_text("hello\n", encoding="utf-8")
    job.events_path.touch()
    job.save(provider="openrouter", cwd=str(tmp_path), model="test/model", provider_sessions=[])
    monkeypatch.setattr(openrouter_client, "provider_status", lambda **kwargs: (True, "ready"))
    rounds = []

    def fake_stream(messages, model, **kwargs):
        rounds.append(json.loads(json.dumps(messages)))
        if len(rounds) == 1:
            yield {
                "done": False,
                "delta": {"content": "Let me search for that exact signature.", "reasoning": ""},
            }
            yield {
                "done": True,
                "message": {
                    "role": "assistant",
                    "content": "Let me search for that exact signature.",
                    "tool_calls": [_raw_call("read_file", relative_path="hello.txt")],
                },
                "usage": {},
                "finish_reason": "tool_calls",
                "stream_complete": True,
            }
        else:
            yield {
                "done": True,
                "message": {"role": "assistant", "content": "Done."},
                "usage": {},
                "finish_reason": "stop",
                "stream_complete": True,
            }

    monkeypatch.setattr(openrouter_client, "stream_chat", fake_stream)
    outcome, summary = job._run_openrouter("Read hello.txt", [])

    assert outcome == "completed" and summary == "Done."
    prior_call = next(message for message in rounds[1] if message.get("tool_calls"))
    assert prior_call["content"] == "Let me search for that exact signature."
    assert prior_call["tool_calls"][0]["function"]["name"] == "read_file"


def test_compaction_preserves_recent_tool_narration_when_under_budget():
    history = [
        {"role": "system", "content": "rules"},
        {"role": "assistant", "content": "Let me search again.", "tool_calls": [_raw_call("read_file")]},
        {"role": "tool", "tool_call_id": "call-read_file", "content": "evidence"},
    ]

    cleaned = code_jobs.CodeJob._compact_local_history(history, 100_000)

    assert cleaned[1]["content"] == "Let me search again."
    assert cleaned[1]["tool_calls"] == history[1]["tool_calls"]
    assert cleaned[2] == history[2]


def test_reasoning_off_is_requested_but_provider_reasoning_is_preserved_for_tool_continuation(job, tmp_path, monkeypatch):
    import openrouter_client

    (tmp_path / "hello.txt").write_text("hello\n", encoding="utf-8")
    job.events_path.touch()
    job.save(
        provider="openrouter", cwd=str(tmp_path), model="test/model", reasoning="off",
        provider_sessions=[],
    )
    job._save_openrouter_history([
        {"role": "system", "content": "old system"},
        {
            "role": "assistant",
            "content": "prior turn",
            "reasoning_details": [{
                "type": "reasoning.encrypted",
                "data": "old-opaque-payload",
                "id": "old-reasoning-1",
                "format": "anthropic-claude-v1",
                "index": 0,
            }],
        },
    ])
    monkeypatch.setattr(openrouter_client, "provider_status", lambda **kwargs: (True, "ready"))
    rounds = []

    def fake_stream(messages, model, **kwargs):
        rounds.append(json.loads(json.dumps(messages)))
        if len(rounds) == 1:
            yield {
                "done": True,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning": "I keep checking the same thing.",
                    "reasoning_details": [{
                        "type": "reasoning.encrypted",
                        "data": "opaque-provider-payload",
                        "id": "reasoning-off-1",
                        "format": "anthropic-claude-v1",
                        "index": 0,
                    }],
                    "tool_calls": [_raw_call("read_file", relative_path="hello.txt")],
                },
                "usage": {"reasoning_tokens": 7},
                "finish_reason": "tool_calls",
                "stream_complete": True,
            }
        else:
            yield {
                "done": True,
                "message": {"role": "assistant", "content": "Done."},
                "usage": {},
                "finish_reason": "stop",
                "stream_complete": True,
            }

    monkeypatch.setattr(openrouter_client, "stream_chat", fake_stream)
    outcome, summary = job._run_openrouter("Read hello.txt", [])

    assert outcome == "completed" and summary == "Done."
    assert len(rounds) == 2
    old_turn = next(message for message in rounds[0] if message.get("reasoning_details"))
    assert old_turn["reasoning_details"][0]["data"] == "old-opaque-payload"
    current_turn = next(message for message in rounds[1] if message.get("tool_calls"))
    assert current_turn["reasoning_details"][0]["data"] == "opaque-provider-payload"
    assert "reasoning" not in current_turn
    saved = json.loads(job._openrouter_history_path().read_text(encoding="utf-8"))
    assert sum(bool(message.get("reasoning_details")) for message in saved) == 2
    events = [json.loads(line) for line in job.events_path.read_text(encoding="utf-8").splitlines()]
    assert any("despite Off" in event.get("text", "") and "preserved" in event.get("text", "") for event in events)


def test_structured_reasoning_is_replayed_wire_exact_between_tool_rounds(job, tmp_path, monkeypatch):
    import openrouter_client

    (tmp_path / "hello.txt").write_text("hello\n", encoding="utf-8")
    job.events_path.touch()
    job.save(
        provider="openrouter", cwd=str(tmp_path), model="test/model", reasoning="low",
        provider_sessions=[],
    )
    monkeypatch.setattr(openrouter_client, "provider_status", lambda **kwargs: (True, "ready"))
    rounds = []
    details = [
        {
            "type": "reasoning.encrypted",
            "data": "opaque-provider-payload",
            "id": "reasoning-1",
            "format": "anthropic-claude-v1",
            "index": 0,
        },
    ]

    def fake_stream(messages, model, **kwargs):
        rounds.append(json.loads(json.dumps(messages)))
        if len(rounds) == 1:
            yield {
                "done": True,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning": "plaintext alias",
                    "reasoning_details": details,
                    "tool_calls": [_raw_call("read_file", relative_path="hello.txt")],
                },
                "usage": {},
                "finish_reason": "tool_calls",
                "stream_complete": True,
            }
        else:
            yield {
                "done": True,
                "message": {"role": "assistant", "content": "Done."},
                "usage": {},
                "finish_reason": "stop",
                "stream_complete": True,
            }

    monkeypatch.setattr(openrouter_client, "stream_chat", fake_stream)
    outcome, summary = job._run_openrouter("Read hello.txt", [])

    assert outcome == "completed" and summary == "Done."
    prior_call = next(message for message in rounds[1] if message.get("tool_calls"))
    assert prior_call["reasoning_details"] == details
    assert "reasoning" not in prior_call


# ------------------------------------------------- a nested repo is not aiOS


def test_the_repo_itself_gets_the_self_description(job):
    assert code_jobs.CodeJob._is_own_repo(code_jobs.ROOT) is True


def test_a_nested_checkout_is_its_own_project(tmp_path, monkeypatch):
    """Bench workspaces live under the repo. Told they were aiOS, one agent
    spent eight calls hunting for aios_ui/ in a workspace without it."""
    nested = code_jobs.ROOT / "bench" / "runs" / "x" / "work" / "task"
    monkeypatch.setattr(code_jobs.Path, "resolve", lambda self: self)
    monkeypatch.setattr(code_jobs.Path, "exists", lambda self: self.name == ".git" and "work" in str(self))
    assert code_jobs.CodeJob._is_own_repo(nested) is False


def test_a_parent_of_the_repo_is_not_the_repo(job):
    assert code_jobs.CodeJob._is_own_repo(code_jobs.ROOT.parent) is False
