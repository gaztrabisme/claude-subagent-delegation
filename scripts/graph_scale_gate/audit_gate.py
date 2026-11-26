"""Acceptance audit for the durable-graph scale gate on real lanes.

Usage: python audit_gate.py report --log <audit.jsonl> --trace <subagent trace.jsonl>

Prints one ``PASS|FAIL | <n>. <name> | <measured values>`` line per condition and exits
nonzero if any condition fails. The log contract is LOG_SCHEMA.md next to this file.
Only counts, ids of the gate's own making and numbers are printed: never a field value
that could carry a secret or an environment value.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

PLANNED_CHILDREN = 200
PLANNED_ROOTS = 2
MAX_DEPTH = 3
MIN_COMPLETED_BEFORE_KILL = 30
MIN_LANE_COMPLETED = {"omlx": 100, "deepseek": 1}
COLD_START_LIMIT = 5.0
LEDGER_TOLERANCE = 0.001
OUTCOMES = {"completed", "failed", "dead", "budget_exhausted"}
ROOT_STATUSES = {"completed", "failed", "budget_exhausted"}
EPS = 1e-9


class Log:
    """The audit events grouped by type, plus the count of malformed lines."""

    def __init__(self, events: list[dict[str, Any]], malformed: int) -> None:
        self.events = events
        self.malformed = malformed
        self.by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in events:
            self.by_type[str(event.get("type", ""))].append(event)

    def of(self, kind: str) -> list[dict[str, Any]]:
        return self.by_type.get(kind, [])

    def first(self, kind: str) -> dict[str, Any] | None:
        rows = self.of(kind)
        return rows[0] if rows else None

    @property
    def config(self) -> dict[str, Any]:
        rows = self.of("config")
        return rows[0] if len(rows) == 1 else {}

    def plan(self) -> tuple[dict[str, dict[str, Any]], int]:
        """Planned requests by id, and the number of duplicate request ids."""
        planned: dict[str, dict[str, Any]] = {}
        duplicates = 0
        for record in self.of("plan"):
            for entry in record.get("requests") or []:
                if not isinstance(entry, dict):
                    duplicates += 1
                    continue
                rid = entry.get("request_id")
                if rid is None or rid in planned:
                    duplicates += 1
                    continue
                planned[rid] = entry
        return planned, duplicates


def read_log(path: Path) -> Log:
    events: list[dict[str, Any]] = []
    malformed = 0
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if not isinstance(record, dict) or not isinstance(record.get("type"), str):
                malformed += 1
                continue
            try:
                record["ts"] = float(record["ts"])
            except (KeyError, TypeError, ValueError):
                malformed += 1
                continue
            if not math.isfinite(record["ts"]):
                malformed += 1
                continue
            events.append(record)
    return Log(events, malformed)


def read_trace_costs(path: Path, wanted: set[str]) -> dict[str, float | None]:
    """Sum of cost.provider_usd per wanted run_id over the trace's run records.

    A run_id whose run record carries no numeric provider_usd maps to None (unpriced).
    A run_id with no run record is absent from the result.
    """
    costs: dict[str, float | None] = {}
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if '"run"' not in line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or record.get("kind") != "run":
                continue
            rid = record.get("run_id")
            if rid not in wanted:
                continue
            cost = record.get("cost")
            amount = cost.get("provider_usd") if isinstance(cost, dict) else None
            value = _num(amount)
            if value is None or (rid in costs and costs[rid] is None):
                costs[rid] = None  # one unpriced record leaves the run unpriced
                continue
            costs[rid] = (costs.get(rid) or 0.0) + value
    return costs


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _child_key(event: dict[str, Any]) -> tuple[Any, Any]:
    return (event.get("request_id"), event.get("attempt"))


def _completed(log: Log) -> list[dict[str, Any]]:
    return [e for e in log.of("child_end") if e.get("outcome") == "completed"]


# ---------------------------------------------------------------- conditions


def cond_requests(log: Log, _trace: Path) -> tuple[bool, str]:
    planned, plan_dupes = log.plan()
    cfg_planned = log.config.get("planned_children")
    ends = Counter(e.get("request_id") for e in log.of("request_end"))
    missing_end = sum(1 for rid in planned if ends.get(rid, 0) == 0)
    multi_end = sum(1 for rid in planned if ends.get(rid, 0) > 1)
    unplanned_end = sum(1 for rid in ends if rid not in planned)
    bad_outcome = sum(1 for e in log.of("request_end") if e.get("outcome") not in OUTCOMES)
    child_ids = {e.get("request_id") for e in log.of("child_start") + log.of("child_end")}
    unplanned_child = sum(1 for rid in child_ids if rid not in planned)

    completed_by_req: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for event in _completed(log):
        completed_by_req[event.get("request_id")].append(event)
    multi_completed = sum(1 for rows in completed_by_req.values() if len(rows) > 1)

    completed_ends = [e for e in log.of("request_end") if e.get("outcome") == "completed"]
    mismatched = 0
    run_ids: list[Any] = []
    for end in completed_ends:
        rid = end.get("run_id")
        rows = completed_by_req.get(end.get("request_id"), [])
        if not isinstance(rid, str) or not rid or len(rows) != 1 \
                or rows[0].get("run_id") != rid:
            mismatched += 1
        run_ids.append(rid)
    # A completed child whose request did not end as completed is also a mismatch.
    completed_req_ids = {e.get("request_id") for e in completed_ends}
    mismatched += sum(1 for rid in completed_by_req if rid not in completed_req_ids)
    duplicate_run_ids = len(run_ids) - len(set(run_ids))

    ok = (cfg_planned == PLANNED_CHILDREN and len(planned) == PLANNED_CHILDREN
          and plan_dupes == 0 and missing_end == 0 and multi_end == 0
          and unplanned_end == 0 and bad_outcome == 0 and unplanned_child == 0
          and multi_completed == 0 and mismatched == 0 and duplicate_run_ids == 0)
    measured = (f"planned={len(planned)}/{PLANNED_CHILDREN}, config_planned={cfg_planned}, "
                f"plan_duplicates={plan_dupes}, missing_terminal={missing_end}, "
                f"multiple_terminal={multi_end}, unplanned_terminal={unplanned_end}, "
                f"bad_outcome={bad_outcome}, unplanned_child={unplanned_child}, "
                f"completed={len(completed_ends)}, multi_completed_dispatch={multi_completed}, "
                f"run_id_mismatch={mismatched}, duplicate_run_ids={duplicate_run_ids}")
    return ok, measured


def cond_admission(log: Log, _trace: Path) -> tuple[bool, str]:
    caps_raw = log.config.get("lane_caps")
    caps = caps_raw if isinstance(caps_raw, dict) else {}
    planned, _ = log.plan()
    starts: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    ends: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for event in log.of("child_start"):
        starts[_child_key(event)].append(event)
    for event in log.of("child_end"):
        ends[_child_key(event)].append(event)
    unmatched_start = sum(1 for key in starts if not ends.get(key))
    unmatched_end = sum(1 for key in ends if not starts.get(key))
    duplicated = sum(1 for key in set(starts) | set(ends)
                     if len(starts.get(key, [])) > 1 or len(ends.get(key, [])) > 1)

    points: dict[str, list[tuple[float, int]]] = defaultdict(list)
    lane_mismatch = 0
    inverted = 0
    for key, rows in starts.items():
        start = rows[0]
        lane = start.get("lane")
        plan_lane = planned.get(start.get("request_id"), {}).get("lane")
        end_rows = ends.get(key)
        if plan_lane != lane or (end_rows and end_rows[0].get("lane") != lane):
            lane_mismatch += 1
        if not end_rows:
            continue
        if end_rows[0]["ts"] < start["ts"]:
            inverted += 1
        points[str(lane)].append((start["ts"], 1))
        points[str(lane)].append((end_rows[0]["ts"], -1))
    maxima: dict[str, int] = {}
    for lane, marks in points.items():
        active = peak = 0
        for _, delta in sorted(marks):
            active += delta
            peak = max(peak, active)
        maxima[lane] = peak
    uncapped = sorted(lane for lane in maxima if not isinstance(caps.get(lane), int)
                      or isinstance(caps.get(lane), bool))
    over = sorted(lane for lane, peak in maxima.items()
                  if lane not in uncapped and peak > caps[lane])
    ok = (bool(caps) and bool(maxima) and not uncapped and not over
          and unmatched_start == 0 and unmatched_end == 0 and duplicated == 0
          and lane_mismatch == 0 and inverted == 0)
    lanes = ", ".join(f"max_concurrent({lane})={maxima[lane]}/{caps.get(lane, 'none')}"
                      for lane in sorted(maxima)) or "no intervals"
    measured = (f"{lanes}, uncapped_lanes={len(uncapped)}, over_cap_lanes={len(over)}, "
                f"start_without_end={unmatched_start}, end_without_start={unmatched_end}, "
                f"duplicate_events={duplicated}, lane_mismatch={lane_mismatch}, "
                f"end_before_start={inverted}")
    return ok, measured


def cond_budget(log: Log, trace: Path) -> tuple[bool, str]:
    cap = _num(log.config.get("budget_cap_usd"))
    reserves = Counter(e.get("reservation_id") for e in log.of("budget_reserve"))
    settles = Counter(e.get("reservation_id") for e in log.of("budget_settle"))
    open_res = sum(1 for rid in reserves if settles.get(rid, 0) == 0)
    double = sum(1 for rid in set(reserves) | set(settles)
                 if reserves.get(rid, 0) > 1 or settles.get(rid, 0) > 1)
    orphan_settle = sum(1 for rid in settles if rid not in reserves)
    committed = 0.0
    bad_amount = 0
    null_run_spend = 0
    settled_runs: set[str] = set()
    for event in log.of("budget_settle"):
        amount = _num(event.get("actual_usd"))
        if amount is None or amount < 0:
            bad_amount += 1
            continue
        committed += amount
        run_id = event.get("run_id")
        if isinstance(run_id, str) and run_id:
            settled_runs.add(run_id)
        elif amount > 0:
            null_run_spend += 1
    rejects = log.of("budget_reject")
    rejected_ids = {e.get("request_id") for e in rejects}
    exhausted_unrejected = sum(
        1 for e in log.of("request_end")
        if e.get("outcome") == "budget_exhausted" and e.get("request_id") not in rejected_ids)
    unsettled_children = sum(
        1 for e in log.of("child_end")
        if isinstance(e.get("run_id"), str) and e.get("run_id")
        and e.get("run_id") not in settled_runs)

    costs = read_trace_costs(trace, settled_runs)
    missing = sum(1 for rid in settled_runs if rid not in costs)
    unpriced = sum(1 for rid in settled_runs if rid in costs and costs[rid] is None)
    trace_total = sum(v for v in costs.values() if v is not None)
    diff = abs(committed - trace_total)

    ok = (cap is not None and committed <= cap + EPS and open_res == 0 and double == 0
          and orphan_settle == 0 and bad_amount == 0 and len(rejects) >= 1
          and exhausted_unrejected == 0 and unsettled_children == 0 and null_run_spend == 0
          and missing == 0 and unpriced == 0 and diff <= LEDGER_TOLERANCE)
    cap_text = f"${cap:.4f}" if cap is not None else "missing"
    measured = (f"committed=${committed:.4f}, cap={cap_text}, open_reservations={open_res}, "
                f"double_reserve_or_settle={double}, settle_without_reserve={orphan_settle}, "
                f"bad_amount={bad_amount}, rejects={len(rejects)}, "
                f"exhausted_without_reject={exhausted_unrejected}, "
                f"unsettled_child_runs={unsettled_children}, null_run_spend={null_run_spend}, "
                f"trace_total=${trace_total:.4f}, ledger_trace_diff=${diff:.4f}, "
                f"runs_missing_in_trace={missing}, runs_unpriced={unpriced}")
    return ok, measured


def cond_owner_pause(log: Log, _trace: Path) -> tuple[bool, str]:
    pause = log.first("owner_paused")
    killed = log.first("client_killed")
    stopped = log.first("server_stopped")
    restarted = log.first("client_restarted")
    signal = log.first("owner_signal")
    resumed = log.first("owner_resumed")
    ready = None
    if stopped:
        ready = next((e for e in log.of("server_ready")
                      if e.get("restart") is True and e["ts"] > stopped["ts"]), None)
    before_kill = sum(1 for e in _completed(log) if killed and e["ts"] < killed["ts"])
    chain = [pause, killed, stopped, ready, restarted, signal, resumed]
    ordered = all(chain) and all(
        chain[i]["ts"] < chain[i + 1]["ts"] for i in range(len(chain) - 1))  # type: ignore[index]
    wf = pause.get("workflow_id") if pause else None
    same_wf = bool(wf) and bool(signal) and bool(resumed) and \
        signal.get("workflow_id") == wf and resumed.get("workflow_id") == wf  # type: ignore[union-attr]

    planned, _ = log.plan()
    gated = {rid for rid, entry in planned.items() if entry.get("gated_by_owner") is True}
    outside = sum(1 for rid in gated if not isinstance(wf, str) or not (
        planned[rid].get("workflow_id") == wf
        or str(planned[rid].get("workflow_id", "")).startswith(f"{wf}/")))
    resume_ts = resumed["ts"] if resumed else math.inf
    early_start = sum(1 for e in log.of("child_start")
                      if e.get("request_id") in gated and e["ts"] <= resume_ts)
    ends = {e.get("request_id"): e for e in log.of("request_end")}
    unfinished = sum(1 for rid in gated
                     if rid not in ends or ends[rid]["ts"] <= resume_ts)
    root = pause.get("root_id") if pause else None
    root_after = any(e.get("root_id") == root and e["ts"] > resume_ts
                     for e in log.of("root_done")) if root else False

    ok = (ordered and before_kill >= MIN_COMPLETED_BEFORE_KILL and same_wf
          and len(gated) >= 1 and outside == 0 and early_start == 0 and unfinished == 0
          and root_after)
    present = "".join("1" if item else "0" for item in chain)
    measured = (f"events_present(paused,killed,stopped,ready,restarted,signal,resumed)={present}, "
                f"ordered={ordered}, completed_before_kill={before_kill}/"
                f"{MIN_COMPLETED_BEFORE_KILL}, same_workflow={same_wf}, gated={len(gated)}, "
                f"gated_outside_subtree={outside}, gated_started_before_resume={early_start}, "
                f"gated_unfinished_after_resume={unfinished}, root_done_after_resume={root_after}")
    return ok, measured


def cond_shape(log: Log, _trace: Path) -> tuple[bool, str]:
    planned, _ = log.plan()
    roots = {entry.get("root_id") for entry in planned.values()}
    submits = log.of("client_submit")
    submitted = {e.get("root_id") for e in submits}
    pids = {e.get("client_pid") for e in submits if e.get("client_pid") is not None}
    completed = _completed(log)
    depth: dict[Any, int] = defaultdict(int)
    for event in completed:
        d = event.get("depth")
        if isinstance(d, int) and not isinstance(d, bool):
            depth[event.get("root_id")] = max(depth[event.get("root_id")], d)
    depth_ok = len(roots) == PLANNED_ROOTS and all(depth.get(r) == MAX_DEPTH for r in roots)
    lanes = Counter(e.get("lane") for e in completed)
    lanes_ok = all(lanes.get(lane, 0) >= need for lane, need in MIN_LANE_COMPLETED.items())
    unverified = sum(1 for e in completed if e.get("verified") is not True)
    ok = (depth_ok and submitted == roots and len(pids) >= 2 and lanes_ok and unverified == 0
          and log.config.get("planned_roots") == PLANNED_ROOTS)
    depth_text = "/".join(str(depth.get(r, 0)) for r in sorted(roots, key=str)) or "none"
    lane_text = ", ".join(f"{lane}={lanes.get(lane, 0)}/{need}"
                          for lane, need in MIN_LANE_COMPLETED.items())
    measured = (f"roots={len(roots)}/{PLANNED_ROOTS}, submitted_roots_match={submitted == roots}, "
                f"max_depth_per_root={depth_text} (need {MAX_DEPTH}), "
                f"client_pids={len(pids)}, completed {lane_text}, unverified={unverified}")
    return ok, measured


def cond_health(log: Log, _trace: Path) -> tuple[bool, str]:
    errors = len(log.of("runtime_error"))
    planned, _ = log.plan()
    roots = {entry.get("root_id") for entry in planned.values()}
    done = Counter(e.get("root_id") for e in log.of("root_done")
                   if e.get("status") in ROOT_STATUSES)
    bad_status = sum(1 for e in log.of("root_done") if e.get("status") not in ROOT_STATUSES)
    roots_ok = bool(roots) and all(done.get(r) == 1 for r in roots) and bad_status == 0 \
        and set(done) <= roots
    shutdowns = log.of("shutdown")
    shutdown = shutdowns[0] if len(shutdowns) == 1 else {}
    clean = shutdown.get("clean") is True and shutdown.get("orphan_pids") == []
    readies = log.of("server_ready")
    initial = next((e for e in readies if e.get("restart") is False), None)
    colds = [_num(e.get("cold_start_seconds")) for e in readies]
    worst = max((c for c in colds if c is not None), default=None)
    cold_ok = initial is not None and bool(colds) and all(
        c is not None and 0 <= c <= COLD_START_LIMIT for c in colds)
    ok = errors == 0 and log.malformed == 0 and roots_ok and clean and cold_ok
    worst_text = f"{worst:.3f}s" if worst is not None else "none"
    measured = (f"runtime_errors={errors}, malformed_lines={log.malformed}, "
                f"roots_done={sum(1 for r in roots if done.get(r) == 1)}/{len(roots)}, "
                f"bad_root_status={bad_status}, shutdown_records={len(shutdowns)}, "
                f"clean_shutdown={clean}, "
                f"orphan_pids={len(shutdown.get('orphan_pids') or []) if shutdown else 'n/a'}, "
                f"initial_ready={initial is not None}, "
                f"max_cold_start={worst_text}/{COLD_START_LIMIT:.1f}s")
    return ok, measured


CONDITIONS: list[tuple[str, Callable[[Log, Path], tuple[bool, str]]]] = [
    ("Requests end once, no duplicate completions", cond_requests),
    ("Zero over-admission per lane", cond_admission),
    ("Zero budget overrun; ledger matches trace", cond_budget),
    ("Owner pause survives kill and restart", cond_owner_pause),
    ("Shape: 3 levels, 2 clients, both lanes, verified", cond_shape),
    ("Health: no errors, roots done, clean shutdown, cold start", cond_health),
]


def report(log_path: Path, trace_path: Path, out: Callable[[str], Any] = print) -> int:
    log: Log | None
    reason = ""
    try:
        log = read_log(log_path)
    except OSError as exc:
        log = None
        reason = f"audit log unreadable ({type(exc).__name__})"
    trace_reason = "" if trace_path.is_file() else "trace unreadable"
    rows: list[tuple[str, bool, str]] = []
    for number, (name, check) in enumerate(CONDITIONS, 1):
        label = f"{number}. {name}"
        if log is None:
            rows.append((label, False, reason))
            continue
        if trace_reason and check is cond_budget:
            rows.append((label, False, trace_reason))
            continue
        try:
            passed, measured = check(log, trace_path)
        except Exception as exc:  # a malformed field must fail the condition, not crash
            passed, measured = False, f"audit error ({type(exc).__name__})"
        rows.append((label, passed, measured))
    all_ok = True
    for label, passed, measured in rows:
        all_ok = all_ok and passed
        out(f"{'PASS' if passed else 'FAIL'} | {label} | {measured}")
    return 0 if all_ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    rep = commands.add_parser("report", help="print the six gate conditions")
    rep.add_argument("--log", required=True, type=Path)
    rep.add_argument("--trace", required=True, type=Path)
    args = parser.parse_args(argv)
    return report(args.log, args.trace)


if __name__ == "__main__":
    sys.exit(main())
