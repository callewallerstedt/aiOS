"""Memory that survives the conversation.

Preferences, facts and workflows live in SQLite, not in the transcript, so a
new thread on the phone starts already knowing how things work here. The
coordinator gets the whole set injected each turn (it is small), and uses these
tools to change it.
"""
from __future__ import annotations

from .. import store
from . import ToolContext, ToolResult, tool

MAX_VALUE = 2000


@tool(
    "remember",
    "Save a durable fact, preference or workflow so future conversations know "
    "it. Use a short stable key. Overwrites the same key.",
    {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Short stable slug, e.g. 'tv-in-living-room'."},
            "value": {"type": "string", "description": "The fact, in one or two sentences."},
        },
        "required": ["key", "value"],
    },
)
async def remember(ctx: ToolContext, key: str = "", value: str = "") -> ToolResult:
    slug = str(key or "").strip()[:80]
    body = str(value or "").strip()[:MAX_VALUE]
    if not slug or not body:
        return ToolResult(error="both key and value are required")
    store.remember(slug, body)
    return ToolResult(
        output=f"remembered {slug}",
        card={"title": "remember", "preview": slug, "meta": f"{len(body)} chars",
              "tone": "ok", "body": body},
    )


@tool(
    "forget",
    "Delete a remembered fact by key.",
    {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]},
)
async def forget(ctx: ToolContext, key: str = "") -> ToolResult:
    slug = str(key or "").strip()
    if not slug:
        return ToolResult(error="no key given")
    store.forget(slug)
    return ToolResult(output=f"forgot {slug}",
                      card={"title": "forget", "preview": slug, "meta": "", "tone": "muted"})


@tool(
    "recall",
    "List everything remembered, or look up one key.",
    {"type": "object", "properties": {"key": {"type": "string"}}},
)
async def recall(ctx: ToolContext, key: str = "") -> ToolResult:
    slug = str(key or "").strip()
    if slug:
        value = store.recall(slug)
        return ToolResult(
            output=value or f"nothing remembered for {slug}",
            card={"title": "recall", "preview": slug, "meta": "", "tone": "ok"})
    rows = store.list_memory(limit=200)
    listing = "\n".join(f"{row['key']}: {row['value']}" for row in rows) or "(nothing remembered yet)"
    return ToolResult(
        output=listing,
        card={"title": "recall", "preview": "all memory", "meta": f"{len(rows)} entries",
              "tone": "ok", "body": listing})


def memory_block(limit: int = 60) -> str:
    """Rendered for the system prompt each turn.

    Only what agents write. The operator keeps its own notes in its own scope —
    where a button lives on one screen is worth a lot to the next operator run
    and nothing to a chat, and this block is paid for on every turn of every
    conversation.
    """
    rows = store.list_memory(scope="global", limit=limit)
    if not rows:
        return ""
    lines = "\n".join(f"- {row['key']}: {row['value']}" for row in rows)
    return f"What you already know about Calle and this setup:\n{lines}"
