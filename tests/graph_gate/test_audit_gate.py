"""Acceptance tests for scripts/graph_scale_gate/audit_gate.py (written by a non-builder).

Every log here is synthetic: no network, no Temporal, no provider CLI. One good log passes
all six conditions; each bad log breaks exactly one condition and must fail only that one.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "graph_scale_gate" / "audit_gate.py"
LINE = re.compile(r"^(PASS|FAIL) \| ([1-6])\. [^|]+ \| .+$")

ROOTS = ("root-1", "root-2")
PAUSED_WF = "root-1/l2-00"
DEEPSEEK_COST = 0.04
BUDGET_CAP = 0.50  # 12 deepseek children fit; the other 8 are rejected
RETRIED = "root-1-r051"  # first attempt fails, second completes
SECRET = "sk-test-NOT-A-REAL-KEY-0123456789"


def _wf(root: str, index: int, depth: int) -> str:
    branch = f"{root}/l2-{index % 10:02d}"
    return root if depth == 1 else branch if depth == 2 else f"{branch}/l3-0"


def build(*, kill_after: int = 40) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (audit events, trace records) for a run that passes every condition."""
    plan: dict[str, list[dict[str, Any]]] = {root: [] for root in ROOTS}
    for root in ROOTS:
        for i in range(100):
            depth = 1 + i % 3
            wf = _wf(root, i, depth)
            plan[root].append({
                "request_id": f"{root}-r{i:03d}",
                "lane": "deepseek" if i % 10 == 5 else "omlx",
                "root_id": root, "workflow_id": wf, "depth": depth,
                # Late root-1 steps inside the paused branch wait for the owner.
                "gated_by_owner": root == "root-1" and i >= 40 and wf.startswith(PAUSED_WF),
            })
    events: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = []
    clock = [0.0]

    def emit(kind: str, **fields: Any) -> None:
        clock[0] += 0.5
        events.append({"ts": clock[0], "type": kind, **fields})

    emit("config", lane_caps={"omlx": 1, "deepseek": 1}, budget_cap_usd=BUDGET_CAP,
         planned_children=200, planned_roots=2)
    for root in ROOTS:
        emit("plan", requests=plan[root])
    emit("server_ready", cold_start_seconds=1.2, restart=False)
    emit("client_submit", client_pid=1001, root_id="root-1")
    emit("client_submit", client_pid=1002, root_id="root-2")
    emit("owner_paused", workflow_id=PAUSED_WF, root_id="root-1")

    committed = [0.0]
    done = [0]

    def run_child(entry: dict[str, Any], attempt: int, outcome: str) -> str:
        rid = entry["request_id"]
        run_id = f"run-{rid}-a{attempt}"
        cost = DEEPSEEK_COST if entry["lane"] == "deepseek" else 0.0
        base = {k: entry[k] for k in ("request_id", "lane", "root_id", "workflow_id", "depth")}
        base.update(attempt=attempt, provider=entry["lane"], worker_pid=2000)
        emit("budget_reserve", reservation_id=f"res-{rid}-{attempt}", request_id=rid,
             amount_usd=cost)
        emit("child_start", **base)
        emit("child_end", **base, outcome=outcome, run_id=run_id, cost_usd=cost,
             verified=outcome == "completed")
        emit("budget_settle", reservation_id=f"res-{rid}-{attempt}", request_id=rid,
             run_id=run_id, actual_usd=cost)
        committed[0] += cost
        trace.append({"schema": 3, "ts": clock[0], "kind": "hop", "run_id": run_id})
        trace.append({"schema": 3, "ts": clock[0], "kind": "run", "run_id": run_id,
                      "provider": entry["lane"], "state": "completed",
                      "cost": {"provider_usd": cost, "counterfactual_usd": cost * 3,
                               "kind": "api" if cost else "local", "note": ""}})
        return run_id

    def finish(entry: dict[str, Any]) -> None:
        rid = entry["request_id"]
        if entry["lane"] == "deepseek" and committed[0] + DEEPSEEK_COST > BUDGET_CAP + 1e-9:
            emit("budget_reject", request_id=rid, requested_usd=DEEPSEEK_COST,
                 committed_usd=round(committed[0], 4))
            emit("request_end", request_id=rid, outcome="budget_exhausted", run_id=None)
            return
        if rid == RETRIED:
            run_child(entry, 1, "failed")
            run_id = run_child(entry, 2, "completed")
        else:
            run_id = run_child(entry, 1, "completed")
        emit("request_end", request_id=rid, outcome="completed", run_id=run_id)
        done[0] += 1
        if done[0] == kill_after:
            emit("client_killed", client_pid=1001, signal="SIGKILL")
            emit("server_stopped", pid=3000)
            emit("server_ready", cold_start_seconds=2.4, restart=True)
            emit("client_restarted", client_pid=1003, root_id="root-1")
            emit("owner_signal", workflow_id=PAUSED_WF, decision="approve")
            emit("owner_resumed", workflow_id=PAUSED_WF)

    everything = plan["root-1"] + plan["root-2"]
    for entry in [e for e in everything if not e["gated_by_owner"]]:
        finish(entry)
    for entry in [e for e in everything if e["gated_by_owner"]]:
        finish(entry)
    emit("root_done", root_id="root-1", status="budget_exhausted")
    emit("root_done", root_id="root-2", status="budget_exhausted")
    emit("shutdown", clean=True, orphan_pids=[])
    return events, trace


def run_gate(tmp_path: Path, events: list[Any], trace: list[Any] | None,
             extra_log_lines: list[str] = (), extra_trace_lines: list[str] = ()
             ) -> tuple[int, dict[int, str], str]:
    log = tmp_path / "audit.jsonl"
    log.write_text("".join(json.dumps(e) + "\n" for e in events)
                   + "".join(line + "\n" for line in extra_log_lines), encoding="utf-8")
    trace_path = tmp_path / "trace.jsonl"
    if trace is not None:
        trace_path.write_text(
            "".join(json.dumps(r) + "\n" for r in trace)
            + "".join(line + "\n" for line in extra_trace_lines), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "report", "--log", str(log), "--trace", str(trace_path)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    verdicts: dict[int, str] = {}
    for line in lines:
        match = LINE.match(line)
        assert match, f"unexpected output line: {line!r}"
        verdicts[int(match.group(2))] = match.group(1)
    assert sorted(verdicts) == [1, 2, 3, 4, 5, 6], proc.stdout + proc.stderr
    return proc.returncode, verdicts, proc.stdout + proc.stderr


def assert_only_fails(tmp_path: Path, events, trace, number: int, **kw) -> str:
    code, verdicts, output = run_gate(tmp_path, events, trace, **kw)
    expected = {n: ("FAIL" if n == number else "PASS") for n in range(1, 7)}
    assert verdicts == expected, output
    assert code != 0
    return output


def find(events, kind: str, **match) -> list[dict[str, Any]]:
    return [e for e in events
            if e["type"] == kind and all(e.get(k) == v for k, v in match.items())]


def first_omlx_request(events, *, after_kill: bool = False) -> str:
    kill_ts = find(events, "client_killed")[0]["ts"]
    for e in find(events, "request_end", outcome="completed"):
        rid = e["request_id"]
        start = find(events, "child_start", request_id=rid)[0]
        if start["lane"] != "omlx" or rid == RETRIED or "root-1/l2-00" in start["workflow_id"]:
            continue
        if after_kill == (start["ts"] > kill_ts):
            return rid
    raise AssertionError("no suitable request")


# ------------------------------------------------------------------ good log


def test_good_log_passes_all_six(tmp_path):
    events, trace = build()
    code, verdicts, output = run_gate(
        tmp_path, events, trace,
        # The trace may hold records and junk the gate does not need.
        extra_trace_lines=["not json", json.dumps({"kind": "turn", "run_id": "x"})])
    assert verdicts == dict.fromkeys(range(1, 7), "PASS"), output
    assert code == 0


def test_good_log_sanity_numbers():
    events, _ = build()
    assert len(find(events, "request_end")) == 200
    assert len(find(events, "budget_reject")) == 8
    assert sum(1 for e in find(events, "child_end", outcome="completed")
               if e["lane"] == "omlx") >= 100
    assert any(e["gated_by_owner"] for p in find(events, "plan") for e in p["requests"])


# ------------------------------------------------------- 1. requests and duplicates


def test_c1_missing_terminal_outcome(tmp_path):
    events, trace = build()
    rid = first_omlx_request(events)
    events.remove(find(events, "request_end", request_id=rid)[0])
    assert_only_fails(tmp_path, events, trace, 1)


def test_c1_second_completed_dispatch(tmp_path):
    events, trace = build()
    rid = first_omlx_request(events, after_kill=True)
    end = find(events, "child_end", request_id=rid)[0]
    start = find(events, "child_start", request_id=rid)[0]
    last = events[-1]["ts"]
    dup_start = {**start, "attempt": 2, "ts": last + 1}
    dup_end = {**end, "attempt": 2, "ts": last + 2}
    events[-1:-1] = [dup_start, dup_end]  # before shutdown, after everything else
    assert_only_fails(tmp_path, events, trace, 1)


def test_c1_duplicate_run_id_across_requests(tmp_path):
    events, trace = build()
    a = first_omlx_request(events)
    b = first_omlx_request(events, after_kill=True)
    run_a = find(events, "request_end", request_id=a)[0]["run_id"]
    for kind in ("child_end", "request_end", "budget_settle"):
        for e in find(events, kind, request_id=b):
            e["run_id"] = run_a
    assert_only_fails(tmp_path, events, trace, 1)


def test_c1_planned_count_not_200(tmp_path):
    events, trace = build()
    find(events, "config")[0]["planned_children"] = 199
    assert_only_fails(tmp_path, events, trace, 1)


# --------------------------------------------------------------- 2. admission


def test_c2_overlap_exceeds_lane_cap(tmp_path):
    events, trace = build()
    rid = first_omlx_request(events, after_kill=True)
    start = find(events, "child_start", request_id=rid)[0]
    start["ts"] -= 6.0  # overlaps the previous omlx child; cap is 1
    assert_only_fails(tmp_path, events, trace, 2)


def test_c2_start_without_end(tmp_path):
    events, trace = build()
    failed = find(events, "child_end", request_id=RETRIED, attempt=1)[0]
    events.remove(failed)
    assert_only_fails(tmp_path, events, trace, 2)


def test_c2_lane_without_cap(tmp_path):
    events, trace = build()
    del find(events, "config")[0]["lane_caps"]["deepseek"]
    assert_only_fails(tmp_path, events, trace, 2)


# ------------------------------------------------------------------ 3. budget


def test_c3_overrun_of_cap(tmp_path):
    events, trace = build()
    find(events, "config")[0]["budget_cap_usd"] = 0.40
    assert_only_fails(tmp_path, events, trace, 3)


def test_c3_open_reservation(tmp_path):
    events, trace = build()
    events.insert(-1, {"ts": events[-1]["ts"] - 0.1, "type": "budget_reserve",
                       "reservation_id": "res-leaked", "request_id": "root-2-r000",
                       "amount_usd": DEEPSEEK_COST})
    assert_only_fails(tmp_path, events, trace, 3)


def test_c3_cap_never_exercised(tmp_path):
    events, trace = build()
    # A run with no reject: rejected requests became failures with no dispatch instead.
    for e in find(events, "request_end", outcome="budget_exhausted"):
        e["outcome"] = "failed"
    for e in find(events, "budget_reject"):
        events.remove(e)
    assert_only_fails(tmp_path, events, trace, 3)


def test_c3_ledger_does_not_match_trace(tmp_path):
    events, trace = build()
    paid = next(r for r in trace if r["kind"] == "run" and r["cost"]["provider_usd"] > 0)
    paid["cost"]["provider_usd"] += 0.01
    assert_only_fails(tmp_path, events, trace, 3)


def test_c3_run_missing_from_trace(tmp_path):
    events, trace = build()
    rid = first_omlx_request(events)
    run_id = find(events, "request_end", request_id=rid)[0]["run_id"]
    trace = [r for r in trace if r.get("run_id") != run_id]
    assert_only_fails(tmp_path, events, trace, 3)


def test_c3_unpriced_run_in_trace(tmp_path):
    events, trace = build()
    paid = next(r for r in trace if r["kind"] == "run" and r["cost"]["provider_usd"] > 0)
    paid["cost"] = None
    assert_only_fails(tmp_path, events, trace, 3)


def test_c3_missing_trace_file(tmp_path):
    events, _ = build()
    assert_only_fails(tmp_path, events, None, 3)


# -------------------------------------------------------------- 4. owner pause


def test_c4_signal_before_server_ready(tmp_path):
    events, trace = build()
    ready = find(events, "server_ready", restart=True)[0]
    find(events, "owner_signal")[0]["ts"] = ready["ts"] - 0.1
    assert_only_fails(tmp_path, events, trace, 4)


def test_c4_too_few_completed_before_kill(tmp_path):
    events, trace = build(kill_after=10)
    assert_only_fails(tmp_path, events, trace, 4)


def test_c4_gated_request_started_before_resume(tmp_path):
    events, trace = build()
    resumed = find(events, "owner_resumed")[0]["ts"]
    early = next(e for e in find(events, "child_start")
                 if e["workflow_id"].startswith(PAUSED_WF) and e["ts"] < resumed)
    for plan in find(events, "plan"):
        for entry in plan["requests"]:
            if entry["request_id"] == early["request_id"]:
                entry["gated_by_owner"] = True
    assert_only_fails(tmp_path, events, trace, 4)


def test_c4_root_done_before_resume(tmp_path):
    events, trace = build()
    resumed = find(events, "owner_resumed")[0]["ts"]
    find(events, "root_done", root_id="root-1")[0]["ts"] = resumed - 0.2
    assert_only_fails(tmp_path, events, trace, 4)


# -------------------------------------------------------------------- 5. shape


def test_c5_one_client_pid(tmp_path):
    events, trace = build()
    for e in find(events, "client_submit"):
        e["client_pid"] = 1001
    assert_only_fails(tmp_path, events, trace, 5)


def test_c5_unverified_completed_child(tmp_path):
    events, trace = build()
    rid = first_omlx_request(events)
    find(events, "child_end", request_id=rid)[0]["verified"] = False
    assert_only_fails(tmp_path, events, trace, 5)


def test_c5_root_without_depth_three(tmp_path):
    events, trace = build()
    for e in find(events, "child_end", root_id="root-2", depth=3):
        e["depth"] = 2
    assert_only_fails(tmp_path, events, trace, 5)


def test_c5_deepseek_lane_unused(tmp_path):
    events, trace = build()
    # Relabel the completed deepseek children as omlx everywhere the shape check looks.
    for e in find(events, "child_end", lane="deepseek"):
        e["lane"] = "omlx"
    for e in find(events, "child_start", lane="deepseek"):
        e["lane"] = "omlx"
    for plan in find(events, "plan"):
        for entry in plan["requests"]:
            if entry["lane"] == "deepseek":
                entry["lane"] = "omlx"
    # The relabelled children were sequential, so omlx stays within cap 1.
    assert_only_fails(tmp_path, events, trace, 5)


# ------------------------------------------------------------------- 6. health


def test_c6_runtime_error(tmp_path):
    events, trace = build()
    events.insert(-1, {"ts": events[-1]["ts"] - 0.1, "type": "runtime_error",
                       "source": "temporal.log", "line": 7,
                       "detail": f"database is locked token={SECRET}"})
    output = assert_only_fails(tmp_path, events, trace, 6)
    assert SECRET not in output


def test_c6_malformed_line(tmp_path):
    events, trace = build()
    assert_only_fails(tmp_path, events, trace, 6, extra_log_lines=["{not json"])


def test_c6_orphan_pid(tmp_path):
    events, trace = build()
    find(events, "shutdown")[0].update(clean=True, orphan_pids=[4242])
    assert_only_fails(tmp_path, events, trace, 6)


def test_c6_slow_cold_start(tmp_path):
    events, trace = build()
    find(events, "server_ready", restart=False)[0]["cold_start_seconds"] = 6.0
    assert_only_fails(tmp_path, events, trace, 6)


def test_c6_root_without_done(tmp_path):
    events, trace = build()
    events.remove(find(events, "root_done", root_id="root-2")[0])
    assert_only_fails(tmp_path, events, trace, 6)


# ------------------------------------------------------------------ robustness


@pytest.mark.parametrize("mutate", ["empty", "config_only"])
def test_degenerate_logs_fail_without_crash(tmp_path, mutate):
    events, trace = build()
    kept = [] if mutate == "empty" else [copy.deepcopy(find(events, "config")[0])]
    code, verdicts, output = run_gate(tmp_path, kept, trace)
    assert code != 0
    assert "Traceback" not in output
    assert all(v == "FAIL" for v in verdicts.values()), output


def test_never_prints_secrets(tmp_path, monkeypatch):
    events, trace = build()
    for e in find(events, "child_end"):
        e["env"] = {"DEEPSEEK_API_KEY": SECRET}
    for r in trace:
        r["api_key"] = SECRET
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    code, verdicts, output = run_gate(tmp_path, events, trace)
    assert SECRET not in output
    assert code == 0, output
