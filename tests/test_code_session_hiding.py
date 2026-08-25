from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CODE_JS = (ROOT / "aios_ui" / "web" / "js" / "code.js").read_text(encoding="utf-8")
CODE_CSS = (ROOT / "aios_ui" / "web" / "css" / "code.css").read_text(encoding="utf-8")


def test_code_sidebar_hide_mode_is_persistent_and_reversible():
    assert 'data-code="hide-toggle"' in CODE_JS
    assert "this.hiddenSessions = loadHiddenSessionIds()" in CODE_JS
    assert "saveHiddenSessionIds(this.hiddenSessions)" in CODE_JS
    assert "this.hideMode || !this.hiddenSessions.has" in CODE_JS
    assert 'action === "hide-toggle"' in CODE_JS
    assert 'toggle.setAttribute("aria-pressed", String(this.hideMode))' in CODE_JS
    assert ".session-row.hidden-session" in CODE_CSS
