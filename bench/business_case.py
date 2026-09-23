#!/usr/bin/env python3
"""Self-hosting business-case model: cloud tokens vs a local GPU fleet.

Pure functions plus a CLI. Give it your department's workload and the hardware
and cloud prices; it returns the monthly cloud cost, the monthly local cost
(power + capex/amortisation + ops), the parity- and retry-adjusted local cost,
a capacity check, the break-even month, 12/24/36-month ROI and a sensitivity
grid over parity x concurrency.

The two measured inputs are optional and their file shapes do not exist yet, so
this module reads the following minimal shape and ignores anything else:

  --from-report report.json
      an object with a "providers" list (or a bare list) of per-provider rows:

        {provider, tokens_in, tokens_out, tokens_cache_read, verified_pass_rate,
         rounds_per_delegation, wall_p50_s, provider_usd, counterfactual_usd}

      `tokens_in` is non-cache input; cache reads are reported separately in
      `tokens_cache_read`. The local row's tokens + wall_p50_s become the
      per-seat-hour demand (tokens * 3600 / wall_p50_s, i.e. delegations run
      back to back); `parity` is local verified_pass_rate / cloud
      verified_pass_rate; `retry_factor` is local rounds_per_delegation / cloud
      rounds_per_delegation; `cache_read_share` comes from the cloud row.

      The measured wall time and pass rate are also read under the names
      `subagent report --json` emits (`wall_p50`, `verified_rate`); a report
      with none of the measured fields warns and keeps the defaults.

  --from-concurrency bench.csv
      CSV with columns `level, aggregate_tps, ttft_p90_s` (one row per level).
      The chosen row is the highest level whose p90 TTFT is under 60 s, falling
      back to the highest measured level. `concurrency_per_unit` is its level
      and `measured_tps` is aggregate_tps / level (per-stream output tok/s).

Run:

    uv run python bench/business_case.py --seats 100 --json
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

M = 1_000_000
CALENDAR_DAYS_PER_MONTH = 30.0  # assumption: power is drawn every day of a 30-day month
ACCEPTABLE_TTFT_S = 60.0  # matches scripts/bench_concurrency.py's recommendation rule
SENSITIVITY_PARITIES = (0.6, 0.8, 1.0)
SENSITIVITY_CONCURRENCY_MULTIPLIERS = (1, 2, 4)

# Every published figure from the brief (read 2026-09-22) with its source URL.
# Street prices and vendor benchmarks have no stable public URL; the URL points
# at the product page and the number itself is the brief's 2026-09-22 snapshot.
# The entries at the bottom are model assumptions, not published figures: they
# are exact numbers chosen so the worked example in docs/business-case.md is
# reproducible, and measured --from-report / --from-concurrency data replace them.
DEFAULTS = {
    # RTX PRO 6000 Blackwell 96 GB: street $14,000-15,500 (midpoint $14,750 used),
    # 600 W. https://www.nvidia.com/en-us/design-visualization/rtx-pro-6000/
    "hardware_capex_usd": 14_750.0,
    "power_w_per_unit": 600.0,
    # RTX 5090 alternative: ~$4,900, 575 W (cited input, not the default).
    # https://www.nvidia.com/en-us/geforce/graphics-cards/rtx-5090/
    "rtx_5090_capex_usd": 4_900.0,
    "rtx_5090_power_w": 575.0,
    # Qwen3.6-27B FP8 on one RTX PRO 6000 via vLLM: ~46 tok/s single stream,
    # ~190 tok/s aggregate at 5 concurrent, ~28 tok/s per user at 32k context x 5.
    # https://qwenlm.github.io/
    "single_stream_tps": 46.0,
    "aggregate_tps_5": 190.0,
    "per_user_tps_32k_5": 28.0,
    # Claude list prices, USD per M tokens in/out; cache reads are 10% of input.
    # https://www.anthropic.com/pricing
    "claude_opus5_in": 5.0,
    "claude_opus5_out": 25.0,
    "claude_sonnet5_in": 2.0,
    "claude_sonnet5_out": 10.0,
    "cache_read_multiplier": 0.1,
    # Seat subscriptions (cited inputs; the default cloud model is per-token).
    # Copilot Business $19/seat (1,900 credits, $0.01/credit); Enterprise $39.
    # https://github.com/features/copilot/plans
    "copilot_business_seat_usd": 19.0,
    "copilot_business_credits": 1_900.0,
    "copilot_business_overage_per_credit": 0.01,
    "copilot_enterprise_seat_usd": 39.0,
    # Claude Team $25-125/seat; Max $100/$200. https://www.anthropic.com/pricing
    "claude_team_seat_usd": (25.0, 125.0),
    "claude_max_seat_usd": (100.0, 200.0),
    # DeepSeek v4-pro peak $1.32/$3.96 per M in/out (half off-peak).
    # https://api-docs.deepseek.com/quick_start/pricing
    "deepseek_v4_pro_in": 1.32,
    "deepseek_v4_pro_out": 3.96,
    # GLM-5.3-Flash $0.15/$0.50 per M in/out. https://z.ai/pricing
    "glm_53_flash_in": 0.15,
    "glm_53_flash_out": 0.50,
    # --- model assumptions (exact, not published) ---
    "seats": 100,
    "hours_per_day": 8.0,
    "work_days_per_month": 21.0,
    "tokens_in_per_seat_hour": 30_000.0,
    "tokens_out_per_seat_hour": 6_000.0,
    "cache_read_share": 0.8,
    "usd_per_kwh": 0.13,
    "utilisation": 0.6,
    "amortisation_months": 36,
    "ops_hours_per_month": 4.0,
    "ops_usd_per_hour": 100.0,
    "concurrency_per_unit": 5.0,
    "parity": 1.0,
    "retry_factor": 1.0,
}


# --------------------------------------------------------------------------- inputs


@dataclass(frozen=True)
class CloudPrice:
    """Per-token cloud price, USD per M tokens."""

    input_usd_per_mtok: float
    output_usd_per_mtok: float
    cache_read_usd_per_mtok: float


@dataclass(frozen=True)
class CloudSeat:
    """A per-seat subscription with an included token allowance and overage."""

    monthly_usd: float
    allowance_tokens_per_seat: float = 0.0
    overage_usd_per_mtok: float = 0.0


@dataclass(frozen=True)
class Inputs:
    """Every number the model needs. Exactly one of cloud_price / cloud_seat.

    `hardware_count` may be None, meaning "buy as many units as the capacity
    check says you need". `measured_tps` is the per-stream output tokens/s the
    fleet sustains at `concurrency_per_unit` children per unit; it is the
    "measured tok/s" of the capacity check.
    """

    seats: int
    hours_per_day: float
    work_days_per_month: float
    tokens_in_per_seat_hour: float
    tokens_out_per_seat_hour: float
    cache_read_share: float
    cloud_price: CloudPrice | None = None
    cloud_seat: CloudSeat | None = None
    hardware_capex_usd: float = DEFAULTS["hardware_capex_usd"]
    hardware_count: int | None = None
    power_w_per_unit: float = DEFAULTS["power_w_per_unit"]
    usd_per_kwh: float = DEFAULTS["usd_per_kwh"]
    utilisation: float = DEFAULTS["utilisation"]
    amortisation_months: int = DEFAULTS["amortisation_months"]
    ops_hours_per_month: float = DEFAULTS["ops_hours_per_month"]
    ops_usd_per_hour: float = DEFAULTS["ops_usd_per_hour"]
    concurrency_per_unit: float = DEFAULTS["concurrency_per_unit"]
    measured_tps: float = DEFAULTS["per_user_tps_32k_5"]
    parity: float = DEFAULTS["parity"]
    retry_factor: float = DEFAULTS["retry_factor"]

    def __post_init__(self) -> None:
        if (self.cloud_price is None) == (self.cloud_seat is None):
            raise ValueError("set exactly one of cloud_price or cloud_seat")
        for name, value in (
            ("seats", self.seats),
            ("hours_per_day", self.hours_per_day),
            ("work_days_per_month", self.work_days_per_month),
            ("amortisation_months", self.amortisation_months),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        for name, value in (
            ("tokens_in_per_seat_hour", self.tokens_in_per_seat_hour),
            ("tokens_out_per_seat_hour", self.tokens_out_per_seat_hour),
            ("hardware_capex_usd", self.hardware_capex_usd),
            ("power_w_per_unit", self.power_w_per_unit),
            ("usd_per_kwh", self.usd_per_kwh),
            ("ops_hours_per_month", self.ops_hours_per_month),
            ("ops_usd_per_hour", self.ops_usd_per_hour),
            ("measured_tps", self.measured_tps),
        ):
            if value < 0:
                raise ValueError(f"{name} must not be negative")
        if not 0.0 <= self.cache_read_share <= 1.0:
            raise ValueError("cache_read_share must be between 0 and 1")
        if not 0.0 < self.utilisation <= 1.0:
            raise ValueError("utilisation must be in (0, 1]")
        if self.concurrency_per_unit <= 0:
            raise ValueError("concurrency_per_unit must be positive")
        if self.parity <= 0:
            raise ValueError("parity must be positive")
        if self.retry_factor <= 0:
            raise ValueError("retry_factor must be positive")
        if self.hardware_count is not None and self.hardware_count < 0:
            raise ValueError("hardware_count must not be negative")
        if self.cloud_price is not None:
            for name, value in (
                ("input_usd_per_mtok", self.cloud_price.input_usd_per_mtok),
                ("output_usd_per_mtok", self.cloud_price.output_usd_per_mtok),
                ("cache_read_usd_per_mtok", self.cloud_price.cache_read_usd_per_mtok),
            ):
                if value < 0:
                    raise ValueError(f"cloud price {name} must not be negative")
        if self.cloud_seat is not None:
            for name, value in (
                ("monthly_usd", self.cloud_seat.monthly_usd),
                ("allowance_tokens_per_seat", self.cloud_seat.allowance_tokens_per_seat),
                ("overage_usd_per_mtok", self.cloud_seat.overage_usd_per_mtok),
            ):
                if value < 0:
                    raise ValueError(f"cloud seat {name} must not be negative")


def inputs_to_dict(inputs: Inputs) -> dict[str, Any]:
    """A JSON-safe dict of the inputs (for the --json output)."""
    return {
        "seats": inputs.seats,
        "hours_per_day": inputs.hours_per_day,
        "work_days_per_month": inputs.work_days_per_month,
        "tokens_in_per_seat_hour": inputs.tokens_in_per_seat_hour,
        "tokens_out_per_seat_hour": inputs.tokens_out_per_seat_hour,
        "cache_read_share": inputs.cache_read_share,
        "cloud_price": (
            {
                "input_usd_per_mtok": inputs.cloud_price.input_usd_per_mtok,
                "output_usd_per_mtok": inputs.cloud_price.output_usd_per_mtok,
                "cache_read_usd_per_mtok": inputs.cloud_price.cache_read_usd_per_mtok,
            }
            if inputs.cloud_price
            else None
        ),
        "cloud_seat": (
            {
                "monthly_usd": inputs.cloud_seat.monthly_usd,
                "allowance_tokens_per_seat": inputs.cloud_seat.allowance_tokens_per_seat,
                "overage_usd_per_mtok": inputs.cloud_seat.overage_usd_per_mtok,
            }
            if inputs.cloud_seat
            else None
        ),
        "hardware_capex_usd": inputs.hardware_capex_usd,
        "hardware_count": inputs.hardware_count,
        "power_w_per_unit": inputs.power_w_per_unit,
        "usd_per_kwh": inputs.usd_per_kwh,
        "utilisation": inputs.utilisation,
        "amortisation_months": inputs.amortisation_months,
        "ops_hours_per_month": inputs.ops_hours_per_month,
        "ops_usd_per_hour": inputs.ops_usd_per_hour,
        "concurrency_per_unit": inputs.concurrency_per_unit,
        "measured_tps": inputs.measured_tps,
        "parity": inputs.parity,
        "retry_factor": inputs.retry_factor,
    }


# --------------------------------------------------------------------------- model


def monthly_volumes(inputs: Inputs) -> dict[str, float]:
    """Total tokens per month. cache_read is a share of input tokens."""
    seat_hours = inputs.seats * inputs.hours_per_day * inputs.work_days_per_month
    tokens_in = seat_hours * inputs.tokens_in_per_seat_hour
    tokens_cache_read = tokens_in * inputs.cache_read_share
    return {
        "seat_hours": seat_hours,
        "tokens_in": tokens_in,
        "tokens_non_cache_in": tokens_in - tokens_cache_read,
        "tokens_out": seat_hours * inputs.tokens_out_per_seat_hour,
        "tokens_cache_read": tokens_cache_read,
    }


def cloud_monthly_cost(inputs: Inputs) -> float:
    """USD per month for the same work on the cloud provider."""
    volumes = monthly_volumes(inputs)
    if inputs.cloud_seat is not None:
        billable = volumes["tokens_in"] + volumes["tokens_out"]
        allowance = inputs.seats * inputs.cloud_seat.allowance_tokens_per_seat
        overage = max(0.0, billable - allowance) * inputs.cloud_seat.overage_usd_per_mtok / M
        return inputs.seats * inputs.cloud_seat.monthly_usd + overage
    price = inputs.cloud_price
    return (
        volumes["tokens_non_cache_in"] * price.input_usd_per_mtok
        + volumes["tokens_out"] * price.output_usd_per_mtok
        + volumes["tokens_cache_read"] * price.cache_read_usd_per_mtok
    ) / M


def capacity(inputs: Inputs) -> dict[str, Any]:
    """Peak-hour demand vs what the fleet can serve. Tokens/hour throughout."""
    demand_per_hour = inputs.seats * (
        inputs.tokens_in_per_seat_hour + inputs.tokens_out_per_seat_hour
    )
    unit_capacity = (
        inputs.concurrency_per_unit * inputs.measured_tps * 3600.0 * inputs.utilisation
    )
    units_needed = (
        math.ceil(demand_per_hour / unit_capacity) if unit_capacity > 0 else None
    )
    installed = (inputs.hardware_count or 0) * unit_capacity
    return {
        "demand_tokens_per_hour": demand_per_hour,
        "capacity_per_unit_per_hour": unit_capacity,
        "units_needed": units_needed,
        "installed_capacity_per_hour": installed,
        "shortfall_tokens_per_hour": max(0.0, demand_per_hour - installed),
    }


def effective_units(inputs: Inputs, units_needed: int | None) -> int | None:
    """The fleet size the cost model uses: the stated count, else capacity."""
    if inputs.hardware_count is not None:
        return inputs.hardware_count
    return units_needed


def local_costs(inputs: Inputs, units: int | None) -> dict[str, Any] | None:
    """Monthly local cost for `units` GPUs. None when the fleet size is unknown."""
    if units is None:
        return None
    power_kwh = (
        units * inputs.power_w_per_unit * inputs.utilisation
        * 24.0 * CALENDAR_DAYS_PER_MONTH / 1000.0
    )
    power_usd = power_kwh * inputs.usd_per_kwh
    capex_usd = units * inputs.hardware_capex_usd
    amortisation_usd = capex_usd / inputs.amortisation_months
    ops_usd = inputs.ops_hours_per_month * inputs.ops_usd_per_hour
    return {
        "units": units,
        "power_usd": power_usd,
        "capex_usd": capex_usd,
        "amortisation_usd": amortisation_usd,
        "ops_usd": ops_usd,
        "monthly_local_cost_usd": power_usd + amortisation_usd + ops_usd,
        "monthly_local_cash_usd": power_usd + ops_usd,
    }


def break_even_month(
    capex_usd: float, cloud_monthly: float, local_cash_adjusted: float
) -> int | None:
    """First month cumulative cloud savings repay the upfront capex (cash basis).

    0 means already paid off; None means it never breaks even.
    """
    if capex_usd <= 0:
        return 0
    saving_per_month = cloud_monthly - local_cash_adjusted
    if saving_per_month <= 0:
        return None
    return math.ceil(capex_usd / saving_per_month)


def roi_at(
    months: int, capex_usd: float, cloud_monthly: float, local_cash_adjusted: float
) -> float | None:
    """(cumulative net saving over `months` months) / capex, on a cash basis."""
    if capex_usd <= 0:
        return None
    return (months * (cloud_monthly - local_cash_adjusted) - capex_usd) / capex_usd


def _cell(
    inputs: Inputs,
    cloud_monthly: float,
    parity: float,
    concurrency_multiplier: int,
) -> dict[str, Any]:
    """One sensitivity cell: the fleet sized for the demand at this concurrency."""
    concurrency = inputs.concurrency_per_unit * concurrency_multiplier
    demand_per_hour = inputs.seats * (
        inputs.tokens_in_per_seat_hour + inputs.tokens_out_per_seat_hour
    )
    unit_capacity = concurrency * inputs.measured_tps * 3600.0 * inputs.utilisation
    units_needed = math.ceil(demand_per_hour / unit_capacity) if unit_capacity > 0 else None
    n = units_needed or 0
    capex_usd = n * inputs.hardware_capex_usd
    power_usd = (
        n * inputs.power_w_per_unit * inputs.utilisation
        * 24.0 * CALENDAR_DAYS_PER_MONTH / 1000.0 * inputs.usd_per_kwh
    )
    ops_usd = inputs.ops_hours_per_month * inputs.ops_usd_per_hour
    cash_adjusted = (power_usd + ops_usd) * inputs.retry_factor / parity
    return {
        "parity": parity,
        "concurrency_multiplier": concurrency_multiplier,
        "concurrency_per_unit": concurrency,
        "units_needed": units_needed,
        "capex_usd": capex_usd,
        "adjusted_local_cash_usd": cash_adjusted,
        "break_even_month": break_even_month(capex_usd, cloud_monthly, cash_adjusted),
        "roi_12": roi_at(12, capex_usd, cloud_monthly, cash_adjusted),
        "roi_24": roi_at(24, capex_usd, cloud_monthly, cash_adjusted),
        "roi_36": roi_at(36, capex_usd, cloud_monthly, cash_adjusted),
    }


def sensitivity(inputs: Inputs, cloud_monthly: float) -> list[dict[str, Any]]:
    """Break-even/ROI across parity x concurrency, assuming you buy exactly the
    units the capacity check needs at each concurrency."""
    return [
        _cell(inputs, cloud_monthly, parity, multiplier)
        for parity in SENSITIVITY_PARITIES
        for multiplier in SENSITIVITY_CONCURRENCY_MULTIPLIERS
    ]


def model(inputs: Inputs) -> dict[str, Any]:
    """The full business case as a JSON-safe dict.

    ROI and break-even compare the fleet's cost against the *whole* cloud
    bill, so they are only meaningful for a fleet that can serve the demand:
    when `hardware_count` is fixed below `units_needed`, `undersized_fleet`
    is true and both are infeasible (None) even though the local cost of the
    fleet that was priced is still reported.
    """
    volumes = monthly_volumes(inputs)
    cloud = cloud_monthly_cost(inputs)
    cap = capacity(inputs)
    # `hardware_count = None` sizes the fleet to the demand, so only a stated
    # count can leave it short of what the workload needs.
    undersized = inputs.hardware_count is not None and cap["shortfall_tokens_per_hour"] > 0
    units = effective_units(inputs, cap["units_needed"])
    local = local_costs(inputs, units)

    if local is None:
        adjusted_cost = adjusted_cash = None
        break_even = None
        roi = {"12": None, "24": None, "36": None}
    else:
        adjusted_cost = local["monthly_local_cost_usd"] * inputs.retry_factor / inputs.parity
        adjusted_cash = local["monthly_local_cash_usd"] * inputs.retry_factor / inputs.parity
        if undersized:
            break_even = None
            roi = {"12": None, "24": None, "36": None}
        else:
            break_even = break_even_month(local["capex_usd"], cloud, adjusted_cash)
            roi = {
                "12": roi_at(12, local["capex_usd"], cloud, adjusted_cash),
                "24": roi_at(24, local["capex_usd"], cloud, adjusted_cash),
                "36": roi_at(36, local["capex_usd"], cloud, adjusted_cash),
            }

    return {
        "inputs": inputs_to_dict(inputs),
        "volumes": volumes,
        "cloud": {
            "kind": "seat" if inputs.cloud_seat is not None else "per_token",
            "monthly_cost_usd": cloud,
        },
        "capacity": cap,
        "undersized_fleet": undersized,
        "local": local or {},
        "adjusted_local_cost_usd": adjusted_cost,
        "adjusted_local_cash_usd": adjusted_cash,
        "break_even_month": break_even,
        "roi": roi,
        "sensitivity": sensitivity(inputs, cloud),
    }


# --------------------------------------------------------------------------- loaders


def _f(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def read_report(path: Path) -> list[dict]:
    """Read the report JSON: a bare list of rows, or {"providers": [...]}."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = (data.get("providers") or data.get("rows") or []) if isinstance(data, dict) else data
    return [row for row in rows if isinstance(row, dict)]


def report_rows_for(rows: list[dict], local: str, cloud: str) -> tuple[dict, dict]:
    by_provider = {row.get("provider"): row for row in rows if row.get("provider")}
    if local not in by_provider:
        raise SystemExit(f"provider {local!r} not found in report")
    if cloud not in by_provider:
        raise SystemExit(f"cloud provider {cloud!r} not found in report")
    return by_provider[local], by_provider[cloud]


def _measured(row: dict, *names: str) -> float | None:
    """The first present, numeric value among `names`.

    The measured fields are read under both spellings: this module's
    documented ones (`wall_p50_s`, `verified_pass_rate`) and the ones
    `subagent report --json` emits (`wall_p50`, `verified_rate`).
    """
    for name in names:
        value = _f(row.get(name))
        if value is not None:
            return value
    return None


def report_overrides(local: dict, cloud: dict) -> dict[str, float]:
    """Map two measured report rows onto the model inputs they can fill."""
    overrides: dict[str, float] = {}
    wall = _measured(local, "wall_p50_s", "wall_p50")
    if wall and wall > 0:
        tokens_in = _f(local.get("tokens_in")) or 0.0
        tokens_cache = _f(local.get("tokens_cache_read")) or 0.0
        tokens_out = _f(local.get("tokens_out")) or 0.0
        overrides["tokens_in_per_seat_hour"] = (tokens_in + tokens_cache) * 3600.0 / wall
        overrides["tokens_out_per_seat_hour"] = tokens_out * 3600.0 / wall
    cloud_in = _f(cloud.get("tokens_in")) or 0.0
    cloud_cache = _f(cloud.get("tokens_cache_read")) or 0.0
    if cloud_in + cloud_cache > 0:
        overrides["cache_read_share"] = cloud_cache / (cloud_in + cloud_cache)
    local_rate = _measured(local, "verified_pass_rate", "verified_rate")
    cloud_rate = _measured(cloud, "verified_pass_rate", "verified_rate")
    if local_rate is not None and local_rate > 0 and cloud_rate and cloud_rate > 0:
        overrides["parity"] = local_rate / cloud_rate
    local_rounds = _measured(local, "rounds_per_delegation")
    cloud_rounds = _measured(cloud, "rounds_per_delegation")
    if local_rounds is not None and local_rounds > 0 and cloud_rounds and cloud_rounds > 0:
        overrides["retry_factor"] = local_rounds / cloud_rounds
    return overrides


def read_concurrency(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def pick_concurrency(rows: list[dict]) -> dict | None:
    """The bench.csv row to use: highest level with p90 TTFT under 60 s, else
    the highest measured level."""
    usable = []
    for row in rows:
        level = _f(row.get("level"))
        aggregate = _f(row.get("aggregate_tps"))
        if level is None or aggregate is None:
            continue
        usable.append(
            {"level": level, "aggregate_tps": aggregate, "ttft_p90_s": _f(row.get("ttft_p90_s"))}
        )
    if not usable:
        return None
    ok_ttft = [row for row in usable if row["ttft_p90_s"] is not None
               and row["ttft_p90_s"] < ACCEPTABLE_TTFT_S]
    return max(ok_ttft or usable, key=lambda row: row["level"])


def concurrency_overrides(row: dict | None) -> dict[str, float]:
    if row is None:
        return {}
    level = row["level"]
    aggregate = row["aggregate_tps"]
    return {
        "concurrency_per_unit": level,
        "measured_tps": aggregate / level if level else aggregate,
    }


# --------------------------------------------------------------------------- output


def _money(value: float | None) -> str:
    return "—" if value is None else f"${value:,.2f}"


def _months(value: int | None) -> str:
    return "never" if value is None else str(value)


def render_markdown(result: dict[str, Any]) -> str:
    inputs = result["inputs"]
    local = result["local"]
    cap = result["capacity"]
    lines = [
        "# Business case: cloud vs self-hosted",
        "",
        "## Assumptions",
        "",
        "| Input | Value |",
        "|---|---|",
        f"| seats | {inputs['seats']} |",
        f"| hours / day | {inputs['hours_per_day']:g} |",
        f"| work days / month | {inputs['work_days_per_month']:g} |",
        f"| tokens in / seat-hour | {inputs['tokens_in_per_seat_hour']:,.0f} |",
        f"| tokens out / seat-hour | {inputs['tokens_out_per_seat_hour']:,.0f} |",
        f"| cache read share | {inputs['cache_read_share']:.0%} |",
    ]
    if inputs["cloud_price"]:
        p = inputs["cloud_price"]
        lines += [
            "| cloud price in/out/cache-read ($/M) | "
            f"{p['input_usd_per_mtok']:g} / {p['output_usd_per_mtok']:g} / "
            f"{p['cache_read_usd_per_mtok']:g} |",
        ]
    else:
        s = inputs["cloud_seat"]
        lines += [
            "| cloud seat $/month (allowance tok, overage $/M) | "
            f"{s['monthly_usd']:g} ({s['allowance_tokens_per_seat']:,.0f}, "
            f"{s['overage_usd_per_mtok']:g}) |",
        ]
    lines += [
        f"| hardware capex / unit | {_money(inputs['hardware_capex_usd'])} |",
        f"| hardware count (None = size to capacity) | {inputs['hardware_count']} |",
        f"| power / unit (W) | {inputs['power_w_per_unit']:g} |",
        f"| $ / kWh | {inputs['usd_per_kwh']:g} |",
        f"| utilisation | {inputs['utilisation']:.0%} |",
        f"| amortisation (months) | {inputs['amortisation_months']} |",
        f"| ops (h/month x $/h) | {inputs['ops_hours_per_month']:g} x "
        f"{inputs['ops_usd_per_hour']:g} |",
        f"| concurrency / unit | {inputs['concurrency_per_unit']:g} |",
        f"| measured tok/s (per stream) | {inputs['measured_tps']:g} |",
        f"| parity (local / cloud pass rate) | {inputs['parity']:g} |",
        f"| retry factor (local / cloud rounds) | {inputs['retry_factor']:g} |",
        "",
        "## Monthly cost",
        "",
        "| Cost | USD / month |",
        "|---|---|",
        f"| cloud ({result['cloud']['kind']}) | {_money(result['cloud']['monthly_cost_usd'])} |",
    ]
    if local:
        lines += [
            f"| local — power | {_money(local['power_usd'])} |",
            f"| local — capex amortisation | {_money(local['amortisation_usd'])} |",
            f"| local — ops | {_money(local['ops_usd'])} |",
            f"| local — total | {_money(local['monthly_local_cost_usd'])} |",
            f"| local — parity/retry adjusted | {_money(result['adjusted_local_cost_usd'])} |",
        ]
    else:
        lines.append("| local | — (no measured throughput) |")
    lines += [
        "",
        "## Capacity",
        "",
        "| Quantity | Value |",
        "|---|---|",
        f"| demand | {cap['demand_tokens_per_hour']:,.0f} tokens/hour |",
        f"| capacity per unit | {cap['capacity_per_unit_per_hour']:,.0f} tokens/hour |",
        f"| units needed | {cap['units_needed']} |",
        f"| installed capacity | {cap['installed_capacity_per_hour']:,.0f} tokens/hour |",
        f"| shortfall | {cap['shortfall_tokens_per_hour']:,.0f} tokens/hour |",
        "",
        "## Break-even and ROI",
        "",
    ]
    if result["undersized_fleet"]:
        lines.append(
            f"The installed fleet ({inputs['hardware_count']} unit(s)) is below the "
            f"{cap['units_needed']} unit(s) the demand needs: it cannot serve the workload, "
            "so break-even and ROI against the full cloud bill are infeasible."
        )
        lines.append("")
    lines += [
        "| Horizon | Value |",
        "|---|---|",
    ]
    if result["undersized_fleet"]:
        lines.append("| break-even month | infeasible (fleet below demand) |")
    else:
        lines.append(f"| break-even month | {_months(result['break_even_month'])} |")
    for months in ("12", "24", "36"):
        roi = result["roi"][months]
        value = "—" if roi is None else f"{roi:+.0%}"
        if result["undersized_fleet"]:
            value = "infeasible (fleet below demand)"
        lines.append(f"| {months}-month ROI | {value} |")
    lines += ["", "## Sensitivity (parity x concurrency)", ""]
    cells = result["sensitivity"]
    parities = SENSITIVITY_PARITIES
    multipliers = SENSITIVITY_CONCURRENCY_MULTIPLIERS
    lines.append("Break-even month (units needed in parentheses):")
    lines.append("")
    header = "| parity \\ concurrency | " + " | ".join(
        f"{m}x ({inputs['concurrency_per_unit'] * m:g}/unit)" for m in multipliers
    ) + " |"
    lines.append(header)
    lines.append("|" + "---|" * (len(multipliers) + 1))
    by_key = {(c["parity"], c["concurrency_multiplier"]): c for c in cells}
    for parity in parities:
        row = [f"| {parity:g}"]
        for multiplier in multipliers:
            cell = by_key[(parity, multiplier)]
            units = "—" if cell["units_needed"] is None else str(cell["units_needed"])
            row.append(f"{_months(cell['break_even_month'])} ({units})")
        lines.append(" | ".join(row) + " |")
    lines += [
        "",
        "12-month ROI (same grid):",
        "",
    ]
    lines.append(header)
    lines.append("|" + "---|" * (len(multipliers) + 1))
    for parity in parities:
        row = [f"| {parity:g}"]
        for multiplier in multipliers:
            cell = by_key[(parity, multiplier)]
            roi = cell["roi_12"]
            row.append("—" if roi is None else f"{roi:+.0%}")
        lines.append(" | ".join(row) + " |")
    return "\n".join(lines)


# --------------------------------------------------------------------------- CLI


def build_inputs(args: argparse.Namespace) -> tuple[Inputs, dict[str, Any]]:
    values: dict[str, Any] = {
        "seats": args.seats,
        "hours_per_day": args.hours_per_day,
        "work_days_per_month": args.work_days_per_month,
        "tokens_in_per_seat_hour": args.tokens_in_per_seat_hour,
        "tokens_out_per_seat_hour": args.tokens_out_per_seat_hour,
        "cache_read_share": args.cache_read_share,
        "hardware_capex_usd": args.hardware_capex_usd,
        "hardware_count": args.hardware_count,
        "power_w_per_unit": args.power_w_per_unit,
        "usd_per_kwh": args.usd_per_kwh,
        "utilisation": args.utilisation,
        "amortisation_months": args.amortisation_months,
        "ops_hours_per_month": args.ops_hours_per_month,
        "ops_usd_per_hour": args.ops_usd_per_hour,
        "concurrency_per_unit": args.concurrency_per_unit,
        "measured_tps": args.measured_tps,
        "parity": args.parity,
        "retry_factor": args.retry_factor,
    }
    measured: dict[str, Any] = {}
    if args.from_report is not None:
        if not args.provider or not args.cloud_provider:
            raise SystemExit("--from-report needs --provider and --cloud-provider")
        rows = read_report(args.from_report)
        local, cloud = report_rows_for(rows, args.provider, args.cloud_provider)
        overrides = report_overrides(local, cloud)
        values.update(overrides)
        measured["report"] = {args.provider: local, args.cloud_provider: cloud,
                              "overrides": overrides}
        if not overrides:
            print(f"warning: the report rows for {args.provider!r} and {args.cloud_provider!r} "
                  "carry none of the measured fields (tokens, wall_p50_s/wall_p50, "
                  "verified_pass_rate/verified_rate, rounds_per_delegation); "
                  "the default assumptions are kept", file=sys.stderr)
    if args.from_concurrency is not None:
        row = pick_concurrency(read_concurrency(args.from_concurrency))
        values.update(concurrency_overrides(row))
        measured["concurrency"] = row
    if args.cloud_seat_usd is not None:
        values["cloud_price"] = None
        values["cloud_seat"] = CloudSeat(
            args.cloud_seat_usd, args.seat_allowance_tokens, args.seat_overage_usd_per_mtok
        )
    else:
        values["cloud_price"] = CloudPrice(
            args.cloud_price_in, args.cloud_price_out, args.cloud_price_cache_read
        )
        values["cloud_seat"] = None
    return Inputs(**values), measured


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--seats", type=int, default=DEFAULTS["seats"])
    parser.add_argument("--hours-per-day", type=float, default=DEFAULTS["hours_per_day"])
    parser.add_argument(
        "--work-days-per-month", type=float, default=DEFAULTS["work_days_per_month"]
    )
    parser.add_argument(
        "--tokens-in-per-seat-hour", type=float, default=DEFAULTS["tokens_in_per_seat_hour"]
    )
    parser.add_argument(
        "--tokens-out-per-seat-hour", type=float, default=DEFAULTS["tokens_out_per_seat_hour"]
    )
    parser.add_argument("--cache-read-share", type=float, default=DEFAULTS["cache_read_share"])
    parser.add_argument("--cloud-price-in", type=float, default=DEFAULTS["claude_opus5_in"])
    parser.add_argument("--cloud-price-out", type=float, default=DEFAULTS["claude_opus5_out"])
    parser.add_argument(
        "--cloud-price-cache-read",
        type=float,
        default=DEFAULTS["claude_opus5_in"] * DEFAULTS["cache_read_multiplier"],
    )
    parser.add_argument("--cloud-seat-usd", type=float, default=None)
    parser.add_argument("--seat-allowance-tokens", type=float, default=0.0)
    parser.add_argument("--seat-overage-usd-per-mtok", type=float, default=0.0)
    parser.add_argument(
        "--hardware-capex-usd", type=float, default=DEFAULTS["hardware_capex_usd"]
    )
    parser.add_argument("--hardware-count", type=int, default=None)
    parser.add_argument("--power-w-per-unit", type=float, default=DEFAULTS["power_w_per_unit"])
    parser.add_argument("--usd-per-kwh", type=float, default=DEFAULTS["usd_per_kwh"])
    parser.add_argument("--utilisation", type=float, default=DEFAULTS["utilisation"])
    parser.add_argument(
        "--amortisation-months", type=int, default=DEFAULTS["amortisation_months"]
    )
    parser.add_argument(
        "--ops-hours-per-month", type=float, default=DEFAULTS["ops_hours_per_month"]
    )
    parser.add_argument("--ops-usd-per-hour", type=float, default=DEFAULTS["ops_usd_per_hour"])
    parser.add_argument(
        "--concurrency-per-unit", type=float, default=DEFAULTS["concurrency_per_unit"]
    )
    parser.add_argument(
        "--measured-tps", type=float, default=DEFAULTS["per_user_tps_32k_5"]
    )
    parser.add_argument("--parity", type=float, default=DEFAULTS["parity"])
    parser.add_argument("--retry-factor", type=float, default=DEFAULTS["retry_factor"])
    parser.add_argument("--from-report", type=Path, default=None)
    parser.add_argument("--provider", default=None, help="local provider name in the report")
    parser.add_argument("--cloud-provider", default=None, help="cloud provider name in the report")
    parser.add_argument("--from-concurrency", type=Path, default=None)
    parser.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    args = parser.parse_args(argv)

    try:
        inputs, measured = build_inputs(args)
        result = model(inputs)
    except ValueError as exc:
        parser.error(str(exc))
    result["measured"] = measured or None
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(render_markdown(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
