import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_reasoning_transport_newlines_are_rendered_as_words_not_rows():
    transcript = ROOT / "aios_ui/web/js/transcript.js"
    script = r"""
import { pathToFileURL } from 'node:url';
const { normalizeReasoningText } = await import(pathToFileURL(process.argv[1]).href);
const tokenized = 'All\n six\n sites\n are\n null\n-safe.';
const normalized = normalizeReasoningText(tokenized);
if (normalized !== 'All six sites are null-safe.') throw new Error(JSON.stringify(normalized));
const markdown = 'Paragraph one.\n\n- first item\n- second item';
if (normalizeReasoningText(markdown) !== markdown) throw new Error('markdown paragraphs were flattened');
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(transcript)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_desktop_and_director_use_the_same_compact_stream_renderer():
    desktop = (ROOT / "aios_ui/web/js/transcript.js").read_text(encoding="utf-8")
    phone = (ROOT / "phone_site/code/transcript.js").read_text(encoding="utf-8")

    assert desktop == phone
    assert 'data-transcript-toggle="thinking"' not in desktop
    assert 'data-transcript-toggle="reasoning"' not in desktop
    assert 'hide-live-thinking' not in desktop
    assert 'hide-reasoning' not in desktop
    assert 'previousEpisode?.run || this.ensureToolRun()' in desktop
    assert 'run.reasoningParts.set(key, this.thinkingText)' in desktop
    assert 'this.thinkingText = String(run.reasoningParts.get(key) || "")' in desktop
    assert 'activity.summary && !this.thinkingText' in desktop
    assert "export function normalizeReasoningText" in desktop
    assert "const transportSpace" in desktop
    assert 'this.thinkingText += String(raw.delta)' in desktop
    assert 'normalizeReasoningText(this.thinkingText).trim()' in desktop
    assert 'const previousEpisode = this.thinkingEpisodes.get(key)' in desktop
    assert 'this.addRow("thinking expanded"' not in desktop


def test_stream_uses_file_diff_and_todo_components_with_active_pixel_state():
    script = (ROOT / "aios_ui/web/js/transcript.js").read_text(encoding="utf-8")
    styles = (ROOT / "aios_ui/web/css/code-beautiful.css").read_text(encoding="utf-8")

    assert 'class="${className}" data-reveal=' in script
    assert 'fileDiffMarkup(change, "turn-diff")' in script
    assert 'class="diffRow ${row.type}"' not in script
    assert '"tool-card aicss-todo todo expandable expanded"' in script
    assert 'class="todoItem${done ? " done" : active ? " active" : ""}"' in script
    assert 'loadingPixelsMarkup("orbit")' in script
    assert 'updateRollingCount(node.querySelector(".todoCount")' in script
    assert ".turn-diff .add" in styles
    assert ".turn-diff .del" in styles
    assert ".aicss-diff .diffBody::before" not in styles
    assert ".aicss-todo .todoCollapsible" in styles
    assert ".aicss-todo .rollInner.on" in styles
    assert "animation: todo-shine 2.25s cubic-bezier(0.25, 0.1, 0.25, 1) infinite" in styles


def test_director_code_session_tool_is_labeled_and_accented():
    script = (ROOT / "phone_site/director.js").read_text(encoding="utf-8")
    styles = (ROOT / "phone_site/director.css").read_text(encoding="utf-8")

    assert 'chip.querySelector(".glyph").innerHTML = codeLogo()' in script
    assert 'textContent = preview || "CODE session"' in script
    assert 'setAttribute("aria-label"' in script
    assert "#screen-chat .tool-card.code-job" in styles
    assert "border: 1px solid var(--accent)" in styles
    assert "grid-template-columns: 22px minmax(0, 1fr) 12px" in styles
    assert "@keyframes code-logo-runner" in styles
    assert ".code-logo-grid span:nth-child(5)" in styles
    assert ".code-logo-grid span:nth-child(7)" in styles
