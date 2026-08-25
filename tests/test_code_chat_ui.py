"""The native CODE session chat: bubbles, markdown, and one card per tool call."""
import tkinter as tk

import pytest

import helper_overlay


def build_view(root):
    view = object.__new__(helper_overlay.HelperOverlay)
    view.root = root
    view.theme = dict(helper_overlay.DEFAULT_CONFIG["theme"])
    holder = tk.Frame(root, bg=view.CODE_CHAT_BG)
    holder.pack(fill="both", expand=True)
    view.code_chat = helper_overlay.ScrollFrame(holder, view.CODE_CHAT_BG)
    view.code_chat.pack(fill="both", expand=True)
    view.code_chat.on_user_scroll = view._code_chat_user_scrolled
    view.code_chat_inner = view.code_chat.inner
    view._code_chat_reset()
    return view


@pytest.fixture(scope="module")
def root():
    # One root for the module: repeated Tk() startups are slow and, on some
    # Windows Python builds, flaky.
    try:
        window = tk.Tk()
    except tk.TclError as exc:  # headless CI
        pytest.skip(f"no Tk display: {exc}")
    window.geometry("620x520")
    window.update_idletasks()
    try:
        yield window
    finally:
        try:
            window.destroy()
        except tk.TclError:
            pass


@pytest.fixture()
def view(root):
    for child in root.winfo_children():
        child.destroy()
    return build_view(root)


def rows(view):
    return [child for child in view.code_chat_inner.winfo_children() if child is not view.code_busy_row]


def text_of(widget):
    parts = []
    if isinstance(widget, tk.Text):
        parts.append(widget.get("1.0", "end-1c"))
    else:
        for option in ("text", "textvariable"):
            try:
                value = widget.cget(option)
            except tk.TclError:
                continue
            if option == "textvariable" and value:
                value = widget.tk.globalgetvar(value)
            if value:
                parts.append(str(value))
    for child in widget.winfo_children():
        parts.append(text_of(child))
    return "\n".join(part for part in parts if part)


def test_user_messages_get_their_own_right_aligned_bubble(view):
    view._code_render_event({"kind": "user", "text": "ship it"})
    row = rows(view)[0]
    bubble = row.winfo_children()[0]
    assert bubble.pack_info()["side"] == "right"
    assert "ship it" in text_of(bubble)


def test_agent_deltas_stream_into_one_markdown_block(view):
    for delta in ("## Plan\n", "- run **tests**", " now"):
        view._code_render_event({"kind": "assistant", "text": delta})
    assert len(rows(view)) == 1
    rendered = text_of(rows(view)[0])
    # Markdown syntax is applied as tags, so the marks themselves are gone.
    assert "Plan" in rendered and "#" not in rendered
    assert "•  run tests now" in rendered


def test_cursor_assistant_delta_events_stream_into_one_native_block(view):
    for delta in ("Every", " token", " stays together"):
        view._code_render_event({"kind": "assistant_delta", "delta": delta, "text": delta})
    assert len(rows(view)) == 1
    assert "Every token stays together" in text_of(rows(view)[0])


def test_command_phases_update_a_single_expandable_card(view):
    view._code_render_event({"kind": "tool", "text": "$ pytest -q"})
    view._code_render_event({"kind": "tool", "text": "Running pytest -q"})
    view._code_render_event({"kind": "tool", "text": "Ran pytest -q"})
    assert len(rows(view)) == 1
    assert len(view.code_activity_cards) == 1

    card = next(iter(view.code_activity_cards.values()))
    assert card["open"] is False
    assert "pytest -q" in card["preview_var"].get()
    view._code_toggle_activity(card)
    assert card["open"] is True
    assert card["body"].winfo_ismapped() or card["body"].winfo_manager() == "pack"


def test_activity_events_reuse_their_card_and_finish_with_a_check(view):
    for phase in ("started", "update", "completed"):
        view._code_render_event({
            "kind": "activity",
            "activity_id": "call-1",
            "activity_type": "command",
            "phase": phase,
            "title": "Ran command",
            "command": "npm run build",
            "output": "done" if phase == "completed" else "",
            "duration_ms": 2500 if phase == "completed" else None,
        })
    assert len(rows(view)) == 1
    card = view.code_activity_cards["call-1"]
    assert card["icon_var"].get() == "✓"
    assert "2.5s" in card["meta_var"].get()
    view._code_toggle_activity(card)
    assert "npm run build" in text_of(card["body"])
    assert "done" in text_of(card["body"])


def test_spinner_runs_while_the_job_is_working(view):
    view.code_stop_button = tk.Button(view.root)
    view.code_delete_button = tk.Button(view.root)
    view.code_detail_title_var = tk.StringVar(master=view.root)
    view.code_detail_meta_var = tk.StringVar(master=view.root)
    view._code_render_detail_meta({"title": "job", "status": "running", "provider": "claude"})
    assert view.code_busy_row is not None
    first = view.code_busy_icon.get()
    view._code_spin()
    assert view.code_busy_icon.get() != first
    view._code_render_detail_meta({"title": "job", "status": "completed", "provider": "claude"})
    assert view.code_busy_row is None


def test_markdown_blocks_keep_code_fences_and_lists_intact():
    blocks = helper_overlay.code_markdown_blocks("# Title\n\n1. first\n\n```sh\nls -la\n```\n> note")
    assert [block["type"] for block in blocks] == ["h1", "blank", "number", "blank", "code", "quote"]
    assert blocks[4]["spans"][0][0] == "ls -la"
    assert blocks[2]["marker"] == "1."


def test_markdown_tables_and_task_lists_are_structured():
    blocks = helper_overlay.code_markdown_blocks(
        "| File | Purpose |\n|---|---|\n| app.py | Main |\n\n- [x] Done\n- [ ] Next"
    )
    assert blocks[0]["type"] == "table"
    assert blocks[0]["rows"] == [["File", "Purpose"], ["app.py", "Main"]]
    tasks = [block for block in blocks if block["type"] == "task"]
    assert [task["checked"] for task in tasks] == [True, False]


def test_inline_spans_split_bold_code_and_links():
    spans = helper_overlay.code_inline_spans("use `npm ci` and **stop**, see [docs](https://x.dev)")
    assert ("npm ci", "code") in spans
    assert ("stop", "bold") in spans
    assert ("docs", "link", "https://x.dev") in spans


def test_native_markdown_renders_table_and_clickable_link(view):
    widget = view._code_add_message(
        "| File | Purpose |\n|---|---|\n| app.py | Main |\n\n[Docs](https://example.com)",
        "assistant",
    )
    rendered = widget.get("1.0", "end-1c")
    assert "File" in rendered and "Purpose" in rendered and "├" in rendered
    assert "|---|" not in rendered
    assert any(str(tag).startswith("link_") for tag in widget.tag_names())


def test_manual_scroll_is_not_overridden_by_new_output(view, root):
    view.code_auto_follow = False
    for index in range(25):
        view._code_add_status(f"line {index}")
    root.update_idletasks()
    view.code_chat.canvas.yview_moveto(0.15)
    before = view.code_chat.canvas.yview()[0]
    view._code_add_status("new output")
    root.update_idletasks()
    after = view.code_chat.canvas.yview()[0]
    assert abs(after - before) < 0.04


def test_manual_scroll_anchor_survives_growth_of_streamed_markdown(view, root):
    view.code_auto_follow = False
    for index in range(25):
        view._code_add_status(f"line {index}")
    view._code_stream_assistant("initial response")
    root.update_idletasks()
    view.code_chat.canvas.yview_moveto(0.15)
    before = view.code_chat.canvas.canvasy(0)

    view._code_stream_assistant(" additional streamed text" * 80)
    root.update_idletasks()
    root.update()
    after = view.code_chat.canvas.canvasy(0)

    assert abs(after - before) < 3


def test_completion_result_does_not_repeat_agent_text(view):
    view._code_render_event({"kind": "assistant", "text": "Done with the task."})
    view._code_render_event({"kind": "result", "text": "Done with the task.", "notify": True})
    assert len(rows(view)) == 1
    assert "FINAL REPORT" not in text_of(rows(view)[0])


def test_result_without_streamed_text_is_plain_agent_prose(view):
    view._code_render_event({"kind": "result", "text": "Only completion message"})
    assert len(rows(view)) == 1
    assert "Only completion message" in text_of(rows(view)[0])
    assert "FINAL REPORT" not in text_of(rows(view)[0])


def test_native_provider_switch_is_a_dedicated_handoff_card(view):
    view._code_render_event({
        "kind": "provider_switch",
        "text": "Switched from Claude · sonnet to Codex · gpt-5.6-sol",
        "from_provider": "claude",
        "to_provider": "codex",
        "to_model": "gpt-5.6-sol",
        "native_continuation": False,
    })
    assert len(rows(view)) == 1
    rendered = text_of(rows(view)[0])
    assert "PROVIDER HANDOFF" in rendered
    assert "Claude" in rendered and "Codex" in rendered
    assert "New native provider session" in rendered


def test_mousewheel_binding_is_not_duplicated_and_consumes_text_scroll(view, root):
    view._code_add_message("A long agent response " * 80, "assistant")
    root.update_idletasks()
    widget = next(item for item in view._code_chat_text_widgets())
    view.code_chat._bind_wheel_tree(view.code_chat.inner)
    first_binding = widget.bind("<MouseWheel>")
    view.code_chat._bind_wheel_tree(view.code_chat.inner)
    assert widget.bind("<MouseWheel>") == first_binding

    event = type("WheelEvent", (), {"widget": widget, "delta": 120})()
    assert view.code_chat._mousewheel(event) == "break"
    assert view.code_auto_follow is False


def test_activity_key_folds_provider_phrasing_for_the_same_command():
    keys = {
        helper_overlay.code_activity_key({"kind": "tool", "text": "$ pytest -q"}),
        helper_overlay.code_activity_key({"kind": "tool", "text": "Running pytest -q"}),
        helper_overlay.code_activity_key({"kind": "tool", "text": "Ran   pytest -q"}),
    }
    assert len(keys) == 1
    assert helper_overlay.code_activity_key({"activity_id": "abc"}) == "abc"


def test_rounded_rect_points_clamp_radius_to_the_shape():
    points = helper_overlay.rounded_rect_points(0, 0, 100, 40, 10)
    assert len(points) == 24
    assert min(points[0::2]) == 0 and max(points[0::2]) == 100
    assert min(points[1::2]) == 0 and max(points[1::2]) == 40
    # A radius larger than half the box must not invert the outline.
    tiny = helper_overlay.rounded_rect_points(0, 0, 10, 6, 50)
    assert min(tiny[0::2]) == 0 and max(tiny[0::2]) == 10
    assert sorted(tiny[0::2])[:2] == [0, 0]


def test_activity_cards_are_borderless_rounded_and_track_content_height(root):
    view = build_view(root)

    view._code_upsert_activity({
        "kind": "activity", "activity_id": "t1", "activity_type": "files",
        "phase": "completed", "title": "Edited file", "detail": "app/main.py",
        "files": ["app/main.py"],
    })
    root.update_idletasks()
    card = view.code_activity_cards["t1"]
    canvas = card["frame"]

    # No border anywhere on the card chrome.
    assert int(canvas.cget("highlightthickness")) == 0
    assert int(canvas.cget("bd")) == 0
    assert int(card["inner"].cget("highlightthickness")) == 0

    # The rounded background is a smoothed polygon sized to the content.
    shapes = [item for item in canvas.find_all() if canvas.type(item) == "polygon"]
    assert len(shapes) == 1
    coords = canvas.coords(shapes[0])
    assert len(coords) == 24
    assert max(coords[1::2]) == pytest.approx(card["inner"].winfo_reqheight(), abs=1)
    assert int(canvas.cget("height")) == pytest.approx(card["inner"].winfo_reqheight(), abs=1)


def test_collapsed_tool_card_stays_on_a_single_compact_line(root):
    view = build_view(root)

    view._code_upsert_activity({
        "kind": "activity", "activity_id": "t2", "activity_type": "search",
        "phase": "completed", "title": "Searched code", "detail": "openrouter",
    })
    root.update_idletasks()
    card = view.code_activity_cards["t2"]
    header = card["header"]

    # Title and preview share one row, so the card stays near text height.
    assert card["title_var"].get() == "Searched code"
    assert card["preview_var"].get() == "openrouter"
    assert header.winfo_reqheight() <= 26
    assert card["frame"].winfo_reqheight() <= 34


def test_user_bubble_hugs_its_text_instead_of_spanning_the_chat(root):
    view = build_view(root)
    root.update_idletasks()

    view._code_add_user_bubble("short question")
    root.update_idletasks()
    canvas = view.code_chat.inner.winfo_children()[-1].winfo_children()[0]

    assert int(canvas.cget("highlightthickness")) == 0
    # Sized to the text, not stretched across the 620px chat column.
    assert 40 < canvas.winfo_reqwidth() < 300
    shapes = [item for item in canvas.find_all() if canvas.type(item) == "polygon"]
    assert len(shapes) == 1
    assert max(canvas.coords(shapes[0])[0::2]) == pytest.approx(canvas.winfo_reqwidth(), abs=1)


def test_edit_rows_show_green_added_and_red_deleted_counts(root):
    view = build_view(root)

    view._code_upsert_activity({
        "kind": "activity", "activity_id": "e1", "activity_type": "files",
        "phase": "completed", "title": "Edited file", "detail": "app/main.py",
        "lines_added": 12, "lines_deleted": 3,
    })
    root.update_idletasks()
    card = view.code_activity_cards["e1"]

    assert card["plus_var"].get() == "+12"
    assert card["minus_var"].get() == "-3"
    assert card["plus"].cget("fg") == view.c("success")
    assert card["minus"].cget("fg") == view.c("danger")
    # Both counters sit on the collapsed row, so the card stays one line tall.
    assert card["plus"].winfo_manager() == "pack"
    assert card["frame"].winfo_reqheight() <= 34


def test_rows_without_edits_show_no_diff_counters(root):
    view = build_view(root)

    view._code_upsert_activity({
        "kind": "activity", "activity_id": "r1", "activity_type": "read",
        "phase": "completed", "title": "Read file", "detail": "app/main.py",
    })
    root.update_idletasks()
    card = view.code_activity_cards["r1"]

    assert card["plus_var"].get() == ""
    assert card["minus_var"].get() == ""


def test_subagent_card_expands_into_brief_steps_and_report(root):
    view = build_view(root)

    view._code_upsert_activity({
        "kind": "activity", "activity_id": "sa1", "activity_type": "subagent",
        "phase": "completed", "title": "Scout-ORION", "agent_name": "Scout-ORION",
        "objective": "Map how login reaches validation",
        "output": "Read file · src/auth.py\nLocate symbol · validate",
        "summary": "MAP\nsrc/auth.py:2 - login\nANSWER\nlogin calls validate.",
    })
    root.update_idletasks()
    card = view.code_activity_cards["sa1"]

    assert card["title_var"].get() == "Scout-ORION"
    assert "subagent" in card["meta_var"].get()

    view._code_toggle_activity(card)
    root.update_idletasks()
    labels = []

    def walk(widget):
        for child in widget.winfo_children():
            try:
                text = child.cget("text")
            except tk.TclError:
                text = ""
            if text:
                labels.append(str(text))
            walk(child)

    walk(card["body"])
    joined = "\n".join(labels)
    assert "BRIEF" in joined
    assert "Map how login reaches validation" in joined
    assert "STEPS · 2" in joined
    assert "1. Read file · src/auth.py" in joined
    assert "REPORT" in joined


def test_chat_sticks_to_bottom_as_content_grows(root):
    view = build_view(root)
    view._code_set_auto_follow(True)

    for index in range(30):
        view._code_add_status(f"line {index}")
    view.code_chat.pin_to_bottom()
    root.update_idletasks()

    assert view.code_chat.stick_to_bottom is True
    assert view.code_chat.at_bottom()

    # Growth after the scroll must not push the viewport back up: this is the
    # jump that used to hide the live "working" row.
    view._code_add_status("newest line")
    root.update_idletasks()
    assert view.code_chat.at_bottom()


def test_scrolling_up_stops_following_and_shows_the_latest_button(root):
    view = build_view(root)
    view.code_jump_button = tk.Button(view.code_chat.master, text="Latest")
    for index in range(40):
        view._code_add_status(f"line {index}")
    root.update_idletasks()

    view._code_chat_user_scrolled(wheel_delta=120)   # wheel up
    root.update_idletasks()

    assert view.code_auto_follow is False
    assert view.code_chat.stick_to_bottom is False
    assert view.code_jump_button.winfo_manager() == "place"

    view._code_jump_to_latest()
    root.update_idletasks()

    assert view.code_auto_follow is True
    assert view.code_chat.stick_to_bottom is True
    assert view.code_jump_button.winfo_manager() == ""


def test_thinking_renders_as_inline_italic_text_not_a_card(root):
    view = build_view(root)

    view._code_render_event({"kind": "activity", "activity_id": "th1", "activity_type": "thinking",
                             "phase": "update", "summary": "Checking how login reaches validation"})
    view._code_render_event({"kind": "activity", "activity_id": "th1", "activity_type": "thinking",
                             "phase": "update", "delta": "then I will edit."})
    root.update_idletasks()

    assert "th1" not in view.code_activity_cards       # never becomes a tool card
    label = view.code_thinking["label"]
    assert "Checking how login reaches validation" in label.cget("text")
    assert "then I will edit." in label.cget("text")
    font = str(label.cget("font"))
    assert "italic" in font
    assert label.cget("fg") != view.c("text")          # dimmed, not body copy

    view._code_render_event({"kind": "activity", "activity_id": "th1", "activity_type": "thinking",
                             "phase": "completed", "summary": ""})
    assert view.code_thinking is None


def test_composer_switches_between_launch_and_follow_up(root):
    view = build_view(root)
    view.code_selected_id = ""
    view.code_target_var = tk.StringVar()
    view.code_send_label = tk.StringVar()
    view.code_project_var = tk.StringVar(value=r"C:\work\myrepo")
    view.code_provider_var = tk.StringVar(value="openrouter")
    view.code_model_var = tk.StringVar(value="deepseek/deepseek-v4-flash")
    view.code_send_button = tk.Button(root)
    view.code_new_session_button = tk.Label(root)
    view.code_urgent_check = tk.Checkbutton(root)

    view._code_sync_compose_target()
    assert "New session in myrepo" in view.code_target_var.get()
    assert view.code_send_label.get() == "Launch ▸"

    view.code_selected_id = "abc123"
    view._code_sync_compose_target({"provider": "openrouter", "model": "deepseek/deepseek-v4-flash",
                                    "project_name": "myrepo", "cwd": r"C:\work\myrepo"})
    assert view.code_target_var.get().startswith("↩ myrepo")
    assert view.code_send_label.get() == "Send ▸"


def test_target_menu_builds_from_the_three_field_provider_table(root, monkeypatch):
    """CODE_PROVIDER_CHOICES rows are (id, icon, label); unpacking two raised
    and silently killed every click on the composer strip."""
    view = build_view(root)
    view.config = dict(helper_overlay.DEFAULT_CONFIG)
    view.code_selected_id = ""
    view.code_project_var = tk.StringVar(value=r"C:\work\repo")
    view.code_provider_var = tk.StringVar(value="openrouter")
    view.code_model_var = tk.StringVar(value="m1")
    view.code_projects = [{"name": "repo", "path": r"C:\work\repo"}]
    view.code_capabilities = {"providers": [
        {"provider": "openrouter", "ready": True, "models": [{"id": "m1", "label": "M1"}]}]}
    view.code_target_button = tk.Label(root, text="target")
    view.code_target_button.pack()
    root.update_idletasks()
    monkeypatch.setattr(tk.Menu, "tk_popup", lambda self, *a, **k: None)

    view._code_open_target_menu()     # must not raise

    assert len(helper_overlay.CODE_PROVIDER_CHOICES[0]) == 3


def test_new_session_clears_the_open_transcript_and_selection(root):
    view = build_view(root)
    view.code_selected_id = "old-job"
    view.code_log_size = 4200
    view.code_target_var = tk.StringVar()
    view.code_send_label = tk.StringVar()
    view.code_project_var = tk.StringVar(value=r"C:\work\repo")
    view.code_provider_var = tk.StringVar(value="openrouter")
    view.code_model_var = tk.StringVar(value="m1")
    view.code_send_button = tk.Button(root)
    view.code_new_session_button = tk.Label(root)
    view.code_urgent_check = tk.Checkbutton(root)
    view.code_detail_title_var = tk.StringVar()
    view.code_detail_meta_var = tk.StringVar()
    view.code_sessions_frame = helper_overlay.ScrollFrame(root, view.c("surface"))
    view.code_jobs = []
    view.code_followup = tk.Text(root)
    view.code_selected_id = "old-job"
    view._code_add_status("stale line from the previous session")
    view._code_set_busy(True)
    root.update_idletasks()

    view._code_new_session()
    root.update_idletasks()

    assert view.code_selected_id == ""
    assert view.code_log_size == 0
    assert view.code_busy_row is None            # no leftover "working" spinner
    assert rows(view) == []                      # old transcript is gone
    assert view.code_detail_title_var.get() == "New session"
    assert view.code_send_label.get() == "Launch ▸"
