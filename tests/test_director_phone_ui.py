"""Contracts the Director phone client has to keep: no sideways chat scroll,
no reconnecting banner, a floating composer."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "phone_site" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "phone_site" / "director.css").read_text(encoding="utf-8")
JS = (ROOT / "phone_site" / "director.js").read_text(encoding="utf-8")


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


def test_composer_reserves_layout_space_and_respects_the_safe_area():
    """The composer is a flex row, not an overlay. Its safe-area gap replaces
    the base gap when larger so rounded-screen insets are not double-counted."""
    block = _block(CSS, ".composer {", ".composer-pill,")
    assert "position: relative" in block
    assert "flex: none" in block
    assert "max(12px, env(safe-area-inset-right, 0px))" in block
    assert "max(12px, env(safe-area-inset-bottom, 0px))" in block
    assert "max(12px, env(safe-area-inset-left, 0px))" in block
    assert "background: transparent" in block
    assert "bottom: 0;" not in block
    assert "position: absolute" not in block
    transcript = _block(CSS, ".transcript {", ".transcript > *")
    assert "padding: 14px 12px" in transcript
    assert "safe-area-inset-bottom" not in transcript
    chat = _block(CSS, "#screen-chat {", "#screen-chat .topbar")
    assert "display: flex" in chat
    assert "flex-direction: column" in chat


def test_app_boots_without_the_code_transcript_module():
    """A static import of /code/transcript.js 404s on Vercel (that folder is
    outside the phone_site project) and Safari never runs load(), so a paired
    phone sees Connect again with the token still in localStorage."""
    assert 'from "/code/transcript.js"' not in JS
    assert 'await import("/code/transcript.js")' in JS
    assert 'localStorage.getItem("aios-director")' in HTML
    assert (ROOT / "phone_site" / "code" / "transcript.js").is_file()
    assert (ROOT / "phone_site" / "code" / "markdown.js").is_file()
    build = (ROOT / "phone_site" / "scripts" / "build.js").read_text(encoding="utf-8")
    assert "localCode" in build


def test_compacted_history_has_a_small_disclosure_control():
    assert 'id="btn-history-toggle"' in HTML
    assert 'aria-expanded="false"' in HTML
    transcript = _block(HTML, '<div class="transcript" id="transcript">', '</div>\n\n      <div class="composer">')
    assert 'id="btn-history-toggle"' in transcript
    header = _block(HTML, '<header class="topbar">', '</header>')
    assert 'id="btn-history-toggle"' not in header
    assert "compacted_through" in JS
    assert "togglePreviousMessages" in JS
    assert 'message.sequence' in JS


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
    assert ".blob-grid.eyes { grid-template-columns: repeat(3" in CSS
