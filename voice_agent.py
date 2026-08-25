"""Voice agent: turns a dictated transcript into an answer plus real actions.

The dictation overlay hands a finished transcript here whenever the macro
keyboard routed the turn to the agent instead of the cursor. The agent runs on
the OpenAI Responses API with tool calling, so one spoken sentence can search
the web, open an app, run a PowerShell command, or hand a full computer-control
task to the aiOS OPERATOR.

It is deliberately its own module: voice_dictation.py stays a dictation engine,
and this file owns everything that talks to the model or the machine.
"""

from __future__ import annotations

import base64
import io
import json
import os
import platform
import re
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "helper_config.json"
LOG_PATH = BASE_DIR / "voice-agent.log"
MEMORY_PATH = BASE_DIR / "voice-agent-memory.json"
TIMERS_PATH = BASE_DIR / "voice-agent-timers.json"
# The agent's own files: who it is, and what it has chosen to remember. It reads
# these every turn and is allowed to rewrite them, so "be less formal" or
# "remember that I use Vite" become durable instead of lasting one conversation.
SELF_DIR = BASE_DIR / "agent_self"
SOUL_PATH = SELF_DIR / "SOUL.md"
SELF_MEMORY_PATH = SELF_DIR / "MEMORY.md"
SELF_FILE_LIMIT = 24000
HELPER_HOST = "127.0.0.1"
HELPER_PORT = 48736  # aiOS helper_overlay command server
CODE_PORT = int(os.environ.get("AIOS_PHONE_BRIDGE_PORT", "5000"))
SHELL_TIMEOUT_SECONDS = 45
TOOL_OUTPUT_LIMIT = 4000
FILE_READ_LIMIT = 20000
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
OPERATOR_EVENTS_PATH = BASE_DIR / "phone_operator_events" / "events.jsonl"
OPERATOR_STATUS_PATH = BASE_DIR / "phone_operator_events" / "status.json"
OPERATOR_WAIT_SECONDS = max(30.0, float(os.environ.get("AIOS_OPERATOR_WAIT_SECONDS", "1800")))
OPERATOR_POLL_SECONDS = 0.20
# How long a new spoken turn waits for the previous one before it is treated as
# an interjection. Long enough to swallow a double-tap, short enough that the
# user never sits watching a frozen "thinking…".
TURN_ACQUIRE_SECONDS = 0.35
# Transient OpenAI failures are worth one quiet retry; a spoken turn that dies
# on a network blip is just lost.
API_RETRIES = 2
API_RETRY_BACKOFF = 0.8
MAX_FACTS = 200

DEFAULT_AGENT_SETTINGS = {
    "agent_model": "gpt-5.6-luna",
    "agent_reasoning": "low",
    "agent_max_rounds": 6,
    "agent_web_search": True,
    "agent_shell": True,
    "agent_operator": True,
    "agent_open_apps": True,
    "agent_memory_minutes": 10,
    "agent_clipboard_read": True,
    "agent_screen": True,
    "agent_files": True,
    "agent_media": True,
    "agent_timers": True,
    "agent_windows": True,
    "agent_remember": True,
    "agent_file_roots": [],
    "agent_shell_guard": True,
    "agent_shell_confirm": True,
    "agent_persist_memory": True,
}

# Shell commands that are never worth the risk of a mis-transcription. The
# agent's input is dictated speech; "delete the temp folder" and "delete the
# temp folder recursively from C:" are one Whisper slip apart.
SHELL_DENY_PATTERNS = (
    (r"\bremove-item\b[^|;]*\s-(recurse|force)\b", "recursive or forced delete"),
    (r"\brd\b\s+/s|\brmdir\b\s+/s", "recursive directory delete"),
    (r"\bformat(-volume)?\b", "disk format"),
    (r"\bclear-disk\b|\bremove-partition\b|\binitialize-disk\b", "disk operation"),
    (r"\bdiskpart\b|\bbcdedit\b|\bbootrec\b", "boot or partition tooling"),
    (r"\bstop-computer\b|\brestart-computer\b|\bshutdown\b(?!.*\/a)", "shutting the machine down"),
    (r"\bremove-item\b[^|;]*\bhk(lm|cu|cr|u|cc):", "registry delete"),
    (r"\bset-executionpolicy\b|\bdisable-\w*(firewall|defender|realtimemonitoring)", "security setting change"),
    (r"\bcipher\b\s+/w|\bsdelete\b", "secure wipe"),
    (r"\bnet\s+user\b[^|;]*\/(add|delete)", "local account change"),
    (r"\binvoke-(expression|webrequest)\b[^|;]*\|\s*(iex|invoke-expression)", "download-and-execute"),
    (r"\biwr\b[^|;]*\|\s*iex\b", "download-and-execute"),
    (r"\bremove-item\b\s+[\"']?[a-z]:[\\/][\"']?\s*$", "deleting a drive root"),
)

# Commands that only read state. Anything outside this set needs confirmation
# when agent_shell_confirm is on.
SHELL_READONLY_PREFIXES = (
    "get-", "measure-", "test-", "select-", "where-", "sort-", "format-table",
    "format-list", "compare-", "resolve-", "convertfrom-", "convertto-", "out-string",
    "echo", "write-output", "write-host", "dir", "ls", "cat", "type", "whoami",
    "hostname", "ipconfig", "systeminfo", "date", "tree", "find", "findstr", "ping",
    "nslookup", "tasklist", "wmic", "$", "[", "(",
)


def log_event(message):
    try:
        with LOG_PATH.open("a", encoding="utf-8") as file:
            file.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def load_config():
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def agent_settings():
    config = load_config()
    settings = dict(DEFAULT_AGENT_SETTINGS)
    voice = config.get("voice_dictation")
    if isinstance(voice, dict):
        settings.update({key: value for key, value in voice.items() if key in settings})
    model = str(settings.get("agent_model") or DEFAULT_AGENT_SETTINGS["agent_model"])
    if model.startswith("openrouter:"):
        import openrouter_client

        settings["provider"] = "openrouter"
        settings["api_base"] = openrouter_client.API_BASE
        settings["api_key"] = openrouter_client.get_api_key(config=config)
        settings["agent_model"] = model.split(":", 1)[1]
    else:
        settings["provider"] = "openai"
        settings["api_base"] = ""
        settings["api_key"] = str(
            config.get("openai_api_key") or os.environ.get("OPENAI_API_KEY") or ""
        ).strip()
    settings["config"] = config
    return settings


def normalize_name(text):
    return "".join(char for char in str(text).casefold() if char.isalnum())


def start_menu_shortcuts():
    """name -> .lnk path for everything on the Start Menu (both hives)."""
    shortcuts = {}
    roots = [
        Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs",
        Path(os.environ.get("PROGRAMDATA", "")) / r"Microsoft\Windows\Start Menu\Programs",
    ]
    for root in roots:
        if not str(root) or not root.exists():
            continue
        for item in root.rglob("*.lnk"):
            key = item.stem
            if any(word in key.casefold() for word in ("uninstall", "readme", "help")):
                continue
            shortcuts.setdefault(key, item)
    return shortcuts


def send_to_helper(action, text="", options=None):
    """Fire a command at the running aiOS window (chat, operator, voice log)."""
    payload = {"action": action, "text": text}
    if options:
        payload["options"] = options
    try:
        with socket.create_connection((HELPER_HOST, HELPER_PORT), timeout=1.5) as client:
            client.sendall(json.dumps(payload).encode("utf-8"))
        return True
    except OSError as exc:
        log_event(f"helper command {action} failed: {exc}")
        return False


def foreground_window_title():
    if os.name != "nt":
        return ""
    try:
        import ctypes

        user32 = ctypes.windll.user32
        handle = user32.GetForegroundWindow()
        length = user32.GetWindowTextLengthW(handle)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(handle, buffer, length + 1)
        return buffer.value
    except Exception:
        return ""


def classify_shell_command(command, settings=None):
    """Decide whether a spoken PowerShell command may run unattended.

    Returns (verdict, reason) where verdict is "allow", "confirm" or "deny".
    """
    settings = settings or {}
    text = str(command or "").strip()
    if not text:
        return "deny", "no command given"
    lowered = text.casefold()
    if settings.get("agent_shell_guard", True):
        for pattern, reason in SHELL_DENY_PATTERNS:
            if re.search(pattern, lowered):
                return "deny", reason
    if not settings.get("agent_shell_confirm", True):
        return "allow", ""
    # Split on the usual separators so `Get-Process; Remove-Item x` is judged on
    # its most dangerous half, not its first word.
    for part in re.split(r"[;|&]+|\|\|", lowered):
        part = part.strip().lstrip("(").strip()
        if not part:
            continue
        if not part.startswith(SHELL_READONLY_PREFIXES):
            return "confirm", part.split()[0] if part.split() else part
    return "allow", ""


def default_file_roots():
    """Directories the file tools may touch when nothing is configured."""
    home = Path.home()
    roots = [BASE_DIR]
    for name in ("Documents", "Desktop", "Downloads"):
        candidate = home / name
        if candidate.exists():
            roots.append(candidate)
    return roots


def resolve_file_roots(settings):
    configured = (settings or {}).get("agent_file_roots") or []
    roots = []
    for entry in configured:
        try:
            path = Path(str(entry)).expanduser().resolve()
        except (OSError, ValueError):
            continue
        if path.exists():
            roots.append(path)
    return roots or default_file_roots()


def resolve_inside_roots(candidate, roots):
    """Resolve a user-supplied path and confirm it sits inside an allowed root.

    Returns (path, error). Resolution happens before the containment check so
    `..` and symlinks cannot escape.
    """
    text = str(candidate or "").strip().strip('"').strip("'")
    if not text:
        return None, "no path given"
    try:
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = roots[0] / path
        path = path.resolve()
    except (OSError, ValueError) as exc:
        return None, f"bad path: {exc}"
    for root in roots:
        try:
            path.relative_to(root)
        except ValueError:
            continue
        return path, ""
    allowed = ", ".join(str(root) for root in roots)
    return None, f"path is outside the allowed folders ({allowed})"


def desktop_monitors():
    """Physical monitors in the same stable 1-based order OPERATOR uses."""
    from agent_clicker.desktop_agent.screen import list_monitors

    return [monitor for monitor in list_monitors() if int(monitor.index) > 0]


def resolve_desktop_monitor(selector):
    monitors = desktop_monitors()
    raw = str(selector if selector not in (None, "") else "1").strip().casefold()
    match = re.search(r"\d+", raw)
    if not match:
        available = ", ".join(str(mon.index) for mon in monitors)
        return None, f"screen must be one of {available}"
    wanted = int(match.group(0))
    monitor = next((item for item in monitors if int(item.index) == wanted), None)
    if monitor is None:
        available = ", ".join(str(mon.index) for mon in monitors)
        return None, f"no screen {wanted}; connected screens are {available}"
    return monitor, ""


def monitor_context_line():
    try:
        monitors = desktop_monitors()
    except Exception as exc:
        return f"- Connected screens: unavailable ({exc})"
    labels = "; ".join(
        f"screen {mon.index}: {mon.width}x{mon.height} at virtual ({mon.left},{mon.top})"
        for mon in monitors
    )
    return f"- Connected screens: {len(monitors)}. {labels}"


def capture_screen_jpeg(screen="1", max_width=1536, quality=70):
    """Capture one monitor for the agent without hiding aiOS globally."""
    from PIL import Image
    from agent_clicker.desktop_agent.screen import (
        capture,
        current_process_windows_hidden_from_agent_capture,
    )

    monitor, error = resolve_desktop_monitor(screen)
    if error:
        raise ValueError(error)
    capture_token = f"voice:{threading.get_ident()}:{time.time_ns()}"
    helper_notified = send_to_helper("agent_capture_begin", capture_token)
    try:
        # This hides the dictation/voice HUD in this process. The helper command
        # does the same for the main aiOS window during this exact frame only.
        with current_process_windows_hidden_from_agent_capture(settle_seconds=0.05):
            image = capture(monitor)
    finally:
        if helper_notified:
            send_to_helper("agent_capture_end", capture_token)
    if image.width > max_width:
        height = max(1, round(image.height * max_width / image.width))
        image = image.resize((max_width, height), Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue(), image.size


def list_open_windows(limit=40):
    """Visible top-level windows as [{"title", "handle", "process"}]."""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    windows = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def collect(handle, _param):
        if not user32.IsWindowVisible(handle):
            return True
        length = user32.GetWindowTextLengthW(handle)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(handle, buffer, length + 1)
        title = buffer.value.strip()
        if not title or title in {"Program Manager", "Windows Input Experience"}:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        windows.append({"title": title, "handle": int(handle), "pid": int(pid.value)})
        return True

    user32.EnumWindows(collect, 0)
    try:
        import psutil

        for entry in windows:
            try:
                entry["process"] = psutil.Process(entry["pid"]).name()
            except Exception:
                entry["process"] = ""
    except ImportError:
        pass
    return windows[:limit]


@dataclass
class AgentResult:
    reply: str = ""
    error: str = ""
    tools: list = field(default_factory=list)
    tool_details: list = field(default_factory=list)
    elapsed: float = 0.0
    cancelled: bool = False


class CancelledTurn(Exception):
    """Raised inside a turn when the user asked the agent to stop."""


class VoiceAgent:
    """One long-lived agent; each spoken turn continues the same conversation."""

    def __init__(self, on_event=None, type_text=None, copy_text=None, hide_overlay=None, speak=None):
        self.on_event = on_event or (lambda kind, text: None)
        self._type_text = type_text
        self._copy_text = copy_text
        self._hide_overlay = hide_overlay
        self._speak = speak
        self._hide_requested = False
        self._client = None
        self._client_key = ""
        self._plan = []
        # The conversation itself: user turns, what the tools did, and the
        # replies. This is what gets replayed to the model every turn.
        self.turns = []
        self.history_limit = 24
        self._last_turn_at = 0.0
        # Held for the duration of one turn. Acquired with a short timeout so a
        # turn that is parked waiting on OPERATOR can never wedge the next one.
        self._lock = threading.Lock()
        # Set while a tool call is blocked on the OPERATOR agent. New speech
        # arriving in that window is routed straight to OPERATOR instead of
        # queueing behind a job that may run for half an hour.
        self._operator_active = threading.Event()
        self._operator_cancel = threading.Event()
        # Set by cancel(): unwinds the current turn at the next checkpoint.
        self._cancelled = threading.Event()
        self._timers = {}
        self._timer_seq = 0
        # Screenshots captured this turn, attached to the next model request.
        self._pending_images = []
        self._last_code_job_id = ""
        # Last visible image geometry per monitor. Click coordinates refer to
        # these pixels and are scaled back to the monitor's physical pixels.
        self._screen_views = {}
        self._facts = self._load_facts()
        self._load_memory()
        self._restore_timers()

    # ---------------------------------------------------------------- plumbing

    def _emit(self, kind, payload=""):
        try:
            self.on_event(kind, payload)
        except Exception as exc:
            log_event(f"event callback failed: {exc}")

    def _ensure_client(self, api_key, base_url=""):
        if not api_key:
            raise RuntimeError("No API key — set it in aiOS Settings.")
        client_key = f"{base_url}|{api_key}"
        if self._client is not None and self._client_key == client_key:
            return self._client
        from openai import OpenAI

        options = {"api_key": api_key}
        if base_url:
            options["base_url"] = base_url
        self._client = OpenAI(**options)
        self._client_key = client_key
        return self._client

    def warmup(self):
        """Pre-create the OpenAI client so the first real turn isn't cold."""
        settings = agent_settings()
        key = str(settings.get("api_key") or "").strip()
        if not key:
            return False
        self._ensure_client(key, str(settings.get("api_base") or ""))
        return True

    def reset(self):
        self.turns = []
        self._last_turn_at = 0.0
        self._save_memory()

    # -------------------------------------------------------------- persistence

    def _load_memory(self):
        """Restore the conversation from disk so a restart is not amnesia."""
        if not agent_settings().get("agent_persist_memory", True):
            return
        try:
            data = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        turns = data.get("turns")
        if not isinstance(turns, list):
            return
        restored = []
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            role = str(turn.get("role") or "")
            if role not in {"user", "assistant", "tool"}:
                continue
            restored.append(
                {"role": role, "text": str(turn.get("text") or ""), "at": float(turn.get("at") or 0.0)}
            )
        self.turns = restored[-max(4, int(self.history_limit)):]
        try:
            self._last_turn_at = float(data.get("last_turn_at") or 0.0)
        except (TypeError, ValueError):
            self._last_turn_at = 0.0
        if self.turns:
            log_event(f"restored {len(self.turns)} turns from disk")

    def _save_memory(self):
        if not agent_settings().get("agent_persist_memory", True):
            return
        payload = {"turns": self.turns, "last_turn_at": self._last_turn_at}
        try:
            MEMORY_PATH.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            log_event(f"could not persist memory: {exc}")

    # ---------------------------------------------------------------- own files

    @staticmethod
    def _ensure_self_dir():
        try:
            SELF_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log_event(f"could not create {SELF_DIR}: {exc}")

    def read_self_file(self, path):
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def soul(self):
        """The agent's own description of how it should behave."""
        return self.read_self_file(SOUL_PATH).strip()

    def _load_facts(self):
        """Remembered facts, parsed out of the bullets in MEMORY.md.

        Markdown rather than JSON so the agent — and the user — can open the
        file and edit it like any other note.
        """
        text = self.read_self_file(SELF_MEMORY_PATH)
        facts = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("- ", "* ")):
                fact = stripped[2:].strip()
                if fact:
                    facts.append(fact)
        return facts[:MAX_FACTS]

    def _save_facts(self):
        """Rewrite the Facts section of MEMORY.md, leaving the prose above it."""
        self._ensure_self_dir()
        existing = self.read_self_file(SELF_MEMORY_PATH)
        marker = "## Facts"
        if marker in existing:
            head = existing.split(marker)[0].rstrip()
        else:
            head = (existing.rstrip() or "# MEMORY").rstrip()
        body = "\n".join(f"- {fact}" for fact in self._facts)
        content = f"{head}\n\n{marker}\n{body}\n" if body else f"{head}\n\n{marker}\n"
        try:
            SELF_MEMORY_PATH.write_text(content, encoding="utf-8")
        except OSError as exc:
            log_event(f"could not persist memory notes: {exc}")

    def _self_file(self, name):
        """Resolve a name inside the agent's own directory, and nowhere else."""
        raw = str(name or "").strip().strip('"').strip("'")
        if not raw:
            return None, "no file name given"
        candidate = Path(raw)
        if candidate.suffix.lower() not in {".md", ".txt", ""}:
            return None, "only .md and .txt files live here"
        if not candidate.suffix:
            candidate = candidate.with_suffix(".md")
        try:
            resolved = (SELF_DIR / candidate.name).resolve()
            resolved.relative_to(SELF_DIR.resolve())
        except (OSError, ValueError) as exc:
            return None, f"bad file name: {exc}"
        return resolved, ""

    # ------------------------------------------------------------------ control

    def cancel(self):
        """Abandon whatever the agent is doing right now.

        Unwinds an in-flight turn at its next checkpoint and releases any
        OPERATOR wait, so the overlay never sits on a dead "thinking…".
        """
        self._cancelled.set()
        self._operator_cancel.set()
        log_event("agent turn cancelled by request")
        return True

    def busy(self):
        """True while a turn is in flight."""
        return self._lock.locked()

    def operator_running(self):
        return self._operator_active.is_set()

    # ------------------------------------------------------------------ prompt

    def _instructions(self, settings):
        config = settings.get("config") or {}
        now = time.localtime()
        window = foreground_window_title()
        notes = str((config.get("dashboard") or {}).get("notes") or "").strip()
        soul = self.soul()
        lines = [
            "You are the aiOS voice agent — a resident agent on Calle's Windows PC,",
            "not a chat window in a browser. Understand your own situation:",
            "",
            "- Someone holds a key, speaks, and releases. Whisper transcribes that and",
            "  hands you the text. So your input is SPEECH: expect transcription errors,",
            "  missing punctuation and mixed Swedish/English. Read through the mistakes",
            "  rather than asking about them.",
            "- Your reply is spoken out loud by a speech engine and shown on a small",
            "  overlay. You are heard, not read. Never use markdown, bullet lists,",
            "  headings, code blocks or emoji — only sentences a person can listen to.",
            "- You are not a sandboxed assistant. You have real hands on this machine:",
            "  the tools below actually open apps, run commands, move files, change the",
            "  volume and drive the mouse. Use them instead of describing what could be done.",
            "- You have a body of files at agent_self/. SOUL.md is how you should act and",
            "  MEMORY.md is what you have chosen to remember. You read both every turn and",
            "  you may rewrite them. When Calle tells you to behave differently, or to",
            "  remember something for good, change the file — that is what makes it stick.",
            "",
            "How to behave:",
            "- If the user asks for an action, DO it with a tool, then confirm in one short sentence.",
            "- If the user asks a question, answer it directly. Two or three sentences at most.",
            "- Never ask a follow-up question when you can reasonably act. Act, then report.",
            "- If a tool fails, say plainly what failed. Do not invent success.",
            "- Answer in the language you were spoken to.",
            "- Do quick, bounded desktop actions yourself. Opening an app or URL, reading a screen,",
            "  making one or two obvious clicks, starting a video, scrolling once, or using media",
            "  controls are YOUR job and do not need OPERATOR.",
            "- Hand work to OPERATOR when it needs repeated visual reasoning, more than about three",
            "  clicks, filling forms, editing substantial content, recovery from uncertainty, or",
            "  verification across a longer workflow. Do not use OPERATOR as a reflex.",
            "- For software projects, code changes, repo work, tests, commits, pushes, or deployments,",
            "  dispatch the work through the CODE tools to Codex, Claude Code, or Cursor Agent. Do not",
            "  use OPERATOR or raw PowerShell for work that belongs to a coding agent.",
        ]
        if soul:
            lines += [
                "",
                "Your SOUL.md — this is your own description of yourself, and it wins over",
                "the generic guidance above wherever the two disagree:",
                "",
                soul[:6000],
            ]
        lines += [
            "",
            "Machine context:",
            f"- User: {os.environ.get('USERNAME') or 'Calle'} on {platform.node()} ({platform.system()} {platform.release()})",
            f"- Local time: {time.strftime('%A %d %B %Y, %H:%M', now)}",
            f"- Project root: {config.get('project_root', '')}",
            monitor_context_line(),
        ]
        if window:
            lines.append(f"- Focused window right now: {window}")
        if notes:
            lines.append(f"- The user's dashboard notes say: {notes[:600]}")
        if self._facts:
            lines.append("")
            lines.append("From your MEMORY.md — things you chose to remember:")
            lines.extend(f"- {fact}" for fact in self._facts[-60:])
        lines += [
            "",
            "Tool notes:",
            "- open_app takes a plain app name; it resolves against the Start Menu itself.",
            "- run_powershell is for quick local facts and small changes. Keep commands short,",
            "  non-interactive, and never destructive unless the user clearly asked for it.",
            "  Destructive commands are blocked outright, and state-changing ones come back as",
            "  needs_confirmation — when that happens, tell the user exactly what you want to run",
            "  and call the tool again with confirm set to true only after they agree.",
            "- This PC has multiple screens and an app may be on any of them. read_screen accepts",
            "  screen 1, 2, 3, or all. If you do not know where something opened, read all screens.",
            "  YOU look at the attached pixels; nothing describes them for you.",
            "- click_screen uses coordinates from the most recent read_screen image for that screen.",
            "  It can left-click, right-click, or double-click. scroll_screen scrolls at a visible",
            "  point. Use these directly for short obvious actions, then re-read to verify if needed.",
            "- read_self_file / write_self_file / append_self_file / list_self_files are your own",
            "  files. Edit SOUL.md when told how to behave; edit MEMORY.md for what to remember.",
            "  Read a file before you overwrite it, and keep SOUL.md written in your own voice.",
            "- search_files finds files by name or content. open_path reveals a file or folder.",
            "- system_status reports CPU, memory, disk, battery and uptime.",
            "- list_processes / kill_process manage what is running. Ask before killing anything",
            "  the user did not name.",
            "- read_url fetches a page as plain text when you need the actual contents rather",
            "  than a search result summary.",
            "- notify puts a message on the aiOS window without speaking it.",
            "- read_clipboard returns what the user has copied. Use it when they say 'this', 'that",
            "  link', 'what I just copied'.",
            "- read_file / write_file / append_file / list_files work inside the user's allowed",
            "  folders only. Prefer append_file over rewriting a file you have not read.",
            "- set_volume, media_control adjust the machine's audio. media_control takes",
            "  playpause, next, previous, stop or mute.",
            "- set_timer schedules a spoken reminder. The user hears it out loud when it fires,",
            "  so phrase the label as the thing to say.",
            "- list_windows / focus_window / close_window manage open windows by title.",
            "- remember stores a durable fact about the user across restarts. Use it when they say",
            "  'remember that…'. Do not store secrets or passwords.",
            "- operator_task hands a job to the aiOS OPERATOR agent, which takes the mouse and",
            "  keyboard and works visibly. Use it for longer multi-step GUI work, not quick",
            "  actions you can finish with open_app, read_screen, click_screen, or media_control.",
            "  The tool waits and returns OPERATOR's",
            "  verified completion, failure, or question. Summarize that result for the user.",
            "- If OPERATOR returns needs_input, relay its exact question plainly and wait for the",
            "  user's answer. Never claim OPERATOR finished when its result says otherwise.",
            "- operator_followup sends more instructions to a RUNNING operator job (or starts",
            "  a new one if nothing is running). Use this when the user says continue / also /",
            "  next / tell the operator …",
            "- operator_stop cancels the running OPERATOR job. Use when the user says stop,",
            "  cancel, abort, or kill the operator.",
            "- CODE jobs are persistent native coding-agent sessions. The provider must always be",
            "  explicit: codex, claude, or cursor. The exact model, intelligence/reasoning level,",
            "  and fast-mode choice must also be explicit. Never silently choose or substitute them.",
            "- If the user omitted any CODE choice, call code_capabilities, propose the most sensible",
            "  available values in one short spoken sentence, and ask before launching. If everything",
            "  is clear, call code_start immediately. Rewrite their request into a self-contained brief",
            "  with outcome, context, constraints, acceptance checks, and finish expectations.",
            "- CODE providers have full autonomy and approve their own permission prompts. Their native",
            "  terminal result is the completion report; relay it without independently re-verifying.",
            "  If a job asks a question, repeat the exact question and use code_continue for the answer.",
            "- Normal code_continue messages queue behind the active turn. Set urgent=true only when the",
            "  user says urgent, interrupt, immediately, or stop what it is doing and change direction.",
            "- If the user asks to switch an existing CODE session to another provider, call",
            "  code_handoff. This preserves the aiOS job and working tree, creates a provider-neutral",
            "  context manifest, and starts a fresh native target session. Never describe it as the",
            "  same native provider transcript. Exact target model/reasoning/fast choices are required.",
            "- Use code_list/code_status to find current jobs. Follow-ups continue the same native session.",
            "- If a chosen provider is installed but not signed in, offer to open its official local sign-in",
            "  terminal. Call code_setup only after the user agrees; aiOS never handles the credentials.",
            "- type_text types into whatever window the user had focused when they spoke.",
            "  Use it when they dictate content meant for that window.",
            "- hide_overlay dismisses the visible dictation/chat overlay without deleting the",
            "  conversation. Use it when the user clearly ends the conversation (for example",
            "  'perfect, thanks, bye' or 'goodbye') or directly asks to close/hide the overlay.",
            "  Before the overlay closes, always return a short, natural spoken sign-off such as",
            "  'Alright, thank you. Goodbye.' Never end this tool turn with an empty response.",
        ]
        return "\n".join(lines)

    @staticmethod
    def _code_tools():
        attachment_schema = {
            "type": "array",
            "description": "Optional absolute local file paths or http/https links explicitly supplied by the user.",
            "items": {"type": "string"},
        }
        return [
            {
                "type": "function",
                "name": "code_capabilities",
                "description": "List current local Codex, Claude Code, and Cursor readiness plus exact model and intelligence choices. Call before proposing missing CODE settings.",
                "parameters": {
                    "type": "object",
                    "properties": {"refresh": {"type": "boolean", "description": "Force a live capability refresh."}},
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "code_setup",
                "description": "Open the official local CLI sign-in terminal for Codex, Claude Code, or Cursor. Only call after the user asks or agrees to sign in.",
                "parameters": {
                    "type": "object",
                    "properties": {"provider": {"type": "string", "enum": ["codex", "claude", "cursor"]}},
                    "required": ["provider"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "code_start",
                "description": "Start a persistent autonomous coding job. Only call when provider, exact model, reasoning/intelligence, fast choice, folder, and brief are all explicit.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "provider": {"type": "string", "enum": ["codex", "claude", "cursor"]},
                        "cwd": {"type": "string", "description": "Absolute project folder on this PC."},
                        "brief": {"type": "string", "description": "Self-contained outcome, context, constraints, acceptance checks, and finish expectations."},
                        "model": {"type": "string", "description": "Exact model id returned by code_capabilities."},
                        "reasoning": {"type": "string", "description": "Exact intelligence/reasoning value returned for that model."},
                        "fast": {"type": "boolean", "description": "Explicit fast-mode choice."},
                        "attachments": attachment_schema,
                    },
                    "required": ["provider", "cwd", "brief", "model", "reasoning", "fast"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "code_list",
                "description": "List persistent CODE jobs with ids, providers, projects, settings, and current state.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "provider": {"type": "string", "enum": ["codex", "claude", "cursor"]},
                        "status": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "code_status",
                "description": "Read one CODE job's current state and recent output, including its exact question or final report.",
                "parameters": {
                    "type": "object",
                    "properties": {"job_id": {"type": "string", "description": "CODE job id. Omit to use the last job started in this voice conversation."}},
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "code_continue",
                "description": "Send a follow-up or answer to the same native CODE session. Normal messages queue; urgent messages steer or interrupt the active turn.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string", "description": "CODE job id. Omit to use the last job."},
                        "text": {"type": "string"},
                        "urgent": {"type": "boolean"},
                        "attachments": attachment_schema,
                    },
                    "required": ["text", "urgent"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "code_handoff",
                "description": "Switch an existing logical CODE session to a different provider. aiOS transfers a bounded context manifest and working-tree state into a new native target session; it does not claim native transcript identity.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string", "description": "CODE job id. Omit to use the last job."},
                        "provider": {"type": "string", "enum": ["codex", "claude", "cursor"]},
                        "model": {"type": "string", "description": "Exact target model id returned by code_capabilities."},
                        "reasoning": {"type": "string", "description": "Exact target reasoning/intelligence level."},
                        "fast": {"type": "boolean", "description": "Explicit target fast-mode choice."},
                        "instruction": {"type": "string", "description": "Optional direction for what the target provider should do next; omit to continue the transferred task."},
                    },
                    "required": ["provider", "model", "reasoning", "fast"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "code_stop",
                "description": "Stop the active turn for a CODE job without deleting its persistent session.",
                "parameters": {
                    "type": "object",
                    "properties": {"job_id": {"type": "string", "description": "CODE job id. Omit to use the last job."}},
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "code_delete",
                "description": "Delete a stopped CODE session transcript and metadata. Project files are not deleted. Only use when the user explicitly asks to delete it and confirms deletion.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string", "description": "CODE job id. Omit to use the last job."},
                        "confirm": {"type": "boolean", "description": "True only after the user explicitly confirmed deletion."},
                    },
                    "required": ["confirm"],
                    "additionalProperties": False,
                },
            },
        ]

    def _tools(self, settings):
        tools = []
        if settings.get("agent_web_search"):
            tools.append({
                "type": "openrouter:web_search" if settings.get("provider") == "openrouter" else "web_search"
            })
        tools.append(
            {
                "type": "function",
                "name": "update_plan",
                "description": (
                    "Create or update the visible task plan. Use for work with multiple meaningful steps; "
                    "keep at most one step in_progress and mark finished steps completed."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "steps": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "step": {"type": "string"},
                                    "status": {
                                        "type": "string",
                                        "enum": ["pending", "in_progress", "completed"],
                                    },
                                },
                                "required": ["step", "status"],
                                "additionalProperties": False,
                            },
                        }
                    },
                    "required": ["steps"],
                    "additionalProperties": False,
                },
            }
        )
        if settings.get("agent_open_apps"):
            tools.append(
                {
                    "type": "function",
                    "name": "open_app",
                    "description": "Open an installed application by name (resolves against the Start Menu).",
                    "parameters": {
                        "type": "object",
                        "properties": {"name": {"type": "string", "description": "App name, e.g. 'Spotify'"}},
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                }
            )
            tools.append(
                {
                    "type": "function",
                    "name": "open_url",
                    "description": "Open a URL in the default browser. Also used for web apps.",
                    "parameters": {
                        "type": "object",
                        "properties": {"url": {"type": "string"}},
                        "required": ["url"],
                        "additionalProperties": False,
                    },
                }
            )
        if settings.get("agent_shell"):
            tools.append(
                {
                    "type": "function",
                    "name": "run_powershell",
                    "description": (
                        "Run a short non-interactive PowerShell command on this PC and return its output. "
                        "Use for local facts (files, processes, settings) and small changes. "
                        "State-changing commands return needs_confirmation until you re-send them "
                        "with confirm=true after the user has agreed out loud."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string"},
                            "confirm": {
                                "type": "boolean",
                                "description": "True only after the user approved this exact command.",
                            },
                        },
                        "required": ["command"],
                        "additionalProperties": False,
                    },
                }
            )
        if settings.get("agent_operator"):
            tools.append(
                {
                    "type": "function",
                    "name": "operator_task",
                    "description": (
                        "Hand a multi-step computer-control task to the aiOS OPERATOR agent, which drives "
                        "the mouse and keyboard. Waits for OPERATOR to finish, fail, or ask a "
                        "question, then returns that terminal result. Prefer operator_followup "
                        "if an operator job is already running."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {"task": {"type": "string", "description": "The task, written for an agent."}},
                        "required": ["task"],
                        "additionalProperties": False,
                    },
                }
            )
            tools.append(
                {
                    "type": "function",
                    "name": "operator_followup",
                    "description": (
                        "Send a follow-up instruction to the running OPERATOR job, or start a new "
                        "operator task if none is running. Use for 'continue', 'also do…', answers "
                        "to an operator question, etc."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "message": {
                                "type": "string",
                                "description": "Follow-up instruction or answer for the operator.",
                            },
                            "steps": {
                                "type": "integer",
                                "description": "Optional extra step budget to grant (1–200).",
                            },
                        },
                        "required": ["message"],
                        "additionalProperties": False,
                    },
                }
            )
            tools.append(
                {
                    "type": "function",
                    "name": "operator_stop",
                    "description": "Stop / cancel the running aiOS OPERATOR job immediately.",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }
            )
        tools.append(
            {
                "type": "function",
                "name": "type_text",
                "description": "Type text into the window the user had focused when they started speaking.",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
            }
        )
        tools.append(
            {
                "type": "function",
                "name": "copy_text",
                "description": "Put text on the clipboard.",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
            }
        )
        tools.append(
            {
                "type": "function",
                "name": "add_note",
                "description": "Append a line to the notes on the user's aiOS dashboard.",
                "parameters": {
                    "type": "object",
                    "properties": {"note": {"type": "string"}},
                    "required": ["note"],
                    "additionalProperties": False,
                },
            }
        )
        if settings.get("agent_clipboard_read"):
            tools.append(
                {
                    "type": "function",
                    "name": "read_clipboard",
                    "description": (
                        "Read what is currently on the clipboard. Use when the user refers to "
                        "'this', 'that link', or something they just copied."
                    ),
                    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                }
            )
        if settings.get("agent_screen"):
            tools.append(
                {
                    "type": "function",
                    "name": "read_screen",
                    "description": (
                        "Capture one physical screen or all connected screens and attach the pixels "
                        "to your next turn. Use 'all' when you do not know which screen contains the app."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": "What to find out, e.g. 'what does the error say?'",
                            },
                            "screen": {
                                "type": "string",
                                "enum": ["1", "2", "3", "all"],
                                "description": "Physical screen number, or all to inspect every screen.",
                            },
                        },
                        "required": ["question", "screen"],
                        "additionalProperties": False,
                    },
                }
            )
            tools.append(
                {
                    "type": "function",
                    "name": "click_screen",
                    "description": (
                        "Make one quick click on a physical screen. Coordinates are pixels from the "
                        "latest read_screen image for that screen. Read the screen first."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "screen": {"type": "string", "enum": ["1", "2", "3"]},
                            "x": {"type": "integer", "description": "X pixel in the attached screen image."},
                            "y": {"type": "integer", "description": "Y pixel in the attached screen image."},
                            "button": {"type": "string", "enum": ["left", "right"]},
                            "clicks": {"type": "integer", "enum": [1, 2]},
                        },
                        "required": ["screen", "x", "y", "button", "clicks"],
                        "additionalProperties": False,
                    },
                }
            )
            tools.append(
                {
                    "type": "function",
                    "name": "scroll_screen",
                    "description": (
                        "Scroll once at a point visible in the latest read_screen image. Positive "
                        "amount scrolls up and negative amount scrolls down."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "screen": {"type": "string", "enum": ["1", "2", "3"]},
                            "x": {"type": "integer"},
                            "y": {"type": "integer"},
                            "amount": {"type": "integer", "minimum": -12, "maximum": 12},
                        },
                        "required": ["screen", "x", "y", "amount"],
                        "additionalProperties": False,
                    },
                }
            )
        if settings.get("agent_files"):
            tools.append(
                {
                    "type": "function",
                    "name": "read_file",
                    "description": "Read a text file from the user's allowed folders.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                }
            )
            tools.append(
                {
                    "type": "function",
                    "name": "write_file",
                    "description": (
                        "Create or overwrite a text file in the user's allowed folders. "
                        "Prefer append_file when adding to something that already exists."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                        "required": ["path", "content"],
                        "additionalProperties": False,
                    },
                }
            )
            tools.append(
                {
                    "type": "function",
                    "name": "append_file",
                    "description": "Append a line or block of text to a file in the allowed folders.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                        "required": ["path", "content"],
                        "additionalProperties": False,
                    },
                }
            )
            tools.append(
                {
                    "type": "function",
                    "name": "list_files",
                    "description": "List the entries of a folder inside the user's allowed folders.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "pattern": {"type": "string", "description": "Optional glob, e.g. '*.py'"},
                        },
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                }
            )
        if settings.get("agent_media"):
            tools.append(
                {
                    "type": "function",
                    "name": "set_volume",
                    "description": "Set the master output volume to a percentage, or read it back.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "percent": {
                                "type": "integer",
                                "description": "0–100. Omit to just report the current volume.",
                            }
                        },
                        "additionalProperties": False,
                    },
                }
            )
            tools.append(
                {
                    "type": "function",
                    "name": "media_control",
                    "description": "Control media playback on this PC.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["playpause", "next", "previous", "stop", "mute", "unmute"],
                            }
                        },
                        "required": ["action"],
                        "additionalProperties": False,
                    },
                }
            )
        if settings.get("agent_timers"):
            tools.append(
                {
                    "type": "function",
                    "name": "set_timer",
                    "description": (
                        "Schedule a spoken reminder. The label is read out loud when it fires, "
                        "so write it as the sentence the user should hear."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "seconds": {"type": "integer", "description": "Delay in seconds (1–86400)."},
                            "label": {"type": "string", "description": "What to say when it fires."},
                        },
                        "required": ["seconds", "label"],
                        "additionalProperties": False,
                    },
                }
            )
            tools.append(
                {
                    "type": "function",
                    "name": "list_timers",
                    "description": "List the reminders that are still pending, with their ids.",
                    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                }
            )
            tools.append(
                {
                    "type": "function",
                    "name": "cancel_timer",
                    "description": "Cancel a pending reminder by id, or all of them.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "timer_id": {"type": "string", "description": "Id from list_timers, or 'all'."}
                        },
                        "required": ["timer_id"],
                        "additionalProperties": False,
                    },
                }
            )
        if settings.get("agent_windows"):
            tools.append(
                {
                    "type": "function",
                    "name": "list_windows",
                    "description": "List the visible windows open right now, with their titles.",
                    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                }
            )
            tools.append(
                {
                    "type": "function",
                    "name": "focus_window",
                    "description": "Bring a window to the front by a piece of its title.",
                    "parameters": {
                        "type": "object",
                        "properties": {"title": {"type": "string"}},
                        "required": ["title"],
                        "additionalProperties": False,
                    },
                }
            )
            tools.append(
                {
                    "type": "function",
                    "name": "close_window",
                    "description": (
                        "Ask a window to close by a piece of its title. Sends a normal close "
                        "request, so the app can still prompt to save."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {"title": {"type": "string"}},
                        "required": ["title"],
                        "additionalProperties": False,
                    },
                }
            )
        if settings.get("agent_files"):
            tools.append(
                {
                    "type": "function",
                    "name": "search_files",
                    "description": (
                        "Find files by name, or by text inside them, under one of the allowed folders."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Name fragment or glob."},
                            "path": {"type": "string", "description": "Folder to search. Defaults to the project root."},
                            "contains": {
                                "type": "string",
                                "description": "Optional text that must appear inside the file.",
                            },
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                }
            )
            tools.append(
                {
                    "type": "function",
                    "name": "open_path",
                    "description": "Open a file with its default app, or reveal a folder in Explorer.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                }
            )
        if settings.get("agent_shell"):
            tools.append(
                {
                    "type": "function",
                    "name": "system_status",
                    "description": "CPU, memory, disk, battery and uptime for this PC.",
                    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                }
            )
            tools.append(
                {
                    "type": "function",
                    "name": "list_processes",
                    "description": "The heaviest running processes, by memory or CPU.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "sort": {"type": "string", "enum": ["memory", "cpu"]},
                            "limit": {"type": "integer", "description": "How many to return (1–40)."},
                        },
                        "additionalProperties": False,
                    },
                }
            )
            tools.append(
                {
                    "type": "function",
                    "name": "kill_process",
                    "description": (
                        "End a process by name or pid. Only use it for something the user named "
                        "or clearly agreed to."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}, "pid": {"type": "integer"}},
                        "additionalProperties": False,
                    },
                }
            )
        if settings.get("agent_web_search"):
            tools.append(
                {
                    "type": "function",
                    "name": "read_url",
                    "description": (
                        "Fetch a web page and return its text. Use when you need the actual "
                        "contents of a page rather than a search summary."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {"url": {"type": "string"}},
                        "required": ["url"],
                        "additionalProperties": False,
                    },
                }
            )
        tools.extend(self._code_tools())
        tools.append(
            {
                "type": "function",
                "name": "notify",
                "description": "Put a short message on the aiOS window without speaking it.",
                "parameters": {
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                    "additionalProperties": False,
                },
            }
        )
        if settings.get("agent_self_edit", True):
            tools.append(
                {
                    "type": "function",
                    "name": "list_self_files",
                    "description": "List your own files in agent_self/ — SOUL.md, MEMORY.md and any notes.",
                    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                }
            )
            tools.append(
                {
                    "type": "function",
                    "name": "read_self_file",
                    "description": (
                        "Read one of your own files. SOUL.md is how you should act, MEMORY.md is "
                        "what you remember. Always read before you overwrite."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {"name": {"type": "string", "description": "e.g. SOUL.md"}},
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                }
            )
            tools.append(
                {
                    "type": "function",
                    "name": "write_self_file",
                    "description": (
                        "Rewrite one of your own files. Use this when told to change how you behave: "
                        "edit SOUL.md so the change is permanent. Keep it in your own voice."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}, "content": {"type": "string"}},
                        "required": ["name", "content"],
                        "additionalProperties": False,
                    },
                }
            )
            tools.append(
                {
                    "type": "function",
                    "name": "append_self_file",
                    "description": "Add a line or paragraph to one of your own files.",
                    "parameters": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}, "content": {"type": "string"}},
                        "required": ["name", "content"],
                        "additionalProperties": False,
                    },
                }
            )
        if settings.get("agent_remember"):
            tools.append(
                {
                    "type": "function",
                    "name": "remember",
                    "description": (
                        "Store a durable fact about the user that should survive restarts and "
                        "conversation resets. Never store passwords, keys or card numbers."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {"fact": {"type": "string"}},
                        "required": ["fact"],
                        "additionalProperties": False,
                    },
                }
            )
            tools.append(
                {
                    "type": "function",
                    "name": "forget",
                    "description": "Remove a stored fact that matches the given text, or 'all'.",
                    "parameters": {
                        "type": "object",
                        "properties": {"match": {"type": "string"}},
                        "required": ["match"],
                        "additionalProperties": False,
                    },
                }
            )
        tools.append(
            {
                "type": "function",
                "name": "hide_overlay",
                "description": (
                    "Hide the visible aiOS dictation/Agent chat overlay while preserving conversation "
                    "memory. Use when the user says goodbye, clearly indicates they are finished, or "
                    "asks to close/dismiss/hide the overlay."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            }
        )
        return tools

    # ------------------------------------------------------------------- tools

    def _execute(self, name, arguments):
        handler = {
            "update_plan": self._tool_update_plan,
            "open_app": self._tool_open_app,
            "open_url": self._tool_open_url,
            "run_powershell": self._tool_run_powershell,
            "operator_task": self._tool_operator_task,
            "operator_followup": self._tool_operator_followup,
            "operator_stop": self._tool_operator_stop,
            "code_capabilities": self._tool_code_capabilities,
            "code_setup": self._tool_code_setup,
            "code_start": self._tool_code_start,
            "code_list": self._tool_code_list,
            "code_status": self._tool_code_status,
            "code_continue": self._tool_code_continue,
            "code_handoff": self._tool_code_handoff,
            "code_stop": self._tool_code_stop,
            "code_delete": self._tool_code_delete,
            "type_text": self._tool_type_text,
            "copy_text": self._tool_copy_text,
            "add_note": self._tool_add_note,
            "hide_overlay": self._tool_hide_overlay,
            "read_clipboard": self._tool_read_clipboard,
            "read_screen": self._tool_read_screen,
            "click_screen": self._tool_click_screen,
            "scroll_screen": self._tool_scroll_screen,
            "read_file": self._tool_read_file,
            "write_file": self._tool_write_file,
            "append_file": self._tool_append_file,
            "list_files": self._tool_list_files,
            "set_volume": self._tool_set_volume,
            "media_control": self._tool_media_control,
            "set_timer": self._tool_set_timer,
            "list_timers": self._tool_list_timers,
            "cancel_timer": self._tool_cancel_timer,
            "list_windows": self._tool_list_windows,
            "focus_window": self._tool_focus_window,
            "close_window": self._tool_close_window,
            "remember": self._tool_remember,
            "forget": self._tool_forget,
            "search_files": self._tool_search_files,
            "open_path": self._tool_open_path,
            "system_status": self._tool_system_status,
            "list_processes": self._tool_list_processes,
            "kill_process": self._tool_kill_process,
            "read_url": self._tool_read_url,
            "notify": self._tool_notify,
            "list_self_files": self._tool_list_self_files,
            "read_self_file": self._tool_read_self_file,
            "write_self_file": self._tool_write_self_file,
            "append_self_file": self._tool_append_self_file,
        }.get(name)
        if handler is None:
            return f"unknown tool {name}"
        try:
            return handler(arguments)
        except Exception as exc:
            log_event(f"tool {name} failed: {exc}")
            return f"tool failed: {exc}"

    def _tool_update_plan(self, arguments):
        rows = arguments.get("steps") if isinstance(arguments, dict) else []
        plan = []
        active_seen = False
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            step = str(row.get("step") or "").strip()
            status = str(row.get("status") or "pending").strip().casefold()
            if not step or status not in {"pending", "in_progress", "completed"}:
                continue
            if status == "in_progress":
                if active_seen:
                    status = "pending"
                active_seen = True
            plan.append({"step": step[:300], "status": status})
        self._plan = plan[:20]
        return json.dumps({"ok": True, "steps": self._plan}, ensure_ascii=False)

    def _tool_open_app(self, arguments):
        wanted = str(arguments.get("name") or "").strip()
        if not wanted:
            return "no app name given"
        shortcuts = start_menu_shortcuts()
        key = normalize_name(wanted)
        match = None
        for label, path in shortcuts.items():
            if normalize_name(label) == key:
                match = (label, path)
                break
        if match is None:
            partial = [(label, path) for label, path in shortcuts.items() if key and key in normalize_name(label)]
            if partial:
                match = sorted(partial, key=lambda item: len(item[0]))[0]
        if match is None:
            close = sorted({label for label in shortcuts if key[:4] and key[:4] in normalize_name(label)})[:8]
            return f"no app named {wanted}. closest: {', '.join(close) or 'nothing similar'}"
        label, path = match
        os.startfile(str(path))
        return f"opened {label}"

    def _tool_open_url(self, arguments):
        url = str(arguments.get("url") or "").strip()
        if not url:
            return "no url given"
        parsed = urllib.parse.urlparse(url if "://" in url else f"https://{url}")
        if parsed.scheme not in ("http", "https"):
            return f"refused to open {parsed.scheme or 'unknown'} url"
        target = parsed.geturl()
        webbrowser.open(target)
        return f"opened {target}"

    def _tool_run_powershell(self, arguments):
        command = str(arguments.get("command") or "").strip()
        if not command:
            return "no command given"
        settings = agent_settings()
        verdict, reason = classify_shell_command(command, settings)
        if verdict == "deny":
            log_event(f"powershell REFUSED ({reason}): {command}")
            return (
                f"refused: this command does {reason}, which the voice agent never runs "
                "unattended. Tell the user to run it themselves if they really want it."
            )
        if verdict == "confirm" and not bool(arguments.get("confirm")):
            log_event(f"powershell needs confirmation: {command}")
            return json.dumps(
                {
                    "state": "needs_confirmation",
                    "ok": False,
                    "command": command,
                    "message": (
                        "This command changes state, so it needs the user's spoken approval. "
                        "Read the command out to them, wait for a yes, then call run_powershell "
                        "again with the identical command and confirm set to true."
                    ),
                },
                ensure_ascii=False,
            )
        log_event(f"powershell: {command}")
        try:
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                text=True,
                timeout=SHELL_TIMEOUT_SECONDS,
                creationflags=CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            return f"command timed out after {SHELL_TIMEOUT_SECONDS}s"
        output = (completed.stdout or "").strip()
        errors = (completed.stderr or "").strip()
        parts = [f"exit code {completed.returncode}"]
        if output:
            parts.append(f"stdout:\n{output}")
        if errors:
            parts.append(f"stderr:\n{errors}")
        # The model receives a bounded copy after execution, but the sidebar
        # keeps the exact local stdout/stderr for inspection.
        return "\n".join(parts)

    def _tool_operator_task(self, arguments):
        task = str(arguments.get("task") or "").strip()
        if not task:
            return "no task given"
        log_event(f"operator task: {task[:160]}")
        sent_at = time.time()
        if send_to_helper("operator", task):
            return self._wait_for_operator(task=task, after_ts=sent_at, require_run_start=True)
        return "could not reach the aiOS window — is it running?"

    def _tool_operator_followup(self, arguments):
        message = str(
            arguments.get("message")
            or arguments.get("task")
            or arguments.get("text")
            or ""
        ).strip()
        if not message:
            return "no follow-up message given"
        options = {}
        if arguments.get("steps") not in (None, ""):
            try:
                options["steps"] = max(1, min(200, int(float(arguments.get("steps")))))
            except (TypeError, ValueError):
                pass
        log_event(f"operator followup: {message[:160]}")
        sent_at = time.time()
        if send_to_helper("operator_followup", message, options or None):
            return self._wait_for_operator(task=message, after_ts=sent_at, require_run_start=False)
        return "could not reach the aiOS window — is it running?"

    @staticmethod
    def _operator_events():
        try:
            lines = OPERATOR_EVENTS_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        events = []
        for line in lines:
            try:
                event = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(event, dict):
                events.append(event)
        return events

    @staticmethod
    def _operator_result(event, task):
        kind = str(event.get("type") or "").strip().lower()
        result = {
            "source": "aiOS OPERATOR",
            "state": "completed" if kind == "done" and bool(event.get("ok")) else kind,
            "task": task,
        }
        if kind == "done":
            if not bool(event.get("ok")):
                result["state"] = "failed"
            result.update({
                "ok": bool(event.get("ok")),
                "verified": bool(event.get("verified")),
                "steps": event.get("steps"),
                "message": str(event.get("message") or "").strip(),
            })
            if event.get("usage"):
                result["usage"] = event.get("usage")
            if event.get("cost") is not None:
                result["cost"] = event.get("cost")
        elif kind in {"ask", "max_steps"}:
            result.update({
                "state": "needs_input",
                "ok": False,
                "question": str(event.get("message") or "What should OPERATOR do next?").strip(),
            })
            if event.get("steps") is not None:
                result["steps"] = event.get("steps")
        else:
            result.update({
                "state": "failed",
                "ok": False,
                "message": str(event.get("message") or event.get("title") or "OPERATOR failed.").strip(),
            })
        return json.dumps(result, ensure_ascii=False, default=str)

    def _wait_for_operator(self, *, task, after_ts, require_run_start):
        """Wait for OPERATOR's mirrored terminal event and return it to the model.

        The turn lock is still held here, so ``run()`` deliberately does not
        block on it — new speech during this window is routed to OPERATOR by
        ``_interject`` instead of queueing behind a job that may run for a very
        long time.
        """
        deadline = time.monotonic() + OPERATOR_WAIT_SECONDS
        run_started = not require_run_start
        run_ts = float(after_ts)
        last_status_at = 0.0
        self._operator_cancel.clear()
        self._operator_active.set()
        self._emit("status", "OPERATOR is working")
        try:
            return self._operator_wait_loop(
                task=task,
                after_ts=after_ts,
                deadline=deadline,
                run_started=run_started,
                run_ts=run_ts,
                last_status_at=last_status_at,
            )
        finally:
            self._operator_active.clear()
            self._operator_cancel.clear()

    def _operator_wait_loop(self, *, task, after_ts, deadline, run_started, run_ts, last_status_at):
        while time.monotonic() < deadline:
            if self._operator_cancel.is_set() or self._cancelled.is_set():
                log_event(f"operator wait cancelled: {task[:120]}")
                return json.dumps(
                    {
                        "source": "aiOS OPERATOR",
                        "state": "cancelled",
                        "ok": False,
                        "task": task,
                        "message": "The user interrupted and asked OPERATOR to stop.",
                    },
                    ensure_ascii=False,
                )
            now = time.monotonic()
            if now - last_status_at >= 8.0:
                self._emit("status", "OPERATOR is working")
                last_status_at = now
            for event in self._operator_events():
                try:
                    event_ts = float(event.get("ts") or 0.0)
                except (TypeError, ValueError):
                    event_ts = 0.0
                if event_ts < float(after_ts):
                    continue
                kind = str(event.get("type") or "").strip().lower()
                if kind == "run_start":
                    event_task = str(event.get("task") or "").strip()
                    if not task or not event_task or event_task == task:
                        run_started = True
                        run_ts = event_ts
                    continue
                if not run_started or event_ts < run_ts:
                    continue
                if kind in {"done", "ask", "max_steps", "error"}:
                    output = self._operator_result(event, task)
                    log_event(f"operator terminal: {output[:500]}")
                    return output
            time.sleep(OPERATOR_POLL_SECONDS)

        status = {}
        try:
            status = json.loads(OPERATOR_STATUS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        output = {
            "source": "aiOS OPERATOR",
            "state": "timeout",
            "ok": False,
            "task": task,
            "message": f"OPERATOR did not return a terminal result within {int(OPERATOR_WAIT_SECONDS)} seconds.",
            "operator_status": status if isinstance(status, dict) else {},
        }
        log_event(f"operator wait timed out: {task[:160]}")
        return json.dumps(output, ensure_ascii=False, default=str)

    def _tool_operator_stop(self, arguments):
        log_event("operator stop")
        if send_to_helper("operator_stop"):
            return "OPERATOR stop requested"
        return "could not reach the aiOS window — is it running?"

    @staticmethod
    def _code_request(path, method="GET", payload=None, timeout=35):
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{CODE_PORT}{path}",
            data=body,
            method=method,
            headers={"Content-Type": "application/json"} if body is not None else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                return json.loads(detail)
            except json.JSONDecodeError:
                return {"ok": False, "error": detail or str(exc)}
        except Exception as exc:
            return {"ok": False, "error": f"CODE backend unavailable: {exc}"}

    @staticmethod
    def _code_attachments(values):
        rows = []
        for value in values or []:
            text = str(value or "").strip()
            if text.startswith(("https://", "http://")):
                rows.append({"url": text, "label": text})
            elif text:
                rows.append({"path": text, "label": Path(text).name})
        return rows

    def _code_job_id(self, arguments):
        return str(arguments.get("job_id") or self._last_code_job_id or "").strip()

    def _tool_code_capabilities(self, arguments):
        suffix = "?refresh=1" if arguments.get("refresh") else ""
        result = self._code_request(f"/api/code/capabilities{suffix}", timeout=50)
        if result.get("ok"):
            compact = []
            for provider in result.get("providers") or []:
                compact.append({
                    "provider": provider.get("provider"),
                    "ready": provider.get("ready"),
                    "message": provider.get("message"),
                    "models": [
                        {
                            "id": model.get("id"),
                            "reasoning": model.get("reasoning") or [],
                            "default_reasoning": model.get("default_reasoning"),
                            "fast_available": bool(model.get("fast")),
                            "default": bool(model.get("default")),
                        }
                        for model in provider.get("models") or []
                    ],
                })
            result = {"ok": True, "providers": compact}
        return json.dumps(result, ensure_ascii=False, default=str)

    def _tool_code_setup(self, arguments):
        provider = str(arguments.get("provider") or "").strip().lower()
        result = self._code_request(
            f"/api/code/providers/{urllib.parse.quote(provider)}/setup",
            "POST",
            {},
        )
        return json.dumps(result, ensure_ascii=False, default=str)

    def _tool_code_start(self, arguments):
        payload = {
            "provider": str(arguments.get("provider") or "").strip().lower(),
            "cwd": str(arguments.get("cwd") or "").strip(),
            "brief": str(arguments.get("brief") or "").strip(),
            "model": str(arguments.get("model") or "").strip(),
            "reasoning": str(arguments.get("reasoning") or "").strip().lower(),
            "fast": bool(arguments.get("fast")),
            "attachments": self._code_attachments(arguments.get("attachments")),
        }
        result = self._code_request("/api/code/jobs", "POST", payload, timeout=45)
        job = result.get("job") or {}
        if result.get("ok") and job.get("id"):
            self._last_code_job_id = str(job["id"])
            result = {
                "ok": True,
                "job_id": job.get("id"),
                "title": job.get("title"),
                "provider": job.get("provider"),
                "status": job.get("status"),
                "model": job.get("model"),
                "reasoning": job.get("reasoning"),
                "fast": job.get("fast"),
                "cwd": job.get("cwd"),
                "message": "The job was dispatched. aiOS will notify at milestones, questions, failure, and the provider's final report.",
            }
        log_event(f"CODE start: {json.dumps(result, ensure_ascii=False, default=str)[:800]}")
        return json.dumps(result, ensure_ascii=False, default=str)

    def _tool_code_list(self, arguments):
        result = self._code_request(f"/api/code/jobs?limit={max(1, min(100, int(arguments.get('limit') or 20)))}")
        if result.get("ok"):
            provider = str(arguments.get("provider") or "").strip().lower()
            status = str(arguments.get("status") or "").strip().lower()
            rows = []
            for job in result.get("jobs") or []:
                if provider and job.get("provider") != provider:
                    continue
                if status and job.get("status") != status:
                    continue
                rows.append({key: job.get(key) for key in ("id", "title", "provider", "project_name", "cwd", "status", "model", "reasoning", "fast", "pending_question", "last_summary", "last_error", "updated_at")})
            result = {"ok": True, "jobs": rows}
            if rows:
                self._last_code_job_id = str(rows[0].get("id") or self._last_code_job_id)
        return json.dumps(result, ensure_ascii=False, default=str)

    def _tool_code_status(self, arguments):
        job_id = self._code_job_id(arguments)
        if not job_id:
            return json.dumps({"ok": False, "error": "No CODE job is selected. Call code_list first."})
        result = self._code_request(f"/api/code/jobs/{urllib.parse.quote(job_id)}/log?since=0")
        if result.get("ok"):
            self._last_code_job_id = job_id
            events = result.get("events") or []
            result = {
                "ok": True,
                "job": result.get("job"),
                "recent_events": [
                    {key: event.get(key) for key in ("ts", "kind", "text", "notify")}
                    for event in events[-16:]
                ],
            }
        return json.dumps(result, ensure_ascii=False, default=str)

    def _tool_code_continue(self, arguments):
        job_id = self._code_job_id(arguments)
        if not job_id:
            return json.dumps({"ok": False, "error": "No CODE job is selected. Call code_list first."})
        payload = {
            "text": str(arguments.get("text") or "").strip(),
            "urgent": bool(arguments.get("urgent")),
            "attachments": self._code_attachments(arguments.get("attachments")),
        }
        result = self._code_request(f"/api/code/jobs/{urllib.parse.quote(job_id)}/messages", "POST", payload)
        if result.get("ok"):
            self._last_code_job_id = job_id
            result = {
                "ok": True,
                "job_id": job_id,
                "steered": bool(result.get("steered")),
                "queued": bool(result.get("queued")),
                "status": (result.get("job") or {}).get("status"),
            }
        return json.dumps(result, ensure_ascii=False, default=str)

    def _tool_code_handoff(self, arguments):
        job_id = self._code_job_id(arguments)
        if not job_id:
            return json.dumps({"ok": False, "error": "No CODE job is selected. Call code_list first."})
        payload = {
            "provider": str(arguments.get("provider") or "").strip().lower(),
            "model": str(arguments.get("model") or "").strip(),
            "reasoning": str(arguments.get("reasoning") or "").strip().lower(),
            "fast": bool(arguments.get("fast")),
            "instruction": str(arguments.get("instruction") or "").strip(),
        }
        result = self._code_request(
            f"/api/code/jobs/{urllib.parse.quote(job_id)}/handoff",
            "POST",
            payload,
            timeout=90,
        )
        if result.get("ok"):
            self._last_code_job_id = job_id
            handoff = result.get("handoff") or {}
            result = {
                "ok": True,
                "job_id": job_id,
                "from_provider": handoff.get("from_provider"),
                "from_model": handoff.get("from_model"),
                "provider": handoff.get("to_provider"),
                "model": handoff.get("to_model"),
                "native_continuation": False,
                "status": (result.get("job") or {}).get("status"),
                "message": "The logical CODE session was handed off. The target provider is continuing in a new native session with the aiOS context manifest.",
            }
        log_event(f"CODE handoff: {json.dumps(result, ensure_ascii=False, default=str)[:800]}")
        return json.dumps(result, ensure_ascii=False, default=str)

    def _tool_code_stop(self, arguments):
        job_id = self._code_job_id(arguments)
        if not job_id:
            return json.dumps({"ok": False, "error": "No CODE job is selected. Call code_list first."})
        result = self._code_request(f"/api/code/jobs/{urllib.parse.quote(job_id)}/stop", "POST", {})
        return json.dumps(result, ensure_ascii=False, default=str)

    def _tool_code_delete(self, arguments):
        job_id = self._code_job_id(arguments)
        if not job_id:
            return json.dumps({"ok": False, "error": "No CODE job is selected. Call code_list first."})
        if arguments.get("confirm") is not True:
            return json.dumps({
                "ok": False,
                "error": "Deletion was not confirmed. Ask the user to explicitly confirm deleting this CODE session.",
                "needs_confirmation": True,
            })
        result = self._code_request(
            f"/api/code/jobs/{urllib.parse.quote(job_id)}",
            "DELETE",
            {"confirm": job_id},
        )
        if result.get("ok") and job_id == self._last_code_job_id:
            self._last_code_job_id = ""
        return json.dumps(result, ensure_ascii=False, default=str)

    def _tool_type_text(self, arguments):
        text = str(arguments.get("text") or "")
        if not text:
            return "no text given"
        if self._type_text is None:
            return "typing is unavailable"
        self._type_text(text)
        return f"typed {len(text)} characters"

    def _tool_copy_text(self, arguments):
        text = str(arguments.get("text") or "")
        if not text:
            return "no text given"
        if self._copy_text is None:
            return "clipboard is unavailable"
        self._copy_text(text)
        return f"copied {len(text)} characters"

    def _tool_add_note(self, arguments):
        note = str(arguments.get("note") or "").strip()
        if not note:
            return "no note given"
        config = load_config()
        dashboard = config.setdefault("dashboard", {})
        existing = str(dashboard.get("notes") or "")
        dashboard["notes"] = (existing.rstrip() + "\n" + note).strip() if existing.strip() else note
        try:
            with CONFIG_PATH.open("w", encoding="utf-8") as file:
                json.dump(config, file, indent=2)
        except OSError as exc:
            return f"could not write notes: {exc}"
        return "added to the dashboard notes"

    # ------------------------------------------------------- reading the machine

    def _tool_read_clipboard(self, _arguments):
        try:
            import pyperclip

            text = str(pyperclip.paste() or "")
        except Exception as exc:
            return f"could not read the clipboard: {exc}"
        if not text.strip():
            return "the clipboard is empty"
        clipped = text[:TOOL_OUTPUT_LIMIT]
        suffix = "" if len(text) <= TOOL_OUTPUT_LIMIT else f"\n…({len(text)} chars total)"
        return f"clipboard ({len(text)} chars):\n{clipped}{suffix}"

    def _tool_read_screen(self, arguments):
        """Capture the screen and hand the picture to the model running this turn.

        The image is attached to the next request instead of being described by
        a second model: you are the one who should look at it, so a relayed
        description would only lose detail.
        """
        question = str(arguments.get("question") or "").strip()
        selector = str(arguments.get("screen") or "all").strip().casefold()
        try:
            monitors = desktop_monitors()
            if selector != "all":
                monitor, error = resolve_desktop_monitor(selector)
                if error:
                    return error
                monitors = [monitor]
        except Exception as exc:
            return f"could not enumerate screens: {exc}"
        if not monitors:
            return "could not capture the screen: no physical screens found"
        if not hasattr(self, "_screen_views"):
            self._screen_views = {}
        attached = []
        for monitor in monitors:
            try:
                image_bytes, size = capture_screen_jpeg(screen=str(monitor.index))
            except Exception as exc:
                return f"could not capture screen {monitor.index}: {exc}"
            encoded = base64.b64encode(image_bytes).decode("ascii")
            note = (
                f"screen {monitor.index}, attached image {size[0]}x{size[1]}, "
                f"physical screen {monitor.width}x{monitor.height}; {question or 'inspect this screen'}"
            )
            self._pending_images.append(
                {"url": f"data:image/jpeg;base64,{encoded}", "note": note}
            )
            self._screen_views[int(monitor.index)] = {
                "image_width": int(size[0]),
                "image_height": int(size[1]),
                "captured_at": time.time(),
            }
            attached.append(f"screen {monitor.index} as {size[0]}x{size[1]}")
            log_event(
                f"read_screen {monitor.index} {size[0]}x{size[1]} "
                f"({len(image_bytes) // 1024} KB) q={question[:80]}"
            )
        return (
            f"attached {', '.join(attached)}. Look at those images directly. "
            "For a quick click, use pixel coordinates from the matching attached image."
        )

    def _screen_point(self, arguments):
        monitor, error = resolve_desktop_monitor(arguments.get("screen"))
        if error:
            return None, None, None, error
        try:
            x = int(arguments.get("x"))
            y = int(arguments.get("y"))
        except (TypeError, ValueError):
            return None, None, None, "x and y must be image pixel coordinates"
        view = getattr(self, "_screen_views", {}).get(int(monitor.index), {})
        image_width = int(view.get("image_width") or monitor.width)
        image_height = int(view.get("image_height") or monitor.height)
        if not (0 <= x < image_width and 0 <= y < image_height):
            return None, None, None, (
                f"point ({x},{y}) is outside the last screen {monitor.index} image "
                f"({image_width}x{image_height}); read_screen again"
            )
        local_x = min(int(monitor.width) - 1, round(x * int(monitor.width) / image_width))
        local_y = min(int(monitor.height) - 1, round(y * int(monitor.height) / image_height))
        return monitor, local_x, local_y, ""

    def _tool_click_screen(self, arguments):
        monitor, local_x, local_y, error = self._screen_point(arguments)
        if error:
            return error
        button = str(arguments.get("button") or "left").strip().lower()
        if button not in {"left", "right"}:
            return "button must be left or right"
        try:
            clicks = int(arguments.get("clicks") or 1)
        except (TypeError, ValueError):
            clicks = 1
        if clicks not in {1, 2}:
            return "clicks must be 1 or 2"
        try:
            from agent_clicker.desktop_agent.actions import execute

            result = execute(
                {"type": "click", "x": local_x, "y": local_y,
                 "button": button, "clicks": clicks, "duration": 0.12},
                monitor,
                cancel_event=self._cancelled,
            )
        except Exception as exc:
            return f"could not click screen {monitor.index}: {exc}"
        if not result.ok:
            return f"click failed on screen {monitor.index}: {result.detail}"
        return (
            f"clicked screen {monitor.index} at attached-image ({arguments.get('x')},"
            f"{arguments.get('y')}); physical local ({local_x},{local_y}); {result.detail}"
        )

    def _tool_scroll_screen(self, arguments):
        monitor, local_x, local_y, error = self._screen_point(arguments)
        if error:
            return error
        try:
            amount = max(-12, min(12, int(arguments.get("amount") or 0)))
        except (TypeError, ValueError):
            return "amount must be an integer from -12 to 12"
        if amount == 0:
            return "amount must not be zero"
        try:
            from agent_clicker.desktop_agent.actions import execute

            result = execute(
                {"type": "scroll", "x": local_x, "y": local_y, "dy": amount},
                monitor,
                cancel_event=self._cancelled,
            )
        except Exception as exc:
            return f"could not scroll screen {monitor.index}: {exc}"
        if not result.ok:
            return f"scroll failed on screen {monitor.index}: {result.detail}"
        return f"scrolled screen {monitor.index} by {amount} at ({local_x},{local_y}); {result.detail}"

    # ----------------------------------------------------------------- the disk

    def _file_roots(self):
        return resolve_file_roots(agent_settings())

    def _tool_read_file(self, arguments):
        path, error = resolve_inside_roots(arguments.get("path"), self._file_roots())
        if error:
            return error
        if not path.exists():
            return f"no such file: {path}"
        if path.is_dir():
            return f"{path} is a folder — use list_files"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"could not read {path.name}: {exc}"
        clipped = text[:FILE_READ_LIMIT]
        suffix = "" if len(text) <= FILE_READ_LIMIT else f"\n…(truncated, {len(text)} chars total)"
        return f"{path} ({len(text)} chars):\n{clipped}{suffix}"

    def _tool_write_file(self, arguments):
        path, error = resolve_inside_roots(arguments.get("path"), self._file_roots())
        if error:
            return error
        content = str(arguments.get("content") or "")
        existed = path.exists()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            return f"could not write {path.name}: {exc}"
        log_event(f"wrote {len(content)} chars to {path}")
        return f"{'overwrote' if existed else 'created'} {path} ({len(content)} chars)"

    def _tool_append_file(self, arguments):
        path, error = resolve_inside_roots(arguments.get("path"), self._file_roots())
        if error:
            return error
        content = str(arguments.get("content") or "")
        if not content:
            return "nothing to append"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
            separator = "" if not existing or existing.endswith("\n") else "\n"
            with path.open("a", encoding="utf-8") as file:
                file.write(separator + content + ("" if content.endswith("\n") else "\n"))
        except OSError as exc:
            return f"could not append to {path.name}: {exc}"
        return f"appended {len(content)} chars to {path}"

    def _tool_list_files(self, arguments):
        path, error = resolve_inside_roots(arguments.get("path"), self._file_roots())
        if error:
            return error
        if not path.exists():
            return f"no such folder: {path}"
        if not path.is_dir():
            return f"{path} is a file, not a folder"
        pattern = str(arguments.get("pattern") or "*").strip() or "*"
        try:
            entries = sorted(path.glob(pattern), key=lambda item: (item.is_file(), item.name.lower()))
        except (OSError, ValueError) as exc:
            return f"could not list {path}: {exc}"
        if not entries:
            return f"{path} has nothing matching {pattern}"
        lines = []
        for entry in entries[:120]:
            try:
                size = f"{entry.stat().st_size:,} B" if entry.is_file() else "folder"
            except OSError:
                size = "?"
            lines.append(f"{entry.name}\t{size}")
        more = "" if len(entries) <= 120 else f"\n…and {len(entries) - 120} more"
        return f"{path} ({len(entries)} entries):\n" + "\n".join(lines) + more

    # ------------------------------------------------------------------- audio

    @staticmethod
    def _audio_endpoint():
        from ctypes import cast, POINTER

        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        return cast(interface, POINTER(IAudioEndpointVolume))

    def _tool_set_volume(self, arguments):
        try:
            volume = self._audio_endpoint()
        except Exception as exc:
            return f"volume control unavailable: {exc}"
        raw = arguments.get("percent")
        if raw in (None, ""):
            try:
                current = round(volume.GetMasterVolumeLevelScalar() * 100)
                muted = bool(volume.GetMute())
            except Exception as exc:
                return f"could not read the volume: {exc}"
            return f"volume is {current}%" + (" (muted)" if muted else "")
        try:
            percent = max(0, min(100, int(float(raw))))
        except (TypeError, ValueError):
            return f"'{raw}' is not a percentage"
        try:
            volume.SetMasterVolumeLevelScalar(percent / 100.0, None)
            if percent > 0 and volume.GetMute():
                volume.SetMute(0, None)
        except Exception as exc:
            return f"could not set the volume: {exc}"
        return f"volume set to {percent}%"

    def _tool_media_control(self, arguments):
        action = str(arguments.get("action") or "").strip().lower()
        if action in {"mute", "unmute"}:
            try:
                volume = self._audio_endpoint()
                volume.SetMute(1 if action == "mute" else 0, None)
            except Exception as exc:
                return f"could not {action}: {exc}"
            return f"{action}d" if action == "mute" else "unmuted"
        keys = {
            "playpause": "play/pause media",
            "next": "next track",
            "previous": "previous track",
            "stop": "stop media",
        }
        if action not in keys:
            return f"unknown media action {action!r}"
        try:
            import keyboard

            keyboard.send(keys[action])
        except Exception as exc:
            return f"could not send the media key: {exc}"
        return f"sent {action}"

    # ------------------------------------------------------------------ timers

    def _arm_timer(self, timer_id, label, due_at):
        """Schedule one reminder. Fires immediately if it is already overdue."""
        delay = max(0.0, due_at - time.time())

        def fire():
            self._timers.pop(timer_id, None)
            self._save_timers()
            log_event(f"timer {timer_id} fired: {label[:80]}")
            if self._speak is not None:
                try:
                    self._speak(label)
                except Exception as exc:
                    log_event(f"timer speech failed: {exc}")
            send_to_helper("voice_event", label, {"kind": "timer", "timer_id": timer_id})

        timer = threading.Timer(delay, fire)
        timer.daemon = True
        self._timers[timer_id] = {"timer": timer, "label": label, "due_at": due_at}
        timer.start()

    def _save_timers(self):
        """Timers live on disk: a reminder must survive a restart of aiOS."""
        payload = [
            {"id": timer_id, "label": entry["label"], "due_at": entry["due_at"]}
            for timer_id, entry in self._timers.items()
        ]
        try:
            TIMERS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            log_event(f"could not persist timers: {exc}")

    def _restore_timers(self):
        try:
            data = json.loads(TIMERS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, list):
            return
        restored = 0
        for entry in data:
            if not isinstance(entry, dict):
                continue
            label = str(entry.get("label") or "").strip()
            try:
                due_at = float(entry.get("due_at") or 0)
            except (TypeError, ValueError):
                continue
            if not label or not due_at:
                continue
            # More than an hour late means the machine was off; saying it now
            # would be confusing rather than useful.
            if time.time() - due_at > 3600:
                continue
            timer_id = str(entry.get("id") or f"t{restored + 1}")
            number = "".join(ch for ch in timer_id if ch.isdigit())
            if number:
                self._timer_seq = max(self._timer_seq, int(number))
            self._arm_timer(timer_id, label, due_at)
            restored += 1
        if restored:
            log_event(f"restored {restored} pending timer(s)")
        self._save_timers()

    def _tool_set_timer(self, arguments):
        try:
            seconds = int(float(arguments.get("seconds")))
        except (TypeError, ValueError):
            return "no valid delay given"
        if not 1 <= seconds <= 86400:
            return "delay must be between 1 second and 24 hours"
        label = str(arguments.get("label") or "").strip() or "Your timer is up."
        self._timer_seq += 1
        timer_id = f"t{self._timer_seq}"
        due_at = time.time() + seconds
        self._arm_timer(timer_id, label, due_at)
        self._save_timers()
        when = time.strftime("%H:%M", time.localtime(due_at))
        return f"timer {timer_id} set for {seconds}s (fires at {when}): {label}"

    def _tool_list_timers(self, _arguments):
        if not self._timers:
            return "no timers pending"
        now = time.time()
        rows = []
        for timer_id, entry in sorted(self._timers.items(), key=lambda item: item[1]["due_at"]):
            remaining = max(0, int(entry["due_at"] - now))
            rows.append(f"{timer_id}: {entry['label']} (in {remaining}s)")
        return "\n".join(rows)

    def _tool_cancel_timer(self, arguments):
        wanted = str(arguments.get("timer_id") or "").strip().lower()
        if not wanted:
            return "no timer id given"
        if wanted == "all":
            count = len(self._timers)
            for entry in list(self._timers.values()):
                entry["timer"].cancel()
            self._timers.clear()
            self._save_timers()
            return f"cancelled {count} timer(s)"
        entry = self._timers.pop(wanted, None)
        if entry is None:
            return f"no pending timer called {wanted}"
        entry["timer"].cancel()
        self._save_timers()
        return f"cancelled {wanted}: {entry['label']}"

    # ----------------------------------------------------------------- windows

    def _tool_list_windows(self, _arguments):
        try:
            windows = list_open_windows()
        except Exception as exc:
            return f"could not list windows: {exc}"
        if not windows:
            return "no visible windows found"
        rows = []
        for entry in windows:
            process = entry.get("process") or ""
            rows.append(f"{entry['title']}" + (f"  [{process}]" if process else ""))
        return "\n".join(rows)

    @staticmethod
    def _find_window(title):
        wanted = normalize_name(title)
        if not wanted:
            return None, "no window title given"
        try:
            windows = list_open_windows(limit=200)
        except Exception as exc:
            return None, f"could not list windows: {exc}"
        exact = [item for item in windows if normalize_name(item["title"]) == wanted]
        partial = [item for item in windows if wanted in normalize_name(item["title"])]
        matches = exact or partial
        if not matches:
            titles = ", ".join(item["title"][:40] for item in windows[:8])
            return None, f"no window matching {title!r}. open windows: {titles}"
        return sorted(matches, key=lambda item: len(item["title"]))[0], ""

    def _tool_focus_window(self, arguments):
        match, error = self._find_window(arguments.get("title"))
        if error:
            return error
        import ctypes

        user32 = ctypes.windll.user32
        handle = match["handle"]
        try:
            if user32.IsIconic(handle):
                user32.ShowWindow(handle, 9)  # SW_RESTORE
            user32.SetForegroundWindow(handle)
        except Exception as exc:
            return f"could not focus {match['title']}: {exc}"
        return f"focused {match['title']}"

    def _tool_close_window(self, arguments):
        match, error = self._find_window(arguments.get("title"))
        if error:
            return error
        import ctypes

        try:
            # WM_CLOSE, so the app still gets to prompt about unsaved work.
            ctypes.windll.user32.PostMessageW(match["handle"], 0x0010, 0, 0)
        except Exception as exc:
            return f"could not close {match['title']}: {exc}"
        return f"asked {match['title']} to close"

    # -------------------------------------------------------- finding and system

    def _tool_search_files(self, arguments):
        roots = self._file_roots()
        raw_path = arguments.get("path")
        if raw_path:
            base, error = resolve_inside_roots(raw_path, roots)
            if error:
                return error
        else:
            base = roots[0]
        if not base.is_dir():
            return f"{base} is not a folder"
        query = str(arguments.get("query") or "").strip()
        if not query:
            return "no search query given"
        pattern = query if any(ch in query for ch in "*?") else f"*{query}*"
        contains = str(arguments.get("contains") or "").strip()
        hits = []
        try:
            for item in base.rglob(pattern):
                if not item.is_file():
                    continue
                if contains:
                    try:
                        if contains.casefold() not in item.read_text(
                            encoding="utf-8", errors="ignore"
                        ).casefold():
                            continue
                    except OSError:
                        continue
                hits.append(item)
                if len(hits) >= 60:
                    break
        except (OSError, ValueError) as exc:
            return f"search failed: {exc}"
        if not hits:
            where = f" containing {contains!r}" if contains else ""
            return f"nothing matching {query!r}{where} under {base}"
        return f"{len(hits)} match(es) under {base}:\n" + "\n".join(str(item) for item in hits)

    def _tool_open_path(self, arguments):
        path, error = resolve_inside_roots(arguments.get("path"), self._file_roots())
        if error:
            return error
        if not path.exists():
            return f"no such path: {path}"
        try:
            if path.is_dir():
                os.startfile(str(path))
            else:
                # Reveal the file with it selected, which is what "show me" means.
                subprocess.Popen(["explorer", "/select,", str(path)])
        except Exception as exc:
            return f"could not open {path}: {exc}"
        return f"opened {path}"

    def _tool_system_status(self, _arguments):
        try:
            import psutil
        except ImportError:
            return "psutil is not installed"
        try:
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage(str(BASE_DIR.anchor or "C:\\"))
            uptime = time.time() - psutil.boot_time()
            parts = [
                f"CPU {psutil.cpu_percent(interval=0.3):.0f}%",
                f"RAM {memory.percent:.0f}% ({memory.used / 1e9:.1f} of {memory.total / 1e9:.1f} GB)",
                f"disk {disk.percent:.0f}% used, {disk.free / 1e9:.0f} GB free",
                f"up {uptime / 3600:.1f} h",
            ]
            battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
            if battery is not None:
                plugged = "plugged in" if battery.power_plugged else "on battery"
                parts.append(f"battery {battery.percent:.0f}% ({plugged})")
        except Exception as exc:
            return f"could not read system status: {exc}"
        return ", ".join(parts)

    def _tool_list_processes(self, arguments):
        try:
            import psutil
        except ImportError:
            return "psutil is not installed"
        sort = str(arguments.get("sort") or "memory").strip().lower()
        try:
            limit = max(1, min(40, int(arguments.get("limit") or 12)))
        except (TypeError, ValueError):
            limit = 12
        rows = []
        for process in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent"]):
            try:
                info = process.info
                rows.append(
                    {
                        "pid": info["pid"],
                        "name": info["name"] or "?",
                        "memory": (info["memory_info"].rss if info["memory_info"] else 0),
                        "cpu": info["cpu_percent"] or 0.0,
                    }
                )
            except Exception:
                continue
        rows.sort(key=lambda row: row["cpu"] if sort == "cpu" else row["memory"], reverse=True)
        lines = [
            f"{row['name']} (pid {row['pid']}) — {row['memory'] / 1e6:.0f} MB, {row['cpu']:.0f}% CPU"
            for row in rows[:limit]
        ]
        return f"top {len(lines)} by {sort}:\n" + "\n".join(lines)

    def _tool_kill_process(self, arguments):
        try:
            import psutil
        except ImportError:
            return "psutil is not installed"
        pid = arguments.get("pid")
        name = str(arguments.get("name") or "").strip()
        targets = []
        if pid not in (None, ""):
            try:
                targets.append(psutil.Process(int(pid)))
            except Exception as exc:
                return f"no process with pid {pid}: {exc}"
        elif name:
            wanted = normalize_name(name)
            for process in psutil.process_iter(["pid", "name"]):
                try:
                    if wanted and wanted in normalize_name(process.info["name"] or ""):
                        targets.append(process)
                except Exception:
                    continue
        else:
            return "give a process name or pid"
        if not targets:
            return f"nothing running called {name!r}"
        # Never take out the agent's own host process.
        targets = [item for item in targets if item.pid != os.getpid()]
        if not targets:
            return "refused: that is the process the agent itself runs in"
        ended = []
        for process in targets[:10]:
            try:
                label = f"{process.name()} (pid {process.pid})"
                process.terminate()
                ended.append(label)
            except Exception as exc:
                ended.append(f"could not end pid {process.pid}: {exc}")
        psutil.wait_procs(targets[:10], timeout=3)
        return "ended " + ", ".join(ended)

    def _tool_read_url(self, arguments):
        url = str(arguments.get("url") or "").strip()
        if not url:
            return "no url given"
        parsed = urllib.parse.urlparse(url if "://" in url else f"https://{url}")
        if parsed.scheme not in ("http", "https"):
            return f"refused to fetch {parsed.scheme or 'unknown'} url"
        try:
            import httpx

            response = httpx.get(
                parsed.geturl(),
                timeout=20,
                follow_redirects=True,
                headers={"User-Agent": "aiOS-voice-agent/1.0"},
            )
            response.raise_for_status()
            body = response.text
        except Exception as exc:
            return f"could not fetch {parsed.geturl()}: {exc}"
        # Crude but dependency-free: drop script/style, strip tags, collapse space.
        text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", body)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = re.sub(r"&nbsp;?", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        clipped = text[:TOOL_OUTPUT_LIMIT]
        suffix = "" if len(text) <= TOOL_OUTPUT_LIMIT else f"\n…({len(text)} chars total)"
        return f"{parsed.geturl()}:\n{clipped}{suffix}"

    def _tool_notify(self, arguments):
        message = str(arguments.get("message") or "").strip()
        if not message:
            return "nothing to show"
        if send_to_helper("voice_event", message, {"kind": "notice"}):
            return "shown on the aiOS window"
        return "could not reach the aiOS window"

    # --------------------------------------------------------------- own files

    def _tool_list_self_files(self, _arguments):
        self._ensure_self_dir()
        try:
            entries = sorted(SELF_DIR.glob("*"))
        except OSError as exc:
            return f"could not list {SELF_DIR}: {exc}"
        if not entries:
            return "you have no files yet"
        rows = []
        for entry in entries:
            try:
                rows.append(f"{entry.name} — {entry.stat().st_size:,} bytes")
            except OSError:
                rows.append(entry.name)
        return "\n".join(rows)

    def _tool_read_self_file(self, arguments):
        path, error = self._self_file(arguments.get("name"))
        if error:
            return error
        if not path.exists():
            return f"you have no file called {path.name}"
        text = self.read_self_file(path)
        clipped = text[:SELF_FILE_LIMIT]
        suffix = "" if len(text) <= SELF_FILE_LIMIT else f"\n…({len(text)} chars total)"
        return f"{path.name}:\n{clipped}{suffix}"

    def _tool_write_self_file(self, arguments):
        path, error = self._self_file(arguments.get("name"))
        if error:
            return error
        content = str(arguments.get("content") or "")
        if not content.strip():
            return "refused: that would leave the file empty"
        self._ensure_self_dir()
        existed = path.exists()
        try:
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            return f"could not write {path.name}: {exc}"
        log_event(f"self-edit: {'rewrote' if existed else 'created'} {path.name} ({len(content)} chars)")
        if path == SELF_MEMORY_PATH:
            self._facts = self._load_facts()
        return f"{'updated' if existed else 'created'} {path.name}"

    def _tool_append_self_file(self, arguments):
        path, error = self._self_file(arguments.get("name"))
        if error:
            return error
        content = str(arguments.get("content") or "").strip()
        if not content:
            return "nothing to append"
        self._ensure_self_dir()
        try:
            existing = self.read_self_file(path)
            separator = "" if not existing or existing.endswith("\n") else "\n"
            with path.open("a", encoding="utf-8") as file:
                file.write(separator + content + "\n")
        except OSError as exc:
            return f"could not append to {path.name}: {exc}"
        log_event(f"self-edit: appended {len(content)} chars to {path.name}")
        if path == SELF_MEMORY_PATH:
            self._facts = self._load_facts()
        return f"added to {path.name}"

    # ------------------------------------------------------------------- facts

    def _tool_remember(self, arguments):
        fact = str(arguments.get("fact") or "").strip()
        if not fact:
            return "nothing to remember"
        if any(fact.casefold() == existing.casefold() for existing in self._facts):
            return "already remembered that"
        self._facts.append(fact)
        del self._facts[:-MAX_FACTS]
        self._save_facts()
        log_event(f"remembered: {fact[:120]}")
        return f"remembered: {fact}"

    def _tool_forget(self, arguments):
        match = str(arguments.get("match") or "").strip()
        if not match:
            return "nothing to forget"
        if match.casefold() == "all":
            count = len(self._facts)
            self._facts = []
            self._save_facts()
            return f"forgot all {count} stored fact(s)"
        needle = match.casefold()
        kept = [fact for fact in self._facts if needle not in fact.casefold()]
        removed = len(self._facts) - len(kept)
        if not removed:
            return f"nothing stored matches {match!r}"
        self._facts = kept
        self._save_facts()
        return f"forgot {removed} stored fact(s)"

    def _tool_hide_overlay(self, _arguments):
        if self._hide_overlay is None:
            return "overlay hide is unavailable"
        self._hide_requested = True
        log_event("agent requested overlay hide after sign-off")
        return "overlay will hide after your short spoken sign-off; conversation memory is preserved"

    def _finish_overlay_hide(self, reply):
        if not self._hide_requested or self._hide_overlay is None:
            return
        self._hide_requested = False
        try:
            self._hide_overlay(reply)
        except TypeError:
            # Compatibility for embedders using the original no-argument hook.
            self._hide_overlay()

    # --------------------------------------------------------------------- run

    # Words that mean "stop what you are doing", in both languages the user
    # speaks. Matched locally because reaching the model would mean queueing
    # behind the very turn we are trying to interrupt.
    STOP_WORDS = (
        "stop", "cancel", "abort", "kill", "quit", "halt", "nevermind", "never mind",
        "forget it", "stoppa", "sluta", "avbryt", "lagg av", "glom det",
    )

    @classmethod
    def _is_stop_request(cls, transcript):
        folded = "".join(
            char if char.isalnum() or char.isspace() else " " for char in str(transcript).casefold()
        )
        folded = " ".join(folded.split())
        if not folded:
            return False
        # Only a short utterance counts — "don't stop until it's done" is not a
        # stop request, and neither is a paragraph that happens to contain it.
        if len(folded.split()) > 6:
            return False
        return any(word in folded for word in cls.STOP_WORDS)

    def run(self, transcript, overrides=None):
        transcript = str(transcript or "").strip()
        if not transcript:
            return AgentResult(error="nothing to send")
        # A turn parked on OPERATOR can hold the lock for half an hour. Rather
        # than queue behind it — which used to make "stop the operator"
        # impossible to say — take the wait as an interjection.
        if not self._lock.acquire(timeout=TURN_ACQUIRE_SECONDS):
            return self._interject(transcript)
        try:
            self._cancelled.clear()
            return self._run_locked(transcript, overrides=overrides)
        finally:
            self._lock.release()

    def _interject(self, transcript):
        """Handle speech that arrives while a previous turn is still running."""
        started = time.monotonic()
        stop_requested = self._is_stop_request(transcript)
        if self._operator_active.is_set():
            if stop_requested:
                log_event(f"interject stop -> operator_stop: {transcript[:120]}")
                self._operator_cancel.set()
                send_to_helper("operator_stop")
                reply = "Stopping OPERATOR."
            else:
                log_event(f"interject followup -> operator: {transcript[:120]}")
                send_to_helper("operator_followup", transcript)
                reply = "Passed that to OPERATOR."
            self._emit("reply_start", "")
            self._emit("reply_delta", reply)
            self._emit("reply_done", reply)
            return AgentResult(
                reply=reply,
                tools=["operator_stop" if stop_requested else "operator_followup"],
                elapsed=time.monotonic() - started,
            )
        if stop_requested:
            log_event(f"interject stop -> cancel turn: {transcript[:120]}")
            self.cancel()
            reply = "Stopped."
            self._emit("reply_start", "")
            self._emit("reply_delta", reply)
            self._emit("reply_done", reply)
            return AgentResult(reply=reply, cancelled=True, elapsed=time.monotonic() - started)
        log_event(f"interject dropped, turn still running: {transcript[:120]}")
        return AgentResult(
            error="still working on the previous turn — say 'stop' to cancel it",
            elapsed=time.monotonic() - started,
        )

    @staticmethod
    def _is_transient(exc):
        """Worth one quiet retry: a spoken turn should not die on a blip."""
        name = type(exc).__name__.casefold()
        if any(token in name for token in ("connection", "timeout", "apistatus", "internalserver")):
            return True
        status = getattr(exc, "status_code", None)
        if isinstance(status, int) and (status == 429 or status >= 500):
            return True
        message = str(exc).casefold()
        return any(
            token in message
            for token in ("connection error", "timed out", "temporarily unavailable", "502", "503", "504")
        )

    def _call_with_retry(self, client, request_options):
        """One model round, retrying transient failures with a short backoff."""
        last_error = None
        for attempt in range(API_RETRIES + 1):
            if self._cancelled.is_set():
                raise CancelledTurn()
            try:
                return self._stream_response(client, request_options)
            except CancelledTurn:
                raise
            except Exception as exc:
                last_error = exc
                if attempt >= API_RETRIES or not self._is_transient(exc):
                    raise
                delay = API_RETRY_BACKOFF * (attempt + 1)
                log_event(f"transient API failure ({exc}); retry {attempt + 1} in {delay:.1f}s")
                self._emit("status", "reconnecting")
                time.sleep(delay)
        raise last_error if last_error else RuntimeError("model call failed")

    def _stream_response(self, client, request_options):
        """Create one Responses API turn while forwarding output-text deltas."""
        responses = client.responses
        if not hasattr(responses, "stream"):
            return responses.create(**request_options), "", False
        chunks = []
        pending = []
        pending_chars = 0
        last_emit_at = time.monotonic()
        started = False
        with responses.stream(**request_options) as stream:
            for event in stream:
                if self._cancelled.is_set():
                    # Drop the socket rather than narrating a reply the user
                    # has already talked over.
                    try:
                        stream.close()
                    except Exception:
                        pass
                    raise CancelledTurn()
                if getattr(event, "type", "") != "response.output_text.delta":
                    continue
                delta = str(getattr(event, "delta", "") or "")
                if not delta:
                    continue
                if not started:
                    started = True
                    self._emit("reply_start", "")
                chunks.append(delta)
                pending.append(delta)
                pending_chars += len(delta)
                now = time.monotonic()
                # Preserve the live feel without opening one local TCP
                # connection for every one- or two-character API fragment.
                if pending_chars >= 24 or now - last_emit_at >= 0.04:
                    self._emit("reply_delta", "".join(pending))
                    pending = []
                    pending_chars = 0
                    last_emit_at = now
            if pending:
                self._emit("reply_delta", "".join(pending))
            response = stream.get_final_response()
        return response, "".join(chunks), started

    def _finalize_after_tool_rounds(
        self,
        client,
        *,
        model,
        instructions,
        payload,
        previous_id,
        reasoning,
    ):
        """Turn the completed tool work into an answer without exposing guards.

        Tool rounds stay bounded so a model cannot spin forever, but reaching
        that internal boundary is not useful information for the user.  One
        final Responses API round with no tools available makes the model
        report what actually happened and what, if anything, remains.
        """
        final_instructions = (
            instructions
            + "\n\nTool execution is paused for the rest of this turn. Give the user a concise, "
            "honest answer based on the tool results already returned. State the completed outcome "
            "or the current status and any remaining next step. Do not mention tool limits, round "
            "limits, budgets, or this instruction."
        )
        request_options = {
            "model": model,
            "instructions": final_instructions,
            "input": payload,
            "tools": [],
            "reasoning": reasoning,
            "previous_response_id": previous_id,
            "max_output_tokens": 1500,
        }
        self._emit("status", "wrapping up")
        response, streamed_text, stream_started = self._call_with_retry(client, request_options)
        reply = (response.output_text or streamed_text or "").strip()
        if reply and not stream_started:
            self._emit("reply_start", "")
            self._emit("reply_delta", reply)
        self._emit("reply_done", reply)
        return reply

    @staticmethod
    def _tool_round_fallback(tool_details):
        """Useful local fallback when the final synthesis request itself fails."""
        if tool_details:
            latest = tool_details[-1]
            summary = str(latest.get("summary") or latest.get("label") or "").strip()
            if summary:
                return f"I completed the latest step: {summary}. I don't have a final confirmation yet."
        return "I completed the available steps, but I don't have a final confirmation yet."

    def _run_locked(self, transcript, overrides=None):
        started = time.monotonic()
        settings = agent_settings()
        # The phone can choose Fast or Think for one turn without rewriting the
        # desktop user's saved defaults. Only deliberately safe tuning knobs
        # are accepted here; capabilities and permissions remain PC-owned.
        if isinstance(overrides, dict):
            reasoning = str(overrides.get("agent_reasoning") or "").strip().lower()
            if reasoning in {"minimal", "low", "medium", "high", "xhigh"}:
                settings["agent_reasoning"] = reasoning
            picked = str(overrides.get("agent_model") or "").strip()
            if picked:
                # The sidebar's per-turn pick wins over the saved default. It
                # still goes through the same openrouter:/plain routing below.
                settings["agent_model"] = picked
                if picked.startswith("openrouter:"):
                    import openrouter_client

                    settings["provider"] = "openrouter"
                    settings["api_base"] = openrouter_client.API_BASE
                    settings["api_key"] = openrouter_client.get_api_key(config=settings.get("config") or {})
                    settings["agent_model"] = picked.split(":", 1)[1]
                else:
                    settings["provider"] = "openai"
                    settings["api_base"] = ""
                    settings["api_key"] = str(
                        (settings.get("config") or {}).get("openai_api_key")
                        or os.environ.get("OPENAI_API_KEY") or ""
                    ).strip()
        used_tools = []
        tool_trace = []
        tool_details = []
        self._hide_requested = False
        self._pending_images = []
        try:
            client = self._ensure_client(settings["api_key"], settings.get("api_base", ""))
        except Exception as exc:
            log_event(f"client unavailable: {exc}")
            return AgentResult(error=str(exc))

        self._expire_idle(settings)
        self.turns.append({"role": "user", "text": transcript, "at": time.time()})

        instructions = self._instructions(settings)
        tools = self._tools(settings)
        reasoning = {"effort": str(settings.get("agent_reasoning") or "low")}
        model = str(settings.get("agent_model") or DEFAULT_AGENT_SETTINGS["agent_model"])
        # The whole conversation goes up every turn — the model sees what was
        # said, what it ran and what came back, not just the latest sentence.
        payload = self._conversation_input(settings)
        previous_id = None
        self._emit("status", "thinking")
        log_event(f"turn ({len(self.turns)} in memory): {transcript[:200]}")

        try:
            for _round in range(max(1, int(settings.get("agent_max_rounds", 6)))):
                if self._cancelled.is_set():
                    raise CancelledTurn()
                self._emit("status", "thinking")
                request_options = {
                    "model": model,
                    "instructions": instructions,
                    "input": payload,
                    "tools": tools,
                    "reasoning": reasoning,
                    "previous_response_id": previous_id,
                    "max_output_tokens": 1500,
                }
                if settings.get("agent_web_search") and settings.get("provider") != "openrouter":
                    # Include the actual searches, sources and result records so
                    # the sidebar can inspect the web call in full.
                    request_options["include"] = [
                        "web_search_call.action.sources",
                        "web_search_call.results",
                    ]
                response, streamed_text, stream_started = self._call_with_retry(client, request_options)
                # Within a turn the tool rounds chain server-side, so the model
                # keeps its own reasoning between calls.
                previous_id = response.id
                calls = [item for item in response.output if getattr(item, "type", "") == "function_call"]
                for item in response.output:
                    if getattr(item, "type", "") == "web_search_call":
                        detail = self._web_search_tool_detail(item)
                        self._emit("tool", detail["label"])
                        self._emit("tool_done", detail)
                        used_tools.append("web_search")
                        tool_trace.append(detail["summary"])
                        tool_details.append(detail)
                if not calls:
                    reply = (response.output_text or streamed_text or "").strip()
                    if self._hide_requested and not reply:
                        reply = "Alright, thank you. Goodbye."
                    if not stream_started and reply:
                        self._emit("reply_start", "")
                        self._emit("reply_delta", reply)
                    self._emit("reply_done", reply or streamed_text.strip())
                    self._remember(tool_trace, reply)
                    log_event(f"reply: {reply[:200]}")
                    self._finish_overlay_hide(reply)
                    return AgentResult(
                        reply=reply,
                        tools=used_tools,
                        tool_details=tool_details,
                        elapsed=time.monotonic() - started,
                    )

                payload = []
                for call in calls:
                    if self._cancelled.is_set():
                        raise CancelledTurn()
                    try:
                        arguments = json.loads(call.arguments or "{}")
                    except json.JSONDecodeError:
                        arguments = {}
                    label = self._tool_label(call.name, arguments)
                    self._emit("tool", label)
                    self._emit(
                        "tool_start",
                        {"name": call.name, "arguments": arguments, "label": label},
                    )
                    used_tools.append(call.name)
                    output = self._execute(call.name, arguments)
                    detail = self._tool_detail(call.name, arguments, output, label=label)
                    detail["call_id"] = str(call.call_id or "")
                    tool_trace.append(detail["summary"])
                    tool_details.append(detail)
                    self._emit("tool_done", detail)
                    payload.append(
                        {
                            "type": "function_call_output",
                            "call_id": call.call_id,
                            "output": str(output)[:TOOL_OUTPUT_LIMIT],
                        }
                    )
                # Screenshots ride along as real image input, so this model sees
                # the pixels rather than another model's summary of them.
                if self._pending_images:
                    content = []
                    for image in self._pending_images:
                        content.append(
                            {"type": "input_text", "text": f"Screen capture for: {image['note']}"}
                        )
                        content.append({"type": "input_image", "image_url": image["url"]})
                    payload.append({"role": "user", "content": content})
                    self._pending_images = []

            try:
                reply = self._finalize_after_tool_rounds(
                    client,
                    model=model,
                    instructions=instructions,
                    payload=payload,
                    previous_id=previous_id,
                    reasoning=reasoning,
                )
            except CancelledTurn:
                raise
            except Exception as exc:
                log_event(f"final tool summary failed: {exc}")
                reply = self._tool_round_fallback(tool_details)
                self._emit("reply_start", "")
                self._emit("reply_delta", reply)
                self._emit("reply_done", reply)
            if not reply:
                reply = self._tool_round_fallback(tool_details)
                self._emit("reply_start", "")
                self._emit("reply_delta", reply)
                self._emit("reply_done", reply)
            self._remember(tool_trace, reply)
            log_event(f"reply after bounded tool work: {reply[:200]}")
            self._finish_overlay_hide(reply)
            return AgentResult(
                reply=reply,
                tools=used_tools,
                tool_details=tool_details,
                elapsed=time.monotonic() - started,
            )
        except CancelledTurn:
            log_event("turn cancelled mid-flight")
            # Drop the user turn too — replaying a question the user talked over
            # would make the next turn answer the wrong thing.
            self._forget_pending_turn()
            self._emit("reply_done", "")
            return AgentResult(
                reply="",
                cancelled=True,
                tools=used_tools,
                tool_details=tool_details,
                elapsed=time.monotonic() - started,
            )
        except Exception as exc:
            log_event(f"agent failed: {exc}")
            # The user turn was appended before the model ran; a turn with no
            # reply would poison the next one, so take it back out.
            self._forget_pending_turn()
            return AgentResult(
                error=str(exc)[:200],
                tools=used_tools,
                tool_details=tool_details,
                elapsed=time.monotonic() - started,
            )

    # ---------------------------------------------------------------- memory

    def _expire_idle(self, settings):
        """Forget the conversation once it has been quiet for long enough."""
        window = float(settings.get("agent_memory_minutes", 10)) * 60
        if not self.turns or not window:
            return
        idle = time.time() - self._last_turn_at
        if idle > window:
            log_event(f"memory cleared after {idle / 60:.1f} min idle")
            self.turns = []
            self._save_memory()

    @staticmethod
    def _tool_label(name, arguments):
        arguments = arguments or {}
        if name == "open_app":
            return f"opening {arguments.get('name') or 'app'}"
        if name == "open_url":
            url = str(arguments.get("url") or "")
            host = urllib.parse.urlparse(url if "://" in url else f"https://{url}").netloc
            return f"opening {host or 'url'}"
        if name == "run_powershell":
            command = str(arguments.get("command") or "")
            shown = command if len(command) <= 46 else command[:46] + "…"
            return f"powershell {shown}" if shown else "powershell"
        if name == "operator_task":
            return "handing to OPERATOR"
        if name == "operator_followup":
            return "sending OPERATOR follow-up"
        if name == "operator_stop":
            return "stopping OPERATOR"
        if name == "code_capabilities":
            return "checking CODE agents and models"
        if name == "code_setup":
            return f"opening {arguments.get('provider') or 'CODE'} sign-in"
        if name == "code_start":
            return f"dispatching {arguments.get('provider') or 'CODE'}"
        if name == "code_list":
            return "checking CODE sessions"
        if name == "code_status":
            return "reading CODE progress"
        if name == "code_continue":
            return "sending CODE follow-up"
        if name == "code_handoff":
            return f"switching CODE to {arguments.get('provider') or 'another provider'}"
        if name == "code_stop":
            return "stopping CODE job"
        if name == "code_delete":
            return "deleting CODE session"
        if name == "type_text":
            return "typing into your window"
        if name == "copy_text":
            return "copied to clipboard"
        if name == "add_note":
            return "adding a note"
        if name == "hide_overlay":
            return "hiding the overlay"
        if name == "web_search":
            return "searching the web"
        if name == "read_clipboard":
            return "reading the clipboard"
        if name == "update_plan":
            return "updating the plan"
        if name == "read_screen":
            return f"looking at screen {arguments.get('screen') or 'all'}"
        if name == "click_screen":
            clicks = int(arguments.get("clicks") or 1)
            verb = "double-clicking" if clicks == 2 else "clicking"
            return f"{verb} screen {arguments.get('screen') or '?'}"
        if name == "scroll_screen":
            return f"scrolling screen {arguments.get('screen') or '?'}"
        if name == "read_file":
            return f"reading {Path(str(arguments.get('path') or 'file')).name}"
        if name in {"write_file", "append_file"}:
            verb = "writing" if name == "write_file" else "appending to"
            return f"{verb} {Path(str(arguments.get('path') or 'file')).name}"
        if name == "list_files":
            return f"listing {Path(str(arguments.get('path') or 'folder')).name}"
        if name == "set_volume":
            percent = arguments.get("percent")
            return f"setting volume to {percent}%" if percent not in (None, "") else "checking the volume"
        if name == "media_control":
            return f"media {arguments.get('action') or 'control'}"
        if name == "set_timer":
            return "setting a reminder"
        if name == "list_timers":
            return "checking reminders"
        if name == "cancel_timer":
            return "cancelling a reminder"
        if name == "list_windows":
            return "listing open windows"
        if name == "focus_window":
            return f"focusing {arguments.get('title') or 'a window'}"
        if name == "close_window":
            return f"closing {arguments.get('title') or 'a window'}"
        if name == "remember":
            return "remembering that"
        if name == "forget":
            return "forgetting that"
        if name == "search_files":
            return f"searching for {arguments.get('query') or 'files'}"
        if name == "open_path":
            return f"opening {Path(str(arguments.get('path') or 'that')).name}"
        if name == "system_status":
            return "checking the machine"
        if name == "list_processes":
            return "listing processes"
        if name == "kill_process":
            return f"ending {arguments.get('name') or arguments.get('pid') or 'a process'}"
        if name == "read_url":
            host = urllib.parse.urlparse(str(arguments.get("url") or "")).netloc
            return f"reading {host or 'a page'}"
        if name == "notify":
            return "showing a note"
        if name == "list_self_files":
            return "checking my own files"
        if name == "read_self_file":
            return f"reading my {arguments.get('name') or 'notes'}"
        if name == "write_self_file":
            return f"rewriting my {arguments.get('name') or 'notes'}"
        if name == "append_self_file":
            return f"adding to my {arguments.get('name') or 'notes'}"
        return str(name or "tool")

    @classmethod
    def _tool_detail(cls, name, arguments, output, label=""):
        arguments = arguments if isinstance(arguments, dict) else {}
        output_text = str(output or "")
        summary = cls._trace_line(name, arguments, output_text)
        lowered = output_text.casefold()
        ok = not lowered.startswith(
            (
                "tool failed", "unknown tool", "no ", "could not", "refused",
                "command timed out", "nothing ", "path is outside", "bad path",
                "vision unavailable", "volume control unavailable", "unknown media",
            )
        )
        try:
            structured = json.loads(output_text)
        except (TypeError, json.JSONDecodeError):
            structured = None
        if isinstance(structured, dict) and isinstance(structured.get("ok"), bool):
            ok = structured["ok"]
        return {
            "name": str(name or "tool"),
            "label": label or cls._tool_label(name, arguments),
            "arguments": arguments,
            "output": output_text,
            "summary": summary,
            "ok": ok,
        }

    @classmethod
    def _web_search_tool_detail(cls, item):
        """Preserve the complete built-in web-search call returned by OpenAI."""
        try:
            payload = item.model_dump(mode="json", exclude_none=True)
        except (AttributeError, TypeError):
            payload = {
                "id": str(getattr(item, "id", "") or ""),
                "type": "web_search_call",
                "status": str(getattr(item, "status", "") or ""),
                "action": getattr(item, "action", None),
                "results": getattr(item, "results", None),
            }
            payload = json.loads(json.dumps(payload, default=str))
        action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
        detail = cls._tool_detail(
            "web_search",
            action,
            json.dumps(payload, ensure_ascii=False, indent=2),
            label="searched the web",
        )
        detail["call_id"] = str(payload.get("id") or "")
        detail["status"] = str(payload.get("status") or "")
        return detail

    @staticmethod
    def _trace_line(name, arguments, output):
        detail = ""
        for key in (
            "name", "url", "command", "task", "message", "note", "text",
            "path", "question", "title", "fact", "match", "action", "label",
        ):
            if arguments.get(key):
                detail = str(arguments[key])
                break
        detail = detail if len(detail) <= 70 else detail[:70] + "…"
        result = str(output or "").replace("\n", " ")
        result = result if len(result) <= 160 else result[:160] + "…"
        return f"{name}({detail}) → {result}" if detail else f"{name} → {result}"

    def _forget_pending_turn(self):
        """Remove the user turn that never got an answer."""
        while self.turns and self.turns[-1]["role"] != "assistant":
            self.turns.pop()
        self._save_memory()

    def _remember(self, tool_trace, reply):
        now = time.time()
        if tool_trace:
            self.turns.append({"role": "tool", "text": " · ".join(tool_trace), "at": now})
        if reply:
            self.turns.append({"role": "assistant", "text": reply, "at": now})
        self._last_turn_at = now
        limit = max(4, int(self.history_limit))
        if len(self.turns) > limit:
            self.turns = self.turns[-limit:]
        self._save_memory()

    def _conversation_input(self, _settings):
        items = []
        for turn in self.turns:
            role = turn["role"]
            text = str(turn.get("text") or "")
            if not text:
                continue
            if role == "tool":
                # Replayed as the assistant's own account of what it did.
                items.append({"role": "assistant", "content": f"[ran: {text}]"})
            else:
                items.append({"role": role, "content": text})
        return items

    def history(self):
        """(role, text) rows for the overlay, newest last. 'tool' rows included."""
        return [(turn["role"], str(turn.get("text") or "")) for turn in self.turns]

    def history_before_current(self):
        """Everything except the turn being spoken right now."""
        rows = self.history()
        while rows and rows[-1][0] != "assistant":
            rows.pop()
        return rows

    def clear(self):
        """Drop the conversation (GUI Reset / idle expiry). Facts survive."""
        acquired = self._lock.acquire(timeout=TURN_ACQUIRE_SECONDS)
        try:
            self.turns = []
            self._last_turn_at = 0.0
            self._save_memory()
            log_event("memory cleared by request")
        finally:
            if acquired:
                self._lock.release()


if __name__ == "__main__":
    import sys

    def show(kind, text):
        print(f"[{kind}] {text}")

    agent = VoiceAgent(on_event=show)
    result = agent.run(" ".join(sys.argv[1:]) or "what time is it and what is my cpu doing")
    print("\nreply:", result.reply or result.error)
    print("tools:", result.tools, f"{result.elapsed:.1f}s")
