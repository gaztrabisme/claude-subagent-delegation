"""Spec 4 rules: reserve, settle, release, close, overrun, micro-dollar exactness."""

from __future__ import annotations

import pytest
from subagent.graph.ledger import BudgetRefused, Settlement

from ._helpers import make_ledger, open_root, release, reserve, settle

ROOT = "gr-000000000001"


def child(ledger, parent, node, requested=None):
    return ledger.open_child(parent_path=parent, run_path=f"{parent}/{node}", node_id=node,
                             requested_usd=requested)


# --- reserve ---------------------------------------------------------------


def test_reserve_moves_amount_into_reserved(tmp_path):
    store, ledger = make_ledger(tmp_path)
    open_root(ledger, requested=5.0)
    reserve(store, ledger, ROOT, "r1", 1.25)
    acct = ledger.account(ROOT)
    assert acct.reserved_usd == 1.25
    assert acct.remaining_usd == 3.75


def test_reserve_over_remaining_is_refused_with_code(tmp_path):
    store, ledger = make_ledger(tmp_path)
    open_root(ledger, requested=1.0)
    with pytest.raises(BudgetRefused) as info:
        reserve(store, ledger, ROOT, "r1", 1.01)
    assert info.value.code == "budget_exhausted"
    assert ledger.account(ROOT).reserved_usd == 0.0


def test_reserve_exactly_remaining_is_allowed(tmp_path):
    store, ledger = make_ledger(tmp_path)
    open_root(ledger, requested=1.0)
    reserve(store, ledger, ROOT, "r1", 1.0)
    assert ledger.account(ROOT).remaining_usd == 0.0
    with pytest.raises(BudgetRefused):
        reserve(store, ledger, ROOT, "r2", 0.000001)


def test_reserve_counts_held_by_children(tmp_path):
    store, ledger = make_ledger(tmp_path)
    open_root(ledger, requested=5.0)
    child(ledger, ROOT, "a", requested=4.0)
    with pytest.raises(BudgetRefused) as info:
        reserve(store, ledger, ROOT, "r1", 1.5)
    assert info.value.code == "budget_exhausted"
    reserve(store, ledger, ROOT, "r2", 1.0)


def test_reserve_is_idempotent_per_request_and_attempt(tmp_path):
    store, ledger = make_ledger(tmp_path)
    open_root(ledger, requested=5.0)
    reserve(store, ledger, ROOT, "r1", 1.0)
    reserve(store, ledger, ROOT, "r1", 1.0)
    assert ledger.account(ROOT).reserved_usd == 1.0
    reserve(store, ledger, ROOT, "r1", 1.0, attempt=2)  # a new attempt is a new reservation
    assert ledger.account(ROOT).reserved_usd == 2.0


def test_reserve_rolls_back_with_its_transaction(tmp_path):
    store, ledger = make_ledger(tmp_path)
    open_root(ledger, requested=5.0)
    with pytest.raises(RuntimeError), store.txn() as conn:
        ledger.reserve(conn, run_path=ROOT, request_id="r1", attempt=1, amount_usd=2.0)
        raise RuntimeError("boom")
    assert ledger.account(ROOT).reserved_usd == 0.0


def test_closed_account_refuses_reserve(tmp_path):
    store, ledger = make_ledger(tmp_path)
    open_root(ledger, requested=5.0)
    ledger.close(ROOT)
    with pytest.raises(BudgetRefused) as info:
        reserve(store, ledger, ROOT, "r1", 0.1)
    assert info.value.code == "account_closed"


# --- settle ----------------------------------------------------------------


def test_settle_with_actual_charges_actual(tmp_path):
    store, ledger = make_ledger(tmp_path)
    open_root(ledger, requested=5.0)
    reserve(store, ledger, ROOT, "r1", 1.0)
    result = settle(store, ledger, "r1", 0.4)
    assert isinstance(result, Settlement)
    assert (result.request_id, result.attempt) == ("r1", 1)
    assert result.reserved_usd == 1.0
    assert result.charged_usd == 0.4
    assert result.cost_source == "actual"
    assert result.overrun_usd == 0.0
    acct = ledger.account(ROOT)
    assert acct.reserved_usd == 0.0 and acct.spent_usd == 0.4


def test_settle_none_charges_the_reservation(tmp_path):
    store, ledger = make_ledger(tmp_path)
    open_root(ledger, requested=5.0)
    reserve(store, ledger, ROOT, "r1", 1.0)
    result = settle(store, ledger, "r1", None)
    assert result.charged_usd == 1.0
    assert result.cost_source == "reservation"
    assert ledger.account(ROOT).spent_usd == 1.0
    assert ledger.account(ROOT).reserved_usd == 0.0


def test_settle_zero_actual_is_actual_not_reservation(tmp_path):
    store, ledger = make_ledger(tmp_path)
    open_root(ledger, requested=5.0)
    reserve(store, ledger, ROOT, "r1", 1.0)
    result = settle(store, ledger, "r1", 0.0)
    assert result.charged_usd == 0.0
    assert result.cost_source == "actual"
    assert ledger.account(ROOT).spent_usd == 0.0


def test_overrun_is_recorded_not_hidden(tmp_path):
    store, ledger = make_ledger(tmp_path)
    open_root(ledger, requested=1.0)
    reserve(store, ledger, ROOT, "r1", 1.0)
    result = settle(store, ledger, "r1", 1.5)
    assert result.charged_usd == 1.5
    assert result.overrun_usd == 0.5
    assert ledger.account(ROOT).spent_usd == 1.5


def test_settle_is_idempotent_returns_first_settlement(tmp_path):
    store, ledger = make_ledger(tmp_path)
    open_root(ledger, requested=5.0)
    reserve(store, ledger, ROOT, "r1", 1.0)
    first = settle(store, ledger, "r1", 0.4)
    second = settle(store, ledger, "r1", 0.9)
    assert second == first
    assert ledger.account(ROOT).spent_usd == 0.4


def test_attempts_settle_independently(tmp_path):
    store, ledger = make_ledger(tmp_path)
    open_root(ledger, requested=5.0)
    reserve(store, ledger, ROOT, "r1", 1.0, attempt=1)
    reserve(store, ledger, ROOT, "r1", 1.0, attempt=2)
    settle(store, ledger, "r1", None, attempt=1)  # interrupted: charged at reservation
    settle(store, ledger, "r1", 0.25, attempt=2)
    assert ledger.account(ROOT).spent_usd == 1.25


# --- release ---------------------------------------------------------------


def test_release_returns_reservation_whole(tmp_path):
    store, ledger = make_ledger(tmp_path)
    open_root(ledger, requested=5.0)
    reserve(store, ledger, ROOT, "r1", 2.0)
    release(store, ledger, "r1")
    acct = ledger.account(ROOT)
    assert acct.reserved_usd == 0.0 and acct.spent_usd == 0.0
    assert acct.remaining_usd == 5.0


def test_release_is_idempotent(tmp_path):
    store, ledger = make_ledger(tmp_path)
    open_root(ledger, requested=5.0)
    reserve(store, ledger, ROOT, "r1", 2.0)
    release(store, ledger, "r1")
    release(store, ledger, "r1")
    release(store, ledger, "never-reserved")
    assert ledger.account(ROOT).reserved_usd == 0.0


# --- close -----------------------------------------------------------------


def test_close_child_returns_hold_and_adds_child_spend_to_parent(tmp_path):
    store, ledger = make_ledger(tmp_path)
    open_root(ledger, requested=10.0)
    kid = child(ledger, ROOT, "a", requested=4.0)
    reserve(store, ledger, kid.run_path, "k1", 2.0)
    settle(store, ledger, "k1", 1.5)
    closed = ledger.close(kid.run_path)
    assert closed.state == "closed"
    parent = ledger.account(ROOT)
    assert parent.held_usd == 0.0
    assert parent.spent_usd == 1.5
    assert parent.remaining_usd == pytest.approx(8.5)


def test_close_settles_open_reservations_at_reservation_first(tmp_path):
    store, ledger = make_ledger(tmp_path)
    open_root(ledger, requested=10.0)
    kid = child(ledger, ROOT, "a", requested=4.0)
    reserve(store, ledger, kid.run_path, "k1", 2.0)  # never settled: a dead path
    ledger.close(kid.run_path, state="dead")
    assert ledger.account(kid.run_path).state == "dead"
    assert ledger.account(kid.run_path).reserved_usd == 0.0
    assert ledger.account(ROOT).spent_usd == 2.0
    assert ledger.account(ROOT).held_usd == 0.0


def test_overrun_propagates_to_parent_on_close(tmp_path):
    store, ledger = make_ledger(tmp_path)
    open_root(ledger, requested=10.0)
    kid = child(ledger, ROOT, "a", requested=1.0)
    reserve(store, ledger, kid.run_path, "k1", 1.0)
    settle(store, ledger, "k1", 1.75)
    ledger.close(kid.run_path)
    assert ledger.account(ROOT).spent_usd == 1.75  # more than the child's cap of 1.0
    assert ledger.account(ROOT).held_usd == 0.0


def test_close_is_idempotent(tmp_path):
    store, ledger = make_ledger(tmp_path)
    open_root(ledger, requested=10.0)
    kid = child(ledger, ROOT, "a", requested=4.0)
    reserve(store, ledger, kid.run_path, "k1", 1.0)
    settle(store, ledger, "k1", 1.0)
    ledger.close(kid.run_path)
    ledger.close(kid.run_path)
    parent = ledger.account(ROOT)
    assert parent.spent_usd == 1.0 and parent.held_usd == 0.0


def test_close_root_returns_pool_hold_and_adds_spend(tmp_path):
    store, ledger = make_ledger(tmp_path)
    open_root(ledger, requested=6.0)
    reserve(store, ledger, ROOT, "r1", 2.0)
    settle(store, ledger, "r1", 2.0)
    ledger.close(ROOT)
    pool = ledger.account("pool:default")
    assert pool.held_usd == 0.0
    assert pool.spent_usd == 2.0
    assert pool.remaining_usd == 8.0


def test_subtree_spent_sums_the_subtree(tmp_path):
    store, ledger = make_ledger(tmp_path)
    open_root(ledger, requested=10.0)
    kid = child(ledger, ROOT, "a", requested=4.0)
    grand = child(ledger, kid.run_path, "b", requested=2.0)
    reserve(store, ledger, ROOT, "r0", 1.0)
    settle(store, ledger, "r0", 1.0)
    reserve(store, ledger, kid.run_path, "r1", 1.0)
    settle(store, ledger, "r1", 0.5)
    reserve(store, ledger, grand.run_path, "r2", 1.0)
    settle(store, ledger, "r2", 0.25)
    assert ledger.subtree_spent(grand.run_path) == 0.25
    assert ledger.subtree_spent(kid.run_path) == pytest.approx(0.75)
    assert ledger.subtree_spent(ROOT) == pytest.approx(1.75)


def test_leaf_charges_equal_pool_spent_after_all_closes(tmp_path):
    store, ledger = make_ledger(tmp_path)
    open_root(ledger, requested=10.0)
    kid = child(ledger, ROOT, "a", requested=4.0)
    grand = child(ledger, kid.run_path, "b", requested=2.0)
    charges = {ROOT: [0.3, 0.2], kid.run_path: [0.7], grand.run_path: [0.4, 0.1]}
    n = 0
    for path, amounts in charges.items():
        for amount in amounts:
            n += 1
            reserve(store, ledger, path, f"q{n}", 0.5)
            settle(store, ledger, f"q{n}", amount)
    for path in (grand.run_path, kid.run_path, ROOT):
        ledger.close(path)
    assert ledger.account("pool:default").spent_usd == pytest.approx(1.7)


# --- exactness ------------------------------------------------------------


def test_166_settles_of_006_against_10_give_exactly_996(tmp_path):
    store, ledger = make_ledger(tmp_path)
    open_root(ledger, requested=10.0)
    for i in range(166):
        reserve(store, ledger, ROOT, f"r{i}", 0.06)
        settle(store, ledger, f"r{i}", 0.06)
    acct = ledger.account(ROOT)
    assert acct.spent_usd == 9.96
    assert acct.reserved_usd == 0.0
    assert acct.remaining_usd == 0.04
    with pytest.raises(BudgetRefused):
        reserve(store, ledger, ROOT, "one-too-many", 0.05)
    reserve(store, ledger, ROOT, "last-fit", 0.04)


def test_float_sums_do_not_drift(tmp_path):
    store, ledger = make_ledger(tmp_path)
    open_root(ledger, requested=1.0)
    reserve(store, ledger, ROOT, "r1", 0.1)
    reserve(store, ledger, ROOT, "r2", 0.2)
    assert ledger.account(ROOT).reserved_usd == 0.3  # not 0.30000000000000004
