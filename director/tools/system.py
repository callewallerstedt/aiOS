"""Shell and filesystem tools on the Director box itself."""
from __future__ import annotations

import asyncio
import os
import pathlib
import re
import shlex

from . import ToolContext, ToolResult, tool

MAX_OUTPUT = 6000
DEFAULT_TIMEOUT = 120

# Commands that reach past this box or destroy data. They still run — Director
# is meant to be useful — but only behind an approval card.
DESTRUCTIVE_HINTS = (
    "rm ", "rmdir", "mkfs", "dd ", "shutdown", "reboot", "systemctl stop",
    "systemctl disable", "docker rm", "docker rmi", "docker stop", "kill ",
    "pkill", "truncate", "chown", "chmod 777", "apt remove", "apt purge",
    "apt install", "snap remove", "pip uninstall", "git push", "git reset --hard",
    "curl", "wget", "ssh ", "scp ",
)

# The pixel operator owns all GUI input. Keeping this boundary in the tool
# implementation prevents a coordinator from silently bypassing the operator's
# screenshot/reason/action loop when a GUI task becomes difficult.
GUI_AUTOMATION_HINTS = re.compile(
    r"(?<![\w.-])(xdotool|wmctrl|xte|ydotool|dotool|pyautogui)(?![\w.-])",
    re.IGNORECASE,
)


def looks_destructive(command: str) -> bool:
    lowered = f" {str(command or '').lower().strip()} "
    return any(hint in lowered for hint in DESTRUCTIVE_HINTS)


def looks_like_gui_automation(command: str) -> bool:
    return bool(GUI_AUTOMATION_HINTS.search(str(command or "")))


def _clip(text: str, limit: int = MAX_OUTPUT) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2:]
    return f"{head}\n… [{len(text) - limit} characters trimmed] …\n{tail}"


@tool(
    "shell",
    "Run a shell command on the Director Linux box and return its output. "
    "Use for scripts, services, package state and anything the box can answer "
    "faster than a browser. Never use it to drive the desktop or browser GUI; "
    "all GUI input must use the operator tool. Destructive commands need the "
    "user's approval.",
    {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The command line to run (bash)."},
            "cwd": {"type": "string", "description": "Working directory. Defaults to $HOME."},
            "timeout": {"type": "integer", "description": "Seconds before the command is killed (default 120)."},
        },
        "required": ["command"],
    },
    destructive=True,
    approval_summary=lambda args: f"Run on Linux: {str(args.get('command'))[:120]}",
)
async def shell(ctx: ToolContext, command: str = "", cwd: str = "",
                timeout: int = DEFAULT_TIMEOUT) -> ToolResult:
    command = str(command or "").strip()
    if not command:
        return ToolResult(error="no command given")
    if looks_like_gui_automation(command):
        return ToolResult(
            error=("GUI automation is not available through shell. Use the operator "
                   "tool so clicks are reasoned from the current screenshot."),
            card={"title": "shell", "preview": command[:90],
                  "meta": "use operator", "tone": "danger"},
        )
    workdir = pathlib.Path(cwd).expanduser() if cwd else pathlib.Path.home()
    if not workdir.is_dir():
        return ToolResult(error=f"no such directory: {workdir}")

    proc = await asyncio.create_subprocess_shell(
        command, cwd=str(workdir),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "TERM": "dumb"},
    )
    try:
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=max(1, int(timeout or DEFAULT_TIMEOUT)))
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return ToolResult(
            error=f"command timed out after {timeout}s",
            card={"title": "shell", "preview": command[:80], "meta": "timeout", "tone": "danger"},
        )
    text = _clip(raw.decode("utf-8", errors="replace").strip())
    code = proc.returncode or 0
    first = text.splitlines()[0][:80] if text else ""
    return ToolResult(
        output=f"exit {code}\n{text}" if text else f"exit {code}",
        card={"title": "shell", "preview": command[:90],
              "meta": f"exit {code}" + (f" · {first}" if first else ""),
              "tone": "ok" if code == 0 else "danger",
              "body": text},
    )


@tool(
    "read_file",
    "Read a text file from the Director Linux box.",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "max_bytes": {"type": "integer", "description": "Default 60000."},
        },
        "required": ["path"],
    },
)
async def read_file(ctx: ToolContext, path: str = "", max_bytes: int = 60000) -> ToolResult:
    target = pathlib.Path(str(path or "")).expanduser()
    if not target.is_file():
        return ToolResult(error=f"no such file: {target}")
    try:
        data = target.read_bytes()[: max(1024, int(max_bytes or 60000))]
    except OSError as exc:
        return ToolResult(error=f"cannot read {target}: {exc}")
    text = data.decode("utf-8", errors="replace")
    return ToolResult(
        output=text,
        card={"title": "read", "preview": str(target),
              "meta": f"{len(text)} chars", "tone": "ok"},
    )


@tool(
    "write_file",
    "Write a text file on the Director Linux box, creating parent directories.",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    },
    destructive=True,
    approval_summary=lambda args: f"Write file on Linux: {args.get('path')}",
)
async def write_file(ctx: ToolContext, path: str = "", content: str = "") -> ToolResult:
    target = pathlib.Path(str(path or "")).expanduser()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content or ""), encoding="utf-8")
    except OSError as exc:
        return ToolResult(error=f"cannot write {target}: {exc}")
    return ToolResult(
        output=f"wrote {len(content or '')} characters to {target}",
        card={"title": "write", "preview": str(target),
              "meta": f"{len(content or '')} chars", "tone": "ok"},
    )


@tool(
    "list_dir",
    "List a directory on the Director Linux box.",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "limit": {"type": "integer", "description": "Default 200 entries."},
        },
        "required": ["path"],
    },
)
async def list_dir(ctx: ToolContext, path: str = "", limit: int = 200) -> ToolResult:
    target = pathlib.Path(str(path or "")).expanduser()
    if not target.is_dir():
        return ToolResult(error=f"no such directory: {target}")
    rows = []
    for entry in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if len(rows) >= max(1, int(limit or 200)):
            break
        try:
            size = entry.stat().st_size if entry.is_file() else 0
        except OSError:
            size = 0
        rows.append(f"{'d' if entry.is_dir() else '-'} {entry.name}" + (f"  {size}" if size else ""))
    listing = "\n".join(rows) or "(empty)"
    return ToolResult(
        output=listing,
        card={"title": "ls", "preview": str(target),
              "meta": f"{len(rows)} entries", "tone": "ok", "body": listing},
    )


@tool(
    "processes",
    "Show what is running on the Director Linux box (top processes by memory).",
    {"type": "object", "properties": {"limit": {"type": "integer"}}},
)
async def processes(ctx: ToolContext, limit: int = 15) -> ToolResult:
    count = max(1, min(int(limit or 15), 40))
    command = f"ps -eo pid,pmem,pcpu,comm --sort=-pmem | head -n {count + 1}"
    proc = await asyncio.create_subprocess_shell(
        command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    raw, _ = await proc.communicate()
    text = raw.decode("utf-8", errors="replace").strip()
    return ToolResult(
        output=text,
        card={"title": "processes", "preview": f"top {count} by memory",
              "meta": "", "tone": "ok", "body": text},
    )
