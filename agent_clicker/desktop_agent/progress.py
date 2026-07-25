"""Is the run making progress, or going in circles?

A computer-use agent that misreads one button will happily click it thirty
times. Nothing in the loop noticed that before: every step cost a model call
and the run only ended when it ran out of steps. These helpers give the loop
two cheap signals — "you did the same thing again" and "the screen never
changed" — so it can nudge the model, and give up when nudging stops working.
"""

from __future__ import annotations

import hashlib

from PIL import Image

FINGERPRINT_SIZE = 32
# A blinking caret or a ticking clock nudges the average by almost nothing and
# never lights up a whole cell. A menu opening, a page loading or a dialog
# appearing does one or the other. Both signals must be quiet to call a screen
# still — otherwise a small but real change (one new field, one typed word)
# would read as "frozen".
STILL_MEAN = 0.004
STILL_PEAK = 24  # out of 255, on the downscaled thumbnail
BUCKET = 16  # pixels — clicks within one bucket are the same intent


def frame_fingerprint(image: Image.Image) -> bytes:
    """A tiny greyscale thumbnail — enough to tell "changed" from "identical"."""
    try:
        small = image.convert("L").resize((FINGERPRINT_SIZE, FINGERPRINT_SIZE), Image.BILINEAR)
        return small.tobytes()
    except Exception:
        return b""


def frame_delta(before: bytes, after: bytes) -> tuple[float, int]:
    """(mean difference 0.0-1.0, largest single-cell difference 0-255)."""
    if not before or not after or len(before) != len(after):
        return 1.0, 255
    total = 0
    peak = 0
    for a, b in zip(before, after):
        difference = abs(a - b)
        total += difference
        if difference > peak:
            peak = difference
    return total / (len(before) * 255), peak


def frame_change(before: bytes, after: bytes) -> float:
    """Mean absolute difference, 0.0 (identical) to 1.0 (inverted)."""
    return frame_delta(before, after)[0]


def frame_moved(before: bytes, after: bytes) -> bool:
    """Did anything a person would notice happen between these two frames?"""
    mean, peak = frame_delta(before, after)
    return mean >= STILL_MEAN or peak >= STILL_PEAK


def action_signature(actions) -> str:
    """Same intent, same signature.

    Coordinates are bucketed so that nudging a click two pixels sideways still
    reads as the same failing click, and long point lists collapse to their
    length so a redrawn path is not mistaken for a new idea.
    """
    parts = []
    for action in actions or []:
        if not isinstance(action, dict):
            parts.append(str(action)[:80])
            continue
        fields = [str(action.get("type") or "?").lower()]
        for key in sorted(action):
            if key == "type":
                continue
            value = action[key]
            if isinstance(value, bool):
                fields.append(f"{key}={value}")
            elif isinstance(value, (int, float)):
                fields.append(f"{key}={round(float(value) / BUCKET)}")
            elif isinstance(value, str):
                fields.append(f"{key}={value.strip()[:120]}")
            elif isinstance(value, (list, tuple)):
                fields.append(f"{key}=[{len(value)}]")
            else:
                fields.append(f"{key}={value}")
        parts.append("|".join(fields))
    if not parts:
        return "none"
    return hashlib.sha1(";".join(parts).encode("utf-8", "ignore")).hexdigest()[:16]


class LoopWatch:
    """Counts consecutive repeats and consecutive frozen screens.

    A nudge goes out when either count crosses its threshold. An abort needs
    both: the same actions AND a screen that will not move. That combination is
    a genuinely stuck agent, while a repeated `wait` in front of a progress bar
    keeps changing the screen and is left alone.
    """

    def __init__(self, nudge_repeats: int = 3, nudge_still: int = 4,
                 abort_repeats: int = 5, abort_still: int = 3):
        self.nudge_repeats = nudge_repeats
        self.nudge_still = nudge_still
        self.abort_repeats = abort_repeats
        self.abort_still = abort_still
        self.repeats = 0
        self.still = 0
        self._signature = ""
        self._fingerprint = b""
        self._nudged_at = 0

    def note_screen(self, fingerprint: bytes, acted: bool) -> dict:
        """Record the screen at the start of a step, and say whether it moved."""
        first = not self._fingerprint
        mean, peak = (1.0, 255) if first else frame_delta(self._fingerprint, fingerprint)
        moved = first or mean >= STILL_MEAN or peak >= STILL_PEAK
        if acted and not moved:
            self.still += 1
        else:
            self.still = 0
        self._fingerprint = fingerprint
        return {"mean": mean, "peak": peak, "moved": moved}

    def note_actions(self, signature: str) -> None:
        if signature == "none":
            # A pure observation step is not a repeat of anything.
            self._signature = signature
            return
        self.repeats = self.repeats + 1 if signature == self._signature else 0
        self._signature = signature

    def verdict(self, step: int) -> dict:
        """"", "nudge" or "abort", with a reason worth showing the model."""
        reasons = []
        if self.repeats >= self.nudge_repeats:
            reasons.append(f"the same actions {self.repeats + 1} steps running")
        if self.still >= self.nudge_still:
            reasons.append(f"the screen unchanged for {self.still} steps")
        if self.repeats >= self.abort_repeats and self.still >= self.abort_still:
            return {"level": "abort", "reason": " and ".join(reasons) or "no progress"}
        if reasons and step - self._nudged_at >= 2:
            self._nudged_at = step
            return {"level": "nudge", "reason": " and ".join(reasons)}
        return {"level": "", "reason": ""}

    def cleared(self) -> None:
        """Progress happened — forget the streaks."""
        self.repeats = 0
        self.still = 0


def _clean_items(value, limit: int, length: int = 240) -> list[str]:
    items = []
    for item in value if isinstance(value, (list, tuple)) else []:
        text = str(item).strip()
        if text:
            items.append(text[:length])
        if len(items) >= limit:
            break
    return items


def _todo_from_prose(plan: str, limit: int) -> list[str]:
    """Planners that ignore the schema still write numbered plans."""
    items = []
    for raw in (plan or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        head, _, rest = line.partition(" ")
        if head.rstrip(".)").isdigit() and head.rstrip(".)"):
            # The checklist renumbers, so drop the planner's own numbering.
            items.append(rest.strip()[:240] or line[:240])
        elif line[:2] in {"- ", "* "}:
            items.append(line[2:].strip()[:240])
        if len(items) >= limit:
            break
    return items


def parse_plan(parsed: dict, *, max_todo: int = 12, max_checks: int = 8) -> dict:
    """Normalise the planner's reply into a plan, a todo list and its checks."""
    parsed = parsed if isinstance(parsed, dict) else {}
    plan = str(parsed.get("plan") or "").strip()[:8000]
    todo = _clean_items(parsed.get("todo"), max_todo)
    if not todo:
        todo = _todo_from_prose(plan, max_todo)
    return {
        "plan": plan,
        "todo": todo,
        "done_when": _clean_items(parsed.get("done_when"), max_checks),
    }


def checklist_block(task: str, plan: dict) -> str:
    """The pinned message that keeps the goal in front of the model."""
    lines = [f"TASK (never lose sight of this): {task}"]
    if plan.get("plan"):
        lines.append("\nPLAN from the planning model:\n" + plan["plan"])
    if plan.get("todo"):
        lines.append("\nTODO LIST — work through these in order:")
        lines.extend(f"  {index}. [ ] {item}" for index, item in enumerate(plan["todo"], 1))
        lines.append("In every `thought`, state which TODO number you are on and what "
                     "you have already ticked off.")
    if plan.get("done_when"):
        lines.append("\nDONE WHEN all of these are true on screen:")
        lines.extend(f"  - {item}" for item in plan["done_when"])
    lines.append("\nThe plan is guidance, not gospel: verify each step against the live "
                 "screen and adapt when the UI differs.")
    return "\n".join(lines)
