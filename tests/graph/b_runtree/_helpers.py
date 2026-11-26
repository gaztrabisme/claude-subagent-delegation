"""Helpers for the run-tree tests: a Store on tmp_path and one-line ledger moves."""

from __future__ import annotations

from pathlib import Path


def make_store(tmp_path: Path, name: str = "g.sqlite3"):
    from subagent.graph.store import Store

    return Store(tmp_path / name)


def make_ledger(tmp_path: Path, *, max_depth: int = 3, pool: float | None = 10.0):
    from subagent.graph.ledger import Ledger

    store = make_store(tmp_path)
    ledger = Ledger(store, max_depth=max_depth)
    if pool is not None:
        ledger.ensure_pool("default", pool)
    return store, ledger


def open_root(ledger, root="gr-000000000001", requested=None, pool="default"):
    return ledger.open_root(root_run_id=root, pool=pool, requested_usd=requested,
                            graph_id="g", graph_hash="h" * 64)


def reserve(store, ledger, run_path, request_id, amount, attempt=1):
    with store.txn() as conn:
        ledger.reserve(conn, run_path=run_path, request_id=request_id, attempt=attempt,
                       amount_usd=amount)


def settle(store, ledger, request_id, actual, attempt=1):
    with store.txn() as conn:
        return ledger.settle(conn, request_id=request_id, attempt=attempt, actual_usd=actual)


def release(store, ledger, request_id, attempt=1):
    with store.txn() as conn:
        ledger.release(conn, request_id=request_id, attempt=attempt)
