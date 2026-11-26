"""The nine `graph_*` MCP tools (spec 9): thin wrappers over `GraphService`.

`register(app)` adds them to an MCP server. The service is built on first use, so importing this
module or registering the tools never touches Temporal or the state directory.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import INSTALL_HINT, GraphUnavailable
from .service import GraphService

TOOL_NAMES = ("graph_validate", "graph_start", "graph_status", "graph_pause", "graph_resume",
              "graph_cancel", "graph_list", "graph_exceptions", "graph_answer")


def _default_factory() -> GraphService:
    from .. import config

    return GraphService(config.load(Path.cwd()))


def register(app: Any, *, service_factory: Callable[[], GraphService] | None = None) -> list[str]:
    """Register the nine tools on `app`; return their names."""
    factory = service_factory or _default_factory
    holder: dict[str, Any] = {}

    def service() -> Any:
        if "service" not in holder:
            holder["service"] = factory()
        return holder["service"]

    def failure(code: str, message: str) -> dict[str, Any]:
        return {"ok": False, "error": {"code": code, "message": message}}

    async def run(method: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return await getattr(service(), method)(*args, **kwargs)
        except GraphUnavailable as exc:
            return failure("unavailable", str(exc) or INSTALL_HINT)
        except (ValueError, TypeError) as exc:
            return failure("invalid_argument", str(exc))

    @app.tool(name="graph_validate")
    async def graph_validate(graph: dict | str) -> dict[str, Any]:
        """Validate a graph document (object, JSON text or file path) without starting it."""
        return await run("validate", graph)

    @app.tool(name="graph_start")
    async def graph_start(
        graph: dict | str,
        inputs: dict | None = None,
        pool: str = "default",
        budget_usd: float | None = None,
        priority: int = 3,
        request_id: str | None = None,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """Start a durable graph run, or attach to the run already started with `request_id`."""
        return await run("start", graph, inputs=inputs or {}, pool=pool, budget_usd=budget_usd,
                         priority=priority, request_id=request_id, workspace=workspace)

    @app.tool(name="graph_status")
    async def graph_status(root_run_id: str, detail: bool = False) -> dict[str, Any]:
        """State, budget, node progress and waiting items of one run."""
        return await run("status", root_run_id, detail=detail)

    @app.tool(name="graph_pause")
    async def graph_pause(root_run_id: str, reason: str | None = None) -> dict[str, Any]:
        """Stop starting new tasks in a run; running tasks finish."""
        return await run("pause", root_run_id, reason)

    @app.tool(name="graph_resume")
    async def graph_resume(root_run_id: str) -> dict[str, Any]:
        """Continue a paused run."""
        return await run("resume", root_run_id)

    @app.tool(name="graph_cancel")
    async def graph_cancel(root_run_id: str, reason: str) -> dict[str, Any]:
        """Cancel a run; `reason` is required."""
        return await run("cancel", root_run_id, reason)

    @app.tool(name="graph_list")
    async def graph_list(state: str | None = None, limit: int = 50) -> dict[str, Any]:
        """List runs, newest first."""
        return await run("list", state=state, limit=limit)

    @app.tool(name="graph_exceptions")
    async def graph_exceptions(
        root_run_id: str | None = None,
        state: str = "open",
        kind: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """The owner-decision and exception queue, with a digest."""
        return await run("exceptions", root_run_id=root_run_id, state=state, kind=kind,
                         limit=limit)

    @app.tool(name="graph_answer")
    async def graph_answer(
        exception_id: str,
        answer: str,
        note: str | None = None,
        answered_by: str = "orchestrator",
    ) -> dict[str, Any]:
        """Answer one queued exception; the run continues from the answer."""
        return await run("answer", exception_id, answer, note=note, answered_by=answered_by)

    return list(TOOL_NAMES)
