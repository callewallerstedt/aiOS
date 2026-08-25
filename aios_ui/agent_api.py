"""Backend for the right-hand agent chat.

The Tk build's chat panel stopped running its own model loop a while ago: `send`
hands the typed turn to voice_dictation's VoiceAgent over a local socket "so
typed + spoken share one brain", and the reply comes back through the shared
event log. This module is the same wiring for the WebView shell, so the sidebar,
the voice overlay and the phone are three views of one conversation with one
tool set -- including the whole CODE surface (code_start, code_continue,
code_handoff and friends, with explicit provider/model/reasoning choices).

Nothing here re-implements the agent. It only carries text in and events out.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
VOICE_SCRIPT = BASE_DIR / "voice_dictation.py"
EVENTS_PATH = BASE_DIR / "phone_voice_events" / "events.jsonl"

VOICE_HOST = "127.0.0.1"
VOICE_PORT = 48737

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008

REASONING_LEVELS = {"minimal", "low", "medium", "high", "xhigh"}

# The event kinds worth showing in a transcript. reply_start/reply_done are
# consumed by the client from reply_delta + turn_done, so forwarding them too
# would double every answer.
RENDERED = {"turn_start", "status", "tool_start", "tool_done", "reply_delta", "turn_done"}


def _voice_listening(timeout: float = 0.15) -> bool:
    try:
        with socket.create_connection((VOICE_HOST, VOICE_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def _find_pythonw() -> str:
    for candidate in (
        BASE_DIR / ".venv" / "Scripts" / "pythonw.exe",
        Path(sys.executable).with_name("pythonw.exe"),
    ):
        if candidate.exists():
            return str(candidate)
    return sys.executable


def ensure_voice_server() -> bool:
    """Start voice_dictation.py if nothing is listening yet.

    Same contract as helper_overlay._ensure_voice_server: detached, no console,
    logs to the same two files, so both shells share one agent process rather
    than racing to own it.
    """
    if _voice_listening():
        return True
    if not VOICE_SCRIPT.exists():
        return False
    environment = os.environ.copy()
    environment.setdefault("VOICE_PRELOAD", "1")
    flags = CREATE_NO_WINDOW | DETACHED_PROCESS if sys.platform.startswith("win") else 0
    try:
        with open(BASE_DIR / "voice-out.log", "a", encoding="utf-8") as out, \
                open(BASE_DIR / "voice-err.log", "a", encoding="utf-8") as err:
            subprocess.Popen(
                [_find_pythonw(), str(VOICE_SCRIPT)],
                cwd=str(BASE_DIR),
                env=environment,
                stdout=out,
                stderr=err,
                stdin=subprocess.DEVNULL,
                creationflags=flags,
            )
    except OSError:
        return False
    # A freshly spawned server needs a moment to bind before the first ask.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if _voice_listening():
            return True
        time.sleep(0.12)
    return False


def _send(payload: Any, timeout: float = 1.5) -> dict:
    raw = payload if isinstance(payload, (bytes, bytearray)) else json.dumps(payload).encode("utf-8")
    try:
        with socket.create_connection((VOICE_HOST, VOICE_PORT), timeout=timeout) as client:
            client.sendall(raw)
        return {"ok": True}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def send(text: str, reasoning: str = "", model: str = "") -> dict:
    """Ask the agent a typed question. The answer arrives on the event stream."""
    text = str(text or "").strip()
    if not text:
        return {"ok": False, "error": "Type something first."}
    if not ensure_voice_server():
        return {"ok": False, "error": "The voice agent is not running and could not be started."}
    payload = {
        "cmd": "ask",
        "text": text,
        "echo_user": True,
        # The PC is right here; let it answer out loud like the overlay does.
        "speak_reply": True,
    }
    level = str(reasoning or "").strip().lower()
    if level in REASONING_LEVELS:
        payload["reasoning"] = level
    picked = str(model or "").strip()
    if picked:
        # The sidebar's selection wins for this turn even before the saved
        # default catches up.
        payload["model"] = picked
    return _send(payload)


def stop() -> dict:
    """Panic button: stop the spoken reply and the turn in flight."""
    return _send(b"stop_agent")


def reset() -> dict:
    """Forget the conversation, exactly as the Tk Reset button did."""
    result = _send({"cmd": "reset_agent"})
    # The voice process clears its in-memory history, but every client (this
    # sidebar, the phone, a freshly restarted aiOS) replays the shared event
    # log from byte 0. Truncate it too, or the old conversation reappears the
    # next time the app opens.
    try:
        EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        EVENTS_PATH.write_text("", encoding="utf-8")
    except OSError:
        pass
    return result


def read_events(since: int = 0) -> dict:
    """Read the shared agent log from a byte offset.

    Byte offsets rather than indices, matching the OPERATOR and phone readers: a
    trimmed (shrunk) file is a reset, and the client re-syncs from zero instead
    of silently skipping a slice of the conversation.
    """
    try:
        size = EVENTS_PATH.stat().st_size
    except OSError:
        return {"ok": True, "events": [], "size": 0, "reset": False, "running": running()}

    since = max(0, int(since or 0))
    reset_needed = since > size
    if reset_needed:
        since = 0

    # Binary, because the cursor is a byte offset: seeking a text stream to an
    # arbitrary offset is not something Python guarantees.
    try:
        with EVENTS_PATH.open("rb") as file:
            file.seek(since)
            chunk = file.read(max(0, size - since))
    except OSError as exc:
        return {"ok": False, "error": str(exc), "events": [], "size": since, "reset": False}

    events = []
    for raw in chunk.splitlines():
        if not raw.strip():
            continue
        try:
            event = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and str(event.get("type")) in RENDERED:
            events.append(event)

    return {"ok": True, "events": events, "size": size, "reset": reset_needed, "running": running()}


def running() -> bool:
    """True while a turn is in flight, so a reopened panel shows the spinner.

    Derived from the log rather than asked over the socket: the agent has no
    query command, and the last lifecycle event already carries the answer.
    """
    try:
        lines = EVENTS_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    for line in reversed(lines[-80:]):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = str(event.get("type") or "")
        if kind == "turn_done":
            return False
        if kind == "turn_start":
            # A turn that never finished is stale, not live.
            return (time.time() - float(event.get("ts") or 0)) < 600
    return False


def dispatch(route: str, method: str, params: dict, data: dict) -> dict | None:
    """Route table for aios_ui.server; None means "not mine".

    When Director mode is configured, the whole panel is served from the Linux
    coordinator instead of the local voice loop, so the sidebar and the phone
    are the same conversation. The local path stays intact for when Director is
    off or unreachable.
    """
    try:
        from . import director_link
        if director_link.enabled():
            answered = director_link.dispatch(route, method, params, data)
            if answered is not None:
                return answered
    except Exception:
        # A broken or unreachable Director must never take the sidebar down;
        # fall through to the local agent.
        pass

    if route == "/api/agent/send" and method == "POST":
        return send(str(data.get("text") or ""), str(data.get("reasoning") or ""),
                    str(data.get("model") or ""))
    if route == "/api/agent/stop" and method == "POST":
        return stop()
    if route == "/api/agent/reset" and method == "POST":
        return reset()
    if route == "/api/agent/log" and method == "GET":
        try:
            since = int((params.get("since") or ["0"])[0])
        except (TypeError, ValueError):
            since = 0
        return read_events(since)
    return None
