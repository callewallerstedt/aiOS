"""The Director HTTP + WebSocket server.

Binds loopback only. Tailscale Funnel publishes it at
https://rocky-server.tail4d08fd.ts.net/director, which is how the phone reaches
it from anywhere without opening a port on the house router.

Three kinds of client connect here:

  the phone PWA        REST for state, one WebSocket for the live event stream
  the Windows desktop  one outbound WebSocket it dials in on, so nothing has to
                       listen on that machine
  noVNC                served from this same origin, with the RFB stream
                       bridged over a WebSocket, so screen takeover needs no
                       second public port and no second credential
"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import pathlib
import time
from typing import Any

import aiohttp
from aiohttp import web

from . import agents as agents_mod
from . import auth, config, models, push, store, wake
from . import routines as routines_mod
from . import runtime as runtime_mod
from .operator import display as display_mod
from .operator import x11

ROUTES = web.RouteTableDef()
PUBLIC_PATHS = {"/api/health", "/api/pair"}

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Director-Token",
    "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
    "Access-Control-Max-Age": "86400",
}


def json_response(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, headers=CORS_HEADERS)


def error(message: str, status: int = 400) -> web.Response:
    return json_response({"ok": False, "error": message}, status=status)


def strip_funnel_prefix(path: str) -> str:
    """Tailscale Funnel publishes us at /director; the app itself lives at /."""
    if path == "/director":
        return "/"
    if path.startswith("/director/"):
        return path[len("/director"):]
    return path


@web.middleware
async def funnel_prefix(request: web.Request, handler):
    stripped = strip_funnel_prefix(request.path)
    if stripped != request.path:
        rel = stripped
        if request.query_string:
            rel = f"{stripped}?{request.query_string}"
        request = request.clone(rel_url=rel)
    return await handler(request)


# ---------------- middleware ----------------

@web.middleware
async def cors_and_auth(request: web.Request, handler):
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=CORS_HEADERS)

    path = request.path
    needs_auth = path.startswith("/api/") and path not in PUBLIC_PATHS
    if needs_auth:
        token = auth.bearer(request.headers) or request.query.get("token", "")
        device = auth.device_for_token(token)
        machine = None if device else auth.machine_for_token(token)
        if device is None and machine is None:
            return error("not paired with this Director", status=401)
        request["device"] = device
        request["machine"] = machine

    try:
        response = await handler(request)
    except web.HTTPException:
        raise
    except Exception as exc:  # never leak a stack trace to the phone
        return error(f"{type(exc).__name__}: {exc}", status=500)
    for key, value in CORS_HEADERS.items():
        response.headers.setdefault(key, value)
    return response


# ---------------- health & pairing ----------------

@ROUTES.get("/api/health")
async def health(request: web.Request) -> web.Response:
    return json_response({
        "ok": True,
        "service": "aios-director",
        "time": time.time(),
        "paired_devices": len(store.list_devices()),
    })


@ROUTES.post("/api/pair")
async def pair(request: web.Request) -> web.Response:
    body = await request.json()
    result = auth.redeem_pairing_code(str(body.get("code") or ""),
                                      name=str(body.get("name") or ""),
                                      kind=str(body.get("kind") or "phone"))
    if result is None:
        return error("that pairing code is not valid (they expire after ten minutes)",
                     status=403)
    return json_response({"ok": True, **result})


# ---------------- state ----------------

def _agent_row(agent: dict, runtime: runtime_mod.Runtime) -> dict:
    thread = store.latest_thread(agent["id"])
    routines = store.list_routines(agent_id=agent["id"], include_disabled=False)
    working = runtime.working_from_group(thread["id"]) if thread and store.is_group(agent) else []
    return {
        "id": agent["id"],
        "name": agent["name"],
        "emoji": agent["emoji"],
        "avatar": agent.get("avatar") or "",
        "kind": agent["kind"],
        "subtitle": agent["subtitle"],
        "system_prompt": agent["system_prompt"],
        "backend": agent["backend"],
        "model": agent["model"],
        "reasoning": agent["reasoning"],
        "auto_approve": bool(agent.get("auto_approve")),
        "notify": bool(agent.get("notify", 1)),
        "members": list(agent.get("members") or []),
        "rules": agent.get("rules") or "",
        "routines": len(routines),
        "thread_id": thread["id"] if thread else "",
        "preview": thread["preview"] if thread else "",
        "updated_at": thread["updated_at"] if thread else agent["created_at"],
        "status": thread["status"] if thread else "idle",
        "busy": runtime.busy(thread["id"]) if thread else False,
        "working": working,
    }


@ROUTES.get("/api/state")
async def state(request: web.Request) -> web.Response:
    runtime = request.app["runtime"]
    settings = config.load_settings()
    agents = [_agent_row(agent, runtime) for agent in agents_mod.ensure_seeded()]
    agents.sort(key=lambda row: row["updated_at"], reverse=True)
    machines = runtime.online_machines()
    return json_response({
        "ok": True,
        "agents": agents,
        "machines": machines,
        "wake": await wake.status_with_probe(machines=machines, settings=settings),
        "cursor": store.latest_event_id(),
        "defaults": settings.get("defaults", {}),
        "phone": settings.get("phone", {}),
        "timezone": routines_mod.TIMEZONE_NAME,
        "operator": await display_mod.status(settings),
        "pending_approvals": store.list_approvals(status="pending"),
    })


@ROUTES.get("/api/agents")
async def list_agents(request: web.Request) -> web.Response:
    runtime = request.app["runtime"]
    return json_response({"ok": True,
                          "agents": [_agent_row(a, runtime) for a in agents_mod.ensure_seeded()]})


@ROUTES.post("/api/agents")
async def create_agent(request: web.Request) -> web.Response:
    body = await request.json()
    name = str(body.get("name") or "").strip()
    if not name:
        return error("an agent needs a name")
    kind = str(body.get("kind") or "custom").strip() or "custom"
    if kind == "group":
        members = agents_mod.resolve_member_ids(body.get("members") or [])
        if len(members) < 2:
            return error("a group chat needs at least two agents")
        agent = store.create_agent(
            name=name, emoji=str(body.get("emoji") or "🤖"),
            kind="group",
            subtitle=str(body.get("subtitle") or ""),
            avatar=str(body.get("avatar") or ""),
            tools=[],
            members=members,
            rules=str(body.get("rules") or ""),
            sort=int(body.get("sort") or 99))
    else:
        agent = store.create_agent(
            name=name, emoji=str(body.get("emoji") or "🤖"),
            kind=kind,
            subtitle=str(body.get("subtitle") or ""),
            system_prompt=str(body.get("system_prompt") or ""),
            backend=str(body.get("backend") or ""), model=str(body.get("model") or ""),
            reasoning=str(body.get("reasoning") or ""),
            avatar=str(body.get("avatar") or ""),
            tools=list(body.get("tools") or agents_mod.DIRECTOR_TOOLS),
            sort=int(body.get("sort") or 99))
    store.create_thread(agent["id"])
    return json_response({"ok": True, "agent": agent})


@ROUTES.patch("/api/agents/{agent_id}")
async def patch_agent(request: web.Request) -> web.Response:
    body = await request.json()
    if "members" in body:
        body = dict(body)
        body["members"] = agents_mod.resolve_member_ids(body.get("members") or [])
        if len(body["members"]) < 2:
            return error("a group chat needs at least two agents")
    agent = store.update_agent(request.match_info["agent_id"], body)
    if agent is None:
        return error("no such agent", status=404)
    return json_response({"ok": True, "agent": agent})


@ROUTES.delete("/api/agents/{agent_id}")
async def delete_agent(request: web.Request) -> web.Response:
    store.delete_agent(request.match_info["agent_id"])
    return json_response({"ok": True})


# ---------------- threads ----------------

def _thread_payload(thread_id: str, runtime: runtime_mod.Runtime | None = None) -> dict:
    thread = store.get_thread(thread_id)
    if not thread:
        return {}
    messages = store.list_messages(thread_id)
    through = int(thread.get("compacted_through") or 0)
    thread["hidden_count"] = sum(
        1 for message in messages if int(message.get("sequence") or 0) <= through)
    working = runtime.working_from_group(thread_id) if runtime else []
    return {"thread": thread, "messages": messages, "working": working}


@ROUTES.get("/api/agents/{agent_id}/thread")
async def agent_thread(request: web.Request) -> web.Response:
    agent_id = request.match_info["agent_id"]
    agent = store.get_agent(agent_id)
    if agent is None:
        return error("no such agent", status=404)
    thread = store.latest_thread(agent_id) or store.create_thread(agent_id)
    runtime = request.app["runtime"]
    return json_response({"ok": True, "cursor": store.latest_event_id(),
                          **_thread_payload(thread["id"], runtime)})


@ROUTES.get("/api/threads/{thread_id}")
async def get_thread(request: web.Request) -> web.Response:
    runtime = request.app["runtime"]
    payload = _thread_payload(request.match_info["thread_id"], runtime)
    if not payload:
        return error("no such thread", status=404)
    return json_response({"ok": True, "cursor": store.latest_event_id(), **payload})


@ROUTES.post("/api/threads/{thread_id}/messages")
async def post_message(request: web.Request) -> web.Response:
    runtime = request.app["runtime"]
    thread_id = request.match_info["thread_id"]
    body = await request.json()
    text = str(body.get("text") or "").strip()
    attachments = list(body.get("attachments") or [])
    if not text and not attachments:
        return error("empty message")
    try:
        message = await runtime.send_message(thread_id, text, attachments)
    except ValueError as exc:
        return error(str(exc), status=404)
    return json_response({"ok": True, "message": message})


@ROUTES.post("/api/threads/{thread_id}/stop")
async def stop_thread(request: web.Request) -> web.Response:
    runtime = request.app["runtime"]
    stopped = runtime.stop_thread(request.match_info["thread_id"])
    return json_response({"ok": True, "stopped": stopped})


@ROUTES.post("/api/threads/{thread_id}/clear")
async def clear_thread(request: web.Request) -> web.Response:
    thread_id = request.match_info["thread_id"]
    thread = store.get_thread(thread_id)
    if not thread:
        return error("no such thread", status=404)
    store.archive_thread(thread_id)
    fresh = store.create_thread(thread["agent_id"])
    return json_response({"ok": True, "thread": fresh})


@ROUTES.post("/api/agents/{agent_id}/threads")
async def new_thread(request: web.Request) -> web.Response:
    agent_id = request.match_info["agent_id"]
    if store.get_agent(agent_id) is None:
        return error("no such agent", status=404)
    return json_response({"ok": True, "thread": store.create_thread(agent_id)})


# ---------------- questions, approvals, jobs ----------------

@ROUTES.post("/api/questions/{question_id}")
async def answer_question(request: web.Request) -> web.Response:
    body = await request.json()
    ok = request.app["runtime"].answer_question(request.match_info["question_id"],
                                                str(body.get("answer") or ""))
    return json_response({"ok": ok})


@ROUTES.post("/api/approvals/{approval_id}")
async def decide_approval(request: web.Request) -> web.Response:
    body = await request.json()
    status = "approved" if str(body.get("status")) == "approved" else "declined"
    scope = str(body.get("scope") or "")
    if scope not in ("", "run", "agent", "all"):
        return error("scope must be run, agent or all")
    ok = request.app["runtime"].decide_approval(
        request.match_info["approval_id"], status, str(body.get("note") or ""),
        scope=scope)
    return json_response({"ok": ok})


# ---------------- routines ----------------

def _routine_row(row: dict) -> dict:
    return {
        "id": row["id"], "agent_id": row["agent_id"], "name": row["name"],
        "prompt": row["prompt"], "schedule": row["schedule"],
        "described": routines_mod.describe(row["schedule"]),
        "enabled": bool(row["enabled"]), "next_run": row["next_run"],
        "next_human": routines_mod.humanize_next(row["next_run"]) if row["enabled"] else "paused",
        "last_run": row["last_run"], "runs": row["runs"],
    }


@ROUTES.get("/api/routines")
async def list_routines(request: web.Request) -> web.Response:
    rows = store.list_routines(agent_id=request.query.get("agent_id", ""))
    return json_response({"ok": True, "routines": [_routine_row(row) for row in rows]})


@ROUTES.post("/api/routines")
async def create_routine(request: web.Request) -> web.Response:
    body = await request.json()
    agent_id = str(body.get("agent_id") or "")
    if store.get_agent(agent_id) is None:
        return error("no such agent", status=404)
    name = str(body.get("name") or "").strip()
    prompt = str(body.get("prompt") or "").strip()
    if not name or not prompt:
        return error("a routine needs a name and a prompt")
    try:
        schedule = routines_mod.normalize(body.get("schedule") or {})
        when = routines_mod.next_run(schedule)
    except routines_mod.ScheduleError as exc:
        return error(str(exc))
    row = store.create_routine(agent_id=agent_id, name=name, prompt=prompt,
                               schedule=schedule, next_run=when)
    return json_response({"ok": True, "routine": _routine_row(row)})


@ROUTES.patch("/api/routines/{routine_id}")
async def patch_routine(request: web.Request) -> web.Response:
    body = await request.json()
    existing = store.get_routine(request.match_info["routine_id"])
    if not existing:
        return error("no such routine", status=404)
    patch = {key: body[key] for key in ("name", "prompt", "enabled") if key in body}
    if body.get("schedule"):
        try:
            patch["schedule"] = routines_mod.normalize(body["schedule"])
            patch["next_run"] = routines_mod.next_run(patch["schedule"])
        except routines_mod.ScheduleError as exc:
            return error(str(exc))
    elif "enabled" in patch and patch["enabled"]:
        # Re-enabling something that has gone stale needs a fresh next run.
        patch["next_run"] = routines_mod.next_run(existing["schedule"])
    row = store.update_routine(existing["id"], patch)
    return json_response({"ok": True, "routine": _routine_row(row)})


@ROUTES.delete("/api/routines/{routine_id}")
async def delete_routine(request: web.Request) -> web.Response:
    store.delete_routine(request.match_info["routine_id"])
    return json_response({"ok": True})


@ROUTES.post("/api/routines/{routine_id}/run")
async def run_routine_now(request: web.Request) -> web.Response:
    row = store.get_routine(request.match_info["routine_id"])
    if not row:
        return error("no such routine", status=404)
    await request.app["runtime"].fire_routine(row)
    return json_response({"ok": True})


# ---------------- push ----------------

@ROUTES.get("/api/push/key")
async def push_key(request: web.Request) -> web.Response:
    return json_response({"ok": True, "available": push.AVAILABLE,
                          "public_key": push.public_key()})


@ROUTES.post("/api/push/subscribe")
async def push_subscribe(request: web.Request) -> web.Response:
    body = await request.json()
    subscription = body.get("subscription") or body
    if not (subscription or {}).get("endpoint"):
        return error("a push subscription with an endpoint is required")
    device = request.get("device") or {}
    push.subscribe(subscription, device_id=str(device.get("id") or ""))
    return json_response({"ok": True})


@ROUTES.post("/api/push/unsubscribe")
async def push_unsubscribe(request: web.Request) -> web.Response:
    body = await request.json()
    push.unsubscribe(str(body.get("endpoint") or ""))
    return json_response({"ok": True})


@ROUTES.post("/api/push/test")
async def push_test(request: web.Request) -> web.Response:
    result = await push.send("Director", "Notifications are working.",
                             tag="test")
    return json_response({"ok": True, **result})


@ROUTES.post("/api/threads/{thread_id}/watching")
async def set_watching(request: web.Request) -> web.Response:
    """The app says whether this thread is on screen, so a reply being read
    does not also arrive as a notification."""
    body = await request.json()
    request.app["runtime"].watching(request.match_info["thread_id"],
                                    active=bool(body.get("active", True)))
    return json_response({"ok": True})


@ROUTES.get("/api/jobs")
async def list_jobs(request: web.Request) -> web.Response:
    return json_response({"ok": True,
                          "jobs": store.list_jobs(thread_id=request.query.get("thread_id", ""))})


@ROUTES.get("/api/jobs/{job_id}")
async def get_job(request: web.Request) -> web.Response:
    job = store.get_job(request.match_info["job_id"])
    if not job:
        return error("no such job", status=404)
    return json_response({"ok": True, "job": job})


@ROUTES.get("/api/jobs/{job_id}/code-events")
async def job_code_events(request: web.Request) -> web.Response:
    """Proxy the CODE transcript events for a Director-dispatched job.

    The events live on the Windows machine (`code_jobs` events.jsonl). The
    phone opens a job card and pulls through Director so nothing has to listen
    on Windows.
    """
    job = store.get_job(request.match_info["job_id"])
    if not job:
        return error("no such job", status=404)
    result = job.get("result") or {}
    request_meta = job.get("request") or {}
    session_id = str(result.get("session_id") or request_meta.get("session_id") or "")
    machine_id = str(job.get("machine_id") or "")
    if not session_id or not machine_id:
        return error("this job has no CODE session yet", status=409)
    try:
        since = int(request.query.get("since") or 0)
    except (TypeError, ValueError):
        since = 0
    runtime = request.app["runtime"]
    payload = await runtime.call_machine(
        machine_id, "code.events",
        {"session_id": session_id, "since": since},
        timeout=45.0)
    if not isinstance(payload, dict):
        return error("machine returned nothing", status=502)
    if payload.get("ok") is False:
        return error(str(payload.get("error") or "could not read CODE events"), status=502)
    payload.setdefault("job_id", job["id"])
    payload.setdefault("session_id", session_id)
    return json_response(payload)


@ROUTES.post("/api/jobs/{job_id}/stop")
async def stop_job(request: web.Request) -> web.Response:
    return json_response({"ok": request.app["runtime"].stop_job(request.match_info["job_id"])})


# ---------------- events ----------------

@ROUTES.get("/api/events")
async def get_events(request: web.Request) -> web.Response:
    since = int(request.query.get("since") or 0)
    thread_id = request.query.get("thread_id", "")
    events = store.list_events(since=since, thread_id=thread_id)
    return json_response({"ok": True, "events": events,
                          "cursor": events[-1]["id"] if events else since})


@ROUTES.get("/ws")
async def websocket(request: web.Request) -> web.WebSocketResponse:
    token = auth.bearer(request.headers) or request.query.get("token", "")
    if auth.device_for_token(token) is None:
        raise web.HTTPUnauthorized(text="not paired")

    ws = web.WebSocketResponse(heartbeat=25.0)
    await ws.prepare(request)
    runtime = request.app["runtime"]
    queue = runtime.subscribe()

    since = int(request.query.get("since") or 0)
    if since:
        for event in store.list_events(since=since):
            await ws.send_json(event)
    await ws.send_json({"kind": "ready", "payload": {"cursor": store.latest_event_id()}})

    async def pump() -> None:
        while True:
            event = await queue.get()
            await ws.send_json(event)

    pump_task = asyncio.create_task(pump())
    try:
        async for message in ws:
            if message.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                payload = json.loads(message.data)
            except json.JSONDecodeError:
                continue
            kind = str(payload.get("type") or "")
            if kind == "ping":
                await ws.send_json({"kind": "pong", "payload": {}})
            elif kind == "answer":
                runtime.answer_question(str(payload.get("id") or ""),
                                        str(payload.get("answer") or ""))
            elif kind == "approve":
                runtime.decide_approval(str(payload.get("id") or ""),
                                        str(payload.get("status") or "declined"),
                                        str(payload.get("note") or ""))
    finally:
        pump_task.cancel()
        runtime.unsubscribe(queue)
    return ws


# ---------------- settings & models ----------------

@ROUTES.get("/api/settings")
async def get_settings(request: web.Request) -> web.Response:
    settings = config.load_settings(refresh=True)
    redacted = json.loads(json.dumps(settings))
    for backend in redacted.get("backends", {}).values():
        if backend.get("api_key"):
            backend["api_key"] = "••••" + str(backend["api_key"])[-4:]
    # The VAPID private key never leaves the box.
    redacted.get("push", {}).pop("private_pem", None)
    voice = redacted.get("voice", {})
    if voice.get("openai_api_key"):
        voice["openai_api_key"] = "••••" + str(voice["openai_api_key"])[-4:]
    return json_response({"ok": True, "settings": redacted})


@ROUTES.patch("/api/settings")
async def patch_settings(request: web.Request) -> web.Response:
    body = await request.json()
    # A redacted key coming back from the UI must never overwrite the real one.
    for backend in (body.get("backends") or {}).values():
        if isinstance(backend, dict) and str(backend.get("api_key") or "").startswith("••••"):
            backend.pop("api_key")
    voice = body.get("voice") or {}
    if str(voice.get("openai_api_key") or "").startswith("••••"):
        voice.pop("openai_api_key")
    if "instructions" in body:
        body["instructions"] = str(body.get("instructions") or "")[:8000]
    config.update_settings(body)
    return json_response({"ok": True})


@ROUTES.get("/api/models")
async def list_models(request: web.Request) -> web.Response:
    settings = config.load_settings()
    out = []
    for backend in models.BACKENDS:
        ready, message = await models.backend_status(backend, settings=settings)
        out.append({"backend": backend, "ready": ready, "message": message})
    return json_response({
        "ok": True,
        "backends": out,
        "codex_models": [
            {"id": "gpt-5.6-luna", "label": "GPT-5.6 Luna",
             "reasoning": ["none", "low", "medium"], "default_reasoning": "low"},
            {"id": "gpt-5.6-terra", "label": "GPT-5.6 Terra",
             "reasoning": ["low", "medium", "high"], "default_reasoning": "medium"},
            {"id": "gpt-5.6-sol", "label": "GPT-5.6 Sol",
             "reasoning": ["low", "medium", "high", "xhigh"], "default_reasoning": "medium"},
        ],
    })


# ---------------- voice ----------------

@ROUTES.post("/api/voice/transcribe")
async def transcribe(request: web.Request) -> web.Response:
    settings = config.load_settings()
    key = config.openai_key(settings)
    if not key:
        return error("no OpenAI key configured for transcription", status=503)
    reader = await request.multipart()
    audio = b""
    filename = "clip.webm"
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "audio":
            filename = part.filename or filename
            audio = await part.read(decode=False)
    if not audio:
        return error("no audio uploaded")

    form = aiohttp.FormData()
    form.add_field("file", audio, filename=filename,
                   content_type=mimetypes.guess_type(filename)[0] or "audio/webm")
    form.add_field("model", str((settings.get("voice") or {}).get("model") or "whisper-1"))
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post("https://api.openai.com/v1/audio/transcriptions",
                                data=form,
                                headers={"Authorization": f"Bearer {key}"}) as resp:
            payload = await resp.json(content_type=None)
            if resp.status != 200:
                detail = str((payload or {}).get("error", {}).get("message") or payload)[:300]
                return error(f"transcription failed: {detail}", status=502)
    return json_response({"ok": True, "text": str((payload or {}).get("text") or "").strip()})


# ---------------- operator ----------------

@ROUTES.get("/api/operator/status")
async def operator_status(request: web.Request) -> web.Response:
    settings = config.load_settings()
    state = await display_mod.status(settings)
    state["takeover_path"] = display_mod.takeover_path()
    state["novnc_installed"] = bool(display_mod.novnc_root())
    return json_response({"ok": True, "operator": state})


@ROUTES.post("/api/operator/start")
async def operator_start(request: web.Request) -> web.Response:
    # Starting the viewer must never launch or replace the browser an active
    # operator turn is controlling. Operator runs request Chrome themselves.
    return json_response({"ok": True, "operator": await display_mod.ensure_running(with_chrome=False)})


@ROUTES.post("/api/operator/restart")
async def operator_restart(request: web.Request) -> web.Response:
    return json_response({"ok": True, "operator": await display_mod.restart_viewer()})


_SHOT_TTL = 1.25
_shot_cache: dict[str, Any] = {"at": 0.0, "full": None, "preview": None}


@ROUTES.get("/api/operator/screenshot")
async def operator_screenshot(request: web.Request) -> web.Response:
    preview = request.query.get("preview") in ("1", "true")
    key = "preview" if preview else "full"
    now = time.monotonic()
    cached = _shot_cache.get(key)
    if cached and now - float(_shot_cache.get("at") or 0) < _SHOT_TTL:
        return json_response({"ok": True, **cached, "cached": True})
    settings = config.load_settings()
    await display_mod.ensure_running(settings, with_chrome=False)
    try:
        png = await x11.capture(settings)
    except RuntimeError as exc:
        return error(str(exc), status=503)
    max_width = 720 if preview else 1400
    quality = 52 if preview else 68
    data_url, width, height = x11.encode_jpeg(png, max_width=max_width, quality=quality)
    payload = {"image": data_url, "width": width, "height": height}
    _shot_cache["at"] = time.monotonic()
    _shot_cache[key] = payload
    return json_response({"ok": True, **payload})


# ---------------- machines ----------------

@ROUTES.post("/api/machines/enroll")
async def enroll_machine(request: web.Request) -> web.Response:
    body = await request.json()
    name = str(body.get("name") or "").strip()
    if not name:
        return error("a machine needs a name")
    result = auth.enroll_machine(name=name, platform=str(body.get("platform") or ""),
                                 caps=dict(body.get("caps") or {}))
    return json_response({"ok": True, **result})


@ROUTES.get("/api/machines")
async def list_machines(request: web.Request) -> web.Response:
    runtime = request.app["runtime"]
    machines = runtime.online_machines()
    settings = config.load_settings()
    return json_response({
        "ok": True,
        "machines": machines,
        "wake": await wake.status_with_probe(machines=machines, settings=settings),
    })


@ROUTES.post("/api/wake")
async def wake_pc(request: web.Request) -> web.Response:
    """Broadcast a Wake-on-LAN magic packet to the house Windows PC."""
    result = wake.send()
    if not result.get("ok"):
        return error(str(result.get("error") or "wake failed"))
    return json_response(result)


@ROUTES.post("/api/power/off")
async def power_off_pc(request: web.Request) -> web.Response:
    """Ask the connected Windows client to schedule a clean shutdown."""
    runtime = request.app["runtime"]
    machine = wake.windows_machine(runtime.online_machines())
    if machine is None or not machine.get("online"):
        return error("the Windows PC is not connected", status=409)
    result = await runtime.call_machine(
        str(machine["id"]), "power.off", {}, timeout=15.0)
    if not result.get("ok"):
        return error(str(result.get("error") or "could not turn off the PC"))
    return json_response(result)


@ROUTES.post("/api/machine/job")
async def machine_job_update(request: web.Request) -> web.Response:
    """A machine reporting progress or the final result of a dispatched job."""
    machine = request.get("machine")
    if machine is None:
        return error("machine token required", status=403)
    body = await request.json()
    job_id = str(body.get("job_id") or "")
    job = store.get_job(job_id)
    if not job:
        return error("no such job", status=404)
    status = str(body.get("status") or "")
    result = dict(body.get("result") or {})
    if status:
        previous = dict(job.get("result") or {})
        store.update_job(job_id, status=status, result={**previous, **result})
    runtime = request.app["runtime"]
    await runtime.emit("code.progress", {"job_id": job_id, "status": status, **result},
                       thread_id=job.get("thread_id", ""), agent_id=job.get("agent_id", ""))
    return json_response({"ok": True})


@ROUTES.get("/machine")
async def machine_socket(request: web.Request) -> web.WebSocketResponse:
    """The outbound link a client machine (the Windows desktop) dials in on."""
    token = auth.bearer(request.headers) or request.query.get("token", "")
    machine = auth.machine_for_token(token)
    if machine is None:
        raise web.HTTPUnauthorized(text="unknown machine token")

    ws = web.WebSocketResponse(heartbeat=30.0)
    await ws.prepare(request)
    runtime = request.app["runtime"]

    class Link:
        async def send(self, payload: dict) -> None:
            await ws.send_json(payload)

    runtime.attach_machine(machine["id"], Link())
    await runtime.emit("machine.online", {"id": machine["id"], "name": machine["name"]})
    try:
        async for message in ws:
            if message.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                payload = json.loads(message.data)
            except json.JSONDecodeError:
                continue
            kind = str(payload.get("type") or "")
            if kind == "hello":
                try:
                    wake.remember(payload)
                except ValueError:
                    pass
            elif kind == "result":
                runtime.resolve_machine_call(str(payload.get("call_id") or ""),
                                             dict(payload.get("result") or {}))
            elif kind == "event":
                job_id = str(payload.get("job_id") or "")
                job = store.get_job(job_id) or {}
                await runtime.emit(str(payload.get("kind") or "code.progress"),
                                   dict(payload.get("payload") or {}),
                                   thread_id=job.get("thread_id", ""),
                                   agent_id=job.get("agent_id", ""))
            elif kind == "job":
                job_id = str(payload.get("job_id") or "")
                existing = store.get_job(job_id) or {}
                previous = dict(existing.get("result") or {})
                incoming = dict(payload.get("result") or {})
                store.update_job(job_id, status=str(payload.get("status") or "done"),
                                 result={**previous, **incoming})
    finally:
        if runtime.detach_machine(machine["id"], link):
            await runtime.emit("machine.offline", {
                "id": machine["id"], "name": machine["name"]})
    return ws


# ---------------- noVNC takeover ----------------

@ROUTES.get("/vnc/ws")
async def vnc_bridge(request: web.Request) -> web.WebSocketResponse:
    """Bridge a browser WebSocket to x11vnc's TCP port — websockify, inline.

    noVNC cannot set an Authorization header, so the token rides in the query
    string. Nothing else about the connection is trusted.
    """
    token = request.query.get("token", "")
    if auth.device_for_token(token) is None:
        raise web.HTTPUnauthorized(text="not paired")

    settings = config.load_settings()

    # Connect before accepting the WebSocket so a healthy display takes the
    # direct, sub-second path. This never probes, starts or restarts Chrome.
    try:
        reader, writer = await display_mod.open_viewer_connection(settings)
    except (OSError, ConnectionError, asyncio.TimeoutError) as exc:
        raise web.HTTPServiceUnavailable(text=str(exc)) from exc

    # noVNC 1.0 always opens with subprotocol "binary". If we do not echo it,
    # the browser aborts the handshake and the phone shows "Failed to connect".
    ws = web.WebSocketResponse(protocols=("binary",), heartbeat=None, max_msg_size=0)
    await ws.prepare(request)

    async def tcp_to_ws() -> None:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            await ws.send_bytes(chunk)

    pump = asyncio.create_task(tcp_to_ws())
    try:
        async for message in ws:
            if message.type == aiohttp.WSMsgType.BINARY:
                writer.write(message.data)
                await writer.drain()
            elif message.type == aiohttp.WSMsgType.TEXT:
                writer.write(message.data.encode("latin-1", errors="ignore"))
                await writer.drain()
    finally:
        pump.cancel()
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, asyncio.CancelledError):
            pass
    return ws


@ROUTES.get("/vnc/view")
async def vnc_view(request: web.Request) -> web.StreamResponse:
    """Chrome-free operator screen. Token is required; the page passes it to /vnc/ws."""
    token = request.query.get("token", "")
    if auth.device_for_token(token) is None:
        return error("not paired", status=401)
    target = pathlib.Path(__file__).resolve().parent / "operator" / "viewer.html"
    if not target.is_file():
        return error("viewer missing", status=500)
    return web.FileResponse(target, headers={"Cache-Control": "no-store", **CORS_HEADERS})


@ROUTES.get("/vnc/{tail:.*}")
async def vnc_static(request: web.Request) -> web.StreamResponse:
    """Serve noVNC's own files from the distribution package."""
    root = display_mod.novnc_root()
    if not root:
        return error("noVNC is not installed on the box (apt install novnc)", status=503)
    tail = request.match_info.get("tail") or "vnc.html"
    target = pathlib.Path(root) / tail
    try:
        target = target.resolve()
        if not str(target).startswith(str(pathlib.Path(root).resolve())):
            return error("nope", status=403)
    except OSError:
        return error("not found", status=404)
    if not target.is_file():
        return error("not found", status=404)
    return web.FileResponse(target, headers={"Cache-Control": "public, max-age=3600"})


# ---------------- app ----------------

async def _startup(app: web.Application) -> None:
    agents_mod.ensure_seeded()
    settings = config.load_settings()
    if not store.list_devices():
        code = auth.new_pairing_code()
        print(f"[director] no devices paired yet — pairing code: {code['code']} "
              f"(valid {int(auth.CODE_TTL / 60)} minutes)", flush=True)
    _realign_wall_clock_routines()
    _catch_up_routines()
    app["runtime"].start_scheduler()
    if push.AVAILABLE:
        push.ensure_keys(settings)
    try:
        # Keep the viewer infrastructure warm. Chrome belongs to operator runs
        # and may already be in the middle of an action.
        await display_mod.ensure_running(settings, with_chrome=False)
    except Exception as exc:
        print(f"[director] operator display not started: {exc}", flush=True)


def _catch_up_routines() -> None:
    """Push any routine whose slot passed while Director was down to its next
    slot, so a restart does not fire a week of missed dailies at once."""
    now = time.time()
    for row in store.list_routines(include_disabled=False):
        if not row["next_run"] or row["next_run"] > now:
            continue
        schedule = row["schedule"]
        if not routines_mod.is_recurring(schedule):
            continue
        # More than one interval late means the box was off; skip to the next.
        try:
            following = routines_mod.next_run(schedule, after=now)
        except routines_mod.ScheduleError:
            continue
        if now - row["next_run"] > 3600:
            store.update_routine(row["id"], {"next_run": following})


def _realign_wall_clock_routines() -> None:
    """Migrate stored host-local slots to explicit Swedish wall-clock slots."""
    now = time.time()
    for row in store.list_routines(include_disabled=False):
        if str((row.get("schedule") or {}).get("kind") or "") not in {
                "daily", "weekdays", "weekly"}:
            continue
        try:
            following = routines_mod.next_run(row["schedule"], after=now)
        except routines_mod.ScheduleError:
            continue
        if abs(float(row.get("next_run") or 0) - following) > 1:
            store.update_routine(row["id"], {"next_run": following})


def create_app() -> web.Application:
    app = web.Application(middlewares=[funnel_prefix, cors_and_auth],
                          client_max_size=64 * 1024 * 1024)
    app["runtime"] = runtime_mod.runtime()
    app.add_routes(ROUTES)
    app.on_startup.append(_startup)
    return app


def main() -> None:
    settings = config.load_settings()
    server_cfg = settings.get("server", {}) or {}
    bind = str(os.environ.get("AIOS_DIRECTOR_BIND") or server_cfg.get("bind") or config.DEFAULT_BIND)
    port = int(os.environ.get("AIOS_DIRECTOR_PORT") or server_cfg.get("port") or config.DEFAULT_PORT)
    print(f"[director] listening on http://{bind}:{port}", flush=True)
    web.run_app(create_app(), host=bind, port=port, print=None)


if __name__ == "__main__":
    main()
