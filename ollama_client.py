"""Shared Ollama helpers for aiOS CODE and the voice/agent chat."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterator
from urllib.request import Request, urlopen

API_BASE = os.environ.get("AIOS_OLLAMA_HOST", "http://localhost:11434").rstrip("/")
MODEL_ROOT = os.environ.get("OLLAMA_MODELS") or r"C:\AI\OllamaModels"
KEEP_ALIVE = os.environ.get("AIOS_OLLAMA_KEEP_ALIVE", "1h")
DEFAULT_CHAT_MODEL = "qwen3:14b"
DEFAULT_CODE_MODEL = "qwen3.6-agent:27b"
_MODELS_CACHE: list[dict[str, Any]] | None = None
_MODELS_CACHE_AT = 0.0
_MODELS_CACHE_TTL = float(os.environ.get("AIOS_OLLAMA_MODELS_TTL", "30"))
_STATUS_CACHE: tuple[bool, str] | None = None
_STATUS_CACHE_AT = 0.0
_STATUS_CACHE_TTL = 8.0

# Short parenthetical blurbs shown in model pickers. Matched against the
# installed tag (case-insensitive substring), first hit wins.
_MODEL_BLURBS: tuple[tuple[str, str, int], ...] = (
    ("qwen3-coder", "best at coding", 10),
    ("qwen3.6-agent", "local coding agent", 20),
    ("qwen3.6-27b", "local coding agent", 20),
    ("qwen3-vl:30b", "vision / screen, slower", 30),
    ("qwen3-vl:8b", "fast vision", 40),
    ("qwen3-vl-30b", "vision GGUF (prefer qwen3-vl:30b)", 35),
    ("qwen3-vl-8b", "vision GGUF (prefer qwen3-vl:8b)", 45),
    ("qwen2.5vl", "lightweight vision", 50),
    ("qwen3:14b", "fast chat / reasoning", 60),
    ("qwen3", "local chat", 70),
)

# Prefer snappy defaults when the user has not picked anything yet.
_SPEED_RANK: tuple[tuple[str, int], ...] = (
    ("qwen3:14b", 0),
    ("qwen3-vl:8b", 1),
    ("qwen2.5vl", 2),
    ("qwen3.6-agent", 3),
    ("qwen3-coder", 4),
    ("qwen3-vl:30b", 5),
)


def ollama_exe() -> str:
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
    if local.exists():
        return str(local)
    return "ollama"


def request_json(path: str, payload: dict | None = None, timeout: float = 60) -> dict:
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(API_BASE + path, data=data, headers=headers)
    with urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def stream_json(path: str, payload: dict, timeout: float = 900) -> Iterator[dict]:
    req = Request(
        API_BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(req, timeout=timeout) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if line:
                yield json.loads(line)


def ensure_ollama(timeout: float = 30) -> bool:
    os.environ.setdefault("OLLAMA_MODELS", MODEL_ROOT)
    try:
        request_json("/api/version", timeout=2)
        return True
    except Exception:
        pass

    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
    try:
        subprocess.Popen(
            [ollama_exe(), "serve"],
            env=os.environ.copy(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except Exception:
        return False

    deadline = time.time() + max(3.0, float(timeout))
    while time.time() < deadline:
        try:
            request_json("/api/version", timeout=2)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def provider_status(*, use_cache: bool = True, ensure: bool = True) -> tuple[bool, str]:
    """Report whether Ollama is reachable.

    ensure=True (default) will try to start the daemon when it is down — that
    can take several seconds and is fine for intentional Ollama setup paths.
    Callers that are just painting a UI (Settings meta, model pickers) must
    pass ensure=False so a stopped daemon cannot stall the page.
    """
    global _STATUS_CACHE, _STATUS_CACHE_AT
    now = time.time()
    if use_cache and _STATUS_CACHE and now - _STATUS_CACHE_AT < _STATUS_CACHE_TTL:
        return _STATUS_CACHE
    # Fail fast when we are not allowed to start the daemon — Windows can sit
    # on a connect timeout otherwise, and Settings would stay blank.
    probe_timeout = 2.0 if ensure else 0.35
    try:
        version = request_json("/api/version", timeout=probe_timeout)
        tag = str(version.get("version") or "").strip()
        result = (True, f"Ollama is ready{f' ({tag})' if tag else ''}")
    except Exception:
        if ensure and ensure_ollama(timeout=8):
            try:
                version = request_json("/api/version", timeout=2)
                tag = str(version.get("version") or "").strip()
                result = (True, f"Ollama is ready{f' ({tag})' if tag else ''}")
            except Exception:
                result = (True, "Ollama is ready")
        else:
            result = (False, "Ollama is not running. Install it or start the Ollama app.")
    _STATUS_CACHE = result
    _STATUS_CACHE_AT = now
    return result


def describe_model(name: str) -> str:
    lowered = str(name or "").casefold()
    for needle, blurb, _rank in sorted(_MODEL_BLURBS, key=lambda row: row[2]):
        if needle in lowered:
            return blurb
    if "coder" in lowered:
        return "coding"
    if "vl" in lowered or "vision" in lowered:
        return "vision"
    if "agent" in lowered:
        return "local agent"
    return "local model"


def model_label(name: str, blurb: str | None = None) -> str:
    text = str(name or "").strip()
    note = (blurb if blurb is not None else describe_model(text)).strip()
    if not text:
        return ""
    # Shorten huge Hugging Face tags for the picker while keeping the id exact.
    display = text
    if text.startswith("hf.co/") and ":" in text:
        display = text.rsplit("/", 1)[-1]
    return f"{display} ({note})" if note else display


def _speed_rank(name: str) -> int:
    lowered = str(name or "").casefold()
    for needle, rank in _SPEED_RANK:
        if needle in lowered:
            return rank
    return 80 + len(lowered)


def _short_model_name(name: str) -> str:
    text = str(name or "").strip()
    if text.startswith("hf.co/") and "/" in text:
        text = text.rsplit("/", 1)[-1]
    if len(text) > 42:
        return text[:39] + "..."
    return text


def invalidate_cache() -> None:
    global _MODELS_CACHE, _MODELS_CACHE_AT, _STATUS_CACHE, _STATUS_CACHE_AT
    _MODELS_CACHE = None
    _MODELS_CACHE_AT = 0.0
    _STATUS_CACHE = None
    _STATUS_CACHE_AT = 0.0


def list_installed_models(
    *, ready_only: bool = True, use_cache: bool = True, ensure: bool = True
) -> list[dict[str, Any]]:
    global _MODELS_CACHE, _MODELS_CACHE_AT
    now = time.time()
    if use_cache and _MODELS_CACHE is not None and now - _MODELS_CACHE_AT < _MODELS_CACHE_TTL:
        return list(_MODELS_CACHE)
    if ready_only:
        ready, message = provider_status(use_cache=use_cache, ensure=ensure)
        if not ready:
            return []
    else:
        message = ""
    try:
        # Settings / pickers pass ensure=False; keep the tags probe short too.
        tags = request_json("/api/tags", timeout=8 if ensure else 0.8)
    except Exception as exc:
        if message:
            raise RuntimeError(message) from exc
        raise
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in tags.get("models") or []:
        name = str(item.get("name") or item.get("model") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        blurb = describe_model(name)
        size = item.get("size")
        short = _short_model_name(name)
        models.append(
            {
                "id": name,
                "label": model_label(name, blurb),
                "short_label": f"{short} ({blurb})" if blurb else short,
                "description": blurb,
                "size": size,
                "reasoning": ["off", "low", "medium", "high"],
                "default_reasoning": "off",
                "fast": _speed_rank(name) <= 2,
                "default": False,
                "input_modalities": (
                    ["text", "image"]
                    if any(token in name.casefold() for token in ("vl", "vision"))
                    else ["text"]
                ),
            }
        )
    models.sort(key=lambda row: (_speed_rank(str(row["id"])), str(row["id"]).casefold()))
    if models:
        preferred = next(
            (row for row in models if DEFAULT_CODE_MODEL in str(row["id"])),
            next((row for row in models if DEFAULT_CHAT_MODEL in str(row["id"])), models[0]),
        )
        preferred["default"] = True
    _MODELS_CACHE = list(models)
    _MODELS_CACHE_AT = now
    return models


def capabilities() -> dict[str, Any]:
    ready, message = provider_status()
    data: dict[str, Any] = {
        "provider": "ollama",
        "ready": ready,
        "message": message,
        "models": [],
    }
    if not ready:
        return data
    try:
        data["models"] = list_installed_models(ready_only=False)
        if not data["models"]:
            data["message"] = "Ollama is running but no models are installed. Run `ollama pull qwen3:14b`."
        else:
            data["message"] = f"Ollama ready · {len(data['models'])} local model{'s' if len(data['models']) != 1 else ''}"
    except Exception as exc:
        data["ready"] = False
        data["message"] = f"Ollama model discovery failed: {exc}"
    return data


def think_setting(model: str, reasoning: str | bool) -> bool | str:
    """Map aiOS reasoning levels onto Ollama's think flag."""
    if isinstance(reasoning, bool):
        enabled = reasoning
    else:
        enabled = str(reasoning or "").strip().lower() not in {"", "off", "none", "false", "0"}
    if str(model or "").casefold().startswith("gpt-oss"):
        return "medium" if enabled else "low"
    return bool(enabled)


def is_ollama_model_id(model: str, installed: set[str] | None = None) -> bool:
    text = str(model or "").strip()
    if not text:
        return False
    if text.lower().startswith("ollama:"):
        return True
    if installed is not None:
        return text in installed
    try:
        return any(row["id"] == text for row in list_installed_models())
    except Exception:
        return False


def strip_ollama_prefix(model: str) -> str:
    text = str(model or "").strip()
    if text.lower().startswith("ollama:"):
        return text.split(":", 1)[1].strip()
    return text


def chat(
    messages: list[dict],
    model: str,
    *,
    reasoning: str | bool = "off",
    tools: list[dict] | None = None,
    options: dict | None = None,
    timeout: float = 900,
) -> dict:
    payload: dict[str, Any] = {
        "model": strip_ollama_prefix(model),
        "messages": messages,
        "stream": False,
        "keep_alive": KEEP_ALIVE,
        "think": think_setting(model, reasoning),
        "options": options or {"num_ctx": 8192, "temperature": 0.4},
    }
    if tools:
        payload["tools"] = tools
    return request_json("/api/chat", payload, timeout=timeout)


def stream_chat(
    messages: list[dict],
    model: str,
    *,
    reasoning: str | bool = "off",
    tools: list[dict] | None = None,
    options: dict | None = None,
    timeout: float = 900,
) -> Iterator[dict]:
    payload: dict[str, Any] = {
        "model": strip_ollama_prefix(model),
        "messages": messages,
        "stream": True,
        "keep_alive": KEEP_ALIVE,
        "think": think_setting(model, reasoning),
        "options": options or {"num_ctx": 8192, "temperature": 0.4},
    }
    if tools:
        payload["tools"] = tools
    yield from stream_json("/api/chat", payload, timeout=timeout)


def agent_model_choices(cloud: list[str] | None = None) -> list[str]:
    """Labels used by the Agent chat picker: cloud models + local Ollama tags."""
    choices: list[str] = []
    for name in cloud or ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"):
        choices.append(str(name))
    try:
        for row in list_installed_models():
            choices.append(f"ollama:{row['id']}")
    except Exception:
        pass
    return choices


def agent_model_display(model: str) -> str:
    text = str(model or "").strip()
    if text.lower().startswith("ollama:"):
        raw = strip_ollama_prefix(text)
        return model_label(raw)
    return text
