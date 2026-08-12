"""Paths, defaults and settings for the aiOS Director runtime.

Director runs on the always-on Linux box (calle-linux / rocky-server). Every
path is overridable so the same package can be exercised on Windows during
development and in tests.

Settings live in one JSON file. Nothing here imports the rest of the package,
so the server, the agent loop and the tools can all read configuration without
importing each other.
"""
from __future__ import annotations

import json
import os
import pathlib
import threading
from typing import Any

PACKAGE_DIR = pathlib.Path(__file__).resolve().parent


def _env_path(name: str) -> pathlib.Path | None:
    raw = str(os.environ.get(name, "")).strip()
    return pathlib.Path(raw).expanduser() if raw else None


def home() -> pathlib.Path:
    """Directory holding the database, logs, browser profile and screenshots."""
    override = _env_path("AIOS_DIRECTOR_HOME")
    if override:
        return override
    base = _env_path("XDG_DATA_HOME") or (pathlib.Path.home() / ".local" / "share")
    return base / "aios-director"


def ensure_home() -> pathlib.Path:
    path = home()
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> pathlib.Path:
    return ensure_home() / "director.db"


def settings_path() -> pathlib.Path:
    return ensure_home() / "settings.json"


def shots_dir() -> pathlib.Path:
    path = ensure_home() / "shots"
    path.mkdir(parents=True, exist_ok=True)
    return path


def uploads_dir() -> pathlib.Path:
    path = ensure_home() / "uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def chrome_profile_dir() -> pathlib.Path:
    """Persistent Chrome profile so web logins survive restarts."""
    path = ensure_home() / "chrome-profile"
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_path() -> pathlib.Path:
    return ensure_home() / "director.log"


# The house default. Luna is the fast, cheap coordinator model and it reaches
# the Codex backend through the ChatGPT OAuth tokens rather than a billed key.
DEFAULT_MODEL = "gpt-5.6-luna"
DEFAULT_BACKEND = "codex"
DEFAULT_REASONING = "low"

# Port Director listens on. Tailscale Funnel proxies /director on the public
# hostname to this, so it only ever binds loopback.
DEFAULT_PORT = 8770
DEFAULT_BIND = "127.0.0.1"

DEFAULT_SETTINGS: dict[str, Any] = {
    "backends": {
        # `codex` needs no key: it reads ~/.codex/auth.json written by the CLI.
        "codex": {"enabled": True, "codex_home": ""},
        "openrouter": {"enabled": True, "api_key": ""},
    },
    "defaults": {
        "backend": DEFAULT_BACKEND,
        "model": DEFAULT_MODEL,
        "reasoning": DEFAULT_REASONING,
    },
    "operator": {
        "backend": DEFAULT_BACKEND,
        "model": DEFAULT_MODEL,
        "reasoning": DEFAULT_REASONING,
        "display": ":99",
        "width": 1600,
        "height": 900,
        "max_steps": 40,
        "vnc_port": 5999,
        "novnc_port": 6080,
    },
    "voice": {
        # Transcription for phone voice input. `openai` posts the recording to
        # the Whisper endpoint with the OpenRouter-independent OpenAI key.
        "transcribe_backend": "openai",
        "openai_api_key": "",
        "model": "whisper-1",
        "tts_enabled": False,
    },
    "server": {
        "bind": DEFAULT_BIND,
        "port": DEFAULT_PORT,
        "public_url": "",
    },
    "appearance": {
        "user_bubble": "#3a5a8c",
        "user_text": "#f2f3f4",
        "agent_bubble": "#2b2c2f",
        "agent_text": "#f2f3f4",
    },
    "safety": {
        # Tools that always raise an approval card before they run.
        "confirm_destructive": True,
        # Blanket yes. Set from the phone ("Approve everything"), and turned
        # off again there. Per-agent and per-run grants live elsewhere.
        "approve_all": False,
    },
    # Standing instructions every agent sees. Edited from the phone Settings
    # screen; empty means none.
    "instructions": "",
    # Wake-on-LAN for calle-windows. The client refreshes ip/mac when it
    # connects; these are the house Ethernet NIC so a cold box can still
    # be woken before that hello arrives.
    "wake": {
        "mac": "30:C5:99:D0:0D:4A",
        "broadcast": "192.168.0.255",
        "ip": "192.168.0.83",
    },
    "push": {
        "enabled": True,
        "public_key": "",
        "private_pem": "",
        "subject": "mailto:calle.wallerstedt@gmail.com",
    },
}

_LOCK = threading.Lock()
_CACHE: dict[str, Any] | None = None
_CACHE_MTIME: float = 0.0


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_settings(*, refresh: bool = False) -> dict[str, Any]:
    """Read settings.json merged over the defaults.

    Cached on mtime so the agent loop can call this per turn without hitting
    the disk every time, while an edit from the settings API still lands.
    """
    global _CACHE, _CACHE_MTIME
    path = settings_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    with _LOCK:
        if not refresh and _CACHE is not None and mtime == _CACHE_MTIME:
            return json.loads(json.dumps(_CACHE))
        raw: dict[str, Any] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    raw = loaded
            except (OSError, json.JSONDecodeError):
                raw = {}
        merged = _merge(DEFAULT_SETTINGS, raw)
        _CACHE = merged
        _CACHE_MTIME = mtime
        return json.loads(json.dumps(merged))


def save_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Write settings.json atomically and refresh the cache."""
    global _CACHE, _CACHE_MTIME
    merged = _merge(DEFAULT_SETTINGS, settings or {})
    path = settings_path()
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    temp.replace(path)
    with _LOCK:
        _CACHE = merged
        try:
            _CACHE_MTIME = path.stat().st_mtime
        except OSError:
            _CACHE_MTIME = 0.0
    return json.loads(json.dumps(merged))


def update_settings(patch: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge a patch into the stored settings."""
    return save_settings(_merge(load_settings(refresh=True), patch or {}))


def openrouter_key(settings: dict[str, Any] | None = None) -> str:
    cfg = settings if settings is not None else load_settings()
    key = str((cfg.get("backends", {}).get("openrouter", {}) or {}).get("api_key") or "").strip()
    return key or str(os.environ.get("OPENROUTER_API_KEY", "")).strip()


def openai_key(settings: dict[str, Any] | None = None) -> str:
    cfg = settings if settings is not None else load_settings()
    key = str((cfg.get("voice", {}) or {}).get("openai_api_key") or "").strip()
    return key or str(os.environ.get("OPENAI_API_KEY", "")).strip()


def public_url(settings: dict[str, Any] | None = None) -> str:
    cfg = settings if settings is not None else load_settings()
    url = str((cfg.get("server", {}) or {}).get("public_url") or "").strip()
    return url or str(os.environ.get("AIOS_DIRECTOR_PUBLIC_URL", "")).strip()
