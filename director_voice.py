"""Route this PC's voice agent at Director.

Whisper, the hotkeys and the speaking overlay all stay here — only the brain
moves. `DirectorVoiceAgent` presents the same surface `voice_dictation` already
uses for `VoiceAgent` (run / cancel / history / clear plus the on_event stream),
so a spoken turn lands in the same conversation the phone is showing.

Turned on in helper_config.json:

    {"director": {"enabled": true, "voice": true,
                  "url": "https://rocky-server.tail4d08fd.ts.net/director",
                  "token": "<device token>",
                  "agent_id": "agt_director"}}

With `enabled` false, or Director unreachable, voice_dictation keeps using the
local VoiceAgent exactly as before.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "helper_config.json"

POLL_SECONDS = 1.2
TURN_TIMEOUT = 900.0
REQUEST_TIMEOUT = 25.0


@dataclass
class DirectorResult:
    """Same fields voice_dictation reads off a VoiceAgent result."""
    reply: str = ""
    error: str = ""
    tools: list = field(default_factory=list)
    tool_details: list = field(default_factory=list)
    elapsed: float = 0.0
    cancelled: bool = False


def config() -> dict:
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    section = raw.get("director")
    return section if isinstance(section, dict) else {}


def enabled() -> bool:
    """True when spoken turns should go to Director rather than the local loop."""
    section = config()
    return (bool(section.get("enabled")) and bool(section.get("voice", True))
            and bool(str(section.get("url") or "").strip())
            and bool(str(section.get("token") or "").strip()))


class DirectorVoiceAgent:
    """A drop-in stand-in for VoiceAgent that talks to the Linux coordinator."""

    def __init__(self, on_event: Callable[[str, Any], None] | None = None,
                 speak: Callable[[str], None] | None = None, **_ignored: Any) -> None:
        # voice_dictation passes type_text/copy_text/hide_overlay too; those are
        # local-tool plumbing this agent does not use — Director has its own
        # hands. Accepting and ignoring them keeps the call site unchanged.
        self.on_event = on_event
        self.speak = speak
        self._cancel = threading.Event()
        self._thread_id = ""
        self._cursor = 0
        self._turns: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    # ---- transport ----

    def _request(self, path: str, payload: dict | None = None) -> dict:
        section = config()
        url = str(section.get("url") or "").rstrip("/")
        token = str(section.get("token") or "")
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"{url}{path}", data=data, method="POST" if data is not None else "GET",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return {"ok": False, "error": f"Director HTTP {exc.code}"}
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            return {"ok": False, "error": f"Director unreachable: {exc}"}

    def _ensure_thread(self) -> str:
        if self._thread_id:
            return self._thread_id
        agent = str(config().get("agent_id") or "agt_director")
        got = self._request(f"/api/agents/{agent}/thread")
        if got.get("ok"):
            self._thread_id = str((got.get("thread") or {}).get("id") or "")
            self._cursor = int(got.get("cursor") or 0)
        return self._thread_id

    def _emit(self, kind: str, payload: Any) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(kind, payload)
        except Exception:
            # A UI callback must never break the turn.
            pass

    # ---- the VoiceAgent surface ----

    def run(self, text: str, overrides: dict | None = None) -> DirectorResult:
        started = time.monotonic()
        self._cancel.clear()
        thread = self._ensure_thread()
        if not thread:
            return DirectorResult(error="Director is not reachable.",
                                  elapsed=time.monotonic() - started)

        with self._lock:
            self._turns.append(("user", str(text or "")))

        sent = self._request(f"/api/threads/{thread}/messages", {"text": str(text or "")})
        if not sent.get("ok"):
            return DirectorResult(error=str(sent.get("error") or "Director refused the turn."),
                                  elapsed=time.monotonic() - started)

        reply, error = "", ""
        tools: list[str] = []
        details: list[dict] = []
        idle_seen = 0
        deadline = time.monotonic() + TURN_TIMEOUT

        while time.monotonic() < deadline:
            if self._cancel.is_set():
                self._request(f"/api/threads/{thread}/stop", {})
                return DirectorResult(reply=reply, cancelled=True, tools=tools,
                                      tool_details=details,
                                      elapsed=time.monotonic() - started)
            time.sleep(POLL_SECONDS)
            got = self._request(f"/api/events?since={self._cursor}&thread_id={thread}")
            if not got.get("ok"):
                continue
            for event in got.get("events") or []:
                self._cursor = max(self._cursor, int(event.get("id") or 0))
                kind = str(event.get("kind") or "")
                payload = event.get("payload") or {}
                if kind == "message.assistant":
                    reply = str(payload.get("text") or "")
                    with self._lock:
                        self._turns.append(("assistant", reply))
                    self._emit("reply_delta", reply)
                elif kind == "thread.error":
                    error = str(payload.get("error") or "That turn failed.")
                elif kind == "tool.start":
                    name = str(payload.get("name") or "tool")
                    self._emit("tool_start", {"name": name, "label": name})
                elif kind == "tool.done":
                    card = payload.get("card") or {}
                    name = str(payload.get("name") or "tool")
                    label = str(card.get("title") or name)
                    preview = str(card.get("preview") or "")
                    tools.append(name)
                    details.append({"name": name, "label": label, "detail": preview,
                                    "ok": card.get("tone") != "danger"})
                    with self._lock:
                        self._turns.append(("tool", f"{label} {preview}".strip()))
                    self._emit("tool_done", {"name": name, "label": label,
                                             "ok": card.get("tone") != "danger"})
                elif kind in ("question", "approval"):
                    summary = str(payload.get("question") or payload.get("summary") or "")
                    self._emit("status", f"waiting for you: {summary}"[:120])
                elif kind == "thread.status":
                    status = str(payload.get("status") or "")
                    if status == "running":
                        self._emit("status", "thinking")
                    elif status == "idle":
                        idle_seen += 1
            # One idle is the end of the turn. A job dispatched during it wakes
            # the coordinator again, and that reply arrives as its own turn.
            if idle_seen:
                break

        return DirectorResult(reply=reply, error=error, tools=tools, tool_details=details,
                              elapsed=time.monotonic() - started,
                              cancelled=self._cancel.is_set())

    def cancel(self) -> None:
        self._cancel.set()
        if self._thread_id:
            self._request(f"/api/threads/{self._thread_id}/stop", {})

    def history(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._turns)

    def history_before_current(self) -> list[tuple[str, str]]:
        rows = self.history()
        while rows and rows[-1][0] != "assistant":
            rows.pop()
        return rows

    def clear(self) -> None:
        thread = self._ensure_thread()
        if thread:
            got = self._request(f"/api/threads/{thread}/clear", {})
            if got.get("ok"):
                self._thread_id = str((got.get("thread") or {}).get("id") or "")
        with self._lock:
            self._turns.clear()
