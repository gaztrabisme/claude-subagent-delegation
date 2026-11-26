"""Helpers for the R1a-agy addendum: synthetic agy conversation records."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tests.r1a_agy._common import init_event, result_event, run_fake, text_step, trace_of

__all__ = ["run_fake", "trace_of", "CONV", "CMD_A", "CMD_B", "CMD_FALSE", "make_db",
           "events", "run_with_state", "state_dir_for"]

CONV = "c-1"  # the conversation id the fake agy reports in init_event()
CMD_A = "echo A > /tmp/audit-a"
CMD_B = "touch /tmp/audit-b"
CMD_FALSE = "echo FALSE > /tmp/audit-false"
NOISE = b"\x00\x01\xff\xfe\x08\x12\x0aSTEP\x9c\x00\x00"


def _blob(text: str) -> bytes:
    return NOISE + text.encode("utf-8") + NOISE[::-1]


def make_db(path: Path, *, long_command: str | None = None) -> Path:
    """A synthetic agy conversation record with the shapes R2 names."""
    path.parent.mkdir(parents=True, exist_ok=True)
    obj_a = f'{{"BypassSandbox":true,"CommandLine":"{CMD_A}"}}'
    obj_b = f'{{"BypassSandbox" : true, "CommandLine": "{CMD_B}"}}'
    obj_f = f'{{"BypassSandbox":false,"CommandLine":"{CMD_FALSE}"}}'
    prose = "The user asked me to call run_command with BypassSandbox: true, so I did."
    payloads = [obj_a, prose, obj_b, obj_a, obj_f]
    if long_command is not None:
        payloads.append(f'{{"BypassSandbox":true,"CommandLine":"{long_command}"}}')
    con = sqlite3.connect(path)
    con.execute("create table steps (step_index integer primary key, step_payload blob)")
    for i, text in enumerate(payloads):
        con.execute("insert into steps values (?, ?)", (i, _blob(text)))
    con.commit()
    con.close()
    return path


def events() -> list:
    return [init_event(), text_step(1, "working"), result_event("all done")]


def state_dir_for(tmp_path: Path) -> Path:
    return tmp_path / "agy_state"


def run_with_state(tmp_path: Path, monkeypatch, *, audit: bool | None = True, **extra):
    """One real server turn with bypass_audit configured and agy_state_dir in tmp_path."""
    cfg: dict = {"agy_state_dir": str(state_dir_for(tmp_path)), **extra}
    if audit is not None:
        cfg["bypass_audit"] = audit
    return run_fake(tmp_path, monkeypatch, events(), **cfg)
