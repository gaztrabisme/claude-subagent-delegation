"""ExceptionQueue: raise_, list, digest, delivery, close_for_root (SPEC 6)."""

from __future__ import annotations

import hashlib
import re

import pytest


def test_kinds_are_the_seven_in_the_spec():
    from subagent.graph.exceptions import KINDS

    assert KINDS == ("owner_decision", "node_failed", "budget_exhausted", "lane_closed",
                     "test_change_request", "guard_denied", "run_dead")


def test_exception_id_format_and_derivation(raise_record):
    from subagent.graph.ids import exception_id

    rec = raise_record("gr-a/n1:node_failed")
    assert re.fullmatch(r"gx-[0-9a-f]{12}", rec.exception_id)
    assert rec.exception_id == "gx-" + hashlib.sha256(b"gr-a/n1:node_failed").hexdigest()[:12]
    assert rec.exception_id == exception_id("gr-a/n1:node_failed")


def test_raise_stores_the_record_open(raise_record, queue, clock):
    rec = raise_record("k1", kind="owner_decision", options=("merge", "hold"), default="hold",
                       detail={"why": "x"}, summary="Merge?")
    assert rec.state == "open"
    assert rec.kind == "owner_decision"
    assert rec.key == "k1"
    assert rec.root_run_id == "gr-a"
    assert rec.options == ["merge", "hold"]
    assert rec.default == "hold"
    assert rec.needs_answer is True
    assert rec.detail == {"why": "x"}
    assert rec.summary == "Merge?"
    assert rec.created_at == clock()
    assert (rec.answer, rec.note, rec.answered_by, rec.answered_at) == (None, None, None, None)
    assert (rec.delivered_at, rec.closed_reason) == (None, None)
    assert queue.get(rec.exception_id) == rec
    assert rec.as_dict()["exception_id"] == rec.exception_id


def test_raise_is_idempotent_on_key(raise_record, queue):
    first = raise_record("same-key")
    second = raise_record("same-key", summary="a retry of the activity")
    assert second.exception_id == first.exception_id
    assert len(queue.list(state="all")) == 1
    # A replay after an answer must not reopen or overwrite it.
    queue.answer(first.exception_id, "skip")
    third = raise_record("same-key")
    assert third.exception_id == first.exception_id
    assert queue.get(first.exception_id).state == "answered"
    assert len(queue.list(state="all")) == 1


def test_get_unknown_id_is_not_found(queue):
    from subagent.graph.exceptions import ExceptionError

    with pytest.raises(ExceptionError) as err:
        queue.get("gx-000000000000")
    assert err.value.code == "not_found"


def test_list_defaults_to_open_oldest_first(raise_record, queue, clock):
    ids = []
    for i in range(3):
        ids.append(raise_record(f"k{i}").exception_id)
        clock.advance(10)
    queue.answer(ids[1], "skip")
    assert [r.exception_id for r in queue.list()] == [ids[0], ids[2]]
    assert [r.exception_id for r in queue.list(state="open")] == [ids[0], ids[2]]
    assert [r.exception_id for r in queue.list(state="answered")] == [ids[1]]
    assert [r.exception_id for r in queue.list(state="all")] == ids


def test_list_filters_by_root_kind_and_limit(raise_record, queue, clock):
    raise_record("a1", root="gr-a", kind="node_failed")
    clock.advance(1)
    raise_record("a2", root="gr-a", kind="owner_decision", options=("go", "stop"))
    clock.advance(1)
    raise_record("b1", root="gr-b", kind="node_failed")
    clock.advance(1)
    assert {r.key for r in queue.list(root_run_id="gr-a")} == {"a1", "a2"}
    assert {r.key for r in queue.list(kind="node_failed")} == {"a1", "b1"}
    assert [r.key for r in queue.list(root_run_id="gr-a", kind="node_failed")] == ["a1"]
    assert [r.key for r in queue.list(limit=2)] == ["a1", "a2"]
    assert queue.list(root_run_id="gr-nobody") == []


def test_list_state_delivered_and_closed(raise_record, queue):
    one = raise_record("d1")
    two = raise_record("d2")
    queue.answer(one.exception_id, "retry")
    queue.mark_delivered(one.exception_id)
    queue.close_for_root("gr-a", "test")  # closes what is still open (two)
    assert [r.exception_id for r in queue.list(state="delivered")] == [one.exception_id]
    assert [r.exception_id for r in queue.list(state="closed")] == [two.exception_id]


def test_digest_shape_when_empty(queue):
    digest = queue.digest()
    assert digest["open"] == 0
    assert digest["needs_answer"] == 0
    assert digest["by_kind"] == {}
    assert digest["roots"] == []
    assert digest["oldest_age_s"] is None
    assert set(digest) == {"open", "needs_answer", "by_kind", "roots", "oldest_age_s"}


def test_digest_counts_open_records(raise_record, queue, clock):
    raise_record("g1", root="gr-a", kind="node_failed")
    clock.advance(30)
    raise_record("g2", root="gr-a", kind="owner_decision", options=("go", "stop"))
    raise_record("g3", root="gr-a", kind="budget_exhausted", options=(), needs_answer=False)
    raise_record("g4", root="gr-b", kind="node_failed")
    clock.advance(12)

    digest = queue.digest()
    assert digest["open"] == 4
    assert digest["needs_answer"] == 3
    assert digest["by_kind"] == {"node_failed": 2, "owner_decision": 1, "budget_exhausted": 1}
    assert digest["oldest_age_s"] == pytest.approx(42.0)
    assert len(digest["roots"]) == 2
    assert "gr-a" in str(digest["roots"]) and "gr-b" in str(digest["roots"])

    queue.answer(raise_record("g1").exception_id, "retry")
    assert queue.digest()["open"] == 3


def test_undelivered_lists_answered_records_until_marked(raise_record, queue):
    one = raise_record("u1")
    two = raise_record("u2")
    assert queue.undelivered() == []  # open records have no answer to deliver
    queue.answer(one.exception_id, "retry")
    queue.answer(two.exception_id, "skip")
    assert {r.exception_id for r in queue.undelivered()} == {one.exception_id, two.exception_id}
    queue.mark_delivered(one.exception_id)
    assert [r.exception_id for r in queue.undelivered()] == [two.exception_id]
    delivered = queue.get(one.exception_id)
    assert delivered.state == "delivered"
    assert delivered.delivered_at is not None


def test_close_for_root_closes_only_that_roots_open_records(raise_record, queue):
    a1 = raise_record("c1", root="gr-a")
    a2 = raise_record("c2", root="gr-a")
    b1 = raise_record("c3", root="gr-b")
    assert queue.close_for_root("gr-a", "run_dead") == 2
    for rec in (a1, a2):
        closed = queue.get(rec.exception_id)
        assert closed.state == "closed"
        assert closed.closed_reason == "run_dead"
    assert queue.get(b1.exception_id).state == "open"
    assert queue.close_for_root("gr-a", "run_dead") == 0
    assert queue.close_for_root("gr-nobody", "x") == 0
