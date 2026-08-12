"""aiOS Director client for this Windows desktop.

Dials out to Director over a WebSocket and stays connected, so nothing has to
listen on this machine and no port has to be opened. Director sends calls; this
answers them.

What it offers Director:

    code.start / code.status / code.stop   CODE sessions through code_jobs.py,
                                           the same harness the aiOS CODE tab
                                           drives, so a session started from
                                           the phone shows up there too.
    shell / read_file / write_file         approval-gated access to this box,
                                           for jobs that are specifically about
                                           files or environment here.

Run it with the desktop:

    pythonw director_client.py

Configuration lives in aios_director_client.json next to this file:

    {"url": "https://rocky-server.tail4d08fd.ts.net/director",
     "token": "<from: director.cli enroll-machine>",
     "name": "calle-windows"}
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import platform
import socket
import subprocess
import sys
import time
import traceback

try:
    import aiohttp
except ImportError:  # pragma: no cover
    print("director_client needs aiohttp:  pip install aiohttp", file=sys.stderr)
    raise

ROOT = pathlib.Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "aios_director_client.json"
LOG_PATH = ROOT / ".aios-director-client.log"

RECONNECT_MIN = 2.0
RECONNECT_MAX = 60.0
SHELL_TIMEOUT = 180

CAPS = {"code": True, "shell": True, "files": True, "platform": "windows"}


def log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line, flush=True)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def load_config() -> dict:
    if not CONFIG_PATH.is_file():
        raise SystemExit(
            f"No {CONFIG_PATH.name}. Create it with the token from the Linux box:\n"
            "  ssh calle@192.168.0.17\n"
            "  cd ~/aios-director && .venv/bin/python -m director.cli "
            "enroll-machine --name calle-windows\n")
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    for key in ("url", "token"):
        if not str(data.get(key) or "").strip():
            raise SystemExit(f"{CONFIG_PATH.name} is missing {key!r}")
    data.setdefault("name", socket.gethostname())
    return data


# ---------------- CODE sessions ----------------

# Reasoning level for each provider's default model, matching the harness's own
# catalogue (gpt-5.6-sol -> medium, sonnet -> high, and so on).
DEFAULT_REASONING = {
    "codex": "medium",      # gpt-5.6-sol
    "claude": "high",       # sonnet
    "cursor": "medium",
    "ollama": "medium",
    "openrouter": "medium",
}

# Statuses code_jobs writes into job.json that mean the session is over.
TERMINAL_OK = {"done", "completed", "finished", "ready"}
TERMINAL_BAD = {"failed", "error", "stopped", "cancelled", "interrupted"}


class CodeBridge:
    """Wraps code_jobs so Director can start and follow a session.

    The harness is imported lazily so the client still connects and answers
    shell calls on a machine where CODE cannot import for some reason. Every
    call into it goes through the signatures code_jobs actually exposes:
    create_job(provider, cwd, brief, model, reasoning), get_job(id),
    send_message(id, text), stop_job(id).
    """

    def __init__(self) -> None:
        self._jobs = None
        self._sessions: dict[str, dict] = {}

    def harness(self):
        if self._jobs is None:
            sys.path.insert(0, str(ROOT))
            import code_jobs  # noqa: PLC0415  (deliberately lazy)
            self._jobs = code_jobs
        return self._jobs

    def available(self) -> tuple[bool, str]:
        try:
            self.harness()
        except Exception as exc:
            return False, f"CODE harness unavailable: {type(exc).__name__}: {exc}"
        return True, "ready"

    def start(self, payload: dict) -> dict:
        jobs = self.harness()
        task = str(payload.get("task") or "").strip()
        project = str(payload.get("project") or "").strip() or str(ROOT)
        provider = str(payload.get("provider") or "").strip().lower() or "codex"
        if provider not in jobs.PROVIDERS:
            return {"ok": False, "error": f"provider must be one of {', '.join(jobs.PROVIDERS)}"}

        cwd = pathlib.Path(project).expanduser()
        if not cwd.is_dir():
            return {"ok": False, "error": f"no such project directory: {cwd}"}

        model = str(payload.get("model") or "").strip() or jobs.DEFAULT_MODELS.get(provider, "")
        reasoning = str(payload.get("reasoning") or "").strip() or DEFAULT_REASONING.get(provider, "medium")

        result = jobs.create_job(
            provider=provider, cwd=str(cwd), brief=task, model=model,
            reasoning=reasoning, title=str(payload.get("title") or "")[:120])
        if not isinstance(result, dict):
            return {"ok": False, "error": "code_jobs.create_job returned no job"}
        if result.get("ok") is False:
            return {"ok": False, "error": str(result.get("error") or "create_job refused")}

        session_id = str(result.get("id") or (result.get("job") or {}).get("id") or "")
        if not session_id:
            return {"ok": False, "error": "code_jobs.create_job returned no job id"}
        self._sessions[session_id] = {"started": time.time(),
                                      "director_job": payload.get("job_id", "")}
        return {"ok": True, "session_id": session_id, "project": str(cwd),
                "provider": provider, "model": model}

    def status(self, session_id: str) -> dict:
        jobs = self.harness()
        try:
            meta = jobs.get_job(session_id)
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if not meta:
            return {"ok": False, "error": "no such CODE session"}
        return {
            "ok": True,
            "status": str(meta.get("status") or "running"),
            "summary": str(meta.get("last_summary") or meta.get("title") or ""),
            "title": str(meta.get("title") or ""),
            "provider": str(meta.get("provider") or ""),
            "model": str(meta.get("model") or ""),
        }

    def send(self, session_id: str, text: str) -> dict:
        jobs = self.harness()
        return jobs.send_message(session_id, text)

    def stop(self, session_id: str) -> dict:
        jobs = self.harness()
        return jobs.stop_job(session_id)


# ---------------- the link ----------------

class DirectorClient:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.url = str(config["url"]).rstrip("/")
        self.token = str(config["token"])
        self.name = str(config["name"])
        self.code = CodeBridge()
        self.session: aiohttp.ClientSession | None = None
        self.socket: aiohttp.ClientWebSocketResponse | None = None

    # --- transport ---

    def ws_url(self) -> str:
        return f"{self.url.replace('https://', 'wss://').replace('http://', 'ws://')}/machine"

    async def run_forever(self) -> None:
        delay = RECONNECT_MIN
        while True:
            try:
                await self.connect_once()
                delay = RECONNECT_MIN
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log(f"link down: {type(exc).__name__}: {exc}")
            log(f"reconnecting in {delay:.0f}s")
            await asyncio.sleep(delay)
            delay = min(delay * 1.8, RECONNECT_MAX)

    async def connect_once(self) -> None:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            self.session = session
            headers = {"Authorization": f"Bearer {self.token}"}
            async with session.ws_connect(self.ws_url(), headers=headers,
                                          heartbeat=30.0) as socket:
                self.socket = socket
                log(f"connected to {self.url} as {self.name}")
                async for message in socket:
                    if message.type != aiohttp.WSMsgType.TEXT:
                        continue
                    try:
                        payload = json.loads(message.data)
                    except json.JSONDecodeError:
                        continue
                    if payload.get("type") == "call":
                        asyncio.create_task(self.handle_call(payload))
        self.socket = None
        log("socket closed")

    async def reply(self, call_id: str, result: dict) -> None:
        if self.socket is None or self.socket.closed:
            return
        await self.socket.send_json({"type": "result", "call_id": call_id, "result": result})

    async def report_job(self, job_id: str, status: str, result: dict) -> None:
        if self.socket is None or self.socket.closed:
            return
        await self.socket.send_json({"type": "job", "job_id": job_id,
                                     "status": status, "result": result})

    async def emit(self, job_id: str, kind: str, payload: dict) -> None:
        if self.socket is None or self.socket.closed:
            return
        await self.socket.send_json({"type": "event", "job_id": job_id,
                                     "kind": kind, "payload": payload})

    # --- calls ---

    async def handle_call(self, message: dict) -> None:
        call_id = str(message.get("call_id") or "")
        action = str(message.get("action") or "")
        payload = dict(message.get("payload") or {})
        try:
            handler = getattr(self, f"do_{action.replace('.', '_')}", None)
            if handler is None:
                await self.reply(call_id, {"ok": False, "error": f"unknown action {action}"})
                return
            await self.reply(call_id, await handler(payload))
        except Exception as exc:
            log(f"call {action} failed:\n{traceback.format_exc()}")
            await self.reply(call_id, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    async def do_ping(self, payload: dict) -> dict:
        return {"ok": True, "name": self.name, "platform": platform.platform()}

    async def do_code_start(self, payload: dict) -> dict:
        ready, message = self.code.available()
        if not ready:
            return {"ok": False, "error": message}
        result = await asyncio.get_running_loop().run_in_executor(
            None, self.code.start, payload)
        if result.get("ok"):
            asyncio.create_task(self.follow_session(str(result["session_id"]),
                                                    str(payload.get("job_id") or "")))
        return result

    async def do_code_status(self, payload: dict) -> dict:
        return await asyncio.get_running_loop().run_in_executor(
            None, self.code.status, str(payload.get("session_id") or ""))

    async def do_code_send(self, payload: dict) -> dict:
        return await asyncio.get_running_loop().run_in_executor(
            None, self.code.send, str(payload.get("session_id") or ""),
            str(payload.get("text") or ""))

    async def do_code_stop(self, payload: dict) -> dict:
        return await asyncio.get_running_loop().run_in_executor(
            None, self.code.stop, str(payload.get("session_id") or ""))

    async def follow_session(self, session_id: str, job_id: str) -> None:
        """Poll the CODE session and stream its state back to Director."""
        last = ""
        deadline = time.time() + 7200
        while time.time() < deadline:
            await asyncio.sleep(5)
            info = await asyncio.get_running_loop().run_in_executor(
                None, self.code.status, session_id)
            if not info.get("ok"):
                await self.report_job(job_id, "fail",
                                      {"summary": info.get("error", "lost the session")})
                return
            status = str(info.get("status") or "running").lower()
            summary = str(info.get("summary") or "")
            if summary and summary != last:
                last = summary
                await self.emit(job_id, "code.progress",
                                {"job_id": job_id, "title": summary[:120], "status": status})
            if status in TERMINAL_OK or status in TERMINAL_BAD:
                await self.report_job(job_id, "done" if status in TERMINAL_OK else "fail",
                                      {"summary": summary or status})
                return
        await self.report_job(job_id, "stopped", {"summary": "session ran past two hours"})

    async def do_shell(self, payload: dict) -> dict:
        command = str(payload.get("command") or "").strip()
        if not command:
            return {"ok": False, "error": "no command"}
        cwd = str(payload.get("cwd") or ROOT)
        proc = await asyncio.create_subprocess_shell(
            command, cwd=cwd, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            raw, _ = await asyncio.wait_for(proc.communicate(), timeout=SHELL_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            return {"ok": False, "error": f"timed out after {SHELL_TIMEOUT}s"}
        return {"ok": True, "exit_code": proc.returncode or 0,
                "output": raw.decode("utf-8", errors="replace")[:8000]}

    async def do_read_file(self, payload: dict) -> dict:
        target = pathlib.Path(str(payload.get("path") or "")).expanduser()
        if not target.is_file():
            return {"ok": False, "error": f"no such file: {target}"}
        return {"ok": True, "content": target.read_text(encoding="utf-8", errors="replace")[:80000]}

    async def do_write_file(self, payload: dict) -> dict:
        target = pathlib.Path(str(payload.get("path") or "")).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(payload.get("content") or ""), encoding="utf-8")
        return {"ok": True, "path": str(target)}

    async def do_chat_send(self, payload: dict) -> dict:
        """Let Director push a line into the desktop's own UI later on."""
        return {"ok": True, "noted": str(payload.get("text") or "")[:200]}


def main() -> int:
    config = load_config()
    log(f"aiOS Director client starting for {config['name']}")
    client = DirectorClient(config)
    try:
        asyncio.run(client.run_forever())
    except KeyboardInterrupt:
        log("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
