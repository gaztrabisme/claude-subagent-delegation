"""Helpers for the fix-round-1 tests (review code-ds-1): a trace sink and a traced worker."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from tests.conftest import make_settings


class TraceSink:
    """Stands in for a telemetry Trace: records every `write(kind, **fields)`."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def write(self, kind: str, **fields: Any) -> None:
        self.records.append({"kind": kind, **fields})

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [r for r in self.records if r["kind"] == kind]


@contextlib.asynccontextmanager
async def traced_worker(settings, gsettings, dispatcher, trace):
    from subagent.graph.worker import run_worker

    stop = asyncio.Event()
    task = asyncio.create_task(
        run_worker(settings, gsettings, dispatcher=dispatcher, stop=stop, trace=trace))
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


def boot(env, tmp_path, *, state_name: str = "state", address: str | None = None):
    """(settings, gsettings, workspace) for a service against `env`'s dev server."""
    from subagent.graph.server import GraphSettings

    settings = make_settings(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    gsettings = GraphSettings(
        state_dir=tmp_path / state_name,
        address=address or env.client.service_client.config.target_host,
        autostart=False, admit_poll_s=0.2, budget_usd=10.0, lane_slots={"glm": 2},
    )
    return settings, gsettings, str(workspace)


def two_task_doc(workspace: str, *, graph_id: str = "fix1") -> dict[str, Any]:
    return {
        "schema": "subagent.graph/1", "id": graph_id, "version": 1, "workspace": workspace,
        "budget_usd": 2.0, "defaults": {"provider": "glm", "reserve_usd": 0.2},
        "body": [
            {"id": "t1", "kind": "task", "prompt": "one", "verification": "true"},
            {"id": "t2", "kind": "task", "prompt": "two", "verification": "true"},
        ],
    }
