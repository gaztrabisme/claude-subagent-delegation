"""An owner_decision waits without holding a process; answers come from SQLite (SPEC 6, 12.2 d)."""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import make_settings

pytest.importorskip("temporalio")

from .conftest import in_process_worker, local_temporal, until  # noqa: E402

DOC = {
    "schema": "subagent.graph/1", "id": "od-demo", "version": 1,
    "body": [{"id": "ask", "kind": "owner_decision", "question": "Merge?",
              "options": ["merge", "hold"], "default": "hold"}],
    "output": {"answer": "nodes.ask.output.answer"},
}


def _setup(env, tmp_path):
    from subagent.graph.server import GraphSettings

    settings = make_settings(tmp_path)
    gsettings = GraphSettings(
        state_dir=tmp_path / "state",
        address=env.client.service_client.config.target_host,
        autostart=False, admit_poll_s=0.2,
    )
    return settings, gsettings


async def _wait_for_open_exception(service, root):
    async def check():
        res = await service.exceptions(root_run_id=root)
        return res["exceptions"][0] if res.get("ok") and res["exceptions"] else None

    return await until(check, timeout=30, what="the owner_decision exception")


async def _pending_activities(env, root):
    desc = await env.client.get_workflow_handle(root).describe()
    return list(desc.raw_description.pending_activities)


def test_owner_decision_waits_idle_and_resumes_only_on_a_stored_answer(tmp_path):
    async def scenario():
        from subagent.graph.dispatch import FakeDispatcher
        from subagent.graph.service import GraphService

        async with local_temporal(tmp_path) as env:
            settings, gsettings = _setup(env, tmp_path)
            service = GraphService(settings, gsettings=gsettings)
            async with in_process_worker(settings, gsettings, FakeDispatcher()):
                started = await service.start(DOC, request_id="od-1")
                assert started["ok"] is True, started
                root = started["root_run_id"]
                exc = await _wait_for_open_exception(service, root)
                assert exc["kind"] == "owner_decision"
                assert exc["needs_answer"] is True
                assert exc["options"] == ["merge", "hold"]

                async def idle():
                    return not await _pending_activities(env, root)

                await until(idle, timeout=10, what="no pending activities while waiting")
                state = (await service.status(root))["state"]
                assert state == "waiting"

                # A forged signal carries only an id; with no stored answer it must do nothing.
                handle = env.client.get_workflow_handle(root)
                await handle.signal("answer", exc["exception_id"])
                await asyncio.sleep(2.0)
                still = await service.status(root)
                assert still["state"] == "waiting"
                assert still["result"] is None
                open_now = await service.exceptions(root_run_id=root)
                assert [e["exception_id"] for e in open_now["exceptions"]] == [exc["exception_id"]]

                # The real path: SQLite first, then the signal.
                answered = await service.answer(exc["exception_id"], "merge", note="ok",
                                                answered_by="tester")
                assert answered["ok"] is True, answered
                result = await asyncio.wait_for(handle.result(), 45)
                assert result["status"] == "completed"
                assert result["output"]["answer"] == "merge"

                async def delivered():
                    res = await service.exceptions(root_run_id=root, state="all")
                    rec = res["exceptions"][0]
                    return rec if rec["state"] == "delivered" else None

                rec = await until(delivered, timeout=15, what="the answer to be marked delivered")
                assert rec["answer"] == "merge"
                assert rec["answered_by"] == "tester"

    asyncio.run(scenario())


def test_an_undelivered_answer_is_delivered_by_the_sweeper(tmp_path):
    async def scenario():
        from subagent.graph.dispatch import FakeDispatcher
        from subagent.graph.exceptions import ExceptionQueue
        from subagent.graph.service import GraphService
        from subagent.graph.store import Store

        async with local_temporal(tmp_path) as env:
            settings, gsettings = _setup(env, tmp_path)
            service = GraphService(settings, gsettings=gsettings)
            async with in_process_worker(settings, gsettings, FakeDispatcher()):
                started = await service.start(DOC, request_id="od-2")
                root = started["root_run_id"]
                exc = await _wait_for_open_exception(service, root)

                # Write the answer to SQLite only: no signal is sent by this process.
                queue = ExceptionQueue(Store(gsettings.state_dir / "graph.sqlite3"))
                queue.answer(exc["exception_id"], "hold", answered_by="tester")
                assert queue.get(exc["exception_id"]).state == "answered"
                assert [r.exception_id for r in queue.undelivered()] == [exc["exception_id"]]

                handle = env.client.get_workflow_handle(root)
                result = await asyncio.wait_for(handle.result(), 60)  # sweeper runs every 10 s
                assert result["status"] == "completed"
                assert result["output"]["answer"] == "hold"
                assert queue.get(exc["exception_id"]).state == "delivered"
                assert queue.undelivered() == []

    asyncio.run(scenario())
