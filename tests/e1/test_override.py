"""E1-5: the override on MCP delegate, and the CLI flag."""

from __future__ import annotations

import importlib
import inspect
import json
from pathlib import Path

import anyio
import pytest

from subagent import config
from subagent.runs import Registry

from ._helpers import build_settings, kinds, patch_spawn, read_trace, seed_percent, write_plan

SENTINEL = "SENTINEL-reason-quarterly-close"


@pytest.fixture
def server(tmp_path: Path, monkeypatch):
    plan = write_plan(tmp_path)
    settings = build_settings(tmp_path, ("codex", "glm"), plan=plan)
    seed_percent(settings, "codex", 93.0)
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "subagent.toml"))
    module = importlib.import_module("subagent.mcp_server")
    registry = Registry(settings, start_reaper=False)
    module.spawned = patch_spawn(monkeypatch)  # type: ignore[attr-defined]
    monkeypatch.setattr(module, "settings", settings)
    monkeypatch.setattr(module, "registry", registry)
    yield module
    registry.shutdown()


def call(server, tmp_path: Path, **kwargs):
    if "override_envelope" in kwargs and "override_envelope" not in inspect.signature(
        server.delegate
    ).parameters:
        pytest.fail("mcp_server.delegate has no override_envelope parameter")

    async def go():
        return await server.delegate(task="do it", verification="true",
                                     workspace=str(tmp_path), provider="codex",
                                     fallback="none", wait_seconds=5, **kwargs)

    return anyio.run(go)


def test_delegate_declares_override_envelope_defaulting_to_none(server):
    params = inspect.signature(server.delegate).parameters
    assert "override_envelope" in params, "mcp_server.delegate has no override_envelope"
    assert params["override_envelope"].default is None


def test_without_override_the_refused_lane_does_not_spawn(server, tmp_path: Path):
    out = call(server, tmp_path)
    assert server.spawned == []
    assert "envelope" in json.dumps(out).lower()


def test_override_lets_a_refused_run_proceed_and_records_it(server, tmp_path: Path):
    out = call(server, tmp_path, override_envelope=SENTINEL)
    assert len(server.spawned) == 1
    assert out.get("state") != "rejected"
    (rec,) = kinds(read_trace(tmp_path), "envelope_override")
    assert rec["lane"] == "codex" and rec["entry"] == "mcp"
    assert rec["level"] == "refuse" and rec["rule"] == "percent_used"
    assert (rec["used"], rec["limit"]) == (93.0, 85.0)
    assert SENTINEL in json.dumps(rec)


def test_override_on_an_ok_lane_writes_nothing(server, tmp_path: Path):
    seed_percent(server.settings, "codex", 10.0)
    call(server, tmp_path, override_envelope=SENTINEL)
    assert len(server.spawned) == 1
    assert kinds(read_trace(tmp_path), "envelope_override") == []


@pytest.mark.parametrize("reason", ["", "   ", "\n\t "])
def test_empty_reason_is_an_error_before_dispatch(server, tmp_path: Path, reason):
    try:
        out = call(server, tmp_path, override_envelope=reason)
    except Exception as exc:  # noqa: BLE001 - RegistryError or ToolError both count
        assert not isinstance(exc, TypeError), f"signature problem: {exc}"
    else:
        assert out["state"] == "rejected"
    assert server.spawned == []
    assert kinds(read_trace(tmp_path), "envelope_override") == []


def test_worker_never_sees_the_override(server, tmp_path: Path):
    call(server, tmp_path, override_envelope=SENTINEL)
    (proc,) = server.spawned
    blob = json.dumps(proc.argv) + json.dumps(proc.env)
    assert SENTINEL not in blob
    assert "override" not in blob.lower() and "envelope" not in blob.lower()


def test_cli_run_declares_the_flag():
    from subagent import cli

    try:
        args = cli._parser().parse_args(["run", "--plan", "p.md", "--override-envelope", "why"])
    except SystemExit:
        pytest.fail("subagent run does not accept --override-envelope")
    assert args.override_envelope == "why"


def test_cli_flag_defaults_to_none():
    from subagent import cli

    args = cli._parser().parse_args(["run", "--plan", "p.md"])
    assert getattr(args, "override_envelope", "missing") is None
