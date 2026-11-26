"""Fix-round-2 review (fix1-ds.json check 1): autostart after a cached client, the spawned
worker uses the parent's resolved settings, and a failed spawn is an error (SPEC 8.2).

Every process started is found by the workspace it runs in and killed; every wait is bounded.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from ..conftest import require_temporal
from ..e_crash._cluster import free_loopback_port
from ._auto import (
    _kill,
    _pids_in,
    autostart_config,
    completed,
    gate_doc,
    live_workers,
    no_live_workers,
)

pytest.importorskip("temporalio")


def _service(config):
    from subagent import config as cfg
    from subagent.graph.server import settings_from
    from subagent.graph.service import GraphService

    settings = cfg.load(extra=config)
    gs = settings_from(settings, raw=cfg.read_config(extra=config)["graph"])
    return settings, gs, GraphService(settings, gsettings=gs)


def test_start_after_a_status_connect_still_autostarts_and_restarts_a_dead_worker(
        tmp_path, monkeypatch):  # blocking 1
    require_temporal()
    address = f"127.0.0.1:{free_loopback_port()}"
    config, workspace, state = autostart_config(tmp_path, monkeypatch, address=address,
                                                prefix="fix2cache")

    async def scenario():
        from subagent.graph.server import DevServer

        _, gs, service = _service(config)
        try:
            await service.status("gr-nothing000001")  # connects (and caches) without a worker
            assert not await asyncio.to_thread(live_workers, service), \
                "sanity: a status call must not start a worker"
            first = await service.start(gate_doc("cache1"), request_id="fix2-cache-1")
            assert first["ok"] is True, first
            done = await completed(service, first["root_run_id"])
            assert done["state"] == "completed", done
            assert await asyncio.to_thread(live_workers, service), "no worker is alive"
            await asyncio.to_thread(_kill, _pids_in(workspace))
            assert await no_live_workers(service), "the killed worker still reads alive"
            second = await service.start(gate_doc("cache2"), request_id="fix2-cache-2")
            assert second["ok"] is True, second
            done2 = await completed(service, second["root_run_id"])
            return done, done2
        finally:
            await asyncio.to_thread(_kill, _pids_in(workspace))
            await asyncio.to_thread(DevServer(state, address=address).stop)

    try:
        first, second = asyncio.run(scenario())
    finally:
        _kill(_pids_in(workspace))
    assert first["state"] == "completed", first
    assert second["state"] == "completed", second


def test_spawned_worker_uses_the_parents_port_when_config_says_port_zero(
        tmp_path, monkeypatch):  # blocking 2
    require_temporal()
    config, workspace, state = autostart_config(tmp_path, monkeypatch, address="127.0.0.1:0",
                                                prefix="fix2zero")

    async def scenario():
        from subagent.graph.server import DevServer

        _, gs, service = _service(config)
        try:
            started = await service.start(gate_doc("zero1"), request_id="fix2-zero-1")
            assert started["ok"] is True, started
            return await completed(service, started["root_run_id"])
        finally:
            await asyncio.to_thread(_kill, _pids_in(workspace))
            await asyncio.to_thread(DevServer(state, address="127.0.0.1:0").stop)

    try:
        final = asyncio.run(scenario())
    finally:
        _kill(_pids_in(workspace))
    assert final["state"] == "completed", final


def test_spawned_worker_follows_server_json_when_it_differs_from_the_config(
        tmp_path, monkeypatch):  # blocking 2 (server.json)
    require_temporal()
    configured = f"127.0.0.1:{free_loopback_port()}"  # nothing listens here
    config, workspace, state = autostart_config(tmp_path, monkeypatch, address=configured,
                                                prefix="fix2json")

    async def scenario():
        from subagent.graph.server import DevServer

        actual = f"127.0.0.1:{free_loopback_port()}"
        assert actual != configured
        server = DevServer(state, address=actual, startup_timeout_s=30.0)
        info = await asyncio.to_thread(server.start)
        try:
            _, gs, service = _service(config)
            started = await service.start(gate_doc("json1"), request_id="fix2-json-1")
            assert started["ok"] is True, started
            assert service.gs.address == info.address, "parent must use the recorded server"
            return await completed(service, started["root_run_id"])
        finally:
            await asyncio.to_thread(_kill, _pids_in(workspace))
            await asyncio.to_thread(server.stop)

    try:
        final = asyncio.run(scenario())
    finally:
        _kill(_pids_in(workspace))
    assert final["state"] == "completed", final


def test_a_worker_that_cannot_be_spawned_makes_start_an_error(tmp_path, monkeypatch):  # item 4
    require_temporal()
    address = f"127.0.0.1:{free_loopback_port()}"
    config, workspace, state = autostart_config(tmp_path, monkeypatch, address=address,
                                                prefix="fix2spawn")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "no-such-python"))

    async def scenario():
        from subagent.graph.server import DevServer

        _, gs, service = _service(config)
        try:
            return await asyncio.wait_for(
                service.start(gate_doc("spawn1"), request_id="fix2-spawn-1"), 90)
        finally:
            await asyncio.to_thread(_kill, _pids_in(workspace))
            await asyncio.to_thread(DevServer(state, address=address).stop)

    try:
        result = asyncio.run(scenario())
    finally:
        _kill(_pids_in(workspace))
    assert result.get("ok") is False, f"start reports success with no worker: {result}"
    assert result["error"]["code"] == "unavailable", result
    assert result["error"]["message"]
    assert "state" not in result or result.get("state") != "running"
