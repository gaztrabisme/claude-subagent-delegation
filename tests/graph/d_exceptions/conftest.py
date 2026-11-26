"""Fixtures for the exceptions tests.

Pure fixtures import the package lazily so the folder collects while it is
missing. The Temporal helpers follow SPEC 12.2: the CLI binary comes from
TEMPORAL_BIN or PATH, `dev_server_existing_path` is always given (otherwise the
SDK downloads a server), and time skipping is never used.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest


class FakeClock:
    def __init__(self, start: float = 2_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(tmp_path):
    from subagent.graph.store import Store

    return Store(tmp_path / "g.sqlite3")


@pytest.fixture
def queue(store, clock):
    from subagent.graph.exceptions import ExceptionQueue

    return ExceptionQueue(store, clock=clock)


@pytest.fixture
def raise_record(queue):
    """raise_ with sensible defaults; returns the ExceptionRecord."""

    def make(key: str, *, root: str = "gr-a", kind: str = "node_failed",
             options=("retry", "skip", "abort"), needs_answer: bool = True, **extra):
        return queue.raise_(
            key=key, root_run_id=root, run_path=extra.pop("run_path", root),
            node_id=extra.pop("node_id", "n1"), kind=kind,
            summary=extra.pop("summary", f"{kind} on {key}"),
            options=list(options), needs_answer=needs_answer, **extra,
        )

    return make


# -- Temporal ---------------------------------------------------------------


def temporal_binary() -> str:
    binary = os.environ.get("TEMPORAL_BIN") or shutil.which("temporal")
    if not binary:
        pytest.skip("Temporal CLI not found")
    return binary


@contextlib.asynccontextmanager
async def local_temporal(tmp_path: Path):
    from temporalio.testing import WorkflowEnvironment

    env = await WorkflowEnvironment.start_local(
        dev_server_existing_path=temporal_binary(),
        dev_server_database_filename=str(tmp_path / "t.db"),
    )
    try:
        yield env
    finally:
        await env.shutdown()


@contextlib.asynccontextmanager
async def in_process_worker(settings, gsettings, dispatcher):
    from subagent.graph.worker import run_worker

    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(settings, gsettings, dispatcher=dispatcher, stop=stop))
    try:
        yield task
    finally:
        stop.set()
        try:
            await asyncio.wait_for(task, 45)
        except BaseException:  # noqa: BLE001 - a stuck worker must not hang the suite
            task.cancel()
            with contextlib.suppress(BaseException):
                await task


async def until(check: Callable[[], Awaitable[Any]], *, timeout: float = 30.0,
                interval: float = 0.25, what: str = "condition") -> Any:
    """Poll `check` until it returns something truthy, or fail at the deadline."""
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = await check()
        if last:
            return last
        await asyncio.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}; last = {last!r}")
