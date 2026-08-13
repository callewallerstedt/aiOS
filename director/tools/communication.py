"""Agent directory, cross-chat search, and visible inter-agent messages."""
from __future__ import annotations

from .. import store
from . import ToolContext, ToolResult, tool


@tool(
    "list_agents",
    "List the agents and group chats you can message, including their exact "
    "names, ids, kind, current status and latest preview. Use this before "
    "message_agent when the destination name is not exact.",
    {"type": "object", "properties": {}},
)
async def list_agents(ctx: ToolContext) -> ToolResult:
    rows = []
    for agent in store.list_agents():
        thread = store.latest_thread(agent["id"])
        rows.append(
            f"{agent['name']} | {agent['id']} | {agent['kind']} | "
            f"{(thread or {}).get('status') or 'idle'} | "
            f"{str((thread or {}).get('preview') or '')[:100]}"
        )
    listing = "\n".join(rows) or "(no agents)"
    return ToolResult(
        output=listing,
        card={"title": "agents", "preview": f"{len(rows)} available",
              "meta": "", "tone": "ok", "body": listing},
    )


@tool(
    "message_agent",
    "Send a visible internal message to another agent or group chat. The "
    "destination sees who sent it and runs a normal turn, so it can answer, "
    "use tools, or coordinate further. Use the recipient's exact name or id. "
    "Do not use this to talk to the current chat itself.",
    {
        "type": "object",
        "properties": {
            "recipient": {
                "type": "string",
                "description": "Exact agent/group name or id from list_agents.",
            },
            "message": {
                "type": "string",
                "description": "The message to deliver, including necessary context.",
            },
        },
        "required": ["recipient", "message"],
    },
)
async def message_agent(ctx: ToolContext, recipient: str = "",
                        message: str = "") -> ToolResult:
    if ctx.hub is None or not hasattr(ctx.hub, "relay_agent_message"):
        return ToolResult(error="agent messaging is unavailable")
    return await ctx.hub.relay_agent_message(ctx, recipient, message)


@tool(
    "search_chats",
    "Search messages across all agent and group chats. Use this when Calle "
    "refers to an earlier conversation but does not remember which agent had it.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "description": "1-30 results; default 12."},
        },
        "required": ["query"],
    },
)
async def search_chats(ctx: ToolContext, query: str = "", limit: int = 12) -> ToolResult:
    needle = str(query or "").strip()
    if not needle:
        return ToolResult(error="give me something to search for")
    rows = store.search_messages(needle, limit=max(1, min(int(limit or 12), 30)))
    lines = []
    for row in rows:
        speaker = "Calle" if row["role"] == "user" else row["agent_name"]
        lines.append(
            f"[{row['agent_name']} / {row['agent_kind']}] {speaker}: "
            f"{str(row.get('content') or '').replace(chr(10), ' ')[:300]}"
        )
    listing = "\n".join(lines) or "(no matching chat messages)"
    return ToolResult(
        output=listing,
        card={"title": "chat search", "preview": needle[:80],
              "meta": f"{len(rows)} matches", "tone": "ok", "body": listing},
    )
