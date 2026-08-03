import json

import voice_agent


def make_agent(monkeypatch, responses):
    agent = voice_agent.VoiceAgent.__new__(voice_agent.VoiceAgent)
    agent._last_code_job_id = ""
    calls = []

    def fake_request(path, method="GET", payload=None, timeout=35):
        calls.append((path, method, payload, timeout))
        return responses.pop(0)

    monkeypatch.setattr(agent, "_code_request", fake_request)
    return agent, calls


def test_code_start_contract_is_exact_and_remembers_session(monkeypatch):
    response = {
        "ok": True,
        "job": {
            "id": "job-1",
            "title": "Ship the dashboard",
            "provider": "codex",
            "status": "queued",
            "model": "gpt-5.6-sol",
            "reasoning": "high",
            "fast": True,
            "cwd": r"C:\project",
        },
    }
    agent, calls = make_agent(monkeypatch, [response])
    result = json.loads(
        agent._tool_code_start(
            {
                "provider": "CODEX",
                "cwd": r"C:\project",
                "brief": "Implement and test the dashboard.",
                "model": "gpt-5.6-sol",
                "reasoning": "HIGH",
                "fast": True,
                "attachments": [r"C:\project\spec.png", "https://example.com/ticket"],
            }
        )
    )
    assert result["job_id"] == "job-1"
    assert agent._last_code_job_id == "job-1"
    path, method, payload, _timeout = calls[0]
    assert (path, method) == ("/api/code/jobs", "POST")
    assert payload["provider"] == "codex"
    assert payload["model"] == "gpt-5.6-sol"
    assert payload["reasoning"] == "high"
    assert payload["fast"] is True
    assert payload["attachments"] == [
        {"path": r"C:\project\spec.png", "label": "spec.png"},
        {"url": "https://example.com/ticket", "label": "https://example.com/ticket"},
    ]


def test_code_followup_uses_last_native_job_and_preserves_urgent(monkeypatch):
    agent, calls = make_agent(
        monkeypatch,
        [{"ok": True, "queued": False, "steered": True, "job": {"status": "running"}}],
    )
    agent._last_code_job_id = "job-2"
    result = json.loads(agent._tool_code_continue({"text": "Change direction now", "urgent": True}))
    assert result == {
        "ok": True,
        "job_id": "job-2",
        "steered": True,
        "queued": False,
        "status": "running",
    }
    path, method, payload, _timeout = calls[0]
    assert (path, method) == ("/api/code/jobs/job-2/messages", "POST")
    assert payload == {"text": "Change direction now", "urgent": True, "attachments": []}


def test_voice_exposes_complete_code_tool_set():
    tools = {tool["name"]: tool for tool in voice_agent.VoiceAgent._code_tools()}
    assert set(tools) == {
        "code_capabilities",
        "code_setup",
        "code_start",
        "code_list",
        "code_status",
        "code_continue",
        "code_handoff",
        "code_stop",
        "code_delete",
    }
    required = tools["code_start"]["parameters"]["required"]
    assert required == ["provider", "cwd", "brief", "model", "reasoning", "fast"]
    assert tools["code_handoff"]["parameters"]["required"] == ["provider", "model", "reasoning", "fast"]


def test_voice_handoff_continues_logical_job_with_new_native_provider(monkeypatch):
    agent, calls = make_agent(
        monkeypatch,
        [{
            "ok": True,
            "handoff": {
                "from_provider": "claude",
                "from_model": "sonnet",
                "to_provider": "codex",
                "to_model": "gpt-5.6-sol",
                "native_continuation": False,
            },
            "job": {"id": "job-voice", "status": "queued"},
        }],
    )
    agent._last_code_job_id = "job-voice"

    result = json.loads(agent._tool_code_handoff({
        "provider": "CODEX",
        "model": "gpt-5.6-sol",
        "reasoning": "HIGH",
        "fast": True,
        "instruction": "Continue naturally and finish the tests.",
    }))

    assert result == {
        "ok": True,
        "job_id": "job-voice",
        "from_provider": "claude",
        "from_model": "sonnet",
        "provider": "codex",
        "model": "gpt-5.6-sol",
        "native_continuation": False,
        "status": "queued",
        "message": "The logical CODE session was handed off. The target provider is continuing in a new native session with the aiOS context manifest.",
    }
    path, method, payload, timeout = calls[0]
    assert (path, method, timeout) == ("/api/code/jobs/job-voice/handoff", "POST", 90)
    assert payload == {
        "provider": "codex",
        "model": "gpt-5.6-sol",
        "reasoning": "high",
        "fast": True,
        "instruction": "Continue naturally and finish the tests.",
    }


def test_voice_delete_requires_explicit_confirmation(monkeypatch):
    agent, calls = make_agent(monkeypatch, [{"ok": True, "recoverable": True}])
    agent._last_code_job_id = "job-safe"

    rejected = json.loads(agent._tool_code_delete({}))
    assert rejected["needs_confirmation"] is True
    assert calls == []

    accepted = json.loads(agent._tool_code_delete({"confirm": True}))
    assert accepted["ok"] is True
    assert calls == [
        ("/api/code/jobs/job-safe", "DELETE", {"confirm": "job-safe"}, 35)
    ]
