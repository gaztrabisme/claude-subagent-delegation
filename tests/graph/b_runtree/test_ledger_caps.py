"""Spec 4: pool, root and child caps, the min rule, depth limit, idempotent opens."""

from __future__ import annotations

import pytest

from subagent.graph import ids
from subagent.graph.ledger import Account, BudgetRefused

from ._helpers import make_ledger, open_root, reserve, settle

ROOT = "gr-000000000001"


def child(ledger, parent, node, requested=None):
    return ledger.open_child(parent_path=parent, run_path=ids.child_run_path(parent, node),
                             node_id=node, requested_usd=requested)


def test_ensure_pool_creates_pool_account_at_depth_minus_one(tmp_path):
    _, ledger = make_ledger(tmp_path, pool=None)
    pool = ledger.ensure_pool("default", 10.0)
    assert isinstance(pool, Account)
    assert pool.run_path == "pool:default"
    assert pool.depth == -1
    assert pool.cap_usd == 10.0
    assert pool.remaining_usd == 10.0
    assert pool.state == "open"


def test_ensure_pool_resets_cap_from_config_each_time(tmp_path):
    _, ledger = make_ledger(tmp_path, pool=None)
    ledger.ensure_pool("default", 10.0)
    assert ledger.ensure_pool("default", 25.0).cap_usd == 25.0
    assert ledger.account("pool:default").cap_usd == 25.0


def test_open_root_without_request_takes_pool_remaining(tmp_path):
    _, ledger = make_ledger(tmp_path)
    root = open_root(ledger)
    assert root.run_path == ROOT and root.root_run_id == ROOT
    assert root.depth == 0 and root.parent_path == "pool:default"
    assert root.cap_usd == 10.0
    assert root.capped is False
    assert ledger.account("pool:default").held_usd == 10.0
    assert ledger.account("pool:default").remaining_usd == 0.0


def test_open_root_request_below_remaining_is_not_capped(tmp_path):
    _, ledger = make_ledger(tmp_path)
    root = open_root(ledger, requested=4.0)
    assert root.cap_usd == 4.0 and root.capped is False
    assert ledger.account("pool:default").held_usd == 4.0
    assert ledger.account("pool:default").remaining_usd == 6.0


def test_open_root_request_above_remaining_is_capped_to_remaining(tmp_path):
    _, ledger = make_ledger(tmp_path)
    open_root(ledger, "gr-000000000001", requested=7.0)
    second = open_root(ledger, "gr-000000000002", requested=20.0)
    assert second.cap_usd == 3.0
    assert second.capped is True


def test_open_root_pool_exhausted_is_refused(tmp_path):
    _, ledger = make_ledger(tmp_path)
    open_root(ledger, "gr-000000000001")  # takes the whole pool
    with pytest.raises(BudgetRefused) as info:
        open_root(ledger, "gr-000000000002", requested=1.0)
    assert info.value.code == "budget_exhausted"


def test_open_root_unknown_pool_refused(tmp_path):
    _, ledger = make_ledger(tmp_path)
    with pytest.raises(BudgetRefused) as info:
        open_root(ledger, pool="nope", requested=1.0)
    assert info.value.code == "pool_unknown"


def test_open_root_is_idempotent_no_second_hold(tmp_path):
    _, ledger = make_ledger(tmp_path)
    first = open_root(ledger, requested=4.0)
    again = open_root(ledger, requested=4.0)
    assert again.run_path == first.run_path and again.cap_usd == 4.0
    assert ledger.account("pool:default").held_usd == 4.0
    again_other = open_root(ledger, requested=9.0)  # a different request changes nothing
    assert again_other.cap_usd == 4.0
    assert ledger.account("pool:default").held_usd == 4.0


def test_child_cap_is_min_of_request_and_parent_remaining(tmp_path):
    _, ledger = make_ledger(tmp_path)
    open_root(ledger, requested=10.0)
    first = child(ledger, ROOT, "a", requested=6.0)
    assert first.cap_usd == 6.0 and first.depth == 1 and first.parent_path == ROOT
    assert ledger.account(ROOT).held_usd == 6.0
    second = child(ledger, ROOT, "b", requested=100.0)
    assert second.cap_usd == 4.0
    assert ledger.account(ROOT).remaining_usd == 0.0


def test_child_without_request_takes_parent_remaining(tmp_path):
    _, ledger = make_ledger(tmp_path)
    open_root(ledger, requested=10.0)
    one = child(ledger, ROOT, "a", requested=3.0)
    two = child(ledger, ROOT, "b")
    assert one.cap_usd == 3.0
    assert two.cap_usd == 7.0


def test_min_rule_holds_at_every_level_three_deep(tmp_path):
    store, ledger = make_ledger(tmp_path)
    open_root(ledger, requested=10.0)
    l1 = child(ledger, ROOT, "a", requested=8.0)
    l2 = child(ledger, l1.run_path, "b", requested=20.0)
    l3 = child(ledger, l2.run_path, "c", requested=50.0)
    assert (l1.cap_usd, l2.cap_usd, l3.cap_usd) == (8.0, 8.0, 8.0)
    assert (l1.depth, l2.depth, l3.depth) == (1, 2, 3)
    assert ledger.account(l2.run_path).held_usd == 8.0
    assert ledger.account(l1.run_path).remaining_usd == 0.0
    # a child can never exceed its parent's remaining at creation, at any level
    reserve(store, ledger, l3.run_path, "r1", 5.0)
    with pytest.raises(BudgetRefused) as info:
        child(ledger, l2.run_path, "late", requested=1.0)
    assert info.value.code == "budget_exhausted"


def test_depth_limit_at_max_depth_plus_one(tmp_path):
    _, ledger = make_ledger(tmp_path, max_depth=3)
    open_root(ledger, requested=10.0)
    path = ROOT
    for level in range(1, 4):
        path = child(ledger, path, f"n{level}", requested=5.0).run_path
    with pytest.raises(BudgetRefused) as info:
        child(ledger, path, "n4", requested=1.0)
    assert info.value.code == "depth_limit"


def test_depth_limit_respects_ledger_max_depth(tmp_path):
    _, ledger = make_ledger(tmp_path, max_depth=1)
    open_root(ledger, requested=10.0)
    child(ledger, ROOT, "a", requested=2.0)
    with pytest.raises(BudgetRefused) as info:
        child(ledger, f"{ROOT}/a", "b", requested=1.0)
    assert info.value.code == "depth_limit"


def test_child_open_is_idempotent_on_run_path(tmp_path):
    _, ledger = make_ledger(tmp_path)
    open_root(ledger, requested=10.0)
    first = child(ledger, ROOT, "a", requested=4.0)
    again = child(ledger, ROOT, "a", requested=4.0)
    assert again.cap_usd == first.cap_usd == 4.0
    assert ledger.account(ROOT).held_usd == 4.0


def test_child_of_exhausted_parent_is_refused(tmp_path):
    store, ledger = make_ledger(tmp_path)
    open_root(ledger, requested=2.0)
    reserve(store, ledger, ROOT, "r1", 2.0)
    with pytest.raises(BudgetRefused) as info:
        child(ledger, ROOT, "a", requested=1.0)
    assert info.value.code == "budget_exhausted"


def test_remaining_formula_and_account_fields(tmp_path):
    store, ledger = make_ledger(tmp_path)
    open_root(ledger, requested=10.0)
    child(ledger, ROOT, "a", requested=3.0)
    reserve(store, ledger, ROOT, "r1", 2.0)
    settle(store, ledger, "r1", 1.5)
    reserve(store, ledger, ROOT, "r2", 1.0)
    acct = ledger.account(ROOT)
    assert acct.cap_usd == 10.0
    assert acct.held_usd == 3.0
    assert acct.spent_usd == 1.5
    assert acct.reserved_usd == 1.0
    assert acct.remaining_usd == pytest.approx(10.0 - 1.5 - 1.0 - 3.0)
    assert acct.state == "open"


def test_tree_lists_root_first_then_depth_then_created(tmp_path):
    _, ledger = make_ledger(tmp_path)
    open_root(ledger, requested=10.0)
    a = child(ledger, ROOT, "a", requested=3.0)
    b = child(ledger, ROOT, "b", requested=3.0)
    c = child(ledger, a.run_path, "c", requested=1.0)
    tree = ledger.tree(ROOT)
    assert [x.run_path for x in tree] == [ROOT, a.run_path, b.run_path, c.run_path]
