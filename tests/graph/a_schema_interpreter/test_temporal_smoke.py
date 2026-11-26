"""Spec 12.2 a: the Temporal binding on `start_local` with a FakeDispatcher worker in-process.

Needs temporalio and the Temporal CLI; skipped otherwise. Never downloads, never uses
time skipping.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest

pytest.importorskip("temporalio")

from subagent.graph.dispatch import FakeDispatcher, FakeStep  # noqa: E402
from subagent.graph.interpreter import GraphInput  # noqa: E402

from ..conftest import require_temporal, start_local, worked_example  # noqa: E402
from ._helpers import ROOT_ID, answer_with, make_queue, run_graph, task  # noqa: E402


def build_context(settings, tmp_path, dispatcher, prefix):
    from subagent.graph.activities import GraphContext
    from subagent.graph.admission import Admission, Workers
    from subagent.graph.exceptions import ExceptionQueue
    from subagent.graph.journal import Journal
    from subagent.graph.ledger import Ledger
    from subagent.graph.server import GraphSettings
    from subagent.graph.store import Store

    state = tmp_path / "graph"
    state.mkdir(exist_ok=True, mode=0o700)
    gsettings = GraphSettings(
        state_dir=state, task_queue_prefix=prefix, admit_poll_s=0.1, lease_ttl_s=30.0,
        lane_slots={"codex": 4, "deepseek": 4, "gemini": 4, "glm": 4})
    store = Store(state / "graph.sqlite3")
    ledger = Ledger(store, max_depth=gsettings.max_depth)
    ledger.ensure_pool("default", 50.0)
    journal = Journal(store)
    workers = Workers(store)
    admission = Admission(store, ledger, journal, caps=gsettings.caps(settings), workers=workers,
                          lease_ttl_s=gsettings.lease_ttl_s)
    exceptions = ExceptionQueue(store)
    worker_id = workers.register(os.getpid())
    return GraphContext(store, ledger, journal, admission, exceptions, dispatcher, settings,
                        gsettings, worker_id), gsettings


class Cluster:
    """Two in-process Temporal workers (graph queue, tasks queue) on a start_local server."""

    def __init__(self, env, ctx, gsettings):
        self.env, self.ctx, self.gsettings = env, ctx, gsettings
        self.prefix = gsettings.task_queue_prefix

    async def __aenter__(self):
        from subagent.graph.activities import bind
        from subagent.graph.workflow import GraphWorkflow
        from temporalio.worker import Worker

        activities = bind(self.ctx)
        self._graph = Worker(self.env.client, task_queue=f"{self.prefix}-graph",
                             workflows=[GraphWorkflow], activities=activities)
        self._tasks = Worker(self.env.client, task_queue=f"{self.prefix}-tasks",
                             activities=activities)
        await self._graph.__aenter__()
        await self._tasks.__aenter__()
        return self

    async def __aexit__(self, *exc):
        await self._tasks.__aexit__(*exc)
        await self._graph.__aexit__(*exc)

    async def start(self, document, inputs=None, root=ROOT_ID):
        graph_inputs = dict(document.get("inputs", {}))
        graph_inputs.update(inputs or {})
        inp = GraphInput(doc=document, inputs=graph_inputs, root_run_id=root, run_path=root,
                         parent_path=None, depth=0)
        return await self.env.client.start_workflow(
            "subagent.graph.GraphWorkflow", inp.as_dict(), id=root,
            task_queue=f"{self.prefix}-graph")

    async def answer_decisions(self, handle, answer, deadline_s=60.0):
        """The graph_answer path: SQLite first, then the wake-up signal."""
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            for record in self.ctx.exceptions.list(state="open"):
                if record.needs_answer:
                    self.ctx.exceptions.answer(record.exception_id, answer)
                    await handle.signal("answer", record.exception_id)
                    return record.exception_id
            await asyncio.sleep(0.2)
        raise AssertionError("no owner decision was raised")


def fd(script=None, default=None):
    return FakeDispatcher(script or {}, default=default or FakeStep(cost_usd=0.1))


def slim(result_dict):
    """The parts of a GraphResult that must agree between the two runtimes."""
    return {
        "status": result_dict["status"],
        "output": result_dict["output"],
        "error": result_dict["error"],
        "nodes": {k: {f: v[f] for f in ("status", "attempts", "request_id", "output", "error")}
                  for k, v in result_dict["nodes"].items()},
    }


def worked_script():
    return {
        "plan": [FakeStep(result_json={"packages": ["pkg_a", "pkg_b"]}, cost_usd=0.4)],
        "review": [FakeStep(verdict="PASS", result_json={"verdict": "PASS", "findings": []},
                            cost_usd=0.2)],
    }


def test_worked_example_same_graph_result_as_local_runtime(graph_settings, tmp_path):
    require_temporal()
    (tmp_path / "x").mkdir()
    local, _, _ = run_graph(graph_settings, worked_example(), fd(worked_script()),
                            answers=answer_with("merge"), exceptions=make_queue(tmp_path / "x"))

    async def go():
        async with start_local(tmp_path) as env:
            ctx, gs = build_context(graph_settings, tmp_path / "t", fd(worked_script()),
                                    "smk" + uuid.uuid4().hex[:6])
            async with Cluster(env, ctx, gs) as cluster:
                handle = await cluster.start(worked_example())
                answered = asyncio.create_task(cluster.answer_decisions(handle, "merge"))
                result = await asyncio.wait_for(handle.result(), 90)
                await answered
                return result

    (tmp_path / "t").mkdir()
    remote = asyncio.run(go())
    assert slim(remote) == slim(local.as_dict())
    assert remote["status"] == "completed"
    assert remote["output"] == {"verdict": "PASS", "decision": "merge"}
    assert remote["run_path"] == ROOT_ID


def test_status_query_matches_spec_mid_run(graph_settings, tmp_path):
    require_temporal()
    document = {"schema": "subagent.graph/1", "id": "slowg", "version": 1, "body": [
        task("quick"), task("slow", prompt="{{ nodes.quick.output.result_text }}")]}
    dispatcher = fd({"slow": [FakeStep(sleep_s=4.0)]})

    async def go():
        async with start_local(tmp_path) as env:
            ctx, gs = build_context(graph_settings, tmp_path / "t", dispatcher,
                                    "qry" + uuid.uuid4().hex[:6])
            async with Cluster(env, ctx, gs) as cluster:
                handle = await cluster.start(document)
                snapshot = None
                deadline = time.monotonic() + 40
                while time.monotonic() < deadline:
                    snap = await handle.query("status")
                    if "slow" in snap["running"]:
                        snapshot = snap
                        break
                    await asyncio.sleep(0.2)
                result = await asyncio.wait_for(handle.result(), 60)
                final = await handle.query("status")
                return snapshot, final, result

    (tmp_path / "t").mkdir()
    snapshot, final, result = asyncio.run(go())
    assert snapshot is not None, "the slow node never showed as running"
    for key in ("run_path", "state", "paused", "pause_reason", "nodes", "running", "queued",
                "waiting_exceptions", "children"):
        assert key in snapshot, key
    assert snapshot["run_path"] == ROOT_ID
    assert snapshot["state"] == "running"
    assert snapshot["paused"] is False and snapshot["pause_reason"] is None
    assert snapshot["nodes"]["quick"]["status"] == "completed"
    assert snapshot["nodes"]["quick"]["request_id"] == f"{ROOT_ID}/quick"
    for key in ("status", "attempts", "request_id", "cost_usd", "error"):
        assert key in snapshot["nodes"]["quick"]
    assert "slow" not in snapshot["nodes"] or snapshot["nodes"]["slow"]["status"] != "completed"
    assert result["status"] == "completed"
    assert final["state"] == "completed"


def test_replay_of_recorded_history_is_deterministic(graph_settings, tmp_path):
    require_temporal()

    async def go():
        from subagent.graph.workflow import GraphWorkflow
        from temporalio.worker import Replayer

        async with start_local(tmp_path) as env:
            ctx, gs = build_context(graph_settings, tmp_path / "t", fd(worked_script()),
                                    "rpl" + uuid.uuid4().hex[:6])
            async with Cluster(env, ctx, gs) as cluster:
                handle = await cluster.start(worked_example())
                answered = asyncio.create_task(cluster.answer_decisions(handle, "merge"))
                await asyncio.wait_for(handle.result(), 90)
                await answered
                history = await handle.fetch_history()
            await Replayer(workflows=[GraphWorkflow]).replay_workflow(history)
            return len(history.events)

    (tmp_path / "t").mkdir()
    assert asyncio.run(go()) > 10


def test_pause_stops_new_tasks_and_resume_continues(graph_settings, tmp_path):
    require_temporal()
    document = {"schema": "subagent.graph/1", "id": "pz", "version": 1, "body": [
        task("first"), task("second"), task("third")]}
    dispatcher = fd(default=FakeStep(cost_usd=0.1, sleep_s=0.5))

    async def go():
        async with start_local(tmp_path) as env:
            ctx, gs = build_context(graph_settings, tmp_path / "t", dispatcher,
                                    "pse" + uuid.uuid4().hex[:6])
            async with Cluster(env, ctx, gs) as cluster:
                handle = await cluster.start(document)
                await handle.signal("pause", "test pause")
                deadline = time.monotonic() + 30
                snap = await handle.query("status")
                while snap["state"] != "paused" and time.monotonic() < deadline:
                    await asyncio.sleep(0.2)
                    snap = await handle.query("status")
                started_when_paused = len(dispatcher.calls)
                await asyncio.sleep(2.0)
                still = await handle.query("status")
                after_wait = len(dispatcher.calls)
                await handle.signal("resume")
                result = await asyncio.wait_for(handle.result(), 60)
                return snap, still, started_when_paused, after_wait, result

    (tmp_path / "t").mkdir()
    snap, still, before, after, result = asyncio.run(go())
    assert snap["state"] == "paused" and snap["paused"] is True
    assert snap["pause_reason"] == "test pause"
    assert still["state"] == "paused"
    assert after == before, "a paused graph must not dispatch new tasks"
    assert result["status"] == "completed"
    assert len(result["nodes"]) == 3
