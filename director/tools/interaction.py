"""Talking back to the human mid-task.

Three shapes, all of which park the run until the phone answers:

    ask_user   a question ("which of these two?") — free text back.
    handoff    "this needs your hands": secret entry, manual 2FA approval, a
               captcha or payment details that actually block progress.
               Director opens the takeover view and waits. It never sees,
               types or stores the secret.
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
    "ask_yes_no",
    "Ask Calle a simple yes/no question and wait for a one-tap answer. Use for "
    "a binary decision or a quick confirmation where two big buttons in chat "
    "(a green \u2713 yes and a red \u2715 no) are clearer than free text.",
    {
        "type": "object",
        "properties": {
            "question": {"type": "string"},
        },
        "required": ["question"],
    },
)
async def ask_yes_no(ctx: ToolContext, question: str = "") -> ToolResult:
    text = str(question or "").strip()
    if not text:
        return ToolResult(error="no question given")
    answer = await ctx.ask_user(text, options=["Yes", "No"], kind="yes_no")
    yes = str(answer or "").strip().lower().startswith("yes")
    return ToolResult(
        output="yes" if yes else "no",
        card={"title": "yes/no", "preview": text[:90],
              "meta": "yes" if yes else "no", "tone": "ok" if yes else "danger"},
    )


@tool(
    "handoff",
    "Hand control to Calle only when the operator is genuinely blocked by "
    "secret entry, manual 2FA approval, a captcha, payment details, or missing "
    "account access. Try the existing signed-in session and ordinary Google "
    "SSO/account selection first. Director opens the takeover view and waits. "
    "Never type credentials yourself, and never ask for them in chat.",
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
