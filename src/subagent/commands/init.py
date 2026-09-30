"""Temporary init command seam; the legacy wizard remains in cli.py for S1."""

from __future__ import annotations

import argparse
import sys


def run(args: argparse.Namespace) -> int:
    del args
    print("not implemented", file=sys.stderr)
    return 2


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser("init", help="write a starter config from the examples")
    parser.add_argument("--from", dest="from_example", metavar="EXAMPLE",
                        help="seed the wizard with one example")
    parser.add_argument("--yes", action="store_true",
                        help="write --from EXAMPLE non-interactively")
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing config file")
    parser.add_argument("--path", metavar="PATH",
                        help="write to PATH instead of $XDG_CONFIG_HOME/subagent/config.toml")
    parser.set_defaults(command_handler=run)
    return parser
