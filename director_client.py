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
import re
import socket
import subprocess
import sys
import time
import traceback
import uuid

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

CAPS = {"code": True, "shell": True, "files": True, "power": True,
        "platform": "windows"}

_SKIP_ADAPTER = re.compile(
    r"WSL|Hyper-V|vEthernet|Bluetooth|Loopback|Tailscale|WireGuard|VPN|"
    r"Virtual|Local Area Connection\*|lcvpn|Wi-Fi Direct",
    re.I,
)


def _format_mac(raw: str) -> str:
    hexed = re.sub(r"[^0-9A-Fa-f]", "", raw or "")
    if len(hexed) != 12:
        return ""
    return ":".join(hexed[i:i + 2] for i in range(0, 12, 2)).upper()


def parse_ipconfig(text: str) -> dict:
    """Pick the house LAN adapter from `ipconfig /all` text."""
    current = ""
    mac = ""
    ip = ""
    best: dict = {}

    def consider() -> dict | None:
        nonlocal best
        if not current or _SKIP_ADAPTER.search(current) or not mac:
            return None
        row = {"name": current, "mac": mac, "ip": ip}
        if ip.startswith("192.168."):
            return row
        if not best:
            best = row
        return None

    for line in (text or "").splitlines():
        name_match = re.match(r"^.+ adapter (.+):", line, re.I)
        if name_match:
            hit = consider()
            if hit:
                return hit
            current = name_match.group(1).strip()
            mac = ""
            ip = ""
            continue
        mac_match = re.search(r"Physical Address[ .]*:\s*([0-9A-Fa-f-]+)", line)
        if mac_match:
            mac = _format_mac(mac_match.group(1))
        ip_match = re.search(r"IPv4 Address[ .]*:\s*([0-9.]+)", line)
        if ip_match:
            ip = ip_match.group(1)
    return consider() or best


def lan_identity() -> dict:
    """MAC + IPv4 of the adapter Director should wake."""
    ip = ""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("192.168.0.17", 80))
        ip = probe.getsockname()[0]
        probe.close()
    except OSError:
        ip = ""
    info: dict = {}
    if sys.platform == "win32":
        try:
            raw = subprocess.check_output(
                ["ipconfig", "/all"], text=True, encoding="oem", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            info = parse_ipconfig(raw)
        except (OSError, subprocess.CalledProcessError):
            info = {}
    if not info.get("mac"):
        info["mac"] = _format_mac(f"{uuid.getnode():012x}")
    if ip and not info.get("ip"):
        info["ip"] = ip
    elif ip and str(info.get("ip") or "").startswith("192.168."):
        pass
    elif ip:
        info["ip"] = ip
    if info.get("ip") and info["ip"].count(".") == 3:
        info["broadcast"] = ".".join(info["ip"].split(".")[:3] + ["255"])
    return {k: v for k, v in info.items() if v}


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

# Same saved preset the CODE tab / voice agent treat as the recommended default.
DEFAULT_CODE_CONFIG_ID = "harness-balanced-engineering"
DEFAULT_CODE_CONFIG_NAME = "Balanced Engineering"

# Statuses code_jobs writes into job.json that mean the session is over.
TERMINAL_OK = {"done", "completed", "finished", "ready"}
TERMINAL_BAD = {"failed", "error", "stopped", "cancelled", "interrupted"}


class CodeBridge:
    """Wraps code_jobs so Director can start and follow a session.

    The harness is imported lazily so the client still connects and answers
    shell calls on a machine where CODE cannot import for some reason. Every
    call into it goes through the signatures code_jobs actually exposes:
    create_job(...), get_job(id), read_events(id, since), send_message, stop_job.
    """

    def __init__(self) -> None:
        self._jobs = None
        self._roles = None
        self._sessions: dict[str, dict] = {}

    def harness(self):
        if self._jobs is None:
            sys.path.insert(0, str(ROOT))
            import code_jobs  # noqa: PLC0415  (deliberately lazy)
            self._jobs = code_jobs
        return self._jobs

    def roles(self):
        if self._roles is None:
            sys.path.insert(0, str(ROOT))
            import code_roles  # noqa: PLC0415  (deliberately lazy)
            self._roles = code_roles
        return self._roles

    def available(self) -> tuple[bool, str]:
        try:
            self.harness()
        except Exception as exc:
            return False, f"CODE harness unavailable: {type(exc).__name__}: {exc}"
        return True, "ready"

    def list_configs(self) -> dict:
        try:
            roles = self.roles()
            configs = roles.load_model_configs()
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "configs": []}
        rows = []
        for row in configs:
            if not isinstance(row, dict):
                continue
            rows.append({
                "id": str(row.get("id") or ""),
                "name": str(row.get("name") or ""),
                "description": str(row.get("description") or "")[:240],
                "provider": str(row.get("provider") or ""),
                "strategy": str(row.get("strategy") or "auto"),
                "review_fix": bool(row.get("review_fix")),
                "show_in_composer": bool(row.get("show_in_composer", True)),
                "roles": row.get("roles") if isinstance(row.get("roles"), dict) else {},
            })
        return {"ok": True, "configs": rows,
                "default_id": DEFAULT_CODE_CONFIG_ID,
                "default_name": DEFAULT_CODE_CONFIG_NAME}

    def _find_config(self, config_id: str = "", config_name: str = "") -> dict | None:
        listed = self.list_configs()
        rows = listed.get("configs") if listed.get("ok") else []
        wanted_id = str(config_id or "").strip().casefold()
        wanted_name = str(config_name or "").strip().casefold()
        if wanted_id:
            for row in rows or []:
                if str(row.get("id") or "").casefold() == wanted_id:
                    return row
        if wanted_name:
            matches = [row for row in (rows or [])
                       if wanted_name in str(row.get("name") or "").casefold()]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                return None
        return None

    def _resolve_launch(self, payload: dict) -> dict:
        """Turn Director args into a create_job payload.

        Preference order:
          1. explicit config_id / config_name
          2. explicit provider (+ model/reasoning/fast)
          3. Balanced Engineering saved preset
          4. provider-only harness defaults (last resort)
        """
        jobs = self.harness()
        config_id = str(payload.get("config_id") or "").strip()
        config_name = str(payload.get("config_name") or "").strip()
        provider = str(payload.get("provider") or "").strip().lower()
        model = str(payload.get("model") or "").strip()
        reasoning = str(payload.get("reasoning") or "").strip().lower()
        strategy = str(payload.get("strategy") or "").strip().lower()
        has_fast = "fast" in payload
        fast = bool(payload.get("fast")) if has_fast else None

        config = None
        if config_id or config_name:
            config = self._find_config(config_id, config_name)
            if config is None:
                label = config_id or config_name
                return {"ok": False, "error": f"unknown CODE configuration: {label}"}
        elif not provider and not model:
            config = self._find_config(DEFAULT_CODE_CONFIG_ID, DEFAULT_CODE_CONFIG_NAME)

        role_config = None
        review_fix = None
        resolved_id = ""
        resolved_name = ""
        if config is not None:
            roles = config.get("roles") if isinstance(config.get("roles"), dict) else {}
            coder = roles.get("coder") if isinstance(roles.get("coder"), dict) else {}
            provider = str(config.get("provider") or provider or "").strip().lower()
            model = str(coder.get("model") or model or "").strip()
            reasoning = str(coder.get("reasoning") or reasoning or "").strip().lower()
            fast = bool(coder.get("fast")) if fast is None else bool(fast)
            strategy = str(config.get("strategy") or strategy or "auto").strip().lower()
            review_fix = bool(config.get("review_fix"))
            role_config = roles
            resolved_id = str(config.get("id") or "")
            resolved_name = str(config.get("name") or "")
        else:
            provider = provider or "codex"
            model = model or jobs.DEFAULT_MODELS.get(provider, "")
            reasoning = reasoning or DEFAULT_REASONING.get(provider, "medium")
            fast = bool(fast) if fast is not None else False
            strategy = strategy or "auto"

        if provider not in jobs.PROVIDERS:
            return {"ok": False, "error": f"provider must be one of {', '.join(jobs.PROVIDERS)}"}
        if not model:
            return {"ok": False, "error": "exact model is required"}
        if not reasoning:
            return {"ok": False, "error": "reasoning/intelligence level is required"}

        return {
            "ok": True,
            "provider": provider,
            "model": model,
            "reasoning": reasoning,
            "fast": bool(fast),
            "strategy": strategy or "auto",
            "review_fix": review_fix,
            "role_config": role_config,
            "config_id": resolved_id,
            "config_name": resolved_name,
        }

    def start(self, payload: dict) -> dict:
        jobs = self.harness()
        task = str(payload.get("task") or "").strip()
        project = str(payload.get("project") or "").strip() or str(ROOT)
        if not task:
            return {"ok": False, "error": "no task given"}

        cwd = pathlib.Path(project).expanduser()
        if not cwd.is_dir():
            return {"ok": False, "error": f"no such project directory: {cwd}"}

        resolved = self._resolve_launch(payload)
        if not resolved.get("ok"):
            return resolved

        create_kwargs = {
            "provider": resolved["provider"],
            "cwd": str(cwd),
            "brief": task,
            "model": resolved["model"],
            "reasoning": resolved["reasoning"],
            "fast": resolved["fast"],
            "title": str(payload.get("title") or "")[:120],
            "strategy": resolved["strategy"],
            "config_id": resolved["config_id"],
            "config_name": resolved["config_name"],
        }
        if resolved.get("role_config") is not None:
            create_kwargs["role_config"] = resolved["role_config"]
        if resolved.get("review_fix") is not None:
            create_kwargs["review_fix"] = resolved["review_fix"]

        result = jobs.create_job(**create_kwargs)
        if not isinstance(result, dict):
            return {"ok": False, "error": "code_jobs.create_job returned no job"}
        if result.get("ok") is False:
            return {"ok": False, "error": str(result.get("error") or "create_job refused")}

        session_id = str(result.get("id") or (result.get("job") or {}).get("id") or "")
        if not session_id:
            return {"ok": False, "error": "code_jobs.create_job returned no job id"}
        self._sessions[session_id] = {"started": time.time(),
                                      "director_job": payload.get("job_id", "")}
        return {
            "ok": True,
            "session_id": session_id,
            "project": str(cwd),
            "provider": resolved["provider"],
            "model": resolved["model"],
            "reasoning": resolved["reasoning"],
            "fast": resolved["fast"],
            "strategy": resolved["strategy"],
            "config_id": resolved["config_id"],
            "config_name": resolved["config_name"],
        }

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
            "config_id": str(meta.get("config_id") or ""),
            "config_name": str(meta.get("config_name") or ""),
        }

    def events(self, session_id: str, since: int = 0) -> dict:
        jobs = self.harness()
        try:
            return jobs.read_events(session_id, int(since or 0))
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "events": [], "size": int(since or 0)}

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
                hello = lan_identity()
                hello["type"] = "hello"
                hello["name"] = self.name
                hello["caps"] = CAPS
                try:
                    await socket.send_json(hello)
                    log(f"lan {hello.get('ip', '?')} {hello.get('mac', '?')}")
                except Exception as exc:
                    log(f"hello failed: {type(exc).__name__}: {exc}")
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

    async def do_power_off(self, payload: dict) -> dict:
        """Schedule shutdown after replying, so Director receives the result."""
        if sys.platform != "win32":
            return {"ok": False, "error": "power off is only available on Windows"}
        proc = await asyncio.create_subprocess_exec(
            "shutdown.exe", "/s", "/t", "5", "/c",
            "Turned off from aiOS Director",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        raw, _ = await proc.communicate()
        if proc.returncode:
            return {"ok": False, "error": raw.decode(
                "utf-8", errors="replace").strip() or "shutdown.exe failed"}
        return {"ok": True, "status": "shutdown scheduled", "delay": 5}

    async def do_code_start(self, payload: dict) -> dict:
        ready, message = self.code.available()
        if not ready:
            return {"ok": False, "error": message}
        result = await asyncio.get_running_loop().run_in_executor(
            None, self.code.start, payload)
        if result.get("ok"):
            asyncio.create_task(self.follow_session(str(result["session_id"]),
                                                    str(payload.get("job_id") or ""),
                                                    result))
        return result

    async def do_code_status(self, payload: dict) -> dict:
        return await asyncio.get_running_loop().run_in_executor(
            None, self.code.status, str(payload.get("session_id") or ""))

    async def do_code_configs(self, payload: dict) -> dict:
        return await asyncio.get_running_loop().run_in_executor(
            None, self.code.list_configs)

    async def do_code_events(self, payload: dict) -> dict:
        session_id = str(payload.get("session_id") or "")
        since = int(payload.get("since") or 0)
        return await asyncio.get_running_loop().run_in_executor(
            None, self.code.events, session_id, since)

    async def do_code_send(self, payload: dict) -> dict:
        return await asyncio.get_running_loop().run_in_executor(
            None, self.code.send, str(payload.get("session_id") or ""),
            str(payload.get("text") or ""))

    async def do_code_stop(self, payload: dict) -> dict:
        return await asyncio.get_running_loop().run_in_executor(
            None, self.code.stop, str(payload.get("session_id") or ""))

    async def follow_session(self, session_id: str, job_id: str,
                             meta: dict | None = None) -> None:
        """Poll the CODE session and stream its events/state back to Director."""
        last = ""
        since = 0
        meta = dict(meta or {})
        deadline = time.time() + 7200
        ticks = 0
        while time.time() < deadline:
            await asyncio.sleep(1.0)
            ticks += 1
            events = await asyncio.get_running_loop().run_in_executor(
                None, self.code.events, session_id, since)
            if events.get("ok"):
                if events.get("reset"):
                    since = 0
                batch = events.get("events") or []
                size = int(events.get("size") or since)
                if batch:
                    await self.emit(job_id, "code.events", {
                        "job_id": job_id,
                        "session_id": session_id,
                        "events": batch,
                        "size": size,
                        "reset": bool(events.get("reset")),
                    })
                since = size

            # Status is cheaper than full meta; check every ~5s.
            if ticks % 5 != 0:
                continue
            info = await asyncio.get_running_loop().run_in_executor(
                None, self.code.status, session_id)
            if not info.get("ok"):
                await self.report_job(job_id, "fail", {
                    "summary": info.get("error", "lost the session"),
                    "session_id": session_id,
                    "config_id": meta.get("config_id") or "",
                    "config_name": meta.get("config_name") or "",
                    "provider": meta.get("provider") or "",
                    "model": meta.get("model") or "",
                })
                return
            status = str(info.get("status") or "running").lower()
            summary = str(info.get("summary") or "")
            if summary and summary != last:
                last = summary
                await self.emit(job_id, "code.progress",
                                {"job_id": job_id, "session_id": session_id,
                                 "title": summary[:120], "status": status})
            if status in TERMINAL_OK or status in TERMINAL_BAD:
                await self.report_job(
                    job_id, "done" if status in TERMINAL_OK else "fail",
                    {"summary": summary or status,
                     "session_id": session_id,
                     "config_id": info.get("config_id") or meta.get("config_id") or "",
                     "config_name": info.get("config_name") or meta.get("config_name") or "",
                     "provider": info.get("provider") or meta.get("provider") or "",
                     "model": info.get("model") or meta.get("model") or ""})
                return
        await self.report_job(job_id, "stopped", {
            "summary": "session ran past two hours",
            "session_id": session_id,
            "config_id": meta.get("config_id") or "",
            "config_name": meta.get("config_name") or "",
            "provider": meta.get("provider") or "",
            "model": meta.get("model") or "",
        })

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
