"""Regression: completed CODE-agent plan items show a green check icon.

The shared todo renderer (aios_ui/web/js/transcript.js paintPlan) marks a
completed step's visible icon with class "todoIcon on" inside an
li.todoItem.done; code-beautiful.css must color that shared state green in
both light and dark schemes instead of the greyed-out #a1a1a1/#737373 default
applied to .todoIcon.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRANSCRIPT = ROOT / "aios_ui" / "web" / "js" / "transcript.js"
STYLES = ROOT / "aios_ui" / "web" / "css" / "code-beautiful.css"


def test_completed_plan_step_renders_the_shared_check_icon_state():
    js = TRANSCRIPT.read_text(encoding="utf-8")

    match = re.search(r"paintPlan\(card\) \{.*?\.todoList\"\)\.innerHTML.*?\.join\(\"\"\);", js, re.S)
    assert match, "paintPlan renderer not found in transcript.js"
    body = match.group(0)

    assert 'done ? " done"' in body, "completed steps are not tagged with .todoItem.done"
    assert re.search(
        r"icon\(TODO_CHECK_ICON,\s*done\)", body
    ), "completed steps do not switch on the shared TODO_CHECK_ICON"
    assert (
        """markup.replace('class="todoIcon', 'class="todoIcon on')""" in body
    ), "the on-class mechanism for the visible icon is missing"


def test_done_check_icon_is_green_in_light_and_dark_schemes():
    css = STYLES.read_text(encoding="utf-8")
    light = ".aicss-todo .todoItem.done .todoIcon.on { color: #15a06a; }"
    dark = ".aicss-todo .todoItem.done .todoIcon.on { color: #34d399; }"

    assert light in css, "light-scheme green done-check rule missing"
    assert dark in css, "dark-scheme green done-check rule missing"

    # The dark rule must live inside the dark-scheme media block so it wins
    # over the greyed .todoIcon default there.
    dark_block = re.search(r"@media \(prefers-color-scheme: dark\) \{(.*?)\n\}", css, re.S)
    assert dark_block, "dark-scheme media block not found"
    assert dark in dark_block.group(1), "green done-check rule not inside dark block"

    # The selector targets the shared completed state, not one hardcoded item.
    assert css.count(".todoItem.done .todoIcon.on") >= 2
