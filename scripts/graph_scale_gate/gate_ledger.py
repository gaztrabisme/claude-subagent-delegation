"""SQLite state shared by every process: budget ledger, attempt table, request journal.

All writes run under ``BEGIN IMMEDIATE`` so two lane workers cannot interleave a
check and an insert. A reservation settles exactly once; a request id in the journal
is never dispatched again.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS reservations (
  reservation_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, attempt INTEGER NOT NULL,
  amount REAL NOT NULL, status TEXT NOT NULL, actual REAL, run_id TEXT);
CREATE TABLE IF NOT EXISTS attempts (
  request_id TEXT NOT NULL, attempt INTEGER NOT NULL, state TEXT NOT NULL, base TEXT,
  PRIMARY KEY (request_id, attempt));
CREATE TABLE IF NOT EXISTS journal (request_id TEXT PRIMARY KEY, result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS markers (key TEXT PRIMARY KEY);
"""


class Ledger:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        with self._tx() as conn:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _tx(self) -> _Tx:
        return _Tx(self._connect())

    # -- journal ---------------------------------------------------------
    def journal_get(self, request_id: str) -> dict[str, Any] | None:
        with self._tx() as conn:
            row = conn.execute("SELECT result FROM journal WHERE request_id=?",
                               (request_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def journal_put(self, request_id: str, result: dict[str, Any]) -> bool:
        with self._tx() as conn:
            cur = conn.execute("INSERT OR IGNORE INTO journal VALUES (?, ?)",
                               (request_id, json.dumps(result)))
            return cur.rowcount == 1

    def marker_once(self, key: str) -> bool:
        with self._tx() as conn:
            return conn.execute("INSERT OR IGNORE INTO markers VALUES (?)",
                                (key,)).rowcount == 1

    # -- attempts --------------------------------------------------------
    def begin_attempt(self, request_id: str, base: dict[str, Any]) -> int:
        with self._tx() as conn:
            row = conn.execute("SELECT COALESCE(MAX(attempt), 0) FROM attempts "
                               "WHERE request_id=?", (request_id,)).fetchone()
            attempt = int(row[0]) + 1
            conn.execute("INSERT INTO attempts VALUES (?, ?, 'reserving', ?)",
                         (request_id, attempt, json.dumps(base)))
        return attempt

    def set_attempt_state(self, request_id: str, attempt: int, state: str) -> None:
        with self._tx() as conn:
            conn.execute("UPDATE attempts SET state=? WHERE request_id=? AND attempt=?",
                         (state, request_id, attempt))

    def dangling(self, request_id: str) -> tuple[list[tuple[int, dict[str, Any]]], list[str]]:
        """Attempts left 'started' by a dead activity, and reservations still open."""
        with self._tx() as conn:
            started = conn.execute(
                "SELECT attempt, base FROM attempts WHERE request_id=? AND state='started'",
                (request_id,)).fetchall()
            reserved = conn.execute(
                "SELECT reservation_id FROM reservations WHERE request_id=? "
                "AND status='reserved'", (request_id,)).fetchall()
        return [(a, json.loads(b)) for a, b in started], [r[0] for r in reserved]

    # -- budget ----------------------------------------------------------
    def reserve(self, request_id: str, attempt: int, amount: float,
                cap: float) -> tuple[bool, str, float]:
        """Reserve `amount` unless reserved+committed would pass `cap`.

        Returns (admitted, reservation_id, committed_plus_reserved_before).
        A zero amount is always admitted: it cannot raise the total.
        """
        reservation_id = f"res-{request_id}-{attempt}"
        with self._tx() as conn:
            used = conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN status='reserved' THEN amount ELSE actual END), 0) "
                "FROM reservations").fetchone()[0]
            if amount > 0 and round(used + amount, 9) > round(cap, 9):
                return False, reservation_id, float(used)
            conn.execute("INSERT INTO reservations VALUES (?, ?, ?, ?, 'reserved', NULL, NULL)",
                         (reservation_id, request_id, attempt, amount))
            return True, reservation_id, float(used)

    def settle(self, reservation_id: str, run_id: str | None, actual: float) -> bool:
        """Close a reservation at its actual cost. True only the first time."""
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE reservations SET status='settled', actual=?, run_id=? "
                "WHERE reservation_id=? AND status='reserved'",
                (actual, run_id, reservation_id))
            return cur.rowcount == 1


class _Tx:
    """BEGIN IMMEDIATE ... COMMIT around a connection, closed afterwards."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.conn.close()
