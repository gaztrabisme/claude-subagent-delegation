"""`subagent report`: per-provider and per-delegation tables, and an HTML dashboard.

Reads schema-3 traces (`run`, `hop`, `turn`, `run_summary`, `delegation`) and
prices them with the same rules as `telemetry/cost.py`:

  - `provider_usd` is what the run actually cost. It is taken from the `cost`
    dict the loop wrote at run end (per_token, local, credits). A flat_plan
    subscription is spread at report time: its monthly fee is divided evenly
    over that provider's runs in each UTC month, reusing `provider_costs()`.
  - `counterfactual_usd` is what the same tokens would have cost on the
    counterfactual model, reusing `Pricing.usd()`; when the trace carries no
    `cost`, the `cost.counterfactual_usd` value is used as a fallback.
  - `api_equivalent_usd` prices a subscription provider's tokens at the
    per-token rates listed in `[providers.<n>.pricing.api_equivalent]`, so the
    plan fee is never presented as billed dollars: the column is labelled
    "plan (API-equivalent)".

`cost` and `delegation` are optional: a trace of bare `run` records still
yields the per-provider table, with null USD columns when nothing prices them.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import sys
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ConfigError, load as config_load
from .telemetry.cost import DEFAULT_TRACES, M, Pricing, lane_rows, print_table, provider_costs

TEMPLATE = Path(__file__).with_name("report_template.html")

PROVIDER_FIELDS = [
    "provider", "runs", "verified_passed", "verified_rate", "tokens_in", "tokens_out",
    "tokens_cache_read", "tokens_cache_write", "credits", "provider_usd",
    "api_equivalent_usd", "counterfactual_usd", "saving_usd", "wall_p50", "wall_p90",
    "ttft_p50", "rounds_per_delegation", "guard_denials",
]
DELEGATION_FIELDS = [
    "delegation_id", "bench_run_id", "orchestrator", "providers", "status", "rounds",
    "reviews", "tests", "tokens", "provider_usd", "counterfactual_usd", "wall_seconds",
    "verified_pass",
]


@dataclass
class Report:
    """The tables `build` produces: rich per-provider rows, per-delegation rows,
    and the `cost.py` lane rows the plain text output prints."""

    providers: list[dict[str, Any]]
    delegations: list[dict[str, Any]]
    lanes: list[dict[str, Any]]
    runs: int
    counterfactual: str | None


def _num(v: Any) -> float | None:
    """A number from a trace/config value, or None when unknown."""
    if v is None or (isinstance(v, str) and v.strip().lower() in ("null", "unknown", "")):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ts(rec: Mapping[str, Any]) -> float:
    """A record's epoch timestamp, whether stored as a float or an ISO string."""
    ts = rec.get("ts")
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _percentile(values: list[Any], p: float) -> float | None:
    """The p-th percentile (0..1) of numeric values, linear-interpolated like numpy."""
    nums = sorted(float(v) for v in values if v is not None)
    if not nums:
        return None
    if len(nums) == 1:
        return nums[0]
    rank = (len(nums) - 1) * p
    lo, hi = math.floor(rank), math.ceil(rank)
    if lo == hi:
        return nums[lo]
    return nums[lo] + (nums[hi] - nums[lo]) * (rank - lo)


def _sum_or_none(values: list[Any]) -> float | None:
    """The sum, or None when any value is unknown (USD never treats null as zero)."""
    if any(v is None for v in values):
        return None
    return float(sum(values))


def _read_records(paths: list[Path]) -> list[dict[str, Any]]:
    """All `run` and `delegation` records from the given trace files."""
    records: list[dict[str, Any]] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.is_file():
            print(f"skip missing trace: {path}", file=sys.stderr)
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("kind") in ("run", "delegation"):
                    records.append(rec)
    return records


def _specs(settings: Any) -> dict[str, dict[str, Any]]:
    """Provider name -> {kind, ...values} for every provider that declares pricing."""
    out: dict[str, dict[str, Any]] = {}
    for name, cfg in settings.providers.items():
        kind = cfg.pricing.kind
        if kind and kind != "none":
            out[name] = {"kind": kind, **cfg.pricing.values}
    return out


def _build_pricing(settings: Any) -> Pricing | None:
    """The cost.py `Pricing` over the config's `[pricing]` table, or None.

    The legacy object reads `provider` keyed by lane; the new config keys
    providers by name, so the provider map is injected from the provider
    pricing specs. `None` means no pricing is configured and every USD column
    stays null.
    """
    data = dict(settings.pricing)
    data["provider"] = _specs(settings)
    counterfactual = data.get("counterfactual")
    try:
        return Pricing(data, counterfactual=counterfactual)
    except SystemExit:
        return None


def _run_row(rec: Mapping[str, Any], pricing: Pricing | None, cf_model: str | None,
             specs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    usage = rec.get("usage") or {}
    provider = rec.get("provider") or rec.get("lane") or "?"
    input_t = int(usage.get("input") or 0)
    output_t = int(usage.get("output") or 0)
    cache_read_t = int(usage.get("cache_read") or 0)
    cache_write_t = int(usage.get("cache_write") or 0)
    cost = rec.get("cost")
    cf: float | None = None
    if isinstance(cost, dict):
        if pricing is not None and cf_model:
            cf = pricing.usd(cf_model, input_t, output_t, cache_read_t, cache_write_t, 0)
        if cf is None:
            cf = _num(cost.get("counterfactual_usd"))
    return {
        "lane": provider,
        "provider": provider,
        "model": rec.get("model"),
        "run_id": rec.get("run_id"),
        "ts": _ts(rec),
        "state": rec.get("state"),
        "verification_passed": rec.get("verification_passed"),
        "input": input_t,
        "output": output_t,
        "cache_read": cache_read_t,
        "cache_write": cache_write_t,
        "counterfactual": cf,
        "counterfactual_usd": cf if cf is not None else 0.0,
        "provider_usd": None,
        "parent_usd": 0.0,
        "parent_calls": 0,
        "net_saving_usd": None,
        "net_saving_usd_excl_provider": cf if cf is not None else 0.0,
        "wall_seconds": _num(rec.get("wall_seconds")),
        "ttft_seconds": _num(rec.get("ttft_seconds")),
        "credits": _num(usage.get("credits")) or 0.0,
        "guard_denials": int((rec.get("guard_verdicts") or {}).get("deny") or 0),
        "api_equivalent_usd": None,
        "cost": cost,
        "kind": (specs.get(provider) or {}).get("kind"),
    }


def _price_row(row: dict[str, Any], specs: dict[str, dict[str, Any]]) -> None:
    """Finish one run row's pricing: run-end `cost` wins, then credits and the
    plan's API-equivalent estimate, then the saving column."""
    spec = specs.get(row["provider"])
    cost = row.get("cost")
    if isinstance(cost, dict):
        run_end = _num(cost.get("provider_usd"))
        if run_end is not None:
            row["provider_usd"] = run_end
    else:
        row["provider_usd"] = None  # no `cost` dict: nothing priced this run

    if spec is not None:
        kind = spec["kind"]
        if kind == "credits" and row["provider_usd"] is None:
            rate = _num(spec.get("usd_per_credit"))
            if rate is not None:
                row["provider_usd"] = row["credits"] * rate
        elif kind == "flat_plan":
            ae = spec.get("api_equivalent")
            if isinstance(ae, Mapping):
                rin, rout, rread = (_num(ae.get(k)) for k in ("input", "output", "cache_read"))
                if None not in (rin, rout, rread):
                    row["api_equivalent_usd"] = (
                        (row["input"] + row["cache_write"]) * rin
                        + row["output"] * rout
                        + row["cache_read"] * rread
                    ) / M

    cf = row["counterfactual"]
    row["net_saving_usd"] = (
        (cf - row["provider_usd"]) if (cf is not None and row["provider_usd"] is not None)
        else None
    )


def _rounds_per_delegation(provider: str, delegations: list[dict[str, Any]]) -> float | None:
    """Average rounds per delegation for one provider, over delegations it touched."""
    total, count = 0, 0
    for d in delegations:
        rounds = [
            r for r in (d.get("rounds") or [])
            if isinstance(r, dict) and r.get("provider") == provider
        ]
        if rounds:
            total += len(rounds)
            count += 1
    return (total / count) if count else None


def _provider_rows(run_rows: list[dict[str, Any]],
                   delegations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in run_rows:
        groups[r["provider"]].append(r)
    out: list[dict[str, Any]] = []
    for provider in sorted(groups):
        g = groups[provider]
        n = len(g)
        passed = sum(1 for r in g if r["verification_passed"] is True)
        cf = _sum_or_none([r["counterfactual"] for r in g])
        prov = _sum_or_none([r["provider_usd"] for r in g])
        api = _sum_or_none([r["api_equivalent_usd"] for r in g])
        out.append({
            "provider": provider,
            "runs": n,
            "verified_passed": passed,
            "verified_rate": passed / n if n else None,
            "tokens_in": sum(r["input"] for r in g),
            "tokens_out": sum(r["output"] for r in g),
            "tokens_cache_read": sum(r["cache_read"] for r in g),
            "tokens_cache_write": sum(r["cache_write"] for r in g),
            "credits": sum(r["credits"] for r in g),
            "provider_usd": prov,
            "api_equivalent_usd": api,
            "counterfactual_usd": cf,
            "saving_usd": (cf - prov) if (cf is not None and prov is not None) else None,
            "wall_p50": _percentile([r["wall_seconds"] for r in g], 0.5),
            "wall_p90": _percentile([r["wall_seconds"] for r in g], 0.9),
            "ttft_p50": _percentile([r["ttft_seconds"] for r in g], 0.5),
            "rounds_per_delegation": _rounds_per_delegation(provider, delegations),
            "guard_denials": sum(r["guard_denials"] for r in g),
        })
    return out


def _delegation_row(d: Mapping[str, Any]) -> dict[str, Any]:
    rounds = [r for r in (d.get("rounds") or []) if isinstance(r, dict)]
    reviews = [r for r in (d.get("reviews") or []) if isinstance(r, dict)]
    providers = sorted({r.get("provider") for r in rounds if r.get("provider")})
    usage_total = d.get("usage_total") or {}
    tokens = _num(usage_total.get("total"))
    if tokens is None:
        tokens = _sum_or_none(
            [_num(usage_total.get(k)) or 0 for k in ("input", "output", "cache_read", "cache_write")]
        )
    if tokens is not None:
        tokens = int(tokens)
    cost_total = d.get("cost_total") or {}
    return {
        "delegation_id": d.get("delegation_id"),
        "bench_run_id": d.get("bench_run_id"),
        "orchestrator": d.get("orchestrator"),
        "providers": providers,
        "status": d.get("status"),
        "rounds": len(rounds),
        "reviews": len(reviews),
        "tests": sum(int(r.get("tests") or 0) for r in rounds),
        "tokens": tokens,
        "provider_usd": _num(cost_total.get("provider_usd")),
        "counterfactual_usd": _num(cost_total.get("counterfactual_usd")),
        "wall_seconds": _num(d.get("wall_seconds")),
        "verified_pass": d.get("verified_pass"),
        "ts": _ts(d),
    }


def build(traces: list[Path], since: dt.datetime | None, bench_run: str | None) -> Report:
    """Build the report from schema-3 trace files, filtered by `since`/`bench_run`."""
    settings = config_load()
    records = _read_records(traces)
    if since is not None:
        cutoff = since.timestamp()
        records = [r for r in records if _ts(r) >= cutoff]
    if bench_run is not None:
        records = [r for r in records if r.get("bench_run_id") == bench_run]

    pricing = _build_pricing(settings)
    specs = _specs(settings)
    cf_model = pricing.resolve(pricing.counterfactual) if pricing is not None else None

    run_rows = [_run_row(r, pricing, cf_model, specs) for r in records if r.get("kind") == "run"]
    if pricing is not None:
        provider_costs(pricing, run_rows)
    for row in run_rows:
        _price_row(row, specs)

    delegation_records = sorted(
        (r for r in records if r.get("kind") == "delegation"), key=_ts
    )
    delegation_rows = [_delegation_row(d) for d in delegation_records]
    return Report(
        providers=_provider_rows(run_rows, delegation_records),
        delegations=delegation_rows,
        lanes=lane_rows(run_rows),
        runs=len(run_rows),
        counterfactual=cf_model,
    )


def _report_data(report: Report) -> dict[str, Any]:
    return {
        "meta": {
            "generated": dt.datetime.now(dt.UTC).isoformat(),
            "runs": report.runs,
            "delegations": len(report.delegations),
            "counterfactual": report.counterfactual,
        },
        "providers": report.providers,
        "delegations": report.delegations,
    }


def render_html(report: Report) -> str:
    """The self-contained dashboard: the template with the JSON data embedded."""
    data = json.dumps(_report_data(report)).replace("</", "<\\/")
    return TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", data)


def _fmt(v: Any, kind: str) -> str:
    if v is None:
        return "null"
    if kind == "usd":
        return f"{v:,.2f}"
    if kind == "int":
        return f"{int(v):,}"
    if kind == "pct":
        return f"{v * 100:.0f}%"
    if kind == "wall":
        return f"{v:,.1f}"
    return str(v)


def _print_delegations(rows: list[dict[str, Any]], out: Any = sys.stdout) -> None:
    cols = [
        ("delegation_id", "delegation", "str"),
        ("bench_run_id", "bench", "str"),
        ("providers", "provider", "str"),
        ("status", "status", "str"),
        ("rounds", "rounds", "int"),
        ("reviews", "reviews", "int"),
        ("tests", "tests", "int"),
        ("tokens", "tokens", "int"),
        ("provider_usd", "usd", "usd"),
        ("wall_seconds", "wall_s", "wall"),
    ]
    body: list[list[str]] = []
    for r in rows:
        body.append([
            _fmt(r.get("delegation_id"), "str"),
            _fmt(r.get("bench_run_id"), "str"),
            ",".join(r.get("providers") or []),
            _fmt(r.get("status"), "str"),
            _fmt(r.get("rounds"), "int"),
            _fmt(r.get("reviews"), "int"),
            _fmt(r.get("tests"), "int"),
            _fmt(r.get("tokens"), "int"),
            _fmt(r.get("provider_usd"), "usd"),
            _fmt(r.get("wall_seconds"), "wall"),
        ])
    widths = [
        max(len(h), *(len(b[i]) for b in body)) if body else len(h)
        for i, (_k, h, _kind) in enumerate(cols)
    ]
    print("  ".join(h.ljust(w) if i == 0 else h.rjust(w)
                    for i, ((_k, h, _), w) in enumerate(zip(cols, widths, strict=True))), file=out)
    print("-" * (sum(widths) + 2 * (len(widths) - 1)), file=out)
    for b in body:
        print("  ".join(v.ljust(w) if i == 0 else v.rjust(w)
                        for i, (v, w) in enumerate(zip(b, widths, strict=True))), file=out)


def _csv_cell(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    return v


def _write_csv(report: Report, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, fields, rows in (
        ("providers.csv", PROVIDER_FIELDS, report.providers),
        ("delegations.csv", DELEGATION_FIELDS, report.delegations),
    ):
        with open(out_dir / name, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({k: _csv_cell(row.get(k)) for k in fields})


def _parse_since(text: str | None) -> dt.datetime | None:
    if not text:
        return None
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="subagent report", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--trace", nargs="+", type=Path, default=DEFAULT_TRACES,
                    help="trace files to read (default: the usual session traces)")
    ap.add_argument("--since", metavar="ISO", default=None,
                    help="only records at or after this ISO timestamp")
    ap.add_argument("--bench-run", dest="bench_run", metavar="ID", default=None,
                    help="only records from this bench run")
    ap.add_argument("--json", action="store_true", help="print JSON instead of tables")
    ap.add_argument("--csv", metavar="DIR", type=Path, default=None,
                    help="write providers.csv and delegations.csv into DIR")
    ap.add_argument("--html", metavar="FILE", type=Path, default=None,
                    help="write the self-contained dashboard to FILE")
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "report":
        raw = raw[1:]
    args = ap.parse_args(raw)

    try:
        report = build(args.trace, _parse_since(args.since), args.bench_run)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.csv:
        _write_csv(report, args.csv)
        print(f"wrote {args.csv}/providers.csv, delegations.csv")

    if args.json:
        print(json.dumps(_report_data(report), indent=2))
        return 0

    print(f"counterfactual model: {report.counterfactual or 'unknown'}")
    print(f"runs: {report.runs}  delegations: {len(report.delegations)}")
    print_table(report.lanes)
    print()
    _print_delegations(report.delegations)

    if args.html:
        args.html.parent.mkdir(parents=True, exist_ok=True)
        args.html.write_text(render_html(report), encoding="utf-8")
        print(f"wrote {args.html}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
