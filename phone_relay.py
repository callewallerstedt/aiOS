"""Secure bridge between the local aiOS helper and the hosted aiOS Remote relay."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "helper_config.json"
STATE_PATH = ROOT / "phone-relay-state.json"
HEARTBEAT_PATH = ROOT / ".aios-phone-relay-heartbeat"
LOCAL_EVENTS_PATH = ROOT / "phone_operator_events" / "events.jsonl"
LOCAL_BASE = "http://127.0.0.1:5000"
DEFAULT_RELAY = os.environ.get("AIOS_RELAY_URL", "").rstrip("/")
MODEL_MAP = {
    "luna": "gpt-5.6-luna",
    "terra": "gpt-5.6-terra",
    "sol": "gpt-5.6-sol",
}


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
        self.last_frame_at = 0.0
        self.last_monitors_at = 0.0
        self.monitors = []
        self.backoff = 1.0

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
        self.last_frame_at = 0.0
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
            },
            "helper": bool(raw.get("helper")),
            "monitors": self.monitors,
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

    def upload_frames(self) -> None:
        if time.monotonic() - self.last_frame_at < 2.2:
            return
        for monitor in self.monitors:
            monitor_id = urllib.parse.quote(str(monitor["id"]), safe="")
            image = request_bytes(f"{LOCAL_BASE}/api/phone/screen?monitor={monitor_id}&q=68&max=1600", timeout=8)
            request_bytes(
                f"{self.relay_url}/api/agent/frame/{monitor_id}",
                method="PUT",
                data=image,
                headers={**self.headers, "Content-Type": "image/jpeg"},
                timeout=15,
            )
        self.last_frame_at = time.monotonic()

    def tick(self) -> None:
        self.reload_pairing()
        self.refresh_monitors()
        response = self.remote_json("/api/agent/commands", timeout=15)
        for command in response.get("commands") or []:
            try:
                result = self.execute(command)
                self.post_events(
                    events=[{"type": "command", "payload": {"title": "Command received", "message": command.get("type")}, "created_at": int(time.time() * 1000)}],
                    command_id=command.get("id"), result=result,
                )
            except Exception as exc:
                self.post_events(
                    events=[{"type": "error", "payload": {"title": "Command failed", "message": str(exc)}, "created_at": int(time.time() * 1000)}],
                    command_id=command.get("id"), result={"ok": False, "error": str(exc)},
                )
        events = self.collect_events()
        status = None
        if time.monotonic() - self.last_status_at >= 2.5:
            status = self.collect_status()
            self.last_status_at = time.monotonic()
        if events or status is not None:
            self.post_events(events=events, status=status)
        self.upload_frames()

    def run(self) -> None:
        if not self.ready():
            raise RuntimeError("This PC is not paired. Open aiOS Settings → Mobile remote first.")
        print(f"aiOS Remote bridge connected as {self.machine_id}", flush=True)
        while True:
            try:
                HEARTBEAT_PATH.touch()
            except OSError:
                pass
            try:
                self.tick()
                self.backoff = 1.0
                time.sleep(0.7)
            except KeyboardInterrupt:
                return
            except Exception as exc:
                print(f"[{time.strftime('%H:%M:%S')}] relay: {exc}", file=sys.stderr, flush=True)
                time.sleep(self.backoff)
                self.backoff = min(20.0, self.backoff * 1.7)


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
        relay = load_config().get("phone_relay") or {}
        Bridge(relay).run()
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
