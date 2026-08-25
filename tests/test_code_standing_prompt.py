from pathlib import Path
from types import SimpleNamespace

from aios_ui import server


ROOT = Path(__file__).resolve().parent.parent
CODE_JS = (ROOT / "aios_ui" / "web" / "js" / "code.js").read_text(encoding="utf-8")
SPLIT_JS = (ROOT / "aios_ui" / "web" / "js" / "code_split.js").read_text(encoding="utf-8")
CODE_CSS = (ROOT / "aios_ui" / "web" / "css" / "code.css").read_text(encoding="utf-8")


def test_standing_prompt_is_appended_once_with_a_visible_label():
    result = server._append_code_standing_prompt("Build it.  ", "  Keep the UI subtle.  ")

    assert result == (
        "Build it.\n\n---\n"
        "Standing instruction (automatically appended by aiOS):\n"
        "Keep the UI subtle."
    )
    assert server._append_code_standing_prompt("Build it.", "  ") == "Build it."


def test_standing_prompt_is_bounded_before_reaching_a_model():
    result = server._append_code_standing_prompt("Task", "x" * 5000)

    assert result.endswith("x" * server.CODE_STANDING_PROMPT_LIMIT)
    assert len(result.rsplit("\n", 1)[-1]) == server.CODE_STANDING_PROMPT_LIMIT


def test_ui_server_applies_the_same_prompt_to_new_and_existing_sessions(monkeypatch):
    calls = []

    def create_job(*args, **kwargs):
        calls.append(("create", args, kwargs))
        return {"ok": True, "job": {"id": "job-1"}}

    def send_message(*args, **kwargs):
        calls.append(("message", args, kwargs))
        return {"ok": True}

    monkeypatch.setattr(
        server.BRIDGE,
        "_code_jobs",
        SimpleNamespace(create_job=create_job, send_message=send_message),
    )

    server.dispatch(
        "/api/code/jobs",
        "POST",
        {},
        {
            "provider": "openrouter",
            "cwd": "C:/repo",
            "brief": "First task",
            "standing_prompt": "Use short names.",
        },
    )
    server.dispatch(
        "/api/code/jobs/job-1/messages",
        "POST",
        {},
        {"text": "Continue", "standing_prompt": "Use short names."},
    )

    assert calls[0][1][2].startswith("First task\n\n---\nStanding instruction")
    assert calls[0][1][2].endswith("Use short names.")
    assert calls[1][1][1].startswith("Continue\n\n---\nStanding instruction")
    assert calls[1][1][1].endswith("Use short names.")


def test_composer_is_subtle_durable_and_covers_split_messages():
    assert 'data-code="standing-toggle"' in CODE_JS
    assert 'data-code="standing-prompt"' in CODE_JS
    assert "Saved automatically" in CODE_JS
    assert "code_standing_prompt" in CODE_JS
    assert 'api("/api/config", {' in CODE_JS
    assert "standing_prompt: this.standingPrompt" in CODE_JS
    assert "standing_prompt: this.code.standingPrompt" in SPLIT_JS
    assert ".standing-prompt-toggle" in CODE_CSS
    assert '.standing-prompt-panel[hidden] { display: none; }' in CODE_CSS
