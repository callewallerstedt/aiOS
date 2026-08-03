"""Turn AIOS/OPERATOR token usage into an exact API cost breakdown.

Rates are USD per one million tokens.  Calls made through the signed-in Codex
backend consume the user's ChatGPT plan and therefore have a $0 incremental API
charge; only calls that actually use the API-key backend are priced here.

To correct or extend the table without touching the code, drop a
``model_pricing.json`` next to this file. Anything listed there wins over the
defaults below.
"""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path

PRICES_PATH = Path(__file__).resolve().parent / "model_pricing.json"

# USD per 1M tokens. Cache writes are charged at 1.25x fresh input. Requests
# above the long-context threshold charge 2x input and 1.5x output.
DEFAULT_PRICES = {
    "gpt-5.6-sol": {
        "input": 5.00, "cached_input": 0.50, "cache_write_input": 6.25,
        "output": 30.00, "long_input_multiplier": 2.0, "long_output_multiplier": 1.5,
    },
    "gpt-5.6-terra": {
        "input": 2.00, "cached_input": 0.20, "cache_write_input": 2.50,
        "output": 12.00, "long_input_multiplier": 2.0, "long_output_multiplier": 1.5,
    },
    "gpt-5.6-luna": {
        "input": 0.20, "cached_input": 0.02, "cache_write_input": 0.25,
        "output": 1.20, "long_input_multiplier": 2.0, "long_output_multiplier": 1.5,
    },
}

ALIASES = {"sol": "gpt-5.6-sol", "terra": "gpt-5.6-terra", "luna": "gpt-5.6-luna"}
BILLED_BACKENDS = {"api", "", "openai"}
TOKEN_KEYS = (
    "requests", "input_tokens", "output_tokens", "cached_input_tokens",
    "cache_write_input_tokens", "total_tokens", "long_context_requests",
    "long_context_input_tokens", "long_context_output_tokens",
    "long_context_cached_input_tokens", "long_context_cache_write_input_tokens",
)


def _decimal(value) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except Exception:
        return Decimal(0)


def load_prices() -> dict:
    prices = {name: dict(rates) for name, rates in DEFAULT_PRICES.items()}
    try:
        override = json.loads(PRICES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return prices
    if not isinstance(override, dict):
        return prices
    allowed = (
        "input", "cached_input", "cache_write_input", "output",
        "long_input_multiplier", "long_output_multiplier",
    )
    for name, rates in override.items():
        if not isinstance(rates, dict):
            continue
        entry = prices.setdefault(str(name).strip().lower(), {})
        for key in allowed:
            if key in rates:
                try:
                    entry[key] = float(rates[key])
                except (TypeError, ValueError):
                    continue
    return {name: rates for name, rates in prices.items() if rates.get("input") is not None}


def price_for(model: str, prices: dict | None = None) -> dict | None:
    """Rates for a model name, tolerating aliases and dated variants."""
    prices = load_prices() if prices is None else prices
    name = ALIASES.get(str(model or "").strip().lower(), str(model or "").strip().lower())
    while name:
        if name in prices:
            return prices[name]
        if "-" not in name:
            return None
        name = name.rsplit("-", 1)[0]
    return None


def token_breakdown(tokens: dict) -> dict:
    """Normalize token categories while ensuring their totals cannot overlap."""
    input_tokens = max(0, int(tokens.get("input_tokens") or 0))
    cached = min(max(0, int(tokens.get("cached_input_tokens") or 0)), input_tokens)
    cache_write = min(
        max(0, int(tokens.get("cache_write_input_tokens") or 0)),
        max(0, input_tokens - cached),
    )
    output = max(0, int(tokens.get("output_tokens") or 0))
    total = max(0, int(tokens.get("total_tokens") or input_tokens + output))
    return {
        "requests": max(0, int(tokens.get("requests") or 0)),
        "input_tokens": input_tokens,
        "fresh_input_tokens": max(0, input_tokens - cached - cache_write),
        "cached_input_tokens": cached,
        "cache_write_input_tokens": cache_write,
        "output_tokens": output,
        "total_tokens": total,
    }


def _cost(tokens: dict, rates: dict) -> Decimal:
    counts = token_breakdown(tokens)
    long_input = min(max(0, int(tokens.get("long_context_input_tokens") or 0)), counts["input_tokens"])
    long_cached = min(
        max(0, int(tokens.get("long_context_cached_input_tokens") or 0)),
        counts["cached_input_tokens"], long_input,
    )
    long_write = min(
        max(0, int(tokens.get("long_context_cache_write_input_tokens") or 0)),
        counts["cache_write_input_tokens"], max(0, long_input - long_cached),
    )
    long_fresh = max(0, long_input - long_cached - long_write)
    long_output = min(
        max(0, int(tokens.get("long_context_output_tokens") or 0)), counts["output_tokens"])
    normal_fresh = counts["fresh_input_tokens"] - long_fresh
    normal_cached = counts["cached_input_tokens"] - long_cached
    normal_write = counts["cache_write_input_tokens"] - long_write
    normal_output = counts["output_tokens"] - long_output

    input_rate = _decimal(rates.get("input"))
    cached_rate = _decimal(rates.get("cached_input", rates.get("input")))
    write_rate = _decimal(rates.get("cache_write_input", rates.get("input")))
    output_rate = _decimal(rates.get("output"))
    long_input_multiplier = _decimal(rates.get("long_input_multiplier") or 1)
    long_output_multiplier = _decimal(rates.get("long_output_multiplier") or 1)
    token_dollars = (
        Decimal(normal_fresh) * input_rate
        + Decimal(normal_cached) * cached_rate
        + Decimal(normal_write) * write_rate
        + Decimal(normal_output) * output_rate
        + Decimal(long_fresh) * input_rate * long_input_multiplier
        + Decimal(long_cached) * cached_rate * long_input_multiplier
        + Decimal(long_write) * write_rate * long_input_multiplier
        + Decimal(long_output) * output_rate * long_output_multiplier
    )
    return token_dollars / Decimal(1_000_000)


def _backends(model_usage: dict, fallback_backend: str) -> dict:
    backends = model_usage.get("backends")
    return backends if isinstance(backends, dict) and backends else {fallback_backend: model_usage}


def estimate_cost(usage: dict, default_model: str = "") -> dict:
    """Return exact API charges plus separately metered ChatGPT-plan usage."""
    usage = usage if isinstance(usage, dict) else {}
    run_backend = str(usage.get("backend") or "api").strip().lower()
    models = usage.get("models")
    if not isinstance(models, dict) or not models:
        models = {str(default_model or usage.get("model") or ""): usage}

    prices = load_prices()
    exact_total = Decimal(0)
    result = {
        "usd": 0.0,
        "usd_exact": "0.000000000",
        "api_requests": 0,
        "plan_tokens": 0,
        "plan_requests": 0,
        "unpriced": [],
        "models": {},
        "currency": "USD",
    }
    for model, model_usage in models.items():
        if not isinstance(model_usage, dict):
            continue
        rates = price_for(model, prices)
        for backend, tokens in _backends(model_usage, run_backend).items():
            if not isinstance(tokens, dict):
                continue
            counts = token_breakdown(tokens)
            if str(backend).strip().lower() not in BILLED_BACKENDS:
                result["plan_tokens"] += counts["total_tokens"]
                result["plan_requests"] += counts["requests"]
                continue
            result["api_requests"] += counts["requests"]
            if not rates:
                if model and model not in result["unpriced"]:
                    result["unpriced"].append(model)
                continue
            amount = _cost(tokens, rates)
            exact_total += amount
            entry = result["models"].setdefault(model, {"usd": 0.0, "usd_exact": "0.000000000"})
            entry_total = _decimal(entry["usd_exact"]) + amount
            entry["usd_exact"] = f"{entry_total:.9f}"
            entry["usd"] = float(entry_total)
            for key in TOKEN_KEYS:
                entry[key] = int(entry.get(key) or 0) + int(tokens.get(key) or 0)
    result["usd_exact"] = f"{exact_total:.9f}"
    result["usd"] = float(exact_total)
    result["priced"] = bool(result["models"])
    result["billing"] = (
        "mixed" if result["api_requests"] and result["plan_requests"]
        else "api" if result["api_requests"] else "plan" if result["plan_requests"] else "none"
    )
    plan_usage = usage.get("plan_usage") if isinstance(usage.get("plan_usage"), dict) else {}
    if result["plan_requests"] and plan_usage:
        result["plan_usage_measured"] = bool(plan_usage.get("measured"))
        if plan_usage.get("used_percent_delta") is not None:
            result["plan_usage_percent"] = float(plan_usage["used_percent_delta"])
        if plan_usage.get("end_used_percent") is not None:
            result["plan_window_used_percent"] = float(plan_usage["end_used_percent"])
        result["plan_window_minutes"] = plan_usage.get("window_minutes")
        result["plan_type"] = plan_usage.get("plan_type") or ""
    return result


def format_usd(amount: float) -> str:
    amount = float(amount or 0.0)
    if amount <= 0:
        return "$0.00"
    if amount < 0.0001:
        return "<$0.0001"
    if amount < 1:
        return f"${amount:.4f}"
    return f"${amount:,.2f}"


def format_usd_exact(cost_or_amount) -> str:
    """Nine decimals preserve the smallest supported per-token rate exactly."""
    if isinstance(cost_or_amount, dict):
        value = cost_or_amount.get("usd_exact", "0")
    else:
        value = cost_or_amount
    return f"${_decimal(value):,.9f}"


def describe_cost(usage: dict, default_model: str = "") -> str:
    """One exact line for the desktop log and the phone timeline."""
    cost = estimate_cost(usage, default_model)
    parts = [f"{format_usd_exact(cost)} API charge"]
    if cost["plan_requests"]:
        plan_type = str(cost.get("plan_type") or "ChatGPT").strip()
        label = f"ChatGPT {plan_type.title()} plan" if plan_type.lower() != "chatgpt" else "ChatGPT plan"
        parts.append(f"{cost['plan_tokens']:,} tokens on your {label}")
        if cost.get("plan_usage_measured"):
            percent = float(cost.get("plan_usage_percent") or 0)
            amount = f"{percent:g}%" if percent > 0 else "less than 1%"
            parts.append(f"{amount} of your ChatGPT plan this run")
        if cost.get("plan_window_used_percent") is not None:
            parts.append(f"{float(cost['plan_window_used_percent']):g}% of the current window used")
    if cost["unpriced"]:
        parts.append(f"no price set for {', '.join(cost['unpriced'])}")
    return " · ".join(parts)
