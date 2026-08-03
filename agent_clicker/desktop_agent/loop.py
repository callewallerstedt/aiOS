"""Agent loop: task -> screenshot -> reason -> actions -> repeat."""
from __future__ import annotations
import json
import os
import re
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import getpass
import locale
import platform
import socket
from datetime import datetime, timezone

from PIL import Image

from agent import vlm


def _now_block() -> str:
    """The clock, spelled out. Models reason badly about "tomorrow" without it."""
    now = datetime.now(timezone.utc).astimezone()
    tz = now.strftime("%Z") or now.strftime("%z")
    return (
        f"Now: {now.strftime('%A %d %B %Y, %H:%M')} ({tz}, UTC{now.strftime('%z')}) · "
        f"ISO {now.strftime('%Y-%m-%d')} · week {now.isocalendar().week}"
    )


def _system_info_block(monitor=None, shell_enabled: bool = False) -> str:
    try:
        host = socket.gethostname()
    except Exception:
        host = "?"
    try:
        user = getpass.getuser()
    except Exception:
        user = "?"
    try:
        lang = locale.getdefaultlocale()[0] or "?"
    except Exception:
        lang = "?"
    screen = ""
    if monitor is not None:
        screen = (f"Controlled screen: {getattr(monitor, 'width', '?')}x{getattr(monitor, 'height', '?')} "
                  f"at ({getattr(monitor, 'left', 0)},{getattr(monitor, 'top', 0)}) "
                  f"— {getattr(monitor, 'label', 'display')}\n"
                  "You ONLY see and click this monitor. Apps may open on another "
                  "display; if a launched window is missing from the screenshot, "
                  "use ensure_on_monitor before continuing.\n")
    other = ""
    try:
        from .screen import list_monitors
        others = [
            m for m in list_monitors()
            if getattr(m, "index", 0) != 0
            and (
                monitor is None
                or getattr(m, "left", None) != getattr(monitor, "left", None)
                or getattr(m, "top", None) != getattr(monitor, "top", None)
            )
        ]
        if others:
            bits = [
                f"{m.label} @ ({m.left},{m.top}) {m.width}x{m.height}"
                for m in others
            ]
            other = "Other monitors: " + "; ".join(bits) + "\n"
    except Exception:
        other = ""
    return (
        "--- SYSTEM INFO ---\n"
        f"OS: {platform.system()} {platform.release()} ({platform.version()})\n"
        f"Machine: {platform.machine()}\n"
        f"User: {user}@{host}\n"
        f"Locale: {lang}\n"
        + screen
        + other
        + f"PowerShell available: {'yes' if shell_enabled else 'no'}\n"
        + f"{_now_block()}\n"
        "Anything relative — today, tomorrow, this Friday, next month, "
        "'the latest' — is measured from that clock. Each step's CONTEXT line "
        "carries the current time; use it instead of guessing.\n"
        "--- END SYSTEM INFO ---\n"
    )
from agent.config import MODEL as DEFAULT_MODEL
from .prompts import SYSTEM_PROMPT
from .progress import LoopWatch, action_signature, checklist_block, frame_fingerprint, parse_plan
from .screen import Monitor, capture_for_agent as capture
from .actions import execute, ExecResult, any_button_held, any_key_held, release_all


# Where per-run debug folders go (sibling of this package).
DEBUG_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "debug_runs")


def _env_float(name: str, default: float) -> float:
    try:
        value = float(str(os.environ.get(name, "")).strip())
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


# A model call that never returns used to freeze the whole run: the loop has no
# cancellation point inside it, so even Stop did nothing. Every call now runs on
# a worker thread the loop will walk away from.
MODEL_CALL_TIMEOUT = _env_float("AIOS_MODEL_CALL_TIMEOUT", 420.0)
# Backstop for anything else that can block forever (a wedged capture, a driver
# call). Time spent waiting on the user's answer to an ASK does not count.
STALL_ABORT_SEC = _env_float("AIOS_STALL_ABORT_SEC", 900.0)

PLANNER_SYSTEM_PROMPT = """You are the pre-run planner for a desktop computer-use agent.
Study the user's task and current screenshot, then produce a short execution plan AND a
todo list the agent will tick off as it works.

Return exactly one JSON object:

{
  "plan":      "a few sentences: the app to use, the approach, the risky parts",
  "todo":      ["one concrete milestone per item, in order, 3-10 items"],
  "done_when": ["observable end conditions someone could confirm from a screenshot"]
}

Rules:
- Each todo item is a milestone the agent can finish and see finished, not a
  mouse movement. "Open the compose window" — not "click at 1200,400".
- done_when describes what the SCREEN must show when the task is genuinely
  complete, in the user's terms. If the user asked to send something, the
  proof is that it was sent — not that a draft exists.
- Cover the whole of the user's request, including parts that are easy to
  forget (attachments, recipients, saving, confirming a dialog).
- No coordinates. Never claim the task is already done."""

VERIFIER_SYSTEM_PROMPT = """You check whether a desktop computer-use agent actually did
what the user asked, before the run is allowed to finish.

You are given the user's TASK, the agent's own closing message, its todo list and
end conditions, and the FINAL SCREENSHOT of the desktop.

Return exactly one JSON object:

{"verdict":"pass"|"fail", "reason":"one line of evidence", "missing":["what is still undone"]}

Judge from the screenshot, not from the agent's confidence. The agent claiming
success is not evidence. If the screen does not show the requested outcome, or
shows only part of it, the verdict is "fail" and `missing` says exactly what is
left. Be fair: judge the user's request as asked, not a stricter version of it,
and pass work that is genuinely finished even if the route differed from the
plan. If the task's result simply cannot be confirmed from a screenshot (it
happened in a file, or off-screen), say so in `reason` and pass unless
something visible contradicts it."""


def _slug(s: str, maxlen: int = 40) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")
    return (s[:maxlen] or "task").lower()


def _scale_model_actions(actions: list, image_scale: float,
                         width: int, height: int) -> list:
    """Translate coordinates from the image sent to the VLM to monitor pixels.

    The screenshot encoder may shrink a 1920px capture to 1600px. Vision
    models naturally report coordinates in the pixels they actually received,
    so execution must undo that resize exactly once.
    """
    if not isinstance(actions, list):
        return []
    try:
        factor = 1.0 / float(image_scale)
    except (TypeError, ValueError, ZeroDivisionError):
        factor = 1.0

    def coord(value, limit):
        try:
            return max(0, min(limit - 1, int(round(float(value) * factor))))
        except (TypeError, ValueError):
            return value

    translated = []
    for raw_action in actions:
        if not isinstance(raw_action, dict):
            translated.append(raw_action)
            continue
        action = dict(raw_action)
        action_type = str(action.get("type") or "").lower()
        if action_type not in {"mouse_rel", "mouse_move_rel", "look"}:
            if "x" in action:
                action["x"] = coord(action["x"], width)
            if "y" in action:
                action["y"] = coord(action["y"], height)
            for key, limit in (("x1", width), ("x2", width),
                               ("y1", height), ("y2", height)):
                if key in action:
                    action[key] = coord(action[key], limit)
            for key in ("from", "to"):
                point = action.get(key)
                if isinstance(point, (list, tuple)) and len(point) >= 2:
                    action[key] = [coord(point[0], width), coord(point[1], height), *point[2:]]
            points = action.get("points")
            if isinstance(points, list):
                action["points"] = [
                    [coord(point[0], width), coord(point[1], height), *point[2:]]
                    if isinstance(point, (list, tuple)) and len(point) >= 2 else point
                    for point in points
                ]
        translated.append(action)
    return translated


def _sanitize_messages_for_disk(messages: list, step_dir: str) -> list:
    """Strip base64 image payloads from a messages list so the dumped request.json
    is small + readable. Replaces each inline image_url with {'image_ref': '<path>'},
    pointing at PNGs written next to the step folder."""
    out = []
    img_n = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            new_parts = []
            for p in content:
                if isinstance(p, dict) and p.get("type") == "image_url":
                    url = (p.get("image_url") or {}).get("url", "")
                    if url.startswith("data:image/"):
                        try:
                            import base64
                            head, b64 = url.split(",", 1)
                            ext = "png" if "png" in head else "jpg"
                            img_n += 1
                            fn = f"req_img_{img_n:02d}.{ext}"
                            with open(os.path.join(step_dir, fn), "wb") as f:
                                f.write(base64.b64decode(b64))
                            new_parts.append({"type": "image_ref", "ref": fn,
                                              "detail": (p.get("image_url") or {}).get("detail")})
                        except Exception as e:
                            new_parts.append({"type": "image_url",
                                              "image_url_error": str(e)})
                    else:
                        new_parts.append({"type": "image_url", "url": url[:200]})
                else:
                    new_parts.append(p)
            out.append({"role": m.get("role"), "content": new_parts})
        else:
            out.append(m)
    return out


# Actions worth snapshotting after — i.e. that meaningfully change UI state.
# Skipped: "move", "scroll", "wait", "mouse_down"/"mouse_up" (covered by drag/path).
KEY_ACTION_TYPES = {
    "click", "right_click", "double_click", "drag", "path",
    "type", "hotkey", "key_combo", "key",
    "key_hold", "tap_hold",  # press+release pairs are worth a screenshot
    # Note: key_down / key_up / mouse_rel are intentionally omitted — they're
    # typically part of a held-input chain where snapshotting between them
    # would freeze the action and the screen barely changes per call.
}
MID_TRAIL_MAX = 3  # how many mid-step screenshots to carry into the next round
# Actions that change what is on screen — next step should see a fresh capture.
UI_ACTION_TYPES = frozenset({
    "move", "click", "right_click", "double_click", "drag", "path",
    "mouse_down", "mouse_up", "type", "hotkey", "key_combo", "key",
    "scroll", "key_down", "key_up", "key_hold", "tap_hold", "mouse_rel",
})
# Text-only steps (shell stdout, write_file) do not need a fresh screenshot
# unless the model explicitly asks for one.
TEXT_ACTION_TYPES = frozenset({"shell", "write_file", "wait"})


@dataclass
class StepRecord:
    n: int
    thought: str = ""
    message: str = ""
    say: str = ""
    status: str = ""
    actions: list = field(default_factory=list)
    results: list = field(default_factory=list)
    screenshot_ms: int = 0
    think_ms: int = 0
    act_ms: int = 0
    raw: str = ""
    error: str = ""


class AgentLoop:
    """Owns the worker thread. Emits events via `on_event(dict)`.

    Events:
      {"type":"step_begin", "n":int}
      {"type":"screenshot", "n":int, "image": PIL.Image, "size":[w,h]}
      {"type":"thought",    "n":int, "thought":str, "message":str, "status":str,
                            "actions":[...], "elapsed_ms":int, "raw":str}
      {"type":"action_done","n":int, "i":int, "result": ExecResult dict-ish}
      {"type":"step_end",   "n":int, "record": StepRecord-ish}
      {"type":"done",       "ok":bool, "message":str, "steps":int}
      {"type":"log",        "msg":str}
    """

    def __init__(self, on_event: Callable[[dict], None]):
        self._event_sink = on_event
        self._active_event_wrapper = None
        self.on_event = on_event
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._pause.set()  # set = not paused
        self._follow_ups: list[str] = []
        self._follow_ups_lock = threading.Lock()
        self._awaiting_answer = False
        self._awaiting_more_steps = False
        self._step_grant = 0
        # Stall detection: bumped on every emitted event. Waiting on the user's
        # ASK answer is not a stall, so it is excluded explicitly.
        self._last_event_at = time.monotonic()
        self._done_emitted = False
        # Bumped per run. A run abandoned by the stall watchdog may still be
        # blocked in a call that returns much later; the generation lets that
        # zombie recognise it has been superseded instead of emitting events
        # into — or acting on behalf of — whatever run is current now.
        self._generation = 0

    def is_running(self) -> bool:
        # A run the stall watchdog gave up on may leave its thread wedged in a
        # call that never returns. Once `done` is out the run is over as far as
        # everything else is concerned — otherwise the user could never start
        # another one.
        if self._done_emitted:
            return False
        return self._thread is not None and self._thread.is_alive()

    def stop(self):
        self._stop.set()
        self._pause.set()  # in case it's paused
        # safety: drop any held buttons so the OS isn't left mid-drag
        release_all()

    def pause(self):
        self._pause.clear()

    def resume(self):
        self._pause.set()

    def is_paused(self) -> bool:
        return not self._pause.is_set()

    def is_awaiting_answer(self) -> bool:
        return self._awaiting_answer or self._awaiting_more_steps

    def grant_steps(self, extra: int) -> None:
        """Queue another step budget when the run is waiting at the step cap."""
        try:
            amount = int(extra)
        except (TypeError, ValueError):
            return
        if amount < 1:
            return
        self._step_grant = max(self._step_grant, min(200, amount))

    def add_follow_up(self, text: str, images: list | None = None,
                      extra_steps: int | None = None) -> bool:
        text = (text or "").strip()
        images = images or []
        if not self.is_running():
            return False
        if not text and not images and extra_steps is None and not self._awaiting_more_steps:
            return False
        # Queue and un-pause under one lock. Doing the check afterwards left a
        # window where an answer arriving just before the loop armed the wait
        # was queued but never woke it, and the run hung with the reply already
        # in hand.
        with self._follow_ups_lock:
            if text or images:
                self._follow_ups.append({"text": text, "images": list(images)})
            answering_ask = self._awaiting_answer or self._awaiting_more_steps
            if extra_steps is not None:
                self.grant_steps(extra_steps)
            if answering_ask:
                self._awaiting_answer = False
                self._awaiting_more_steps = False
                self._pause.set()
        self.on_event({"type": "follow_up_received", "text": text,
                       "answering_ask": answering_ask,
                       "images": len(images)})
        return True

    def _drain_follow_ups(self) -> list[dict]:
        with self._follow_ups_lock:
            items = list(self._follow_ups)
            self._follow_ups.clear()
        return items

    def start(self, task: str, monitor: Monitor, model: str = DEFAULT_MODEL,
              max_steps: int = 25, action_delay: float = 0.20,
              settle_after_step: float = 0.6,
              shell_enabled: bool = False, shell_cwd: str | None = None,
              backend: str = "api", reasoning_effort: str | None = None,
              mid_screenshots: str = "key",
              attachments: list[dict] | None = None,
              user_context: str = "", planner_model: str = ""):
        """mid_screenshots: 'off' | 'key' — capture an extra screenshot after
        each state-changing action so the next round sees the process trail.

        attachments: optional list of dicts the user attached to the task.
          Each dict is one of:
            {"kind": "image", "image": PIL.Image, "name": str}
            {"kind": "text",  "text": str,        "name": str}
          Injected into the FIRST step's user message so the agent can see them.
        """
        if self.is_running():
            raise RuntimeError("loop already running")
        # Some embedders replace the public callback between construction and
        # start. Preserve that supported pattern while never adopting a stale
        # wrapper left by an earlier run.
        if self.on_event is not self._active_event_wrapper:
            self._event_sink = self.on_event
        self._stop.clear()
        self._pause.set()
        # Per-run state — without this a second run would be born "already
        # finished" and every one of its events would be swallowed.
        self._done_emitted = False
        self._awaiting_answer = False
        self._awaiting_more_steps = False
        self._step_grant = max(1, int(max_steps or 25))
        self._last_event_at = time.monotonic()
        self._generation += 1
        with self._follow_ups_lock:
            self._follow_ups.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(task, monitor, model, max_steps, action_delay, settle_after_step,
                  shell_enabled, shell_cwd, backend, reasoning_effort, mid_screenshots,
                  attachments or [], user_context, planner_model),
            daemon=True,
        )
        self._thread.start()

    # -----------------------------------------------------------

    def _call_model(self, system, msgs, *, model, backend, reasoning_effort,
                    timeout: float | None = None) -> tuple[str, dict]:
        """`vlm.chat_raw` that cannot hang the run.

        The call happens on a throwaway worker thread. If it overruns we raise
        TimeoutError and abandon the thread — it is a daemon and will die with
        the process. The caller's `except` turns that into a normal step error,
        so the run retries instead of freezing forever.

        Usage is collected on the worker thread and returned, because
        `vlm.take_last_usage()` is thread-local: reading it from here, after the
        call ran elsewhere, would silently report zero tokens for every step.
        """
        timeout = timeout or MODEL_CALL_TIMEOUT
        box: dict = {}

        def worker():
            try:
                raw = vlm.chat_raw(system, msgs, model=model, backend=backend,
                                   reasoning_effort=reasoning_effort)
                box["result"] = (raw, vlm.take_last_usage())
            except BaseException as exc:  # noqa: BLE001 — re-raised on the loop thread
                box["error"] = exc

        thread = threading.Thread(target=worker, daemon=True,
                                  name="aiOS-model-call")
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            raise TimeoutError(
                f"model call exceeded {timeout:.0f}s with no reply "
                f"(backend={backend}, model={model}); abandoning this step"
            )
        if "error" in box:
            raise box["error"]
        raw, usage = box.get("result") or ("", {})
        return raw or "", usage if isinstance(usage, dict) else {}

    def _wait_unpaused(self):
        while not self._pause.is_set():
            if self._stop.is_set(): return
            time.sleep(0.05)

    def _sleep_cancelable(self, seconds: float):
        end = time.time() + max(0.0, float(seconds))
        while not self._stop.is_set():
            remaining = end - time.time()
            if remaining <= 0:
                return
            time.sleep(min(0.05, remaining))

    def _run(self, task, monitor, model, max_steps, action_delay, settle_after_step,
             shell_enabled=False, shell_cwd=None, backend="api",
             reasoning_effort=None, mid_screenshots="key", attachments=None,
             user_context="", planner_model=""):
        attachments = attachments or []
        my_gen = self._generation

        def _superseded() -> bool:
            return my_gen != self._generation

        user_context = (user_context or "").strip()
        effective_system = SYSTEM_PROMPT + "\n\n" + _system_info_block(monitor, shell_enabled)
        if user_context:
            effective_system = (
                effective_system
                + "\n--- USER CONTEXT (persistent settings from the UI) ---\n"
                + user_context
                + "\n--- END USER CONTEXT ---\n"
            )
        # Pinned messages survive history trimming. The task, the plan and the
        # user's own follow-ups are exactly what a long run must not forget —
        # and dropping them at step nine is why long runs used to wander.
        pinned_msgs: list[dict] = []
        history_msgs: list[dict] = []
        plan_data = {"plan": "", "todo": [], "done_when": []}
        watch = LoopWatch()
        verify_failures = 0
        last_status = ""
        rec_actions_last = 0
        last_was_ask = False
        # Mid-step trail: screenshots captured AFTER actions in the prior step,
        # attached to the NEXT user message so the model sees the process.
        # Each entry: (PIL.Image, label_text). Capped at MID_TRAIL_MAX (newest kept).
        mid_trail: list[tuple] = []

        # ---------- Debug folder for this run ----------
        run_start = datetime.now()
        run_dir = os.path.join(
            DEBUG_ROOT,
            f"{run_start.strftime('%Y%m%d_%H%M%S')}_{_slug(task)}")
        try:
            os.makedirs(run_dir, exist_ok=True)
            with open(os.path.join(run_dir, "system_prompt.txt"), "w",
                      encoding="utf-8") as f:
                f.write(effective_system)
            with open(os.path.join(run_dir, "meta.json"), "w", encoding="utf-8") as f:
                json.dump({
                    "task": task,
                    "model": model, "backend": backend,
                    "planner_model": planner_model,
                    "reasoning_effort": reasoning_effort,
                    "mid_screenshots": mid_screenshots,
                    "max_steps": max_steps,
                    "action_delay": action_delay,
                    "shell_enabled": shell_enabled, "shell_cwd": shell_cwd,
                    "monitor": {"label": monitor.label, "left": monitor.left,
                                "top": monitor.top,
                                "width": monitor.width, "height": monitor.height},
                    "attachments": [{"kind": a.get("kind"), "name": a.get("name"),
                                     "size": (a["image"].size if a.get("image") else
                                              len(a.get("text") or ""))}
                                    for a in attachments],
                    "user_context_chars": len(user_context),
                    "start": run_start.isoformat(timespec="seconds"),
                    "status": "running",
                }, f, indent=2)
            # Save attachments themselves
            if attachments:
                att_dir = os.path.join(run_dir, "attachments")
                os.makedirs(att_dir, exist_ok=True)
                for i, a in enumerate(attachments):
                    base = f"{i:02d}_{_slug(a.get('name','att'), 30)}"
                    if a.get("kind") == "image" and a.get("image"):
                        try: a["image"].save(os.path.join(att_dir, base + ".png"))
                        except Exception: pass
                    elif a.get("kind") == "text":
                        try:
                            with open(os.path.join(att_dir, base + ".txt"),
                                      "w", encoding="utf-8") as f:
                                f.write(a.get("text") or "")
                        except Exception: pass
            transcript = open(os.path.join(run_dir, "transcript.jsonl"), "w",
                              encoding="utf-8")
            self.on_event({"type": "log", "msg": f"debug dir: {run_dir}"})
            self.on_event({"type": "debug_dir", "path": run_dir})
        except Exception as e:
            run_dir = None
            transcript = None
            self.on_event({"type": "log", "msg": f"debug dir setup failed: {e}"})

        # ---- helpers that close over run_dir/transcript ----
        def _write_event(ev: dict):
            if transcript is None: return
            try:
                safe = {k: v for k, v in ev.items()
                        if k not in ("image",)}  # PIL not JSON-able
                if "image" in ev:
                    safe["image"] = "<PIL.Image — see step folder>"
                transcript.write(json.dumps(safe, default=str) + "\n")
                transcript.flush()
            except Exception:
                pass

        def _step_dir(n: int) -> str | None:
            if run_dir is None: return None
            d = os.path.join(run_dir, f"step_{n:02d}")
            os.makedirs(d, exist_ok=True)
            return d

        # Wrap on_event so every event also lands in transcript.jsonl
        # Always wrap the permanent UI sink, not the previous run's wrapper.
        # Re-wrapping a stale closure caused every event after run one to be
        # treated as superseded, leaving follow-up HUDs and waiters blank.
        orig_on_event = self._event_sink
        usage_total = {
            "requests": 0, "input_tokens": 0, "output_tokens": 0,
            "cached_input_tokens": 0, "cache_write_input_tokens": 0, "total_tokens": 0,
            "backend": backend, "models": {},
        }
        initial_plan_usage = {}
        if "codex" in str(backend).lower():
            try:
                from agent import codex_backend
                initial_plan_usage = codex_backend.latest_plan_usage()
            except Exception:
                initial_plan_usage = {}
        if time.time() - float(initial_plan_usage.get("updated_at") or 0) > 600:
            initial_plan_usage = {}
        if initial_plan_usage:
            usage_total["plan_usage"] = {
                "start_used_percent": initial_plan_usage.get("used_percent"),
                "start_reset_at": initial_plan_usage.get("reset_at"),
                "window_minutes": initial_plan_usage.get("window_minutes"),
                "plan_type": initial_plan_usage.get("plan_type"),
                "measured": False,
            }

        _COUNTERS = (
            "requests", "input_tokens", "output_tokens", "cached_input_tokens",
            "cache_write_input_tokens", "total_tokens", "long_context_requests",
            "long_context_input_tokens", "long_context_output_tokens",
            "long_context_cached_input_tokens", "long_context_cache_write_input_tokens",
        )

        def _accumulate(into: dict, item: dict):
            for key in _COUNTERS:
                into[key] = int(into.get(key) or 0) + int(item.get(key) or 0)

        def _add_usage(item: dict):
            if not isinstance(item, dict):
                return
            _accumulate(usage_total, item)
            plan_sample = item.get("plan_usage")
            if isinstance(plan_sample, dict) and plan_sample.get("used_percent") is not None:
                plan_total = usage_total.setdefault("plan_usage", {})
                if plan_total.get("start_used_percent") is None:
                    # The first response establishes the current window, but
                    # cannot measure the model call that produced it.
                    plan_total["start_used_percent"] = plan_sample.get("used_percent")
                    plan_total["start_reset_at"] = plan_sample.get("reset_at")
                    plan_total["measured"] = False
                plan_total.update({
                    "end_used_percent": plan_sample.get("used_percent"),
                    "end_reset_at": plan_sample.get("reset_at"),
                    "window_minutes": plan_sample.get("window_minutes"),
                    "plan_type": plan_sample.get("plan_type"),
                })
                same_window = (
                    plan_total.get("start_reset_at")
                    and plan_total.get("start_reset_at") == plan_total.get("end_reset_at")
                )
                if same_window and initial_plan_usage:
                    start = float(plan_total.get("start_used_percent") or 0)
                    end = float(plan_total.get("end_used_percent") or 0)
                    plan_total["used_percent_delta"] = round(max(0.0, end - start), 2)
                    plan_total["measured"] = True
            used_model = str(item.get("model") or model)
            model_usage = usage_total["models"].setdefault(used_model, {})
            _accumulate(model_usage, item)
            # Split by backend as well: Codex calls ride the ChatGPT plan, so
            # only the API-key ones can be turned into a dollar figure.
            used_backend = str(item.get("backend") or backend)
            backend_usage = model_usage.setdefault("backends", {}).setdefault(used_backend, {})
            _accumulate(backend_usage, item)

        emit_lock = threading.Lock()

        def _emit(ev: dict):
            if _superseded():
                # This run was abandoned and another has started. Stay silent.
                return
            if ev.get("type") == "done":
                # The stall watchdog and the loop itself can both decide a run
                # is over. The UI must see exactly one `done`.
                with emit_lock:
                    if self._done_emitted:
                        return
                    self._done_emitted = True
                ev = dict(ev)
                ev["usage"] = json.loads(json.dumps(usage_total))
            self._last_event_at = time.monotonic()
            _write_event(ev)
            orig_on_event(ev)
        self._active_event_wrapper = _emit
        self.on_event = _emit  # type: ignore

        def _stall_watch():
            """Last line of defence: a run that emits nothing for long enough
            is declared stuck, so the UI gets an answer instead of a spinner
            that never resolves."""
            while not self._stop.is_set() and not self._done_emitted and not _superseded():
                time.sleep(2.0)
                if self._awaiting_answer or self.is_paused():
                    # Waiting on a human is not a stall.
                    self._last_event_at = time.monotonic()
                    continue
                idle = time.monotonic() - self._last_event_at
                if idle < STALL_ABORT_SEC:
                    continue
                self.on_event({"type": "log", "msg": (
                    f"stall watchdog: no activity for {idle:.0f}s — ending the run")})
                self.on_event({"type": "done", "ok": False, "steps": 0, "message": (
                    f"Stopped: the run stopped responding for {idle:.0f}s and was "
                    "ended so it would not hang. Nothing was reported by the last "
                    "step — try again, and if it keeps happening check the network.")})
                self._stop.set()
                self._pause.set()
                return

        threading.Thread(target=_stall_watch, daemon=True,
                         name="aiOS-stall-watch").start()

        try:
            planner_model = (planner_model or "").strip()
            if planner_model.lower() in {"off", "none", "disabled"}:
                planner_model = ""  # never let "off" reach the API as a model name
            if planner_model:
                if self._stop.is_set():
                    self.on_event({"type": "done", "ok": False, "message": "stopped by user", "steps": 0})
                    return
                self.on_event({"type": "planning_begin", "model": planner_model})
                try:
                    plan_image = capture(monitor)
                    plan_url, plan_scale = vlm.encode_image(plan_image)
                    plan_w = max(1, int(plan_image.width * plan_scale))
                    plan_h = max(1, int(plan_image.height * plan_scale))
                    plan_content = [
                        vlm.text_part(
                            f"TASK: {task}\nCurrent screenshot: {plan_w}x{plan_h}. "
                            "Create the execution plan for the clicking model."
                        ),
                        vlm.image_part(plan_url),
                    ]
                    raw_plan, plan_usage = self._call_model(
                        PLANNER_SYSTEM_PROMPT,
                        [{"role": "user", "content": plan_content}],
                        model=planner_model,
                        backend=backend,
                        reasoning_effort="high",
                    )
                    _add_usage(plan_usage)
                    plan_data = parse_plan(vlm.parse_json_lenient(raw_plan))
                    if not plan_data["plan"] and not plan_data["todo"]:
                        raise ValueError("planner returned an empty plan")
                    self.on_event({"type": "plan", "model": planner_model,
                                   "plan": plan_data["plan"], "todo": plan_data["todo"],
                                   "done_when": plan_data["done_when"]})
                except Exception as exc:
                    self.on_event({"type": "log", "msg": f"pre-run planner failed; continuing without it: {exc}"})

            # Pinned first so it stays in front of the model for the whole run,
            # with or without a planner.
            pinned_msgs.append({"role": "user", "content": checklist_block(task, plan_data)})

            step_budget = max(1, int(max_steps))
            need_screen = True  # first step always sees the desktop
            n = 0
            while True:
                if _superseded():
                    return  # abandoned run waking up — never touch the desktop again
                if self._stop.is_set():
                    self.on_event({"type": "done", "ok": False, "message": "stopped by user", "steps": n})
                    return
                if n >= step_budget:
                    # Cap hit — pause like ASK so the phone can Continue with more
                    # steps instead of killing a run that was still making progress.
                    if not self._awaiting_more_steps:
                        self._step_grant = 0
                        self._pause.clear()
                        with self._follow_ups_lock:
                            if self._follow_ups:
                                self._pause.set()
                            else:
                                self._awaiting_more_steps = True
                        self.on_event({
                            "type": "max_steps",
                            "steps": step_budget,
                            "message": (
                                f"Used all {step_budget} steps without finishing. "
                                "Continue with another batch?"
                            ),
                        })
                    self._wait_unpaused()
                    self._awaiting_more_steps = False
                    if self._stop.is_set():
                        self.on_event({"type": "done", "ok": False, "message": "stopped by user",
                                       "steps": n})
                        return
                    extra = self._step_grant or max(1, int(max_steps))
                    self._step_grant = 0
                    step_budget = n + extra
                    self.on_event({"type": "log", "msg": (
                        f"continuing — step budget raised to {step_budget}")})
                    need_screen = True
                    continue

                n += 1
                self._wait_unpaused()

                rec = StepRecord(n=n)
                self.on_event({"type": "step_begin", "n": n})

                # 1) Screenshot — only when this step needs eyes on the desktop.
                # Shell / write_file rounds usually do not; the model asks with
                # need_screen:true when it must look again.
                img = None
                W = max(1, int(getattr(monitor, "width", 0) or 0))
                H = max(1, int(getattr(monitor, "height", 0) or 0))
                image_scale = 1.0
                sent_w, sent_h = W, H
                screen = {"moved": True, "same_streak": 0}
                sd = _step_dir(n)
                if need_screen:
                    t0 = time.time()
                    img = capture(monitor)
                    rec.screenshot_ms = int((time.time() - t0) * 1000)
                    if self._stop.is_set():
                        self.on_event({"type": "done", "ok": False, "message": "stopped by user", "steps": n - 1})
                        return
                    W, H = img.size
                    self.on_event({"type": "screenshot", "n": n, "image": img, "size": [W, H]})
                    screen = watch.note_screen(frame_fingerprint(img), acted=bool(rec_actions_last))
                    if sd:
                        try: img.save(os.path.join(sd, "primary.png"))
                        except Exception: pass
                        for mi, (mimg, label) in enumerate(mid_trail):
                            try:
                                mimg.save(os.path.join(sd, f"trail_in_{mi:02d}.png"))
                            except Exception: pass
                        if mid_trail:
                            try:
                                with open(os.path.join(sd, "trail_labels.json"),
                                          "w", encoding="utf-8") as f:
                                    json.dump([lbl for _, lbl in mid_trail], f, indent=2)
                            except Exception: pass
                    data_url, image_scale = vlm.encode_image(img)
                    sent_w = max(1, int(W * image_scale))
                    sent_h = max(1, int(H * image_scale))
                else:
                    if sd and mid_trail:
                        for mi, (mimg, label) in enumerate(mid_trail):
                            try:
                                mimg.save(os.path.join(sd, f"trail_in_{mi:02d}.png"))
                            except Exception: pass

                # 2) Build user message for this step
                user_content: list[dict] = []
                # 2.-1) User follow-ups injected since last step (or as ASK answers).
                pending_followups = self._drain_follow_ups()
                if pending_followups:
                    header = ("ANSWER from user to your previous ASK:"
                              if last_was_ask else
                              "NEW INSTRUCTION from user (treat as updated/added "
                              "context — keep working on the original task unless "
                              "this clearly supersedes it):")
                    parts = [header]
                    for f in pending_followups:
                        if f.get("text"):
                            parts.append(f["text"])
                    blob = "\n".join(parts) if len(parts) > 1 else header + " (image only)"
                    user_content.append(vlm.text_part(blob))
                    for f in pending_followups:
                        for im in (f.get("images") or []):
                            try:
                                im_url, _ = vlm.encode_image(im)
                                user_content.append(vlm.text_part("[user attached image]"))
                                user_content.append(vlm.image_part(im_url, detail="high"))
                            except Exception:
                                pass
                    # Pin it: what the user says mid-run must outlive trimming.
                    pinned_msgs.append({"role": "user", "content": blob})
                    if len(pinned_msgs) > 8:
                        del pinned_msgs[1:len(pinned_msgs) - 7]
                    last_was_ask = False
                # 2.0) First-turn user attachments (images / text files).
                if n == 1 and attachments:
                    user_content.append(vlm.text_part(
                        f"User attached {len(attachments)} file(s) to the task. "
                        "Treat them as reference material:"
                    ))
                    for att in attachments:
                        kind = att.get("kind")
                        name = att.get("name", "?")
                        if kind == "image" and att.get("image") is not None:
                            att_url, _ = vlm.encode_image(att["image"])
                            user_content.append(vlm.text_part(f"[attached image: {name}]"))
                            user_content.append(vlm.image_part(att_url, detail="high"))
                        elif kind == "text" and att.get("text"):
                            txt = att["text"]
                            if len(txt) > 8000:
                                txt = txt[:8000] + "\n…[truncated]"
                            user_content.append(vlm.text_part(
                                f"[attached file: {name}]\n{txt}"))
                    user_content.append(vlm.text_part("--- end attachments ---"))
                # 2a) Process trail from prior step (low detail to limit cost).
                if mid_trail:
                    user_content.append(vlm.text_part(
                        f"Process trail from prior step ({len(mid_trail)} intermediate "
                        "screenshot(s), low-detail, in order). Use these to judge "
                        "whether your last action chain went as planned:"
                    ))
                    for trail_img, label in mid_trail:
                        trail_url, _ = vlm.encode_image(trail_img)
                        user_content.append(vlm.text_part(label))
                        user_content.append(vlm.image_part(trail_url, detail="low"))
                    user_content.append(vlm.text_part("--- end trail ---"))
                # 2b) Current high-detail screenshot — only when this step needs it.
                remaining = step_budget - n
                budget = ("This is the LAST step — finish or report honestly."
                          if remaining == 0 else
                          f"{remaining} step(s) left after this one."
                          + (" Running short: go straight for the goal."
                             if remaining <= 3 else ""))
                movement = ""
                if img is not None and rec_actions_last and not screen["moved"]:
                    movement = ("The screen did NOT change after your last actions — they had no "
                               "effect. Do not repeat them; try a different route.\n")
                if img is not None:
                    user_content.append(vlm.text_part(
                        f"STEP {n}/{step_budget}\nTASK: {task}\n"
                        f"CONTEXT: {_now_block()}\n"
                        f"The screenshot image shown to you is {sent_w}x{sent_h} px "
                        f"(top-left = 0,0). Output coordinates in the SHOWN IMAGE'S "
                        f"{sent_w}x{sent_h} pixel space.\n"
                        + movement
                        + (f"Previous step status: {last_status}\n" if last_status else "")
                        + f"{budget}\n"
                        + "Set need_screen:true in your JSON when the NEXT step must see "
                          "the desktop; false after shell/write_file when text output is enough.\n"
                        + "Now think and reply with JSON."
                    ))
                    user_content.append(vlm.image_part(data_url))
                else:
                    user_content.append(vlm.text_part(
                        f"STEP {n}/{step_budget}\nTASK: {task}\n"
                        f"CONTEXT: {_now_block()}\n"
                        "No new screenshot this step — use shell/file output and history. "
                        "Set need_screen:true when you need to see the desktop again "
                        "(clicks, UI checks, verifying on-screen results).\n"
                        + (f"Previous step status: {last_status}\n" if last_status else "")
                        + f"{budget}\n"
                        + "Now think and reply with JSON."
                    ))
                msgs = list(pinned_msgs) + list(history_msgs) + [{"role": "user", "content": user_content}]
                # Trail is consumed; reset before this step's actions repopulate it.
                mid_trail = []

                # 3) Reason
                # Debug: dump the request before sending (images become file refs).
                if sd:
                    try:
                        safe_msgs = _sanitize_messages_for_disk(msgs, sd)
                        with open(os.path.join(sd, "request.json"), "w",
                                  encoding="utf-8") as f:
                            json.dump({
                                "model": model, "backend": backend,
                                "reasoning_effort": reasoning_effort,
                                "messages": safe_msgs,
                            }, f, indent=2, default=str)
                    except Exception:
                        pass
                t0 = time.time()
                try:
                    raw, step_usage = self._call_model(
                        effective_system, msgs, model=model, backend=backend,
                        reasoning_effort=reasoning_effort,
                    )
                    _add_usage(step_usage)
                    rec.raw = raw
                    if sd:
                        try:
                            with open(os.path.join(sd, "response.txt"), "w",
                                      encoding="utf-8") as f:
                                f.write(raw or "")
                        except Exception: pass
                    if self._stop.is_set():
                        self.on_event({"type": "done", "ok": False, "message": "stopped by user", "steps": n - 1})
                        return
                    parsed = vlm.parse_json_lenient(raw)
                except Exception as e:
                    rec.error = f"VLM/JSON error: {e}"
                    rec.think_ms = int((time.time() - t0) * 1000)
                    self.on_event({"type": "step_end", "n": n, "record": _rec_dict(rec)})
                    # tell model to retry
                    history_msgs.append({"role": "assistant", "content": rec.raw or ""})
                    history_msgs.append({"role": "user", "content":
                        f"Your last reply could not be parsed: {e}. Reply with ONE valid JSON object only."})
                    rec_actions_last = 0
                    last_status = "unparseable reply"
                    continue
                rec.think_ms = int((time.time() - t0) * 1000)

                rec.thought = str(parsed.get("thought", ""))[:4000]
                rec.message = str(parsed.get("message", ""))[:500]
                rec.say     = str(parsed.get("say", "")).strip()[:300]
                rec.status  = str(parsed.get("status", "continue")).lower()
                rec.actions = _scale_model_actions(
                    parsed.get("actions", []) or [], image_scale if img is not None else 1.0, W, H
                )
                # Decide whether the NEXT step gets a screenshot. Prefer the
                # model's need_screen flag; otherwise skip after text-only
                # rounds (shell / write_file) and capture after UI actions.
                explicit_screen = parsed.get("need_screen", parsed.get("screenshot"))
                action_types = {
                    str((action or {}).get("type") or "").lower()
                    for action in (rec.actions or [])
                    if isinstance(action, dict)
                }
                if explicit_screen is True or str(explicit_screen).strip().lower() in {"1", "true", "yes"}:
                    need_screen = True
                elif explicit_screen is False or str(explicit_screen).strip().lower() in {"0", "false", "no"}:
                    need_screen = False
                elif action_types & UI_ACTION_TYPES:
                    need_screen = True
                elif action_types and action_types <= TEXT_ACTION_TYPES:
                    need_screen = False
                else:
                    need_screen = img is not None

                if self._stop.is_set():
                    self.on_event({"type": "done", "ok": False, "message": "stopped by user", "steps": n - 1})
                    return

                self.on_event({"type": "thought", "n": n, "thought": rec.thought,
                               "message": rec.message, "say": rec.say,
                               "status": rec.status, "actions": rec.actions,
                               "elapsed_ms": rec.think_ms, "raw": rec.raw})

                # 4) Terminal statuses
                if rec.status == "fail":
                    self.on_event({"type": "step_end", "n": n, "record": _rec_dict(rec)})
                    self.on_event({"type": "done", "ok": False, "message": rec.message, "steps": n})
                    return
                if rec.status == "done":
                    # Claiming done is not being done. Check the screen against
                    # what the user actually asked for before ending the run.
                    self.on_event({"type": "step_end", "n": n, "record": _rec_dict(rec)})
                    check = self._verify_completion(
                        task=task, plan=plan_data, closing=rec.message or rec.thought,
                        monitor=monitor, model=(planner_model or model), backend=backend,
                        add_usage=_add_usage, step_dir=sd,
                    )
                    if check:
                        self.on_event({"type": "verified", "n": n, **check})
                    if check and check.get("verdict") == "fail" and verify_failures < 2:
                        verify_failures += 1
                        missing = check.get("missing") or []
                        pinned_msgs.append({"role": "user", "content": (
                            "COMPLETION CHECK FAILED — the run is NOT finished.\n"
                            f"Reason: {check.get('reason') or 'the screen does not show the requested result'}\n"
                            + ("Still missing:\n" + "\n".join(f"  - {item}" for item in missing)
                               if missing else "")
                            + "\nKeep working: fix what is missing, then report done again. "
                              "If it genuinely cannot be done, use status \"fail\" or \"ask\"."
                        )})
                        history_msgs.append({"role": "assistant", "content": rec.raw})
                        last_status = "done (rejected by completion check)"
                        watch.cleared()
                        rec_actions_last = 0
                        continue
                    message = rec.message
                    if check and check.get("verdict") == "fail":
                        message = (message + " — completion check still disagrees: "
                                   + str(check.get("reason") or "")).strip()
                    self.on_event({"type": "done", "ok": True, "message": message, "steps": n,
                                   "verified": bool(check and check.get("verdict") == "pass")})
                    return
                if rec.status == "ask":
                    history_msgs.append({"role": "assistant", "content": rec.raw or ""})
                    self.on_event({"type": "step_end", "n": n, "record": _rec_dict(rec)})
                    # Arm the wait BEFORE announcing the ask. The phone can
                    # answer in milliseconds, and an answer that landed while we
                    # were still emitting used to be queued without waking us.
                    self._pause.clear()
                    with self._follow_ups_lock:
                        if self._follow_ups:
                            # Already answered — don't wait at all.
                            self._pause.set()
                        else:
                            self._awaiting_answer = True
                    self.on_event({"type": "ask", "message": rec.message})
                    last_was_ask = True
                    self._wait_unpaused()
                    self._awaiting_answer = False
                    if self._stop.is_set():
                        self.on_event({"type": "done", "ok": False, "message": "stopped", "steps": n})
                        return
                    rec_actions_last = 0
                    last_status = "asked the user"
                    watch.cleared()
                    continue

                # 4b) Going in circles? Say so before spending another step on it.
                watch.note_actions(action_signature(rec.actions))
                stuck = watch.verdict(n)
                if stuck["level"] == "abort":
                    self.on_event({"type": "log", "msg": f"stuck: {stuck['reason']}"})
                    self.on_event({"type": "step_end", "n": n, "record": _rec_dict(rec)})
                    self.on_event({"type": "done", "ok": False, "steps": n, "message": (
                        f"Stopped: no progress — {stuck['reason']}. "
                        "The screen stopped responding to these actions, so the run was "
                        "ended instead of repeating them. Tell me what to try instead.")})
                    return
                if stuck["level"] == "nudge":
                    history_msgs.append({"role": "user", "content": (
                        f"PROGRESS WARNING: {stuck['reason']}. What you are doing is not "
                        "working. Do NOT repeat it. Look at the screenshot again and pick a "
                        "different route: a different element, keyboard instead of mouse, "
                        "scroll to reveal what you need, or the shell if it is enabled. "
                        "If nothing can work here, say so with status \"ask\" or \"fail\" "
                        "instead of trying again.")})
                    self.on_event({"type": "log", "msg": f"nudged: {stuck['reason']}"})

                # 5) Execute actions
                rec_actions_last = len(rec.actions)
                t0 = time.time()
                for i, action in enumerate(rec.actions):
                    if _superseded():
                        return
                    if self._stop.is_set():
                        self.on_event({"type": "done", "ok": False, "message": "stopped", "steps": n})
                        return
                    self._wait_unpaused()
                    if self._stop.is_set():
                        self.on_event({"type": "done", "ok": False, "message": "stopped by user", "steps": n})
                        return
                    res = execute(action, monitor, on_click=self._fire_click_event,
                                  shell_enabled=shell_enabled, shell_cwd=shell_cwd,
                                  cancel_event=self._stop)
                    rec.results.append(_exec_dict(res))
                    self.on_event({"type": "action_done", "n": n, "i": i,
                                   "result": _exec_dict(res)})
                    if self._stop.is_set():
                        release_all()
                        self.on_event({"type": "done", "ok": False, "message": "stopped by user", "steps": n})
                        return
                    if not res.ok:
                        # don't leave the mouse hung mid-stroke on a failed action
                        if any_button_held():
                            release_all()
                        break
                    # don't freeze mid-stroke: shorten delay while a button is held
                    if action_delay > 0:
                        self._sleep_cancelable(0.02 if any_button_held() else action_delay)
                        if self._stop.is_set():
                            release_all()
                            self.on_event({"type": "done", "ok": False, "message": "stopped by user", "steps": n})
                            return
                    # Mid-step screenshot after state-changing actions (skip the
                    # last one — the next step's primary screenshot covers it,
                    # and skip while a mouse button is still held mid-stroke).
                    atype = (action.get("type") or "").lower()
                    is_last = (i == len(rec.actions) - 1)
                    if (mid_screenshots == "key" and not is_last
                            and atype in KEY_ACTION_TYPES
                            and not any_button_held()
                            and not any_key_held()):
                        try:
                            mid_img = capture(monitor)
                            label = f"step {n} after action {i} ({atype})"
                            mid_trail.append((mid_img, label))
                            if len(mid_trail) > MID_TRAIL_MAX:
                                mid_trail = mid_trail[-MID_TRAIL_MAX:]
                            # Debug: persist every mid-step shot to disk (not
                            # just the last 3 — full record for forensics).
                            if sd:
                                try:
                                    mid_img.save(os.path.join(
                                        sd, f"mid_after_{i:02d}_{atype}.png"))
                                except Exception: pass
                        except Exception as e:
                            self.on_event({"type": "log",
                                "msg": f"mid-step capture failed: {e}"})
                rec.act_ms = int((time.time() - t0) * 1000)

                # 6) Settle, then summarize this step for history
                self._sleep_cancelable(settle_after_step)
                if self._stop.is_set():
                    release_all()
                    self.on_event({"type": "done", "ok": False, "message": "stopped by user", "steps": n})
                    return
                history_msgs.append({"role": "assistant", "content": rec.raw})
                ok_count = sum(1 for r in rec.results if r["ok"])
                lines = [f"Step {n} executed {len(rec.results)} actions, "
                         f"{ok_count} ok. Status was '{rec.status}'."]
                # surface any rich output (shell stdout/stderr etc.) verbatim
                for i, r in enumerate(rec.results):
                    if r.get("output"):
                        lines.append(f"\n[action {i} output]\n{r['output']}")
                    elif not r["ok"]:
                        lines.append(f"[action {i} FAILED] {r['detail']}")
                history_msgs.append({"role": "user", "content": "\n".join(lines)})
                last_status = rec.status

                # Keep history bounded — in whole assistant/user pairs, so the
                # trimmed conversation never starts on a dangling reply.
                if len(history_msgs) > 16:
                    history_msgs = history_msgs[-16:]
                    if history_msgs and history_msgs[0].get("role") == "user":
                        history_msgs = history_msgs[1:]

                # Debug: dump per-step actions + results + parsed thought.
                if sd:
                    try:
                        with open(os.path.join(sd, "actions.json"), "w",
                                  encoding="utf-8") as f:
                            json.dump({
                                "thought": rec.thought, "message": rec.message,
                                "say": rec.say, "status": rec.status,
                                "actions": rec.actions, "results": rec.results,
                                "think_ms": rec.think_ms, "act_ms": rec.act_ms,
                                "screenshot_ms": rec.screenshot_ms,
                            }, f, indent=2, default=str)
                    except Exception: pass

                self.on_event({"type": "step_end", "n": n, "record": _rec_dict(rec)})

        except Exception as e:
            self.on_event({"type": "log", "msg": f"FATAL: {e}\n{traceback.format_exc()}"})
            self.on_event({"type": "done", "ok": False, "message": f"fatal: {e}", "steps": 0})
        finally:
            if self.on_event is _emit:
                self.on_event = self._event_sink
                self._active_event_wrapper = None
            # Finalize debug run: update meta.json with end status, close transcript.
            try:
                if run_dir:
                    meta_path = os.path.join(run_dir, "meta.json")
                    meta = {}
                    if os.path.isfile(meta_path):
                        try:
                            with open(meta_path, "r", encoding="utf-8") as f:
                                meta = json.load(f)
                        except Exception: pass
                    meta["end"] = datetime.now().isoformat(timespec="seconds")
                    meta["status"] = "finished"
                    with open(meta_path, "w", encoding="utf-8") as f:
                        json.dump(meta, f, indent=2)
                if transcript is not None:
                    transcript.close()
            except Exception:
                pass

    def _fire_click_event(self, x: int, y: int, button: str):
        self.on_event({"type": "click_fx", "x": x, "y": y, "button": button})

    def _verify_completion(self, *, task, plan, closing, monitor, model, backend,
                           add_usage, step_dir=None) -> dict:
        """Ask a second opinion whether the task is really finished.

        Returns {} when the check itself could not run — a broken checker must
        never hold a finished run hostage.
        """
        self.on_event({"type": "verify_begin", "model": model})
        try:
            image = capture(monitor)
            url, _ = vlm.encode_image(image)
            wanted = ""
            if plan.get("done_when"):
                wanted = "\nThe plan said this is done when:\n" + "\n".join(
                    f"  - {item}" for item in plan["done_when"])
            todo = ""
            if plan.get("todo"):
                todo = "\nThe todo list was:\n" + "\n".join(
                    f"  {index}. {item}" for index, item in enumerate(plan["todo"], 1))
            content = [
                vlm.text_part(
                    f"TASK the user asked for: {task}\n"
                    f"{_now_block()}\n"
                    f"The agent says it is finished. Its closing message: {closing or '(none)'}"
                    f"{todo}{wanted}\n\n"
                    "Here is the desktop right now. Did it actually do what the user asked?"
                ),
                vlm.image_part(url, detail="high"),
            ]
            raw, verify_usage = self._call_model(
                VERIFIER_SYSTEM_PROMPT, [{"role": "user", "content": content}],
                model=model, backend=backend, reasoning_effort="low")
            add_usage(verify_usage)
            parsed = vlm.parse_json_lenient(raw)
            verdict = str(parsed.get("verdict") or "").strip().lower()
            if verdict not in {"pass", "fail"}:
                verdict = "pass"
            result = {
                "verdict": verdict,
                "reason": str(parsed.get("reason") or "").strip()[:400],
                "missing": [str(item).strip()[:200]
                            for item in (parsed.get("missing") or []) if str(item).strip()][:6],
            }
            if step_dir:
                try:
                    image.save(os.path.join(step_dir, "verify.png"))
                    with open(os.path.join(step_dir, "verify.json"), "w", encoding="utf-8") as fh:
                        json.dump({"raw": raw, **result}, fh, indent=2)
                except Exception:
                    pass
            return result
        except Exception as exc:
            self.on_event({"type": "log", "msg": f"completion check skipped: {exc}"})
            return {}


def _exec_dict(r: ExecResult) -> dict:
    return {"action": r.action, "ok": r.ok, "detail": r.detail,
            "elapsed_ms": r.elapsed_ms, "output": r.output}


def _rec_dict(r: StepRecord) -> dict:
    return {
        "n": r.n, "thought": r.thought, "message": r.message, "say": r.say,
        "status": r.status, "actions": r.actions, "results": r.results,
        "screenshot_ms": r.screenshot_ms, "think_ms": r.think_ms, "act_ms": r.act_ms,
        "error": r.error,
    }
