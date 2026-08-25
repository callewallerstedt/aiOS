"""The HARNESS page describes the harness, so it has to read the harness.

The failure mode this file exists to prevent is a page that is merely plausible.
A hand-written description of the agent would be wrong the first time someone
added a tool or changed a limit, and nothing would notice -- so the test is not
"does the page list 17 tools", it is "does the page's tool list come from the
same call the model's tool list comes from".
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import code_jobs  # noqa: E402
from aios_ui import harness_api  # noqa: E402

WEB = Path(__file__).resolve().parent.parent / "aios_ui" / "web"


@pytest.fixture()
def meta():
    payload = harness_api.dispatch("/api/harness/meta", "GET", {}, {})
    assert payload and payload.get("ok")
    return payload


def test_the_tool_list_is_the_schema_the_model_is_given(meta):
    """Not a copy of it. The same call, so it cannot fall behind.

    The page documents the full schema. What a given session is offered is a
    subset -- spawn_agent is withheld when the scout role is switched off.
    """
    live = [entry["function"]["name"] for entry in code_jobs.CodeJob._local_tool_schema()]
    assert [row["name"] for row in meta["tools"]] == live
    for row in meta["tools"]:
        assert row["description"], f"{row['name']} has no description"


def test_every_tool_says_whether_it_can_run_alongside_others(meta):
    """The parallel/sequential split is the harness's, not the page's."""
    for row in meta["tools"]:
        assert row["parallel"] is (row["name"] in code_jobs.PARALLEL_SAFE_TOOLS)
        assert row["subagent"] is (row["name"] in code_jobs.SUBAGENT_TOOLS)
    # An edit that could interleave with another edit would be a real bug.
    assert not any(row["parallel"] for row in meta["tools"]
                   if row["name"] in {"edit_file", "write_file", "run_shell"})


def test_every_provider_is_listed_and_says_whose_tools_it_uses(meta):
    assert [row["id"] for row in meta["providers"]] == list(code_jobs.PROVIDERS)
    owners = {row["id"]: row["tools"] for row in meta["providers"]}
    # The split that matters: the two in-process providers use the tools above,
    # the three CLIs arrive with their own.
    assert owners["ollama"] == "aios" and owners["openrouter"] == "aios"
    assert owners["codex"] == owners["claude"] == owners["cursor"] == "own"


def test_the_secondary_models_are_reported_with_their_live_model_ids(meta):
    by_id = {row["id"]: row for row in meta["models"]}
    assert set(by_id) == {"reviewer", "subagent", "titler"}
    assert by_id["reviewer"]["model"] == code_jobs.review_model_default()
    assert by_id["reviewer"]["enabled"] == code_jobs.review_enabled()
    assert by_id["subagent"]["model"] == code_jobs.subagent_model_default()
    assert by_id["titler"]["model"] == code_jobs.title_model_default()
    assert "Planned and Distributed" in by_id["reviewer"]["when"]
    assert "Direct" in by_id["reviewer"]["when"]
    assert "report-only" in by_id["reviewer"]["affects"]


def test_the_limits_are_the_constants_the_loop_reads(meta):
    values = {row["name"]: row["value"] for row in meta["limits"]}
    assert values["Parallel tools"] == str(code_jobs.MAX_PARALLEL_TOOLS)
    assert values["Subagent rounds"] == (
        "unlimited" if code_jobs.SUBAGENT_MAX_ROUNDS <= 0 else str(code_jobs.SUBAGENT_MAX_ROUNDS)
    )
    assert values["Context budget"] == "token-aware"


def test_the_lifecycle_covers_every_state_a_job_can_be_in(meta):
    listed = {row["name"] for row in meta["lifecycle"]}
    assert code_jobs.TERMINAL_STATES | code_jobs.ACTIVE_STATES <= listed


def test_readiness_is_a_separate_request(monkeypatch):
    """The page renders before it knows; a CLI probe costs a subprocess each."""
    monkeypatch.setattr(code_jobs, "provider_status", lambda name: (True, f"{name} ok"))
    payload = harness_api.dispatch("/api/harness/status", "GET", {}, {})
    assert payload["ok"]
    assert {row["id"] for row in payload["providers"]} == set(code_jobs.PROVIDERS)
    assert all(row["ready"] for row in payload["providers"])


def test_a_provider_that_cannot_answer_does_not_break_the_page(monkeypatch):
    def explode(name):
        raise OSError("wsl is not installed")

    monkeypatch.setattr(code_jobs, "provider_status", explode)
    payload = harness_api.dispatch("/api/harness/status", "GET", {}, {})
    assert payload["ok"]
    assert all(row["ready"] is False for row in payload["providers"])
    assert all("wsl" in row["message"] for row in payload["providers"])


def test_the_module_owns_only_its_own_routes():
    assert harness_api.dispatch("/api/code/jobs", "GET", {}, {}) is None
    assert harness_api.dispatch("/api/harness/meta", "POST", {}, {}) is None


# ---------------------------------------------------------------------- wiring


def test_the_page_is_reachable_from_code_and_from_bench():
    code = (WEB / "js" / "code.js").read_text(encoding="utf-8")
    bench = (WEB / "js" / "bench.js").read_text(encoding="utf-8")
    assert 'data-code="harness"' in code and 'this.shell.show("HARNESS")' in code
    assert 'data-bench="harness"' in bench and 'this.shell.show("HARNESS")' in bench


def test_harness_is_a_code_subpage_not_a_rail_tab():
    """It must not become the tab aiOS reopens on, and CODE stays lit inside it."""
    app = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    assert 'CODE_PAGES = new Set(["BENCH", "HARNESS"])' in app
    assert re.search(r'if \(!(?:this\.phoneMirror && )?!CODE_PAGES\.has\(name\)\)\s*\{?\s*api\("/api/config"', app)
    assert re.search(r'const TABS = \[(?:(?!\]).)*\]', app, re.S).group(0).count("HARNESS") == 0
    assert 'new HarnessTab(this.page, this)' in app


def test_the_page_has_styles_and_they_are_loaded():
    assert (WEB / "css" / "harness.css").exists()
    assert 'href="css/harness.css"' in (WEB / "index.html").read_text(encoding="utf-8")


def test_nothing_about_the_harness_is_hardcoded_in_the_page():
    """Every number and name on the page arrives from the API.

    A literal tool name in the frontend is the beginning of the drift this page
    exists to avoid.
    """
    source = (WEB / "js" / "harness.js").read_text(encoding="utf-8")
    for name in ("edit_file", "run_shell", "spawn_agent", "deepseek", "AGENTS.md"):
        assert name not in source, f"{name} is written into the page instead of read from the harness"
