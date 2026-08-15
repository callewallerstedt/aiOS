"""Dispatching work to the pixel operator.

The run happens in a background job, not inside the coordinator's turn, so the
chat keeps answering while the screen is being driven. Every step streams into
the same thread, which is why the phone can watch it and stop it.
"""
from __future__ import annotations

import asyncio

from .. import store
from ..operator import display as display_mod
from ..operator import loop as operator_loop
from ..operator import x11
from . import ToolContext, ToolResult, tool


@tool(
    "operator",
    "Drive the Linux screen to do something: use a signed-in website, work in "
    "a GUI app, click through a flow. The run happens in the background and "
    "streams into this chat; you get the result as a new message when it "
    "finishes, so end your turn after dispatching. Describe the goal and what "
    "'done' looks like, not the individual clicks.",
    {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "The goal, in plain language, including any value to "
                               "read back and what finished looks like.",
            },
        },
        "required": ["task"],
    },
)
async def operator(ctx: ToolContext, task: str = "", review_every: int = 0,
                   max_steps: int = 0) -> ToolResult:
    goal = str(task or "").strip()
    if not goal:
        return ToolResult(error="no task given")
    if ctx.depth > 2:
        return ToolResult(error="operator dispatch nested too deep")

    # There is one screen and one mouse. Two runs at once fight over both: they
    # click through each other's dialogs and neither finishes. It has happened
    # — two runs started three minutes apart and both stalled.
    live = ctx.hub.live_jobs("operator")
    if live:
        busy = live[0]
        running = str((busy.get("request") or {}).get("task") or "")[:200]
        return ToolResult(
            error=f"the operator is already running job {busy['id']}: {running!r}. "
                  "There is only one screen, so this was not started. Steer that "
                  "run with `operator_say`, wait for it to report back, or stop "
                  "it with `stop_job` if it is doing the wrong thing.",
            card={"title": "operator", "preview": goal[:90], "meta": "already busy",
                  "tone": "danger", "job_id": busy["id"]})

    # The checkpoint cadence is a user setting, not a planning choice. Ignore
    # legacy generated review_every/max_steps arguments so a coordinator cannot
    # quietly shorten the requested 30-step interval.
    configured_interval = int(((ctx.settings.get("operator", {}) or {})
                               .get("review_every") or 30))
    interval = max(1, configured_interval)
    job = store.create_job(kind="operator", request={"task": goal,
                                                      "review_every": interval},
                           thread_id=ctx.thread_id, agent_id=ctx.agent.get("id", ""),
                           status="running")
    cancel = asyncio.Event()

    async def emit(kind: str, payload: dict) -> None:
        await ctx.emit(kind, {**payload, "job_id": job["id"]})

    async def ask(question: str, **kwargs) -> str:
        return await ctx.ask_user(question, **kwargs)

    hub = ctx.hub
    job_id = job["id"]

    async def run() -> dict:
        return await operator_loop.run_task(
            goal, emit=emit, settings=ctx.settings, cancel=cancel,
            ask_user=ask, review_every=interval,
            follow_ups=lambda: hub.take_job_notes(job_id))

    ctx.hub.start_job(job, run)
    return ToolResult(
        output=f"operator job {job_id} started — it will report back here when it "
               "finishes. Tell Calle what you kicked off, then stop. If he adds "
               "something while it runs, send it on with `operator_say` instead "
               "of starting a second run.",
        card={"title": "operator", "preview": goal[:90], "meta": "running",
              "tone": "accent", "job_id": job_id, "job_kind": "operator"},
    )


@tool(
    "operator_say",
    "Send an instruction to the operator run that is already going: a "
    "correction, a detail it is missing, or an answer it needs. It reads this "
    "before its next action and treats it as the current task. Use this "
    "whenever Calle adds something mid-run — starting a second run is not "
    "possible, because there is only one screen.",
    {
        "type": "object",
        "properties": {
            "text": {"type": "string",
                     "description": "What the operator should know or do now."},
            "job_id": {"type": "string",
                       "description": "Operator job to steer. Defaults to the one running."},
        },
        "required": ["text"],
    },
)
async def operator_say(ctx: ToolContext, text: str = "", job_id: str = "") -> ToolResult:
    note = str(text or "").strip()
    if not note:
        return ToolResult(error="nothing to say")
    live = ctx.hub.live_jobs("operator")
    target = job_id.strip() or (live[0]["id"] if live else "")
    if not target:
        return ToolResult(error="no operator run is going right now — dispatch one "
                                "with `operator` instead")
    if not ctx.hub.note_job(target, note):
        return ToolResult(error=f"operator job {target} is not running any more")
    await ctx.emit("operator.note", {"job_id": target, "text": note})
    return ToolResult(
        output=f"passed to operator job {target}; it picks this up on its next step",
        card={"title": "operator", "preview": note[:90], "meta": "steered",
              "tone": "accent", "job_id": target},
    )


@tool(
    "operator_screenshot",
    "Look at the operator screen right now, without starting a run.",
    {"type": "object", "properties": {}},
)
async def operator_screenshot(ctx: ToolContext) -> ToolResult:
    state = await display_mod.ensure_running(ctx.settings, with_chrome=False)
    if not state.get("ready"):
        return ToolResult(error=f"the operator display is not running ({state.get('units')})")
    try:
        png = await x11.capture(ctx.settings)
    except RuntimeError as exc:
        return ToolResult(error=str(exc))
    data_url, width, height = x11.encode_jpeg(png)
    windows = await x11.window_list(settings=ctx.settings)
    await ctx.emit("operator.screenshot", {"image": data_url, "width": width, "height": height})
    listing = ", ".join(windows[:8]) or "no windows open"
    return ToolResult(
        output=f"Screenshot taken ({width}x{height}). Open windows: {listing}.",
        card={"title": "screen", "preview": listing[:80], "meta": f"{width}x{height}",
              "tone": "ok"},
        image=data_url,
    )


@tool(
    "operator_takeover",
    "Open the operator screen on Calle's phone so he can use it himself. Use "
    "when he asks to see or drive the screen; for credentials use `handoff`.",
    {"type": "object", "properties": {
        "why": {"type": "string", "description": "One line on what he should look at."}}},
)
async def operator_takeover(ctx: ToolContext, why: str = "") -> ToolResult:
    state = await display_mod.ensure_running(ctx.settings, with_chrome=True)
    if not state.get("ready"):
        return ToolResult(error=f"the operator display is not running ({state.get('units')})")
    await ctx.emit("operator.takeover", {"path": display_mod.takeover_path(),
                                         "why": str(why or "")})
    return ToolResult(
        output="takeover view opened on the phone",
        card={"title": "takeover", "preview": str(why or "operator screen")[:90],
              "meta": state.get("display", ""), "tone": "accent",
              "takeover": display_mod.takeover_path()},
    )
