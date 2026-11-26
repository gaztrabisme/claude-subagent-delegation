"""The graph interpreter (spec 3.1): sequencing, joins, loops, retries and ids.

Pure and deterministic: no I/O, no clock but `runtime.now()`, no randomness. Every effect goes
through the `Runtime` protocol, so one interpreter runs on LocalRuntime and under Temporal.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections import ChainMap
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import expr, ids
from .dispatch import TaskOutcome
from .schema import (
    GateNode,
    Graph,
    LoopNode,
    Node,
    OwnerDecisionNode,
    ParallelNode,
    SubgraphNode,
    TaskNode,
    build_body,
)

STATUSES = ("completed", "failed", "skipped", "not_run", "cancelled", "budget_exhausted",
            "interrupted")
OK = ("completed", "skipped")


@dataclass
class NodeResult:
    id: str
    kind: str
    status: str
    attempts: int = 0
    request_id: str | None = None
    output: dict[str, Any] = field(default_factory=dict)
    cost_usd: float | None = None
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "status": self.status,
                "attempts": self.attempts, "request_id": self.request_id,
                "output": dict(self.output), "cost_usd": self.cost_usd, "error": self.error,
                "started_at": self.started_at, "finished_at": self.finished_at}


@dataclass
class GraphInput:
    doc: dict[str, Any]
    inputs: dict[str, Any]
    root_run_id: str
    run_path: str
    parent_path: str | None
    depth: int
    priority: int = 3
    pool: str = "default"
    body_ref: str | None = None
    inline_body: list[dict] | None = None
    inline_output: dict[str, str] | None = None  # additive: the output map of an inline body
    cap_usd: float | None = None  # additive: a child's granted cap, for its trace record

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> GraphInput:
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


@dataclass
class GraphResult:
    run_path: str
    status: str
    nodes: dict[str, dict[str, Any]]
    output: dict[str, Any]
    error: str | None

    def as_dict(self) -> dict[str, Any]:
        return {"run_path": self.run_path, "status": self.status, "nodes": dict(self.nodes),
                "output": dict(self.output), "error": self.error}


@dataclass(frozen=True)
class Scope:
    run_path: str
    node_id: str
    scope: str
    request_id: str
    priority: int
    root_run_id: str = ""  # additive
    attempt: int = 1  # additive: 1-based attempt of a task or gate


class Runtime(Protocol):
    def now(self) -> float: ...

    async def run_task(self, scope: Scope, node: TaskNode, prompt: str,
                       instructions: str | None) -> NodeResult: ...

    async def run_gate_command(self, scope: Scope, node: GateNode) -> NodeResult: ...

    async def run_subgraph(self, scope: Scope, node: SubgraphNode,
                           child: GraphInput) -> NodeResult: ...

    async def ask(self, scope: Scope, kind: str, summary: str, options: list[str],
                  default: str | None, timeout_s: float | None,
                  detail: dict | None) -> dict: ...

    async def wait_unpaused(self) -> None: ...

    async def sleep(self, seconds: float) -> None: ...


_OUTCOME_STATUS = {"completed": "completed", "unverified": "failed", "failed": "failed",
                   "interrupted": "interrupted", "lane_refused": "failed",
                   "cancelled": "cancelled"}


def result_from_outcome(outcome: TaskOutcome, node_id: str) -> NodeResult:
    """The node result a finished task attempt maps to (spec 3.3 step 5)."""
    output: dict[str, Any] = {
        "result_text": outcome.result_text, "result_json": outcome.result_json,
        "verdict": outcome.verdict, "run_id": outcome.run_id, "agent_id": outcome.agent_id,
        "lane": outcome.lane, "verification": outcome.verification, "usage": outcome.usage,
        "cost_kind": outcome.cost_kind, "guard_denials": outcome.guard_denials,
        "test_change_request": outcome.test_change_request, "needs_human": outcome.needs_human,
        "replayed": outcome.replayed}
    if outcome.status == "unverified":
        output["unverified"] = True
    if outcome.status == "lane_refused":
        output["lane_refused"] = True
    status = _OUTCOME_STATUS.get(outcome.status, "failed")
    error = None if status == "completed" else (outcome.error or outcome.status)
    return NodeResult(id=node_id, kind="task", status=status, output=output,
                      cost_usd=outcome.cost_usd, error=error)


def _retry_key(result: NodeResult) -> str | None:
    if result.output.get("unverified"):
        return "unverified"
    if result.output.get("lane_refused"):
        return "lane_refused"
    if result.status in ("interrupted", "failed"):
        return result.status
    return None


@dataclass(frozen=True)
class _Ctx:
    nodes: MutableMapping[str, Any]
    enclosing: tuple[tuple[str, int], ...] = ()
    fe: tuple[tuple[str, int], ...] = ()
    item: Mapping[str, Any] = field(default_factory=dict)
    loop: Mapping[str, Any] = field(default_factory=dict)


class Interpreter:
    def __init__(self, graph: Graph, runtime: Runtime, inp: GraphInput) -> None:
        self.graph = graph
        self.rt = runtime
        self.inp = inp
        self.inputs = inp.inputs
        self.state = "running"
        self.paused = False
        self.pause_reason: str | None = None
        self._root_nodes: dict[str, Any] = {}
        self._all: dict[str, dict[str, Any]] = {}
        self._running: dict[str, None] = {}
        self._waiting: dict[str, None] = {}
        self._children: list[str] = []

    # -- public ------------------------------------------------------------------
    async def run(self) -> GraphResult:
        nodes, output_map = self._root()
        try:
            status, error = await self._run_body(nodes, _Ctx(nodes=self._root_nodes))
        except asyncio.CancelledError:
            self.state = "cancelled"
            raise
        scope = self._expr_scope(_Ctx(nodes=self._root_nodes))
        output = {key: (None if (v := expr.resolve(path, scope)) is expr.MISSING else v)
                  for key, path in output_map.items()}
        self.state = status
        return GraphResult(self.inp.run_path, status, dict(self._all), output, error)

    def set_paused(self, paused: bool, reason: str | None = None) -> None:
        self.paused = paused
        self.pause_reason = reason if paused else None

    def snapshot(self) -> dict[str, Any]:
        state = self.state
        if state == "running":
            state = "waiting" if self._waiting else ("paused" if self.paused else "running")
        keys = ("status", "attempts", "request_id", "cost_usd", "error")
        return {"run_path": self.inp.run_path, "state": state, "paused": self.paused,
                "pause_reason": self.pause_reason,
                "nodes": {k: {key: v[key] for key in keys} for k, v in self._all.items()},
                "running": list(self._running), "queued": [],
                "waiting_exceptions": list(self._waiting), "children": list(self._children)}

    # -- structure -----------------------------------------------------------------
    def _root(self) -> tuple[tuple[Node, ...], Mapping[str, str]]:
        inp, graph = self.inp, self.graph
        if inp.inline_body is not None:
            return (build_body(inp.inline_body, graph.defaults, graph.workspace),
                    inp.inline_output or {})
        if inp.body_ref is not None:
            named = graph.graphs[inp.body_ref]
            return named.body, named.output
        return graph.body, graph.output

    def _expr_scope(self, ctx: _Ctx) -> dict[str, Any]:
        return {"nodes": ctx.nodes, "inputs": self.inputs, "item": ctx.item, "loop": ctx.loop}

    async def _record(self, result: NodeResult, ctx: _Ctx) -> None:
        view = result.as_dict()
        ctx.nodes[result.id] = view
        scope = ids.node_scope(result.id, ctx.fe)
        self._all[scope] = view
        hook = getattr(self.rt, "record_node", None)
        if hook is not None:
            await hook(self.inp, result, scope)

    def _scope(self, node: Node, scope: str, request_id: str, attempt: int = 1) -> Scope:
        priority = getattr(node, "priority", None)
        if priority is None or priority == 3:
            priority = self.inp.priority
        return Scope(run_path=self.inp.run_path, node_id=node.id, scope=scope,
                     request_id=request_id, priority=priority,
                     root_run_id=self.inp.root_run_id, attempt=attempt)

    # -- bodies and nodes ----------------------------------------------------------------
    async def _run_body(self, nodes: tuple[Node, ...], ctx: _Ctx) -> tuple[str, str | None]:
        for index, node in enumerate(nodes):
            result = await self._exec(node, ctx)
            if result.status in OK or node.on_failure == "continue":
                continue
            for rest in nodes[index + 1:]:
                await self._record(NodeResult(id=rest.id, kind=rest.kind, status="not_run"), ctx)
            status = result.status if result.status in ("budget_exhausted", "cancelled") \
                else "failed"
            return status, f"node {node.id} {result.status}: {result.error}"
        return "completed", None

    async def _exec(self, node: Node, ctx: _Ctx) -> NodeResult:
        retry = 0
        while True:
            result = await self._run_one(node, ctx, retry)
            await self._record(result, ctx)
            if (result.status in OK or result.status == "cancelled"
                    or node.on_failure != "exception"):
                return result
            sc = self._scope(node, ids.node_scope(node.id, ctx.enclosing, retry),
                             result.request_id or "")
            key = f"{sc.request_id}:node_failed"
            self._waiting[ids.exception_id(key)] = None
            try:
                answer = (await self.rt.ask(
                    sc, "node_failed", f"node {node.id} {result.status}: {result.error}",
                    ["retry", "skip", "abort"], None, None,
                    {"node_id": node.id, "status": result.status, "error": result.error,
                     "scope": sc.scope})).get("answer")
            finally:
                self._waiting.pop(ids.exception_id(key), None)
            if answer == "retry":
                retry += 1
                continue
            if answer == "skip":
                result = dataclasses.replace(result, status="skipped")
                await self._record(result, ctx)
            return result

    async def _run_one(self, node: Node, ctx: _Ctx, retry: int) -> NodeResult:
        scope = ids.node_scope(node.id, ctx.enclosing, retry)
        request_id = ids.task_request_id(self.inp.run_path, scope)
        started = self.rt.now()
        sc = self._scope(node, scope, request_id)
        try:
            if node.when is not None and not expr.evaluate(node.when, self._expr_scope(ctx)):
                result = NodeResult(id=node.id, kind=node.kind, status="skipped")
            else:
                self._running[scope] = None
                try:
                    result = await getattr(self, f"_{node.kind}")(node, ctx, sc)
                finally:
                    self._running.pop(scope, None)
        except expr.ExprError as err:
            result = NodeResult(id=node.id, kind=node.kind, status="failed",
                                error=f"{err.code}: {err}")
        result.request_id = request_id
        result.started_at, result.finished_at = started, self.rt.now()
        return result

    async def _task(self, node: TaskNode, ctx: _Ctx, sc: Scope) -> NodeResult:
        scope = self._expr_scope(ctx)
        prompt = expr.render(node.prompt, scope)
        instructions = (expr.render(node.instructions, scope)
                        if node.instructions is not None else None)
        attempts, cost = 0, None
        while True:
            attempts += 1
            result = await self.rt.run_task(dataclasses.replace(sc, attempt=attempts), node,
                                            prompt, instructions)
            if result.cost_usd is not None:
                cost = (cost or 0.0) + result.cost_usd
            if _retry_key(result) in node.retry_on and attempts <= node.retries:
                if node.retry_backoff_s > 0:
                    await self.rt.sleep(node.retry_backoff_s)
                continue
            break
        result.attempts, result.cost_usd = attempts, cost
        return result

    async def _gate(self, node: GateNode, ctx: _Ctx, sc: Scope) -> NodeResult:
        if node.condition is not None:
            passed = expr.evaluate(node.condition, self._expr_scope(ctx))
            return NodeResult(id=node.id, kind="gate", status="completed" if passed else "failed",
                              attempts=1, output={"passed": passed},
                              error=None if passed else "condition_false")
        attempts = 0
        while True:
            attempts += 1
            result = await self.rt.run_gate_command(
                dataclasses.replace(sc, attempt=attempts), node)
            if result.status == "completed" or attempts > node.retries:
                result.attempts = attempts
                return result

    async def _owner_decision(self, node: OwnerDecisionNode, ctx: _Ctx, sc: Scope) -> NodeResult:
        question = expr.render(node.question, self._expr_scope(ctx))
        options = list(node.options)
        xid = ids.exception_id(f"{sc.request_id}:owner_decision")
        self._waiting[xid] = None
        try:
            ans = await self.rt.ask(sc, "owner_decision", question, options, node.default,
                                    node.timeout_s, {"node_id": node.id})
        finally:
            self._waiting.pop(xid, None)
        answer = ans.get("answer")
        if answer is None:
            return NodeResult(id=node.id, kind="owner_decision", status="failed", attempts=1,
                              error="decision_timeout: no answer before the timeout")
        if answer not in options:
            return NodeResult(id=node.id, kind="owner_decision", status="failed", attempts=1,
                              error=f"invalid_answer: {answer!r} is not one of {options}")
        output = {"answer": answer, "note": ans.get("note"),
                  "answered_by": ans.get("answered_by")}
        if ans.get("timed_out"):
            output["timed_out"] = True
        return NodeResult(id=node.id, kind="owner_decision", status="completed", attempts=1,
                          output=output)

    async def _loop(self, node: LoopNode, ctx: _Ctx, sc: Scope) -> NodeResult:
        for iteration in range(node.max_iterations):
            inner = dataclasses.replace(
                ctx, enclosing=ctx.enclosing + ((node.id, iteration),),
                loop={**ctx.loop, node.id: {"iteration": iteration}})
            status, error = await self._run_body(node.body, inner)
            if status != "completed":
                return NodeResult(id=node.id, kind="loop", status=status, error=error,
                                  output={"iterations": iteration + 1})
            if expr.evaluate(node.until, self._expr_scope(inner)):
                return NodeResult(id=node.id, kind="loop", status="completed",
                                  output={"iterations": iteration + 1})
        return NodeResult(id=node.id, kind="loop", status="failed",
                          error=f"loop_exhausted: until not met in {node.max_iterations} "
                                "iterations",
                          output={"iterations": node.max_iterations})

    def _render_value(self, value: Any, scope: Mapping[str, Any]) -> Any:
        if isinstance(value, str):
            return expr.render(value, scope)
        if isinstance(value, list):
            return [self._render_value(v, scope) for v in value]
        if isinstance(value, dict):
            return {k: self._render_value(v, scope) for k, v in value.items()}
        return value

    async def _subgraph(self, node: SubgraphNode, ctx: _Ctx, sc: Scope) -> NodeResult:
        scope = self._expr_scope(ctx)
        merged: dict[str, Any] = {}
        if node.graph is not None:
            merged.update(self.graph.graphs[node.graph].inputs)
        merged.update(self._render_value(dict(node.inputs), scope))
        inline = node.raw_body is not None
        child_path = ids.child_run_path(self.inp.run_path, sc.scope)
        child = GraphInput(
            doc=self.inp.doc, inputs=merged, root_run_id=self.inp.root_run_id,
            run_path=child_path, parent_path=self.inp.run_path, depth=self.inp.depth + 1,
            priority=node.priority or self.inp.priority, pool=self.inp.pool,
            body_ref=node.graph, inline_body=[dict(n) for n in node.raw_body] if inline else None,
            inline_output=dict(node.output) if inline else None)
        self._children.append(child_path)
        result = await self.rt.run_subgraph(sc, node, child)
        result.attempts = 1
        return result

    # -- parallel ---------------------------------------------------------------------
    async def _parallel(self, node: ParallelNode, ctx: _Ctx, sc: Scope) -> NodeResult:
        branches: list[tuple[str, Node, _Ctx]] = []
        if node.for_each is not None:
            spec = node.for_each
            items = expr.resolve(spec.items, self._expr_scope(ctx))
            if not isinstance(items, list):
                return NodeResult(id=node.id, kind="parallel", status="failed",
                                  error=f"for_each_not_list: {spec.items!r} is not a list")
            if len(items) > spec.max_items:
                return NodeResult(id=node.id, kind="parallel", status="failed",
                                  error=f"for_each_too_many: {len(items)} items; the limit is "
                                        f"{spec.max_items}")
            for index, item in enumerate(items):
                branches.append((str(index), spec.node, dataclasses.replace(
                    ctx, nodes=ChainMap({}, ctx.nodes),
                    enclosing=ctx.enclosing + ((node.id, index),),
                    fe=ctx.fe + ((node.id, index),), item={**ctx.item, spec.as_: item})))
        else:
            branches = [(b.id, b, ctx) for b in node.branches]
        tasks = [asyncio.ensure_future(self._exec(b, c)) for _, b, c in branches]
        hit = await self._join(node.join, tasks)
        statuses: dict[str, str] = {}
        for (key, branch, bctx), task in zip(branches, tasks, strict=True):
            if task.cancelled():
                result = NodeResult(id=branch.id, kind=branch.kind, status="cancelled")
                await self._record(result, bctx)
                statuses[key] = "cancelled"
            else:
                statuses[key] = task.result().status
        counts = list(statuses.values())
        cancelled = counts.count("cancelled")
        output: dict[str, Any] = {
            "completed": counts.count("completed"),
            "failed": len([s for s in counts if s not in (*OK, "cancelled")]),
            "branches": statuses}
        if cancelled:
            output["cancelled"] = cancelled
        status, error = "completed", None
        if node.join == "failFast" and hit is not None:
            status, error = "failed", f"branch {branches[hit][0]} {statuses[branches[hit][0]]}"
        elif node.join == "any":
            output["winner"] = branches[hit][0] if hit is not None else None
            if hit is None:
                status, error = "failed", "no branch completed"
        return NodeResult(id=node.id, kind="parallel", status=status, error=error,
                          output=output)

    @staticmethod
    async def _join(join: str, tasks: list[asyncio.Future]) -> int | None:
        """Wait per the join policy; cancel the rest once decided. Returns the deciding index."""
        hit: int | None = None
        try:
            if join == "allSettled":
                await asyncio.gather(*tasks)
                return None
            pending = list(tasks)  # ordered: cancellation order must not depend on hashes
            while pending and hit is None:
                done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                pending = [task for task in pending if task not in done]
                for index, task in enumerate(tasks):
                    if task in done:
                        status = task.result().status
                        if (join == "failFast" and status not in OK) or (
                                join == "any" and status == "completed"):
                            hit = index
                            break
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            return hit
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
