"""Coder-led CODE team configuration.

The selected coder is the only mandatory model. It owns the turn and may call:

    scout       read-only subagents for bounded repository exploration.
    consultant  a tool-less reasoning model for a hard design or debugging
                decision. It advises from sanitized facts supplied by the
                coder and never claims to have inspected the workspace.

The reviewer key remains readable for old saved configurations, but Auto mode
does not run an automatic pre-plan or post-review pipeline.

Roles live in helper_config.json under `code_roles`. Older installs configured
some of this through separate keys (`code_subagent_model`, `code_review_model`,
`code_review_enabled`, `code_default_model`); those are read once as seeds so an
upgrade keeps the settings it already had.

Nothing here calls a model or touches a session. It is the shape of the
configuration and the rules for reading it, kept apart from the loop so both the
harness and the API can use it without importing each other.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("AIOS_CODE_CONFIG_PATH") or (ROOT / "helper_config.json"))

ROLE_KEYS: tuple[str, ...] = ("coder", "scout", "consultant", "reviewer")

# Roles the operator may switch off. The coder is the session itself, so there
# is no session without it. Scouts are optional because they cost real time
# before any code is written -- switching them off means no subagent
# exploration at all, neither the pre-plan sweep nor the coder's spawn_agent
# tool, which is the only reading of "off" that is not half-on.
OPTIONAL_ROLES: frozenset[str] = frozenset({"consultant", "reviewer", "scout"})

ROLE_META: dict[str, dict[str, str]] = {
    "scout": {
        "label": "Scouts",
        "tagline": "Explore on demand, report evidence",
        "detail": (
            "Read-only subagents the coder may spawn for a bounded search or repository survey. "
            "They show their tool steps and report verified paths back to the coder."
        ),
        "picks": "cheap, long context",
    },
    "consultant": {
        "label": "Consultant",
        "tagline": "Think through the hard part on demand",
        "detail": (
            "A tool-less reasoning model the coder may consult at any point. It receives the current "
            "objective, one focused question, and sanitized verified facts from the coder. It advises; "
            "the coder decides and implements."
        ),
        "picks": "smartest you will pay for",
    },
    "coder": {
        "label": "Coder",
        "tagline": "Read, edit, verify",
        "detail": (
            "The lead agent. It starts every turn, chooses tools, decides whether Scouts or the "
            "Consultant are useful, implements changes, verifies them, and gives the final answer."
        ),
        "picks": "strong and cheap",
    },
    "reviewer": {
        "label": "Reviewer",
        "tagline": "Review risky work; skip tiny direct edits",
        "detail": (
            "Legacy saved-role compatibility. Coder-led Auto mode does not run an automatic reviewer; "
            "use the Consultant when the coder wants a second opinion."
        ),
        "picks": "different from the coder",
    },
}

# Shipped defaults. Deliberately not all the same model: the point of splitting
# the roles is that they get different ones.
DEFAULT_ROLES: dict[str, dict[str, Any]] = {
    "scout": {"enabled": True, "model": "qwen/qwen3.7-flash", "reasoning": "off", "fast": True},
    "consultant": {"enabled": True, "model": "deepseek/deepseek-v4-pro", "reasoning": "high", "fast": False},
    "coder": {"enabled": True, "model": "deepseek/deepseek-v4-flash", "reasoning": "off", "fast": True},
    "reviewer": {"enabled": False, "model": "deepseek/deepseek-v4-pro", "reasoning": "medium", "fast": False},
}

# Where an older install's settings come from, once, when `code_roles` is absent.
_LEGACY_SEEDS: dict[str, dict[str, str]] = {
    "scout": {"model": "code_subagent_model"},
    "coder": {"model": "code_default_model", "reasoning": "code_default_reasoning"},
    "reviewer": {"model": "code_review_model", "enabled": "code_review_enabled"},
}

VALID_REASONING: tuple[str, ...] = ("off", "on", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def _read_config() -> dict[str, Any]:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _clean_role(name: str, raw: Any, base: dict[str, Any]) -> dict[str, Any]:
    """One role, with every field forced back into range."""
    row = raw if isinstance(raw, dict) else {}
    reasoning = str(row.get("reasoning") or base["reasoning"]).strip().casefold()
    if reasoning not in VALID_REASONING:
        reasoning = base["reasoning"]
    enabled = bool(row.get("enabled", base["enabled"]))
    return {
        "role": name,
        # A role that is not optional is always on, whatever the file says.
        "enabled": enabled if name in OPTIONAL_ROLES else True,
        "model": str(row.get("model") or base["model"]).strip() or base["model"],
        "reasoning": reasoning,
        "fast": bool(row.get("fast", base["fast"])),
    }


def load_roles(config: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """The four roles, complete and valid, whatever is in the config file."""
    data = config if config is not None else _read_config()
    stored = data.get("code_roles") if isinstance(data.get("code_roles"), dict) else {}
    roles: dict[str, dict[str, Any]] = {}
    for name in ROLE_KEYS:
        base = dict(DEFAULT_ROLES[name])
        if not stored:
            # First read after an upgrade: inherit whatever was configured
            # before roles existed, so nothing silently changes model.
            for field, key in _LEGACY_SEEDS.get(name, {}).items():
                value = data.get(key)
                if isinstance(value, bool) and field == "enabled":
                    base["enabled"] = value
                elif isinstance(value, str) and value.strip() and field != "enabled":
                    base[field] = value.strip()
        raw = stored.get(name)
        if name == "consultant" and not isinstance(raw, dict):
            raw = stored.get("planner")
        roles[name] = _clean_role(name, raw, base)
    return roles


def save_roles(patch: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Merge a partial update over the current roles and return the result.

    Returns the merged roles; writing them to disk is the caller's job, because
    the UI process owns helper_config.json and this module is imported by both
    sides.
    """
    current = load_roles(config)
    incoming = patch if isinstance(patch, dict) else {}
    if "consultant" not in incoming and isinstance(incoming.get("planner"), dict):
        incoming = {**incoming, "consultant": incoming["planner"]}
    for name in ROLE_KEYS:
        if name not in incoming:
            continue
        merged = dict(current[name])
        merged.update(incoming[name] if isinstance(incoming[name], dict) else {})
        current[name] = _clean_role(name, merged, DEFAULT_ROLES[name])
    return current


def role(name: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    if str(name).strip().casefold() == "planner":
        name = "consultant"
    return load_roles(config).get(name) or dict(DEFAULT_ROLES.get(name) or DEFAULT_ROLES["coder"])


def catalogue() -> list[dict[str, Any]]:
    """The active coder-led team rendered in the Models window."""
    return [
        {
            "role": name,
            "optional": name in OPTIONAL_ROLES,
            **ROLE_META[name],
        }
        for name in ("coder", "scout", "consultant")
    ]


# ---------------------------------------------------------------------------
# Saved model-config presets
# ---------------------------------------------------------------------------

_MODEL_CONFIGS_KEY = "model_configs"
_VALID_PROVIDERS = frozenset({"codex", "claude", "cursor", "ollama", "openrouter"})
_VALID_STRATEGIES = frozenset({"auto", "direct", "planned", "distributed"})
_MODEL_CONFIG_ORIGINS = frozenset({"user", "benchmark_history"})
_LEGACY_BENCH_RECOVERY_DESCRIPTION = "Restored from benchmark history"


def _clean_provider(value: Any) -> str:
    provider = str(value or "openrouter").strip().lower()
    return provider if provider in _VALID_PROVIDERS else "openrouter"


def _clean_strategy(value: Any) -> str:
    strategy = str(value or "auto").strip().lower().replace("-", "_")
    aliases = {"plan": "planned", "team": "distributed", "small": "direct"}
    strategy = aliases.get(strategy, strategy)
    return strategy if strategy in _VALID_STRATEGIES else "auto"


def _preset_id(raw: dict[str, Any]) -> str:
    preset_id = re.sub(r"[^A-Za-z0-9._-]+", "-", str(raw.get("id") or "").strip()).strip("-._")[:64]
    if preset_id:
        return preset_id
    name = str(raw.get("name") or "").strip()
    if not name:
        return ""
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:12]


_BENCH_RECOVERY_CACHE: tuple[float, list[dict[str, Any]]] = (0.0, [])


def _load_stored_model_configs(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Presets persisted in helper_config.json only."""
    data = config if config is not None else _read_config()
    rows = data.get(_MODEL_CONFIGS_KEY) if isinstance(data.get(_MODEL_CONFIGS_KEY), list) else []
    cleaned = [row for item in rows if (row := _clean_model_config(item))]
    cleaned.sort(key=lambda row: float(row.get("updated_at") or 0), reverse=True)
    return cleaned[:100]


def _scan_bench_model_configs() -> list[dict[str, Any]]:
    """Bring back presets that only survived inside benchmark run.json files."""
    try:
        from bench.runs import RUNS_DIR, _read
    except ImportError:
        return []

    seen: set[str] = set()
    recovered: list[dict[str, Any]] = []
    try:
        directories = list(RUNS_DIR.iterdir()) if RUNS_DIR.exists() else []
    except OSError:
        directories = []
    for directory in directories:
        if not directory.is_dir():
            continue
        run = _read(directory / "run.json")
        preset_id = _preset_id({
            "id": run.get("saved_config_id"),
            "name": run.get("saved_config_name"),
        })
        name = str(run.get("saved_config_name") or "").strip()[:80]
        roles_raw = run.get("saved_config_roles")
        if not preset_id or not name or not isinstance(roles_raw, dict):
            continue
        if preset_id in seen:
            continue
        seen.add(preset_id)
        candidate = {
            "id": preset_id,
            "name": name,
            "description": _LEGACY_BENCH_RECOVERY_DESCRIPTION,
            "origin": "benchmark_history",
            "show_in_composer": False,
            "show_in_composer_explicit": False,
            "roles": roles_raw,
            "provider": _clean_provider(run.get("saved_config_provider") or run.get("provider")),
            "strategy": _clean_strategy(
                (run.get("config") or {}).get("strategy") if isinstance(run.get("config"), dict) else None,
            ),
            "review_fix": _clean_review_fix(
                (run.get("config") or {}).get("review_fix") if isinstance(run.get("config"), dict) else None,
            ),
            "created_at": float(run.get("created_at") or run.get("started_at") or 0),
            "updated_at": float(run.get("updated_at") or run.get("started_at") or 0),
        }
        cleaned = _clean_model_config(candidate)
        if cleaned:
            recovered.append(cleaned)
    recovered.sort(key=lambda row: float(row.get("updated_at") or 0), reverse=True)
    return recovered


def _bench_recovered_configs(existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Presets referenced by benchmark runs that are not already stored on disk."""
    global _BENCH_RECOVERY_CACHE
    now = time.time()
    if now - _BENCH_RECOVERY_CACHE[0] >= 300.0:
        _BENCH_RECOVERY_CACHE = (now, _scan_bench_model_configs())
    known = {str(row.get("id")) for row in existing}
    return [
        cleaned
        for raw in _BENCH_RECOVERY_CACHE[1]
        if str(raw.get("id")) not in known
        if (cleaned := _clean_model_config(raw))
    ]


def _clean_review_fix(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _clean_show_in_composer(value: Any) -> bool:
    """Visibility was implicit before this field existed, so missing means on."""
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _clean_model_config(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    preset_id = _preset_id(raw)
    name = str(raw.get("name") or "").strip()[:80]
    if not preset_id or not name:
        return None
    description = str(raw.get("description") or "").strip()[:500]
    raw_origin = str(raw.get("origin") or "").strip().casefold()
    if raw_origin in _MODEL_CONFIG_ORIGINS:
        origin = raw_origin
    elif description == _LEGACY_BENCH_RECOVERY_DESCRIPTION:
        # Older recovery rows predate explicit provenance.  Their visibility
        # bit was synthesized as True merely because the field was absent, so
        # the system description is the one safe migration marker we own.
        origin = "benchmark_history"
    else:
        origin = "user"
    visibility_explicit = _clean_review_fix(raw.get("show_in_composer_explicit"))
    show_in_composer = _clean_show_in_composer(raw.get("show_in_composer"))
    if origin == "benchmark_history" and not visibility_explicit:
        show_in_composer = False
    try:
        created_at = float(raw.get("created_at") or 0)
        updated_at = float(raw.get("updated_at") or created_at or 0)
    except (TypeError, ValueError):
        created_at = updated_at = 0.0
    # A preset is a complete, immutable-at-use snapshot. Filling missing roles
    # here also keeps presets created by early development builds usable.
    roles = save_roles(raw.get("roles") if isinstance(raw.get("roles"), dict) else {}, {})
    return {
        "id": preset_id,
        "name": name,
        "description": description,
        "origin": origin,
        "provider": _clean_provider(raw.get("provider")),
        "strategy": _clean_strategy(raw.get("strategy")),
        "review_fix": _clean_review_fix(raw.get("review_fix")),
        "show_in_composer": show_in_composer,
        "show_in_composer_explicit": visibility_explicit,
        "roles": roles,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def load_model_configs(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return saved model-config presets, newest first."""
    stored = _load_stored_model_configs(config)
    recovered = _bench_recovered_configs(stored)
    if not recovered:
        return stored
    merged = stored + recovered
    merged.sort(key=lambda row: float(row.get("updated_at") or 0), reverse=True)
    return merged[:100]


def merge_recovered_model_configs(config: dict[str, Any]) -> bool:
    """Persist recovered presets and one-time provenance/visibility migration."""
    raw = config.get(_MODEL_CONFIGS_KEY) if isinstance(config.get(_MODEL_CONFIGS_KEY), list) else []
    stored = _load_stored_model_configs(config)
    recovered = _bench_recovered_configs(stored)
    merged = stored + recovered
    merged.sort(key=lambda row: float(row.get("updated_at") or 0), reverse=True)
    merged = merged[:100]
    if raw == merged:
        return False
    config[_MODEL_CONFIGS_KEY] = merged
    return True


def persist_model_configs(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Write the full preset list (including bench recovery) back to *config*."""
    merge_recovered_model_configs(config)
    return _load_stored_model_configs(config)


def save_model_config(
    preset: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Add or update a model-config preset.

    *preset* should contain at minimum ``name`` and ``roles`` (the full
    four-role dict).  If ``id`` is absent a new entry is appended; if present
    the matching entry is updated in place.

    Returns the full updated list; the caller is responsible for writing it.
    """
    data = config if config is not None else _read_config()
    configs = _load_stored_model_configs(data)
    requested_id = re.sub(r"[^A-Za-z0-9._-]+", "-", str(preset.get("id") or "").strip()).strip("-._")[:64]
    preset_id = requested_id or uuid.uuid4().hex[:12]
    now = round(time.time(), 3)
    name = str(preset.get("name") or "").strip()[:80]
    if not name:
        raise ValueError("give the configuration a name")

    entry = {
        "id": preset_id,
        "name": name,
        "description": str(preset.get("description") or "").strip()[:500],
        # A save through the CODE configuration API is an explicit user
        # adoption, including when it promotes a history-only snapshot.
        "origin": "user",
        "provider": _clean_provider(preset.get("provider")),
        "strategy": _clean_strategy(preset.get("strategy")),
        "review_fix": _clean_review_fix(preset.get("review_fix")),
        "show_in_composer": _clean_show_in_composer(preset.get("show_in_composer")),
        "show_in_composer_explicit": "show_in_composer" in preset,
        "roles": save_roles(preset.get("roles") if isinstance(preset.get("roles"), dict) else {}, {}),
        "created_at": now,
        "updated_at": now,
    }

    idx = None
    for i, existing in enumerate(configs):
        if str(existing.get("id")) == preset_id:
            idx = i
            break
    if idx is not None:
        old = configs[idx]
        entry["created_at"] = old.get("created_at", now)
        if not str(preset.get("provider") or "").strip():
            entry["provider"] = _clean_provider(old.get("provider"))
        if "strategy" not in preset:
            entry["strategy"] = _clean_strategy(old.get("strategy"))
        if "review_fix" not in preset:
            entry["review_fix"] = _clean_review_fix(old.get("review_fix"))
        if "show_in_composer" not in preset:
            entry["show_in_composer"] = _clean_show_in_composer(old.get("show_in_composer"))
            entry["show_in_composer_explicit"] = _clean_review_fix(
                old.get("show_in_composer_explicit")
            )
        configs[idx] = entry
    else:
        configs.insert(0, entry)

    return configs


def delete_model_config(preset_id: str, config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Remove a saved model-config preset by id."""
    data = config if config is not None else _read_config()
    configs = _load_stored_model_configs(data)
    configs = [c for c in configs if str(c.get("id")) != str(preset_id)]
    return configs
