"""Director's tool registry.

A tool is a schema the model sees plus an async function Director runs. Every
tool returns a `ToolResult`, which carries two different things on purpose:

    output  what the model is told. Short. Never echoes back content the model
            already produced — token waste is a bug, not a rounding error.
    card    what the phone renders: one compact line (title + preview + meta),
            the same shape the aiOS CODE transcript uses.

Tools that change the world outside this box (sending, deleting, paying,
installing) declare `destructive = True` and are routed through an approval
card before they run.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class ToolResult:
    output: str = ""
    card: dict = field(default_factory=dict)
    error: str = ""
    # Optional data-URL the model should see on the next round (screenshots).
    image: str = ""

    def as_output(self) -> str:
        return self.error or self.output or "ok"


@dataclass
class ToolContext:
    """Everything a tool is allowed to reach."""
    agent: dict
    thread_id: str
    settings: dict
    emit: Callable[[str, dict], Awaitable[None]]
    request_approval: Callable[..., Awaitable[dict]]
    ask_user: Callable[..., Awaitable[str]]
    cancel: asyncio.Event
    hub: Any = None          # server hub, for talking to machines
    depth: int = 0           # subagent nesting, to stop runaway delegation
    source: str = ""         # "group" when this turn is a group-chat reply


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    run: Callable[..., Awaitable[ToolResult]]
    destructive: bool = False
    approval_summary: Callable[[dict], str] | None = None

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description,
                "parameters": self.parameters}


_REGISTRY: dict[str, Tool] = {}
_LOADED = False


def register(tool: Tool) -> Tool:
    _REGISTRY[tool.name] = tool
    return tool


def tool(name: str, description: str, parameters: dict, *, destructive: bool = False,
         approval_summary: Callable[[dict], str] | None = None):
    """Decorator form: @tool("shell", "...", {...})"""
    def wrap(func: Callable[..., Awaitable[ToolResult]]) -> Callable[..., Awaitable[ToolResult]]:
        register(Tool(name=name, description=description, parameters=parameters,
                      run=func, destructive=destructive,
                      approval_summary=approval_summary))
        return func
    return wrap


def load_all() -> None:
    """Import every tool module so the registry is populated.

    Guarded by a flag rather than by "is the registry empty", because other
    modules import individual tool modules for their own reasons (agents.py
    wants the memory block). A non-empty registry is not proof that every tool
    is in it — and a half-loaded registry silently hands the model three tools
    instead of nineteen, which reads as the model refusing to work.
    """
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    from . import (code, communication, group, interaction, memory, operator,  # noqa: F401
                   schedule, system, web)


def get(name: str) -> Tool | None:
    load_all()
    return _REGISTRY.get(name)


def all_tools() -> list[Tool]:
    load_all()
    return list(_REGISTRY.values())


def schemas(names: list[str] | None = None) -> list[dict]:
    tools = all_tools()
    if names:
        wanted = set(names)
        tools = [t for t in tools if t.name in wanted]
    return [t.schema() for t in tools]


def missing(names: list[str]) -> list[str]:
    """Names an agent asks for that no module registers."""
    known = {tool.name for tool in all_tools()}
    return sorted(set(names or []) - known)
