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


def test_code_followup_uses_last_native_job_and_maps_semantic_delivery(monkeypatch):
    agent, calls = make_agent(
        monkeypatch,
        [{"ok": True, "queued": False, "steered": True, "job": {"status": "running"}}],
    )
    agent._last_code_job_id = "job-2"
    result = json.loads(agent._tool_code_continue({"text": "Change direction now", "delivery": "steer_now"}))
    assert result == {
        "ok": True,
        "job_id": "job-2",
        "session": {"id": "job-2"},
        "delivery": "steer_now",
        "answered": False,
        "steered": True,
        "queued": False,
        "queue_position": 0,
        "status": "running",
        "accepted_as": "steered_now",
        "message": "Instruction steered into the active turn at its next safe model boundary.",
    }
    path, method, payload, _timeout = calls[0]
    assert (path, method) == ("/api/code/jobs/job-2/messages", "POST")
    assert payload == {"text": "Change direction now", "urgent": True, "attachments": []}


def test_queue_next_is_not_sent_as_urgent(monkeypatch):
    agent, calls = make_agent(
        monkeypatch,
        [{"ok": True, "queued": True, "job": {"status": "queued"}}],
    )
    agent._last_code_job_id = "job-queue"
    result = json.loads(agent._tool_code_continue({"text": "Then update the docs", "delivery": "queue_next"}))
    assert result["delivery"] == "queue_next"
    assert result["accepted_as"] == "next_turn"
    assert result["queue_position"] == 1
    assert result["message"] == "Follow-up queued #1 for the next CODE turn."
    assert calls[0][2]["urgent"] is False


def test_voice_exposes_complete_code_tool_set():
    tools = {tool["name"]: tool for tool in voice_agent.VoiceAgent._code_tools()}
    assert set(tools) == {
        "code_capabilities",
        "code_setup",
        "code_projects",
        "code_configs",
        "code_start",
        "code_list",
        "code_status",
        "code_continue",
        "code_handoff",
        "code_stop",
        "code_delete",
    }
    required = tools["code_start"]["parameters"]["required"]
    assert required == ["cwd", "brief"]
    assert tools["code_continue"]["parameters"]["required"] == ["text", "delivery"]
    assert "urgent" not in tools["code_continue"]["parameters"]["properties"]
    assert tools["code_continue"]["parameters"]["properties"]["delivery"]["enum"] == ["steer_now", "queue_next"]
    for name in ("code_status", "code_continue", "code_handoff", "code_stop", "code_delete"):
        assert {"job_id", "title", "project", "latest"} <= set(tools[name]["parameters"]["properties"])
    assert tools["code_handoff"]["parameters"]["required"] == ["provider", "model", "reasoning", "fast"]


def test_project_and_config_menus_are_read_from_local_aios_catalogues(monkeypatch):
    import code_jobs
    import code_roles
    import helper_overlay

    monkeypatch.setattr(code_jobs, "list_projects", lambda: [{"id": "p1", "name": "aiOS"}])
    monkeypatch.setattr(helper_overlay, "load_config", lambda: {"model_configs": []})
    monkeypatch.setattr(code_roles, "load_model_configs", lambda config: [{"id": "c1", "name": "Fast"}])

    assert voice_agent.VoiceAgent._code_request("/api/code/projects") == {
        "ok": True,
        "projects": [{"id": "p1", "name": "aiOS"}],
    }
    assert voice_agent.VoiceAgent._code_request("/api/code/model-configs") == {
        "ok": True,
        "configs": [{"id": "c1", "name": "Fast"}],
    }


def test_code_start_expands_one_saved_configuration(monkeypatch):
    config = {
        "id": "balanced",
        "name": "Balanced engineering",
        "provider": "openrouter",
        "strategy": "auto",
        "review_fix": False,
        "roles": {
            "scout": {"enabled": True, "model": "qwen/scout", "reasoning": "off", "fast": True},
            "planner": {"enabled": False, "model": "smart/plan", "reasoning": "high", "fast": False},
            "coder": {"enabled": True, "model": "deepseek/coder", "reasoning": "low", "fast": True},
            "reviewer": {"enabled": True, "model": "smart/review", "reasoning": "medium", "fast": False},
        },
    }
    response = {
        "ok": True,
        "job": {
            "id": "job-config",
            "title": "Tiny CSS edit",
            "provider": "openrouter",
            "status": "queued",
            "model": "deepseek/coder",
            "reasoning": "low",
            "fast": True,
            "cwd": r"C:\site",
            "config_id": "balanced",
            "config_name": "Balanced engineering",
        },
    }
    agent, calls = make_agent(monkeypatch, [{"ok": True, "configs": [config]}, response])

    result = json.loads(agent._tool_code_start({
        "config_id": "balanced",
        "cwd": r"C:\site",
        "brief": "Make the header 4px shorter.",
    }))

    assert result["job_id"] == "job-config"
    assert result["config_id"] == "balanced"
    assert calls[0][0] == "/api/code/model-configs"
    path, method, payload, timeout = calls[1]
    assert (path, method, timeout) == ("/api/code/jobs", "POST", 45)
    assert payload == {
        "cwd": r"C:\site",
        "brief": "Make the header 4px shorter.",
        "attachments": [],
        "provider": "openrouter",
        "model": "deepseek/coder",
        "reasoning": "low",
        "fast": True,
        "review_fix": False,
        "roles": config["roles"],
        "config_id": "balanced",
        "config_name": "Balanced engineering",
        "strategy": "auto",
    }


def test_config_start_reuses_the_menu_already_read_this_turn(monkeypatch):
    config = {
        "id": "fast",
        "name": "Fast",
        "provider": "openrouter",
        "strategy": "direct",
        "roles": {"coder": {"enabled": True, "model": "deepseek/coder", "reasoning": "off", "fast": True}},
    }
    agent, calls = make_agent(monkeypatch, [
        {"ok": True, "configs": [config]},
        {"ok": True, "job": {"id": "new", "status": "queued"}},
    ])
    json.loads(agent._tool_code_configs({}))
    result = json.loads(agent._tool_code_start({
        "config_id": "fast",
        "cwd": r"C:\site",
        "brief": "Fix the typo.",
    }))
    assert result["ok"] is True
    assert [call[0] for call in calls] == ["/api/code/model-configs", "/api/code/jobs"]


def test_code_start_rejects_implicit_model_choices(monkeypatch):
    agent, calls = make_agent(monkeypatch, [])
    result = json.loads(agent._tool_code_start({
        "provider": "openrouter",
        "cwd": r"C:\site",
        "brief": "Change one color.",
    }))
    assert result["ok"] is False
    assert set(result["needs"]) == {"model", "reasoning", "fast"}
    assert calls == []


def test_projects_and_configs_are_searchable_and_bounded(monkeypatch):
    projects = {
        "ok": True,
        "projects": [
            {"id": "1", "name": "aiOS", "path": r"C:\aiOS", "exists": True},
            {"id": "2", "name": "Old site", "path": r"D:\old", "exists": False},
        ],
    }
    configs = {
        "ok": True,
        "configs": [{
            "id": "fast",
            "name": "Fast edits",
            "description": "Tiny precise edits",
            "provider": "openrouter",
            "strategy": "direct",
            "review_fix": False,
            "show_in_composer": True,
            "roles": {
                "coder": {"enabled": True, "model": "deepseek/coder", "reasoning": "off", "fast": True},
                "planner": {"enabled": False, "model": "smart", "reasoning": "high", "fast": False},
            },
        }],
    }
    agent, _calls = make_agent(monkeypatch, [projects, configs])
    project_result = json.loads(agent._tool_code_projects({"query": "aios", "exists": True, "limit": 2}))
    config_result = json.loads(agent._tool_code_configs({"query": "fast", "limit": 2}))
    assert [row["path"] for row in project_result["projects"]] == [r"C:\aiOS"]
    assert config_result["configs"][0]["id"] == "fast"
    assert set(config_result["configs"][0]["roles"]) == {"coder"}


def test_spoken_project_resolves_to_newest_matching_session(monkeypatch):
    listing = {
        "ok": True,
        "jobs": [
            {"id": "new", "title": "Restore Audio On Quit", "project_name": "assettocorsa", "cwd": r"C:\games\assettocorsa", "provider": "openrouter", "status": "running"},
            {"id": "old", "title": "Old track fix", "project_name": "assettocorsa", "cwd": r"C:\games\assettocorsa", "provider": "codex", "status": "completed"},
        ],
    }
    continued = {"ok": True, "answered": True, "job": {"status": "running"}}
    agent, calls = make_agent(monkeypatch, [listing, continued])
    result = json.loads(agent._tool_code_continue({
        "project": "assetto",
        "text": "And switch it to headphones right now.",
        "delivery": "steer_now",
    }))
    assert result["job_id"] == "new"
    assert result["session"]["title"] == "Restore Audio On Quit"
    assert result["accepted_as"] == "answer"
    assert calls[0][0] == "/api/code/jobs?limit=100"
    assert calls[1][0] == "/api/code/jobs/new/messages"


def test_session_resolution_reuses_a_list_already_read_this_turn(monkeypatch):
    listing = {
        "ok": True,
        "jobs": [{
            "id": "job-audio",
            "title": "Restore Audio On Quit",
            "project_name": "assettocorsa",
            "cwd": r"C:\games\assettocorsa",
            "provider": "openrouter",
            "status": "completed",
        }],
    }
    status = {"ok": True, "job": listing["jobs"][0], "events": []}
    agent, calls = make_agent(monkeypatch, [listing, status])
    json.loads(agent._tool_code_list({"query": "audio"}))
    result = json.loads(agent._tool_code_status({"title": "restore audio"}))
    assert result["ok"] is True
    assert [call[0] for call in calls] == [
        "/api/code/jobs?limit=100",
        "/api/code/jobs/job-audio/log?since=0",
    ]


def test_code_status_does_not_return_the_unbounded_job_ledger(monkeypatch):
    agent, _calls = make_agent(monkeypatch, [{
        "ok": True,
        "job": {
            "id": "large",
            "title": "Large session",
            "provider": "openrouter",
            "status": "completed",
            "last_summary": "s" * 4000,
            "task_strategy": {"name": "planned", "reasons": ["scope was broad"]},
            "verification": {
                "state": "verified",
                "generation": 9,
                "blocked": False,
                "reason": "focused tests passed",
                "source_paths": ["src/app.py"],
                "evidence": [{"output": "x" * 30000}],
            },
        },
        "events": [{"kind": "assistant", "text": "done"}],
    }])
    raw = agent._tool_code_status({"job_id": "large"})
    result = json.loads(raw)
    assert len(raw) < 5000
    assert len(result["job"]["last_summary"]) <= 500
    assert result["job"]["verification"] == {
        "state": "verified",
        "generation": 9,
        "blocked": False,
        "reason": "focused tests passed",
        "source_paths": ["src/app.py"],
    }
    assert "evidence" not in result["job"]["verification"]


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
