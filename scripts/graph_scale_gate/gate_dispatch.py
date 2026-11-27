"""Dispatchers: ``fake`` (sleeps, made-up runs) and ``real`` (a subagent Registry run).

Both return ``{"outcome", "run_id", "cost_usd", "verified"}`` and both leave a ``kind: run``
record in a trace the audit can read: fake writes ``<run_dir>/fake_trace.jsonl``; real uses
the trace the subagent Settings name (the server's own session trace).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import threading
import time
import uuid
from pathlib import Path
from typing import Any

TASK = ("Create a file named ok.txt in the workspace containing exactly the word OK. "
        "Do nothing else.")
VERIFICATION = "test -s ok.txt"
MAX_STEPS = 6
TIMEOUT_SECONDS = 300
FAKE_FAIL_FIRST = frozenset({"root-2-r007", "root-1-r015"})  # attempt 1 fails, attempt 2 passes


def _append_line(path: Path, record: dict[str, Any]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


async def fake_dispatch(req: dict[str, Any], attempt: int, cfg: dict[str, Any]) -> dict[str, Any]:
    await asyncio.sleep(random.uniform(0.2, 0.5))
    run_id = f"fake-{uuid.uuid4().hex[:12]}"
    cost = float(cfg["estimates"].get(req["lane"], 0.0))
    ok = not (attempt == 1 and req["request_id"] in FAKE_FAIL_FIRST)
    if not ok:
        cost = 0.0
    _append_line(Path(cfg["run_dir"]) / "fake_trace.jsonl", {
        "schema": 3, "ts": time.time(), "kind": "run", "run_id": run_id,
        "provider": req["lane"], "state": "completed" if ok else "completed_unverified",
        "cost": {"provider_usd": cost, "counterfactual_usd": cost * 3,
                 "kind": "api" if cost else "local", "note": "fake"},
    })
    return {"outcome": "completed" if ok else "failed", "run_id": run_id,
            "cost_usd": cost, "verified": ok}


class RealDispatcher:
    """Runs each child through a subagent Registry built as mcp_server builds it."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        from subagent import config
        from subagent.guard.supervisor import Supervisor
        from subagent.runs import Registry

        self.settings = config.load(Path.cwd())  # same call mcp_server makes at import
        self.registry = Registry(self.settings)
        self.supervisor = Supervisor(self.settings, self.registry, trace=self.registry.trace)
        self.run_dir = Path(cfg["run_dir"])
        self.trace_path = self.registry.trace.path

    async def serve_supervisor(self, task_group: Any) -> None:
        """Start the approval socket the worker hooks talk to (mcp_server._lifespan)."""
        await task_group.start(self.supervisor.serve)

    def shutdown(self) -> None:
        self.supervisor.cleanup()
        self.registry.shutdown()

    def _run_blocking(self, req: dict[str, Any], attempt: int, stop: threading.Event
                      ) -> dict[str, Any]:
        from subagent.runs import Agent, RegistryError

        lane = req["lane"]
        workspace = self.run_dir / "workspaces" / f"{req['request_id']}-a{attempt}"
        workspace.mkdir(parents=True, exist_ok=True)
        failed: dict[str, Any] = {"outcome": "failed", "run_id": None, "cost_usd": 0.0,
                                  "verified": False}
        agent = None
        try:
            chosen = self.registry.select_provider(lane, "none")
            Agent.validate_caps(TIMEOUT_SECONDS, MAX_STEPS)
            agent = self.registry.create_agent(
                name=f"gate-{req['request_id']}-a{attempt}", workspace=workspace.resolve(),
                provider=chosen.name, fallback="none", task=TASK, provider_explicit=True)
            start_error = agent.wait_ready(60.0)
            if start_error is not None:
                return failed
            run = agent.delegate(TASK, VERIFICATION, timeout=TIMEOUT_SECONDS,
                                 max_steps=MAX_STEPS)
            deadline = time.monotonic() + TIMEOUT_SECONDS + 60
            while not run.done.wait(1.0):
                if stop.is_set() or time.monotonic() > deadline:
                    break
            cost = (run.cost or {}).get("provider_usd")
            verified = run.state == "completed"
            return {"outcome": "completed" if verified else "failed", "run_id": run.run_id,
                    "cost_usd": float(cost) if isinstance(cost, (int, float)) else 0.0,
                    "verified": verified}
        except RegistryError:
            return failed
        finally:
            if agent is not None:
                with contextlib.suppress(Exception):  # a close failure must not hide the result
                    agent.close("gate child finished")  # frees the provider slot

    async def dispatch(self, req: dict[str, Any], attempt: int, cfg: dict[str, Any]
                       ) -> dict[str, Any]:
        stop = threading.Event()
        try:
            return await asyncio.to_thread(self._run_blocking, req, attempt, stop)
        except asyncio.CancelledError:
            stop.set()
            raise


_real: RealDispatcher | None = None


def real_dispatcher(cfg: dict[str, Any]) -> RealDispatcher:
    global _real
    if _real is None:
        _real = RealDispatcher(cfg)
    return _real


def real_trace_path() -> Path:
    """The trace file a real run writes, resolved from Settings as Registry does."""
    from subagent import config
    from subagent.telemetry.trace import open_trace

    settings = config.load(Path.cwd())
    path = open_trace(settings.trace, settings.session_root).path
    if path is None:
        raise SystemExit("the subagent trace is off; the gate needs it on")
    return Path(path)
