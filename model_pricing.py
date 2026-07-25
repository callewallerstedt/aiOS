"""Turn OPERATOR token usage into a US dollar estimate.

Rates are per one million tokens, as published for the OpenAI API in July 2026.
Cached input tokens bill at the reduced cache-read rate.

To correct or extend the table without touching the code, drop a
``model_pricing.json`` next to this file:

    {"gpt-5.6-sol": {"input": 5.0, "cached_input": 0.5, "output": 30.0}}

Anything listed there wins over the defaults below. Prices are estimates for
standard-tier calls: OpenAI's separate long-context meter is not applied, and
runs on a signed-in Codex account bill to the ChatGPT plan rather than
per token, so those calls are reported as plan usage instead of dollars.
"""

from __future__ import annotations

import json
from pathlib import Path

PRICES_PATH = Path(__file__).resolve().parent / "model_pricing.json"

# USD per 1M tokens.
DEFAULT_PRICES = {
    "gpt-5.6-sol": {"input": 5.00, "cached_input": 0.50, "output": 30.00},
    "gpt-5.6-terra": {"input": 2.50, "cached_input": 0.25, "output": 15.00},
    "gpt-5.6-luna": {"input": 1.00, "cached_input": 0.10, "output": 6.00},
}

# The phone and the desktop both speak in short names.
ALIASES = {"sol": "gpt-5.6-sol", "terra": "gpt-5.6-terra", "luna": "gpt-5.6-luna"}

# Backends that spend the user's API key. Codex calls ride the ChatGPT plan.
BILLED_BACKENDS = {"api", "", "openai"}

TOKEN_KEYS = ("requests", "input_tokens", "output_tokens", "cached_input_tokens", "total_tokens")


def load_prices() -> dict:
    prices = {name: dict(rates) for name, rates in DEFAULT_PRICES.items()}
    try:
        override = json.loads(PRICES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return prices
    if not isinstance(override, dict):
        return prices
    for name, rates in override.items():
        if not isinstance(rates, dict):
            continue
        entry = prices.setdefault(str(name).strip().lower(), {})
        for key in ("input", "cached_input", "output"):
            if key in rates:
                try:
                    entry[key] = float(rates[key])
                except (TypeError, ValueError):
                    continue
    return {name: rates for name, rates in prices.items() if rates.get("input") is not None}


def price_for(model: str, prices: dict | None = None) -> dict | None:
    """Rates for a model name, tolerating aliases and dated variants."""
    prices = load_prices() if prices is None else prices
    name = str(model or "").strip().lower()
    name = ALIASES.get(name, name)
    while name:
        if name in prices:
            return prices[name]
        if "-" not in name:
            return None
        name = name.rsplit("-", 1)[0]
    return None


def _cost(tokens: dict, rates: dict) -> float:
    input_tokens = int(tokens.get("input_tokens") or 0)
    cached_tokens = min(int(tokens.get("cached_input_tokens") or 0), input_tokens)
    output_tokens = int(tokens.get("output_tokens") or 0)
    fresh_input = input_tokens - cached_tokens
    return (
        fresh_input * float(rates.get("input") or 0.0)
        + cached_tokens * float(rates.get("cached_input", rates.get("input")) or 0.0)
        + output_tokens * float(rates.get("output") or 0.0)
    ) / 1_000_000


def _backends(model_usage: dict, fallback_backend: str) -> dict:
    backends = model_usage.get("backends")
    if isinstance(backends, dict) and backends:
        return backends
    return {fallback_backend: model_usage}


def estimate_cost(usage: dict, default_model: str = "") -> dict:
    """Summarise what a finished run cost.

    Returns the dollar total for API-billed calls, the tokens that went to a
    ChatGPT plan instead, and any model we have no price for — so the UI can
    be honest about which of the three it is showing.
    """
    usage = usage if isinstance(usage, dict) else {}
    run_backend = str(usage.get("backend") or "api").strip().lower()
    models = usage.get("models")
    if not isinstance(models, dict) or not models:
        models = {str(default_model or usage.get("model") or ""): usage}

    prices = load_prices()
    result = {
        "usd": 0.0,
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
            billed = str(backend).strip().lower() in BILLED_BACKENDS
            if not billed:
                result["plan_tokens"] += int(tokens.get("total_tokens") or 0)
                result["plan_requests"] += int(tokens.get("requests") or 0)
                continue
            if not rates:
                if model and model not in result["unpriced"]:
                    result["unpriced"].append(model)
                continue
            amount = _cost(tokens, rates)
            result["usd"] += amount
            entry = result["models"].setdefault(model, {"usd": 0.0})
            entry["usd"] = round(entry["usd"] + amount, 6)
            for key in TOKEN_KEYS:
                entry[key] = int(entry.get(key) or 0) + int(tokens.get(key) or 0)
    result["usd"] = round(result["usd"], 6)
    result["priced"] = bool(result["models"])
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


def describe_cost(usage: dict, default_model: str = "") -> str:
    """One line for the desktop log and the phone timeline."""
    cost = estimate_cost(usage, default_model)
    parts = []
    if cost["priced"]:
        parts.append(f"≈ {format_usd(cost['usd'])}")
    if cost["plan_requests"]:
        parts.append(f"{cost['plan_tokens']:,} tokens on your ChatGPT plan")
    if cost["unpriced"]:
        parts.append(f"no price set for {', '.join(cost['unpriced'])}")
    return " · ".join(parts)
