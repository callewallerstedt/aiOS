"""Talking back to the human mid-task.

Three shapes, all of which park the run until the phone answers:

    ask_user   a question ("which of these two?") — free text back.
    handoff    "this needs your hands": a login, a 2FA prompt, a captcha, a
               card number. Director opens the takeover view on the operator's
               screen and waits. It never sees, types or stores the secret.
    confirm    an explicit yes/no before something outward-facing.
"""
from __future__ import annotations

from . import ToolContext, ToolResult, tool

HANDOFF_REASONS = ("login", "2fa", "captcha", "payment", "other")


@tool(
    "ask_user",
    "Ask Calle a question and wait for the answer. Use when the request is "
    "ambiguous in a way that changes what you would do, not for things you can "
    "reasonably decide yourself.",
    {
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional quick-reply choices.",
            },
        },
        "required": ["question"],
    },
)
async def ask_user(ctx: ToolContext, question: str = "", options=None) -> ToolResult:
    text = str(question or "").strip()
    if not text:
        return ToolResult(error="no question given")
    answer = await ctx.ask_user(text, options=list(options or []), kind="question")
    return ToolResult(
        output=answer or "(no answer)",
        card={"title": "asked", "preview": text[:90], "meta": "answered", "tone": "accent"},
    )


@tool(
    "handoff",
    "Hand control to Calle on the operator screen for something you must not "
    "do yourself: signing in, a 2FA code, a captcha, or entering payment "
    "details. Director opens the takeover view and waits. Never type "
    "credentials yourself, and never ask for them in chat.",
    {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "enum": list(HANDOFF_REASONS)},
            "what": {"type": "string", "description": "What Calle needs to do, in one line."},
        },
        "required": ["reason", "what"],
    },
)
async def handoff(ctx: ToolContext, reason: str = "other", what: str = "") -> ToolResult:
    detail = str(what or "").strip() or "Take over the screen"
    kind = str(reason or "other").strip().lower()
    if kind not in HANDOFF_REASONS:
        kind = "other"
    operator_cfg = (ctx.settings.get("operator") or {})
    answer = await ctx.ask_user(
        detail,
        options=["Done", "Cancel"],
        kind="handoff",
        extra={"reason": kind, "takeover": True,
               "novnc_port": operator_cfg.get("novnc_port")},
    )
    done = str(answer or "").strip().lower().startswith("done")
    return ToolResult(
        output="Calle finished on the screen; continue." if done
               else f"Calle did not complete the handoff ({answer or 'cancelled'}).",
        card={"title": "handoff", "preview": detail[:90],
              "meta": kind, "tone": "accent" if done else "muted"},
    )


@tool(
    "confirm",
    "Get an explicit yes before doing something outward-facing or hard to "
    "undo that no other tool already gates.",
    {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "What you are about to do."},
            "detail": {"type": "string"},
        },
        "required": ["summary"],
    },
)
async def confirm(ctx: ToolContext, summary: str = "", detail: str = "") -> ToolResult:
    text = str(summary or "").strip()
    if not text:
        return ToolResult(error="nothing to confirm")
    decision = await ctx.request_approval(tool="confirm", summary=text,
                                          detail=str(detail or ""), payload={})
    approved = str(decision.get("status")) == "approved"
    return ToolResult(
        output="approved" if approved else f"declined: {decision.get('note') or 'no reason given'}",
        card={"title": "confirm", "preview": text[:90],
              "meta": "approved" if approved else "declined",
              "tone": "ok" if approved else "danger"},
    )
