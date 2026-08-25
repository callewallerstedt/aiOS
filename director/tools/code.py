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
import hashlib
import time

from .. import store
from . import ToolContext, ToolResult, tool

PROVIDERS = ("codex", "claude", "cursor", "openrouter", "ollama")
DEFAULT_CONFIG_ID = "harness-balanced-engineering"
DEFAULT_CONFIG_NAME = "Balanced Engineering"
CONTINUATION_RETRY_WINDOW = 10 * 60
DIRECTOR_TERMINAL = {
    "done", "fail", "failed", "stopped", "incomplete", "completed",
    "cancelled", "interrupted",
}
CODE_METADATA_FIELDS = (
    "project", "provider", "model", "reasoning", "fast", "strategy",
    "config_id", "config_name", "native_session_id",
)


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
                  "tone": "danger", "job_id": job["id"], "job_kind": "code"})

    session_id = str(result.get("session_id") or "")
    store.update_job(job["id"], result={
        "session_id": session_id,
        "summary": "running",
        "provider": result.get("provider") or payload.get("provider") or "",
        "model": result.get("model") or payload.get("model") or "",
        "config_id": result.get("config_id") or payload.get("config_id") or "",
        "config_name": result.get("config_name") or payload.get("config_name") or "",
        "project": result.get("project") or payload.get("project") or "",
        "reasoning": result.get("reasoning") or payload.get("reasoning") or "",
        "fast": result.get("fast") if "fast" in result else payload.get("fast", False),
        "strategy": result.get("strategy") or payload.get("strategy") or "",
        "native_session_id": result.get("native_session_id") or "",
    })
    await ctx.emit("code.started", {
        "job_id": job["id"], "session_id": session_id,
        "machine": target["name"], "task": brief,
        "project": payload.get("project") or "",
        "provider": result.get("provider") or "",
        "model": result.get("model") or "",
        "config_id": result.get("config_id") or "",
        "config_name": result.get("config_name") or "",
        "job_kind": "code",
    })

    async def run() -> dict:
        # The machine streams progress/events and posts the final result; wait.
        finished = await _await_completion(job["id"])
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
              "tone": "accent", "job_id": job["id"], "session_id": session_id,
              "job_kind": "code"},
    )


async def _await_completion(job_id: str) -> dict:
    """Poll the durable job row until the machine reports a terminal state.

    CODE sessions are deliberately persistent.  A wall-clock cap here used to
    orphan healthy sessions after two hours even though their machine-side
    agent was still working, so lifetime is now controlled only by an explicit
    stop or a terminal report from the harness.
    """
    while True:
        await asyncio.sleep(3.0)
        row = store.get_job(job_id)
        if not row:
            return {"status": "fail", "summary": "job vanished"}
        if str(row["status"]).lower() in DIRECTOR_TERMINAL:
            result = dict(row.get("result") or {})
            finished = {
                "status": row["status"],
                "summary": str(result.get("summary") or "session finished"),
                "session_id": str(result.get("session_id") or ""),
            }
            for field in CODE_METADATA_FIELDS:
                if field in result:
                    finished[field] = result[field]
            for field in (
                    "parent_job_id", "continuation_of", "continuation_id",
                    "event_cursor", "code_status"):
                if field in result:
                    finished[field] = result[field]
            return finished


def _latest_code_job(thread_id: str, job_id: str = "") -> dict | None:
    """Resolve an explicit job or the newest reusable CODE session in this chat."""
    if job_id:
        row = store.get_job(job_id)
        return row if (row and row.get("kind") == "code"
                       and str(row.get("thread_id") or "") == thread_id) else None
    for row in store.list_jobs(thread_id=thread_id, limit=50):
        if (row.get("kind") == "code"
                and str((row.get("result") or {}).get("session_id") or "")
                and row.get("machine_id")):
            return row
    return None


def _metadata(row: dict, remote: dict | None = None) -> dict:
    """Carry the launch identity across every Director tracking continuation."""
    request = dict(row.get("request") or {})
    result = dict(row.get("result") or {})
    remote = dict(remote or {})
    merged = {}
    for field in CODE_METADATA_FIELDS:
        if field == "fast":
            if field in remote:
                merged[field] = bool(remote[field])
            elif field in result:
                merged[field] = bool(result[field])
            elif field in request:
                merged[field] = bool(request[field])
            continue
        value = remote.get(field)
        if value in (None, ""):
            value = result.get(field)
        if value in (None, ""):
            value = request.get(field)
        if value not in (None, ""):
            merged[field] = value
    return merged


def _director_status(code_status: str) -> str:
    status = str(code_status or "").strip().lower()
    if status in {"done", "completed", "finished", "ready"}:
        return "done"
    if status == "incomplete":
        return "incomplete"
    if status in {"stopped", "cancelled", "interrupted"}:
        return "stopped"
    if status in {"failed", "error"}:
        return "fail"
    return "running"


def _continuation_fingerprint(session_id: str, instruction: str,
                              urgent: bool) -> str:
    raw = f"{session_id}\0{1 if urgent else 0}\0{instruction}".encode(
        "utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()


def _uncertain_continuation(thread_id: str, session_id: str,
                            fingerprint: str) -> dict | None:
    """Find the durable delivery attempt that an identical retry must reuse."""
    rows_by_id: dict[str, dict] = {}
    for state in ("recovering", "incomplete", "running", "done", "fail", "stopped"):
        for row in store.list_jobs(
                thread_id=thread_id, status=state, limit=10_000):
            rows_by_id[str(row.get("id") or "")] = row
    rows = sorted(rows_by_id.values(),
                  key=lambda row: float(row.get("created_at") or 0), reverse=True)
    for row in rows:
        if row.get("kind") != "code":
            continue
        request = dict(row.get("request") or {})
        result = dict(row.get("result") or {})
        try:
            retry_until = float(result.get("continuation_retry_until") or 0)
        except (TypeError, ValueError):
            retry_until = 0
        if (str(result.get("session_id") or request.get("session_id") or "") == session_id
                and str(result.get("continuation_fingerprint")
                        or request.get("continuation_fingerprint") or "") == fingerprint
                and retry_until >= time.time()
                and str(request.get("continuation_id")
                        or result.get("continuation_id") or "")):
            return row
    return None


def _supersede_stale_tracking(prior: dict, tracking_id: str) -> None:
    """Close a stale Director row once a linked row owns the same session."""
    if (str(prior.get("id") or "") == tracking_id
            or str(prior.get("status") or "").lower() in DIRECTOR_TERMINAL):
        return
    result = dict(prior.get("result") or {})
    result.update({
        "summary": f"Superseded by linked CODE tracking job {tracking_id}.",
        "superseded_by": tracking_id,
    })
    store.update_job(prior["id"], status="incomplete", result=result)


def _latest_session_job(thread_id: str, session_id: str) -> dict | None:
    for row in store.list_jobs(thread_id=thread_id, limit=500):
        result = dict(row.get("result") or {})
        request = dict(row.get("request") or {})
        if (row.get("kind") == "code"
                and str(result.get("session_id") or request.get("session_id") or "")
                == session_id):
            return row
    return None


def _live_job_ids(hub, thread_id: str) -> set[str]:
    live_jobs = getattr(hub, "live_jobs", None)
    if not callable(live_jobs):
        return set()
    try:
        rows = live_jobs("code", thread_id=thread_id)
    except TypeError:  # compatibility with a small test/fake hub
        rows = live_jobs("code")
    return {
        str(row.get("id") or "") for row in rows
        if str(row.get("thread_id") or thread_id) == thread_id
    }


async def _send_continuation(ctx: ToolContext, machine_id: str,
                             payload: dict) -> tuple[dict, bool]:
    """Deliver once, with one idempotent retry only for an unknown outcome.

    A machine timeout can mean either "not received" or "received but its
    response was lost".  Reusing ``continuation_id`` lets the client return the
    first receipt instead of enqueueing the instruction twice.
    """
    result: dict = {}
    for attempt in range(2):
        result = await ctx.hub.call_machine(
            machine_id, "code.continue", payload, timeout=20.0)
        if result.get("ok"):
            return result, False
        uncertain = "did not answer within" in str(result.get("error") or "").lower()
        if not uncertain or attempt:
            return result, uncertain
        await asyncio.sleep(0.25)
    return result, True


@tool(
    "code_continue",
    "Continue the latest related CODE session instead of starting over. Reuses "
    "the same logical/native CODE session and its context. While it is active, "
    "ordinary instructions queue for the next turn; set urgent only for a real "
    "correction that should steer/interrupt the active turn. After it has "
    "finished, this creates one linked Director tracking job and reports the "
    "continued progress/final result back here. Use code_session only for "
    "unrelated work or when Calle explicitly asks for a fresh session.",
    {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The follow-up instruction."},
            "job_id": {"type": "string",
                       "description": "Prior Director CODE job. Defaults to the newest in this conversation."},
            "urgent": {"type": "boolean",
                       "description": "Steer/interrupt the active turn instead of queueing normally."},
        },
        "required": ["text"],
    },
)
async def code_continue(ctx: ToolContext, text: str = "", job_id: str = "",
                        urgent: bool = False) -> ToolResult:
    instruction = str(text or "").strip()
    if not instruction:
        return ToolResult(error="no continuation instruction given")
    prior = _latest_code_job(ctx.thread_id, str(job_id or "").strip())
    if prior is None:
        detail = f": {job_id}" if job_id else " in this conversation"
        return ToolResult(error=f"no reusable CODE job{detail}")
    result_before = dict(prior.get("result") or {})
    session_id = str(result_before.get("session_id") or "")
    machine_id = str(prior.get("machine_id") or "")
    if not session_id:
        return ToolResult(error=f"job {prior['id']} has no CODE session to continue")
    if not machine_id:
        return ToolResult(error=f"job {prior['id']} has no machine recorded")

    prior_terminal = str(prior.get("status") or "").lower() in DIRECTOR_TERMINAL
    prior_live = str(prior["id"]) in _live_job_ids(ctx.hub, ctx.thread_id)
    fingerprint = _continuation_fingerprint(session_id, instruction, bool(urgent))
    uncertain_tracking = _uncertain_continuation(
        ctx.thread_id, session_id, fingerprint)
    needs_tracking = (
        bool(uncertain_tracking) or prior_terminal or not prior_live
        or bool(result_before.get("delivery_uncertain"))
    )
    tracking = uncertain_tracking or prior
    root_job_id = str(
        (tracking.get("request") or {}).get("continuation_of")
        or (prior.get("request") or {}).get("continuation_of")
        or prior["id"])
    # A successful earlier continuation may have left its receipt on the
    # active tracking row.  Reuse that key only for the durable *uncertain*
    # attempt found by fingerprint; every new instruction gets a new key.
    continuation_id = str(
        ((tracking.get("request") or {}).get("continuation_id")
         or (tracking.get("result") or {}).get("continuation_id"))
        if uncertain_tracking else store.new_id("cont"))
    preserved = _metadata(prior)
    created_tracking = False
    if needs_tracking and uncertain_tracking is None:
        request = {
            **preserved,
            "task": instruction,
            "session_id": session_id,
            "parent_job_id": prior["id"],
            "continuation_of": root_job_id,
            "continuation_id": continuation_id,
            "continuation_fingerprint": fingerprint,
            "urgent": bool(urgent),
        }
        tracking = store.create_job(
            kind="code", request=request, thread_id=ctx.thread_id,
            agent_id=ctx.agent.get("id", ""), machine_id=machine_id,
            status="running")
        created_tracking = True
        _supersede_stale_tracking(prior, tracking["id"])

    delivered_cursor = max(0, int((tracking.get("result") or result_before).get(
        "event_cursor") or 0))

    payload = {
        "session_id": session_id,
        "text": instruction,
        "urgent": bool(urgent),
        "job_id": tracking["id"],
        "continuation_id": continuation_id,
        # Replacing every unique continuation follower closes the race where
        # the previous follower has already observed terminal state but has not
        # posted it yet. Idempotent retries keep the replacement already made.
        "replace_follower": True,
    }
    if not created_tracking:
        # For an active/retried tracking row this is the last offset Director
        # actually received, not the machine's local end-of-file cursor.
        payload["follow_since"] = delivered_cursor
    remote, uncertain = await _send_continuation(ctx, machine_id, payload)
    if not remote.get("ok"):
        reason = str(remote.get("error") or "the CODE session refused the continuation")
        if uncertain:
            current = dict((store.get_job(tracking["id"]) or tracking).get("result") or {})
            store.update_job(tracking["id"], status="recovering", result={
                **current, **preserved,
                "summary": (
                    "Continuation delivery outcome is unknown. It was not retried beyond "
                    "the idempotent transport retry; reattach/check this session before "
                    "sending the instruction again."
                ),
                "session_id": session_id,
                "continuation_id": continuation_id,
                "continuation_fingerprint": fingerprint,
                "delivery_uncertain": True,
                "continuation_retry_until": time.time() + CONTINUATION_RETRY_WINDOW,
                "event_cursor": delivered_cursor,
                **({"parent_job_id": prior["id"], "continuation_of": root_job_id}
                   if needs_tracking else {}),
            })

            async def recover() -> dict:
                return await _await_completion(tracking["id"])

            ctx.hub.start_job(tracking, recover)
        elif needs_tracking:
            state = "recovering" if uncertain else "fail"
            store.update_job(tracking["id"], status=state, result={
                **preserved,
                "summary": reason,
                "session_id": session_id,
                "parent_job_id": prior["id"],
                "continuation_of": root_job_id,
                "continuation_id": continuation_id,
                "continuation_fingerprint": fingerprint,
            })
        return ToolResult(
            error=(f"CODE continuation delivery is uncertain: {reason}. Check the "
                   "same session before sending it again."
                   if uncertain else f"CODE continuation did not start: {reason}"),
            card={"title": "code", "preview": instruction[:90],
                  "meta": "delivery uncertain" if uncertain else "did not continue",
                  "tone": "danger",
                  "job_id": tracking["id"], "session_id": session_id,
                  "job_kind": "code"})

    preserved = _metadata(prior, remote)
    # Build a fresh result.  Copying the previous row wholesale leaked fields
    # such as ``code_status=completed`` into a newly-running continuation.
    continuation_result = {
        **preserved,
        "session_id": session_id,
        "summary": "running",
        "event_cursor": (int(remote.get("event_cursor") or 0)
                         if created_tracking else delivered_cursor),
        "continuation_id": continuation_id,
        "continuation_fingerprint": fingerprint,
    }
    if needs_tracking:
        continuation_result.update({
            "parent_job_id": prior["id"],
            "continuation_of": root_job_id,
        })
    store.update_job(tracking["id"], status="running", result=continuation_result)

    await ctx.emit("code.continued", {
        "job_id": tracking["id"], "parent_job_id": prior["id"],
        "session_id": session_id, "machine_id": machine_id,
        "event_cursor": continuation_result["event_cursor"],
        "urgent": bool(urgent), "continuation_id": continuation_id,
        "job_kind": "code",
    })

    async def run() -> dict:
        finished = await _await_completion(tracking["id"])
        if needs_tracking:
            finished.setdefault("session_id", session_id)
            finished.setdefault("parent_job_id", prior["id"])
            finished.setdefault("continuation_of", root_job_id)
            finished.setdefault("continuation_id", continuation_id)
            finished.setdefault("event_cursor", continuation_result["event_cursor"])
            for field, value in preserved.items():
                finished.setdefault(field, value)
        return finished

    ctx.hub.start_job(tracking, run)

    action = "steered into" if remote.get("steered") else "queued in"
    if needs_tracking:
        action = "continued"
    return ToolResult(
        output=(f"CODE session {session_id} {action} job {tracking['id']}. "
                "It will keep reporting progress and its final result here."),
        card={"title": "code", "preview": instruction[:90],
              "meta": ("urgent continuation" if urgent else "continuation"),
              "tone": "accent", "job_id": tracking["id"],
              "session_id": session_id, "job_kind": "code",
              "parent_job_id": prior["id"]},
    )


@tool(
    "code_reply",
    "Answer a CODE session that is waiting on an explicit question. For new "
    "instructions or related work, use code_continue so the session keeps its "
    "context and a finished follower is reattached. A waiting session does nothing until it gets "
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
    row = store.get_job(job_id) if job_id else _latest_code_job(ctx.thread_id)
    if row is not None and str(row.get("thread_id") or "") != ctx.thread_id:
        return ToolResult(error=f"CODE job {job_id} does not belong to this conversation")
    if row is None:
        return ToolResult(
            error="no running CODE job or recoverable session in this conversation to reply to")
    if row.get("kind") != "code":
        return ToolResult(error=f"job {row['id']} is not a CODE session")
    session_id = str((row.get("result") or {}).get("session_id") or "")
    if not session_id:
        return ToolResult(error=f"job {row['id']} has no CODE session to reply to")
    if not row.get("machine_id"):
        return ToolResult(error=f"job {row['id']} has no machine recorded")
    # A reporting failure may have made the selected Director row terminal
    # while the harness is still waiting. Route the answer to the newest row
    # that owns this same current-thread session.
    latest = _latest_session_job(ctx.thread_id, session_id)
    if latest is not None:
        row = latest
    if not row.get("machine_id"):
        return ToolResult(error=f"job {row['id']} has no machine recorded")
    # The machine bridge checks pending_question immediately before delivery.
    # Keeping that check at the session owner avoids a status/answer TOCTOU
    # race and saves a round trip for every answer.
    result = await ctx.hub.call_machine(
        row["machine_id"], "code.answer",
        {"session_id": session_id, "text": answer}, timeout=45.0)
    if not result.get("ok"):
        return ToolResult(error=str(result.get("error") or "the session did not take the answer"))
    asked = str(result.get("question") or "").strip()
    current = store.get_job(row["id"]) or row
    current_result = dict(current.get("result") or {})
    current_result.update({"session_id": session_id, "summary": "running"})
    current_result.pop("pending_question", None)
    current_result.pop("delivery_uncertain", None)
    current_result.pop("code_status", None)
    current = store.update_job(row["id"], status="running", result=current_result) or current
    follow_payload = {
        "job_id": row["id"], "session_id": session_id,
        "since": max(0, int(current_result.get("event_cursor") or 0)),
        "meta": _metadata(current),
    }
    continuation_id = str(current_result.get("continuation_id") or "")
    if continuation_id:
        follow_payload["continuation_id"] = continuation_id
        follow_payload["meta"]["continuation_id"] = continuation_id
    follow = await ctx.hub.call_machine(
        row["machine_id"], "code.follow", follow_payload, timeout=20.0)
    reporter_ok = bool(follow.get("ok"))
    if reporter_ok:
        async def run() -> dict:
            return await _await_completion(row["id"])

        ctx.hub.start_job(current, run)
    else:
        current_result["summary"] = (
            "Answer delivered, but CODE reporting could not be reattached: "
            f"{follow.get('error') or 'unknown error'}."
        )
        store.update_job(row["id"], status="incomplete", result=current_result)
    return ToolResult(
        output=f"answered {row['id']}" + (f" ({asked[:120]})" if asked else "")
               + (" — it is working again"
                  if reporter_ok else " — answer delivered; reporting is paused"),
        card={"title": "code reply", "preview": answer[:90],
              "meta": row["id"], "tone": "ok" if reporter_ok else "warning",
              "job_id": row["id"],
              "session_id": session_id, "job_kind": "code"},
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
        if str(row.get("thread_id") or "") != ctx.thread_id:
            return ToolResult(error=f"CODE job {job_id} does not belong to this conversation")
        if row.get("kind") != "code":
            return ToolResult(error=f"job is not a CODE session: {job_id}")
        result = dict(row.get("result") or {})
        session_id = str(result.get("session_id") or "")
        machine_id = str(row.get("machine_id") or "")
        reporter_live = str(row["id"]) in _live_job_ids(ctx.hub, ctx.thread_id)
        remote: dict = {}
        if session_id and machine_id:
            remote = await ctx.hub.call_machine(
                machine_id, "code.status", {"session_id": session_id}, timeout=20.0)
        if remote.get("ok"):
            code_state = str(remote.get("status") or "running").lower()
            mapped = _director_status(code_state)
            merged = {
                **result, **_metadata(row, remote),
                "session_id": session_id,
                "summary": str(remote.get("summary") or result.get("summary") or code_state),
                "code_status": code_state,
            }
            question = str(remote.get("pending_question") or "").strip()
            if question:
                merged["pending_question"] = question
            else:
                merged.pop("pending_question", None)
            # A terminal tracking row remains historical even if the same
            # logical native session was later continued under a newer row.
            if str(row.get("status") or "").lower() not in DIRECTOR_TERMINAL:
                director_state = mapped
                if mapped == "running":
                    follow_payload = {
                        "job_id": row["id"], "session_id": session_id,
                        "since": max(0, int(result.get("event_cursor") or 0)),
                        "meta": _metadata(row, remote),
                    }
                    continuation_id = str(result.get("continuation_id") or "")
                    if continuation_id:
                        follow_payload["continuation_id"] = continuation_id
                        follow_payload["meta"]["continuation_id"] = continuation_id
                    # code.follow is itself idempotent and repairs the
                    # machine-side follower even when Director's waiter still
                    # exists after a client-side task failure.
                    follow = await ctx.hub.call_machine(
                        machine_id, "code.follow", follow_payload, timeout=20.0)
                    if follow.get("ok"):
                        async def run() -> dict:
                            return await _await_completion(row["id"])

                        ctx.hub.start_job(row, run)
                        reporter_live = True
                    else:
                        director_state = "incomplete"
                        merged["summary"] = (
                            "CODE is active on its machine, but reporting could not be "
                            f"reattached: {follow.get('error') or 'unknown error'}. "
                            "The session is preserved and can be continued explicitly."
                        )
                row = store.update_job(
                    row["id"], status=director_state, result=merged) or row
                result = merged
            lines = [
                f"CODE job {row['id']}: {row['status']}",
                f"Harness session {session_id}: {code_state}",
                f"Director reporter: {'attached' if reporter_live else 'not attached'}",
                str(result.get("summary") or remote.get("summary") or ""),
            ]
            if question:
                lines.append(f"Waiting for answer: {question}")
            body = "\n".join(line for line in lines if line)
        else:
            detail = str(remote.get("error") or "machine/session not available for a live check")
            body = (
                f"CODE job {row['id']}: {row['status']}\n"
                f"Director reporter: {'attached' if reporter_live else 'not attached'}\n"
                f"Stored summary: {result.get('summary', '')}\n"
                f"Live check unavailable: {detail}"
            )
        return ToolResult(output=body,
                          card={"title": "code", "preview": row["id"], "meta": row["status"],
                                "tone": "ok" if row["status"] == "done" else "accent",
                                "job_id": row["id"],
                                "session_id": session_id,
                                "job_kind": "code"})
    rows = [row for row in store.list_jobs(thread_id=ctx.thread_id, limit=50)
            if row.get("kind") == "code"][:10]
    if not rows:
        return ToolResult(output="no jobs dispatched from this conversation yet",
                          card={"title": "code", "preview": "no jobs", "meta": "",
                                "tone": "muted", "job_kind": "code"})
    live_ids = _live_job_ids(ctx.hub, ctx.thread_id)
    listing = "\n".join(
        f"{row['id']} — {row['kind']} — {row['status']} — "
        f"reporter {'attached' if row['id'] in live_ids else 'not attached'}"
        for row in rows)
    return ToolResult(output=listing,
                      card={"title": "code", "preview": f"{len(rows)} jobs", "meta": "",
                            "tone": "ok", "body": listing, "job_kind": "code"})


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
    if str(row.get("thread_id") or "") != ctx.thread_id:
        return ToolResult(error=f"CODE job {job_id} does not belong to this conversation")
    if row.get("kind") != "code":
        return ToolResult(error=f"job is not a CODE session: {job_id}")
    session_id = str((row.get("result") or {}).get("session_id") or "")
    latest = _latest_session_job(ctx.thread_id, session_id) if session_id else None
    if latest is not None:
        row = latest
        job_id = str(row["id"])
        session_id = str((row.get("result") or {}).get("session_id") or session_id)
    stored_state = str(row.get("status") or "").lower()
    if stored_state in DIRECTOR_TERMINAL:
        remote: dict = {}
        if session_id and row.get("machine_id"):
            remote = await ctx.hub.call_machine(
                row["machine_id"], "code.status", {"session_id": session_id}, timeout=20.0)
        remote_state = _director_status(str(remote.get("status") or "")) if remote.get("ok") else ""
        # An incomplete row describes Director reporting, not necessarily the
        # harness. If the harness is active (or its check is unavailable), an
        # explicit stop must still reach the real session.
        may_still_run = remote_state == "running" or (
            not remote.get("ok") and stored_state == "incomplete")
        if not may_still_run:
            return ToolResult(
                output=f"CODE job {job_id} is already {row['status']}.",
                card={"title": "code", "preview": job_id, "meta": row["status"],
                      "tone": "muted", "job_id": job_id,
                      "session_id": session_id, "job_kind": "code"})
    result = await ctx.hub.stop_job(job_id)
    if not result.get("ok"):
        return ToolResult(error=str(result.get("error") or "could not stop CODE job"))
    return ToolResult(
        output=f"Stopped CODE job {job_id} on its paired machine.",
        card={"title": "code", "preview": job_id, "meta": "stopped", "tone": "ok",
              "job_id": job_id,
              "session_id": session_id,
              "job_kind": "code"})
