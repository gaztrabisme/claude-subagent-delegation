#!/usr/bin/env python3
"""Concurrency benchmark for a local lane: 1, 2, 4, 8 children at once.

For each level N, starts N fresh agents on the lane at the same time (fallback
"none", each in its own temporary workspace). Each reads a provided ~200-line
Python file and writes summary.txt naming its three functions; verification is
a grep for one of the names. SAM_<LANE>_MAX_AGENTS and SAM_MAX_AGENTS are
raised to the largest level for the run. One in-process server (this package's
Settings, Registry, Supervisor) serves every level, with a fresh session root.

Per child (bench_children.csv): TTFT, wall, output tokens, decode tok/s (from
its turn records), verified. Per level (bench.csv, one row per level):
aggregate output tok/s (sum of output tokens / wall of the level), median and
p90 child TTFT, peak model_memory_used and peak swap (from metrics.jsonl sample
rows for that level's runs), failures.

The recommendation is the largest N that (1) had no failed children, (2) has
aggregate tok/s at least 1.3x the next lower level's, (3) has p90 TTFT under
60 s, and (4) whose peak swap grew by no more than 512 MB over level 1. The
printed line names the rule that decided it.

    uv run python scripts/bench_concurrency.py --lane omlx --levels 1,2,4,8 --out /tmp/bench
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import statistics
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lane_harness  # noqa: E402

SPEEDUP = 1.3
TTFT_P90_LIMIT = 60.0
SWAP_GROWTH_MB = 512.0

FUNCTIONS = ("tally_harbor_manifest", "reconcile_tide_ledger", "forecast_berth_windows")
TASK = (
    "Read the file module.py in the current working directory. Then create a file named "
    "summary.txt in the same directory that lists the names of the three top-level "
    "functions defined in module.py, one name per line, and nothing else. Do not edit "
    "module.py. Reply with one short line when done."
)
VERIFICATION = f"grep -q {FUNCTIONS[1]} summary.txt"

LEVEL_COLUMNS = [
    "level", "children", "completed", "verified", "failures", "level_wall_s",
    "output_tokens", "aggregate_tps", "ttft_median_s", "ttft_p90_s", "decode_tps_mean",
    "peak_model_memory_used", "peak_swap_mb", "swap_growth_mb",
]
CHILD_COLUMNS = [
    "level", "child", "agent_id", "run_id", "state", "verified", "ttft_s", "wall_s",
    "output_tokens", "decode_tps", "turns", "error",
]


# --- the task file ---------------------------------------------------------------


def module_source() -> str:
    """A deterministic ~200-line Python module with exactly three functions."""
    lines = ['"""Harbor operations helpers: manifests, tide ledgers, berth windows."""', "",
             "from __future__ import annotations", "", "import math", "",
             "PORT_CODES = {"]
    for i in range(24):
        lines.append(f'    "P{i:02d}": ("Harbor {i}", {round(1.5 + i * 0.25, 2)}),')
    lines += ["}", ""]
    bodies = {
        FUNCTIONS[0]: ("Count containers per port in a manifest.",
                       ["totals = {}", "for entry in manifest:",
                        "    code = entry.get('port', 'P00')",
                        "    totals[code] = totals.get(code, 0) + int(entry.get('count', 0))"],
                       "totals"),
        FUNCTIONS[1]: ("Match tide readings against the ledger and report drift.",
                       ["drift = []", "for reading, booked in zip(readings, ledger):",
                        "    delta = float(reading) - float(booked)",
                        "    drift.append(round(delta, 3))"],
                       "drift"),
        FUNCTIONS[2]: ("Predict free berth windows from arrivals and a tide depth.",
                       ["windows = []", "for hour, arrivals in enumerate(schedule):",
                        "    depth = base_depth + math.sin(hour / 12 * math.pi)",
                        "    if arrivals < capacity and depth > 2.0:",
                        "        windows.append(hour)"],
                       "windows"),
    }
    args = {FUNCTIONS[0]: "manifest", FUNCTIONS[1]: "readings, ledger",
            FUNCTIONS[2]: "schedule, base_depth=3.0, capacity=4"}
    for name, (doc, body, result) in bodies.items():
        lines += ["", f"def {name}({args[name]}):", f'    """{doc}', ""]
        for step in range(14):
            lines.append(f"    Step {step + 1}: the operator checks item {step + 1} of the "
                         f"routine before continuing.")
        lines += ['    """']
        lines += [f"    {b}" for b in body]
        for step in range(22):
            lines.append(f"    # note {step + 1}: values are validated upstream; this "
                         f"helper stays pure.")
        lines += [f"    return {result}", ""]
    while len(lines) < 200:
        lines.append(f"# end of module padding line {len(lines)}")
    return "\n".join(lines) + "\n"


# --- aggregation (pure) ----------------------------------------------------------


def percentile(values: Sequence[float], pct: float) -> float | None:
    """Nearest-rank percentile; None for no values."""
    ordered = sorted(v for v in values if v is not None)
    if not ordered:
        return None
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def child_stats(turns: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Output tokens, decode tok/s and turn count from one run's turn records."""
    turns = list(turns)
    output = sum(int(t.get("output") or 0) for t in turns)
    decode_tokens = 0
    decode_seconds = 0.0
    for turn in turns:
        first, end, out = turn.get("ts_first_token"), turn.get("ts_end"), turn.get("output")
        if first is None or end is None or not out or end - first <= 0:
            continue
        decode_tokens += int(out)
        decode_seconds += end - first
    tps = decode_tokens / decode_seconds if decode_seconds > 0 else None
    return {"output_tokens": output, "decode_tps": round(tps, 3) if tps else None,
            "turns": len(turns)}


def _peak(values: Iterable[Any]) -> float | None:
    found = [float(v) for v in values if isinstance(v, (int, float))]
    return max(found) if found else None


def level_stats(level: int, children: Sequence[Mapping[str, Any]], level_wall: float,
                samples: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """One bench.csv row. `samples` are metrics.jsonl sample rows for this level."""
    samples = list(samples)
    output = sum(int(c.get("output_tokens") or 0) for c in children)
    ttfts = [float(c["ttft_s"]) for c in children if c.get("ttft_s") is not None]
    decode = [float(c["decode_tps"]) for c in children if c.get("decode_tps")]
    completed = sum(1 for c in children if c.get("state") in ("completed",
                                                                "completed_unverified"))
    verified = sum(1 for c in children if c.get("verified"))
    median = statistics.median(ttfts) if ttfts else None
    p90 = percentile(ttfts, 90)
    return {
        "level": level,
        "children": len(children),
        "completed": completed,
        "verified": verified,
        "failures": len(children) - verified,
        "level_wall_s": round(level_wall, 3),
        "output_tokens": output,
        "aggregate_tps": round(output / level_wall, 3) if level_wall > 0 else None,
        "ttft_median_s": round(median, 3) if median is not None else None,
        "ttft_p90_s": round(p90, 3) if p90 is not None else None,
        "decode_tps_mean": round(statistics.fmean(decode), 3) if decode else None,
        "peak_model_memory_used": _peak(
            (s.get("snapshot") or {}).get("omlx", {}).get("model_memory_used") for s in samples),
        "peak_swap_mb": _peak(
            (s.get("snapshot") or {}).get("mac", {}).get("swap_used_mb") for s in samples),
        "swap_growth_mb": None,
    }


def with_swap_growth(levels: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fill swap_growth_mb: each level's peak swap minus the first level's."""
    rows = [dict(r) for r in levels]
    base = rows[0].get("peak_swap_mb") if rows else None
    for row in rows:
        peak = row.get("peak_swap_mb")
        row["swap_growth_mb"] = (round(peak - base, 1)
                                 if peak is not None and base is not None else None)
    return rows


def _rule_failures(row: Mapping[str, Any], lower: Mapping[str, Any]) -> list[str]:
    """Which rules `row` breaks against the next lower level `lower`."""
    broken = []
    if row.get("failures"):
        broken.append(f"failures: {row['failures']} of {row['children']} children failed")
    agg, prev = row.get("aggregate_tps"), lower.get("aggregate_tps")
    if agg is None or not prev:
        broken.append("speedup: aggregate tok/s not measured")
    elif agg < SPEEDUP * prev:
        broken.append(f"speedup: aggregate {agg:.1f} tok/s is {agg / prev:.2f}x level "
                      f"{lower['level']}'s {prev:.1f}, under {SPEEDUP}x")
    p90 = row.get("ttft_p90_s")
    if p90 is None:
        broken.append("ttft: p90 TTFT not measured")
    elif p90 >= TTFT_P90_LIMIT:
        broken.append(f"ttft: p90 TTFT {p90:.1f} s is not under {TTFT_P90_LIMIT:g} s")
    growth = row.get("swap_growth_mb")
    if growth is None:
        broken.append("swap: peak swap not measured")
    elif growth > SWAP_GROWTH_MB:
        broken.append(f"swap: peak swap grew {growth:.0f} MB over level 1, more than "
                      f"{SWAP_GROWTH_MB:g} MB")
    return broken


def recommend(levels: Sequence[Mapping[str, Any]]) -> tuple[int | None, str]:
    """(recommended N, the sentence saying which rule decided it).

    The first (lowest) level is the baseline and always qualifies. Every
    higher level is judged against the level just below it; the largest one
    that breaks no rule wins.
    """
    rows = sorted(with_swap_growth(sorted(levels, key=lambda r: r["level"])),
                  key=lambda r: r["level"])
    if not rows:
        return None, "no levels were measured"
    verdicts: list[tuple[int, list[str]]] = []
    for lower, row in zip(rows, rows[1:], strict=False):
        verdicts.append((row["level"], _rule_failures(row, lower)))
    passing = [level for level, broken in verdicts if not broken]
    chosen = max(passing) if passing else rows[0]["level"]
    above = [(level, broken) for level, broken in verdicts if level > chosen and broken]
    if above:
        level, broken = above[0]
        why = f"level {level} broke {broken[0].split(':', 1)[0]} ({'; '.join(broken)})"
    elif chosen == rows[-1]["level"] and len(rows) > 1:
        why = f"level {chosen} passed every rule and is the largest level tested"
    else:
        why = "only one level was measured"
    if not passing and len(rows) > 1:
        why = f"no level above {chosen} passed; {why}"
    return chosen, f"recommend max_agents={chosen}: {why}"


# --- running a level ----------------------------------------------------------------


def run_level(server: lane_harness.InProcessServer, lane: str, level: int, scratch: Path,
              timeout: float) -> tuple[list[dict[str, Any]], float, set[str]]:
    """Start `level` children at once, wait for all. (child rows, level wall, run ids)."""
    source = module_source()
    runs = []
    started = time.time()
    for index in range(level):
        workspace = scratch / f"level{level}" / f"child{index}"
        workspace.mkdir(parents=True)
        (workspace / "module.py").write_text(source)
        runs.append((index, workspace, server.delegate(
            lane=lane, task=TASK, verification=VERIFICATION, workspace=workspace,
            fallback="none", name=f"bench-{level}-{index}")))
    deadline = time.monotonic() + timeout
    for _, _, run in runs:
        run.done.wait(max(0.0, deadline - time.monotonic()))
    finished = [r.finished_at for _, _, r in runs if r.finished_at]
    level_wall = (max(finished) if finished else time.time()) - started
    for _, _, run in runs:
        server.close_agent(run.agent_id)

    rows = []
    for index, _, run in runs:
        stats = child_stats(run.turn_log)
        verification = run.verification_result
        ttft = run.ttft_seconds
        if ttft is None and run.turn_log:
            first = run.turn_log[0]
            if first.get("ts_first_token") is not None:
                ttft = round(first["ts_first_token"] - first["ts_start"], 3)
        rows.append({
            "level": level,
            "child": index,
            "agent_id": run.agent_id,
            "run_id": run.run_id,
            "state": run.state,
            "verified": bool(verification and verification.passed),
            "ttft_s": ttft,
            "wall_s": round((run.finished_at or time.time()) - run.created_at, 3),
            **stats,
            "error": (run.error or "")[:200],
        })
    return rows, level_wall, {run.run_id for _, _, run in runs}


def _write_csv(path: Path, columns: list[str], rows: Iterable[Mapping[str, Any]]) -> None:
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in columns})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--lane", default="omlx", choices=("omlx", "bppc"))
    parser.add_argument("--levels", default="1,2,4,8")
    parser.add_argument("--timeout", type=float, default=900.0,
                        help="seconds per level (also each child's run timeout)")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    levels = sorted({int(x) for x in args.levels.split(",") if x.strip()})
    if not levels or levels[0] <= 0:
        parser.error("--levels must be positive integers")
    top = max(levels)

    scratch = Path(tempfile.mkdtemp(prefix=f"sam-bench-{args.lane}-")).resolve()
    out = args.out.expanduser().resolve() if args.out else scratch
    out.mkdir(parents=True, exist_ok=True)
    env = {
        "SAM_MAX_AGENTS": str(top),
        f"SAM_{args.lane.upper()}_MAX_AGENTS": str(top),
        f"SAM_{args.lane.upper()}_RUN_TIMEOUT": str(int(args.timeout)),
        "SAM_MAX_STEPS": "10",
        "SAM_RATE_LIMIT_RETRIES": "0",
        "SAM_SAMPLE_SECONDS": "5",
    }
    print(f"scratch: {scratch}")
    print(f"out: {out}")
    server = lane_harness.InProcessServer(scratch / "sessions", env).start()
    children: list[dict[str, Any]] = []
    level_rows: list[dict[str, Any]] = []
    try:
        lane = server.settings.lanes[args.lane]
        print(f"lane: {lane.name} model={lane.model} max_agents={lane.max_agents}")
        for level in levels:
            print(f"level {level}: starting {level} children", flush=True)
            rows, wall, run_ids = run_level(server, args.lane, level, scratch, args.timeout)
            children += rows
            samples = [m for m in lane_harness.read_jsonl(server.metrics_path)
                       if m.get("kind") == "sample" and set(m.get("run_ids") or []) & run_ids]
            row = level_stats(level, rows, wall, samples)
            level_rows.append(row)
            print(f"level {level}: wall {row['level_wall_s']} s, aggregate "
                  f"{row['aggregate_tps']} tok/s, ttft p50/p90 {row['ttft_median_s']}/"
                  f"{row['ttft_p90_s']} s, failures {row['failures']}", flush=True)
    finally:
        server.stop()

    level_rows = with_swap_growth(level_rows)
    _write_csv(out / "bench.csv", LEVEL_COLUMNS, level_rows)
    _write_csv(out / "bench_children.csv", CHILD_COLUMNS, children)
    for path in (server.trace_path, server.metrics_path):
        if path.exists():
            shutil.copy2(path, out / path.name)
    chosen, line = recommend(level_rows)
    (out / "recommendation.json").write_text(json.dumps(
        {"lane": args.lane, "recommended": chosen, "reason": line, "levels": level_rows},
        indent=2))
    print(f"bench: {out / 'bench.csv'} ({len(level_rows)} rows)")
    print(f"children: {out / 'bench_children.csv'} ({len(children)} rows)")
    print(line)
    return 0 if len(level_rows) == len(levels) else 1


if __name__ == "__main__":
    sys.exit(main())
