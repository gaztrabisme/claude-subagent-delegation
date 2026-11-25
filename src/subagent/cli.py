"""Console entry point: `subagent init`, `subagent doctor`, and the delegate loop.

The delegate-loop subcommands (`detect run wait test review undo checkpoints
watch`) each print one JSON object on stdout and exit with the code the loop
reports (`done` -> 0, `running` -> 3, anything else -> 2), except `watch`, which
streams the live log. `init` writes a starter config from the examples in
`examples/`; `doctor` checks the configured providers against this machine.

`doctor` reads keys from the same provider env file the MCP wrapper sources, so
a key that lives only there (see `load_provider_env`) still answers 200.

The delegating loop commands (`run`, `wait`, `review`) stand up an in-process
server (Settings -> Registry -> Supervisor) so their workers run through the
provider Registry, not a hand-built child process. The read-only ones (`detect`,
`test`, `undo`, `checkpoints`, `watch`) run without one: starting the server
binds the approval socket, which they never use and which some homes forbid
(a sandboxed orchestrator cell cannot bind Unix sockets at all).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
import uuid
from collections.abc import Mapping, MutableMapping
from datetime import UTC, datetime
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
from .lane_status import LaneStatusStore, probe_stale_lanes
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


# --- the provider env file ------------------------------------------------------

# The MCP wrapper starts the server after `set -a; . <file>`, the file being
# $SUBAGENT_ENV_FILE or ~/.config/agent-capabilities/subagent.env. On a machine
# where the keys live only in that file, a `subagent doctor` typed in a plain
# shell has none of them and a keyed health check answers HTTP 401. The name and
# the default path live here, not in config.py: this is a convenience of the
# command line, not something the server reads.
PROVIDER_ENV_FILE = "SUBAGENT_ENV_FILE"
PROVIDER_ENV_DEFAULT = (".config", "agent-capabilities", "subagent.env")

# `FOO=bar` and `export FOO=bar` both match; `set -a`, a bare word and a name
# that is not a valid variable name do not.
_ASSIGNMENT = re.compile(r"\A([A-Za-z_][A-Za-z0-9_]*)[ \t]*=(.*)\Z")
_EXPORT = re.compile(r"\Aexport[ \t]+")
_TRAILING_COMMENT = re.compile(r"[ \t]+#")


def _provider_env_path(env: Mapping[str, str]) -> Path:
    """The env file to read: `$SUBAGENT_ENV_FILE`, else the wrapper's default."""
    named = (env.get(PROVIDER_ENV_FILE) or "").strip()
    if named:
        return Path(named).expanduser()
    home = (env.get("HOME") or "").strip()
    base = Path(home).expanduser() if home else Path.home()
    return base.joinpath(*PROVIDER_ENV_DEFAULT)


def _shell_value(raw: str) -> str:
    """A shell assignment value with its outer quoting removed. No expansion."""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        inner = value[1:-1]
        if value[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\`", "`").replace("\\\\", "\\")
        return inner
    # Unquoted, a `#` that follows whitespace starts a comment, as in the shell.
    return _TRAILING_COMMENT.split(value, maxsplit=1)[0].strip()


def _env_assignments(text: str) -> list[tuple[str, str]]:
    """The assignments in an env file.

    Comments, blank lines and anything that is not a `KEY=VALUE` line (a `set -a`
    line, a bare word, a name that is not a valid variable) are ignored, and an
    `export ` prefix is dropped. Nothing in the file is executed.
    """
    found: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = _EXPORT.sub("", line, count=1)
        match = _ASSIGNMENT.match(line)
        if match is None:
            continue
        found.append((match.group(1), _shell_value(match.group(2))))
    return found


def load_provider_env(env: MutableMapping[str, str] | None = None) -> Path | None:
    """Import the wrapper's provider env file into `env` (this process by default).

    The same file, and the same net effect, as the wrapper's `set -a; . <file>`,
    with two deliberate differences: the file is parsed for `KEY=VALUE` lines
    instead of run as shell, so nothing in it executes, and a variable that
    already has a value in the environment keeps it, so an explicit `export` in
    the calling shell wins over the file (a variable set to an empty string
    holds no value and is filled). Only names are ever seen here, never values:
    nothing is printed, and a missing or unreadable file is skipped silently.

    Returns the path read, or None when there was no file to read.
    """
    target: MutableMapping[str, str] = os.environ if env is None else env
    path = _provider_env_path(target)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    for name, value in _env_assignments(text):
        current = target.get(name)
        if current is not None and current.strip():
            continue
        target[name] = value
    return path


# --- doctor ---------------------------------------------------------------------


def _key_envs(cfg: ProviderConfig) -> list[dict[str, Any]]:
    return [
        {"name": name, "set": bool((os.environ.get(name) or "").strip())}
        for name in cfg.api_key_envs
    ]


def _prompt_turn(
    settings: Settings,
    cfg: ProviderConfig,
    *,
    stop: Any = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """One cheap turn on `cfg` through an in-process server, for `--prompt`."""
    import tempfile

    from .core import InProcessServer

    epi_workspace = None
    if cfg.driver == "epi":
        from .providers.epi import EPI_PROVIDER, _project

        probe = EPI_PROVIDER.doctor(settings, cfg)
        if not probe["health_ok"]:
            return {"ok": False, "state": "refused", "error": probe["health"]}
        epi_workspace = _project(cfg)

    # A short prefix leaves room for the per-probe Unix socket on macOS, even
    # when TMPDIR points inside a deeply nested worktree.
    tmp = Path(tempfile.mkdtemp(prefix="d-"))
    workspace = epi_workspace or tmp / "workspace"
    if epi_workspace is None:
        workspace.mkdir()
    probe_settings = dataclasses.replace(settings, trace="off")
    server = InProcessServer(tmp / "sessions", probe_settings)
    stopped = False

    def stop_server() -> None:
        nonlocal stopped
        if not stopped:
            stopped = True
            server.stop()

    try:
        server.start()
        run = server.delegate(
            provider=cfg.name,
            task="Reply with the single word ok",
            verification="true",
            workspace=workspace,
            fallback="none",
        )
        deadline = time.monotonic() + min(max(timeout, 0.1), max(cfg.run_timeout, 0.1))
        while not run.done.is_set():
            if stop is not None and stop.is_set():
                stop_server()
                return {"ok": False, "state": "cancelled", "error": "probe stopped"}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stop_server()
                return {"ok": False, "state": "timeout", "error": "probe timed out"}
            run.done.wait(timeout=min(0.1, remaining))
        ok = run.state == "completed"
        usage = None
        if cfg.driver == "codex":
            from .lane_status import codex_usage

            if run.agent_id:
                usage = codex_usage(
                    tmp / "sessions" / "agents" / run.agent_id / "codex-home"
                )
        return {
            "ok": ok,
            "state": run.state,
            "error": run.error,
            "output_tokens": run.usage.output,
            "ttft_seconds": round(run.ttft_seconds, 3) if run.ttft_seconds is not None else None,
            "wall_seconds": round((run.finished_at or 0) - (run.started_at or 0), 3),
            "usage": usage,
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
        stop_server()
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
        if cfg.driver == "dsh":
            login_warning = (
                f"warning: provider {cfg.name!r} uses the owner's DSH login; "
                "the worker's DSH_HOME is isolated while the owner credential store is used"
            )
            if cfg.max_agents > 1:
                login_warning += (
                    f"; max_agents = {cfg.max_agents} shares one owner login and credential store"
                )
        else:
            login_warning = (
                f"warning: provider {cfg.name!r} uses the owner's Claude login and plan quota; "
                "worker sessions are written into the owner's Claude history, and the owner's "
                "global instructions, commands and skills may load"
            )
        warning = f"{warning}\n{login_warning}" if warning else login_warning
        if cfg.max_agents > 1:
            concurrency_warning = (
                f"warning: provider {cfg.name!r} max_agents = {cfg.max_agents}; concurrent "
                "login children share ~/.claude.json and one OAuth refresh token"
            )
            warning = f"{warning}\n{concurrency_warning}"
    if warning:
        row["warning"] = warning
    if cfg.driver == "epi" and not args.no_probe:
        from .providers.epi import EPI_PROVIDER

        row.update(EPI_PROVIDER.doctor(settings, cfg))
        row["adapter"] = None
        row["prompt"] = None
        if args.prompt:
            row["prompt"] = (_prompt_turn(settings, cfg) if row["health_ok"] else
                             {"ok": False, "state": "refused", "error": row["health"]})
        return row, not row["health_ok"] or bool(row["prompt"] and not row["prompt"]["ok"])
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
    # Before the config is loaded: `load` reports a provider with no key, and a
    # health probe that sends one, from this process's environment.
    load_provider_env()

    workspace = Path(args.root or ".").expanduser().resolve()

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
    refusal = settings.workspace_refusal(workspace)
    workspace_row: dict[str, Any] = {
        "path": str(workspace),
        "accepted": refusal is None,
        "reason": refusal,
    }
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
            "workspace": workspace_row,
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
        if refusal is None:
            print("workspace: accepted")
        else:
            print(f"workspace: refused: {refusal}")
        for row in rows:
            if row.get("warning"):
                print(row["warning"], file=sys.stderr)
        if goal_warning:
            print(goal_warning, file=sys.stderr)
        elif goal is not None:
            print(f"goal block valid: {len(goal.rows)} rows")

    return 1 if hard else 0


def cmd_lanes(args: argparse.Namespace) -> int:
    """Print persisted lane state, optionally refreshing stale idle lanes."""
    if args.probe:
        load_provider_env()
    root = Path(getattr(args, "root", None) or ".").expanduser().resolve()
    project_root = root if getattr(args, "root", None) else _project_config_root(root)
    try:
        settings = config.load(project_root=project_root)
        settings.require_providers()
    except ConfigError as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
    store = LaneStatusStore(settings.session_root, redact_keys=settings.leaked_keys)
    if args.probe:
        snapshot = probe_stale_lanes(settings, store, _prompt_turn)
    else:
        snapshot = store.snapshot(settings)
    if args.json:
        print(json.dumps(snapshot, indent=2))
    else:
        print("lane  state    source  stale  live/limit  spend USD")
        for name, view in snapshot.items():
            spend = view["spend"]["month_usd"]
            spend_text = "unknown" if spend is None else f"{spend:.4f}"
            slots = view["slots"]
            print(
                f"{name}  {view['state']:<8} {view['source'] or '-':<6} "
                f"{str(view['stale']).lower():<5}  {slots['live']}/{slots['limit']}"
                f"        {spend_text}"
            )
    return 0


def cmd_harness(args: argparse.Namespace) -> int:
    """Inspect or explicitly maintain configured provider CLIs."""
    from . import harness

    root = Path(getattr(args, "root", None) or ".").expanduser().resolve()
    project_root = root if getattr(args, "root", None) else _project_config_root(root)
    try:
        settings = config.load(project_root=project_root)
        settings.require_providers()
    except ConfigError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1

    if args.harness_command == "status":
        rows = harness.status(settings)
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            print("lane  harness version  state     verified version")
            for row in rows:
                print(
                    f"{row['lane']}  {row['harness_version'] or '-'}  "
                    f"{row['harness_state']}  {row['verified_version'] or '-'}"
                )
        return 0
    if args.harness_command == "check":
        results = harness.check_updates(settings, now=datetime.now(UTC))
        print(json.dumps(results, indent=2))
        return 1 if any(
            row.get("current") is None or row.get("available") is None
            for row in results.values()
        ) else 0
    if args.harness_command == "verify":
        result = harness.verify(args.lane, settings)
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1
    return 2


def _run_argv(argv: list[str], root: Path) -> int:
    """Run one command in `root`; a missing binary is the shell's 127.

    A child killed by a signal reports a negative status from subprocess; the
    shell's convention (and `exec`'s exit code) is 128 + the signal number.
    """
    try:
        code = subprocess.run(argv, cwd=root).returncode
    except FileNotFoundError:
        return 127
    except OSError as exc:
        print(f"error: cannot run {argv[0]!r}: {exc}", file=sys.stderr)
        return 126
    return 128 - code if code < 0 else code


class _LockListingError(RuntimeError):
    """The locked commit's tree could not be listed, so it cannot be verified."""


def _resolve_rev(root: Path, rev: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "--quiet",
         "--end-of-options", f"{rev}^{{commit}}"],
        capture_output=True,
        text=True,
    )
    sha = result.stdout.strip() if result.returncode == 0 else ""
    return sha or None


def _symlink_paths(root: Path, globs: list[str]) -> set[str]:
    """Symlinks matching `globs`: walk_files skips them, but a link is a change."""
    from .loop.common import SKIP_DIRS, matches

    found: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            if not path.is_symlink():
                continue
            rel = path.relative_to(root).as_posix()
            if matches(rel, globs):
                found.add(rel)
    return found


def _lock_violations(root: Path, rev: str, globs: list[str]) -> list[str]:
    """Files matching `globs` whose working-tree content differs from `rev`."""
    from .loop.common import matches, walk_files

    listed = subprocess.run(
        ["git", "-C", str(root), "ls-tree", "-r", "--name-only", "-z", rev],
        capture_output=True,
    )
    if listed.returncode != 0:
        detail = listed.stderr or b""
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", "replace")
        message = str(detail).strip() or f"git ls-tree exited {listed.returncode}"
        raise _LockListingError(message)
    at_rev = {
        rel for rel in listed.stdout.decode("utf-8", "replace").split("\0")
        if rel and matches(rel, globs)
    }
    now = {rel for rel in walk_files(root) if matches(rel, globs)}
    now |= _symlink_paths(root, globs)
    violations = []
    for rel in sorted(at_rev):
        if (root / rel).is_symlink():
            violations.append(rel)
            continue
        try:
            current = (root / rel).read_bytes()
        except OSError:
            violations.append(rel)
            continue
        original = subprocess.run(
            ["git", "-C", str(root), "show", f"{rev}:{rel}"], capture_output=True
        )
        if original.returncode != 0 or original.stdout != current:
            violations.append(rel)
    violations.extend(sorted(now - at_rev))
    return violations


def _trace_exec(
    settings: Settings,
    cfg: ProviderConfig,
    lane: str,
    root: Path,
    started: float,
    finished: float,
    state: str,
    finish_reason: str,
    exit_code: int,
    lock: str | None,
) -> None:
    from .runs import Run
    from .telemetry.trace import open_trace

    run = Run(
        run_id=f"exec-{uuid.uuid4().hex}",
        agent_id="exec",
        prompt="",
        state=state,
        finish_reason=finish_reason,
    )
    run.created_at = started
    run.started_at = started
    run.finished_at = finished
    run.lane = lane
    run.provider = cfg.vendor
    run.driver = cfg.driver
    run.guard = "none"
    open_trace(settings.trace, settings.session_root).run(
        run,
        str(root),
        cfg.model or "",
        entry="exec-wrapper",
        exit_code=exit_code,
        lock=lock,
    )


def cmd_exec(args: argparse.Namespace) -> int:
    """Run an arbitrary command as one traced, lane-leased exec-wrapper run."""
    root = Path(args.root or ".").expanduser().resolve()
    project_root = root if args.root else _project_config_root(root)
    try:
        settings = config.load(project_root=project_root)
        settings.require_providers()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    cfg = settings.providers.get(args.lane)
    if cfg is None:
        print(
            f"error: unknown lane {args.lane!r}; configured: "
            f"{', '.join(settings.providers) or '(none)'}",
            file=sys.stderr,
        )
        return 2
    argv = [str(part) for part in (args.argv or [])]
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        print("error: exec needs a command after --", file=sys.stderr)
        return 2
    lock_rev = None
    if args.lock_commit:
        lock_rev = _resolve_rev(root, args.lock_commit)
        if lock_rev is None:
            print(f"error: unknown --lock-commit {args.lock_commit!r}", file=sys.stderr)
            return 2
    if args.locked:
        globs = list(args.locked)
    else:
        from .loop.detect import detect

        globs = list(detect(root, settings.loop)["test_globs"])

    store = LaneStatusStore(settings.session_root, redact_keys=settings.leaked_keys)
    started = time.time()
    interrupted = False
    try:
        with store.active_run(args.lane):
            try:
                rc = _run_argv(argv, root)
            except KeyboardInterrupt:
                interrupted = True
                rc = 130
    except TimeoutError as exc:
        # The lane probe lock never cleared: report it as one failed run, with
        # no command started and no traceback.
        print(f"error: {exc}", file=sys.stderr)
        _trace_exec(
            settings, cfg, args.lane, root, started, time.time(),
            "failed", "error", 1, None,
        )
        return 1
    finished = time.time()

    lock = None
    violations: list[str] = []
    listing_failed = False
    if not interrupted and lock_rev is not None:
        try:
            violations = _lock_violations(root, lock_rev, globs)
        except _LockListingError as exc:
            listing_failed = True
            print(
                f"error: cannot verify --lock-commit {lock_rev!r}: {exc}",
                file=sys.stderr,
            )
        lock = "violated" if (listing_failed or violations) else "ok"

    if interrupted:
        state, finish_reason, code = "failed", "error", 130
    elif listing_failed or violations:
        for path in violations:
            print(path, file=sys.stderr)
        state, finish_reason, code = "failed", "violated_tests", 3
    else:
        state = "completed" if rc == 0 else "failed"
        finish_reason = "complete" if rc == 0 else "error"
        code = rc
    store.record_run(args.lane, "completed" if state == "completed" else "error")
    _trace_exec(
        settings, cfg, args.lane, root, started, finished,
        state, finish_reason, rc, lock,
    )
    return code


def cmd_promote(args: argparse.Namespace) -> int:
    """Install one exact commit as the served build."""
    root = Path(args.root or ".").expanduser().resolve()
    project_root = root if args.root else _project_config_root(root)
    try:
        settings = config.load(project_root=project_root)
        settings.require_providers()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    from .promote import PromoteError, promote

    try:
        result = promote(root, args.commit, settings, force=args.force)
    except PromoteError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code
    print(result["commit"][:7])
    return 0


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

    lanes = sub.add_parser("lanes", help="show stored provider lane status")
    lanes.add_argument("--json", action="store_true", help="print JSON instead of a table")
    lanes.add_argument("--probe", action="store_true", help="refresh stale idle lanes first")

    harness_command = sub.add_parser("harness", help="check and verify provider CLI versions")
    harness_sub = harness_command.add_subparsers(dest="harness_command", required=True)
    harness_status = harness_sub.add_parser("status", help="show stored harness versions")
    harness_status.add_argument("--json", action="store_true", help="print JSON")
    harness_sub.add_parser("check", help="check configured update sources")
    harness_verify = harness_sub.add_parser("verify", help="verify one provider harness")
    harness_verify.add_argument("lane", metavar="LANE")

    exec_command = sub.add_parser(
        "exec", help="run a command as a traced lane run, optionally locking test files"
    )
    exec_command.add_argument("lane", metavar="LANE")
    exec_command.add_argument(
        "--lock-commit", metavar="REV", help="fail with exit 3 if locked files changed since REV"
    )
    exec_command.add_argument(
        "--locked", metavar="GLOB", action="append",
        help="a protected glob (repeatable; default: the detected test files)",
    )
    exec_command.add_argument("argv", nargs="*", metavar="ARGV")

    promote_command = sub.add_parser(
        "promote", help="install one exact commit as the served build"
    )
    promote_command.add_argument("commit", metavar="COMMIT")
    promote_command.add_argument(
        "--force", action="store_true", help="promote even while lanes have live runs"
    )

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
        approval_socket = config._approval_socket_path(
            settings.session_root / f"a{os.getpid():x}.sock"
        )
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


def _split_exec_argv(raw: list[str]) -> tuple[list[str], list[str] | None]:
    """Separate `exec`'s own options from the command after `--`.

    argparse cannot tell an option that belongs to `exec` from one that belongs
    to the wrapped command, so the `--` boundary is split before parsing.
    """
    if "--" not in raw:
        return raw, None
    boundary = raw.index("--")
    if "exec" not in raw[:boundary]:
        return raw, None
    return raw[:boundary], raw[boundary + 1:]


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
    raw, exec_argv = _split_exec_argv(raw)
    args = _parser().parse_args(raw)
    if args.command == "exec" and exec_argv is not None:
        args.argv = exec_argv
    if args.command == "doctor":
        return cmd_doctor(args)
    if args.command == "lanes":
        return cmd_lanes(args)
    if args.command == "harness":
        return cmd_harness(args)
    if args.command == "exec":
        return cmd_exec(args)
    if args.command == "promote":
        return cmd_promote(args)
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
