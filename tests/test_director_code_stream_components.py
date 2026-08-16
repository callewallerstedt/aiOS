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

    assert 'class="aicss-diff diff"' in script
    assert 'class="diffRow ${row.type}"' in script
    assert '"tool-card aicss-todo todo expandable expanded"' in script
    assert 'class="todoItem${done ? " done" : active ? " active" : ""}"' in script
    assert 'loadingPixelsMarkup("orbit")' in script
    assert 'updateRollingCount(node.querySelector(".todoCount")' in script
    assert ".aicss-diff .diffBody::before" in styles
    assert "grid-template-columns: 32px 32px 18px 1fr" in styles
    assert "background: repeating-linear-gradient(45deg, #dc2626 0, #dc2626 1.5px" in styles
    assert ".aicss-todo .todoCollapsible" in styles
    assert ".aicss-todo .rollInner.on" in styles
    assert "animation: todo-shine 2.25s cubic-bezier(0.25, 0.1, 0.25, 1) infinite" in styles


def test_todo_header_uses_the_requested_white_and_red_three_by_three_grid():
    script = (ROOT / "aios_ui/web/js/transcript.js").read_text(encoding="utf-8")
    styles = (ROOT / "aios_ui/web/css/code-beautiful.css").read_text(encoding="utf-8")

    assert 'const TODO_GRID_ICON = `<span class="todoGridIcon"' in script
    assert '${"<span></span>".repeat(9)}' in script
    assert "grid-template-columns: repeat(3, 3px)" in styles
    assert "border-radius: 1.25px; background: #fff" in styles
    assert "span:nth-child(5), .aicss-todo .todoGridIcon > span:nth-child(7)" in styles
    assert "background: #ef4444" in styles
    assert "todoHeadPie" not in script


def test_director_code_session_tool_is_labeled_and_accented():
    script = (ROOT / "phone_site/director.js").read_text(encoding="utf-8")
    styles = (ROOT / "phone_site/director.css").read_text(encoding="utf-8")

    assert 'textContent = "aiOS CODE session"' in script
    assert '"AI-generated code session"' in script
    assert 'setAttribute("aria-label"' in script
    assert ".tool-card.code-job" in styles
    assert "var(--accent) 58%" in styles
