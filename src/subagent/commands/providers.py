"""Temporary provider command seam, completed by the provider CLI unit."""

from __future__ import annotations

import argparse
import sys


def run(args: argparse.Namespace) -> int:
    del args
    print("not implemented", file=sys.stderr)
    return 2


def register(subparsers: argparse._SubParsersAction) -> None:
    provider = subparsers.add_parser("provider", help="manage configured providers")
    provider.set_defaults(command_handler=run)
    actions = provider.add_subparsers(dest="provider_action")

    add = actions.add_parser("add", help="add a provider from an example or local URL")
    add.add_argument("name", nargs="?", help="provider name (defaults to local for --url)")
    add.add_argument("--from", dest="preset", metavar="PRESET")
    add.add_argument("--url", metavar="URL")
    add.add_argument("--project", action="store_true")
    add.add_argument("--force", action="store_true")
    add.set_defaults(command_handler=run)

    listing = actions.add_parser("list", help="list configured providers")
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(command_handler=run)

    remove = actions.add_parser("remove", help="remove a provider")
    remove.add_argument("name")
    remove.add_argument("--project", action="store_true")
    remove.set_defaults(command_handler=run)

    test = actions.add_parser("test", help="check a provider")
    test.add_argument("name")
    test.add_argument("--prompt", action="store_true")
    test.add_argument("--json", action="store_true")
    test.set_defaults(command_handler=run)

    use = subparsers.add_parser("use", help="select the default provider")
    use.add_argument("name")
    use.add_argument("--project", action="store_true")
    use.set_defaults(command_handler=run)
