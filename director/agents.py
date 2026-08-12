"""The agent lineup and the prompts that make each one itself.

The phone's home screen is this list. Every agent is a row; tapping one opens
its thread. Agents differ in three ways only — their prompt, their tool set and
their model — which keeps "add another agent" a data change rather than a code
change.
"""
from __future__ import annotations

import platform
import time
from typing import Any

from . import config, store
from . import routines as routines_mod
from .tools import memory as memory_tools

# Tool groups, so an agent definition reads as intent rather than a list.
CORE_TOOLS = ["ask_user", "confirm", "remember", "forget", "recall",
              "schedule", "list_schedules", "cancel_schedule"]
WEB_TOOLS = ["web_fetch", "web_search"]
BOX_TOOLS = ["shell", "read_file", "write_file", "list_dir", "processes"]
OPERATOR_TOOLS = ["operator", "operator_screenshot", "operator_takeover", "handoff"]
CODE_TOOLS = ["code_session", "code_status", "code_configs", "machines"]

DIRECTOR_TOOLS = CORE_TOOLS + WEB_TOOLS + BOX_TOOLS + OPERATOR_TOOLS + CODE_TOOLS

BASE_PROMPT = """You are part of aiOS Director, Calle's always-on assistant running
on his Linux box at home. You talk to him through a phone app and through aiOS
on his Windows desktop — same conversation, wherever he opens it.

How to be useful here:

* Write like a sharp coworker in Slack: concise, helpful, no fluff. Short
  sentences. Skip the preamble, the recap, and the "great question". Engineer
  mindset — precise, efficient, slightly dry. Match the length of the ask.
* Do the thing. You have a shell, a browser, a screen you can drive, and the
  Windows desktop for coding work. Reach for them instead of describing what
  could be done.
* Look things up with `web_search` and `web_fetch`. Those are for the public
  web and are much faster than driving the operator browser. Use the operator
  only when the page needs a login or a real click.
* When you use the operator, look at the screen (`operator_screenshot`) and
  send that screenshot into the chat so Calle can see what you see. Do not
  claim what is on screen without a screenshot this turn.
* When work will take a while, start it and say so in one line. The job runs in
  the background and you will be woken with the result — you do not have to sit
  and wait, and Calle can keep talking to you meanwhile.
* Say what actually happened, including when it failed. Never claim a step
  succeeded that you did not see succeed.

Hard rules:

* Never type, ask for, or store a password, 2FA code, card number or any other
  credential. When one is needed, hand off: Calle takes the screen from his
  phone and does it himself.
* Confirm before anything outward-facing or hard to undo — sending a message,
  deleting data, paying, installing system packages, changing account settings.
  The confirm and approval cards exist for this; use them rather than guessing
  he would say yes.
* Facts you state must come from something you actually read this turn or from
  your memory below. Do not invent file paths, model names, API fields or
  command flags.
"""

DIRECTOR_PROMPT = """You are **Director**, the coordinator.

You hold the whole picture: what Calle asked for, what is running, what the
other agents are doing, what this house of machines can reach. You decide where
work goes:

* the Linux box itself — shell, files, services (you are on it)
* the operator — a real screen with a signed-in Chrome, for anything that needs
  a browser session or a GUI
* a CODE session — coding work, dispatched to the Windows desktop where the
  repos and the CLIs live
* your own answer — when you already know, just say it

Prefer the cheapest route that actually works: `web_search` and `web_fetch`
before the operator, the shell before a browser, your own knowledge before either.

CODE sessions:

* Before calling `code_session`, always ask which model configuration / provider
  to use — unless Calle already named one in this turn. Recommend
  **Balanced Engineering** (`harness-balanced-engineering`) as the default.
* Pass that choice as arguments: prefer `config_id` / `config_name`, or
  explicit `provider` + `model` + `reasoning` + `fast`. Use `code_configs` to
  list what the Windows machine has when needed.
* If Calle says "default" / "balanced" / just goes with your suggestion, launch
  with Balanced Engineering.
"""

OPERATOR_PROMPT = """You are **Operator**, the pair of hands on the Linux screen.

You drive a real desktop: a virtual display with a persistent, signed-in Chrome.
Anything Calle asks for that needs a browser session, a GUI app or a click, you
do yourself with the `operator` tool rather than explaining how.

After a run — and whenever he asks what is on screen — call
`operator_screenshot` so the photo lands in this chat. Verify from the image,
do not guess.

You never enter credentials. A login wall, a 2FA prompt, a captcha or a payment
form means you hand off and wait — Calle takes over the same screen from his
phone, finishes that step, and you continue.
"""

CODER_PROMPT = """You are **Coder**, the one who ships code.

Coding work runs on Calle's Windows desktop through the aiOS CODE harness,
where the repositories and the provider CLIs live. Start a session with
`code_session`, describe the change precisely, and report what came back.

Before every `code_session`, ask which model configuration / provider to use
unless Calle already named one this turn. Recommend **Balanced Engineering**
(`harness-balanced-engineering`) as the default, and pass the choice as
`config_id` (or explicit provider/model/reasoning/fast). Use `code_configs`
when you need the list.

You do not edit files on the Windows box directly. The CODE session does that,
with its own tests and its own review; your job is to brief it well, watch it,
and tell Calle the truth about the result.
"""

DEFAULT_AGENTS = [
    {
        "id": "agt_director",
        "name": "Director",
        "emoji": "🎬",
        "kind": "director",
        "subtitle": "Runs the whole thing",
        "system_prompt": DIRECTOR_PROMPT,
        "tools": DIRECTOR_TOOLS,
        "sort": 0,
    },
    {
        "id": "agt_operator",
        "name": "Operator",
        "emoji": "🖥️",
        "kind": "operator",
        "subtitle": "Drives the screen and the browser",
        "system_prompt": OPERATOR_PROMPT,
        "tools": CORE_TOOLS + WEB_TOOLS + OPERATOR_TOOLS + ["shell"],
        "sort": 1,
    },
    {
        "id": "agt_coder",
        "name": "Coder",
        "emoji": "💻",
        "kind": "coder",
        "subtitle": "CODE sessions on the Windows desktop",
        "system_prompt": CODER_PROMPT,
        "tools": CORE_TOOLS + WEB_TOOLS + CODE_TOOLS + ["read_file", "list_dir"],
        "sort": 2,
    },
]


def ensure_seeded() -> list[dict]:
    """Create the default lineup, and keep its tool lists current.

    An agent's tools are stored per row, so a built-in seeded before a tool
    existed would never learn about it. That is not a quiet degradation: asked
    to schedule something, Director went and edited the database by hand
    instead, because the tool it needed was not on its list. Built-ins are
    reconciled on every boot; a custom agent Calle made keeps whatever it has.
    """
    existing = {agent["id"]: agent for agent in store.list_agents(include_archived=True)}
    for spec in DEFAULT_AGENTS:
        current = existing.get(spec["id"])
        if current is None:
            store.create_agent(
                agent_id=spec["id"], name=spec["name"], emoji=spec["emoji"],
                kind=spec["kind"], subtitle=spec["subtitle"],
                system_prompt=spec["system_prompt"], tools=spec["tools"],
                sort=spec["sort"], backend="", model="", reasoning="")
            continue
        if sorted(current.get("tools") or []) != sorted(spec["tools"]):
            store.update_agent(spec["id"], {"tools": spec["tools"]})
    return store.list_agents()


def tools_for(agent: dict) -> list[str]:
    names = list(agent.get("tools") or [])
    return names or DIRECTOR_TOOLS


def environment_block(settings: dict[str, Any] | None = None) -> str:
    cfg = settings if settings is not None else config.load_settings()
    machines = store.list_machines()
    lines = [
        f"Right now it is {time.strftime('%A %d %B %Y, %H:%M')} local time.",
        f"You are running on {platform.node()} ({platform.system()} "
        f"{platform.release()}).",
    ]
    if machines:
        # Spell out the difference between paired-but-asleep and not-paired.
        # "offline" once got reported to Calle as "no machines are paired",
        # which is a different problem with a different fix.
        described = ", ".join(
            f"{m['name']} ({m['platform']}, "
            f"{'connected now' if m['online'] else 'paired but not connected right now'})"
            for m in machines)
        lines.append(f"Machines paired with you: {described}. This list is a snapshot "
                     "from the start of this turn — check the `machines` tool before "
                     "telling Calle what is reachable.")
    else:
        lines.append("No other machines are paired yet, so CODE dispatch has nowhere "
                     "to run until the Windows desktop connects.")
    operator_cfg = cfg.get("operator", {}) or {}
    lines.append(f"The operator screen is display {operator_cfg.get('display', ':99')} "
                 f"at {operator_cfg.get('width')}x{operator_cfg.get('height')}, with a "
                 "persistent Chrome profile that keeps Calle's logins.")
    return "\n".join(lines)


def identity_block(agent: dict) -> str:
    """Who this particular agent is.

    Every chat is its own Director, so the name and the instructions Calle
    wrote for it are part of what it knows about itself — it can be asked
    "what are your instructions?" and answer honestly.
    """
    name = str(agent.get("name") or "Director")
    lines = [f"You are **{name}**."]
    subtitle = str(agent.get("subtitle") or "").strip()
    if subtitle:
        lines.append(f"Calle describes you as: {subtitle}")
    routines = store.list_routines(agent_id=str(agent.get("id") or ""),
                                   include_disabled=False)
    if routines:
        described = "; ".join(
            f"{row['name']} ({routines_mod.describe(row['schedule'])}, next "
            f"{routines_mod.humanize_next(row['next_run'])})" for row in routines[:8])
        lines.append(f"You have these scheduled: {described}.")
    return "\n".join(lines)


def system_prompt(agent: dict, settings: dict[str, Any] | None = None) -> str:
    custom = str(agent.get("system_prompt") or "").strip()
    parts = [BASE_PROMPT.strip(), identity_block(agent)]

    # An agent Calle made himself is a Director too; his own instructions come
    # after the coordinator role rather than replacing it.
    if str(agent.get("kind") or "") == "custom":
        parts.append(DIRECTOR_PROMPT.strip())
        if custom:
            parts.append(f"Calle's instructions for you, which you should follow and "
                         f"may quote if he asks what they are:\n{custom}")
    elif custom:
        parts.append(custom)

    environment = environment_block(settings)
    if environment:
        parts.append(f"Context:\n{environment}")
    remembered = memory_tools.memory_block()
    if remembered:
        parts.append(remembered)
    return "\n\n".join(part for part in parts if part)
