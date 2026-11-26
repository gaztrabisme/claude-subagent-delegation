"""Fix-round-2 review (fix1-ds.json check 1, trace half of finding 3): SPEC 10 events that were
still missing: exception `answered`, admission `expired` with the budget `release`, exception
`closed` for run_dead, and a child graph_run `started` that carries its real cap."""

from __future__ import annotations

import asyncio
import json
import types

import pytest

from ..f_tools_cli.conftest import local_temporal, until
from ..h_fix1.conftest import TraceSink, boot, traced_worker

pytest.importorskip("temporalio")


# -- fakes for the reconcile path: no Temporal server needed -----------------------------


class _Handle:
    def __init__(self, *, running: bool):
        self.running = running

    async def describe(self):
        from temporalio.client import WorkflowExecutionStatus
        from temporalio.service import RPCError, RPCStatusCode

        if not self.running:
            raise RPCError("workflow not found", RPCStatusCode.NOT_FOUND, b"")
        return types.SimpleNamespace(status=WorkflowExecutionStatus.RUNNING)


class _Client:
    def __init__(self, *, running: bool):
        self.running = running

    def get_workflow_handle(self, _id):
        return _Handle(running=self.running)


def _ctx(tmp_path, trace):
    from subagent.graph.dispatch import FakeDispatcher
    from subagent.graph.server import GraphSettings
    from subagent.graph.worker import build_context
    from tests.conftest import make_settings

    settings = make_settings(tmp_path)
    gs = GraphSettings(state_dir=tmp_path / "state", address="127.0.0.1:1", autostart=False,
                       budget_usd=10.0, lane_slots={"glm": 2})
    (tmp_path / "state").mkdir(mode=0o700, exist_ok=True)
    return build_context(settings, gs, FakeDispatcher(), trace=trace)


def _open_root(ctx, root: str, reserve: float = 0.5):
    ctx.ledger.open_root(root_run_id=root, pool="default", requested_usd=5.0,
                         graph_id="g", graph_hash="h")
    ctx.admission.enqueue(request_id=f"{root}-rq", attempt=1, lane="glm", priority=3,
                          fairness_key=root, run_path=root, node_id="t", reserve_usd=reserve,
                          deps=[])
    res = ctx.admission.try_acquire(f"{root}-rq", 1)
    assert res.status == "granted", res
    return f"{root}-rq#a1"


def _advance(ctx, seconds: float) -> None:
    base = ctx.admission.clock
    ctx.admission.clock = lambda: base() + seconds


def test_reconcile_of_an_expired_lease_traces_admission_expired_and_budget_release(tmp_path):
    from subagent.graph.worker import _reconcile_once

    trace = TraceSink()
    ctx = _ctx(tmp_path, trace)
    attempt = _open_root(ctx, "gr-expire000001")
    trace.records.clear()
    _advance(ctx, 100_000)  # far past any grant or lease ttl
    asyncio.run(_reconcile_once(_Client(running=True), ctx))
    expired = [r for r in trace.of("graph_admission") if r["event"] == "expired"]
    assert [r["attempt_id"] for r in expired] == [attempt], \
        f"no graph_admission 'expired' for the lapsed lease; saw {trace.of('graph_admission')}"
    assert expired[0]["root_run_id"] == "gr-expire000001" and expired[0]["lane"] == "glm"
    released = [r for r in trace.of("graph_budget")
                if r["event"] == "release" and r["attempt_id"] == attempt]
    assert released, f"no graph_budget 'release' for the expired lease; saw {trace.of('graph_budget')}"
    assert released[0]["run_path"] == "gr-expire000001"


def test_admit_that_expires_another_lease_traces_it_too(tmp_path):
    from subagent.graph import activities

    trace = TraceSink()
    ctx = _ctx(tmp_path, trace)
    attempt = _open_root(ctx, "gr-expire000002")
    _advance(ctx, 100_000)
    acts = {a.__name__: a for a in activities.bind(ctx)}
    request = {"request_id": "gr-expire000002-rq2", "attempt": 1,
               "root_run_id": "gr-expire000002", "run_path": "gr-expire000002",
               "node_id": "t2", "lane": "glm", "priority": 3, "reserve_usd": 0.5, "deps": []}
    trace.records.clear()
    asyncio.run(acts["admit"](request))  # try_acquire expires the stale lease first
    events = [(r["event"], r["attempt_id"]) for r in trace.of("graph_admission")]
    assert ("expired", attempt) in events, f"admit swallowed the expiry; saw {events}"
    assert any(r["event"] == "release" and r["attempt_id"] == attempt
               for r in trace.of("graph_budget")), trace.of("graph_budget")


def test_run_dead_closes_open_exceptions_with_a_trace_event(tmp_path):
    from subagent.graph.worker import _reconcile_once

    trace = TraceSink()
    ctx = _ctx(tmp_path, trace)
    root = "gr-dead00000002"
    ctx.ledger.open_root(root_run_id=root, pool="default", requested_usd=1.0, graph_id="g",
                         graph_hash="h")
    with ctx.store.txn() as conn:  # past the fresh-root grace
        conn.execute("UPDATE accounts SET created_at=created_at-100000 WHERE run_path=?", (root,))
    record = ctx.exceptions.raise_(key=f"{root}:ask", root_run_id=root, run_path=root,
                                   node_id="ask", kind="owner_decision", summary="Go?",
                                   options=["go", "stop"], needs_answer=True)
    trace.records.clear()
    asyncio.run(_reconcile_once(_Client(running=False), ctx))
    assert ctx.exceptions.get(record.exception_id).state == "closed", "sanity: run_dead closes it"
    closed = [r for r in trace.of("graph_exception") if r["event"] == "closed"]
    assert [r["exception_id"] for r in closed] == [record.exception_id], \
        f"no graph_exception 'closed' for close_for_root; saw {trace.of('graph_exception')}"
    assert closed[0]["root_run_id"] == root and closed[0]["exception_kind"] == "owner_decision"
    assert closed[0]["needs_answer"] is True


def test_answer_through_the_service_writes_a_graph_exception_answered_record(tmp_path):
    from tests.conftest import make_settings

    trace_file = tmp_path / "trace.jsonl"

    async def scenario():
        from subagent.graph.server import GraphSettings
        from subagent.graph.service import GraphService

        async with local_temporal(tmp_path) as env:
            settings = make_settings(tmp_path, trace=str(trace_file))
            gs = GraphSettings(state_dir=tmp_path / "state",
                               address=env.client.service_client.config.target_host,
                               autostart=False, budget_usd=10.0)
            service = GraphService(settings, gsettings=gs)
            root = "gr-answer000001"
            await asyncio.to_thread(service._ensure_pools)
            record = await asyncio.to_thread(
                service.queue.raise_, key=f"{root}:ask", root_run_id=root, run_path=root,
                node_id="ask", kind="owner_decision", summary="Go?", options=["go", "stop"],
                needs_answer=True)
            res = await service.answer(record.exception_id, "go", answered_by="tester")
            assert res["ok"] is True, res
            return record, res

    record, _ = asyncio.run(scenario())
    lines = [json.loads(x) for x in trace_file.read_text().splitlines()] \
        if trace_file.exists() else []
    answered = [r for r in lines if r.get("kind") == "graph_exception"
                and r.get("event") == "answered"]
    assert len(answered) == 1, f"no graph_exception 'answered' in the trace; saw {lines}"
    assert answered[0]["exception_id"] == record.exception_id
    assert answered[0]["root_run_id"] == "gr-answer000001"
    assert answered[0]["exception_kind"] == "owner_decision"
    assert answered[0]["needs_answer"] is True


SUB_DOC = {
    "schema": "subagent.graph/1", "id": "fix2-child", "version": 1, "budget_usd": 2.0,
    "body": [{"id": "sub", "kind": "subgraph", "budget_usd": 0.5, "inputs": {"x": 1},
              "body": [{"id": "g", "kind": "gate",
                        "condition": {"path": "inputs.x", "op": "eq", "value": 1}}]}],
}


def test_child_graph_run_started_carries_its_cap(tmp_path):
    async def scenario():
        from subagent.graph.dispatch import FakeDispatcher
        from subagent.graph.service import GraphService

        async with local_temporal(tmp_path) as env:
            settings, gsettings, _ = boot(env, tmp_path)
            service = GraphService(settings, gsettings=gsettings)
            trace = TraceSink()
            async with traced_worker(settings, gsettings, FakeDispatcher(), trace):
                started = await service.start(SUB_DOC, request_id="fix2-child")
                assert started["ok"] is True, started
                root = started["root_run_id"]

                async def done():
                    res = await service.status(root)
                    return res if res.get("state") in ("completed", "failed",
                                                       "cancelled") else None

                final = await until(done, timeout=60, what="the parent graph to finish")
                assert final["state"] == "completed", final
        return root, trace

    root, trace = asyncio.run(scenario())
    child_started = [r for r in trace.of("graph_run")
                     if r["event"] == "started" and r["run_path"] != root]
    assert len(child_started) == 1, trace.of("graph_run")
    assert child_started[0]["depth"] == 1
    assert child_started[0]["cap_usd"] == pytest.approx(0.5), \
        f"child graph_run started has cap_usd={child_started[0]['cap_usd']}"
