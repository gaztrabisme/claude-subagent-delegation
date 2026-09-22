"""The server without the MCP transport: Settings → Registry → Supervisor.

`InProcessServer` builds the same objects `mcp_server` builds at import time,
so a script (or `subagent doctor --prompt`) can run a real delegation with no
MCP client in the loop. The session root is a fresh directory, so the trace
and metrics of one run are isolated from every other, and the approval socket
is served on a background event loop. The supervisor is never bound to an MCP
session, so an escalation falls through to deny (deterministic tier); the
policy's own deny verdicts are unaffected.
"""

from __future__ import annotations

import dataclasses
import threading
from pathlib import Path
from typing import Any

from .config import Settings
from .guard.classify import protect_provider_homes
from .guard.supervisor import Supervisor
from .runs import Registry, Run


class InProcessServer:
    """Settings + Registry + Supervisor from the package, no MCP transport."""

    def __init__(self, session_root: Path, settings: Settings, *,
                 approval_socket: str | None = None):
        self.session_root = Path(session_root)
        self.session_root.mkdir(parents=True, exist_ok=True)
        # A unix socket path must stay under ~104 bytes on macOS, so it lives
        # beside the session root rather than in the shared tmpdir.
        self.settings = dataclasses.replace(
            settings,
            session_root=self.session_root,
            approval_socket=approval_socket
            or str(self.session_root / "approval.sock"),
        )
        protect_provider_homes(self.settings)
        self.registry = Registry(self.settings, start_reaper=False)
        self.supervisor = Supervisor(self.settings, self.registry, trace=self.registry.trace)
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._serve, daemon=True, name="sam-approval")

    @property
    def trace_path(self) -> Path:
        path = self.registry.trace.path
        return path if path is not None else self.session_root / "trace.jsonl"

    def _serve(self) -> None:
        import anyio

        async def main() -> None:
            async with anyio.create_task_group() as tg:
                await tg.start(self.supervisor.serve)
                self._ready.set()
                while not self._stop.is_set():
                    await anyio.sleep(0.2)
                tg.cancel_scope.cancel()

        try:
            anyio.run(main)
        except BaseException as exc:  # noqa: BLE001 - reported by start()
            self._error = exc
            self._ready.set()

    def start(self) -> InProcessServer:
        self._thread.start()
        if not self._ready.wait(10) or self._error is not None:
            raise RuntimeError(f"approval socket did not start: {self._error!r}")
        return self

    def delegate(self, *, provider: str, task: str, verification: str, workspace: Path,
                 fallback: str = "none", name: str | None = None) -> Run:
        """Create an agent on `provider` and submit its delegate run."""
        agent = self.registry.create_agent(
            name, workspace.resolve(), provider=provider, fallback=fallback
        )
        error = agent.wait_ready(60.0)
        if error is not None:
            agent.close("runtime failed to start")
            raise RuntimeError(f"runtime failed to start on provider {provider}: {error}")
        return agent.delegate(task, verification)

    def close_agent(self, agent_id: str) -> None:
        agent = self.registry.find_agent(agent_id)
        if agent is not None and not agent.closed:
            agent.close("script finished")

    def stop(self) -> None:
        try:
            self.registry.shutdown()
        finally:
            self._stop.set()
            self._thread.join(timeout=5)
            self.supervisor.cleanup()


__all__ = ["InProcessServer"]
