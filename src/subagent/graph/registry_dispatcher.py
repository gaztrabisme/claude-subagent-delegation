"""RegistryDispatcher: runs one graph task through the existing `runs.Registry` (spec 8.1).

It uses only public `Registry`/`Agent` calls, the way `mcp_server.delegate` does, with
`fallback="none"` (a lease is per provider). Journal-before-report, not `request_once`, is the
cross-process idempotency guarantee.
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Callable
from pathlib import Path

from ..config import Settings
from ..runs import Agent, Registry, RegistryError, Run
from .dispatch import DispatchError, TaskOutcome, TaskRequest

_SLICE_S = 5.0
_RESULT_TEXT_CAP = 4000
_RESULT_JSON_CAP = 64 * 1024
_TCR_CAP = 16 * 1024
_CANCEL_GRACE_S = 10.0
_STATE = {"completed": "completed", "completed_unverified": "unverified",
          "failed": "failed", "cancelled": "cancelled"}


def _read_fresh(path: Path, cap: int, since: float) -> str | None:
    """Text of `path` when it was written at or after `since`; None otherwise."""
    try:
        if path.stat().st_mtime < since - 1.0:
            return None
        return path.read_text(encoding="utf-8", errors="replace")[:cap + 1]
    except OSError:
        return None


class RegistryDispatcher:
    def __init__(self, registry: Registry, settings: Settings):
        self.registry = registry
        self.settings = settings

    def run(self, request: TaskRequest, *, heartbeat: Callable[[dict], None],
            cancelled: threading.Event) -> TaskOutcome:
        workspace = Path(request.workspace)
        agent: Agent | None = None
        try:
            try:
                agent = self.registry.create_agent(
                    name=f"graph:{request.node_id}", workspace=workspace,
                    provider=request.provider, model=request.model, fallback="none",
                    task=request.prompt, instructions=request.instructions)
            except RegistryError as exc:
                text = str(exc)
                if "agent limit reached" in text:
                    raise DispatchError(text, retryable=True) from exc
                if "unknown provider" in text:
                    raise DispatchError(text, retryable=False) from exc
                return TaskOutcome(status="failed", error=text)
            started = _now()
            error = agent.wait_ready(60.0)
            if error is not None:
                return TaskOutcome(status="failed", agent_id=agent.agent_id, error=str(error))
            run = agent.delegate(
                request.prompt, request.verification,
                parent={"graph_root": request.root_run_id, "run_path": request.run_path,
                        "node_id": request.node_id, "request_id": request.attempt_id,
                        "role": request.role},
                request_id=request.attempt_id, timeout=request.timeout_s,
                max_steps=request.max_steps)
            was_cancelled = False
            while not run.done.wait(_SLICE_S):
                heartbeat({"agent_id": agent.agent_id, "run_id": run.run_id})
                if cancelled.is_set() and not was_cancelled:
                    was_cancelled = True
                    with contextlib.suppress(RegistryError):
                        self.registry.cancel(agent.agent_id, "graph cancelled")
                    run.done.wait(_CANCEL_GRACE_S)
                    break
            if was_cancelled:
                return self._outcome(run, agent, workspace, started, forced="cancelled")
            return self._outcome(run, agent, workspace, started)
        finally:
            if agent is not None:
                with contextlib.suppress(Exception):  # a close failure must not hide the result
                    agent.close("graph task finished")

    @staticmethod
    def _outcome(run: Run, agent: Agent, workspace: Path, started: float,
                 forced: str | None = None) -> TaskOutcome:
        detail = run.detail()
        status = forced or _STATE.get(run.state, "failed")
        if status == "failed" and not forced and run.finish_reason in ("refused", "no_lane"):
            status = "lane_refused"
        text = str(detail.get("result") or run.result_text or run.final_response or "")
        result_json: dict | None = None
        raw = _read_fresh(workspace / ".subagent" / "result.json", _RESULT_JSON_CAP, started)
        if raw is not None and len(raw) <= _RESULT_JSON_CAP:
            try:
                loaded = json.loads(raw)
                result_json = loaded if isinstance(loaded, dict) else None
            except ValueError:
                result_json = None
        verdict = result_json.get("verdict") if result_json else None
        tcr = _read_fresh(workspace / ".subagent" / "test_change_request.md", _TCR_CAP, started)
        cost = run.cost or {}
        usd = cost.get("provider_usd")
        return TaskOutcome(
            status=status, run_id=run.run_id, agent_id=agent.agent_id, lane=run.lane,
            verification=detail.get("verification"), result_text=text[:_RESULT_TEXT_CAP],
            result_json=result_json, verdict=verdict if isinstance(verdict, str) else None,
            usage=dict(detail.get("usage") or {}),
            cost_usd=float(usd) if isinstance(usd, (int, float)) else None,
            cost_kind=cost.get("kind") if isinstance(cost.get("kind"), str) else None,
            guard_denials=[v for v in (run.guard_verdicts or [])
                           if "deny" in {str(v.get(k, "")).lower()
                                         for k in ("action", "verdict", "decision")}],
            test_change_request=tcr[:_TCR_CAP] if tcr else None,
            needs_human=bool(run.needs_human),
            error=(str(detail["error"]) if detail.get("error") else None))


def _now() -> float:
    import time

    return time.time()
