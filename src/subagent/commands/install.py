"""CLI entry point for installing project harness files."""

from __future__ import annotations

import argparse
import sys

from .. import config
from .. import install as installer


def _positive_seconds(value: str) -> int:
    try:
        seconds = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if seconds <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return seconds


def _show_result(result: installer.InstallResult) -> None:
    print(f"Installed for: {result.harness}")
    print(f"Target: {result.target}")
    for path in result.written:
        print(f"Wrote: {path}")
    print(
        f"MCP registration: {result.registration} "
        "(server: subagent, command: subagent-mcp)"
    )
    print(f"Timeout: {result.timeout_seconds:g} seconds")


def run(args: argparse.Namespace) -> int:
    target = installer.resolve_target(args.target_dir, args.root)
    try:
        settings = config.load(project_root=target)
    except config.ConfigError as exc:
        print(f"error: cannot load default timeout: {exc}", file=sys.stderr)
        return 1

    timeout = args.timeout if args.timeout is not None else settings.run_timeout
    if timeout <= 0:
        args._install_parser.error("effective timeout must be a positive number of seconds")
    if args.for_harness == "copilot" and timeout < 60:
        args._install_parser.error("Copilot requires a timeout of at least 60 seconds")
    try:
        result = installer.install(
            args.for_harness, target, timeout, force=args.force
        )
    except installer.InstallError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _show_result(result)
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("install", help="install subagent prompts and MCP config")
    parser.add_argument(
        "--for", dest="for_harness", choices=installer.HARNESS_NAMES, required=True
    )
    parser.add_argument("--target-dir", metavar="DIR")
    parser.add_argument("--timeout", metavar="SECONDS", type=_positive_seconds)
    parser.add_argument("--force", action="store_true")
    parser.set_defaults(command_handler=run, _install_parser=parser)
