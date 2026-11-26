"""Review findings 2, 5 and the determinism note: start/reconcile race, dead roots in
graph_list/graph_status, ordered join cancellation (SPEC 3, 5, 9)."""

from __future__ import annotations

import asyncio
import socket

import pytest

from subagent.graph.interpreter import NodeResult


class _Clock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _stack(tmp_path, clock):
    from subagent.graph.admission import Admission, Workers
    from subagent.graph.journal import Journal
    from subagent.graph.ledger import Ledger
    from subagent.graph.store import Store

    store = Store(tmp_path / "g.sqlite3")
    ledger = Ledger(store, clock=clock)
    ledger.ensure_pool("default", 100.0)
    admission = Admission(store, ledger, Journal(store), caps={"a": 1},
                          workers=Workers(store, clock=clock), clock=clock)
    return ledger, admission


def _open(ledger, name):
    return ledger.open_root(root_run_id=name, pool="default", requested_usd=5.0,
                            graph_id="g", graph_hash="h")


def test_reconcile_spares_a_root_opened_a_moment_ago(tmp_path):  # finding 2
    clock = _Clock()
    ledger, admission = _stack(tmp_path, clock)
    _open(ledger, "gr-old000000001")
    clock.advance(3600)
    _open(ledger, "gr-new000000002")  # opened, start_workflow not yet called
    clock.advance(1)
    report = admission.reconcile(root_live=lambda root: False)  # Temporal: NOT_FOUND
    assert ledger.account("gr-new000000002").state == "open", "fresh root marked dead"
    assert "gr-new000000002" not in report.dead
    assert ledger.account("gr-old000000001").state == "dead"
    assert report.dead == ["gr-old000000001"]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_dead_root_reads_dead_in_list_and_status(tmp_path):  # finding 5
    pytest.importorskip("temporalio")

    async def scenario():
        from subagent.graph import roots
        from subagent.graph.admission import Admission
        from subagent.graph.journal import Journal
        from subagent.graph.server import GraphSettings
        from subagent.graph.service import GraphService
        from tests.conftest import make_settings

        settings = make_settings(tmp_path)
        gs = GraphSettings(state_dir=tmp_path / "state", address=f"127.0.0.1:{_free_port()}",
                           autostart=False)
        service = GraphService(settings, gsettings=gs)
        root = "gr-dead00000001"
        await asyncio.to_thread(service._ensure_pools)
        await asyncio.to_thread(service._open_root, root, "default", 1.0, "g", "h")
        await asyncio.to_thread(roots.register, service.store, root, graph_id="g",
                                graph_version=1)
        with service.store.txn() as conn:  # an old orphan, past any start grace
            conn.execute("UPDATE accounts SET created_at=created_at-100000 WHERE run_path=?",
                         (root,))
        admission = Admission(service.store, service.ledger, Journal(service.store), caps={})
        report = await asyncio.to_thread(admission.reconcile, root_live=lambda r: False)
        assert report.dead == [root]
        listed = await service.list()
        (row,) = [r for r in listed["runs"] if r["root_run_id"] == root]
        status = await service.status(root)
        return row["state"], status["state"]

    list_state, status_state = asyncio.run(scenario())
    assert list_state in ("dead", "failed"), f"graph_list says {list_state!r}"
    assert status_state in ("dead", "failed"), f"graph_status says {status_state!r}"
    assert list_state == status_state


def _hashed_task(index: int):
    """A Task whose hash falls in reverse index order, so set iteration is reversed."""
    class HashedTask(asyncio.Task):
        __hash__ = lambda self: 1000 - index  # noqa: E731

    return HashedTask


def test_join_returns_the_lowest_done_index_and_cancels_in_branch_order():
    from subagent.graph.interpreter import Interpreter

    cancelled: list[int] = []

    async def branch(i: int) -> NodeResult:
        if i in (1, 3):
            return NodeResult(id=f"b{i}", kind="task", status="completed")
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(i)
            raise
        return NodeResult(id=f"b{i}", kind="task", status="completed")

    async def scenario():
        loop = asyncio.get_running_loop()
        tasks = [_hashed_task(i)(branch(i), loop=loop) for i in range(10)]
        hit = await asyncio.wait_for(Interpreter._join("any", tasks), 20)
        return hit

    hit = asyncio.run(scenario())
    assert hit == 1
    assert cancelled == [i for i in range(10) if i not in (1, 3)], \
        f"branches cancelled in set order, not branch order: {cancelled}"
