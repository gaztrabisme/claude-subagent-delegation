"""The `graph_roots` table: one row per root run with its last known state (additive, pure).

Temporal holds the live state; this row is what `graph_list` and `graph_status` fall back to
while the Temporal server is unreachable. Created with `CREATE TABLE IF NOT EXISTS` (the store's
schema version is pinned by the locked store tests, as for the exceptions table).
"""

from __future__ import annotations

import time
from typing import Any

from .store import Store

_DDL = """CREATE TABLE IF NOT EXISTS graph_roots (
    root_run_id TEXT PRIMARY KEY, graph_id TEXT, graph_version INTEGER, state TEXT NOT NULL,
    started_at REAL NOT NULL, updated_at REAL NOT NULL, inputs_digest TEXT)"""

TERMINAL = ("completed", "failed", "cancelled", "budget_exhausted")


def ensure(store: Store) -> None:
    with store.txn() as conn:
        conn.execute(_DDL)


def register(store: Store, root_run_id: str, *, graph_id: str | None = None,
             graph_version: int | None = None, state: str = "running",
             inputs_digest: str | None = None) -> None:
    """Idempotent: the first call wins, later calls keep the row."""
    now = time.time()
    with store.txn() as conn:
        conn.execute(_DDL)
        conn.execute("INSERT OR IGNORE INTO graph_roots VALUES (?,?,?,?,?,?,?)",
                     (root_run_id, graph_id, graph_version, state, now, now, inputs_digest))


def set_state(store: Store, root_run_id: str, state: str) -> None:
    now = time.time()
    with store.txn() as conn:
        conn.execute(_DDL)
        conn.execute("INSERT OR IGNORE INTO graph_roots VALUES (?,?,?,?,?,?,?)",
                     (root_run_id, None, None, state, now, now, None))
        conn.execute("UPDATE graph_roots SET state=?, updated_at=? WHERE root_run_id=?",
                     (state, now, root_run_id))


def get(store: Store, root_run_id: str) -> dict[str, Any] | None:
    ensure(store)
    row = store.read().execute("SELECT * FROM graph_roots WHERE root_run_id=?",
                               (root_run_id,)).fetchone()
    return dict(row) if row else None


def listing(store: Store) -> list[dict[str, Any]]:
    ensure(store)
    rows = store.read().execute("SELECT * FROM graph_roots ORDER BY started_at DESC, rowid DESC")
    return [dict(r) for r in rows]
