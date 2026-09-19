#!/usr/bin/env python3
"""Benchmark: Claude alone vs Claude + /delegate on the same tasks.

Each task is bench/tasks/<name>/ with:
  repo/     starting project (copied fresh for every run; contains TASK.md)
  hidden/   acceptance tests never shown to the agents; copied in after the run

For every (task, mode, run) it records Claude's cost/tokens (from `claude -p --output-format json`),
wall time, how many worker runs were delegated, and the hidden test pass rate.

  python3 bench/run.py                         # all tasks, both modes, 1 run each
  python3 bench/run.py --tasks expr-eval --runs 3 --jobs 2 --model sonnet
"""

import argparse
import concurrent.futures as cf
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

BENCH = Path(__file__).resolve().parent
PROMPTS = {
    "alone": "Implement the task described in TASK.md in this repository. Verify your work before finishing.",
    "delegate": "/delegate Implement the task described in TASK.md in this repository.",
}
ALLOWED_TOOLS = ["Bash", "Read", "Edit", "Write", "Glob", "Grep", "Skill"]


def sh(cmd, cwd, **kw):
    return subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **kw)


def run_one(task, mode, n, out_dir, model, timeout):
    work = out_dir / f"{task}-{mode}-{n}"
    shutil.copytree(BENCH / "tasks" / task / "repo", work)
    sh(["git", "init", "-q"], work)
    sh(["git", "add", "-A"], work)
    sh(["git", "-c", "user.name=bench", "-c", "user.email=bench@local", "commit", "-qm", "start"], work)

    argv = ["claude", "-p", PROMPTS[mode], "--output-format", "json", "--no-session-persistence",
            "--permission-mode", "acceptEdits", "--allowedTools", *ALLOWED_TOOLS]
    if model:
        argv += ["--model", model]
    started = time.time()
    try:
        proc = sh(argv, work, timeout=timeout)
        raw = proc.stdout
    except subprocess.TimeoutExpired as exc:
        raw = exc.stdout or ""
    wall = round(time.time() - started)
    (work.parent / f"{work.name}.claude.json").write_text(raw if isinstance(raw, str) else raw.decode())

    try:
        claude = json.loads(raw)
    except (ValueError, TypeError):
        claude = {"is_error": True, "result": str(raw)[-2000:]}
    usage = claude.get("usage") or {}

    # Hidden acceptance tests, copied in only now.
    hidden_dir = work / ".hidden"
    shutil.copytree(BENCH / "tasks" / task / "hidden", hidden_dir)
    files = sorted(str(p.relative_to(work)) for p in hidden_dir.glob("*.test.js"))
    tap = sh(["node", "--test", *files], work, timeout=120).stdout
    passed = int((re.search(r"^# pass (\d+)", tap, re.M) or [0, 0])[1])
    failed = int((re.search(r"^# fail (\d+)", tap, re.M) or [0, 0])[1])
    (work.parent / f"{work.name}.hidden.tap").write_text(tap)

    logs = work / ".delegate" / "logs"
    return {
        "task": task, "mode": mode, "run": n,
        "ok": not claude.get("is_error", False),
        "cost_usd": claude.get("total_cost_usd"),
        "input_tokens": usage.get("input_tokens", 0),
        "cache_write_tokens": usage.get("cache_creation_input_tokens", 0),
        "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "turns": claude.get("num_turns"),
        "wall_seconds": wall,
        "worker_runs": len(list(logs.glob("run-*.log"))) if logs.exists() else 0,
        "hidden_passed": passed,
        "hidden_total": passed + failed,
        "permission_denials": len(claude.get("permission_denials") or []),
        "models": sorted((claude.get("modelUsage") or {}).keys()),
    }


def summarize(results):
    lines = [
        "| task | mode | hidden tests | Claude cost | input (uncached+cache write) | cache read | output | turns | wall | Copilot runs |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(results, key=lambda r: (r["task"], r["mode"], r["run"])):
        cost = f"${r['cost_usd']:.3f}" if r["cost_usd"] is not None else "n/a"
        lines.append(
            f"| {r['task']} | {r['mode']}{'' if r['ok'] else ' (error)'} | {r['hidden_passed']}/{r['hidden_total']} | {cost} "
            f"| {r['input_tokens'] + r['cache_write_tokens']:,} | {r['cache_read_tokens']:,} | {r['output_tokens']:,} "
            f"| {r['turns']} | {r['wall_seconds']}s | {r['worker_runs']} |"
        )
    lines += ["", "Averages per mode:", ""]
    for mode in PROMPTS:
        rs = [r for r in results if r["mode"] == mode and r["cost_usd"] is not None]
        if not rs:
            continue
        avg = lambda k: sum(r[k] for r in rs) / len(rs)
        passed = sum(r["hidden_passed"] for r in rs)
        total = sum(r["hidden_total"] for r in rs)
        lines.append(f"- **{mode}**: ${avg('cost_usd'):.3f}/run, {avg('output_tokens'):,.0f} output tokens, "
                     f"{avg('wall_seconds'):.0f}s, hidden tests {passed}/{total}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tasks", nargs="*", help="task names (default: all)")
    parser.add_argument("--modes", nargs="*", default=list(PROMPTS), choices=list(PROMPTS))
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--jobs", type=int, default=1, help="parallel runs")
    parser.add_argument("--model", help="Claude model for both modes (default: your Claude Code default)")
    parser.add_argument("--timeout", type=int, default=2400, help="seconds per Claude run")
    args = parser.parse_args()

    tasks = args.tasks or sorted(p.name for p in (BENCH / "tasks").iterdir() if p.is_dir())
    out_dir = BENCH / "results" / time.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True)
    jobs = [(t, m, n) for t in tasks for m in args.modes for n in range(1, args.runs + 1)]
    print(f"{len(jobs)} runs -> {out_dir}", flush=True)

    results = []
    with cf.ThreadPoolExecutor(args.jobs) as pool:
        futures = {pool.submit(run_one, t, m, n, out_dir, args.model, args.timeout): (t, m, n) for t, m, n in jobs}
        for fut in cf.as_completed(futures):
            r = fut.result()
            results.append(r)
            print(f"done {r['task']}/{r['mode']}#{r['run']}: hidden {r['hidden_passed']}/{r['hidden_total']}, "
                  f"cost {r['cost_usd']}, {r['wall_seconds']}s", flush=True)
            (out_dir / "results.json").write_text(json.dumps(results, indent=2))

    summary = summarize(results)
    (out_dir / "summary.md").write_text(summary + "\n")
    print("\n" + summary)


if __name__ == "__main__":
    main()
