from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_desktop_and_director_use_the_same_compact_stream_renderer():
    desktop = (ROOT / "aios_ui/web/js/transcript.js").read_text(encoding="utf-8")
    phone = (ROOT / "phone_site/code/transcript.js").read_text(encoding="utf-8")

    assert desktop == phone
    assert 'data-transcript-toggle="thinking"' in desktop
    assert 'data-transcript-toggle="reasoning"' in desktop
    assert 'this.transcript.classList.toggle("hide-reasoning"' in desktop
    assert 'const run = this.ensureToolRun();' in desktop
    assert 'run.reasoningParts.set(key, this.thinkingText)' in desktop
    assert 'this.addRow("thinking expanded"' not in desktop


def test_stream_uses_file_diff_and_todo_components_with_active_pixel_state():
    script = (ROOT / "aios_ui/web/js/transcript.js").read_text(encoding="utf-8")
    styles = (ROOT / "aios_ui/web/css/code-beautiful.css").read_text(encoding="utf-8")

    assert 'class="file-diff-row ${row.type}"' in script
    assert 'class="step todo-item ${escapeHtml(status)}"' in script
    assert 'loadingPixelsMarkup("bloom")' in script
    assert "updateRollingCount(agentStatus" in script
    assert ".file-diff-body::before" in styles
    assert "grid-template-columns: 32px 32px 18px minmax(360px, 1fr)" in styles
    assert ".tool-card.task-row.is-plan.expanded .steps" in styles
    assert ".roll-inner.on" in styles


def test_director_code_session_tool_is_labeled_and_accented():
    script = (ROOT / "phone_site/director.js").read_text(encoding="utf-8")
    styles = (ROOT / "phone_site/director.css").read_text(encoding="utf-8")

    assert 'textContent = "aiOS CODE session"' in script
    assert '"AI-generated code session"' in script
    assert 'setAttribute("aria-label"' in script
    assert ".tool-card.code-job" in styles
    assert "var(--accent) 58%" in styles
