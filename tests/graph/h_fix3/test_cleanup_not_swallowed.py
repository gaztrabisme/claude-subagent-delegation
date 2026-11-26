"""Full-hydrogen review fix2-ds check 3: cleanup failures are logged, not silently dropped."""

from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path

import pytest

from tests.conftest import make_settings

GRAPH = Path(__file__).resolve().parents[3] / "src" / "subagent" / "graph"

DOC = {
    "schema": "subagent.graph/1", "id": "od-cancel", "version": 1,
    "body": [{"id": "ask", "kind": "owner_decision", "question": "Merge?",
              "options": ["merge", "hold"], "default": "hold"}],
}


def _silent_blocks(path: Path, needle: str) -> list[int]:
    """Lines of `with suppress(Exception)` / `except Exception: pass` blocks mentioning needle."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    hits = []
    for node in ast.walk(tree):
        body_src = ast.get_source_segment(source, node) or ""
        if isinstance(node, ast.With):
            for item in node.items:
                call = item.context_expr
                if (isinstance(call, ast.Call) and ast.unparse(call.func).endswith("suppress")
                        and any(ast.unparse(a) == "Exception" for a in call.args)
                        and needle in body_src):
                    hits.append(node.lineno)
        elif (isinstance(node, ast.ExceptHandler) and node.type is not None
              and ast.unparse(node.type) == "Exception"
              and all(isinstance(s, ast.Pass) for s in node.body) and needle in body_src):
            hits.append(node.lineno)
    return hits


def test_owner_decision_cancel_close_is_not_silently_suppressed():
    lines = _silent_blocks(GRAPH / "workflow.py", "close_exception")
    assert not lines, f"workflow.py silently drops close_exception failures at lines {lines}"


def test_service_trace_write_is_not_silently_suppressed():
    lines = _silent_blocks(GRAPH / "service.py", "write_graph_exception")
    assert not lines, f"service.py silently drops trace-write failures at lines {lines}"


def test_a_failing_close_on_cancellation_is_logged_and_the_cancel_still_propagates(
        tmp_path, caplog, monkeypatch):
    pytest.importorskip("temporalio")
    from temporalio.client import WorkflowFailureError
    from temporalio.exceptions import ApplicationError

    from subagent.graph.exceptions import ExceptionQueue

    from ..d_exceptions.conftest import in_process_worker, local_temporal, until

    real_close = ExceptionQueue.close

    def failing_close(self, exception_id, reason):
        if reason == "cancelled":
            raise ApplicationError("close boom", non_retryable=True)
        return real_close(self, exception_id, reason)

    async def scenario():
        from subagent.graph.dispatch import FakeDispatcher
        from subagent.graph.server import GraphSettings
        from subagent.graph.service import GraphService

        settings = make_settings(tmp_path)
        async with local_temporal(tmp_path) as env:
            gsettings = GraphSettings(
                state_dir=tmp_path / "state",
                address=env.client.service_client.config.target_host,
                autostart=False, admit_poll_s=0.2)
            service = GraphService(settings, gsettings=gsettings)
            async with in_process_worker(settings, gsettings, FakeDispatcher()):
                started = await service.start(DOC, request_id="od-c1")
                root = started["root_run_id"]

                async def open_exc():
                    res = await service.exceptions(root_run_id=root)
                    return res["exceptions"][0] if res.get("ok") and res["exceptions"] else None

                await until(open_exc, timeout=30, what="the owner_decision exception")
                monkeypatch.setattr(ExceptionQueue, "close", failing_close)
                handle = env.client.get_workflow_handle(root)
                await handle.cancel()
                with pytest.raises(WorkflowFailureError):  # the cancellation still propagates
                    await asyncio.wait_for(handle.result(), 40)

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    ours = [r for r in caplog.records
            if r.levelno >= logging.WARNING
            and not r.name.startswith(("temporalio.activity", "temporalio.worker"))
            and any(w in r.getMessage().lower() for w in ("close", "exception", "cancel"))]
    assert ours, ("the failed close of the exception record on cancellation was dropped "
                  f"silently; warnings seen: {[(r.name, r.getMessage()) for r in caplog.records]}")
