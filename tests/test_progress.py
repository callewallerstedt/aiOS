import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent_clicker"))

from desktop_agent.progress import (  # noqa: E402
    LoopWatch, action_signature, checklist_block, frame_change, frame_fingerprint,
    frame_moved, parse_plan,
)


def screen(fill=(20, 20, 20), box=None):
    image = Image.new("RGB", (400, 300), fill)
    if box:
        ImageDraw.Draw(image).rectangle(box, fill=(230, 230, 230))
    return image


def click(x, y):
    return [{"type": "click", "x": x, "y": y, "button": "left"}]


def test_a_click_two_pixels_over_is_the_same_click():
    assert action_signature(click(500, 400)) == action_signature(click(502, 398))
    assert action_signature(click(500, 400)) != action_signature(click(900, 400))


def test_typing_different_text_is_a_different_action():
    assert action_signature([{"type": "type", "text": "hello"}]) \
        != action_signature([{"type": "type", "text": "goodbye"}])


def test_an_empty_step_has_no_signature():
    assert action_signature([]) == "none"


def test_a_still_screen_reads_as_unchanged():
    assert frame_change(frame_fingerprint(screen()), frame_fingerprint(screen())) == 0.0
    assert not frame_moved(frame_fingerprint(screen()), frame_fingerprint(screen()))
    moved = frame_change(frame_fingerprint(screen()), frame_fingerprint(screen(box=(10, 10, 300, 200))))
    assert moved > 0.05


def desktop(box=None):
    """A real monitor, where a dialog is small and a caret is tiny."""
    image = Image.new("RGB", (2560, 1440), (20, 20, 20))
    if box:
        ImageDraw.Draw(image).rectangle(box, fill=(230, 230, 230))
    return image


def test_a_small_but_real_change_still_counts_as_movement():
    """A dialog covers little of a 2560x1440 screen and barely moves the
    average — but a person would see it, so it is not a frozen screen."""
    before = frame_fingerprint(desktop())
    after = frame_fingerprint(desktop(box=(1100, 600, 1500, 760)))

    assert frame_change(before, after) < 0.02, "this really is a small change"
    assert frame_moved(before, after)


def test_a_blinking_caret_is_not_movement():
    before = frame_fingerprint(desktop())
    after = frame_fingerprint(desktop(box=(1200, 700, 1202, 722)))

    assert not frame_moved(before, after)


def test_repeating_a_dead_click_earns_a_nudge_then_an_abort():
    watch = LoopWatch()
    frozen = frame_fingerprint(screen())
    levels = []
    for step in range(1, 9):
        watch.note_screen(frozen, acted=True)
        watch.note_actions(action_signature(click(500, 400)))
        levels.append(watch.verdict(step)["level"])

    assert "nudge" in levels, levels
    assert levels.index("nudge") < levels.index("abort"), levels
    assert levels.index("abort") >= 4, "give the agent room before pulling the plug"


def test_a_moving_screen_is_never_aborted():
    watch = LoopWatch()
    levels = []
    for step in range(1, 12):
        watch.note_screen(frame_fingerprint(screen(box=(step * 10, 10, step * 10 + 80, 90))), acted=True)
        watch.note_actions(action_signature([{"type": "key", "key": "pagedown"}]))
        levels.append(watch.verdict(step)["level"])

    assert "abort" not in levels, "a progressing run must not be killed for repeating a key"


def test_observation_steps_do_not_count_as_repeats():
    watch = LoopWatch()
    frozen = frame_fingerprint(screen())
    for step in range(1, 7):
        watch.note_screen(frozen, acted=False)
        watch.note_actions(action_signature([]))
        assert watch.verdict(step)["level"] != "abort"


def test_progress_clears_the_streak():
    watch = LoopWatch()
    frozen = frame_fingerprint(screen())
    for _ in range(4):
        watch.note_screen(frozen, acted=True)
        watch.note_actions(action_signature(click(500, 400)))
    assert watch.repeats >= 3

    watch.cleared()
    assert watch.repeats == 0 and watch.still == 0


def test_the_planner_reply_becomes_a_todo_list():
    plan = parse_plan({
        "plan": "Open Outlook and reply to Anna.",
        "todo": ["Open Outlook", "Find Anna's thread", "Write the reply", "Send it"],
        "done_when": ["The message shows in Sent Items"],
    })

    assert plan["todo"][0] == "Open Outlook"
    assert len(plan["todo"]) == 4
    assert plan["done_when"] == ["The message shows in Sent Items"]


def test_a_prose_plan_still_yields_a_todo_list():
    plan = parse_plan({"plan": "1. Open the browser\n2. Search for the file\n3. Download it"})

    assert plan["todo"] == ["Open the browser", "Search for the file", "Download it"]


def test_the_checklist_keeps_the_task_in_front_of_the_model():
    block = checklist_block("Send Anna the report", {
        "plan": "Use Outlook.", "todo": ["Open Outlook", "Attach the report"],
        "done_when": ["The mail is in Sent Items"],
    })

    assert "Send Anna the report" in block
    assert "1. [ ] Open Outlook" in block
    assert "DONE WHEN" in block
    assert "The mail is in Sent Items" in block


def test_the_checklist_works_without_a_planner():
    block = checklist_block("Rename my files", {"plan": "", "todo": [], "done_when": []})

    assert "Rename my files" in block
    assert "TODO" not in block
