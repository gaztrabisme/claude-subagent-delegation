"""The singleton worker process: control queue plus one capped queue per lane.

One process owns every lane cap: each lane has its own task queue and a Worker with
``max_concurrent_activities`` equal to the cap, so admission is enforced here and not by
the clients. Run by orchestrate.py; env GATE_CONFIG and GATE_AUDIT_LOG must be set.
"""

from __future__ import annotations

import asyncio
import os
import signal

from gate_activities import audit_marker, run_request
from gate_common import CONTROL_QUEUE, audit, lane_queue, load_config
from gate_dispatch import real_dispatcher
from gate_workflow import GateGraph
from temporalio.client import Client
from temporalio.worker import Worker


async def main() -> None:
    cfg = load_config()
    dispatcher = real_dispatcher(cfg) if cfg["dispatch"] == "real" else None
    client = await Client.connect(cfg["address"])
    workers = [Worker(client, task_queue=CONTROL_QUEUE, workflows=[GateGraph],
                      activities=[audit_marker], max_concurrent_workflow_tasks=200,
                      max_concurrent_activities=20)]
    for lane, cap in cfg["lane_caps"].items():
        workers.append(Worker(client, task_queue=lane_queue(lane), activities=[run_request],
                              max_concurrent_activities=int(cap)))
    stop = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
    asyncio.get_running_loop().add_signal_handler(signal.SIGINT, stop.set)
    async with _supervisor(dispatcher):
        tasks = [asyncio.create_task(w.run()) for w in workers]
        await asyncio.sleep(0.5)
        for task in tasks:
            if task.done():
                await task
        audit("worker_started", pid=os.getpid(), lane_caps=cfg["lane_caps"])
        await stop.wait()
        for worker in workers:
            await worker.shutdown()
        await asyncio.gather(*tasks, return_exceptions=True)
    if dispatcher is not None:
        dispatcher.shutdown()


class _supervisor:  # noqa: N801 - a context manager used like a function
    """Real mode serves the guard's approval socket for the worker's lifetime."""

    def __init__(self, dispatcher) -> None:
        self.dispatcher = dispatcher
        self.group = None

    async def __aenter__(self):
        if self.dispatcher is not None:
            import anyio
            self.group = anyio.create_task_group()
            await self.group.__aenter__()
            await self.dispatcher.serve_supervisor(self.group)
        return self

    async def __aexit__(self, *exc):
        if self.group is not None:
            self.group.cancel_scope.cancel()
            await self.group.__aexit__(*exc)


if __name__ == "__main__":
    asyncio.run(main())
