"""Persistent Claude / Codex coding sessions driven from the phone bridge.

Each session lives in cli_sessions/<sid>/ with:
  meta.json    - session metadata (cli, project, native resume id, status, ...)
  events.jsonl - normalized event log (user / assistant / tool / result / error)

A session maps 1:1 to a native CLI conversation:
  - claude: `claude -p --output-format stream-json --resume <native_id>`
  - codex:  `codex exec resume <native_id> --json`

Messages within a session are serialized so the underlying CLI conversation
stays one continuous thread.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

from pc_cli_runner import find_claude, find_codex, resolve_project, DEFAULT_PROJECTS_ROOT

REPO_ROOT = Path(__file__).resolve().parent
SESSIONS_DIR = REPO_ROOT / "cli_sessions"
SESSIONS_DIR.mkdir(exist_ok=True)

DEFAULT_CODEX_MODEL = os.environ.get("AIOS_CODEX_MODEL", "gpt-5.5")
TURN_TIMEOUT_SECONDS = int(os.environ.get("AIOS_CLI_TURN_TIMEOUT", "3600"))

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_SESSIONS_LOCK = threading.Lock()
_SESSIONS: dict[str, "CodingSession"] = {}


def _now() -> float:
    return time.time()


def _safe_title(text: str, limit: int = 64) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


class CodingSession:
    def __init__(self, sid: str):
        self.sid = sid
        self.dir = SESSIONS_DIR / sid
        self.meta_path = self.dir / "meta.json"
        self.events_path = self.dir / "events.jsonl"
        self.turn_lock = threading.Lock()      # serializes turns
        self.write_lock = threading.Lock()     # protects meta + event file writes
        self.process: subprocess.Popen | None = None
        self.stop_requested = False
        self.queued = 0

    # ---------- persistence ----------

    def load_meta(self) -> dict:
        try:
            return json.loads(self.meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def save_meta(self, **updates) -> dict:
        with self.write_lock:
            meta = self.load_meta()
            meta.update(updates)
            meta["updated_at"] = _now()
            tmp = self.meta_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.meta_path)
            return meta

    def append_event(self, role: str, text: str = "", **extra) -> None:
        event = {"ts": round(_now(), 3), "role": role, "text": text}
        event.update(extra)
        line = json.dumps(event, ensure_ascii=False)
        with self.write_lock:
            with self.events_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    # ---------- turn execution ----------

    def send(self, text: str) -> dict:
        """Queue a message; the turn runs on a background thread."""
        text = str(text or "").strip()
        if not text:
            return {"ok": False, "error": "text required"}
        meta = self.load_meta()
        if not meta:
            return {"ok": False, "error": "unknown session"}
        if not meta.get("title"):
            self.save_meta(title=_safe_title(text))
        self.append_event("user", text)
        self.queued += 1
        threading.Thread(target=self._run_turn_locked, args=(text,), daemon=True).start()
        return {"ok": True, "queued": self.queued > 1}

    def stop(self) -> dict:
        self.stop_requested = True
        proc = self.process
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
            return {"ok": True, "stopped": True}
        return {"ok": True, "stopped": False}

    def _run_turn_locked(self, text: str) -> None:
        with self.turn_lock:
            self.queued = max(0, self.queued - 1)
            if self.stop_requested:
                # A stop that arrived while queued cancels pending turns.
                self.stop_requested = False
                self.append_event("status", "cancelled")
                return
            try:
                self._run_turn(text)
            except Exception as exc:  # never let a turn thread die silently
                self.append_event("error", f"internal error: {exc}")
                self.save_meta(status="idle")

    def _build_command(self, meta: dict) -> tuple[list[str], str]:
        cli = meta.get("cli", "claude")
        model = str(meta.get("model") or "").strip()
        native = str(meta.get("native_session_id") or "").strip()
        if cli == "claude":
            claude = find_claude()
            if not claude:
                raise RuntimeError("Claude CLI not found. Install with: npm i -g @anthropic-ai/claude-code")
            cmd = ["cmd.exe", "/d", "/c", claude] if os.name == "nt" else [claude]
            cmd += ["-p", "--output-format", "stream-json", "--verbose",
                    "--dangerously-skip-permissions"]
            if model:
                cmd += ["--model", model]
            effort = str(meta.get("reasoning") or "").strip().lower()
            if effort and effort not in ("none", "auto"):
                if effort == "ultracode":
                    cmd += ["--settings", '{"ultracode":true}']
                else:
                    cmd += ["--effort", effort]
            if native:
                cmd += ["--resume", native]
            return cmd, "claude"
        codex = find_codex()
        if not codex:
            raise RuntimeError("Codex CLI not found. Open Codex once on the PC or set AIOS_CODEX_PATH.")
        cmd = [codex, "exec"]
        if native:
            cmd += ["resume", native]
        cmd += ["--json", "--skip-git-repo-check", "--sandbox", "workspace-write"]
        cmd += ["-m", model or DEFAULT_CODEX_MODEL]
        reasoning = str(meta.get("reasoning") or "").strip().lower()
        if reasoning and reasoning != "none":
            cmd += ["-c", f'model_reasoning_effort="{reasoning}"']
        cmd += ["-"]  # read prompt from stdin
        return cmd, "codex"

    def _run_turn(self, text: str) -> None:
        meta = self.load_meta()
        cmd, cli = self._build_command(meta)
        project = Path(meta.get("project") or DEFAULT_PROJECTS_ROOT)
        project.mkdir(parents=True, exist_ok=True)
        self.save_meta(status="running")
        self.append_event("status", "working")
        env = dict(os.environ)
        env.setdefault("NO_COLOR", "1")
        proc = subprocess.Popen(
            cmd,
            cwd=str(project),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=_CREATE_NO_WINDOW,
            env=env,
        )
        self.process = proc
        stderr_buf: list[bytes] = []
        stderr_thread = threading.Thread(
            target=lambda: stderr_buf.append(proc.stderr.read() or b""), daemon=True)
        stderr_thread.start()
        killer = threading.Timer(TURN_TIMEOUT_SECONDS, lambda: proc.poll() is None and proc.kill())
        killer.start()
        try:
            proc.stdin.write(text.encode("utf-8"))
            proc.stdin.close()
        except OSError:
            pass
        got_result = False
        try:
            for raw in proc.stdout:
                line = raw.decode("utf-8", "ignore").strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if cli == "claude":
                    got_result = self._handle_claude_event(payload) or got_result
                else:
                    got_result = self._handle_codex_event(payload) or got_result
        finally:
            proc.wait()
            killer.cancel()
            stderr_thread.join(timeout=2)
            self.process = None
        code = proc.returncode
        if self.stop_requested:
            self.stop_requested = False
            self.append_event("status", "stopped")
        elif code != 0 and not got_result:
            err = (stderr_buf[0] if stderr_buf else b"").decode("utf-8", "ignore").strip()
            self.append_event("error", err[-2000:] or f"{cli} exited with code {code}")
        elif not got_result:
            self.append_event("status", "done (no summary)")
        self.save_meta(status="idle")

    # ---------- event normalization ----------

    def _handle_claude_event(self, ev: dict) -> bool:
        etype = ev.get("type")
        if etype == "system" and ev.get("subtype") == "init":
            sid = ev.get("session_id")
            if sid:
                self.save_meta(native_session_id=sid)
            return False
        if etype == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                btype = block.get("type")
                if btype == "text" and block.get("text", "").strip():
                    self.append_event("assistant", block["text"])
                elif btype == "tool_use":
                    self.append_event("tool", _describe_claude_tool(block))
            return False
        if etype == "result":
            sid = ev.get("session_id")
            if sid:
                self.save_meta(native_session_id=sid)
            text = str(ev.get("result") or "").strip()
            is_error = bool(ev.get("is_error"))
            extra = {}
            if ev.get("duration_ms"):
                extra["duration_ms"] = ev["duration_ms"]
            if ev.get("total_cost_usd") is not None:
                extra["cost_usd"] = round(float(ev["total_cost_usd"]), 4)
            if is_error:
                self.append_event("error", text or "Claude reported an error", **extra)
            else:
                self.append_event("result", text, **extra)
                self.save_meta(last_summary=_safe_title(text, 200))
            return True
        return False

    def _handle_codex_event(self, ev: dict) -> bool:
        etype = str(ev.get("type") or "")
        # Modern `codex exec --json` shape: thread.started / item.* / turn.*
        if etype == "thread.started":
            tid = ev.get("thread_id")
            if tid:
                self.save_meta(native_session_id=tid)
            return False
        if etype in {"item.started", "item.completed", "item.updated"}:
            item = ev.get("item") or {}
            itype = item.get("item_type") or item.get("type") or ""
            if itype == "agent_message" and etype == "item.completed":
                text = str(item.get("text") or "").strip()
                if text:
                    self.append_event("assistant", text)
                    self.save_meta(last_summary=_safe_title(text, 200))
                return False
            if itype == "command_execution" and etype == "item.started":
                cmd = str(item.get("command") or "").strip()
                if cmd:
                    self.append_event("tool", f"$ {cmd}")
                return False
            if itype in {"file_change", "patch_apply"} and etype == "item.completed":
                changes = item.get("changes") or []
                if changes:
                    names = ", ".join(str(c.get("path", "")) for c in changes[:6])
                    self.append_event("tool", f"edited {names}")
                else:
                    self.append_event("tool", "applied file changes")
                return False
            if itype == "reasoning" and etype == "item.completed":
                text = _safe_title(str(item.get("text") or ""), 160)
                if text:
                    self.append_event("thinking", text)
                return False
            return False
        if etype == "turn.completed":
            self.append_event("result", "", usage=ev.get("usage") or {})
            return True
        if etype == "turn.failed":
            err = (ev.get("error") or {}).get("message") or "Codex turn failed"
            self.append_event("error", str(err))
            return True
        # Legacy shape: {"id": ..., "msg": {"type": ...}}
        msg = ev.get("msg")
        if isinstance(msg, dict):
            mtype = msg.get("type")
            if mtype == "session_configured" and msg.get("session_id"):
                self.save_meta(native_session_id=msg["session_id"])
            elif mtype == "agent_message" and msg.get("message"):
                self.append_event("assistant", str(msg["message"]))
                self.save_meta(last_summary=_safe_title(str(msg["message"]), 200))
            elif mtype == "exec_command_begin" and msg.get("command"):
                cmd = msg["command"]
                if isinstance(cmd, list):
                    cmd = " ".join(str(c) for c in cmd)
                self.append_event("tool", f"$ {cmd}")
            elif mtype == "patch_apply_begin":
                self.append_event("tool", "applying file changes")
            elif mtype == "task_complete":
                self.append_event("result", str(msg.get("last_agent_message") or ""))
                return True
            elif mtype == "error":
                self.append_event("error", str(msg.get("message") or "Codex error"))
                return True
        return False


def _describe_claude_tool(block: dict) -> str:
    name = block.get("name") or "tool"
    inp = block.get("input") or {}
    detail = ""
    for key in ("file_path", "path", "pattern", "command", "description", "prompt", "url", "query"):
        if inp.get(key):
            detail = str(inp[key])
            break
    detail = re.sub(r"\s+", " ", detail).strip()
    if len(detail) > 120:
        detail = detail[:120] + "…"
    return f"{name}: {detail}" if detail else name


# ---------- module-level API used by the server ----------

def _get_session(sid: str) -> CodingSession | None:
    sid = "".join(c for c in str(sid or "") if c.isalnum())
    if not sid:
        return None
    with _SESSIONS_LOCK:
        session = _SESSIONS.get(sid)
        if session is None:
            candidate = CodingSession(sid)
            if not candidate.meta_path.exists():
                return None
            _SESSIONS[sid] = candidate
            session = candidate
        return session


def create_session(cli: str, project: str = "", projects_root: str = "",
                   model: str = "", reasoning: str = "", title: str = "") -> dict:
    cli = str(cli or "claude").strip().lower()
    if cli not in {"claude", "codex"}:
        return {"ok": False, "error": f"unknown cli: {cli}"}
    if cli == "claude" and not find_claude():
        return {"ok": False, "error": "Claude CLI not found on the PC."}
    if cli == "codex" and not find_codex():
        return {"ok": False, "error": "Codex CLI not found on the PC."}
    project_path = resolve_project(projects_root or DEFAULT_PROJECTS_ROOT, project)
    try:
        project_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": f"cannot use project folder: {exc}"}
    sid = uuid.uuid4().hex[:12]
    session = CodingSession(sid)
    session.dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": sid,
        "cli": cli,
        "project": str(project_path),
        "project_name": project_path.name,
        "model": str(model or "").strip(),
        "reasoning": str(reasoning or "").strip(),
        "title": _safe_title(title),
        "native_session_id": "",
        "status": "idle",
        "created_at": _now(),
    }
    session.save_meta(**meta)
    session.events_path.touch()
    with _SESSIONS_LOCK:
        _SESSIONS[sid] = session
    return {"ok": True, "session": session.load_meta()}


def list_sessions(limit: int = 40) -> list[dict]:
    sessions = []
    try:
        dirs = [d for d in SESSIONS_DIR.iterdir() if d.is_dir()]
    except OSError:
        return []
    for d in dirs:
        meta_path = d / "meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        # A stale "running" from a previous server run means idle now.
        live = _SESSIONS.get(d.name)
        if meta.get("status") == "running" and (live is None or live.process is None):
            meta["status"] = "idle"
        sessions.append(meta)
    sessions.sort(key=lambda m: m.get("updated_at") or 0, reverse=True)
    return sessions[:limit]


def get_session_meta(sid: str) -> dict | None:
    session = _get_session(sid)
    return session.load_meta() if session else None


def send_message(sid: str, text: str) -> dict:
    session = _get_session(sid)
    if session is None:
        return {"ok": False, "error": "unknown session"}
    return session.send(text)


def stop_session(sid: str) -> dict:
    session = _get_session(sid)
    if session is None:
        return {"ok": False, "error": "unknown session"}
    return session.stop()


def delete_session(sid: str) -> dict:
    session = _get_session(sid)
    if session is None:
        return {"ok": False, "error": "unknown session"}
    session.stop()
    with _SESSIONS_LOCK:
        _SESSIONS.pop(session.sid, None)
    import shutil
    try:
        shutil.rmtree(session.dir)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True}


def read_events(sid: str, since: int = 0) -> dict:
    session = _get_session(sid)
    if session is None:
        return {"ok": False, "error": "unknown session", "events": [], "size": 0}
    events = []
    size = 0
    reset = False
    try:
        if session.events_path.exists():
            size = session.events_path.stat().st_size
            if since > size:
                since, reset = 0, True
            with session.events_path.open("rb") as fh:
                if since:
                    fh.seek(since)
                raw = fh.read()
            for line in raw.decode("utf-8", "ignore").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    meta = session.load_meta()
    if meta.get("status") == "running" and session.process is None and not session.queued:
        meta["status"] = "idle"
    return {"ok": True, "events": events, "size": size, "reset": reset, "meta": meta}


def events_file_for(sid: str) -> Path | None:
    session = _get_session(sid)
    return session.events_path if session else None
