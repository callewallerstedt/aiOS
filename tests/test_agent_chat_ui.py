import copy
import ctypes
import json
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

import pytest

import helper_overlay
import voice_agent
import voice_dictation


def _root():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    return root


def _mic_overlay():
    try:
        return voice_dictation.MicOverlay()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")


def _chat_overlay():
    app = helper_overlay.HelperOverlay.__new__(helper_overlay.HelperOverlay)
    app.root = _root()
    app.config = copy.deepcopy(helper_overlay.DEFAULT_CONFIG)
    app.theme = app.config["theme"]
    app.brand_font_family = "Segoe UI"
    app.project_root = Path(__file__).resolve().parents[1]
    app.history = []
    app.chat_inner = tk.Frame(app.root, bg=app.c("surface"))
    app.chat_inner.pack()
    app.chat_canvas = tk.Canvas(app.root, width=420, height=500, bg=app.c("surface"))
    app.chat_canvas.pack()
    app._chat_canvas_window = app.chat_canvas.create_window(
        (0, 0), window=app.chat_inner, anchor="nw"
    )
    app._chat_embeds = []
    app._live_turn_col = None
    app._live_tools_box = None
    app._live_tool_count = 0
    app._agent_turn_active = False
    app._assistant_bg = app.blend_color(app.c("surface"), app.c("text"), 0.08)
    app._user_bubble_bg = app.blend_color(app.c("surface"), app.c("accent"), 0.22)
    app.thinking_after = None
    app.thinking_step = 0
    app.thinking_frame = None
    app.thinking_canvas = None
    app.thinking_label = None
    app.thinking_status_text = "Thinking"
    app._chat_scroll_after = None
    app.busy = True
    app.chat_busy_since = 1
    app.send_button = tk.Button(app.root)
    app.subtitle = tk.Label(app.root)

    def add_history(role, text, tools=None):
        entry = {"role": role, "text": text}
        if tools:
            entry["tools"] = tools
        app.history.append(entry)

    app.add_history = add_history
    return app


def _widget_texts(parent):
    texts = []
    for child in parent.winfo_children():
        try:
            text = child.cget("text")
        except tk.TclError:
            text = ""
        if text:
            texts.append(str(text))
        texts.extend(_widget_texts(child))
    return texts


def _text_widget_payloads(parent):
    payloads = []
    for child in parent.winfo_children():
        if isinstance(child, tk.Text):
            payloads.append(child.get("1.0", "end-1c"))
        payloads.extend(_text_widget_payloads(child))
    return payloads


def test_dictation_waveform_is_grey_when_idle_and_accent_when_recording():
    class Canvas:
        def create_oval(self, *_args, **_kwargs):
            return 1

        def create_text(self, *_args, **_kwargs):
            return 1

    overlay = voice_dictation.MicOverlay.__new__(voice_dictation.MicOverlay)
    overlay.canvas = Canvas()
    overlay.status_text = "ready"
    overlay._last_level_at = 0.0
    overlay.danger = "#ff0000"
    overlay.muted = "#777777"
    overlay.accent = "#cc2233"
    overlay.panel_bg = "#111111"
    overlay.compose_target = "cursor"
    overlay.recording = False
    overlay.pulse = 0.0
    overlay.level = 0.0
    overlay.font_pill = None
    overlay.blend_color = lambda _background, foreground, _amount: foreground
    colors = []
    overlay._draw_wave = lambda _x0, _x1, _cy, color, _height: colors.append(color)
    overlay.set_recording(False)
    overlay._draw_dictation(0, 0, 236, 52)
    assert colors[-1] == overlay.muted

    overlay.set_recording(True)
    overlay._draw_dictation(0, 0, 236, 52)
    assert colors[-1] == overlay.accent


def test_timed_agent_bubble_accepts_elapsed_footer():
    app = _chat_overlay()
    try:
        bubble = app._make_bubble(app.chat_inner, "The real reply", meta="600 ms")
        bubble.pack()
        app.root.update_idletasks()
        assert "The real reply" in _widget_texts(app.chat_inner)
        assert "600 ms" in _widget_texts(app.chat_inner)
    finally:
        app.root.destroy()


def test_voice_log_keeps_reply_with_elapsed_time_and_tool_card():
    app = _chat_overlay()
    long_argument = "argument-start-" + ("x" * 700) + "-argument-end"
    long_output = "result-start-" + ("y" * 5000) + "-result-end"
    tool = {
        "name": "scroll",
        "label": "Scrolled screen",
        "summary": "scroll distance 0.6",
        "arguments": {"distance": "0.6", "request": long_argument},
        "output": long_output,
        "call_id": "call-exact-123",
        "ok": True,
    }
    try:
        app._remote_voice_event("", {"kind": "turn_start", "echo_user": False})
        app._remote_voice_event(
            "Scrolled screen",
            {"kind": "tool_done", "tool": tool, "echo_user": False},
        )
        app._remote_voice_event(
            "Scrolled screen",
            {"kind": "tool_done", "tool": tool, "echo_user": False},
        )
        app._remote_voice_log(
            "diagnostic",
            {
                "reply": "AIOS_CHAT_RENDER_OK",
                "tool_details": [tool],
                "tools": ["scroll"],
                "echo_user": False,
                "elapsed": 0.6,
            },
        )
        app.root.update_idletasks()
        texts = _widget_texts(app.chat_inner)
        assert "AIOS_CHAT_RENDER_OK" in texts
        assert not any("chat update failed" in text for text in texts)
        assert app.history[-1]["text"] == "AIOS_CHAT_RENDER_OK"
        inspectors = "\n".join(_text_widget_payloads(app.chat_inner))
        assert "argument-end" in inspectors
        assert "result-end" in inspectors
        assert "call-exact-123" in inspectors
        assert inspectors.count("call-exact-123") == 1
        assert app._live_tool_count == 1
    finally:
        app.root.destroy()


def test_voice_reply_stream_grows_one_sidebar_bubble_then_finalizes_once():
    app = _chat_overlay()
    try:
        app._remote_voice_event("", {"kind": "turn_start", "echo_user": False})
        app._remote_voice_event("", {"kind": "reply_start", "echo_user": False})
        app._remote_voice_event("Hello", {"kind": "reply_delta", "echo_user": False})
        app._remote_voice_event(" there", {"kind": "reply_delta", "echo_user": False})
        app.root.update_idletasks()
        assert app._stream_reply_var.get() == "Hello there"

        app._remote_voice_event("Hello there", {"kind": "reply_done", "echo_user": False})
        app._remote_voice_log(
            "diagnostic",
            {"reply": "Hello there", "echo_user": False, "elapsed": 0.2},
        )
        app.root.update_idletasks()
        assert [item["text"] for item in app.history] == ["Hello there"]
        assert app._stream_reply_frame is None
    finally:
        app.root.destroy()


def test_responses_api_text_deltas_are_forwarded_in_order():
    class Event:
        type = "response.output_text.delta"

        def __init__(self, delta):
            self.delta = delta

    class Response:
        id = "resp_test"
        output_text = "Hello there"
        output = []

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            return iter([Event("Hello"), Event(" there")])

        def get_final_response(self):
            return Response()

    class Responses:
        def stream(self, **_kwargs):
            return Stream()

    class Client:
        responses = Responses()

    events = []
    agent = voice_agent.VoiceAgent(on_event=lambda kind, payload: events.append((kind, payload)))
    response, text, started = agent._stream_response(Client(), {"model": "test"})
    assert response.output_text == "Hello there"
    assert text == "Hello there"
    assert started
    assert events == [
        ("reply_start", ""),
        ("reply_delta", "Hello there"),
    ]


def test_agent_can_hide_overlay_without_clearing_conversation():
    hidden = []
    agent = voice_agent.VoiceAgent(hide_overlay=lambda reply="": hidden.append(reply))

    tools = agent._tools({
        "agent_web_search": False,
        "agent_open_apps": False,
        "agent_shell": False,
        "agent_operator": False,
    })
    hide_tool = next(tool for tool in tools if tool.get("name") == "hide_overlay")
    output = agent._execute("hide_overlay", {})

    assert hide_tool["parameters"]["additionalProperties"] is False
    assert hidden == []
    assert agent._hide_requested is True
    assert output == "overlay will hide after your short spoken sign-off; conversation memory is preserved"
    agent._finish_overlay_hide("Alright, thank you. Goodbye.")
    assert hidden == ["Alright, thank you. Goodbye."]
    assert agent._tool_label("hide_overlay", {}) == "hiding the overlay"


def test_operator_hud_contains_only_current_thought_and_last_action():
    app = helper_overlay.HelperOverlay.__new__(helper_overlay.HelperOverlay)
    app.agent_operator_overlay_thought = "I need to open Settings and inspect the Network page."
    app.agent_operator_overlay_action = "click: clicked Settings"
    app.agent_operator_log_buffer = [("dim", "old noisy debug output that must not leak")]

    text = app._agent_operator_overlay_log_text()

    assert text == (
        "THINKING\nI need to open Settings and inspect the Network page.\n"
        "DID\nclick: clicked Settings"
    )
    assert "old noisy debug" not in text


def test_operator_tool_waits_for_terminal_result(monkeypatch, tmp_path):
    events_path = tmp_path / "events.jsonl"
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(voice_agent, "OPERATOR_EVENTS_PATH", events_path)
    monkeypatch.setattr(voice_agent, "OPERATOR_STATUS_PATH", status_path)
    monkeypatch.setattr(voice_agent, "OPERATOR_WAIT_SECONDS", 1.0)
    monkeypatch.setattr(voice_agent, "OPERATOR_POLL_SECONDS", 0.01)
    started = time.time()

    def finish_operator():
        time.sleep(0.04)
        rows = [
            {"type": "run_start", "ts": started + 0.01, "task": "Open Settings"},
            {
                "type": "done",
                "ts": started + 0.02,
                "ok": True,
                "verified": True,
                "steps": 3,
                "message": "Opened Settings and confirmed the page.",
            },
        ]
        events_path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )

    worker = threading.Thread(target=finish_operator, daemon=True)
    worker.start()
    output = voice_agent.VoiceAgent()._wait_for_operator(
        task="Open Settings",
        after_ts=started,
        require_run_start=True,
    )
    worker.join(timeout=1)
    result = json.loads(output)
    assert result["state"] == "completed"
    assert result["ok"] is True
    assert result["verified"] is True
    assert result["message"] == "Opened Settings and confirmed the page."


def test_web_search_detail_keeps_queries_sources_and_results():
    class SearchCall:
        def model_dump(self, **_kwargs):
            return {
                "id": "ws_123",
                "type": "web_search_call",
                "status": "completed",
                "action": {
                    "type": "search",
                    "queries": ["full search query"],
                    "sources": [{"type": "url", "url": "https://example.com/source"}],
                },
                "results": [{"title": "Exact result", "url": "https://example.com/result"}],
            }

    detail = voice_agent.VoiceAgent._web_search_tool_detail(SearchCall())
    assert detail["arguments"]["queries"] == ["full search query"]
    assert "https://example.com/source" in detail["output"]
    assert "Exact result" in detail["output"]


def test_voice_bridge_does_not_truncate_tool_details():
    tail = "z" * 6000 + "THE_END"
    details = voice_dictation.Dictation._transport_tool_details(
        [{"name": "run", "arguments": {"payload": tail}, "output": tail}]
    )
    assert details[0]["arguments"]["payload"].endswith("THE_END")
    assert details[0]["output"].endswith("THE_END")


def test_gui_chat_scrolls_to_newest_bubble_after_layout_settles():
    app = _chat_overlay()
    try:
        for index in range(30):
            app.append_assistant_message(
                f"message {index} " + "content " * 20,
                trim=False,
            )
        app.root.update()
        assert app.chat_canvas.yview()[1] == pytest.approx(1.0)
    finally:
        app.root.destroy()


def test_agent_overlay_grows_up_and_keeps_newest_messages_visible():
    overlay = _mic_overlay()
    try:
        overlay._pill_anchor = (500.0, 900.0)
        _, short_y, _, short_bottom = overlay._compute_dictation_placement(
            overlay.pill_size[0], overlay.pill_size[1]
        )
        assert short_y + overlay.pill_size[1] == pytest.approx(short_bottom)

        overlay.set_target("agent")
        overlay.set_history(
            [("assistant", f"OLD_{index} " + "message " * 18) for index in range(20)]
        )
        overlay.compose_open = True
        ops, panel_height = overlay._compose_build()
        rendered = " ".join(
            line
            for op in ops
            if op[0] == "bubble"
            for line in op[6]
        )

        total_height = panel_height + overlay.compose_gap + overlay.pill_size[1]
        _, tall_y, _, tall_bottom = overlay._compute_dictation_placement(
            overlay.compose_width, total_height
        )
        assert tall_y + total_height == pytest.approx(tall_bottom)
        assert tall_bottom == pytest.approx(short_bottom)
        assert 430 < panel_height <= voice_dictation.COMPOSE_MAX_HEIGHT
        assert "OLD_19" in rendered
        assert "OLD_0" not in rendered
    finally:
        overlay.root.destroy()


def test_cursor_mode_hides_agent_chat_and_long_transcript_is_not_cut_off():
    overlay = _mic_overlay()
    try:
        overlay.show_finished_turn(
            [("user", "OLD AGENT QUESTION"), ("assistant", "OLD AGENT ANSWER")]
        )
        overlay.set_target("cursor")
        transcript = "TRANSCRIPT_START " + " ".join(f"word{index}" for index in range(240)) + " TRANSCRIPT_END"
        overlay.set_transcript(transcript)
        ops, panel_height = overlay._compose_build()
        rendered = " ".join(
            line
            for op in ops
            if op[0] == "bubble"
            for line in op[6]
        )
        assert ops[0][0] == "chip" and ops[0][3] == "TO CURSOR"
        assert "OLD AGENT QUESTION" not in rendered
        assert "OLD AGENT ANSWER" not in rendered
        assert "TRANSCRIPT_START" in rendered
        assert "TRANSCRIPT_END" in rendered
        assert not rendered.endswith("…")
        assert panel_height > 560
        assert panel_height <= overlay._compose_height_limit()
        assert overlay.compose_history[-1] == ("assistant", "OLD AGENT ANSWER")
    finally:
        overlay.root.destroy()


def test_clipboard_button_maps_agent_mode_back_to_clean_cursor_overlay():
    overlay = _mic_overlay()
    controller = voice_dictation.Dictation.__new__(voice_dictation.Dictation)
    try:
        overlay._visible = True
        overlay.show = lambda: None
        overlay.show_finished_turn(
            [("user", "PRESERVED QUESTION"), ("assistant", "PRESERVED ANSWER")]
        )
        overlay.set_target("agent")
        controller.overlay = overlay
        controller.target = "agent"
        controller.active = True
        controller._dispatch_pending = False
        controller._target_set_at = 0.0
        controller.transcript_parts = []
        controller.ui = lambda fn, *args: fn(*args)
        controller._cancel_linger = lambda: None

        controller.set_target("clipboard")
        assert controller.target == "cursor"
        assert overlay.compose_target == "cursor"
        assert overlay._compose_build()[1] == 0
        assert overlay.compose_history[-1] == ("assistant", "PRESERVED ANSWER")
    finally:
        overlay.root.destroy()


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows capture affinity")
def test_dictation_overlay_is_visible_normally_but_hidden_during_agent_capture():
    from agent_clicker.desktop_agent.screen import current_process_windows_hidden_from_agent_capture

    overlay = _mic_overlay()
    try:
        overlay.root.deiconify()
        overlay.root.update()
        assert overlay._allow_normal_capture()
        affinity = ctypes.c_uint(0)
        ok = ctypes.windll.user32.GetWindowDisplayAffinity(
            overlay._overlay_hwnd(), ctypes.byref(affinity)
        )
        assert ok
        assert affinity.value == voice_dictation.WDA_NONE

        with current_process_windows_hidden_from_agent_capture(settle_seconds=0):
            ok = ctypes.windll.user32.GetWindowDisplayAffinity(
                overlay._overlay_hwnd(), ctypes.byref(affinity)
            )
            assert ok
            assert affinity.value in {
                voice_dictation.WDA_EXCLUDEFROMCAPTURE,
                voice_dictation.WDA_MONITOR,
            }

        ok = ctypes.windll.user32.GetWindowDisplayAffinity(
            overlay._overlay_hwnd(), ctypes.byref(affinity)
        )
        assert ok
        assert affinity.value == voice_dictation.WDA_NONE
    finally:
        overlay.root.withdraw()
        overlay.root.destroy()


def test_remote_agent_capture_lease_restores_visibility_after_timeout():
    app = helper_overlay.HelperOverlay.__new__(helper_overlay.HelperOverlay)
    callbacks = []
    events = []
    app.root = type("Root", (), {"after": lambda _self, ms, fn: callbacks.append((ms, fn))})()
    app._agent_capture_affinity_begin = lambda token: events.append(("begin", token))
    app._agent_capture_affinity_end = lambda token: events.append(("end", token))

    app._remote_agent_capture_begin("capture-1")

    assert events == [("begin", "capture-1")]
    assert callbacks[0][0] == 5000
    callbacks[0][1]()
    assert events[-1] == ("end", "capture-1")
