"""Run tree and budget accounts (spec section 4).

Every workflow has one account; a named pool sits above the roots. `remaining = cap - spent -
reserved - held`. Amounts are integer micro-dollars in SQLite and float USD (6 decimals) in the API.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass

from .store import Store, micro, usd


class BudgetRefused(Exception):
    """code: budget_exhausted | depth_limit | pool_unknown | account_closed"""

    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or code)
        self.code = code


@dataclass
class Account:
    run_path: str
    parent_path: str | None
    root_run_id: str | None
    depth: int
    cap_usd: float
    reserved_usd: float
    held_usd: float
    spent_usd: float
    state: str
    capped: bool
    node_id: str | None = None
    graph_id: str | None = None
    graph_hash: str | None = None
    created_at: float = 0.0
    closed_at: float | None = None

    @property
    def remaining_usd(self) -> float:
        return round(self.cap_usd - self.spent_usd - self.reserved_usd - self.held_usd, 6)


@dataclass
class Settlement:
    request_id: str
    attempt: int
    reserved_usd: float
    charged_usd: float
    cost_source: str
    overrun_usd: float


def _account(row: sqlite3.Row) -> Account:
    return Account(
        run_path=row["run_path"], parent_path=row["parent_path"], root_run_id=row["root_run_id"],
        depth=row["depth"], cap_usd=usd(row["cap"]), reserved_usd=usd(row["reserved"]),
        held_usd=usd(row["held"]), spent_usd=usd(row["spent"]), state=row["state"],
        capped=bool(row["capped"]), node_id=row["node_id"], graph_id=row["graph_id"],
        graph_hash=row["graph_hash"], created_at=row["created_at"], closed_at=row["closed_at"])


def _remaining(row: sqlite3.Row) -> int:
    return row["cap"] - row["spent"] - row["reserved"] - row["held"]


def _settlement(row: sqlite3.Row) -> Settlement:
    return Settlement(request_id=row["request_id"], attempt=row["attempt"],
                      reserved_usd=usd(row["amount"]), charged_usd=usd(row["charged"]),
                      cost_source=row["cost_source"] or "", overrun_usd=usd(row["overrun"]))


class Ledger:
    def __init__(self, store: Store, *, max_depth: int = 3,
                 clock: Callable[[], float] = time.time):
        self.store = store
        self.max_depth = max_depth
        self.clock = clock

    # -- accounts ---------------------------------------------------------

    @staticmethod
    def _row(conn: sqlite3.Connection, run_path: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM accounts WHERE run_path=?", (run_path,)).fetchone()

    def _need(self, conn: sqlite3.Connection, run_path: str) -> sqlite3.Row:
        row = self._row(conn, run_path)
        if row is None:
            raise KeyError(f"unknown account {run_path!r}")
        return row

    def ensure_pool(self, name: str, cap_usd: float) -> Account:
        path, cap = f"pool:{name}", micro(cap_usd)
        with self.store.txn() as conn:
            if self._row(conn, path) is None:
                conn.execute(
                    "INSERT INTO accounts (run_path, depth, node_id, cap, state, created_at) "
                    "VALUES (?, -1, ?, ?, 'open', ?)", (path, name, cap, self.clock()))
            else:
                conn.execute("UPDATE accounts SET cap=? WHERE run_path=?", (cap, path))
            return _account(self._need(conn, path))

    def _insert(self, conn, *, run_path, parent, root_run_id, depth, node_id, graph_id,
                graph_hash, requested, parent_row) -> Account:
        remaining = _remaining(parent_row)
        if parent_row["state"] != "open":
            raise BudgetRefused("account_closed")
        if remaining <= 0:
            raise BudgetRefused("budget_exhausted")
        want = None if requested is None else micro(requested)
        cap = min(remaining if want is None else want, remaining)
        capped = want is not None and want > remaining
        conn.execute(
            "INSERT INTO accounts (run_path, parent_path, root_run_id, depth, node_id, graph_id, "
            "graph_hash, cap, state, capped, created_at) VALUES (?,?,?,?,?,?,?,?, 'open', ?, ?)",
            (run_path, parent, root_run_id, depth, node_id, graph_id, graph_hash, cap,
             int(capped), self.clock()))
        conn.execute("UPDATE accounts SET held = held + ? WHERE run_path=?", (cap, parent))
        return _account(self._need(conn, run_path))

    def open_root(self, *, root_run_id: str, pool: str, requested_usd: float | None,
                  graph_id: str, graph_hash: str) -> Account:
        with self.store.txn() as conn:
            existing = self._row(conn, root_run_id)
            if existing is not None:
                return _account(existing)
            pool_row = self._row(conn, f"pool:{pool}")
            if pool_row is None:
                raise BudgetRefused("pool_unknown", f"unknown pool {pool!r}")
            return self._insert(conn, run_path=root_run_id, parent=pool_row["run_path"],
                                root_run_id=root_run_id, depth=0, node_id=None,
                                graph_id=graph_id, graph_hash=graph_hash,
                                requested=requested_usd, parent_row=pool_row)

    def open_child(self, *, parent_path: str, run_path: str, node_id: str,
                   requested_usd: float | None) -> Account:
        with self.store.txn() as conn:
            existing = self._row(conn, run_path)
            if existing is not None:
                return _account(existing)
            parent = self._need(conn, parent_path)
            depth = parent["depth"] + 1
            if depth > self.max_depth:
                raise BudgetRefused("depth_limit", f"depth {depth} exceeds {self.max_depth}")
            return self._insert(conn, run_path=run_path, parent=parent_path,
                                root_run_id=parent["root_run_id"], depth=depth, node_id=node_id,
                                graph_id=parent["graph_id"], graph_hash=parent["graph_hash"],
                                requested=requested_usd, parent_row=parent)

    def account(self, run_path: str) -> Account:
        return _account(self._need(self.store.read(), run_path))

    def tree(self, root_run_id: str) -> list[Account]:
        rows = self.store.read().execute(
            "SELECT * FROM accounts WHERE root_run_id=? ORDER BY depth, rowid", (root_run_id,))
        return [_account(r) for r in rows]

    def subtree_spent(self, run_path: str) -> float:
        total = self.store.read().execute(
            "SELECT COALESCE(SUM(charged), 0) FROM reservations WHERE state='settled' AND "
            "(run_path=? OR substr(run_path, 1, ?)=?)",
            (run_path, len(run_path) + 1, run_path + "/")).fetchone()[0]
        return usd(total)

    # -- reservations -----------------------------------------------------

    def reserve(self, conn: sqlite3.Connection, *, run_path: str, request_id: str, attempt: int,
                amount_usd: float) -> None:
        if conn.execute("SELECT 1 FROM reservations WHERE request_id=? AND attempt=?",
                        (request_id, attempt)).fetchone():
            return
        row = self._need(conn, run_path)
        amount = micro(amount_usd)
        if row["state"] != "open":
            raise BudgetRefused("account_closed")
        if amount > _remaining(row):
            raise BudgetRefused("budget_exhausted")
        conn.execute("UPDATE accounts SET reserved = reserved + ? WHERE run_path=?",
                     (amount, run_path))
        conn.execute("INSERT INTO reservations (request_id, attempt, run_path, amount, state) "
                     "VALUES (?,?,?,?, 'reserved')", (request_id, attempt, run_path, amount))

    def settle(self, conn: sqlite3.Connection, *, request_id: str, attempt: int,
               actual_usd: float | None) -> Settlement:
        res = conn.execute("SELECT * FROM reservations WHERE request_id=? AND attempt=?",
                           (request_id, attempt)).fetchone()
        if res is None:
            raise KeyError(f"no reservation for {request_id!r} attempt {attempt}")
        if res["state"] != "reserved":
            return _settlement(res)
        return self._settle_row(conn, res, actual_usd)

    def _settle_row(self, conn, res: sqlite3.Row, actual_usd: float | None) -> Settlement:
        charged = res["amount"] if actual_usd is None else micro(actual_usd)
        source = "reservation" if actual_usd is None else "actual"
        acct = self._need(conn, res["run_path"])
        spent = acct["spent"] + charged
        overrun = max(0, spent - acct["cap"])
        conn.execute("UPDATE accounts SET reserved = reserved - ?, spent = ? WHERE run_path=?",
                     (res["amount"], spent, res["run_path"]))
        conn.execute("UPDATE reservations SET state='settled', charged=?, cost_source=?, "
                     "overrun=? WHERE request_id=? AND attempt=?",
                     (charged, source, overrun, res["request_id"], res["attempt"]))
        return Settlement(res["request_id"], res["attempt"], usd(res["amount"]), usd(charged),
                          source, usd(overrun))

    def release(self, conn: sqlite3.Connection, *, request_id: str, attempt: int) -> None:
        res = conn.execute("SELECT * FROM reservations WHERE request_id=? AND attempt=?",
                           (request_id, attempt)).fetchone()
        if res is None or res["state"] != "reserved":
            return
        conn.execute("UPDATE accounts SET reserved = reserved - ? WHERE run_path=?",
                     (res["amount"], res["run_path"]))
        conn.execute("UPDATE reservations SET state='released' WHERE request_id=? AND attempt=?",
                     (request_id, attempt))

    # -- close ------------------------------------------------------------

    def close(self, run_path: str, *, state: str = "closed") -> Account:
        with self.store.txn() as conn:
            self.close_in(conn, run_path, state=state)
            return _account(self._need(conn, run_path))

    def close_in(self, conn: sqlite3.Connection, run_path: str, *, state: str = "closed") -> None:
        """`close` inside the caller's transaction (idempotent)."""
        acct = self._need(conn, run_path)
        if acct["state"] != "open":
            return
        for res in conn.execute("SELECT * FROM reservations WHERE run_path=? AND state='reserved'",
                                (run_path,)).fetchall():
            self._settle_row(conn, res, None)
        acct = self._need(conn, run_path)
        conn.execute("UPDATE accounts SET state=?, closed_at=? WHERE run_path=?",
                     (state, self.clock(), run_path))
        if acct["parent_path"]:
            conn.execute("UPDATE accounts SET held = held - ?, spent = spent + ? "
                         "WHERE run_path=?", (acct["cap"], acct["spent"], acct["parent_path"]))
