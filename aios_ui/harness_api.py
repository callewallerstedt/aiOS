"""Backend for the HARNESS page: what the CODE agent actually is.

Everything here is read out of `code_jobs` at request time rather than written
down. That is the whole point. A hand-maintained page describing the harness
would be wrong within a week -- someone adds a tool, raises a limit, swaps the
reviewer model, and the documentation quietly starts lying. Ask the module
instead and the page cannot drift: the tool list *is* the schema the model is
given, the limits *are* the constants the loop reads.

The prose that cannot be introspected -- what a part is *for* -- lives here in
one place, next to the value it explains.
"""

from __future__ import annotations

from typing import Any

import code_jobs

# What each provider is, in one line each. Everything else about them (default
# model, readiness) is asked of code_jobs.
PROVIDER_NOTES: dict[str, dict[str, str]] = {
    "codex": {
        "label": "Codex",
        "runs": "OpenAI's Codex CLI, driven over JSON-RPC on stdin/stdout (`codex app-server`).",
        "tools": "own",
        "detail": "aiOS starts or resumes a thread and streams the turn back. Approvals and "
                  "sandbox permissions are auto-accepted; a request for user input pauses the "
                  "job at `waiting_user` instead. Models are discovered live from the CLI.",
    },
    "claude": {
        "label": "Claude Code",
        "runs": "Anthropic's Claude Code CLI as a subprocess, streaming JSON.",
        "tools": "own",
        "detail": "Launched with `--permission-mode bypassPermissions`, so it does not stop to "
                  "ask. Its own tool calls are translated into aiOS activity cards. Cost comes "
                  "back from the CLI rather than being estimated.",
    },
    "cursor": {
        "label": "Cursor Agent",
        "runs": "`cursor-agent` inside WSL, streaming JSON.",
        "tools": "own",
        "detail": "Runs with `--force --trust`. The model list is whatever the CLI reports, "
                  "filtered to a shortlist so the picker stays usable.",
    },
    "ollama": {
        "label": "Ollama",
        "runs": "A local model over the Ollama API, in this process.",
        "tools": "aios",
        "detail": "This is aiOS's own agent loop: aiOS sends the tools, executes the calls, and "
                  "feeds the results back. Nothing leaves the machine.",
    },
    "openrouter": {
        "label": "OpenRouter",
        "runs": "A hosted model over the OpenRouter API, in this process.",
        "tools": "aios",
        "detail": "The same aiOS agent loop as Ollama, against any model OpenRouter serves. "
                  "This is the path the benchmark measures by default.",
    },
}

# Which part of the job each tool belongs to. The description shown on the page
# is the real one from the schema, so only the grouping is editorial.
TOOL_GROUPS: dict[str, str] = {
    "list_dir": "explore",
    "find_files": "explore",
    "repo_map": "explore",
    "find_symbol": "explore",
    "search_text": "explore",
    "outline_file": "read",
    "read_file": "read",
    "edit_file": "change",
    "write_file": "change",
    "list_checkpoints": "change",
    "restore_checkpoint": "change",
    "run_shell": "run",
    "spawn_agent": "delegate",
    "update_plan": "steer",
    "ask_user": "steer",
    "fetch_url": "outside",
    "web_search": "outside",
}

GROUP_LABELS: dict[str, str] = {
    "explore": "Find things",
    "read": "Read things",
    "change": "Change things",
    "run": "Run things",
    "delegate": "Delegate",
    "steer": "Steer the job",
    "outside": "Reach outside",
}

# The three model calls that are not the coding agent itself.
def _secondary_models() -> list[dict]:
    return [
        {
            "id": "reviewer",
            "name": "The reviewer",
            "model": code_jobs.review_model_default(),
            "enabled": code_jobs.review_enabled(),
            "when": "After completed Planned and Distributed turns; Direct turns skip it.",
            "sees": "The brief and the net session-owned diff, never the agent's own summary or "
                    "discarded scratch files. It may "
                    "inspect project files with read/search tools when the diff does not provide "
                    "enough evidence.",
            "does": "Starts from the captured diff and only inspects a named missing fact. Low or "
                    "fast review uses no extra reasoning; explicit medium and higher are preserved. "
                    "It returns pass or concerns with concrete findings and unmet requirements.",
            "affects": "Adaptive and report-only. A 'concerns' verdict is posted into the "
                       "session and stored on the job; it does not reopen the work. Its "
                       "tokens are folded into the session's cost.",
            "limit": f"diff truncated at {code_jobs.REVIEW_MAX_DIFF_CHARS:,} characters; "
                     f"up to 6 tool/model rounds with {len(code_jobs.REVIEW_TOOL_NAMES)} tools",
        },
        {
            "id": "subagent",
            "name": "Subagents",
            "model": code_jobs.subagent_model_default(),
            "enabled": True,
            "when": "Whenever the agent calls `spawn_agent`.",
            "sees": "Only the objective it was given, and a read-only slice of the tools.",
            "does": "Explores and reports back in one message. It cannot edit, run shell "
                    "commands, or spawn further agents, and it cannot nest more than one deep.",
            "affects": "Its report comes back as the tool result. Its tokens are counted "
                       "against the parent session.",
            "limit": f"{code_jobs.SUBAGENT_MAX_ROUNDS} rounds, "
                     f"{len(code_jobs.SUBAGENT_TOOLS)} read-only tools",
        },
        {
            "id": "titler",
            "name": "The titler",
            "model": code_jobs.title_model_default(),
            "enabled": True,
            "when": "Once, when a session is created.",
            "sees": "The provider, the project name, and the first 400 characters of the brief.",
            "does": "Writes the short session title you see in the list.",
            "affects": "Cosmetic. If it fails, the title falls back to the brief's first line.",
            "limit": "one call per session",
        },
    ]


def _tools() -> list[dict]:
    """The live tool schema, exactly as the model receives it."""
    rows = []
    for entry in code_jobs.CodeJob._local_tool_schema():
        function = entry.get("function") or {}
        name = str(function.get("name") or "")
        parameters = (function.get("parameters") or {}).get("properties") or {}
        required = set((function.get("parameters") or {}).get("required") or [])
        rows.append({
            "name": name,
            "description": str(function.get("description") or ""),
            "group": TOOL_GROUPS.get(name, "explore"),
            "arguments": [
                {"name": key, "type": str((spec or {}).get("type") or "any"),
                 "required": key in required}
                for key, spec in parameters.items()
            ],
            # Read-only tools are batched and run together; anything that can
            # change the repository runs one at a time, in order.
            "parallel": name in code_jobs.PARALLEL_SAFE_TOOLS,
            "subagent": name in code_jobs.SUBAGENT_TOOLS,
        })
    return rows


def _providers() -> list[dict]:
    rows = []
    for provider in code_jobs.PROVIDERS:
        note = PROVIDER_NOTES.get(provider, {})
        rows.append({
            "id": provider,
            "label": note.get("label") or provider.title(),
            "runs": note.get("runs") or "",
            "detail": note.get("detail") or "",
            "tools": note.get("tools") or "own",
            "default_model": code_jobs.DEFAULT_MODELS.get(provider, ""),
        })
    return rows


def _rounds(value: int) -> str:
    return "unlimited" if not value else f"{value} rounds"


def _limits() -> list[dict]:
    """The numbers the loop actually reads, with what running into one feels like."""
    return [
        {"name": "Turn timeout", "value": f"{code_jobs.TURN_TIMEOUT_SECONDS // 3600}h",
         "detail": "One turn is abandoned after this. It is deliberately long: a turn that "
                   "is genuinely working looks the same as one that is stuck."},
        {"name": "Stall warning", "value": f"{code_jobs.SOFT_WARNING_SECONDS // 60}m",
         "detail": f"A quiet turn posts a warning at this point and again every "
                   f"{code_jobs.SOFT_WARNING_REPEAT_SECONDS // 60} minutes. It does not stop "
                   f"anything; it exists so a wedged run is visible."},
        {"name": "Tool rounds", "value": _rounds(code_jobs.OPENROUTER_MAX_TOOL_ROUNDS),
         "detail": "How many times the loop will hand tool results back to the model in one "
                   "turn. Unlimited by default: the agent stops when it is done, not when it "
                   "runs out of turns."},
        {"name": "Parallel tools", "value": str(code_jobs.MAX_PARALLEL_TOOLS),
         "detail": "Read-only calls in the same round are executed together, up to this many. "
                   "Anything that writes runs sequentially."},
        {"name": "Context budget", "value": "token-aware",
         "detail": "The selected model window is split into working context and a hard output "
                   "reserve. Serialized messages and tool schemas are estimated as tokens; old "
                   "tool receipts are compacted before a provider request can overflow."},
        {"name": "Subagent rounds", "value": (
            "unlimited" if code_jobs.SUBAGENT_MAX_ROUNDS <= 0 else str(code_jobs.SUBAGENT_MAX_ROUNDS)
        ),
         "detail": "A scout reports as soon as its objective is answered. This cap prevents a "
                   "read-only branch from consuming the parent turn indefinitely."},
    ]


def _context() -> list[dict]:
    return [
        {"name": "Anti-hallucination rules", "detail":
            "A standing instruction not to state an API, flag or path it has not seen, and to "
            "verify against the repository or the docs before reaching for one."},
        {"name": "Reasoning rules", "detail":
            "How to plan, when to stop exploring, and when to ask instead of guessing."},
        {"name": "AGENTS.md", "detail":
            "Walked from the git root down to the project folder and injected in full. "
            "`AGENTS.override.md` wins if it exists. CLAUDE.md is picked up too."},
        {"name": "Nested instructions", "detail":
            "Reading a file in a subtree pulls in that subtree's own AGENTS.md, lazily, so "
            "folder-local rules arrive when they become relevant."},
        {"name": "Self-location", "detail":
            "When the project is aiOS itself, the agent is told where its own parts live so "
            "it can work on the thing it is running inside."},
        {"name": "Compaction", "detail":
            "Over budget, old tool outputs are replaced by placeholders first; if that is not "
            "enough, older protocol groups become one continuity message while recent complete "
            "assistant/tool groups are retained. Full receipts remain in the event log."},
    ]


def _telemetry() -> list[dict]:
    """Per-session fields exposed beside the live CODE transcript."""
    return [
        {
            "id": "strategy",
            "name": "Task strategy",
            "fields": "name · reasons · score · role gates · working context",
            "detail": "Why this turn is direct, planned, or distributed, and which roles it may use.",
        },
        {
            "id": "profile",
            "name": "Model profile",
            "fields": "model · edit mode · tool schema · context mode",
            "detail": "The exact model-facing edit, schema, and context policy selected for this turn.",
        },
        {
            "id": "verification",
            "name": "Verification",
            "fields": "state · generation · evidence · completion blocks",
            "detail": "Evidence only clears the completion gate when it belongs to the current edit generation.",
        },
        {
            "id": "progress",
            "name": "Progress",
            "fields": "state · productive calls · no-progress calls · redirects",
            "detail": "Separates useful work from repeated empty searches, reads, or blocked tool calls.",
        },
        {
            "id": "tokens",
            "name": "Token context",
            "fields": "estimated · actual · cached · reserve · compactions · artifacts",
            "detail": "Provider-reported tokens win; estimates are labelled and output reserve stays visible.",
        },
    ]


def _lifecycle() -> list[dict]:
    return [
        {"name": "queued", "kind": "active", "detail": "Work accepted, waiting for the turn lock."},
        {"name": "running", "kind": "active", "detail": "A turn is in flight."},
        {"name": "waiting_user", "kind": "active",
         "detail": "The agent asked a question and is holding the turn open for the answer."},
        {"name": "completed", "kind": "done",
         "detail": "The coder passed its completion gate and any enabled reviewer finished; only then is terminal completion published."},
        {"name": "incomplete", "kind": "attention",
         "detail": "A configured safety stop ended the turn before completion; work is preserved and never reviewed as finished."},
        {"name": "failed", "kind": "done", "detail": "The provider errored out."},
        {"name": "stopped", "kind": "done", "detail": "You stopped it."},
        {"name": "interrupted", "kind": "done",
         "detail": "The turn was cut off -- by an interrupt, or by aiOS restarting mid-turn."},
    ]


def _flow() -> list[dict]:
    return [
        {"step": "Brief", "detail":
            "Your text, the project folder, an exact model and a reasoning level. A session is "
            "created and the work is queued."},
        {"step": "Context", "detail":
            "The system prompt, the project's AGENTS.md, and the conversation so far are "
            "assembled and compacted to fit."},
        {"step": "Turn", "detail":
            "The model streams. Text becomes the transcript; tool calls are collected."},
        {"step": "Tools", "detail":
            "Read-only calls run in parallel, writes run in order, results go back to the "
            "model, and the loop repeats until the model stops calling tools."},
        {"step": "Diff", "detail":
            "Every edit is recorded against a baseline, so the session knows exactly which "
            "files and lines it changed."},
        {"step": "Verify", "detail":
            "Checks are tied to the latest edit generation. Stale or failing evidence blocks "
            "completion for a bounded retry, then the session ends incomplete."},
        {"step": "Review", "detail":
            "For Planned and Distributed work, a second model reads the brief and net session diff "
            "with a frozen read-only tool set. Direct work skips this stage; when it runs, terminal "
            "completion is published only after the report finishes."},
    ]


def dispatch(route: str, method: str, params: dict, data: dict) -> Any:
    """Return None for anything this module does not own."""
    if route == "/api/harness/meta" and method == "GET":
        return {
            "ok": True,
            "flow": _flow(),
            "providers": _providers(),
            "tools": _tools(),
            "groups": [{"id": key, "label": label} for key, label in GROUP_LABELS.items()],
            "models": _secondary_models(),
            "limits": _limits(),
            "context": _context(),
            "telemetry": _telemetry(),
            "lifecycle": _lifecycle(),
            "jobs_dir": str(code_jobs.JOBS_DIR),
        }

    # Readiness costs a subprocess per CLI provider, so the page renders first
    # and asks for this afterwards.
    if route == "/api/harness/status" and method == "GET":
        rows = []
        for provider in code_jobs.PROVIDERS:
            try:
                ready, message = code_jobs.provider_status(provider)
            except Exception as exc:
                ready, message = False, str(exc)
            rows.append({"id": provider, "ready": bool(ready), "message": str(message)})
        return {"ok": True, "providers": rows}

    return None
