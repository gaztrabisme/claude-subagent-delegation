"""Temporal activities: the audit marker and the lane-capped request runner."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from gate_common import audit, load_config
from gate_dispatch import fake_dispatch, real_dispatcher
from gate_ledger import Ledger
from temporalio import activity

OUTCOME_PRIORITY = ("budget_exhausted", "dead", "failed", "completed")


def _ledger(cfg: dict[str, Any]) -> Ledger:
    return Ledger(cfg["ledger_db"])


@activity.defn(name="audit_marker")
async def audit_marker(event: dict[str, Any]) -> None:
    """Write one workflow-side audit record, once per key even if the activity retries."""
    if _ledger(load_config()).marker_once(event["key"]):
        audit(event["type"], **event["fields"])


def _recover(ledger: Ledger, request_id: str) -> None:
    """Close what a dead earlier execution of this activity left open."""
    started, open_reservations = ledger.dangling(request_id)
    for attempt, base in started:
        audit("child_end", **base, attempt=attempt, outcome="dead", run_id=None,
              cost_usd=0.0, verified=False)
        ledger.set_attempt_state(request_id, attempt, "ended")
    for reservation_id in open_reservations:
        if ledger.settle(reservation_id, None, 0.0):
            audit("budget_settle", reservation_id=reservation_id, request_id=request_id,
                  run_id=None, actual_usd=0.0)


async def _heartbeating(task: asyncio.Task[Any]) -> Any:
    while not task.done():
        await asyncio.wait({task}, timeout=2.0)
        activity.heartbeat()
    return task.result()


@activity.defn(name="run_request")
async def run_request(req: dict[str, Any]) -> dict[str, Any]:
    cfg = load_config()
    ledger = _ledger(cfg)
    rid, lane = req["request_id"], req["lane"]
    journaled = ledger.journal_get(rid)
    if journaled is not None:  # a retry of a finished request never dispatches again
        return journaled
    _recover(ledger, rid)

    estimate = float(cfg["estimates"].get(lane, 0.0))
    identity = {k: req[k] for k in ("request_id", "lane", "root_id", "workflow_id", "depth")}
    base = {**identity, "provider": lane, "worker_pid": os.getpid()}
    last: dict[str, Any] = {"outcome": "failed", "run_id": None}
    for _ in range(int(cfg["max_attempts"])):
        attempt = ledger.begin_attempt(rid, base)
        admitted, reservation_id, committed = ledger.reserve(
            rid, attempt, estimate, float(cfg["budget_cap_usd"]))
        if not admitted:
            ledger.set_attempt_state(rid, attempt, "rejected")
            audit("budget_reject", request_id=rid, requested_usd=estimate,
                  committed_usd=round(committed, 6))
            last = {"outcome": "budget_exhausted", "run_id": None}
            break
        audit("budget_reserve", reservation_id=reservation_id, request_id=rid,
              amount_usd=estimate)
        ledger.set_attempt_state(rid, attempt, "started")
        audit("child_start", **base, attempt=attempt)
        if cfg["dispatch"] == "real":
            runner = real_dispatcher(cfg).dispatch(req, attempt, cfg)
        else:
            runner = fake_dispatch(req, attempt, cfg)
        result = await _heartbeating(asyncio.ensure_future(runner))
        audit("child_end", **base, attempt=attempt, outcome=result["outcome"],
              run_id=result["run_id"], cost_usd=result["cost_usd"], verified=result["verified"])
        ledger.set_attempt_state(rid, attempt, "ended")
        if ledger.settle(reservation_id, result["run_id"], result["cost_usd"]):
            audit("budget_settle", reservation_id=reservation_id, request_id=rid,
                  run_id=result["run_id"], actual_usd=result["cost_usd"])
        last = result
        if result["outcome"] == "completed":
            break

    final = {"request_id": rid, "outcome": last["outcome"],
             "run_id": last["run_id"] if last["outcome"] == "completed" else None}
    if ledger.journal_put(rid, final) and ledger.marker_once(f"request_end:{rid}"):
        audit("request_end", request_id=rid, outcome=final["outcome"], run_id=final["run_id"])
    return final
