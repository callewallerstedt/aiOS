"""Local HTTP + SSE server backing the aiOS WebView2 shell.

Why a socket instead of pywebview's js_api bridge: the bridge marshals every
call through the UI thread and cannot push. CODE needs *push* -- the whole point
of the rebuild is that streamed tokens arrive without the UI asking for them.
Server-sent events give us that with no polling jitter, and plain fetch() covers
ordinary request/response.

Everything is bound to 127.0.0.1 and gated on a per-launch token so nothing else
on the machine can drive the backend.
"""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import BASE_DIR, WEB_DIR

ASSET_ROOT = BASE_DIR.parent / "assets"

# How often the SSE loops look for new work. The Tk build polled the transcript
# every 250ms and the session list every 900ms; both were visible as stutter.
EVENT_TICK = 0.05
JOBS_TICK = 0.6
HEARTBEAT = 15.0
CODE_STANDING_PROMPT_LIMIT = 4000


class _Bridge:
    """Lazy handles to the existing aiOS backend modules."""

    def __init__(self) -> None:
        self._code_jobs = None
        self._helper = None
        # The first page opens several endpoints concurrently.  A single lock
        # made an unrelated config read wait behind the much larger CODE import
        # (and vice versa), which amplified a slow import into a blank startup.
        self._code_lock = threading.Lock()
        self._helper_lock = threading.Lock()

    @property
    def code_jobs(self):
        if self._code_jobs is None:
            with self._code_lock:
                if self._code_jobs is None:
                    import code_jobs

                    self._code_jobs = code_jobs
        return self._code_jobs

    @property
    def helper(self):
        """helper_overlay owns load_config/save_config and the defaults.

        Importing it pulls in tkinter but creates no window -- the Tk root only
        appears inside main(). Reusing it keeps config semantics (defaults,
        migrations, atomic writes, backup recovery) byte-identical to the old
        build rather than reimplementing them and drifting.
        """
        if self._helper is None:
            with self._helper_lock:
                if self._helper is None:
                    import helper_overlay

                    self._helper = helper_overlay
        return self._helper


BRIDGE = _Bridge()


# --------------------------------------------------------------------- routes


def _int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _append_code_standing_prompt(text: Any, standing_prompt: Any) -> str:
    """Append the global CODE instruction at the shared UI API boundary."""
    message = str(text or "")
    instruction = str(standing_prompt or "").strip()[:CODE_STANDING_PROMPT_LIMIT]
    if not message.strip() or not instruction:
        return message
    return (
        f"{message.rstrip()}\n\n---\n"
        "Standing instruction (automatically appended by aiOS):\n"
        f"{instruction}"
    )


def dispatch(route: str, method: str, params: dict, data: dict) -> Any:
    """Mirror of helper_overlay._code_api_local so CODE wiring stays identical."""
    method = method.upper()
    route = route.rstrip("/")

    if route == "/api/native/apps" and method == "GET":
        from .native_windows import list_apps

        return list_apps()

    # Settings, the updater and Quick Tools live in their own module -- they own
    # the clamping rules that used to sit in the Tk widget callbacks.
    if route.startswith(("/api/settings/", "/api/update/", "/api/tools/")):
        from . import settings_api

        handled = settings_api.dispatch(route, method, params, data)
        if handled is not None:
            return handled

    # BENCH runs the same harness against a fixed set of tasks, in its own jobs
    # directory, so measuring it never pollutes the session list above.
    if route.startswith("/api/bench/"):
        from . import bench_api

        handled = bench_api.dispatch(route, method, params, data)
        if handled is not None:
            return handled

    # HARNESS is a read of code_jobs itself, so the page describing the agent
    # cannot drift from the agent.
    if route.startswith("/api/harness/"):
        from . import harness_api

        handled = harness_api.dispatch(route, method, params, data)
        if handled is not None:
            return handled

    # The sidebar chat talks to voice_dictation's agent, the same one the voice
    # overlay and the phone use, so all three share one conversation.
    if route.startswith("/api/agent/"):
        from . import agent_api

        handled = agent_api.dispatch(route, method, params, data)
        if handled is not None:
            return handled

    if route == "/api/config":
        if method == "GET":
            return {"ok": True, "config": BRIDGE.helper.load_config()}
        if method == "POST":
            config = BRIDGE.helper.load_config()
            patch = data.get("patch") if isinstance(data.get("patch"), dict) else data
            config.update(patch)
            BRIDGE.helper.save_config(config)
            return {"ok": True, "config": config}

    if route == "/api/openrouter/balance" and method == "GET":
        import openrouter_client

        force = str((params.get("refresh") or [""])[0]).lower() in {"1", "true", "yes"}
        return openrouter_client.credit_balance(refresh=force)

    # Handoff: pull the latest Claude Code / Codex session into a compact brief
    # (user turns + assistant replies + files edited) so the CODE agent can pick
    # up where another tool left off. Lives in its own module so the parsing
    # stays out of the route table.
    if route == "/api/handoff" and method == "GET":
        from . import handoff

        return handoff.list_sessions()

    if route == "/api/handoff" and method == "POST":
        from . import handoff

        return handoff.read_session(
            str(data.get("tool") or ""),
            str(data.get("path") or ""),
            full=bool(data.get("full")),
        )

    # Import the 300k CODE harness only for routes that actually need it.  The
    # shell's config, settings, chat, and static UI can now become usable even
    # if provider discovery or a harness import is slow during startup.
    jobs = BRIDGE.code_jobs

    if route == "/api/code/capabilities" and method == "GET":
        force = str((params.get("refresh") or [""])[0]).lower() in {"1", "true", "yes"}
        return jobs.capabilities(force=force)

    if route.startswith("/api/code/providers/") and route.endswith("/setup") and method == "POST":
        return jobs.setup_provider(route.split("/")[-2])

    if route == "/api/code/roles":
        import code_roles

        if method == "GET":
            return {"ok": True, "roles": code_roles.load_roles(), "catalogue": code_roles.catalogue()}
        if method == "POST":
            config = BRIDGE.helper.load_config()
            merged = code_roles.save_roles(data.get("roles") if isinstance(data.get("roles"), dict) else data, config)
            config["code_roles"] = merged
            BRIDGE.helper.save_config(config)
            return {"ok": True, "roles": merged}

    if route == "/api/code/model-configs":
        import code_roles

        if method == "GET":
            config = BRIDGE.helper.load_config()
            configs = code_roles.load_model_configs(config)
            if code_roles.merge_recovered_model_configs(config):
                BRIDGE.helper.save_config(config)
                configs = code_roles.load_model_configs(config)
            return {"ok": True, "configs": configs}
        if method == "POST":
            config = BRIDGE.helper.load_config()
            try:
                configs = code_roles.save_model_config(data, config)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            config["model_configs"] = configs
            BRIDGE.helper.save_config(config)
            return {"ok": True, "configs": configs}
        if method == "DELETE":
            config = BRIDGE.helper.load_config()
            configs = code_roles.delete_model_config(str(data.get("id") or ""), config)
            config["model_configs"] = configs
            BRIDGE.helper.save_config(config)
            return {"ok": True, "configs": configs}

    if route == "/api/code/models" and method == "GET":
        import openrouter_client

        force = str((params.get("refresh") or [""])[0]).lower() in {"1", "true", "yes"}
        return {
            "ok": True,
            "models": openrouter_client.model_specs(refresh=force),
            "speed": jobs.measured_model_speed(),
        }

    if route == "/api/code/usage" and method == "GET":
        return {"ok": True, "usage": jobs.usage_window(_int((params.get("days") or ["28"])[0], 28))}

    if route == "/api/code/projects":
        if method == "GET":
            return {"ok": True, "projects": jobs.list_projects()}
        if method == "POST":
            return jobs.add_project(str(data.get("path") or ""), str(data.get("name") or ""))

    if route.startswith("/api/code/projects/") and method == "DELETE":
        return jobs.remove_project(route.rsplit("/", 1)[-1])

    if route == "/api/code/jobs/retitle" and method == "POST":
        return jobs.refresh_session_titles(
            limit=_int(data.get("limit"), 250),
            force=bool(data.get("force")),
            project=data.get("project"),
            workers=_int(data.get("workers"), 0) or None,
            wait=bool(data.get("wait")),
        )

    if route == "/api/code/jobs":
        if method == "GET":
            return {"ok": True, "jobs": jobs.list_jobs(_int((params.get("limit") or ["100"])[0], 100))}
        if method == "POST":
            return jobs.create_job(
                str(data.get("provider") or data.get("cli") or ""),
                str(data.get("cwd") or data.get("project") or "").strip(),
                _append_code_standing_prompt(
                    data.get("brief") or data.get("text") or "",
                    data.get("standing_prompt"),
                ),
                str(data.get("model") or ""),
                str(data.get("reasoning") or data.get("effort") or ""),
                fast=bool(data.get("fast")),
                title=str(data.get("title") or ""),
                attachments=data.get("attachments") or [],
                review_fix=data.get("review_fix") if "review_fix" in data else None,
                role_config=data.get("roles") if isinstance(data.get("roles"), dict) else None,
                config_id=str(data.get("config_id") or ""),
                config_name=str(data.get("config_name") or ""),
                strategy=str(data.get("strategy") or "auto"),
            )

    parts = route.split("/")
    if len(parts) >= 5 and parts[1] == "api" and parts[2] == "code" and parts[3] == "jobs":
        job_id = parts[4]
        action = parts[5] if len(parts) > 5 else ""
        if method == "GET" and not action:
            job = jobs.get_job(job_id)
            return {"ok": True, "job": job} if job else {"ok": False, "error": "unknown CODE job"}
        if method == "DELETE" and not action:
            return jobs.delete_job(job_id, confirmed=str(data.get("confirm") or "") == str(job_id))
        if method == "POST" and action in {"messages", "continue"}:
            return jobs.send_message(
                job_id,
                _append_code_standing_prompt(
                    data.get("text") or data.get("message") or "",
                    data.get("standing_prompt"),
                ),
                urgent=bool(data.get("urgent")),
                attachments=data.get("attachments") or [],
                model=str(data.get("model") or ""),
                reasoning=str(data.get("reasoning") or data.get("effort") or ""),
                fast=data.get("fast") if "fast" in data else None,
                strategy=str(data.get("strategy") or "auto"),
                question_answers=data.get("question_answers"),
            )
        if method == "POST" and action == "stop":
            return jobs.stop_job(job_id)
        if method == "POST" and action == "undo":
            return jobs.undo_job(job_id, confirmed=bool(data.get("confirm")))
        if method == "POST" and action == "compact":
            return jobs.compact_job_context(job_id, force=bool(data.get("force")))
        if method == "POST" and action == "handoff":
            return jobs.handoff_job(
                job_id,
                str(data.get("provider") or data.get("target_provider") or ""),
                str(data.get("model") or data.get("target_model") or ""),
                str(data.get("reasoning") or data.get("effort") or data.get("target_reasoning") or ""),
                fast=bool(data.get("fast") if "fast" in data else data.get("target_fast")),
                instruction=str(data.get("instruction") or data.get("text") or ""),
            )
        if method == "POST" and action == "configuration":
            return jobs.apply_job_configuration(
                job_id,
                str(data.get("provider") or ""),
                str(data.get("model") or ""),
                str(data.get("reasoning") or data.get("effort") or ""),
                bool(data.get("fast")),
                data.get("roles") if isinstance(data.get("roles"), dict) else {},
                config_id=str(data.get("config_id") or ""),
                config_name=str(data.get("config_name") or ""),
            )
        if method == "POST" and action == "review":
            return jobs.create_session_review(
                job_id,
                str(data.get("provider") or ""),
                str(data.get("model") or ""),
                str(data.get("reasoning") or data.get("effort") or ""),
                bool(data.get("fast")),
                data.get("roles") if isinstance(data.get("roles"), dict) else {},
                config_id=str(data.get("config_id") or ""),
                config_name=str(data.get("config_name") or ""),
            )
        if method == "GET" and action in {"log", "events"}:
            return jobs.read_events(job_id, _int((params.get("since") or ["0"])[0], 0))

    return {"ok": False, "error": f"no route for {method} {route}"}


# ---------------------------------------------------------------- SSE streams


def _sse(handler: BaseHTTPRequestHandler, event: str, payload: Any) -> None:
    body = json.dumps(payload, default=str)
    handler.wfile.write(f"event: {event}\ndata: {body}\n\n".encode("utf-8"))
    handler.wfile.flush()


def stream_events(handler: BaseHTTPRequestHandler, params: dict) -> None:
    """Push one CODE session's transcript as it is written to disk."""
    job_id = str((params.get("job") or [""])[0])
    since = _int((params.get("since") or ["0"])[0], 0)
    jobs = BRIDGE.code_jobs
    if not job_id:
        _sse(handler, "error", {"error": "missing job"})
        return

    last_beat = time.monotonic()
    last_status = None
    while not handler.server.stopping:
        result = jobs.read_events(job_id, since)
        if not result.get("ok"):
            _sse(handler, "error", {"error": result.get("error") or "unknown CODE job"})
            return
        events = result.get("events") or []
        size = _int(result.get("size"), since)
        job = result.get("job") or {}
        if result.get("reset"):
            _sse(handler, "reset", {"job": job})
            since = 0
        if events:
            # One frame per batch, not per event. The client reveals assistant
            # text on its own rAF clock so bursty provider output still reads as
            # an even stream instead of arriving in visible chunks.
            _sse(handler, "events", {"events": events, "size": size, "job": job})
            since = size
        status = job.get("status")
        if status != last_status:
            last_status = status
            _sse(handler, "job", {"job": job})
        now = time.monotonic()
        if now - last_beat > HEARTBEAT:
            last_beat = now
            _sse(handler, "ping", {"t": now})
        # Idle sessions do not need a 50ms file stat.
        time.sleep(EVENT_TICK if status in {"queued", "running", "waiting_user"} else 0.4)


def stream_agent(handler: BaseHTTPRequestHandler, params: dict) -> None:
    """Push the agent conversation as voice_dictation writes it.

    The same byte-offset protocol as the CODE transcript, so a typed turn here
    and a spoken turn at the overlay land in this panel identically.
    """
    from . import agent_api

    since = _int((params.get("since") or ["0"])[0], 0)
    last_beat = time.monotonic()
    last_running = None
    while not handler.server.stopping:
        # Use the same Director/local decision for reads as agent_api.dispatch
        # uses for sends. Previously a typed turn went to Director while this
        # SSE loop tailed the local voice log, so the sidebar churned forever.
        result = agent_api.dispatch(
            "/api/agent/log", "GET", {"since": [str(since)]}, {},
        ) or agent_api.read_events(since)
        if result.get("reset"):
            _sse(handler, "reset", {})
            since = 0
        events = result.get("events") or []
        size = _int(result.get("size"), since)
        if events:
            _sse(handler, "events", {"events": events, "size": size})
        since = size
        is_running = bool(result.get("running"))
        if is_running != last_running:
            last_running = is_running
            _sse(handler, "state", {"running": is_running})
        now = time.monotonic()
        if now - last_beat > HEARTBEAT:
            last_beat = now
            _sse(handler, "ping", {"t": now})
        # Tight while the agent is answering so streamed text reads as a stream;
        # relaxed otherwise, because an idle agent writes nothing.
        time.sleep(EVENT_TICK if is_running else 0.4)


def stream_reload(handler: BaseHTTPRequestHandler, params: dict) -> None:
    """Dev live-reload stream — intentionally idle.

    Pushing reloads when web/ files change used to update the running window
    mid-session. That crashes aiOS when CODE edits the GUI (or anything else
    touches those assets), so this endpoint stays connected but never emits a
    reload. Callers can still open /sse/dev/reload; they just get heartbeats.
    """
    last_beat = time.monotonic()
    while not handler.server.stopping:
        now = time.monotonic()
        if now - last_beat > HEARTBEAT:
            last_beat = now
            _sse(handler, "ping", {"t": now})
        time.sleep(1.0)


def stream_jobs(handler: BaseHTTPRequestHandler, params: dict) -> None:
    """Push the session list + counters whenever anything actually changes."""
    jobs = BRIDGE.code_jobs
    limit = _int((params.get("limit") or ["250"])[0], 250)
    signature = None
    last_beat = time.monotonic()
    while not handler.server.stopping:
        try:
            listing = jobs.list_jobs(limit)
        except Exception as exc:  # a half-written job file must not kill the stream
            _sse(handler, "error", {"error": str(exc)})
            time.sleep(2.0)
            continue
        fingerprint = json.dumps(
            [
                (j.get("id"), j.get("status"), j.get("updated_at"), j.get("title"), j.get("turns"))
                for j in listing
            ],
            default=str,
        )
        if fingerprint != signature:
            signature = fingerprint
            _sse(handler, "jobs", {"jobs": listing})
        now = time.monotonic()
        if now - last_beat > HEARTBEAT:
            last_beat = now
            _sse(handler, "ping", {"t": now})
        time.sleep(JOBS_TICK)


# -------------------------------------------------------------------- handler


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "aiOS"

    def log_message(self, *_args) -> None:  # keep the console clean
        pass

    # -- helpers

    def _authorised(self, params: dict) -> bool:
        token = (params.get("token") or [""])[0]
        return secrets.compare_digest(str(token), self.server.token)

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _binary(self, body: bytes, content_type: str, *, headers: dict[str, str] | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path: str) -> None:
        rel = path.lstrip("/") or "index.html"
        # /assets/* comes from the repo's existing asset tree (brand font,
        # provider logos, icons) rather than being duplicated under web/.
        root = ASSET_ROOT if rel.startswith("assets/") else WEB_DIR
        target = (root / (rel[len("assets/"):] if root is ASSET_ROOT else rel)).resolve()
        if not str(target).startswith(str(root.resolve())) or not target.is_file():
            self.send_error(404)
            return
        body = target.read_bytes()
        kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", f"{kind}; charset=utf-8" if kind.startswith("text/") or kind.endswith("javascript") else kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = _int(self.headers.get("Content-Length"), 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _route(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        route = parsed.path

        # The USB mirror has one stable, loopback-only bootstrap URL.  It
        # redirects into the exact same token-gated WebView application as the
        # desktop shell, so the Android wrapper never needs a copied UI or a
        # credential baked into its APK.  Normal desktop servers do not expose
        # this convenience route.
        if getattr(self.server, "mirror_bootstrap", False):
            if route == "/mirror-health" and method == "GET":
                self._json({"ok": True, "service": "aios-mirror"})
                return
            if route == "/mirror" and method == "GET":
                query = urllib.parse.urlencode({"token": self.server.token, "phone": "1"})
                self.send_response(302)
                self.send_header("Location", f"/index.html?{query}")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if route.startswith("/native/frame/") and method == "GET":
                if not self._authorised(params):
                    self.send_error(403)
                    return
                from .native_windows import capture_jpeg

                image, details = capture_jpeg(route.rsplit("/", 1)[-1].casefold())
                if image is None:
                    self._json(details, 404)
                    return
                self._binary(image, "image/jpeg", headers={
                    "X-aiOS-Window-Title": urllib.parse.quote(str(details.get("title") or "")),
                })
                return

        if route.startswith("/sse/"):
            if not self._authorised(params):
                self.send_error(403)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                if route == "/sse/code/events":
                    stream_events(self, params)
                elif route == "/sse/code/jobs":
                    stream_jobs(self, params)
                elif route == "/sse/agent/events":
                    stream_agent(self, params)
                elif route.startswith("/sse/bench/"):
                    from . import bench_api

                    if route == "/sse/bench/state":
                        bench_api.stream_state(self, params, _sse)
                    elif route == "/sse/bench/events":
                        bench_api.stream_events(self, params, _sse)
                elif route == "/sse/dev/reload":
                    stream_reload(self, params)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # the view navigated away; EventSource will reconnect
            return

        if route.startswith("/api/"):
            if not self._authorised(params):
                self._json({"ok": False, "error": "forbidden"}, 403)
                return
            try:
                result = dispatch(route, method, params, self._body())
            except Exception as exc:
                traceback.print_exc()
                result = {"ok": False, "error": f"backend error: {exc}"}
            self._json(result if result is not None else {"ok": False, "error": "not found"})
            return

        if method == "GET":
            self._static(route)
            return
        self.send_error(405)

    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    def do_DELETE(self) -> None:
        self._route("DELETE")


class UIServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address) -> None:
        """A closing webview drops its SSE sockets mid-flight.

        That is normal shutdown, not a fault, and socketserver's default handler
        prints a full traceback for each one. Only surface real errors.
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)

    def __init__(
        self,
        address: tuple[str, int] = ("127.0.0.1", 0),
        *,
        mirror_bootstrap: bool = False,
    ) -> None:
        super().__init__(address, _Handler)
        self.token = secrets.token_urlsafe(24)
        self.mirror_bootstrap = mirror_bootstrap
        self.stopping = False

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}/index.html?token={self.token}"

    @property
    def quick_tools_url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}/quick_tools.html?token={self.token}"

    def start(self) -> "UIServer":
        threading.Thread(target=self.serve_forever, daemon=True, name="aios-ui-http").start()
        return self

    def stop(self) -> None:
        self.stopping = True
        self.shutdown()


def start_server() -> UIServer:
    mimetypes.add_type("application/javascript", ".js")
    mimetypes.add_type("text/css", ".css")
    return UIServer().start()


def start_mirror_server(port: int = 48738) -> UIServer:
    """Serve the real aiOS WebView UI on a stable USB-forwardable port."""
    mimetypes.add_type("application/javascript", ".js")
    mimetypes.add_type("text/css", ".css")
    return UIServer(("127.0.0.1", int(port)), mirror_bootstrap=True).start()


if __name__ == "__main__":  # manual smoke test: python -m aios_ui.server
    server = start_server()
    print(server.url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.stop()
