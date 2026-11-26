"""`subagent graph ...` (spec 9): argparse wiring and handlers over GraphService.

Lazy: this module never imports temporalio, the workflow or the worker at the top level.
`add_graph_parser(sub)` registers the verbs and sets `command_handler` so the main parser
needs one call and no dispatch edit.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

from .. import config
from . import GraphUnavailable

_EXIT = {"invalid_graph": 2, "invalid_argument": 2, "invalid_answer": 2, "unavailable": 3,
         "not_found": 4, "conflict": 4, "already_answered": 4, "closed": 4}


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="print the result as JSON")


def add_graph_parser(sub: argparse._SubParsersAction) -> None:
    graph = sub.add_parser("graph", help="durable workflow graphs")
    graph.set_defaults(command_handler=cmd_graph)
    verbs = graph.add_subparsers(dest="graph_command", required=True)

    validate = verbs.add_parser("validate", help="validate a graph file")
    validate.add_argument("file")
    _common(validate)

    start = verbs.add_parser("start", help="start (or attach to) a run")
    start.add_argument("file")
    start.add_argument("--input", action="append", default=[], metavar="K=V")
    start.add_argument("--pool", default="default")
    start.add_argument("--budget", type=float, default=None)
    start.add_argument("--priority", type=int, default=3)
    start.add_argument("--request-id", dest="request_id", default=None)
    start.add_argument("--workspace", default=None)
    _common(start)

    status = verbs.add_parser("status", help="state of one run")
    status.add_argument("root")
    status.add_argument("--detail", action="store_true")
    _common(status)

    pause = verbs.add_parser("pause", help="stop starting new tasks")
    pause.add_argument("root")
    pause.add_argument("--reason", default=None)
    _common(pause)

    resume = verbs.add_parser("resume", help="continue a paused run")
    resume.add_argument("root")
    _common(resume)

    cancel = verbs.add_parser("cancel", help="cancel a run")
    cancel.add_argument("root")
    cancel.add_argument("--reason", required=True)
    _common(cancel)

    listing = verbs.add_parser("list", help="recent runs")
    listing.add_argument("--state", default=None)
    listing.add_argument("--limit", type=int, default=50)
    _common(listing)

    exceptions = verbs.add_parser("exceptions", help="things waiting for the orchestrator")
    exceptions.add_argument("--root", default=None)
    exceptions.add_argument("--state", default="open")
    exceptions.add_argument("--kind", default=None)
    exceptions.add_argument("--limit", type=int, default=100)
    _common(exceptions)

    answer = verbs.add_parser("answer", help="answer an exception")
    answer.add_argument("exception_id")
    answer.add_argument("answer")
    answer.add_argument("--note", default=None)
    answer.add_argument("--by", default="orchestrator")
    _common(answer)

    server = verbs.add_parser("server", help="the local Temporal dev server")
    server.add_argument("action", choices=("start", "stop", "status"))
    _common(server)

    worker = verbs.add_parser("worker", help="run a graph worker process")
    worker.add_argument("--dispatcher", choices=("registry", "fake"), default="registry")
    worker.add_argument("--fake-script", dest="fake_script", default=None)
    worker.add_argument("--max-tasks", dest="max_tasks", type=int, default=None)
    worker.add_argument("--address", default=None, help="Temporal address (autostart passes it)")
    worker.add_argument("--state-dir", dest="state_dir", default=None)
    worker.add_argument("--task-queue-prefix", dest="task_queue_prefix", default=None)
    _common(worker)


def _parse_inputs(items: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise ValueError(f"--input expects K=V, got {item!r}")
        try:
            out[key] = json.loads(value)
        except ValueError:
            out[key] = value
    return out


def _emit(result: dict[str, Any], as_json: bool) -> int:
    if as_json:
        print(json.dumps(result))
    elif result.get("ok"):
        for key, value in result.items():
            if key != "ok":
                text = json.dumps(value) if isinstance(value, (dict, list)) else value
                print(f"{key}: {text}")
    else:
        error = result.get("error", {})
        print(f"error [{error.get('code')}]: {error.get('message')}", file=sys.stderr)
        for problem in error.get("problems") or ():
            print(f"  {problem['path']}: {problem['message']}", file=sys.stderr)
    if result.get("ok"):
        return 0
    return _EXIT.get(result.get("error", {}).get("code", ""), 1)


def _service(settings: Any) -> Any:
    from .service import GraphService

    return GraphService(settings)


def cmd_graph(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    try:
        settings = config.load(Path.cwd())
        return _dispatch(args, settings, as_json)
    except GraphUnavailable as exc:
        return _emit({"ok": False, "error": {"code": "unavailable", "message": str(exc)}},
                     as_json)
    except (ValueError, config.ConfigError) as exc:
        return _emit({"ok": False, "error": {"code": "invalid_argument", "message": str(exc)}},
                     as_json)


def _dispatch(args: argparse.Namespace, settings: Any, as_json: bool) -> int:
    verb = args.graph_command
    if verb == "worker":
        return _worker(args, settings)
    if verb == "server":
        return _server(args, settings, as_json)
    service = _service(settings)
    if verb == "validate":
        result = asyncio.run(service.validate(str(Path(args.file).expanduser().resolve())))
    elif verb == "start":
        result = asyncio.run(service.start(
            str(Path(args.file).expanduser().resolve()), inputs=_parse_inputs(args.input),
            pool=args.pool, budget_usd=args.budget, priority=args.priority,
            request_id=args.request_id, workspace=args.workspace))
    elif verb == "status":
        result = asyncio.run(service.status(args.root, detail=args.detail))
    elif verb == "pause":
        result = asyncio.run(service.pause(args.root, args.reason))
    elif verb == "resume":
        result = asyncio.run(service.resume(args.root))
    elif verb == "cancel":
        result = asyncio.run(service.cancel(args.root, args.reason))
    elif verb == "list":
        result = asyncio.run(service.list(state=args.state, limit=args.limit))
    elif verb == "exceptions":
        result = asyncio.run(service.exceptions(
            root_run_id=args.root, state=args.state, kind=args.kind, limit=args.limit))
    elif verb == "answer":
        result = asyncio.run(service.answer(args.exception_id, args.answer, note=args.note,
                                            answered_by=args.by))
    else:
        raise ValueError(f"unknown graph verb {verb!r}")
    return _emit(result, as_json)


def _server(args: argparse.Namespace, settings: Any, as_json: bool) -> int:
    from .server import DevServer, settings_from

    gs = settings_from(settings)
    server = DevServer(gs.state_dir, address=gs.address, startup_timeout_s=30.0)
    if args.action == "start":
        info = server.start()
        result: dict[str, Any] = {"ok": True, **dataclasses.asdict(info)}
    elif args.action == "stop":
        result = {"ok": True, "stopped": server.stop()}
    else:
        result = {"ok": True, **server.health()}
    return _emit(result, as_json)


def _worker(args: argparse.Namespace, settings: Any) -> int:
    from . import temporal_available
    from .server import settings_from

    if args.dispatcher == "fake" and not args.fake_script:
        raise ValueError("--dispatcher fake requires --fake-script")
    if not temporal_available():
        raise GraphUnavailable("install the graph extra: uv sync --extra graph")
    gs = settings_from(settings)
    if args.max_tasks is not None:
        if args.max_tasks < 1:
            raise ValueError("--max-tasks must be at least 1")
        gs = dataclasses.replace(gs, worker_max_tasks=args.max_tasks)
    overrides = {k: v for k, v in (
        ("address", args.address), ("state_dir", Path(args.state_dir) if args.state_dir else None),
        ("task_queue_prefix", args.task_queue_prefix)) if v is not None}
    if overrides:  # the parent's resolved values win over this process's re-read config
        gs = dataclasses.replace(gs, **overrides)
    from .worker import run_registry_worker, run_worker

    if args.dispatcher == "fake":
        if not args.fake_script:
            raise ValueError("--dispatcher fake requires --fake-script")
        from .dispatch import FakeDispatcher

        dispatcher = FakeDispatcher.from_file(Path(args.fake_script))
        asyncio.run(run_worker(settings, gs, dispatcher=dispatcher))
    else:
        asyncio.run(run_registry_worker(settings, gs))
    return 0
