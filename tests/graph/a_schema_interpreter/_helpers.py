"""Helpers for the interpreter tests: run a graph on LocalRuntime + FakeDispatcher."""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any

ROOT_ID = "gr-000000000001"


def task(node_id: str, **extra: Any) -> dict[str, Any]:
    node = {"id": node_id, "kind": "task", "provider": "codex", "prompt": f"do {node_id}",
            "verification": "true", "retry_backoff_s": 0}
    node.update(extra)
    return node


def doc(body: list[dict[str, Any]], **top: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"schema": "subagent.graph/1", "id": "g1", "version": 1, "body": body}
    out.update(top)
    return out


def make_runtime(settings, dispatcher, *, answers=None, exceptions=None, clock=None):
    """A LocalRuntime whose sleeps are recorded, not waited, and whose clock follows them."""
    from subagent.graph.local import LocalRuntime

    class FastRuntime(LocalRuntime):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.sleeps: list[float] = []
            self._clock = 1_000_000.0

        def now(self) -> float:
            return self._clock

        async def sleep(self, seconds: float) -> None:
            self.sleeps.append(seconds)
            self._clock += seconds
            await asyncio.sleep(0)

    return FastRuntime(settings, dispatcher, exceptions=exceptions, answers=answers)


def graph_input(document: dict[str, Any], inputs: dict[str, Any] | None = None):
    from subagent.graph.interpreter import GraphInput
    from subagent.graph.schema import load_graph

    graph = load_graph(copy.deepcopy(document))
    merged = dict(graph.inputs)
    merged.update(inputs or {})
    return graph, GraphInput(doc=copy.deepcopy(document), inputs=merged, root_run_id=ROOT_ID,
                             run_path=ROOT_ID, parent_path=None, depth=0)


def run_graph(settings, document, dispatcher, *, inputs=None, answers=None, exceptions=None,
              runtime_factory=None, timeout: float = 30.0):
    """Run `document` and return (GraphResult, runtime, interpreter)."""
    from subagent.graph.interpreter import Interpreter

    graph, inp = graph_input(document, inputs)
    if runtime_factory is None:
        runtime = make_runtime(settings, dispatcher, answers=answers, exceptions=exceptions)
    else:
        runtime = runtime_factory(settings, dispatcher, answers, exceptions)
    interp = Interpreter(graph, runtime, inp)
    result = asyncio.run(asyncio.wait_for(interp.run(), timeout))
    return result, runtime, interp


def make_queue(tmp_path: Path):
    from subagent.graph.exceptions import ExceptionQueue
    from subagent.graph.store import Store

    return ExceptionQueue(Store(tmp_path / "ex.sqlite3"))


def answer_with(answer: str, note: str | None = None, by: str = "test"):
    def answers(_exception_id: str):
        return {"answer": answer, "note": note, "answered_by": by}

    return answers


def request_ids(dispatcher) -> list[str]:
    return [c.request_id for c in dispatcher.calls]


def node_status(result, node_id: str) -> str:
    return result.nodes[node_id]["status"]
