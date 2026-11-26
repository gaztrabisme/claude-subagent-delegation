"""Review finding 1: with `[graph].autostart = true`, graph_start leaves a running worker.

SPEC 8.2 and 13 item 8: autostart spawns a detached worker that outlives the MCP process.
The spawned worker is the production one (`subagent graph worker`), so the graph here needs
no provider: a gate on an input, which the worker executes end to end. The test kills every
process it caused, found by the workspace it runs in.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import time

import pytest

from tests.conftest import _toml_table, make_settings

from ..conftest import require_temporal
from ..e_crash._cluster import free_loopback_port
from ..f_tools_cli.conftest import until

pytest.importorskip("temporalio")

GATE_DOC = {
    "schema": "subagent.graph/1", "id": "autostart", "version": 1, "inputs": {"x": 1},
    "body": [{"id": "g", "kind": "gate",
              "condition": {"path": "inputs.x", "op": "eq", "value": 1}}],
}


def _pids_in(workspace) -> list[int]:
    """Pids of `graph worker` processes whose cwd is `workspace`."""
    out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    found = []
    for line in out.splitlines():
        pid_text, _, command = line.strip().partition(" ")
        if "graph" not in command or "worker" not in command or not pid_text.isdigit():
            continue
        cwd = subprocess.run(["lsof", "-a", "-p", pid_text, "-d", "cwd", "-Fn"],
                             capture_output=True, text=True).stdout
        if f"n{workspace}" in cwd.split():
            found.append(int(pid_text))
    return found


def _kill(pids: list[int]) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in pids:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, sig)
        time.sleep(1.0)


def test_autostart_true_runs_a_graph_to_completion(tmp_path, monkeypatch):
    binary = require_temporal()
    workspace = (tmp_path / "ws").resolve()
    workspace.mkdir()
    state = tmp_path / "state"
    address = f"127.0.0.1:{free_loopback_port()}"
    make_settings(tmp_path, workspace=str(workspace))
    config = tmp_path / "cfg.toml"
    config.write_text((tmp_path / "subagent.toml").read_text() + "\n" + _toml_table("graph", {
        "state_dir": str(state), "address": address, "task_queue_prefix": "fix1auto",
        "autostart": True, "admit_poll_s": 0.3,
    }) + "\n")
    monkeypatch.setenv("SUBAGENT_CONFIG", str(config))
    monkeypatch.setenv("TEMPORAL_BIN", str(binary))
    monkeypatch.chdir(workspace)

    async def scenario():
        from subagent import config as cfg
        from subagent.graph.server import DevServer, settings_from
        from subagent.graph.service import GraphService

        settings = cfg.load(extra=config)
        gs = settings_from(settings, raw=cfg.read_config(extra=config)["graph"])
        assert gs.autostart is True
        service = GraphService(settings, gsettings=gs)
        try:
            started = await service.start(GATE_DOC, request_id="autostart-1")
            assert started["ok"] is True, started
            root = started["root_run_id"]

            async def finished():
                res = await service.status(root)
                return res if res.get("state") in ("completed", "failed", "cancelled") else None

            try:
                return await until(finished, timeout=60, what="the autostarted worker to run it")
            except AssertionError as err:
                last = await service.status(root)
                raise AssertionError(
                    f"no worker ran the graph (state={last.get('state')!r}): {err}") from None
        finally:
            _kill(_pids_in(workspace))
            await asyncio.to_thread(DevServer(state, address=address).stop)

    try:
        final = asyncio.run(scenario())
    finally:
        _kill(_pids_in(workspace))
    assert final["state"] == "completed", final
