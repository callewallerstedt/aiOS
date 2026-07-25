"""Secure bridge between the local aiOS helper and the hosted aiOS Remote relay."""

from __future__ import annotations

import argparse
import concurrent.futures
import ctypes
import json
import os
from pathlib import Path
import platform
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import httpx

import aios_codex_accounts
from prompt_clarifier import clarify_prompt_for_provider, normalize_questions


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "helper_config.json"
STATE_PATH = ROOT / "phone-relay-state.json"
HEARTBEAT_PATH = ROOT / ".aios-phone-relay-heartbeat"
STREAM_HEALTH_PATH = ROOT / ".aios-stream-health.json"
LOCAL_EVENTS_PATH = ROOT / "phone_operator_events" / "events.jsonl"
UPDATE_REQUEST_PATH = ROOT / ".aios-update-request"
LOCAL_BASE = "http://127.0.0.1:5000"
DEFAULT_RELAY = os.environ.get("AIOS_RELAY_URL", "").rstrip("/")
MODEL_MAP = {
    "luna": "gpt-5.6-luna",
    "terra": "gpt-5.6-terra",
    "sol": "gpt-5.6-sol",
}
RELAY_MUTEX_NAME = "Local\\aiOS.PhoneRelay.Singleton"
_RELAY_MUTEX_HANDLE = None


def claim_single_instance() -> bool:
    global _RELAY_MUTEX_HANDLE
    if os.name != "nt":
        return True
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, RELAY_MUTEX_NAME)
    if not handle:
        return True
    if kernel32.GetLastError() == 183:
        kernel32.CloseHandle(handle)
        return False
    _RELAY_MUTEX_HANDLE = handle
    return True


class FrameStreamer:
    """Continuously publish the display the phone is actively watching.

    This runs separately from command/event polling so a slow frame can never
    delay a Stop command. The phone refreshes the lease while visible; without
    a lease we drop to one frame per second to keep bandwidth reasonable.
    """

    def __init__(self, bridge):
        self.bridge = bridge
        self.target_fps = max(10.0, min(20.0, float(os.environ.get("AIOS_PHONE_STREAM_FPS", "12"))))
        self.monitor_id = ""
        self.active_until = time.monotonic() + 15.0
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._frame_times = []
        self._sequence = 0
        self._last_health_write = 0.0
        self._stats = {
            "fps": 0.0, "target_fps": self.target_fps, "active": False,
            "capture_ms": 0, "upload_ms": 0, "monitor_id": "",
            "last_frame_at": 0, "error": "",
        }

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="aios-live-desktop", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def select_monitor(self, monitor_id: str, lease_seconds: float = 15.0):
        self.monitor_id = str(monitor_id or "")
        self.active_until = time.monotonic() + max(5.0, min(60.0, float(lease_seconds)))
        self._wake.set()

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def _chosen_monitor(self) -> str:
        available = [str(item.get("id")) for item in self.bridge.monitors if item.get("id") is not None]
        if self.monitor_id in available:
            return self.monitor_id
        return available[0] if available else "1"

    def _record(self, *, monitor_id, capture_ms=0, upload_ms=0, error=""):
        now = time.monotonic()
        with self._lock:
            if not error:
                self._frame_times.append(now)
                cutoff = now - 2.0
                self._frame_times = [stamp for stamp in self._frame_times if stamp >= cutoff]
            fps = 0.0
            if len(self._frame_times) >= 2:
                span = self._frame_times[-1] - self._frame_times[0]
                if span > 0:
                    fps = (len(self._frame_times) - 1) / span
            self._stats.update({
                "fps": round(fps, 1),
                "active": time.monotonic() < self.active_until,
                "capture_ms": int(capture_ms),
                "upload_ms": int(upload_ms),
                "monitor_id": monitor_id,
                "last_frame_at": int(time.time() * 1000) if not error else self._stats.get("last_frame_at", 0),
                "error": str(error)[:180],
            })
            snapshot = dict(self._stats)
        if now - self._last_health_write >= 0.5:
            self._last_health_write = now
            temp = STREAM_HEALTH_PATH.with_suffix(".json.tmp")
            try:
                temp.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
                temp.replace(STREAM_HEALTH_PATH)
            except OSError:
                pass

    def _run(self):
        local_limits = httpx.Limits(max_keepalive_connections=2, max_connections=2)
        remote_limits = httpx.Limits(max_keepalive_connections=16, max_connections=16)
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="aios-frame-upload")
        pending = {}

        def upload(remote_client, monitor_id, sequence, content):
            t0 = time.monotonic()
            response = remote_client.put(
                f"{self.bridge.relay_url}/api/agent/frame/{urllib.parse.quote(monitor_id, safe='')}",
                content=content,
                headers={
                    **self.bridge.headers,
                    "Content-Type": "image/jpeg",
                    "X-aiOS-Frame-Seq": str(sequence),
                },
            )
            response.raise_for_status()
            return (time.monotonic() - t0) * 1000

        with httpx.Client(timeout=httpx.Timeout(8.0, connect=4.0), limits=local_limits) as local_client, \
                httpx.Client(timeout=httpx.Timeout(12.0, connect=5.0), limits=remote_limits) as remote_client:
            while not self._stop.is_set():
                started = time.monotonic()
                for future in [item for item in pending if item.done()]:
                    monitor_id, capture_ms = pending.pop(future)
                    try:
                        self._record(monitor_id=monitor_id, capture_ms=capture_ms, upload_ms=future.result())
                    except Exception as exc:
                        self._record(monitor_id=monitor_id, capture_ms=capture_ms, error=exc)
                active = started < self.active_until
                fps = self.target_fps if active else 1.0
                monitor_id = self._chosen_monitor()
                try:
                    t0 = time.monotonic()
                    local = local_client.get(
                        f"{LOCAL_BASE}/api/phone/screen",
                        params={"monitor": monitor_id, "q": 48, "max": 1024, "stream": 1},
                    )
                    local.raise_for_status()
                    capture_ms = (time.monotonic() - t0) * 1000
                    if len(pending) < 16:
                        self._sequence += 1
                        future = executor.submit(upload, remote_client, monitor_id, self._sequence, local.content)
                        pending[future] = (monitor_id, capture_ms)
                except Exception as exc:
                    self._record(monitor_id=monitor_id, error=exc)
                interval = 1.0 / fps
                wait_for = max(0.0, interval - (time.monotonic() - started))
                self._wake.wait(wait_for)
                self._wake.clear()
        executor.shutdown(wait=False, cancel_futures=True)


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(config: dict) -> None:
    temp = CONFIG_PATH.with_suffix(".json.tmp")
    temp.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(CONFIG_PATH)


def request_json(url: str, *, method="GET", payload=None, headers=None, timeout=12):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 aiOS-Remote-Bridge/1.0",
        **(headers or {}),
    }
    if data is not None:
        request_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error")
        except Exception:
            detail = None
        raise RuntimeError(detail or f"HTTP {exc.code}") from exc


def request_bytes(url: str, *, method="GET", data=None, headers=None, timeout=12):
    request_headers = {
        "User-Agent": "Mozilla/5.0 aiOS-Remote-Bridge/1.0",
        **(headers or {}),
    }
    req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def default_machine_name() -> str:
    return os.environ.get("COMPUTERNAME") or socket.gethostname() or "Windows PC"


def pair(relay_url: str, code: str, name: str) -> dict:
    relay_url = relay_url.strip().rstrip("/")
    if not relay_url.startswith(("https://", "http://")):
        raise RuntimeError("Enter the full aiOS Remote URL, starting with https://")
    result = request_json(
        f"{relay_url}/api/machines/pair",
        method="POST",
        payload={"code": code, "name": name, "platform": f"Windows · {platform.release()}"},
    )
    config = load_config()
    config["phone_relay"] = {
        "url": relay_url,
        "machine_id": result["machine_id"],
        "machine_token": result["machine_token"],
        "machine_name": result.get("name") or name,
        "enabled": True,
    }
    save_config(config)
    return config["phone_relay"]


class Bridge:
    def __init__(self, relay: dict):
        self.relay_url = str(relay.get("url") or "").rstrip("/")
        self.machine_id = str(relay.get("machine_id") or "")
        self.token = str(relay.get("machine_token") or "")
        self.headers = {"X-aiOS-Machine-Token": self.token}
        self.log_cursor = self.load_cursor()
        self.last_status_at = 0.0
        self.last_monitors_at = 0.0
        self.monitors = []
        self.backoff = 1.0
        self.frame_streamer = FrameStreamer(self)

    def load_cursor(self) -> int:
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            saved = int((state.get("machines") or {}).get(self.machine_id, {}).get("log_cursor", 0))
            if saved:
                return saved
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        # A fresh pairing should not import a previous machine's old run.
        try:
            return int(LOCAL_EVENTS_PATH.stat().st_size)
        except OSError:
            return 0

    def save_cursor(self) -> None:
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
        except (OSError, json.JSONDecodeError):
            state = {}
        machines = state.setdefault("machines", {})
        machines[self.machine_id] = {"log_cursor": int(self.log_cursor), "updated_at": int(time.time() * 1000)}
        temp = STATE_PATH.with_suffix(".json.tmp")
        try:
            temp.write_text(json.dumps(state, indent=2), encoding="utf-8")
            temp.replace(STATE_PATH)
        except OSError:
            pass

    def reload_pairing(self) -> bool:
        """Adopt a newly paired machine key without restarting the daemon."""
        relay = load_config().get("phone_relay") or {}
        relay_url = str(relay.get("url") or "").rstrip("/")
        machine_id = str(relay.get("machine_id") or "")
        token = str(relay.get("machine_token") or "")
        changed = (relay_url, machine_id, token) != (self.relay_url, self.machine_id, self.token)
        if not changed:
            return False
        if not relay_url or not machine_id or not token:
            return False
        self.relay_url = relay_url
        self.machine_id = machine_id
        self.token = token
        self.headers = {"X-aiOS-Machine-Token": token}
        self.log_cursor = self.load_cursor()
        self.last_status_at = 0.0
        self.last_monitors_at = 0.0
        self.monitors = []
        print(f"aiOS Remote bridge switched to pairing {machine_id}", flush=True)
        return True

    def ready(self) -> bool:
        return bool(self.relay_url and self.machine_id and self.token)

    def remote_json(self, path: str, **kwargs):
        kwargs.setdefault("headers", self.headers)
        return request_json(f"{self.relay_url}{path}", **kwargs)

    def local_json(self, path: str, **kwargs):
        return request_json(f"{LOCAL_BASE}{path}", **kwargs)

    def execute(self, command: dict) -> dict:
        kind = str(command.get("type") or "")
        payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
        # New control messages can be tunneled through the legacy relay's
        # existing `config` command until every hosted backend is upgraded.
        tunneled = str(payload.get("_aios_command") or "") if kind == "config" else ""
        if tunneled in {"stream", "update", "codex_switch", "ai_settings"}:
            kind = tunneled
            payload = {key: value for key, value in payload.items() if key != "_aios_command"}
        if kind in {"prompt", "followup"}:
            model = MODEL_MAP.get(str(payload.get("model") or "").lower(), payload.get("model") or "gpt-5.6-luna")
            options = {
                "model": model,
                "planner_model": MODEL_MAP.get(
                    str(payload.get("planner_model") or "").lower(),
                    payload.get("planner_model") or "off",
                ),
                "reasoning": str(payload.get("reasoning_effort") or "low"),
                "steps": str(payload.get("max_steps") or 30),
            }
            return self.local_json(
                "/api/phone/send",
                method="POST",
                payload={
                    "text": str(payload.get("prompt") or ""),
                    "target": "operator",
                    "intent": "followup" if kind == "followup" else "new",
                    "options": options,
                },
            )
        if kind == "stop":
            return self.local_json("/api/phone/operator/stop", method="POST", payload={})
        if kind == "config":
            incoming = payload.get("operator") if isinstance(payload.get("operator"), dict) else payload
            return self.local_json("/api/phone/operator/config", method="POST", payload={"operator": incoming})
        if kind == "clarify":
            draft = str(payload.get("draft") or "").strip()[:8000]
            request_id = str(payload.get("request_id") or "")[:100]
            if len(draft) < 8:
                return {"ok": True, "request_id": request_id, "questions": []}
            helper_config = load_config()
            api_key = str(helper_config.get("openai_api_key") or os.environ.get("OPENAI_API_KEY") or "").strip()
            operator = helper_config.get("ai_operator") if isinstance(helper_config.get("ai_operator"), dict) else {}
            provider_mode = str(operator.get("provider_mode") or ("codex" if operator.get("codex_auth") else "api"))
            result = clarify_prompt_for_provider(
                draft,
                normalize_questions(payload.get("previous") or []),
                provider_mode=provider_mode,
                api_key=api_key,
            )
            return {"ok": True, "request_id": request_id, **result}
        if kind == "stream":
            monitor_id = str(payload.get("monitor_id") or "1")[:32]
            self.frame_streamer.select_monitor(monitor_id, payload.get("lease_seconds") or 15)
            return {"ok": True, "monitor_id": monitor_id, "target_fps": self.frame_streamer.target_fps}
        if kind == "codex_switch":
            account_id = str(payload.get("account_id") or "")
            return aios_codex_accounts.switch_account(account_id, CONFIG_PATH)
        if kind == "ai_settings":
            provider_mode = str(payload.get("provider_mode") or "").strip().lower()
            local_payload = {"provider_mode": provider_mode}
            if "openai_api_key" in payload:
                local_payload["openai_api_key"] = str(payload.get("openai_api_key") or "").strip()
            if payload.get("clear_openai_api_key"):
                local_payload["clear_openai_api_key"] = True
            return self.local_json("/api/phone/ai/config", method="POST", payload=local_payload)
        if kind == "update":
            UPDATE_REQUEST_PATH.touch()
            return {"ok": True, "queued": True, "message": "Update requested; it will install when OPERATOR is idle."}
        raise RuntimeError(f"Unsupported command: {kind}")

    def collect_status(self) -> dict:
        raw = self.local_json("/api/phone/status", timeout=4)
        operator_state = raw.get("operator_state") if isinstance(raw.get("operator_state"), dict) else {}
        operator_config = raw.get("operator") if isinstance(raw.get("operator"), dict) else {}
        running = bool(operator_state.get("running"))
        asking = bool(operator_state.get("asking"))
        return {
            "state": "running" if running else "idle",
            "operator": {
                "state": "waiting" if asking else ("running" if running else "idle"),
                "task": operator_state.get("last_question") if asking else operator_state.get("task", ""),
                "asking": asking,
                "model": operator_config.get("model") or "gpt-5.6-luna",
                "provider_mode": operator_config.get("provider_mode") or (
                    "codex" if operator_config.get("codex_auth") else "api"
                ),
            },
            "ai": raw.get("ai") or {},
            "helper": bool(raw.get("helper")),
            "monitors": self.monitors,
            "stream": self.frame_streamer.snapshot(),
            "codex_usage": raw.get("codex_usage") or {},
            "codex_accounts": aios_codex_accounts.list_accounts(CONFIG_PATH),
            "update": raw.get("update") or {},
        }

    def refresh_monitors(self) -> None:
        if time.monotonic() - self.last_monitors_at < 20:
            return
        data = self.local_json("/api/phone/monitors", timeout=4)
        monitors = data.get("monitors") if isinstance(data.get("monitors"), list) else []
        # Skip the synthetic all-monitors surface when physical displays exist.
        selected = monitors[1:] if len(monitors) > 1 else monitors
        # left/top let the phone place click markers on the right display.
        self.monitors = [
            {
                "id": str(item.get("index", index + 1)),
                "name": item.get("name") or f"Display {index + 1}",
                "width": int(item.get("width") or 0),
                "height": int(item.get("height") or 0),
                "left": int(item.get("left") or 0),
                "top": int(item.get("top") or 0),
            }
            for index, item in enumerate(selected)
        ] or [{"id": "1", "name": "Main display", "width": 0, "height": 0, "left": 0, "top": 0}]
        self.last_monitors_at = time.monotonic()

    def collect_events(self) -> list[dict]:
        data = self.local_json(f"/api/phone/operator/log?since={self.log_cursor}", timeout=4)
        if data.get("reset"):
            self.log_cursor = 0
        self.log_cursor = int(data.get("size") or self.log_cursor)
        self.save_cursor()
        output = []
        for item in data.get("events") or []:
            if not isinstance(item, dict):
                continue
            created = float(item.get("ts") or time.time())
            output.append({
                "type": str(item.get("type") or "log"),
                "payload": item,
                "created_at": int(created * 1000) if created < 10_000_000_000 else int(created),
            })
        return output

    def post_events(self, *, events=None, status=None, command_id=None, result=None) -> None:
        payload = {"events": events or []}
        if status is not None:
            payload["status"] = status
        if command_id is not None:
            payload["completed_command_id"] = command_id
            payload["result"] = result or {}
        self.remote_json("/api/agent/events", method="POST", payload=payload, timeout=10)

    def tick(self) -> None:
        self.reload_pairing()
        self.refresh_monitors()
        response = self.remote_json("/api/agent/commands", timeout=15)
        for command in response.get("commands") or []:
            raw_payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
            command_type = str(raw_payload.get("_aios_command") or command.get("type") or "")
            is_clarify = command_type == "clarify"
            is_silent = command_type == "stream"
            try:
                result = self.execute(command)
                self.post_events(
                    events=[] if is_silent else [{
                        "type": "clarification" if is_clarify else "command",
                        "payload": result if is_clarify else {"title": "Command received", "message": command.get("type")},
                        "created_at": int(time.time() * 1000),
                    }],
                    command_id=command.get("id"), result=result,
                )
            except Exception as exc:
                self.post_events(
                    events=[{
                        "type": "clarification" if is_clarify else "error",
                        "payload": ({
                            "ok": False,
                            "request_id": str((command.get("payload") or {}).get("request_id") or ""),
                            "error": str(exc),
                        } if is_clarify else {"title": "Command failed", "message": str(exc)}),
                        "created_at": int(time.time() * 1000),
                    }],
                    command_id=command.get("id"), result={"ok": False, "error": str(exc)},
                )
        events = self.collect_events()
        status = None
        if time.monotonic() - self.last_status_at >= 2.5:
            status = self.collect_status()
            self.last_status_at = time.monotonic()
        if events or status is not None:
            self.post_events(events=events, status=status)
    def run(self) -> None:
        if not self.ready():
            raise RuntimeError("This PC is not paired. Open aiOS Settings → Mobile remote first.")
        print(f"aiOS Remote bridge connected as {self.machine_id}", flush=True)
        self.frame_streamer.start()
        try:
            while True:
                try:
                    HEARTBEAT_PATH.touch()
                except OSError:
                    pass
                try:
                    self.tick()
                    self.backoff = 1.0
                    time.sleep(0.35)
                except KeyboardInterrupt:
                    return
                except Exception as exc:
                    print(f"[{time.strftime('%H:%M:%S')}] relay: {exc}", file=sys.stderr, flush=True)
                    time.sleep(self.backoff)
                    self.backoff = min(20.0, self.backoff * 1.7)
        finally:
            self.frame_streamer.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="Pair or run the aiOS Remote bridge")
    sub = parser.add_subparsers(dest="command")
    pair_parser = sub.add_parser("pair", help="Pair this computer")
    pair_parser.add_argument("code")
    pair_parser.add_argument("--url", default=DEFAULT_RELAY, required=not bool(DEFAULT_RELAY))
    pair_parser.add_argument("--name", default=default_machine_name())
    sub.add_parser("run", help="Run the paired bridge")
    args = parser.parse_args()
    if args.command == "pair":
        paired = pair(args.url, args.code, args.name)
        print(f"Paired {paired['machine_name']} successfully.")
        return 0
    if args.command == "run":
        if not claim_single_instance():
            return 0
        relay = load_config().get("phone_relay") or {}
        Bridge(relay).run()
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
