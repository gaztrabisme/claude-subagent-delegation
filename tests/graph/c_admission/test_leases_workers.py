"""Lease expiry and renewal, worker liveness (SPEC 5.2, 5.3)."""

from __future__ import annotations

import os
import re

import pytest


def test_lease_without_renew_expires_at_the_next_acquire(stack_factory):
    from subagent.graph.admission import LeaseLost

    st = stack_factory(caps={"a": 1}, grant_ttl_s=120.0)
    root = st.root("gr-exp")
    st.enq("gr-exp/e1", root_path=root, reserve=0.5)
    st.enq("gr-exp/e2", root_path=root, reserve=0.5)
    first = st.acquire("gr-exp/e1")
    assert first.status == "granted"
    assert first.lease.expires_at == pytest.approx(st.clock() + 120.0)
    assert st.acquire("gr-exp/e2").reason == "lane_full"

    st.clock.advance(121)
    second = st.acquire("gr-exp/e2")
    assert second.status == "granted"
    assert st.journal.get("gr-exp/e1", 1).phase == "released"
    # Only e2's reservation is left on the account.
    assert st.ledger.account(root).reserved_usd == pytest.approx(0.5)
    with pytest.raises(LeaseLost):
        st.admission.renew(first.lease.lease_id, holder="w-1")


def test_renew_sets_holder_and_extends_by_lease_ttl(stack_factory):
    st = stack_factory(caps={"a": 1}, grant_ttl_s=120.0, lease_ttl_s=60.0)
    root = st.root("gr-ren")
    st.enq("gr-ren/e1", root_path=root)
    st.enq("gr-ren/e2", root_path=root)
    first = st.acquire("gr-ren/e1")
    t0 = st.clock()

    st.clock.advance(100)
    renewed = st.admission.renew(first.lease.lease_id, holder="w-1")
    assert renewed.holder == "w-1"
    assert renewed.expires_at == pytest.approx(t0 + 100 + 60)

    st.clock.advance(50)  # t0+150: past the grant expiry, inside the renewal
    assert st.acquire("gr-ren/e2").reason == "lane_full"
    st.clock.advance(20)  # t0+170: renewal over
    assert st.acquire("gr-ren/e2").status == "granted"


def test_renew_after_release_raises_lease_lost(stack_factory):
    from subagent.graph.admission import LeaseLost

    st = stack_factory(caps={"a": 1})
    root = st.root("gr-rl")
    st.enq("gr-rl/e1", root_path=root)
    lease = st.acquire("gr-rl/e1").lease
    st.admission.release(lease.lease_id)
    with pytest.raises(LeaseLost):
        st.admission.renew(lease.lease_id, holder="w-1")
    st.admission.release(lease.lease_id)  # releasing twice is harmless


def test_expiry_leaves_dispatched_rows_to_run_task_or_reconcile(stack_factory):
    st = stack_factory(caps={"a": 1}, grant_ttl_s=120.0)
    root = st.root("gr-disp")
    st.enq("gr-disp/e1", root_path=root)
    st.enq("gr-disp/e2", root_path=root)
    wid = st.workers.register(os.getpid())
    lease = st.acquire("gr-disp/e1").lease
    st.dispatch("gr-disp/e1", wid)
    assert lease is not None

    st.clock.advance(500)
    st.acquire("gr-disp/e2")
    assert st.journal.get("gr-disp/e1", 1).phase == "dispatched"


def test_released_never_dispatched_attempt_returns_its_reservation(stack_factory):
    st = stack_factory(caps={"a": 1}, grant_ttl_s=120.0)
    root = st.root("gr-rr")
    st.enq("gr-rr/e1", root_path=root, reserve=0.7)
    st.acquire("gr-rr/e1")
    assert st.ledger.account(root).reserved_usd == pytest.approx(0.7)
    st.clock.advance(121)
    st.enq("gr-rr/e2", root_path=root, reserve=0.1)
    st.acquire("gr-rr/e2")
    assert st.ledger.account(root).reserved_usd == pytest.approx(0.1)


# -- workers -----------------------------------------------------------------


def test_worker_register_id_format_and_listing(stack_factory):
    st = stack_factory()
    wid = st.workers.register(os.getpid())
    assert re.fullmatch(rf"w-{os.getpid()}-[0-9a-f]{{6}}", wid)
    rows = st.workers.list()
    assert any(r["worker_id"] == wid and r["pid"] == os.getpid() for r in rows)
    other = st.workers.register(os.getpid())
    assert other != wid


def test_worker_alive_until_the_beat_is_stale(stack_factory):
    st = stack_factory()
    wid = st.workers.register(os.getpid())
    assert st.workers.alive(wid) is True
    st.clock.advance(19)
    assert st.workers.alive(wid) is True
    st.clock.advance(2)  # 21 s since the last beat; default stale_s is 20
    assert st.workers.alive(wid) is False
    st.workers.beat(wid)
    assert st.workers.alive(wid) is True
    st.clock.advance(6)
    assert st.workers.alive(wid, stale_s=5.0) is False
    assert st.workers.alive(wid, stale_s=30.0) is True


def test_worker_with_a_dead_pid_is_not_alive_even_with_a_fresh_beat(stack_factory, dead_process_pid):
    st = stack_factory()
    wid = st.workers.register(dead_process_pid)
    st.workers.beat(wid)
    assert st.workers.alive(wid) is False


def test_unknown_worker_is_not_alive(stack_factory):
    st = stack_factory()
    assert st.workers.alive("w-1-abcdef") is False
