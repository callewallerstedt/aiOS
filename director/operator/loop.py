"""The pixel operator loop.

Screenshot -> model -> actions -> repeat, until the model says done, the step
reviewer decides an interval made no progress, or the user stops it. Every step
emits events so the phone can watch the work happen and step in.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .. import config, models, store
from . import display as display_mod
from . import prompts, x11

Emit = Callable[[str, dict], Awaitable[None]]

MAX_HISTORY_STEPS = 12
MEANINGFUL_SCREEN_CHANGE = 0.005
MAX_REPEATED_NO_EFFECT_ROUNDS = 3
MAX_DISTINCT_ACTIONS_ON_SCREEN = 8
MAX_NO_POSTCONDITION_ACTIONS = 4
MAX_STATE_CYCLE_PERIOD = 6
MAX_CYCLE_STRIKES = 2
STATE_VISUAL_MATCH = 0.025
STATE_STRUCTURAL_MATCH = 0.04
POST_ACTION_SETTLE = 1.0
FAST_ACTION_SETTLE = 0.25
# The operator's own memory, kept apart from the coordinator's so notes about
# which button moved and where a login lives do not fill every chat's prompt.
MEMORY_SCOPE = "operator"
RECALL_RUNS = 6
JSON_BLOCK = re.compile(r"(?s)\{.*\}")


@dataclass(frozen=True)
class ActionResult:
    """What the input layer proved, separate from what it merely issued."""

    description: str
    ok: bool = True
    issued: bool = True
    verified: bool = False
    private_detail: str = field(default="", repr=False)


def normalize_action_result(value: ActionResult | str) -> ActionResult:
    if isinstance(value, ActionResult):
        return value
    return ActionResult(str(value or "action issued"))


def screen_structure_signature(windows: list[str], controls: list[dict]) -> str:
    """Stable, secret-free shape of the active app and its interactive controls."""
    rows = ["window:" + re.sub(r"\s+", " ", str(title)).strip().casefold()[:160]
            for title in sorted(set(windows[:8]))]
    for control in controls[:80]:
        flags = "".join(name[0] for name in (
            "focused", "checked", "selected", "expanded", "pressed", "enabled")
                        if control.get(name))
        rows.append("|".join([
            str(control.get("id") or ""),
            str(control.get("role") or "").casefold()[:40],
            re.sub(r"\s+", " ", str(control.get("name") or "")).strip().casefold()[:160],
            str(int(control.get("x") or 0) // 8),
            str(int(control.get("y") or 0) // 8),
            str(int(control.get("width") or 0) // 8),
            str(int(control.get("height") or 0) // 8),
            flags,
        ]))
    if not rows:
        return ""
    return hashlib.sha256("\n".join(rows).encode("utf-8", "replace")).hexdigest()[:20]


class ScreenStateTracker:
    """Assign animation-tolerant canonical ids to recurring desktop states."""

    def __init__(self, *, visual_match: float = STATE_VISUAL_MATCH,
                 structural_match: float = STATE_STRUCTURAL_MATCH,
                 max_states: int = 64) -> None:
        self.visual_match = float(visual_match)
        self.structural_match = float(structural_match)
        self.max_states = max(8, int(max_states))
        self._states: dict[int, dict[str, Any]] = {}
        self._next_id = 1

    def identify(self, image: bytes, structure: str) -> int:
        best: tuple[float, int] | None = None
        for state_id, state in self._states.items():
            same_structure = bool(structure and structure == state.get("structure"))
            threshold = self.structural_match if same_structure else self.visual_match
            for sample in state.get("samples") or []:
                delta = x11.image_change_ratio(sample, image)
                if delta <= threshold and (best is None or delta < best[0]):
                    best = (delta, state_id)
        if best is not None:
            state = self._states[best[1]]
            samples = state["samples"]
            if all(x11.image_change_ratio(sample, image) > self.visual_match
                   for sample in samples):
                samples.append(image)
                del samples[:-3]
            return best[1]
        state_id = self._next_id
        self._next_id += 1
        self._states[state_id] = {"structure": structure, "samples": [image]}
        if len(self._states) > self.max_states:
            oldest = min(self._states)
            self._states.pop(oldest, None)
        return state_id


def recurring_state_cycle(states: list[int], *, max_period: int = MAX_STATE_CYCLE_PERIOD
                          ) -> tuple[int, ...]:
    """Return a repeated suffix such as A-B-A-B or A-B-C-A-B-C."""
    if len(states) < 4:
        return ()
    ceiling = min(max(1, int(max_period)), len(states) // 2)
    for period in range(1, ceiling + 1):
        if states[-period:] == states[-2 * period:-period]:
            cycle = tuple(states[-period:])
            # The same loop is observed at a different phase on each step:
            # A-B and B-A are one cycle, not two approaches. Canonicalizing
            # rotations makes the same navigation loop stop regardless of
            # which page or phase first exposed it.
            rotations = [cycle[index:] + cycle[:index]
                         for index in range(len(cycle))]
            return min(rotations)
    return ()


def action_target(action: dict[str, Any]) -> tuple[int, int] | None:
    try:
        if action.get("x") is not None and action.get("y") is not None:
            return int(action["x"]), int(action["y"])
        target = action.get("to") or action.get("from")
        if isinstance(target, (list, tuple)) and len(target) >= 2:
            return int(target[0]), int(target[1])
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def click_marker(action: dict[str, Any]) -> dict[str, Any] | None:
    """Describe a pointer press without altering the screenshot sent to vision."""
    kind = str(action.get("type") or "").strip().casefold()
    if kind not in {"click", "double_click", "right_click", "type"}:
        return None
    target = action_target(action)
    if target is None:
        return None
    x, y = target
    return {
        "x": x,
        "y": y,
        "kind": "type-focus" if kind == "type" else "click",
        "button": str(action.get("button") or "left"),
        "clicks": int(action.get("clicks") or (2 if kind == "double_click" else 1)),
    }


def post_action_settle_seconds(actions: list[dict[str, Any]]) -> float:
    """Allow navigation-producing inputs to render before the next screenshot."""
    if not actions:
        return 0.0
    # Legacy JSON responses can contain a deliberate chain. Do not insert a
    # one-second pause between its constituent actions; the native tool path is
    # one action per turn and receives the full navigation settle.
    if len(actions) != 1:
        return FAST_ACTION_SETTLE
    action = actions[0]
    kind = str(action.get("type") or "").strip().casefold()
    navigation = kind in {
        "click", "double_click", "right_click", "type", "open_url", "launch",
    }
    if kind == "key":
        navigation = str(action.get("key") or "").strip().casefold() in {
            "enter", "return", "kp_enter",
        }
    elif kind == "hotkey":
        navigation = any(str(key).strip().casefold() in {
            "enter", "return", "kp_enter",
        } for key in (action.get("keys") or []))
    return POST_ACTION_SETTLE if navigation else FAST_ACTION_SETTLE


def _is_text_control(control: dict[str, Any]) -> bool:
    role = str(control.get("role") or "").strip().casefold()
    return role in {"entry", "password text", "text", "text box"} or "entry" in role


def text_control_for_action(action: dict[str, Any], controls: list[dict]
                            ) -> dict[str, Any] | None:
    """Resolve a typed action to a privacy-safe accessible field snapshot."""
    candidates = [row for row in controls if _is_text_control(row)]
    target = action_target(action)
    if target is not None:
        x, y = target
        containing = [row for row in candidates
                      if int(row.get("x") or 0) <= x
                      < int(row.get("x") or 0) + int(row.get("width") or 0)
                      and int(row.get("y") or 0) <= y
                      < int(row.get("y") or 0) + int(row.get("height") or 0)]
        if containing:
            return min(containing, key=lambda row: int(row.get("width") or 0)
                       * int(row.get("height") or 0))
    return next((row for row in candidates if row.get("focused")), None)


def typed_text_postcondition(action: dict[str, Any], before: dict[str, Any] | None,
                             controls: list[dict]) -> bool:
    """Prove paste by character count without reading or persisting its value."""
    expected = len(str(action.get("text") or ""))
    if expected <= 0:
        return False
    after = None
    control_id = str((before or {}).get("id") or "")
    if control_id:
        after = next((row for row in controls
                      if str(row.get("id") or "") == control_id), None)
    if after is None:
        after = text_control_for_action(action, controls)
    try:
        after_length = int((after or {}).get("text_length"))
    except (TypeError, ValueError):
        return False
    # A final length without a baseline is not evidence that this paste caused
    # it.  The field may have appeared after the click with old text already in
    # it; treating that as progress recreates the false-success input loop.
    try:
        before_length = int((before or {}).get("text_length"))
    except (TypeError, ValueError):
        return False
    if bool(action.get("replace")):
        return after_length == expected and after_length != before_length
    return after_length >= before_length + expected


def local_image_changed(previous: bytes, current: bytes, target: tuple[int, int] | None,
                        screen_width: int, screen_height: int, *, radius: int = 2) -> bool:
    """Did pixels around the acted-on control change, rather than an animation elsewhere?"""
    if (not target or not previous or len(previous) != len(current)
            or len(current) != 64 * 36 or screen_width <= 0 or screen_height <= 0):
        return False
    cell_x = max(0, min(63, int(target[0] * 64 / screen_width)))
    cell_y = max(0, min(35, int(target[1] * 36 / screen_height)))
    for y in range(max(0, cell_y - radius), min(36, cell_y + radius + 1)):
        for x in range(max(0, cell_x - radius), min(64, cell_x + radius + 1)):
            index = y * 64 + x
            if abs(previous[index] - current[index]) >= 2:
                return True
    return False


def action_bounds_error(action: dict[str, Any], width: int, height: int) -> str:
    """Reject coordinates the model could not possibly have seen on this screen."""
    points: list[tuple[Any, Any]] = []
    if action.get("x") is not None or action.get("y") is not None:
        points.append((action.get("x"), action.get("y")))
    for name in ("from", "to"):
        value = action.get(name)
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            points.append((value[0], value[1]))
    for value in action.get("points") or []:
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            points.append((value[0], value[1]))
    for raw_x, raw_y in points:
        try:
            x, y = int(raw_x), int(raw_y)
        except (TypeError, ValueError, OverflowError):
            return "action has a non-numeric screen coordinate"
        if x < 0 or y < 0 or x >= width or y >= height:
            return f"coordinate ({x},{y}) is outside the {width}x{height} screen"
    return ""


def action_signature(action: dict[str, Any]) -> tuple[Any, ...] | None:
    """Describe an action by effect so unchanged-screen retries can be caught."""
    kind = str(action.get("type") or "").strip().casefold()

    def bucket(value: Any) -> int:
        try:
            return int(float(value or 0)) // 24
        except (TypeError, ValueError, OverflowError):
            return 0

    def point(value: Any) -> tuple[int, int]:
        if not isinstance(value, (list, tuple)) or len(value) < 2:
            return 0, 0
        return bucket(value[0]), bucket(value[1])

    def integer(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return default

    if kind in {"click", "double_click", "right_click"}:
        return (kind, bucket(action.get("x")), bucket(action.get("y")),
                str(action.get("button") or "left"), integer(action.get("clicks"), 1))
    if kind == "move":
        return (kind, bucket(action.get("x")), bucket(action.get("y")))
    if kind == "type":
        text = str(action.get("text") or "")
        return (kind, bucket(action.get("x")), bucket(action.get("y")),
                len(text), hashlib.sha256(text.encode("utf-8")).hexdigest()[:12])
    if kind in {"key", "press", "key_down", "key_up"}:
        return (kind, str(action.get("key") or "").casefold(),
                integer(action.get("presses"), 1))
    if kind == "hotkey":
        return (kind, tuple(str(key).casefold() for key in (action.get("keys") or [])))
    if kind == "scroll":
        amount = integer(action.get("dy"))
        return (kind, bucket(action.get("x")), bucket(action.get("y")),
                1 if amount > 0 else -1, min(abs(amount), 25))
    if kind == "select_all":
        return (kind, bucket(action.get("x")), bucket(action.get("y")))
    if kind == "drag":
        return (kind, *point(action.get("from")), *point(action.get("to")))
    if kind == "path":
        points = action.get("points") or []
        return (kind, tuple(point(value) for value in points[:24]))
    if kind == "wait_screen":
        return (kind, str(action.get("condition") or "stable").casefold())
    if kind in {"open_url", "launch", "shell"}:
        value = str(action.get("url") or action.get("command") or "")
        return (kind, hashlib.sha256(value.encode("utf-8")).hexdigest()[:12])
    return None


def parse_reply(text: str) -> dict:
    """Pull the JSON object out of a model reply that may be fenced or chatty."""
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw).strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        match = JSON_BLOCK.search(raw)
        if not match:
            return {"thought": raw[:400], "status": "fail", "actions": [],
                    "message": "model did not return JSON"}
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {"thought": raw[:400], "status": "fail", "actions": [],
                    "message": "model returned malformed JSON"}
    if not isinstance(parsed, dict):
        return {"thought": raw[:400], "status": "fail", "actions": [],
                "message": "model returned a non-object"}
    parsed.setdefault("actions", [])
    parsed.setdefault("status", "continue")
    parsed.setdefault("thought", "")
    parsed.setdefault("message", "")
    parsed.setdefault("need_screen", True)
    if not isinstance(parsed.get("actions"), list):
        parsed["actions"] = []
    return parsed


def scale_actions(actions: list[dict], factor: float) -> list[dict]:
    """Model coordinates are in the (possibly shrunk) screenshot's pixel space."""
    if factor == 1.0:
        return actions

    def fix(value: Any) -> Any:
        return int(round(float(value) * factor))

    out = []
    for action in actions:
        item = dict(action)
        for key in ("x", "y"):
            if isinstance(item.get(key), (int, float)):
                item[key] = fix(item[key])
        for key in ("from", "to"):
            pair = item.get(key)
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                item[key] = [fix(pair[0]), fix(pair[1])]
        points = item.get("points")
        if isinstance(points, list):
            item["points"] = [[fix(p[0]), fix(p[1])] for p in points
                              if isinstance(p, (list, tuple)) and len(p) == 2]
        out.append(item)
    return out


TOOL_ACTIONS = {
    "click": "click", "type_text": "type", "key": "key", "hotkey": "hotkey",
    "scroll": "scroll", "open_url": "open_url", "wait": "wait",
    "wait_for_screen": "wait_screen",
    "launch_app": "launch", "shell": "shell", "drag": "drag",
    "select_all_text": "select_all", "remember": "remember",
    "move_pointer": "move", "draw_path": "path",
}


def background(limit: int = RECALL_RUNS) -> str:
    """What this operator already knows: its notes, and how recent runs went.

    Every run used to start from nothing, so the same dead end got walked into
    again the next day. Notes are things it chose to keep; the run list is
    automatic, because the useful lesson is usually "that did not work".
    """
    lines: list[str] = []
    # A global pile of long notes used to consume most of the first vision
    # request. Keep only a small, bounded recall window; the task and live
    # screen must remain the dominant context.
    notes = store.list_memory(scope=MEMORY_SCOPE, limit=12)
    if notes:
        lines.append("WHAT YOU LEARNED ON THIS SCREEN BEFORE:")
        lines += [f"- {row['key']}: {str(row['value'])[:500]}" for row in notes]
    runs = [job for job in store.list_jobs(limit=60) if job.get("kind") == "operator"]
    if runs:
        lines.append("")
        lines.append("YOUR RECENT RUNS (newest first):")
        for job in runs[:limit]:
            task = str((job.get("request") or {}).get("task") or "")[:120]
            result = dict(job.get("result") or {})
            summary = str(result.get("summary") or job.get("status") or "")[:180]
            lines.append(f"- [{job.get('status')}] {task} -> {summary}")
    return "\n".join(lines)


def tool_decision(reply: dict) -> dict:
    """Translate Luna's native function call into one operator action."""
    calls = list(reply.get("tool_calls") or [])
    if not calls:
        return parse_reply(reply.get("text") or "")
    call = calls[0]
    name = str(call.get("name") or "")
    try:
        args = json.loads(str(call.get("arguments") or "{}"))
    except json.JSONDecodeError:
        return {"thought": "", "status": "fail", "actions": [],
                "message": f"{name} returned malformed arguments"}
    if not isinstance(args, dict):
        args = {}
    observation = str(args.pop("observation", "") or "").strip()
    thought = str(args.pop("thought", "") or reply.get("reasoning")
                  or reply.get("text") or "").strip()
    if name == "finish":
        return {"observation": observation, "thought": thought,
                "status": str(args.get("status") or "done"),
                "actions": [], "message": str(args.get("message") or ""),
                "native_tool": name}
    kind = TOOL_ACTIONS.get(name)
    if not kind:
        return {"observation": observation, "thought": thought,
                "status": "fail", "actions": [],
                "message": f"unknown operator tool {name!r}", "native_tool": name}
    return {"observation": observation, "thought": thought, "status": "continue",
            "actions": [{"type": kind, **args}], "message": name,
            "need_screen": True, "native_tool": name}


def progress_review_decision(reply: dict) -> dict:
    """Translate the checkpoint review's required native tool call."""
    calls = list(reply.get("tool_calls") or [])
    if not calls:
        return {"progress": False,
                "issue": "the progress reviewer returned no decision",
                "attempts": str(reply.get("text") or "")[:500]}
    call = calls[0]
    name = str(call.get("name") or "")
    try:
        args = json.loads(str(call.get("arguments") or "{}"))
    except json.JSONDecodeError:
        args = {}
    if not isinstance(args, dict):
        args = {}
    if name == "continue_work":
        return {"progress": True,
                "summary": str(args.get("summary") or "meaningful progress was made"),
                "next_approach": str(args.get("next_approach") or "continue toward the task")}
    if name == "stop_stuck":
        return {"progress": False,
                "issue": str(args.get("issue") or "no meaningful progress was made"),
                "attempts": str(args.get("attempts") or "")}
    return {"progress": False,
            "issue": f"the progress reviewer returned unknown decision {name!r}",
            "attempts": ""}


async def review_progress(task: str, checkpoint_step: int, current_step: int,
                          checkpoint_image: str, current_image: str,
                          history: str, settings: dict) -> dict:
    """Have the same configured operator model judge one work interval."""
    operator_cfg = settings.get("operator", {}) or {}
    message = prompts.progress_review_message(
        task, checkpoint_step, current_step, history)
    items = [models.user_message([
        models.text_part(message),
        models.text_part("CHECKPOINT SCREEN:"),
        models.image_part(checkpoint_image),
        models.text_part("CURRENT SCREEN:"),
        models.image_part(current_image),
    ])]
    reply = await models.complete(
        backend=str(operator_cfg.get("backend") or ""),
        model=str(operator_cfg.get("model") or ""),
        reasoning=str(operator_cfg.get("reasoning") or ""),
        instructions=prompts.PROGRESS_REVIEW_PROMPT, tool_choice="required",
        items=items, tools=prompts.PROGRESS_REVIEW_TOOLS, timeout=180.0,
        settings=settings)
    return progress_review_decision(reply)


async def execute(action: dict, settings: dict) -> ActionResult:
    """Run one action and state only what the input layer actually proved."""
    kind = str(action.get("type") or "").strip().lower()
    x, y = action.get("x"), action.get("y")

    if kind == "move":
        await x11.move(x, y, settings)
        return ActionResult(f"moved pointer to ({x},{y})")
    if kind == "click":
        await x11.click(x, y, str(action.get("button") or "left"),
                        int(action.get("clicks") or 1), settings)
        # AT-SPI doAction proves that the desktop accepted the input, just as a
        # successful xdotool call does. Neither proves the intended page/app
        # state changed; the next screenshot and accessibility snapshot do.
        return ActionResult(f"issued click at ({x},{y})")
    if kind == "double_click":
        await x11.click(x, y, "left", 2, settings)
        return ActionResult(f"issued double click at ({x},{y})")
    if kind == "right_click":
        await x11.click(x, y, "right", 1, settings)
        return ActionResult(f"issued right click at ({x},{y})")
    if kind == "drag":
        start, end = action.get("from") or [x, y], action.get("to") or [x, y]
        await x11.drag((int(start[0]), int(start[1])), (int(end[0]), int(end[1])),
                       str(action.get("button") or "left"), settings=settings)
        return ActionResult(f"issued drag {start} -> {end}")
    if kind == "path":
        points = action.get("points") or []
        await x11.stroke(points, str(action.get("button") or "left"), settings=settings)
        return ActionResult(f"issued stroke through {len(points)} points")
    if kind == "mouse_down":
        await x11.mouse_down(x, y, str(action.get("button") or "left"), settings)
        return ActionResult(f"held mouse at ({x},{y})")
    if kind == "mouse_up":
        await x11.mouse_up(x, y, str(action.get("button") or "left"), settings)
        return ActionResult(f"released mouse at ({x},{y})", verified=True)
    if kind == "type":
        text = str(action.get("text") or "")
        if not text:
            return ActionResult("type had no text", ok=False, issued=False)
        if x is None or y is None:
            await x11.type_text(text, settings)
        else:
            await x11.type_text(text, settings, x=x, y=y,
                                replace=bool(action.get("replace")))
        # A typed value can be a one-time verification code. Keep its content
        # out of the persisted action trace while still recording the action.
        where = f" at ({x},{y})" if x is not None and y is not None else ""
        return ActionResult(f"issued {len(text)} typed characters{where}")
    if kind == "key":
        await x11.press(str(action.get("key") or ""), int(action.get("presses") or 1), settings)
        return ActionResult(f"issued key {action.get('key')}")
    if kind == "hotkey":
        keys = [str(k) for k in (action.get("keys") or [])]
        await x11.hotkey(keys, settings)
        return ActionResult(f"issued hotkey {'+'.join(keys)}")
    if kind == "select_all":
        await x11.hotkey(["ctrl", "a"], settings)
        return ActionResult("issued select-all in the focused field")
    if kind == "remember":
        key = str(action.get("key") or "").strip()[:80]
        value = str(action.get("value") or "").strip()[:2000]
        if not key or not value:
            return ActionResult("remember was missing a key or value", ok=False, issued=False)
        store.remember(key, value, scope=MEMORY_SCOPE)
        return ActionResult(f"remembered {key}", verified=True)
    if kind == "key_down":
        await x11.key_down(str(action.get("key") or ""), settings)
        return ActionResult(f"held key {action.get('key')}")
    if kind == "key_up":
        await x11.key_up(str(action.get("key") or ""), settings)
        return ActionResult(f"released {action.get('key') or 'modifiers'}", verified=True)
    if kind == "scroll":
        await x11.scroll(x, y, int(action.get("dy") or 3), settings)
        return ActionResult(f"issued scroll {action.get('dy')}")
    if kind == "wait":
        seconds = max(0.0, min(float(action.get("seconds") or 0.5), 10.0))
        await asyncio.sleep(seconds)
        return ActionResult(f"waited {seconds}s")
    if kind == "wait_screen":
        condition = str(action.get("condition") or "stable").strip().casefold()
        timeout = max(0.5, min(float(action.get("timeout") or 10.0), 20.0))
        stable_for = max(0.2, min(float(action.get("stable_for") or 0.8), 3.0))
        previous = x11.image_signature(await x11.capture(settings))
        deadline = asyncio.get_running_loop().time() + timeout
        stable_since = asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.2)
            current = x11.image_signature(await x11.capture(settings))
            changed = x11.image_change_ratio(previous, current) >= MEANINGFUL_SCREEN_CHANGE
            if condition == "change" and changed:
                return ActionResult("screen changed", verified=True)
            if condition == "stable":
                if changed:
                    stable_since = asyncio.get_running_loop().time()
                elif asyncio.get_running_loop().time() - stable_since >= stable_for:
                    return ActionResult("screen became stable", verified=True)
            previous = current
        return ActionResult(
            f"screen did not become {condition} within {timeout}s", ok=False)
    if kind == "open_url":
        url = str(action.get("url") or "")
        message = await display_mod.launch_chrome(url, settings)
        failed = message.startswith("no Chrome") or "exited immediately" in message
        return ActionResult(message, ok=not failed)
    if kind == "launch":
        command = str(action.get("command") or "").strip()
        if not command:
            return ActionResult("launch had no command", ok=False, issued=False)
        argv = shlex.split(command)
        await asyncio.create_subprocess_exec(
            *argv, env=display_mod.display_env(settings),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True)
        await asyncio.sleep(1.5)
        return ActionResult(f"launched {command}")
    if kind == "shell":
        command = str(action.get("command") or "").strip()
        code, out = await x11.run(["bash", "-lc", command], settings, timeout=90)
        clipped = out[:1500]
        return ActionResult(
            f"shell exited {code}", ok=code == 0,
            verified=code == 0 and bool(clipped.strip()), private_detail=clipped)
    return ActionResult(f"unknown action {kind!r}", ok=False, issued=False)


async def run_task(task: str, *, emit: Emit, settings: dict | None = None,
                   cancel: asyncio.Event | None = None,
                   ask_user: Callable[..., Awaitable[str]] | None = None,
                   review_every: int = 0,
                   follow_ups: Callable[[], list[str]] | None = None) -> dict:
    """Drive the screen until the task is done. Returns a result summary."""
    cfg = settings if settings is not None else config.load_settings()
    operator_cfg = cfg.get("operator", {}) or {}
    cancel = cancel or asyncio.Event()
    review_interval = max(1, int(review_every or operator_cfg.get("review_every") or 30))

    state = await display_mod.ensure_running(cfg, with_chrome=True)
    if not state.get("ready"):
        return {"status": "fail", "summary": "the operator display is not running "
                                             f"({state.get('units')})", "steps": 0}

    history: list[str] = []
    review_history: list[str] = []
    observation_ledger: list[str] = []
    action_trace: list[str] = []
    need_screen = True
    last_shot: tuple[str, int, int, bytes] | None = None
    checkpoint_shot: tuple[str, int, int, bytes] | None = None
    checkpoint_step = 0
    steps = 0
    last_performed = ""
    notes: list[str] = []
    last_windows: list[str] = []
    last_controls: list[dict] = []
    last_structure = ""
    last_state_id = 0
    last_signature = b""
    pending_actions: list[tuple[dict[str, Any], ActionResult,
                                dict[str, Any] | None]] = []
    actions_by_state: dict[int, set[tuple[Any, ...]]] = {}
    state_sequence: list[int] = []
    active_cycle: tuple[int, ...] = ()
    cycle_strikes = 0
    no_postcondition_actions = 0
    last_postcondition_failed = False
    max_no_postcondition = max(
        2, int(operator_cfg.get("max_no_postcondition_actions")
               or operator_cfg.get("max_repeated_no_effect_rounds")
               or MAX_NO_POSTCONDITION_ACTIONS))
    max_distinct_actions = max(
        max_no_postcondition + 1,
        int(operator_cfg.get("max_distinct_actions_on_screen")
            or MAX_DISTINCT_ACTIONS_ON_SCREEN))
    meaningful_change = max(
        0.0001, min(float(operator_cfg.get("meaningful_screen_change")
                         or MEANINGFUL_SCREEN_CHANGE), 1.0))
    tracker = ScreenStateTracker(visual_match=max(STATE_VISUAL_MATCH,
                                                   meaningful_change * 4.0))
    recalled = background()
    await emit("operator.started", {"task": task, "display": state.get("display"),
                                    "review_every": review_interval,
                                    "recalled": bool(recalled)})

    try:
        while True:
            if cancel.is_set():
                await emit("operator.stopped", {"reason": "stopped by user", "steps": steps})
                return {"status": "stopped", "summary": "stopped by user", "steps": steps}
            steps += 1

            # Anything Calle said since the last step outranks the original brief.
            if follow_ups is not None:
                for raw_note in follow_ups():
                    note = str(raw_note or "").strip()[:2000]
                    if not note:
                        continue
                    notes.append(note)
                    history.append(f"[{steps}] Calle added: {note[:400]}")
                    await emit("operator.note", {"step": steps, "text": note})

            width, height = await x11.screen_size(cfg)
            screen_unchanged = False
            feedback_parts: list[str] = []
            if need_screen or last_shot is None:
                png = await x11.capture(cfg)
                data_url, shot_w, shot_h = x11.encode_jpeg(png)
                signature = x11.image_signature(png)
                last_shot = (data_url, shot_w or width, shot_h or height, signature)
                last_windows, last_controls = await asyncio.gather(
                    x11.window_list(settings=cfg),
                    x11.accessible_controls(settings=cfg))
                structure = screen_structure_signature(last_windows, last_controls)
                state_id = tracker.identify(signature, structure)
                screen_unchanged = bool(last_state_id and state_id == last_state_id)

                if pending_actions:
                    misses = 0
                    verified_any = False
                    for action, result, before_control in pending_actions:
                        verified = result.verified
                        if result.ok and not verified:
                            if str(action.get("type") or "") == "type":
                                verified = typed_text_postcondition(
                                    action, before_control, last_controls)
                            else:
                                verified = (
                                    state_id != last_state_id
                                    or structure != last_structure
                                    or local_image_changed(
                                        last_signature, signature, action_target(action),
                                        width, height)
                                )
                        if result.issued and not verified:
                            misses += 1
                        verified_any = verified_any or verified
                    last_postcondition_failed = bool(misses)
                    if verified_any and not misses:
                        no_postcondition_actions = 0
                    if misses:
                        no_postcondition_actions += misses
                        feedback_parts.append(
                            f"{misses} issued action(s) produced no verified target or "
                            "screen postcondition. Do not treat command exit as success.")
                    pending_actions.clear()

                last_structure = structure
                last_state_id = state_id
                last_signature = signature
                if not state_sequence or state_sequence[-1] != state_id:
                    state_sequence.append(state_id)
                    state_sequence[:] = state_sequence[-32:]
                cycle = recurring_state_cycle(state_sequence)
                if cycle:
                    cycle_strikes = cycle_strikes + 1 if cycle == active_cycle else 1
                    active_cycle = cycle
                    feedback_parts.append(
                        f"The desktop returned to a repeating {len(cycle)}-state cycle. "
                        "Do not use an edge or action from that cycle again; choose a route "
                        "that leaves these states, or ask Calle.")
                else:
                    active_cycle = ()
                    cycle_strikes = 0

                await emit("operator.screenshot", {
                    "step": steps, "image": data_url,
                    "width": last_shot[1], "height": last_shot[2],
                    "state_id": state_id,
                })
                if checkpoint_shot is None:
                    checkpoint_shot = last_shot

            data_url, shot_w, shot_h, _signature = last_shot
            factor = (width / float(shot_w)) if shot_w else 1.0
            actions_on_screen = actions_by_state.setdefault(last_state_id, set())

            if screen_unchanged and last_performed:
                feedback_parts.append(
                    f"The canonical screen state is unchanged after: {last_performed[:300]}. "
                    "Choose a different method.")
            if cycle_strikes >= MAX_CYCLE_STRIKES:
                issue = (
                    f"The operator revisited the same {len(active_cycle)}-state cycle after "
                    "being told to leave it, so it stopped instead of repeating the loop."
                )
                await emit("operator.stuck", {
                    "steps": steps, "issue": issue, "cycle": list(active_cycle),
                })
                return {"status": "stopped", "summary": issue, "steps": steps,
                        "issue": issue}
            if (no_postcondition_actions >= max_no_postcondition
                    or len(actions_on_screen) >= max_distinct_actions):
                issue = (
                    "The operator exhausted distinct approaches or issued too many actions "
                    "without a verified postcondition on this state, so it stopped instead "
                    "of looping."
                )
                await emit("operator.stuck", {
                    "steps": steps, "issue": issue,
                    "attempts": len(actions_on_screen),
                    "unverified_actions": no_postcondition_actions,
                })
                return {"status": "stopped", "summary": issue, "steps": steps,
                        "issue": issue}

            message = prompts.task_message(
                task, shot_w, shot_h, "\n".join(history[-MAX_HISTORY_STEPS:]),
                last_windows, "\n".join(feedback_parts), notes=notes, step=steps,
                background=recalled if steps == 1 else "", controls=last_controls,
                observations=observation_ledger, action_trace=action_trace)
            items = [models.user_message([
                models.text_part(message), models.image_part(data_url)])]

            try:
                reply = await models.complete(
                    backend=str(operator_cfg.get("backend") or ""),
                    model=str(operator_cfg.get("model") or ""),
                    reasoning=str(operator_cfg.get("reasoning") or ""),
                    instructions=prompts.SYSTEM_PROMPT, tool_choice="required",
                    items=items, tools=prompts.ACTION_TOOLS, timeout=180.0, settings=cfg)
            except models.ModelError as exc:
                await emit("operator.failed", {"error": str(exc), "step": steps})
                return {"status": "fail", "summary": str(exc), "steps": steps}

            parsed = tool_decision(reply)
            observation = str(parsed.get("observation") or "").strip()
            thought = str(parsed.get("thought") or "")
            status = str(parsed.get("status") or "continue").lower()
            actions = scale_actions(parsed.get("actions") or [], factor)

            await emit("operator.step", {
                "step": steps, "observation": observation,
                "thought": thought, "status": status,
                "message": str(parsed.get("message") or ""),
                "actions": [str(a.get("type") or "?") for a in actions],
                "native_tool": str(parsed.get("native_tool") or ""),
                "model": str(reply.get("model") or operator_cfg.get("model") or ""),
                "usage": reply.get("usage") or {},
                "state_id": last_state_id,
            })

            if observation:
                concise_observation = re.sub(r"\s+", " ", observation).strip()[:500]
                if (not observation_ledger
                        or concise_observation.casefold()
                        != observation_ledger[-1].casefold()):
                    observation_ledger.append(concise_observation)
                    del observation_ledger[:-12]

            for action in actions:
                marker = click_marker(action)
                if marker:
                    await emit("operator.screenshot", {
                        "step": steps, "image": data_url,
                        "width": width, "height": height,
                        "state_id": last_state_id,
                        "phase": "action_target", "marker": marker,
                    })

            performed: list[str] = []
            private_details: list[str] = []
            action_failed = False
            for action in actions:
                if cancel.is_set():
                    break
                signature = action_signature(action)
                if signature is not None and signature in actions_on_screen:
                    result = ActionResult(
                        f"{action.get('type')} blocked because that action was already "
                        "issued from this canonical screen state",
                        ok=False, issued=False)
                    no_postcondition_actions += 1
                else:
                    bounds_error = action_bounds_error(action, width, height)
                    if bounds_error:
                        result = ActionResult(bounds_error, ok=False, issued=False)
                        no_postcondition_actions += 1
                    else:
                        before_control = (text_control_for_action(action, last_controls)
                                          if str(action.get("type") or "") == "type"
                                          else None)
                        try:
                            result = normalize_action_result(await execute(action, cfg))
                        except Exception as exc:  # one bad action must not kill the run
                            detail = type(exc).__name__
                            if str(action.get("type") or "") not in {"type", "shell", "launch"}:
                                detail += f": {str(exc)[:180]}"
                            result = ActionResult(
                                f"{action.get('type')} failed: {detail}",
                                ok=False, issued=False)
                        if result.ok and result.issued and not result.verified:
                            pending_actions.append((dict(action), result, before_control))
                        elif result.ok and result.verified:
                            no_postcondition_actions = 0
                            last_postcondition_failed = False
                        elif not result.ok:
                            no_postcondition_actions += 1
                            action_failed = True
                    if signature is not None:
                        actions_on_screen.add(signature)
                action_failed = action_failed or not result.ok
                performed.append(result.description)
                if result.private_detail:
                    private_details.append(result.private_detail[:1500])

            settle_seconds = post_action_settle_seconds(actions)
            if settle_seconds:
                await asyncio.sleep(settle_seconds)
            if performed:
                await emit("operator.actions", {"step": steps, "performed": performed,
                                                "state_id": last_state_id,
                                                "settle_seconds": settle_seconds})
            last_performed = "; ".join(performed)

            step_record = (f"[{steps}] saw: {observation[:500] or '(not recorded)'}\n"
                           f"    thought: {thought[:400]}\n    did: " +
                           ("; ".join(performed)[:500] if performed else "(nothing)"))
            if private_details:
                step_record += "\n    private result: " + "\n".join(private_details)[:1600]
            history.append(step_record)
            review_history.append(step_record)
            action_trace.append(
                f"step {steps}, screen {last_state_id}: "
                + ("; ".join(performed)[:240] if performed else status))
            del action_trace[:-24]

            if status in {"done", "fail", "stopped"} and (pending_actions or action_failed):
                history.append(
                    f"[{steps}] The model requested {status}, but its last action has no "
                    "verified postcondition yet. Inspect the next screen before concluding.")
                need_screen = True
                continue
            if status == "done" and last_postcondition_failed:
                issue = (
                    "The Operator tried to report completion after its most recent input "
                    "had no verified postcondition, so it stopped instead of claiming "
                    "an unconfirmed result."
                )
                await emit("operator.stuck", {
                    "steps": steps, "issue": issue,
                    "unverified_actions": no_postcondition_actions,
                })
                return {"status": "stopped", "summary": issue, "steps": steps,
                        "issue": issue}
            if status == "done":
                summary = str(parsed.get("message") or thought or "done")
                await emit("operator.done", {"steps": steps, "summary": summary})
                return {"status": "done", "summary": summary, "steps": steps}
            if status == "fail":
                summary = str(parsed.get("message") or thought or "failed")
                await emit("operator.failed", {"steps": steps, "error": summary})
                return {"status": "fail", "summary": summary, "steps": steps}
            if status == "stopped":
                summary = str(parsed.get("message") or thought or "stopped")
                await emit("operator.stopped", {"steps": steps, "reason": summary})
                return {"status": "stopped", "summary": summary, "steps": steps}
            if status in ("ask", "handoff") and ask_user is not None:
                question = str(parsed.get("message") or thought or "")
                answer = await ask_user(
                    question,
                    options=["Done", "Cancel"] if status == "handoff" else [],
                    kind="handoff" if status == "handoff" else "question",
                    extra={"takeover": status == "handoff"})
                if not str(answer or "").strip():
                    return {"status": "blocked", "summary": question or "needs a human",
                            "steps": steps}
                if str(answer).strip().lower().startswith("cancel"):
                    return {"status": "stopped", "summary": "cancelled at handoff",
                            "steps": steps}
                history.append(f"[{steps}] Calle answered: {str(answer)[:1000]}")
                need_screen = True
                continue
            if status in ("ask", "handoff"):
                return {"status": "blocked", "summary": thought or "needs a human",
                        "steps": steps}

            if steps % review_interval == 0:
                await asyncio.sleep(0.15)
                png = await x11.capture(cfg)
                review_image, review_w, review_h = x11.encode_jpeg(png)
                current_shot = (
                    review_image, review_w or width, review_h or height,
                    x11.image_signature(png),
                )
                await emit("operator.screenshot", {
                    "step": steps, "image": review_image,
                    "width": current_shot[1], "height": current_shot[2],
                    "progress_review": True,
                })
                try:
                    review = await review_progress(
                        task, checkpoint_step, steps,
                        (checkpoint_shot or current_shot)[0], current_shot[0],
                        "\n".join(review_history), cfg)
                except models.ModelError as exc:
                    issue = f"progress review failed at step {steps}: {exc}"
                    await emit("operator.failed", {
                        "steps": steps, "error": issue, "stage": "progress_review"})
                    return {"status": "fail", "summary": issue, "steps": steps}

                await emit("operator.progress_review", {
                    "step": steps, "checkpoint_step": checkpoint_step, **review,
                })
                if not review.get("progress"):
                    issue = str(review.get("issue") or "no meaningful progress was made")
                    attempts = str(review.get("attempts") or "").strip()
                    summary = f"Stopped after a progress review at step {steps}: {issue}"
                    if attempts:
                        summary += f" Tried: {attempts}"
                    await emit("operator.stuck", {
                        "steps": steps, "checkpoint_step": checkpoint_step,
                        "issue": issue, "attempts": attempts,
                    })
                    return {"status": "stopped", "summary": summary, "steps": steps,
                            "issue": issue}

                next_approach = str(review.get("next_approach")
                                    or "continue toward the task")
                # The independent checkpoint sees the post-action screen and
                # action trace. Its positive decision is a verified higher-level
                # postcondition for actions whose local pixel/control check was
                # inconclusive.
                last_postcondition_failed = False
                no_postcondition_actions = 0
                history.append(
                    f"[progress review at step {steps}] Progress confirmed. "
                    f"Next approach: {next_approach[:500]}")
                history = history[-MAX_HISTORY_STEPS:]
                review_history.clear()
                checkpoint_shot = current_shot
                checkpoint_step = steps

            need_screen = bool(parsed.get("need_screen", True)) or bool(actions)
    finally:
        await x11.release_all(cfg)
