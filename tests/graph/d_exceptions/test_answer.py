"""ExceptionQueue.answer rules and messages (SPEC 6)."""

from __future__ import annotations

import pytest


def _err(excinfo):
    return excinfo.value.code, str(excinfo.value)


def test_answer_records_the_decision(raise_record, queue, clock):
    rec = raise_record("a1", kind="owner_decision", options=("merge", "hold"))
    clock.advance(5)
    answered = queue.answer(rec.exception_id, "merge", note="looks good", answered_by="the owner")
    assert answered.state == "answered"
    assert answered.answer == "merge"
    assert answered.note == "looks good"
    assert answered.answered_by == "the owner"
    assert answered.answered_at == clock()
    assert queue.get(rec.exception_id).answer == "merge"


def test_answered_by_defaults_to_orchestrator(raise_record, queue):
    rec = raise_record("a2")
    assert queue.answer(rec.exception_id, "skip").answered_by == "orchestrator"


def test_unknown_id_is_not_found(queue):
    from subagent.graph.exceptions import ExceptionError

    with pytest.raises(ExceptionError) as err:
        queue.answer("gx-ffffffffffff", "retry")
    assert err.value.code == "not_found"


def test_answer_outside_the_options_is_invalid_with_the_exact_message(raise_record, queue):
    from subagent.graph.exceptions import ExceptionError

    rec = raise_record("a3", options=("retry", "skip", "abort"))
    with pytest.raises(ExceptionError) as err:
        queue.answer(rec.exception_id, "maybe")
    code, message = _err(err)
    assert code == "invalid_answer"
    assert "answer 'maybe' is not one of ['retry', 'skip', 'abort']" in message
    assert queue.get(rec.exception_id).state == "open"


def test_same_answer_again_returns_the_record_unchanged(raise_record, queue, clock):
    rec = raise_record("a4")
    first = queue.answer(rec.exception_id, "retry", note="n1", answered_by="x")
    clock.advance(100)
    second = queue.answer(rec.exception_id, "retry", note="n2", answered_by="y")
    assert second == first
    assert second.note == "n1"
    assert second.answered_by == "x"
    assert second.answered_at == first.answered_at


def test_different_answer_after_answering_is_already_answered(raise_record, queue):
    from subagent.graph.exceptions import ExceptionError

    rec = raise_record("a5")
    queue.answer(rec.exception_id, "retry")
    with pytest.raises(ExceptionError) as err:
        queue.answer(rec.exception_id, "skip")
    assert err.value.code == "already_answered"
    assert queue.get(rec.exception_id).answer == "retry"


def test_delivered_records_keep_the_same_rules(raise_record, queue):
    from subagent.graph.exceptions import ExceptionError

    rec = raise_record("a6")
    queue.answer(rec.exception_id, "abort")
    queue.mark_delivered(rec.exception_id)
    again = queue.answer(rec.exception_id, "abort")
    assert again.state == "delivered"
    assert again.answer == "abort"
    with pytest.raises(ExceptionError) as err:
        queue.answer(rec.exception_id, "retry")
    assert err.value.code == "already_answered"


def test_closed_record_refuses_answers(raise_record, queue):
    from subagent.graph.exceptions import ExceptionError

    rec = raise_record("a7")
    queue.close_for_root("gr-a", "run_dead")
    with pytest.raises(ExceptionError) as err:
        queue.answer(rec.exception_id, "retry")
    assert err.value.code == "closed"


def test_ack_closes_an_informational_record(raise_record, queue):
    info = raise_record("i1", kind="budget_exhausted", options=(), needs_answer=False)
    assert info.state == "open"
    done = queue.answer(info.exception_id, "ack")
    assert done.state == "closed"
    assert queue.list() == []
    assert queue.digest()["open"] == 0


def test_close_for_root_also_closes_informational_records(raise_record, queue):
    info = raise_record("i2", kind="run_dead", options=(), needs_answer=False)
    queue.close_for_root("gr-a", "done")
    assert queue.get(info.exception_id).state == "closed"
