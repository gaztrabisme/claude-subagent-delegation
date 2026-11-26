"""GraphService: the client API the MCP tools and the CLI call (spec 9).

Every method returns `{"ok": True, ...}` or `{"ok": False, "error": {...}}`. Temporal is
imported inside functions only, so `graph_validate` works without it. State comes from the
ledger (always available) and from Temporal (describe and the `status` query) when reachable.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import Settings
from . import INSTALL_HINT, GraphUnavailable, ids, roots, temporal_available
from .exceptions import ExceptionError, ExceptionQueue
from .interpreter import GraphInput
from .journal import Journal
from .ledger import BudgetRefused, Ledger
from .schema import GraphError, graph_hash, load_graph, parse
from .server import DevServer, GraphSettings, _private_dir, private_file, settings_from
from .store import Store

_EXC_STATES = ("open", "answered", "delivered", "closed", "all")
_CLOSED_STATUS = {"COMPLETED": None, "FAILED": "failed", "CANCELED": "cancelled",
                  "TERMINATED": "cancelled", "TIMED_OUT": "failed",
                  "CONTINUED_AS_NEW": "failed"}


def _error(code: str, message: str, problems: list[dict[str, Any]] | None = None
           ) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if problems is not None:
        error["problems"] = problems
    return {"ok": False, "error": error}


def _iso(value: Any) -> Any:
    """Epoch seconds as an ISO-8601 UTC string ending in Z; anything else unchanged."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    return datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _no_extra() -> dict[str, Any] | None:
    """The `unavailable` error when the graph extra is missing, else None (spec 9)."""
    return None if temporal_available() else _error("unavailable", INSTALL_HINT)


def _problems(err: GraphError) -> list[dict[str, Any]]:
    return [{"path": p.path, "code": p.code, "message": p.message} for p in err.problems]


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _count_nodes(doc: Mapping[str, Any]) -> int:
    def walk(nodes: Any) -> int:
        total = 0
        for node in nodes or ():
            if not isinstance(node, Mapping):
                continue
            total += 1
            total += walk(node.get("branches")) + walk(node.get("body"))
            fe = node.get("for_each")
            if isinstance(fe, Mapping) and isinstance(fe.get("node"), Mapping):
                total += walk([fe["node"]])
        return total

    named = doc.get("graphs") or {}
    return walk(doc.get("body")) + sum(
        walk(g.get("body")) for g in named.values() if isinstance(g, Mapping))


class GraphService:
    def __init__(self, settings: Settings, *, gsettings: GraphSettings | None = None) -> None:
        self.settings = settings
        self.gs = gsettings if gsettings is not None else settings_from(settings)
        self._store: Store | None = None
        self._client: Any = None
        self._trace: Any = None

    @property
    def trace(self) -> Any:
        """The configured `[core].trace` (spec 10), opened on first use."""
        if self._trace is None:
            from ..telemetry.trace import open_trace

            self._trace = open_trace(self.settings.trace, self.settings.session_root)
        return self._trace

    # -- state files ----
    @property
    def store(self) -> Store:
        if self._store is None:
            _private_dir(self.gs.state_dir)
            self._store = Store(self.gs.state_dir / "graph.sqlite3")
            roots.ensure(self._store)
        return self._store

    @property
    def ledger(self) -> Ledger:
        return Ledger(self.store, max_depth=self.gs.max_depth)

    @property
    def queue(self) -> ExceptionQueue:
        return ExceptionQueue(self.store)

    def _ensure_pools(self) -> None:
        ledger = self.ledger
        ledger.ensure_pool("default", self.gs.pools.get("default", self.gs.budget_usd))
        for name, cap in self.gs.pools.items():
            ledger.ensure_pool(name, cap)

    # -- documents ----
    def _document(self, graph: Any, workspace: str | None) -> dict[str, Any]:
        """A dict from a dict, JSON text, or a path (relative to the workspace)."""
        if isinstance(graph, Mapping):
            return dict(graph)
        if isinstance(graph, bytes):
            graph = graph.decode("utf-8", "replace")
        if not isinstance(graph, str):
            raise ValueError("graph must be an object, JSON text or a file path")
        text = graph
        if not text.lstrip().startswith(("{", "[")) and "\n" not in text and len(text) < 4096:
            base = Path(workspace) if workspace else Path(self.settings.workspace)
            path = Path(text).expanduser()
            path = path if path.is_absolute() else base / path
            if path.is_file():
                text = path.read_text(encoding="utf-8")
        return parse(text)

    # -- Temporal ----
    async def _temporal(self, *, worker: bool = False) -> Any:
        if not temporal_available():
            raise GraphUnavailable(INSTALL_HINT)
        if self._client is not None:
            if worker and self.gs.autostart:  # a cached client must not skip the worker check
                await asyncio.to_thread(self._ensure_worker)
            return self._client
        from temporalio.client import Client

        if self.gs.autostart:
            server = DevServer(self.gs.state_dir, address=self.gs.address)
            if not (await asyncio.to_thread(server.health))["ok"]:
                info = await asyncio.to_thread(server.start)
                self.gs = _with_address(self.gs, info.address)
            else:  # a healthy server may sit on the address recorded in server.json
                known = await asyncio.to_thread(server.info)
                if known is not None:
                    self.gs = _with_address(self.gs, known.address)
            if worker:
                await asyncio.to_thread(self._ensure_worker)
        try:
            self._client = await asyncio.wait_for(Client.connect(self.gs.address), 15)
        except Exception as exc:  # noqa: BLE001
            raise GraphUnavailable(
                f"Temporal server not reachable at {self.gs.address}: {exc}") from exc
        return self._client

    def _ensure_worker(self) -> None:
        """Autostart (spec 8.2): a live worker, else one detached `subagent graph worker`
        that runs on this service's resolved address, state dir and task-queue prefix.
        Raises GraphUnavailable when no worker is alive and none could be started."""
        import fcntl
        import os
        import subprocess
        import sys
        import time

        from .admission import Workers

        state_dir = _private_dir(self.gs.state_dir)
        workers = Workers(self.store)
        with open(private_file(state_dir / "worker.lock"), "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if any(workers.alive(w["worker_id"]) for w in workers.list()):
                return
            known = {w["worker_id"] for w in workers.list()}
            code = "import sys; from subagent.cli import main; sys.exit(main(sys.argv[1:]))"
            argv = [sys.executable, "-P", "-c", code, "graph", "worker",
                    "--address", self.gs.address, "--state-dir", str(self.gs.state_dir),
                    "--task-queue-prefix", self.gs.task_queue_prefix]
            try:
                with open(private_file(state_dir / "worker.log"), "ab") as log:
                    proc = subprocess.Popen(
                        argv, cwd=str(self.settings.workspace),
                        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                        start_new_session=True, env=dict(os.environ))
            except OSError as exc:
                raise GraphUnavailable(f"graph worker could not be started: {exc}") from exc
            deadline = time.monotonic() + 30.0  # hold the lock until it has registered
            while time.monotonic() < deadline:
                if any(w["worker_id"] not in known for w in workers.list()):
                    return
                if proc.poll() is not None:
                    raise GraphUnavailable(
                        f"graph worker exited with {proc.returncode} before registering; "
                        f"see {state_dir / 'worker.log'}")
                time.sleep(0.2)
            raise GraphUnavailable(
                f"graph worker did not register within 30s; see {state_dir / 'worker.log'}")

    async def _view(self, root: str) -> dict[str, Any]:
        """What Temporal says about `root`: reachable, exists, status, snapshot, result."""
        from temporalio.client import WorkflowExecutionStatus
        from temporalio.service import RPCError, RPCStatusCode

        try:
            client = await self._temporal()
            handle = client.get_workflow_handle(root)
            desc = await asyncio.wait_for(handle.describe(), 15)
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                return {"reachable": True, "exists": False}
            return {"reachable": False, "exists": False}
        except Exception:  # noqa: BLE001 - unreachable or not installed
            return {"reachable": False, "exists": False}
        view: dict[str, Any] = {"reachable": True, "exists": True, "status": desc.status.name,
                                "start_time": desc.start_time.timestamp(),
                                "close_time": desc.close_time.timestamp() if desc.close_time
                                else None, "snapshot": None, "result": None}
        if desc.status == WorkflowExecutionStatus.RUNNING:
            with contextlib.suppress(Exception):
                view["snapshot"] = await asyncio.wait_for(handle.query("status"), 20)
        elif desc.status == WorkflowExecutionStatus.COMPLETED:
            with contextlib.suppress(Exception):
                view["result"] = await asyncio.wait_for(handle.result(), 20)
        return view

    # -- validate ----
    async def validate(self, graph: Any) -> dict[str, Any]:
        try:
            doc = self._document(graph, None)
            loaded = load_graph(doc)
        except GraphError as err:
            return _error("invalid_graph", str(err), _problems(err))
        except (ValueError, OSError) as err:
            return _error("invalid_argument", str(err))
        return {"ok": True, "graph_id": loaded.id, "version": loaded.version,
                "graph_hash": graph_hash(doc), "nodes": _count_nodes(doc), "problems": []}

    # -- start ----
    async def start(self, graph: Any, *, inputs: dict[str, Any] | None = None,
                    pool: str = "default", budget_usd: float | None = None, priority: int = 3,
                    request_id: str | None = None, workspace: str | None = None
                    ) -> dict[str, Any]:
        try:
            doc = self._document(graph, workspace)
            loaded = load_graph(doc)
        except GraphError as err:
            return _error("invalid_graph", str(err), _problems(err))
        except (ValueError, OSError) as err:
            return _error("invalid_argument", str(err))
        if inputs is not None and not isinstance(inputs, Mapping):
            return _error("invalid_argument", "inputs must be an object")
        if not isinstance(priority, int) or not 1 <= priority <= 5:
            return _error("invalid_argument", "priority must be an integer from 1 to 5")
        if budget_usd is not None and (isinstance(budget_usd, bool)
                                       or not isinstance(budget_usd, (int, float))
                                       or budget_usd < 0):
            return _error("invalid_argument", "budget_usd must be a non-negative number")
        undeclared = sorted(set(inputs or {}) - set(loaded.inputs))
        if undeclared:
            return _error("invalid_argument", f"undeclared inputs: {undeclared}")
        merged = dict(loaded.inputs)
        merged.update(inputs or {})
        digest, inputs_digest = graph_hash(doc), _digest(merged)
        root = ids.new_root_run_id(request_id)
        try:
            from temporalio.common import WorkflowIDReusePolicy
            from temporalio.exceptions import WorkflowAlreadyStartedError

            client = await self._temporal(worker=True)
        except GraphUnavailable as err:
            return _error("unavailable", str(err))
        except ImportError:
            return _error("unavailable", INSTALL_HINT)
        existing = await asyncio.to_thread(roots.get, self.store, root)
        if existing is not None and request_id is not None:
            account = await asyncio.to_thread(self._account_or_none, root)
            stored_hash = account.graph_hash if account is not None else None
            stored_inputs = existing.get("inputs_digest")
            if (stored_hash not in (None, digest)
                    or stored_inputs not in (None, inputs_digest)):
                return _error("conflict", "request_id reused with a different graph")
        try:
            account = await asyncio.to_thread(
                self._open_root, root, pool,
                budget_usd if budget_usd is not None else loaded.budget_usd, loaded.id, digest)
        except BudgetRefused as exc:
            code = "budget_exhausted" if exc.code == "budget_exhausted" else "invalid_argument"
            return _error(code, f"{exc.code}: {exc}")
        await asyncio.to_thread(
            roots.register, self.store, root, graph_id=loaded.id,
            graph_version=loaded.version, inputs_digest=inputs_digest)
        gi = GraphInput(doc=doc, inputs=merged, root_run_id=root, run_path=root,
                        parent_path=None, depth=0, priority=priority, pool=pool)
        attached, state = False, "running"
        try:
            await client.start_workflow(
                "subagent.graph.GraphWorkflow", gi.as_dict(), id=root,
                task_queue=f"{self.gs.task_queue_prefix}-graph",
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
        except WorkflowAlreadyStartedError:
            attached = True
            view = await self._view(root)
            state = self._state(view, await asyncio.to_thread(roots.get, self.store, root),
                                account)
        except Exception as exc:  # noqa: BLE001
            return _error("unavailable", f"could not start the workflow: {exc}")
        return {"ok": True, "root_run_id": root, "workflow_id": root, "graph_id": loaded.id,
                "graph_hash": digest, "state": state, "attached": attached,
                "budget": {"pool": pool, "cap_usd": account.cap_usd,
                           "remaining_usd": account.remaining_usd, "capped": account.capped}}

    def _account_or_none(self, root: str) -> Any:
        try:
            return self.ledger.account(root)
        except KeyError:
            return None

    def _open_root(self, root: str, pool: str, budget_usd: float | None, graph_id: str,
                   digest: str) -> Any:
        self._ensure_pools()
        return self.ledger.open_root(root_run_id=root, pool=pool, requested_usd=budget_usd,
                                     graph_id=graph_id, graph_hash=digest)

    # -- status ----
    def _budget(self, account: Any) -> dict[str, Any]:
        return {"pool": (account.parent_path or "").removeprefix("pool:"),
                "cap_usd": account.cap_usd, "reserved_usd": account.reserved_usd,
                "held_usd": account.held_usd, "spent_usd": account.spent_usd,
                "remaining_usd": account.remaining_usd,
                "overrun_usd": round(max(0.0, account.spent_usd - account.cap_usd), 6),
                "capped": account.capped}

    @staticmethod
    def _state(view: dict[str, Any], row: dict[str, Any] | None, account: Any) -> str:
        if view.get("exists"):
            status = view["status"]
            if status == "RUNNING":
                snap = view.get("snapshot") or {}
                live = str(snap.get("state") or "running")
                # A run that has finished its work but not yet closed in Temporal still
                # reads as running: `completed` is only reported with its result in hand.
                return "running" if live in roots.TERMINAL else live
            if status == "COMPLETED":
                return str((view.get("result") or {}).get("status")
                           or (row or {}).get("state") or "completed")
            return _CLOSED_STATUS.get(status) or "failed"
        if account.state == "dead":
            return "failed"
        return str((row or {}).get("state") or "running")

    async def status(self, root_run_id: str, *, detail: bool = False) -> dict[str, Any]:
        if (missing := _no_extra()) is not None:
            return missing
        account = await asyncio.to_thread(self._account_or_none, root_run_id)
        if account is None or account.depth != 0:
            return _error("not_found", f"unknown root run {root_run_id!r}")
        row = await asyncio.to_thread(roots.get, self.store, root_run_id)
        view = await self._view(root_run_id)
        snap = view.get("snapshot") or {}
        tree = await asyncio.to_thread(self.ledger.tree, root_run_id)
        out: dict[str, Any] = {
            "ok": True, "root_run_id": root_run_id, "graph_id": account.graph_id,
            "graph_version": (row or {}).get("graph_version"),
            "state": self._state(view, row, account), "paused": bool(snap.get("paused")),
            "started_at": _iso(view.get("start_time") or (row or {}).get("started_at")
                               or account.created_at),
            "updated_at": _iso(view.get("close_time") or (row or {}).get("updated_at")
                               or account.created_at),
            "budget": self._budget(account),
            "nodes": snap.get("nodes") or (view.get("result") or {}).get("nodes") or {},
            "children": [{"run_path": a.run_path, "state": a.state, "cap_usd": a.cap_usd,
                          "spent_usd": a.spent_usd} for a in tree[1:]],
            "waiting": {"admission": list(snap.get("queued") or []),
                        "exceptions": list(snap.get("waiting_exceptions") or [])},
            "result": view.get("result")}
        if not view.get("reachable"):
            out["temporal"] = "unreachable"
        if detail:
            journal = await asyncio.to_thread(Journal(self.store).for_root, root_run_id)
            out["journal"] = [_jsonable(e) for e in journal]
            out["tree"] = [_jsonable(a) for a in tree]
        return out

    # -- pause, resume, cancel ----
    async def _control(self, root_run_id: str, action: str, *args: Any) -> dict[str, Any] | None:
        """None when the signal was accepted; otherwise the error to return."""
        if (missing := _no_extra()) is not None:
            return missing
        account = await asyncio.to_thread(self._account_or_none, root_run_id)
        if account is None or account.depth != 0:
            return _error("not_found", f"unknown root run {root_run_id!r}")
        try:
            client = await self._temporal()
        except GraphUnavailable as err:
            return _error("unavailable", str(err))
        view = await self._view(root_run_id)
        if not view.get("reachable"):
            return _error("unavailable", f"Temporal server not reachable at {self.gs.address}")
        if not view.get("exists") or view.get("status") != "RUNNING":
            return _error("conflict", f"root run {root_run_id} is closed")
        handle = client.get_workflow_handle(root_run_id)
        try:
            if action == "cancel":
                await handle.cancel()
            else:
                await handle.signal(action, *args)
        except Exception as exc:  # noqa: BLE001
            return _error("conflict", f"could not {action} {root_run_id}: {exc}")
        return None

    async def pause(self, root_run_id: str, reason: str | None = None) -> dict[str, Any]:
        failed = await self._control(root_run_id, "pause", reason)
        return failed or {"ok": True, "root_run_id": root_run_id, "state": "paused",
                          "accepted": True}

    async def resume(self, root_run_id: str) -> dict[str, Any]:
        failed = await self._control(root_run_id, "resume")
        return failed or {"ok": True, "root_run_id": root_run_id, "state": "running",
                          "accepted": True}

    async def cancel(self, root_run_id: str, reason: str) -> dict[str, Any]:
        if not isinstance(reason, str) or not reason.strip():
            return _error("invalid_argument", "reason must be a non-empty string")
        failed = await self._control(root_run_id, "cancel")
        return failed or {"ok": True, "root_run_id": root_run_id, "state": "cancelling",
                          "accepted": True}

    # -- list and exceptions ----
    async def list(self, *, state: str | None = None, limit: int = 50) -> dict[str, Any]:
        if (missing := _no_extra()) is not None:
            return missing
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            return _error("invalid_argument", "limit must be an integer from 1 to 500")

        def work() -> list[dict[str, Any]]:
            ledger, queue = self.ledger, self.queue
            out: list[dict[str, Any]] = []
            for row in roots.listing(self.store):
                if state is not None and row["state"] != state:
                    continue
                try:
                    account = ledger.account(row["root_run_id"])
                except KeyError:
                    continue
                opened = queue.list(state="open", root_run_id=row["root_run_id"], limit=10_000)
                out.append({"root_run_id": row["root_run_id"], "graph_id": row["graph_id"],
                            "state": row["state"], "started_at": _iso(row["started_at"]),
                            "updated_at": _iso(row["updated_at"]), "spent_usd": account.spent_usd,
                            "cap_usd": account.cap_usd, "open_exceptions": len(opened)})
                if len(out) >= limit:
                    break
            return out

        return {"ok": True, "runs": await asyncio.to_thread(work)}

    async def exceptions(self, *, root_run_id: str | None = None, state: str = "open",
                         kind: str | None = None, limit: int = 100) -> dict[str, Any]:
        if (missing := _no_extra()) is not None:
            return missing
        if state not in _EXC_STATES:
            return _error("invalid_argument", f"state must be one of {list(_EXC_STATES)}")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            return _error("invalid_argument", "limit must be a positive integer")

        def work() -> dict[str, Any]:
            queue = self.queue
            records = queue.list(state=state, root_run_id=root_run_id, kind=kind, limit=limit)
            return {"ok": True, "exceptions": [r.as_dict() for r in records],
                    "digest": queue.digest()}

        return await asyncio.to_thread(work)

    # -- answer ----
    async def answer(self, exception_id: str, answer: str, *, note: str | None = None,
                     answered_by: str = "orchestrator") -> dict[str, Any]:
        if (missing := _no_extra()) is not None:
            return missing
        queue = self.queue
        try:
            record = await asyncio.to_thread(queue.answer, exception_id, answer, note=note,
                                             answered_by=answered_by)
        except ExceptionError as err:
            return _error(err.code, str(err))
        delivered = False
        with contextlib.suppress(Exception):  # a trace record never fails the answer
            from . import trace as gtrace

            gtrace.write_graph_exception(
                self.trace, exception_id=record.exception_id, root_run_id=record.root_run_id,
                exception_kind=record.kind, event="answered", needs_answer=record.needs_answer)
        if record.state == "answered":  # SQLite first, then the wake-up signal
            try:
                client = await self._temporal()
                from .worker import deliver

                class _Ctx:  # the part of a worker context `deliver` uses
                    exceptions = queue
                    trace = self.trace

                delivered = await deliver(client, _Ctx(), record)  # type: ignore[arg-type]
            except GraphUnavailable:
                delivered = False
            except Exception:  # noqa: BLE001 - the sweeper delivers it later
                delivered = False
            if delivered:
                record = await asyncio.to_thread(queue.get, exception_id)
        elif record.state == "delivered":
            delivered = True
        return {"ok": True, "exception_id": exception_id, "state": record.state,
                "delivered": delivered}


def _with_address(gs: GraphSettings, address: str) -> GraphSettings:
    import dataclasses

    return dataclasses.replace(gs, address=address)


def _jsonable(value: Any) -> dict[str, Any]:
    import dataclasses

    return dataclasses.asdict(value)

