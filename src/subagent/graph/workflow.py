"""GraphWorkflow and TemporalRuntime (spec 3.2, 3.3): the interpreter running under Temporal.

Deterministic: no file, network, subprocess, env or SQLite access here. Every effect is an
activity called by name (they live in activities.py); time is `workflow.now()`; ids come from
`ids.py`. The graph document travels in the workflow input, so replays never re-read disk.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    CancelledError,
    ChildWorkflowError,
)
from temporalio.workflow import ActivityCancellationType, ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    from subagent.graph import ids
    from subagent.graph.dispatch import TaskOutcome, TaskRequest
    from subagent.graph.exceptions import route
    from subagent.graph.interpreter import (
        GraphInput,
        GraphResult,
        Interpreter,
        NodeResult,
        Scope,
        result_from_outcome,
    )
    from subagent.graph.schema import GateNode, SubgraphNode, TaskNode, graph_hash, load_graph

NAME = "subagent.graph."
WORKFLOW_NAME = NAME + "GraphWorkflow"
_INFRA = RetryPolicy(initial_interval=timedelta(seconds=1), maximum_interval=timedelta(seconds=30),
                     backoff_coefficient=2.0)
_TASK = RetryPolicy(initial_interval=timedelta(seconds=5), maximum_interval=timedelta(seconds=60),
                    backoff_coefficient=2.0, non_retryable_error_types=["GraphConfigError"])
_RECORD = RetryPolicy(maximum_attempts=3)
_HEARTBEAT = timedelta(seconds=30)
_REFUSAL_STATUS = {"budget_exhausted": "budget_exhausted", "cancelled": "cancelled"}


def _node_result(data: dict[str, Any]) -> NodeResult:
    names = ("id", "kind", "status", "attempts", "request_id", "output", "cost_usd", "error",
             "started_at", "finished_at")
    return NodeResult(**{k: data[k] for k in names if k in data})


def _is_cancel(exc: BaseException | None) -> bool:
    while exc is not None:
        if isinstance(exc, (CancelledError, asyncio.CancelledError)):
            return True
        exc = exc.__cause__
    return False


class TemporalRuntime:
    """The `Runtime` of the interpreter, implemented with activities, timers and signals."""

    def __init__(self, wf: GraphWorkflow, inp: GraphInput, graph_id: str, digest: str) -> None:
        self.wf = wf
        self.inp = inp
        self.graph_id = graph_id
        self.digest = digest
        self.graph_queue = workflow.info().task_queue
        self.prefix = self.graph_queue[: -len("-graph")] if self.graph_queue.endswith(
            "-graph") else self.graph_queue
        self.tasks_queue = f"{self.prefix}-tasks"
        self.queued: dict[str, None] = {}

    # -- plumbing ----
    async def _graph_act(self, name: str, *args: Any, timeout_s: float = 30.0,
                         retry: RetryPolicy = _INFRA) -> Any:
        return await workflow.execute_activity(
            NAME + name, args=list(args), task_queue=self.graph_queue,
            start_to_close_timeout=timedelta(seconds=timeout_s), retry_policy=retry)

    async def _record(self, kind: str, **fields: Any) -> None:
        with contextlib.suppress(ActivityError):  # a trace record never fails the run
            await self._graph_act("record", kind, fields, timeout_s=10.0, retry=_RECORD)

    async def record_node(self, inp: GraphInput, result: Any, scope: str) -> None:
        """One `graph_node` trace record per finished node (spec 10)."""
        await self._record(
            "graph_node", root_run_id=inp.root_run_id, run_path=inp.run_path,
            node_id=result.id, node_kind=result.kind, scope=scope, status=result.status,
            attempts=result.attempts, request_id=result.request_id, cost_usd=result.cost_usd,
            error=result.error)

    def now(self) -> float:
        return workflow.now().timestamp()

    async def sleep(self, seconds: float) -> None:
        await workflow.sleep(seconds)

    async def wait_unpaused(self) -> None:
        await workflow.wait_condition(lambda: not self.wf._paused)

    # -- tasks (spec 3.3) ----
    async def run_task(self, scope: Scope, node: TaskNode, prompt: str,
                       instructions: str | None) -> NodeResult:
        await self.wait_unpaused()
        attempt = scope.attempt
        attempt_id = ids.attempt_request_id(scope.request_id, attempt)
        admit = {"request_id": scope.request_id, "attempt": attempt,
                 "root_run_id": scope.root_run_id, "run_path": scope.run_path,
                 "node_id": node.id, "lane": node.provider or "", "priority": scope.priority,
                 "reserve_usd": node.reserve_usd, "deps": []}
        lease: dict[str, Any] | None = None
        try:
            while True:
                res = await self._graph_act("admit", admit)
                if res["status"] == "granted":
                    lease = res["lease"]
                    break
                if res["status"] == "refused":
                    return await self._refused(scope, node, str(res.get("reason") or "refused"))
                self.queued[attempt_id] = None
                await workflow.sleep(max(0.01, float(res.get("poll_s") or 2.0)))
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):  # cleanup must not mask the cancellation
                await self._graph_act("cancel_admission", scope.request_id, attempt)
            raise
        finally:
            self.queued.pop(attempt_id, None)
        assert lease is not None
        request = TaskRequest(
            request_id=scope.request_id, attempt=attempt, attempt_id=attempt_id,
            root_run_id=scope.root_run_id, run_path=scope.run_path, node_id=node.id,
            role=node.role, provider=node.provider or "", model=node.model, prompt=prompt,
            instructions=instructions, workspace=node.workspace or "",
            verification=node.verification, max_steps=node.max_steps, timeout_s=node.timeout_s)
        try:
            raw = await workflow.execute_activity(
                NAME + "run_task", {"lease_id": lease["lease_id"], "task": request.as_dict()},
                task_queue=self.tasks_queue,
                start_to_close_timeout=timedelta(seconds=node.timeout_s + 600),
                heartbeat_timeout=_HEARTBEAT, retry_policy=_TASK,
                cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED)
        except ActivityError as exc:
            if _is_cancel(exc):
                raise asyncio.CancelledError() from exc
            return NodeResult(id=node.id, kind="task", status="failed",
                              error=f"activity_error: {exc.cause or exc}")
        outcome = TaskOutcome.from_dict(raw)
        await self._raise_items(scope, node, outcome)
        return result_from_outcome(outcome, node.id)

    async def _refused(self, scope: Scope, node: TaskNode, reason: str) -> NodeResult:
        if reason in ("budget_exhausted", "lane_closed_timeout"):
            outcome = TaskOutcome(status=reason, lane=node.provider, error=reason)
        else:
            outcome = TaskOutcome(status="failed", lane=node.provider, error=reason)
        await self._raise_items(scope, node, outcome)
        status = _REFUSAL_STATUS.get(reason, "failed")
        return NodeResult(id=node.id, kind="task", status=status,
                          error=f"{reason}: admission refused the task")

    async def _raise_items(self, scope: Scope, node: TaskNode, outcome: TaskOutcome) -> None:
        for item in route(outcome, node, scope):
            if item["kind"] == "node_failed":
                continue  # the interpreter raises it through `ask` once retries are spent
            await self._graph_act("raise_exception", {
                **item, "root_run_id": scope.root_run_id, "run_path": scope.run_path})

    # -- gates ----
    async def run_gate_command(self, scope: Scope, node: GateNode) -> NodeResult:
        await self.wait_unpaused()
        timeout_s = float(node.timeout_s or 600.0)
        request = {"request_id": scope.request_id, "attempt": scope.attempt,
                   "run_path": scope.run_path, "node_id": node.id, "command": node.command or "",
                   "workspace": node.workspace or "", "timeout_s": timeout_s}
        try:
            raw = await workflow.execute_activity(
                NAME + "run_gate", request, task_queue=self.tasks_queue,
                start_to_close_timeout=timedelta(seconds=timeout_s + 60),
                heartbeat_timeout=_HEARTBEAT, retry_policy=_TASK)
        except ActivityError as exc:
            if _is_cancel(exc):
                raise asyncio.CancelledError() from exc
            return NodeResult(id=node.id, kind="gate", status="failed",
                              error=f"activity_error: {exc.cause or exc}")
        return _node_result(raw)

    # -- subgraphs ----
    async def run_subgraph(self, scope: Scope, node: SubgraphNode,
                           child: GraphInput) -> NodeResult:
        account = await self._graph_act("open_account", {
            "run_path": child.run_path, "parent_path": child.parent_path,
            "root_run_id": child.root_run_id, "node_id": node.id,
            "requested_usd": node.budget_usd, "pool": child.pool, "graph_id": self.graph_id,
            "graph_hash": self.digest})
        if "refused" in account:
            code = str(account["refused"])
            return NodeResult(id=node.id, kind="subgraph",
                              status="budget_exhausted" if code == "budget_exhausted"
                              else "failed", error=f"{code}: {account.get('message') or code}")
        handle = await workflow.start_child_workflow(
            WORKFLOW_NAME, child.as_dict(), id=child.run_path, task_queue=self.graph_queue,
            parent_close_policy=ParentClosePolicy.REQUEST_CANCEL)
        self.wf._children[child.run_path] = handle
        try:
            if self.wf._paused:
                await self.wf._signal_child(handle, "pause", self.wf._pause_reason)
            raw = await handle
        except ChildWorkflowError as exc:
            if _is_cancel(exc):
                raise asyncio.CancelledError() from exc
            return NodeResult(id=node.id, kind="subgraph", status="failed",
                              error=f"child_failed: {exc.cause or exc}")
        finally:
            self.wf._children.pop(child.run_path, None)
        return NodeResult(id=node.id, kind="subgraph", status=raw["status"],
                          output=raw.get("output") or {}, error=raw.get("error"))

    # -- exceptions and decisions ----
    async def ask(self, scope: Scope, kind: str, summary: str, options: list[str],
                  default: str | None, timeout_s: float | None, detail: dict | None) -> dict:
        started = self.now()
        key = f"{scope.request_id}:{kind}"
        xid = ids.exception_id(key)
        await self._graph_act("raise_exception", {
            "key": key, "root_run_id": scope.root_run_id or scope.run_path,
            "run_path": scope.run_path, "node_id": scope.node_id, "kind": kind,
            "summary": summary, "detail": detail or {}, "options": list(options),
            "default": default, "needs_answer": True})
        while True:
            seen = self.wf._pokes
            found = await self._graph_act("read_answer", xid)
            if found is not None:
                return {"answer": found.get("answer"), "note": found.get("note"),
                        "answered_by": found.get("answered_by"), "timed_out": False}
            remaining = None if timeout_s is None else timeout_s - (self.now() - started)
            if remaining is not None and remaining <= 0:
                await self._graph_act("close_exception", xid, "timeout")
                return {"answer": default, "note": None, "answered_by": "timeout",
                        "timed_out": True}
            with contextlib.suppress(TimeoutError):
                await workflow.wait_condition(lambda seen=seen: self.wf._pokes != seen,
                                              timeout=remaining)


@workflow.defn(name=WORKFLOW_NAME)
class GraphWorkflow:
    def __init__(self) -> None:
        self._paused = False
        self._pause_reason: str | None = None
        self._pokes = 0
        self._interp: Interpreter | None = None
        self._runtime: TemporalRuntime | None = None
        self._children: dict[str, Any] = {}
        self._run_path = ""

    # -- signals and query ----
    @workflow.signal(name="pause")
    async def pause(self, reason: str | None = None) -> None:
        self._paused = True
        self._pause_reason = reason
        if self._interp is not None:
            self._interp.set_paused(True, reason)
        for handle in list(self._children.values()):
            await self._signal_child(handle, "pause", reason)

    @workflow.signal(name="resume")
    async def resume(self) -> None:
        self._paused = False
        self._pause_reason = None
        if self._interp is not None:
            self._interp.set_paused(False)
        for handle in list(self._children.values()):
            await self._signal_child(handle, "resume")

    @workflow.signal(name="answer")
    def answer(self, exception_id: str) -> None:
        """A wake-up only: the answer itself is read from the ledger by `read_answer`."""
        self._pokes += 1

    @workflow.query(name="status")
    def status(self) -> dict:
        if self._interp is None:
            return {"run_path": self._run_path, "state": "paused" if self._paused else "running",
                    "paused": self._paused, "pause_reason": self._pause_reason, "nodes": {},
                    "running": [], "queued": [], "waiting_exceptions": [], "children": []}
        snap = self._interp.snapshot()
        snap["queued"] = list(self._runtime.queued) if self._runtime is not None else []
        return snap

    @staticmethod
    async def _signal_child(handle: Any, name: str, *args: Any) -> None:
        with contextlib.suppress(Exception):  # a child that already ended needs no signal
            await handle.signal(name, *args)

    # -- run ----
    @workflow.run
    async def run(self, inp: dict) -> dict:
        gi = GraphInput.from_dict(inp)
        self._run_path = gi.run_path
        graph = load_graph(gi.doc)
        digest = graph_hash(gi.doc)
        runtime = TemporalRuntime(self, gi, graph.id, digest)
        interp = Interpreter(graph, runtime, gi)
        self._runtime, self._interp = runtime, interp
        if self._paused:
            interp.set_paused(True, self._pause_reason)
        is_root = gi.parent_path is None
        cap_usd = 0.0
        if is_root:
            account = await runtime._graph_act("open_account", {
                "run_path": gi.run_path, "parent_path": None, "root_run_id": gi.root_run_id,
                "node_id": None, "requested_usd": graph.budget_usd, "pool": gi.pool,
                "graph_id": graph.id, "graph_hash": digest})
            if "refused" in account:
                code = str(account["refused"])
                interp.state = "budget_exhausted" if code == "budget_exhausted" else "failed"
                await runtime._graph_act("root_state", gi.root_run_id, interp.state)
                return GraphResult(gi.run_path, interp.state, {}, {},
                                   f"{code}: {account.get('message') or code}").as_dict()
            cap_usd = float(account["cap_usd"])
            await runtime._graph_act("root_state", gi.root_run_id, "running")
        await runtime._record(
            "graph_run", root_run_id=gi.root_run_id, run_path=gi.run_path, graph_id=graph.id,
            graph_hash=digest, event="started", state="running", depth=gi.depth,
            cap_usd=cap_usd, spent_usd=0.0)
        result: GraphResult | None = None
        final_state = "cancelled"
        try:
            try:
                result = await interp.run()
                final_state = result.status
            except asyncio.CancelledError:
                raise
            except ApplicationError:
                raise
            except Exception as exc:  # noqa: BLE001 - an interpreter fault ends the run, not loops it
                interp.state = "failed"
                final_state = "failed"
                result = GraphResult(gi.run_path, "failed", dict(interp._all), {},
                                     f"internal: {type(exc).__name__}: {exc}")
        finally:
            async def cleanup() -> None:
                closed = await runtime._graph_act("close_account", gi.run_path, "closed")
                if is_root:
                    await runtime._graph_act("root_state", gi.root_run_id, final_state)
                await runtime._record(
                    "graph_run", root_run_id=gi.root_run_id, run_path=gi.run_path,
                    graph_id=graph.id, graph_hash=digest, event="finished", state=final_state,
                    depth=gi.depth, cap_usd=float(closed.get("cap_usd", 0.0)),
                    spent_usd=float(closed.get("spent_usd", 0.0)))

            # A cancelled workflow still closes its account: the cleanup runs as its own
            # task and a further cancellation only waits for it.
            cleanup_task = asyncio.ensure_future(cleanup())
            while not cleanup_task.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(cleanup_task)
            cleanup_task.result()
        assert result is not None
        return result.as_dict()
