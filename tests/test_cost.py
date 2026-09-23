"""Tests for the in-process run pricing in subagent.telemetry.cost.

`price_run` is what Agent._finish calls; `Pricing.from_settings` builds the
Pricing from parsed Settings. The parent-transcript join and the flat_plan
spreading are exercised here too, without pandas.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from subagent.providers.base import PricingSpec, ProviderConfig
from subagent.telemetry import cost as cost


def _pricing(models=None, provider=None):
    data = {
        "cache_multipliers": {"cache_write_5m": 1.25, "cache_write_1h": 2.0, "cache_read": 0.1},
        "models": models or {
            "claude-opus-5": {"input": 5.0, "output": 25.0},
            "claude-fable-5-1": {"input": 10.0, "output": 50.0, "cache_read_multiplier": 0.025},
        },
    }
    if provider is not None:
        data["provider"] = provider
    return cost.Pricing(data)


def _cfg(kind, **values):
    return ProviderConfig(name="p", driver="claude", vendor="v",
                          pricing=PricingSpec(kind=kind, values=values))


def _usage(input=0, output=0, cache_read=0, cache_write=0, reasoning=0):
    return {"input": input, "output": output, "cache_read": cache_read,
            "cache_write": cache_write, "reasoning": reasoning}


def test_per_token_provider_and_counterfactual():
    pricing = _pricing()
    result = cost.price_run(_usage(input=1000, output=500, cache_read=200, cache_write=100),
                            None, _cfg("per_token", input=1.32, output=3.96, cache_read=0.044),
                            pricing, 0.0)
    # provider: (input + cache_write) * input rate + output * output rate + read * read rate
    assert result["provider_usd"] == pytest.approx(
        (1100 * 1.32 + 500 * 3.96 + 200 * 0.044) / 1e6, abs=1e-12
    )
    # counterfactual: cache_write billed as 5m writes (input * 1.25)
    assert result["counterfactual_usd"] == pytest.approx(
        (1000 * 5 + 500 * 25 + 200 * 0.5 + 100 * 6.25) / 1e6, abs=1e-12
    )
    assert result["kind"] == "per_token"
    assert result["note"] is None


def test_counterfactual_cache_multipliers():
    pricing = _pricing()
    result = cost.price_run(_usage(input=100, output=0, cache_read=1000, cache_write=200),
                            None, _cfg("local", usd=0), pricing, 0.0)
    # claude-opus-5: read at input * 0.1 = 0.5; write at input * 1.25 = 6.25
    assert result["counterfactual_usd"] == pytest.approx(
        (100 * 5 + 1000 * 0.5 + 200 * 6.25) / 1e6, abs=1e-12
    )


def test_counterfactual_cache_read_override():
    pricing = _pricing()
    pricing.counterfactual = "claude-fable-5-1"
    result = cost.price_run(_usage(input=0, output=0, cache_read=1_000_000), None,
                            _cfg("local", usd=0), pricing, 0.0)
    # fable read multiplier 0.025 -> 10 * 0.025 = 0.25 per 1M
    assert result["counterfactual_usd"] == pytest.approx(0.25, abs=1e-9)


def test_flat_plan_is_null_at_run_end_and_spread_at_report():
    pricing = _pricing(provider={"glm": {"kind": "flat_plan", "monthly_usd": 80}})
    result = cost.price_run(_usage(input=100), None, _cfg("flat_plan", monthly_usd=80),
                            pricing, 0.0)
    assert result["provider_usd"] is None
    assert result["note"] == "spread at report time"

    runs = [
        {"lane": "glm", "ts": 1789700600.0, "input": 100, "output": 0,
         "cache_read": 0, "cache_write": 0},
        {"lane": "glm", "ts": 1789700600.0, "input": 100, "output": 0,
         "cache_read": 0, "cache_write": 0},
    ]
    cost.provider_costs(pricing, runs)
    assert runs[0]["provider_usd"] == pytest.approx(40.0)


def test_local_provider():
    result = cost.price_run(_usage(), None, _cfg("local", usd=0), _pricing(), 0.0)
    assert result["provider_usd"] == 0.0
    assert result["note"] is None


def test_credits_are_priced_at_usd_per_credit():
    result = cost.price_run(_usage(), 2.5, _cfg("credits", usd_per_credit=0.01), _pricing(), 0.0)
    assert result["provider_usd"] == pytest.approx(0.025)
    # No credits reported -> nothing to price.
    assert cost.price_run(_usage(), None, _cfg("credits", usd_per_credit=0.01),
                          _pricing(), 0.0)["provider_usd"] is None


def test_per_token_peak_offpeak_note():
    pricing = _pricing()

    def ts(hour):
        return dt.datetime(2026, 1, 1, hour, 0, tzinfo=dt.UTC).timestamp()

    cfg = _cfg("per_token", input=1.0, output=1.0, cache_read=0.1, offpeak_hours_utc=[0, 8])
    assert cost.price_run(_usage(), None, cfg, pricing, ts(4))["note"] == "offpeak"
    assert cost.price_run(_usage(), None, cfg, pricing, ts(12))["note"] == "peak"
    # Rates are unchanged by the timestamp; the note is the only difference.
    assert cost.price_run(_usage(input=1_000_000), None, cfg, pricing,
                          ts(4))["provider_usd"] == pytest.approx(1.0)


def test_from_settings_reads_the_config_counterfactual_key():
    settings = SimpleNamespace(pricing={
        "counterfactual": "claude-opus-5",
        "cache_multipliers": {"cache_write_5m": 1.25, "cache_write_1h": 2.0, "cache_read": 0.1},
        "models": {"claude-opus-5": {"input": 5.0, "output": 25.0}},
    })
    pricing = cost.Pricing.from_settings(settings)
    assert pricing.counterfactual == "claude-opus-5"
    assert pricing.resolve("claude-opus-5") == "claude-opus-5"


def test_from_settings_with_no_pricing_prices_nothing():
    settings = SimpleNamespace(pricing={})
    pricing = cost.Pricing.from_settings(settings)
    result = cost.price_run(_usage(input=100), None, _cfg("local", usd=0), pricing, 0.0)
    assert result["counterfactual_usd"] == 0.0
    assert result["provider_usd"] == 0.0
