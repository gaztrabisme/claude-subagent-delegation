"""Console entry point: `subagent init`, `subagent doctor`, and the delegate loop.

The delegate-loop subcommands (`detect run wait test review undo checkpoints
watch`) each print one JSON object on stdout and exit with the code the loop
reports (`done` -> 0, `running` -> 3, anything else -> 2), except `watch`, which
streams the live log. `init` writes a starter config from the examples in
`examples/`; `doctor` checks the configured providers against this machine.

The delegating loop commands (`run`, `wait`, `review`) stand up an in-process
server (Settings -> Registry -> Supervisor) so their workers run through the
provider Registry, not a hand-built child process. The read-only ones (`detect`,
`test`, `undo`, `checkpoints`, `watch`) run without one: starting the server
binds the approval socket, which they never use and which some homes forbid
(a sandboxed orchestrator cell cannot bind Unix sockets at all).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from . import config
from .commands import init as init_command
from .commands import install as install_command
from .commands import providers as providers_command
from .config import ConfigError, Settings
from .goal import Goal, GoalError, parse_goal
from .health import check as health_check
from .loop import loop
from .providers.base import ProviderConfig

# The examples `init` offers, one per file.
EXAMPLES = Path(__file__).resolve().parents[2] / "examples"

# The binary each driver runs when a provider names none. The claude and codex
# modules carry their own defaults; the unported drivers use their own name.
DRIVER_BINARY = {
    "claude": "claude",
    "codex": "codex",
    "copilot": "copilot",
    "gemini": "gemini",
    "grok": "grok",
    "antigravity": "agy",
}


def _binary(cfg: ProviderConfig) -> str:
    return cfg.binary or DRIVER_BINARY.get(cfg.driver, cfg.driver)


# --- init -----------------------------------------------------------------------


def _example_path(name: str) -> Path:
    return EXAMPLES / f"config.{name}.toml"


def _example_names() -> list[str]:
    if not EXAMPLES.is_dir():
        return []
    return sorted(p.stem.removeprefix("config.") for p in EXAMPLES.glob("config.*.toml"))


def _dest_path(path_arg: str | None) -> Path:
    if path_arg:
        return Path(path_arg).expanduser()
    home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".config"
    return base / "subagent" / config.CONFIG_NAME


def _load_example(name: str) -> dict[str, Any]:
    path = _example_path(name)
    if not path.is_file():
        raise ConfigError(
            f"unknown example {name!r}; examples: {', '.join(_example_names()) or '(none)'}"
        )
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _merge_dict(base: dict[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in over.items():
        current = out.get(key)
        if isinstance(value, Mapping) and isinstance(current, Mapping):
            out[key] = _merge_dict(dict(current), value)
        else:
            out[key] = value
    return out


def _toml_key(key: str) -> str:
    if key and all(ch.isalnum() or ch in "_-" for ch in key):
        return key
    return json.dumps(key)


def _toml_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_literal(v) for v in value) + "]"
    raise ConfigError(f"cannot write {value!r} as TOML")


def _emit_table(lines: list[str], prefix: str, table: Mapping[str, Any]) -> None:
    scalars = {k: v for k, v in table.items() if not isinstance(v, Mapping)}
    if prefix:
        lines.append(f"[{prefix}]")
    for key, value in scalars.items():
        lines.append(f"{_toml_key(key)} = {_toml_literal(value)}")
    for key, value in table.items():
        if isinstance(value, Mapping):
            child = f"{prefix}.{_toml_key(key)}" if prefix else _toml_key(key)
            _emit_table(lines, child, value)


def _render_toml(data: Mapping[str, Any]) -> str:
    lines: list[str] = []
    _emit_table(lines, "", data)
    return "\n".join(lines) + "\n"


def _needed_exports(settings: Settings) -> list[str]:
    """`export NAME=...` lines for providers whose key is not yet in the env."""
    lines: list[str] = []
    for cfg in settings.providers.values():
        if cfg.api_key() is not None:
            continue
        for name in cfg.api_key_envs:
            lines.append(f"export {name}=...")
    return lines


# --- doctor ---------------------------------------------------------------------


def _key_envs(cfg: ProviderConfig) -> list[dict[str, Any]]:
    return [
        {"name": name, "set": bool((os.environ.get(name) or "").strip())}
        for name in cfg.api_key_envs
    ]


def _prompt_turn(settings: Settings, cfg: ProviderConfig) -> dict[str, Any]:
    """One cheap turn on `cfg` through an in-process server, for `--prompt`."""
    import tempfile

    from .core import InProcessServer

    # A short prefix leaves room for the per-probe Unix socket on macOS, even
    # when TMPDIR points inside a deeply nested worktree.
    tmp = Path(tempfile.mkdtemp(prefix="d-"))
    workspace = tmp / "workspace"
    workspace.mkdir()
    server = InProcessServer(tmp / "sessions", settings)
    try:
        server.start()
        run = server.delegate(
            provider=cfg.name,
            task="Reply with the single word ok",
            verification="true",
            workspace=workspace,
            fallback="none",
        )
        run.done.wait(timeout=cfg.run_timeout + 60)
        ok = run.state == "completed"
        return {
            "ok": ok,
            "state": run.state,
            "error": run.error,
            "output_tokens": run.usage.output,
            "ttft_seconds": round(run.ttft_seconds, 3) if run.ttft_seconds is not None else None,
            "wall_seconds": round((run.finished_at or 0) - (run.started_at or 0), 3),
        }
    except Exception as exc:  # noqa: BLE001 - a failed turn is reported, not fatal
        return {
            "ok": False,
            "state": "error",
            "error": str(exc),
            "output_tokens": 0,
            "ttft_seconds": None,
            "wall_seconds": 0.0,
        }
    finally:
        server.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def _doctor_row(settings: Settings, cfg: ProviderConfig, args: argparse.Namespace
                ) -> tuple[dict[str, Any], bool]:
    hard = False
    binary = _binary(cfg)
    path = shutil.which(binary)
    row: dict[str, Any] = {
        "name": cfg.name,
        "driver": cfg.driver,
        "vendor": cfg.vendor,
        "binary": binary,
        "binary_path": path,
        "binary_ok": path is not None,
        "api_key_envs": _key_envs(cfg),
    }
    warning = _thinking_warning(cfg)
    if cfg.extra.get("auth") == "login":
        login_warning = (
            f"warning: provider {cfg.name!r} uses the owner's Claude login and plan quota; "
            "worker sessions are written into the owner's Claude history, and the owner's "
            "global instructions, commands and skills may load"
        )
        warning = f"{warning}\n{login_warning}" if warning else login_warning
    if warning:
        row["warning"] = warning
    if not args.no_probe:
        if path is None:
            hard = True
        gate = health_check(cfg)
        row["health_ok"] = gate.ok
        row["health"] = gate.message or ("ok" if gate.ok else "failed")
        if not gate.ok:
            hard = True
        row["adapter"] = None
        if cfg.adapter and cfg.base_url:
            try:
                from . import adapter

                row["adapter"] = adapter.child_base_url(cfg)
            except Exception as exc:  # noqa: BLE001
                row["adapter"] = f"error: {exc}"
        row["prompt"] = None
        if args.prompt:
            result = _prompt_turn(settings, cfg)
            row["prompt"] = result
            if not result.get("ok"):
                hard = True
    return row, hard


def _thinking_warning(cfg: ProviderConfig) -> str | None:
    thinking = cfg.thinking
    if thinking is None:
        return None
    supported = (
        (cfg.driver == "omp" and thinking in ("off", "low"))
        or (cfg.driver in ("codex", "antigravity") and thinking == "low")
    )
    if supported:
        return None
    return (
        f"warning: provider {cfg.name!r} driver {cfg.driver!r} does not support thinking = "
        f"'{thinking}'; provider default used."
    )


def _goal_cli_errors(exc: GoalError) -> list[str]:
    if exc.block:
        return [f"error: goal block is invalid: {problem}" for problem in exc.problems]
    return [f"error: {problem}" for problem in exc.problems]


def _report_goal_error(exc: GoalError, *, json_mode: bool) -> int:
    messages = _goal_cli_errors(exc)
    if json_mode:
        print(json.dumps({"errors": messages}))
    else:
        for message in messages:
            print(message, file=sys.stderr)
    return 1


def _print_table(rows: list[dict[str, Any]], args: argparse.Namespace) -> None:
    headers = ["name", "driver", "binary", "key", "health", "adapter", "prompt"]
    widths = {h: len(h) for h in headers}
    rendered: list[dict[str, str]] = []
    for row in rows:
        key_text = ", ".join(
            f"{e['name']}={'set' if e['set'] else 'missing'}" for e in row["api_key_envs"]
        ) or "none"
        binary_text = row["binary_path"] or "MISSING"
        health_text = "-"
        adapter_text = "-"
        prompt_text = "-"
        if not args.no_probe:
            health_text = "ok" if row["health_ok"] else row["health"]
            adapter_text = row.get("adapter") or "-"
            prompt = row.get("prompt")
            if prompt:
                if prompt.get("ok"):
                    prompt_text = (
                        f"{prompt['output_tokens']} tok, ttft {prompt['ttft_seconds']}s, "
                        f"{prompt['wall_seconds']}s"
                    )
                else:
                    prompt_text = f"failed: {prompt.get('error') or prompt.get('state')}"
        cells = {
            "name": row["name"],
            "driver": row["driver"],
            "binary": binary_text,
            "key": key_text,
            "health": health_text,
            "adapter": adapter_text,
            "prompt": prompt_text,
        }
        rendered.append(cells)
        for header in headers:
            widths[header] = max(widths[header], len(cells[header]))

    print("  ".join(header.ljust(widths[header]) for header in headers))
    for cells in rendered:
        print("  ".join(cells[header].ljust(widths[header]) for header in headers))


def cmd_doctor(args: argparse.Namespace) -> int:
    goal: Goal | None = None
    if args.goal is not None:
        workspace = Path(args.root or ".").expanduser().resolve()
        try:
            goal = parse_goal(args.goal, workspace)
        except GoalError as exc:
            return _report_goal_error(exc, json_mode=args.json)

    try:
        settings = config.load()
        settings.require_providers()
    except ConfigError as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.provider and args.provider not in settings.providers:
        print(
            f"error: unknown provider {args.provider!r}; configured: "
            f"{', '.join(settings.providers) or '(none)'}",
            file=sys.stderr,
        )
        return 2

    names = [args.provider] if args.provider else list(settings.providers)
    rows: list[dict[str, Any]] = []
    hard = False
    for name in names:
        row, failed = _doctor_row(settings, settings.providers[name], args)
        rows.append(row)
        hard = hard or failed

    goal_warning = (
        "warning: goal has no command rows; completion has no server-side goal check "
        "when it contains only prose"
        if goal is not None and not any(row.kind == "command" for row in goal.rows)
        else None
    )
    if args.json:
        response: dict[str, Any] = {
            "ok": not hard,
            "providers": rows,
            "loop_auto": settings.loop.auto,
        }
        if goal is not None:
            response["goal"] = {
                "rows": len(goal.rows),
                "command_rows": sum(row.kind == "command" for row in goal.rows),
            }
        if goal_warning:
            response["warnings"] = [goal_warning]
        print(json.dumps(response, indent=2))
    else:
        _print_table(rows, args)
        for row in rows:
            if row.get("warning"):
                print(row["warning"], file=sys.stderr)
        if goal_warning:
            print(goal_warning, file=sys.stderr)
        elif goal is not None:
            print(f"goal block valid: {len(goal.rows)} rows")

    return 1 if hard else 0


# --- the delegate loop ----------------------------------------------------------


def _loop_parsers(subparsers: argparse._SubParsersAction) -> None:
    run = subparsers.add_parser("run", help="one worker round (or autopilot) on a plan")
    run.add_argument("--plan", metavar="FILE", help="the plan file (single-round mode)")
    run.add_argument("--goal", metavar="VALUE", help="goal-block text or workspace GOAL.md")
    run.add_argument(
        "--parallel", metavar="MANIFEST", help="a manifest of parts, one worktree each"
    )
    run.add_argument("--auto", action="store_true", help="retry/repair until done or stuck")
    run.add_argument("--continue", dest="continue_session", action="store_true",
                     help="continue the previous worker session")
    run.add_argument("--feedback", metavar="TEXT", help="reviewer feedback for this round")
    run.add_argument("--feedback-file", metavar="FILE", help="feedback from a file ('-' = stdin)")
    run.add_argument("--tier", choices=("normal", "hard"), default="normal")
    run.add_argument("--model", metavar="MODEL", help="override the tier's model")
    run.add_argument("--provider", metavar="NAME", help="override the tier's provider")
    run.add_argument(
        "--test-outline", metavar="FILE", help="write tests from this outline (--auto)"
    )
    run.add_argument(
        "--background", action="store_true", help="start and return; collect with `wait`"
    )
    run.add_argument("--wait", metavar="SECONDS", type=float, help="background, then wait up to S")
    run.add_argument(
        "--run-id", metavar="ID", help="the run id (a background child reuses its parent's)"
    )

    wait = subparsers.add_parser("wait", help="wait for the current run")
    wait.add_argument("--timeout", metavar="SECONDS", type=float, default=540.0)

    subparsers.add_parser("test", help="run the test suite")
    subparsers.add_parser("detect", help="detected test command and protected files")

    review = subparsers.add_parser("review", help="review the work (or the tests) so far")
    review.add_argument("--tier", choices=("normal", "hard"), default="normal")
    review.add_argument(
        "--base", metavar="ID", help="checkpoint to diff against (default: task start)"
    )
    review.add_argument(
        "--tests", action="store_true", help="review the tests against the plan/spec"
    )
    review.add_argument("--plan", metavar="FILE", help="the plan file (for --tests)")
    review.add_argument("--model", metavar="MODEL", help="override the reviewer's model")

    undo = subparsers.add_parser("undo", help="restore the working tree to a checkpoint")
    undo.add_argument(
        "--to", metavar="ID", help="checkpoint to restore (default: before last round)"
    )

    subparsers.add_parser("checkpoints", help="list checkpoints")

    watch = subparsers.add_parser("watch", help="follow the live log (run it in another terminal)")
    watch.add_argument("--log", metavar="FILE", help="follow this log file")
    watch.add_argument("--run", metavar="ID", help="follow one run's logs in order")
    watch.add_argument("--since", metavar="TS", type=float, help="only logs modified after TS")
    watch.add_argument("--hold", action="store_true", help="wait for Enter before closing")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="subagent",
        description="Delegate coding work to a subagent, and inspect its configuration.",
    )
    parser.add_argument(
        "--root", metavar="PATH", help="the project root (default: current directory)"
    )
    sub = parser.add_subparsers(dest="command")

    init_command.register(sub)

    doctor = sub.add_parser("doctor", help="check the configured providers")
    doctor.add_argument("--provider", metavar="NAME", help="check only this provider")
    doctor.add_argument("--prompt", action="store_true",
                        help="also run one cheap turn on each provider")
    doctor.add_argument("--no-probe", action="store_true",
                        help="only validate the file and check binaries")
    doctor.add_argument("--json", action="store_true", help="print JSON instead of a table")
    doctor.add_argument("--goal", metavar="VALUE", help="validate goal-block text or GOAL.md")

    sub.add_parser("report", help="summarize a trace into tables and an HTML dashboard",
                   add_help=False)

    providers_command.register(sub)
    install_command.register(sub)

    _loop_parsers(sub)
    return parser


# The loop commands whose workers need the in-process server, and so the
# approval socket; the rest run against a bare settings stand-in.
SERVER_COMMANDS = frozenset({"run", "wait", "review"})


def _run_loop_command(command: str, args: argparse.Namespace, raw: list[str],
                      root: Path, settings: Settings) -> int:
    """Run one loop command, printing its JSON.

    The read-only commands never see a server: they only read `settings` off
    it, so a stand-in keeps them working where binding a socket is impossible.
    """
    handler = loop.LOOP_COMMANDS[command]
    if command not in SERVER_COMMANDS:
        result, code = handler(root, SimpleNamespace(settings=settings), args)
        if result is not None:
            print(json.dumps(result))
        return code

    from .core import InProcessServer

    # A launcher only spawns the background child (and may wait on its state);
    # it must not own a listener that its cleanup could unlink from the child.
    if command == "run" and (
        getattr(args, "background", False) or getattr(args, "wait", None) is not None
    ):
        result, code = loop.LOOP_COMMANDS[command](root, None, args, raw)
        if result is not None:
            print(json.dumps(result))
        return code

    # Every serving process owns a private socket under the session root. A
    # configured shared pathname can be rebound by a respawned child and then
    # removed when the parent exits.
    if command == "run" and getattr(args, "run_id", None):
        approval_socket = settings.session_root / f"a{os.getpid():x}"
    else:
        approval_socket = Path(settings.approval_socket)
    server = InProcessServer(settings.session_root, settings,
                             approval_socket=str(approval_socket))
    server.start()
    try:
        if command == "run":
            result, code = handler(root, server, args, raw)
        else:
            result, code = handler(root, server, args)
        if result is not None:
            print(json.dumps(result))
        return code
    finally:
        server.stop()


def _project_config_root(root: Path) -> Path:
    """Use the nearest ancestor that owns a project config, if there is one."""
    for candidate in (root, *root.parents):
        if (candidate / config.PROJECT_DIR / config.CONFIG_NAME).is_file():
            return candidate
    return root


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "report":
        try:
            settings = config.load(project_root=_project_config_root(Path.cwd().resolve()))
            settings.require_providers()
        except ConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        from .report import main as report_main

        return report_main(raw[1:])
    args = _parser().parse_args(raw)
    if args.command == "doctor":
        return cmd_doctor(args)
    handler = getattr(args, "command_handler", None)
    if handler is not None:
        return handler(args)
    if args.command not in loop.LOOP_COMMANDS:
        _parser().print_help()
        return 2

    root = Path(args.root or ".").expanduser().resolve()
    if args.command == "run" and args.goal is not None:
        try:
            parse_goal(args.goal, root)
        except GoalError as exc:
            return _report_goal_error(exc, json_mode=False)
    try:
        project_root = root if args.root else _project_config_root(root)
        settings = config.load(project_root=project_root)
        settings.require_providers()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        return _run_loop_command(args.command, args, raw, root, settings)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
