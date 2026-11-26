"""The Temporal activities of the graph (spec 3.2, 3.5): ledger, admission, task and gate runs.

All state lives in the shared SQLite file; the activities are thin and idempotent so Temporal
may retry any of them. `bind(ctx)` returns the registered callables for one process.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from temporalio import activity

from ..config import Settings, log
from . import roots
from . import trace as gtrace
from .admission import Admission, LeaseLost
from .dispatch import Dispatcher, DispatchError, TaskOutcome, TaskRequest
from .exceptions import ExceptionQueue
from .interpreter import NodeResult
from .journal import Journal
from .ledger import BudgetRefused, Ledger
from .server import GraphSettings
from .store import Store

NAME = "subagent.graph."
_CANCEL_GRACE_S = 25.0


class GraphConfigError(RuntimeError):
    """Non-retryable: unknown provider, refused workspace."""


class InFlightElsewhere(RuntimeError):
    """Retryable: a live worker holds this attempt."""


@dataclass
class AdmitRequest:
    request_id: str
    attempt: int
    root_run_id: str
    run_path: str
    node_id: str
    lane: str
    priority: int
    reserve_usd: float
    deps: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AdmitRequest:
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


@dataclass
class RunTaskRequest:
    lease_id: str
    task: TaskRequest

    def as_dict(self) -> dict[str, Any]:
        return {"lease_id": self.lease_id, "task": self.task.as_dict()}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunTaskRequest:
        return cls(lease_id=data["lease_id"], task=TaskRequest.from_dict(data["task"]))


@dataclass
class GateRequest:
    request_id: str
    attempt: int
    run_path: str
    node_id: str
    command: str
    workspace: str
    timeout_s: float

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GateRequest:
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


@dataclass
class AccountRequest:
    run_path: str
    parent_path: str | None
    root_run_id: str
    node_id: str | None
    requested_usd: float | None
    pool: str
    graph_id: str
    graph_hash: str

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AccountRequest:
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


@dataclass
class GraphContext:
    store: Store
    ledger: Ledger
    journal: Journal
    admission: Admission
    exceptions: ExceptionQueue
    dispatcher: Dispatcher
    settings: Settings
    gsettings: GraphSettings
    worker_id: str
    trace: Any = None  # additive: a telemetry Trace (or None) for the graph_* records


def _account_dict(account: Any) -> dict[str, Any]:
    out = dataclasses.asdict(account)
    out["remaining_usd"] = account.remaining_usd
    return out


def _trace(ctx: GraphContext, writer: Callable[..., None], **fields: Any) -> None:
    """A trace record is never worth failing an activity for."""
    if ctx.trace is None:
        return
    try:
        writer(ctx.trace, **fields)
    except Exception:  # noqa: BLE001
        log.warning("graph trace write failed", exc_info=True)


def _resolve_workspace(ctx: GraphContext, value: str) -> Path:
    base = Path(ctx.settings.workspace)
    if not value:
        return base
    path = Path(value)
    return path if path.is_absolute() else base / path


def _config_problem(ctx: GraphContext, request: TaskRequest, workspace: Path) -> str | None:
    if request.provider not in ctx.settings.providers:
        return f"unknown_provider: {request.provider!r}"
    if not workspace.is_dir():
        return f"workspace_missing: {workspace} does not exist"
    refusal = ctx.settings.workspace_refusal(workspace)
    if refusal:
        return f"workspace_refused: {refusal}"
    return None


# --- the task journal (spec 3.5) ----------------------------------------------------------


def _settle(ctx: GraphContext, request: TaskRequest, outcome: TaskOutcome,
            lease_id: str | None) -> TaskOutcome:
    """finished -> settled and the lease release, in one transaction. Idempotent."""
    rid, att = request.request_id, request.attempt
    with ctx.store.txn() as conn:
        entry = ctx.journal.get_in(conn, rid, att)
        if entry is not None and entry.phase == "settled":
            return outcome
        done = ctx.ledger.settle(conn, request_id=rid, attempt=att, actual_usd=outcome.cost_usd)
        if outcome.status == "interrupted" and outcome.cost_usd is None:
            outcome = dataclasses.replace(outcome, cost_usd=done.charged_usd)
        ctx.journal.advance(conn, rid, att, "settled", outcome=outcome.as_dict(),
                            charged_usd=done.charged_usd, cost_source=done.cost_source)
        conn.execute("UPDATE leases SET state='released' WHERE request_id=? AND attempt=? "
                     "AND state='active'", (rid, att))
    _trace(ctx, gtrace.write_graph_budget, run_path=request.run_path,
           attempt_id=request.attempt_id, event="settle", amount_usd=done.charged_usd,
           cost_source=done.cost_source,
           remaining_usd=_remaining(ctx, request.run_path))
    return outcome


def _remaining(ctx: GraphContext, run_path: str) -> float | None:
    try:
        return ctx.ledger.account(run_path).remaining_usd
    except KeyError:
        return None


def _finish(ctx: GraphContext, request: TaskRequest, outcome: TaskOutcome,
            lease_id: str | None) -> TaskOutcome:
    """dispatched -> finished (outcome stored) then settle + release."""
    rid, att = request.request_id, request.attempt
    with ctx.store.txn() as conn:
        entry = ctx.journal.get_in(conn, rid, att)
        if entry is not None and entry.phase == "dispatched":
            ctx.journal.advance(conn, rid, att, "finished", outcome=outcome.as_dict(),
                                agent_id=outcome.agent_id, run_id=outcome.run_id)
    return _settle(ctx, request, outcome, lease_id)


def _interrupted(entry_cost: float | None, error: str) -> TaskOutcome:
    return TaskOutcome(status="interrupted", cost_usd=entry_cost, error=error)


def _begin(ctx: GraphContext, request: TaskRequest, lease_id: str
           ) -> tuple[str, TaskOutcome | None]:
    """Decide what run_task does: ("done", outcome) or ("dispatch", None). Blocking."""
    rid, att = request.request_id, request.attempt
    entry = ctx.journal.get(rid, att)
    if entry is None:
        raise RuntimeError(f"no journal entry for {request.attempt_id}; was it admitted?")
    phase = entry.phase
    if phase == "settled":
        return "done", dataclasses.replace(TaskOutcome.from_dict(entry.outcome or {}),
                                           replayed=True)
    if phase == "finished":
        outcome = TaskOutcome.from_dict(entry.outcome or {})
        return "done", dataclasses.replace(_settle(ctx, request, outcome, lease_id),
                                           replayed=True)
    if phase == "dispatched":
        if ctx.admission.workers.alive(entry.worker_id):
            raise InFlightElsewhere(f"{request.attempt_id} is held by {entry.worker_id}")
        with ctx.store.txn() as conn:
            now = ctx.journal.get_in(conn, rid, att)
            if now is not None and now.phase == "dispatched":
                ctx.admission._interrupt(conn, rid, att)  # charges the reservation
                conn.execute("UPDATE leases SET state='released' WHERE request_id=? AND "
                             "attempt=? AND state='active'", (rid, att))
        again = ctx.journal.get(rid, att)
        if again is not None and again.phase == "settled":  # raced with a finishing worker
            return "done", dataclasses.replace(TaskOutcome.from_dict(again.outcome or {}),
                                               replayed=True)
        _trace(ctx, gtrace.write_graph_admission, root_run_id=request.root_run_id,
               attempt_id=request.attempt_id, lane=entry.lane, event="interrupted",
               reason="worker_dead", active=ctx.admission.active(entry.lane),
               cap=ctx.admission.caps.get(entry.lane))
        cost = again.charged_usd if again is not None else None
        return "done", _interrupted(cost, "worker_dead: the worker holding this attempt died")
    if phase in ("interrupted", "released"):
        return "done", _interrupted(entry.charged_usd if phase == "interrupted" else 0.0,
                                    f"attempt {phase}")
    if phase != "admitted":
        raise RuntimeError(f"{request.attempt_id} is {phase}, not admitted")
    try:
        ctx.admission.renew(lease_id, holder=ctx.worker_id)
    except LeaseLost:
        with ctx.store.txn() as conn:
            now = ctx.journal.get_in(conn, rid, att)
            if now is not None and now.phase == "admitted":
                ctx.ledger.release(conn, request_id=rid, attempt=att)
                ctx.journal.advance(conn, rid, att, "released")
        return "done", _interrupted(0.0, "lease_lost: the lease expired before dispatch")
    with ctx.store.txn() as conn:
        now = ctx.journal.get_in(conn, rid, att)
        if now is None or now.phase != "admitted":
            raise InFlightElsewhere(f"{request.attempt_id} changed phase under us")
        ctx.journal.advance(conn, rid, att, "dispatched", worker_id=ctx.worker_id)
    return "dispatch", None


def _dispatch_blocking(ctx: GraphContext, request: TaskRequest, cancelled: threading.Event,
                       beat: dict[str, Any]) -> TaskOutcome:
    workspace = _resolve_workspace(ctx, request.workspace)
    problem = _config_problem(ctx, request, workspace)
    if problem:
        return TaskOutcome(status="failed", lane=request.provider, error=problem, cost_usd=0.0)
    resolved = dataclasses.replace(request, workspace=str(workspace))
    try:
        outcome = ctx.dispatcher.run(resolved, heartbeat=lambda d: beat.update(d or {}),
                                     cancelled=cancelled)
    except DispatchError as exc:  # an infrastructure fault before any run: no spend
        return TaskOutcome(status="interrupted" if exc.retryable else "failed",
                           lane=request.provider, error=f"dispatch_error: {exc}", cost_usd=0.0)
    except Exception as exc:  # noqa: BLE001 - a dispatcher crash is a task failure, not ours
        log.warning("dispatcher crashed on %s", request.attempt_id, exc_info=True)
        return TaskOutcome(status="failed", lane=request.provider,
                           error=f"dispatcher_crashed: {type(exc).__name__}: {exc}")
    if outcome.lane is None:
        outcome = dataclasses.replace(outcome, lane=request.provider)
    return outcome


async def _run_task(ctx: GraphContext, data: dict[str, Any]) -> dict[str, Any]:
    req = RunTaskRequest.from_dict(data)
    request, lease_id = req.task, req.lease_id
    verdict, early = await asyncio.to_thread(_begin, ctx, request, lease_id)
    if verdict == "done":
        assert early is not None
        return early.as_dict()
    loop = asyncio.get_running_loop()
    cancelled = threading.Event()
    beat: dict[str, Any] = {}
    future = loop.run_in_executor(None, _dispatch_blocking, ctx, request, cancelled, beat)
    renew_every = max(1.0, ctx.gsettings.lease_ttl_s / 3)
    interval = min(5.0, renew_every)
    last_renew = time.monotonic()
    lost = False
    try:
        while True:
            done, _ = await asyncio.wait({future}, timeout=interval)
            if done:
                break
            activity.heartbeat(dict(beat) or {"attempt_id": request.attempt_id})
            if time.monotonic() - last_renew >= renew_every:
                try:
                    await asyncio.to_thread(ctx.admission.renew, lease_id,
                                            holder=ctx.worker_id)
                    last_renew = time.monotonic()
                except LeaseLost:
                    lost = True
                    cancelled.set()
                except Exception:  # noqa: BLE001 - a busy database must not kill the run
                    log.warning("lease renew failed", exc_info=True)
        outcome = future.result()
    except asyncio.CancelledError:
        cancelled.set()
        details = activity.cancellation_details()
        try:
            outcome = await asyncio.wait_for(asyncio.shield(future), _CANCEL_GRACE_S)
        except Exception:  # noqa: BLE001 - includes the grace timeout
            outcome = TaskOutcome(status="cancelled", error="cancelled")
        if details is not None and details.worker_shutdown:
            outcome = _interrupted(None, "worker_shutdown")
        await asyncio.shield(asyncio.to_thread(_finish, ctx, request, outcome, lease_id))
        raise
    if lost:
        outcome = _interrupted(None, "lease_lost: the lease expired while running")
    final = await asyncio.to_thread(_finish, ctx, request, outcome, lease_id)
    return final.as_dict()


# --- gates --------------------------------------------------------------------------------


def _run_gate_blocking(ctx: GraphContext, req: GateRequest) -> NodeResult:
    from .. import verify

    workspace = _resolve_workspace(ctx, req.workspace)
    result = verify.run_verification(req.command or "", workspace, ctx.settings, req.timeout_s)
    passed = bool(result.passed)
    return NodeResult(
        id=req.node_id, kind="gate", status="completed" if passed else "failed",
        output={"passed": passed, "reason": result.reason, "exit_code": result.exit_code,
                "command": req.command, "output_tail": getattr(result, "output_tail", "")},
        error=None if passed else f"gate_failed: {result.reason}")


async def _run_gate(ctx: GraphContext, data: dict[str, Any]) -> dict[str, Any]:
    req = GateRequest.from_dict(data)
    future = asyncio.get_running_loop().run_in_executor(None, _run_gate_blocking, ctx, req)
    while True:
        done, _ = await asyncio.wait({future}, timeout=5.0)
        if done:
            return future.result().as_dict()
        activity.heartbeat({"gate": req.node_id})


# --- bind ---------------------------------------------------------------------------------


def bind(ctx: GraphContext, *, queue: str | None = None) -> list[Callable]:
    """The registered activity callables; `queue` ("graph" or "tasks") selects a subset."""
    roots.ensure(ctx.store)

    @activity.defn(name=NAME + "open_account")
    async def open_account(req: dict) -> dict:
        def work() -> dict:
            r = AccountRequest.from_dict(req)
            try:
                if r.parent_path is None:
                    account = ctx.ledger.open_root(
                        root_run_id=r.root_run_id, pool=r.pool, requested_usd=r.requested_usd,
                        graph_id=r.graph_id, graph_hash=r.graph_hash)
                else:
                    account = ctx.ledger.open_child(
                        parent_path=r.parent_path, run_path=r.run_path,
                        node_id=r.node_id or "", requested_usd=r.requested_usd)
            except BudgetRefused as exc:
                return {"refused": exc.code, "message": str(exc)}
            _trace(ctx, gtrace.write_graph_budget, run_path=r.run_path, attempt_id="",
                   event="hold", amount_usd=account.cap_usd, cost_source="",
                   remaining_usd=account.remaining_usd)
            return _account_dict(account)

        return await asyncio.to_thread(work)

    @activity.defn(name=NAME + "close_account")
    async def close_account(run_path: str, state: str) -> dict:
        def work() -> dict:
            for row in ctx.store.read().execute(
                    "SELECT request_id, attempt FROM queue WHERE run_path=?", (run_path,)
            ).fetchall():
                ctx.admission.cancel(row["request_id"], row["attempt"])
            account = ctx.ledger.close(run_path, state=state)
            _trace(ctx, gtrace.write_graph_budget, run_path=run_path, attempt_id="",
                   event="close", amount_usd=account.spent_usd, cost_source="",
                   remaining_usd=account.remaining_usd)
            return _account_dict(account)

        return await asyncio.to_thread(work)

    @activity.defn(name=NAME + "admit")
    async def admit(req: dict) -> dict:
        def work() -> dict:
            a = AdmitRequest.from_dict(req)
            ctx.admission.enqueue(
                request_id=a.request_id, attempt=a.attempt, lane=a.lane, priority=a.priority,
                fairness_key=a.root_run_id, run_path=a.run_path, node_id=a.node_id,
                reserve_usd=a.reserve_usd, deps=a.deps)
            res = ctx.admission.try_acquire(a.request_id, a.attempt)
            if res.status in ("granted", "refused"):
                _trace(ctx, gtrace.write_graph_admission, root_run_id=a.root_run_id,
                       attempt_id=f"{a.request_id}#a{a.attempt}", lane=a.lane,
                       event=res.status, reason=res.reason or "",
                       active=ctx.admission.active(a.lane),
                       cap=ctx.admission.caps.get(a.lane))
            return {"status": res.status, "reason": res.reason, "position": res.position,
                    "lease": dataclasses.asdict(res.lease) if res.lease else None,
                    "poll_s": ctx.gsettings.admit_poll_s}

        return await asyncio.to_thread(work)

    @activity.defn(name=NAME + "cancel_admission")
    async def cancel_admission(request_id: str, attempt: int) -> None:
        await asyncio.to_thread(ctx.admission.cancel, request_id, attempt)

    @activity.defn(name=NAME + "run_task")
    async def run_task(req: dict) -> dict:
        return await _run_task(ctx, req)

    @activity.defn(name=NAME + "run_gate")
    async def run_gate(req: dict) -> dict:
        return await _run_gate(ctx, req)

    @activity.defn(name=NAME + "raise_exception")
    async def raise_exception(kwargs: dict) -> dict:
        def work() -> dict:
            record = ctx.exceptions.raise_(**kwargs)
            _trace(ctx, gtrace.write_graph_exception, exception_id=record.exception_id,
                   root_run_id=record.root_run_id, kind=record.kind, event="raised",
                   needs_answer=record.needs_answer)
            return record.as_dict()

        return await asyncio.to_thread(work)

    @activity.defn(name=NAME + "read_answer")
    async def read_answer(exception_id: str) -> dict | None:
        def work() -> dict | None:
            record = ctx.exceptions.get(exception_id)
            if record.state in ("answered", "delivered") and record.answer is not None:
                return {"answer": record.answer, "note": record.note,
                        "answered_by": record.answered_by}
            if record.state == "closed":
                return {"answer": record.answer, "note": record.note,
                        "answered_by": record.answered_by, "closed": record.closed_reason}
            return None

        return await asyncio.to_thread(work)

    @activity.defn(name=NAME + "record")
    async def record(kind: str, fields: dict) -> None:
        writer = getattr(gtrace, f"write_{kind}", None)
        if ctx.trace is None or writer is None:
            return
        with contextlib.suppress(Exception):
            await asyncio.to_thread(writer, ctx.trace, **fields)

    @activity.defn(name=NAME + "root_state")
    async def root_state(root_run_id: str, state: str) -> None:
        await asyncio.to_thread(roots.set_state, ctx.store, root_run_id, state)

    graph_side = [open_account, close_account, admit, cancel_admission, raise_exception,
                  read_answer, record, root_state]
    task_side = [run_task, run_gate]
    if queue == "graph":
        return graph_side
    if queue == "tasks":
        return task_side
    return graph_side + task_side
