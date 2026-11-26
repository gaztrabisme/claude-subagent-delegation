"""The graph worker process (spec 8.2): Temporal workers, heartbeats, reconcile, answer sweeper.

`run_worker` starts two Temporal workers (`{prefix}-graph`: the workflow and the ledger
activities; `{prefix}-tasks`: `run_task` and `run_gate`), registers in the `workers` table,
beats every 5 s, reconciles at start and every 30 s, re-signals undelivered answers every
10 s, and on stop drains for up to 30 s before cancelling what is still running.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import signal
import time
from datetime import timedelta
from typing import Any

from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker

from .. import config
from ..config import Settings, log
from . import trace as gtrace
from .activities import GraphContext, bind, trace_admission_events
from .admission import Admission, Workers
from .dispatch import Dispatcher
from .exceptions import ExceptionQueue, NoClientSession
from .journal import Journal
from .ledger import Ledger
from .server import GraphSettings
from .store import Store

BEAT_S = 5.0
RECONCILE_S = 30.0
SWEEP_S = 10.0
DRAIN_S = 30
_LANE_CACHE_S = 5.0


def _lane_open_fn(settings: Settings):
    """`lane_open(lane)` from the stored lane status, cached for a few seconds."""
    from ..lane_status import LaneStatusStore

    store = LaneStatusStore(settings.session_root, redact_keys=settings.leaked_keys)
    cache: dict[str, Any] = {"at": 0.0, "view": {}}

    def lane_open(lane: str) -> bool:
        now = time.monotonic()
        if now - cache["at"] > _LANE_CACHE_S:
            try:
                cache["view"] = store.snapshot(settings, count_runs=False)
            except Exception:  # noqa: BLE001 - an unreadable status counts as open
                log.warning("lane status unreadable; treating lanes as open", exc_info=True)
                cache["view"] = {}
            cache["at"] = now
        return (cache["view"].get(lane) or {}).get("state") != "closed"

    return lane_open


def build_context(settings: Settings, gsettings: GraphSettings, dispatcher: Dispatcher, *,
                  trace: Any = None) -> GraphContext:
    """The store, ledger, admission and exceptions a worker process shares."""
    store = Store(gsettings.state_dir / "graph.sqlite3")
    ledger = Ledger(store, max_depth=gsettings.max_depth)
    ledger.ensure_pool("default", gsettings.pools.get("default", gsettings.budget_usd))
    for name, cap in gsettings.pools.items():
        ledger.ensure_pool(name, cap)
    journal = Journal(store)
    workers = Workers(store)
    worker_id = workers.register(os.getpid())
    admission = Admission(store, ledger, journal, caps=gsettings.caps(settings),
                          lane_open=_lane_open_fn(settings), lease_ttl_s=gsettings.lease_ttl_s,
                          workers=workers)
    return GraphContext(store, ledger, journal, admission, ExceptionQueue(store), dispatcher,
                        settings, gsettings, worker_id, trace=trace)


async def _connect(address: str, stop: asyncio.Event) -> Client | None:
    while not stop.is_set():
        try:
            return await Client.connect(address)
        except Exception as exc:  # noqa: BLE001 - the server may still be starting
            log.warning("graph worker: cannot reach Temporal at %s (%s); retrying", address, exc)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), 1.0)
    return None


async def _root_live(client: Client, ctx: GraphContext, root: str) -> bool | None:
    try:
        desc = await client.get_workflow_handle(root).describe()
    except RPCError as exc:
        return False if exc.status == RPCStatusCode.NOT_FOUND else None
    except Exception:  # noqa: BLE001
        return None
    if desc.status == WorkflowExecutionStatus.RUNNING:
        return True
    # Closed in Temporal. A workflow closes its account before it completes, so an account
    # that is no longer open is simply finished, not dead.
    try:
        account = await asyncio.to_thread(ctx.ledger.account, root)
    except KeyError:
        return None
    return account.state != "open"


async def _beat_loop(ctx: GraphContext, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.to_thread(ctx.admission.workers.beat, ctx.worker_id)
        except Exception:  # noqa: BLE001 - a busy database must not end the worker
            log.warning("worker beat failed", exc_info=True)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), BEAT_S)


async def _reconcile_once(client: Client, ctx: GraphContext) -> None:
    loop = asyncio.get_running_loop()

    def root_live(root: str) -> bool | None:
        try:
            return asyncio.run_coroutine_threadsafe(_root_live(client, ctx, root),
                                                    loop).result(20)
        except Exception:  # noqa: BLE001
            return None

    report = await asyncio.to_thread(ctx.admission.reconcile, root_live=root_live)
    await asyncio.to_thread(trace_admission_events, ctx)
    if report.interrupted or report.dead:
        log.info("graph reconcile: interrupted=%s dead=%s", report.interrupted, report.dead)


async def _reconcile_loop(client: Client, ctx: GraphContext, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await _reconcile_once(client, ctx)
        except Exception:  # noqa: BLE001
            log.warning("graph reconcile failed", exc_info=True)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), RECONCILE_S)


async def deliver(client: Client, ctx: GraphContext, record: Any) -> bool:
    """Signal the owning workflow of an answered exception; True when delivered or moot."""
    try:
        await client.get_workflow_handle(record.run_path).signal("answer", record.exception_id)
    except RPCError as exc:
        gone = exc.status == RPCStatusCode.NOT_FOUND or "already completed" in str(exc).lower()
        if not gone:
            return False
    except Exception:  # noqa: BLE001 - try again at the next sweep
        return False
    await asyncio.to_thread(ctx.exceptions.mark_delivered, record.exception_id)
    if ctx.trace is not None:
        gtrace.write_graph_exception(
            ctx.trace, exception_id=record.exception_id, root_run_id=record.root_run_id,
            exception_kind=record.kind, event="delivered", needs_answer=record.needs_answer)
    return True


async def _sweep_loop(client: Client, ctx: GraphContext, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            for record in await asyncio.to_thread(ctx.exceptions.undelivered):
                await deliver(client, ctx, record)
        except Exception:  # noqa: BLE001
            log.warning("answer sweep failed", exc_info=True)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), SWEEP_S)


async def run_worker(settings: Settings, gsettings: GraphSettings, *, dispatcher: Dispatcher,
                     stop: asyncio.Event | None = None, trace: Any = None) -> None:
    """Run until `stop` is set (SIGTERM or SIGINT when this function owns the event)."""
    from .workflow import GraphWorkflow

    loop = asyncio.get_running_loop()
    owns_stop = stop is None
    if stop is None:
        stop = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(sig, stop.set)
    if trace is None:
        trace = getattr(getattr(dispatcher, "registry", None), "trace", None)
    ctx = await asyncio.to_thread(build_context, settings, gsettings, dispatcher, trace=trace)
    client = await _connect(gsettings.address, stop)
    if client is None:
        return
    prefix = gsettings.task_queue_prefix
    graph_worker = Worker(client, task_queue=f"{prefix}-graph", workflows=[GraphWorkflow],
                          activities=bind(ctx, queue="graph"),
                          graceful_shutdown_timeout=timedelta(seconds=DRAIN_S))
    tasks_worker = Worker(client, task_queue=f"{prefix}-tasks",
                          activities=bind(ctx, queue="tasks"),
                          max_concurrent_activities=gsettings.worker_max_tasks,
                          graceful_shutdown_timeout=timedelta(seconds=DRAIN_S))
    runners = [asyncio.create_task(graph_worker.run()), asyncio.create_task(tasks_worker.run())]
    helpers = [asyncio.create_task(_beat_loop(ctx, stop)),
               asyncio.create_task(_reconcile_loop(client, ctx, stop)),
               asyncio.create_task(_sweep_loop(client, ctx, stop))]
    try:
        waiter = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait([waiter, *runners], return_when=asyncio.FIRST_COMPLETED)
        waiter.cancel()
        for finished in done:
            if finished is not waiter and finished.exception() is not None:
                raise finished.exception()  # type: ignore[misc]
    finally:
        stop.set()
        await asyncio.gather(graph_worker.shutdown(), tasks_worker.shutdown(),
                             return_exceptions=True)
        await asyncio.gather(*runners, return_exceptions=True)
        for helper in helpers:
            helper.cancel()
        await asyncio.gather(*helpers, return_exceptions=True)
        if owns_stop:
            for sig in (signal.SIGTERM, signal.SIGINT):
                with contextlib.suppress(Exception):
                    loop.remove_signal_handler(sig)


async def run_registry_worker(settings: Settings, gsettings: GraphSettings,
                              stop: asyncio.Event | None = None) -> None:
    """The production worker: a Registry-backed dispatcher and a supervisor on a no-client
    session (spec 6), so guard escalations never interrupt an orchestrator."""
    import anyio

    from ..guard.supervisor import Supervisor
    from ..runs import Registry
    from .registry_dispatcher import RegistryDispatcher

    if len(os.fsencode(settings.approval_socket)) >= config.UNIX_SOCKET_PATH_MAX:
        # The worker owns its supervisor socket; a configured path too long to bind falls back
        # to the short private one instead of ending the worker at start.
        settings = dataclasses.replace(
            settings, approval_socket=config._approval_socket_path(settings.approval_socket))
    registry = Registry(settings, entry="graph")
    supervisor = Supervisor(settings, registry, trace=registry.trace)
    supervisor.bind(NoClientSession())
    try:
        async with anyio.create_task_group() as group:
            await group.start(supervisor.serve)
            try:
                await run_worker(settings, gsettings,
                                 dispatcher=RegistryDispatcher(registry, settings), stop=stop,
                                 trace=registry.trace)
            finally:
                group.cancel_scope.cancel()
    finally:
        supervisor.cleanup()
        registry.shutdown()
