"""Provider configuration, selection, and local endpoint discovery commands."""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .. import config, health
from ..config import ConfigError


def _cli():
    # cli imports this module to register the command group.
    from .. import cli

    return cli


def _project_root(args: argparse.Namespace) -> Path:
    root = Path(args.root or ".").expanduser().resolve()
    if args.root:
        return root
    for candidate in (root, *root.parents):
        if (candidate / config.PROJECT_DIR / config.CONFIG_NAME).is_file():
            return candidate
    return root


def _destination(args: argparse.Namespace, root: Path) -> Path:
    if args.project:
        return root / config.PROJECT_DIR / config.CONFIG_NAME
    return _cli()._dest_path(None)


def _target_data(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def _write_target(path: Path, data: Mapping[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_cli()._render_toml(data), encoding="utf-8")
    except (OSError, ConfigError, TypeError, ValueError) as exc:
        raise ConfigError(f"cannot write config {str(path)!r}: {exc}") from exc


def _tables(data: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"[{key}] must be a table")
    return dict(value)


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: "<redacted>" if key == "api_key" else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _provider_list(args: argparse.Namespace, root: Path) -> int:
    settings = config.load(project_root=root)
    data = config.read_config(project_root=root)
    raw_providers = _tables(data, "providers")
    providers = {
        name: {
            **_redact(dict(raw_providers.get(name, {}))),
            "is_default": name == settings.default_provider,
        }
        for name in settings.providers
    }
    if args.json:
        print(json.dumps({
            "default_provider": settings.default_provider,
            "providers": providers,
        }, indent=2))
        return 0

    for name, provider in providers.items():
        marker = "*" if provider["is_default"] else " "
        driver = provider.get("driver", "?")
        model = provider.get("model")
        suffix = f" model={model}" if model else ""
        print(f"{marker} {name} ({driver}){suffix}")
    return 0


def _preset_provider(preset: str) -> tuple[str, dict[str, Any]]:
    cli = _cli()
    try:
        example = cli._load_example(preset)
    except ConfigError as exc:
        raise _UnknownExample(str(exc)) from exc
    providers = _tables(example, "providers")
    selected = _tables(example, "core").get("default_provider")
    if not isinstance(selected, str) or selected not in providers:
        if len(providers) != 1:
            raise ConfigError(f"example {preset!r} does not select one provider")
        selected = next(iter(providers))
    table = providers[selected]
    if not isinstance(table, Mapping):
        raise ConfigError(f"example provider {selected!r} is not a table")
    return selected, dict(table)


class _UnknownExample(ConfigError):
    pass


def _valid_base_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError as exc:
        raise argparse.ArgumentTypeError("invalid URL") from exc
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or any(ch.isspace() for ch in value)
    ):
        raise argparse.ArgumentTypeError("URL must be an http(s) base URL without credentials")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _model_id(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, Mapping):
        for key in ("id", "model_id", "model"):
            found = value.get(key)
            if isinstance(found, str) and found.strip():
                return found.strip()
    return None


def _first_loaded_model(data: Mapping[str, Any]) -> str | None:
    loaded = data.get("loaded_models")
    if isinstance(loaded, list):
        for item in loaded:
            found = _model_id(item)
            if found:
                return found
    return None


def _first_api_model(data: Mapping[str, Any] | None) -> str | None:
    models = data.get("data") if data else None
    if isinstance(models, list):
        for item in models:
            found = _model_id(item)
            if found:
                return found
    return None


def _json_response(response: tuple[int, bytes] | None) -> dict[str, Any] | None:
    if response is None or response[0] != 200:
        return None
    try:
        body = json.loads(response[1].decode("utf-8"))
    except (UnicodeError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _discover(base_url: str) -> tuple[dict[str, Any] | None, str | None]:
    urls = {
        "models": f"{base_url}/v1/models",
        "status": f"{base_url}/api/status",
        "metrics": f"{base_url}/metrics",
        "slots": f"{base_url}/slots",
    }
    responses = {
        name: health._http_get(url, timeout=health.PROBE_TIMEOUT)
        for name, url in urls.items()
    }
    models_data = _json_response(responses["models"])
    api_model = _first_api_model(models_data)

    status_data = _json_response(responses["status"])
    is_omlx = bool(status_data and status_data.get("status") == "ok")
    loaded_model = _first_loaded_model(status_data) if is_omlx and status_data else None
    model = loaded_model or api_model
    if not model:
        models_answered = responses["models"] is not None and responses["models"][0] == 200
        if is_omlx or models_answered:
            return None, "model"
        return None, "unsupported"

    health_kind = "omlx" if is_omlx else "http"
    health_url = urls["status"] if is_omlx else urls["models"]
    provider: dict[str, Any] = {
        "driver": "omp",
        "base_url": base_url,
        "model": model,
        "local": True,
        "health": {"kind": health_kind, "url": health_url},
    }
    probes = {
        f"{key}_url": urls[key]
        for key in ("metrics", "slots")
        if responses[key] is not None and 200 <= responses[key][0] < 300
    }
    if probes:
        provider["probe"] = probes
    return provider, None


def _provider_add(args: argparse.Namespace, root: Path) -> int:
    if not args.preset and not args.url:
        print("error: provider add requires --from PRESET or --url URL", file=sys.stderr)
        return 2
    if args.preset and not args.name:
        args.command_parser.error("NAME is required with --from PRESET")
    name = args.name or "local"
    if not name.strip():
        args.command_parser.error("provider name must not be empty")

    path = _destination(args, root)
    try:
        target = _target_data(path)
        target_providers = _tables(target, "providers")
        effective = config.read_config(project_root=root)
        effective_providers = _tables(effective, "providers")
    except (ConfigError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if name in effective_providers and not args.force:
        print(f"error: provider {name!r} already exists; use --force", file=sys.stderr)
        return 1

    preset_name: str | None = None
    if args.preset:
        try:
            preset_name, provider = _preset_provider(args.preset)
        except _UnknownExample as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        except ConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    else:
        provider, problem = _discover(args.url)
        if problem == "unsupported":
            print(
                f"error: no supported local model endpoint at {args.url!r}", file=sys.stderr
            )
            return 1
        if problem == "model":
            print(f"error: local endpoint at {args.url!r} returned no model id", file=sys.stderr)
            return 1
        assert provider is not None

    try:
        target_providers[name] = provider
        target["providers"] = target_providers
        _write_target(path, target)
    except (ConfigError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if preset_name:
        print(f"added provider {name!r} from example {args.preset!r}")
    else:
        print(f"added local provider {name!r} ({provider['model']})")
    return 0


def _provider_remove(args: argparse.Namespace, root: Path) -> int:
    try:
        settings = config.load(project_root=root)
        if args.name not in settings.providers:
            print(f"error: unknown provider {args.name!r}", file=sys.stderr)
            return 1
        if args.name == settings.default_provider:
            print(
                f"error: cannot remove default provider {args.name!r}; select another default "
                "provider first",
                file=sys.stderr,
            )
            return 1
        path = _destination(args, root)
        data = _target_data(path)
        providers = _tables(data, "providers")
        if args.name not in providers:
            print(f"error: provider {args.name!r} is not in {str(path)!r}", file=sys.stderr)
            return 1
        del providers[args.name]
        if providers:
            data["providers"] = providers
        else:
            data.pop("providers", None)
        _write_target(path, data)
    except (ConfigError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"removed provider {args.name!r}")
    return 0


def _use(args: argparse.Namespace, root: Path) -> int:
    try:
        settings = config.load(project_root=root)
        if args.name not in settings.providers:
            print(f"error: unknown provider {args.name!r}", file=sys.stderr)
            return 1
        path = _destination(args, root)
        data = _target_data(path)
        core = _tables(data, "core")
        core["default_provider"] = args.name
        data["core"] = core
        _write_target(path, data)
    except (ConfigError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"default provider set to {args.name!r}")
    return 0


def _provider_test(args: argparse.Namespace, root: Path) -> int:
    try:
        settings = config.load(project_root=root)
        if args.name not in settings.providers:
            print(f"error: unknown provider {args.name!r}", file=sys.stderr)
            return 1
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    cli = _cli()
    doctor_args = SimpleNamespace(no_probe=False, prompt=args.prompt)
    row, failed = cli._doctor_row(settings, settings.providers[args.name], doctor_args)
    if args.json:
        print(json.dumps({"ok": not failed, "providers": [row]}, indent=2))
    else:
        cli._print_table([row], doctor_args)
    return 1 if failed else 0


def run(args: argparse.Namespace) -> int:
    if not hasattr(args, "command"):
        print("error: provider command requires parsed arguments", file=sys.stderr)
        return 2
    root = _project_root(args)
    try:
        if args.command == "use":
            return _use(args, root)
        action = args.provider_action
        if action == "add":
            return _provider_add(args, root)
        if action == "list":
            return _provider_list(args, root)
        if action == "remove":
            return _provider_remove(args, root)
        if action == "test":
            return _provider_test(args, root)
    except (ConfigError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("error: provider requires add, list, remove or test", file=sys.stderr)
    return 2


def register(subparsers: argparse._SubParsersAction) -> None:
    provider = subparsers.add_parser("provider", help="manage configured providers")
    provider.set_defaults(command_handler=run)
    actions = provider.add_subparsers(dest="provider_action")

    add = actions.add_parser("add", help="add a provider from an example or local URL")
    add.add_argument("name", nargs="?", help="provider name (defaults to local for --url)")
    add.set_defaults(command_parser=add)
    source = add.add_mutually_exclusive_group()
    source.add_argument("--from", dest="preset", metavar="PRESET")
    source.add_argument("--url", metavar="URL", type=_valid_base_url)
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
