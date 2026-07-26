"""Talk to ChatGPT's Codex backend using the OAuth tokens stored by the
Codex CLI in ~/.codex/auth.json.

This lets the agent use your ChatGPT subscription instead of a billed API key,
calling the configured OPERATOR model. The endpoint and protocol are UNDOCUMENTED:
they can break with any Codex update. Falls back cleanly if auth is missing.

Sources used (Nov 2025 / 2026):
  - simonwillison.net/2025/Nov/9/gpt-5-codex-mini/  (request body shape)
  - github.com/icebear0828/codex-proxy                (headers + SSE format)
  - github.com/vnt87/codex-api-endpoint               (overall flow)
"""
from __future__ import annotations
import base64
import json
import os
import pathlib
import time
import uuid
from typing import Any

import httpx

CODEX_ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"
def _current_auth_path() -> pathlib.Path:
    configured = str(os.environ.get("AIOS_ACTIVE_CODEX_HOME") or os.environ.get("CODEX_HOME") or "").strip()
    if not configured:
        try:
            from aios_codex_accounts import active_home
            configured = str(active_home(pathlib.Path(__file__).resolve().parents[2] / "helper_config.json"))
        except Exception:
            configured = str(pathlib.Path.home() / ".codex")
    return pathlib.Path(configured).expanduser() / "auth.json"


class _DynamicAuthPath:
    """Path-like facade that resolves the active account on every request."""

    def is_file(self):
        return _current_auth_path().is_file()

    def read_text(self, *args, **kwargs):
        return _current_auth_path().read_text(*args, **kwargs)

    def __str__(self):
        return str(_current_auth_path())


AUTH_PATH = _DynamicAuthPath()
# These match what the Codex CLI sends — the endpoint accepts our requests
# only with the originator / user-agent it recognizes.
CODEX_USER_AGENT = "codex_cli_rs/0.40.0 (Windows; x64)"
CODEX_ORIGINATOR = "codex_cli_rs"


def _env_float(name: str, default: float) -> float:
    try:
        value = float(str(os.environ.get(name, "")).strip())
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


# Per-read timeout (httpx) and the total wall-clock cap for one call. The API
# path in vlm.py already honoured AIOS_MODEL_TIMEOUT; this path ignored it.
READ_TIMEOUT = _env_float("AIOS_MODEL_TIMEOUT", 150.0)
STREAM_DEADLINE = _env_float("AIOS_MODEL_DEADLINE", max(READ_TIMEOUT * 2, 300.0))
CONNECT_TIMEOUT = _env_float("AIOS_MODEL_CONNECT_TIMEOUT", 20.0)


# ---------------- auth ----------------

def auth_available() -> tuple[bool, str]:
    if not AUTH_PATH.is_file():
        return False, f"no auth file at {AUTH_PATH} — run `codex login`"
    try:
        d = json.loads(AUTH_PATH.read_text())
    except Exception as e:
        return False, f"unreadable auth.json: {e}"
    tok = (d.get("tokens") or {}).get("access_token")
    if not tok:
        return False, "auth.json has no tokens.access_token"
    return True, "ok"


def _decode_jwt_exp(token: str) -> int | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload)).get("exp") or 0)
    except Exception:
        return None


def _load_auth() -> dict:
    ok, msg = auth_available()
    if not ok:
        raise RuntimeError(f"Codex auth unavailable: {msg}")
    d = json.loads(AUTH_PATH.read_text())
    tok = d["tokens"]["access_token"]
    exp = _decode_jwt_exp(tok)
    if exp is not None and time.time() > exp - 30:
        raise RuntimeError(
            "Codex access_token expired. Run any `codex` command (or `codex login`) "
            "to refresh, then retry."
        )
    return d


def _headers(account_id: str, access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Chatgpt-Account-Id": account_id,
        "OpenAI-Beta": "responses=experimental",
        "Originator": CODEX_ORIGINATOR,
        "User-Agent": CODEX_USER_AGENT,
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
        "X-Client-Request-Id": str(uuid.uuid4()),
        "X-Openai-Internal-Codex-Residency": "global",
    }


# ---------------- message translation ----------------

def _convert_messages(messages: list[dict]) -> list[dict]:
    """Chat-Completions-style messages -> Responses-API `input` array."""
    out: list[dict] = []
    for m in messages:
        role = m["role"]
        if role == "system":
            # handled separately as `instructions`
            continue
        raw = m.get("content", "")
        parts: list[dict] = []
        if isinstance(raw, str):
            text_type = "output_text" if role == "assistant" else "input_text"
            parts.append({"type": text_type, "text": raw})
        else:
            for c in raw:
                ct = c.get("type")
                if ct == "text":
                    text_type = "output_text" if role == "assistant" else "input_text"
                    parts.append({"type": text_type, "text": c.get("text", "")})
                elif ct == "image_url":
                    url = c["image_url"]["url"] if isinstance(c["image_url"], dict) else c["image_url"]
                    parts.append({"type": "input_image", "image_url": url})
                elif ct in ("input_text", "output_text", "input_image"):
                    parts.append(c)
        out.append({"type": "message", "role": role, "content": parts})
    return out


def _extract_system(messages: list[dict]) -> str:
    sys_parts = []
    for m in messages:
        if m["role"] != "system":
            continue
        c = m.get("content", "")
        if isinstance(c, str):
            sys_parts.append(c)
        elif isinstance(c, list):
            for p in c:
                if p.get("type") in ("text", "input_text"):
                    sys_parts.append(p.get("text", ""))
    return "\n\n".join(sys_parts)


# ---------------- SSE -> final text ----------------

def _parse_sse(stream_bytes_iter, deadline: float | None = None) -> tuple[str, dict]:
    """Accumulate response text and normalized token usage.

    `deadline` is a monotonic wall-clock cut-off for the WHOLE stream. httpx's
    own timeout is per read, so it resets on every chunk: a stream that dribbles
    keepalives but never completes would block here forever, which is exactly
    how runs used to freeze mid-step with no error and no way out.
    """
    chunks: list[str] = []
    usage: dict = {}
    buf = ""
    for raw in stream_bytes_iter:
        if deadline is not None and time.monotonic() > deadline:
            raise TimeoutError(
                f"Codex stream exceeded {STREAM_DEADLINE:.0f}s without completing "
                f"({len(''.join(chunks))} chars received)."
            )
        if not raw:
            continue
        buf += raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        while "\n\n" in buf:
            event_block, buf = buf.split("\n\n", 1)
            for line in event_block.splitlines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    j = json.loads(data)
                except Exception:
                    continue
                t = j.get("type", "")
                if t == "response.output_text.delta":
                    chunks.append(j.get("delta", ""))
                elif t == "response.completed":
                    raw_usage = (j.get("response") or {}).get("usage") or {}
                    details = raw_usage.get("input_tokens_details") or {}
                    input_tokens = int(raw_usage.get("input_tokens") or 0)
                    output_tokens = int(raw_usage.get("output_tokens") or 0)
                    usage = {
                        "requests": 1,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cached_input_tokens": int(details.get("cached_tokens") or 0),
                        "total_tokens": int(raw_usage.get("total_tokens") or input_tokens + output_tokens),
                    }
                    # also has full content in `response.output[]` — but we
                    # already accumulated deltas
                    pass
                elif t in ("response.error", "error"):
                    raise RuntimeError(f"Codex stream error: {json.dumps(j)[:500]}")
    return "".join(chunks), usage


# ---------------- public ----------------

def chat_with_usage(system: str, messages: list[dict], model: str = "gpt-5.6-luna",
                    timeout: float | None = None, reasoning_effort: str | None = None) -> tuple[str, dict]:
    """Send a chat (with optional images) to the Codex backend; return the
    final assistant text. Drop-in replacement for agent.vlm.chat_raw."""
    auth = _load_auth()
    tokens = auth["tokens"]
    headers = _headers(tokens["account_id"], tokens["access_token"])

    # Merge the explicit `system` arg with any system messages in `messages`.
    inline_sys = _extract_system(messages)
    instructions = "\n\n".join(s for s in [system, inline_sys] if s)
    if not instructions:
        instructions = "You are a helpful assistant."  # endpoint requires non-empty

    reasoning = {"summary": "auto"}
    effort = str(reasoning_effort or "").strip().lower()
    if effort in {"minimal", "low", "medium", "high"}:
        reasoning["effort"] = effort

    body = {
        "model": model,
        "instructions": instructions,
        "input": _convert_messages(messages),
        "tools": [],
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "reasoning": reasoning,
        "store": False,
        "stream": True,
        "include": ["reasoning.encrypted_content"],
        "prompt_cache_key": str(uuid.uuid4()),
    }

    read_timeout = float(timeout) if timeout else READ_TIMEOUT
    # Two independent guards: httpx trips when a single read stalls, the
    # deadline trips when the stream stays alive but never finishes.
    limits = httpx.Timeout(read_timeout, connect=CONNECT_TIMEOUT)
    deadline = time.monotonic() + STREAM_DEADLINE
    with httpx.Client(timeout=limits) as cl:
        with cl.stream("POST", CODEX_ENDPOINT, json=body, headers=headers) as r:
            if r.status_code != 200:
                txt = r.read().decode("utf-8", "replace")
                raise RuntimeError(f"Codex backend HTTP {r.status_code}: {txt[:600]}")
            text, usage = _parse_sse(r.iter_bytes(), deadline=deadline)
    if not text:
        raise RuntimeError("Codex backend returned no text (empty stream).")
    usage.update({"backend": "codex", "model": model, "requests": int(usage.get("requests") or 1)})
    return text, usage


def chat_raw(system: str, messages: list[dict], model: str = "gpt-5.6-luna",
             timeout: float | None = None, reasoning_effort: str | None = None) -> str:
    return chat_with_usage(
        system, messages, model=model, timeout=timeout,
        reasoning_effort=reasoning_effort,
    )[0]
