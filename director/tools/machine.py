"""Looking around a paired machine's filesystem.

Director dispatches coding work to the Windows desktop by naming a project
directory, and before this existed it had no way to learn what those
directories are called. It guessed: `aiOS Director`, then `aiOS`, and two CODE
jobs died on "no such project directory" before Calle had to type `C:\\aiOS`
himself. Guessing a path is not a judgement call the model should be making
when the machine can simply be asked.

Read-only on purpose. Writing and running things over there is what a CODE
session is for.
"""
from __future__ import annotations

from . import ToolContext, ToolResult, tool

MAX_ROWS = 60


def _pick(hub, preferred: str = "") -> dict | None:
    online = [m for m in hub.online_machines() if m.get("online")]
    if preferred:
        for machine in online:
            if machine["name"].lower() == str(preferred).lower():
                return machine
    for machine in online:
        if (machine.get("caps") or {}).get("files"):
            return machine
    return online[0] if online else None


@tool(
    "machine_dirs",
    "List a directory on a paired machine — the Windows desktop where the "
    "repositories live. Leave `path` empty to see the drive roots and home. "
    "Use this to find a real project path before starting a CODE session "
    "instead of guessing one.",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "Absolute path, e.g. C:\\\\aiOS. Empty lists the roots."},
            "machine": {"type": "string", "description": "Machine name. Defaults to the online one."},
        },
    },
)
async def machine_dirs(ctx: ToolContext, path: str = "", machine: str = "") -> ToolResult:
    target = _pick(ctx.hub, machine)
    if target is None:
        return ToolResult(error="no machine is online to look at")
    result = await ctx.hub.call_machine(
        target["id"], "list_dir", {"path": str(path or "")}, timeout=45.0)
    if not result.get("ok"):
        return ToolResult(error=str(result.get("error") or "could not list that directory"))
    rows = result.get("entries") or []
    lines = []
    for row in rows[:MAX_ROWS]:
        mark = "d" if row.get("dir") else "-"
        flag = " ← project" if row.get("project") else ""
        lines.append(f"{mark} {row.get('name')}{flag}")
    if len(rows) > MAX_ROWS:
        lines.append(f"… {len(rows) - MAX_ROWS} more")
    where = str(result.get("path") or path or "roots")
    listing = "\n".join(lines) or "(empty)"
    return ToolResult(
        output=f"{where} on {target['name']}:\n{listing}",
        card={"title": "ls", "preview": where, "meta": f"{len(rows)} on {target['name']}",
              "tone": "ok", "body": listing},
    )


@tool(
    "machine_find",
    "Search a paired machine for a directory by name and get its absolute "
    "path. Use this the moment Calle names a project, repo or folder you do "
    "not already have an exact path for — do not guess the path and do not "
    "ask him for it before looking.",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string",
                     "description": "Folder name or part of it, e.g. aiOS."},
            "machine": {"type": "string", "description": "Machine name. Defaults to the online one."},
            "roots": {"type": "array", "items": {"type": "string"},
                      "description": "Where to start. Defaults to the repo, home and the drives."},
        },
        "required": ["name"],
    },
)
async def machine_find(ctx: ToolContext, name: str = "", machine: str = "",
                       roots=None) -> ToolResult:
    needle = str(name or "").strip()
    if not needle:
        return ToolResult(error="no name to search for")
    target = _pick(ctx.hub, machine)
    if target is None:
        return ToolResult(error="no machine is online to search")
    result = await ctx.hub.call_machine(
        target["id"], "find_paths",
        {"name": needle, "roots": [str(r) for r in (roots or [])]}, timeout=90.0)
    if not result.get("ok"):
        return ToolResult(error=str(result.get("error") or "search failed"))
    hits = result.get("matches") or []
    if not hits:
        return ToolResult(
            output=f"nothing matching {needle!r} on {target['name']}",
            card={"title": "find", "preview": needle, "meta": "no match", "tone": "muted"})
    lines = [f"{hit['path']}{' (project)' if hit.get('project') else ''}"
             for hit in hits[:MAX_ROWS]]
    listing = "\n".join(lines)
    return ToolResult(
        output=listing,
        card={"title": "find", "preview": needle,
              "meta": f"{len(hits)} on {target['name']}", "tone": "ok", "body": listing},
    )
