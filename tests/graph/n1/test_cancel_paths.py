"""N1-2 and N1-3: cancellation closes the owner decision even on a second cancel, and a failing
`cancel_admission` is logged, not swallowed."""

from __future__ import annotations

import ast
import asyncio
import contextlib
import logging
import time
from pathlib import Path

import pytest

from tests.conftest import make_settings

pytest.importorskip("temporalio")


from ..d_exceptions.conftest import in_process_worker, local_temporal, until  # noqa: E402

GRAPH = Path(__file__).resolve().parents[3] / "src" / "subagent" / "graph"

OD_DOC = {
    "schema": "subagent.graph/1", "id": "n1-od", "version": 1,
    "body": [{"id": "ask", "kind": "owner_decision", "question": "Merge?",
              "options": ["merge", "hold"], "default": "hold"}],
}


def _gsettings(env, tmp_path, **extra):
    from subagent.graph.server import GraphSettings

    return GraphSettings(state_dir=tmp_path / "state",
                         address=env.client.service_client.config.target_host,
                         autostart=False, admit_poll_s=0.2, **extra)


def test_second_cancellation_still_closes_the_owner_decision(tmp_path, monkeypatch):
    from temporalio.client import WorkflowFailureError

    from subagent.graph.exceptions import ExceptionQueue

    real_close = ExceptionQueue.close

    def slow_close(self, exception_id, reason):
        if reason == "cancelled":
            time.sleep(2.0)  # the close is still in flight when the second cancel arrives
        return real_close(self, exception_id, reason)

    async def scenario():
        from subagent.graph.dispatch import FakeDispatcher
        from subagent.graph.service import GraphService

        settings = make_settings(tmp_path)
        async with local_temporal(tmp_path) as env:
            gsettings = _gsettings(env, tmp_path)
            service = GraphService(settings, gsettings=gsettings)
            async with in_process_worker(settings, gsettings, FakeDispatcher()):
                started = await service.start(OD_DOC, request_id="n1-od-1")
                root = started["root_run_id"]

                async def opened():
                    res = await service.exceptions(root_run_id=root)
                    return res["exceptions"][0] if res.get("ok") and res["exceptions"] else None

                exc = await until(opened, timeout=30, what="the owner_decision exception")
                monkeypatch.setattr(ExceptionQueue, "close", slow_close)
                handle = env.client.get_workflow_handle(root)
                await handle.cancel()
                await asyncio.sleep(0.7)  # the shielded close is running now
                with contextlib.suppress(Exception):  # the server may refuse a repeated request
                    await handle.cancel()
                with pytest.raises(WorkflowFailureError):
                    await asyncio.wait_for(handle.result(), 40)
                res = await service.exceptions(root_run_id=root, state="all")
                rec = next(e for e in res["exceptions"] if e["exception_id"] == exc["exception_id"])
                assert rec["state"] == "closed", (
                    f"the workflow ended with the owner decision still {rec['state']!r}")
                assert rec["closed_reason"] == "cancelled", rec

    asyncio.run(scenario())


QUEUE_DOC = {
    "schema": "subagent.graph/1", "id": "n1-queue", "version": 1, "budget_usd": 2.0,
    "defaults": {"provider": "glm", "reserve_usd": 0.2},
    "body": [{"id": "par", "kind": "parallel", "join": "allSettled", "branches": [
        {"id": "a", "kind": "task", "prompt": "a", "verification": "true"},
        {"id": "b", "kind": "task", "prompt": "b", "verification": "true"}]}],
}


def test_a_failing_cancel_admission_is_logged_and_the_task_still_ends_cancelled(
        tmp_path, caplog, monkeypatch):
    from temporalio.client import WorkflowFailureError
    from temporalio.exceptions import ApplicationError

    from subagent.graph.admission import Admission

    real_cancel = Admission.cancel
    failed: list[str] = []
    dispatched: set[str] = set()  # filled by the scenario: requests that have a running task

    def failing_cancel(self, request_id, attempt):
        if request_id in dispatched or failed:  # only the first cancel of the queued attempt
            return real_cancel(self, request_id, attempt)
        failed.append(request_id)
        raise ApplicationError("cancel boom", non_retryable=True)

    async def scenario():
        from subagent.graph.dispatch import FakeDispatcher, FakeStep
        from subagent.graph.service import GraphService

        settings = make_settings(tmp_path)
        async with local_temporal(tmp_path) as env:
            gsettings = _gsettings(env, tmp_path, lane_slots={"glm": 1})
            service = GraphService(settings, gsettings=gsettings)
            dispatcher = FakeDispatcher(default=FakeStep(sleep_s=3.0))
            async with in_process_worker(settings, gsettings, dispatcher):
                started = await service.start(QUEUE_DOC, request_id="n1-q-1")
                root = started["root_run_id"]

                async def one_running():
                    return len(dispatcher.calls) >= 1

                await until(one_running, timeout=30, what="the first task running")
                await asyncio.sleep(1.5)  # the second task is queued and polling
                dispatched.update(c.request_id for c in dispatcher.calls)
                monkeypatch.setattr(Admission, "cancel", failing_cancel)
                handle = env.client.get_workflow_handle(root)
                await handle.cancel()
                with contextlib.suppress(WorkflowFailureError):
                    await asyncio.wait_for(handle.result(), 45)
                status = await service.status(root)
                assert status["state"] == "cancelled", status

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    ours = [r for r in caplog.records
            if r.levelno >= logging.WARNING
            and not r.name.startswith(("temporalio.activity", "temporalio.worker"))
            and any(w in r.getMessage().lower() for w in ("cancel_admission", "admission"))]
    assert ours, ("the failed cancel_admission was dropped silently; warnings seen: "
                  f"{[(r.name, r.getMessage()) for r in caplog.records]}")
    assert any(r.name.startswith(("subagent.graph", "temporalio.workflow")) for r in ours), \
        [r.name for r in ours]


def test_no_suppress_exception_wraps_cancel_admission():
    path = GRAPH / "workflow.py"
    source = path.read_text(encoding="utf-8")
    hits = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.With):
            for item in node.items:
                call = item.context_expr
                if (isinstance(call, ast.Call) and ast.unparse(call.func).endswith("suppress")
                        and any(ast.unparse(a) in ("Exception", "BaseException")
                                for a in call.args)
                        and "cancel_admission" in (ast.get_source_segment(source, node) or "")):
                    hits.append(node.lineno)
        elif (isinstance(node, ast.ExceptHandler) and node.type is not None
              and ast.unparse(node.type) in ("Exception", "BaseException")
              and all(isinstance(s, ast.Pass) for s in node.body)
              and "cancel_admission" in (ast.get_source_segment(source, node) or "")):
            hits.append(node.lineno)
    assert not hits, f"workflow.py silently suppresses cancel_admission at lines {hits}"
