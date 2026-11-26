"""The nine tools against a real (dev) Temporal server and an in-process FakeDispatcher worker."""

from __future__ import annotations

import asyncio
import re

import pytest

from tests.conftest import make_settings

pytest.importorskip("temporalio")

from .conftest import (  # noqa: E402
    FakeApp,
    call,
    in_process_worker,
    local_temporal,
    until,
)

ISO_Z = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z")


def _doc(workspace: str) -> dict:
    return {
        "schema": "subagent.graph/1", "id": "demo", "version": 1, "workspace": workspace,
        "inputs": {"label": "x"}, "budget_usd": 2.0,
        "defaults": {"provider": "glm", "reserve_usd": 0.2},
        "body": [
            {"id": "t1", "kind": "task", "prompt": "one {{ inputs.label }}", "verification": "true"},
            {"id": "t2", "kind": "task", "prompt": "two", "verification": "true"},
            {"id": "ask", "kind": "owner_decision", "question": "Go?", "options": ["go", "stop"]},
        ],
        "output": {"answer": "nodes.ask.output.answer"},
    }


def _only_ask(graph_id: str) -> dict:
    return {
        "schema": "subagent.graph/1", "id": graph_id, "version": 1,
        "body": [{"id": "ask", "kind": "owner_decision", "question": "Go?",
                  "options": ["go", "stop"]}],
    }


async def _boot(env, tmp_path):
    from subagent.graph.dispatch import FakeDispatcher, FakeStep
    from subagent.graph.mcp_tools import register
    from subagent.graph.server import GraphSettings
    from subagent.graph.service import GraphService

    settings = make_settings(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    gsettings = GraphSettings(
        state_dir=tmp_path / "state",
        address=env.client.service_client.config.target_host,
        autostart=False, admit_poll_s=0.2, budget_usd=10.0,
        lane_slots={"glm": 2}, pools={"small": 1.0},
    )
    service = GraphService(settings, gsettings=gsettings)
    app = FakeApp()
    register(app, service_factory=lambda: service)
    dispatcher = FakeDispatcher(script={"t1": [FakeStep(sleep_s=2.0, cost_usd=0.05)]},
                                default=FakeStep(cost_usd=0.05))

    async def tool(name, **kwargs):
        return await call(app.tools[name], **kwargs)

    return settings, gsettings, dispatcher, tool, str(workspace)


def _error(result, code):
    assert result["ok"] is False, result
    assert result["error"]["code"] == code, result
    assert result["error"]["message"]


def test_tool_lifecycle_fields_and_error_codes(tmp_path):
    async def scenario():
        from subagent.graph import graph_hash
        from subagent.graph.ids import new_root_run_id

        async with local_temporal(tmp_path) as env:
            settings, gsettings, dispatcher, tool, workspace = await _boot(env, tmp_path)
            doc = _doc(workspace)
            async with in_process_worker(settings, gsettings, dispatcher):
                # -- start ------------------------------------------------
                started = await tool("graph_start", graph=doc, inputs={"label": "hello"},
                                     request_id="life-1")
                assert started["ok"] is True, started
                root = started["root_run_id"]
                assert re.fullmatch(r"gr-[0-9a-f]{12}", root)
                assert root == new_root_run_id("life-1")
                assert started["workflow_id"] == root
                assert started["graph_id"] == "demo"
                assert started["graph_hash"] == graph_hash(doc)
                assert started["state"] == "running"
                assert started["attached"] is False
                assert started["budget"]["pool"] == "default"
                assert started["budget"]["cap_usd"] == pytest.approx(2.0)
                assert started["budget"]["capped"] is False
                assert set(started["budget"]) == {"pool", "cap_usd", "remaining_usd", "capped"}

                # -- same request id attaches; a different graph conflicts -------
                again = await tool("graph_start", graph=doc, inputs={"label": "hello"},
                                   request_id="life-1")
                assert again["ok"] is True and again["attached"] is True
                assert again["root_run_id"] == root
                other = {**doc, "version": 2}
                _error(await tool("graph_start", graph=other, inputs={"label": "hello"},
                                  request_id="life-1"), "conflict")
                clash = await tool("graph_start", graph=other, inputs={"label": "hello"},
                                   request_id="life-1")
                assert "request_id reused with a different graph" in clash["error"]["message"]
                _error(await tool("graph_start", graph=doc, inputs={"label": "different"},
                                  request_id="life-1"), "conflict")

                # -- pause / resume ---------------------------------------
                paused = await tool("graph_pause", root_run_id=root, reason="hold on")
                assert paused == {"ok": True, "root_run_id": root, "state": "paused",
                                  "accepted": True}

                async def is_paused():
                    res = await tool("graph_status", root_run_id=root)
                    return res if res["paused"] and res["state"] == "paused" else None

                await until(is_paused, timeout=20, what="state paused")
                resumed = await tool("graph_resume", root_run_id=root)
                assert resumed == {"ok": True, "root_run_id": root, "state": "running",
                                   "accepted": True}

                # -- run until the owner decision -------------------------
                async def waiting():
                    res = await tool("graph_status", root_run_id=root)
                    return res if res["state"] == "waiting" and res["waiting"]["exceptions"] else None

                status = await until(waiting, timeout=60, what="state waiting")
                assert status["ok"] is True
                assert status["root_run_id"] == root
                assert status["graph_id"] == "demo" and status["graph_version"] == 1
                assert status["paused"] is False
                assert ISO_Z.fullmatch(status["started_at"])
                assert ISO_Z.fullmatch(status["updated_at"])
                for key in ("t1", "t2"):
                    # `nodes` is the 3.6 query body or its `nodes` map; accept either reading.
                    node = status["nodes"]["nodes"][key] if "nodes" in status["nodes"] \
                        else status["nodes"][key]
                    assert node["status"] == "completed"
                    assert node["attempts"] == 1
                    assert node["request_id"] == f"{root}/{key}"
                    assert node["cost_usd"] == pytest.approx(0.05)
                budget = status["budget"]
                assert set(budget) == {"pool", "cap_usd", "reserved_usd", "held_usd", "spent_usd",
                                       "remaining_usd", "overrun_usd", "capped"}
                assert budget["cap_usd"] == pytest.approx(2.0)
                assert budget["spent_usd"] == pytest.approx(0.10)
                assert budget["reserved_usd"] == 0
                assert budget["held_usd"] == 0
                assert budget["remaining_usd"] == pytest.approx(1.90)
                assert budget["overrun_usd"] == 0
                assert budget["capped"] is False
                assert status["children"] == []
                assert status["result"] is None
                assert status["waiting"]["admission"] == []
                (exception_id,) = status["waiting"]["exceptions"]

                detail = await tool("graph_status", root_run_id=root, detail=True)
                assert len(detail["journal"]) == 2
                assert {e["request_id"] for e in detail["journal"]} == {f"{root}/t1", f"{root}/t2"}
                assert detail["tree"][0]["run_path"] == root
                assert "journal" not in status and "tree" not in status

                # -- exceptions -------------------------------------------
                listed = await tool("graph_exceptions", root_run_id=root)
                assert listed["ok"] is True
                assert [e["exception_id"] for e in listed["exceptions"]] == [exception_id]
                assert listed["exceptions"][0]["kind"] == "owner_decision"
                assert listed["digest"]["open"] == 1
                assert (await tool("graph_exceptions", root_run_id=root,
                                   kind="node_failed"))["exceptions"] == []
                _error(await tool("graph_exceptions", state="bogus"), "invalid_argument")

                # -- answer ---------------------------------------------
                _error(await tool("graph_answer", exception_id=exception_id, answer="maybe"),
                       "invalid_answer")
                _error(await tool("graph_answer", exception_id="gx-000000000000", answer="go"),
                       "not_found")
                ok = await tool("graph_answer", exception_id=exception_id, answer="go",
                                note="fine", answered_by="tester")
                assert ok["ok"] is True
                assert ok["exception_id"] == exception_id
                assert ok["state"] in ("answered", "delivered")
                assert isinstance(ok["delivered"], bool)
                assert (await tool("graph_answer", exception_id=exception_id,
                                   answer="go"))["ok"] is True
                _error(await tool("graph_answer", exception_id=exception_id, answer="stop"),
                       "already_answered")

                async def completed():
                    res = await tool("graph_status", root_run_id=root)
                    return res if res["state"] == "completed" else None

                done = await until(completed, timeout=60, what="state completed")
                assert done["result"]["status"] == "completed"
                assert done["result"]["output"] == {"answer": "go"}

                # -- closed and unknown roots -------------------------------
                for name, extra in (("graph_pause", {}), ("graph_resume", {}),
                                    ("graph_cancel", {"reason": "late"})):
                    _error(await tool(name, root_run_id=root, **extra), "conflict")
                ghost = "gr-ffffffffffff"
                _error(await tool("graph_status", root_run_id=ghost), "not_found")
                _error(await tool("graph_pause", root_run_id=ghost), "not_found")
                _error(await tool("graph_resume", root_run_id=ghost), "not_found")
                _error(await tool("graph_cancel", root_run_id=ghost, reason="x"), "not_found")
                _error(await tool("graph_cancel", root_run_id=root, reason=""), "invalid_argument")

                # -- a capped pool, then cancel ------------------------------
                capped = await tool("graph_start", graph=_only_ask("capped"), pool="small",
                                    budget_usd=5.0, request_id="cap-1")
                assert capped["ok"] is True, capped
                assert capped["budget"]["pool"] == "small"
                assert capped["budget"]["cap_usd"] == pytest.approx(1.0)
                assert capped["budget"]["capped"] is True
                cancelling = await tool("graph_cancel", root_run_id=capped["root_run_id"],
                                        reason="no longer needed")
                assert cancelling == {"ok": True, "root_run_id": capped["root_run_id"],
                                      "state": "cancelling", "accepted": True}

                async def cancelled():
                    res = await tool("graph_status", root_run_id=capped["root_run_id"])
                    return res if res["state"] == "cancelled" else None

                await until(cancelled, timeout=30, what="state cancelled")

                # -- list -------------------------------------------------
                runs = await tool("graph_list")
                assert runs["ok"] is True
                by_id = {r["root_run_id"]: r for r in runs["runs"]}
                assert set(by_id) == {root, capped["root_run_id"]}
                assert [r["root_run_id"] for r in runs["runs"]] == [capped["root_run_id"], root]
                row = by_id[root]
                assert set(row) == {"root_run_id", "graph_id", "state", "started_at",
                                    "updated_at", "spent_usd", "cap_usd", "open_exceptions"}
                assert row["graph_id"] == "demo" and row["state"] == "completed"
                assert row["spent_usd"] == pytest.approx(0.10)
                assert row["cap_usd"] == pytest.approx(2.0)
                assert row["open_exceptions"] == 0
                assert ISO_Z.fullmatch(row["started_at"])
                only_done = await tool("graph_list", state="completed")
                assert [r["root_run_id"] for r in only_done["runs"]] == [root]
                assert len((await tool("graph_list", limit=1))["runs"]) == 1
                _error(await tool("graph_list", limit=0), "invalid_argument")
                _error(await tool("graph_list", limit=501), "invalid_argument")

                _error(await tool("graph_start", graph=doc, priority=9), "invalid_argument")
                bad = await tool("graph_start", graph={"id": "x"})
                _error(bad, "invalid_graph")
                assert bad["error"]["problems"]

    asyncio.run(scenario())
