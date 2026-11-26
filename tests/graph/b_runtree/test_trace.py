"""Spec 10: graph trace records are additive, schema 3, with exactly the required keys."""

from __future__ import annotations

import json

import pytest

from subagent.telemetry.trace import SCHEMA, Trace

REQUIRED = {
    "graph_run": {"root_run_id", "run_path", "graph_id", "graph_hash", "event", "state",
                  "depth", "cap_usd", "spent_usd"},
    "graph_node": {"root_run_id", "run_path", "node_id", "kind", "scope", "status", "attempts",
                   "request_id", "cost_usd", "error"},
    "graph_admission": {"root_run_id", "attempt_id", "lane", "event", "reason", "active", "cap"},
    "graph_budget": {"run_path", "attempt_id", "event", "amount_usd", "cost_source",
                     "remaining_usd"},
    "graph_exception": {"exception_id", "root_run_id", "kind", "event", "needs_answer"},
}
SAMPLE = {
    "root_run_id": "gr-000000000001", "run_path": "gr-000000000001", "graph_id": "g",
    "graph_hash": "h" * 64, "event": "started", "state": "running", "depth": 0, "cap_usd": 5.0,
    "spent_usd": 0.0, "node_id": "plan", "kind": "task", "scope": "plan", "status": "completed",
    "attempts": 1, "request_id": "gr-000000000001/plan", "cost_usd": 0.1, "error": None,
    "attempt_id": "gr-000000000001/plan#a1", "lane": "codex", "reason": None, "active": 1,
    "cap": 2, "amount_usd": 0.5, "cost_source": "reservation", "remaining_usd": 4.5,
    "exception_id": "gx-0123456789ab", "needs_answer": True,
}
EVENTS = {"graph_run": "finished", "graph_admission": "granted", "graph_budget": "settle",
          "graph_exception": "raised"}


def writer(kind):
    from subagent.graph import trace as graph_trace

    fn = getattr(graph_trace, f"write_{kind}", None)
    assert callable(fn), f"subagent.graph.trace must expose write_{kind}(trace, **fields)"
    return fn


def fields_for(kind):
    out = {k: SAMPLE[k] for k in REQUIRED[kind]}
    if kind in EVENTS:
        out["event"] = EVENTS[kind]
    return out


def test_graph_kinds_are_the_five_documented_kinds():
    from subagent.graph.trace import GRAPH_KINDS

    assert set(GRAPH_KINDS) == set(REQUIRED)


def test_graph_kinds_are_not_added_to_the_locked_kinds():
    from subagent.telemetry.trace import KINDS

    assert not any(k.startswith("graph_") for k in KINDS)


@pytest.mark.parametrize("kind", sorted(REQUIRED))
def test_writer_emits_exactly_required_keys_plus_common(tmp_path, kind):
    path = tmp_path / "trace.jsonl"
    writer(kind)(Trace(path), **fields_for(kind))
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert set(record) == REQUIRED[kind] | {"schema", "ts", "kind"}
    assert record["kind"] == kind
    assert record["schema"] == SCHEMA == 3
    assert isinstance(record["ts"], (int, float))
    for key, value in fields_for(kind).items():
        assert record[key] == value


@pytest.mark.parametrize("kind", sorted(REQUIRED))
def test_writer_never_records_prompt_text_or_environment(tmp_path, kind, monkeypatch):
    monkeypatch.setenv("SUBAGENT_SECRET_PROBE", "env-value-must-not-appear")
    path = tmp_path / "trace.jsonl"
    secret = "PROMPT-TEXT-MUST-NOT-APPEAR"
    try:
        writer(kind)(Trace(path), **fields_for(kind), prompt=secret, note=secret)
    except TypeError:
        return  # a writer that refuses unknown keys also keeps prompts out
    text = path.read_text() if path.exists() else ""
    assert secret not in text
    assert "env-value-must-not-appear" not in text
    if text:
        assert set(json.loads(text.splitlines()[0])) == REQUIRED[kind] | {"schema", "ts", "kind"}


@pytest.mark.parametrize("kind", sorted(REQUIRED))
def test_writer_is_a_noop_on_a_disabled_trace(tmp_path, kind):
    writer(kind)(Trace(None), **fields_for(kind))  # Trace(None) writes nothing, never raises


def test_event_vocabularies_in_the_spec_are_accepted(tmp_path):
    path = tmp_path / "trace.jsonl"
    trace = Trace(path)
    for event in ("started", "finished"):
        writer("graph_run")(trace, **dict(fields_for("graph_run"), event=event))
    for event in ("granted", "refused", "expired", "interrupted"):
        writer("graph_admission")(trace, **dict(fields_for("graph_admission"), event=event))
    for event in ("reserve", "settle", "release", "hold", "close"):
        writer("graph_budget")(trace, **dict(fields_for("graph_budget"), event=event))
    for event in ("raised", "answered", "delivered", "closed"):
        writer("graph_exception")(trace, **dict(fields_for("graph_exception"), event=event))
    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(events) == 2 + 4 + 5 + 4
