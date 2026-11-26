"""reconcile: interrupted attempts, live and dead roots, idempotency (SPEC 5.3)."""

from __future__ import annotations

import os

import pytest


def _dispatch_under_dead_worker(st, rid, root, dead_pid, *, reserve=0.5):
    wid = st.workers.register(dead_pid)
    st.enq(rid, root_path=root, reserve=reserve)
    lease = st.acquire(rid).lease
    assert lease is not None
    st.dispatch(rid, wid)
    st.admission.renew(lease.lease_id, holder=wid)
    st.clock.advance(300)  # lease and worker beat both long expired
    return lease


def test_dispatched_row_of_a_dead_worker_is_interrupted_and_charged(stack_factory, dead_process_pid):
    st = stack_factory(caps={"a": 1})
    root = st.root("gr-rec1")
    _dispatch_under_dead_worker(st, "gr-rec1/t1", root, dead_process_pid, reserve=0.5)

    report = st.admission.reconcile(root_live=lambda r: True)

    assert report.interrupted == ["gr-rec1/t1#a1"]
    assert report.released_leases >= 1
    row = st.journal.get("gr-rec1/t1", 1)
    assert row.phase == "interrupted"
    assert row.charged_usd == pytest.approx(0.5)
    assert row.cost_source == "reservation"
    assert st.admission.active("a") == 0
    account = st.ledger.account(root)
    assert account.reserved_usd == 0
    assert account.spent_usd == pytest.approx(0.5)
    assert "gr-rec1" in report.recovered


def test_reconcile_is_idempotent(stack_factory, dead_process_pid):
    st = stack_factory(caps={"a": 1})
    root = st.root("gr-rec2")
    _dispatch_under_dead_worker(st, "gr-rec2/t1", root, dead_process_pid)
    st.admission.reconcile(root_live=lambda r: True)
    again = st.admission.reconcile(root_live=lambda r: True)
    assert again.interrupted == []
    assert again.dead == []
    assert st.ledger.account(root).spent_usd == pytest.approx(0.5)
    assert st.journal.get("gr-rec2/t1", 1).phase == "interrupted"


def test_dispatched_row_of_a_live_worker_is_left_alone(stack_factory):
    st = stack_factory(caps={"a": 1})
    root = st.root("gr-rec3")
    wid = st.workers.register(os.getpid())
    st.enq("gr-rec3/t1", root_path=root)
    lease = st.acquire("gr-rec3/t1").lease
    st.dispatch("gr-rec3/t1", wid)
    st.admission.renew(lease.lease_id, holder=wid)

    report = st.admission.reconcile(root_live=lambda r: True)
    assert report.interrupted == []
    assert st.journal.get("gr-rec3/t1", 1).phase == "dispatched"
    assert st.admission.active("a") == 1


def test_root_live_true_is_recovered_and_untouched(stack_factory):
    st = stack_factory(caps={"a": 1})
    root = st.root("gr-live")
    report = st.admission.reconcile(root_live=lambda r: True)
    assert "gr-live" in report.recovered
    assert report.dead == []
    assert st.ledger.account(root).state == "open"


def test_root_not_live_is_marked_dead_with_everything_under_it(stack_factory):
    from subagent.graph.exceptions import ExceptionQueue

    st = stack_factory(caps={"a": 1})
    root = st.root("gr-dead")
    other = st.root("gr-alive")
    child = st.ledger.open_child(parent_path=root, run_path=f"{root}/sub", node_id="sub",
                                 requested_usd=5.0)
    st.enq("gr-dead/running", root_path=root, reserve=0.5)
    st.enq("gr-dead/queued", root_path=root, reserve=0.5)
    assert st.acquire("gr-dead/running").status == "granted"  # reservation never settled
    assert st.acquire("gr-dead/queued").reason == "lane_full"

    queue = ExceptionQueue(st.store)
    pending = queue.raise_(key="gr-dead/ask:owner_decision", root_run_id="gr-dead",
                           run_path=root, node_id="ask", kind="owner_decision",
                           summary="Merge?", options=["merge", "hold"])

    report = st.admission.reconcile(root_live=lambda r: r != "gr-dead")

    assert report.dead == ["gr-dead"]
    assert "gr-alive" in report.recovered
    assert st.ledger.account(root).state == "dead"
    assert st.ledger.account(child.run_path).state == "dead"
    assert st.ledger.account(other).state == "open"
    # Unsettled reservation charged at reservation; queued entry cancelled.
    assert st.ledger.account(root).reserved_usd == 0
    assert st.ledger.account(root).spent_usd == pytest.approx(0.5)
    assert st.acquire("gr-dead/queued").reason == "cancelled"
    # Exceptions: the open one is closed run_dead; one informational run_dead is raised.
    closed = queue.get(pending.exception_id)
    assert (closed.state, closed.closed_reason) == ("closed", "run_dead")
    dead_records = queue.list(state="all", root_run_id="gr-dead", kind="run_dead")
    assert len(dead_records) == 1
    assert dead_records[0].needs_answer is False
    assert dead_records[0].state == "open"

    again = st.admission.reconcile(root_live=lambda r: r != "gr-dead")
    assert again.dead == []
    assert len(queue.list(state="all", root_run_id="gr-dead", kind="run_dead")) == 1


def test_root_live_none_leaves_everything_untouched(stack_factory):
    st = stack_factory(caps={"a": 1})
    root = st.root("gr-unk")
    st.enq("gr-unk/held", root_path=root)
    st.enq("gr-unk/waiting", root_path=root)
    st.acquire("gr-unk/held")

    report = st.admission.reconcile(root_live=lambda r: None)

    assert report.dead == []
    assert report.recovered == []
    assert st.ledger.account(root).state == "open"
    assert st.ledger.account(root).spent_usd == 0
    assert st.acquire("gr-unk/waiting").reason == "lane_full"
