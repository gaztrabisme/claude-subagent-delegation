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
from .config import ConfigError, Settings
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


def _write_config(data: Mapping[str, Any], dest: Path, force: bool) -> Settings:
    if dest.exists() and not force:
        raise ConfigError(
            f"{dest} already exists; use --force to overwrite, or --path to choose another file"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(_render_toml(data), encoding="utf-8")
    return config.load(extra=dest)


def _write_example(src: Path, dest: Path, force: bool) -> Settings:
    if dest.exists() and not force:
        raise ConfigError(
            f"{dest} already exists; use --force to overwrite, or --path to choose another file"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return config.load(extra=dest)


def _report_written(settings: Settings, dest: Path) -> None:
    print(f"wrote {dest}")
    for line in _needed_exports(settings):
        print(line)


def _from_env_data() -> dict[str, Any]:
    """The providers the old SAM_*/key environment stands for."""
    providers: dict[str, Any] = {}
    if os.environ.get("GLM_API_KEY") or os.environ.get("ZAI_API_KEY"):
        key_env = "GLM_API_KEY" if os.environ.get("GLM_API_KEY") else "ZAI_API_KEY"
        providers["glm"] = {
            "driver": "claude",
            "base_url": os.environ.get("SAM_GLM_BASE_URL") or "https://api.z.ai/api/anthropic",
            "model": os.environ.get("SAM_GLM_MODEL") or "glm-5.3-flash[1m]",
            "api_key_env": key_env,
            "vendor": "zai",
            "pricing": {"kind": "flat_plan", "monthly_usd": 80},
        }
    if os.environ.get("DEEPSEEK_API_KEY"):
        providers["deepseek"] = {
            "driver": "claude",
            "base_url": "https://api.deepseek.com/anthropic",
            "model": "deepseek-v4-pro",
            "api_key_env": "DEEPSEEK_API_KEY",
            "vendor": "deepseek",
            "pricing": {"kind": "per_token", "input": 1.32, "output": 3.96,
                        "cache_read": 0.044},
        }
    codex_bin = os.environ.get("SAM_CODEX_BIN")
    if codex_bin or shutil.which("codex"):
        codex: dict[str, Any] = {"driver": "codex"}
        if codex_bin:
            codex["binary"] = codex_bin
        codex["pricing"] = {"kind": "flat_plan"}
        providers["codex"] = codex
    core: dict[str, Any] = {}
    if providers:
        core["default_provider"] = next(iter(providers))
    return {"core": core, "providers": providers}


def _wizard(seed: dict[str, Any] | None, args: argparse.Namespace) -> int:
    """Ask which examples to merge, which providers to keep, and write them."""
    names = _example_names()
    if seed is None:
        print("examples:", ", ".join(names))
        choice = input("pick one or more examples (comma-separated) [full]: ").strip() or "full"
        chosen = [n.strip() for n in choice.split(",") if n.strip()]
        merged: dict[str, Any] = {}
        for name in chosen:
            if name not in names:
                print(f"error: unknown example {name!r}", file=sys.stderr)
                return 2
            merged = _merge_dict(merged, _load_example(name))
    else:
        merged = dict(seed)

    providers = merged.get("providers") or {}
    if not isinstance(providers, dict):
        providers = {}
    keep = input(f"provider names to keep [{', '.join(providers)}]: ").strip()
    if keep:
        wanted = [n.strip() for n in keep.split(",") if n.strip()]
        providers = {n: providers[n] for n in wanted if n in providers}

    # Ask which env var holds each key -- never the value.
    for name, table in list(providers.items()):
        if not isinstance(table, dict):
            continue
        envs = table.get("api_key_env")
        if isinstance(envs, str):
            envs = [envs]
        envs = [e for e in (envs or []) if isinstance(e, str)]
        if not envs:
            continue
        answer = input(f"key for provider {name!r} [{envs[0]}]: ").strip()
        if answer:
            table["api_key_env"] = answer
        elif isinstance(table.get("api_key_env"), list):
            table["api_key_env"] = envs[0]

    default = next(iter(providers), "")
    if providers:
        answer = input(f"default_provider [{default}]: ").strip()
        default = answer or default
    merged["providers"] = providers
    merged.setdefault("core", {})
    merged["core"]["default_provider"] = default

    dest = _dest_path(args.path)
    settings = _write_config(merged, dest, args.force)
    _report_written(settings, dest)
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    try:
        if args.from_env:
            if args.from_example:
                print("--from-env and --from are mutually exclusive", file=sys.stderr)
                return 2
            dest = _dest_path(args.path)
            settings = _write_config(_from_env_data(), dest, args.force)
            _report_written(settings, dest)
            return 0

        if args.from_example:
            src = _example_path(args.from_example)
            if not src.is_file():
                print(
                    f"unknown example {args.from_example!r}; "
                    f"examples: {', '.join(_example_names()) or '(none)'}",
                    file=sys.stderr,
                )
                return 2
            if args.yes:
                dest = _dest_path(args.path)
                settings = _write_example(src, dest, args.force)
                _report_written(settings, dest)
                return 0
            return _wizard(tomllib.loads(src.read_text(encoding="utf-8")), args)

        if args.yes:
            print("--yes needs --from <example-name>", file=sys.stderr)
            return 2

        return _wizard(None, args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


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

    tmp = Path(tempfile.mkdtemp(prefix="subagent-doctor-"))
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

    if args.json:
        print(json.dumps({"ok": not hard, "providers": rows}, indent=2))
    else:
        _print_table(rows, args)

    return 1 if hard else 0


# --- the delegate loop ----------------------------------------------------------


def _loop_parsers(subparsers: argparse._SubParsersAction) -> None:
    run = subparsers.add_parser("run", help="one worker round (or autopilot) on a plan")
    run.add_argument("--plan", metavar="FILE", help="the plan file (single-round mode)")
    run.add_argument("--parallel", metavar="MANIFEST", help="a manifest of parts, one worktree each")
    run.add_argument("--auto", action="store_true", help="retry/repair until done or stuck")
    run.add_argument("--continue", dest="continue_session", action="store_true",
                     help="continue the previous worker session")
    run.add_argument("--feedback", metavar="TEXT", help="reviewer feedback for this round")
    run.add_argument("--feedback-file", metavar="FILE", help="feedback from a file ('-' = stdin)")
    run.add_argument("--tier", choices=("normal", "hard"), default="normal")
    run.add_argument("--model", metavar="MODEL", help="override the tier's model")
    run.add_argument("--provider", metavar="NAME", help="override the tier's provider")
    run.add_argument("--test-outline", metavar="FILE", help="write tests from this outline (--auto)")
    run.add_argument("--background", action="store_true", help="start and return; collect with `wait`")
    run.add_argument("--wait", metavar="SECONDS", type=float, help="background, then wait up to S")
    run.add_argument("--run-id", metavar="ID", help="the run id (a background child reuses its parent's)")

    wait = subparsers.add_parser("wait", help="wait for the current run")
    wait.add_argument("--timeout", metavar="SECONDS", type=float, default=540.0)

    subparsers.add_parser("test", help="run the test suite")
    subparsers.add_parser("detect", help="detected test command and protected files")

    review = subparsers.add_parser("review", help="review the work (or the tests) so far")
    review.add_argument("--tier", choices=("normal", "hard"), default="normal")
    review.add_argument("--base", metavar="ID", help="checkpoint to diff against (default: task start)")
    review.add_argument("--tests", action="store_true", help="review the tests against the plan/spec")
    review.add_argument("--plan", metavar="FILE", help="the plan file (for --tests)")
    review.add_argument("--model", metavar="MODEL", help="override the reviewer's model")

    undo = subparsers.add_parser("undo", help="restore the working tree to a checkpoint")
    undo.add_argument("--to", metavar="ID", help="checkpoint to restore (default: before last round)")

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
    parser.add_argument("--root", metavar="PATH", help="the project root (default: current directory)")
    sub = parser.add_subparsers(dest="command")

    init = sub.add_parser("init", help="write a starter config from the examples")
    init.add_argument("--from", dest="from_example", metavar="EXAMPLE",
                      help="seed the wizard with one example")
    init.add_argument("--yes", action="store_true",
                      help="write --from EXAMPLE non-interactively")
    init.add_argument("--from-env", action="store_true",
                      help="convert the old SAM_*/GLM_API_KEY/DEEPSEEK_API_KEY environment")
    init.add_argument("--force", action="store_true",
                      help="overwrite an existing config file")
    init.add_argument("--path", metavar="PATH",
                      help="write to PATH instead of $XDG_CONFIG_HOME/subagent/config.toml")

    doctor = sub.add_parser("doctor", help="check the configured providers")
    doctor.add_argument("--provider", metavar="NAME", help="check only this provider")
    doctor.add_argument("--prompt", action="store_true",
                        help="also run one cheap turn on each provider")
    doctor.add_argument("--no-probe", action="store_true",
                        help="only validate the file and check binaries")
    doctor.add_argument("--json", action="store_true", help="print JSON instead of a table")

    sub.add_parser("report", help="summarize a trace into tables and an HTML dashboard",
                   add_help=False)

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


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "report":
        try:
            settings = config.load()
            settings.require_providers()
        except ConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        from .report import main as report_main

        return report_main(raw[1:])
    args = _parser().parse_args(raw)
    if args.command == "init":
        return cmd_init(args)
    if args.command == "doctor":
        return cmd_doctor(args)
    if args.command not in loop.LOOP_COMMANDS:
        _parser().print_help()
        return 2

    root = Path(args.root or ".").expanduser().resolve()
    try:
        settings = config.load(project_root=root)
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
