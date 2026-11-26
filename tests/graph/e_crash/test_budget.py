"""Spec 7 / 12.2 e: the budget sized for 20 of 24 tasks; the rest are refused, never dispatched."""

from __future__ import annotations

from ._checks import (
    CAPS,
    assert_books_balance,
    assert_no_duplicate_starts,
    assert_reconcile_leaves_no_dispatched,
    attempt_id,
    max_concurrency,
    starts_and_ends,
)
from ._cluster import crash_graph, task_statuses


def test_tasks_beyond_the_budget_get_budget_exhausted_and_are_never_dispatched(make_cluster):
    cluster = make_cluster(default_step={"cost_usd": 0.5, "sleep_s": 0.5}, root_budget=10.0)
    cluster.bring_up(workers=2)
    started = cluster.call("start", crash_graph(root_usd=10.0, a_usd=6.0, b_usd=3.0),
                           request_id="budget-1", workspace=str(cluster.workspace))
    root = started["root_run_id"]
    assert started["budget"]["cap_usd"] == 10.0
    final = cluster.wait_done(root, timeout=150)
    assert final["state"] == "completed"
    statuses = task_statuses(final["result"]["output"])
    assert len(statuses) == 24
    values = list(statuses.values())
    assert values.count("completed") == 20
    assert values.count("budget_exhausted") == 4
    assert all(statuses[f"r{i}"] == "completed" for i in range(8))
    assert all(statuses[f"a{i}"] == "completed" for i in range(8))

    events = cluster.audit_events()
    assert_no_duplicate_starts(events)
    starts, ends = starts_and_ends(events)
    assert len(starts) == 20 and set(starts) == set(ends), "a refused task must not dispatch"

    store, ledger, journal = assert_books_balance(cluster, root)
    root_account = ledger.account(root)
    assert root_account.spent_usd == 10.0
    assert root_account.cap_usd == 10.0
    assert final["budget"]["spent_usd"] == 10.0
    assert final["budget"]["overrun_usd"] == 0.0

    entries = journal.for_root(root)
    lane_of = {attempt_id(e): e.lane for e in entries}
    peaks = max_concurrency(events, lane_of, {})
    for lane, cap in CAPS.items():
        assert peaks.get(lane, 0) <= cap, (lane, peaks)
    assert_reconcile_leaves_no_dispatched(store, ledger, journal, root)
