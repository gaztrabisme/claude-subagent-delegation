"""R2: the off-by-default bypass audit reads agy's own conversation record after a turn."""

from __future__ import annotations

import builtins
import os
import sqlite3
import sys
from urllib.parse import unquote

import pytest

from subagent.runs import COMPLETED

from ._common2 import (
    CMD_A,
    CMD_B,
    CMD_FALSE,
    CONV,
    make_db,
    run_with_state,
    state_dir_for,
    trace_of,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="antigravity runs only under macOS Seatbelt"
)


def _db(tmp_path, conv=CONV, **kw):
    return make_db(state_dir_for(tmp_path) / "conversations" / f"{conv}.db", **kw)


def _unavailable(trace_path):
    return trace_of(trace_path, "bypass_audit_unavailable")


def test_r2_3_exactly_the_distinct_true_objects_are_recorded(tmp_path, monkeypatch) -> None:
    """R2.3: one sandbox_bypass record per distinct true object, with the fixed fields and source."""
    _db(tmp_path)
    agent, run, trace_path, _ = run_with_state(tmp_path, monkeypatch)
    records = trace_of(trace_path, "sandbox_bypass")
    assert sorted(r["command"] for r in records) == sorted([CMD_A, CMD_B]), records
    for rec in records:
        assert rec["type"] == "sandbox_bypass"
        assert rec["agent_id"] == agent.agent_id
        assert rec["run_id"] == run.run_id
        assert rec["provider"] == "gemini"
        assert rec["tool"] == "run_command"
        assert rec["source"] == "conversation_record"
        assert isinstance(rec["ts"], (int, float))
    assert CMD_FALSE not in {r["command"] for r in records}
    assert not _unavailable(trace_path)


def test_r2_3_command_is_clipped_to_500_characters(tmp_path, monkeypatch) -> None:
    """R2.3: a recorded command is clipped to 500 characters."""
    _db(tmp_path, long_command="echo " + "y" * 2000)
    _, _, trace_path, _ = run_with_state(tmp_path, monkeypatch)
    lengths = [len(r["command"]) for r in trace_of(trace_path, "sandbox_bypass")]
    assert len(lengths) == 3 and max(lengths) == 500


def test_r2_4_run_state_and_result_are_unchanged(tmp_path, monkeypatch) -> None:
    """R2.4: the audit does not change the run's state, finish reason or result."""
    on_dir, off_dir = tmp_path / "on", tmp_path / "off"
    on_dir.mkdir()
    off_dir.mkdir()
    _db(on_dir)
    _, on, on_trace, _ = run_with_state(on_dir, monkeypatch, audit=True)
    assert len(trace_of(on_trace, "sandbox_bypass")) == 2, "the audit must have run"
    _db(off_dir)
    _, off, off_trace, _ = run_with_state(off_dir, monkeypatch, audit=False)
    assert not trace_of(off_trace, "sandbox_bypass"), "the off control must not audit"
    assert on.state == COMPLETED
    for field in ("state", "finish_reason", "result_text", "final_response", "error"):
        assert getattr(on, field) == getattr(off, field), field
    assert "all done" in f"{on.result_text}{on.final_response}"


def _missing(tmp_path):
    pass


def _not_sqlite(tmp_path):
    path = state_dir_for(tmp_path) / "conversations" / f"{CONV}.db"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"this is not a sqlite database\n" * 50)


def _no_steps_table(tmp_path):
    path = state_dir_for(tmp_path) / "conversations" / f"{CONV}.db"
    path.parent.mkdir(parents=True)
    con = sqlite3.connect(path)
    con.execute("create table other (x)")
    con.commit()
    con.close()


def _no_payload_column(tmp_path):
    path = state_dir_for(tmp_path) / "conversations" / f"{CONV}.db"
    path.parent.mkdir(parents=True)
    con = sqlite3.connect(path)
    con.execute("create table steps (step_index integer, other blob)")
    con.commit()
    con.close()


def _db_path_is_a_directory(tmp_path):
    (state_dir_for(tmp_path) / "conversations" / f"{CONV}.db").mkdir(parents=True)


@pytest.mark.parametrize("prepare", [_missing, _not_sqlite, _no_steps_table, _no_payload_column,
                                     _db_path_is_a_directory])
def test_r2_5_unreadable_record_fails_open_with_one_unavailable_record(
        tmp_path, monkeypatch, prepare) -> None:
    """R2.5: a missing, non-SQLite, steps-less or unreadable record writes one bypass_audit_unavailable record."""
    prepare(tmp_path)
    agent, run, trace_path, _ = run_with_state(tmp_path, monkeypatch)
    records = _unavailable(trace_path)
    assert len(records) == 1, records
    rec = records[0]
    assert rec["type"] == "bypass_audit_unavailable"
    assert rec["agent_id"] == agent.agent_id and rec["run_id"] == run.run_id
    assert rec["provider"] == "gemini"
    assert isinstance(rec["reason"], str) and rec["reason"]
    assert isinstance(rec["ts"], (int, float))
    assert not trace_of(trace_path, "sandbox_bypass")
    assert run.state == COMPLETED and "all done" in f"{run.result_text}{run.final_response}"


def _uri_path(target: str) -> str:
    """The filesystem path of a sqlite3 target (a plain path or a file: URI)."""
    if target.startswith("file:"):
        return unquote(target[len("file:"):].split("?", 1)[0])
    return target


def test_r2_6_audit_unset_is_on(tmp_path, monkeypatch) -> None:
    """R2.6 (S56): bypass_audit unset audits: exactly the run's own record is opened, read-only."""
    own = _db(tmp_path)
    state = state_dir_for(tmp_path)
    make_db(state / "conversations" / "other-id.db")
    # A decoy at the default state dir (HOME is the test's home): the configured dir must win.
    decoy_dir = tmp_path / "home" / ".gemini" / "antigravity-cli"
    make_db(decoy_dir / "conversations" / f"{CONV}.db")
    watched = {str(p) for p in (state, state.resolve(), decoy_dir, decoy_dir.resolve())}
    opened: list[tuple[str, str]] = []
    real_connect, real_open, real_os_open = sqlite3.connect, builtins.open, os.open

    def connect(target, *a, **kw):
        opened.append(("sqlite3.connect", str(target)))
        return real_connect(target, *a, **kw)

    def spy_open(file, *a, **kw):
        opened.append(("open", str(file)))
        return real_open(file, *a, **kw)

    def spy_os_open(path, *a, **kw):
        opened.append(("os.open", str(path)))
        return real_os_open(path, *a, **kw)

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(os, "open", spy_os_open)
    _, run, trace_path, _ = run_with_state(tmp_path, monkeypatch, audit=None)
    touched = [(how, target) for how, target in opened
               if any(_uri_path(target).startswith(w) for w in watched)]
    assert len(touched) == 1, touched
    how, target = touched[0]
    assert how == "sqlite3.connect" and "mode=ro" in target, touched
    assert _uri_path(target) == str(own.resolve()), touched
    records = trace_of(trace_path, "sandbox_bypass")
    assert sorted(r["command"] for r in records) == sorted([CMD_A, CMD_B]), records
    assert all(r["source"] == "conversation_record" for r in records)
    assert run.state == COMPLETED


@pytest.mark.parametrize("audit", [False])
def test_r2_6_audit_off_opens_nothing(tmp_path, monkeypatch, audit) -> None:
    """R2.6: with bypass_audit = false, the conversation record is never opened and nothing is recorded."""
    db = _db(tmp_path)
    db.chmod(0)  # a read attempt would raise and be recorded as unavailable
    state = str(state_dir_for(tmp_path))
    opened: list[str] = []
    real_connect, real_open, real_os_open = sqlite3.connect, builtins.open, os.open

    def connect(target, *a, **kw):
        opened.append(str(target))
        return real_connect(target, *a, **kw)

    def spy_open(file, *a, **kw):
        opened.append(str(file))
        return real_open(file, *a, **kw)

    def spy_os_open(path, *a, **kw):
        opened.append(str(path))
        return real_os_open(path, *a, **kw)

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(os, "open", spy_os_open)
    try:
        _, run, trace_path, _ = run_with_state(tmp_path, monkeypatch, audit=audit)
    finally:
        db.chmod(0o600)
    assert not [p for p in opened if state in p], opened
    assert not trace_of(trace_path, "sandbox_bypass")
    assert not _unavailable(trace_path)
    assert run.state == COMPLETED


def test_r2_2_only_the_runs_own_conversation_is_read(tmp_path, monkeypatch) -> None:
    """R2.2: only <agy_state_dir>/conversations/<conversation_id>.db is opened; a decoy id is not."""
    _db(tmp_path)
    decoy = make_db(state_dir_for(tmp_path) / "conversations" / "other-id.db")
    con = sqlite3.connect(decoy)
    con.execute("insert into steps values (99, ?)",
                (b'\x00{"BypassSandbox":true,"CommandLine":"DECOY_CMD"}\x00',))
    con.commit()
    con.close()
    decoy_bytes = decoy.read_bytes()
    opened: list[str] = []
    real_connect = sqlite3.connect

    def connect(target, *a, **kw):
        opened.append(str(target))
        return real_connect(target, *a, **kw)

    monkeypatch.setattr(sqlite3, "connect", connect)
    _, _, trace_path, _ = run_with_state(tmp_path, monkeypatch)
    commands = {r["command"] for r in trace_of(trace_path, "sandbox_bypass")}
    assert commands == {CMD_A, CMD_B}
    assert "DECOY_CMD" not in commands
    assert not [p for p in opened if "other-id" in p], opened
    assert any(f"{CONV}.db" in p for p in opened), opened
    assert decoy.read_bytes() == decoy_bytes


def test_r2_2_record_is_opened_read_only(tmp_path, monkeypatch) -> None:
    """R2.2: the record is opened read-only: bytes, mtime and the folder listing are unchanged."""
    db = _db(tmp_path)
    folder = db.parent
    before = (db.read_bytes(), db.stat().st_mtime_ns, sorted(p.name for p in folder.iterdir()))
    opened: list[str] = []
    real_connect = sqlite3.connect

    def connect(target, *a, **kw):
        opened.append(str(target))
        return real_connect(target, *a, **kw)

    monkeypatch.setattr(sqlite3, "connect", connect)
    _, _, trace_path, _ = run_with_state(tmp_path, monkeypatch)
    assert len(trace_of(trace_path, "sandbox_bypass")) == 2, "audit must have read the record"
    after = (db.read_bytes(), db.stat().st_mtime_ns, sorted(p.name for p in folder.iterdir()))
    assert after == before
    assert any("mode=ro" in p for p in opened), opened
