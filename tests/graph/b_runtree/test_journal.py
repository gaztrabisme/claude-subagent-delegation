"""Spec 3.5: the per-attempt journal: phases, legal transitions, stored fields."""

# ruff: noqa: C408

from __future__ import annotations

import pytest
from subagent.graph.journal import Journal, JournalEntry, JournalError

from ._helpers import make_store

ROOT = "gr-000000000001"
BASE = dict(root_run_id=ROOT, run_path=ROOT, node_id="plan", lane="codex", reserved_usd=0.5)


def journal(tmp_path):
    store = make_store(tmp_path)
    return store, Journal(store)


def adv(store, jr, request_id, attempt, phase, **fields):
    with store.txn() as conn:
        return jr.advance(conn, request_id, attempt, phase, **fields)


def walk(store, jr, request_id="gr-000000000001/plan", attempt=1, upto="settled"):
    chain = ["queued", "admitted", "dispatched", "finished", "settled"]
    extra = {
        "queued": dict(BASE),
        "dispatched": dict(worker_id="w-1-abc123", agent_id="ag-1", run_id="run-1"),
        "finished": dict(outcome={"status": "completed", "cost_usd": 0.2}),
        "settled": dict(charged_usd=0.2, cost_source="actual"),
    }
    entry = None
    for phase in chain:
        entry = adv(store, jr, request_id, attempt, phase, **extra.get(phase, {}))
        if phase == upto:
            break
    return entry


def test_missing_entry_is_none_and_attempts_empty(tmp_path):
    _, jr = journal(tmp_path)
    assert jr.get("nope", 1) is None
    assert jr.attempts("nope") == []
    assert jr.for_root(ROOT) == []


def test_queued_creates_entry_with_fields(tmp_path):
    store, jr = journal(tmp_path)
    entry = adv(store, jr, "gr-000000000001/plan", 1, "queued", **BASE)
    assert isinstance(entry, JournalEntry)
    assert entry.request_id == "gr-000000000001/plan" and entry.attempt == 1
    assert (entry.root_run_id, entry.run_path, entry.node_id, entry.lane) == (
        ROOT, ROOT, "plan", "codex")
    assert entry.phase == "queued"
    assert entry.reserved_usd == 0.5
    assert entry.worker_id is None and entry.outcome is None and entry.charged_usd is None
    assert entry.created_at <= entry.updated_at
    assert jr.get("gr-000000000001/plan", 1).phase == "queued"


def test_full_legal_chain_records_each_phases_fields(tmp_path):
    store, jr = journal(tmp_path)
    walk(store, jr, upto="dispatched")
    entry = jr.get("gr-000000000001/plan", 1)
    assert entry.phase == "dispatched"
    assert (entry.worker_id, entry.agent_id, entry.run_id) == ("w-1-abc123", "ag-1", "run-1")
    adv(store, jr, "gr-000000000001/plan", 1, "finished",
        outcome={"status": "completed", "cost_usd": 0.2})
    entry = jr.get("gr-000000000001/plan", 1)
    assert entry.phase == "finished"
    assert entry.outcome == {"status": "completed", "cost_usd": 0.2}
    adv(store, jr, "gr-000000000001/plan", 1, "settled", charged_usd=0.2, cost_source="actual")
    entry = jr.get("gr-000000000001/plan", 1)
    assert entry.phase == "settled"
    assert entry.charged_usd == 0.2 and entry.cost_source == "actual"
    assert entry.outcome == {"status": "completed", "cost_usd": 0.2}  # kept through settle
    assert entry.worker_id == "w-1-abc123"


@pytest.mark.parametrize("path", [
    ("queued", "admitted", "released"),
    ("queued", "admitted", "dispatched", "interrupted"),
])
def test_other_legal_terminals(tmp_path, path):
    store, jr = journal(tmp_path)
    fields = {"queued": dict(BASE),
              "dispatched": dict(worker_id="w-1-abc123", agent_id=None, run_id=None),
              "interrupted": dict(charged_usd=0.5, cost_source="reservation")}
    for phase in path:
        adv(store, jr, "r/x", 1, phase, **fields.get(phase, {}))
    assert jr.get("r/x", 1).phase == path[-1]


@pytest.mark.parametrize("start,target", [
    ("queued", "dispatched"),
    ("queued", "finished"),
    ("queued", "settled"),
    ("admitted", "finished"),
    ("admitted", "settled"),
    ("dispatched", "settled"),
    ("dispatched", "admitted"),
    ("finished", "dispatched"),
    ("finished", "queued"),
    ("settled", "dispatched"),
    ("settled", "finished"),
    ("settled", "queued"),
    ("settled", "interrupted"),
    ("interrupted", "dispatched"),
    ("interrupted", "settled"),
    ("released", "admitted"),
    ("released", "dispatched"),
])
def test_illegal_transitions_raise_with_message(tmp_path, start, target):
    store, jr = journal(tmp_path)
    chains = {
        "queued": ["queued"],
        "admitted": ["queued", "admitted"],
        "dispatched": ["queued", "admitted", "dispatched"],
        "finished": ["queued", "admitted", "dispatched", "finished"],
        "settled": ["queued", "admitted", "dispatched", "finished", "settled"],
        "interrupted": ["queued", "admitted", "dispatched", "interrupted"],
        "released": ["queued", "admitted", "released"],
    }
    fields = {"queued": dict(BASE), "dispatched": dict(worker_id="w-1-abc123"),
              "finished": dict(outcome={"status": "completed"}),
              "settled": dict(charged_usd=0.1, cost_source="actual"),
              "interrupted": dict(charged_usd=0.5, cost_source="reservation")}
    for phase in chains[start]:
        adv(store, jr, "r/x", 1, phase, **fields.get(phase, {}))
    with pytest.raises(JournalError) as info:
        adv(store, jr, "r/x", 1, target, **fields.get(target, {}))
    assert str(info.value) == f"illegal transition {start} -> {target}"
    assert jr.get("r/x", 1).phase == start  # the refused move changed nothing


def test_journal_error_is_a_runtime_error():
    assert issubclass(JournalError, RuntimeError)


def test_a_failed_transaction_leaves_the_phase_unchanged(tmp_path):
    store, jr = journal(tmp_path)
    adv(store, jr, "r/x", 1, "queued", **BASE)
    with pytest.raises(RuntimeError), store.txn() as conn:
        jr.advance(conn, "r/x", 1, "admitted")
        raise RuntimeError("boom")
    assert jr.get("r/x", 1).phase == "queued"


def test_attempts_lists_in_attempt_order(tmp_path):
    store, jr = journal(tmp_path)
    for attempt in (2, 1, 3):
        adv(store, jr, "r/x", attempt, "queued", **BASE)
    assert [e.attempt for e in jr.attempts("r/x")] == [1, 2, 3]
    assert jr.get("r/x", 2).attempt == 2


def test_attempts_are_independent_rows(tmp_path):
    store, jr = journal(tmp_path)
    adv(store, jr, "r/x", 1, "queued", **BASE)
    adv(store, jr, "r/x", 1, "admitted")
    adv(store, jr, "r/x", 2, "queued", **BASE)
    assert jr.get("r/x", 1).phase == "admitted"
    assert jr.get("r/x", 2).phase == "queued"


def test_for_root_returns_all_entries_of_that_root_only(tmp_path):
    store, jr = journal(tmp_path)
    other = dict(BASE, root_run_id="gr-000000000002", run_path="gr-000000000002")
    adv(store, jr, "a/1", 1, "queued", **BASE)
    adv(store, jr, "a/2", 1, "queued", **BASE)
    adv(store, jr, "b/1", 1, "queued", **other)
    mine = jr.for_root(ROOT)
    assert sorted(e.request_id for e in mine) == ["a/1", "a/2"]
    assert [e.request_id for e in jr.for_root("gr-000000000002")] == ["b/1"]


def test_advance_is_visible_across_store_instances(tmp_path):
    store, jr = journal(tmp_path)
    adv(store, jr, "r/x", 1, "queued", **BASE)
    other = Journal(make_store(tmp_path))
    assert other.get("r/x", 1).phase == "queued"
