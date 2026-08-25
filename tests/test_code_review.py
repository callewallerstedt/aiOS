"""Independent review of a finished CODE change.

The implementer is not a reliable witness to its own success, so a separate pass
judges the brief against the actual diff. Two properties matter more than the
review's cleverness:

  * it sees the diff and the brief, and NOT the executor's summary -- a
    persuasive account of intent is exactly the thing that fools a reviewer;
  * it can never turn a finished job into a failed one. The change is already
    on disk; a reviewer that crashes must cost a second opinion, nothing more.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import code_jobs  # noqa: E402
import code_roles  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_code_storage(tmp_path, monkeypatch):
    """Review tests must never create sessions in the live CODE registry."""
    jobs_dir = tmp_path / "code_jobs"
    jobs_dir.mkdir()
    config_path = tmp_path / "helper_config.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(code_jobs, "JOBS_DIR", jobs_dir)
    monkeypatch.setattr(code_jobs, "CAPABILITIES_CACHE", jobs_dir / "capabilities.json")
    monkeypatch.setattr(code_jobs, "CONFIG_PATH", config_path)
    monkeypatch.setattr(code_roles, "CONFIG_PATH", config_path)
    with code_jobs._REGISTRY_LOCK:
        code_jobs._LIVE.clear()
    yield jobs_dir
    with code_jobs._REGISTRY_LOCK:
        code_jobs._LIVE.clear()


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=30)


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "calc.py").write_text("def average(values):\n    return 0\n", encoding="utf-8")
    git(tmp_path, "init", "-q")
    git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    return tmp_path


def test_review_jobs_use_isolated_runtime_storage(isolated_code_storage):
    job_id = "review-isolation-regression"
    live_path = ROOT / "code_jobs" / job_id
    assert not live_path.exists()

    runner = code_jobs.CodeJob(job_id)
    runner.directory.mkdir(parents=True, exist_ok=True)
    runner.save(id=runner.id, provider="openrouter", status="running")

    assert runner.directory == isolated_code_storage / job_id
    assert runner.meta_path.is_file()
    assert not live_path.exists()


# ------------------------------------------------------------- collecting


def test_a_clean_tree_has_nothing_to_review(repo):
    assert code_jobs.collect_change(repo)["available"] is False


def test_the_diff_is_what_changed_since_the_last_commit(repo):
    (repo / "calc.py").write_text("def average(values):\n    return 1\n", encoding="utf-8")
    change = code_jobs.collect_change(repo)
    assert change["available"] is True
    assert "-    return 0" in change["diff"]
    assert "+    return 1" in change["diff"]


def test_new_files_are_named_but_not_inlined(repo):
    """A vendored directory must not swamp the review with unrequested code."""
    (repo / "vendor.py").write_text("x = 1\n" * 500, encoding="utf-8")
    change = code_jobs.collect_change(repo)
    assert "vendor.py" in change["untracked"]
    assert "x = 1" not in change["diff"]


def test_a_huge_diff_is_truncated_and_says_so(repo, monkeypatch):
    monkeypatch.setattr(code_jobs, "REVIEW_MAX_DIFF_CHARS", 200)
    (repo / "calc.py").write_text("# padding\n" * 400, encoding="utf-8")
    change = code_jobs.collect_change(repo)
    assert len(change["diff"]) == 200
    assert change["diff_truncated"] is True


def test_session_paths_hide_unrelated_dirty_files(repo):
    """A review of this session must not judge other WIP in the working tree."""
    (repo / "calc.py").write_text("def average(values):\n    return 1\n", encoding="utf-8")
    (repo / "unrelated.py").write_text("noise = True\n", encoding="utf-8")
    change = code_jobs.collect_change(repo, paths=["calc.py"])
    assert change["available"] is True
    assert "return 1" in change["diff"]
    assert "unrelated.py" not in change["diff"]
    assert "unrelated.py" not in change["files"]
    assert "unrelated.py" not in change["untracked"]


def test_empty_session_path_list_means_nothing_to_review(repo):
    (repo / "calc.py").write_text("changed\n", encoding="utf-8")
    change = code_jobs.collect_change(repo, paths=[])
    assert change["available"] is False


def test_a_non_repository_is_reported_not_crashed(tmp_path):
    change = code_jobs.collect_change(tmp_path)
    assert change["available"] is False
    assert "not a git repository" in change["reason"]


def test_binary_content_does_not_break_the_diff(repo):
    """git output is bytes; decoding it as the Windows default codec raised."""
    (repo / "calc.py").write_bytes(b"def average(values):\n    return '\x8f\xfe'\n")
    change = code_jobs.collect_change(repo)
    assert change["available"] is True
    assert isinstance(change["diff"], str)


def test_finished_review_evidence_excludes_reverted_scratch_paths(repo):
    (repo / "calc.py").write_text("def average(values):\n    return 1\n", encoding="utf-8")
    meta = {
        "cwd": str(repo),
        "edited_files": ["calc.py", "_sanity.py"],
        "verification": {
            "schema_version": 3,
            "changed_path_hashes": {"calc.py": "current"},
        },
        "diff_snapshots": {
            "source": {
                "files": ["calc.py"],
                "diff": "--- calc.py\n+++ calc.py\n@@ -1,2 +1,2 @@\n-    return 0\n+    return 1",
            },
            "scratch": {
                "files": ["_sanity.py"],
                "diff": "--- _sanity.py\n+++ _sanity.py\n+discarded assumption",
            },
        },
    }

    change = code_jobs.collect_change_for_job(meta)

    assert change["evidence_source"] == "session_snapshots"
    assert change["files"] == ["calc.py"]
    assert "return 1" in change["diff"]
    assert "_sanity.py" not in change["diff"]
    assert "discarded assumption" not in change["diff"]


def test_a_fully_reverted_session_has_nothing_to_review(repo):
    meta = {
        "cwd": str(repo),
        "edited_files": ["_sanity.py"],
        "verification": {"schema_version": 3, "changed_path_hashes": {}},
        "diff_snapshots": {
            "scratch": {"files": ["_sanity.py"], "diff": "+temporary check"},
        },
    }
    assert code_jobs.collect_change_for_job(meta)["available"] is False


def test_followup_review_excludes_changes_carried_from_an_earlier_turn(repo):
    (repo / "calc.py").write_text("def average(values):\n    return 1\n", encoding="utf-8")
    meta = {
        "cwd": str(repo),
        "verification": {
            "schema_version": 4,
            "changed_path_hashes": {"calc.py": "session-change"},
            "current_changed_path_hashes": {},
            "carried_path_hashes": {"calc.py": "session-change"},
        },
        "diff_snapshots": {
            "earlier": {
                "files": ["calc.py"],
                "diff": "--- calc.py\n+++ calc.py\n-    return 0\n+    return 1",
            },
        },
    }

    change = code_jobs.collect_change_for_job(meta)

    assert change["available"] is False
    assert "No file changes" in change["reason"]


# ---------------------------------------------------------------- judging


def fake_reply(monkeypatch, content):
    module = type("M", (), {
        "provider_status": staticmethod(lambda: (True, "ready")),
        "chat": staticmethod(lambda *a, **k: {"choices": [{"message": {"content": content}}]}),
    })
    monkeypatch.setitem(sys.modules, "openrouter_client", module)


def test_the_reviewer_never_sees_the_authors_summary(repo, monkeypatch):
    seen = {}
    module = type("M", (), {
        "provider_status": staticmethod(lambda: (True, "ready")),
        "chat": staticmethod(lambda messages, *a, **k: seen.update(text="\n".join(
            m["content"] for m in messages)) or {"choices": [{"message": {"content": '{"verdict":"pass"}'}}]}),
    })
    monkeypatch.setitem(sys.modules, "openrouter_client", module)
    (repo / "calc.py").write_text("def average(values):\n    return 1\n", encoding="utf-8")
    code_jobs.review_change("make average correct", code_jobs.collect_change(repo))
    assert "make average correct" in seen["text"]
    assert "return 1" in seen["text"]
    # The signal for the thing that must be absent: no author narrative section.
    assert "SUMMARY" not in seen["text"]


def test_reviewer_contract_calls_out_literal_boundary_mismatches():
    contract = code_jobs.REVIEW_CONTRACT.casefold()
    assert "zero- versus one-based indexing" in contract
    assert "validation or error branch" in contract
    assert "scratch assumption is not proof" in contract


def test_a_fenced_json_reply_is_still_parsed(repo, monkeypatch):
    fake_reply(monkeypatch, '```json\n{"verdict": "concerns", "findings": '
                            '[{"severity": "high", "file": "calc.py", "issue": "x", "why": "y"}]}\n```')
    (repo / "calc.py").write_text("changed\n", encoding="utf-8")
    result = code_jobs.review_change("brief", code_jobs.collect_change(repo))
    assert result["verdict"] == "concerns"
    assert result["findings"][0]["file"] == "calc.py"


def test_concerns_without_anything_concrete_is_a_pass(repo, monkeypatch):
    """An unsupported worry is noise; the point of review is specifics."""
    fake_reply(monkeypatch, '{"verdict": "concerns", "findings": [], "unmet": []}')
    (repo / "calc.py").write_text("changed\n", encoding="utf-8")
    assert code_jobs.review_change("brief", code_jobs.collect_change(repo))["verdict"] == "pass"


def test_a_non_json_reply_still_gives_human_output(repo, monkeypatch):
    fake_reply(monkeypatch, "Looks fine to me — the agent handled the brief.")
    (repo / "calc.py").write_text("changed\n", encoding="utf-8")
    result = code_jobs.review_change("brief", code_jobs.collect_change(repo))
    assert result["ok"] is True
    assert result.get("summary")
    assert "fine" in result["summary"].lower()


def test_a_dead_reviewer_is_unavailable_not_a_pass(repo, monkeypatch):
    """Silence must never be read as approval."""
    module = type("M", (), {"provider_status": staticmethod(lambda: (False, "no key"))})
    monkeypatch.setitem(sys.modules, "openrouter_client", module)
    (repo / "calc.py").write_text("changed\n", encoding="utf-8")
    result = code_jobs.review_change("brief", code_jobs.collect_change(repo))
    assert result["verdict"] == "unavailable"
    assert result["verdict"] != "pass"


def test_a_reviewer_that_raises_is_unavailable(repo, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network gone")

    module = type("M", (), {
        "provider_status": staticmethod(lambda: (True, "ready")), "chat": staticmethod(boom)})
    monkeypatch.setitem(sys.modules, "openrouter_client", module)
    (repo / "calc.py").write_text("changed\n", encoding="utf-8")
    assert code_jobs.review_change("brief", code_jobs.collect_change(repo))["verdict"] == "unavailable"


def test_agentic_reviewer_has_read_only_inspection_tools(repo, monkeypatch):
    (repo / "calc.py").write_text("changed\n", encoding="utf-8")
    runner = code_jobs.CodeJob("review-tools")
    runner.directory.mkdir(parents=True, exist_ok=True)
    runner.save(id=runner.id, cwd=str(repo), provider="openrouter", status="running")
    seen = {"round": 0, "tools": set(), "executed": []}

    def fake_stream(_messages, _model, **kwargs):
        seen["round"] += 1
        seen["tools"] = {
            str((tool.get("function") or {}).get("name") or "")
            for tool in kwargs.get("tools") or []
        }
        if seen["round"] == 1:
            yield {
                "done": True,
                "message": {
                    "content": "",
                    "tool_calls": [{
                        "id": "check-1",
                        "type": "function",
                            "function": {"name": "read_file", "arguments": '{"path":"calc.py"}'},
                    }],
                },
                "usage": {"input_tokens": 10, "output_tokens": 2},
            }
        else:
            yield {
                "done": True,
                "message": {"content": '{"verdict":"pass","summary":"Checks passed","findings":[],"unmet":[]}'},
                "usage": {"input_tokens": 12, "output_tokens": 4},
            }

    monkeypatch.setattr("openrouter_client.provider_status", lambda: (True, "ready"))
    monkeypatch.setattr("openrouter_client.stream_chat", fake_stream)

    def execute(project, calls, prefix):
        seen["executed"].append((project, calls, prefix))
        return [{"id": "check-1", "result": '{"path":"calc.py","content":"changed\\n"}'}]

    monkeypatch.setattr(runner, "_execute_tool_calls", execute)
    result = code_jobs.review_change(
        "brief", code_jobs.collect_change(repo), runner=runner, project=repo,
    )
    assert result["verdict"] == "pass"
    assert seen["round"] == 2 and seen["executed"]
    assert {"read_file", "search_text"} <= seen["tools"]
    assert "run_shell" not in seen["tools"]
    assert "edit_file" not in seen["tools"] and "write_file" not in seen["tools"]


def test_diff_contained_literal_mismatch_returns_without_tool_calls(repo, monkeypatch):
    runner = code_jobs.CodeJob("review-diff-first")
    runner.directory.mkdir(parents=True, exist_ok=True)
    runner.save(id=runner.id, cwd=str(repo), provider="openrouter", status="running")
    seen = {"requests": 0}

    def fake_stream(messages, _model, **_kwargs):
        seen["requests"] += 1
        prompt = "\n".join(str(row.get("content") or "") for row in messages)
        assert "DIFF-FIRST" in prompt
        assert "frame_number=0" in prompt
        yield {
            "done": True,
            "message": {"content": json.dumps({
                "verdict": "concerns",
                "summary": "The diff contradicts the one-based contract.",
                "findings": [{
                    "severity": "high",
                    "file": "telemetry/protocol.py",
                    "issue": "Non-bytes input reports frame zero.",
                    "why": "The brief requires a one-based rejected-frame number.",
                }],
                "unmet": [],
            })},
            "usage": {"input_tokens": 20, "output_tokens": 10},
        }

    monkeypatch.setattr("openrouter_client.provider_status", lambda: (True, "ready"))
    monkeypatch.setattr("openrouter_client.stream_chat", fake_stream)
    monkeypatch.setattr(
        runner, "_execute_tool_calls",
        lambda *_args: pytest.fail("reviewer inspected files despite complete diff evidence"),
    )
    change = {
        "available": True,
        "files": ["telemetry/protocol.py"],
        "untracked": [],
        "diff_truncated": False,
        "evidence_source": "session_snapshots",
        "diff": (
            "--- telemetry/protocol.py\n+++ telemetry/protocol.py\n"
            "+raise ProtocolError('bytes required', frame_number=0)"
        ),
    }

    result = code_jobs.review_change(
        "Every rejected decoder condition has a one-based frame number.",
        change,
        runner=runner,
        project=repo,
    )

    assert result["verdict"] == "concerns"
    assert seen["requests"] == 1


def test_agentic_reviewer_normalizes_and_sums_provider_usage(repo, monkeypatch):
    (repo / "calc.py").write_text("changed\n", encoding="utf-8")
    runner = code_jobs.CodeJob("review-usage")
    runner.directory.mkdir(parents=True, exist_ok=True)
    runner.save(id=runner.id, cwd=str(repo), provider="openrouter", status="running")
    rounds = iter([
        {
            "done": True,
            "message": {
                "content": "",
                "tool_calls": [{
                    "id": "read-1", "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"calc.py"}'},
                }],
            },
            "usage": {
                "prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
                "prompt_tokens_details": {"cached_tokens": 40},
                "completion_tokens_details": {"reasoning_tokens": 3},
                "cost": 0.001,
            },
        },
        {
            "done": True,
            "message": {"content": '{"verdict":"pass","summary":"Good"}'},
            "usage": {
                "prompt_tokens": 120, "completion_tokens": 20, "total_tokens": 140,
                "prompt_tokens_details": {"cached_tokens": 60},
                "completion_tokens_details": {"reasoning_tokens": 4},
                "cost": 0.002,
            },
        },
    ])

    def fake_stream(*_args, **_kwargs):
        yield next(rounds)

    monkeypatch.setattr("openrouter_client.provider_status", lambda: (True, "ready"))
    monkeypatch.setattr("openrouter_client.stream_chat", fake_stream)
    monkeypatch.setattr(
        runner, "_execute_tool_calls",
        lambda *_args: [{"id": "read-1", "result": '{"content":"changed\\n"}'}],
    )

    result = code_jobs.review_change(
        "brief", code_jobs.collect_change(repo), runner=runner, project=repo,
    )

    assert result["verdict"] == "pass"
    assert result["usage"] == {
        "input_tokens": 220,
        "cached_input_tokens": 100,
        "output_tokens": 30,
        "reasoning_tokens": 7,
        "total_tokens": 250,
        "cost_usd": 0.003,
    }


def test_agentic_reviewer_forces_a_tool_free_final_verdict(repo, monkeypatch):
    (repo / "calc.py").write_text("changed\n", encoding="utf-8")
    runner = code_jobs.CodeJob("review-final-round")
    runner.directory.mkdir(parents=True, exist_ok=True)
    runner.save(id=runner.id, cwd=str(repo), provider="openrouter", status="running")
    rounds = []

    def fake_stream(_messages, _model, **kwargs):
        offered = bool(kwargs.get("tools"))
        rounds.append(offered)
        if offered:
            yield {
                "done": True,
                "message": {
                    "content": "",
                    "tool_calls": [{
                        "id": f"read-{len(rounds)}", "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"calc.py"}'},
                    }],
                },
                "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
            }
        else:
            yield {
                "done": True,
                "message": {"content": '{"verdict":"pass","summary":"Enough evidence"}'},
                "usage": {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14},
            }

    monkeypatch.setattr("openrouter_client.provider_status", lambda: (True, "ready"))
    monkeypatch.setattr("openrouter_client.stream_chat", fake_stream)
    monkeypatch.setattr(
        runner, "_execute_tool_calls",
        lambda _project, calls, _prefix: [{"id": calls[0]["id"], "result": '{"content":"changed\\n"}'}],
    )

    result = code_jobs.review_change(
        "brief", code_jobs.collect_change(repo), runner=runner, project=repo,
    )

    assert result["verdict"] == "pass"
    assert rounds == ([True] * (code_jobs.REVIEW_MAX_ROUNDS - 1)) + [False]


def test_empty_reviewer_reply_is_unavailable_not_a_fake_concern(repo, monkeypatch):
    fake_reply(monkeypatch, "")
    (repo / "calc.py").write_text("changed\n", encoding="utf-8")

    result = code_jobs.review_change("brief", code_jobs.collect_change(repo))

    assert result["ok"] is False
    assert result["verdict"] == "unavailable"
    assert "without returning a verdict" in result["error"]


def test_findings_render_for_a_human(repo):
    text = code_jobs.CodeJob._review_text({
        "findings": [{"severity": "high", "file": "calc.py", "issue": "divides by zero",
                      "why": "empty list raises"}],
        "unmet": ["handle empty input"],
    })
    assert "[HIGH] calc.py: divides by zero" in text
    assert "[UNMET] handle empty input" in text


def test_review_can_be_switched_off(monkeypatch, tmp_path):
    """Through the role config now, but the key an older install wrote still
    has to turn it off -- an upgrade that silently re-enabled the reviewer
    would bill for a pass the operator had already declined."""
    monkeypatch.setattr(code_roles, "CONFIG_PATH", tmp_path / "cfg.json")
    (tmp_path / "cfg.json").write_text(json.dumps({"code_review_enabled": False}), encoding="utf-8")
    assert code_jobs.review_enabled() is False

    (tmp_path / "cfg.json").write_text(
        json.dumps({"code_roles": {"reviewer": {"enabled": False}}}), encoding="utf-8")
    assert code_jobs.review_enabled() is False


def test_the_reviewer_runs_on_its_own_model(monkeypatch, tmp_path):
    """A second opinion from the same configuration is not a second opinion."""
    monkeypatch.setattr(code_roles, "CONFIG_PATH", tmp_path / "cfg.json")
    (tmp_path / "cfg.json").write_text(json.dumps({"code_review_model": "x/y"}), encoding="utf-8")
    assert code_jobs.review_model_default() == "x/y"
    assert code_jobs.REVIEW_MODEL_DEFAULT != code_jobs.SUBAGENT_MODEL_DEFAULT
    # And the shipped roles must not point the reviewer at the coder's model.
    shipped = code_roles.DEFAULT_ROLES
    assert shipped["reviewer"]["model"] != shipped["coder"]["model"]


# ------------------------------------------------------- cursor shortlist


def test_the_cursor_list_keeps_only_the_useful_families():
    models = [{"id": f"cursor-junk-{i}", "label": "j", "default": False} for i in range(190)]
    models += [{"id": "cursor-grok-4.5-high", "label": "g", "default": False},
               {"id": "composer-2.5", "label": "c", "default": False},
               {"id": "auto", "label": "a", "default": True}]
    kept = code_jobs._shortlist_cursor_models(models)
    assert {m["id"] for m in kept} == {"cursor-grok-4.5-high", "composer-2.5", "auto"}


def test_an_unrecognised_cursor_lineup_falls_back_to_everything():
    """If Cursor renames its models, an empty shortlist would break the provider."""
    models = [{"id": "brand-new-model", "label": "n", "default": True}]
    assert code_jobs._shortlist_cursor_models(models) == models


def test_the_shortlist_always_has_a_default():
    models = [{"id": "composer-2.5", "label": "c", "default": False}]
    assert code_jobs._shortlist_cursor_models(models)[0]["default"] is True
