"""Letting Director put things in its own calendar.

"Remind me Friday at five", "check the deploy every ten minutes", "every
morning tell me what's on" — all the same mechanism: a prompt Director will
send itself later, in this conversation.
"""
from __future__ import annotations

from .. import routines as routines_mod
from .. import store
from . import ToolContext, ToolResult, tool

SCHEDULE_SCHEMA = {
    "type": "object",
    "description": "When to run. One of: "
                   "{\"kind\":\"daily\",\"time\":\"08:00\"}, "
                   "{\"kind\":\"weekdays\",\"time\":\"07:30\"}, "
                   "{\"kind\":\"weekly\",\"time\":\"17:00\",\"weekday\":4} (0=Monday), "
                   "{\"kind\":\"interval\",\"seconds\":600}, "
                   "{\"kind\":\"once\",\"in_seconds\":1800} or "
                   "{\"kind\":\"once\",\"at\":<unix timestamp>}.",
    "properties": {
        "kind": {"type": "string", "enum": list(routines_mod.KINDS)},
        "time": {"type": "string", "description": "HH:MM, local time on the box."},
        "weekday": {"type": "integer", "description": "0=Monday … 6=Sunday."},
        "seconds": {"type": "number"},
        "in_seconds": {"type": "number"},
        "at": {"type": "number"},
    },
    "required": ["kind"],
}


@tool(
    "schedule",
    "Schedule something for later: a reminder, a recurring check, a morning "
    "digest. When it comes due you receive `prompt` as a message in this "
    "conversation and act on it with every tool you normally have. Use this "
    "whenever Calle says 'remind me', 'every day', 'every Friday', 'in an "
    "hour', or asks you to keep an eye on something.",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Short label, e.g. 'morning digest'."},
            "prompt": {
                "type": "string",
                "description": "What you will be told to do when it fires. Write it as "
                               "an instruction to yourself, with everything you will "
                               "need — future you has only this line and your memory.",
            },
            "schedule": SCHEDULE_SCHEMA,
        },
        "required": ["name", "prompt", "schedule"],
    },
)
async def schedule(ctx: ToolContext, name: str = "", prompt: str = "",
                   schedule: dict | None = None) -> ToolResult:
    label = str(name or "").strip()[:80]
    body = str(prompt or "").strip()
    if not label or not body:
        return ToolResult(error="both a name and a prompt are required")
    try:
        canonical = routines_mod.normalize(schedule or {})
        when = routines_mod.next_run(canonical)
    except routines_mod.ScheduleError as exc:
        return ToolResult(error=str(exc))
    if when <= 0:
        return ToolResult(error="that schedule never fires")

    row = store.create_routine(agent_id=ctx.agent.get("id", ""), name=label,
                               prompt=body, schedule=canonical, next_run=when)
    described = routines_mod.describe(canonical)
    await ctx.emit("routine.created", {"id": row["id"], "name": label,
                                       "schedule": described,
                                       "next_run": when})
    return ToolResult(
        output=f"scheduled '{label}' {described}; first run {routines_mod.humanize_next(when)}",
        card={"title": "scheduled", "preview": f"{label} — {described}",
              "meta": routines_mod.humanize_next(when), "tone": "accent",
              "body": body},
    )


@tool(
    "list_schedules",
    "List what you have scheduled in this conversation.",
    {"type": "object", "properties": {
        "all_agents": {"type": "boolean", "description": "Include other agents' routines."}}},
)
async def list_schedules(ctx: ToolContext, all_agents: bool = False) -> ToolResult:
    rows = store.list_routines(agent_id="" if all_agents else ctx.agent.get("id", ""))
    if not rows:
        return ToolResult(output="nothing is scheduled",
                          card={"title": "schedules", "preview": "none", "meta": "",
                                "tone": "muted"})
    lines = []
    for row in rows:
        state = "" if row["enabled"] else " (paused)"
        lines.append(f"{row['id']}  {row['name']} — {routines_mod.describe(row['schedule'])}"
                     f" — next {routines_mod.humanize_next(row['next_run'])}{state}")
    listing = "\n".join(lines)
    return ToolResult(
        output=listing,
        card={"title": "schedules", "preview": f"{len(rows)} scheduled", "meta": "",
              "tone": "ok", "body": listing})


@tool(
    "cancel_schedule",
    "Cancel or pause something you scheduled. Use list_schedules first to get the id.",
    {
        "type": "object",
        "properties": {
            "routine_id": {"type": "string"},
            "pause_only": {"type": "boolean",
                           "description": "Pause instead of deleting it."},
        },
        "required": ["routine_id"],
    },
)
async def cancel_schedule(ctx: ToolContext, routine_id: str = "",
                          pause_only: bool = False) -> ToolResult:
    row = store.get_routine(str(routine_id or "").strip())
    if not row:
        return ToolResult(error=f"no routine with id {routine_id}")
    if pause_only:
        store.update_routine(row["id"], {"enabled": False})
        verb = "paused"
    else:
        store.delete_routine(row["id"])
        verb = "cancelled"
    await ctx.emit("routine.changed", {"id": row["id"], "state": verb})
    return ToolResult(
        output=f"{verb} '{row['name']}'",
        card={"title": verb, "preview": row["name"], "meta": "", "tone": "muted"})
