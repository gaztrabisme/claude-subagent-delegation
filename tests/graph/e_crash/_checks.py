"""Invariant checks shared by the crash scenarios (read the audit log, journal and ledger)."""

from __future__ import annotations

from collections import Counter
from typing import Any

from ._cluster import CAPS


def starts_and_ends(events: list[dict[str, Any]]):
    starts = Counter(e["attempt_id"] for e in events if e["event"] == "start")
    ends = Counter(e["attempt_id"] for e in events if e["event"] == "end")
    return starts, ends


def in_flight_by_pid(events: list[dict[str, Any]]) -> dict[int, list[str]]:
    """{worker pid: attempt ids with a start line and no end line}."""
    _, ends = starts_and_ends(events)
    out: dict[int, list[str]] = {}
    for e in events:
        if e["event"] == "start" and e["attempt_id"] not in ends:
            out.setdefault(e["pid"], []).append(e["attempt_id"])
    return out


def attempt_id(entry) -> str:
    return f"{entry.request_id}#a{entry.attempt}"


def assert_no_duplicate_starts(events):
    starts, _ = starts_and_ends(events)
    dupes = {a: n for a, n in starts.items() if n > 1}
    assert not dupes, f"attempt ids started more than once: {dupes}"


def max_concurrency(events, lane_of: dict[str, str], killed_end: dict[str, float]):
    """Per-lane peak of simultaneously running attempts, from the audit log alone."""
    marks: dict[str, list[tuple[float, int]]] = {}
    ended = {e["attempt_id"]: e["ts"] for e in events if e["event"] == "end"}
    for e in events:
        if e["event"] != "start":
            continue
        lane = lane_of.get(e["attempt_id"])
        if lane is None:
            continue
        finish = ended.get(e["attempt_id"], killed_end.get(e["attempt_id"]))
        assert finish is not None, f"no end for {e['attempt_id']}"
        marks.setdefault(lane, []).extend([(e["ts"], 1), (finish, -1)])
    peaks = {}
    for lane, items in marks.items():
        running = peak = 0
        for _, delta in sorted(items, key=lambda m: (round(m[0], 3), m[1])):
            running += delta
            peak = max(peak, running)
        peaks[lane] = peak
    return peaks


def assert_books_balance(cluster, root: str):
    from subagent.graph.journal import Journal
    from subagent.graph.ledger import Ledger

    store = cluster.store()
    ledger = Ledger(store)
    journal = Journal(store)
    tree = ledger.tree(root)
    assert tree
    for account in tree:
        assert account.reserved_usd == 0.0, f"open reservation on {account.run_path}"
    root_account = tree[0]
    assert root_account.run_path == root
    assert root_account.spent_usd <= root_account.cap_usd + 1e-6
    charged = sum((e.charged_usd or 0.0) for e in journal.for_root(root))
    assert abs(root_account.spent_usd - charged) < 1e-5, (root_account.spent_usd, charged)
    return store, ledger, journal


def assert_reconcile_leaves_no_dispatched(store, ledger, journal, root: str):
    from subagent.graph.admission import Admission

    admission = Admission(store, ledger, journal, caps=dict(CAPS))
    admission.reconcile(root_live=lambda _root: None)
    stuck = [attempt_id(e) for e in journal.for_root(root) if e.phase == "dispatched"]
    assert stuck == []
    for lane in CAPS:
        assert admission.active(lane) == 0
