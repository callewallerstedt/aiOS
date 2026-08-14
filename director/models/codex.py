"""Codex backend: ChatGPT OAuth tokens -> the Codex responses endpoint.

Ported to asyncio from `agent_clicker/agent/codex_backend.py`, with two things
that module does not need and Director cannot live without:

  * function tools. Verified against the live endpoint: `tools` of type
    "function" produce `function_call` items with a `call_id`, and a
    `function_call_output` item feeds the result back. Items rebuilt from
    storage (no `id`, no `status`) are accepted, which is what lets a
    conversation resume after a restart.
  * token refresh. Access tokens live ten days. An always-on coordinator
    cannot ask someone to run `codex login` every ten days, so it refreshes
    against the issuer's published token endpoint and writes auth.json back
    atomically, exactly like the CLI does.

The endpoint itself is undocumented and can change with any Codex release. All
failures raise ModelError with the response body attached so the phone shows
something true rather than a spinner.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import pathlib
import time
import uuid
from typing import Any, Awaitable, Callable

import aiohttp

from .. import config
from . import ModelError

CODEX_ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"
# From the issuer's OIDC discovery document at https://auth.openai.com.
TOKEN_ENDPOINT = "https://auth.openai.com/api/accounts/oauth/token"
# The endpoint only answers requests carrying the originator it recognises.
USER_AGENT = "codex_cli_rs/0.40.0 (Linux; x64)"
ORIGINATOR = "codex_cli_rs"

REFRESH_MARGIN = 1800.0   # refresh when under 30 minutes of life remains
_REFRESH_LOCK = asyncio.Lock()


def auth_path(settings: dict[str, Any] | None = None) -> pathlib.Path:
    cfg = settings if settings is not None else config.load_settings()
    configured = str((cfg.get("backends", {}).get("codex", {}) or {}).get("codex_home") or "").strip()
    configured = configured or str(os.environ.get("CODEX_HOME", "")).strip()
    base = pathlib.Path(configured).expanduser() if configured else (pathlib.Path.home() / ".codex")
    return base / "auth.json"


def _read_auth(settings: dict[str, Any] | None = None) -> dict:
    path = auth_path(settings)
    if not path.is_file():
        raise ModelError(
            f"Codex is not signed in on this machine (no {path}). "
            "Run `codex login` on the Director box.", backend="codex")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelError(f"unreadable {path}: {exc}", backend="codex") from exc
    if not (data.get("tokens") or {}).get("access_token"):
        raise ModelError(f"{path} has no tokens.access_token", backend="codex")
    return data


def _write_auth(data: dict, settings: dict[str, Any] | None = None) -> None:
    """Atomic write. A half-written auth.json would log the box out of Codex."""
    path = auth_path(settings)
    temp = path.with_suffix(".json.director-tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    temp.replace(path)


def token_expiry(access_token: str) -> int | None:
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload)).get("exp") or 0)
    except (IndexError, ValueError, TypeError, json.JSONDecodeError):
        return None


def token_client_id(access_token: str) -> str:
    """The OAuth client this token was minted for, read from the token itself
    rather than hard-coded, so a Codex release that re-registers still works."""
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return str(json.loads(base64.urlsafe_b64decode(payload)).get("client_id") or "")
    except (IndexError, ValueError, TypeError, json.JSONDecodeError):
        return ""


def status(*, settings: dict[str, Any] | None = None) -> tuple[bool, str]:
    try:
        data = _read_auth(settings)
    except ModelError as exc:
        return False, str(exc)
    token = data["tokens"]["access_token"]
    exp = token_expiry(token)
    if exp is None:
        return True, "Codex signed in (token expiry unknown)"
    left = exp - time.time()
    if left <= 0:
        return False, "Codex token expired — Director will refresh on the next call"
    return True, f"Codex signed in ({left / 86400:.1f} days of token left)"


async def _refresh(session: aiohttp.ClientSession, settings: dict[str, Any] | None = None) -> dict:
    """Exchange the refresh token for a new access token and persist it."""
    data = _read_auth(settings)
    tokens = data.get("tokens") or {}
    refresh_token = str(tokens.get("refresh_token") or "").strip()
    if not refresh_token:
        raise ModelError(
            "Codex access token expired and auth.json has no refresh_token. "
            "Run `codex login` on the Director box.", backend="codex")
    client_id = token_client_id(str(tokens.get("access_token") or ""))
    if not client_id:
        raise ModelError("could not read client_id from the Codex token", backend="codex")
    body = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": "openid profile email offline_access",
    }
    async with session.post(TOKEN_ENDPOINT, json=body,
                            headers={"Content-Type": "application/json"},
                            timeout=aiohttp.ClientTimeout(total=45)) as resp:
        text = await resp.text()
        if resp.status != 200:
            raise ModelError(
                f"Codex token refresh failed (HTTP {resp.status}): {text[:400]}. "
                "Run `codex login` on the Director box.", backend="codex")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ModelError(f"Codex token refresh returned non-JSON: {text[:200]}",
                             backend="codex") from exc

    access = str(payload.get("access_token") or "").strip()
    if not access:
        raise ModelError("Codex token refresh returned no access_token", backend="codex")
    tokens["access_token"] = access
    # The grant rotates the refresh token; dropping the new one would strand
    # the box at the next refresh.
    if payload.get("refresh_token"):
        tokens["refresh_token"] = str(payload["refresh_token"])
    if payload.get("id_token"):
        tokens["id_token"] = str(payload["id_token"])
    data["tokens"] = tokens
    data["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%S.000000000Z", time.gmtime())
    _write_auth(data, settings)
    return data


async def _tokens(session: aiohttp.ClientSession, settings: dict[str, Any] | None = None,
                  *, force_refresh: bool = False) -> dict:
    data = _read_auth(settings)
    tokens = data.get("tokens") or {}
    exp = token_expiry(str(tokens.get("access_token") or ""))
    stale = force_refresh or (exp is not None and time.time() > exp - REFRESH_MARGIN)
    if not stale:
        return tokens
    async with _REFRESH_LOCK:
        # Another coroutine may have refreshed while we waited for the lock.
        data = _read_auth(settings)
        tokens = data.get("tokens") or {}
        exp = token_expiry(str(tokens.get("access_token") or ""))
        if not force_refresh and exp is not None and time.time() < exp - REFRESH_MARGIN:
            return tokens
        data = await _refresh(session, settings)
        return data.get("tokens") or {}


def _headers(tokens: dict) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {tokens['access_token']}",
        "Chatgpt-Account-Id": str(tokens.get("account_id") or ""),
        "OpenAI-Beta": "responses=experimental",
        "Originator": ORIGINATOR,
        "User-Agent": USER_AGENT,
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
        "X-Client-Request-Id": str(uuid.uuid4()),
        "X-Openai-Internal-Codex-Residency": "global",
    }


# ---------------- item translation ----------------

def _content_parts(parts: list[dict], role: str) -> list[dict]:
    text_type = "output_text" if role == "assistant" else "input_text"
    out: list[dict] = []
    for part in parts or []:
        kind = part.get("type")
        if kind == "text":
            out.append({"type": text_type, "text": str(part.get("text") or "")})
        elif kind == "image":
            out.append({"type": "input_image", "image_url": str(part.get("url") or "")})
    return out


def to_input(items: list[dict]) -> list[dict]:
    """Normalized items -> the Responses `input` array."""
    out: list[dict] = []
    for item in items or []:
        kind = item.get("type")
        if kind == "message":
            role = str(item.get("role") or "user")
            parts = _content_parts(item.get("content") or [], role)
            if parts:
                out.append({"type": "message", "role": role, "content": parts})
        elif kind == "tool_call":
            out.append({
                "type": "function_call",
                "call_id": str(item.get("call_id") or ""),
                "name": str(item.get("name") or ""),
                "arguments": str(item.get("arguments") or "{}"),
            })
        elif kind == "tool_result":
            out.append({
                "type": "function_call_output",
                "call_id": str(item.get("call_id") or ""),
                "output": str(item.get("output") or ""),
            })
    return out


def to_tools(tools: list[dict]) -> list[dict]:
    out = []
    for tool in tools or []:
        out.append({
            "type": "function",
            "name": str(tool.get("name") or ""),
            "description": str(tool.get("description") or ""),
            "strict": False,
            "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


def reasoning_block(level: str) -> dict[str, Any]:
    """Map an aiOS reasoning level onto the endpoint's `reasoning` field.

    aiOS calls the cheapest level "none"; this endpoint calls it "minimal".
    Sending the wrong word means the field is ignored and reasoning tokens get
    spent at the provider default.
    """
    block: dict[str, Any] = {"summary": "auto"}
    effort = str(level or "").strip().lower()
    if effort in {"none", "minimal", "off"}:
        block["effort"] = "minimal"
    elif effort in {"low", "medium", "high"}:
        block["effort"] = effort
    elif effort in {"xhigh", "max", "ultra"}:
        block["effort"] = "high"
    return block


def _usage_from(raw: dict) -> dict:
    details = raw.get("input_tokens_details") or {}
    input_tokens = int(raw.get("input_tokens") or 0)
    output_tokens = int(raw.get("output_tokens") or 0)
    return {
        "requests": 1,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": int(details.get("cached_tokens") or 0),
        "total_tokens": int(raw.get("total_tokens") or input_tokens + output_tokens),
    }


async def complete(*, model: str, instructions: str, items: list[dict],
                   tools: list[dict], tool_choice: str = "auto",
                   reasoning: str, timeout: float,
                   on_delta: Callable[[str], Awaitable[None]] | None = None,
                   on_reasoning: Callable[[str], Awaitable[None]] | None = None,
                   settings: dict[str, Any] | None = None) -> dict:
    body = {
        "model": model,
        "instructions": instructions or "You are a helpful assistant.",
        "input": to_input(items),
        "tools": to_tools(tools),
        "tool_choice": tool_choice,
        "parallel_tool_calls": False,
        "reasoning": reasoning_block(reasoning),
        "store": False,
        "stream": True,
        "include": ["reasoning.encrypted_content"],
        "prompt_cache_key": str(uuid.uuid4()),
    }

    client_timeout = aiohttp.ClientTimeout(total=None, sock_read=timeout, connect=20)
    async with aiohttp.ClientSession(timeout=client_timeout) as session:
        tokens = await _tokens(session, settings)
        result = await _stream_once(session, tokens, body, on_delta, on_reasoning, timeout)
        if result is None:
            # 401: the token died earlier than its `exp` claimed. Refresh once.
            tokens = await _tokens(session, settings, force_refresh=True)
            result = await _stream_once(session, tokens, body, on_delta, on_reasoning, timeout)
            if result is None:
                raise ModelError("Codex rejected the refreshed token (HTTP 401). "
                                 "Run `codex login` on the Director box.", backend="codex")
    result["backend"] = "codex"
    result["model"] = model
    return result


async def _stream_once(session: aiohttp.ClientSession, tokens: dict, body: dict,
                       on_delta, on_reasoning, timeout: float) -> dict | None:
    """One streamed request. Returns None on 401 so the caller can refresh."""
    text_chunks: list[str] = []
    reasoning_chunks: list[str] = []
    tool_calls: list[dict] = []
    usage: dict = {}
    deadline = time.monotonic() + max(timeout * 2, 300.0)

    async with session.post(CODEX_ENDPOINT, json=body, headers=_headers(tokens)) as resp:
        if resp.status == 401:
            await resp.read()
            return None
        if resp.status != 200:
            detail = (await resp.text())[:600]
            raise ModelError(f"Codex backend HTTP {resp.status}: {detail}",
                             retryable=resp.status in (429, 500, 502, 503, 504),
                             backend="codex")
        buffer = ""
        async for chunk in resp.content.iter_any():
            if time.monotonic() > deadline:
                raise ModelError(
                    f"Codex stream ran past {deadline:.0f}s without completing "
                    f"({len(''.join(text_chunks))} chars received)",
                    retryable=True, backend="codex")
            buffer += chunk.decode("utf-8", errors="replace")
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                for line in block.splitlines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    kind = event.get("type", "")
                    if kind == "response.output_text.delta":
                        delta = str(event.get("delta") or "")
                        text_chunks.append(delta)
                        if on_delta and delta:
                            await on_delta(delta)
                    elif kind == "response.reasoning_summary_text.delta":
                        delta = str(event.get("delta") or "")
                        reasoning_chunks.append(delta)
                        if on_reasoning and delta:
                            await on_reasoning(delta)
                    elif kind == "response.output_item.done":
                        item = event.get("item") or {}
                        if item.get("type") == "function_call":
                            tool_calls.append({
                                "call_id": str(item.get("call_id") or ""),
                                "name": str(item.get("name") or ""),
                                "arguments": str(item.get("arguments") or "{}"),
                            })
                    elif kind == "response.completed":
                        usage = _usage_from((event.get("response") or {}).get("usage") or {})
                    elif kind in ("response.error", "error"):
                        raise ModelError(f"Codex stream error: {json.dumps(event)[:400]}",
                                         retryable=True, backend="codex")

    return {
        "text": "".join(text_chunks),
        "reasoning": "".join(reasoning_chunks),
        "tool_calls": tool_calls,
        "usage": usage,
    }
