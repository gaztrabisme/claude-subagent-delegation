"""Spec 4 invariant under concurrency: 4 processes x 50 reserve+settle on one root."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

from ._helpers import make_ledger, open_root

ROOT = "gr-000000000001"

WORKER = textwrap.dedent('''
    import json, sys
    from pathlib import Path
    from subagent.graph.ledger import BudgetRefused, Ledger
    from subagent.graph.store import Store

    db, proc = sys.argv[1], sys.argv[2]
    store = Store(Path(db))
    ledger = Ledger(store)
    root = "gr-000000000001"
    charged_micro = 0
    refused = 0
    for i in range(50):
        request_id = f"p{proc}-{i}"
        try:
            with store.txn() as conn:
                ledger.reserve(conn, run_path=root, request_id=request_id, attempt=1,
                               amount_usd=0.10)
        except BudgetRefused as exc:
            assert exc.code == "budget_exhausted", exc.code
            refused += 1
            continue
        actual = 0.04 if i % 3 else 0.07
        with store.txn() as conn:
            result = ledger.settle(conn, request_id=request_id, attempt=1, actual_usd=actual)
        charged_micro += round(result.charged_usd * 1_000_000)
    print(json.dumps({"charged_micro": charged_micro, "refused": refused}))
''')


def test_four_processes_never_break_the_invariant(tmp_path):
    store, ledger = make_ledger(tmp_path)
    open_root(ledger, requested=12.0)
    procs = [subprocess.Popen([sys.executable, "-I", "-c", WORKER, str(tmp_path / "g.sqlite3"),
                               str(n)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True) for n in range(4)]
    outputs = []
    try:
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            assert proc.returncode == 0, err
            outputs.append(json.loads(out.strip().splitlines()[-1]))
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
    charged = sum(o["charged_micro"] for o in outputs)
    refused = sum(o["refused"] for o in outputs)
    acct = ledger.account(ROOT)
    assert acct.reserved_usd == 0.0
    assert acct.spent_usd + acct.reserved_usd + acct.held_usd <= acct.cap_usd + 1e-9
    assert round(acct.spent_usd * 1_000_000) == charged
    assert charged > 0
    # every refusal was budget_exhausted (the worker asserts it); 200 attempts fit in 12.00
    # only if reservations are released quickly, so a refusal here is allowed, not required
    assert refused >= 0
    ledger.close(ROOT)
    pool = ledger.account("pool:default")
    assert round(pool.spent_usd * 1_000_000) == charged
    assert pool.held_usd == 0.0


def test_concurrent_processes_hit_the_cap_and_are_refused_not_overspent(tmp_path):
    """A small cap forces refusals: the cap holds and refusals use the budget code."""
    store, ledger = make_ledger(tmp_path)
    open_root(ledger, requested=3.0)
    procs = [subprocess.Popen([sys.executable, "-I", "-c", WORKER, str(tmp_path / "g.sqlite3"),
                               str(n)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True) for n in range(4)]
    outputs = []
    try:
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            assert proc.returncode == 0, err
            outputs.append(json.loads(out.strip().splitlines()[-1]))
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
    acct = ledger.account(ROOT)
    assert acct.spent_usd <= acct.cap_usd + 1e-9
    assert acct.reserved_usd == 0.0
    assert sum(o["refused"] for o in outputs) > 0
    assert round(acct.spent_usd * 1_000_000) == sum(o["charged_micro"] for o in outputs)
