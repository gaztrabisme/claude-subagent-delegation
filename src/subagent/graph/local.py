"""LocalRuntime (spec 3.1): runs a graph in-process on a Dispatcher, with plain asyncio.

It lets the semantics be tested without Temporal and lets a project dry-run a graph on
FakeDispatcher. It keeps no durable state: a crash loses the run.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import ids
from .dispatch import Dispatcher, DispatchError, TaskOutcome, TaskRequest
from .interpreter import GraphInput, Interpreter, NodeResult, Scope, result_from_outcome
from .schema import GateNode, Graph, SubgraphNode, TaskNode, graph_hash, load_graph

_CANCEL_GRACE_S = 20.0


class LocalRuntime:
    def __init__(self, settings: Any, dispatcher: Dispatcher, *, ledger: Any = None,
                 admission: Any = None, exceptions: Any = None,
                 answers: Callable[[str], dict | None] | None = None,
                 poll_s: float = 1.0) -> None:
        self.settings = settings
        self.dispatcher = dispatcher
        self.ledger = ledger  # accepted for the later budget wiring; not used here
        self.admission = admission
        self.exceptions = exceptions
        self.answers = answers
        self.poll_s = poll_s
        self._graphs: dict[str, Graph] = {}

    # -- clock -----------------------------------------------------------------
    def now(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def wait_unpaused(self) -> None:
        return None

    # -- helpers ----------------------------------------------------------------
    def _workspace(self, value: str | None) -> Path:
        base = Path(self.settings.workspace)
        if not value:
            return base
        path = Path(value)
        return path if path.is_absolute() else base / path

    @staticmethod
    def _failed(node: Any, kind: str, error: str, status: str = "failed") -> NodeResult:
        return NodeResult(id=node.id, kind=kind, status=status, error=error)

    # -- tasks ------------------------------------------------------------------
    async def run_task(self, scope: Scope, node: TaskNode, prompt: str,
                       instructions: str | None) -> NodeResult:
        providers = getattr(self.settings, "providers", None)
        if providers is not None and node.provider not in providers:
            return self._failed(node, "task", f"unknown_provider: {node.provider!r}")
        workspace = self._workspace(node.workspace)
        if not workspace.is_dir():
            return self._failed(node, "task", f"workspace_missing: {workspace} does not exist")
        refusal = getattr(self.settings, "workspace_refusal", lambda _w: None)(workspace)
        if refusal:
            return self._failed(node, "task", f"workspace_refused: {refusal}")
        request = TaskRequest(
            request_id=scope.request_id, attempt=scope.attempt,
            attempt_id=ids.attempt_request_id(scope.request_id, scope.attempt),
            root_run_id=scope.root_run_id, run_path=scope.run_path, node_id=node.id,
            role=node.role, provider=node.provider or "", model=node.model, prompt=prompt,
            instructions=instructions, workspace=str(workspace), verification=node.verification,
            max_steps=node.max_steps, timeout_s=node.timeout_s)
        try:
            outcome = await self._dispatch(request)
        except DispatchError as err:
            return self._failed(node, "task", f"dispatch_error: {err}",
                                "interrupted" if err.retryable else "failed")
        return result_from_outcome(outcome, node.id)

    async def _dispatch(self, request: TaskRequest) -> TaskOutcome:
        loop = asyncio.get_running_loop()
        cancelled = threading.Event()
        future = loop.run_in_executor(
            None, lambda: self.dispatcher.run(request, heartbeat=lambda _d: None,
                                              cancelled=cancelled))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled.set()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(future), _CANCEL_GRACE_S)
            raise

    # -- gates --------------------------------------------------------------------
    async def run_gate_command(self, scope: Scope, node: GateNode) -> NodeResult:
        from .. import verify

        workspace = self._workspace(node.workspace)
        result = await asyncio.get_running_loop().run_in_executor(
            None, lambda: verify.run_verification(node.command or "", workspace, self.settings,
                                                  node.timeout_s))
        passed = bool(result.passed)
        return NodeResult(
            id=node.id, kind="gate", status="completed" if passed else "failed",
            output={"passed": passed, "reason": result.reason, "exit_code": result.exit_code,
                    "command": node.command, "output_tail": getattr(result, "output_tail", "")},
            error=None if passed else f"gate_failed: {result.reason}")

    # -- subgraphs ------------------------------------------------------------------
    async def run_subgraph(self, scope: Scope, node: SubgraphNode,
                           child: GraphInput) -> NodeResult:
        digest = graph_hash(child.doc)
        graph = self._graphs.get(digest)
        if graph is None:
            graph = self._graphs[digest] = load_graph(child.doc)
        result = await Interpreter(graph, self, child).run()
        return NodeResult(id=node.id, kind="subgraph", status=result.status,
                          output=result.output, error=result.error)

    # -- exceptions and decisions -------------------------------------------------------
    async def ask(self, scope: Scope, kind: str, summary: str, options: list[str],
                  default: str | None, timeout_s: float | None, detail: dict | None) -> dict:
        key = f"{scope.request_id}:{kind}"
        exception_id = ids.exception_id(key)
        if self.exceptions is not None:
            self.exceptions.raise_(
                key=key, root_run_id=scope.root_run_id or scope.run_path, run_path=scope.run_path,
                node_id=scope.node_id, kind=kind, summary=summary, detail=detail or {},
                options=options, default=default, needs_answer=True)
        if self.answers is None:  # a dry run: nobody to ask, so the default or nothing
            return {"answer": default, "note": None, "answered_by": "default",
                    "timed_out": True}
        started = self.now()
        while True:
            found = self.answers(exception_id)
            if found:
                return {"answer": found.get("answer"), "note": found.get("note"),
                        "answered_by": found.get("answered_by"), "timed_out": False}
            wait = self.poll_s
            if timeout_s is not None:
                remaining = timeout_s - (self.now() - started)
                if remaining <= 0:
                    return {"answer": default, "note": None, "answered_by": "timeout",
                            "timed_out": True}
                wait = min(wait, remaining)
            await self.sleep(wait)
