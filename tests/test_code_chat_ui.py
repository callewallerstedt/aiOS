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
