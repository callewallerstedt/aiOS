"""Point the desktop's AGENT chat at Director instead of the local voice loop.

Same contract as `agent_api` — send / stop / reset / read_events — so the
sidebar, the voice overlay and the phone stay three views of one conversation.
The difference is where the conversation lives: on the Linux box, so closing
the laptop does not end it and the phone shows the same thread.

Enable it in helper_config.json:

    {"director": {"enabled": true,
                  "url": "https://rocky-server.tail4d08fd.ts.net/director",
                  "token": "<a device token>",
                  "agent_id": "agt_director"}}

The token is a *device* token (from pairing), not the machine token that
director_client.py uses: this is a person reading their own chat, not a machine
answering calls.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "helper_config.json"
CLIENT_CONFIG = BASE_DIR / "aios_director_client.json"

TIMEOUT = 25.0
_LOCK = threading.Lock()
_STATE: dict[str, Any] = {"thread_id": "", "agent_id": "", "checked": 0.0}


def _config() -> dict:
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    section = raw.get("director")
    return section if isinstance(section, dict) else {}


def enabled() -> bool:
    config = _config()
    return bool(config.get("enabled")) and bool(str(config.get("url") or "").strip()) \
        and bool(str(config.get("token") or "").strip())


def _request(path: str, payload: dict | None = None, method: str = "") -> dict:
    config = _config()
    url = str(config.get("url") or "").rstrip("/")
    token = str(config.get("token") or "")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{url}{path}", data=data, method=method or ("POST" if data else "GET"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        return {"ok": False, "error": f"Director HTTP {exc.code}: {detail}"}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return {"ok": False, "error": f"Director unreachable: {exc}"}


def thread_id(*, refresh: bool = False) -> str:
    """The thread this desktop shares with the phone."""
    with _LOCK:
        fresh_enough = time.time() - float(_STATE.get("checked") or 0) < 300
        if _STATE["thread_id"] and fresh_enough and not refresh:
            return str(_STATE["thread_id"])
    agent = str(_config().get("agent_id") or "agt_director")
    got = _request(f"/api/agents/{agent}/thread")
    if not got.get("ok"):
        return ""
    with _LOCK:
        _STATE["thread_id"] = str((got.get("thread") or {}).get("id") or "")
        _STATE["agent_id"] = agent
        _STATE["checked"] = time.time()
        return str(_STATE["thread_id"])


def _backend_split(model_id: str) -> tuple[str, str]:
    """Map a sidebar model id onto Director's two backends.

    `openrouter:<id>` routes to Director's OpenRouter backend; anything else
    (the gpt-5.6-* names) rides the Codex backend. Ollama models are local to
    this PC and have no Director equivalent, so they send no override.
    """
    raw = str(model_id or "").strip()
    if not raw:
        return "", ""
    if raw.startswith("openrouter:"):
        return "openrouter", raw.split(":", 1)[1]
    if raw.startswith("ollama:"):
        return "", ""
    return "codex", raw


def send(text: str, reasoning: str = "", model: str = "") -> dict:
    text = str(text or "").strip()
    if not text:
        return {"ok": False, "error": "Type something first."}
    target = thread_id()
    if not target:
        return {"ok": False, "error": "Director is not reachable."}
    body = {"text": text}
    # The desktop dropdown owns the brain choice; without this the Linux side
    # silently keeps its own agent defaults (historically the Codex backend).
    backend, picked = _backend_split(model)
    if picked:
        body["backend"] = backend
        body["model"] = picked
    level = str(reasoning or "").strip().lower()
    if level in {"minimal", "low", "medium", "high", "xhigh"}:
        body["reasoning"] = level
    got = _request(f"/api/threads/{target}/messages", body)
    return {"ok": bool(got.get("ok")), "error": got.get("error", "")}


def stop() -> dict:
    target = thread_id()
    if not target:
        return {"ok": False, "error": "Director is not reachable."}
    return _request(f"/api/threads/{target}/stop", {})


def reset() -> dict:
    """Start a fresh conversation — the phone sees the same new thread."""
    target = thread_id()
    if not target:
        return {"ok": False, "error": "Director is not reachable."}
    got = _request(f"/api/threads/{target}/clear", {})
    if got.get("ok"):
        with _LOCK:
            _STATE["thread_id"] = str((got.get("thread") or {}).get("id") or "")
            _STATE["checked"] = time.time()
    return got


def translate(event: dict) -> dict | None:
    """Director event -> the shape aios_ui/web/js/chat.js already renders."""
    kind = str(event.get("kind") or "")
    payload = event.get("payload") or {}

    if kind == "message.user":
        return {"type": "turn_start", "text": str(payload.get("text") or "")}
    if kind == "message.delta":
        return {"type": "reply_delta", "text": str(payload.get("text") or "")}
    if kind == "message.assistant":
        return {"type": "turn_done", "text": str(payload.get("text") or "")}
    if kind == "thread.error":
        return {"type": "turn_done", "text": str(payload.get("error") or "That failed."),
                "error": True}
    if kind == "tool.start":
        name = str(payload.get("name") or "tool")
        return {"type": "tool_start", "text": f"{name}…"}
    if kind == "tool.done":
        card = payload.get("card") or {}
        label = " · ".join(part for part in [str(card.get("title") or payload.get("name") or "tool"),
                                             str(card.get("preview") or "")] if part)
        return {"type": "tool_done", "text": label, "ok": card.get("tone") != "danger"}
    if kind == "approval":
        return {"type": "tool_done",
                "text": f"Waiting for your approval on the phone: {payload.get('summary', '')}",
                "ok": True}
    if kind == "question":
        return {"type": "tool_done",
                "text": f"Director asked: {payload.get('question', '')}", "ok": True}
    if kind in ("operator.started", "code.started"):
        return {"type": "tool_start", "text": str(payload.get("task") or "working")}
    if kind == "job.finished":
        return {"type": "tool_done", "text": f"{payload.get('kind', 'job')}: {payload.get('status', '')}",
                "ok": payload.get("status") == "done"}
    if kind == "thread.status":
        status = str(payload.get("status") or "")
        if status in ("running", "waiting"):
            return {"type": "status", "text": "waiting for you" if status == "waiting" else "thinking"}
        return None
    return None


def read_events(since: int = 0) -> dict:
    """Poll Director's event log.

    `size` carries Director's event id, which is monotonic, so the existing
    cursor logic in the panel works unchanged. A cursor ahead of the server
    means Director was rebuilt, and the panel resyncs from zero rather than
    skipping a slice of the conversation.
    """
    target = thread_id()
    if not target:
        return {"ok": False, "error": "Director is not reachable.", "events": [],
                "size": since, "reset": False, "running": False}

    got = _request(f"/api/events?since={max(0, int(since or 0))}&thread_id={target}")
    if not got.get("ok"):
        return {"ok": False, "error": got.get("error", ""), "events": [],
                "size": since, "reset": False, "running": False}

    events, cursor, running = [], int(since or 0), False
    for raw in got.get("events") or []:
        cursor = max(cursor, int(raw.get("id") or 0))
        translated = translate(raw)
        if translated:
            events.append(translated)
        if raw.get("kind") == "thread.status":
            running = str((raw.get("payload") or {}).get("status")) in ("running", "waiting")

    latest = int(got.get("cursor") or cursor)
    reset_needed = int(since or 0) > latest and latest > 0
    return {"ok": True, "events": events, "size": max(cursor, latest),
            "reset": reset_needed, "running": running}


def dispatch(route: str, method: str, params: dict, data: dict) -> dict | None:
    """Same route table as agent_api, used when Director mode is on."""
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
