"""Unified persistent Codex, Claude Code, and Cursor jobs for aiOS.

The voice agent, desktop CODE tab, phone UI, and web dashboard all use this
module.  Provider-specific stdout is normalized into one append-only event log
while native conversation ids are retained for follow-up turns.
"""
from __future__ import annotations

import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import code_handoff
from pc_cli_runner import find_claude, find_codex


ROOT = Path(__file__).resolve().parent
JOBS_DIR = Path(os.environ.get("AIOS_CODE_JOBS_DIR") or ROOT / "code_jobs")
CAPABILITIES_CACHE = JOBS_DIR / "capabilities.json"
CONFIG_PATH = ROOT / "helper_config.json"
TURN_TIMEOUT_SECONDS = int(os.environ.get("AIOS_CODE_TURN_TIMEOUT", "14400"))
SOFT_WARNING_SECONDS = int(os.environ.get("AIOS_CODE_SOFT_WARNING", "1800"))
SOFT_WARNING_REPEAT_SECONDS = int(os.environ.get("AIOS_CODE_SOFT_WARNING_REPEAT", "3600"))
MAX_ACTIVITY_STREAM_CHARS = int(os.environ.get("AIOS_CODE_ACTIVITY_STREAM_LIMIT", "600000"))
WSL_DISTRO = os.environ.get("AIOS_CURSOR_WSL_DISTRO", "Ubuntu-22.04")
CURSOR_AGENT = os.environ.get("AIOS_CURSOR_AGENT", "/home/dev/.local/bin/cursor-agent")
DEFAULT_MODELS = {
    "codex": "gpt-5.6-sol",
    "claude": "sonnet",
    "cursor": "auto",
}
PROVIDERS = ("codex", "claude", "cursor")
TERMINAL_STATES = {"completed", "failed", "interrupted", "stopped"}
ACTIVE_STATES = {"queued", "running", "waiting_user"}
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
CREATE_NEW_CONSOLE = 0x00000010 if os.name == "nt" else 0

JOBS_DIR.mkdir(exist_ok=True)

_REGISTRY_LOCK = threading.RLock()
_LIVE: dict[str, "CodeJob"] = {}
_CAPABILITIES_LOCK = threading.Lock()
_CAPABILITIES_MEMORY: dict[str, Any] | None = None
_CAPABILITIES_AT = 0.0


def _now() -> float:
    return round(time.time(), 3)


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _short(value: Any, limit: int = 180) -> str:
    text = _clean_text(value)
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"


def _provider_label(provider: str) -> str:
    return {"codex": "Codex", "claude": "Claude", "cursor": "Cursor"}.get(provider, provider.title())


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _provider_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("NO_COLOR", "1")
    configured = str(env.get("AIOS_ACTIVE_CODEX_HOME") or env.get("CODEX_HOME") or "").strip()
    if not configured:
        try:
            from aios_codex_accounts import active_home

            configured = str(active_home(CONFIG_PATH))
        except Exception:
            configured = ""
    if configured:
        env["CODEX_HOME"] = configured
        env["AIOS_ACTIVE_CODEX_HOME"] = configured
    return env


def windows_to_wsl(path: str | Path) -> str:
    """Convert a local Windows path without invoking a shell."""
    raw = str(Path(path).resolve())
    match = re.match(r"^([A-Za-z]):[\\/](.*)$", raw)
    if not match:
        return raw.replace("\\", "/")
    drive, rest = match.groups()
    return f"/mnt/{drive.lower()}/{rest.replace(chr(92), '/')}"


def normalize_attachments(values: Any) -> list[dict]:
    out: list[dict] = []
    for item in values or []:
        if isinstance(item, str):
            item = {"path": item}
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        url = str(item.get("url") or "").strip()
        label = _short(item.get("label") or (Path(path).name if path else url), 100)
        if path:
            resolved = Path(path).expanduser().resolve()
            if resolved.exists():
                out.append({"kind": "file", "path": str(resolved), "label": label or resolved.name})
        elif url.startswith(("https://", "http://")):
            out.append({"kind": "url", "url": url, "label": label or url})
    return out


def compose_brief(brief: str, attachments: list[dict]) -> str:
    text = str(brief or "").strip()
    if not attachments:
        return text
    lines = [text, "", "Attached context:"]
    for item in attachments:
        target = item.get("path") or item.get("url") or ""
        lines.append(f"- {item.get('label') or target}: {target}")
    return "\n".join(lines).strip()


class JsonRpcProcess:
    """Small thread-safe stdio client for one Codex app-server process."""

    def __init__(self, command: list[str], cwd: Path, on_server_request: Callable[[dict], dict] | None = None):
        self.command = command
        self.cwd = cwd
        self.on_server_request = on_server_request
        self.process: subprocess.Popen | None = None
        self._write_lock = threading.Lock()
        self._pending: dict[int, queue.Queue] = {}
        self._pending_lock = threading.Lock()
        self.notifications: queue.Queue = queue.Queue()
        self._next_id = 1
        self.stderr: list[str] = []

    def start(self) -> None:
        self.process = subprocess.Popen(
            self.command,
            cwd=str(self.cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
            env=_provider_env(),
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        threading.Thread(target=self._read_stdout, daemon=True, name="codex-appserver-out").start()
        threading.Thread(target=self._read_stderr, daemon=True, name="codex-appserver-err").start()

    def _read_stderr(self) -> None:
        proc = self.process
        if not proc or not proc.stderr:
            return
        for line in proc.stderr:
            if line.strip():
                self.stderr.append(line.rstrip())
                self.stderr[:] = self.stderr[-80:]

    def _read_stdout(self) -> None:
        proc = self.process
        if not proc or not proc.stdout:
            return
        for raw in proc.stdout:
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if "id" in message and ("result" in message or "error" in message):
                with self._pending_lock:
                    waiter = self._pending.pop(int(message["id"]), None)
                if waiter:
                    waiter.put(message)
                continue
            if "id" in message and message.get("method"):
                threading.Thread(
                    target=self._answer_server_request,
                    args=(message,),
                    daemon=True,
                    name="codex-appserver-request",
                ).start()
                continue
            self.notifications.put(message)
        self.notifications.put({"method": "process/exited"})

    def _answer_server_request(self, message: dict) -> None:
        try:
            result = self.on_server_request(message) if self.on_server_request else {"decision": "acceptForSession"}
            if result is None:
                return
            self.send({"id": message["id"], "result": result})
        except Exception as exc:
            self.send({"id": message["id"], "error": {"code": -32000, "message": str(exc)}})

    def send(self, payload: dict) -> None:
        proc = self.process
        if not proc or not proc.stdin or proc.poll() is not None:
            raise RuntimeError("Codex app-server is not running")
        with self._write_lock:
            proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            proc.stdin.flush()

    def notify(self, method: str, params: dict | None = None) -> None:
        self.send({"method": method, "params": params or {}})

    def request(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        with self._pending_lock:
            request_id = self._next_id
            self._next_id += 1
            waiter: queue.Queue = queue.Queue(maxsize=1)
            self._pending[request_id] = waiter
        self.send({"method": method, "id": request_id, "params": params or {}})
        try:
            response = waiter.get(timeout=timeout)
        except queue.Empty as exc:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise TimeoutError(f"Codex app-server timed out on {method}") from exc
        if response.get("error"):
            error = response["error"]
            raise RuntimeError(str(error.get("message") if isinstance(error, dict) else error))
        return response.get("result") or {}

    def stop(self) -> None:
        proc = self.process
        if not proc or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except OSError:
                pass


class CodeJob:
    def __init__(self, job_id: str):
        self.id = job_id
        self.directory = JOBS_DIR / job_id
        self.meta_path = self.directory / "job.json"
        self.events_path = self.directory / "events.jsonl"
        self.lock = threading.RLock()
        self.handoff_lock = threading.RLock()
        self.turn_lock = threading.Lock()
        self.process: subprocess.Popen | None = None
        self.rpc: JsonRpcProcess | None = None
        self.active_turn_id = ""
        self.stop_requested = False
        self.interrupt_requested = False
        self.queued = 0
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._worker_lock = threading.Lock()
        self._worker_running = False
        self.question_waiter: queue.Queue[str] | None = None
        self.pending_question_params: dict[str, Any] = {}
        self._activity_stream_sizes: dict[str, int] = {}
        self._activity_stream_truncated: set[str] = set()
        self._activity_types: dict[str, str] = {}
        self._claude_message_id = ""
        self._claude_saw_text_deltas = False
        self._claude_block_types: dict[str, str] = {}
        self._cursor_saw_text_deltas = False
        self._cursor_text_buffer = ""
        self._cursor_tool_ids: dict[str, str] = {}

    def load(self) -> dict:
        try:
            return json.loads(self.meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def save(self, **updates: Any) -> dict:
        with self.lock:
            meta = self.load()
            meta.update(updates)
            meta["updated_at"] = _now()
            _atomic_json(self.meta_path, meta)
            return meta

    def record_native_session(self, native_session_id: Any) -> dict:
        """Persist a provider-native id on the active provider segment."""
        native = str(native_session_id or "").strip()
        meta = self.load()
        segments = list(meta.get("provider_sessions") or [])
        if segments and not segments[-1].get("ended_at"):
            current = dict(segments[-1])
            current["native_session_id"] = native
            segments[-1] = current
        return self.save(native_session_id=native, provider_sessions=segments)

    def append(self, kind: str, text: str = "", *, notify: bool = False, **extra: Any) -> dict:
        event = {
            "ts": _now(),
            "kind": kind,
            "role": kind if kind in {"user", "assistant", "tool", "thinking", "result", "error", "status"} else "status",
            "text": str(text or ""),
            "notify": bool(notify),
        }
        event.update(extra)
        with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def activity(self, activity_id: str, activity_type: str, phase: str,
                 title: str, **extra: Any) -> dict:
        """Append one provider-neutral lifecycle update for the rich CODE UI."""
        key = str(activity_id or f"activity-{time.time_ns()}")
        requested_type = str(activity_type or "tool")
        if requested_type == "tool" and key in self._activity_types:
            requested_type = self._activity_types[key]
        self._activity_types[key] = requested_type
        return self.append(
            "activity",
            title,
            activity_id=key,
            activity_type=requested_type,
            phase=str(phase or "update"),
            title=str(title or "Working"),
            **extra,
        )

    def activity_delta(self, activity_id: str, activity_type: str, title: str,
                       delta: Any, *, stream: str = "output", **extra: Any) -> dict | None:
        text = str(delta or "")
        if not text:
            return None
        key = str(activity_id)
        used = self._activity_stream_sizes.get(key, 0)
        remaining = max(0, MAX_ACTIVITY_STREAM_CHARS - used)
        if remaining <= 0:
            if key in self._activity_stream_truncated:
                return None
            self._activity_stream_truncated.add(key)
            text = "\n… live output truncated in aiOS; the provider session retains the full run.\n"
        elif len(text) > remaining:
            text = text[:remaining] + "\n… live output truncated in aiOS.\n"
            self._activity_stream_truncated.add(key)
        self._activity_stream_sizes[key] = used + len(text)
        return self.activity(
            key,
            activity_type,
            "update",
            title,
            delta=text,
            stream=stream,
            **extra,
        )

    def send(self, text: str, *, urgent: bool = False, attachments: Any = None,
             model: str = "", reasoning: str = "", fast: bool | None = None) -> dict:
        with self.handoff_lock:
            return self._send(
                text,
                urgent=urgent,
                attachments=attachments,
                model=model,
                reasoning=reasoning,
                fast=fast,
            )

    def _send(self, text: str, *, urgent: bool = False, attachments: Any = None,
              model: str = "", reasoning: str = "", fast: bool | None = None) -> dict:
        text = str(text or "").strip()
        if not text:
            return {"ok": False, "error": "message required"}
        meta = self.load()
        if not meta:
            return {"ok": False, "error": "unknown CODE job"}
        updates: dict[str, Any] = {}
        if model:
            updates["model"] = model.strip()
        if reasoning:
            updates["reasoning"] = reasoning.strip().lower()
        if fast is not None:
            updates["fast"] = bool(fast)
        chosen_model = updates.get("model", meta.get("model", ""))
        chosen_reasoning = updates.get("reasoning", meta.get("reasoning", ""))
        chosen_fast = updates.get("fast", meta.get("fast", False))
        invalid = selection_error(str(meta.get("provider") or ""), chosen_model, chosen_reasoning, bool(chosen_fast))
        if invalid:
            return invalid
        if updates:
            meta = self.save(**updates)
        normalized = normalize_attachments(attachments)
        payload = compose_brief(text, normalized)
        self.append("user", text, attachments=normalized, urgent=bool(urgent))
        if meta.get("pending_question"):
            meta = self.save(pending_question="")
        if self.question_waiter is not None:
            try:
                self.question_waiter.put_nowait(payload)
            except queue.Full:
                return {"ok": False, "error": "The CODE question already has an answer in flight."}
            self.save(status="running", pending_question="")
            self.append("status", "Answer delivered to the active agent question.", notify=True, state="running")
            return {"ok": True, "answered": True, "job": self.load()}

        if urgent and self.rpc and self.active_turn_id:
            try:
                self.rpc.request(
                    "turn/steer",
                    {
                        "threadId": meta.get("native_session_id"),
                        "expectedTurnId": self.active_turn_id,
                        "input": [{"type": "text", "text": payload}],
                    },
                    timeout=10,
                )
                self.append("status", "Urgent instruction steered into the active turn.", notify=True)
                return {"ok": True, "steered": True, "job": self.load()}
            except Exception as exc:
                self.append("status", f"Direct steering was unavailable; queued after interrupt: {exc}")
                self.stop(interrupted=True)
        elif urgent and meta.get("status") == "running":
            self.append("status", "Urgent instruction interrupted the active turn and will run next.", notify=True)
            self.stop(interrupted=True)

        self._queue_payload(payload, normalized)
        return {"ok": True, "queued": self.queued > 1, "job": self.load()}

    def _queue_payload(self, payload: str, attachments: list[dict] | None = None) -> None:
        self._messages.put({"payload": payload, "attachments": attachments or []})
        self.queued += 1
        self.save(status="queued", queued=self.queued)
        with self._worker_lock:
            if not self._worker_running:
                self._worker_running = True
                threading.Thread(
                    target=self._drain_messages,
                    daemon=True,
                    name=f"code-job-{self.id}",
                ).start()

    def handoff(self, target_provider: str, target_model: str, target_reasoning: str,
                target_fast: bool = False, instruction: str = "") -> dict:
        """Move this logical job to a fresh native session on another provider."""
        with self.handoff_lock:
            source = self.load()
            if not source:
                return {"ok": False, "error": "unknown CODE job"}
            target_provider = str(target_provider or "").strip().lower()
            target_model = str(target_model or "").strip()
            target_reasoning = str(target_reasoning or "").strip().lower()
            if target_provider not in PROVIDERS:
                return {"ok": False, "error": "provider must be codex, claude, or cursor"}
            if target_provider == str(source.get("provider") or "").lower():
                return {
                    "ok": False,
                    "error": "Choose a different provider for handoff; same-provider model changes use normal CODE continuation.",
                    "needs": ["provider"],
                }
            if not target_model:
                return {"ok": False, "error": "exact target model is required", "needs": ["model"]}
            if not target_reasoning:
                return {"ok": False, "error": "target reasoning/intelligence level is required", "needs": ["reasoning"]}
            ready, message = provider_status(target_provider)
            if not ready:
                return {"ok": False, "error": message, "provider": target_provider}
            invalid = selection_error(target_provider, target_model, target_reasoning, bool(target_fast))
            if invalid:
                return invalid

            # End any active source turn first.  Acquiring turn_lock after the
            # interrupt guarantees that late source-provider status writes have
            # finished before the target metadata and bridge are installed.
            if source.get("status") in ACTIVE_STATES or self.process or self.rpc or self.queued:
                self.stop(interrupted=True)
            with self.turn_lock:
                source = self.load()
                event_result = read_events(self.id, 0)
                events = event_result.get("events") or []
                changes = code_handoff.collect_worktree_changes(source.get("cwd") or ROOT)
                manifest = code_handoff.build_manifest(
                    source,
                    events,
                    target_provider=target_provider,
                    target_model=target_model,
                    target_reasoning=target_reasoning,
                    target_fast=bool(target_fast),
                    instruction=instruction,
                    worktree_changes=changes,
                )
                handoff_id = manifest["handoff_id"]
                handoffs_dir = self.directory / "handoffs"
                manifest_path = handoffs_dir / f"{handoff_id}.json"
                _atomic_json(manifest_path, manifest)

                now = _now()
                segments = [dict(item) for item in source.get("provider_sessions") or []]
                if not segments:
                    segments.append({
                        "provider": source.get("provider"),
                        "model": source.get("model"),
                        "reasoning": source.get("reasoning"),
                        "fast": bool(source.get("fast")),
                        "native_session_id": source.get("native_session_id") or "",
                        "started_at": source.get("created_at"),
                    })
                if not segments[-1].get("ended_at"):
                    segments[-1].update({
                        "native_session_id": source.get("native_session_id") or segments[-1].get("native_session_id") or "",
                        "ended_at": now,
                        "handoff_id": handoff_id,
                    })
                segments.append({
                    "provider": target_provider,
                    "model": target_model,
                    "reasoning": target_reasoning,
                    "fast": bool(target_fast),
                    "native_session_id": "",
                    "started_at": now,
                    "handoff_id": handoff_id,
                })
                history = list(source.get("handoffs") or [])
                history.append({
                    "id": handoff_id,
                    "created_at": manifest["created_at"],
                    "from_provider": source.get("provider"),
                    "from_model": source.get("model"),
                    "to_provider": target_provider,
                    "to_model": target_model,
                    "manifest": str(manifest_path),
                })
                self.queued = 0
                self.save(
                    provider=target_provider,
                    model=target_model,
                    reasoning=target_reasoning,
                    fast=bool(target_fast),
                    native_session_id="",
                    status="queued",
                    queued=0,
                    pending_question="",
                    provider_sessions=segments,
                    handoffs=history,
                    last_handoff_id=handoff_id,
                    last_handoff_manifest=str(manifest_path),
                )
                switch_text = (
                    f"Switched from {_provider_label(str(source.get('provider') or ''))} · {source.get('model') or 'default'} "
                    f"to {_provider_label(target_provider)} · {target_model}"
                )
                self.append(
                    "provider_switch",
                    switch_text,
                    role="provider_switch",
                    notify=True,
                    state="queued",
                    handoff_id=handoff_id,
                    from_provider=source.get("provider"),
                    from_model=source.get("model"),
                    from_native_session_id=source.get("native_session_id") or "",
                    to_provider=target_provider,
                    to_model=target_model,
                    to_reasoning=target_reasoning,
                    to_fast=bool(target_fast),
                    native_continuation=False,
                )
                self.stop_requested = False
                self.interrupt_requested = False
                self._queue_payload(code_handoff.bridge_prompt(manifest), [])

            return {
                "ok": True,
                "handoff": {
                    "id": handoff_id,
                    "from_provider": source.get("provider"),
                    "from_model": source.get("model"),
                    "to_provider": target_provider,
                    "to_model": target_model,
                    "native_continuation": False,
                    "manifest": str(manifest_path),
                },
                "job": self.load(),
            }

    def _drain_messages(self) -> None:
        try:
            while True:
                try:
                    message = self._messages.get_nowait()
                except queue.Empty:
                    return
                self._run_locked(message["payload"], message.get("attachments") or [])
                self._messages.task_done()
        finally:
            with self._worker_lock:
                self._worker_running = False
                # Close the race where a message arrives after get_nowait()
                # but before the running flag is cleared.
                if not self._messages.empty():
                    self._worker_running = True
                    threading.Thread(
                        target=self._drain_messages,
                        daemon=True,
                        name=f"code-job-{self.id}",
                    ).start()

    def stop(self, *, interrupted: bool = False) -> dict:
        self.stop_requested = not interrupted
        self.interrupt_requested = bool(interrupted)
        if self.question_waiter is not None:
            try:
                self.question_waiter.put_nowait("")
            except queue.Full:
                pass
        if self.rpc:
            try:
                meta = self.load()
                if self.active_turn_id and meta.get("native_session_id"):
                    self.rpc.request("turn/interrupt", {"threadId": meta["native_session_id"]}, timeout=5)
            except Exception:
                pass
            self.rpc.stop()
        proc = self.process
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        while True:
            try:
                self._messages.get_nowait()
                self._messages.task_done()
            except queue.Empty:
                break
        self.queued = 0
        state = "interrupted" if interrupted else "stopped"
        self.save(status=state, queued=0)
        self.append("status", state.capitalize(), notify=True, state=state)
        return {"ok": True, "stopped": True, "job": self.load()}

    def _run_locked(self, payload: str, attachments: list[dict] | None = None) -> None:
        with self.turn_lock:
            # A stop/handoff can land after the queue worker has claimed a
            # message but before it acquires the turn lock. Do not start that
            # stale source-provider turn.
            if self.stop_requested or self.interrupt_requested:
                return
            self.queued = max(0, self.queued - 1)
            self.stop_requested = False
            self.interrupt_requested = False
            started = _now()
            self.save(status="running", queued=self.queued, started_at=started, last_error="")
            self.append("status", "Working", notify=True, state="running")
            warning_stop = threading.Event()
            threading.Thread(
                target=self._runtime_warnings,
                args=(warning_stop, started),
                daemon=True,
                name=f"code-warning-{self.id}",
            ).start()
            outcome = "failed"
            summary = ""
            try:
                provider = self.load().get("provider")
                if provider == "codex":
                    outcome, summary = self._run_codex(payload, attachments or [])
                elif provider == "claude":
                    outcome, summary = self._run_claude(payload)
                elif provider == "cursor":
                    outcome, summary = self._run_cursor(payload)
                else:
                    raise RuntimeError(f"unknown provider: {provider}")
            except Exception as exc:
                summary = str(exc)
                if self.stop_requested:
                    outcome = "stopped"
                elif self.interrupt_requested:
                    outcome = "interrupted"
                else:
                    outcome = "failed"
            finally:
                warning_stop.set()
                self.process = None
                if self.rpc:
                    self.rpc.stop()
                self.rpc = None
                self.active_turn_id = ""

            if self.stop_requested:
                outcome = "stopped"
            elif self.interrupt_requested:
                outcome = "interrupted"
            if outcome == "completed":
                self.save(status="completed", completed_at=_now(), last_summary=_short(summary, 500))
                self.append("result", summary, notify=True, state="completed")
            elif outcome == "waiting_user":
                self.save(status="waiting_user", last_summary=_short(summary, 500))
            elif outcome == "stopped":
                self.save(status="stopped", completed_at=_now())
            elif outcome == "interrupted":
                self.save(status="interrupted", completed_at=_now())
            else:
                self.save(status="failed", completed_at=_now(), last_error=_short(summary, 1000))
                self.append("error", summary or "The coding agent failed without an error message.", notify=True, state="failed")
            if self.queued:
                self.save(status="queued", queued=self.queued)
            self.interrupt_requested = False

    def _runtime_warnings(self, stop: threading.Event, started: float) -> None:
        if SOFT_WARNING_SECONDS <= 0 or stop.wait(SOFT_WARNING_SECONDS):
            return
        elapsed = int(time.time() - started)
        self.append("warning", f"Still working after {elapsed // 60} minutes.", notify=True)
        while SOFT_WARNING_REPEAT_SECONDS > 0 and not stop.wait(SOFT_WARNING_REPEAT_SECONDS):
            elapsed = int(time.time() - started)
            self.append("warning", f"Still working after {elapsed // 60} minutes.", notify=True)

    def _codex_server_request(self, message: dict) -> dict | None:
        method = str(message.get("method") or "")
        params = message.get("params") or {}
        if "requestApproval" in method or "permissions/requestApproval" in method:
            self.append(
                "approval",
                _short(params.get("reason") or params.get("command") or method, 300),
                approved=True,
                method=method,
            )
            if "permissions/requestApproval" in method:
                return {"permissions": params.get("permissions") or params.get("requestedPermissions") or [], "scope": "session"}
            return {"decision": "acceptForSession"}
        if "requestUserInput" in method:
            question = _extract_question(params)
            self.append("question", question, notify=True, request=message)
            self.save(status="waiting_user", pending_question=question)
            waiter: queue.Queue[str] = queue.Queue(maxsize=1)
            self.question_waiter = waiter
            self.pending_question_params = params
            try:
                answer = waiter.get(timeout=TURN_TIMEOUT_SECONDS)
            except queue.Empty:
                answer = ""
            finally:
                self.question_waiter = None
                self.pending_question_params = {}
            answers = {}
            for row in params.get("questions") or []:
                question_id = str(row.get("id") or "") if isinstance(row, dict) else ""
                if question_id:
                    answers[question_id] = {"answers": [answer] if answer else []}
            return {"answers": answers}
        if "elicitation/request" in method:
            question = _extract_question(params)
            self.append("question", question, notify=True, request=message)
            self.save(status="waiting_user", pending_question=question)
            waiter = queue.Queue(maxsize=1)
            self.question_waiter = waiter
            self.pending_question_params = params
            try:
                answer = waiter.get(timeout=TURN_TIMEOUT_SECONDS)
            except queue.Empty:
                answer = ""
            finally:
                self.question_waiter = None
                self.pending_question_params = {}
            return {"action": "accept" if answer else "cancel", "content": {"answer": answer} if answer else None}
        return {"decision": "acceptForSession"}

    def _run_codex(self, payload: str, attachments: list[dict]) -> tuple[str, str]:
        codex = find_codex()
        if not codex:
            raise RuntimeError("Codex is not installed or cannot be located")
        meta = self.load()
        project = Path(meta["cwd"])
        rpc = JsonRpcProcess([codex, "app-server"], project, self._codex_server_request)
        self.rpc = rpc
        rpc.start()
        rpc.request(
            "initialize",
            {"clientInfo": {"name": "aios_code", "title": "aiOS CODE", "version": "1.0"}},
            timeout=30,
        )
        rpc.notify("initialized")
        native = str(meta.get("native_session_id") or "")
        if native:
            result = rpc.request(
                "thread/resume",
                {
                    "threadId": native,
                    "model": meta["model"],
                    "cwd": str(project),
                    "approvalPolicy": "never",
                    "sandbox": "danger-full-access",
                },
                timeout=60,
            )
        else:
            result = rpc.request(
                "thread/start",
                {
                    "model": meta["model"],
                    "cwd": str(project),
                    "approvalPolicy": "never",
                    "sandbox": "danger-full-access",
                    "serviceName": "aiOS CODE",
                },
                timeout=60,
            )
        thread = result.get("thread") or {}
        native = str(thread.get("id") or native)
        if not native:
            raise RuntimeError("Codex did not return a thread id")
        self.record_native_session(native)
        inputs: list[dict] = [{"type": "text", "text": payload}]
        for item in attachments:
            path = item.get("path")
            if path and Path(path).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                inputs.append({"type": "localImage", "path": path})
        params: dict[str, Any] = {
            "threadId": native,
            "input": inputs,
            "cwd": str(project),
            "approvalPolicy": "never",
            "sandboxPolicy": {"type": "dangerFullAccess"},
            "model": meta["model"],
        }
        if meta.get("reasoning") not in {"", "none", "auto"}:
            params["effort"] = meta["reasoning"]
        if meta.get("fast"):
            params["serviceTier"] = "fast"
        turn = rpc.request("turn/start", params, timeout=60).get("turn") or {}
        self.active_turn_id = str(turn.get("id") or "")
        final_messages: list[str] = []
        deadline = time.monotonic() + TURN_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                message = rpc.notifications.get(timeout=0.5)
            except queue.Empty:
                if rpc.process and rpc.process.poll() is not None:
                    break
                continue
            method = str(message.get("method") or "")
            data = message.get("params") or {}
            if self._handle_codex_progress(method, data):
                continue
            if method == "item/agentMessage/delta":
                delta = str(data.get("delta") or "")
                if delta:
                    self.append("assistant", delta)
                continue
            if method in {"item/started", "item/completed"}:
                self._handle_codex_item(method, data)
                item = data.get("item") or {}
                if method == "item/completed" and item.get("type") in {"agentMessage", "agent_message"}:
                    text = str(item.get("text") or "").strip()
                    if text:
                        final_messages.append(text)
                continue
            if method == "turn/started":
                current = data.get("turn") or {}
                self.active_turn_id = str(current.get("id") or self.active_turn_id)
                continue
            if method == "turn/completed":
                status = str((data.get("turn") or {}).get("status") or data.get("status") or "completed")
                if status in {"failed", "error"}:
                    error = data.get("error") or (data.get("turn") or {}).get("error") or "Codex turn failed"
                    return "failed", str(error)
                if status in {"interrupted", "cancelled"}:
                    return ("stopped" if self.stop_requested else "interrupted"), "Codex turn interrupted."
                return "completed", final_messages[-1] if final_messages else "Codex finished the turn."
            if method == "process/exited":
                break
        rpc.stop()
        if time.monotonic() >= deadline:
            return "failed", f"Codex exceeded the {TURN_TIMEOUT_SECONDS // 60}-minute turn limit."
        error = "\n".join(rpc.stderr[-12:]).strip()
        return "failed", error or "Codex app-server exited before completing the turn."

    def _handle_codex_progress(self, method: str, params: dict) -> bool:
        item_id = str(params.get("itemId") or params.get("item_id") or "")
        if method == "item/commandExecution/outputDelta":
            delta = params.get("delta") or params.get("output") or ""
            self.activity_delta(item_id or "codex-command", "command", "Running command", delta)
            return True
        if method in {"item/reasoning/summaryTextDelta", "item/reasoning/textDelta"}:
            delta = params.get("delta") or params.get("text") or ""
            self.activity_delta(item_id or "codex-thinking", "thinking", "Thinking", delta, stream="summary")
            return True
        if method == "item/plan/delta":
            delta = params.get("delta") or params.get("text") or ""
            self.activity_delta(item_id or "codex-plan", "plan", "Planning", delta, stream="plan")
            return True
        if method == "turn/diff/updated":
            turn_id = str(params.get("turnId") or params.get("turn_id") or self.active_turn_id or "current")
            diff = str(params.get("diff") or "")
            self.activity(
                f"turn-{turn_id}-diff",
                "diff",
                "update",
                "Reviewing working changes",
                diff=diff[-MAX_ACTIVITY_STREAM_CHARS:],
            )
            return True
        if method == "turn/plan/updated":
            turn_id = str(params.get("turnId") or params.get("turn_id") or self.active_turn_id or "current")
            plan = params.get("plan") or []
            self.activity(
                f"turn-{turn_id}-plan",
                "plan",
                "update",
                "Plan",
                detail=str(params.get("explanation") or ""),
                steps=plan,
            )
            return True
        return False

    def _handle_codex_item(self, method: str, params: dict) -> None:
        item = params.get("item") or {}
        kind = str(item.get("type") or item.get("item_type") or "")
        item_id = str(item.get("id") or params.get("itemId") or f"codex-{kind}-{time.time_ns()}")
        phase = "started" if method == "item/started" else _activity_phase(item.get("status"))
        if kind in {"commandExecution", "command_execution"}:
            command = item.get("command") or item.get("cmd") or ""
            if isinstance(command, list):
                command = " ".join(str(part) for part in command)
            command = _display_command(command)
            title = "Running command" if phase in {"started", "update"} else ("Command failed" if phase == "failed" else "Ran command")
            self.activity(
                item_id,
                "command",
                phase,
                title,
                command=str(command),
                cwd=str(item.get("cwd") or ""),
                output=_structured_text(item.get("aggregatedOutput"))[-MAX_ACTIVITY_STREAM_CHARS:],
                exit_code=item.get("exitCode"),
                duration_ms=item.get("durationMs"),
                detail=_short(command, 260),
            )
        elif kind in {"fileChange", "file_change", "patch_apply"}:
            changes = item.get("changes") or []
            paths = [str(change.get("path") or "") for change in changes if isinstance(change, dict)]
            verb = "Editing" if phase in {"started", "update"} else ("Edit failed" if phase == "failed" else "Edited")
            label = _file_summary(paths)
            normalized = []
            for change in changes:
                if not isinstance(change, dict):
                    continue
                normalized.append({
                    "path": str(change.get("path") or ""),
                    "change_kind": str(change.get("kind") or "update"),
                    "diff": str(change.get("diff") or "")[-MAX_ACTIVITY_STREAM_CHARS:],
                })
            self.activity(item_id, "files", phase, f"{verb} {label}", files=paths, changes=normalized)
        elif kind == "reasoning":
            summary = _structured_text(item.get("summary") or item.get("text"))
            self.activity(
                item_id,
                "thinking",
                phase,
                "Thinking" if phase in {"started", "update"} else "Thought through the approach",
                summary=summary,
            )
        elif kind == "plan":
            self.activity(item_id, "plan", phase, "Planning" if phase == "started" else "Plan", detail=_structured_text(item.get("text")))
        elif kind in {"mcpToolCall", "dynamicToolCall", "collabToolCall", "webSearch", "imageView"}:
            arguments = item.get("arguments") or {}
            tool_name = str(item.get("tool") or item.get("server") or kind)
            activity_type, title, detail = _tool_activity(tool_name, arguments)
            if kind == "webSearch":
                activity_type, title = "search", "Searching the web"
                detail = _short(item.get("query") or detail, 260)
            self.activity(
                item_id,
                activity_type,
                phase,
                title,
                detail=detail,
                tool=tool_name,
                arguments=arguments,
                output=_structured_text(item.get("result") or item.get("contentItems")),
                error=_structured_text(item.get("error")),
                duration_ms=item.get("durationMs"),
            )

    def _run_claude(self, payload: str) -> tuple[str, str]:
        claude = find_claude()
        if not claude:
            raise RuntimeError("Claude Code is not installed or cannot be located")
        meta = self.load()
        command = ["cmd.exe", "/d", "/c", claude] if os.name == "nt" else [claude]
        command += [
            "-p", "--output-format", "stream-json", "--verbose",
            "--include-partial-messages", "--permission-mode", "bypassPermissions",
            "--model", meta["model"],
        ]
        if meta.get("reasoning") not in {"", "none", "auto"}:
            command += ["--effort", meta["reasoning"]]
        if meta.get("native_session_id"):
            command += ["--resume", meta["native_session_id"]]
        command.append(payload)
        self._claude_message_id = ""
        self._claude_saw_text_deltas = False
        self._claude_block_types = {}
        return self._run_stream_process(command, Path(meta["cwd"]), "claude")

    def _run_cursor(self, payload: str) -> tuple[str, str]:
        meta = self.load()
        # Cursor's live model list returns exact runnable ids. Intelligence and
        # fast variants are already encoded in ids such as `...-high-fast`.
        # Appending Codex-style `[effort=...]` modifiers breaks models such as
        # `composer-2.5`, even though that exact id is valid.
        model = str(meta["model"])
        command = [
            "wsl.exe", "-d", WSL_DISTRO, "--", CURSOR_AGENT,
            "-p", "--force", "--trust", "--output-format", "stream-json",
            "--stream-partial-output", "--model", model,
            "--workspace", windows_to_wsl(meta["cwd"]),
        ]
        if meta.get("native_session_id"):
            command += ["--resume", meta["native_session_id"]]
        command.append(payload)
        self._cursor_saw_text_deltas = False
        self._cursor_text_buffer = ""
        self._cursor_tool_ids = {}
        return self._run_stream_process(command, Path(meta["cwd"]), "cursor")

    def _run_stream_process(self, command: list[str], cwd: Path, provider: str) -> tuple[str, str]:
        proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
            env=_provider_env(),
        )
        self.process = proc
        stderr_data: list[bytes] = []
        threading.Thread(target=lambda: stderr_data.append(proc.stderr.read() or b""), daemon=True).start()
        final = ""
        saw_result = False
        question = ""
        deadline = time.monotonic() + TURN_TIMEOUT_SECONDS
        assert proc.stdout is not None
        stdout_queue: queue.Queue[bytes | None] = queue.Queue()

        def read_stdout() -> None:
            assert proc.stdout is not None
            for output_line in iter(proc.stdout.readline, b""):
                stdout_queue.put(output_line)
            stdout_queue.put(None)

        threading.Thread(target=read_stdout, daemon=True, name=f"{provider}-code-stdout").start()
        provider_error = ""
        while time.monotonic() < deadline:
            try:
                raw = stdout_queue.get(timeout=0.25)
            except queue.Empty:
                if proc.poll() is not None and stdout_queue.empty():
                    break
                continue
            if raw is None:
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("is_error") or (event.get("type") == "result" and event.get("subtype") in {"error", "failed"}):
                provider_error = str(event.get("result") or event.get("error") or "Provider reported an error")
            if provider == "claude":
                result, current_question = self._handle_claude_event(event)
            else:
                result, current_question = self._handle_cursor_event(event)
            if result is not None:
                saw_result = True
                final = result
            if current_question:
                question = current_question
                if proc.poll() is None:
                    proc.kill()
                break
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        stderr = (stderr_data[0] if stderr_data else b"").decode("utf-8", "replace").strip()
        if question:
            self.save(pending_question=question)
            return "waiting_user", question
        if self.stop_requested:
            return "stopped", "Stopped."
        if provider_error:
            return "failed", _friendly_provider_error(provider, provider_error)
        if proc.returncode != 0 and not saw_result:
            return "failed", _friendly_provider_error(
                provider,
                stderr or f"{_provider_label(provider)} exited with code {proc.returncode}.",
            )
        if not saw_result:
            return "failed", stderr[-3000:] or f"{_provider_label(provider)} ended without a final result."
        return "completed", final or f"{_provider_label(provider)} finished the turn."

    def _handle_claude_event(self, event: dict) -> tuple[str | None, str]:
        event_type = event.get("type")
        if event_type == "system" and event.get("subtype") == "init":
            if event.get("session_id"):
                self.record_native_session(event["session_id"])
            return None, ""
        if event_type == "stream_event":
            streamed = event.get("event") or {}
            streamed_type = str(streamed.get("type") or "")
            if streamed_type == "message_start":
                self._claude_message_id = str((streamed.get("message") or {}).get("id") or self._claude_message_id)
                return None, ""
            index = streamed.get("index", 0)
            activity_id = f"claude-{self._claude_message_id or 'message'}-{index}"
            if streamed_type == "content_block_start":
                block = streamed.get("content_block") or {}
                block_type = str(block.get("type") or "")
                self._claude_block_types[activity_id] = block_type
                if block_type in {"thinking", "reasoning"}:
                    self.activity(activity_id, "thinking", "started", "Thinking")
                elif block_type == "tool_use":
                    tool_id = str(block.get("id") or activity_id)
                    name = str(block.get("name") or "tool")
                    activity_type, title, detail = _tool_activity(name, block.get("input") or {})
                    self.activity(tool_id, activity_type, "started", title, detail=detail, tool=name, arguments=block.get("input") or {})
                return None, ""
            if streamed_type == "content_block_delta":
                delta = streamed.get("delta") or {}
                delta_type = str(delta.get("type") or "")
                if delta_type == "text_delta":
                    text = str(delta.get("text") or "")
                    if text:
                        self._claude_saw_text_deltas = True
                        self.append("assistant", text)
                elif delta_type in {"thinking_delta", "reasoning_delta"}:
                    self.activity_delta(activity_id, "thinking", "Thinking", delta.get("thinking") or delta.get("text"), stream="summary")
                return None, ""
            if streamed_type == "content_block_stop":
                if self._claude_block_types.get(activity_id) in {"thinking", "reasoning"}:
                    self.activity(activity_id, "thinking", "completed", "Thought through the approach")
                return None, ""
        if event_type == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                kind = block.get("type")
                if kind == "text" and str(block.get("text") or "").strip() and not self._claude_saw_text_deltas:
                    self.append("assistant", block["text"], notify=True)
                elif kind == "tool_use":
                    name = str(block.get("name") or "tool")
                    if name.casefold() in {"askuserquestion", "ask_user_question"}:
                        question = _extract_question(block.get("input") or {})
                        self.append("question", question, notify=True)
                        return None, question
                    tool_id = str(block.get("id") or f"claude-{name}-{time.time_ns()}")
                    activity_type, title, detail = _tool_activity(name, block.get("input") or {})
                    self.activity(
                        tool_id,
                        activity_type,
                        "started",
                        title,
                        detail=detail,
                        tool=name,
                        arguments=block.get("input") or {},
                        command=_tool_command(block.get("input") or {}),
                        files=_tool_paths(block.get("input") or {}),
                    )
            return None, ""
        if event_type == "user":
            for block in (event.get("message") or {}).get("content") or event.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tool_id = str(block.get("tool_use_id") or block.get("id") or f"claude-tool-{time.time_ns()}")
                failed = bool(block.get("is_error"))
                activity_type = self._activity_types.get(tool_id, "tool")
                self.activity(
                    tool_id,
                    activity_type,
                    "failed" if failed else "completed",
                    "Tool failed" if failed else _completed_title(activity_type),
                    output=_structured_text(block.get("content"))[-MAX_ACTIVITY_STREAM_CHARS:],
                    error=_structured_text(block.get("content")) if failed else "",
                )
            return None, ""
        if event_type in {"tool_progress", "tool_use_progress"}:
            tool_id = str(event.get("tool_use_id") or event.get("id") or "claude-tool")
            name = str(event.get("tool_name") or event.get("name") or "Tool")
            activity_type, title, _detail = _tool_activity(name, {})
            delta = event.get("delta") or event.get("output") or event.get("content") or ""
            if delta:
                self.activity_delta(tool_id, activity_type, title, _structured_text(delta))
            else:
                self.activity(tool_id, activity_type, "update", title, elapsed_seconds=event.get("elapsed_time_seconds"))
            return None, ""
        if event_type == "result":
            if event.get("session_id"):
                self.record_native_session(event["session_id"])
            text = str(event.get("result") or "").strip()
            return text, ""
        return None, ""

    def _handle_cursor_event(self, event: dict) -> tuple[str | None, str]:
        event_type = event.get("type")
        if event_type == "system" and event.get("subtype") == "init":
            if event.get("session_id"):
                self.record_native_session(event["session_id"])
            return None, ""
        if event_type == "assistant":
            texts = []
            for block in (event.get("message") or {}).get("content") or []:
                text = str(block.get("text") or "") if isinstance(block, dict) else ""
                if text:
                    texts.append(text)
            text = "".join(texts)
            if not text:
                return None, ""
            if event.get("timestamp_ms") is not None:
                self._cursor_saw_text_deltas = True
                # Cursor sends token fragments and then a timestamped assembled
                # snapshot. Emit only the unseen suffix so streaming never
                # repeats the same paragraph.
                if text == self._cursor_text_buffer:
                    delta = ""
                elif text.startswith(self._cursor_text_buffer):
                    delta = text[len(self._cursor_text_buffer):]
                    self._cursor_text_buffer = text
                else:
                    delta = text
                    self._cursor_text_buffer += text
                if delta:
                    self.append("assistant_delta", delta, delta=delta)
            elif self._cursor_saw_text_deltas:
                # Cursor emits the assembled assistant message after its
                # timestamped fragments. The UI already has the fragments.
                self._cursor_saw_text_deltas = False
            else:
                self.append("assistant", text, notify=True)
            return None, ""
        if event_type == "tool_call":
            tool = event.get("tool_call") or {}
            name = next(iter(tool.keys()), "tool")
            payload = tool.get(name) or {}
            arguments = payload.get("args") or payload.get("arguments") or {}
            status = payload.get("status") or event.get("status")
            has_result = any(key in payload for key in ("result", "output", "error"))
            phase = _activity_phase(status) if status or has_result else "started"
            explicit_id = payload.get("id") or event.get("tool_call_id") or event.get("id")
            fingerprint = _cursor_tool_fingerprint(name, arguments)
            if explicit_id:
                tool_id = str(explicit_id)
            elif phase in {"started", "update"}:
                tool_id = self._cursor_tool_ids.setdefault(fingerprint, f"cursor-{name}-{time.time_ns()}")
            else:
                tool_id = self._cursor_tool_ids.pop(fingerprint, f"cursor-{name}-{time.time_ns()}")
            self._cursor_text_buffer = ""
            activity_type, title, detail = _tool_activity(name, arguments)
            self.activity(
                tool_id,
                activity_type,
                phase,
                title if phase in {"started", "update"} else ("Tool failed" if phase == "failed" else _completed_title(activity_type, title)),
                detail=detail,
                tool=name,
                arguments=arguments,
                command=_tool_command(arguments),
                files=_tool_paths(arguments),
                output=_structured_text(payload.get("result") or payload.get("output"))[-MAX_ACTIVITY_STREAM_CHARS:],
                error=_structured_text(payload.get("error")),
            )
            return None, ""
        if event_type in {"tool_result", "tool_call_result", "tool_progress", "tool_call_delta"}:
            tool_id = str(event.get("tool_call_id") or event.get("tool_use_id") or event.get("id") or "cursor-tool")
            name = str(event.get("tool_name") or event.get("name") or "tool")
            activity_type, title, _detail = _tool_activity(name, event.get("args") or {})
            delta = event.get("delta")
            if delta:
                self.activity_delta(tool_id, activity_type, title, _structured_text(delta))
            else:
                failed = bool(event.get("is_error") or event.get("error"))
                self.activity(
                    tool_id,
                    activity_type,
                    "failed" if failed else "completed",
                    "Tool failed" if failed else _completed_title(activity_type, title),
                    output=_structured_text(event.get("result") or event.get("output") or event.get("content"))[-MAX_ACTIVITY_STREAM_CHARS:],
                    error=_structured_text(event.get("error")),
                )
            return None, ""
        if event_type in {"thinking", "reasoning"}:
            activity_id = str(event.get("id") or "cursor-thinking")
            subtype = str(event.get("subtype") or "").casefold()
            if subtype == "delta":
                phase = "update"
            elif subtype in {"completed", "complete", "done"}:
                phase = "completed"
            else:
                phase = _activity_phase(event.get("status") or ("completed" if event.get("done") else "update"))
            text = event.get("delta") or event.get("text") or event.get("content") or ""
            if phase == "update":
                self.activity_delta(activity_id, "thinking", "Thinking", _structured_text(text), stream="summary")
            else:
                self.activity(activity_id, "thinking", phase, "Thought through the approach", summary=_structured_text(text))
            return None, ""
        if event_type == "result":
            if event.get("session_id"):
                self.record_native_session(event["session_id"])
            text = str(event.get("result") or "").strip()
            return text, ""
        return None, ""


def _activity_phase(status: Any) -> str:
    value = re.sub(r"[^a-z]", "", str(status or "").casefold())
    if value in {"failed", "error", "errored", "declined", "cancelled", "canceled"}:
        return "failed"
    if value in {"completed", "complete", "success", "succeeded", "done"}:
        return "completed"
    if value in {"started", "inprogress", "running", "pending"}:
        return "started"
    return "completed" if value else "completed"


def _structured_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(filter(None, (_structured_text(item) for item in value)))
    if isinstance(value, dict):
        for key in ("text", "content", "output", "message", "summary"):
            if key in value:
                rendered = _structured_text(value.get(key))
                if rendered:
                    return rendered
        try:
            return json.dumps(value, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _friendly_provider_error(provider: str, value: Any) -> str:
    """Keep provider failures actionable instead of dumping huge model lists."""
    message = str(value or "").strip()
    if provider == "cursor" and "Available models:" in message:
        reason = message.split("Available models:", 1)[0].strip().rstrip(". ")
        return f"{reason}. Refresh CODE and choose one of Cursor's currently discovered model ids."
    return message[-3000:] if len(message) > 3000 else message


def _cursor_tool_fingerprint(name: str, arguments: Any) -> str:
    """Match Cursor's id-less started/completed events into one activity."""
    try:
        payload = json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        payload = str(arguments or "")
    return f"{name}:{payload}"


def _tool_command(arguments: Any) -> str:
    if not isinstance(arguments, dict):
        return ""
    value = arguments.get("command") or arguments.get("cmd") or arguments.get("script") or ""
    if isinstance(value, list):
        return _display_command(" ".join(str(part) for part in value))
    return _display_command(value)


def _display_command(value: Any) -> str:
    command = str(value or "")
    # Codex app-server command previews on Windows can contain JSON-escaped
    # path separators after decoding. Collapse those for human display only.
    if re.search(r"[A-Za-z]:\\\\", command):
        command = command.replace("\\\\", "\\")
    return command


def _tool_paths(arguments: Any) -> list[str]:
    if not isinstance(arguments, dict):
        return []
    paths: list[str] = []
    for key in ("path", "file_path", "target_file", "notebook_path"):
        if arguments.get(key):
            paths.append(str(arguments[key]))
    for key in ("paths", "files"):
        values = arguments.get(key) or []
        if isinstance(values, (list, tuple)):
            paths.extend(str(value) for value in values if value)
    return list(dict.fromkeys(paths))


def _file_summary(paths: list[str]) -> str:
    names = [re.split(r"[\\/]", path)[-1] for path in paths if path]
    if not names:
        return "project files"
    if len(names) == 1:
        return names[0]
    return f"{names[0]} and {len(names) - 1} more"


def _pretty_tool_name(name: str) -> str:
    words = re.sub(r"(?<!^)(?=[A-Z])", " ", str(name or "tool"))
    words = re.sub(r"[_-]+", " ", words).strip()
    return words[:1].upper() + words[1:] if words else "Tool"


def _tool_activity(name: str, arguments: Any) -> tuple[str, str, str]:
    args = arguments if isinstance(arguments, dict) else {}
    folded = re.sub(r"[^a-z]", "", str(name or "").casefold())
    paths = _tool_paths(args)
    command = _tool_command(args)
    detail = command or (_file_summary(paths) if paths else "")
    if any(token in folded for token in ("bash", "shell", "terminal", "command", "exec", "powershell")):
        return "command", "Running command", _short(detail, 260)
    if any(token in folded for token in ("edit", "write", "patch", "replace", "notebook")):
        return "files", f"Editing {_file_summary(paths)}", _short(detail, 260)
    if any(token in folded for token in ("read", "view", "openfile")) and paths:
        return "read", f"Reading {_file_summary(paths)}", _short(detail, 260)
    if any(token in folded for token in ("grep", "glob", "search", "find")):
        query = args.get("query") or args.get("pattern") or args.get("glob") or detail
        return "search", "Searching the codebase", _short(query, 260)
    if any(token in folded for token in ("web", "browser", "url", "fetch")):
        return "web", "Using the web", _short(args.get("url") or args.get("query") or detail, 260)
    pretty = _pretty_tool_name(name)
    return "tool", f"Using {pretty}", _short(_describe_tool(name, args), 260)


def _completed_title(activity_type: str, started_title: str = "") -> str:
    return {
        "command": "Ran command",
        "files": "Edited files",
        "read": "Read file",
        "search": "Searched the codebase",
        "web": "Used the web",
        "thinking": "Thought through the approach",
        "plan": "Planned the work",
    }.get(activity_type, started_title.replace("Using ", "Used ", 1) or "Tool completed")


def _describe_tool(name: str, arguments: dict) -> str:
    detail = ""
    for key in ("path", "file_path", "command", "query", "url", "description", "prompt"):
        if arguments.get(key):
            detail = _short(arguments[key], 220)
            break
    return f"{name}: {detail}" if detail else name


def _extract_question(payload: Any) -> str:
    if isinstance(payload, str):
        return _short(payload, 1000) or "The coding agent needs your input."
    if not isinstance(payload, dict):
        return "The coding agent needs your input."
    for key in ("question", "message", "prompt", "reason"):
        if payload.get(key):
            return _short(payload[key], 1000)
    questions = payload.get("questions") or []
    if questions:
        rendered = []
        for row in questions[:3]:
            if not isinstance(row, dict):
                continue
            label = str(row.get("question") or row.get("prompt") or "").strip()
            options = [str(option.get("label") or "").strip() for option in row.get("options") or [] if isinstance(option, dict)]
            if label and options:
                label += " Options: " + ", ".join(value for value in options if value)
            if label:
                rendered.append(label)
        if rendered:
            return _short(" ".join(rendered), 1000)
    return "The coding agent needs your input."


def _get_job(job_id: str) -> CodeJob | None:
    safe = re.sub(r"[^a-zA-Z0-9]", "", str(job_id or ""))
    if not safe:
        return None
    with _REGISTRY_LOCK:
        job = _LIVE.get(safe)
        if job is None:
            candidate = CodeJob(safe)
            if not candidate.meta_path.exists():
                return None
            _LIVE[safe] = candidate
            job = candidate
        return job


def create_job(provider: str, cwd: str, brief: str, model: str, reasoning: str,
               fast: bool = False, title: str = "", attachments: Any = None) -> dict:
    provider = str(provider or "").strip().lower()
    if provider not in PROVIDERS:
        return {"ok": False, "error": "provider must be codex, claude, or cursor"}
    if not str(model or "").strip():
        return {"ok": False, "error": "exact model is required", "needs": ["model"]}
    if not str(reasoning or "").strip():
        return {"ok": False, "error": "reasoning/intelligence level is required", "needs": ["reasoning"]}
    if not str(brief or "").strip():
        return {"ok": False, "error": "job brief is required", "needs": ["brief"]}
    project = Path(str(cwd or "").strip()).expanduser()
    if not project.is_absolute():
        project = (Path.cwd() / project).resolve()
    try:
        project.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": f"cannot use project folder: {exc}"}
    project = project.resolve()
    available, message = provider_status(provider)
    if not available:
        return {"ok": False, "error": message, "provider": provider}
    invalid = selection_error(provider, str(model).strip(), str(reasoning).strip().lower(), bool(fast))
    if invalid:
        return invalid
    job_id = uuid.uuid4().hex[:12]
    job = CodeJob(job_id)
    job.directory.mkdir(parents=True, exist_ok=True)
    normalized = normalize_attachments(attachments)
    fallback_title = title.strip() or f"{_provider_label(provider)} · {_short(brief, 42)}"
    meta = {
        "id": job_id,
        "title": _short(fallback_title, 72),
        "provider": provider,
        "cwd": str(project),
        "project_name": project.name or str(project),
        "brief": str(brief).strip(),
        "attachments": normalized,
        "model": str(model).strip(),
        "reasoning": str(reasoning).strip().lower(),
        "fast": bool(fast),
        "native_session_id": "",
        "provider_sessions": [{
            "provider": provider,
            "model": str(model).strip(),
            "reasoning": str(reasoning).strip().lower(),
            "fast": bool(fast),
            "native_session_id": "",
            "started_at": _now(),
        }],
        "handoffs": [],
        "status": "queued",
        "queued": 0,
        "created_at": _now(),
        "updated_at": _now(),
        "last_summary": "",
        "last_error": "",
        "pending_question": "",
    }
    _atomic_json(job.meta_path, meta)
    job.events_path.touch()
    with _REGISTRY_LOCK:
        _LIVE[job_id] = job
    threading.Thread(target=_generate_title, args=(job_id,), daemon=True, name=f"code-title-{job_id}").start()
    result = job.send(str(brief), attachments=normalized)
    result["job"] = job.load()
    return result


def _generate_title(job_id: str) -> None:
    job = _get_job(job_id)
    if not job:
        return
    meta = job.load()
    prompt = (
        "Name this coding session in 2 to 6 plain words. Return only the title.\n"
        f"Provider: {meta.get('provider')}\nProject: {meta.get('project_name')}\n"
        f"Task: {_short(meta.get('brief'), 360)}"
    )
    try:
        agent_path = ROOT / "agent_clicker"
        if str(agent_path) not in sys.path:
            sys.path.insert(0, str(agent_path))
        from agent import codex_backend

        title = codex_backend.chat_raw(
            "You write extremely short titles. No punctuation or explanation.",
            [{"role": "user", "content": prompt}],
            model="gpt-5.6-luna",
            timeout=45,
            reasoning_effort=None,
        )
        title = _short(title.strip().strip('"\''), 72)
        if title:
            job.save(title=title, title_source="gpt-5.6-luna")
    except Exception:
        job.save(title_source="fallback")


def list_jobs(limit: int = 100) -> list[dict]:
    rows: list[dict] = []
    try:
        directories = [path for path in JOBS_DIR.iterdir() if path.is_dir()]
    except OSError:
        return []
    for directory in directories:
        try:
            meta = json.loads((directory / "job.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not meta.get("id"):
            continue
        live = _LIVE.get(directory.name)
        if meta.get("status") in ACTIVE_STATES and (not live or (not live.process and not live.rpc and not live.queued)):
            meta["status"] = "interrupted"
        rows.append(meta)
    rows.sort(key=lambda row: float(row.get("updated_at") or 0), reverse=True)
    return rows[: max(1, min(int(limit or 100), 500))]


def get_job(job_id: str) -> dict | None:
    job = _get_job(job_id)
    return job.load() if job else None


def send_message(job_id: str, text: str, **kwargs: Any) -> dict:
    job = _get_job(job_id)
    return job.send(text, **kwargs) if job else {"ok": False, "error": "unknown CODE job"}


def handoff_job(job_id: str, provider: str, model: str, reasoning: str,
                fast: bool = False, instruction: str = "") -> dict:
    job = _get_job(job_id)
    if not job:
        return {"ok": False, "error": "unknown CODE job"}
    return job.handoff(provider, model, reasoning, fast, instruction)


def stop_job(job_id: str) -> dict:
    job = _get_job(job_id)
    return job.stop() if job else {"ok": False, "error": "unknown CODE job"}


def delete_job(job_id: str, *, confirmed: bool = False) -> dict:
    job = _get_job(job_id)
    if not job:
        return {"ok": False, "error": "unknown CODE job"}
    if not confirmed:
        return {
            "ok": False,
            "error": "CODE session deletion requires explicit confirmation",
            "needs_confirmation": True,
        }
    meta = job.load()
    if meta.get("status") in ACTIVE_STATES:
        return {
            "ok": False,
            "error": "Stop the active CODE session before deleting it",
            "active": True,
        }
    # A worker can still be unwinding just after a stop. Wait for its owner
    # before moving storage so a late status save cannot recreate a ghost.
    with job.turn_lock:
        with _REGISTRY_LOCK:
            _LIVE.pop(job.id, None)
        try:
            trash = JOBS_DIR / ".trash"
            trash.mkdir(parents=True, exist_ok=True)
            destination = trash / f"{job.id}-{int(time.time() * 1000)}"
            job.directory.replace(destination)
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
    return {"ok": True, "recoverable": True, "trash_id": destination.name}


def read_events(job_id: str, since: int = 0) -> dict:
    job = _get_job(job_id)
    if not job:
        return {"ok": False, "error": "unknown CODE job", "events": [], "size": 0}
    events: list[dict] = []
    size = 0
    reset = False
    try:
        size = job.events_path.stat().st_size
        if since > size:
            since = 0
            reset = True
        with job.events_path.open("rb") as handle:
            handle.seek(max(0, since))
            raw = handle.read()
        for line in raw.decode("utf-8", "replace").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    except OSError:
        pass
    return {"ok": True, "events": coalesce_events(events), "size": size, "reset": reset, "job": job.load()}


def coalesce_events(events: list[dict]) -> list[dict]:
    """Collapse token-sized provider events before any aiOS UI sees them."""
    result: list[dict] = []
    cursor_activity_ids: dict[str, str] = {}
    for source in events:
        event = dict(source)
        kind = str(event.get("kind") or "")
        if kind == "activity" and event.get("tool") and isinstance(event.get("arguments"), dict):
            fingerprint = _cursor_tool_fingerprint(str(event.get("tool")), event.get("arguments"))
            phase = str(event.get("phase") or "")
            if phase in {"started", "update"}:
                cursor_activity_ids.setdefault(fingerprint, str(event.get("activity_id") or ""))
            elif fingerprint in cursor_activity_ids:
                event["activity_id"] = cursor_activity_ids.pop(fingerprint)
        if kind == "assistant_delta":
            text = str(event.get("delta") or event.get("text") or "")
            if not text:
                continue
            if result and result[-1].get("_coalesce") == "assistant":
                current = str(result[-1].get("text") or "")
                if text == current:
                    continue
                if text.startswith(current):
                    text = text[len(current):]
                result[-1]["text"] = current + text
                result[-1]["delta"] = result[-1]["text"]
                result[-1]["ts"] = event.get("ts", result[-1].get("ts"))
            else:
                event.update(kind="assistant", role="assistant", text=text, delta=text, _coalesce="assistant")
                result.append(event)
            continue
        if (
            kind == "activity"
            and str(event.get("phase") or "") == "update"
            and event.get("delta")
            and result
            and result[-1].get("_coalesce") == "activity_delta"
            and result[-1].get("activity_id") == event.get("activity_id")
            and result[-1].get("stream") == event.get("stream")
        ):
            result[-1]["delta"] = str(result[-1].get("delta") or "") + str(event.get("delta") or "")
            result[-1]["ts"] = event.get("ts", result[-1].get("ts"))
            continue
        if kind == "activity" and str(event.get("phase") or "") == "update" and event.get("delta"):
            event["_coalesce"] = "activity_delta"
        result.append(event)
    for event in result:
        event.pop("_coalesce", None)
    return result


def events_file_for(job_id: str) -> Path | None:
    job = _get_job(job_id)
    return job.events_path if job else None


def provider_status(provider: str) -> tuple[bool, str]:
    if provider == "codex":
        path = find_codex()
        return (bool(path), "Codex is ready" if path else "Codex is not installed or cannot be located")
    if provider == "claude":
        path = find_claude()
        if not path:
            return False, "Claude Code is not installed or cannot be located"
        try:
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", path, "auth", "status"],
                capture_output=True,
                text=True,
                timeout=12,
                creationflags=CREATE_NO_WINDOW,
            )
            data = json.loads(result.stdout or "{}")
            return (bool(data.get("loggedIn")), "Claude is ready" if data.get("loggedIn") else "Claude Code is not signed in")
        except Exception:
            return True, "Claude CLI found; authentication could not be checked"
    if provider == "cursor":
        try:
            result = subprocess.run(
                ["wsl.exe", "-d", WSL_DISTRO, "--", CURSOR_AGENT, "status"],
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=CREATE_NO_WINDOW,
            )
            text = (result.stdout + result.stderr).strip()
            ready = result.returncode == 0 and "not logged in" not in text.casefold()
            return ready, "Cursor is ready" if ready else "Cursor Agent is installed in WSL but needs `cursor-agent login`"
        except Exception as exc:
            return False, f"Cursor Agent is unavailable in WSL: {exc}"
    return False, f"unknown provider: {provider}"


def setup_provider(provider: str) -> dict:
    """Open the provider's official interactive sign-in in a new terminal.

    This is deliberately opt-in: the web/native UI or voice agent calls it only
    after the user asks to set up an agent. Credentials remain owned by each
    provider CLI and never pass through aiOS.
    """
    provider = str(provider or "").strip().lower()
    if provider not in PROVIDERS:
        return {"ok": False, "error": "provider must be codex, claude, or cursor"}
    ready, message = provider_status(provider)
    if ready:
        return {"ok": True, "provider": provider, "launched": False, "message": message}
    if os.name != "nt":
        return {"ok": False, "error": "Interactive provider setup is currently available on Windows only."}

    if provider == "codex":
        executable = find_codex()
        if not executable:
            return {"ok": False, "error": message}
        title = "aiOS Codex sign-in"
        login_command = f'call "{executable}" login'
    elif provider == "claude":
        executable = find_claude()
        if not executable:
            return {"ok": False, "error": message}
        title = "aiOS Claude sign-in"
        login_command = f'call "{executable}" auth login'
    else:
        title = "aiOS Cursor sign-in"
        # wsl.exe parses the raw command line itself and keeps quotes as part of
        # the distro name, so these two arguments must stay unquoted.
        login_command = f"wsl.exe -d {WSL_DISTRO} -- {CURSOR_AGENT} login"

    # Pass one raw command line instead of an argument list: Python quotes list
    # arguments with backslash-escaped quotes, which cmd.exe does not
    # understand, so the sign-in path arrived as an unrecognised command.
    # cmd /k takes the whole rest of the line, so no outer quotes are needed.
    command_line = f"cmd.exe /d /k title {title} && {login_command}"
    try:
        subprocess.Popen(
            command_line,
            cwd=str(ROOT),
            creationflags=CREATE_NEW_CONSOLE,
            close_fds=True,
        )
    except OSError as exc:
        return {"ok": False, "error": f"Could not open {title}: {exc}"}
    return {
        "ok": True,
        "provider": provider,
        "launched": True,
        "message": f"{title} opened. Finish the provider's sign-in there, then refresh CODE.",
    }


def selection_error(provider: str, model: str, reasoning: str, fast: bool) -> dict | None:
    """Reject stale or invented settings instead of silently substituting."""
    snapshot = capabilities(force=False)
    info = next((row for row in snapshot.get("providers") or [] if row.get("provider") == provider), None)
    if not info:
        return {"ok": False, "error": f"No capabilities found for {provider}", "needs": ["provider"]}
    models = info.get("models") or []
    chosen = next((row for row in models if str(row.get("id") or "") == str(model)), None)
    if chosen is None:
        return {
            "ok": False,
            "error": f"{model!r} is not a current {provider} model. Choose an exact discovered model.",
            "needs": ["model"],
            "choices": [row.get("id") for row in models if row.get("id")],
        }
    efforts = [str(value) for value in chosen.get("reasoning") or []]
    if reasoning not in efforts:
        return {
            "ok": False,
            "error": f"{reasoning!r} is not supported by {model}. Choose an exact intelligence level.",
            "needs": ["reasoning"],
            "choices": efforts,
        }
    if fast and not chosen.get("fast"):
        return {
            "ok": False,
            "error": f"Fast mode is not available for {model}.",
            "needs": ["fast"],
            "choices": [False],
        }
    return None


def _codex_capabilities() -> dict:
    ready, message = provider_status("codex")
    data = {"provider": "codex", "ready": ready, "message": message, "models": []}
    if not ready:
        return data
    rpc: JsonRpcProcess | None = None
    try:
        codex = find_codex()
        assert codex
        rpc = JsonRpcProcess([codex, "app-server"], ROOT)
        rpc.start()
        rpc.request("initialize", {"clientInfo": {"name": "aios_code_probe", "title": "aiOS CODE", "version": "1.0"}}, 25)
        rpc.notify("initialized")
        result = rpc.request("model/list", {"limit": 100, "includeHidden": False}, 30)
        for item in result.get("data") or []:
            efforts = [
                row.get("reasoningEffort")
                for row in item.get("supportedReasoningEfforts") or []
                if row.get("reasoningEffort")
            ]
            data["models"].append({
                "id": item.get("model") or item.get("id"),
                "label": item.get("displayName") or item.get("model") or item.get("id"),
                "reasoning": efforts or [item.get("defaultReasoningEffort") or "medium"],
                "default_reasoning": item.get("defaultReasoningEffort") or "medium",
                "fast": str(item.get("model") or item.get("id") or "").startswith("gpt-5.6"),
                "input_modalities": item.get("inputModalities") or ["text", "image"],
                "default": bool(item.get("isDefault")),
            })
    except Exception as exc:
        data["message"] = f"Codex found, but live model discovery failed: {exc}"
        data["models"] = [
            {"id": "gpt-5.6-sol", "label": "GPT-5.6 Sol", "reasoning": ["low", "medium", "high", "xhigh"], "default_reasoning": "medium", "fast": True, "default": True},
            {"id": "gpt-5.6-terra", "label": "GPT-5.6 Terra", "reasoning": ["low", "medium", "high"], "default_reasoning": "medium", "fast": True},
            {"id": "gpt-5.6-luna", "label": "GPT-5.6 Luna", "reasoning": ["none", "low", "medium"], "default_reasoning": "low", "fast": True},
        ]
    finally:
        if rpc:
            rpc.stop()
    return data


def _claude_capabilities() -> dict:
    ready, message = provider_status("claude")
    return {
        "provider": "claude",
        "ready": ready,
        "message": message,
        "models": [
            {"id": "sonnet", "label": "Claude Sonnet", "reasoning": ["low", "medium", "high", "xhigh", "max"], "default_reasoning": "high", "fast": False, "default": True},
            {"id": "opus", "label": "Claude Opus", "reasoning": ["low", "medium", "high", "xhigh", "max"], "default_reasoning": "high", "fast": False},
            {"id": "fable", "label": "Claude Fable", "reasoning": ["low", "medium", "high", "xhigh", "max"], "default_reasoning": "medium", "fast": False},
        ],
    }


def _parse_cursor_models(text: str) -> list[dict]:
    def row_for(model_id: str, label: str, *, default: bool = False) -> dict:
        lowered = model_id.casefold()
        tokens = lowered.split("-")
        if "-extra-high" in lowered:
            effort = "xhigh"
        else:
            effort = next(
                (token for token in reversed(tokens) if token in {"none", "low", "medium", "high", "xhigh", "max"}),
                "auto",
            )
        return {
            "id": model_id,
            "label": label,
            # Cursor exposes separate exact ids for effort and fast variants.
            # Keep the controls descriptive and never synthesize a new id.
            "reasoning": [effort],
            "default_reasoning": effort,
            "fast": False,
            "intrinsic_fast": lowered.endswith("-fast"),
            "default": bool(default or lowered == "auto"),
        }

    models: list[dict] = []
    seen: set[str] = set()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    rows = payload if isinstance(payload, list) else (payload.get("models") if isinstance(payload, dict) else None)
    if rows:
        for row in rows:
            if isinstance(row, str):
                model_id, label = row, row
            else:
                model_id = str(row.get("id") or row.get("model") or row.get("slug") or "")
                label = str(row.get("name") or row.get("displayName") or model_id)
            if model_id and model_id not in seen:
                seen.add(model_id)
                is_default = bool(row.get("default") or row.get("isDefault")) if isinstance(row, dict) else model_id.casefold() == "auto"
                models.append(row_for(model_id, label, default=is_default))
        return models
    for line in text.splitlines():
        clean = re.sub(r"^[\s*•>\-\d.)]+", "", line).strip()
        match = re.match(r"([A-Za-z0-9][A-Za-z0-9._:/+\-]*(?:\[[^]]+\])?)", clean)
        if not match:
            continue
        model_id = match.group(1)
        if model_id.casefold() in {"available", "models", "model"} or model_id in seen:
            continue
        seen.add(model_id)
        parts = re.split(r"\s+-\s+", clean, maxsplit=1)
        label = parts[1].strip() if len(parts) == 2 else model_id
        is_default = "(default)" in label.casefold() or model_id.casefold() == "auto"
        label = re.sub(r"\s*\(default\)\s*$", "", label, flags=re.IGNORECASE).strip()
        models.append(row_for(model_id, label or model_id, default=is_default))
    return models


def _cursor_capabilities() -> dict:
    ready, message = provider_status("cursor")
    data = {"provider": "cursor", "ready": ready, "message": message, "models": []}
    if ready:
        try:
            result = subprocess.run(
                ["wsl.exe", "-d", WSL_DISTRO, "--", CURSOR_AGENT, "--list-models"],
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=CREATE_NO_WINDOW,
            )
            data["models"] = _parse_cursor_models(result.stdout)
            if result.returncode != 0:
                data["message"] = (result.stderr or result.stdout or message).strip()
        except Exception as exc:
            data["message"] = f"Cursor model discovery failed: {exc}"
    return data


def capabilities(force: bool = False) -> dict:
    global _CAPABILITIES_MEMORY, _CAPABILITIES_AT
    with _CAPABILITIES_LOCK:
        if not force and _CAPABILITIES_MEMORY and time.time() - _CAPABILITIES_AT < 300:
            return _CAPABILITIES_MEMORY
        providers = [_codex_capabilities(), _claude_capabilities(), _cursor_capabilities()]
        payload = {"ok": True, "updated_at": _now(), "providers": providers}
        _CAPABILITIES_MEMORY = payload
        _CAPABILITIES_AT = time.time()
        try:
            _atomic_json(CAPABILITIES_CACHE, payload)
        except OSError:
            pass
        return payload


def recover_interrupted() -> None:
    try:
        directories = [path for path in JOBS_DIR.iterdir() if path.is_dir()]
    except OSError:
        directories = []
    for directory in directories:
        try:
            meta = json.loads((directory / "job.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if meta.get("status") not in ACTIVE_STATES:
            continue
        job = _get_job(str(meta.get("id") or directory.name))
        if job:
            job.save(status="interrupted", queued=0, completed_at=_now())
            job.append("status", "Interrupted by aiOS or PC restart", notify=True, state="interrupted")
