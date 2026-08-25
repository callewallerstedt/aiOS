"""CODE split-pane chrome stays integrated with the cards."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CODE = (ROOT / "aios_ui" / "web" / "js" / "code.js").read_text(encoding="utf-8")
SPLIT = (ROOT / "aios_ui" / "web" / "js" / "code_split.js").read_text(encoding="utf-8")
CSS = (ROOT / "aios_ui" / "web" / "css" / "code-beautiful.css").read_text(encoding="utf-8")
THEME = (ROOT / "aios_ui" / "web" / "css" / "theme.css").read_text(encoding="utf-8")
SETTINGS = (ROOT / "aios_ui" / "web" / "js" / "settings.js").read_text(encoding="utf-8")
APP = (ROOT / "aios_ui" / "web" / "js" / "app.js").read_text(encoding="utf-8")


def test_session_names_and_split_controls_live_in_card_headers():
    assert 'data-code="pane-tabs"' not in CODE
    assert 'data-code="main-pane-title"' in CODE
    assert 'data-code="add-pane"' in CODE
    assert 'class="pane-title-close" data-pane-act="close"' in SPLIT
    assert 'class="title-block" draggable="true"' in SPLIT
    assert "this.movePane(Number(payload.slice(4))" in SPLIT
    assert 'data-code="raw-output"' in CODE
    assert 'data-pane-act="raw"' in SPLIT


def test_chat_cards_and_composer_follow_appearance_radius():
    assert "--bui-card-radius: var(--radius, 28px)" in CSS
    assert "border-radius: var(--bui-card-radius)" in CSS
    assert ".prompt-config-button { height: 28px; border-radius: 999px !important" in CSS
    assert "*, *::before, *::after { border-radius: var(--global-radius) !important; }" in THEME
    assert "--global-radius: var(--radius)" in THEME
    assert 'exact.type = "number"' in SETTINGS
    assert 'exact.min = "0"' in SETTINGS
    assert 'root.setProperty("--global-radius", `${radius}px`)' in APP
