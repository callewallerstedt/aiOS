"""Contracts the Director phone client has to keep: no sideways chat scroll,
no reconnecting banner, a floating composer."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "phone_site" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "phone_site" / "director.css").read_text(encoding="utf-8")
JS = (ROOT / "phone_site" / "director.js").read_text(encoding="utf-8")
SW = (ROOT / "phone_site" / "sw.js").read_text(encoding="utf-8")


def _block(source: str, start: str, end: str) -> str:
    i = source.index(start)
    j = source.index(end, i + len(start))
    return source[i:j]


def test_connection_is_a_dot_not_a_reconnecting_banner():
    assert "conn-dot" in HTML
    assert HTML.count('class="conn-dot"') >= 2
    assert "agents-sub" not in HTML
    assert "Connecting…" not in HTML
    assert "Reconnecting" not in JS
    assert "agents · connected" not in JS
    assert "function setConnection(online, issue)" in JS
    assert 'dot.dataset.conn = conn' in JS


def test_websocket_close_does_not_reconnect_over_a_live_socket():
    """connect() used to close the current socket, whose close handler then
    called connect() again — a reconnecting/connected loop on the phone."""
    body = _block(JS, "function connect()", "function setConnection")
    assert "if (state.socket !== socket) return;" in body
    assert "if (state.token && !state.socket) connect();" in body


def test_tool_preview_ellipsis_is_not_a_flex_item():
    """display:flex on .detail makes text-overflow a no-op, so a long tool
    preview (the Director chat's operator/web chips) expands the transcript."""
    block = _block(CSS, ".tool-chip .detail {", ".tool-chip .detail:empty")
    assert "display: flex" not in block
    assert "flex: 1 1 0" in block
    assert "text-overflow: ellipsis" in block
    assert "overflow: hidden" in _block(CSS, ".tool-card {", ".tool-chip {")
    assert "overflow-x: clip" in CSS


def test_composer_floats_over_the_transcript_with_a_transparent_bar():
    """The pill overlays the thread. The wrapper is transparent so there is
    no solid bar behind it; a spacer keeps the last message above the pill."""
    block = _block(CSS, ".composer {", ".composer-pill,")
    assert "position: absolute" in block
    assert "left: 0" in block
    assert "right: 0" in block
    assert "bottom: 0" in block
    assert "max(12px, env(safe-area-inset-right, 0px))" in block
    assert "var(--composer-bottom)" in block
    assert "max(12px, env(safe-area-inset-left, 0px))" in block
    assert "background: transparent" in block
    assert "interactive-widget=resizes-visual" in HTML
    assert "function syncKeyboardViewport()" in JS
    assert 'style.setProperty("--visual-height"' in JS
    assert 'keyboardOpen ? "8px"' in JS
    assert 'visualViewport?.addEventListener("resize"' in JS
    transcript = _block(CSS, ".transcript {", ".transcript > *")
    assert "padding: 14px 12px" in transcript
    assert "safe-area-inset-bottom" not in transcript
    spacer = _block(CSS, ".transcript-spacer {", ".chat-stamp")
    assert "var(--composer-bottom)" in spacer
    chat = _block(CSS, "#screen-chat {", "#screen-chat .topbar")
    assert "display: flex" in chat
    assert "flex-direction: column" in chat
    assert "position: relative" in chat


def test_app_boots_without_the_code_transcript_module():
    """A static import of /code/transcript.js 404s on Vercel (that folder is
    outside the phone_site project) and Safari never runs load(), so a paired
    phone sees Connect again with the token still in localStorage."""
    assert 'from "/code/transcript.js"' not in JS
    assert 'await import("/code/transcript.js?v=44")' in JS
    assert 'localStorage.getItem("aios-director")' in HTML
    assert (ROOT / "phone_site" / "code" / "transcript.js").is_file()
    assert (ROOT / "phone_site" / "code" / "markdown.js").is_file()
    build = (ROOT / "phone_site" / "scripts" / "build.js").read_text(encoding="utf-8")
    assert "fs.existsSync(localPath) ? localPath : aiosPath" in build
    assert "localCode" in build


def test_offline_mock_is_localhost_only():
    """The playground mock must not ship to Vercel or run on the live PWA."""
    assert 'src="/mock.js?v=47"' in HTML
    assert 'host !== "localhost"' in HTML
    assert 'host !== "127.0.0.1"' in HTML
    build = (ROOT / "phone_site" / "scripts" / "build.js").read_text(encoding="utf-8")
    assert "mock.js" not in build
    assert (ROOT / "phone_site" / "mock.js").is_file()


def test_compacted_history_has_a_small_disclosure_control():
    assert 'id="btn-history-toggle"' in HTML
    assert 'aria-expanded="false"' in HTML
    transcript = _block(HTML, '<div class="transcript" id="transcript">', '</div>\n\n      <div class="composer">')
    assert 'id="btn-history-toggle"' in transcript
    assert "Older chats" in transcript
    header = _block(HTML, '<header class="topbar">', '</header>')
    assert 'id="btn-history-toggle"' not in header
    assert "compacted_through" in JS
    assert "togglePreviousMessages" in JS
    assert 'message.sequence' in JS
    assert "thread?.hidden_count" in JS
    history_css = _block(CSS, ".history-toggle {", ".history-toggle:hover")
    assert "position: sticky" in history_css
    assert "top: 0" in history_css
    assert "function ensureHistoryToggle()" in JS
    ensure = _block(JS, "function ensureHistoryToggle()", "function renderMessages")
    assert 'button.addEventListener("click", togglePreviousMessages)' in ensure
    assert "const historyButton = ensureHistoryToggle()" in JS


def test_operator_tool_card_opens_its_own_reasoning_and_screenshot_timeline():
    tool = _block(JS, "function toolCard", "function markPendingTool")
    assert "wrap.dataset.operatorJobId" in tool
    assert "loadOperatorJob" in tool
    finish = _block(JS, "function finishTool", "function configureOperatorJob")
    assert 'card?.job_kind === "operator"' in finish
    assert 'name === "operator"' not in finish
    assert "events?since=${cursor}&limit=${pageSize}" in JS
    assert "operatorEventCompare" in JS
    assert "insertBefore(node, following.node)" in JS
    assert "OPERATOR_TERMINAL_EVENTS" in JS
    assert "operatorEventNode" in JS
    assert 'event.kind === "operator.screenshot"' in JS
    assert 'event.kind === "operator.step"' in JS
    assert 'event.kind === "operator.actions"' in JS
    assert ".operator-frame" in CSS
    assert ".operator-click-marker" in CSS
    assert 'payload.phase === "action_target"' in JS
    assert "payload.marker.x" in JS
    assert ".operator-event-text" in CSS


def test_operator_card_is_typed_on_start_and_history_is_paginated_deterministically():
    live = _block(JS, 'case "tool.start":', 'case "tool.done":')
    assert "card: payload.card" in live
    assert 'job_kind": "operator"' in (
        ROOT / "director" / "runtime.py").read_text(encoding="utf-8")
    loader = _block(JS, "async function loadOperatorJob", "function approvalCard")
    assert "while (true)" in loader
    assert "pageSize = 80" in loader
    assert "next <= cursor" in loader
    server = (ROOT / "director" / "server.py").read_text(encoding="utf-8")
    assert 'request.query.get("limit")' in server
    assert 'store.list_job_events(job["id"], since=since, limit=limit)' in server
    meta = _block(JS, "function updateOperatorMeta", "function appendOperatorEvent")
    assert "OPERATOR_TERMINAL_EVENTS" in meta
    assert "terminal || progress" in meta


def test_operator_live_and_history_events_merge_in_id_order_without_status_regression(tmp_path):
    import subprocess

    logic = _block(JS, "function operatorEventKey", "function recordOperatorEvent")
    script = r'''
function operatorEventNode(event) { return { kind: event.kind, parentNode: null }; }
class Classes {
  constructor() { this.values = new Set(); }
  add(...names) { names.forEach((name) => this.values.add(name)); }
  remove(...names) { names.forEach((name) => this.values.delete(name)); }
}
const meta = { textContent: "running" };
const body = {
  children: [],
  querySelector() { return null; },
  append(node) { node.parentNode = this; this.children.push(node); },
  insertBefore(node, following) {
    node.parentNode = this;
    this.children.splice(this.children.indexOf(following), 0, node);
  },
};
const view = {
  body, seen: new Set(), events: new Map(),
  wrap: { classList: new Classes(), querySelector() { return meta; } },
};
appendOperatorEvent(view, { id: 10, kind: "operator.done", payload: { summary: "done" } });
appendOperatorEvent(view, { id: 5, kind: "operator.step", payload: { step: 2 } });
appendOperatorEvent(view, { id: 7, kind: "operator.note", payload: { step: 2 } });
appendOperatorEvent(view, { id: 5, kind: "operator.step", payload: { step: 2 } });
if (body.children.map((node) => node.kind).join(",") !==
    "operator.step,operator.note,operator.done") process.exit(1);
if (meta.textContent !== "done") process.exit(2);
if (view.events.size !== 3) process.exit(3);
process.stdout.write("ok");
'''
    path = tmp_path / "operator-order.js"
    path.write_text(logic + "\n" + script, encoding="utf-8")

    result = subprocess.run(
        ["node", str(path)], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok"


def test_live_churning_grid_survives_into_real_thinking_and_history():
    theme = (ROOT / "phone_site" / "code" / "code-beautiful.css").read_text(
        encoding="utf-8")
    assert ":root {" in theme[:500]
    assert "--bui-ink-3:" in theme[:1000]
    assert "--bui-accent-ink:" in theme[:1000]
    assert 'head.innerHTML = `${loadingPixels()}<span class="thinking-label">Thinking</span>' in JS
    ensure = _block(JS, "function ensureThinking()", "function settleThinking()")
    assert ensure.index("loadingPixels()") < ensure.index("settleWorking()")
    settle = _block(JS, "function settleThinking()", "function toolCard")
    assert 'querySelector(".loading-pixels")?.remove()' in settle
    assert "addThinking(message.meta?.reasoning" in JS
    assert '"reasoning": str(reply.get("reasoning")' in (
        ROOT / "director" / "runtime.py").read_text(encoding="utf-8")


def test_tool_result_images_are_visible_live_and_after_reopening_chat():
    history = _block(JS, "function renderMessages", "/* ---------------- events")
    assert "message.meta?.image" in history
    assert "shotCard({ image: message.meta.image" in history
    live = _block(JS, 'case "tool.done":', 'case "approval":')
    assert "payload.image" in live
    assert "shotCard({ image: payload.image" in live
    shots = _block(JS, "function shotCard", "function taskRow")
    assert "persistent = false" in shots
    assert 'row.removeAttribute("id")' in shots
    runtime = (ROOT / "director" / "runtime.py").read_text(encoding="utf-8")
    assert 'payload["image"] = image' in runtime


def test_operator_stuck_issue_is_visible_in_live_chat():
    block = _block(JS, 'case "operator.stuck":', 'case "operator.started":')
    assert "payload.issue" in block
    assert "Operator stopped:" in block


def test_agent_relay_messages_are_attributed_and_destination_cards_open():
    add_user = _block(JS, "function addUser", "function addAssistant")
    assert 'extra.kind === "agent_message"' in add_user
    assert "From ${sender.name" in add_user
    tool = _block(JS, "function toolCard", "function finishTool")
    assert "card?.agent_id" in tool
    assert "openAgent(card.agent_id)" in tool
    assert ".relay-message .relay-bubble" in CSS


def test_user_bubbles_keep_the_selected_background_on_live_and_restored_messages():
    add_user = _block(JS, "function addUser", "function addAssistant")
    assert 'el("div", "bubble-user")' in add_user
    assert "node.style.backgroundColor = state.appearance?.user_bubble" in add_user
    assert "node.style.color = state.appearance?.user_text" in add_user


def test_left_edge_swipe_and_browser_back_share_navigation():
    assert "function installEdgeSwipe()" in JS
    assert "clientX > 24" in JS
    assert "dx > 72" in JS
    assert 'window.addEventListener("popstate"' in JS
    assert '$("btn-back").addEventListener("click", navigateBack)' in JS


def test_returning_to_list_does_not_refetch_or_resize_the_selected_avatar():
    leave = _block(JS, "function leaveChat()", "function navigateBack()")
    assert "loadAgents" not in leave
    blob = _block(JS, "function blobSvg", "function fillAvatar")
    assert 'viewBox="0 0 64 64"' in blob
    assert 'mood === "sleeping" ?' not in blob


def test_homepage_settings_has_universal_instructions():
    """The agent-list Settings sheet is where standing instructions are edited.
    PATCH /api/settings {instructions} is what every agent prompt reads."""
    body = _block(JS, "async function openSettings()", "// permissions")
    assert 'el("h3", null, "Instructions")' in body
    assert "instructions: houseInput.value" in body
    assert "Every agent sees this" in body
    assert "Save instructions" in body


def test_settings_shows_openrouter_balance_and_luna_choice():
    body = _block(JS, "async function openSettings()", "// operator")
    assert 'api("/api/openrouter/balance")' in body
    assert 'api("/api/openrouter/balance?refresh=1")' in body
    assert '"OpenRouter balance"' in body
    assert '`${model.label} (OpenRouter)`' in body
    assert "model || orInput.value.trim()" in body


def test_group_chat_has_a_chooser_editor_and_working_chips():
    assert "function newChatChooser()" in JS
    assert "function groupEditor(group)" in JS
    assert "function paintGroupWorking()" in JS
    assert 'kind: "group"' in JS
    assert "start_work" in JS
    assert "work-cluster" in JS
    assert "work-chip" in CSS
    assert "avatar-stack" in CSS
    assert 'title="New chat"' in HTML
    assert '$("btn-new-agent").addEventListener("click", newChatChooser)' in JS
    assert "they'll pick this up without stopping" in JS
    diamond = _block(JS, "function blobBody", "function sleepWindow")
    assert "41.4" in diamond
    assert "11.3" in diamond
    assert "function sleepWindow(agent)" in JS
    assert "function isAsleep(agent, now)" in JS
    assert 'return "sleeping";' in _block(JS, "function agentMood", "function blobMoodEyes")
    assert "isAsleep(agent)" in _block(JS, "function agentMood", "function blobMoodEyes")
    assert 'stack.dataset.count' in JS
    assert ".avatar-stack[data-count=\"3\"]" in CSS
    assert "animation: none" in _block(CSS, ".avatar-stack {", ".row-agent.named")
    assert "sub-pill" in CSS
    assert "function paintAgentName(node, agent)" in JS
    assert "function addReaction(payload, at)" in JS
    assert "react-chip" in CSS
    assert 'case "message.reaction"' in JS
    assert 'avatarNode(member, "tiny", "idle")' in JS
    assert 'avatarNode(speaker, "tiny", "idle")' in JS
    assert 'payload.name === "react"' in _block(JS, 'case "tool.done":', "case \"approval\":")
    assert "target_id" in _block(JS, "function addReaction", "function paintGroupWorking")
    assert "data-msg-id" in JS or "dataset.msgId" in JS
    assert "position: absolute" in _block(CSS, ".react-bar {", ".react-chip {")
    assert "margin-inline: auto" not in _block(CSS, ".transcript > * {", ".transcript > .thinking")


def test_no_agent_kind_is_mandatory_in_the_editor():
    editor = _block(JS, "async function agentEditor", "function toggleRow")
    assert 'if (!creating && !isGroup(agent))' in editor
    assert 'agent.kind === "custom"' not in editor
    assert "Delete this agent" in editor


def test_agent_markdown_renders_pipe_tables(tmp_path):
    """Agent bubbles used to print | tables as paragraphs. GFM tables,
    including blank lines between rows, have to become a real <table>."""
    import json
    import subprocess

    body = _block(JS, "function markdown(source)", "function relativeTime")
    assert "<table>" in body
    assert "thead" in body
    assert r"/^:?-{3,}:?$/" in body
    assert 'class="md-table"' in body
    assert ".md-table" in CSS
    assert ".assistant th, .assistant td" in CSS

    sample = (
        "Known positions from the previous record\n"
        "| Position | Entry | Quantity | Today |\n"
        "\n"
        "|---|---:|---:|---:|\n"
        "\n"
        "| NVIDIA | $221.84 | 2 | Not reliably verified |\n"
        "\n"
        "| SAAB B | 673.80 kr | 4 | Not reliably verified |\n"
        "\n"
        "| Tesla | — | — | $326.13, down 2.01% at 13:29 EDT |\n"
    )
    helpers = _block(JS, "function escapeHtml(text)", "function relativeTime")
    script = (
        helpers
        + "\nconst html = markdown(" + json.dumps(sample) + ");\n"
        + "if (!html.includes('<table>') || !html.includes('NVIDIA')) process.exit(1);\n"
        + "if (html.includes('| NVIDIA |')) process.exit(2);\n"
        + "if (!html.includes('text-align:right')) process.exit(3);\n"
        + "if (!html.includes('SAAB B') || !html.includes('Tesla')) process.exit(4);\n"
        + "process.stdout.write('ok');\n"
    )
    path = tmp_path / "md.js"
    path.write_text(script, encoding="utf-8")
    result = subprocess.run(
        ["node", str(path)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout == "ok"


def test_homepage_has_a_wake_pc_button_for_an_offline_windows_box():
    assert 'id="btn-wake-pc"' in HTML
    assert "Wake PC" in HTML
    assert "function paintWakeButton()" in JS
    assert '"/api/wake"' in JS
    assert "wake.available" in JS
    assert "pc-asleep" in CSS
    assert ".wake-pc" in CSS
    assert '"/api/power/off"' in JS
    assert "Turn PC off" in JS
    assert "can_power_off" in JS
    wake_css = _block(CSS, ".wake-pc {", ".wake-pc span")
    assert "width: 34px" in wake_css
    assert "height: 34px" in wake_css
    assert "place-items: center" in wake_css
    assert "border-radius: 9px" in wake_css
    paint = _block(JS, "function paintWakeButton()", "function markMachine")
    assert 'wake.power_state === "off"' in paint
    assert "windows?.online === true" in paint


def test_homepage_voice_and_hidden_component_gallery_are_wired():
    assert 'id="btn-home-voice"' in HTML
    assert "toggleHomeVoice" in JS
    assert "pinned_agent_id" in JS
    assert 'id="director-logo"' in HTML
    assert "installDirectorDoubleTap" in JS
    assert "developerGallery" in JS


def test_hidden_developer_menu_opens_the_accelerometer_phone_mouse():
    menu = _block(JS, "function openDeveloperMenu()", "function installDirectorDoubleTap()")
    assert '"Open phone mouse"' in menu
    assert "openPhoneMouse()" in menu
    assert "requestMotionPermission" in JS
    assert "DeviceMotionEvent.requestPermission" in JS
    assert 'window.addEventListener("devicemotion", handlePhoneMotion' in JS
    assert 'type: "mouse", machine_id: target, action, payload' in JS
    assert 'sendPhoneMouse("button", { button: name, pressed: true })' in JS
    assert 'sendPhoneMouse("button", { button: name, pressed: false })' in JS
    assert 'sendPhoneMouse("stop"' in JS
    assert "phoneMouseMachines" in JS
    assert 'data-mode="accelerometer"' in JS
    assert 'data-mode="trackpad"' in JS
    assert "bindPhoneMousePad" in JS
    assert "bindPhoneMouseWheel" in JS
    assert 'sendPhoneMouseDelta("scroll"' in JS
    assert "activatePhoneMouse()" in _block(JS, "async function startPhoneMouse()", "async function activatePhoneMouse()")
    assert "accelerationIncludingGravity" in JS
    assert "rotationRate" not in _block(JS, "function handlePhoneMotion", "async function requestMotionPermission")
    assert ".phone-mouse-page" in CSS
    assert ".phone-mouse-clicks" in CSS
    assert ".phone-mouse-wheel" in CSS
    assert ".phone-mouse-pad" in CSS
    clicks = _block(CSS, ".phone-mouse-clicks {", ".phone-mouse-click,")
    assert "grid-template-columns: 1fr 78px 1fr" in clicks
    assert "min-height: 96px" in _block(CSS, ".phone-mouse-click,", ".phone-mouse-click {")


def test_churning_pixels_survive_the_late_code_theme_override():
    assert "body .loading-pixels span { background: var(--accent); }" in CSS
    reduced = _block(CSS, "@media (prefers-reduced-motion: reduce)", "/* ---------------- CODE")
    assert "body .loading-pixels span { opacity: .72" in reduced


def test_startup_animates_then_reveals_cached_or_fresh_chat_list():
    assert 'id="screen-launch"' in HTML
    assert 'class="loading-pixels anim-wave launch-pixels"' in HTML
    assert HTML.count("<span></span>") >= 9
    assert ".launch-screen.is-leaving" in CSS
    assert ".launch-pixels.loading-pixels" in CSS
    storage = _block(JS, "function load()", "function save()")
    assert "Array.isArray(raw.agents)" in storage
    save = _block(JS, "function save()", "function applyAppearance")
    assert "agents," in save
    load_agents = _block(JS, "async function loadAgents", "async function loadDirectorStatus")
    assert 'api("/api/agents"' in load_agents
    assert 'api("/api/state"' not in load_agents
    boot = _block(JS, "async function boot()", "function wire()")
    assert "Promise.race([agentsRequest" in boot
    assert "CACHED_START_GRACE_MS" in boot
    assert "await finishLaunch()" in boot
    assert "Opening the phone always lands on the complete chat list" in boot
    assert "isWide() && state.agentId" in boot
    assert boot.index("await finishLaunch()") < boot.index("loadDirectorStatus().catch")


def test_service_worker_serves_the_cached_shell_before_network_refresh():
    assert "if (url.pathname.startsWith(\"/api/\")) return;" in SW
    assert 'const cacheKey = isPage ? "/" : request;' in SW
    assert "caches.match(cacheKey).then((hit) => hit || fresh" in SW
    assert "event.waitUntil(fresh" in SW


def test_push_uses_agent_heading_and_is_silent_while_app_is_visible():
    assert 'payload.agent || payload.title || "Director"' in SW
    assert 'client.visibilityState === "visible"' in SW
    assert "showNotification(title" in SW
    assert 'const VERSION = "director-v47"' in SW
    assert 'director.js?v=47' in HTML
    assert 'director.css?v=47' in HTML


def test_stop_button_clears_stale_working_state_after_server_acknowledges():
    stop = _block(JS, "async function stopTurn()", "/* ---------------- voice")
    assert '/api/threads/${state.threadId}/stop' in stop
    assert "if (!result.hard_cancel)" in stop
    assert "state.groupWorking.clear()" in stop
    assert "settleThinking()" in stop
    assert "setBusy(false)" in stop
    assert 'addStatus("Stopped.")' in stop


def test_subtitle_rolls_seamlessly_only_when_it_overflows():
    assert "function subtitlePill(text)" in JS
    assert 'duplicate.setAttribute("aria-hidden", "true")' in JS
    assert 'width > pill.clientWidth' in JS
    assert "@keyframes subtitle-roll" in CSS
    assert "translateX(calc(-50% - 12px))" in CSS


def test_chat_bubbles_share_padding_and_have_sharp_tails():
    bubbles = _block(CSS, ".bubble-user, .bubble-agent {", ".bubble-user .attach-thumbs")
    assert "padding: 10px 14px" in bubbles
    assert "border-radius: 18px 18px 3px 18px" in bubbles
    assert "border-radius: 18px 18px 18px 3px" in bubbles
    beautiful = (ROOT / "phone_site/code/code-beautiful.css").read_text(encoding="utf-8")
    override = _block(beautiful, ".bubble-user {", "/* Streaming Text answer surface. */")
    assert "padding: 10px 14px" in override
    assert "border-radius: 18px 18px 3px 18px" in override


def test_home_voice_is_compact_icon_only_and_tap_to_toggle():
    assert "home-voice-label" not in HTML
    voice = _block(CSS, ".home-voice {", ".home-voice:active")
    assert "width: clamp(104px, 34vw, 132px)" in voice
    assert "aspect-ratio: 1" in voice
    assert "background: var(--accent)" in voice
    assert 'aria-label="Tap to talk"' in HTML
    assert 'aria-pressed="false"' in HTML
    toggle = _block(JS, "async function toggleHomeVoice", "/* ---------------- notifications")
    assert "if (state.homeRecording)" in toggle
    assert 'paintHomeVoice("Transcribing…")' in toggle
    assert "stopRecording()" in toggle
    assert "Listening — tap again to send" in toggle
    assert "homePressed" not in JS
    assert 'homeVoice?.addEventListener("click", toggleHomeVoice)' in JS
    assert 'homeVoice?.addEventListener("pointerdown"' not in JS
    assert 'homeVoice?.addEventListener("pointerup"' not in JS


def test_takeover_toolbar_can_request_the_software_keyboard():
    shell = _block(JS, "function ensureTakeoverShell()", "function openTakeover")
    assert 'aria-label", "Open keyboard"' in shell
    assert 'el("textarea", "takeover-keyboard-input")' in shell
    assert "keyboardInput.focus()" in shell
    assert "director.keyboard-input" in shell
    assert "director.keyboard-key" in shell
    assert ".takeover-keyboard-input" in CSS
    assert 'const VERSION = "director-v47"' in SW


def test_home_voice_change_does_not_restyle_the_chat_microphone():
    assert "#btn-mic {" not in CSS
    assert "#btn-mic svg {" not in CSS
    assert '<button class="pill-btn" id="btn-mic" type="button" aria-label="Voice">' in HTML


def test_supplied_eye_sheet_is_selectable_and_rotates_on_active_blobs():
    supplied = [
        "frustrated", "annoyed", "thinking",
        "focused", "sleepy", "confused",
        "skeptical", "worried", "mischievous",
    ]
    emotions = _block(JS, "const BLOB_EMOTIONS", "function blobSpec")
    eye_art = _block(JS, "function blobEyes", "function activeBlobEmotion")
    rotation = _block(JS, "function activeBlobEmotion", "function blobBody")
    picker = _block(JS, 'const eyeRow = el("div", "blob-row")', "const shuffle")
    for name in supplied:
        assert f'"{name}"' in emotions
        assert f'case "{name}"' in eye_art
    assert "ACTIVE_EYE_ROTATION" in rotation
    assert 'mood !== "working" && mood !== "waiting"' in rotation
    assert "ACTIVE_EYE_INTERVAL = 4500" in JS
    assert "setInterval(rotateActiveBlobEyes, ACTIVE_EYE_INTERVAL)" in JS
    assert 'btn.setAttribute("aria-label", btn.title)' in picker


def test_agent_editor_offers_an_ai_avatar_generator():
    """The agent editor's avatar picker has a 'Use AI' button that prompts for
    a description and calls the avatar generation endpoint, then sets the
    returned image as the draft avatar."""
    picker = _block(JS, 'const pickerButtons = el("div", "avatar-actions")',
                    "const blobBox = el(\"div\", \"blob-customize\")")
    assert '"Use AI"' in picker
    assert 'aiBox.hidden = true' in picker
    assert 'aiInput.placeholder' in picker
    assert '"/api/avatar/generate"' in picker
    assert 'draft.avatar = result.avatar' in picker
    assert 'paintAvatar()' in picker
    assert 'aiGenerate.textContent = "Generating…"' in picker
    assert "closeAi" in picker
    assert ".avatar-ai {" in CSS
    assert ".avatar-ai textarea {" in CSS
    assert ".avatar-ai-actions {" in CSS
    assert ".blob-grid.eyes { grid-template-columns: repeat(3" in CSS
