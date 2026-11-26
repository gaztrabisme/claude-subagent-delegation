"""Spec 7, row 3: stop and restart the Temporal dev server on the same db, mid-run."""

from __future__ import annotations

import time
from collections import Counter

from ._checks import assert_no_duplicate_starts
from ._cluster import crash_graph, task_node, task_statuses, wait_for


def pause_graph():
    return {"schema": "subagent.graph/1", "id": "pausing", "version": 1, "budget_usd": 5.0,
            "defaults": {"provider": "codex", "reserve_usd": 0.5},
            "body": [task_node("p1", "codex"), task_node("p2", "codex"), task_node("p3", "codex")],
            "output": {"p3": "nodes.p3.status"}}


def decision_graph():
    return {"schema": "subagent.graph/1", "id": "deciding", "version": 1, "budget_usd": 5.0,
            "defaults": {"provider": "codex", "reserve_usd": 0.5},
            "body": [task_node("d1", "codex"),
                     {"id": "ask", "kind": "owner_decision", "question": "Ship it?",
                      "options": ["ship", "hold"]},
                     task_node("d2", "codex")],
            "output": {"decision": "nodes.ask.output.answer"}}


def starts_matching(cluster, root):
    return Counter(e["attempt_id"] for e in cluster.audit_events()
                   if e["event"] == "start" and e["attempt_id"].startswith(root + "/"))


def test_run_completes_pause_and_owner_wait_survive_a_dev_server_restart(make_cluster):
    cluster = make_cluster(default_step={"cost_usd": 0.2, "sleep_s": 1.0}, root_budget=60.0)
    first = cluster.bring_up(workers=2)
    address = first.address

    main = cluster.call("start", crash_graph(root_usd=30.0, a_usd=15.0, b_usd=8.0),
                        request_id="restart-main", workspace=str(cluster.workspace))["root_run_id"]
    paused = cluster.call("start", pause_graph(), request_id="restart-pause",
                          workspace=str(cluster.workspace))["root_run_id"]
    cluster.call("pause", paused, "paused across a restart")
    cluster.wait_state(paused, ("paused",), 60)
    deciding = cluster.call("start", decision_graph(), request_id="restart-decision",
                            workspace=str(cluster.workspace))["root_run_id"]
    cluster.wait_state(deciding, ("waiting",), 90)
    wait_for(lambda: len(cluster.audit_events()) >= 4, 60, "the main graph to be running")
    paused_before = sum(starts_matching(cluster, paused).values())

    assert cluster.server.stop(timeout_s=15) is True
    assert not cluster.server.health(timeout_s=0.5)["ok"]
    assert (cluster.state_dir / "temporal.db").exists()
    time.sleep(1.5)  # the workers lose their connection while it is down
    second = cluster.start_server()  # same state dir, same address, same db file
    assert second.address == address and second.started_by_us is True

    final = cluster.wait_done(main, timeout=200)
    assert final["state"] == "completed", final
    statuses = task_statuses(final["result"]["output"])
    assert len(statuses) == 24 and set(statuses.values()) == {"completed"}
    assert_no_duplicate_starts(cluster.audit_events())

    # the paused root is still paused and dispatched nothing while the server was away
    state = cluster.wait_state(paused, ("paused",), 60)
    assert state["paused"] is True
    assert sum(starts_matching(cluster, paused).values()) == paused_before
    resumed = cluster.call("resume", paused)
    assert resumed["ok"] is True and resumed["accepted"] is True
    done = cluster.wait_done(paused, timeout=120)
    assert done["state"] == "completed"
    assert max(starts_matching(cluster, paused).values()) == 1

    # the owner decision is still waiting, and an answer resumes it
    waiting = cluster.wait_state(deciding, ("waiting",), 60)
    open_ids = waiting["waiting"]["exceptions"]
    assert len(open_ids) == 1
    answered = cluster.call("answer", open_ids[0], "ship", note="after the restart")
    assert answered["ok"] is True
    finished = cluster.wait_done(deciding, timeout=120)
    assert finished["state"] == "completed"
    assert finished["result"]["output"] == {"decision": "ship"}
    assert_no_duplicate_starts(cluster.audit_events())
