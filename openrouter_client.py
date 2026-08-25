"""OpenRouter helpers for aiOS CODE.

API key and enabled-model selection live in helper_config.json (Settings → Models).
Only enabled catalog entries appear when the OpenRouter provider is selected in CODE.
"""

from __future__ import annotations

import json
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "helper_config.json"
MODEL_CACHE_PATH = ROOT / "code_jobs" / "openrouter_models.json"
API_BASE = os.environ.get("AIOS_OPENROUTER_BASE", "https://openrouter.ai/api/v1").rstrip("/")
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
HTTP_REFERER = os.environ.get("AIOS_OPENROUTER_REFERER", "https://aios.local")
APP_TITLE = os.environ.get("AIOS_OPENROUTER_TITLE", "aiOS")

# Catalog shown in Settings. Enable entries there to surface them in the CODE picker.
MODEL_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "deepseek/deepseek-v4-flash",
        "label": "DeepSeek V4 Flash",
        "description": "Fast MoE coding / chat · 1M context",
        "reasoning": ["off", "low", "medium", "high", "xhigh"],
        "default_reasoning": "off",
        "fast": True,
        "default": True,
        "input_modalities": ["text"],
    },
    {
        "id": "moonshotai/kimi-k3",
        "label": "Kimi K3",
        "description": "Moonshot agentic coding model",
        "reasoning": ["off", "low", "medium", "high"],
        "default_reasoning": "medium",
        "fast": False,
        "default": False,
        "input_modalities": ["text"],
    },
    {
        "id": "moonshotai/kimi-k2.7-code",
        "label": "Kimi K2.7 Code",
        "description": "Moonshot code-specialised model",
        "reasoning": ["off", "low", "medium", "high"],
        "default_reasoning": "medium",
        "fast": False,
        "default": False,
        "input_modalities": ["text"],
    },
    # Cheap, fast models for the work that is looking rather than deciding:
    # sweeping a repo, reading logs, classifying a request, summarising a diff.
    # Routing that work away from a frontier model is where most of the cost of
    # an agent run actually goes.
    {
        "id": "google/gemini-3.5-flash-lite",
        "label": "Gemini 3.5 Flash Lite",
        "description": "Cheapest sweep · repo search, log triage, summaries",
        "reasoning": ["off", "low"],
        "default_reasoning": "off",
        "fast": True,
        "default": False,
        "scout": True,
        "input_modalities": ["text", "image"],
    },
    {
        "id": "google/gemini-3.6-flash",
        "label": "Gemini 3.6 Flash",
        "description": "Fast long-context reader · large files and transcripts",
        "reasoning": ["off", "low", "medium"],
        "default_reasoning": "off",
        "fast": True,
        "default": False,
        "scout": True,
        "input_modalities": ["text", "image"],
    },
    {
        "id": "qwen/qwen3-coder",
        "label": "Qwen3 Coder",
        "description": "Cheap code-specialised model · mechanical edits",
        "reasoning": ["off", "low", "medium"],
        "default_reasoning": "off",
        "fast": True,
        "default": False,
        "scout": True,
        "input_modalities": ["text"],
    },
    {
        "id": "qwen/qwen3.8-max",
        "label": "Qwen3.8 Max",
        "description": "Qwen3.8 flagship · coding, reasoning, agentic work · 1M context",
        "reasoning": ["off", "low", "medium", "high"],
        "default_reasoning": "medium",
        "fast": False,
        "default": False,
        "planner": True,
        "input_modalities": ["text", "image"],
    },
    {
        "id": "deepseek/deepseek-v4-pro",
        "label": "DeepSeek V4 Pro",
        "description": "DeepSeek flagship · deep reasoning and review · 1M context",
        "reasoning": ["off", "low", "medium", "high", "xhigh"],
        "default_reasoning": "medium",
        "fast": False,
        "default": False,
        "planner": True,
        "input_modalities": ["text"],
    },
    {
        "id": "qwen/qwen3.7-flash",
        "label": "Qwen3.7 Flash",
        "description": "Very cheap 1M-context sweeper · scouting and summaries",
        "reasoning": ["off", "low", "medium"],
        "default_reasoning": "off",
        "fast": True,
        "default": False,
        "scout": True,
        "input_modalities": ["text"],
    },
    {
        "id": "qwen/qwen3.7-plus",
        "label": "Qwen3.7 Plus",
        "description": "Mid-tier Qwen · cheap coder with room for judgement",
        "reasoning": ["off", "low", "medium", "high"],
        "default_reasoning": "low",
        "fast": True,
        "default": False,
        "input_modalities": ["text"],
    },
    {
        "id": "google/gemini-3.1-pro-preview",
        "label": "Gemini 3.1 Pro",
        "description": "Google flagship · strong planning and long-context review",
        "reasoning": ["off", "low", "medium", "high"],
        "default_reasoning": "medium",
        "fast": False,
        "default": False,
        "planner": True,
        "input_modalities": ["text", "image"],
    },
)

# Models worth suggesting for the role that decides rather than types. Data, not
# a hard-coded id, for the same reason as SCOUT_MODELS below.
PLANNER_MODELS: tuple[str, ...] = tuple(
    str(row["id"]) for row in MODEL_CATALOG if row.get("planner")
)

# The subset worth pointing an exploration subagent at. Kept as data rather
# than a hard-coded id so adding a cheaper model later needs no code change.
SCOUT_MODELS: tuple[str, ...] = tuple(
    str(row["id"]) for row in MODEL_CATALOG if row.get("scout")
)

_STATUS_CACHE: tuple[bool, str] | None = None
_STATUS_CACHE_AT = 0.0
_STATUS_CACHE_TTL = 8.0
_MODEL_CACHE: list[dict[str, Any]] | None = None
_MODEL_CACHE_AT = 0.0
_MODEL_CACHE_TTL = 900.0
_BALANCE_CACHE: dict[str, Any] | None = None
_BALANCE_CACHE_AT = 0.0
_BALANCE_CACHE_TTL = 45.0


def invalidate_cache() -> None:
    global _STATUS_CACHE, _STATUS_CACHE_AT, _MODEL_CACHE, _MODEL_CACHE_AT
    global _BALANCE_CACHE, _BALANCE_CACHE_AT
    _STATUS_CACHE = None
    _STATUS_CACHE_AT = 0.0
    _MODEL_CACHE = None
    _MODEL_CACHE_AT = 0.0
    _BALANCE_CACHE = None
    _BALANCE_CACHE_AT = 0.0


def _load_config() -> dict[str, Any]:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def get_api_key(*, config: dict[str, Any] | None = None) -> str:
    data = config if config is not None else _load_config()
    stored = str((data or {}).get("openrouter_api_key") or "").strip()
    if stored:
        return stored
    return str(os.environ.get("OPENROUTER_API_KEY") or "").strip()


def enabled_model_ids(*, config: dict[str, Any] | None = None) -> list[str]:
    data = config if config is not None else _load_config()
    raw = (data or {}).get("openrouter_enabled_models")
    if isinstance(raw, list) and raw:
        chosen = [str(item).strip() for item in raw if str(item).strip()]
        if chosen:
            return list(dict.fromkeys(chosen))
    # First install: ship with the default catalog entry enabled.
    return [str(row["id"]) for row in MODEL_CATALOG if row.get("default")] or [DEFAULT_MODEL]


def _normalize_model(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict) or not str(row.get("id") or "").strip():
        return None
    supported = {str(value) for value in row.get("supported_parameters") or []}
    if supported and "tools" not in supported:
        return None
    model_id = str(row["id"]).strip()
    context = int(row.get("context_length") or (row.get("top_provider") or {}).get("context_length") or 0)
    reasoning_info = row.get("reasoning") if isinstance(row.get("reasoning"), dict) else {}
    supports_effort = "reasoning_effort" in supported
    supports_reasoning_toggle = "reasoning" in supported
    advertised_efforts = [
        ("off" if str(value).strip().casefold() == "none" else str(value).strip().casefold())
        for value in reasoning_info.get("supported_efforts") or []
        if str(value).strip()
    ]
    if supports_effort or advertised_efforts:
        reasoning = list(dict.fromkeys(advertised_efforts or ["low", "medium", "high", "xhigh"]))
        if not reasoning_info.get("mandatory") and "off" not in reasoning:
            reasoning.insert(0, "off")
    elif supports_reasoning_toggle:
        # A generic `reasoning` capability does not promise support for the
        # `reasoning.effort` enum. Expose the toggle the provider actually
        # advertises instead of inventing four unsupported effort levels.
        reasoning = ["on"] if reasoning_info.get("mandatory") else ["off", "on"]
    else:
        reasoning = ["off"]
    advertised_default = str(reasoning_info.get("default_effort") or "").strip().casefold()
    if advertised_default == "none":
        advertised_default = "off"
    default_reasoning = (
        advertised_default
        if advertised_default in reasoning
        else (
            "off" if "off" in reasoning
            else ("medium" if "medium" in reasoning else reasoning[0])
        )
    )
    description = str(row.get("description") or "").strip().split("\n", 1)[0]
    if context:
        description = f"{context // 1000:,}K context" + (f" · {description}" if description else "")
    return {
        "id": model_id,
        "label": str(row.get("name") or model_id),
        "short_label": str(row.get("name") or model_id),
        "description": description[:220],
        "reasoning": reasoning,
        "default_reasoning": default_reasoning,
        # OpenRouter's dynamic :nitro routing is supported on every model.
        # aiOS sends the equivalent provider.sort=throughput request setting.
        "fast": True,
        "default": model_id == DEFAULT_MODEL,
        "input_modalities": list((row.get("architecture") or {}).get("input_modalities") or ["text"]),
        "context_length": context,
        "pricing": row.get("pricing") or {},
        "supported_parameters": sorted(supported),
        "benchmarks": row.get("benchmarks") if isinstance(row.get("benchmarks"), dict) else {},
    }


def _endpoint_average_tps(model_id: str) -> float | None:
    """Average the current provider p50 throughput values OpenRouter reports.

    The endpoint API exposes rolling 30-minute p50 throughput per provider,
    not a network-wide arithmetic mean. Averaging those observed provider
    medians gives the picker a useful, honestly-labelled comparison number.
    """
    try:
        response = _request_json(f"/models/{model_id}/endpoints", None, get_api_key(), 10)
    except Exception:
        return None
    values: list[float] = []
    data = response.get("data") or {}
    if not isinstance(data, dict):
        return None
    for endpoint in (data.get("endpoints") or []):
        if not isinstance(endpoint, dict) or str(endpoint.get("status") or "") == "offline":
            continue
        try:
            value = float(((endpoint.get("throughput_last_30m") or {}).get("p50")))
        except (TypeError, ValueError):
            continue
        if value > 0:
            values.append(value)
    return round(statistics.fmean(values), 1) if values else None


def _live_model_rankings(limit: int = 24) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, int], dict[str, float]]:
    """Fetch official weekly popularity, throughput order, and popular-model TPS."""
    popular = _request_json("/models?supported_parameters=tools&sort=most-popular", None, get_api_key(), 15).get("data") or []
    throughput = _request_json("/models?supported_parameters=tools&sort=throughput-high-to-low", None, get_api_key(), 15).get("data") or []
    popular_rows = [row for row in popular if isinstance(row, dict) and row.get("id")]
    popularity_rank = {str(row["id"]): index + 1 for index, row in enumerate(popular_rows)}
    throughput_rank = {
        str(row["id"]): index + 1
        for index, row in enumerate(throughput)
        if isinstance(row, dict) and row.get("id")
    }
    model_ids = [str(row["id"]) for row in popular_rows[:max(1, min(int(limit), 40))]]
    average_tps: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="openrouter-metrics") as pool:
        pending = {pool.submit(_endpoint_average_tps, model_id): model_id for model_id in model_ids}
        for future in as_completed(pending):
            try:
                value = future.result()
            except Exception:
                value = None
            if value is not None:
                average_tps[pending[future]] = value
    return popular_rows, popularity_rank, throughput_rank, average_tps


def _read_model_cache() -> list[dict[str, Any]]:
    try:
        payload = json.loads(MODEL_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = payload.get("models") or [] if isinstance(payload, dict) else []
    return [row for row in rows if isinstance(row, dict) and row.get("id")]


def catalog_models(*, refresh: bool = False, limit: int = 80) -> list[dict[str, Any]]:
    """Return current tool-capable OpenRouter models with an offline cache."""
    global _MODEL_CACHE, _MODEL_CACHE_AT
    now = time.time()
    if _MODEL_CACHE is None:
        _MODEL_CACHE = _read_model_cache()
    if refresh:
        try:
            popular, popularity_rank, throughput_rank, average_tps = _live_model_rankings()
            fresh = []
            for source in popular:
                row = _normalize_model(source)
                if row:
                    model_id = str(row["id"])
                    row["popularity_rank"] = popularity_rank.get(model_id)
                    row["throughput_rank"] = throughput_rank.get(model_id)
                    row["openrouter_average_tps"] = average_tps.get(model_id)
                    fresh.append(row)
            if fresh:
                _MODEL_CACHE = fresh
                _MODEL_CACHE_AT = now
                MODEL_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
                temp = MODEL_CACHE_PATH.with_suffix(".tmp")
                temp.write_text(json.dumps({"updated_at": now, "models": fresh}, ensure_ascii=False, indent=2), encoding="utf-8")
                temp.replace(MODEL_CACHE_PATH)
        except Exception:
            # The static default and last successful cache keep CODE usable
            # during an OpenRouter outage or before the first API-key setup.
            pass
    merged: dict[str, dict[str, Any]] = {str(row["id"]): dict(row) for row in MODEL_CATALOG}
    for row in _MODEL_CACHE or []:
        model_id = str(row["id"])
        live = dict(row)
        curated = merged.get(model_id)
        if curated:
            # Live data wins on the facts OpenRouter owns -- price, context,
            # modalities. The curated entry keeps the judgements it exists for:
            # whether fast mode works, and which roles the model is good at.
            # Overwriting wholesale silently disabled fast mode on every model
            # the moment the catalog refreshed.
            live.update({
                key: curated[key]
                for key in ("label", "short_label", "scout", "planner")
                if key in curated
            })
            curated_default = str(curated.get("default_reasoning") or "")
            if curated_default in (live.get("reasoning") or []):
                live["default_reasoning"] = curated_default
            # Curated entries carry `label` only. Without this the live
            # short_label survives and wins downstream, so the picker showed
            # "DeepSeek: DeepSeek V4 Flash 0423" for an entry named to be read.
            if "label" in curated:
                live["short_label"] = curated["label"]
        merged[model_id] = live
    rows = list(merged.values())
    # :nitro is a dynamic routing variant supported by every OpenRouter model.
    for row in rows:
        row["fast"] = True
    rows.sort(key=lambda row: (str(row.get("id")) != DEFAULT_MODEL, -int(row.get("context_length") or 0), str(row.get("label") or "").casefold()))
    return rows[:max(1, min(int(limit or 80), 250))]


def list_enabled_models(*, config: dict[str, Any] | None = None, refresh: bool = False) -> list[dict[str, Any]]:
    enabled = enabled_model_ids(config=config)
    models: list[dict[str, Any]] = []
    known = {str(row["id"]): row for row in catalog_models(refresh=refresh, limit=250)}
    for model_id in enabled:
        row = known.get(model_id) or {
            "id": model_id,
            "label": model_id,
            "description": "Configured OpenRouter tool model",
            "reasoning": ["off", "low", "medium", "high", "xhigh"],
            "default_reasoning": "medium",
            "fast": False,
            "input_modalities": ["text"],
        }
        item = dict(row)
        item["short_label"] = item.get("label") or item["id"]
        models.append(item)
    if models:
        # Exactly one default among the enabled set.
        for item in models:
            item["default"] = False
        preferred = next((item for item in models if item["id"] == DEFAULT_MODEL), models[0])
        preferred["default"] = True
    return models


def _per_million(value: Any) -> float | None:
    """OpenRouter quotes dollars per token. Nobody reads that; convert once.

    Returned as dollars per million tokens, or None when the field is absent --
    never as 0.0, which the UI would render as "free".
    """
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return round(price * 1_000_000, 4)


def _good_for(row: dict[str, Any]) -> list[str]:
    """Compact strengths grounded in OpenRouter metadata and benchmark fields."""
    tags: list[str] = []
    benchmarks = row.get("benchmarks") if isinstance(row.get("benchmarks"), dict) else {}
    aa = benchmarks.get("artificial_analysis") if isinstance(benchmarks.get("artificial_analysis"), dict) else {}
    try:
        coding = float(aa.get("coding_index") or 0)
        agentic = float(aa.get("agentic_index") or 0)
        intelligence = float(aa.get("intelligence_index") or 0)
    except (TypeError, ValueError):
        coding = agentic = intelligence = 0
    haystack = f"{row.get('id', '')} {row.get('description', '')}".casefold()
    if coding >= 50 or any(word in haystack for word in ("coding", "coder", "software", "code-special")):
        tags.append("coding")
    if agentic >= 35:
        tags.append("agents")
    if intelligence >= 45 or "reasoning" in haystack:
        tags.append("reasoning")
    if any(modality in {"image", "file"} for modality in row.get("input_modalities") or []):
        tags.append("vision")
    if int(row.get("context_length") or 0) >= 500_000:
        tags.append("long context")
    if row.get("scout"):
        tags.insert(0, "scouting")
    if row.get("planner"):
        tags.insert(0, "planning")
    return list(dict.fromkeys(tags))[:3]


def model_specs(*, config: dict[str, Any] | None = None, refresh: bool = False,
                limit: int = 250) -> list[dict[str, Any]]:
    """Every selectable model with the numbers needed to choose between them.

    Pricing is whatever OpenRouter last reported, converted to dollars per
    million tokens and left as None when it did not report any. It is never
    estimated: a made-up price is worse than a blank, because a blank is
    obviously missing and a wrong number gets budgeted against.
    """
    enabled = set(enabled_model_ids(config=config))
    rows: list[dict[str, Any]] = []
    for row in catalog_models(refresh=refresh, limit=limit):
        pricing = row.get("pricing") or {}
        model_id = str(row["id"])
        spec = {
            "id": model_id,
            "label": str(row.get("short_label") or row.get("label") or model_id),
            "description": str(row.get("description") or ""),
            "context_length": int(row.get("context_length") or 0),
            "reasoning": [str(item) for item in (row.get("reasoning") or ["off"])],
            "default_reasoning": str(row.get("default_reasoning") or "off"),
            "fast": bool(row.get("fast")),
            "input_modalities": list(row.get("input_modalities") or ["text"]),
            "enabled": model_id in enabled,
            "scout": model_id in SCOUT_MODELS,
            "planner": model_id in PLANNER_MODELS,
            "price_in": _per_million(pricing.get("prompt")),
            "price_out": _per_million(pricing.get("completion")),
            "price_cached_in": _per_million(pricing.get("input_cache_read")),
            "popularity_rank": row.get("popularity_rank"),
            "throughput_rank": row.get("throughput_rank"),
            "openrouter_average_tps": row.get("openrouter_average_tps"),
            "benchmarks": row.get("benchmarks") if isinstance(row.get("benchmarks"), dict) else {},
        }
        spec["good_for"] = _good_for({**row, **spec})
        rows.append(spec)
    rows.sort(key=lambda row: (
        not row["enabled"],
        row["price_in"] if row["price_in"] is not None else 1e9,
        row["label"].casefold(),
    ))
    return rows


def provider_status(*, use_cache: bool = True, config: dict[str, Any] | None = None) -> tuple[bool, str]:
    global _STATUS_CACHE, _STATUS_CACHE_AT
    now = time.time()
    if use_cache and _STATUS_CACHE and now - _STATUS_CACHE_AT < _STATUS_CACHE_TTL:
        return _STATUS_CACHE
    key = get_api_key(config=config)
    if not key:
        result = (False, "Add your OpenRouter API key in Settings -> Models.")
    else:
        models = list_enabled_models(config=config)
        if not models:
            result = (False, "Enable at least one OpenRouter model in Settings -> Models.")
        else:
            result = (True, f"OpenRouter ready · {len(models)} model{'s' if len(models) != 1 else ''}")
    _STATUS_CACHE = result
    _STATUS_CACHE_AT = now
    return result


def credit_balance(*, refresh: bool = False, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the authenticated OpenRouter account's remaining USD credits."""
    global _BALANCE_CACHE, _BALANCE_CACHE_AT
    now = time.time()
    if not refresh and _BALANCE_CACHE and now - _BALANCE_CACHE_AT < _BALANCE_CACHE_TTL:
        return dict(_BALANCE_CACHE)

    key = get_api_key(config=config)
    if not key:
        return {"ok": False, "error": "Add your OpenRouter API key in Settings -> Models."}
    try:
        payload = _request_json("/credits", None, key, 12)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise RuntimeError("OpenRouter returned no credit data.")
        purchased = float(data["total_credits"])
        used = float(data["total_usage"])
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        return {"ok": False, "error": str(exc)}

    result = {
        "ok": True,
        "currency": "USD",
        "balance": purchased - used,
        "total_credits": purchased,
        "total_usage": used,
    }
    _BALANCE_CACHE = result
    _BALANCE_CACHE_AT = now
    return dict(result)


def capabilities(*, config: dict[str, Any] | None = None) -> dict[str, Any]:
    ready, message = provider_status(use_cache=False, config=config)
    return {
        "provider": "openrouter",
        "ready": ready,
        "message": message,
        "models": list_enabled_models(config=config, refresh=ready) if ready else [],
    }


def headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": HTTP_REFERER,
        "X-Title": APP_TITLE,
    }


def _reasoning_control(model: str) -> str:
    """Return the reasoning control the live model metadata actually supports."""
    model_id = str(model or "").strip()
    cached = _MODEL_CACHE if _MODEL_CACHE is not None else _read_model_cache()
    for row in cached:
        if str(row.get("id") or "") != model_id:
            continue
        supported = {str(value) for value in row.get("supported_parameters") or []}
        if not supported:
            return "effort"
        if "reasoning_effort" in supported:
            return "effort"
        if "reasoning" in supported:
            return "toggle"
        return "none"
    # Curated and manually configured models predate live capability metadata.
    # Keep their established effort behavior until OpenRouter provides facts.
    return "effort"


def reasoning_payload(model: str, reasoning: str | bool) -> dict[str, Any] | None:
    """Map aiOS reasoning onto the model's advertised OpenRouter control."""
    control = _reasoning_control(model)
    if control == "none":
        return None
    if isinstance(reasoning, bool):
        enabled = reasoning
        if control == "toggle":
            return {"enabled": enabled}
        if not enabled:
            return {"effort": "none"}
        effort = "high"
    else:
        level = str(reasoning or "").strip().lower()
        enabled = level not in {"", "off", "none", "false", "0"}
        if control == "toggle":
            return {"enabled": enabled}
        if level in {"", "off", "none", "false", "0"}:
            # Omitting the field means provider default, which can still emit
            # large reasoning traces.  "Off" must be an explicit request.
            return {"effort": "none"}
        if level in {"minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
            # The picker exposes only efforts from live model metadata. Preserve
            # that exact contract: minimal/ultra are distinct provider values,
            # not aliases for low/xhigh.
            effort = level
        else:
            effort = "medium"
    return {"effort": effort}


def web_search_plugins(web_search: Any, *, max_results: int = 5) -> list[dict[str, Any]]:
    """Build OpenRouter's `plugins` entry for web search.

    OpenRouter exposes search through the `web` plugin (or an `:online` model
    suffix); there is no top-level `web_search` request field.
    """
    if isinstance(web_search, list):
        return web_search
    if isinstance(web_search, dict):
        plugin = {"id": "web", **web_search}
        return [plugin]
    return [{"id": "web", "max_results": max(1, min(int(max_results), 20))}]


def _request_json(path: str, payload: dict | None, api_key: str, timeout: float) -> dict:
    data = None
    hdrs = headers(api_key)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = Request(f"{API_BASE}{path}", data=data, headers=hdrs, method="POST" if data else "GET")
    try:
        with urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(detail).get("error", {}).get("message", detail)
        except json.JSONDecodeError:
            message = detail or str(exc)
        raise RuntimeError(message) from exc
    except URLError as exc:
        raise RuntimeError(f"Network error: {exc.reason}") from exc


def chat(
    messages: list[dict],
    model: str,
    *,
    api_key: str | None = None,
    reasoning: str | bool = "off",
    tools: list[dict] | None = None,
    temperature: float = 0.35,
    timeout: float = 900,
    web_search: bool = False,
    fast: bool = False,
    max_completion_tokens: int | None = None,
    session_id: str = "",
) -> dict:
    key = (api_key or get_api_key()).strip()
    if not key:
        raise RuntimeError("OpenRouter API key is missing.")
    payload: dict[str, Any] = {
        "model": str(model or DEFAULT_MODEL).strip(),
        "messages": messages,
        "temperature": temperature,
    }
    sticky = str(session_id or "").strip()[:256]
    if sticky:
        # OpenRouter uses this as the conversation's sticky-routing key.  That
        # keeps agent rounds on one endpoint so implicit provider prompt caches
        # can reuse the stable prefix instead of warming again every tool call.
        payload["session_id"] = sticky
    effort = reasoning_payload(model, reasoning)
    if effort:
        payload["reasoning"] = effort
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if web_search:
        payload["plugins"] = web_search_plugins(web_search)
    if fast:
        payload["provider"] = {"sort": "throughput"}
    if max_completion_tokens is not None:
        limit = int(max_completion_tokens)
        if limit <= 0:
            raise ValueError("max_completion_tokens must be positive")
        payload["max_completion_tokens"] = limit
    return _request_json("/chat/completions", payload, key, timeout)


def stream_chat(
    messages: list[dict],
    model: str,
    *,
    api_key: str | None = None,
    reasoning: str | bool = "off",
    tools: list[dict] | None = None,
    temperature: float = 0.35,
    timeout: float = 900,
    web_search: bool = False,
    fast: bool = False,
    max_completion_tokens: int | None = None,
    session_id: str = "",
) -> Iterator[dict]:
    """Yield OpenAI-style SSE chunks, then a final assembled message dict."""
    key = (api_key or get_api_key()).strip()
    if not key:
        raise RuntimeError("OpenRouter API key is missing.")
    payload: dict[str, Any] = {
        "model": str(model or DEFAULT_MODEL).strip(),
        "messages": messages,
        "temperature": temperature,
        "stream": True,
        # OpenRouter only reports token counts and cost on streamed
        # responses when usage accounting is explicitly requested.
        "usage": {"include": True},
    }
    sticky = str(session_id or "").strip()[:256]
    if sticky:
        payload["session_id"] = sticky
    effort = reasoning_payload(model, reasoning)
    if effort:
        payload["reasoning"] = effort
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if web_search:
        payload["plugins"] = web_search_plugins(web_search)
    if fast:
        payload["provider"] = {"sort": "throughput"}
    if max_completion_tokens is not None:
        limit = int(max_completion_tokens)
        if limit <= 0:
            raise ValueError("max_completion_tokens must be positive")
        payload["max_completion_tokens"] = limit

    req = Request(
        f"{API_BASE}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers(key),
        method="POST",
    )
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    reasoning_details: list[Any] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    final_usage: dict[str, Any] = {}
    generation_id = ""
    finish_reason = ""
    stream_complete = False

    try:
        with urlopen(req, timeout=timeout) as response:
            for raw in response:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    stream_complete = True
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if isinstance(chunk.get("usage"), dict):
                    final_usage = dict(chunk["usage"])
                if chunk.get("id"):
                    generation_id = str(chunk.get("id"))
                if chunk.get("error"):
                    error = chunk.get("error")
                    if isinstance(error, dict):
                        raise RuntimeError(str(error.get("message") or error.get("code") or error))
                    raise RuntimeError(str(error))
                choice = (chunk.get("choices") or [{}])[0]
                if choice.get("finish_reason") is not None:
                    finish_reason = str(choice.get("finish_reason") or "")
                delta = choice.get("delta") or {}
                piece = str(delta.get("content") or "")
                think = str(
                    delta.get("reasoning")
                    or delta.get("reasoning_content")
                    or ""
                )
                details = delta.get("reasoning_details")
                if not isinstance(details, list):
                    details = []
                if piece:
                    content_parts.append(piece)
                if think:
                    reasoning_parts.append(think)
                # OpenRouter streams structured reasoning as ordered blocks.
                # Keep every block wire-exact so a tool-result continuation can
                # pass the full sequence back without rewriting or reordering it.
                if details:
                    reasoning_details.extend(details)
                for call in delta.get("tool_calls") or []:
                    try:
                        index = int(call.get("index", 0))
                    except (TypeError, ValueError):
                        index = 0
                    slot = tool_calls.setdefault(
                        index,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if call.get("id"):
                        slot["id"] = str(call["id"])
                    if call.get("type"):
                        slot["type"] = str(call["type"])
                    function = call.get("function") or {}
                    if function.get("name"):
                        slot["function"]["name"] = str(function["name"])
                    if function.get("arguments"):
                        slot["function"]["arguments"] += str(function["arguments"])
                yield {
                    "delta": {
                        "content": piece,
                        "reasoning": think,
                        "reasoning_details": details,
                        "tool_calls": delta.get("tool_calls") or [],
                    },
                    "done": False,
                }
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(detail).get("error", {}).get("message", detail)
        except json.JSONDecodeError:
            message = detail or str(exc)
        raise RuntimeError(message) from exc
    except URLError as exc:
        raise RuntimeError(f"Network error: {exc.reason}") from exc

    assembled_tools = [tool_calls[index] for index in sorted(tool_calls)]
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(content_parts),
    }
    if reasoning_parts:
        message["reasoning"] = "".join(reasoning_parts)
    if reasoning_details:
        message["reasoning_details"] = reasoning_details
    if assembled_tools:
        message["tool_calls"] = assembled_tools
    if finish_reason:
        message["finish_reason"] = finish_reason
    yield {
        "message": message,
        "usage": final_usage,
        "generation_id": generation_id,
        "finish_reason": finish_reason,
        "stream_complete": stream_complete,
        "done": True,
    }
