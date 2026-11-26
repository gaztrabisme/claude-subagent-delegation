"""Fixtures for the admission tests: a store, ledger, journal, workers and
admission wired on one SQLite file with an injected clock.

Everything graph-related is imported inside the fixtures so that the folder
collects (and its tests fail, not error at collection) while the package is
missing.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


class FakeClock:
    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def dead_pid() -> int:
    """The pid of a process that has exited and been reaped."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    return proc.pid


class Stack:
    """Store + ledger + journal + workers + admission on one file."""

    def __init__(self, tmp_path: Path, caps: dict[str, int], **kwargs):
        from subagent.graph.admission import Admission, Workers
        from subagent.graph.journal import Journal
        from subagent.graph.ledger import Ledger
        from subagent.graph.store import Store

        self.clock = FakeClock()
        self.closed_lanes: set[str] = set()
        self.store = Store(tmp_path / "g.sqlite3")
        self.ledger = Ledger(self.store)
        self.journal = Journal(self.store)
        self.workers = Workers(self.store, clock=self.clock)
        self.admission = Admission(
            self.store, self.ledger, self.journal, caps=caps,
            lane_open=lambda lane: lane not in self.closed_lanes,
            workers=self.workers, clock=self.clock, **kwargs,
        )
        self.ledger.ensure_pool("default", 1000.0)

    # -- builders -------------------------------------------------------

    def root(self, name: str, usd: float | None = 50.0) -> str:
        account = self.ledger.open_root(
            root_run_id=name, pool="default", requested_usd=usd,
            graph_id="g", graph_hash="h",
        )
        return account.run_path

    def enq(self, rid: str, *, root_path: str, lane: str = "a", priority: int = 3,
            key: str | None = None, reserve: float = 0.5, deps=(), attempt: int = 1) -> None:
        self.admission.enqueue(
            request_id=rid, attempt=attempt, lane=lane, priority=priority,
            fairness_key=key or root_path, run_path=root_path, node_id=rid.rsplit("/", 1)[-1],
            reserve_usd=reserve, deps=list(deps),
        )

    def acquire(self, rid: str, attempt: int = 1):
        return self.admission.try_acquire(rid, attempt)

    def dispatch(self, rid: str, worker_id: str, attempt: int = 1) -> None:
        with self.store.txn() as conn:
            self.journal.advance(conn, rid, attempt, "dispatched", worker_id=worker_id,
                                 agent_id="agent-1", run_id="run-1")

    def settle(self, rid: str, lease, *, status: str = "completed",
               actual: float | None = 0.05, attempt: int = 1) -> None:
        """Walk an admitted attempt to `settled` and release its lease."""
        with self.store.txn() as conn:
            self.journal.advance(conn, rid, attempt, "dispatched", worker_id="w-test",
                                 agent_id="agent-1", run_id="run-1")
            self.journal.advance(conn, rid, attempt, "finished", outcome={"status": status})
            done = self.ledger.settle(conn, request_id=rid, attempt=attempt, actual_usd=actual)
            self.journal.advance(conn, rid, attempt, "settled",
                                 charged_usd=done.charged_usd, cost_source=done.cost_source)
        self.admission.release(lease.lease_id)

    def drain(self, rids: list[str], attempt: int = 1) -> list[str]:
        """Grant and release `rids` one at a time; returns the grant order.

        Every pending entry is asked each round; exactly one may be granted
        (the head of line), the others must be refused a place for now.
        """
        order: list[str] = []
        pending = list(rids)
        while pending:
            granted = []
            for rid in pending:
                res = self.acquire(rid, attempt)
                if res.status == "granted":
                    granted.append((rid, res.lease))
            assert len(granted) == 1, f"expected one grant per round, got {granted}"
            rid, lease = granted[0]
            order.append(rid)
            pending.remove(rid)
            self.admission.release(lease.lease_id)
        return order


@pytest.fixture
def stack_factory(tmp_path):
    def make(caps=None, **kwargs) -> Stack:
        return Stack(tmp_path, caps if caps is not None else {"a": 1}, **kwargs)

    return make


@pytest.fixture
def dead_process_pid() -> int:
    return dead_pid()
