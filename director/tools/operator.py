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
            "review_every": {
                "type": "integer",
                "minimum": 1,
                "description": ("Progress-review interval. Default 30. This is not a hard "
                                "step limit; work continues when meaningful progress is confirmed."),
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

    # Treat the old hidden max_steps argument as a review interval so an
    # already-generated call cannot reintroduce a hard ceiling during deploy.
    configured_interval = int(((ctx.settings.get("operator", {}) or {})
                               .get("review_every") or 30))
    interval = max(1, int(review_every or max_steps or configured_interval))
    job = store.create_job(kind="operator", request={"task": goal,
                                                      "review_every": interval},
                           thread_id=ctx.thread_id, agent_id=ctx.agent.get("id", ""),
                           status="running")
    cancel = asyncio.Event()

    async def emit(kind: str, payload: dict) -> None:
        await ctx.emit(kind, {**payload, "job_id": job["id"]})

    async def ask(question: str, **kwargs) -> str:
        return await ctx.ask_user(question, **kwargs)

    async def run() -> dict:
        return await operator_loop.run_task(
            goal, emit=emit, settings=ctx.settings, cancel=cancel,
            ask_user=ask, review_every=interval)

    ctx.hub.start_job(job, run)
    return ToolResult(
        output=f"operator job {job['id']} started — it will report back here when it "
               "finishes. Tell Calle what you kicked off, then stop.",
        card={"title": "operator", "preview": goal[:90], "meta": "running",
              "tone": "accent", "job_id": job["id"]},
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
