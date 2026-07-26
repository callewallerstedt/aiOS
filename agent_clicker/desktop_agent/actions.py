"""Action executor: turns the model's action JSON into real OS input."""
from __future__ import annotations
import time
from dataclasses import dataclass
from typing import Any, Callable

import pyautogui
import pyperclip

from . import winput
from . import shell as ps

# Safety: moving mouse to a corner aborts (keyboard still uses pyautogui).
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.02

# Every cursor positioning slides over this many seconds by default,
# so you can see where it's going. Chained drag moves use SMOOTH_HELD.
SMOOTH = 0.22       # standalone move / pre-click slide
SMOOTH_HELD = 0.10  # move while a button is held (a drag segment)
SMOOTH_PATH_STEP = 0.035  # per-segment time inside a path stroke

# Track whether a mouse button is currently held by us, so chains like
# mouse_down -> move -> move -> mouse_up don't accidentally release.
_HELD: dict[str, bool] = {"left": False, "right": False, "middle": False}

# Track held keyboard keys (lowercased names). Game inputs can leave a key
# stuck down across many actions ("hold W to walk forward while looking").
# Auto-released on stop/pause so we never leave the user's keyboard borked.
_KEYS_HELD: set[str] = set()


def any_button_held() -> bool:
    return any(_HELD.values())


def any_key_held() -> bool:
    return bool(_KEYS_HELD)


def release_all():
    """Emergency: release any held mouse buttons AND held keys. stop/pause."""
    # Keys first — releasing buttons can take a frame.
    for k in list(_KEYS_HELD):
        try: winput.key_up(k)
        except Exception: pass
        _KEYS_HELD.discard(k)
    for b, held in list(_HELD.items()):
        if held:
            try: winput.mouse_up(b)
            except Exception: pass
            _HELD[b] = False


class ActionStopped(Exception):
    pass


def _check_cancel(cancel_event=None):
    if cancel_event is not None and cancel_event.is_set():
        release_all()
        raise ActionStopped("STOP requested; action cancelled before more input.")


def _sleep_cancelable(seconds: float, cancel_event=None) -> None:
    if cancel_event is None:
        time.sleep(seconds)
        return
    end = time.time() + max(0.0, float(seconds))
    while not cancel_event.is_set():
        remaining = end - time.time()
        if remaining <= 0:
            return
        time.sleep(min(0.02, remaining))
    _check_cancel(cancel_event)


def _f(v, default=0.0) -> float:
    try: return float(v)
    except (TypeError, ValueError): return float(default)


def _i(v, default=0) -> int:
    try: return int(round(float(v)))
    except (TypeError, ValueError): return int(default)


def _xy(a, mon) -> tuple[int, int]:
    return _to_screen(_i(a.get("x")), _i(a.get("y")), mon)


def _slide_to(x: int, y: int, duration: float, cancel_event=None):
    """Smoothly slide cursor to (x,y) over `duration` seconds via winput
    (works correctly on monitors with negative virtual coords)."""
    _check_cancel(cancel_event)
    winput.slide_to(x, y, duration, cancel_event=cancel_event)
    _check_cancel(cancel_event)


@dataclass
class ExecResult:
    action: dict          # the action that was run
    ok: bool
    detail: str = ""
    elapsed_ms: int = 0
    # Optional rich payload (shell output, captured text, etc.) — included
    # verbatim in the next step's user message so the model can read it.
    output: str = ""


KEY_MAP = {
    # normalize a few aliases to pyautogui names
    "win": "winleft", "windows": "winleft", "meta": "winleft", "cmd": "winleft",
    "control": "ctrl", "option": "alt",
    "return": "enter", "esc": "escape", "del": "delete",
    "pageup": "pageup", "pagedown": "pagedown",
    " ": "space",
}


def _norm_key(k: str) -> str:
    if not isinstance(k, str):
        raise ValueError(f"key must be str, got {type(k).__name__}")
    k = k.strip().lower()
    return KEY_MAP.get(k, k)


def _to_screen(local_x: float, local_y: float, mon) -> tuple[int, int]:
    """Convert monitor-local pixel to virtual-screen (pyautogui) pixel."""
    return int(round(mon.left + local_x)), int(round(mon.top + local_y))


def execute(
    action: dict,
    monitor,                      # desktop_agent.screen.Monitor
    on_click: Callable[[int, int, str], None] | None = None,
    shell_enabled: bool = False,
    shell_cwd: str | None = None,
    cancel_event=None,
) -> ExecResult:
    """Run one action. Coordinates in `action` are MONITOR-LOCAL pixels
    (0..mon.width, 0..mon.height). They are translated to virtual-screen here.
    """
    t0 = time.time()
    a = dict(action)  # copy
    t = (a.get("type") or "").lower()

    try:
        _check_cancel(cancel_event)
        if t == "move":
            x, y = _xy(a, monitor)
            held = next((b for b, h in _HELD.items() if h), None)
            dur = _f(a.get("duration"), SMOOTH_HELD if held else SMOOTH)
            _slide_to(x, y, dur, cancel_event)
            d = f"move -> ({x},{y})" + (f" [dragging {held}]" if held else "")

        elif t == "click":
            x, y = _xy(a, monitor)
            button = a.get("button", "left")
            clicks = 2 if a.get("double") else _i(a.get("clicks"), 1)
            interval = _f(a.get("interval"), 0.06)
            _slide_to(x, y, _f(a.get("duration"), SMOOTH), cancel_event)
            _check_cancel(cancel_event)
            winput.click(button=button, clicks=clicks, interval=interval, cancel_event=cancel_event)
            _check_cancel(cancel_event)
            if on_click:
                on_click(x, y, button)
            d = f"{button}_click x{clicks} @ ({x},{y})"

        elif t == "right_click":
            x, y = _xy(a, monitor)
            _slide_to(x, y, _f(a.get("duration"), SMOOTH), cancel_event)
            _check_cancel(cancel_event)
            winput.click(button="right", cancel_event=cancel_event)
            _check_cancel(cancel_event)
            if on_click: on_click(x, y, "right")
            d = f"right_click @ ({x},{y})"

        elif t == "double_click":
            x, y = _xy(a, monitor)
            _slide_to(x, y, _f(a.get("duration"), SMOOTH), cancel_event)
            _check_cancel(cancel_event)
            winput.click(button="left", clicks=2, cancel_event=cancel_event)
            _check_cancel(cancel_event)
            if on_click: on_click(x, y, "left")
            d = f"double_click @ ({x},{y})"

        elif t == "mouse_down":
            x, y = _xy(a, monitor)
            button = a.get("button", "left")
            _slide_to(x, y, _f(a.get("duration"), SMOOTH), cancel_event)
            _check_cancel(cancel_event)
            winput.mouse_down(button)
            _HELD[button] = True
            d = f"mouse_down {button} @ ({x},{y})"

        elif t == "mouse_up":
            x = a.get("x"); y = a.get("y")
            button = a.get("button", "left")
            if x is not None and y is not None:
                sx, sy = _to_screen(_i(x), _i(y), monitor)
                _slide_to(sx, sy, _f(a.get("duration"), SMOOTH_HELD), cancel_event)
                _check_cancel(cancel_event)
                winput.mouse_up(button)
                d = f"mouse_up {button} @ ({sx},{sy})"
            else:
                winput.mouse_up(button)
                d = f"mouse_up {button} (at current pos)"
            _HELD[button] = False

        elif t == "drag":
            fr = a.get("from") or [a.get("x1"), a.get("y1")]
            to = a.get("to") or [a.get("x2"), a.get("y2")]
            fx, fy = _to_screen(_i(fr[0]), _i(fr[1]), monitor)
            tx, ty = _to_screen(_i(to[0]), _i(to[1]), monitor)
            button = a.get("button", "left")
            dur = _f(a.get("duration"), 0.45)
            _slide_to(fx, fy, SMOOTH, cancel_event)
            _check_cancel(cancel_event)
            winput.mouse_down(button); _HELD[button] = True
            _slide_to(tx, ty, dur, cancel_event)
            _check_cancel(cancel_event)
            winput.mouse_up(button); _HELD[button] = False
            if on_click:
                on_click(fx, fy, button); on_click(tx, ty, button)
            d = f"drag {button} ({fx},{fy}) -> ({tx},{ty})"

        elif t == "path":
            # Single continuous stroke: press, glide through every point, release.
            # Use this for drawing shapes / signatures / multi-segment drags.
            raw_points = a.get("points") or []
            if len(raw_points) < 2:
                return ExecResult(action=a, ok=False,
                                  detail="path needs >= 2 points",
                                  elapsed_ms=int((time.time() - t0) * 1000))
            points = [_to_screen(_i(p[0]), _i(p[1]), monitor) for p in raw_points]
            button = a.get("button", "left")
            step = _f(a.get("step_duration"), SMOOTH_PATH_STEP)
            _slide_to(points[0][0], points[0][1], SMOOTH, cancel_event)
            _check_cancel(cancel_event)
            winput.mouse_down(button); _HELD[button] = True
            try:
                for (px, py) in points[1:]:
                    _slide_to(px, py, step, cancel_event)
            finally:
                winput.mouse_up(button); _HELD[button] = False
            if on_click:
                on_click(*points[0], button)
                on_click(*points[-1], button)
            d = f"path {button} {len(points)} pts step={step:.3f}s"

        elif t == "type":
            text = str(a.get("text", ""))
            interval = float(a.get("interval", 0.012))
            if all(ord(c) < 128 for c in text):
                for ch in text:
                    _check_cancel(cancel_event)
                    pyautogui.write(ch, interval=0)
                    if interval > 0:
                        _sleep_cancelable(interval, cancel_event)
                d = f"type ascii len={len(text)}: {text[:60]!r}"
            else:
                # Unicode (e.g. åäö) — paste via clipboard
                _check_cancel(cancel_event)
                pyperclip.copy(text)
                _sleep_cancelable(0.05, cancel_event)
                _check_cancel(cancel_event)
                pyautogui.hotkey("ctrl", "v")
                d = f"type unicode (paste) len={len(text)}: {text[:60]!r}"

        elif t in ("hotkey", "key_combo"):
            keys = a.get("keys") or []
            if isinstance(keys, str):
                keys = [k.strip() for k in keys.split("+")]
            keys = [_norm_key(k) for k in keys]
            _check_cancel(cancel_event)
            pyautogui.hotkey(*keys)
            d = f"hotkey {'+'.join(keys)}"

        elif t == "key":
            k = _norm_key(a.get("key", ""))
            presses = int(a.get("presses", 1))
            interval = float(a.get("interval", 0.04))
            for i in range(presses):
                _check_cancel(cancel_event)
                pyautogui.press(k)
                if i < presses - 1 and interval > 0:
                    _sleep_cancelable(interval, cancel_event)
            d = f"key {k} x{presses}"

        elif t == "scroll":
            x = a.get("x"); y = a.get("y")
            if x is not None and y is not None:
                sx, sy = _to_screen(_i(x), _i(y), monitor)
                _slide_to(sx, sy, _f(a.get("duration"), SMOOTH), cancel_event)
            _check_cancel(cancel_event)
            clicks = _i(a.get("dy", a.get("clicks", -3)))
            winput.scroll(clicks)
            d = f"scroll dy={clicks}" + (f" @ ({x},{y})" if x is not None else "")

        elif t == "key_down":
            k = str(a.get("key", "")).strip().lower()
            if not k:
                return ExecResult(action=a, ok=False, detail="key_down: missing key",
                                  elapsed_ms=int((time.time() - t0) * 1000))
            winput.key_down(k)
            _KEYS_HELD.add(k)
            d = f"key_down {k} (held: {sorted(_KEYS_HELD)})"

        elif t == "key_up":
            k = str(a.get("key", "")).strip().lower()
            if not k:
                # No key given -> release all held keys (panic release).
                released = sorted(_KEYS_HELD)
                for kk in released:
                    try: winput.key_up(kk)
                    except Exception: pass
                _KEYS_HELD.clear()
                d = f"key_up ALL ({released})"
            else:
                winput.key_up(k)
                _KEYS_HELD.discard(k)
                d = f"key_up {k}"

        elif t in ("key_hold", "tap_hold"):
            # Convenience: press, sleep, release in one action.
            k = str(a.get("key", "")).strip().lower()
            if not k:
                return ExecResult(action=a, ok=False, detail="key_hold: missing key",
                                  elapsed_ms=int((time.time() - t0) * 1000))
            hold = _f(a.get("seconds"), 0.1)
            winput.key_down(k)
            _KEYS_HELD.add(k)
            try:
                _sleep_cancelable(hold, cancel_event)
            finally:
                try: winput.key_up(k)
                except Exception: pass
                _KEYS_HELD.discard(k)
            d = f"key_hold {k} {hold:.2f}s"

        elif t in ("mouse_rel", "mouse_move_rel", "look"):
            # Relative mouse delta for FPS / mouse-look games (raw input).
            dx = _i(a.get("dx", 0))
            dy = _i(a.get("dy", 0))
            steps = max(1, _i(a.get("steps"), 1))
            step_dt = _f(a.get("step_seconds"), 0.0)
            for s in range(steps):
                _check_cancel(cancel_event)
                winput.move_rel(dx, dy)
                if step_dt > 0 and s < steps - 1:
                    _sleep_cancelable(step_dt, cancel_event)
            d = (f"mouse_rel dx={dx} dy={dy}"
                 + (f" x{steps}" if steps > 1 else ""))

        elif t == "wait":
            sec = float(a.get("seconds", a.get("ms", 100) / 1000.0))
            _sleep_cancelable(sec, cancel_event)
            d = f"wait {sec:.2f}s"

        elif t in ("shell", "powershell", "ps"):
            if not shell_enabled:
                return ExecResult(action=a, ok=False,
                                  detail="shell disabled — toggle '🖥 Shell' on in the UI",
                                  elapsed_ms=int((time.time() - t0) * 1000))
            cwd = a.get("cwd") or shell_cwd
            timeout = ps.clamp_timeout(_f(a.get("timeout"), ps.DEFAULT_TIMEOUT))
            interpreter = str(a.get("interpreter") or a.get("shell") or "powershell")
            script = a.get("script")
            cmd = str(a.get("command", "")).strip()
            _check_cancel(cancel_event)
            if script:
                # Multi-line: temp .ps1 + -File. Here-strings work correctly here.
                script = str(script)
                sr = ps.run_script(script, cwd=cwd, timeout=timeout,
                                   cancel_event=cancel_event)
                preview = (script.splitlines() or [""])[0][:80]
                d = (f"ps-script exit={sr.exit_code}"
                     + (" TIMEOUT" if sr.timed_out else "")
                     + f" ({sr.elapsed_ms}ms): {preview}")
            else:
                if not cmd:
                    return ExecResult(action=a, ok=False,
                                      detail="shell: empty command (provide 'command' or 'script')",
                                      elapsed_ms=int((time.time() - t0) * 1000))
                sr = ps.run(cmd, cwd=cwd, timeout=timeout, cancel_event=cancel_event,
                            interpreter=interpreter)
                d = (f"shell exit={sr.exit_code}"
                     + (" TIMEOUT" if sr.timed_out else "")
                     + f" ({sr.elapsed_ms}ms): {cmd[:80]}")
            ok = (sr.exit_code == 0 and not sr.timed_out)
            return ExecResult(action=a, ok=ok, detail=d, output=sr.to_text(),
                              elapsed_ms=int((time.time() - t0) * 1000))

        elif t in ("write_file", "writefile"):
            # Atomic-ish text file write. No shell, no here-string mangling.
            # {"type":"write_file","path":"C:/...","text":"...","encoding":"utf8",
            #  "append":false, "newline":"\n"}
            path = str(a.get("path", "")).strip()
            if not path:
                return ExecResult(action=a, ok=False, detail="write_file: missing path",
                                  elapsed_ms=int((time.time() - t0) * 1000))
            text = a.get("text", "")
            if not isinstance(text, str):
                try: text = str(text)
                except Exception: text = ""
            encoding = a.get("encoding", "utf-8") or "utf-8"
            append = bool(a.get("append", False))
            newline = a.get("newline", None)  # None = leave \n in place
            try:
                import os as _os
                _check_cancel(cancel_event)
                parent = _os.path.dirname(path)
                if parent:
                    _os.makedirs(parent, exist_ok=True)
                # Optional newline normalization
                if newline in ("\r\n", "\n"):
                    text = text.replace("\r\n", "\n")
                    if newline == "\r\n":
                        text = text.replace("\n", "\r\n")
                if append:
                    with open(path, "ab") as f:
                        f.write(text.encode(encoding, errors="replace"))
                else:
                    tmp = path + ".tmp"
                    with open(tmp, "wb") as f:
                        if encoding.lower().replace("-", "") in ("utf8sig", "utf8bom"):
                            f.write(b"\xef\xbb\xbf")
                            encoding = "utf-8"
                        f.write(text.encode(encoding, errors="replace"))
                    _os.replace(tmp, path)
                size = _os.path.getsize(path)
                n_lines = text.count("\n") + (0 if text.endswith("\n") else 1) if text else 0
                d = (f"write_file {'+= ' if append else '-> '}{path} "
                     f"({size} bytes, {n_lines} lines)")
                # Output: file head so model can verify it landed
                head = "\n".join(text.splitlines()[:8])
                if len(text.splitlines()) > 8:
                    head += f"\n…[+{len(text.splitlines()) - 8} more lines]"
                return ExecResult(action=a, ok=True, detail=d,
                                  output=f"$ write_file {path}\n{head}",
                                  elapsed_ms=int((time.time() - t0) * 1000))
            except Exception as e:
                return ExecResult(action=a, ok=False,
                                  detail=f"write_file failed: {type(e).__name__}: {e}",
                                  elapsed_ms=int((time.time() - t0) * 1000))

        elif t == "noop" or t == "":
            d = "noop"

        else:
            return ExecResult(action=a, ok=False, detail=f"unknown action type: {t!r}",
                              elapsed_ms=int((time.time() - t0) * 1000))

        return ExecResult(action=a, ok=True, detail=d,
                          elapsed_ms=int((time.time() - t0) * 1000))

    except pyautogui.FailSafeException:
        return ExecResult(action=a, ok=False,
                          detail="FAILSAFE triggered (mouse moved to corner) — aborted.",
                          elapsed_ms=int((time.time() - t0) * 1000))
    except ActionStopped as e:
        return ExecResult(action=a, ok=False, detail=str(e),
                          elapsed_ms=int((time.time() - t0) * 1000))
    except Exception as e:
        return ExecResult(action=a, ok=False, detail=f"{type(e).__name__}: {e}",
                          elapsed_ms=int((time.time() - t0) * 1000))
