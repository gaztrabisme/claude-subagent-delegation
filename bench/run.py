#!/usr/bin/env python3
"""Benchmark matrix: orchestrator harness x config x task x mode x run.

One cell per combination. Each cell copies the task's `repo/` fresh, commits
it, and runs the orchestrator CLI (claude, codex, gemini, grok or copilot)
with the prompt for the mode. The cell's environment carries
`SUBAGENT_BENCH_RUN_ID=<cell id>`, `SUBAGENT_CONFIG=<derived config>` and
`SUBAGENT_ORCHESTRATOR=<harness>`, and the derived config sets `[core].session_root`
to a directory inside the cell, so delegation traces never interleave.

Afterwards the hidden tests are copied in and run (`run_hidden_tests`), the
cell's `delegation` trace records are read for the worker side, and one row
goes into `results.csv` (+ `results.parquet` when pandas is importable).
`subagent report` draws the dashboard from the cells' traces; a failure there
is logged and the run still succeeds.

  python3 bench/run.py --harness claude --config examples/config.glm.toml \
      --tasks cron --modes alone --runs 1 --dry-run
  python3 bench/run.py --harness claude codex --tasks cron spreadsheet --runs 3 --jobs 2
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
import tomllib
from pathlib import Path

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parent
for _path in (str(ROOT / "src"), str(BENCH)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import harness as bench_harness  # noqa: E402
from harness import prompts  # noqa: E402

from subagent import report as subagent_report  # noqa: E402
from subagent.loop.detect import parse_test_counts  # noqa: E402

DEFAULT_CONFIG = ROOT / "examples" / "config.glm.toml"
DEFAULT_MODES = ["alone", "delegate"]
CSV_COLUMNS = [
    "cell", "harness", "config", "task", "mode", "run",
    "hidden_passed", "hidden_total", "orch_cost_usd",
    "orch_tokens_in", "orch_tokens_out", "orch_tokens_cache", "orch_turns",
    "worker_provider", "worker_tokens_in", "worker_tokens_out", "worker_tokens_cache",
    "worker_credits", "worker_usd", "counterfactual_usd",
    "rounds", "reviews", "verified_pass", "wall_s",
]
# Delegation-record keys this bench reads, all optional: a record missing them
# (older trace, different version) becomes null columns, not a failed run.
NULL_COLUMNS = ("worker_provider", "worker_credits", "worker_usd", "counterfactual_usd",
                "worker_tokens_in", "worker_tokens_out", "worker_tokens_cache",
                "rounds", "reviews", "verified_pass")


def config_label(path: Path) -> str:
    """`glm` from `examples/config.glm.toml`; the file stem for anything else."""
    stem = Path(path).stem
    return stem.split(".", 1)[1] if stem.startswith("config.") else stem


def config_tag(path: Path) -> str:
    """A stable short hash of one config's resolved path and content.

    Cell ids and result filenames carry it: two configs in different
    directories with the same stem (`/tmp/a/config.glm.toml` vs
    `/tmp/b/config.glm.toml`) would otherwise expand to the same cell id and
    race for one workspace.
    """
    resolved = Path(path).resolve()
    digest = hashlib.sha256(str(resolved).encode())
    try:
        digest.update(resolved.read_bytes())
    except OSError:
        pass
    return digest.hexdigest()[:8]


def sh(cmd, cwd, **kw):
    return subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, **kw)


def run_capture(cmd, cwd, env, timeout):
    """(stdout, stderr, timed_out) of one harness run; stderr stays separate."""
    try:
        proc = subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, env=env, timeout=timeout)
        return proc.stdout, proc.stderr, False
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        return (out if isinstance(out, str) else out.decode()), "", True


def run_hidden_tests(task, work):
    """Copy the hidden tests in (only now, after the agent finished) and run them."""
    hidden = BENCH / "tasks" / task / "hidden"
    if any(hidden.glob("*.py")):
        target = work / "_hidden_tests"
        shutil.copytree(hidden, target)
        (target / "__init__.py").touch()
        cmd = ["python3", "-m", "unittest", "discover", "-s", "_hidden_tests", "-t", "."]
    else:
        target = work / ".hidden"
        shutil.copytree(hidden, target)
        cmd = ["node", "--test",
               *sorted(str(p.relative_to(work)) for p in target.glob("*.test.js"))]
    try:
        output = sh(cmd, work, timeout=300).stdout
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + "\nTIMEOUT"
    counts = parse_test_counts(output) or {"total": 0, "passed": 0}
    return counts, output


# --- the per-cell config ---------------------------------------------------------


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)  # a JSON string is a valid TOML basic string
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"bench cannot serialize {type(value).__name__} to TOML")


def toml_dump(data: dict) -> str:
    """The flat-table TOML `bench/run.py` writes: scalars, then sub-tables.

    Only the shapes a bench config has (str, number, bool, list, table).
    `[core]` cannot simply be appended to a copy of the file: TOML forbids
    declaring a table twice.
    """
    lines = ["# Written by bench/run.py for one cell: [core].session_root is the cell's."]

    def emit(prefix, table):
        for key, value in table.items():
            if not isinstance(value, dict):
                if value is None:
                    continue  # TOML has no null: absent means the default
                lines.append(f"{key} = {_toml_value(value)}")
        for key, value in table.items():
            if isinstance(value, dict):
                name = f"{prefix}.{key}" if prefix else str(key)
                lines.extend(["", f"[{name}]"])
                emit(name, value)

    emit("", data)
    return "\n".join(lines) + "\n"


def write_cell_config(source: Path, cell: Path, session_root: Path) -> Path:
    """A copy of the chosen config with `[core].session_root` set to the cell's."""
    data = tomllib.loads(source.read_text(encoding="utf-8"))
    core = data.get("core")
    if not isinstance(core, dict):
        core = {}
        data["core"] = core
    core["session_root"] = str(session_root)
    path = cell / "config.toml"
    path.write_text(toml_dump(data), encoding="utf-8")
    return path


# --- the worker side: the cell's delegation records ------------------------------


def read_delegations(cell: Path) -> list[dict]:
    """Every `delegation` record in the cell's traces; none if the trace is missing."""
    records: list[dict] = []
    root = cell / "sessions"
    if not root.is_dir():
        return records
    for path in sorted(root.rglob("trace.jsonl")):
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("kind") == "delegation":
                records.append(record)
    return records


def read_cell_records(cell: Path) -> tuple[list[dict], list[dict]]:
    """The cell's `delegation` and `run` trace records (either may be empty).

    The run records are priced through `report.worker_usd`, so a flat-plan
    provider's spread reaches the CSV's `worker_usd` too.
    """
    delegations: list[dict] = []
    runs: list[dict] = []
    root = cell / "sessions"
    if not root.is_dir():
        return delegations, runs
    for path in sorted(root.rglob("trace.jsonl")):
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            if record.get("kind") == "delegation":
                delegations.append(record)
            elif record.get("kind") == "run":
                runs.append(record)
    return delegations, runs


def _num(mapping, *names):
    """The first present, numeric key of `mapping` (delegation fields are in flux)."""
    if not isinstance(mapping, dict):
        return None
    for name in names:
        value = mapping.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return None


def delegation_fields(records: list[dict], runs: list[dict] | None = None,
                      config_path: Path | None = None) -> dict:
    """The worker-side CSV fields, summed over the cell's delegation records.

    With the cell's config, `worker_usd` uses the report's allocation policy
    (`report.worker_usd`): a flat-plan month's fee reaches the CSV spread over
    the cell's runs. Without it, only the records' own totals count.
    """
    if not records:
        return dict.fromkeys(NULL_COLUMNS)
    provider = None
    tokens = {"in": 0, "out": 0, "cache": 0}
    credits = counterfactual = 0.0
    rounds = reviews = 0
    verified = None
    for record in records:
        for rnd in (record.get("rounds") or []):
            if isinstance(rnd, dict) and rnd.get("provider"):
                provider = rnd["provider"]  # the worker that ran last
        usage = record.get("usage_total") or {}
        tokens["in"] += _num(usage, "input", "input_tokens") or 0
        tokens["out"] += _num(usage, "output", "output_tokens") or 0
        tokens["cache"] += (_num(usage, "cache_read", "cache_read_input_tokens") or 0) + \
            (_num(usage, "cache_write", "cache_creation_input_tokens") or 0)
        credits += _num(record, "credits_total") or 0
        cost = record.get("cost_total") or {}
        counterfactual += _num(cost, "counterfactual_usd") or 0
        rounds += len(record.get("rounds") or [])
        reviews += len(record.get("reviews") or [])
        if isinstance(record.get("verified_pass"), bool):
            verified = record["verified_pass"]
    if config_path is not None:
        usd = subagent_report.worker_usd(config_path, [*records, *(runs or [])])
    else:
        total = sum(_num(r.get("cost_total"), "provider_usd") or 0 for r in records)
        usd = round(total, 4) if total else None
    return {
        "worker_provider": provider,
        "worker_tokens_in": tokens["in"] or None,
        "worker_tokens_out": tokens["out"] or None,
        "worker_tokens_cache": tokens["cache"] or None,
        "worker_credits": round(credits, 2) if credits else None,
        "worker_usd": round(usd, 4) if usd else None,
        "counterfactual_usd": round(counterfactual, 4) if counterfactual else None,
        "rounds": rounds or None,
        "reviews": reviews or None,
        "verified_pass": verified,
    }


# --- the dashboard ---------------------------------------------------------------


def write_dashboard(out_dir: Path, traces: list[Path]) -> bool:
    """`subagent report --html --csv` over the cells' traces.

    The report is a convenience on top of a finished run: a failure is logged
    and the bench still succeeds.
    """
    cmd = [sys.executable, "-m", "subagent.cli", "report"]
    for trace in traces:
        cmd += ["--trace", str(trace)]
    cmd += ["--html", str(out_dir / "dashboard.html"), "--csv", str(out_dir)]
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              timeout=600)
    except Exception as exc:  # noqa: BLE001
        print(f"dashboard: skipped ({type(exc).__name__}: {exc})", flush=True)
        return False
    if proc.returncode != 0:
        print(f"dashboard: `subagent report` failed ({proc.returncode}): "
              f"{proc.stdout[-1500:].strip()}", flush=True)
        return False
    print(f"dashboard: {out_dir / 'dashboard.html'}", flush=True)
    return True


# --- one cell --------------------------------------------------------------------


def run_one(harness_name: str, config_path: Path, task: str, mode: str, n: int,
            out_dir: Path, model: str | None, timeout: int) -> dict:
    """One matrix cell: run the harness, the hidden tests, read the delegations."""
    orch = bench_harness.get(harness_name)
    cell = cell_id((harness_name, config_path, task, mode, n))
    work = out_dir / cell
    shutil.copytree(BENCH / "tasks" / task / "repo", work)
    sh(["git", "init", "-q"], work)
    sh(["git", "add", "-A"], work)
    sh(["git", "-c", "user.name=bench", "-c", "user.email=bench@local", "commit", "-qm", "start"],
       work)

    config = write_cell_config(config_path, work, work / "sessions")
    env = {**os.environ,
           "SUBAGENT_BENCH_RUN_ID": cell,
           "SUBAGENT_CONFIG": str(config),
           "SUBAGENT_ORCHESTRATOR": harness_name,
           # No live-view windows during benchmark runs.
           "DELEGATE_LIVE_VIEW": "off"}

    argv = orch.argv(prompts(harness_name)[mode], str(work), model)
    started = time.time()
    stdout, stderr, _timed_out = run_capture(argv, work, env, timeout)
    wall = round(time.time() - started)
    (out_dir / f"{cell}.{harness_name}.out").write_text(stdout)
    if stderr.strip():
        (out_dir / f"{cell}.stderr.txt").write_text(stderr)
    parsed = orch.parse(stdout, stderr)

    counts, output = run_hidden_tests(task, work)
    (out_dir / f"{cell}.hidden.txt").write_text(output)
    delegations, runs = read_cell_records(work)
    fields = delegation_fields(delegations, runs, config_path=config)

    usage = parsed["usage"]
    row = {
        "cell": cell,
        "harness": harness_name,
        "config": config_label(config_path),
        "task": task,
        "mode": mode,
        "run": n,
        "hidden_passed": counts.get("passed", 0),
        "hidden_total": counts.get("total", 0),
        "orch_cost_usd": parsed["cost_usd"],
        "orch_tokens_in": usage["input"] or None,
        "orch_tokens_out": usage["output"] or None,
        "orch_tokens_cache": (usage["cache_read"] + usage["cache_write"]) or None,
        "orch_turns": parsed["turns"],
        **fields,
        "wall_s": wall,
    }
    print(f"done {cell_id}: hidden {row['hidden_passed']}/{row['hidden_total']}, "
          f"cost {row['orch_cost_usd']}, credits {row['worker_credits']}, {wall}s", flush=True)
    return row


# --- output ----------------------------------------------------------------------


def write_csv(path: Path, rows: list[dict]) -> None:
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in CSV_COLUMNS})


def write_parquet(rows: list[dict], path: Path) -> bool:
    try:
        import pandas as pd
        pd.DataFrame(rows, columns=CSV_COLUMNS).to_parquet(path, index=False)
    except ImportError:
        return False  # pandas missing, or present with no parquet engine
    return True


def _stat(values, fmt):
    values = [v for v in values if v is not None]
    if not values:
        return "n/a"
    mean = fmt.format(statistics.mean(values))
    if len(values) == 1:
        return mean
    return f"{mean} ({fmt.format(min(values))}–{fmt.format(max(values))})"


def summarize(results: list[dict]) -> str:
    header = "| harness | config | task | mode | runs | hidden tests | orch cost | credits | wall |"
    lines = [header, "|---|---|---|---|---|---|---|---|---|"]
    groups: dict[tuple, list[dict]] = {}
    expected: dict[str, int] = {}
    for r in results:
        groups.setdefault((r["harness"], r["config"], r["task"], r["mode"]), []).append(r)
        # A solution that fails to import reports 1 failing test; use the full count.
        expected[r["task"]] = max(expected.get(r["task"], 0), r["hidden_total"])
    for key, rs in sorted(groups.items()):
        passed = sum(r["hidden_passed"] for r in rs)
        total = expected[key[2]] * len(rs)
        errors = sum(1 for r in rs if r["hidden_total"] == 0)
        lines.append(
            f"| {key[0]} | {key[1]} | {key[2]} | {key[3]} | {len(rs)}"
            f"{f' ({errors} errored)' if errors else ''} | {passed}/{total} "
            f"| {_stat([r['orch_cost_usd'] for r in rs], '${:.2f}')} "
            f"| {_stat([r['worker_credits'] for r in rs], '{:.1f}')} "
            f"| {_stat([r['wall_s'] for r in rs], '{:.0f}s')} |")
    lines += ["", "Values are means, with (min–max) over the runs.", "", "Per cell:", "",
              "| cell | hidden | orch cost | credits | rounds | reviews | wall |",
              "|---|---|---|---|---|---|---|"]
    for r in sorted(results, key=lambda r: r["cell"]):
        cost = f"${r['orch_cost_usd']:.3f}" if r["orch_cost_usd"] is not None else "n/a"
        lines.append(f"| {r['cell']} | {r['hidden_passed']}/{expected[r['task']]} | {cost} "
                     f"| {r['worker_credits']} | {r['rounds']} | {r['reviews']} | {r['wall_s']}s |")
    return "\n".join(lines)


# --- the matrix ------------------------------------------------------------------


def expand_cells(harnesses, configs, tasks, modes, runs):
    """The cell list, in harness × config × task × mode × run order."""
    return [(h, c, t, m, n) for h in harnesses for c in configs
            for t in tasks for m in modes for n in range(1, runs + 1)]


def cell_argv(cell) -> str:
    """The command line one cell would run, for --dry-run."""
    harness, config, task, mode, n = cell
    orch = bench_harness.get(harness)
    return " ".join(orch.argv(prompts(harness)[mode], f"<out>/{cell_id(cell)}", None))


def cell_id(cell) -> str:
    harness, config, task, mode, n = cell
    # The config tag keeps two configs with the same stem on one workspace.
    return f"{harness}-{config_label(config)}-{config_tag(config)}-{task}-{mode}-{n}"


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--harness", nargs="*", default=["claude"],
                        help="orchestrator CLIs: claude codex gemini grok copilot "
                             "(default: claude)")
    parser.add_argument("--config", nargs="*", type=Path, default=[DEFAULT_CONFIG],
                        help="subagent config TOMLs, one per cell factor "
                             "(default: examples/config.glm.toml)")
    parser.add_argument("--tasks", nargs="*", help="task names (default: all)")
    parser.add_argument("--modes", nargs="*", default=DEFAULT_MODES,
                        help="alone, delegate (the size check decides), force (always delegate)")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--jobs", type=int, default=1, help="parallel cells")
    parser.add_argument("--model", default=None,
                        help="model for the orchestrator (default: each CLI's own default)")
    parser.add_argument("--timeout", type=int, default=2400, help="seconds per cell")
    parser.add_argument("--out", type=Path, default=None,
                        help="output directory (default: bench/results/<ts>)")
    parser.add_argument("--dry-run", action="store_true", help="print the cells and exit")
    args = parser.parse_args()

    for name in args.harness:
        if name not in bench_harness.HARNESSES:
            parser.error(f"unknown harness {name!r}; expected one of "
                         f"{', '.join(bench_harness.HARNESSES)}")
    for mode in args.modes:
        if mode not in prompts(args.harness[0]):
            parser.error(f"unknown mode {mode!r}; expected alone, delegate or force")
    configs = []
    for path in args.config:
        path = path if path.is_absolute() else ROOT / path
        if not path.is_file():
            parser.error(f"config not found: {path}")
        configs.append(path)
    tasks = args.tasks or sorted(p.name for p in (BENCH / "tasks").iterdir() if p.is_dir())
    for task in tasks:
        if not (BENCH / "tasks" / task / "repo").is_dir():
            parser.error(f"task not found: {BENCH / 'tasks' / task}")
    if args.runs < 1:
        parser.error("--runs must be 1 or more")

    cells = expand_cells(args.harness, configs, tasks, args.modes, args.runs)
    if args.dry_run:
        for cell in cells:
            print(f"{cell_id(cell)}\n  {cell_argv(cell)}")
        return 0

    out_dir = args.out or BENCH / "results" / time.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True)
    print(f"{len(cells)} cells -> {out_dir}", flush=True)

    results: list[dict] = []
    with cf.ThreadPoolExecutor(args.jobs) as pool:
        futures = {pool.submit(run_one, h, c, t, m, n, out_dir, args.model,
                               args.timeout): (h, c, t, m, n)
                   for h, c, t, m, n in cells}
        for fut in cf.as_completed(futures):
            results.append(fut.result())
            (out_dir / "results.json").write_text(json.dumps(results, indent=2))

    write_csv(out_dir / "results.csv", results)
    if write_parquet(results, out_dir / "results.parquet"):
        print(f"parquet: {out_dir / 'results.parquet'}", flush=True)
    summary = summarize(results)
    (out_dir / "summary.md").write_text(summary + "\n")
    traces = sorted(p for p in out_dir.rglob("trace.jsonl"))
    write_dashboard(out_dir, traces)
    print("\n" + summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
