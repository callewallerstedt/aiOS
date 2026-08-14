"""OpenRouter backend for Director.

The request shape mirrors the one already proven in this repo's
`openrouter_client.py` — same base URL, same headers, same
`reasoning: {"effort": ...}` mapping, same `tools` / `tool_choice` pair — but
async and streaming, translating Director's normalized items to and from
chat-completions messages.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable

import aiohttp

from .. import config
from . import ModelError

API_BASE = "https://openrouter.ai/api/v1"
HTTP_REFERER = "https://github.com/callewallerstedt/aios"
APP_TITLE = "aiOS Director"
FEATURED_MODELS = (
    {
        "id": "openai/gpt-5.6-luna",
        "label": "GPT-5.6 Luna",
        "reasoning": ["none", "low", "medium"],
        "default_reasoning": "low",
    },
)
_BALANCE_CACHE: dict[str, Any] | None = None
_BALANCE_CACHE_KEY = ""
_BALANCE_CACHE_AT = 0.0
_BALANCE_CACHE_TTL = 45.0


def status(*, settings: dict[str, Any] | None = None) -> tuple[bool, str]:
    key = config.openrouter_key(settings)
    if not key:
        return False, "No OpenRouter API key configured"
    return True, "OpenRouter key configured"


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": HTTP_REFERER,
        "X-Title": APP_TITLE,
    }


def _balance_from_payload(payload: Any) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ValueError("OpenRouter returned no credit data.")
    purchased = float(data["total_credits"])
    used = float(data["total_usage"])
    return {
        "ok": True,
        "currency": "USD",
        "balance": purchased - used,
        "total_credits": purchased,
        "total_usage": used,
    }


async def credit_balance(*, refresh: bool = False,
                         settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the same remaining-credit figures shown by aiOS CODE."""
    global _BALANCE_CACHE, _BALANCE_CACHE_KEY, _BALANCE_CACHE_AT
    key = config.openrouter_key(settings)
    if not key:
        return {"ok": False, "error": "Add your OpenRouter API key in Director settings."}
    now = time.time()
    if (not refresh and _BALANCE_CACHE and _BALANCE_CACHE_KEY == key
            and now - _BALANCE_CACHE_AT < _BALANCE_CACHE_TTL):
        return dict(_BALANCE_CACHE)

    timeout = aiohttp.ClientTimeout(total=12)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{API_BASE}/credits", headers=_headers(key)) as resp:
                payload = await resp.json(content_type=None)
                if resp.status != 200:
                    detail = payload.get("error") if isinstance(payload, dict) else payload
                    if isinstance(detail, dict):
                        detail = detail.get("message") or detail
                    return {"ok": False, "error": f"OpenRouter HTTP {resp.status}: {detail}"}
        result = _balance_from_payload(payload)
    except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError,
            KeyError, TypeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}

    _BALANCE_CACHE = result
    _BALANCE_CACHE_KEY = key
    _BALANCE_CACHE_AT = now
    return dict(result)


def _effort(reasoning: str) -> dict[str, str]:
    level = str(reasoning or "").strip().lower()
    if level in {"", "off", "none", "false", "0", "minimal"}:
        return {"effort": "none"} if level != "minimal" else {"effort": "low"}
    if level in {"xhigh", "max", "ultra"}:
        return {"effort": "xhigh"}
    if level in {"high", "medium", "low"}:
        return {"effort": level}
    return {"effort": "medium"}


def to_messages(instructions: str, items: list[dict]) -> list[dict]:
    """Normalized items -> chat-completions messages."""
    out: list[dict] = []
    if instructions:
        out.append({"role": "system", "content": instructions})
    for item in items or []:
        kind = item.get("type")
        if kind == "message":
            role = str(item.get("role") or "user")
            parts = item.get("content") or []
            texts = [str(p.get("text") or "") for p in parts if p.get("type") == "text"]
            images = [str(p.get("url") or "") for p in parts if p.get("type") == "image"]
            if images and role == "user":
                content: Any = [{"type": "text", "text": "\n".join(texts)}]
                content += [{"type": "image_url", "image_url": {"url": url}} for url in images]
            else:
                content = "\n".join(texts)
            out.append({"role": role, "content": content})
        elif kind == "tool_call":
            out.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": str(item.get("call_id") or ""),
                    "type": "function",
                    "function": {
                        "name": str(item.get("name") or ""),
                        "arguments": str(item.get("arguments") or "{}"),
                    },
                }],
            })
        elif kind == "tool_result":
            out.append({
                "role": "tool",
                "tool_call_id": str(item.get("call_id") or ""),
                "content": str(item.get("output") or ""),
            })
    return out


def to_tools(tools: list[dict]) -> list[dict]:
    return [{
        "type": "function",
        "function": {
            "name": str(tool.get("name") or ""),
            "description": str(tool.get("description") or ""),
            "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
        },
    } for tool in tools or []]


async def complete(*, model: str, instructions: str, items: list[dict],
                   tools: list[dict], tool_choice: str = "auto",
                   reasoning: str, timeout: float,
                   on_delta: Callable[[str], Awaitable[None]] | None = None,
                   on_reasoning: Callable[[str], Awaitable[None]] | None = None,
                   settings: dict[str, Any] | None = None) -> dict:
    key = config.openrouter_key(settings)
    if not key:
        raise ModelError("OpenRouter API key is missing — add it in Director settings.",
                         backend="openrouter")

    payload: dict[str, Any] = {
        "model": model,
        "messages": to_messages(instructions, items),
        "stream": True,
        "reasoning": _effort(reasoning),
    }
    if tools:
        payload["tools"] = to_tools(tools)
        payload["tool_choice"] = tool_choice

    text_chunks: list[str] = []
    reasoning_chunks: list[str] = []
    # Tool calls stream in fragments keyed by index; assemble then flatten.
    pending: dict[int, dict] = {}
    usage: dict = {}

    client_timeout = aiohttp.ClientTimeout(total=None, sock_read=timeout, connect=20)
    async with aiohttp.ClientSession(timeout=client_timeout) as session:
        async with session.post(f"{API_BASE}/chat/completions", json=payload,
                                headers=_headers(key)) as resp:
            if resp.status != 200:
                detail = (await resp.text())[:600]
                try:
                    detail = json.loads(detail).get("error", {}).get("message", detail)
                except (json.JSONDecodeError, AttributeError):
                    pass
                raise ModelError(f"OpenRouter HTTP {resp.status}: {detail}",
                                 retryable=resp.status in (429, 500, 502, 503, 504),
                                 backend="openrouter")
            buffer = ""
            async for chunk in resp.content.iter_any():
                buffer += chunk.decode("utf-8", errors="replace")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if event.get("usage"):
                        raw = event["usage"]
                        usage = {
                            "requests": 1,
                            "input_tokens": int(raw.get("prompt_tokens") or 0),
                            "output_tokens": int(raw.get("completion_tokens") or 0),
                            "total_tokens": int(raw.get("total_tokens") or 0),
                        }
                    for choice in event.get("choices") or []:
                        delta = choice.get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            text_chunks.append(str(piece))
                            if on_delta:
                                await on_delta(str(piece))
                        trace = delta.get("reasoning")
                        if trace:
                            reasoning_chunks.append(str(trace))
                            if on_reasoning:
                                await on_reasoning(str(trace))
                        for call in delta.get("tool_calls") or []:
                            index = int(call.get("index") or 0)
                            slot = pending.setdefault(
                                index, {"call_id": "", "name": "", "arguments": ""})
                            if call.get("id"):
                                slot["call_id"] = str(call["id"])
                            fn = call.get("function") or {}
                            if fn.get("name"):
                                slot["name"] = str(fn["name"])
                            if fn.get("arguments"):
                                slot["arguments"] += str(fn["arguments"])

    tool_calls = []
    for slot, index in enumerate(sorted(pending)):
        call = pending[index]
        if not call.get("name"):
            continue
        # Some providers omit the id on streamed fragments; the pair only has to
        # match the tool_result we send back, so a local id is enough.
        call["call_id"] = call.get("call_id") or f"call_or_{slot}_{call['name']}"
        call["arguments"] = call.get("arguments") or "{}"
        tool_calls.append(call)

    return {
        "text": "".join(text_chunks),
        "reasoning": "".join(reasoning_chunks),
        "tool_calls": tool_calls,
        "usage": usage,
        "backend": "openrouter",
        "model": model,
    }
