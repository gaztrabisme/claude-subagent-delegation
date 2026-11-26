"""Full-hydrogen review C1: `[graph].default_reserve_usd` is the default task reservation (SPEC 2.3).

A task with no `reserve_usd` of its own and no `defaults.reserve_usd` reserves the configured
default; a node value or a `defaults` value still wins. Bounded waits, in-process worker.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from tests.conftest import make_settings

pytest.importorskip("temporalio")

from ..d_exceptions.conftest import in_process_worker, local_temporal, until  # noqa: E402


def _task(node_id: str, workspace: str, **extra) -> dict:
    node = {"id": node_id, "kind": "task", "provider": "glm", "prompt": "do it",
            "verification": "true", "workspace": workspace}
    node.update(extra)
    return node


def _reserved(state_dir, root: str) -> dict[str, float]:
    conn = sqlite3.connect(state_dir / "graph.sqlite3")
    try:
        rows = conn.execute("SELECT node_id, reserved FROM journal WHERE root_run_id=?",
                            (root,)).fetchall()
    finally:
        conn.close()
    return {node: micro / 1_000_000 for node, micro in rows}


async def _run(tmp_path, doc: dict, *, expect_nodes: set[str]) -> dict[str, float]:
    from subagent.graph.dispatch import FakeDispatcher, FakeStep
    from subagent.graph.server import GraphSettings
    from subagent.graph.service import GraphService

    settings = make_settings(tmp_path)
    async with local_temporal(tmp_path) as env:
        gsettings = GraphSettings(
            state_dir=tmp_path / "state",
            address=env.client.service_client.config.target_host,
            autostart=False, admit_poll_s=0.2, budget_usd=10.0, default_reserve_usd=0.07)
        service = GraphService(settings, gsettings=gsettings)
        async with in_process_worker(settings, gsettings,
                                     FakeDispatcher({}, default=FakeStep(cost_usd=0.01))):
            started = await service.start(doc, request_id="rsv-1")
            assert started["ok"] is True, started
            root = started["root_run_id"]

            async def done():
                res = await service.status(root)
                return res if res.get("state") in ("completed", "failed", "cancelled") else None

            final = await until(done, timeout=60, what="the graph to finish")
            assert final["state"] == "completed", final
            return _reserved(gsettings.state_dir, root)


def test_a_task_with_no_reserve_uses_the_configured_default_and_explicit_values_win(tmp_path):
    ws = str(tmp_path)
    doc = {"schema": "subagent.graph/1", "id": "rsv-a", "version": 1,
           "body": [_task("plain", ws), _task("explicit", ws, reserve_usd=0.3)]}
    got = asyncio.run(_run(tmp_path, doc, expect_nodes={"plain", "explicit"}))
    assert got.get("plain") == pytest.approx(0.07), \
        f"[graph].default_reserve_usd=0.07 was ignored; reservations were {got}"
    assert got.get("explicit") == pytest.approx(0.3), got


def test_defaults_reserve_usd_beats_the_configured_default(tmp_path):
    ws = str(tmp_path)
    doc = {"schema": "subagent.graph/1", "id": "rsv-b", "version": 1,
           "defaults": {"reserve_usd": 0.2},
           "body": [_task("inherits", ws)]}
    got = asyncio.run(_run(tmp_path, doc, expect_nodes={"inherits"}))
    assert got.get("inherits") == pytest.approx(0.2), got
