from __future__ import annotations

import io
import http.client
import json
from pathlib import Path
import subprocess
import time
from urllib.parse import urlsplit

import pytest

from bench import adapters


class Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeProcess:
    next_pid = 7000

    def __init__(self, lines, stderr=b"", returncode=0, hold=False):
        FakeProcess.next_pid += 1
        self.pid = FakeProcess.next_pid
        body = b"".join(
            (json.dumps(line).encode("utf-8") if isinstance(line, dict) else line) + b"\n"
            for line in lines
        )
        self.stdout = io.BytesIO(body)
        self.stderr = io.BytesIO(stderr)
        self.returncode = None if hold else returncode
        self._final_returncode = returncode
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None and not self.terminated:
            raise subprocess.TimeoutExpired("fake", timeout)
        if self.returncode is None:
            self.returncode = self._final_returncode
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.terminated = True
        self.returncode = -9


@pytest.fixture(autouse=True)
def clear_catalogue():
    adapters.catalogue.cache_clear()
    yield
    adapters.catalogue.cache_clear()


@pytest.fixture
def no_diff(monkeypatch):
    monkeypatch.setattr(
        adapters,
        "_git_diff_stats",
        lambda workspace: {"files": [], "files_edited": 0, "lines_added": 0, "lines_deleted": 0},
    )


def _entry(engine, version="test-1"):
    provenance = "api_equivalent" if engine == "claude" else (
        "provider_reported" if engine == "omp" else (
            "model_pricing_estimate" if engine == "hermes" else "unavailable"
        )
    )
    return {
        "id": engine,
        "label": engine.title(),
        "ready": True,
        "version": version,
        "auth": "authenticated",
        "cost_provenance": provenance,
        "sandbox_note": "isolated test",
    }


def _events(job_dir: Path):
    return [json.loads(line) for line in (job_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]


def test_catalogue_probes_clis_once_and_normalizes_claude_ps1(tmp_path, monkeypatch):
    codex = tmp_path / "codex.exe"
    claude_ps1 = tmp_path / "claude.ps1"
    claude_cmd = tmp_path / "claude.cmd"
    codex.touch()
    claude_ps1.touch()
    claude_cmd.touch()
    monkeypatch.setattr(adapters, "find_codex", lambda: str(codex))
    monkeypatch.setattr(adapters, "find_claude", lambda: str(claude_ps1))
    monkeypatch.setattr(adapters.shutil, "which", lambda name: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "empty-local-app-data"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "empty-user-profile"))
    monkeypatch.delenv("KIMI_INSTALL_DIR", raising=False)
    monkeypatch.setattr(adapters, "_openrouter_api_key", lambda: "")
    monkeypatch.setattr(adapters, "_codex_defaults", lambda: ("gpt-exact", "high", "codex_config"))
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        joined = " ".join(command)
        if "login status" in joined:
            return Completed(stdout="Logged in using ChatGPT")
        if "auth status --json" in joined:
            return Completed(stdout=json.dumps({"loggedIn": True}))
        if "rev-parse" in joined:
            return Completed(stdout="abc123\n")
        if str(codex) in command:
            return Completed(stdout="codex-cli 1.2.3\n")
        if str(claude_cmd) in command:
            return Completed(stdout="2.3.4 (Claude Code)\n")
        return Completed(returncode=1)

    monkeypatch.setattr(adapters.subprocess, "run", fake_run)
    first = adapters.catalogue()
    second = adapters.catalogue()
    assert first is second
    assert [row["id"] for row in first] == ["aios", "codex", "claude", "omp", "hermes", "kimi"]
    assert all(
        {"id", "label", "ready", "version", "auth", "cost_provenance", "sandbox_note"} <= set(row)
        for row in first
    )
    assert next(row for row in first if row["id"] == "codex")["ready"] is True
    assert next(row for row in first if row["id"] == "claude")["ready"] is True
    assert next(row for row in first if row["id"] == "codex")["default_model"] == "gpt-exact"
    assert next(row for row in first if row["id"] == "codex")["tool_profile"] == "built-in Codex CLI defaults"
    claude_row = next(row for row in first if row["id"] == "claude")
    assert claude_row["default_model"] == "sonnet"
    assert claude_row["tool_profile"] == "Bash,Edit,Read,Write,Glob,Grep"
    assert claude_row["model_source"] == "official_cli_alias"
    assert any(str(claude_cmd) in command for command in calls)
    assert not any(str(claude_ps1) in command for command in calls)
    assert sum("--version" in command for command in calls) == 2


def test_omp_catalogue_resolves_official_windows_install_and_verified_model(tmp_path, monkeypatch):
    local_app_data = tmp_path / "LocalAppData"
    executable = local_app_data / "omp" / "omp.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setattr(adapters.shutil, "which", lambda name: None)
    monkeypatch.setattr(adapters, "find_codex", lambda: "")
    monkeypatch.setattr(adapters, "find_claude", lambda: "")
    monkeypatch.setattr(adapters, "_openrouter_api_key", lambda: "bench-secret")
    monkeypatch.setattr(adapters, "_git_version", lambda: "test-worktree")
    monkeypatch.setattr(adapters, "_version", lambda path, name: "omp/17.2.11" if name == "omp" and path else "")
    monkeypatch.setattr(adapters, "_codex_defaults", lambda: ("", "", ""))

    assert adapters._omp_path() == str(executable)
    row = next(item for item in adapters.catalogue() if item["id"] == "omp")
    assert row["ready"] is True
    assert row["version"] == "omp/17.2.11"
    assert row["auth"] == "openrouter_configured"
    assert row["default_provider"] == "openrouter"
    assert row["default_model"] == "openrouter/deepseek/deepseek-v4-flash-0731"
    assert row["default_reasoning"] == "high"
    assert row["cost_provenance"] == "provider_reported"
    assert row["tool_profile"] == "read,bash,edit,write,glob,grep,lsp,ast_grep,ast_edit,todo"
    assert "bench-secret" not in json.dumps(row)


def test_hermes_catalogue_resolves_native_windows_install_and_pinned_model(tmp_path, monkeypatch):
    local_app_data = tmp_path / "LocalAppData"
    executable = local_app_data / "hermes" / "hermes-agent" / "venv" / "Scripts" / "hermes.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setattr(adapters.shutil, "which", lambda name: None)
    monkeypatch.setattr(adapters, "find_codex", lambda: "")
    monkeypatch.setattr(adapters, "find_claude", lambda: "")
    monkeypatch.setattr(adapters, "_openrouter_api_key", lambda: "hermes-bench-secret")
    monkeypatch.setattr(adapters, "_git_version", lambda: "test-worktree")
    monkeypatch.setattr(
        adapters,
        "_version",
        lambda path, name: "Hermes Agent v0.20.0 (2026.8.3)" if name == "hermes" and path else "",
    )
    monkeypatch.setattr(adapters, "_codex_defaults", lambda: ("", "", ""))

    assert adapters._hermes_path() == str(executable)
    row = next(item for item in adapters.catalogue() if item["id"] == "hermes")
    assert row["ready"] is True
    assert row["version"] == "Hermes Agent v0.20.0 (2026.8.3)"
    assert row["auth"] == "openrouter_configured"
    assert row["default_provider"] == "openrouter"
    assert row["default_model"] == "deepseek/deepseek-v4-flash-0731"
    assert row["default_reasoning"] == "high"
    assert row["cost_provenance"] == "model_pricing_estimate"
    assert row["tool_profile"] == "file,terminal"
    assert row["supports_cost_limit"] is False
    assert "final text" in row["sandbox_note"]
    assert "hermes-bench-secret" not in json.dumps(row)

    adapters.catalogue.cache_clear()
    monkeypatch.setattr(adapters, "_openrouter_api_key", lambda: "")
    unavailable = next(item for item in adapters.catalogue() if item["id"] == "hermes")
    assert unavailable["ready"] is False
    assert unavailable["auth"] == "not_configured"


def test_kimi_catalogue_resolves_official_windows_install_without_exposing_key(tmp_path, monkeypatch):
    user_profile = tmp_path / "UserProfile"
    executable = user_profile / ".kimi-code" / "bin" / "kimi.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setenv("USERPROFILE", str(user_profile))
    monkeypatch.delenv("KIMI_INSTALL_DIR", raising=False)
    monkeypatch.setattr(adapters.shutil, "which", lambda name: None)
    monkeypatch.setattr(adapters, "find_codex", lambda: "")
    monkeypatch.setattr(adapters, "find_claude", lambda: "")
    monkeypatch.setattr(adapters, "_openrouter_api_key", lambda: "kimi-bench-secret")
    monkeypatch.setattr(adapters, "_git_version", lambda: "test-worktree")
    monkeypatch.setattr(adapters, "_version", lambda path, name: "0.38.0" if name == "kimi" and path else "")
    monkeypatch.setattr(adapters, "_codex_defaults", lambda: ("", "", ""))

    assert adapters._kimi_path() == str(executable)
    row = next(item for item in adapters.catalogue() if item["id"] == "kimi")
    assert row["ready"] is True
    assert row["version"] == "0.38.0"
    assert row["auth"] == "openrouter_configured"
    assert row["default_provider"] == "openrouter"
    assert row["default_model"] == "deepseek/deepseek-v4-flash-0731"
    assert row["cost_provenance"] == "provider_reported"
    assert row["tool_profile"] == "Read,Write,Edit,Grep,Glob,Bash"
    assert row["supports_cost_limit"] is True
    assert "wire.jsonl" in row["sandbox_note"]
    assert "kimi-bench-secret" not in json.dumps(row)


def test_codex_stream_updates_bench_job_events_usage_and_tool_count(tmp_path, monkeypatch, no_diff):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    job_dir = tmp_path / "job"
    executable = tmp_path / "codex.exe"
    executable.touch()
    monkeypatch.setattr(adapters, "find_codex", lambda: str(executable))
    monkeypatch.setattr(adapters, "catalogue", lambda: [_entry("codex", "codex-cli test")])
    captured = {}
    process = FakeProcess([
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "item.started", "item": {"id": "cmd-1", "type": "command_execution", "command": "pytest -q"}},
        {"type": "item.completed", "item": {
            "id": "cmd-1", "type": "command_execution", "command": "pytest -q",
            "status": "completed", "aggregated_output": "2 passed",
        }},
        {"type": "item.completed", "item": {"id": "msg-1", "type": "agent_message", "text": "Implemented and tested."}},
        {"type": "turn.completed", "usage": {
            "input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 30,
        }},
    ])

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(adapters.subprocess, "Popen", fake_popen)
    result = adapters.run_native(
        "codex", workspace, "Fix the bug.", "gpt-test", "high", job_dir, 10,
    )
    assert result["status"] == "completed"
    assert result["usage"] == {
        "input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 30,
        "reasoning_tokens": 0, "total_tokens": 130, "cost_usd": 0.0,
    }
    assert result["tool_calls"] == 1
    command = captured["command"]
    assert command[0] == str(executable)
    assert "--ignore-user-config" in command
    assert command.index("exec") < command.index("--ignore-user-config")
    assert "--ephemeral" in command
    assert "--json" in command
    assert "--dangerously-bypass-approvals-and-sandbox" in command
    assert command[command.index("-C") + 1] == str(workspace.resolve())
    assert 'model_reasoning_effort="high"' in command
    assert command[-1] == "Fix the bug."
    assert captured["kwargs"]["cwd"] == str(workspace.resolve())
    assert captured["kwargs"]["env"]["AIOS_BENCHMARK"] == "1"
    assert "OPENROUTER_API_KEY" not in captured["kwargs"]["env"]
    meta = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    assert meta["status"] == "completed"
    assert meta["native_session_id"] == "thread-1"
    assert meta["usage"]["total_tokens"] == 130
    events = _events(job_dir)
    assert any(row.get("kind") == "assistant" and "Implemented" in row.get("text", "") for row in events)
    activities = [row for row in events if row.get("kind") == "activity" and row.get("activity_id") == "cmd-1"]
    assert {row["phase"] for row in activities} == {"started", "completed"}


def test_claude_uses_safe_ephemeral_budgeted_command_and_reported_cost(tmp_path, monkeypatch, no_diff):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    job_dir = tmp_path / "job"
    ps1 = tmp_path / "claude.ps1"
    cmd = tmp_path / "claude.cmd"
    ps1.touch()
    cmd.touch()
    monkeypatch.setattr(adapters, "find_claude", lambda: str(ps1))
    monkeypatch.setattr(adapters, "catalogue", lambda: [_entry("claude", "Claude 2")])
    captured = {}
    process = FakeProcess([
        {"type": "system", "subtype": "init", "session_id": "session-1", "model": "claude-exact"},
        {"type": "assistant", "message": {"id": "m1", "usage": {
            "input_tokens": 70, "cache_read_input_tokens": 10, "output_tokens": 12,
        }, "content": [
            {"type": "tool_use", "id": "tool-1", "name": "Edit", "input": {"file_path": "app.py"}},
            {"type": "text", "text": "Done."},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tool-1", "content": "ok"},
        ]}},
        {"type": "result", "subtype": "success", "result": "Done.", "total_cost_usd": 0.0125,
         "usage": {"input_tokens": 70, "cache_read_input_tokens": 10, "output_tokens": 12},
         "modelUsage": {
             "claude-haiku-4-5-20251001": {"inputTokens": 1},
             "claude-exact": {"inputTokens": 70},
         }},
    ])

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(adapters.subprocess, "Popen", fake_popen)
    result = adapters.run_native(
        "claude", workspace, "Make the edit.", "sonnet-test", "high", job_dir, 10, max_cost_usd=1.25,
    )
    assert result["status"] == "completed"
    assert result["usage"]["cost_usd"] == pytest.approx(0.0125)
    assert result["usage"]["cached_input_tokens"] == 10
    assert result["cost_provenance"] == "api_equivalent"
    assert result["tool_calls"] == 1
    assert result["model"] == "claude-exact"
    assert result["primary_model"] == "claude-exact"
    assert result["models_used"] == ["claude-exact", "claude-haiku-4-5-20251001"]
    command = captured["command"]
    if adapters.os.name == "nt":
        assert command[:4] == ["cmd.exe", "/d", "/c", str(cmd)]
    else:
        assert command[0] == str(cmd)
    assert "--safe-mode" in command
    assert "--no-session-persistence" in command
    assert command[command.index("--tools") + 1] == "Bash,Edit,Read,Write,Glob,Grep"
    assert command[command.index("--max-budget-usd") + 1] == "1.25"
    assert command[-1] == "Make the edit."
    assert captured["kwargs"]["cwd"] == str(workspace.resolve())
    meta = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    assert meta["native_session_id"] == "session-1"
    assert meta["model"] == "claude-exact"
    assert meta["native_primary_model"] == "claude-exact"
    assert meta["native_models_used"] == ["claude-exact", "claude-haiku-4-5-20251001"]
    assert meta["estimated_cost_usd"] == pytest.approx(0.0125)


def test_claude_stream_and_assembled_message_do_not_duplicate_text_or_tool(tmp_path, monkeypatch, no_diff):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    job_dir = tmp_path / "job"
    cmd = tmp_path / "claude.cmd"
    cmd.touch()
    monkeypatch.setattr(adapters, "find_claude", lambda: str(cmd))
    monkeypatch.setattr(adapters, "catalogue", lambda: [_entry("claude")])
    process = FakeProcess([
        {"type": "stream_event", "event": {"type": "message_start", "message": {
            "id": "m1", "usage": {"input_tokens": 4, "output_tokens": 0},
        }}},
        {"type": "stream_event", "message_id": "m1", "event": {"type": "content_block_start",
         "content_block": {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest"}}}},
        {"type": "stream_event", "message_id": "m1", "event": {"type": "content_block_delta",
         "delta": {"type": "text_delta", "text": "All good."}}},
        {"type": "assistant", "message": {"id": "m1", "usage": {
            "input_tokens": 4, "output_tokens": 2,
        }, "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest"}},
            {"type": "text", "text": "All good."},
        ]}},
        {"type": "result", "subtype": "success", "result": "All good."},
    ])
    monkeypatch.setattr(adapters.subprocess, "Popen", lambda *args, **kwargs: process)
    result = adapters.run_native("claude", workspace, "Test.", "sonnet", "", job_dir, 5)
    assert result["tool_calls"] == 1
    assert result["usage"]["total_tokens"] == 6
    assert result["cost_provenance"] == "unavailable"
    assistant = "".join(row["text"] for row in _events(job_dir) if row.get("kind") == "assistant")
    assert assistant == "All good."
    started = [row for row in _events(job_dir) if row.get("activity_id") == "t1" and row.get("phase") == "started"]
    assert len(started) == 1


def test_claude_cost_only_result_keeps_assistant_token_usage(tmp_path, monkeypatch, no_diff):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cmd = tmp_path / "claude.cmd"
    cmd.touch()
    monkeypatch.setattr(adapters, "find_claude", lambda: str(cmd))
    monkeypatch.setattr(adapters, "catalogue", lambda: [_entry("claude")])
    process = FakeProcess([
        {"type": "assistant", "message": {"id": "m1", "usage": {
            "input_tokens": 11, "output_tokens": 3,
        }, "content": [{"type": "text", "text": "Done."}]}},
        {"type": "result", "subtype": "success", "result": "Done.", "total_cost_usd": 0.02},
    ])
    monkeypatch.setattr(adapters.subprocess, "Popen", lambda *args, **kwargs: process)
    result = adapters.run_native("claude", workspace, "Test.", "sonnet", "", tmp_path / "job", 5)
    assert result["usage"]["total_tokens"] == 14
    assert result["usage"]["cost_usd"] == pytest.approx(0.02)
    assert result["cost_provenance"] == "api_equivalent"


def test_omp_json_run_is_ephemeral_bounded_and_preserves_exact_usage(tmp_path, monkeypatch, no_diff):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    job_dir = tmp_path / "job"
    executable = tmp_path / "omp.exe"
    executable.touch()
    monkeypatch.setattr(adapters, "_omp_path", lambda: str(executable))
    monkeypatch.setattr(adapters, "_openrouter_api_key", lambda: "omp-bench-secret")
    monkeypatch.setattr(adapters, "catalogue", lambda: [_entry("omp", "omp/17.2.11")])
    captured = {}
    first_message = {
        "role": "assistant",
        "provider": "openrouter",
        "model": "deepseek/deepseek-v4-flash-0731",
        "responseId": "response-1",
        "content": [{"type": "text", "text": "Inspecting."}],
        "usage": {
            "input": 100,
            "output": 20,
            "cacheRead": 10,
            "cacheWrite": 5,
            "totalTokens": 135,
            "reasoningTokens": 8,
            "cost": {"input": 0.004, "output": 0.006, "cacheRead": 0, "cacheWrite": 0, "total": 0.01},
        },
        "stopReason": "toolUse",
    }
    final_message = {
        "role": "assistant",
        "provider": "openrouter",
        "model": "deepseek/deepseek-v4-flash-0731",
        "responseId": "response-2",
        "content": [{"type": "text", "text": "Implemented."}],
        "usage": {
            "input": 50,
            "output": 10,
            "cacheRead": 5,
            "cacheWrite": 0,
            "totalTokens": 65,
            "reasoningTokens": 4,
            "cost": {"input": 0.002, "output": 0.003, "cacheRead": 0, "cacheWrite": 0, "total": 0.005},
        },
        "stopReason": "stop",
    }
    process = FakeProcess([
        {"type": "session", "id": "omp-session-1", "cwd": str(workspace)},
        {"type": "message_start", "message": {"role": "assistant", "content": []}},
        {"type": "message_update", "assistantMessageEvent": {
            "type": "text_delta", "contentIndex": 0, "delta": "Inspecting.",
        }},
        {"type": "message_end", "message": first_message},
        {"type": "tool_execution_start", "toolCallId": "tool-1", "toolName": "edit",
         "args": {"path": "app.py"}},
        {"type": "tool_execution_end", "toolCallId": "tool-1", "toolName": "edit",
         "args": {"path": "app.py"}, "result": {"content": [{"type": "text", "text": "ok"}]},
         "isError": False},
        {"type": "message_start", "message": {"role": "assistant", "content": []}},
        {"type": "message_update", "assistantMessageEvent": {
            "type": "text_delta", "contentIndex": 0, "delta": "Implemented.",
        }},
        {"type": "message_end", "message": final_message},
        # OMP repeats completed messages in some JSON streams. Request count
        # and per-round usage must fingerprint-deduplicate this envelope.
        {"type": "message_end", "message": final_message},
        {"type": "agent_end", "messages": [first_message, final_message], "isTerminal": True},
    ])

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(adapters.subprocess, "Popen", fake_popen)
    result = adapters.run_native(
        "omp",
        workspace,
        "Fix the implementation.",
        "openrouter/deepseek/deepseek-v4-flash-0731",
        "high",
        job_dir,
        12.5,
        max_cost_usd=0.25,
    )
    assert result["status"] == "completed"
    assert result["summary"] == "Implemented."
    assert result["usage"] == {
        "input_tokens": 150,
        "cached_input_tokens": 20,
        "canonical_prompt_tokens": 170,
        "output_tokens": 30,
        "reasoning_tokens": 12,
        "total_tokens": 200,
        "cost_usd": pytest.approx(0.015),
    }
    assert result["cost_provenance"] == "provider_reported"
    assert result["tool_calls"] == 1
    assert result["model"] == "openrouter/deepseek/deepseek-v4-flash-0731"
    assert result["model_request_count"] == 2
    assert result["model_request_count_source"] == "omp_unique_assistant_message_end"
    assert [row["stop_reason"] for row in result["model_request_rounds"]] == ["toolUse", "stop"]
    assert result["model_request_rounds"][0]["usage_raw"]["cacheRead"] == 10
    assert result["model_request_rounds"][1]["usage"]["total_tokens"] == 65

    command = captured["command"]
    assert command[0] == str(executable)
    assert command[command.index("--mode") + 1] == "json"
    assert "--no-session" in command
    assert command[command.index("--cwd") + 1] == str(workspace.resolve())
    assert command[command.index("--model") + 1] == "openrouter/deepseek/deepseek-v4-flash-0731"
    assert command[command.index("--thinking") + 1] == "high"
    assert command[command.index("--tools") + 1] == adapters._OMP_TOOLS
    assert command[command.index("--approval-mode") + 1] == "yolo"
    assert command[command.index("--max-time") + 1] == "12.5s"
    assert {"--no-extensions", "--no-skills", "--no-rules", "--no-title", "--no-pty"} <= set(command)
    assert "task" not in command[command.index("--tools") + 1].split(",")
    assert command[-2:] == ["--", "Fix the implementation."]
    assert "--max-budget-usd" not in command
    assert captured["kwargs"]["cwd"] == str(workspace.resolve())
    assert captured["kwargs"]["env"]["OPENROUTER_API_KEY"] == "omp-bench-secret"

    meta = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    assert meta["status"] == "completed"
    assert meta["native_session_id"] == "omp-session-1"
    assert meta["usage"]["total_tokens"] == 200
    assert meta["estimated_cost_usd"] == pytest.approx(0.015)
    assert meta["model_request_count"] == 2
    assert len(meta["model_request_rounds"]) == 2
    persisted = (job_dir / "job.json").read_text(encoding="utf-8") + (job_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "omp-bench-secret" not in persisted
    assistant = "".join(row["text"] for row in _events(job_dir) if row.get("kind") == "assistant")
    assert assistant == "Inspecting.Implemented."


def test_kimi_stream_and_wire_are_isolated_bounded_and_preserve_exact_usage(tmp_path, monkeypatch, no_diff):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    job_dir = tmp_path / "job"
    executable = tmp_path / "kimi.exe"
    executable.touch()
    secret = "sk-or-kimi-bench-secret"
    model = "deepseek/deepseek-v4-flash-0731"
    monkeypatch.setattr(adapters, "_kimi_path", lambda: str(executable))
    monkeypatch.setattr(adapters, "_openrouter_api_key", lambda: secret)
    monkeypatch.setattr(adapters, "_kimi_context_size", lambda exact: 131_072)
    kimi_entry = {
        **_entry("kimi", "0.38.0"),
        "default_provider": "openrouter",
        "default_model": model,
        "default_reasoning": "high",
    }
    monkeypatch.setattr(adapters, "catalogue", lambda: [kimi_entry])
    monkeypatch.setenv("KIMI_MODEL_NAME", "must-not-inherit")
    monkeypatch.setenv("KIMI_CODE_HOME", str(tmp_path / "must-not-inherit"))
    captured = {}
    audit_calls = []
    exact_redactor = adapters._redact_kimi_secret

    def recording_redactor(root, exact_secret):
        audit_calls.append((Path(root).resolve(), exact_secret))
        return exact_redactor(root, exact_secret)

    monkeypatch.setattr(adapters, "_redact_kimi_secret", recording_redactor)

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        home = Path(kwargs["env"]["KIMI_CODE_HOME"])
        wire = home / "sessions" / "workspace-hash" / "kimi-session-1" / "agents" / "main" / "wire.jsonl"
        wire.parent.mkdir(parents=True)
        usages = [
            {"inputOther": 100, "inputCacheRead": 10, "inputCacheCreation": 5, "output": 20},
            {"inputOther": 50, "inputCacheRead": 20, "inputCacheCreation": 0, "output": 10},
        ]
        records = [
            {"type": "metadata", "protocol_version": "1.5"},
            {
                "type": "profile.bind",
                "profileName": adapters._KIMI_AGENT_NAME,
                "systemPrompt": adapters._KIMI_AGENT_PROMPT,
                "activeToolNames": list(adapters._KIMI_TOOLS),
                "disallowedTools": [],
                "subagents": [],
            },
            {"type": "llm.tools_snapshot", "hash": "tools-1", "tools": [
                {"name": name, "description": "test", "parameters": {}}
                for name in adapters._KIMI_TOOLS
            ]},
            {
                "type": "llm.request", "kind": "loop", "provider": "openai", "model": model,
                "thinkingEffort": "high", "turnStep": 0, "attempt": 1, "toolsHash": "tools-1",
            },
            {"type": "context.append_loop_event", "event": {
                "type": "tool.call", "uuid": "tool-event-1", "stepUuid": "step-1",
                "toolCallId": "call-1", "name": "Read",
            }},
            {"type": "context.append_loop_event", "event": {
                "type": "step.end", "uuid": "step-1", "usage": usages[0], "finishReason": "tool_calls",
            }},
            {"type": "usage.record", "model": "__internal_profile_alias__", "usage": usages[0], "usageScope": "turn"},
            {
                "type": "llm.request", "kind": "loop", "provider": "openai", "model": model,
                "thinkingEffort": "high", "turnStep": 1, "attempt": 1, "toolsHash": "tools-1",
            },
            {"type": "context.append_loop_event", "event": {
                "type": "content.part", "uuid": "content-2", "stepUuid": "step-2",
                "part": {"type": "text", "text": "Implemented and verified."},
            }},
            {"type": "context.append_loop_event", "event": {
                "type": "step.end", "uuid": "step-2", "usage": usages[1], "finishReason": "end_turn",
            }},
            {"type": "usage.record", "model": "__internal_profile_alias__", "usage": usages[1], "usageScope": "turn"},
        ]
        wire.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )
        return FakeProcess([
            {"role": "meta", "type": "system.version", "version": "0.38.0"},
            {"role": "meta", "type": "session.resume_hint", "session_id": "kimi-session-1"},
            {"role": "assistant", "content": "Inspecting.", "tool_calls": [{
                "id": "call-1", "type": "function",
                "function": {"name": "Read", "arguments": json.dumps({"path": "app.py"})},
            }]},
            {"role": "tool", "tool_call_id": "call-1", "content": f"ok ({secret})"},
            {"role": "assistant", "content": "Implemented and verified."},
        ])

    monkeypatch.setattr(adapters.subprocess, "Popen", fake_popen)
    result = adapters.run_native(
        "kimi", workspace, "Fix the implementation.", model, "high", job_dir, 12.5, max_cost_usd=0.25,
    )

    assert result["status"] == "completed"
    assert result["summary"] == "Implemented and verified."
    assert result["usage"] == {
        "input_tokens": 150,
        "cached_input_tokens": 35,
        "canonical_prompt_tokens": 185,
        "output_tokens": 30,
        "reasoning_tokens": 0,
        "total_tokens": 215,
        "cost_usd": 0.0,
    }
    assert result["cost_provenance"] == "unavailable"
    assert result["tool_calls"] == 1
    assert result["model"] == model
    assert result["primary_model"] == model
    assert result["models_used"] == [model]
    assert result["model_request_count"] == 2
    assert result["model_request_count_source"] == "kimi_wire_llm_request"
    assert [row["usage_raw"]["inputCacheRead"] for row in result["model_request_rounds"]] == [10, 20]

    command = captured["command"]
    assert command[0] == str(executable)
    assert command[command.index("--agent-file") + 1].endswith("aios-bench-coder.md")
    assert command[command.index("--skills-dir") + 1].endswith("empty-skills")
    assert command[command.index("-p") + 1] == "Fix the implementation."
    assert command[command.index("--output-format") + 1] == "stream-json"
    assert "--model" not in command
    assert "--yolo" not in command
    assert secret not in " ".join(command)

    environment = captured["kwargs"]["env"]
    home = Path(environment["KIMI_CODE_HOME"])
    assert home.parent == job_dir.resolve()
    assert home.name.startswith("kimi-home-")
    assert environment["KIMI_MODEL_NAME"] == model
    assert environment["KIMI_MODEL_API_KEY"].startswith("aios-bench-")
    assert environment["KIMI_MODEL_API_KEY"] != secret
    assert environment["KIMI_MODEL_BASE_URL"].startswith("http://127.0.0.1:")
    assert environment["KIMI_MODEL_BASE_URL"].endswith("/v1")
    assert environment["KIMI_MODEL_MAX_CONTEXT_SIZE"] == "131072"
    assert environment["KIMI_DISABLE_TELEMETRY"] == "1"
    assert environment["KIMI_CODE_NO_AUTO_UPDATE"] == "1"
    assert environment["KIMI_DISABLE_CRON"] == "1"
    assert environment["KIMI_CODE_BACKGROUND_KEEP_ALIVE_ON_EXIT"] == "0"
    assert environment["KIMI_LOOP_MAX_STEPS_PER_TURN"] == str(adapters._KIMI_MAX_STEPS)
    assert environment["KIMI_LOOP_MAX_ATTEMPTS_PER_STEP"] == str(adapters._KIMI_MAX_ATTEMPTS)
    assert "OPENROUTER_API_KEY" not in environment
    assert all(secret not in str(value) for value in environment.values())

    config = adapters.tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    assert config["telemetry"] is False
    assert config["builtin_product_skills"] is False
    assert config["tools"]["enabled"] == list(adapters._KIMI_TOOLS)
    assert config["loop_control"] == {
        "max_steps_per_turn": adapters._KIMI_MAX_STEPS,
        "max_attempts_per_step": adapters._KIMI_MAX_ATTEMPTS,
    }
    assert config["background"]["keep_alive_on_exit"] is False
    assert config["background"]["print_background_mode"] == "exit"
    agent = (home / "aios-bench-coder.md").read_text(encoding="utf-8")
    assert adapters._KIMI_AGENT_MARKER in agent
    assert "initial working directory is already the repository root" in agent
    assert "never scan a filesystem root" in agent
    assert "subagents: []" in agent
    assert "${base_prompt}" not in agent
    assert not any((home / "empty-skills").iterdir())

    meta = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    assert meta["status"] == "completed"
    assert meta["native_session_id"] == "kimi-session-1"
    assert meta["native_stream_version"] == "0.38.0"
    assert meta["native_prompt_isolated"] is True
    assert meta["native_tool_profile_verified"] is True
    assert meta["native_usage_source"] == "usage.record"
    assert meta["native_api_calls"] == 2
    assert meta["native_credential_redacted"] is False
    assert meta["native_proxy_requests"] == 0
    assert (home.resolve(), secret) in audit_calls
    assert (workspace.resolve(), secret) in audit_calls
    assert meta["estimated_cost_usd"] == 0.0
    assert all(secret.encode("utf-8") not in path.read_bytes() for path in job_dir.rglob("*") if path.is_file())
    tool_events = [row for row in _events(job_dir) if row.get("activity_id") == "call-1"]
    assert {row["phase"] for row in tool_events} == {"started", "completed"}
    assert all(secret not in json.dumps(row) for row in _events(job_dir))


def test_kimi_proxy_keeps_real_key_parent_side_and_blocks_next_request_at_observed_cap(monkeypatch):
    real_key = "sk-or-parent-only"
    model = "vendor/exact-model"
    captured = {}

    class FakeUpstream:
        status = 200
        headers = {"Content-Type": "text/event-stream"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            return iter([
                b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": 0.02}}\n',
                b'data: [DONE]\n',
            ])

    def fake_urlopen(request, timeout):
        captured["authorization"] = request.get_header("Authorization")
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeUpstream()

    monkeypatch.setattr(adapters, "_kimi_model_pricing", lambda _model: {
        "input": 1.0, "cached_input": 0.5, "output": 2.0,
    })
    monkeypatch.setattr(adapters, "urlopen", fake_urlopen)
    proxy = adapters._KimiOpenRouterProxy(real_key, model, 0.01)
    proxy.start()
    try:
        endpoint = urlsplit(proxy.base_url)
        request_body = json.dumps({"model": model, "stream": True, "messages": []})
        headers = {
            "Authorization": f"Bearer {proxy.client_token}",
            "Content-Type": "application/json",
        }

        first = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=3)
        first.request("POST", endpoint.path + "/chat/completions", body=request_body, headers=headers)
        first_response = first.getresponse()
        first_body = first_response.read()
        first.close()

        second = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=3)
        second.request("POST", endpoint.path + "/chat/completions", body=request_body, headers=headers)
        second_response = second.getresponse()
        second_body = json.loads(second_response.read().decode("utf-8"))
        second.close()

        assert first_response.status == 200
        assert b'"cost": 0.02' in first_body
        assert captured["authorization"] == f"Bearer {real_key}"
        assert captured["payload"]["model"] == model
        assert captured["payload"]["usage"] == {"include": True}
        assert second_response.status == 402
        assert second_body["error"]["code"] == "budget_exhausted"
        assert proxy.snapshot() == {
            "spent_usd": 0.02,
            "max_cost_usd": 0.01,
            "requests": 1,
            "blocked_requests": 1,
            "budget_exhausted": True,
            "cost_provenance": "provider_reported",
            "accounting_error": "",
        }
    finally:
        proxy.close()
    assert proxy._api_key == ""


def test_benchmark_environment_scrubs_generic_secrets_and_kimi_uses_allowlist(monkeypatch):
    monkeypatch.setenv("UNLISTED_VENDOR_CREDENTIAL", "must-not-leak")
    monkeypatch.setenv("ANOTHER_SERVICE_APIKEY", "must-not-leak-either")
    monkeypatch.setenv("AIOS_UNRELATED_RUNTIME_FLAG", "safe-but-unneeded")

    generic = adapters._benchmark_environment("claude")
    assert "UNLISTED_VENDOR_CREDENTIAL" not in generic
    assert "ANOTHER_SERVICE_APIKEY" not in generic
    assert generic["AIOS_UNRELATED_RUNTIME_FLAG"] == "safe-but-unneeded"

    kimi = adapters._benchmark_environment("kimi")
    assert "UNLISTED_VENDOR_CREDENTIAL" not in kimi
    assert "ANOTHER_SERVICE_APIKEY" not in kimi
    assert "AIOS_UNRELATED_RUNTIME_FLAG" not in kimi


def test_kimi_reasoning_preserves_exact_boundary_efforts():
    assert adapters._kimi_reasoning("minimal") == (True, "minimal")
    assert adapters._kimi_reasoning("ultra") == (True, "ultra")


def test_kimi_usage_uses_all_wire_prompt_buckets():
    assert adapters._kimi_usage({
        "inputOther": 9, "inputCacheRead": 4, "inputCacheCreation": 3, "output": 2,
    }) == {
        "input_tokens": 9,
        "cached_input_tokens": 7,
        "canonical_prompt_tokens": 16,
        "output_tokens": 2,
        "reasoning_tokens": 0,
        "total_tokens": 18,
        "cost_usd": 0.0,
    }


def test_kimi_refuses_project_runtime_overrides_before_start(tmp_path, monkeypatch, no_diff):
    workspace = tmp_path / "workspace"
    (workspace / ".kimi-code").mkdir(parents=True)
    (workspace / ".kimi-code" / "local.toml").write_text("telemetry = true\n", encoding="utf-8")
    monkeypatch.setattr(adapters, "catalogue", lambda: [_entry("kimi")])
    monkeypatch.setattr(
        adapters.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("a contaminated Kimi workspace must not start a process"),
    )

    result = adapters.run_native(
        "kimi", workspace, "Work.", adapters._KIMI_DEFAULT_MODEL, "high", tmp_path / "job", 5,
    )
    assert result["status"] == "failed"
    assert ".kimi-code\\local.toml" in result["error"] or ".kimi-code/local.toml" in result["error"]
    assert not list((tmp_path / "job").glob("kimi-home-*"))


def test_kimi_wire_detects_prompt_contamination_and_redacts_durable_secrets(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    runtime = adapters._prepare_kimi_runtime(job_dir, workspace, "high")
    wire = runtime["home"] / "sessions" / "workspace" / "session" / "agents" / "main" / "wire.jsonl"
    wire.parent.mkdir(parents=True)
    records = [
        {
            "type": "profile.bind", "profileName": adapters._KIMI_AGENT_NAME,
            "systemPrompt": adapters._KIMI_AGENT_PROMPT + "\nproject override",
            "activeToolNames": list(adapters._KIMI_TOOLS),
        },
        {"type": "llm.tools_snapshot", "tools": [{"name": name} for name in adapters._KIMI_TOOLS]},
    ]
    wire.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
    report, error = adapters._read_kimi_wire(runtime["home"])
    assert error == ""
    assert report["prompt_isolated"] is False
    assert "prompt contamination" in report["isolation_error"]

    secret = "sk-durable-secret"
    (runtime["home"] / "diagnostic.log").write_text(f"credential={secret}\n", encoding="utf-8")
    binary = runtime["home"] / "state.bin"
    binary.write_bytes(b"prefix:" + secret.encode("utf-8") + b":suffix")
    redacted, audit_error = adapters._redact_kimi_secret(runtime["home"], secret)
    assert redacted is True
    assert audit_error == ""
    assert secret not in (runtime["home"] / "diagnostic.log").read_text(encoding="utf-8")
    assert secret.encode("utf-8") not in binary.read_bytes()


def test_hermes_oneshot_is_private_bounded_and_preserves_estimated_usage(tmp_path, monkeypatch, no_diff):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    job_dir = tmp_path / "job"
    executable = tmp_path / "hermes.exe"
    executable.touch()
    monkeypatch.setattr(adapters, "_hermes_path", lambda: str(executable))
    monkeypatch.setattr(adapters, "_openrouter_api_key", lambda: "hermes-bench-secret")
    monkeypatch.setattr(adapters, "catalogue", lambda: [_entry("hermes", "Hermes Agent v0.20.0")])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak-either")
    captured = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        usage_path = Path(command[command.index("--usage-file") + 1])
        captured["usage_path"] = usage_path
        usage_path.write_text(json.dumps({
            "estimated_cost_usd": 0.0123,
            "cost_status": "estimated",
            "cost_source": "model_pricing",
            "input_tokens": 101,
            "output_tokens": 21,
            "cache_read_tokens": 13,
            "cache_write_tokens": 7,
            "reasoning_tokens": 8,
            "total_tokens": 150,
            "api_calls": 4,
            "model": "deepseek/deepseek-v4-flash-0731",
            "provider": "openrouter",
            "session_id": "hermes-session-1",
            "completed": True,
            "failed": False,
            "service_tier": "default",
        }), encoding="utf-8")
        return FakeProcess([b"Implemented and verified."])

    monkeypatch.setattr(adapters.subprocess, "Popen", fake_popen)
    result = adapters.run_native(
        "hermes",
        workspace,
        "Fix the implementation.",
        "deepseek/deepseek-v4-flash-0731",
        "high",
        job_dir,
        12.5,
        max_cost_usd=0.25,
    )
    assert result["status"] == "completed"
    assert result["summary"] == "Implemented and verified."
    assert result["usage"] == {
        "input_tokens": 101,
        "cached_input_tokens": 20,
        "canonical_prompt_tokens": 121,
        "output_tokens": 21,
        "reasoning_tokens": 8,
        "total_tokens": 150,
        "cost_usd": pytest.approx(0.0123),
    }
    assert result["cost_provenance"] == "model_pricing_estimate"
    assert result["tool_calls"] == 0
    assert result["model"] == "deepseek/deepseek-v4-flash-0731"
    assert result["model_request_count"] == 4
    assert result["model_request_count_source"] == "hermes_usage_api_calls"
    assert result["model_request_rounds"] == []

    command = captured["command"]
    assert command[0] == str(executable)
    assert command[command.index("-z") + 1] == "Fix the implementation."
    assert command[command.index("--model") + 1] == "deepseek/deepseek-v4-flash-0731"
    assert command[command.index("--provider") + 1] == "openrouter"
    assert command[command.index("--reasoning") + 1] == "high"
    assert command[command.index("--toolsets") + 1] == "file,terminal"
    assert {"--yolo", "--safe-mode"} <= set(command)
    assert "--max-budget-usd" not in command
    assert captured["usage_path"] == job_dir.resolve() / "hermes-usage.json"
    assert not (workspace / "hermes-usage.json").exists()

    environment = captured["kwargs"]["env"]
    assert captured["kwargs"]["cwd"] == str(workspace.resolve())
    assert environment["OPENROUTER_API_KEY"] == "hermes-bench-secret"
    assert environment["HERMES_HOME"] == str(job_dir.resolve() / "hermes-home")
    assert environment["HERMES_WRITE_SAFE_ROOT"] == str(workspace.resolve())
    assert "ANTHROPIC_API_KEY" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert (job_dir / "hermes-home").is_dir()

    meta = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    assert meta["status"] == "completed"
    assert meta["native_session_id"] == "hermes-session-1"
    assert meta["native_provider"] == "openrouter"
    assert meta["native_api_calls"] == 4
    assert meta["model_request_count"] == 4
    assert meta["native_cost_status"] == "estimated"
    assert meta["usage"]["total_tokens"] == 150
    assert meta["estimated_cost_usd"] == pytest.approx(0.0123)
    persisted = (job_dir / "job.json").read_text(encoding="utf-8") + (job_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "hermes-bench-secret" not in persisted
    assert "must-not-leak" not in persisted
    assistant = "".join(row["text"] for row in _events(job_dir) if row.get("kind") == "assistant")
    assert assistant == "Implemented and verified."
    assert not any(row.get("kind") == "activity" for row in _events(job_dir))


def test_hermes_usage_failure_is_terminal_but_keeps_spend_attribution(tmp_path, monkeypatch, no_diff):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = tmp_path / "hermes.exe"
    executable.touch()
    monkeypatch.setattr(adapters, "_hermes_path", lambda: str(executable))
    monkeypatch.setattr(adapters, "_openrouter_api_key", lambda: "bench-secret")
    monkeypatch.setattr(adapters, "catalogue", lambda: [_entry("hermes")])

    def fake_popen(command, **kwargs):
        usage_path = Path(command[command.index("--usage-file") + 1])
        usage_path.write_text(json.dumps({
            "estimated_cost_usd": 0.004,
            "input_tokens": 40,
            "output_tokens": 2,
            "cache_read_tokens": 5,
            "cache_write_tokens": 0,
            "reasoning_tokens": 1,
            "total_tokens": 47,
            "api_calls": 1,
            "model": "deepseek/deepseek-v4-flash-0731",
            "provider": "openrouter",
            "session_id": "failed-session",
            "completed": False,
            "failed": True,
            "failure": "OpenRouter request failed",
        }), encoding="utf-8")
        return FakeProcess([], returncode=2)

    monkeypatch.setattr(adapters.subprocess, "Popen", fake_popen)
    result = adapters.run_native(
        "hermes", workspace, "Work.", "deepseek/deepseek-v4-flash-0731", "high", tmp_path / "job", 5,
    )
    assert result["status"] == "failed"
    assert result["exit_code"] == 2
    assert result["error"] == "OpenRouter request failed"
    assert result["usage"]["total_tokens"] == 47
    assert result["usage"]["cost_usd"] == pytest.approx(0.004)
    assert result["cost_provenance"] == "model_pricing_estimate"


def test_hermes_clean_response_without_usage_completes_with_unavailable_cost(tmp_path, monkeypatch, no_diff):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = tmp_path / "hermes.exe"
    executable.touch()
    monkeypatch.setattr(adapters, "_hermes_path", lambda: str(executable))
    monkeypatch.setattr(adapters, "_openrouter_api_key", lambda: "bench-secret")
    monkeypatch.setattr(adapters, "catalogue", lambda: [_entry("hermes")])
    monkeypatch.setattr(adapters.subprocess, "Popen", lambda *args, **kwargs: FakeProcess([b"Done."]))

    result = adapters.run_native(
        "hermes", workspace, "Work.", "deepseek/deepseek-v4-flash-0731", "high", tmp_path / "job", 5,
    )
    assert result["status"] == "completed"
    assert result["summary"] == "Done."
    assert result["usage"] == adapters._ZERO_USAGE
    assert result["cost_provenance"] == "unavailable"
    meta = json.loads((tmp_path / "job" / "job.json").read_text(encoding="utf-8"))
    assert meta["native_usage_error"] == "Hermes did not write its usage report"


def test_hermes_command_maps_off_to_none_and_uses_pinned_model(tmp_path, monkeypatch):
    executable = tmp_path / "hermes.exe"
    executable.touch()
    usage_path = tmp_path / "usage.json"
    monkeypatch.setattr(adapters, "_hermes_path", lambda: str(executable))
    command = adapters._command("hermes", tmp_path, "Work.", "", "off", 1.0, 5, usage_path)
    assert command[command.index("--model") + 1] == "deepseek/deepseek-v4-flash-0731"
    assert command[command.index("--reasoning") + 1] == "none"
    assert command[command.index("--usage-file") + 1] == str(usage_path)


def test_omp_length_stop_fails_even_with_zero_exit_and_terminal_event(tmp_path, monkeypatch, no_diff):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = tmp_path / "omp.exe"
    executable.touch()
    monkeypatch.setattr(adapters, "_omp_path", lambda: str(executable))
    monkeypatch.setattr(adapters, "_openrouter_api_key", lambda: "secret")
    monkeypatch.setattr(adapters, "catalogue", lambda: [_entry("omp")])
    partial = {
        "role": "assistant",
        "provider": "openrouter",
        "model": "deepseek/deepseek-v4-flash-0731",
        "content": [{"type": "text", "text": "Partial"}],
        "usage": {"input": 3, "output": 5, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 8,
                  "cost": {"total": 0.001}},
        "stopReason": "length",
    }
    process = FakeProcess([
        {"type": "message_end", "message": partial},
        {"type": "agent_end", "messages": [partial], "isTerminal": True},
    ])
    monkeypatch.setattr(adapters.subprocess, "Popen", lambda *args, **kwargs: process)
    result = adapters.run_native("omp", workspace, "Finish it.", adapters._OMP_DEFAULT_MODEL, "high", tmp_path / "job", 5)
    assert result["status"] == "failed"
    assert "output limit" in result["error"]
    assert result["exit_code"] == 0
    assert result["usage"]["total_tokens"] == 8


def test_omp_nonterminal_agent_end_does_not_make_clean_eof_success(tmp_path, monkeypatch, no_diff):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = tmp_path / "omp.exe"
    executable.touch()
    monkeypatch.setattr(adapters, "_omp_path", lambda: str(executable))
    monkeypatch.setattr(adapters, "_openrouter_api_key", lambda: "secret")
    monkeypatch.setattr(adapters, "catalogue", lambda: [_entry("omp")])
    process = FakeProcess([{"type": "agent_end", "messages": [], "isTerminal": False}])
    monkeypatch.setattr(adapters.subprocess, "Popen", lambda *args, **kwargs: process)
    result = adapters.run_native("omp", workspace, "Finish it.", adapters._OMP_DEFAULT_MODEL, "high", tmp_path / "job", 5)
    assert result["status"] == "failed"
    assert "without a terminal JSON event" in result["error"]


def test_timeout_terminates_process_tree_and_job_is_terminal(tmp_path, monkeypatch, no_diff):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = tmp_path / "codex.exe"
    executable.touch()
    monkeypatch.setattr(adapters, "find_codex", lambda: str(executable))
    monkeypatch.setattr(adapters, "catalogue", lambda: [_entry("codex")])
    process = FakeProcess([], hold=True)
    # Keep the reader from making EOF look like a provider exit before the timeout.
    blocker = type("Blocked", (), {"readline": lambda self: time.sleep(0.1) or b""})()
    process.stdout = blocker
    monkeypatch.setattr(adapters.subprocess, "Popen", lambda *args, **kwargs: process)
    terminated = []

    def terminate(proc):
        terminated.append(proc.pid)
        proc.terminate()

    monkeypatch.setattr(adapters, "_terminate_tree", terminate)
    result = adapters.run_native("codex", workspace, "Work.", "model", "high", tmp_path / "job", 0.01)
    assert result["status"] == "timeout"
    assert result["timed_out"] is True
    assert terminated == [process.pid]
    meta = json.loads((tmp_path / "job" / "job.json").read_text(encoding="utf-8"))
    assert meta["status"] == "failed"
    assert meta["timed_out"] is True


def test_stop_callback_terminates_and_preserves_stopped_status(tmp_path, monkeypatch, no_diff):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = tmp_path / "codex.exe"
    executable.touch()
    monkeypatch.setattr(adapters, "find_codex", lambda: str(executable))
    monkeypatch.setattr(adapters, "catalogue", lambda: [_entry("codex")])
    process = FakeProcess([], hold=True)
    monkeypatch.setattr(adapters.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(adapters, "_terminate_tree", lambda proc: proc.terminate())
    result = adapters.run_native(
        "codex", workspace, "Work.", "model", "", tmp_path / "job", 5, should_stop=lambda: True,
    )
    assert result["status"] == "stopped"
    assert result["stopped"] is True
    assert process.terminated is True
    meta = json.loads((tmp_path / "job" / "job.json").read_text(encoding="utf-8"))
    assert meta["status"] == "stopped"


def test_git_diff_stats_include_tracked_and_untracked_files(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "new.txt").write_text("one\ntwo", encoding="utf-8")
    replies = iter([
        (0, b"3\t2\ttracked.py\n-\t-\timage.png\n"),
        (0, b"new.txt\0"),
    ])
    monkeypatch.setattr(adapters, "_capture", lambda command, cwd: next(replies))
    assert adapters._git_diff_stats(workspace) == {
        "files": ["tracked.py", "image.png", "new.txt"],
        "files_edited": 3,
        "lines_added": 5,
        "lines_deleted": 2,
    }


def test_unknown_engine_fails_without_starting_a_process(tmp_path, monkeypatch, no_diff):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(adapters, "catalogue", lambda: [_entry("unknown")])
    monkeypatch.setattr(
        adapters.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("unsupported engine must not start a process"),
    )
    result = adapters.run_native("unknown", workspace, "Work.", "model", "", tmp_path / "job", 5)
    assert result["status"] == "failed"
    assert "unknown native benchmark engine" in result["error"]
    assert (tmp_path / "job" / "job.json").is_file()
