"""Spec 7, row 2: SIGKILL a worker that holds dispatched tasks, on the 24-task, 2-lane graph."""

from __future__ import annotations

import os
import signal
import time

from ._checks import (
    CAPS,
    assert_books_balance,
    assert_no_duplicate_starts,
    assert_reconcile_leaves_no_dispatched,
    attempt_id,
    in_flight_by_pid,
    max_concurrency,
    starts_and_ends,
)
from ._cluster import RESERVE, crash_graph, pid_alive, task_statuses, wait_for


def test_sigkill_one_worker_interrupts_charges_reservation_and_retries_with_new_attempt(
        make_cluster):
    cluster = make_cluster(default_step={"cost_usd": 0.2, "sleep_s": 1.2}, root_budget=40.0)
    info = cluster.bring_up(workers=2)
    server_pid = info.pid
    started = cluster.call("start", crash_graph(root_usd=40.0, a_usd=20.0, b_usd=10.0),
                           request_id="worker-kill-1", workspace=str(cluster.workspace))
    root = started["root_run_id"]

    worker_pids = {w.pid for w in cluster.workers}
    victim = wait_for(
        lambda: next((pid for pid, att in in_flight_by_pid(cluster.audit_events()).items()
                      if pid in worker_pids and att), None),
        60, "a worker with dispatched tasks")
    os.kill(victim, signal.SIGKILL)
    killed_at = time.time()
    wait_for(lambda: not pid_alive(victim), 10, "the victim to die")
    killed = set(in_flight_by_pid(cluster.audit_events()).get(victim, []))
    assert killed, "the victim held no dispatched attempt"
    cluster.start_worker()  # a restarted worker picks up the Temporal retries

    final = cluster.wait_done(root, timeout=240)
    assert final["state"] == "completed", final
    statuses = task_statuses(final["result"]["output"])
    assert len(statuses) == 24
    assert set(statuses.values()) == {"completed"}

    events = cluster.audit_events()
    assert_no_duplicate_starts(events)
    starts, ends = starts_and_ends(events)

    store, ledger, journal = assert_books_balance(cluster, root)
    entries = journal.for_root(root)
    by_attempt = {attempt_id(e): e for e in entries}

    for attempt in killed:
        entry = by_attempt[attempt]
        assert entry.phase == "interrupted", (attempt, entry.phase)
        assert entry.charged_usd == RESERVE, "an interrupted attempt is charged at reservation"
        assert entry.cost_source == "reservation"
        request_id, _, n = attempt.rpartition("#a")
        retry = f"{request_id}#a{int(n) + 1}"
        assert retry in starts, f"{attempt} was not replaced by a new attempt id"
        assert by_attempt[retry].phase == "settled"
        assert attempt not in ends

    # every request that completed has exactly one end line across all its attempts
    for entry in entries:
        if entry.phase == "settled" and (entry.outcome or {}).get("status") == "completed":
            ends_for_request = sum(n for a, n in ends.items()
                                   if a.startswith(entry.request_id + "#a"))
            assert ends_for_request == 1, (entry.request_id, ends_for_request)

    lane_of = {attempt_id(e): e.lane for e in entries}
    peaks = max_concurrency(events, lane_of, dict.fromkeys(killed, killed_at))
    for lane, cap in CAPS.items():
        assert peaks.get(lane, 0) <= cap, (lane, peaks)

    assert_reconcile_leaves_no_dispatched(store, ledger, journal, root)

    assert cluster.server.stop(timeout_s=10) is True
    assert not pid_alive(server_pid)
