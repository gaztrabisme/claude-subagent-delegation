"""Review findings 3, 4 and 7: graph_node records, owner timeout closes its exception,
state-dir file modes (SPEC 3, 6, 10)."""

from __future__ import annotations

import os
import stat

import pytest

from ..f_tools_cli.conftest import local_temporal, until
from .conftest import TraceSink, boot, traced_worker, two_task_doc

pytest.importorskip("temporalio")

NODE_KEYS = {"kind", "root_run_id", "run_path", "node_id", "node_kind", "scope", "status",
             "attempts", "request_id", "cost_usd", "error"}


async def _done(service, root, timeout=60):
    async def check():
        res = await service.status(root)
        return res if res.get("state") in ("completed", "failed", "cancelled") else None

    return await until(check, timeout=timeout, what="a finished run")


def test_graph_node_records_for_each_finished_node(tmp_path):  # finding 3
    async def scenario():
        from subagent.graph.dispatch import FakeDispatcher, FakeStep
        from subagent.graph.service import GraphService

        async with local_temporal(tmp_path) as env:
            settings, gsettings, workspace = boot(env, tmp_path)
            service = GraphService(settings, gsettings=gsettings)
            trace = TraceSink()
            async with traced_worker(settings, gsettings,
                                     FakeDispatcher(default=FakeStep(cost_usd=0.05)), trace):
                started = await service.start(two_task_doc(workspace), request_id="fix1-node")
                assert started["ok"] is True, started
                root = started["root_run_id"]
                final = await _done(service, root)
                assert final["state"] == "completed", final
        nodes = trace.of("graph_node")
        assert trace.of("graph_run"), "sanity: graph_run records are written"
        assert {r["node_id"] for r in nodes} >= {"t1", "t2"}, \
            f"no graph_node record for the finished tasks; kinds seen: " \
            f"{sorted({r['kind'] for r in trace.records})}"
        for r in nodes:
            assert set(r) == NODE_KEYS
            assert r["root_run_id"] == root
            assert r["node_kind"] == "task" and r["status"] == "completed"
            assert r["attempts"] == 1
            assert r["cost_usd"] == pytest.approx(0.05)

    import asyncio

    asyncio.run(scenario())


OWNER_DOC = {
    "schema": "subagent.graph/1", "id": "fix1-owner", "version": 1,
    "body": [{"id": "ask", "kind": "owner_decision", "question": "Go?",
              "options": ["go", "stop"], "default": "stop", "timeout_s": 60}],
    "output": {"answer": "nodes.ask.output.answer"},
}


def test_owner_decision_timeout_closes_its_exception(tmp_path):  # finding 4
    async def scenario():
        from subagent.graph.dispatch import FakeDispatcher
        from subagent.graph.service import GraphService

        async with local_temporal(tmp_path) as env:
            settings, gsettings, _ = boot(env, tmp_path)
            service = GraphService(settings, gsettings=gsettings)
            async with traced_worker(settings, gsettings, FakeDispatcher(), TraceSink()):
                started = await service.start(OWNER_DOC, request_id="fix1-owner")
                assert started["ok"] is True, started
                root = started["root_run_id"]
                seen = await until(
                    lambda: _opened(service, root), timeout=30, what="the owner exception")
                final = await _done(service, root, timeout=120)
                assert final["state"] == "completed", final
                still_open = await service.exceptions(root_run_id=root, state="open")
                closed = await service.exceptions(root_run_id=root, state="closed")
        assert [e["exception_id"] for e in still_open["exceptions"]] == [], \
            "the timed-out owner_decision is still listed open"
        assert [e["exception_id"] for e in closed["exceptions"]] == [seen]
        assert closed["exceptions"][0]["closed_reason"]

    import asyncio

    asyncio.run(scenario())


async def _opened(service, root):
    res = await service.exceptions(root_run_id=root, state="open")
    return res["exceptions"][0]["exception_id"] if res.get("ok") and res["exceptions"] else None


def test_state_dir_modes_after_a_full_run(tmp_path):  # finding 7
    """A pre-existing 0755 state dir, a dev server and a worker: everything we own is private."""
    async def scenario():
        from subagent.graph.dispatch import FakeDispatcher
        from subagent.graph.server import DevServer
        from subagent.graph.service import GraphService

        from ..conftest import require_temporal
        from ..e_crash._cluster import free_loopback_port

        binary = require_temporal()
        state = tmp_path / "state"
        state.mkdir(mode=0o755)
        os.chmod(state, 0o755)
        server = DevServer(state, binary=binary, address=f"127.0.0.1:{free_loopback_port()}",
                           startup_timeout_s=30.0)
        info = server.start()
        try:
            settings, gsettings, workspace = boot(None, tmp_path, address=info.address)
            service = GraphService(settings, gsettings=gsettings)
            async with traced_worker(settings, gsettings, FakeDispatcher(), TraceSink()):
                started = await service.start(two_task_doc(workspace), request_id="fix1-perm")
                assert started["ok"] is True, started
                final = await _done(service, started["root_run_id"])
                assert final["state"] == "completed", final
        finally:
            server.stop(timeout_s=10)
        bad = []
        if stat.S_IMODE(state.stat().st_mode) != 0o700:
            bad.append(f"{state.name}/ {oct(stat.S_IMODE(state.stat().st_mode))} want 0o700")
        for path in sorted(state.rglob("*")):
            if path.name.startswith("temporal.db"):  # written by the Temporal CLI, not by us
                continue
            if path.is_symlink():
                continue
            mode = stat.S_IMODE(path.stat().st_mode)
            want = 0o700 if path.is_dir() else 0o600
            if mode != want:
                bad.append(f"{path.relative_to(state)} {oct(mode)} want {oct(want)}")
        assert not bad, "wrong modes:\n" + "\n".join(bad)

    import asyncio

    asyncio.run(scenario())
