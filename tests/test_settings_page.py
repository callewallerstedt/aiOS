"""Tests for the aiOS Settings page: sub-pages, autosave and its edge cases.

These drive the real Tk widgets, so they skip cleanly on a machine with no
display rather than failing.
"""

import copy
import sys
import time
import tkinter as tk
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import helper_overlay


PAGES = [name for name, _hint in helper_overlay.HelperOverlay.SETTINGS_PAGES]


@pytest.fixture
def app(monkeypatch, tmp_path):
    """A HelperOverlay with just enough state to render Settings."""
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()

    writes = []
    monkeypatch.setattr(helper_overlay, "save_config", lambda cfg: writes.append(copy.deepcopy(cfg)))

    overlay = helper_overlay.HelperOverlay.__new__(helper_overlay.HelperOverlay)
    overlay.root = root
    overlay.config = copy.deepcopy(helper_overlay.DEFAULT_CONFIG)
    overlay.theme = overlay.config["theme"]
    overlay.brand_font_family = "Segoe UI"
    overlay.project_root = tmp_path / "projects"
    overlay.project_root.mkdir()
    overlay.settings_page = "General"
    overlay.settings_status_var = None
    overlay._settings_status_after = None
    overlay.settings_color_rows = {}
    overlay.agent_operator_default_model = "gpt-5.6-luna"
    overlay.agent_operator_default_voice = "nova"
    # Mirror the operator variables HelperOverlay.__init__ sets to None.
    for name in (
        "monitor", "model", "planner_model", "reason", "steps",
        "delay", "tts", "voice", "shell", "codex",
    ):
        setattr(overlay, f"agent_operator_{name}_var", None)
    # Side effects that need a real running app. The updater card otherwise
    # fires a GitHub check on a background thread that outlives the test root.
    overlay._updater_check = lambda silent=False: None
    overlay._reload_voice_dictation = lambda: None
    overlay.rebuild_shell = lambda: None
    overlay.refresh_chat_account = lambda: None
    overlay.root.attributes = lambda *args, **kwargs: None
    overlay.page = tk.Frame(root, bg=overlay.c("panel"))
    overlay.page.pack()
    overlay.writes = writes

    yield overlay

    try:
        root.destroy()
    except tk.TclError:
        pass


def show(app, page):
    for child in app.page.winfo_children():
        child.destroy()
    app.settings_page = page
    app.render_settings()
    app.root.update_idletasks()


def pump(app, seconds=0.45):
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.root.update()
        time.sleep(0.02)


def widgets(parent, kind, found=None):
    found = [] if found is None else found
    for child in parent.winfo_children():
        if isinstance(child, kind):
            found.append(child)
        widgets(child, kind, found)
    return found


# ------------------------------------------------------------------- rendering


@pytest.mark.parametrize("page", PAGES)
def test_every_settings_page_renders(app, page):
    show(app, page)
    assert app.page.winfo_children()


@pytest.mark.parametrize("page", PAGES)
def test_rendering_a_page_never_writes_config(app, page):
    """Opening a page must not save. Sliders used to fire on construction."""
    show(app, page)
    pump(app)
    assert app.writes == [], f"{page} wrote config just by being opened"


def test_the_selected_page_survives_a_rerender(app):
    show(app, "Voice")
    assert app.settings_page == "Voice"
    show(app, "Voice")
    assert app.settings_page == "Voice"


def test_an_unknown_page_falls_back_to_the_first(app):
    app.settings_page = "Nonsense"
    for child in app.page.winfo_children():
        child.destroy()
    app.render_settings()
    assert app.settings_page == PAGES[0]


# -------------------------------------------------------------------- autosave


def test_a_text_field_saves_when_it_loses_focus(app):
    show(app, "Voice")
    entry = app.voice_vocabulary_entry
    entry.delete("1.0", "end")
    entry.insert("1.0", "Kubernetes, Grafana")
    entry.event_generate("<FocusOut>")
    app.root.update_idletasks()
    assert app.config["voice_dictation"]["vocabulary"] == ["Kubernetes", "Grafana"]
    assert app.writes


def test_leaving_an_untouched_field_writes_nothing(app):
    show(app, "Voice")
    app.voice_model_settings_entry.event_generate("<FocusOut>")
    app.root.update_idletasks()
    assert app.writes == []


def test_the_saved_indicator_names_the_field(app):
    show(app, "Voice")
    entry = app.voice_input_device_entry
    entry.delete("1.0", "end")
    entry.insert("1.0", "Yeti")
    entry.event_generate("<FocusOut>")
    app.root.update_idletasks()
    assert "Device" in app.settings_status_var.get()


def test_a_toggle_saves_immediately(app):
    show(app, "Voice agent")
    app.config["voice_dictation"]["agent_screen"] = True
    app.set_voice_agent_tool("agent_screen", False)
    assert app.config["voice_dictation"]["agent_screen"] is False
    assert app.writes


def test_dragging_a_slider_is_debounced_to_one_write(app):
    show(app, "Appearance")
    pump(app)
    app.writes.clear()
    slider = widgets(app.page, tk.Scale)[0]
    for value in (90, 91, 92, 93, 94):
        slider.set(value)
    pump(app)
    assert len(app.writes) == 1, f"a five-step drag wrote {len(app.writes)} times"


def test_a_failing_commit_reports_instead_of_raising(app):
    show(app, "General")
    entry = app.root_entry
    entry.delete("1.0", "end")
    # A path Windows cannot create.
    entry.insert("1.0", "Z:\\\0bad")
    entry.event_generate("<FocusOut>")
    app.root.update_idletasks()
    assert app.settings_status_var.get(), "the failure should surface in the status line"


# --------------------------------------------------------------- project root


def test_the_project_root_only_commits_on_blur(app, tmp_path):
    """Saving per keystroke would mkdir 'C', 'C:', 'C:\\Us'… on the way past."""
    show(app, "General")
    target = tmp_path / "new-root"
    entry = app.root_entry
    entry.delete("1.0", "end")
    entry.insert("1.0", str(target))
    app.root.update_idletasks()
    assert not target.exists(), "typing alone must not create the folder"
    entry.event_generate("<FocusOut>")
    app.root.update_idletasks()
    assert target.exists()
    assert app.config["project_root"] == str(target)


def test_an_empty_project_root_is_ignored(app):
    show(app, "General")
    original = app.config.get("project_root")
    entry = app.root_entry
    entry.delete("1.0", "end")
    entry.event_generate("<FocusOut>")
    app.root.update_idletasks()
    assert app.config.get("project_root") == original


# ------------------------------------------------------------------- operator


def test_operator_settings_bind_to_the_shared_variables(app):
    show(app, "OPERATOR")
    assert app.agent_operator_model_var is not None
    assert app.agent_operator_steps_var is not None
    # The OPERATOR tab reuses these same objects, so the screens cannot drift.
    model_var = app.agent_operator_model_var
    show(app, "OPERATOR")
    assert app.agent_operator_model_var is model_var


def test_operator_flag_commits_through_the_shared_saver(app):
    show(app, "OPERATOR")
    app._commit_operator_flag(app.agent_operator_shell_var, True)
    assert app.config["ai_operator"]["shell"] is True
    assert app.writes


# ------------------------------------------------------------------ macro pad


def test_macro_pad_page_lists_the_batch_files(app):
    show(app, "Macro pad")
    labels = [w.cget("text") for w in widgets(app.page, tk.Label)]
    assert any("voice_ptt_down.bat" in text for text in labels)
    assert any("voice_stop_agent.bat" in text for text in labels)


def test_macro_pad_page_flags_a_missing_file(app, monkeypatch, tmp_path):
    # Point BASE_DIR somewhere empty so every binding reads as missing.
    monkeypatch.setattr(helper_overlay, "BASE_DIR", tmp_path)
    show(app, "Macro pad")
    labels = [w.cget("text") for w in widgets(app.page, tk.Label)]
    assert any("(missing)" in text for text in labels)
