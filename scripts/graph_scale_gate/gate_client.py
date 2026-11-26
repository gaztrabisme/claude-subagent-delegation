"""A client process: submit or re-attach one root, or deliver the owner decision.

``submit`` starts the root workflow (or attaches when it already exists, as after a
client restart) and waits for its result. ``signal`` sends the owner decision to the
paused workflow. Env GATE_CONFIG and GATE_AUDIT_LOG must be set.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from gate_common import CONTROL_QUEUE, audit, load_config
from gate_workflow import GateGraph
from temporalio.client import Client
from temporalio.common import Priority
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError


async def connect(address: str, timeout: float = 120.0) -> Client:
    end = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < end:
        try:
            return await asyncio.wait_for(Client.connect(address), timeout=2.0)
        except Exception as exc:  # noqa: BLE001 - keep trying until the server is back
            last = exc
            await asyncio.sleep(0.3)
    raise RuntimeError(f"Temporal not reachable at {address}: {type(last).__name__}")


async def submit(root_id: str, restarted: bool) -> None:
    cfg = load_config()
    spec = json.loads((Path(cfg["run_dir"]) / "specs.json").read_text(encoding="utf-8"))[root_id]
    client = await connect(cfg["address"])
    try:
        await client.start_workflow(
            GateGraph.run, spec, id=root_id, task_queue=CONTROL_QUEUE,
            priority=Priority(priority_key=4, fairness_key=root_id, fairness_weight=1.0))
        if not restarted:
            audit("client_submit", client_pid=os.getpid(), root_id=root_id)
    except WorkflowAlreadyStartedError:
        pass
    if restarted:
        audit("client_restarted", client_pid=os.getpid(), root_id=root_id)
    while True:
        try:
            result = await client.get_workflow_handle(root_id).result()
            print(json.dumps(result["counts"]))
            return
        except RPCError:
            await asyncio.sleep(0.5)
            client = await connect(cfg["address"])


async def signal_owner(workflow_id: str) -> None:
    cfg = load_config()
    client = await connect(cfg["address"])
    audit("owner_signal", workflow_id=workflow_id, decision="approve")
    await client.get_workflow_handle(workflow_id).signal(
        GateGraph.owner_decision, {"approved": True})


def main() -> None:
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="command", required=True)
    one = subs.add_parser("submit")
    one.add_argument("--root-id", required=True)
    one.add_argument("--restarted", action="store_true")
    sig = subs.add_parser("signal")
    sig.add_argument("--workflow-id", required=True)
    args = parser.parse_args()
    if args.command == "submit":
        asyncio.run(submit(args.root_id, args.restarted))
    else:
        asyncio.run(signal_owner(args.workflow_id))


if __name__ == "__main__":
    main()
