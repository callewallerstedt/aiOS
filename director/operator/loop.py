"""The pixel operator loop.

Screenshot -> model -> actions -> repeat, until the model says done, the step
budget runs out, or the user stops it. Every step emits events so the phone can
watch the work happen and step in.
"""
from __future__ import annotations

import asyncio
import json
import re
import shlex
from typing import Any, Awaitable, Callable

from .. import config, models
from . import display as display_mod
from . import prompts, x11

Emit = Callable[[str, dict], Awaitable[None]]

MAX_HISTORY_STEPS = 8
JSON_BLOCK = re.compile(r"(?s)\{.*\}")


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
        await x11.type_text(text, settings)
        return f"type {text[:40]!r}"
    if kind == "key":
        await x11.press(str(action.get("key") or ""), int(action.get("presses") or 1), settings)
        return f"key {action.get('key')}"
    if kind == "hotkey":
        keys = [str(k) for k in (action.get("keys") or [])]
        await x11.hotkey(keys, settings)
        return f"hotkey {'+'.join(keys)}"
    if kind == "key_down":
        await x11.key_down(str(action.get("key") or ""), settings)
        return f"hold {action.get('key')}"
    if kind == "key_up":
        await x11.key_up(str(action.get("key") or ""), settings)
        return f"release {action.get('key') or 'modifiers'}"
    if kind == "scroll":
        await x11.scroll(x, y, int(action.get("dy") or -3), settings)
        return f"scroll {action.get('dy')}"
    if kind == "wait":
        seconds = max(0.0, min(float(action.get("seconds") or 0.5), 10.0))
        await asyncio.sleep(seconds)
        return f"wait {seconds}s"
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
                   max_steps: int = 0) -> dict:
    """Drive the screen until the task is done. Returns a result summary."""
    cfg = settings if settings is not None else config.load_settings()
    operator_cfg = cfg.get("operator", {}) or {}
    cancel = cancel or asyncio.Event()
    budget = int(max_steps or operator_cfg.get("max_steps") or 40)

    state = await display_mod.ensure_running(cfg, with_chrome=True)
    if not state.get("ready"):
        return {"status": "fail", "summary": "the operator display is not running "
                                             f"({state.get('units')})", "steps": 0}

    history: list[str] = []
    need_screen = True
    last_shot: tuple[str, int, int] | None = None
    steps = 0

    await emit("operator.started", {"task": task, "display": state.get("display"),
                                    "max_steps": budget})

    while steps < budget:
        if cancel.is_set():
            await emit("operator.stopped", {"reason": "stopped by user", "steps": steps})
            return {"status": "stopped", "summary": "stopped by user", "steps": steps}
        steps += 1

        width, height = await x11.screen_size(cfg)
        if need_screen or last_shot is None:
            png = await x11.capture(cfg)
            data_url, shot_w, shot_h = x11.encode_jpeg(png)
            last_shot = (data_url, shot_w or width, shot_h or height)
            await emit("operator.screenshot", {"step": steps, "image": data_url,
                                               "width": last_shot[1], "height": last_shot[2]})
        data_url, shot_w, shot_h = last_shot
        factor = (width / float(shot_w)) if shot_w else 1.0

        windows = await x11.window_list(settings=cfg)
        message = prompts.task_message(task, shot_w, shot_h,
                                       "\n".join(history[-MAX_HISTORY_STEPS:]), windows)
        items = [models.user_message([models.text_part(message), models.image_part(data_url)])]

        try:
            reply = await models.complete(
                backend=str(operator_cfg.get("backend") or ""),
                model=str(operator_cfg.get("model") or ""),
                reasoning=str(operator_cfg.get("reasoning") or ""),
                instructions=prompts.SYSTEM_PROMPT,
                items=items, tools=[], timeout=180.0, settings=cfg)
        except models.ModelError as exc:
            await emit("operator.failed", {"error": str(exc), "step": steps})
            return {"status": "fail", "summary": str(exc), "steps": steps}

        parsed = parse_reply(reply.get("text") or "")
        thought = str(parsed.get("thought") or "")
        status = str(parsed.get("status") or "continue").lower()
        actions = scale_actions(parsed.get("actions") or [], factor)

        await emit("operator.step", {
            "step": steps, "thought": thought, "status": status,
            "message": str(parsed.get("message") or ""),
            "actions": [str(a.get("type") or "?") for a in actions],
        })

        performed: list[str] = []
        for action in actions:
            if cancel.is_set():
                break
            try:
                performed.append(await execute(action, cfg))
            except Exception as exc:  # one bad action must not kill the run
                performed.append(f"{action.get('type')} failed: {exc}")
            await asyncio.sleep(0.08)

        if performed:
            await emit("operator.actions", {"step": steps, "performed": performed})

        history.append(f"[{steps}] {thought[:400]}\n    did: " +
                       ("; ".join(performed)[:400] if performed else "(nothing)"))

        if status == "done":
            await emit("operator.done", {"steps": steps, "summary": thought})
            return {"status": "done", "summary": thought or "done", "steps": steps}
        if status == "fail":
            await emit("operator.failed", {"steps": steps, "error": thought})
            return {"status": "fail", "summary": thought or "failed", "steps": steps}
        if status in ("ask", "handoff") and ask_user is not None:
            question = thought or str(parsed.get("message") or "")
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

        need_screen = bool(parsed.get("need_screen", True)) or bool(actions)

    await emit("operator.stopped", {"reason": "step budget reached", "steps": steps})
    return {"status": "stopped", "summary": f"hit the {budget}-step budget", "steps": steps}
