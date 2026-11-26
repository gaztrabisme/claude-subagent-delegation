"""Admission: per-lane leases, the fair queue, the workers table and reconciliation (spec 5).

Everything is one SQLite file with `BEGIN IMMEDIATE`, so a lease grant and its budget
reservation are one transaction and safe across processes.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..config import log
from . import roots as graph_roots
from .journal import Journal
from .ledger import BudgetRefused, Ledger
from .store import Store, micro, usd


class LeaseLost(RuntimeError):
    pass


@dataclass
class Lease:
    lease_id: str
    lane: str
    request_id: str
    attempt: int
    holder: str | None
    acquired_at: float
    expires_at: float


@dataclass
class AdmitResult:
    status: str  # granted | queued | refused
    lease: Lease | None = None
    position: int | None = None
    reason: str | None = None


@dataclass
class ReconcileReport:
    interrupted: list[str] = field(default_factory=list)
    recovered: list[str] = field(default_factory=list)
    dead: list[str] = field(default_factory=list)
    released_leases: int = 0


def _lease(row: sqlite3.Row) -> Lease:
    return Lease(row["lease_id"], row["lane"], row["request_id"], row["attempt"], row["holder"],
                 row["acquired_at"], row["expires_at"])


START_GRACE_S = 60.0  # a root opened this recently may not have reached start_workflow yet


class Workers:
    def __init__(self, store: Store, *, clock: Callable[[], float] = time.time):
        self.store = store
        self.clock = clock

    def register(self, pid: int) -> str:
        worker_id = f"w-{pid}-{secrets.token_hex(3)}"
        now = self.clock()
        with self.store.txn() as conn:
            conn.execute("INSERT INTO workers VALUES (?,?,?,?)", (worker_id, pid, now, now))
        return worker_id

    def beat(self, worker_id: str) -> None:
        with self.store.txn() as conn:
            conn.execute("UPDATE workers SET beat_at=? WHERE worker_id=?",
                         (self.clock(), worker_id))

    def alive(self, worker_id: str | None, *, stale_s: float = 20.0) -> bool:
        if not worker_id:
            return False
        row = self.store.read().execute(
            "SELECT pid, beat_at FROM workers WHERE worker_id=?", (worker_id,)).fetchone()
        if row is None or self.clock() - row["beat_at"] > stale_s:
            return False
        try:
            os.kill(row["pid"], 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def list(self) -> list[dict[str, Any]]:
        rows = self.store.read().execute("SELECT * FROM workers ORDER BY started_at, rowid")
        return [dict(r) for r in rows]


class Admission:
    def __init__(self, store: Store, ledger: Ledger, journal: Journal, *,
                 caps: Mapping[str, int], lane_open: Callable[[str], bool] = lambda lane: True,
                 lease_ttl_s: float = 60.0, grant_ttl_s: float = 120.0, aging_s: float = 600.0,
                 lane_closed_timeout_s: float = 3600.0, workers: Workers | None = None,
                 clock: Callable[[], float] = time.time):
        self.store, self.ledger, self.journal = store, ledger, journal
        self.caps = dict(caps)
        self.lane_open = lane_open
        self.lease_ttl_s, self.grant_ttl_s = lease_ttl_s, grant_ttl_s
        self.aging_s, self.lane_closed_timeout_s = aging_s, lane_closed_timeout_s
        self.clock = clock
        self.workers = workers or Workers(store, clock=clock)

    # -- queue ------------------------------------------------------------

    def enqueue(self, *, request_id: str, attempt: int, lane: str, priority: int,
                fairness_key: str, run_path: str, node_id: str, reserve_usd: float,
                deps: Sequence[str] = ()) -> None:
        """Idempotent per (request_id, attempt): a retried admit never queues twice."""
        with self.store.txn() as conn:
            if self.journal.get_in(conn, request_id, attempt) is not None:
                return
            acct = conn.execute("SELECT root_run_id FROM accounts WHERE run_path=?",
                                (run_path,)).fetchone()
            root = (acct["root_run_id"] if acct and acct["root_run_id"]
                    else run_path.split("/")[0])
            now = self.clock()
            conn.execute(
                "INSERT INTO queue (request_id, attempt, lane, priority, fairness_key, run_path, "
                "node_id, reserve, deps, enqueued_at, root_run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (request_id, attempt, lane, priority, fairness_key, run_path, node_id,
                 micro(reserve_usd), json.dumps(list(deps)), now, root))
            self.journal.advance(conn, request_id, attempt, "queued", root_run_id=root,
                                 run_path=run_path, node_id=node_id, lane=lane,
                                 reserved_usd=reserve_usd)

    def _settled_ok(self, conn: sqlite3.Connection, dep: str) -> bool:
        for row in conn.execute("SELECT outcome FROM journal WHERE request_id=? AND "
                                "phase='settled'", (dep,)):
            outcome = json.loads(row["outcome"]) if row["outcome"] else {}
            if isinstance(outcome, dict) and outcome.get("status") == "completed":
                return True
        return False

    def _ordered(self, conn: sqlite3.Connection, lane: str, now: float) -> list[sqlite3.Row]:
        """The lane's eligible entries (deps settled), head of line first."""
        rows = [r for r in conn.execute("SELECT * FROM queue WHERE lane=?", (lane,))
                if all(self._settled_ok(conn, d) for d in json.loads(r["deps"]))]
        active = {r["fairness_key"]: r["n"] for r in conn.execute(
            "SELECT fairness_key, COUNT(*) AS n FROM leases WHERE lane=? AND state='active' "
            "GROUP BY fairness_key", (lane,))}
        last = {r["fairness_key"]: r["last_seq"] for r in conn.execute(
            "SELECT fairness_key, last_seq FROM fairness WHERE lane=?", (lane,))}

        def key(r: sqlite3.Row) -> tuple[int, int, int, int]:
            waited = max(0.0, now - r["enqueued_at"])
            eff = max(1, r["priority"] - int(waited // self.aging_s))
            fk = r["fairness_key"]
            return (eff, active.get(fk, 0), last.get(fk, 0), r["seq"])

        return sorted(rows, key=key)

    def queue(self, lane: str | None = None) -> list[dict[str, Any]]:
        conn = self.store.read()
        now = self.clock()
        lanes = ([lane] if lane else
                 [r[0] for r in conn.execute("SELECT DISTINCT lane FROM queue ORDER BY lane")])
        out: list[dict[str, Any]] = []
        for name in lanes:
            for pos, r in enumerate(self._ordered(conn, name, now), start=1):
                out.append({"request_id": r["request_id"], "attempt": r["attempt"],
                            "lane": name, "priority": r["priority"],
                            "fairness_key": r["fairness_key"], "run_path": r["run_path"],
                            "node_id": r["node_id"], "reserve_usd": usd(r["reserve"]),
                            "deps": json.loads(r["deps"]), "enqueued_at": r["enqueued_at"],
                            "position": pos})
        return out

    # -- leases -----------------------------------------------------------

    def _drop_entry(self, conn: sqlite3.Connection, request_id: str, attempt: int) -> None:
        conn.execute("DELETE FROM queue WHERE request_id=? AND attempt=?", (request_id, attempt))
        row = self.journal.get_in(conn, request_id, attempt)
        if row is not None and row.phase == "queued":
            self.journal.advance(conn, request_id, attempt, "released")

    def _expire(self, conn: sqlite3.Connection, now: float) -> int:
        """Release leases past their expiry; `dispatched` rows are left to run_task/reconcile."""
        rows = conn.execute("SELECT * FROM leases WHERE state='active' AND expires_at < ?",
                            (now,)).fetchall()
        for row in rows:
            conn.execute("UPDATE leases SET state='expired' WHERE lease_id=?", (row["lease_id"],))
            entry = self.journal.get_in(conn, row["request_id"], row["attempt"])
            if entry is not None and entry.phase == "admitted":
                self.ledger.release(conn, request_id=row["request_id"], attempt=row["attempt"])
                self.journal.advance(conn, row["request_id"], row["attempt"], "released")
        return len(rows)

    def try_acquire(self, request_id: str, attempt: int) -> AdmitResult:
        with self.store.txn() as conn:
            now = self.clock()
            self._expire(conn, now)
            held = conn.execute("SELECT * FROM leases WHERE request_id=? AND attempt=? AND "
                                "state='active'", (request_id, attempt)).fetchone()
            if held is not None:  # a retried admit after the grant
                return AdmitResult("granted", lease=_lease(held))
            entry = conn.execute("SELECT * FROM queue WHERE request_id=? AND attempt=?",
                                 (request_id, attempt)).fetchone()
            if entry is None:
                return AdmitResult("refused", reason="cancelled")
            lane = entry["lane"]
            if lane not in self.caps:
                self._drop_entry(conn, request_id, attempt)
                return AdmitResult("refused", reason="lane_unknown")
            if not all(self._settled_ok(conn, d) for d in json.loads(entry["deps"])):
                return AdmitResult("queued", reason="deps")
            if not self.lane_open(lane):
                if now - entry["enqueued_at"] > self.lane_closed_timeout_s:
                    self._drop_entry(conn, request_id, attempt)
                    return AdmitResult("refused", reason="lane_closed_timeout")
                return AdmitResult("queued", reason="lane_closed")
            order = self._ordered(conn, lane, now)
            seqs = [row["seq"] for row in order]
            if entry["seq"] not in seqs:
                return AdmitResult("queued", reason="deps")
            pos = seqs.index(entry["seq"]) + 1
            if pos > 1:
                return AdmitResult("queued", position=pos, reason="behind")
            active = conn.execute("SELECT COUNT(*) FROM leases WHERE lane=? AND state='active'",
                                  (lane,)).fetchone()[0]
            if active >= self.caps[lane]:
                return AdmitResult("queued", position=1, reason="lane_full")
            try:
                self.ledger.reserve(conn, run_path=entry["run_path"], request_id=request_id,
                                    attempt=attempt, amount_usd=usd(entry["reserve"]))
            except BudgetRefused as exc:
                self._drop_entry(conn, request_id, attempt)
                return AdmitResult("refused", reason=exc.code)
            lease = Lease(f"ls-{secrets.token_hex(6)}", lane, request_id, attempt, None, now,
                          now + self.grant_ttl_s)
            conn.execute("INSERT INTO leases VALUES (?,?,?,?,?,NULL,?,?, 'active')",
                         (lease.lease_id, lane, request_id, attempt, entry["fairness_key"], now,
                          lease.expires_at))
            seq = int(conn.execute("SELECT COALESCE(MAX(last_seq), 0) FROM fairness"
                                   ).fetchone()[0]) + 1
            conn.execute("INSERT OR REPLACE INTO fairness VALUES (?,?,?)",
                         (lane, entry["fairness_key"], seq))
            self.journal.advance(conn, request_id, attempt, "admitted")
            conn.execute("DELETE FROM queue WHERE seq=?", (entry["seq"],))
            return AdmitResult("granted", lease=lease)

    def renew(self, lease_id: str, *, holder: str) -> Lease:
        with self.store.txn() as conn:
            now = self.clock()
            row = conn.execute("SELECT * FROM leases WHERE lease_id=?", (lease_id,)).fetchone()
            if row is None or row["state"] != "active" or row["expires_at"] < now:
                raise LeaseLost(f"lease {lease_id} is gone")
            conn.execute("UPDATE leases SET holder=?, expires_at=? WHERE lease_id=?",
                         (holder, now + self.lease_ttl_s, lease_id))
            return _lease(conn.execute("SELECT * FROM leases WHERE lease_id=?",
                                       (lease_id,)).fetchone())

    def release(self, lease_id: str) -> None:
        with self.store.txn() as conn:
            conn.execute("UPDATE leases SET state='released' WHERE lease_id=? AND "
                         "state='active'", (lease_id,))

    def cancel(self, request_id: str, attempt: int) -> None:
        with self.store.txn() as conn:
            self._drop_entry(conn, request_id, attempt)
            lease = conn.execute("SELECT lease_id FROM leases WHERE request_id=? AND attempt=? "
                                 "AND state='active'", (request_id, attempt)).fetchone()
            entry = self.journal.get_in(conn, request_id, attempt)
            if lease is not None and entry is not None and entry.phase == "admitted":
                conn.execute("UPDATE leases SET state='released' WHERE lease_id=?",
                             (lease["lease_id"],))
                self.ledger.release(conn, request_id=request_id, attempt=attempt)
                self.journal.advance(conn, request_id, attempt, "released")

    def active(self, lane: str) -> int:
        return self.store.read().execute(
            "SELECT COUNT(*) FROM leases WHERE lane=? AND state='active'", (lane,)).fetchone()[0]

    # -- reconcile --------------------------------------------------------

    def _interrupt(self, conn: sqlite3.Connection, request_id: str, attempt: int) -> None:
        done = self.ledger.settle(conn, request_id=request_id, attempt=attempt, actual_usd=None)
        self.journal.advance(conn, request_id, attempt, "interrupted",
                             charged_usd=done.charged_usd, cost_source=done.cost_source)

    def reconcile(self, *, root_live: Callable[[str], bool | None]) -> ReconcileReport:
        report = ReconcileReport()
        with self.store.txn() as conn:
            now = self.clock()
            report.released_leases += self._expire(conn, now)
            rows = conn.execute("SELECT request_id, attempt, worker_id FROM journal WHERE "
                                "phase='dispatched'").fetchall()
            for row in rows:
                rid, att = row["request_id"], row["attempt"]
                if self.workers.alive(row["worker_id"]):
                    continue
                live = conn.execute("SELECT 1 FROM leases WHERE request_id=? AND attempt=? AND "
                                    "state='active' AND expires_at >= ?",
                                    (rid, att, now)).fetchone()
                if live is not None:
                    continue
                self._interrupt(conn, rid, att)
                report.released_leases += conn.execute(
                    "UPDATE leases SET state='released' WHERE request_id=? AND attempt=? AND "
                    "state='active'", (rid, att)).rowcount
                report.interrupted.append(f"{rid}#a{att}")
        # Temporal is asked outside any write transaction.
        now = self.clock()
        roots = [r[0] for r in self.store.read().execute(
            "SELECT run_path, created_at FROM accounts WHERE depth=0 AND state='open' "
            "ORDER BY rowid") if not 0 <= now - r[1] < START_GRACE_S]  # fresh: maybe unstarted
        dead: list[str] = []
        for root in roots:
            verdict = root_live(root)
            if verdict is True:
                report.recovered.append(root)
            elif verdict is False:
                with self.store.txn() as conn:
                    report.released_leases += self._kill_root(conn, root)
                dead.append(root)
        report.dead = dead
        self._raise_run_dead(dead)
        return report

    def _kill_root(self, conn: sqlite3.Connection, root: str) -> int:
        """Mark a root and everything under it dead; charge unsettled reservations."""
        released = conn.execute(
            "UPDATE leases SET state='released' WHERE state='active' AND request_id IN "
            "(SELECT request_id FROM journal WHERE root_run_id=?)", (root,)).rowcount
        conn.execute("DELETE FROM queue WHERE root_run_id=?", (root,))
        for entry in self.journal.for_root_in(conn, root):
            if entry.phase in ("queued", "admitted"):
                self.journal.advance(conn, entry.request_id, entry.attempt, "released")
            elif entry.phase == "dispatched":
                self._interrupt(conn, entry.request_id, entry.attempt)
        paths = [r[0] for r in conn.execute(
            "SELECT run_path FROM accounts WHERE root_run_id=? ORDER BY depth DESC, rowid DESC",
            (root,))]
        for path in paths:
            self.ledger.close_in(conn, path, state="dead")
        graph_roots.mark_in(conn, root, "failed")
        return released

    def _raise_run_dead(self, roots: Sequence[str]) -> None:
        if not roots:
            return
        try:
            from .exceptions import ExceptionQueue
        except ImportError:
            log.warning("exceptions module missing; run_dead not raised for %s", list(roots))
            return
        queue = ExceptionQueue(self.store, clock=self.clock)
        for root in roots:
            queue.close_for_root(root, "run_dead")
            queue.raise_(key=f"{root}:run_dead", root_run_id=root, run_path=root, node_id=None,
                         kind="run_dead", summary=f"run {root} is dead: Temporal no longer "
                         "knows it", detail={}, needs_answer=False)
