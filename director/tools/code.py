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
DEFAULT_CONFIG_ID = "harness-balanced-engineering"
DEFAULT_CONFIG_NAME = "Balanced Engineering"


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
    "code_configs",
    "List the saved CODE model configurations on a paired machine "
    "(provider, strategy, role models). Use this before starting a session "
    f"when Calle has not already named a config. Default recommendation: "
    f"{DEFAULT_CONFIG_NAME} (`{DEFAULT_CONFIG_ID}`).",
    {
        "type": "object",
        "properties": {
            "machine": {"type": "string",
                        "description": "Machine name. Defaults to the first online CODE machine."},
        },
    },
)
async def code_configs(ctx: ToolContext, machine: str = "") -> ToolResult:
    target = _pick_machine(ctx.hub, machine)
    if target is None:
        return ToolResult(error="no machine is online to list CODE configs on")
    result = await ctx.hub.call_machine(target["id"], "code.configs", {}, timeout=30.0)
    if not result.get("ok"):
        return ToolResult(error=str(result.get("error") or "could not list CODE configs"))
    rows = result.get("configs") or []
    if not rows:
        return ToolResult(
            output=f"No saved configs on {target['name']}. You can still pass "
                   "provider/model/reasoning/fast explicitly to code_session. "
                   f"Otherwise recommend {DEFAULT_CONFIG_NAME}.",
            card={"title": "configs", "preview": "none saved", "meta": target["name"],
                  "tone": "muted"})
    lines = []
    for row in rows:
        roles = row.get("roles") if isinstance(row.get("roles"), dict) else {}
        coder = roles.get("coder") if isinstance(roles.get("coder"), dict) else {}
        mark = " ← default" if str(row.get("id") or "") == DEFAULT_CONFIG_ID else ""
        lines.append(
            f"{row.get('name')} ({row.get('id')}) — provider {row.get('provider')}, "
            f"strategy {row.get('strategy') or 'auto'}, "
            f"coder {coder.get('model') or '?'} "
            f"({coder.get('reasoning') or '?'}"
            f"{', fast' if coder.get('fast') else ''}){mark}")
    listing = "\n".join(lines)
    return ToolResult(
        output=listing,
        card={"title": "configs", "preview": f"{len(rows)} on {target['name']}",
              "meta": DEFAULT_CONFIG_NAME, "tone": "ok", "body": listing})


@tool(
    "code_session",
    "Start a CODE session on a paired machine: a real coding agent working in a "
    "real repository, with its own tests. The session runs in the background "
    "and reports back here, so dispatch it and end your turn. Brief it "
    "precisely — the repository or project, the change wanted, and how to know "
    "it worked. Pass a saved config_id (preferred) or explicit "
    "provider/model/reasoning/fast. If Calle has not chosen yet, ask first and "
    f"recommend {DEFAULT_CONFIG_NAME}. When nothing is specified the machine "
    f"defaults to {DEFAULT_CONFIG_NAME}.",
    {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "The full brief for the coding agent."},
            "project": {"type": "string",
                        "description": "Project path or name on that machine, e.g. C:\\\\aiOS."},
            "machine": {"type": "string",
                        "description": "Machine name. Defaults to the first online one that can run CODE."},
            "config_id": {"type": "string",
                          "description": f"Saved CODE configuration id. Default on the machine: "
                                         f"{DEFAULT_CONFIG_ID} ({DEFAULT_CONFIG_NAME})."},
            "config_name": {"type": "string",
                            "description": "Saved CODE configuration name (resolved on the machine)."},
            "provider": {"type": "string", "enum": list(PROVIDERS),
                         "description": "Coding backend when not using a saved config."},
            "model": {"type": "string",
                      "description": "Exact model id when not using a saved config."},
            "reasoning": {"type": "string",
                          "description": "Reasoning / intelligence level (e.g. low, medium, high)."},
            "fast": {"type": "boolean",
                     "description": "Fast mode for the coder when not using a saved config."},
            "strategy": {"type": "string",
                         "description": "Task strategy override (auto / planned / distributed)."},
        },
        "required": ["task"],
    },
)
async def code_session(ctx: ToolContext, task: str = "", project: str = "",
                       machine: str = "", config_id: str = "", config_name: str = "",
                       provider: str = "", model: str = "", reasoning: str = "",
                       fast: bool | None = None, strategy: str = "") -> ToolResult:
    brief = str(task or "").strip()
    if not brief:
        return ToolResult(error="no task given")
    target = _pick_machine(ctx.hub, machine)
    if target is None:
        return ToolResult(error="no machine is online to run CODE on — the Windows "
                                "desktop is not connected")

    # Resolve the project to a real directory first. A name that turns out not
    # to exist used to fail in the background, minutes after this tool had
    # already reported "dispatched" — so the model told Calle work was running
    # that had never started.
    if project:
        resolved = await ctx.hub.call_machine(
            target["id"], "resolve_project", {"project": project}, timeout=90.0)
        if not resolved.get("ok"):
            hint = str(resolved.get("error") or f"no directory matching {project!r}")
            return ToolResult(error=f"{hint} on {target['name']} — nothing was "
                                    f"dispatched. Find the real path with "
                                    f"`machine_find` or `machine_dirs` and try again.")
        found = str(resolved.get("path") or "")
        others = [c["path"] for c in (resolved.get("candidates") or [])
                  if c.get("path") and c["path"] != found][:5]
        if found and found.lower() != str(project).lower() and others:
            # Several plausible folders: say which one is being used rather
            # than picking silently.
            await ctx.emit("code.project", {"path": found, "candidates": others})
        project = found or project

    payload = {
        "task": brief,
        "project": project,
        "config_id": str(config_id or "").strip(),
        "config_name": str(config_name or "").strip(),
        "provider": str(provider or "").strip().lower(),
        "model": str(model or "").strip(),
        "reasoning": str(reasoning or "").strip().lower(),
        "strategy": str(strategy or "").strip().lower(),
        "thread_id": ctx.thread_id,
    }
    if fast is not None:
        payload["fast"] = bool(fast)

    job = store.create_job(
        kind="code",
        request={k: v for k, v in payload.items() if k != "thread_id" and v != ""},
        thread_id=ctx.thread_id, agent_id=ctx.agent.get("id", ""),
        machine_id=target["id"], status="running")
    payload["job_id"] = job["id"]

    # Start it here, inside the turn, so a refusal is this tool's error.
    result = await ctx.hub.call_machine(target["id"], "code.start", payload, timeout=90.0)
    if not result.get("ok"):
        reason = str(result.get("error") or "dispatch failed")
        store.update_job(job["id"], status="fail",
                         result={"summary": reason, "session_id": ""})
        return ToolResult(
            error=f"CODE did not start on {target['name']}: {reason}. Nothing is "
                  f"running — do not tell Calle this job was dispatched.",
            card={"title": "code", "preview": brief[:90], "meta": "did not start",
                  "tone": "danger", "job_id": job["id"]})

    session_id = str(result.get("session_id") or "")
    store.update_job(job["id"], result={
        "session_id": session_id,
        "summary": "running",
        "provider": result.get("provider") or payload.get("provider") or "",
        "model": result.get("model") or payload.get("model") or "",
        "config_id": result.get("config_id") or payload.get("config_id") or "",
        "config_name": result.get("config_name") or payload.get("config_name") or "",
    })
    await ctx.emit("code.started", {
        "job_id": job["id"], "session_id": session_id,
        "machine": target["name"], "task": brief,
        "project": payload.get("project") or "",
        "provider": result.get("provider") or "",
        "model": result.get("model") or "",
        "config_id": result.get("config_id") or "",
        "config_name": result.get("config_name") or "",
    })

    async def run() -> dict:
        # The machine streams progress/events and posts the final result; wait.
        finished = await _await_completion(ctx, job["id"])
        finished.setdefault("session_id", session_id)
        finished.setdefault("config_id", result.get("config_id") or "")
        finished.setdefault("config_name", result.get("config_name") or "")
        finished.setdefault("provider", result.get("provider") or "")
        finished.setdefault("model", result.get("model") or "")
        return finished

    ctx.hub.start_job(job, run)
    label = (str(config_name or "").strip()
             or str(config_id or "").strip()
             or str(provider or "").strip()
             or DEFAULT_CONFIG_NAME)
    where = str(payload.get("project") or "").strip()
    return ToolResult(
        output=f"CODE session running on {target['name']} as job {job['id']} "
               f"({label}{', ' + where if where else ''}). It reports back here "
               f"when it finishes, and asks here if it needs a decision.",
        card={"title": "code", "preview": brief[:90],
              "meta": f"{target['name']} · {label}",
              "tone": "accent", "job_id": job["id"], "session_id": session_id},
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
            result = dict(row.get("result") or {})
            return {
                "status": row["status"],
                "summary": str(result.get("summary") or "session finished"),
                "session_id": str(result.get("session_id") or ""),
                "config_id": str(result.get("config_id") or ""),
                "config_name": str(result.get("config_name") or ""),
                "provider": str(result.get("provider") or ""),
                "model": str(result.get("model") or ""),
            }
    return {"status": "stopped", "summary": "CODE session timed out after two hours"}


@tool(
    "code_reply",
    "Answer a CODE session that is waiting on a question, or send it extra "
    "instructions while it runs. A waiting session does nothing until it gets "
    "an answer, so never leave one hanging: answer it yourself when the answer "
    "follows from what Calle already asked for, otherwise ask him with "
    "`ask_user` / `ask_yes_no` and pass his answer straight through.",
    {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "The CODE job to reply to."},
            "text": {"type": "string", "description": "The answer, in plain words."},
        },
        "required": ["text"],
    },
)
async def code_reply(ctx: ToolContext, job_id: str = "", text: str = "") -> ToolResult:
    answer = str(text or "").strip()
    if not answer:
        return ToolResult(error="no answer given")
    row = store.get_job(job_id) if job_id else None
    if row is None:
        # Falling back to the newest live job in this chat keeps the model from
        # having to carry a job id it may never have been shown.
        live = [j for j in store.list_jobs(thread_id=ctx.thread_id, limit=10)
                if j["kind"] == "code" and j["status"] == "running"]
        row = live[0] if live else None
    if row is None:
        return ToolResult(error="no running CODE job in this conversation to reply to")
    session_id = str((row.get("result") or {}).get("session_id") or "")
    if not session_id:
        return ToolResult(error=f"job {row['id']} has no CODE session to reply to")
    if not row.get("machine_id"):
        return ToolResult(error=f"job {row['id']} has no machine recorded")
    result = await ctx.hub.call_machine(
        row["machine_id"], "code.answer",
        {"session_id": session_id, "text": answer}, timeout=45.0)
    if not result.get("ok"):
        return ToolResult(error=str(result.get("error") or "the session did not take the answer"))
    asked = str(result.get("question") or "").strip()
    return ToolResult(
        output=f"answered {row['id']}" + (f" ({asked[:120]})" if asked else "")
               + " — it is working again",
        card={"title": "code reply", "preview": answer[:90],
              "meta": row["id"], "tone": "ok", "job_id": row["id"]},
    )


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
                                "tone": "ok" if row["status"] == "done" else "accent",
                                "job_id": row["id"],
                                "session_id": str(result.get("session_id") or "")})
    rows = store.list_jobs(thread_id=ctx.thread_id, limit=10)
    if not rows:
        return ToolResult(output="no jobs dispatched from this conversation yet",
                          card={"title": "code", "preview": "no jobs", "meta": "", "tone": "muted"})
    listing = "\n".join(f"{row['id']} — {row['kind']} — {row['status']}" for row in rows)
    return ToolResult(output=listing,
                      card={"title": "code", "preview": f"{len(rows)} jobs", "meta": "",
                            "tone": "ok", "body": listing})


@tool(
    "code_stop",
    "Stop a running CODE job and its real session on the paired machine. Use this when "
    "Calle asks to stop, cancel, or abort a CODE session; do not merely poll code_status.",
    {
        "type": "object",
        "properties": {"job_id": {"type": "string", "description": "Director CODE job id."}},
        "required": ["job_id"],
    },
)
async def code_stop(ctx: ToolContext, job_id: str = "") -> ToolResult:
    job_id = str(job_id or "").strip()
    if not job_id:
        return ToolResult(error="job_id is required")
    row = store.get_job(job_id)
    if not row:
        return ToolResult(error=f"no such job: {job_id}")
    if row.get("kind") != "code":
        return ToolResult(error=f"job is not a CODE session: {job_id}")
    if row.get("status") in ("done", "fail", "stopped"):
        return ToolResult(
            output=f"CODE job {job_id} is already {row['status']}.",
            card={"title": "code", "preview": job_id, "meta": row["status"], "tone": "muted"})
    result = await ctx.hub.stop_job(job_id)
    if not result.get("ok"):
        return ToolResult(error=str(result.get("error") or "could not stop CODE job"))
    return ToolResult(
        output=f"Stopped CODE job {job_id} on its paired machine.",
        card={"title": "code", "preview": job_id, "meta": "stopped", "tone": "ok",
              "job_id": job_id})
