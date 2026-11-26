"""Spec 7, row 4: the Temporal database is lost; reconcile marks the open root dead."""

from __future__ import annotations

import os
import signal

from ._cluster import pid_alive, task_node, wait_for


def waiting_graph():
    return {"schema": "subagent.graph/1", "id": "waiter", "version": 1,
            "defaults": {"provider": "codex", "reserve_usd": 0.5},
            "body": [task_node("w1", "codex"),
                     {"id": "ask", "kind": "owner_decision", "question": "Continue?",
                      "options": ["yes", "no"]},
                     task_node("w2", "codex")]}


def test_lost_temporal_db_marks_root_dead_and_raises_run_dead(make_cluster):
    cluster = make_cluster(default_step={"cost_usd": 0.1, "sleep_s": 0.3}, root_budget=10.0)
    cluster.bring_up(workers=1)
    root = cluster.call("start", waiting_graph(), request_id="db-lost-1",
                        workspace=str(cluster.workspace))["root_run_id"]
    cluster.wait_state(root, ("waiting",), 90)

    # everything goes down, then only the Temporal database is lost
    for worker in cluster.workers:
        os.kill(worker.pid, signal.SIGKILL)
    wait_for(lambda: not any(pid_alive(w.pid) for w in cluster.workers), 15, "workers to die")
    assert cluster.server.stop(timeout_s=15) is True
    for name in ("temporal.db", "temporal.db-wal", "temporal.db-shm"):
        path = cluster.state_dir / name
        if path.exists():
            path.unlink()
    assert not (cluster.state_dir / "temporal.db").exists()

    cluster.start_server()  # a fresh database: Temporal no longer knows the workflow
    cluster.start_worker()  # reconcile runs at worker start

    from subagent.graph.exceptions import ExceptionQueue
    from subagent.graph.ledger import Ledger

    def dead():
        return Ledger(cluster.store()).account(root).state == "dead"

    wait_for(dead, 90, "reconcile to mark the root dead")

    store = cluster.store()
    ledger = Ledger(store)
    account = ledger.account(root)
    assert account.state == "dead"
    assert account.reserved_usd == 0.0
    records = ExceptionQueue(store).list(state="all", root_run_id=root)
    kinds = [r.kind for r in records]
    assert kinds.count("run_dead") == 1
    run_dead = next(r for r in records if r.kind == "run_dead")
    assert run_dead.needs_answer is False
    owner = [r for r in records if r.kind == "owner_decision"]
    assert owner and all(r.state == "closed" and r.closed_reason == "run_dead" for r in owner)
    for node in ledger.tree(root):
        assert node.state == "dead"
