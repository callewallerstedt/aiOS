"""Pure helpers mirroring phone.js send-lifecycle rules.

Keeps the "new task in an old thread" decision table from regressing without
needing a browser. The JS `treatAsFollowUp` / notePrompt reuse rules must stay
aligned with these cases.
"""


def treat_as_follow_up(*, running, force_new_prompt, viewing_run_id, live_run):
    return bool(
        running
        and not force_new_prompt
        and not viewing_run_id
        and live_run
        and live_run.get("status") == "open"
    )


def should_reuse_live_run(*, as_follow_up, live_run):
    return bool(as_follow_up and live_run and live_run.get("status") == "open")


def test_idle_old_thread_is_a_new_prompt():
    # Finished conversation still on screen — liveRun closed/null.
    assert not treat_as_follow_up(
        running=False, force_new_prompt=False, viewing_run_id="", live_run=None
    )
    assert not should_reuse_live_run(as_follow_up=False, live_run=None)


def test_history_view_is_never_a_follow_up_even_if_status_looks_busy():
    assert not treat_as_follow_up(
        running=True,
        force_new_prompt=False,
        viewing_run_id="r_old",
        live_run={"status": "open"},
    )


def test_active_open_run_is_a_follow_up():
    live = {"status": "open"}
    assert treat_as_follow_up(
        running=True, force_new_prompt=False, viewing_run_id="", live_run=live
    )
    assert should_reuse_live_run(as_follow_up=True, live_run=live)


def test_stale_open_run_while_idle_starts_fresh():
    live = {"status": "open"}
    assert not treat_as_follow_up(
        running=False, force_new_prompt=False, viewing_run_id="", live_run=live
    )
    assert not should_reuse_live_run(as_follow_up=False, live_run=live)


def test_new_chat_plus_forces_prompt():
    assert not treat_as_follow_up(
        running=True,
        force_new_prompt=True,
        viewing_run_id="",
        live_run={"status": "open"},
    )
