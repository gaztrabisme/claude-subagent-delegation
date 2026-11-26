"""Exceptions only (spec 6): the queue of things that need the orchestrator, and the routing policy.

Routine guard escalations are settled by the supervisor and never reach the queue. The table is
created here (`CREATE TABLE IF NOT EXISTS`) rather than by a store migration, because the store's
schema version is pinned at 1 by the locked store tests.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from . import ids
from .dispatch import TaskOutcome
from .interpreter import Scope
from .schema import TaskNode
from .store import Store

KINDS = ("owner_decision", "node_failed", "budget_exhausted", "lane_closed",
         "test_change_request", "guard_denied", "run_dead")

_STATES = ("open", "answered", "delivered", "closed")

_DDL = (
    """CREATE TABLE IF NOT EXISTS exceptions (
        exception_id TEXT PRIMARY KEY, key TEXT NOT NULL UNIQUE, root_run_id TEXT NOT NULL,
        run_path TEXT NOT NULL, node_id TEXT, kind TEXT NOT NULL, summary TEXT NOT NULL,
        detail TEXT NOT NULL, options TEXT NOT NULL, "default" TEXT, needs_answer INTEGER NOT NULL,
        state TEXT NOT NULL, created_at REAL NOT NULL, answer TEXT, note TEXT, answered_by TEXT,
        answered_at REAL, delivered_at REAL, closed_reason TEXT)""",
    "CREATE INDEX IF NOT EXISTS exceptions_state ON exceptions (state, created_at)",
    "CREATE INDEX IF NOT EXISTS exceptions_root ON exceptions (root_run_id)",
)


class NoClientSession:
    """Bound to the supervisor in graph workers: there is no MCP client to ask.

    It has no `check_client_capability`, so the supervisor probes nothing and never elicits or
    samples; with `supervisor = "agent"` a local reviewer decides, otherwise escalation denies.
    """


class ExceptionError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass
class ExceptionRecord:
    exception_id: str
    key: str
    root_run_id: str
    run_path: str
    node_id: str | None
    kind: str
    summary: str
    detail: dict
    options: list[str]
    default: str | None
    needs_answer: bool
    state: str
    created_at: float
    answer: str | None = None
    note: str | None = None
    answered_by: str | None = None
    answered_at: float | None = None
    delivered_at: float | None = None
    closed_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _record(row: sqlite3.Row) -> ExceptionRecord:
    return ExceptionRecord(
        exception_id=row["exception_id"], key=row["key"], root_run_id=row["root_run_id"],
        run_path=row["run_path"], node_id=row["node_id"], kind=row["kind"],
        summary=row["summary"], detail=json.loads(row["detail"]),
        options=json.loads(row["options"]), default=row["default"],
        needs_answer=bool(row["needs_answer"]), state=row["state"], created_at=row["created_at"],
        answer=row["answer"], note=row["note"], answered_by=row["answered_by"],
        answered_at=row["answered_at"], delivered_at=row["delivered_at"],
        closed_reason=row["closed_reason"])


class ExceptionQueue:
    def __init__(self, store: Store, *, clock: Callable[[], float] = time.time):
        self.store = store
        self.clock = clock
        with store.txn() as conn:
            for sql in _DDL:
                conn.execute(sql)

    def _fetch(self, conn: sqlite3.Connection, exception_id: str) -> ExceptionRecord:
        row = conn.execute("SELECT * FROM exceptions WHERE exception_id=?",
                           (exception_id,)).fetchone()
        if row is None:
            raise ExceptionError("not_found", f"no exception {exception_id!r}")
        return _record(row)

    def raise_(self, *, key: str, root_run_id: str, run_path: str, node_id: str | None,
               kind: str, summary: str, detail: dict | None = None,
               options: Sequence[str] = (), default: str | None = None,
               needs_answer: bool = True) -> ExceptionRecord:
        exception_id = ids.exception_id(key)
        with self.store.txn() as conn:
            row = conn.execute("SELECT * FROM exceptions WHERE exception_id=?",
                               (exception_id,)).fetchone()
            if row is not None:
                return _record(row)
            conn.execute(
                'INSERT INTO exceptions (exception_id, key, root_run_id, run_path, node_id, kind,'
                ' summary, detail, options, "default", needs_answer, state, created_at)'
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,'open',?)",
                (exception_id, key, root_run_id, run_path, node_id, kind, summary,
                 json.dumps(detail or {}, default=str), json.dumps(list(options)), default,
                 1 if needs_answer else 0, self.clock()))
            return self._fetch(conn, exception_id)

    def get(self, exception_id: str) -> ExceptionRecord:
        return self._fetch(self.store.read(), exception_id)

    def list(self, *, state: str = "open", root_run_id: str | None = None,
             kind: str | None = None, limit: int = 100) -> list[ExceptionRecord]:
        if state != "all" and state not in _STATES:
            raise ValueError(f"state must be one of {(*_STATES, 'all')}, got {state!r}")
        where, params = [], []
        if state != "all":
            where.append("state=?")
            params.append(state)
        if root_run_id is not None:
            where.append("root_run_id=?")
            params.append(root_run_id)
        if kind is not None:
            where.append("kind=?")
            params.append(kind)
        sql = "SELECT * FROM exceptions"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at, rowid LIMIT ?"
        params.append(limit)
        return [_record(r) for r in self.store.read().execute(sql, params)]

    def answer(self, exception_id: str, answer: str, *, note: str | None = None,
               answered_by: str = "orchestrator") -> ExceptionRecord:
        with self.store.txn() as conn:
            rec = self._fetch(conn, exception_id)
            if rec.state == "closed":
                raise ExceptionError("closed", f"exception {exception_id} is closed"
                                     f" ({rec.closed_reason})")
            if rec.state in ("answered", "delivered"):
                if rec.answer == answer:
                    return rec
                raise ExceptionError(
                    "already_answered",
                    f"exception {exception_id} was already answered {rec.answer!r}")
            now = self.clock()
            if not rec.needs_answer:
                if answer != "ack":
                    raise ExceptionError("invalid_answer",
                                         f"answer {answer!r} is not one of ['ack']")
                conn.execute("UPDATE exceptions SET state='closed', closed_reason='ack', answer=?,"
                             " note=?, answered_by=?, answered_at=? WHERE exception_id=?",
                             (answer, note, answered_by, now, exception_id))
                return self._fetch(conn, exception_id)
            if answer not in rec.options:
                raise ExceptionError("invalid_answer",
                                     f"answer {answer!r} is not one of {rec.options}")
            conn.execute("UPDATE exceptions SET state='answered', answer=?, note=?,"
                         " answered_by=?, answered_at=? WHERE exception_id=?",
                         (answer, note, answered_by, now, exception_id))
            return self._fetch(conn, exception_id)

    def mark_delivered(self, exception_id: str) -> None:
        with self.store.txn() as conn:
            conn.execute("UPDATE exceptions SET state='delivered', delivered_at=?"
                         " WHERE exception_id=? AND state='answered'",
                         (self.clock(), exception_id))

    def undelivered(self) -> list[ExceptionRecord]:
        rows = self.store.read().execute(
            "SELECT * FROM exceptions WHERE state='answered' ORDER BY answered_at, rowid")
        return [_record(r) for r in rows]

    def close_for_root(self, root_run_id: str, reason: str) -> int:
        with self.store.txn() as conn:
            cur = conn.execute("UPDATE exceptions SET state='closed', closed_reason=?"
                               " WHERE root_run_id=? AND state='open'", (reason, root_run_id))
            return cur.rowcount

    def digest(self) -> dict[str, Any]:
        rows = self.list(state="open", limit=1_000_000)
        by_kind: dict[str, int] = {}
        roots: dict[str, int] = {}
        for r in rows:
            by_kind[r.kind] = by_kind.get(r.kind, 0) + 1
            roots[r.root_run_id] = roots.get(r.root_run_id, 0) + 1
        oldest = (self.clock() - min(r.created_at for r in rows)) if rows else None
        return {"open": len(rows), "needs_answer": sum(1 for r in rows if r.needs_answer),
                "by_kind": by_kind,
                "roots": [{"root_run_id": k, "open": v} for k, v in roots.items()],
                "oldest_age_s": oldest}


_FAILED = ("failed", "unverified", "interrupted", "lane_refused")


def _item(scope: Scope, kind: str, summary: str, *, options: Sequence[str] = (),
          needs_answer: bool, detail: dict | None = None) -> dict[str, Any]:
    return {"key": f"{scope.request_id}:{kind}", "node_id": scope.node_id, "kind": kind,
            "summary": summary, "detail": detail or {}, "options": list(options),
            "default": None, "needs_answer": needs_answer}


def route(outcome: TaskOutcome, node: TaskNode, scope: Scope) -> list[dict[str, Any]]:
    """Pure: the `raise_` kwargs (minus root_run_id and run_path) the outcome calls for (6.1)."""
    out: list[dict[str, Any]] = []
    refusal = next((x for x in (outcome.status, outcome.error)
                    if x in ("budget_exhausted", "lane_closed_timeout")), None)
    detail = {"status": outcome.status, "error": outcome.error}
    if refusal == "budget_exhausted":
        out.append(_item(scope, "budget_exhausted",
                         f"node {scope.node_id}: budget exhausted", needs_answer=False,
                         detail=detail))
    elif refusal == "lane_closed_timeout":
        out.append(_item(scope, "lane_closed",
                         f"node {scope.node_id}: lane closed, admission timed out",
                         needs_answer=False, detail=detail))
    failed = outcome.status in _FAILED or refusal is not None
    if failed and getattr(node, "on_failure", "fail") == "exception":
        out.append(_item(scope, "node_failed", f"node {scope.node_id} failed ({outcome.status})",
                         options=("retry", "skip", "abort"), needs_answer=True, detail=detail))
    if outcome.test_change_request:
        out.append(_item(scope, "test_change_request",
                         f"node {scope.node_id} asks for a test change",
                         options=("reject", "accept"), needs_answer=True,
                         detail={"request": outcome.test_change_request}))
    if outcome.status != "completed" and outcome.guard_denials:
        out.append(_item(scope, "guard_denied",
                         f"node {scope.node_id}: guard denied {len(outcome.guard_denials)} call(s)",
                         needs_answer=False, detail={"denials": outcome.guard_denials}))
    return out
