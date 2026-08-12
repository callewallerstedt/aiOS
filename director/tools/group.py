"""Group-chat tools.

A group thread is talk only. Real work (CODE, the operator, a long shell job)
leaves the room and runs in that agent's private chat with Calle, the same way
a person would start a DM after saying "I'll take this" in Slack.
"""
from __future__ import annotations

from . import ToolContext, ToolResult, tool


@tool(
    "start_work",
    "Take a task into your private chat with Calle and do it there. Use this "
    "when the group asked you to actually do something — code, the operator, "
    "files, a long job. The group only sees that you are working; the work "
    "itself happens in your private thread and does not interrupt anything "
    "already running there.",
    {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "The brief to carry into your private chat, "
                               "written as if Calle asked you directly.",
            },
        },
        "required": ["task"],
    },
)
async def start_work(ctx: ToolContext, task: str = "") -> ToolResult:
    brief = str(task or "").strip()
    if not brief:
        return ToolResult(error="no task given")
    hub = ctx.hub
    if hub is None or not hasattr(hub, "start_private_work"):
        return ToolResult(error="cannot start private work from here")
    return await hub.start_private_work(ctx, brief)


REACT_EMOJIS = ("👍", "👎", "❤️", "✅", "👀", "😂", "🎉")
REACT_ALIASES = {
    "heart": "❤️", "love": "❤️", "❤": "❤️", "♥️": "❤️", "❤️": "❤️",
    "+1": "👍", "thumbsup": "👍", "thumbs_up": "👍", "thumbs up": "👍",
    "-1": "👎", "thumbsdown": "👎",
    "check": "✅", "ok": "👍", "yes": "👍",
    "eyes": "👀", "lol": "😂", "tada": "🎉", "party": "🎉",
}


def normalize_react(emoji: str) -> str:
    raw = str(emoji or "").strip()
    if raw in REACT_EMOJIS:
        return raw
    key = raw.lower().replace(" ", "").replace("_", "")
    return REACT_ALIASES.get(raw) or REACT_ALIASES.get(key) or "👍"


@tool(
    "react",
    "React with an emoji on Calle's latest message. Use this instead of "
    "saying ok / got it / I did. If Calle says heart or love, use ❤️. "
    "Allowed: 👍 👎 ❤️ ✅ 👀 😂 🎉. Write no other text when you react.",
    {
        "type": "object",
        "properties": {
            "emoji": {
                "type": "string",
                "description": "One of 👍 👎 ❤️ ✅ 👀 😂 🎉. "
                               "heart/love → ❤️. Default 👍.",
            },
        },
    },
)
async def react(ctx: ToolContext, emoji: str = "👍") -> ToolResult:
    hub = ctx.hub
    if hub is None or not hasattr(hub, "post_reaction"):
        return ToolResult(error="cannot react from here")
    return await hub.post_reaction(ctx, emoji)
