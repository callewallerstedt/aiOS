from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_phone_monitor_reuses_code_transcript_with_live_session_drawer():
    code = (ROOT / "aios_ui" / "web" / "js" / "code.js").read_text(encoding="utf-8")
    css = (ROOT / "aios_ui" / "web" / "css" / "code.css").read_text(encoding="utf-8")

    assert 'data-code="phone-sessions"' in code
    assert 'data-code="phone-sessions-close"' in code
    assert 'data-code="phone-history"' in code
    assert 'data-code="phone-agent"' in code
    assert "this.phoneShowHistory" in code
    assert "ACTIVE.has(String(job.status" in code
    assert "if (live && live.id) this.select" in code
    assert ".phone-mirror .code-composer { display: none !important; }" in css
    assert ".phone-mirror .phone-sessions-open .code-sessions" in css
    assert ".phone-mirror .code-transcript" in css


def test_phone_agent_drawer_supports_text_and_pc_dictation():
    app = (ROOT / "aios_ui" / "web" / "js" / "app.js").read_text(encoding="utf-8")
    chat = (ROOT / "aios_ui" / "web" / "js" / "chat.js").read_text(encoding="utf-8")
    mirror = (ROOT / "aios_ui" / "mirror.py").read_text(encoding="utf-8")

    assert "this.chat = new ChatPanel" in app
    assert 'id="chat-input"' in chat
    assert 'id="chat-mic"' in chat
    assert "navigator.mediaDevices.getUserMedia" in chat
    assert "http://127.0.0.1:5000/api/phone/transcribe" in chat
    assert "TRANSCRIBE_PORT = 5000" in mirror


def test_phone_wrapper_reserves_real_cutout_and_rounded_corner_insets():
    activity = (ROOT / "aios-mirror-android" / "app" / "src" / "main" / "java" / "com" / "aios" / "mirror" / "MainActivity.java").read_text(encoding="utf-8")
    manifest = (ROOT / "aios-mirror-android" / "app" / "src" / "main" / "AndroidManifest.xml").read_text(encoding="utf-8")
    shell_css = (ROOT / "aios_ui" / "web" / "css" / "shell.css").read_text(encoding="utf-8")

    assert "getDisplayCutout()" in activity
    assert "getRoundedCorner" in activity
    assert "--phone-safe-left" in activity
    assert "padding: var(--phone-safe-top) var(--phone-safe-right) var(--phone-safe-bottom) var(--phone-safe-left);" in shell_css
    assert "android.permission.MODIFY_AUDIO_SETTINGS" in manifest


def test_phone_native_window_monitor_is_read_only_and_uses_real_frames():
    app = (ROOT / "aios_ui" / "web" / "js" / "app.js").read_text(encoding="utf-8")
    server = (ROOT / "aios_ui" / "server.py").read_text(encoding="utf-8")
    capture = (ROOT / "aios_ui" / "native_windows.py").read_text(encoding="utf-8")

    assert 'data-native-app="codex"' in (ROOT / "aios_ui" / "web" / "index.html").read_text(encoding="utf-8")
    assert 'data-native-app="claude"' in (ROOT / "aios_ui" / "web" / "index.html").read_text(encoding="utf-8")
    assert 'data-native-app="cursor"' in (ROOT / "aios_ui" / "web" / "index.html").read_text(encoding="utf-8")
    assert 'authenticatedUrl(`/native/frame/${selected}' in app
    assert 'route.startswith("/native/frame/")' in server
    assert "PrintWindow" in capture
    assert "no input path is exposed" in capture
