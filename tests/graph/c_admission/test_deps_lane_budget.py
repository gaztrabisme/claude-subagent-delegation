"""deps, closed lanes, budget refusal, cancel (SPEC 5.2 steps 2-7)."""

from __future__ import annotations


def test_unsettled_dependency_keeps_the_entry_queued(stack_factory):
    st = stack_factory(caps={"a": 4})
    root = st.root("gr-dep")
    st.enq("gr-dep/first", root_path=root)
    st.enq("gr-dep/second", root_path=root, deps=["gr-dep/first"])

    held = st.acquire("gr-dep/first")
    assert held.status == "granted"  # admitted and running, not settled
    res = st.acquire("gr-dep/second")
    assert (res.status, res.reason) == ("queued", "deps")


def test_dependency_settled_completed_makes_the_entry_eligible(stack_factory):
    st = stack_factory(caps={"a": 4})
    root = st.root("gr-dep2")
    st.enq("gr-dep2/first", root_path=root)
    st.enq("gr-dep2/second", root_path=root, deps=["gr-dep2/first"])
    held = st.acquire("gr-dep2/first")
    st.settle("gr-dep2/first", held.lease, status="completed")

    res = st.acquire("gr-dep2/second")
    assert res.status == "granted"


def test_dependency_settled_but_not_completed_does_not_release(stack_factory):
    st = stack_factory(caps={"a": 4})
    root = st.root("gr-dep3")
    st.enq("gr-dep3/first", root_path=root)
    st.enq("gr-dep3/second", root_path=root, deps=["gr-dep3/first"])
    held = st.acquire("gr-dep3/first")
    st.settle("gr-dep3/first", held.lease, status="failed")

    res = st.acquire("gr-dep3/second")
    assert (res.status, res.reason) == ("queued", "deps")


def test_closed_lane_queues_then_times_out(stack_factory):
    st = stack_factory(caps={"a": 1}, lane_closed_timeout_s=300.0)
    root = st.root("gr-closed")
    st.enq("gr-closed/x", root_path=root)
    st.closed_lanes.add("a")

    res = st.acquire("gr-closed/x")
    assert (res.status, res.reason) == ("queued", "lane_closed")
    st.clock.advance(299)
    assert st.acquire("gr-closed/x").reason == "lane_closed"

    st.clock.advance(2)  # 301 s since enqueue
    res = st.acquire("gr-closed/x")
    assert (res.status, res.reason) == ("refused", "lane_closed_timeout")
    # The entry is gone: it neither blocks the lane nor can be granted later.
    assert st.admission.queue("a") == []
    st.closed_lanes.clear()
    assert st.acquire("gr-closed/x").reason == "cancelled"


def test_lane_reopening_before_the_timeout_grants(stack_factory):
    st = stack_factory(caps={"a": 1}, lane_closed_timeout_s=300.0)
    root = st.root("gr-reopen")
    st.enq("gr-reopen/x", root_path=root)
    st.closed_lanes.add("a")
    assert st.acquire("gr-reopen/x").reason == "lane_closed"
    st.clock.advance(100)
    st.closed_lanes.clear()
    assert st.acquire("gr-reopen/x").status == "granted"


def test_budget_refusal_removes_the_entry_and_grants_nothing(stack_factory):
    st = stack_factory(caps={"a": 2})
    root = st.root("gr-bud", usd=0.3)
    st.enq("gr-bud/big", root_path=root, reserve=0.5)

    res = st.acquire("gr-bud/big")
    assert (res.status, res.reason) == ("refused", "budget_exhausted")
    assert res.lease is None
    assert st.admission.active("a") == 0
    assert st.admission.queue("a") == []
    assert st.ledger.account(root).reserved_usd == 0
    assert st.acquire("gr-bud/big").reason == "cancelled"


def test_missing_and_cancelled_entries_are_refused_cancelled(stack_factory):
    st = stack_factory(caps={"a": 1})
    root = st.root("gr-can")
    res = st.acquire("gr-can/never-enqueued")
    assert (res.status, res.reason) == ("refused", "cancelled")

    st.enq("gr-can/x", root_path=root)
    st.admission.cancel("gr-can/x", 1)
    res = st.acquire("gr-can/x")
    assert (res.status, res.reason) == ("refused", "cancelled")
    assert st.admission.queue("a") == []


def test_enqueue_is_idempotent_before_the_grant(stack_factory):
    """The admit activity is retried: enqueueing the same attempt twice must
    leave one queue entry and one reservation."""
    st = stack_factory(caps={"a": 1})
    root = st.root("gr-idem")
    st.enq("gr-idem/x", root_path=root, reserve=0.5)
    st.enq("gr-idem/x", root_path=root, reserve=0.5)
    assert len(st.admission.queue("a")) == 1
    assert st.acquire("gr-idem/x").status == "granted"
    assert st.admission.active("a") == 1
    assert st.ledger.account(root).reserved_usd == 0.5
