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
