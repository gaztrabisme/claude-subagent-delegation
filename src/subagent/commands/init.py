"""Create a starter config and offer installed subscription CLI providers."""

from __future__ import annotations

import argparse
import shutil
import sys
from collections.abc import Mapping
from typing import Any

from .. import config
from ..config import ConfigError

SUBSCRIPTION_CLIS = ("claude", "codex", "copilot", "gemini", "grok")


def _cli():
    # cli owns the shared example, destination and TOML helpers.
    from .. import cli

    return cli


def detect_subscription_clis() -> list[str]:
    """Return supported CLI names present on PATH without running them."""
    return [name for name in SUBSCRIPTION_CLIS if shutil.which(name)]


def _provider_table(name: str) -> dict[str, Any]:
    table: dict[str, Any] = {
        "driver": name,
        "pricing": {"kind": "flat_plan"},
    }
    if name == "claude":
        table["auth"] = "login"
    return table


def _include_subscription_clis(data: dict[str, Any], names: list[str]) -> dict[str, Any]:
    providers = data.get("providers")
    providers = {} if not isinstance(providers, dict) else dict(providers)
    providers.update({name: _provider_table(name) for name in names})
    data["providers"] = providers
    return data


def _choose_tools(detected: list[str]) -> list[str]:
    if not detected:
        return []
    answer = input("subscription CLIs to include (comma-separated) [none]: ").strip()
    wanted = {name.strip() for name in answer.split(",") if name.strip()}
    return [name for name in detected if name in wanted]


def _load_seed(name: str) -> dict[str, Any] | None:
    cli = _cli()
    try:
        return cli._load_example(name)
    except ConfigError:
        examples = ", ".join(cli._example_names()) or "(none)"
        print(f"error: unknown example {name!r}; examples: {examples}", file=sys.stderr)
        return None


def _wizard(seed: dict[str, Any] | None, args: argparse.Namespace,
            detected: list[str]) -> int:
    cli = _cli()
    names = cli._example_names()
    if detected:
        print("detected subscription CLIs:", ", ".join(detected))
    if seed is None:
        print("examples:", ", ".join(names))
        choice = input("pick one or more examples (comma-separated) [full]: ").strip() or "full"
        merged: dict[str, Any] = {}
        for name in (part.strip() for part in choice.split(",")):
            if not name:
                continue
            example = _load_seed(name)
            if example is None:
                return 2
            merged = cli._merge_dict(merged, example)
    else:
        merged = dict(seed)

    selected = _choose_tools(detected)
    if selected:
        merged = _include_subscription_clis(merged, selected)

    providers = merged.get("providers") or {}
    if not isinstance(providers, dict):
        providers = {}
    keep = input(f"provider names to keep [{', '.join(providers)}]: ").strip()
    if keep:
        wanted = [name.strip() for name in keep.split(",") if name.strip()]
        providers = {name: providers[name] for name in wanted if name in providers}

    # Ask which env var holds each key -- never the value.
    for name, table in list(providers.items()):
        if not isinstance(table, dict):
            continue
        envs = table.get("api_key_env")
        if isinstance(envs, str):
            envs = [envs]
        envs = [value for value in (envs or []) if isinstance(value, str)]
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
    return _write(merged, args)


def _write(data: Mapping[str, Any], args: argparse.Namespace) -> int:
    cli = _cli()
    dest = cli._dest_path(args.path)
    if dest.exists() and not args.force:
        print(
            f"error: {dest} already exists; use --force to overwrite, or --path to choose "
            "another file",
            file=sys.stderr,
        )
        return 1
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(cli._render_toml(data), encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot write config {str(dest)!r}: {exc}", file=sys.stderr)
        return 1
    try:
        settings = config.load(extra=dest)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {dest}")
    for line in cli._needed_exports(settings):
        print(line)
    return 0


def run(args: argparse.Namespace) -> int:
    detected = detect_subscription_clis()
    if not detected:
        print("No supported subscription CLI detected.")

    seed_name = args.from_example
    if args.yes and seed_name is None:
        seed_name = "full"
    seed = _load_seed(seed_name) if seed_name else None
    if seed_name and seed is None:
        return 2
    if args.yes:
        merged = seed or {}
        merged = _include_subscription_clis(merged, detected)
        return _write(merged, args)
    return _wizard(seed, args, detected)


def register(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser("init", help="write a starter config from examples and CLIs")
    parser.add_argument("--from", dest="from_example", metavar="EXAMPLE",
                        help="seed the wizard with one example")
    parser.add_argument("--yes", action="store_true",
                        help="write --from EXAMPLE and all detected CLIs non-interactively")
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing config file")
    parser.add_argument("--path", metavar="PATH",
                        help="write to PATH instead of $XDG_CONFIG_HOME/subagent/config.toml")
    parser.set_defaults(command_handler=run)
    return parser
