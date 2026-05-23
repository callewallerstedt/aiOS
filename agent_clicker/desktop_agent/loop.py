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

from PIL import Image

from agent import vlm
from agent.config import MODEL as DEFAULT_MODEL
from .prompts import SYSTEM_PROMPT
from .screen import Monitor, capture
from .actions import execute, ExecResult, any_button_held, any_key_held, release_all


# Where per-run debug folders go (sibling of this package).
DEBUG_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "debug_runs")


def _slug(s: str, maxlen: int = 40) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")
    return (s[:maxlen] or "task").lower()


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
    "type", "hotkey", "key_combo", "key", "shell",
    "key_hold", "tap_hold",  # press+release pairs are worth a screenshot
    # Note: key_down / key_up / mouse_rel are intentionally omitted — they're
    # typically part of a held-input chain where snapshotting between them
    # would freeze the action and the screen barely changes per call.
}
MID_TRAIL_MAX = 3  # how many mid-step screenshots to carry into the next round


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
        self.on_event = on_event
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._pause.set()  # set = not paused

    def is_running(self) -> bool:
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

    def start(self, task: str, monitor: Monitor, model: str = DEFAULT_MODEL,
              max_steps: int = 25, action_delay: float = 0.20,
              settle_after_step: float = 0.6,
              shell_enabled: bool = False, shell_cwd: str | None = None,
              backend: str = "api", reasoning_effort: str | None = None,
              mid_screenshots: str = "key",
              attachments: list[dict] | None = None):
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
        self._stop.clear()
        self._pause.set()
        self._thread = threading.Thread(
            target=self._run,
            args=(task, monitor, model, max_steps, action_delay, settle_after_step,
                  shell_enabled, shell_cwd, backend, reasoning_effort, mid_screenshots,
                  attachments or []),
            daemon=True,
        )
        self._thread.start()

    # -----------------------------------------------------------

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
             reasoning_effort=None, mid_screenshots="key", attachments=None):
        attachments = attachments or []
        history_msgs: list[dict] = []
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
                f.write(SYSTEM_PROMPT)
            with open(os.path.join(run_dir, "meta.json"), "w", encoding="utf-8") as f:
                json.dump({
                    "task": task,
                    "model": model, "backend": backend,
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
        orig_on_event = self.on_event
        def _emit(ev: dict):
            _write_event(ev)
            orig_on_event(ev)
        self.on_event = _emit  # type: ignore

        try:
            for n in range(1, max_steps + 1):
                if self._stop.is_set():
                    self.on_event({"type": "done", "ok": False, "message": "stopped by user", "steps": n - 1})
                    return
                self._wait_unpaused()

                rec = StepRecord(n=n)
                self.on_event({"type": "step_begin", "n": n})

                # 1) Screenshot
                t0 = time.time()
                img = capture(monitor)
                rec.screenshot_ms = int((time.time() - t0) * 1000)
                if self._stop.is_set():
                    self.on_event({"type": "done", "ok": False, "message": "stopped by user", "steps": n - 1})
                    return
                W, H = img.size
                self.on_event({"type": "screenshot", "n": n, "image": img, "size": [W, H]})
                # Debug: save primary screenshot for this step.
                sd = _step_dir(n)
                if sd:
                    try: img.save(os.path.join(sd, "primary.png"))
                    except Exception: pass
                    # And persist whatever's in the mid-trail before it gets consumed.
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

                # 2) Build user message for this step
                data_url, _ = vlm.encode_image(img)
                user_content: list[dict] = []
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
                # 2b) Current high-detail screenshot.
                user_content.append(vlm.text_part(
                    f"STEP {n}/{max_steps}\nTASK: {task}\n"
                    f"Monitor screenshot is {W}x{H} px (top-left = 0,0). "
                    f"Output coordinates in this space.\n"
                    + (f"Previous step status: {history_msgs[-1].get('status','?')}\n"
                       if history_msgs else "")
                    + "Now think and reply with JSON."
                ))
                user_content.append(vlm.image_part(data_url))
                msgs = list(history_msgs) + [{"role": "user", "content": user_content}]
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
                    raw = vlm.chat_raw(SYSTEM_PROMPT, msgs, model=model, backend=backend,
                                       reasoning_effort=reasoning_effort)
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
                    continue
                rec.think_ms = int((time.time() - t0) * 1000)

                rec.thought = str(parsed.get("thought", ""))[:4000]
                rec.message = str(parsed.get("message", ""))[:500]
                rec.say     = str(parsed.get("say", "")).strip()[:300]
                rec.status  = str(parsed.get("status", "continue")).lower()
                rec.actions = parsed.get("actions", []) or []

                if self._stop.is_set():
                    self.on_event({"type": "done", "ok": False, "message": "stopped by user", "steps": n - 1})
                    return

                self.on_event({"type": "thought", "n": n, "thought": rec.thought,
                               "message": rec.message, "say": rec.say,
                               "status": rec.status, "actions": rec.actions,
                               "elapsed_ms": rec.think_ms, "raw": rec.raw})

                # 4) Terminal statuses
                if rec.status in ("done", "fail"):
                    self.on_event({"type": "step_end", "n": n, "record": _rec_dict(rec)})
                    self.on_event({"type": "done", "ok": rec.status == "done",
                                   "message": rec.message, "steps": n})
                    return
                if rec.status == "ask":
                    self.on_event({"type": "step_end", "n": n, "record": _rec_dict(rec)})
                    self.on_event({"type": "ask", "message": rec.message})
                    # Loop pauses awaiting external resume w/ injected answer.
                    # For now we just wait for resume; user can provide answer
                    # by stopping and re-running with a new task.
                    self._pause.clear()
                    self._wait_unpaused()
                    if self._stop.is_set():
                        self.on_event({"type": "done", "ok": False, "message": "stopped", "steps": n})
                        return
                    continue

                # 5) Execute actions
                t0 = time.time()
                for i, action in enumerate(rec.actions):
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
                lines = [f"Step {n} executed {len(rec.results)} actions, "
                         f"{sum(1 for r in rec.results if r['ok'])} ok. "
                         f"Status was '{rec.status}'."]
                # surface any rich output (shell stdout/stderr etc.) verbatim
                for i, r in enumerate(rec.results):
                    if r.get("output"):
                        lines.append(f"\n[action {i} output]\n{r['output']}")
                    elif not r["ok"]:
                        lines.append(f"[action {i} FAILED] {r['detail']}")
                history_msgs.append({"role": "user", "content": "\n".join(lines)})

                # Keep history bounded (avoid runaway context).
                if len(history_msgs) > 16:
                    history_msgs = history_msgs[-16:]

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

            self.on_event({"type": "done", "ok": False, "message": "max_steps reached",
                           "steps": max_steps})
        except Exception as e:
            self.on_event({"type": "log", "msg": f"FATAL: {e}\n{traceback.format_exc()}"})
            self.on_event({"type": "done", "ok": False, "message": f"fatal: {e}", "steps": 0})
        finally:
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
