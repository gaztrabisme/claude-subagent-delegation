"""SQLite state file for the graph: connections, migrations, BEGIN IMMEDIATE transactions.

One file (`graph.sqlite3`, WAL, busy_timeout 20 s) holds the run tree, the journal, the
admission queue and the workers table, so a lease grant and its budget reservation are one
transaction. Money is stored as integer micro-dollars.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

BUSY_TIMEOUT_S = 20.0

MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """CREATE TABLE accounts (
            run_path TEXT PRIMARY KEY, parent_path TEXT, root_run_id TEXT, depth INTEGER NOT NULL,
            node_id TEXT, graph_id TEXT, graph_hash TEXT, cap INTEGER NOT NULL,
            reserved INTEGER NOT NULL DEFAULT 0, held INTEGER NOT NULL DEFAULT 0,
            spent INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL,
            capped INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, closed_at REAL)""",
        "CREATE INDEX accounts_root ON accounts (root_run_id)",
        """CREATE TABLE reservations (
            request_id TEXT NOT NULL, attempt INTEGER NOT NULL, run_path TEXT NOT NULL,
            amount INTEGER NOT NULL, state TEXT NOT NULL, charged INTEGER NOT NULL DEFAULT 0,
            cost_source TEXT, overrun INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (request_id, attempt))""",
        "CREATE INDEX reservations_path ON reservations (run_path)",
        """CREATE TABLE journal (
            request_id TEXT NOT NULL, attempt INTEGER NOT NULL, root_run_id TEXT NOT NULL,
            run_path TEXT NOT NULL, node_id TEXT NOT NULL, lane TEXT NOT NULL, phase TEXT NOT NULL,
            worker_id TEXT, agent_id TEXT, run_id TEXT, outcome TEXT, reserved INTEGER NOT NULL,
            charged INTEGER, cost_source TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
            PRIMARY KEY (request_id, attempt))""",
        "CREATE INDEX journal_root ON journal (root_run_id)",
        "CREATE INDEX journal_phase ON journal (phase)",
        """CREATE TABLE queue (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL,
            attempt INTEGER NOT NULL, lane TEXT NOT NULL, priority INTEGER NOT NULL,
            fairness_key TEXT NOT NULL,
            run_path TEXT NOT NULL, node_id TEXT NOT NULL, reserve INTEGER NOT NULL,
            deps TEXT NOT NULL, enqueued_at REAL NOT NULL, root_run_id TEXT NOT NULL,
            UNIQUE (request_id, attempt))""",
        "CREATE INDEX queue_lane ON queue (lane)",
        """CREATE TABLE leases (
            lease_id TEXT PRIMARY KEY, lane TEXT NOT NULL, request_id TEXT NOT NULL,
            attempt INTEGER NOT NULL, fairness_key TEXT NOT NULL, holder TEXT,
            acquired_at REAL NOT NULL, expires_at REAL NOT NULL, state TEXT NOT NULL)""",
        "CREATE INDEX leases_lane ON leases (lane, state)",
        "CREATE INDEX leases_req ON leases (request_id, attempt)",
        """CREATE TABLE fairness (
            lane TEXT NOT NULL, fairness_key TEXT NOT NULL, last_seq INTEGER NOT NULL,
            PRIMARY KEY (lane, fairness_key))""",
        """CREATE TABLE workers (
            worker_id TEXT PRIMARY KEY, pid INTEGER NOT NULL, started_at REAL NOT NULL,
            beat_at REAL NOT NULL)""",
    ),
}


def micro(usd: float) -> int:
    """USD to integer micro-dollars."""
    return round(usd * 1_000_000)


def usd(micros: int | None) -> float:
    """Integer micro-dollars to USD rounded to 6 decimals."""
    return round((micros or 0) / 1_000_000, 6)


class Store:
    """The SQLite file. Every write is a `BEGIN IMMEDIATE` transaction on a fresh connection."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        parent = self.path.parent
        if not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
            os.chmod(parent, 0o700)
        if not self.path.exists():
            with contextlib.suppress(FileExistsError):
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        self._local = threading.local()
        self._migrate()
        self._secure_sidecars()

    def _secure_sidecars(self) -> None:
        """Re-assert 0600 on -wal/-shm whenever SQLite may have just created them."""
        for suffix in ("-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.chmod(f"{self.path}{suffix}", 0o600)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=BUSY_TIMEOUT_S, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _migrate(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
                row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                version = int(row[0]) if row else 0
                for target in sorted(MIGRATIONS):
                    if target > version:
                        for sql in MIGRATIONS[target]:
                            conn.execute(sql)
                        version = target
                conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)",
                             (str(version),))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    @property
    def schema_version(self) -> int:
        row = self.read().execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return int(row[0])

    @contextlib.contextmanager
    def txn(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE ... COMMIT, or ROLLBACK when the block raises."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._secure_sidecars()
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    def read(self) -> sqlite3.Connection:
        """An autocommit connection for reads (one per thread and process)."""
        cached = getattr(self._local, "conn", None)
        if cached is None or self._local.pid != os.getpid():
            cached = self._connect()
            cached.execute("SELECT 1 FROM meta LIMIT 1").fetchall()
            self._secure_sidecars()
            self._local.conn, self._local.pid = cached, os.getpid()
        return cached
