"""The CODE transcript's scrollbar has to tell the truth.

Two separate defects made dragging it unusable, and both are easy to
reintroduce because each looked like an optimisation:

  * `content-visibility: auto` on transcript rows let the engine skip layout
    for anything off screen, so scrollHeight was the sum of 40px estimates
    rather than real heights -- measured on a real session, 5,812px against an
    actual 36,156px. The track stretched under the thumb as you dragged.
  * The auto-follow loop wrote scrollTop on every flush. A scrollbar drag is
    handled on the compositor and its scroll event arrives late, so the loop
    put the thumb back before it ever learned the user had moved it.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSS = "\n".join(
    (ROOT / "aios_ui" / "web" / "css" / name).read_text(encoding="utf-8")
    for name in ("code.css", "code-beautiful.css")
)
# The transcript engine moved into transcript.js when BENCH needed to render a
# benchmark session with it. Both halves are read here: these rules are about
# the transcript wherever its code lives.
JS = "\n".join(
    (ROOT / "aios_ui" / "web" / "js" / name).read_text(encoding="utf-8")
    for name in ("code.js", "transcript.js", "chat_components.js")
)

# Comments are stripped so the note explaining *why* the property is gone does
# not read as the property being present.
RULES = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)


def test_rows_are_really_laid_out_so_scrollheight_is_honest():
    assert "content-visibility" not in RULES
    assert "contain-intrinsic-size" not in RULES
    assert ".code-transcript > * { flex-shrink: 0; }" in RULES


def test_the_transcript_contains_its_own_overscroll():
    block = RULES.split(".code-transcript {")[1].split("}")[0]
    assert "overscroll-behavior: contain" in block


def test_nothing_writes_scrolltop_while_the_user_holds_the_scrollbar():
    assert "scrollHeld" in JS
    assert "pointerdown" in JS
    assert "scrollHoldUntil" in JS
    # The guard has to be inside scrollToEnd, not only at the call sites.
    body = JS.split("scrollToEnd(force = false) {")[1].split("\n  }")[0]
    assert "this.scrollHeld" in body
    assert "scrollHoldUntil" in body


def test_following_does_not_rewrite_the_same_offset_forever():
    """On a finished session nothing arrives, so nothing should be written."""
    body = JS.split("scrollToEnd(force = false) {")[1].split("\n  }")[0]
    assert "wroteHeight" in body


def test_our_own_scroll_writes_are_not_mistaken_for_the_user():
    handler = JS.split('on(this.transcript, "scroll"')[1].split("});")[0]
    assert "this.wroteTop" in handler


def test_opening_a_session_lands_on_the_latest_message():
    assert "pinToEnd" in JS
    # Selecting a session has to ask for it; the method existing is not enough.
    selecting = JS.split("select(jobId) {")[1].split("\n  }")[0]
    assert "pinToEnd()" in selecting


def test_the_jump_button_floats_instead_of_scrolling_away():
    """An absolutely positioned child of a scroller scrolls with its content."""
    assert "code-transcript-wrap" in JS
    assert "code-transcript-wrap" in RULES
    wrap = RULES.split(".code-transcript-wrap {")[1].split("}")[0]
    assert "position: relative" in wrap
    # The scroller itself must not be the containing block any more.
    assert ".code-transcript { position: relative; }" not in RULES


def test_agent_roles_render_as_distinct_pipeline_separators():
    assert 'activity_type || "") === "stage"' in JS
    assert "upsertStage(event)" in JS
    assert ".pipeline-stage {" in RULES
    for role in ("scout", "planner", "coder", "reviewer"):
        assert role in JS


def test_pipeline_separators_show_exact_live_usage_cost_and_time():
    assert 'class="stage-metrics"' in JS
    assert 'usage.total_tokens' in JS
    assert 'usage.cost_usd' in JS
    assert 'toLocaleString()' in JS
    assert 'formatDuration(state.seconds' in JS
    assert ".pipeline-stage .stage-metrics" in RULES


def test_subagents_stay_compact_and_do_not_render_tool_receipts_as_blank_plan_rows():
    assert "autoExpanded" not in JS
    assert 'const steps = type === "plan" && Array.isArray(state.steps) ? state.steps : [];' in JS


def test_consultant_handoffs_keep_their_real_question_activity_and_advice():
    assert 'rawType === "planner" ? "consultant" : rawType' in JS
    for label in ('addSection("QUESTION"', 'addSection("ACTIVITY"', 'addSection("ADVICE"'):
        assert label in JS
    assert ".tool-card.is-consultant" in RULES


def test_reference_task_rows_tool_chips_and_thinking_disclosures_are_reused():
    for hook in (
        'const CHEVRON_ICON', 'taskBadgeMarkup', 'loadingPixelsMarkup',
        'class="stage-head"', 'stage-pill task-pill', 'class="agent-status"',
        'class="card-chevron"', 'class="thinking-head"', 'class="thinking-track"',
    ):
        assert hook in JS
    assert "tool-chip-row" in JS
    assert "height: 44px" in RULES
    assert "border-radius: 22px" in RULES
    assert "min-height: 28px" in RULES
    assert "min-height: 22px" in RULES
    assert ".task-ring.active svg" in RULES
    assert ".loading-pixels" in RULES
    assert "@keyframes pixel-on" in RULES
    assert "grid-template-rows: 0fr" in RULES
    assert ".thinking.live .thinking-label" in RULES


def test_tool_calls_are_grouped_with_named_per_file_diff_chips():
    assert 'class="tool-run-head"' in JS
    assert 'class="tool-run-rows"' in JS
    assert 'class="tool-run-diffs"' in JS
    assert 'files ? `${files} file${files === 1 ? "" : "s"} changed`' in JS
    assert 'name: path.split(/[\\\\/]/).pop() || path' in JS
    assert 'line.startsWith("+") && !line.startsWith("+++")' in JS
    assert ".tool-run.expanded .tool-run-expand" in RULES
    assert ".tool-run-diff .add" in RULES
    assert ".tool-run-diff .del" in RULES


def test_changed_files_are_painted_live_at_the_bottom_then_finalized_in_place():
    assert "this.turnFileDiffs = new Map()" in JS
    assert "this.turnDiffParts = new Map()" in JS
    assert "recordTurnDiff(change, key)" in JS
    assert "this.paintTurnDiffs();" in JS
    assert 'this.turnDiffEl = this.addRow("turn-diffs", "")' in JS
    assert "finishTurnDiffs()" in JS
    assert 'fileDiffMarkup(change, "turn-diff")' in JS
    assert 'class="turn-diffs-label">Files changed' in JS
    assert ".turn-diffs" in RULES


def test_coder_stage_updates_a_live_usage_footer_then_finalizes_in_place():
    assert "this.turnUsage = new Map()" in JS
    assert "this.turnUsageEl = null" in JS
    assert "paintTurnUsage()" in JS
    assert "finishTurnUsage()" in JS
    assert 'if (stage === "coder")' in JS
    assert 'this.turnUsageEl = this.addRow("turn-usage live", "")' in JS
    assert 'row.node.hidden = stage === "coder"' in JS
    for field in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "total_tokens"):
        assert field in JS
    assert ".turn-usage-detail" in RULES
    assert "overflow-x: auto" in RULES
    assert "scrollbar-width: none" in RULES
    assert 'event.target.closest(".turn-usage-row")' in JS


def test_raw_mode_replays_stored_events_and_keeps_provider_transport_out_of_formatted_chat():
    assert "this.eventHistory = []" in JS
    assert "setRawMode(enabled)" in JS
    assert "for (const event of history) this.applyEvent(event)" in JS
    assert 'if (kind === "raw_model_delta")' in JS
    assert 'if (kind === "raw_model_tool")' in JS
    assert "applyRawEvent(event)" in JS
    assert 'body.textContent = block.text' in JS
    assert ".code-transcript.raw-mode" in RULES
    assert ".raw-block-body" in RULES


def test_provider_content_is_visible_as_provisional_reasoning_in_normal_mode():
    assert 'if (String(event.raw_stream || "content") === "content") this.streamProviderContent(event);' in JS
    assert 'this.settleProviderContent(String(event.request_id || event.request_sequence || ""));' in JS
    assert "streamProviderContent(event = {})" in JS
    assert 'title: "Reasoning"' in JS
    assert "discardMatchingProviderContent" in JS
    # The final assistant answer replaces its provisional copy rather than
    # leaving the same prose visible twice.
    assistant_branch = JS.split('if (kind === "assistant" || kind === "assistant_delta") {', 1)[1].split("return;", 1)[0]
    assert "discardMatchingProviderContent" in assistant_branch


def test_scout_cards_render_structured_tool_receipts_and_scroll_inside():
    assert 'class="agent-tools"' in JS
    assert 'class="agent-tool-call' in JS
    assert 'Array.isArray(state.steps) && state.steps.length' in JS
    assert 'event.target.closest(".agent-tool-call")' in JS
    assert ".tool-card.task-row.expanded .expand-inner" in RULES
    assert "overflow-y: auto" in RULES
    assert ".tool-card.task-row:not(.expanded)" in RULES
    assert "height: 44px" in RULES
    assert "touch-action: pan-y" in RULES
    assert 'const autoOpen = (type === "subagent" || type === "consultant") && busy;' in JS
    assert 'else if ((type === "subagent" || type === "consultant") && !busy) node.classList.remove("expanded");' in JS
    assert 'const glyphMode = `${roleType || type}:${busy ? "busy" : state.phase}`;' in JS
    assert 'Do not recreate animated glyph markup' in JS
    assert 'this.setFollow(false, true);' in JS
    # Completed non-Coder lifecycle cards remain visible and expandable.
    assert 'if (phase === "completed")' not in JS


def test_strategy_routing_status_is_not_rendered_as_conversation_text():
    assert "const orchestrationOnly = !!event.strategy || !!event.strategy_override" in JS
    assert "!orchestrationOnly" in JS


def test_active_turn_always_ends_with_a_churning_sentinel():
    assert 'className = "working-sentinel live row-new"' in JS
    assert '<span class="working-label">Churning</span>' in JS
    assert 'this.transcript.appendChild(this.workingEl)' in JS
    assert "scheduleWorkingClock()" in JS
    assert "1000 - (elapsedMs % 1000)" in JS
    assert "this.currentTurnStartedAt" in JS
    assert "this.workingStartedAt = this.currentTurnStartedAt" in JS
    assert 'status === "running" || status === "queued"' in JS
    assert ".working-sentinel" in RULES
    assert "contain: layout paint" in RULES
    assert "working-label" in RULES

    # The dots, the label and the clock are one wave: a single period drives
    # all three and each starts a fixed fraction of it later, so the highlight
    # crosses the row instead of three shimmers beating against each other.
    assert ".working-sentinel { --wave: 2.4s; }" in RULES
    assert "animation: pixel-sweep var(--wave, 1.5s) ease-in-out infinite" in RULES
    assert "animation: bui-shimmer-text var(--wave, 1.4s) linear infinite" in RULES
    assert "animation-delay: calc(var(--wave, 1.4s) * -0.80)" in RULES
    assert "animation-delay: calc(var(--wave, 1.4s) * -0.62)" in RULES


def test_active_local_generation_explains_buffering_in_normal_and_raw_views():
    assert 'state.label || "Generating"' in JS
    assert 'querySelector(".working-label").textContent' in JS
    assert "Native tool arguments arrive as one buffered block" in JS
    detail_rules = re.findall(r"\.working-detail\s*\{([^}]*)\}", RULES)
    assert detail_rules
    assert "display: inline-block" in detail_rules[-1]
    assert all("display: none" not in rule for rule in detail_rules)


def test_edited_file_pills_survive_a_collapsed_tool_run():
    """A turn's edits were only visible while the dropdown happened to be open.

    The pills now sit outside the collapsible body so they appear as soon as a
    file is edited and stay; the verbose diff bodies stay behind the expand.
    """
    head = JS.index('<div class="tool-run-expand">')
    clip_end = JS.index("</div>", JS.index('<div class="tool-run-rows">', head))
    pills = JS.index('<div class="tool-run-diffs"', head)
    expand_end = JS.index('`);', head)
    assert clip_end < pills < expand_end
    # ...and no longer nested inside the clipped region.
    assert '<div class="tool-run-rows"></div>\n            <div class="tool-run-diffs"' not in JS
    assert ".tool-run:not(.expanded) .tool-run-diff-body { display: none; }" in RULES


def test_pixel_glyph_always_fills_exactly_the_nine_grid_cells():
    """The delay list had grown a tenth entry and rendered a stray dot."""
    assert '"<span></span>".repeat(9)' in JS
    assert "grid-template-columns: repeat(3, 4px)" in RULES
    assert 'export const PIXEL_ANIMATIONS = ["wave", "pulse", "orbit", "rain", "bloom"]' in JS
    for variant in ("wave", "pulse", "orbit", "rain", "bloom"):
        assert f".loading-pixels.anim-{variant} span" in RULES
    # `pulse` keeps the plain breathe; the rest share the sweep keyframe.
    assert ".loading-pixels.anim-pulse span { animation-name: pixel-on; }" in RULES


def test_file_diff_counts_raw_adds_deletes_and_deduplicates_activity_updates():
    assert "export function fileDiffSummary(change)" in JS
    assert "export function fileDiffsFromUnifiedDiff(value)" in JS
    assert 'kind.includes("add")' in JS
    assert 'kind.includes("delete") || kind.includes("remove")' in JS
    assert '`${String(source || "change")}\\u0000${path}`' in JS
    assert 'String(state.activity_type || "") === "diff"' in JS


def test_internal_verification_events_never_replace_the_conversation():
    assert "event.verification_state" in JS
    assert 'event.harness_action || ""' in JS
    assert "recovered_from_completion_gate" in JS
    assert "this.suppressHarnessOutput" in JS
    assert 'kind === "assistant_delta" || kind === "result"' in JS
    assert "no longer vetoes or replaces the Coder's final answer" in JS


def test_exact_prompt_bar_replaces_the_old_stacked_composer():
    for hook in (
        'class="prompt-shell"', 'class="prompt-controls"',
        'data-code="prompt-plus"', 'data-code="prompt-config"',
        'data-code="dictate"', 'data-code="send"',
        'data-code="config-menu-list"', '>Settings</strong>',
    ):
        assert hook in JS
    assert "grid-template-columns: 28px minmax(0, 1fr) auto 28px 28px" in RULES
    assert "border-radius: 14px" in RULES
    assert "Follow-up turn started in this session" not in JS
    assert 'data-code="config-pills"' not in JS
    assert 'class="composer-meta"' not in JS


def test_beautiful_ui_source_port_is_the_final_code_style_layer():
    index = (ROOT / "aios_ui" / "web" / "index.html").read_text(encoding="utf-8")
    assert index.index('href="css/code.css"') < index.index('href="css/code-beautiful.css"')
    for token in (
        "--bui-canvas: var(--code-chat-background, #1c1d1f)",
        "--bui-surface: #232427",
        "--bui-field: #2b2c2f",
        "--bui-line: #2e3033",
        "--bui-ink: #f2f3f4",
        "--bui-shadow-card: 0 0 0 1px",
        "max-width: 920px",
        "cubic-bezier(.23, 1, .32, 1)",
    ):
        assert token in CSS
    assert "--bui-accent: var(--accent)" in CSS
    assert ".code-detail > .code-telemetry { display: none !important; }" in CSS


def test_prompt_supports_shift_enter_and_responsive_multiline_growth():
    assert 'event.key === "Enter" && !event.shiftKey' in JS
    assert 'classList.toggle("expanded"' in JS
    assert 'Math.min(180, brief.scrollHeight)' in JS
    assert '.prompt-shell.expanded textarea' in RULES

    # Expanding gives the textarea the whole row, so it re-wraps and can drop
    # back to one line -- measuring in the current geometry made the layout
    # flip on alternate keystrokes. The decision has to come from the collapsed
    # width, which does not move, and the height from the resulting geometry.
    assert 'if (wasExpanded) shell.classList.remove("expanded")' in JS
    assert "needsExpand = brief.scrollHeight > oneLine + 2" in JS
    growth = JS.index("const autosizeBrief")
    decision = JS.index("needsExpand = brief.scrollHeight > oneLine + 2", growth)
    applied = JS.index('shell?.classList.toggle("expanded", needsExpand)', growth)
    final_height = JS.index("Math.min(180, brief.scrollHeight)", applied)
    assert decision < applied < final_height


def test_stopped_sessions_clear_unread_and_show_a_read_dot():
    assert "function sessionDotClass" in JS
    assert '"stopped"' in JS and '"interrupted"' in JS
    assert 'status === "stopped" || status === "interrupted"' in JS
    assert "if (jobId) this.unread.delete(jobId);" in JS
    assert "SESSION_UNREAD_ON_SETTLE" in JS
