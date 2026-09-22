# Business case: cloud tokens vs a self-hosted fleet

For a department head deciding whether to buy GPUs. `bench/business_case.py` turns your
workload, hardware and cloud prices into a monthly cloud cost, a monthly local cost, the
parity- and retry-adjusted local cost, a capacity check, the break-even month, 12/24/36-month
ROI and a sensitivity grid over parity × concurrency.

Every number below is exact; where a figure is a placeholder assumption rather than a published
input it is labelled as such, and measured data replaces it (`--from-report`, `--from-concurrency`).

## Published inputs (read 2026-09-22 — inputs, not results)

| Figure | Value | Source |
|---|---|---|
| RTX PRO 6000 Blackwell 96 GB, street price | $14,000–15,500 (model uses the midpoint $14,750) | https://www.nvidia.com/en-us/design-visualization/rtx-pro-6000/ |
| RTX PRO 6000 Blackwell, board power | 600 W | same |
| RTX 5090, street price | ~$4,900, 575 W (cited alternative, not the default) | https://www.nvidia.com/en-us/geforce/graphics-cards/rtx-5090/ |
| Qwen3.6-27B FP8 on one RTX PRO 6000 via vLLM | ~46 tok/s single stream; ~190 tok/s aggregate at 5 concurrent; ~28 tok/s per user at 32k context × 5 | https://qwenlm.github.io/ |
| Claude Opus 5 | $5 / $25 per M tokens in/out | https://www.anthropic.com/pricing |
| Claude Sonnet 5 | $2 / $10 per M tokens in/out | https://www.anthropic.com/pricing |
| Claude cache reads | 10% of the input price | https://www.anthropic.com/pricing |
| Copilot Business | $19/seat (1,900 credits, $0.01/credit) | https://github.com/features/copilot/plans |
| Copilot Enterprise | $39/seat | https://github.com/features/copilot/plans |
| Claude Team | $25–125/seat | https://www.anthropic.com/pricing |
| Claude Max | $100/$200 | https://www.anthropic.com/pricing |
| DeepSeek v4-pro | $1.32 / $3.96 per M in/out peak (half off-peak) | https://api-docs.deepseek.com/quick_start/pricing |
| GLM-5.3-Flash | $0.15 / $0.50 per M in/out | https://z.ai/pricing |

The model's default cloud price is Opus 5, because that is the repo's counterfactual ("the
Claude list price of the same tokens"). The defaults also hard-code the placeholder assumptions
below; `--from-report` and `--from-concurrency` overwrite the ones they measure.

| Assumption (not published — placeholder) | Value |
|---|---|
| seats | 100 |
| hours per day | 8 |
| work days per month | 21 |
| tokens in per seat-hour | 30,000 |
| tokens out per seat-hour | 6,000 |
| cache read share of input | 0.8 |
| $ per kWh | 0.13 |
| utilisation (fraction of day at full power) | 0.6 |
| amortisation | 36 months |
| ops | 4 h/month × $100/h |
| concurrency per unit | 5 |
| measured tok/s (per stream) | 28 (the published per-user figure) |
| parity (local ÷ cloud pass rate) | 1.0 |
| retry factor (local ÷ cloud rounds) | 1.0 |
| calendar days per month (for power draw) | 30 |

## Formulae, in words

Let seat-hours = `seats × hours_per_day × work_days_per_month`.

- **Monthly token volumes.** `tokens_in = seat-hours × tokens_in_per_seat_hour`;
  `tokens_cache_read = tokens_in × cache_read_share`; `tokens_non_cache_in = tokens_in −
  tokens_cache_read`; `tokens_out = seat-hours × tokens_out_per_seat_hour`.
- **Monthly cloud cost, per-token.** `(tokens_non_cache_in × price_in + tokens_out × price_out
  + tokens_cache_read × price_cache_read) ÷ 1,000,000`, prices in USD per M tokens.
- **Monthly cloud cost, seat.** `seats × monthly_usd + max(0, tokens_in + tokens_out − seats ×
  allowance) × overage ÷ 1,000,000`. Use `--cloud-seat-usd` to switch to this model.
- **Capacity.** Demand is `seats × (tokens_in_per_seat_hour + tokens_out_per_seat_hour)` tokens
  per hour. One unit serves `concurrency_per_unit × measured_tps × 3,600 × utilisation` tokens
  per hour. `units_needed = ceil(demand ÷ unit_capacity)`. If `--hardware-count` is not given,
  the fleet is sized to `units_needed`; the `shortfall` is demand minus the stated fleet's
  installed capacity.
- **Monthly local cost.** `power = units × power_w × utilisation × 24 × 30 ÷ 1,000 ×
  usd_per_kwh`; `capex_amortisation = units × hardware_capex ÷ amortisation_months`; `ops =
  ops_hours × ops_usd_per_hour`. Total is the sum of the three. The cash cost (used for
  break-even and ROI) is `power + ops`; capex is paid upfront.
- **Parity/retry adjustment.** `adjusted = local × retry_factor ÷ parity`. Parity below 1
  (local passes less often) and retry factor above 1 (local needs more rounds) both make local
  effectively more expensive.
- **Break-even (cash basis).** `ceil(capex ÷ (cloud − adjusted power − adjusted ops))`; `0`
  when there is no capex, and *never* when cloud is not more expensive than the adjusted local
  cash cost.
- **ROI.** `(months × (cloud − adjusted cash) − capex) ÷ capex` for 12, 24 and 36 months.
- **Sensitivity.** The same break-even/ROI, recomputed over parity ∈ {0.6, 0.8, 1.0} ×
  concurrency ∈ {1×, 2×, 4× the measured per-unit concurrency}. Each cell buys exactly
  `units_needed` at that concurrency, so higher concurrency means fewer units, less capex and a
  faster payback.

## Worked example (published defaults + placeholders)

Run it with `uv run python bench/business_case.py --seats 100` (or add `--json`).

- Seat-hours: 100 × 8 × 21 = 16,800/month.
- Tokens: in 504,000,000 (cache read 403,200,000, non-cache 100,800,000), out 100,800,000.
- Cloud (Opus 5): (100,800,000 × 5 + 100,800,000 × 25 + 403,200,000 × 0.5) ÷ 1,000,000 =
  **$3,225.60/month**.
- Demand: 100 × 36,000 = 3,600,000 tokens/hour; one unit serves 5 × 28 × 3,600 × 0.6 = 302,400
  tokens/hour → **12 units needed**.
- Capex: 12 × $14,750 = $177,000. Power: 12 × 600 W × 0.6 × 24 × 30 ÷ 1,000 × $0.13 =
  $404.35/month. Ops: $400/month. Amortisation: $177,000 ÷ 36 = $4,916.67/month.
- Local total: **$5,721.02/month** (parity/retry adjusted the same at parity 1.0, retry 1.0).
- Break-even: $177,000 ÷ ($3,225.60 − $804.35) = **74 months** (never within 36).
- ROI: **−84%** at 12 months, **−67%** at 24, **−51%** at 36.

Sensitivity grid (break-even month, units needed in parentheses):

| parity \ concurrency | 1× (5/unit) | 2× (10/unit) | 4× (20/unit) |
|---|---|---|---|
| 0.6 | 94 (12) | 40 (6) | 19 (3) |
| 0.8 | 80 (12) | 36 (6) | 18 (3) |
| 1.0 | 74 (12) | 34 (6) | 17 (3) |

**These are placeholder numbers, not a decision.** The seat-hour token rate, parity and retry
factor must come from a real `subagent report --json` run, and the concurrency/tok/s from
`bench/concurrency.py`:

```sh
uv run python bench/business_case.py \
  --from-report report.json --provider bppc --cloud-provider claude \
  --from-concurrency bench.csv --seats 100 --json
```

The report and concurrency files do not exist yet; the model reads this minimal shape and
ignores anything else. Report per-provider row:

`{provider, tokens_in, tokens_out, tokens_cache_read, verified_pass_rate,
rounds_per_delegation, wall_p50_s, provider_usd, counterfactual_usd}` — `tokens_in` is non-cache
input and cache reads are separate in `tokens_cache_read`. The local row's tokens and
`wall_p50_s` become the per-seat-hour demand (tokens × 3600 ÷ wall_p50_s, delegations back to
back); `parity` is local ÷ cloud `verified_pass_rate`; `retry_factor` is local ÷ cloud
`rounds_per_delegation`; `cache_read_share` is the cloud row's cache reads ÷ (non-cache in +
cache reads). The concurrency CSV columns are `level, aggregate_tps, ttft_p90_s`; the chosen
row is the highest level whose p90 TTFT is under 60 s, and `measured_tps` is `aggregate_tps ÷
level`.

## What the model does not include

- **Rate limits and throttling.** Cloud providers and the local lane both impose them; they are
  not priced.
- **Privacy and data residency.** Self-hosting keeps data on-site, which may be decisive for
  some work but has no dollar value here.
- **Latency floor.** TTFT/latency only appears as the concurrency-acceptability threshold from
  `bench/concurrency.py`; latency itself is not monetised.
- **Vendor lock-in.** Switching costs between cloud providers, and between models, are zero in
  the model.
- **Prefill speed.** The capacity check prices *all* tokens at the per-stream decode rate
  (`measured_tps`), which is deliberately conservative for input-heavy workloads.
- **Bursts and multi-tenancy.** Demand is a flat per-seat-hour average; no peak/burst shaping
  beyond the single concurrency figure.
- **Capital cost of money, cooling, floor space, insurance and resale value.** Capex is
  straight-line over the amortisation window with no financing, and the hardware has no salvage
  value.
- **Electricity price variation and time-of-use tariffs.** One flat $/kWh is used.
