"""Spec 7, row 1: killing the client (the process that called graph start) changes nothing."""

from __future__ import annotations

import contextlib
import json
import os
import select
import signal
import subprocess
import sys
import time

from subagent.graph import ids

from ._checks import assert_no_duplicate_starts, starts_and_ends
from ._cluster import CLI, task_node, task_statuses


def small_graph(n=4):
    return {"schema": "subagent.graph/1", "id": "small", "version": 1,
            "defaults": {"provider": "codex", "reserve_usd": 0.5},
            "body": [{"id": "par", "kind": "parallel", "branches": [
                task_node(f"r{i}", "codex" if i % 2 == 0 else "glm") for i in range(n)]}],
            "output": {"par": "nodes.par.output.branches"}}


def read_json_then_kill(proc: subprocess.Popen, timeout=60.0):
    """Read the client's stdout until it holds one JSON document, then SIGKILL the client."""
    buffer = b""
    deadline = time.monotonic() + timeout
    fd = proc.stdout.fileno()
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.5)
        if ready:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            buffer += chunk
            try:
                payload = json.loads(buffer.decode())
            except ValueError:
                continue
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
            return payload
        if proc.poll() is not None:
            break
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGKILL)
    raise AssertionError(f"no JSON from the client: {buffer!r}")


def test_client_killed_after_start_reattach_by_request_id_and_run_completes(make_cluster,
                                                                              tmp_path):
    cluster = make_cluster()
    cluster.bring_up(workers=1)
    graph_file = tmp_path / "small.json"
    graph_file.write_text(json.dumps(small_graph()))
    client = subprocess.Popen(
        [sys.executable, "-c", CLI, "graph", "start", str(graph_file), "--request-id",
         "client-kill-1", "--workspace", str(cluster.workspace), "--json"],
        env=cluster.env(), cwd=str(cluster.workspace), stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        started = read_json_then_kill(client)
    finally:
        if client.poll() is None:
            os.killpg(client.pid, signal.SIGKILL)
    assert started["ok"] is True
    root = started["root_run_id"]
    assert root == ids.new_root_run_id("client-kill-1")
    assert started["workflow_id"] == root
    assert started["attached"] is False and started["state"] == "running"

    code, again = cluster.run_cli("start", str(graph_file), "--request-id", "client-kill-1",
                                  "--workspace", str(cluster.workspace))
    assert code == 0, again
    assert again["root_run_id"] == root and again["attached"] is True
    assert again["graph_hash"] == started["graph_hash"]

    other_file = tmp_path / "other.json"
    other_file.write_text(json.dumps(small_graph(3)))
    code, conflict = cluster.run_cli("start", str(other_file), "--request-id", "client-kill-1",
                                     "--workspace", str(cluster.workspace))
    assert code == 4, conflict
    assert conflict["error"]["code"] == "conflict"
    assert "request_id reused with a different graph" in conflict["error"]["message"]

    final = cluster.wait_done(root, timeout=120)
    assert final["state"] == "completed"
    statuses = task_statuses(final["result"]["output"])
    assert statuses == {f"r{i}": "completed" for i in range(4)}
    events = cluster.audit_events()
    assert_no_duplicate_starts(events)
    starts, ends = starts_and_ends(events)
    assert len(starts) == 4 and set(starts) == set(ends)
