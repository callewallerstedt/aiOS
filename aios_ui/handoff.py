"""Handoff: pull the latest Claude Code / Codex session into a compact brief.

The CODE agent can pick up where another tool left off. For each tool we read
the newest session transcript on disk and reduce it to the parts that matter
for a handoff:

  * user messages  -- the intent and requirements
  * assistant text -- what was already decided / done
  * files edited   -- the concrete state (paths only, not diffs)

Verbose thinking traces, raw tool internals and token streams are dropped.

Formats (verified against real transcripts on disk):

  Claude Code  ~/.claude/projects/<slug>/<session-id>.jsonl
    {"type":"user","message":{"role":"user","content": str | [{"type":"text","text"}]}}
    {"type":"assistant","message":{"content":[{"type":"text","text"} | {"type":"tool_use","name","input":{"file_path"}}]}}

  Codex  ~/.codex/sessions/<yyyy>/<mm>/<dd>/rollout-<ts>-<id>.jsonl
    {"type":"response_item","payload":{"type":"message","role","content":[{"type":"input_text","text"}]}}
    {"type":"response_item","payload":{"type":"custom_tool_call","name","input": str}}
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# Tool names that indicate a file was written/edited (as opposed to read).
_EDIT_TOOLS = {
    "Edit",
    "Write",
    "MultiEdit",
    "NotebookEdit",
    "edit",
    "write",
    "apply_patch",
    "apply_patch_edit",
}


@dataclass
class Session:
    tool: str
    title: str
    path: str
    mtime: float
    user_msgs: list = field(default_factory=list)
    assistant_msgs: list = field(default_factory=list)
    files_edited: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "title": self.title,
            "path": self.path,
            "mtime": self.mtime,
            "user_msgs": self.user_msgs,
            "assistant_msgs": self.assistant_msgs,
            "files_edited": self.files_edited,
        }


def _home() -> Path:
    return Path(os.path.expanduser("~"))


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------

def _claude_text(content) -> str:
    """Normalise a Claude message.content (str or list of blocks) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return ""


def _claude_session(path: Path) -> Session:
    title = path.parent.name
    sess = Session(tool="claude", title=title, path=str(path), mtime=path.stat().st_mtime)
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return sess
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        typ = rec.get("type")
        if typ == "user":
            msg = rec.get("message") or {}
            text = _claude_text(msg.get("content"))
            if text.strip():
                sess.user_msgs.append(text.strip())
        elif typ == "assistant":
            msg = rec.get("message") or {}
            text = _claude_text(msg.get("content"))
            if text.strip():
                sess.assistant_msgs.append(text.strip())
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and block.get("name") in _EDIT_TOOLS:
                    fp = (block.get("input") or {}).get("file_path")
                    if fp and fp not in sess.files_edited:
                        sess.files_edited.append(fp)
    return sess


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------

def _codex_text(content) -> str:
    """Normalise a Codex content list ([{"type":"input_text","text"}]) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in ("input_text", "output_text"):
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return ""


def _codex_session(path: Path) -> Session:
    title = path.name
    sess = Session(tool="codex", title=title, path=str(path), mtime=path.stat().st_mtime)
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return sess
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") != "response_item":
            continue
        payload = rec.get("payload") or {}
        ptype = payload.get("type")
        if ptype == "message":
            role = payload.get("role")
            text = _codex_text(payload.get("content"))
            if not text.strip():
                continue
            if role == "user":
                sess.user_msgs.append(text.strip())
            elif role == "assistant":
                sess.assistant_msgs.append(text.strip())
        elif ptype == "custom_tool_call":
            name = payload.get("name")
            if name in _EDIT_TOOLS:
                inp = payload.get("input")
                if isinstance(inp, str):
                    try:
                        inp = json.loads(inp)
                    except json.JSONDecodeError:
                        inp = {}
                if isinstance(inp, dict):
                    fp = inp.get("file_path") or inp.get("path")
                    if fp and fp not in sess.files_edited:
                        sess.files_edited.append(fp)
    return sess


# ---------------------------------------------------------------------------
# Discovery + rendering
# ---------------------------------------------------------------------------

def _latest_claude() -> Session | None:
    base = _home() / ".claude" / "projects"
    if not base.is_dir():
        return None
    files = [p for p in base.glob("*/*.jsonl")]
    if not files:
        return None
    newest = max(files, key=lambda p: p.stat().st_mtime)
    return _claude_session(newest)


def _latest_codex() -> Session | None:
    base = _home() / ".codex" / "sessions"
    if not base.is_dir():
        return None
    files = [p for p in base.rglob("*.jsonl")]
    if not files:
        return None
    newest = max(files, key=lambda p: p.stat().st_mtime)
    return _codex_session(newest)


def list_sessions() -> dict:
    """Return the latest Claude and Codex sessions (metadata only)."""
    out = {"claude": None, "codex": None}
    for tool, fn in (("claude", _latest_claude), ("codex", _latest_codex)):
        try:
            sess = fn()
        except Exception:
            sess = None
        if sess is not None:
            out[tool] = {
                "tool": sess.tool,
                "title": sess.title,
                "path": sess.path,
                "mtime": sess.mtime,
                "user_count": len(sess.user_msgs),
                "assistant_count": len(sess.assistant_msgs),
                "files_edited": sess.files_edited,
            }
    return out


def _render(sess: Session) -> str:
    """Render a session as a compact handoff brief."""
    when = datetime.fromtimestamp(sess.mtime).strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# Handoff from {sess.tool} ({sess.title})",
        f"Session: {sess.path}",
        f"Updated: {when}",
        "",
    ]
    if sess.files_edited:
        lines.append("## Files edited")
        for fp in sess.files_edited:
            lines.append(f"- {fp}")
        lines.append("")
    if sess.user_msgs:
        lines.append("## User messages")
        for i, m in enumerate(sess.user_msgs, 1):
            lines.append(f"### User {i}")
            lines.append(m)
            lines.append("")
    if sess.assistant_msgs:
        lines.append("## Assistant replies")
        for i, m in enumerate(sess.assistant_msgs, 1):
            lines.append(f"### Assistant {i}")
            lines.append(m)
            lines.append("")
    return "\n".join(lines)


def read_session(tool: str, path: str, full: bool = False) -> dict:
    """Read a specific session and return it as a handoff brief."""
    p = Path(path)
    if tool == "claude":
        sess = _claude_session(p)
    elif tool == "codex":
        sess = _codex_session(p)
    else:
        return {"error": f"unknown tool: {tool}"}
    return {"tool": sess.tool, "title": sess.title, "brief": _render(sess)}