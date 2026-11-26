"""Lane caps, priority, fairness, aging and queue positions (SPEC 5.2, 12.2 c)."""

from __future__ import annotations


def test_cap_per_lane_limits_concurrent_leases(stack_factory):
    st = stack_factory(caps={"a": 3, "b": 2})
    root = st.root("gr-caps")
    for i in range(1, 5):
        st.enq(f"gr-caps/a{i}", root_path=root, lane="a")
    for i in range(1, 4):
        st.enq(f"gr-caps/b{i}", root_path=root, lane="b")

    a = [st.acquire(f"gr-caps/a{i}") for i in range(1, 5)]
    b = [st.acquire(f"gr-caps/b{i}") for i in range(1, 4)]

    assert [r.status for r in a] == ["granted", "granted", "granted", "queued"]
    assert a[3].reason == "lane_full"
    assert [r.status for r in b] == ["granted", "granted", "queued"]
    assert b[2].reason == "lane_full"
    assert all(r.lease is not None for r in a[:3] + b[:2])
    assert st.admission.active("a") == 3
    assert st.admission.active("b") == 2


def test_release_lets_the_head_of_line_in(stack_factory):
    st = stack_factory(caps={"a": 1})
    root = st.root("gr-rel")
    for i in range(1, 4):
        st.enq(f"gr-rel/e{i}", root_path=root)
    first = st.acquire("gr-rel/e1")
    assert first.status == "granted"
    second = st.acquire("gr-rel/e2")
    third = st.acquire("gr-rel/e3")
    assert (second.status, second.reason) == ("queued", "lane_full")
    assert (third.status, third.reason) == ("queued", "behind")

    st.admission.release(first.lease.lease_id)
    assert st.admission.active("a") == 0
    # e3 is still behind e2; e2 is the head and gets the slot.
    assert st.acquire("gr-rel/e3").reason == "behind"
    assert st.acquire("gr-rel/e2").status == "granted"


def test_unknown_lane_is_refused_without_a_lease(stack_factory):
    st = stack_factory(caps={"a": 1})
    root = st.root("gr-unk")
    st.enq("gr-unk/x", root_path=root, lane="nolane")
    res = st.acquire("gr-unk/x")
    assert (res.status, res.reason) == ("refused", "lane_unknown")
    assert res.lease is None


def test_priority_one_goes_before_priority_three(stack_factory):
    st = stack_factory(caps={"a": 1})
    root = st.root("gr-prio")
    st.enq("gr-prio/lo", root_path=root, priority=3)
    st.enq("gr-prio/hi", root_path=root, priority=1)

    lo = st.acquire("gr-prio/lo")
    assert (lo.status, lo.reason, lo.position) == ("queued", "behind", 2)
    hi = st.acquire("gr-prio/hi")
    assert hi.status == "granted"
    st.admission.release(hi.lease.lease_id)
    assert st.acquire("gr-prio/lo").status == "granted"


def test_grant_order_follows_priority_numbers(stack_factory):
    st = stack_factory(caps={"a": 1})
    root = st.root("gr-order")
    plan = {"p5": 5, "p3": 3, "p1": 1, "p2": 2, "p4": 4}
    for name, prio in plan.items():
        st.enq(f"gr-order/{name}", root_path=root, priority=prio)
    order = st.drain([f"gr-order/{n}" for n in plan])
    assert order == [f"gr-order/p{n}" for n in (1, 2, 3, 4, 5)]


def test_two_fairness_keys_alternate_at_equal_priority(stack_factory):
    st = stack_factory(caps={"a": 1})
    ra = st.root("gr-fa")
    rb = st.root("gr-fb")
    # Root A enqueues all its work first; fairness must still interleave B.
    for i in range(1, 4):
        st.enq(f"gr-fa/t{i}", root_path=ra, key="gr-fa")
    for i in range(1, 4):
        st.enq(f"gr-fb/t{i}", root_path=rb, key="gr-fb")
    pending = [f"gr-fa/t{i}" for i in (1, 2, 3)] + [f"gr-fb/t{i}" for i in (1, 2, 3)]
    order = st.drain(pending)
    assert [o.split("/")[0] for o in order] == ["gr-fa", "gr-fb", "gr-fa", "gr-fb", "gr-fa", "gr-fb"]


def test_fifo_within_one_fairness_key(stack_factory):
    st = stack_factory(caps={"a": 1})
    root = st.root("gr-fifo")
    names = [f"gr-fifo/t{i}" for i in range(1, 6)]
    for n in names:
        st.enq(n, root_path=root)
    assert st.drain(names) == names


def test_aging_lifts_priority_five_after_four_aging_periods(stack_factory):
    st = stack_factory(caps={"a": 1, "b": 1}, aging_s=100.0)
    ra = st.root("gr-age-a")
    rb = st.root("gr-age-b")
    st.enq("gr-age-a/old", root_path=ra, lane="a", priority=5, key="old-a")
    st.enq("gr-age-b/old", root_path=rb, lane="b", priority=5, key="old-b")

    st.clock.advance(399)  # floor(399/100) = 3 -> effective priority 2
    st.enq("gr-age-a/new", root_path=ra, lane="a", priority=1, key="new-a")
    res = st.acquire("gr-age-a/old")
    assert (res.status, res.reason, res.position) == ("queued", "behind", 2)

    st.clock.advance(1)  # floor(400/100) = 4 -> effective priority 1, enqueued first
    st.enq("gr-age-b/new", root_path=rb, lane="b", priority=1, key="new-b")
    assert st.acquire("gr-age-b/new").reason == "behind"
    assert st.acquire("gr-age-b/old").status == "granted"


def test_positions_are_one_based_in_queue_order(stack_factory):
    st = stack_factory(caps={"a": 1})
    root = st.root("gr-pos")
    for i in range(0, 4):
        st.enq(f"gr-pos/e{i}", root_path=root)
    holder = st.acquire("gr-pos/e0")
    assert holder.status == "granted"

    r3 = st.acquire("gr-pos/e3")
    r2 = st.acquire("gr-pos/e2")
    r1 = st.acquire("gr-pos/e1")
    assert (r3.reason, r3.position) == ("behind", 3)
    assert (r2.reason, r2.position) == ("behind", 2)
    assert r1.reason == "lane_full"
    listing = st.admission.queue("a")
    assert len(listing) == 3
    assert len(st.admission.queue()) == 3
    assert st.admission.queue("b") == []
