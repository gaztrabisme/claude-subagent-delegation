"""Spec 5.1: the SQLite store: file modes, WAL, BEGIN IMMEDIATE, rollback, migrations."""

from __future__ import annotations

import os
import sqlite3
import stat

import pytest
from subagent.graph.store import Store


def test_store_creates_file_0600_and_parent_0700(tmp_path):
    path = tmp_path / "sub" / "deeper" / "g.sqlite3"
    Store(path)
    assert path.exists()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700


def test_schema_version_is_one_and_stable_across_reopen(tmp_path):
    assert Store(tmp_path / "g.sqlite3").schema_version == 1
    assert Store(tmp_path / "g.sqlite3").schema_version == 1


def test_wal_and_busy_timeout(tmp_path):
    store = Store(tmp_path / "g.sqlite3")
    with store.txn() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 20000


def test_txn_commits_on_success_and_rolls_back_on_error(tmp_path):
    store = Store(tmp_path / "g.sqlite3")
    with store.txn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS probe (k TEXT PRIMARY KEY, v INTEGER)")
        conn.execute("INSERT INTO probe VALUES ('a', 1)")
    with pytest.raises(RuntimeError), store.txn() as conn:
        conn.execute("INSERT INTO probe VALUES ('b', 2)")
        raise RuntimeError("boom")
    rows = store.read().execute("SELECT k FROM probe ORDER BY k").fetchall()
    assert [r[0] for r in rows] == ["a"]


def test_txn_is_begin_immediate_a_second_writer_waits_or_fails_fast(tmp_path):
    """While one txn is open, a second connection cannot start a write transaction."""
    store = Store(tmp_path / "g.sqlite3")
    with store.txn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS probe (k TEXT)")
    with store.txn():
        other = sqlite3.connect(str(tmp_path / "g.sqlite3"), timeout=0.2,
                                isolation_level=None)
        try:
            with pytest.raises(sqlite3.OperationalError):
                other.execute("BEGIN IMMEDIATE")
        finally:
            other.close()


def test_read_connection_sees_committed_rows_without_a_txn(tmp_path):
    store = Store(tmp_path / "g.sqlite3")
    with store.txn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS probe (k TEXT)")
        conn.execute("INSERT INTO probe VALUES ('x')")
    assert store.read().execute("SELECT count(*) FROM probe").fetchone()[0] == 1


def test_a_second_store_on_the_same_file_shares_data(tmp_path):
    first = Store(tmp_path / "g.sqlite3")
    with first.txn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS probe (k TEXT)")
        conn.execute("INSERT INTO probe VALUES ('shared')")
    second = Store(tmp_path / "g.sqlite3")
    assert second.read().execute("SELECT k FROM probe").fetchone()[0] == "shared"
