"""Fixtures for the tool and CLI tests.

The graph package is imported inside fixtures and tests so the folder collects
while it is missing. Temporal helpers follow SPEC 12.2 (binary from TEMPORAL_BIN
or PATH, `dev_server_existing_path` always, never time skipping).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import inspect
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import make_settings

TOOL_NAMES = ["graph_validate", "graph_start", "graph_status", "graph_pause", "graph_resume",
              "graph_cancel", "graph_list", "graph_exceptions", "graph_answer"]

# A two-task graph with no provider needs beyond the default table's glm.
SIMPLE_DOC = {
    "schema": "subagent.graph/1", "id": "simple", "version": 1,
    "defaults": {"provider": "glm"},
    "body": [
        {"id": "one", "kind": "task", "prompt": "do one", "verification": "true"},
        {"id": "two", "kind": "task", "prompt": "do two", "verification": "true"},
    ],
}


class FakeApp:
    """Collects registered tools whether the registrar uses `app.tool(...)`
    as a decorator or `app.add_tool(fn, ...)`."""

    def __init__(self):
        self.tools: dict[str, Callable] = {}

    def tool(self, name: str | None = None, **_kw):
        def deco(fn):
            self.tools[name or fn.__name__] = fn
            return fn

        return deco

    def add_tool(self, fn, name: str | None = None, **_kw):
        self.tools[name or fn.__name__] = fn


async def call(fn: Callable, /, **kwargs) -> Any:
    """Call a tool function, sync or async, from inside a running loop."""
    if inspect.iscoroutinefunction(fn):
        return await fn(**kwargs)
    result = await asyncio.to_thread(fn, **kwargs)
    return await result if inspect.isawaitable(result) else result


# -- simulating a missing temporalio ----------------------------------------


def _graph_modules() -> list[str]:
    return [m for m in sys.modules if m == "subagent.graph" or m.startswith("subagent.graph.")]


def _temporal_modules() -> list[str]:
    return [m for m in sys.modules if m == "temporalio" or m.startswith("temporalio.")]


@pytest.fixture
def no_temporalio(monkeypatch):
    """Fresh `subagent.graph*` imports that see no `temporalio`."""
    import subagent

    saved = {name: sys.modules.pop(name) for name in _graph_modules() + _temporal_modules()}
    saved_attr = getattr(subagent, "graph", None)
    monkeypatch.setitem(sys.modules, "temporalio", None)
    yield
    for name in _graph_modules():
        del sys.modules[name]
    sys.modules.pop("temporalio", None)
    sys.modules.update(saved)
    if saved_attr is not None:
        subagent.graph = saved_attr


@pytest.fixture
def graph_cfg(tmp_path, monkeypatch):
    """A config file (providers + `[graph]`) that the CLI reads via SUBAGENT_CONFIG."""

    def make(**graph_keys) -> Path:
        path = tmp_path / "subagent.toml"
        if not path.exists():
            make_settings(tmp_path)
        path = tmp_path / "subagent.toml"
        lines = ["", "[graph]", f'state_dir = "{tmp_path / "state"}"', "autostart = false"]
        for key, value in graph_keys.items():
            if isinstance(value, bool):
                lines.append(f"{key} = {str(value).lower()}")
            elif isinstance(value, (int, float)):
                lines.append(f"{key} = {value}")
            else:
                lines.append(f'{key} = "{value}"')
        path.write_text(path.read_text() + "\n".join(lines) + "\n", encoding="utf-8")
        monkeypatch.setenv("SUBAGENT_CONFIG", str(path))
        monkeypatch.chdir(tmp_path)
        return path

    return make


def import_graph(name: str):
    return importlib.import_module(f"subagent.graph.{name}")


def run_cli_inprocess(argv: list[str]) -> int:
    """Parse with a fresh `add_graph_parser` and run `cmd_graph` in this process."""
    import argparse

    cli = import_graph("cli")
    parser = argparse.ArgumentParser(prog="subagent")
    parser.add_argument("--root", default=None)
    sub = parser.add_subparsers(dest="command")
    cli.add_graph_parser(sub)
    args = parser.parse_args(argv)
    return cli.cmd_graph(args)


# -- the real CLI in a subprocess -------------------------------------------

CLI_SNIPPET = "import sys; from subagent.cli import main; sys.exit(main(sys.argv[1:]))"


def run_cli(args: list[str], cwd: Path, *, timeout: float = 90.0,
            env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", CLI_SNIPPET, *args], cwd=cwd, env=env or dict(os.environ),
        capture_output=True, text=True, timeout=timeout,
    )


async def run_cli_async(args: list[str], cwd: Path, **kw) -> subprocess.CompletedProcess:
    return await asyncio.to_thread(run_cli, args, cwd, **kw)


def cli_json(proc: subprocess.CompletedProcess) -> dict:
    try:
        return json.loads(proc.stdout)
    except ValueError:
        raise AssertionError(
            f"CLI did not print JSON (exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
        ) from None


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
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = await check()
        if last:
            return last
        await asyncio.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}; last = {last!r}")
