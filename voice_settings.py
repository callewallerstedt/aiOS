"""Shared voice dictation settings (helper_config.json -> voice_dictation section)."""

from __future__ import annotations

import json
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "helper_config.json"

DEFAULT_VOICE_DICTATION = {
    "hold_ms": 280,
    "whisper_model": "small",
    "language": "auto",
    "device": "cuda",
    "compute_type": "float16",
    "chunk_seconds": 1.0,
    "silence_flush_seconds": 0.35,
    "silence_rms": 0.006,
    "typing_delay_ms": 0,
    "discord_mute_enabled": False,
    "discord_mute_hotkey": "",
    "voice_hotkey": "Insert",
    "aios_hotkey": "Insert",
    "separate_hotkeys": False,
    # Voice agent: what a transcript routed to "agent" runs on, and which
    # tools it may fire without asking.
    "agent_model": "gpt-5.6-luna",
    "agent_reasoning": "low",
    "agent_max_rounds": 6,
    "agent_web_search": True,
    "agent_shell": True,
    "agent_operator": True,
    "agent_open_apps": True,
    "agent_memory_minutes": 10,
    "agent_tts_enabled": True,
    "agent_tts_voice": "m3",
    # Extra agent tools. Each one is a capability the spoken agent may use
    # without asking; turning one off removes it from the tool list entirely.
    "agent_clipboard_read": True,
    "agent_screen": True,
    "agent_files": True,
    "agent_media": True,
    "agent_timers": True,
    "agent_windows": True,
    "agent_remember": True,
    # The agent may rewrite its own SOUL.md / MEMORY.md in agent_self/.
    "agent_self_edit": True,
    # Directories the file tools may touch. Empty means "the built-in set"
    # (project root plus Documents / Desktop / Downloads).
    "agent_file_roots": [],
    # Shell safety: refuse commands that match the destructive patterns, and
    # require a spoken confirmation before running anything that changes state.
    "agent_shell_guard": True,
    "agent_shell_confirm": True,
    # Conversation survives a restart of the dictation process.
    "agent_persist_memory": True,
    # Dictation HUD translucency (pill + compose panel), percent 20–100.
    "overlay_opacity": 85,
    # --- transcription quality -------------------------------------------
    # Preferred microphone by name substring; empty uses the system default.
    "input_device": "",
    # Words Whisper keeps getting wrong: biased into the decoder prompt. The
    # defaults are the aiOS vocabulary it has never heard; add your own.
    "vocabulary": ["aiOS", "OPERATOR", "Codex", "Whisper", "PowerShell", "Claude"],
    # Applied to the finished transcript, case-insensitive, whole word.
    "replacements": {},
    # Drop the classic Whisper silence artifacts ("Thank you.", "Tack för att
    # du tittade!") instead of typing them into whatever you had focused.
    "hallucination_filter": True,
    # Shortest utterance that still gets transcribed. Below this the audio is
    # padded rather than discarded, so "yes" and "stop" survive.
    "min_speech_seconds": 0.22,
    # Append every finished transcript to voice-transcripts.jsonl.
    "transcript_history": True,
    # Cut agent speech the moment the user starts talking again.
    "barge_in": True,
}


# AHK key names that are safe to use as a tap-and-hold trigger (won't block
# normal typing or modifier-only). Display name → canonical AHK key.
SAFE_HOTKEYS = {
    "Insert": "Insert",
    "Home": "Home",
    "End": "End",
    "PageUp": "PgUp",
    "PageDown": "PgDn",
    "Delete": "Delete",
    "ScrollLock": "ScrollLock",
    "Pause": "Pause",
    "AppsKey": "AppsKey",  # the menu/context key
    "Mouse 4": "XButton1",
    "Mouse 5": "XButton2",
    "F1": "F1", "F2": "F2", "F3": "F3", "F4": "F4", "F5": "F5", "F6": "F6",
    "F7": "F7", "F8": "F8", "F9": "F9", "F10": "F10", "F11": "F11", "F12": "F12",
    "F13": "F13", "F14": "F14", "F15": "F15", "F16": "F16", "F17": "F17",
    "F18": "F18", "F19": "F19", "F20": "F20", "F21": "F21", "F22": "F22",
    "F23": "F23", "F24": "F24",
}

VOICE_HOTKEY_OPTIONS = []
for _preferred in (
    "Insert",
    "F13", "F14", "F15", "F16", "F17", "F18", "F19", "F20", "F21", "F22", "F23", "F24",
    "Home", "End", "PageUp", "PageDown", "Delete", "Pause", "ScrollLock", "AppsKey",
    "Mouse 4", "Mouse 5",
    "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12",
):
    if _preferred in SAFE_HOTKEYS and _preferred not in VOICE_HOTKEY_OPTIONS:
        VOICE_HOTKEY_OPTIONS.append(_preferred)
for _name in sorted(SAFE_HOTKEYS.keys()):
    if _name not in VOICE_HOTKEY_OPTIONS:
        VOICE_HOTKEY_OPTIONS.append(_name)


def voice_hotkey_label(stored):
    """Friendly label for a stored AHK or display key name."""
    text = str(stored or "Insert").strip()
    if not text:
        return "Insert"
    for display, ahk in SAFE_HOTKEYS.items():
        if text.lower() == display.lower() or text.lower() == ahk.lower():
            return display
    return text


def voice_hotkey_to_ahk(raw):
    """Resolve user input to the AutoHotkey key name used in hotkey registration."""
    return normalize_voice_hotkey(raw)


def normalize_voice_hotkey(raw):
    """Resolve a user-supplied key name to a canonical AHK key. Falls back to
    Insert if the input is empty or unsafe."""
    text = str(raw or "").strip()
    if not text:
        return "Insert"
    # Accept already-canonical AHK names case-insensitively, and common aliases.
    aliases = {
        "pgup": "PgUp", "pageup": "PgUp",
        "pgdn": "PgDn", "pagedown": "PgDn",
        "del": "Delete",
        "ins": "Insert",
        "menu": "AppsKey", "apps": "AppsKey",
        "mouse4": "XButton1", "mouse 4": "XButton1", "xbutton1": "XButton1",
        "mouse5": "XButton2", "mouse 5": "XButton2", "xbutton2": "XButton2",
    }
    lookup = aliases.get(text.lower(), text)
    # Case-insensitive match against SAFE_HOTKEYS keys.
    for display, ahk in SAFE_HOTKEYS.items():
        if display.lower() == lookup.lower() or ahk.lower() == lookup.lower():
            return ahk
    return "Insert"

WHISPER_MODELS = (
    "tiny.en",
    "base.en",
    "small.en",
    "medium.en",
    "tiny",
    "base",
    "small",
    "medium",
    "large-v3",
    # Roughly `small` latency at near-`large` accuracy on a modern GPU — the
    # best default for a CUDA box.
    "large-v3-turbo",
    "distil-large-v3",
)
# Models that only ever emit English, regardless of what was spoken.
ENGLISH_ONLY_MODELS = frozenset(
    name for name in WHISPER_MODELS if name.endswith(".en") or name.startswith("distil-")
)
WHISPER_LANGUAGES = ("auto", "en", "sv")
COMPUTE_TYPES = ("int8", "float16", "float32")
WHISPER_DEVICES = ("cpu", "cuda", "auto")

LANGUAGE_LABELS = {
    "auto": "Auto (SV + EN)",
    "en": "English",
    "sv": "Swedish",
}


def normalize_discord_hotkey(raw):
    text = str(raw or "").strip()
    if not text:
        return ""
    text = re.sub(r"\s*\+\s*", "+", text)
    if "+" not in text:
        text = re.sub(r"\s+", "+", text)
    return text


def resolve_transcribe_language(language):
    lang = str(language or "auto").strip().lower()
    if lang in ("", "auto", "detect"):
        return None
    return lang if lang in WHISPER_LANGUAGES else None


def normalize_whisper_model(model, language):
    model = str(model or "small").strip()
    lang = str(language or "auto").strip().lower()
    if lang in ("sv", "auto") and model in ENGLISH_ONLY_MODELS:
        # An English-only checkpoint would silently translate Swedish speech.
        model = model[:-3] if model.endswith(".en") else "large-v3-turbo"
    if model in WHISPER_MODELS:
        return model
    if lang == "en":
        return "small.en"
    return "small"


def merge_voice_dictation(raw):
    merged = dict(DEFAULT_VOICE_DICTATION)
    if isinstance(raw, dict):
        merged.update(raw)
    hold_raw = raw.get("hold_ms") if isinstance(raw, dict) else None
    if hold_raw is None and isinstance(raw, dict):
        hold_raw = raw.get("double_press_ms")
    merged["hold_ms"] = max(150, min(800, int(hold_raw if hold_raw is not None else merged.get("hold_ms", 280))))
    merged.pop("double_press_ms", None)
    merged.pop("single_press_grace_ms", None)
    merged["chunk_seconds"] = max(0.6, min(4.0, float(merged.get("chunk_seconds", 1.0))))
    merged["silence_flush_seconds"] = max(0.2, min(2.5, float(merged.get("silence_flush_seconds", 0.35))))
    merged["silence_rms"] = max(0.001, min(0.05, float(merged.get("silence_rms", 0.006))))
    merged["typing_delay_ms"] = max(0, min(50, int(merged.get("typing_delay_ms", 0))))
    merged["discord_mute_enabled"] = bool(merged.get("discord_mute_enabled"))
    merged["discord_mute_hotkey"] = normalize_discord_hotkey(merged.get("discord_mute_hotkey"))
    merged["separate_hotkeys"] = bool(merged.get("separate_hotkeys"))
    merged["voice_hotkey"] = normalize_voice_hotkey(merged.get("voice_hotkey"))
    merged["aios_hotkey"] = normalize_voice_hotkey(merged.get("aios_hotkey") or "Insert")
    if merged["separate_hotkeys"] and merged["aios_hotkey"] == merged["voice_hotkey"]:
        # Keep dictate working; pick a different open-aiOS key (never Ctrl+Space).
        merged["aios_hotkey"] = "Insert" if merged["voice_hotkey"] != "Insert" else "Home"
    language = str(merged.get("language") or "auto").strip().lower()
    merged["language"] = language if language in WHISPER_LANGUAGES else "auto"
    merged["whisper_model"] = normalize_whisper_model(merged.get("whisper_model"), merged["language"])
    device = str(merged.get("device") or "cpu").strip().lower()
    merged["device"] = device if device in WHISPER_DEVICES else "cpu"
    compute = str(merged.get("compute_type") or "int8").strip()
    merged["compute_type"] = compute if compute in COMPUTE_TYPES else "int8"
    if merged["device"] == "cpu" and merged["compute_type"] == "float16":
        merged["compute_type"] = "int8"
    reasoning = str(merged.get("agent_reasoning") or "low").strip().lower()
    merged["agent_reasoning"] = reasoning if reasoning in {"minimal", "low", "medium", "high"} else "low"
    merged["agent_max_rounds"] = max(1, min(12, int(merged.get("agent_max_rounds", 6) or 6)))
    merged["agent_memory_minutes"] = max(0, min(240, int(merged.get("agent_memory_minutes", 20) or 0)))
    merged["agent_model"] = str(merged.get("agent_model") or "gpt-5.6-luna").strip() or "gpt-5.6-luna"
    for flag in (
        "agent_web_search", "agent_shell", "agent_operator", "agent_open_apps",
        "agent_tts_enabled", "agent_clipboard_read", "agent_screen", "agent_files",
        "agent_media", "agent_timers", "agent_windows", "agent_remember", "agent_self_edit",
        "agent_shell_guard", "agent_shell_confirm", "agent_persist_memory",
        "hallucination_filter", "transcript_history", "barge_in",
    ):
        merged[flag] = bool(merged.get(flag))
    voice = str(merged.get("agent_tts_voice") or "m3").strip().lower()
    merged["agent_tts_voice"] = voice if voice in {
        "f1", "f2", "f3", "f4", "f5", "m1", "m2", "m3", "m4", "m5"
    } else "m3"
    merged["agent_file_roots"] = normalize_string_list(merged.get("agent_file_roots"))
    try:
        opacity = int(merged.get("overlay_opacity", 85))
    except (TypeError, ValueError):
        opacity = 85
    merged["overlay_opacity"] = max(20, min(100, opacity))
    merged["input_device"] = str(merged.get("input_device") or "").strip()
    merged["vocabulary"] = normalize_string_list(merged.get("vocabulary"))
    merged["replacements"] = normalize_replacements(merged.get("replacements"))
    try:
        # Not `or 0.22` — an explicit 0 must clamp to the floor, not silently
        # fall back to the default.
        floor = float(merged.get("min_speech_seconds", 0.22))
    except (TypeError, ValueError):
        floor = 0.22
    merged["min_speech_seconds"] = max(0.1, min(1.0, floor))
    return merged


def normalize_string_list(raw):
    """Accept a list, or a newline/comma separated string, from the settings UI."""
    if isinstance(raw, str):
        parts = re.split(r"[\n,]+", raw)
    elif isinstance(raw, (list, tuple, set)):
        parts = list(raw)
    else:
        return []
    seen = []
    for part in parts:
        text = str(part or "").strip()
        if text and text not in seen:
            seen.append(text)
    return seen


def normalize_replacements(raw):
    """`{"wrong": "right"}`, tolerating a "wrong=right" line list from the UI."""
    pairs = {}
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, (list, tuple)):
        items = []
        for entry in raw:
            text = str(entry or "")
            if "=" in text:
                left, _, right = text.partition("=")
                items.append((left, right))
    elif isinstance(raw, str):
        # One per line, or comma separated on a single settings-field line.
        items = []
        for line in re.split(r"[\n,]+", raw):
            if "=" in line:
                left, _, right = line.partition("=")
                items.append((left, right))
    else:
        return {}
    for key, value in items:
        source = str(key or "").strip()
        target = str(value or "").strip()
        if source:
            pairs[source] = target
    return pairs


def load_voice_dictation_settings():
    settings = dict(DEFAULT_VOICE_DICTATION)
    if not CONFIG_PATH.exists():
        return merge_voice_dictation(settings)
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as file:
            config = json.load(file)
    except (OSError, json.JSONDecodeError):
        return merge_voice_dictation(settings)
    return merge_voice_dictation(config.get("voice_dictation"))


def save_voice_dictation_settings(settings, config=None):
    merged = merge_voice_dictation(settings)
    if config is None:
        if CONFIG_PATH.exists():
            try:
                with CONFIG_PATH.open("r", encoding="utf-8") as file:
                    config = json.load(file)
            except (OSError, json.JSONDecodeError):
                config = {}
        else:
            config = {}
    config["voice_dictation"] = merged
    try:
        with CONFIG_PATH.open("w", encoding="utf-8") as file:
            json.dump(config, file, indent=2)
    except OSError:
        pass
    return merged
