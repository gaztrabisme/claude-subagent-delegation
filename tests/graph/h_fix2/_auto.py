"""Shared helpers for the autostart tests: a config on disk, worker pids, bounded waits."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from tests.conftest import _toml_table, make_settings

from ..f_tools_cli.conftest import until
from ..h_fix1.test_autostart import GATE_DOC, _kill, _pids_in

__all__ = ["GATE_DOC", "_kill", "_pids_in", "autostart_config", "completed", "gate_doc",
           "live_workers", "no_live_workers"]


def gate_doc(graph_id: str) -> dict[str, Any]:
    doc = dict(GATE_DOC)
    doc["id"] = graph_id
    return doc


def autostart_config(tmp_path: Path, monkeypatch, *, address: str, prefix: str,
                     state_name: str = "state") -> tuple[Path, Path, Path]:
    """Write a config with `[graph]` autostart on; point SUBAGENT_CONFIG and cwd at it.

    Returns (config, workspace, state_dir)."""
    from ..conftest import require_temporal

    binary = require_temporal()
    workspace = (tmp_path / "ws").resolve()
    workspace.mkdir()
    state = tmp_path / state_name
    make_settings(tmp_path, workspace=str(workspace))
    config = tmp_path / "cfg.toml"
    config.write_text((tmp_path / "subagent.toml").read_text() + "\n" + _toml_table("graph", {
        "state_dir": str(state), "address": address, "task_queue_prefix": prefix,
        "autostart": True, "admit_poll_s": 0.3,
    }) + "\n")
    monkeypatch.setenv("SUBAGENT_CONFIG", str(config))
    monkeypatch.setenv("TEMPORAL_BIN", str(binary))
    monkeypatch.chdir(workspace)
    return config, workspace, state


def live_workers(service) -> list[str]:
    from subagent.graph.admission import Workers

    workers = Workers(service.store)
    return [w["worker_id"] for w in workers.list() if workers.alive(w["worker_id"])]


async def no_live_workers(service, timeout: float = 45.0) -> bool:
    async def check():
        return (not await asyncio.to_thread(live_workers, service)) or None

    with contextlib.suppress(AssertionError):
        return bool(await until(check, timeout=timeout, interval=1.0,
                                what="the killed worker to read dead"))
    return False


async def completed(service, root: str, timeout: float = 60.0) -> dict[str, Any]:
    async def finished():
        res = await service.status(root)
        return res if res.get("state") in ("completed", "failed", "cancelled") else None

    try:
        return await until(finished, timeout=timeout, what="a worker to run the graph")
    except AssertionError as err:
        last = await service.status(root)
        raise AssertionError(f"no worker ran the graph (state={last.get('state')!r}): {err}") \
            from None
