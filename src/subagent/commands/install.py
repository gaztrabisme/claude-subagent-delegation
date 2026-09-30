"""Temporary install command seam, completed by the installer unit."""

from __future__ import annotations

import argparse
import sys


def run(args: argparse.Namespace) -> int:
    del args
    print("not implemented", file=sys.stderr)
    return 2


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("install", help="install subagent prompts and MCP config")
    parser.add_argument(
        "--for", dest="for_harness", choices=("claude", "codex", "gemini", "copilot")
    )
    parser.add_argument("--target-dir", metavar="DIR")
    parser.add_argument("--timeout", metavar="SECONDS")
    parser.add_argument("--force", action="store_true")
    parser.set_defaults(command_handler=run)
