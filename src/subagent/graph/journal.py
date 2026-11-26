"""Per-attempt task journal (spec section 3.5): one row per (request_id, attempt)."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from .store import Store, micro, usd

LEGAL: dict[str, frozenset[str]] = {
    "queued": frozenset({"admitted", "released"}),
    "admitted": frozenset({"dispatched", "released"}),
    "dispatched": frozenset({"finished", "interrupted"}),
    "finished": frozenset({"settled"}),
    "settled": frozenset(),
    "interrupted": frozenset(),
    "released": frozenset(),
}
_PLAIN = ("root_run_id", "run_path", "node_id", "lane", "worker_id", "agent_id", "run_id",
          "cost_source")
_FIELDS = frozenset(_PLAIN) | {"outcome", "reserved_usd", "charged_usd"}


class JournalError(RuntimeError):
    pass


@dataclass
class JournalEntry:
    request_id: str
    attempt: int
    root_run_id: str
    run_path: str
    node_id: str
    lane: str
    phase: str
    worker_id: str | None
    agent_id: str | None
    run_id: str | None
    outcome: dict | None
    reserved_usd: float
    charged_usd: float | None
    cost_source: str | None
    created_at: float
    updated_at: float


def _entry(row: sqlite3.Row) -> JournalEntry:
    return JournalEntry(
        request_id=row["request_id"], attempt=row["attempt"], root_run_id=row["root_run_id"],
        run_path=row["run_path"], node_id=row["node_id"], lane=row["lane"], phase=row["phase"],
        worker_id=row["worker_id"], agent_id=row["agent_id"], run_id=row["run_id"],
        outcome=json.loads(row["outcome"]) if row["outcome"] is not None else None,
        reserved_usd=usd(row["reserved"]),
        charged_usd=None if row["charged"] is None else usd(row["charged"]),
        cost_source=row["cost_source"], created_at=row["created_at"], updated_at=row["updated_at"])


class Journal:
    def __init__(self, store: Store):
        self.store = store

    def get(self, request_id: str, attempt: int) -> JournalEntry | None:
        row = self.store.read().execute(
            "SELECT * FROM journal WHERE request_id=? AND attempt=?", (request_id, attempt)
        ).fetchone()
        return _entry(row) if row else None

    def get_in(self, conn: sqlite3.Connection, request_id: str,
               attempt: int) -> JournalEntry | None:
        """`get` inside the caller's transaction."""
        row = conn.execute("SELECT * FROM journal WHERE request_id=? AND attempt=?",
                           (request_id, attempt)).fetchone()
        return _entry(row) if row else None

    def for_root_in(self, conn: sqlite3.Connection, root_run_id: str) -> list[JournalEntry]:
        rows = conn.execute("SELECT * FROM journal WHERE root_run_id=? ORDER BY created_at, rowid",
                            (root_run_id,))
        return [_entry(r) for r in rows]

    def attempts(self, request_id: str) -> list[JournalEntry]:
        rows = self.store.read().execute(
            "SELECT * FROM journal WHERE request_id=? ORDER BY attempt", (request_id,))
        return [_entry(r) for r in rows]

    def for_root(self, root_run_id: str) -> list[JournalEntry]:
        rows = self.store.read().execute(
            "SELECT * FROM journal WHERE root_run_id=? ORDER BY created_at, rowid",
            (root_run_id,))
        return [_entry(r) for r in rows]

    def advance(self, conn: sqlite3.Connection, request_id: str, attempt: int, phase: str,
                **fields: Any) -> JournalEntry:
        unknown = set(fields) - _FIELDS
        if unknown:
            raise TypeError(f"unknown journal fields {sorted(unknown)}")
        values: dict[str, Any] = {k: fields[k] for k in _PLAIN if k in fields}
        if "outcome" in fields:
            outcome = fields["outcome"]
            values["outcome"] = None if outcome is None else json.dumps(outcome)
        if "reserved_usd" in fields:
            values["reserved"] = micro(fields["reserved_usd"])
        if "charged_usd" in fields:
            charged = fields["charged_usd"]
            values["charged"] = None if charged is None else micro(charged)
        now = time.time()
        row = conn.execute("SELECT phase FROM journal WHERE request_id=? AND attempt=?",
                           (request_id, attempt)).fetchone()
        if row is None:
            if phase != "queued":
                raise JournalError(f"no journal entry for {request_id} attempt {attempt}")
            values.setdefault("root_run_id", request_id.split("/")[0])
            for key in ("run_path", "node_id", "lane"):
                values.setdefault(key, "")
            values.setdefault("reserved", 0)
            values.update(request_id=request_id, attempt=attempt, phase=phase, created_at=now,
                          updated_at=now)
            cols = ", ".join(values)
            conn.execute(f"INSERT INTO journal ({cols}) VALUES ({', '.join('?' * len(values))})",
                         tuple(values.values()))
        else:
            if phase not in LEGAL.get(row["phase"], frozenset()):
                raise JournalError(f"illegal transition {row['phase']} -> {phase}")
            values.update(phase=phase, updated_at=now)
            sets = ", ".join(f"{k}=?" for k in values)
            conn.execute(f"UPDATE journal SET {sets} WHERE request_id=? AND attempt=?",
                         (*values.values(), request_id, attempt))
        return _entry(conn.execute("SELECT * FROM journal WHERE request_id=? AND attempt=?",
                                   (request_id, attempt)).fetchone())
