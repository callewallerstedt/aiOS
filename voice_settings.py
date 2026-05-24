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
    "compute_type": "int8",
    "chunk_seconds": 1.6,
    "silence_flush_seconds": 0.7,
    "silence_rms": 0.006,
    "typing_delay_ms": 0,
    "discord_mute_enabled": False,
    "discord_mute_hotkey": "",
    "voice_hotkey": "Insert",
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
    "F1": "F1", "F2": "F2", "F3": "F3", "F4": "F4", "F5": "F5", "F6": "F6",
    "F7": "F7", "F8": "F8", "F9": "F9", "F10": "F10", "F11": "F11", "F12": "F12",
    "F13": "F13", "F14": "F14", "F15": "F15", "F16": "F16", "F17": "F17",
    "F18": "F18", "F19": "F19", "F20": "F20", "F21": "F21", "F22": "F22",
    "F23": "F23", "F24": "F24",
}


def normalize_voice_hotkey(raw):
    """Resolve a user-supplied key name to a canonical AHK key. Falls back to
    Insert if the input is empty or unsafe."""
    text = str(raw or "").strip()
    if not text:
        return "Insert"
    # Accept already-canonical AHK names case-insensitively, and common aliases.
    aliases = {
        "pgup": "PageUp", "pageup": "PageUp",
        "pgdn": "PageDown", "pagedown": "PageDown",
        "del": "Delete",
        "ins": "Insert",
        "menu": "AppsKey", "apps": "AppsKey",
    }
    lookup = aliases.get(text.lower(), text)
    # Case-insensitive match against SAFE_HOTKEYS keys.
    for display, ahk in SAFE_HOTKEYS.items():
        if display.lower() == lookup.lower() or ahk.lower() == lookup.lower():
            return display
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
)
WHISPER_LANGUAGES = ("auto", "en", "sv")
COMPUTE_TYPES = ("int8", "float16", "float32")

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
    if lang in ("sv", "auto") and model.endswith(".en"):
        model = model[:-3]
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
    merged["chunk_seconds"] = max(0.8, min(4.0, float(merged.get("chunk_seconds", 1.6))))
    merged["silence_flush_seconds"] = max(0.3, min(2.5, float(merged.get("silence_flush_seconds", 0.7))))
    merged["silence_rms"] = max(0.001, min(0.05, float(merged.get("silence_rms", 0.006))))
    merged["typing_delay_ms"] = max(0, min(50, int(merged.get("typing_delay_ms", 0))))
    merged["discord_mute_enabled"] = bool(merged.get("discord_mute_enabled"))
    merged["discord_mute_hotkey"] = normalize_discord_hotkey(merged.get("discord_mute_hotkey"))
    merged["voice_hotkey"] = normalize_voice_hotkey(merged.get("voice_hotkey"))
    language = str(merged.get("language") or "auto").strip().lower()
    merged["language"] = language if language in WHISPER_LANGUAGES else "auto"
    merged["whisper_model"] = normalize_whisper_model(merged.get("whisper_model"), merged["language"])
    compute = str(merged.get("compute_type") or "int8").strip()
    merged["compute_type"] = compute if compute in COMPUTE_TYPES else "int8"
    return merged


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
