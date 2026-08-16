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
from typing import Any, Awaitable, Callable

from .. import config, models, store
from . import display as display_mod
from . import prompts, x11

Emit = Callable[[str, dict], Awaitable[None]]

MAX_HISTORY_STEPS = 12
MEANINGFUL_SCREEN_CHANGE = 0.005
MAX_REPEATED_NO_EFFECT_ROUNDS = 3
MAX_DISTINCT_ACTIONS_ON_SCREEN = 12
# The operator's own memory, kept apart from the coordinator's so notes about
# which button moved and where a login lives do not fill every chat's prompt.
MEMORY_SCOPE = "operator"
RECALL_RUNS = 6
JSON_BLOCK = re.compile(r"(?s)\{.*\}")


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
    notes = store.list_memory(scope=MEMORY_SCOPE, limit=40)
    if notes:
        lines.append("WHAT YOU LEARNED ON THIS SCREEN BEFORE:")
        lines += [f"- {row['key']}: {row['value']}" for row in notes]
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
    thought = str(args.pop("thought", "") or reply.get("reasoning")
                  or reply.get("text") or "").strip()
    if name == "finish":
        return {"thought": thought, "status": str(args.get("status") or "done"),
                "actions": [], "message": str(args.get("message") or ""),
                "native_tool": name}
    kind = TOOL_ACTIONS.get(name)
    if not kind:
        return {"thought": thought, "status": "fail", "actions": [],
                "message": f"unknown operator tool {name!r}", "native_tool": name}
    return {"thought": thought, "status": "continue",
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


async def execute(action: dict, settings: dict) -> str:
    """Run one action; return a one-line description for the log."""
    kind = str(action.get("type") or "").strip().lower()
    x, y = action.get("x"), action.get("y")

    if kind == "move":
        await x11.move(x, y, settings)
        return f"move ({x},{y})"
    if kind == "click":
        await x11.click(x, y, str(action.get("button") or "left"),
                        int(action.get("clicks") or 1), settings)
        return f"click ({x},{y})"
    if kind == "double_click":
        await x11.click(x, y, "left", 2, settings)
        return f"double click ({x},{y})"
    if kind == "right_click":
        await x11.click(x, y, "right", 1, settings)
        return f"right click ({x},{y})"
    if kind == "drag":
        start, end = action.get("from") or [x, y], action.get("to") or [x, y]
        await x11.drag((int(start[0]), int(start[1])), (int(end[0]), int(end[1])),
                       str(action.get("button") or "left"), settings=settings)
        return f"drag {start} -> {end}"
    if kind == "path":
        points = action.get("points") or []
        await x11.stroke(points, str(action.get("button") or "left"), settings=settings)
        return f"stroke through {len(points)} points"
    if kind == "mouse_down":
        await x11.mouse_down(x, y, str(action.get("button") or "left"), settings)
        return f"mouse down ({x},{y})"
    if kind == "mouse_up":
        await x11.mouse_up(x, y, str(action.get("button") or "left"), settings)
        return f"mouse up ({x},{y})"
    if kind == "type":
        text = str(action.get("text") or "")
        if x is None or y is None:
            await x11.type_text(text, settings)
        else:
            await x11.type_text(text, settings, x=x, y=y,
                                replace=bool(action.get("replace")))
        # A typed value can be a one-time verification code. Keep its content
        # out of the persisted action trace while still recording the action.
        return f"focused ({x},{y}) and typed {len(text)} characters"
    if kind == "key":
        await x11.press(str(action.get("key") or ""), int(action.get("presses") or 1), settings)
        return f"key {action.get('key')}"
    if kind == "hotkey":
        keys = [str(k) for k in (action.get("keys") or [])]
        await x11.hotkey(keys, settings)
        return f"hotkey {'+'.join(keys)}"
    if kind == "select_all":
        await x11.hotkey(["ctrl", "a"], settings)
        return "selected all text in the focused field"
    if kind == "remember":
        key = str(action.get("key") or "").strip()[:80]
        value = str(action.get("value") or "").strip()[:2000]
        if not key or not value:
            return "remember: needs both a key and what to remember"
        store.remember(key, value, scope=MEMORY_SCOPE)
        return f"remembered {key}: {value[:120]}"
    if kind == "key_down":
        await x11.key_down(str(action.get("key") or ""), settings)
        return f"hold {action.get('key')}"
    if kind == "key_up":
        await x11.key_up(str(action.get("key") or ""), settings)
        return f"release {action.get('key') or 'modifiers'}"
    if kind == "scroll":
        await x11.scroll(x, y, int(action.get("dy") or 3), settings)
        return f"scroll {action.get('dy')}"
    if kind == "wait":
        seconds = max(0.0, min(float(action.get("seconds") or 0.5), 10.0))
        await asyncio.sleep(seconds)
        return f"wait {seconds}s"
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
                return "screen changed"
            if condition == "stable":
                if changed:
                    stable_since = asyncio.get_running_loop().time()
                elif asyncio.get_running_loop().time() - stable_since >= stable_for:
                    return "screen became stable"
            previous = current
        return f"screen did not become {condition} within {timeout}s"
    if kind == "open_url":
        url = str(action.get("url") or "")
        return await display_mod.launch_chrome(url, settings)
    if kind == "launch":
        command = str(action.get("command") or "").strip()
        if not command:
            return "launch: no command"
        argv = shlex.split(command)
        await asyncio.create_subprocess_exec(
            *argv, env=display_mod.display_env(settings),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True)
        await asyncio.sleep(1.5)
        return f"launched {command}"
    if kind == "shell":
        command = str(action.get("command") or "").strip()
        code, out = await x11.run(["bash", "-lc", command], settings, timeout=90)
        clipped = out[:1500]
        return f"shell exit {code}\n{clipped}"
    return f"unknown action {kind!r}"


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
    need_screen = True
    last_shot: tuple[str, int, int, bytes] | None = None
    checkpoint_shot: tuple[str, int, int, bytes] | None = None
    checkpoint_step = 0
    steps = 0
    last_performed = ""
    notes: list[str] = []
    actions_on_screen: set[tuple[Any, ...]] = set()
    screen_state_signature = b""
    repeated_no_effect_rounds = 0
    max_repeated_no_effect = max(
        2, int(operator_cfg.get("max_repeated_no_effect_rounds")
               or MAX_REPEATED_NO_EFFECT_ROUNDS))
    max_distinct_actions = max(
        max_repeated_no_effect + 1,
        int(operator_cfg.get("max_distinct_actions_on_screen")
            or MAX_DISTINCT_ACTIONS_ON_SCREEN))
    meaningful_change = max(
        0.0001, min(float(operator_cfg.get("meaningful_screen_change")
                         or MEANINGFUL_SCREEN_CHANGE), 1.0))

    recalled = background()
    await emit("operator.started", {"task": task, "display": state.get("display"),
                                    "review_every": review_interval,
                                    "recalled": bool(recalled)})

    while True:
        if cancel.is_set():
            await emit("operator.stopped", {"reason": "stopped by user", "steps": steps})
            return {"status": "stopped", "summary": "stopped by user", "steps": steps}
        steps += 1

        # Anything Calle said since the last step outranks the original brief.
        # There is one screen, so steering the run in flight is the only way to
        # correct it without killing it and starting over.
        if follow_ups is not None:
            for note in follow_ups():
                notes.append(note)
                history.append(f"[{steps}] Calle added: {note[:400]}")
                await emit("operator.note", {"step": steps, "text": note})

        width, height = await x11.screen_size(cfg)
        screen_unchanged = False
        if need_screen or last_shot is None:
            png = await x11.capture(cfg)
            data_url, shot_w, shot_h = x11.encode_jpeg(png)
            signature = x11.image_signature(png)
            last_shot = (data_url, shot_w or width, shot_h or height, signature)
            if screen_state_signature:
                screen_unchanged = (
                    x11.image_change_ratio(screen_state_signature, signature)
                    < meaningful_change
                )
                if not screen_unchanged:
                    screen_state_signature = signature
                    repeated_no_effect_rounds = 0
                    actions_on_screen.clear()
            else:
                screen_state_signature = signature
            await emit("operator.screenshot", {"step": steps, "image": data_url,
                                               "width": last_shot[1], "height": last_shot[2]})
            if checkpoint_shot is None:
                checkpoint_shot = last_shot
        data_url, shot_w, shot_h, _signature = last_shot
        factor = (width / float(shot_w)) if shot_w else 1.0
        feedback = ""
        if screen_unchanged and last_performed:
            feedback = (
                f"The screen is unchanged after: {last_performed[:300]}. "
                "Do not repeat that action; choose a different method.")
        if (repeated_no_effect_rounds >= max_repeated_no_effect
                or len(actions_on_screen) >= max_distinct_actions):
            issue = (
                "The operator exhausted distinct approaches or repeated actions that had no "
                "visible effect on the same screen, so it stopped instead of looping."
            )
            await emit("operator.stuck", {
                "steps": steps, "issue": issue,
                "attempts": sorted(str(item) for item in actions_on_screen),
            })
            return {"status": "stopped", "summary": issue, "steps": steps,
                    "issue": issue}

        windows = await x11.window_list(settings=cfg)
        controls = await x11.accessible_controls(settings=cfg)
        message = prompts.task_message(task, shot_w, shot_h,
                                       "\n".join(history[-MAX_HISTORY_STEPS:]),
                                       windows, feedback, notes=notes, step=steps,
                                       background=recalled if steps == 1 else "",
                                       controls=controls)
        items = [models.user_message([models.text_part(message), models.image_part(data_url)])]

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
        thought = str(parsed.get("thought") or "")
        status = str(parsed.get("status") or "continue").lower()
        actions = scale_actions(parsed.get("actions") or [], factor)

        await emit("operator.step", {
            "step": steps, "thought": thought, "status": status,
            "message": str(parsed.get("message") or ""),
            "actions": [str(a.get("type") or "?") for a in actions],
            "native_tool": str(parsed.get("native_tool") or ""),
            "model": str(reply.get("model") or operator_cfg.get("model") or ""),
        })

        performed: list[str] = []
        no_effect_action = False
        for action in actions:
            if cancel.is_set():
                break
            signature = action_signature(action)
            if screen_unchanged and signature is not None and signature in actions_on_screen:
                performed.append(
                    f"{action.get('type')} blocked: that action already had no visible effect "
                    "on this screen")
                no_effect_action = True
                continue
            try:
                performed.append(await execute(action, cfg))
                if signature is not None:
                    actions_on_screen.add(signature)
            except Exception as exc:  # one bad action must not kill the run
                performed.append(f"{action.get('type')} failed: {exc}")
                no_effect_action = True
                if signature is not None:
                    actions_on_screen.add(signature)
            await asyncio.sleep(0.08)

        if screen_unchanged:
            repeated_no_effect_rounds = (
                repeated_no_effect_rounds + 1 if no_effect_action else 0)

        if performed:
            await emit("operator.actions", {"step": steps, "performed": performed})
        last_performed = "; ".join(performed)

        step_record = (f"[{steps}] {thought[:400]}\n    did: " +
                       ("; ".join(performed)[:400] if performed else "(nothing)"))
        history.append(step_record)
        review_history.append(step_record)

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
            if str(answer or "").strip().lower().startswith("cancel"):
                return {"status": "stopped", "summary": "cancelled at handoff", "steps": steps}
            history.append(f"[{steps}] Calle answered: {answer}")
            need_screen = True
            continue
        if status in ("ask", "handoff"):
            return {"status": "blocked", "summary": thought or "needs a human", "steps": steps}

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
                "step": steps, "checkpoint_step": checkpoint_step,
                **review,
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

            next_approach = str(review.get("next_approach") or "continue toward the task")
            history.append(
                f"[progress review at step {steps}] Progress confirmed. "
                f"Next approach: {next_approach[:500]}")
            history = history[-MAX_HISTORY_STEPS:]
            review_history.clear()
            checkpoint_shot = current_shot
            checkpoint_step = steps
            last_shot = current_shot
            need_screen = False
            continue

        need_screen = bool(parsed.get("need_screen", True)) or bool(actions)
