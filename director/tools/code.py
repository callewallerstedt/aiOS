"""CODE dispatch.

Coding work runs where the repositories and the provider CLIs already live —
today the Windows desktop, through the aiOS CODE harness (`code_jobs.py`). The
Windows client holds an outbound WebSocket to Director, so nothing has to be
open on the internet at that end.

Director picks the machine, hands over the brief, and streams the session's
progress into this chat. Same ecosystem as the CODE tab: these are ordinary
aiOS CODE sessions, startable and readable from either side.
"""
from __future__ import annotations

import asyncio

from .. import store
from . import ToolContext, ToolResult, tool

PROVIDERS = ("codex", "claude", "cursor", "openrouter", "ollama")


def _pick_machine(hub, preferred: str = "") -> dict | None:
    machines = hub.online_machines()
    online = [m for m in machines if m.get("online")]
    if preferred:
        for machine in online:
            if machine["name"].lower() == preferred.lower():
                return machine
    for machine in online:
        if (machine.get("caps") or {}).get("code"):
            return machine
    return online[0] if online else None


@tool(
    "machines",
    "List the machines paired with Director and what each can do.",
    {"type": "object", "properties": {}},
)
async def machines(ctx: ToolContext) -> ToolResult:
    rows = ctx.hub.online_machines()
    if not rows:
        return ToolResult(
            output="No machines are paired yet. The Windows desktop connects by "
                   "running director_client.py from the aiOS repo.",
            card={"title": "machines", "preview": "none paired", "meta": "", "tone": "muted"})
    lines = []
    for machine in rows:
        caps = ", ".join(sorted(k for k, v in (machine.get("caps") or {}).items() if v)) or "none"
        lines.append(f"{machine['name']} — {machine['platform']} — "
                     f"{'online' if machine['online'] else 'offline'} — can: {caps}")
    listing = "\n".join(lines)
    online = sum(1 for m in rows if m["online"])
    return ToolResult(
        output=listing,
        card={"title": "machines", "preview": f"{online}/{len(rows)} online",
              "meta": "", "tone": "ok", "body": listing})


@tool(
    "code_session",
    "Start a CODE session on a paired machine: a real coding agent working in a "
    "real repository, with its own tests. The session runs in the background "
    "and reports back here, so dispatch it and end your turn. Brief it "
    "precisely — the repository or project, the change wanted, and how to know "
    "it worked.",
    {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "The full brief for the coding agent."},
            "project": {"type": "string",
                        "description": "Project path or name on that machine, e.g. C:\\\\aiOS."},
            "machine": {"type": "string",
                        "description": "Machine name. Defaults to the first online one that can run CODE."},
            "provider": {"type": "string", "enum": list(PROVIDERS),
                         "description": "Which coding backend. Defaults to the machine's own default."},
        },
        "required": ["task"],
    },
)
async def code_session(ctx: ToolContext, task: str = "", project: str = "",
                       machine: str = "", provider: str = "") -> ToolResult:
    brief = str(task or "").strip()
    if not brief:
        return ToolResult(error="no task given")
    target = _pick_machine(ctx.hub, machine)
    if target is None:
        return ToolResult(error="no machine is online to run CODE on — the Windows "
                                "desktop is not connected")

    job = store.create_job(
        kind="code", request={"task": brief, "project": project, "provider": provider},
        thread_id=ctx.thread_id, agent_id=ctx.agent.get("id", ""),
        machine_id=target["id"], status="running")

    async def run() -> dict:
        result = await ctx.hub.call_machine(
            target["id"], "code.start",
            {"task": brief, "project": project, "provider": provider,
             "job_id": job["id"], "thread_id": ctx.thread_id},
            timeout=90.0)
        if not result.get("ok"):
            return {"status": "fail", "summary": str(result.get("error") or "dispatch failed")}
        session_id = str(result.get("session_id") or "")
        await ctx.emit("code.started", {"job_id": job["id"], "session_id": session_id,
                                        "machine": target["name"], "task": brief})
        # The machine streams progress as events and posts the final result to
        # /api/machine/job when the session ends; wait for that.
        return await _await_completion(ctx, job["id"])

    ctx.hub.start_job(job, run)
    return ToolResult(
        output=f"CODE session dispatched to {target['name']} as job {job['id']}. It "
               "reports back here when it finishes.",
        card={"title": "code", "preview": brief[:90], "meta": target["name"],
              "tone": "accent", "job_id": job["id"]},
    )


async def _await_completion(ctx: ToolContext, job_id: str, *, timeout: float = 7200.0) -> dict:
    """Poll the job row the machine updates over the API."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(3.0)
        row = store.get_job(job_id)
        if not row:
            return {"status": "fail", "summary": "job vanished"}
        if row["status"] in ("done", "fail", "stopped"):
            result = row.get("result") or {}
            return {"status": row["status"],
                    "summary": str(result.get("summary") or "session finished")}
    return {"status": "stopped", "summary": "CODE session timed out after two hours"}


@tool(
    "code_status",
    "Check CODE sessions Director has dispatched.",
    {"type": "object", "properties": {"job_id": {"type": "string"}}},
)
async def code_status(ctx: ToolContext, job_id: str = "") -> ToolResult:
    if job_id:
        row = store.get_job(job_id)
        if not row:
            return ToolResult(error=f"no such job: {job_id}")
        result = row.get("result") or {}
        body = f"{row['kind']} job {row['id']}: {row['status']}\n{result.get('summary', '')}"
        return ToolResult(output=body,
                          card={"title": "code", "preview": row["id"], "meta": row["status"],
                                "tone": "ok" if row["status"] == "done" else "accent"})
    rows = store.list_jobs(thread_id=ctx.thread_id, limit=10)
    if not rows:
        return ToolResult(output="no jobs dispatched from this conversation yet",
                          card={"title": "code", "preview": "no jobs", "meta": "", "tone": "muted"})
    listing = "\n".join(f"{row['id']} — {row['kind']} — {row['status']}" for row in rows)
    return ToolResult(output=listing,
                      card={"title": "code", "preview": f"{len(rows)} jobs", "meta": "",
                            "tone": "ok", "body": listing})
