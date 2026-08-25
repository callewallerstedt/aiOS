"""Backend for the Settings tab and the Quick Tools tray.

The Tk build wrote settings straight from the widget callbacks, which meant the
clamping rules (voice_settings.merge_voice_dictation, the operator defaults, the
theme ranges) lived next to the widgets. The web UI cannot do that, so this
module is the single place that turns a patch from the browser into a saved
config -- using the *same* helpers the Tk build used, so a value typed in the new
UI lands on disk byte-identical to the old one.

Everything here is import-lazy: helper_overlay pulls in tkinter, openrouter and
ollama do network work, and aios_updater talks to GitHub. None of that should
happen just because the server started.
"""

from __future__ import annotations

import json
import math
import os
import socket
import subprocess
import sys
import threading
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent

# The running voice_dictation process listens here and re-reads helper_config.json
# when nudged. Same port the Tk build used in _reload_voice_dictation.
VOICE_RELOAD_PORT = 48737

RECORDINGS_FOLDER_NAME = "aiOS recordings"

# Sub-pages of the Settings tab, and the colour rows on Appearance. Mirrors
# helper_overlay.HelperApp.SETTINGS_PAGES and _settings_appearance so the two
# builds cannot drift apart silently.
SETTINGS_PAGES = [
    ["General", "Project folder, mobile remote and updates."],
    ["Appearance", "Colors, sizing and how the window behaves."],
    ["Voice", "Dictation keys, microphone and transcription quality."],
    ["Voice agent", "What the agent you talk to is allowed to do."],
    ["OPERATOR", "The agent that drives your mouse and keyboard."],
    ["Models", "Codex, quick chat and API keys."],
    ["Macro pad", "The buttons that drive aiOS from your macro keyboard."],
]

THEME_COLORS = [
    ["accent", "Accent"],
    ["app_background", "App background"],
    ["code_chat_background", "Chat panes"],
    ["code_sidebar_background", "Projects & sessions sidebar"],
    ["panel", "Panel"],
    ["surface", "Cards"],
    ["surface2", "Cards 2"],
    ["panel2", "Inputs"],
    ["text", "Text"],
    ["muted", "Muted"],
    ["chat_link", "Chat links"],
    ["success", "Success"],
    ["danger", "Danger"],
    ["thinking_base", "Dot base"],
    ["thinking_pulse", "Dot pulse"],
]

VOICE_AGENT_TOOLS = [
    ["agent_web_search", "Web search", "Look things up online."],
    ["agent_open_apps", "Open apps & URLs", "Launch Start Menu apps and web pages."],
    ["agent_shell", "PowerShell", "Run local commands for facts and small changes."],
    ["agent_operator", "OPERATOR", "Hand multi-step GUI work to the computer-use agent."],
    ["agent_clipboard_read", "Read clipboard", "Use what you just copied as context."],
    ["agent_screen", "Read screen", "Look at the monitor and answer questions about it."],
    ["agent_files", "Files", "Read and write text files in your allowed folders."],
    ["agent_media", "Volume & media", "Set the volume, play/pause, skip tracks."],
    ["agent_timers", "Reminders", "Schedule reminders that are spoken out loud."],
    ["agent_windows", "Windows", "List, focus and close open windows."],
    ["agent_remember", "Long-term memory", "Remember facts about you across restarts."],
    ["agent_self_edit", "Edit its own soul", "Rewrite SOUL.md / MEMORY.md in agent_self/."],
]

MACRO_PAD_FILES = [
    ["aios_toggle.bat", "aiOS button (one-shot): toggle full aiOS."],
    ["aios_pad_down.bat", "aiOS button down: start short/hold timer (needed for Quick Tools)."],
    ["aios_pad_up.bat", "aiOS button up: tap = aiOS, hold = Quick Tools."],
    ["voice_ptt_down.bat", "Button down: start dictating."],
    ["voice_ptt_up.bat", "Button release: stop and send."],
    ["voice_target_cursor.bat", "Send the transcript to whatever window is focused."],
    ["voice_target_clipboard.bat", "Copy the transcript instead of typing it."],
    ["voice_target_agent.bat", "Send the transcript to the voice agent."],
    ["voice_cancel.bat", "Throw the turn away without sending it."],
    ["voice_stop_agent.bat", "Panic button: stop the reply, the turn and any OPERATOR job."],
]

MACRO_PAD_PLANNED = [
    "aiOS button: tap (<200ms) opens full aiOS; hold opens Quick Tools.",
    "For hold to work in Macro Deck, bind Pressed→aios_pad_down.bat and Released→aios_pad_up.bat.",
    "Quick Tools overlay is a 3×3 WebView2 palette matching the aiOS shell.",
    "Bind a button to any aiOS action from this page.",
]

OPERATOR_MODELS = ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"]
OPERATOR_REASONING = ["minimal", "low", "medium", "high", "xhigh", "max"]
CODEX_REASONING = ["none", "low", "medium", "high", "xhigh"]
AGENT_REASONING = ["off", "minimal", "low", "medium", "high"]
AGENT_TTS_VOICES = ["f1", "f2", "f3", "f4", "f5", "m1", "m2", "m3", "m4", "m5"]
OPERATOR_VOICES = ["alloy", "echo", "fable", "onyx", "nova", "shimmer"]


# --------------------------------------------------------------------- config


def _helper():
    import helper_overlay

    return helper_overlay


def load_config() -> dict:
    return _helper().load_config()


def save_config(config: dict) -> None:
    _helper().save_config(config)


def nudge_voice() -> None:
    """Tell the running dictation process to re-read helper_config.json."""
    try:
        with socket.create_connection(("127.0.0.1", VOICE_RELOAD_PORT), timeout=0.3) as client:
            client.sendall(b"reload")
    except OSError:
        pass  # dictation is not running; it will read the file when it starts


def _voice_defaults() -> dict:
    import voice_settings

    return dict(voice_settings.DEFAULT_VOICE_DICTATION)


# ----------------------------------------------------------------------- meta


def _ollama_models(*, wait_s: float = 0.25) -> list[dict]:
    """Local models for the Voice agent picker — never stall Settings for this.

    Settings boot awaits /api/settings/meta. A live Ollama round-trip (or worse,
    ensure_ollama starting the daemon) used to leave the tab blank for seconds.
    Take the memory cache immediately; if cold, wait briefly for a no-start
    probe and otherwise return [] while a background warm fills the cache for
    the next open.
    """
    try:
        import ollama_client
    except Exception:
        return []

    def rows_from(models: list) -> list[dict]:
        return [
            {
                "id": f"ollama:{row['id']}",
                "label": f"ollama:{row['id']}",
                "hint": row.get("description") or "local",
            }
            for row in models
        ]

    cached = getattr(ollama_client, "_MODELS_CACHE", None)
    if cached is not None:
        return rows_from(cached)

    box: dict[str, list] = {"rows": []}

    def probe() -> None:
        try:
            box["rows"] = ollama_client.list_installed_models(use_cache=True, ensure=False)
        except Exception:
            box["rows"] = []

    worker = threading.Thread(target=probe, daemon=True, name="aios-settings-ollama")
    worker.start()
    worker.join(max(0.0, float(wait_s)))
    if worker.is_alive():
        # Keep warming in the background; next Settings open hits the cache.
        return []
    return rows_from(box["rows"])


def _openrouter_agent_models() -> list[dict]:
    """The models you enabled in Settings -> Models, as agent choices.

    Prefixed `openrouter:` the same way local models are prefixed `ollama:`;
    that prefix is what routes the turn to the Chat Completions path in
    voice_agent, because OpenRouter does not speak the Responses API.
    """
    try:
        import openrouter_client

        return [
            {
                "id": f"openrouter:{row['id']}",
                "label": f"openrouter:{row['id']}",
                "hint": str(row.get("description") or "OpenRouter"),
            }
            for row in openrouter_client.list_enabled_models()
        ]
    except Exception:
        return []


def _openrouter_catalog() -> list[dict]:
    try:
        import openrouter_client

        return [
            {
                "id": str(row["id"]),
                "label": str(row.get("label") or row["id"]),
                "description": str(row.get("description") or ""),
            }
            for row in openrouter_client.catalog_models()
        ]
    except Exception:
        return []


def _updater_source() -> dict:
    try:
        import aios_updater

        source = aios_updater.load_source()
        return {
            "ok": True,
            "owner": source.get("owner", ""),
            "repo": source.get("repo", ""),
            "branch": source.get("branch", "main"),
            "current": aios_updater.get_current_sha() or "(unknown)",
            "current_branch": aios_updater.get_current_branch() or "",
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _agent_model_choices() -> list[dict]:
    """Every selectable brain for the Agent sidebar, before user filtering."""
    return [{"id": name, "label": name, "hint": "cloud"} for name in OPERATOR_MODELS] \
        + _ollama_models() \
        + _openrouter_agent_models()


def agent_chat_models() -> dict:
    """Saved Agent-dropdown preferences plus everything available to pick."""
    config = load_config()
    section = config.get("agent_chat")
    saved = section.get("models") if isinstance(section, dict) else None
    rows = []
    if isinstance(saved, list):
        for row in saved:
            if not isinstance(row, dict):
                continue
            mid = str(row.get("id") or "").strip()
            if not mid:
                continue
            rows.append({
                "id": mid,
                "label": str(row.get("label") or "").strip() or mid,
                "show": bool(row.get("show", True)),
            })
    return {"ok": True, "models": rows, "available": _agent_model_choices()}


def save_agent_chat_models(rows) -> dict:
    """Persist which models appear in the Agent dropdown and their names."""
    config = load_config()
    clean, seen = [], set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        mid = str(row.get("id") or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        clean.append({
            "id": mid,
            "label": str(row.get("label") or "").strip() or mid,
            "show": bool(row.get("show", True)),
        })
    config["agent_chat"] = {"models": clean}
    save_config(config)
    return {"ok": True, "models": clean}


def meta() -> dict:
    """Everything the Settings UI needs that is not the config itself."""
    import voice_settings

    config = load_config()
    relay = config.get("phone_relay") or {}
    return {
        "ok": True,
        "pages": SETTINGS_PAGES,
        "theme_colors": THEME_COLORS,
        "theme_defaults": _helper().DEFAULT_CONFIG["theme"],
        "voice_defaults": _voice_defaults(),
        "voice_tools": VOICE_AGENT_TOOLS,
        "hotkeys": list(voice_settings.VOICE_HOTKEY_OPTIONS),
        "whisper_models": list(voice_settings.WHISPER_MODELS),
        "whisper_languages": [
            {"id": code, "label": voice_settings.LANGUAGE_LABELS.get(code, code)}
            for code in voice_settings.WHISPER_LANGUAGES
        ],
        "compute_types": list(voice_settings.COMPUTE_TYPES),
        "whisper_devices": list(voice_settings.WHISPER_DEVICES),
        "agent_reasoning": AGENT_REASONING,
        "agent_tts_voices": AGENT_TTS_VOICES,
        "agent_models": _agent_model_choices(),
        "operator_models": OPERATOR_MODELS,
        "operator_reasoning": OPERATOR_REASONING,
        "operator_voices": OPERATOR_VOICES,
        "codex_reasoning": CODEX_REASONING,
        "openrouter_models": _openrouter_catalog(),
        "openrouter_enabled": list(config.get("openrouter_enabled_models") or []),
        "macro_files": [
            {"name": name, "hint": hint, "present": (BASE_DIR / name).exists()}
            for name, hint in MACRO_PAD_FILES
        ],
        "macro_planned": MACRO_PAD_PLANNED,
        "env_openai": bool(str(os.environ.get("OPENAI_API_KEY") or "").strip()),
        "env_openrouter": bool(str(os.environ.get("OPENROUTER_API_KEY") or "").strip()),
        "relay_paired": bool(relay.get("machine_token")),
        "computer_name": os.environ.get("COMPUTERNAME", "My computer"),
        "base_dir": str(BASE_DIR),
        "updater": _updater_source(),
    }


# ------------------------------------------------------------------- writers


def save_voice(patch: dict) -> dict:
    """Patch voice_dictation, clamped by the same merge the Tk build used."""
    import voice_settings

    config = load_config()
    current = voice_settings.merge_voice_dictation(config.get("voice_dictation"))
    current.update(patch or {})
    # Keys the merge would clamp to a different value than the UI offers are
    # applied first and re-checked after: separate hotkeys must never collide,
    # exactly as set_voice_separate_hotkeys did.
    merged = voice_settings.merge_voice_dictation(current)
    if "agent_reasoning" in (patch or {}) and str(patch["agent_reasoning"]) == "off":
        # "off" is a real choice in the UI (it skips thinking on Ollama models)
        # but merge_voice_dictation only knows minimal/low/medium/high.
        merged["agent_reasoning"] = "off"
    config["voice_dictation"] = merged
    save_config(config)
    nudge_voice()
    return {"ok": True, "voice_dictation": merged}


def save_theme(patch: dict) -> dict:
    """Patch the theme, clamping the numeric fields to the Tk ranges."""
    config = load_config()
    theme = dict(config.get("theme") or {})
    for key, value in (patch or {}).items():
        if key == "opacity":
            theme[key] = max(0.75, min(1.0, float(value)))
        elif key == "font_size":
            theme[key] = max(8, min(15, int(float(value))))
        elif key == "radius":
            radius = float(value)
            if not math.isfinite(radius):
                raise ValueError("radius must be finite")
            radius = max(0.0, radius)
            theme[key] = int(radius) if radius.is_integer() else round(radius, 3)
        elif key in {"thinking_base_opacity", "thinking_pulse_opacity"}:
            theme[key] = max(0, min(100, int(float(value))))
        elif key == "always_on_top":
            theme[key] = bool(value)
        else:
            theme[key] = value
    config["theme"] = theme
    save_config(config)
    return {"ok": True, "theme": theme}


def save_operator(patch: dict) -> dict:
    """Patch ai_operator, mirroring save_agent_operator_settings."""
    helper = _helper()
    config = load_config()
    settings = dict(config.get("ai_operator") or helper.DEFAULT_CONFIG["ai_operator"])
    for key, value in (patch or {}).items():
        if key in {"tts", "shell", "codex_auth"}:
            settings[key] = bool(value)
        elif key == "steps":
            settings[key] = str(int(float(value)))
        elif key == "delay":
            settings[key] = f"{float(value):.2f}"
        else:
            settings[key] = str(value)
    if "codex_auth" in (patch or {}):
        # The Tk build kept provider_mode in step with the Codex toggle; leaving
        # it stale would silently bill the wrong account.
        mode = str(settings.get("provider_mode") or "").strip().lower()
        if not settings.get("codex_auth"):
            settings["provider_mode"] = "api"
        elif mode not in {"codex", "codex_api_fallback"}:
            settings["provider_mode"] = "codex"
    config["ai_operator"] = helper.merge_dict(helper.DEFAULT_CONFIG["ai_operator"], settings)
    save_config(config)
    return {"ok": True, "ai_operator": config["ai_operator"]}


def save_relay(patch: dict) -> dict:
    config = load_config()
    relay = dict(config.get("phone_relay") or {})
    relay.update(patch or {})
    config["phone_relay"] = relay
    save_config(config)
    return {"ok": True, "phone_relay": relay}


def save_project_root(text: str) -> dict:
    path = Path(str(text or "").strip())
    if not str(path).strip():
        return {"ok": False, "error": "Enter a folder."}
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    config = load_config()
    config["project_root"] = str(path)
    save_config(config)
    return {"ok": True, "project_root": str(path)}


def save_openai_key(text: str) -> dict:
    config = load_config()
    config["openai_api_key"] = str(text or "").strip()
    save_config(config)
    return {"ok": True}


def save_openrouter_key(text: str) -> dict:
    config = load_config()
    config["openrouter_api_key"] = str(text or "").strip()
    save_config(config)
    return {"ok": True}


def save_openrouter_models(enabled: list) -> dict:
    helper = _helper()
    ids = [str(item) for item in (enabled or []) if str(item).strip()]
    if not ids:
        # Never leave CODE with an empty picker.
        ids = list(helper.DEFAULT_CONFIG["openrouter_enabled_models"])
    config = load_config()
    config["openrouter_enabled_models"] = ids
    save_config(config)
    try:
        import code_jobs
        import openrouter_client

        openrouter_client.invalidate_cache()
        code_jobs.capabilities(force=True)
    except Exception:
        pass
    return {"ok": True, "openrouter_enabled_models": ids}


def refresh_openrouter_models() -> dict:
    try:
        import code_jobs
        import openrouter_client

        rows = openrouter_client.catalog_models(refresh=True, limit=250)
        code_jobs.capabilities(force=True)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "count": len(rows), "models": _openrouter_catalog()}


def pair_relay(url: str, code: str, name: str) -> dict:
    url = str(url or "").strip()
    code = str(code or "").strip()
    name = str(name or "").strip() or os.environ.get("COMPUTERNAME", "My computer")
    if not url or not code:
        return {"ok": False, "error": "Enter the remote URL and private code."}
    try:
        from phone_relay import pair

        relay = pair(url, code, name)
    except Exception as exc:
        return {"ok": False, "error": f"Could not connect: {exc}"}
    _start_phone_bridge()
    return {"ok": True, "machine_name": relay.get("machine_name") or name}


def _start_phone_bridge() -> None:
    config = load_config()
    relay = config.get("phone_relay") or {}
    if not relay.get("enabled") or not relay.get("machine_token"):
        return
    try:
        subprocess.Popen(
            [
                "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(BASE_DIR / "start-phone-bridge.ps1"),
            ],
            cwd=str(BASE_DIR),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


# ----------------------------------------------------------------- the updater


def update_save_source(owner: str, repo: str, branch: str) -> dict:
    owner = str(owner or "").strip()
    repo = str(repo or "").strip()
    branch = str(branch or "").strip() or "main"
    if not owner or not repo:
        return {"ok": False, "error": "Owner and Repo are required."}
    try:
        import aios_updater

        if not aios_updater.save_source(owner, repo, branch):
            return {"ok": False, "error": "Could not save (check helper_config.json)."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True}


def update_check() -> dict:
    try:
        import aios_updater

        return aios_updater.check_for_update()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


_UPDATE_LOG: list[str] = []
_UPDATE_BUSY = {"running": False}


def update_run() -> dict:
    """Update and restart. The log is polled from /api/update/log."""
    if _UPDATE_BUSY["running"]:
        return {"ok": False, "error": "An update is already running."}
    _UPDATE_BUSY["running"] = True
    _UPDATE_LOG.clear()

    def progress(message: str) -> None:
        _UPDATE_LOG.append(str(message))

    def worker() -> None:
        try:
            import aios_updater

            result = aios_updater.perform_update(progress=progress)
            _UPDATE_LOG.append(str(result.get("message") or ""))
            if not result.get("ok"):
                return
            if result.get("staged"):
                _UPDATE_LOG.append("Closing aiOS to apply staged files…")
            else:
                _UPDATE_LOG.append("Restarting aiOS…")
            # A moment so the browser's last poll picks the message up before
            # the process goes away.
            threading.Timer(1.0, aios_updater.restart_aios).start()
        except Exception as exc:
            _UPDATE_LOG.append(f"Update failed: {exc}")
        finally:
            _UPDATE_BUSY["running"] = False

    threading.Thread(target=worker, daemon=True, name="aios-update").start()
    return {"ok": True}


def update_log() -> dict:
    return {"ok": True, "lines": list(_UPDATE_LOG), "running": _UPDATE_BUSY["running"]}


# ------------------------------------------------------------- quick tools


def downloads_dir() -> Path:
    path = Path.home() / "Downloads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def recordings_dir() -> Path:
    path = Path.home() / "Videos" / RECORDINGS_FOLDER_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _unique_download_path(stem: str, suffix: str) -> Path:
    downloads = downloads_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = downloads / f"{stem}-{stamp}{suffix}"
    if not base.exists():
        return base
    for index in range(2, 1000):
        candidate = downloads / f"{stem}-{stamp}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise FileExistsError("Could not allocate a Downloads filename.")


def _clipboard_image_via_pillow() -> Path | None:
    try:
        from PIL import ImageGrab
    except ImportError:
        return None
    data = ImageGrab.grabclipboard()
    if data is None:
        return None
    if isinstance(data, list):
        import shutil

        for item in data:
            path = Path(item)
            if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}:
                target = _unique_download_path("aios-clipboard", path.suffix.lower())
                shutil.copy2(path, target)
                return target
        return None
    if hasattr(data, "save"):
        target = _unique_download_path("aios-clipboard", ".png")
        data.save(target, "PNG")
        return target
    return None


def paste_clipboard_image() -> dict:
    try:
        saved = _clipboard_image_via_pillow()
    except Exception as exc:
        return {"ok": False, "error": f"Could not save image: {exc}"}
    if not saved:
        return {"ok": False, "error": "No clipboard image."}
    return {"ok": True, "message": f"Saved {saved.name}", "path": str(saved)}


def phone_photo_session() -> dict:
    """Ask the local phone bridge for a photo-drop link (QR target)."""
    try:
        request = urllib.request.Request(
            "http://127.0.0.1:5000/api/photo-drop/session",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=4) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {"ok": False, "error": f"Phone bridge unavailable: {exc}"}
    return {"ok": True, "session": payload}


def open_folder(target: str) -> dict:
    path = {"downloads": downloads_dir, "recordings": recordings_dir}.get(target)
    if not path:
        return {"ok": False, "error": f"unknown folder {target}"}
    try:
        os.startfile(str(path()))  # noqa: S606 - Windows shell open, same as the Tk build
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "message": f"Opened {target}"}


def run_tool(name: str, data: dict) -> dict:
    if name == "downloads":
        return open_folder("downloads")
    if name == "recordings":
        return open_folder("recordings")
    if name == "paste_image":
        return paste_clipboard_image()
    if name == "phone_photos":
        return phone_photo_session()
    if name == "open_base_dir":
        try:
            os.startfile(str(BASE_DIR))  # noqa: S606
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "message": "Opened the aiOS folder"}
    if name == "open_url":
        url = str((data or {}).get("url") or "").strip()
        if not url:
            return {"ok": False, "error": "No URL."}
        try:
            os.startfile(url)  # noqa: S606
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}
    if name == "record_screen":
        from . import screen_recording

        action = str((data or {}).get("action") or "options").lower()
        if action == "options":
            return screen_recording.state(include_options=True)
        if action == "status":
            return screen_recording.state()
        if action == "start":
            return screen_recording.start(data or {})
        if action == "stop":
            return screen_recording.stop()
        return {"ok": False, "error": f"unknown screen recording action {action}"}
    if name == "webcam_snap":
        from . import webcam_snap

        return webcam_snap.handle(data or {})
    return {"ok": False, "error": f"unknown tool {name}"}


# -------------------------------------------------------------------- routing


def dispatch(route: str, method: str, params: dict, data: dict) -> Any:
    """Return a payload, or None when this module owns no such route."""
    method = method.upper()
    route = route.rstrip("/")
    data = data or {}
    patch = data.get("patch") if isinstance(data.get("patch"), dict) else data

    if route == "/api/settings/meta" and method == "GET":
        return meta()
    if route == "/api/settings/voice" and method == "POST":
        return save_voice(patch)
    if route == "/api/settings/agent-chat-models" and method == "GET":
        return agent_chat_models()
    if route == "/api/settings/agent-chat-models" and method == "POST":
        return save_agent_chat_models(data.get("models"))
    if route == "/api/settings/theme" and method == "POST":
        return save_theme(patch)
    if route == "/api/settings/operator" and method == "POST":
        return save_operator(patch)
    if route == "/api/settings/relay" and method == "POST":
        return save_relay(patch)
    if route == "/api/settings/relay/pair" and method == "POST":
        return pair_relay(data.get("url"), data.get("code"), data.get("name"))
    if route == "/api/settings/project-root" and method == "POST":
        return save_project_root(data.get("path"))
    if route == "/api/settings/openai-key" and method == "POST":
        return save_openai_key(data.get("key"))
    if route == "/api/settings/openrouter-key" and method == "POST":
        return save_openrouter_key(data.get("key"))
    if route == "/api/settings/openrouter/models" and method == "POST":
        return save_openrouter_models(data.get("enabled"))
    if route == "/api/settings/openrouter/refresh" and method == "POST":
        return refresh_openrouter_models()

    if route == "/api/update/source" and method == "POST":
        return update_save_source(data.get("owner"), data.get("repo"), data.get("branch"))
    if route == "/api/update/check" and method == "POST":
        return update_check()
    if route == "/api/update/run" and method == "POST":
        return update_run()
    if route == "/api/update/log" and method == "GET":
        return update_log()

    if route.startswith("/api/tools/") and method == "POST":
        return run_tool(route.rsplit("/", 1)[-1], data)

    return None
