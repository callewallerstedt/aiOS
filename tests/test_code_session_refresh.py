"""Regression coverage for the native CODE session-list refresh lifecycle."""

import threading
import time

import helper_overlay


class ImmediateRoot:
    def __init__(self):
        self.timers = []

    def after(self, delay, callback):
        if delay == 0:
            callback()
        else:
            self.timers.append((delay, callback))
        return len(self.timers)


def _refresh_view():
    view = object.__new__(helper_overlay.HelperOverlay)
    view.root = ImmediateRoot()
    view.active_tab = "CODE"
    view.code_view_token = 7
    view.code_selected_id = ""
    view.code_log_size = 0
    view.code_jobs = []
    view.code_capabilities = {"providers": []}
    view._code_refresh_seq = 0
    view._code_refresh_inflight = None
    view._code_capabilities_busy = False
    view._code_render_summary = lambda: None
    view._code_render_sessions = lambda: None
    view._code_refresh_selectors = lambda: None
    return view


def test_slow_capability_discovery_does_not_block_saved_sessions():
    view = _refresh_view()
    jobs_ready = threading.Event()

    def api(path, **_kwargs):
        if path.startswith("/api/code/capabilities"):
            time.sleep(0.3)
            return {"ok": True, "providers": []}
        jobs_ready.set()
        return {"ok": True, "jobs": [{"id": "saved-session"}]}

    view._code_api = api
    started = time.monotonic()
    view._code_refresh_all()
    assert jobs_ready.wait(0.15)
    deadline = time.monotonic() + 0.15
    while not view.code_jobs and time.monotonic() < deadline:
        time.sleep(0.005)
    assert view.code_jobs == [{"id": "saved-session"}]
    assert time.monotonic() - started < 0.25


def test_stale_refresh_cannot_clear_or_overwrite_the_new_view_request():
    view = _refresh_view()
    view._code_refresh_inflight = {"id": 22, "scope": (7, "", 0)}
    view.code_jobs = [{"id": "current"}]

    view._code_apply_refresh(
        6,
        21,
        "",
        {"ok": True, "jobs": [{"id": "stale"}]},
        None,
        0,
    )

    assert view._code_refresh_inflight["id"] == 22
    assert view.code_jobs == [{"id": "current"}]


def test_notification_poll_prefetches_sessions_before_code_is_open():
    view = _refresh_view()
    view.active_tab = "Dashboard"
    view.code_notify_busy = True
    view.code_notify_offsets = {}
    view.config = {"code_speak_notifications": False}

    jobs = [{"id": "prefetched"}]
    view._code_apply_notifications({}, [], jobs)

    assert view.code_jobs == jobs
    assert view.code_notify_busy is False


def test_opening_a_long_session_only_rebuilds_the_tail(monkeypatch):
    """Replaying thousands of events blocks the Tk thread for many seconds."""
    import helper_overlay

    view = object.__new__(helper_overlay.HelperOverlay)
    view.active_tab = "CODE"
    view.code_view_token = 1
    view.code_selected_id = "job-1"
    view.code_jobs = []
    view.code_log_size = 0
    view._code_refresh_inflight = {"id": 7, "scope": None}
    rendered = []
    notes = []

    monkeypatch.setattr(helper_overlay.HelperOverlay, "_code_chat_reset", lambda self: None)
    monkeypatch.setattr(helper_overlay.HelperOverlay, "_code_set_auto_follow", lambda self, value: None)
    monkeypatch.setattr(helper_overlay.HelperOverlay, "_code_chat_scroll_end", lambda self, force=False: None)
    monkeypatch.setattr(helper_overlay.HelperOverlay, "_code_render_detail_meta", lambda self, job: None)
    monkeypatch.setattr(helper_overlay.HelperOverlay, "_code_render_event",
                        lambda self, event, live=False: rendered.append(event))
    monkeypatch.setattr(helper_overlay.HelperOverlay, "_code_add_status",
                        lambda self, text: notes.append(text))

    class Root:
        def after(self, *_args):
            return None

    view.root = Root()
    limit = helper_overlay.CODE_HISTORY_RENDER_LIMIT
    events = [{"kind": "status", "text": f"e{i}"} for i in range(limit + 400)]

    view._code_apply_refresh(1, 7, "job-1", None,
                             {"ok": True, "events": events, "size": len(events)}, 0)

    assert len(rendered) == limit
    assert rendered[-1]["text"] == f"e{limit + 399}"     # the newest output survives
    assert notes and "earlier events hidden" in notes[0]


def test_short_sessions_render_completely(monkeypatch):
    import helper_overlay

    view = object.__new__(helper_overlay.HelperOverlay)
    view.active_tab = "CODE"
    view.code_view_token = 1
    view.code_selected_id = "job-2"
    view.code_jobs = []
    view.code_log_size = 0
    view._code_refresh_inflight = {"id": 3, "scope": None}
    rendered = []
    notes = []

    monkeypatch.setattr(helper_overlay.HelperOverlay, "_code_chat_reset", lambda self: None)
    monkeypatch.setattr(helper_overlay.HelperOverlay, "_code_set_auto_follow", lambda self, value: None)
    monkeypatch.setattr(helper_overlay.HelperOverlay, "_code_chat_scroll_end", lambda self, force=False: None)
    monkeypatch.setattr(helper_overlay.HelperOverlay, "_code_render_detail_meta", lambda self, job: None)
    monkeypatch.setattr(helper_overlay.HelperOverlay, "_code_render_event",
                        lambda self, event, live=False: rendered.append(event))
    monkeypatch.setattr(helper_overlay.HelperOverlay, "_code_add_status",
                        lambda self, text: notes.append(text))

    class Root:
        def after(self, *_args):
            return None

    view.root = Root()
    events = [{"kind": "status", "text": f"e{i}"} for i in range(12)]

    view._code_apply_refresh(1, 3, "job-2", None,
                             {"ok": True, "events": events, "size": 12}, 0)

    assert len(rendered) == 12
    assert not notes


def test_detail_view_trusts_the_listing_status_over_the_raw_job_file(monkeypatch):
    """After an aiOS restart the job file still says 'running'; the listing
    knows better, and the transcript must not keep spinning 'working'."""
    import helper_overlay

    view = object.__new__(helper_overlay.HelperOverlay)
    view.active_tab = "CODE"
    view.code_view_token = 1
    view.code_selected_id = "job-9"
    view.code_log_size = 0
    view._code_refresh_inflight = {"id": 5, "scope": None}
    shown = {}

    monkeypatch.setattr(helper_overlay.HelperOverlay, "_code_chat_reset", lambda self: None)
    monkeypatch.setattr(helper_overlay.HelperOverlay, "_code_set_auto_follow", lambda self, v: None)
    monkeypatch.setattr(helper_overlay.HelperOverlay, "_code_chat_scroll_end", lambda self, force=False: None)
    monkeypatch.setattr(helper_overlay.HelperOverlay, "_code_render_event", lambda self, e, live=False: None)
    monkeypatch.setattr(helper_overlay.HelperOverlay, "_code_add_status", lambda self, t: None)
    monkeypatch.setattr(helper_overlay.HelperOverlay, "_code_render_summary", lambda self: None)
    monkeypatch.setattr(helper_overlay.HelperOverlay, "_code_render_sessions", lambda self: None)
    monkeypatch.setattr(helper_overlay.HelperOverlay, "_code_render_detail_meta",
                        lambda self, job: shown.update(job))

    class Root:
        def after(self, *_args):
            return None

    view.root = Root()
    listing = {"ok": True, "jobs": [{"id": "job-9", "status": "interrupted", "title": "t"}]}
    log = {"ok": True, "events": [], "size": 3, "job": {"id": "job-9", "status": "running", "title": "t"}}

    view._code_apply_refresh(1, 5, "job-9", listing, log, 0)

    assert shown["status"] == "interrupted"
