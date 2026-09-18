"""Trace record shape. Schema 2 added session_id to run records."""

from __future__ import annotations

import json
from pathlib import Path

from subagent_mcp.runs import Run
from subagent_mcp.trace import SCHEMA, Trace


def test_run_record_carries_session_id_and_current_schema(tmp_path: Path):
    path = tmp_path / "trace.jsonl"
    trace = Trace(path)
    run = Run(run_id="run-abc123", agent_id="a1", prompt="hi", session_id="sess-xyz")
    trace.run(run, str(tmp_path), "glm-5.3[1m]")

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["schema"] == SCHEMA == 2
    assert record["kind"] == "run"
    assert record["session_id"] == "sess-xyz"
