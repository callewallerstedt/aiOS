"""Model backends for Director.

Two backends, one interface:

    codex       ChatGPT OAuth tokens -> the Codex responses endpoint. This is
                the house default (gpt-5.6-luna) and costs no per-token money.
    openrouter  API key -> any catalogue model, used when a task wants a
                different brain than the subscription provides.

Both speak the same normalized item list so the agent loop, the store and the
phone never learn which backend answered:

    {"type": "message",  "role": "user"|"assistant", "content": [...parts]}
    {"type": "tool_call", "call_id": str, "name": str, "arguments": str}
    {"type": "tool_result", "call_id": str, "output": str}

Content parts are {"type": "text", "text": ...} and
{"type": "image", "url": "data:image/..."}.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from .. import config

Emit = Callable[[str, dict], Awaitable[None]]

BACKENDS = ("codex", "openrouter")


def text_part(text: str) -> dict:
    return {"type": "text", "text": text}


def image_part(url: str) -> dict:
    return {"type": "image", "url": url}


def user_message(content: str | list[dict]) -> dict:
    parts = [text_part(content)] if isinstance(content, str) else list(content)
    return {"type": "message", "role": "user", "content": parts}


def assistant_message(content: str | list[dict]) -> dict:
    parts = [text_part(content)] if isinstance(content, str) else list(content)
    return {"type": "message", "role": "assistant", "content": parts}


def tool_call(call_id: str, name: str, arguments: str) -> dict:
    return {"type": "tool_call", "call_id": call_id, "name": name, "arguments": arguments}


def tool_result(call_id: str, output: str) -> dict:
    return {"type": "tool_result", "call_id": call_id, "output": output}


class ModelError(RuntimeError):
    """A backend failed in a way worth showing the user verbatim."""

    def __init__(self, message: str, *, retryable: bool = False, backend: str = ""):
        super().__init__(message)
        self.retryable = retryable
        self.backend = backend


async def complete(*, backend: str = "", model: str = "", instructions: str = "",
                   items: list[dict], tools: list[dict] | None = None,
                   tool_choice: str = "auto",
                   reasoning: str = "", timeout: float = 180.0,
                   on_delta: Callable[[str], Awaitable[None]] | None = None,
                   on_reasoning: Callable[[str], Awaitable[None]] | None = None,
                   settings: dict[str, Any] | None = None) -> dict:
    """Run one model turn and return {text, items, tool_calls, usage, backend, model}.

    `items` is the normalized history. `tools` is a list of
    {"name", "description", "parameters"} — each backend renders its own shape.
    """
    cfg = settings if settings is not None else config.load_settings()
    defaults = cfg.get("defaults", {}) or {}
    backend = (backend or defaults.get("backend") or config.DEFAULT_BACKEND).strip()
    model = (model or defaults.get("model") or config.DEFAULT_MODEL).strip()
    reasoning = (reasoning or defaults.get("reasoning") or config.DEFAULT_REASONING).strip()

    if backend == "codex":
        from . import codex
        return await codex.complete(
            model=model, instructions=instructions, items=items, tools=tools or [],
            tool_choice=tool_choice,
            reasoning=reasoning, timeout=timeout, on_delta=on_delta,
            on_reasoning=on_reasoning, settings=cfg)
    if backend == "openrouter":
        from . import openrouter
        return await openrouter.complete(
            model=model, instructions=instructions, items=items, tools=tools or [],
            tool_choice=tool_choice,
            reasoning=reasoning, timeout=timeout, on_delta=on_delta,
            on_reasoning=on_reasoning, settings=cfg)
    raise ModelError(f"unknown backend: {backend}", backend=backend)


async def backend_status(backend: str, *, settings: dict[str, Any] | None = None) -> tuple[bool, str]:
    cfg = settings if settings is not None else config.load_settings()
    if backend == "codex":
        from . import codex
        return codex.status(settings=cfg)
    if backend == "openrouter":
        from . import openrouter
        return openrouter.status(settings=cfg)
    return False, f"unknown backend: {backend}"
