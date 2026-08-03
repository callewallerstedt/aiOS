import json

import pytest

import model_pricing


@pytest.fixture(autouse=True)
def default_prices(tmp_path, monkeypatch):
    """Never let a developer's local override decide a test."""
    monkeypatch.setattr(model_pricing, "PRICES_PATH", tmp_path / "model_pricing.json")


def api_usage(model, **tokens):
    counts = {"requests": 1, "cached_input_tokens": 0, **tokens}
    return {
        "backend": "api", "models": {model: {**counts, "backends": {"api": dict(counts)}}},
        **counts,
    }


def test_a_run_is_priced_from_the_published_rates():
    cost = model_pricing.estimate_cost(api_usage("gpt-5.6-luna", input_tokens=1_000_000, output_tokens=100_000))

    # $0.20 per 1M input + $1.20 per 1M output (July 30, 2026 rates).
    assert cost["usd"] == pytest.approx(0.32)
    assert cost["priced"] is True
    assert cost["unpriced"] == []


def test_cached_input_bills_at_the_cache_rate():
    cost = model_pricing.estimate_cost(
        api_usage("gpt-5.6-sol", input_tokens=1_000_000, cached_input_tokens=800_000, output_tokens=0))

    # 200k fresh at $5/M + 800k cached at $0.50/M.
    assert cost["usd"] == pytest.approx(1.0 + 0.4)


def test_exact_cost_includes_cache_writes_and_is_not_rounded_away():
    usage = api_usage(
        "gpt-5.6-luna", input_tokens=11, cached_input_tokens=3,
        cache_write_input_tokens=2, output_tokens=1, total_tokens=12,
    )

    cost = model_pricing.estimate_cost(usage)

    # 6 fresh * $0.20/M + 3 cached * $0.02/M + 2 writes * $0.25/M + 1 output * $1.20/M.
    assert cost["usd_exact"] == "0.000002960"
    assert model_pricing.format_usd_exact(cost) == "$0.000002960"


def test_long_context_request_uses_the_published_multipliers():
    usage = api_usage(
        "gpt-5.6-luna", input_tokens=300_000, cached_input_tokens=100_000,
        output_tokens=10_000, total_tokens=310_000,
        long_context_requests=1, long_context_input_tokens=300_000,
        long_context_cached_input_tokens=100_000, long_context_output_tokens=10_000,
    )

    cost = model_pricing.estimate_cost(usage)

    # (200k fresh * $0.20 + 100k cached * $0.02) * 2, plus 10k * $1.20 * 1.5.
    assert cost["usd"] == pytest.approx(0.102)


def test_codex_calls_are_reported_as_plan_usage_not_dollars():
    usage = {
        "backend": "codex",
        "models": {"gpt-5.6-sol": {
            "requests": 3, "input_tokens": 900_000, "output_tokens": 20_000, "total_tokens": 920_000,
            "backends": {"codex": {"requests": 3, "input_tokens": 900_000, "output_tokens": 20_000,
                                   "total_tokens": 920_000}},
        }},
    }

    cost = model_pricing.estimate_cost(usage)

    assert cost["usd"] == 0.0
    assert cost["priced"] is False
    assert cost["plan_requests"] == 3
    assert cost["plan_tokens"] == 920_000
    assert cost["usd_exact"] == "0.000000000"
    assert "$0.000000000 API charge" in model_pricing.describe_cost(usage)
    assert "ChatGPT plan" in model_pricing.describe_cost(usage)


def test_measured_plan_window_delta_is_reported_as_a_percentage():
    usage = {
        "backend": "codex",
        "models": {"gpt-5.6-luna": {
            "requests": 2, "total_tokens": 12_000,
            "backends": {"codex": {"requests": 2, "total_tokens": 12_000}},
        }},
        "plan_usage": {
            "measured": True,
            "used_percent_delta": 1,
            "end_used_percent": 8,
            "window_minutes": 10080,
            "plan_type": "plus",
        },
    }

    cost = model_pricing.estimate_cost(usage)

    assert cost["plan_usage_percent"] == 1
    assert cost["plan_window_used_percent"] == 8
    assert "1% of your ChatGPT plan this run" in model_pricing.describe_cost(usage)


def test_a_fallback_run_charges_only_the_api_half():
    usage = {
        "backend": "codex_fallback",
        "models": {"gpt-5.6-terra": {
            "requests": 2, "input_tokens": 1_200_000, "output_tokens": 20_000, "total_tokens": 1_220_000,
            "backends": {
                "codex": {"requests": 1, "input_tokens": 200_000, "output_tokens": 10_000, "total_tokens": 210_000},
                "api": {"requests": 1, "input_tokens": 1_000_000, "output_tokens": 10_000, "total_tokens": 1_010_000},
            },
        }},
    }

    cost = model_pricing.estimate_cost(usage)

    # Only the API call: 1M input at $2.00 + 10k output at $12/M.
    assert cost["usd"] == pytest.approx(2.00 + 0.12)
    assert cost["plan_tokens"] == 210_000


def test_an_unknown_model_is_named_rather_than_guessed():
    usage = api_usage("gpt-9-hypothetical", input_tokens=500_000, output_tokens=1_000)

    cost = model_pricing.estimate_cost(usage)

    assert cost["usd"] == 0.0
    assert cost["priced"] is False
    assert cost["unpriced"] == ["gpt-9-hypothetical"]
    assert model_pricing.describe_cost(usage) == "$0.000000000 API charge · no price set for gpt-9-hypothetical"


def test_dated_variants_and_short_names_resolve_to_the_same_rates():
    assert model_pricing.price_for("gpt-5.6-sol-2026-07-09") == model_pricing.price_for("sol")
    assert model_pricing.price_for("GPT-5.6-Terra") == model_pricing.DEFAULT_PRICES["gpt-5.6-terra"]
    assert model_pricing.price_for("nonsense") is None


def test_a_local_price_file_overrides_the_defaults(monkeypatch, tmp_path):
    path = tmp_path / "model_pricing.json"
    path.write_text(json.dumps({"gpt-5.6-luna": {"input": 2.0, "cached_input": 0.2, "output": 12.0}}), encoding="utf-8")
    monkeypatch.setattr(model_pricing, "PRICES_PATH", path)

    cost = model_pricing.estimate_cost(api_usage("gpt-5.6-luna", input_tokens=1_000_000, output_tokens=0))

    assert cost["usd"] == pytest.approx(2.0)


def test_older_runs_without_a_model_breakdown_still_price():
    usage = {"backend": "api", "requests": 1, "input_tokens": 1_000_000, "output_tokens": 0}

    cost = model_pricing.estimate_cost(usage, default_model="gpt-5.6-luna")

    assert cost["usd"] == pytest.approx(0.2)


def test_formatting_stays_readable_at_both_ends():
    assert model_pricing.format_usd(0) == "$0.00"
    assert model_pricing.format_usd(0.00001) == "<$0.0001"
    assert model_pricing.format_usd(0.0412) == "$0.0412"
    assert model_pricing.format_usd(12.5) == "$12.50"
