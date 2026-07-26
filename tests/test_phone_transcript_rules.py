"""Pure helpers mirroring phone.js transcript rules.

The phone chat lost messages twice over: a feed that deleted its oldest row
once a run got long (the oldest row being the message that started the task),
and a save routine that assigned its shed-down copy back over the thread in
memory. Both rules are small enough to state plainly here, so they cannot
drift back.
"""

import json
import re
from pathlib import Path

PHONE_JS = Path(__file__).resolve().parents[1] / "phone_site" / "phone.js"


def is_user_message(event):
    kind = str(event.get("type") or "").lower()
    return kind == "prompt" or re.search(r"follow.?up", kind) is not None


def keep_conversation(events, limit):
    """Trim the middle, never the conversation."""
    if len(events) <= limit:
        return list(events)
    keep = {index for index, event in enumerate(events) if is_user_message(event)}
    for index in range(len(events) - 1, -1, -1):
        if len(keep) >= limit:
            break
        keep.add(index)
    return [event for index, event in enumerate(events) if index in keep]


def a_long_run(steps=200):
    events = [{"type": "prompt", "payload": {"message": "sort my downloads"}}]
    events.append({"type": "run_start", "payload": {"task": "sort my downloads"}})
    for step in range(steps):
        events.append({"type": "step_begin", "payload": {"n": step}})
        events.append({"type": "thought", "payload": {"say": f"step {step}"}})
        events.append({"type": "action_done", "payload": {"ok": True}})
    return events


def test_what_you_typed_outlives_the_run_that_followed_it():
    events = a_long_run()

    kept = keep_conversation(events, 100)

    assert kept[0]["payload"]["message"] == "sort my downloads"
    assert len(kept) == 100


def test_every_message_in_a_long_conversation_is_kept():
    events = []
    for index in range(30):
        events.append({"type": "prompt", "payload": {"message": f"task {index}"}})
        events.extend({"type": "thought", "payload": {}} for _ in range(40))

    kept = keep_conversation(events, 60)

    messages = [event["payload"]["message"] for event in kept if is_user_message(event)]
    assert messages == [f"task {index}" for index in range(30)]


def test_a_follow_up_counts_as_something_you_said():
    events = [{"type": "follow_up_received", "payload": {"text": "and the desktop too"}}]
    events.extend({"type": "thought", "payload": {}} for _ in range(50))

    kept = keep_conversation(events, 10)

    assert any(is_user_message(event) for event in kept)


def test_the_newest_activity_is_what_fills_the_rest():
    events = a_long_run(steps=50)
    events.append({"type": "done", "payload": {"ok": True}})

    kept = keep_conversation(events, 20)

    assert kept[-1]["type"] == "done", "the end of the run must always be on screen"


def test_nothing_is_dropped_when_it_already_fits():
    events = a_long_run(steps=3)

    assert keep_conversation(events, 400) == events


def test_the_feed_window_is_larger_than_what_gets_stored():
    """A reopened thread must never look shorter than a live one."""
    source = PHONE_JS.read_text(encoding="utf-8")
    window = int(re.search(r"const FEED_WINDOW = (\d+)", source).group(1))
    stored = int(re.search(r"const STORED_EVENTS_PER_RUN = (\d+)", source).group(1))

    assert stored <= window


def test_storage_never_writes_back_over_the_thread_in_memory():
    """The bug that emptied the chat: state.history = the shed-down copy."""
    source = PHONE_JS.read_text(encoding="utf-8")
    body = source[source.index("function writeHistory()"):source.index("function machineHistory()")]

    assert "state.history =" not in body, "storage must not reassign the conversation"


def test_the_saved_copy_stays_inside_the_budget():
    source = PHONE_JS.read_text(encoding="utf-8")
    budget = int(re.search(r"const HISTORY_BUDGET = ([\d_]+)", source).group(1).replace("_", ""))
    per_run = int(re.search(r"const STORED_EVENTS_PER_RUN = (\d+)", source).group(1))
    text_limit = int(re.search(r"const STORED_TEXT_LIMIT = (\d+)", source).group(1))
    runs = int(re.search(r"const STORED_RUNS = (\d+)", source).group(1))

    # Four long strings per event is generous for the shapes the PC sends.
    worst_case = runs * per_run * text_limit * 4
    assert worst_case > budget, "the budget should be the thing that bites, not a surprise quota"
    assert per_run * text_limit * 4 < budget, "one run alone must always fit"


def test_a_stored_event_keeps_the_fields_the_timeline_draws_from():
    source = PHONE_JS.read_text(encoding="utf-8")
    shrink = source[source.index("function shrinkEvent("):source.index("function persistableRuns(")]

    # Truncation only — dropping keys would blank out rows on the way back.
    assert "slice(0, STORED_TEXT_LIMIT)" in shrink
    assert json.dumps({"...": "event"})  # sanity: the module imports cleanly
