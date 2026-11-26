"""Three real processes share one Store file and must never exceed the lane cap."""

from __future__ import annotations

import json
import subprocess
import sys
import time

CAP = 2
PROCS = 3
GRANTS_EACH = 12
HOLD_S = 0.02

WORKER = r"""
import json, sys, time
from subagent.graph.admission import Admission, Workers
from subagent.graph.journal import Journal
from subagent.graph.ledger import Ledger
from subagent.graph.store import Store

path, name, root_path, out, cap, n, hold = sys.argv[1:8]
cap, n, hold = int(cap), int(n), float(hold)
store = Store(path)
ledger, journal = Ledger(store), Journal(store)
adm = Admission(store, ledger, journal, caps={"a": cap}, workers=Workers(store))
holds = []
deadline = time.time() + 90
for i in range(n):
    rid = f"{name}/t{i}"
    adm.enqueue(request_id=rid, attempt=1, lane="a", priority=3, fairness_key=name,
                run_path=root_path, node_id=f"t{i}", reserve_usd=0.01, deps=[])
    while True:
        res = adm.try_acquire(rid, 1)
        if res.status == "granted":
            break
        assert res.status == "queued", res
        assert time.time() < deadline, "timed out waiting for a lease"
        time.sleep(0.002)
    start = time.time()
    time.sleep(hold)
    end = time.time()
    adm.release(res.lease.lease_id)
    holds.append([start, end])
json.dump(holds, open(out, "w"))
"""


def _max_overlap(intervals: list[list[float]]) -> int:
    events = []
    for start, end in intervals:
        events.append((start, 1))
        events.append((end, -1))
    # An end sorts before a start at the same instant.
    events.sort(key=lambda e: (e[0], e[1]))
    live = best = 0
    for _, delta in events:
        live += delta
        best = max(best, live)
    return best


def test_three_processes_never_hold_more_than_the_cap(tmp_path):
    from subagent.graph.ledger import Ledger
    from subagent.graph.store import Store

    path = tmp_path / "shared.sqlite3"
    store = Store(path)
    ledger = Ledger(store)
    ledger.ensure_pool("default", 100.0)
    root = ledger.open_root(root_run_id="gr-xp", pool="default", requested_usd=50.0,
                            graph_id="g", graph_hash="h").run_path

    procs, outs = [], []
    try:
        for i in range(PROCS):
            out = tmp_path / f"holds{i}.json"
            outs.append(out)
            procs.append(subprocess.Popen(
                [sys.executable, "-I", "-c", WORKER, str(path), f"p{i}", root, str(out),
                 str(CAP), str(GRANTS_EACH), str(HOLD_S)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            ))
        deadline = time.time() + 120
        for proc in procs:
            remaining = max(1.0, deadline - time.time())
            try:
                output, _ = proc.communicate(timeout=remaining)
            except subprocess.TimeoutExpired:
                proc.kill()
                output, _ = proc.communicate()
                raise AssertionError(f"worker process timed out:\n{output}") from None
            assert proc.returncode == 0, output
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    holds = []
    for out in outs:
        holds.extend(json.loads(out.read_text()))
    assert len(holds) == PROCS * GRANTS_EACH
    peak = _max_overlap(holds)
    assert peak <= CAP, f"{peak} holders at once on a cap-{CAP} lane"
    assert peak == CAP, "the processes never contended; the check proved nothing"
