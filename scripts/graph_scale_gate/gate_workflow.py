"""The graph interpreter workflow: requests, child workflows and the owner decision.

Kept free of activity code so the Temporal sandbox imports only this small module.
Activities are called by name; they live in gate_activities.py.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import Priority, RetryPolicy
from temporalio.exceptions import ActivityError

CONTROL_QUEUE = "gate-control"
REQUEST_TIMEOUT = timedelta(minutes=30)
HEARTBEAT_TIMEOUT = timedelta(seconds=60)


def _lane_queue(lane: str) -> str:
    return f"lane-{lane}"


@workflow.defn(name="GateGraph")
class GateGraph:
    def __init__(self) -> None:
        self._decision: dict[str, Any] | None = None

    @workflow.signal
    def owner_decision(self, decision: dict[str, Any]) -> None:
        self._decision = decision

    @workflow.query
    def status(self) -> dict[str, Any]:
        return {"decided": self._decision is not None}

    @workflow.run
    async def run(self, spec: dict[str, Any]) -> dict[str, Any]:
        wid = workflow.info().workflow_id
        counts: dict[str, int] = {}

        def add(outcomes: dict[str, int]) -> None:
            for key, value in outcomes.items():
                counts[key] = counts.get(key, 0) + value

        for step in spec["steps"]:
            kind = step["type"]
            if kind == "requests":
                results = await asyncio.gather(*[self._request(r) for r in step["items"]])
                add({o: results.count(o) for o in set(results)})
            elif kind == "children":
                results = await asyncio.gather(*[
                    workflow.execute_child_workflow(
                        GateGraph.run, {**child, "root_id": spec["root_id"]},
                        id=f"{wid}/{child['name']}", task_queue=CONTROL_QUEUE)
                    for child in step["items"]])
                for result in results:
                    add(result["counts"])
            elif kind == "owner_decision":
                await self._mark("owner_paused", f"owner_paused:{wid}",
                                 workflow_id=wid, root_id=spec["root_id"])
                await workflow.wait_condition(lambda: self._decision is not None)
                await self._mark("owner_resumed", f"owner_resumed:{wid}", workflow_id=wid)
            else:
                raise ValueError(f"unsupported step {kind!r}")
        if spec.get("is_root"):
            status = ("budget_exhausted" if counts.get("budget_exhausted") else
                      "failed" if counts.get("failed") or counts.get("dead") else "completed")
            await self._mark("root_done", f"root_done:{wid}", root_id=spec["root_id"],
                             status=status)
        return {"workflow_id": wid, "counts": counts}

    async def _mark(self, kind: str, key: str, **fields: Any) -> None:
        await workflow.execute_activity(
            "audit_marker", {"type": kind, "key": key, "fields": fields},
            task_queue=CONTROL_QUEUE, start_to_close_timeout=timedelta(seconds=30))

    async def _request(self, req: dict[str, Any]) -> str:
        try:
            result = await workflow.execute_activity(
                "run_request", req, task_queue=_lane_queue(req["lane"]),
                start_to_close_timeout=REQUEST_TIMEOUT, heartbeat_timeout=HEARTBEAT_TIMEOUT,
                retry_policy=RetryPolicy(maximum_attempts=5),
                priority=Priority(priority_key=req["priority"], fairness_key=req["root_id"],
                                  fairness_weight=1.0))
            return str(result["outcome"])
        except ActivityError:
            await self._mark("request_end", f"request_end:{req['request_id']}",
                             request_id=req["request_id"], outcome="failed", run_id=None)
            return "failed"
