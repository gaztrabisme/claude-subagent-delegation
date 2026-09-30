"""Provider configuration, selection, and local endpoint discovery commands."""

from __future__ import annotations

import argparse
import json
import re
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


def _table_path(line: str) -> tuple[str, ...] | None:
    if not line.lstrip().startswith("["):
        return None
    try:
        document = tomllib.loads(line)
    except tomllib.TOMLDecodeError:
        return None

    def empty_table(value: Any, prefix: tuple[str, ...] = ()) -> tuple[str, ...] | None:
        if isinstance(value, dict):
            if not value:
                return prefix
            for key, item in value.items():
                found = empty_table(item, (*prefix, str(key)))
                if found is not None:
                    return found
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            return empty_table(value[0], prefix)
        return None

    return empty_table(document)


def _comment_suffix(line: str, quote: str | None) -> tuple[str | None, str | None]:
    index = 0
    while index < len(line):
        if quote:
            if len(quote) == 3 and line.startswith(quote, index):
                quote = None
                index += 3
                continue
            if len(quote) == 1 and line[index] == quote:
                quote = None
            elif quote[0] == '"' and line[index] == "\\":
                index += 2
                continue
            index += 1
            continue
        if line[index] == "#":
            return line[index:], quote
        if line[index] in ('"', "'"):
            quote = line[index] * (3 if line.startswith(line[index] * 3, index) else 1)
            index += len(quote)
            continue
        index += 1
    return None, quote


def _assignment_path(expression: str) -> tuple[str, ...] | None:
    try:
        document = tomllib.loads(f"{expression}.__subagent_marker = true")
    except tomllib.TOMLDecodeError:
        return None

    def marker_path(value: Any, prefix: tuple[str, ...] = ()) -> tuple[str, ...] | None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "__subagent_marker":
                    return prefix
                found = marker_path(item, (*prefix, str(key)))
                if found is not None:
                    return found
        return None

    return marker_path(document)


def _with_separator(raw: str, addition: str) -> str:
    if not raw:
        return addition
    if raw.endswith("\n\n") or raw.endswith("\r\n\r\n"):
        return raw + addition
    if raw.endswith(("\n", "\r")):
        return raw + "\n" + addition
    return raw + "\n\n" + addition


def _write_text(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tomllib.loads(text)
        path.write_text(text, encoding="utf-8")
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot write config {str(path)!r}: {exc}") from exc


def _write_provider(path: Path, name: str, provider: Mapping[str, Any] | None) -> None:
    try:
        raw = path.read_text(encoding="utf-8") if path.exists() else ""
        tomllib.loads(raw)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    lines = raw.splitlines(keepends=True)
    kept: list[str] = []
    comments: list[str] = []
    active: tuple[str, ...] = ()
    quote: str | None = None
    for line in lines:
        header = _table_path(line)
        if header is not None:
            active = header
        remove = len(active) >= 2 and active[:2] == ("providers", name)
        if not remove and active == ("providers",):
            assignment = re.match(r"^\s*(.+?)\s*=", line)
            key_path = _assignment_path(assignment.group(1)) if assignment else None
            remove = bool(key_path and key_path[0] == name)
        comment, quote = _comment_suffix(line, quote)
        if remove:
            if comment:
                comments.append(comment)
        else:
            kept.append(line)

    updated = "".join(kept)
    comment_text = "".join(comments)
    if provider is not None and comment_text and not comment_text.endswith(("\n", "\r")):
        comment_text += "\n"
    addition = comment_text
    if provider is not None:
        addition += _cli()._render_toml({"providers": {name: dict(provider)}})
    if addition:
        updated = _with_separator(updated, addition)
    _write_text(path, updated)


def _write_default_provider(path: Path, name: str) -> None:
    try:
        raw = path.read_text(encoding="utf-8") if path.exists() else ""
        document = tomllib.loads(raw)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    lines = raw.splitlines(keepends=True)
    core_header: int | None = None
    active: tuple[str, ...] = ()
    assignment_index: int | None = None
    for index, line in enumerate(lines):
        header = _table_path(line)
        if header is not None:
            active = header
            if header == ("core",):
                core_header = index
            continue
        if active == ("core",) and re.match(r"^\s*default_provider\s*=", line):
            assignment_index = index

    if core_header is None and isinstance(document.get("core"), Mapping):
        raise ConfigError(f"cannot edit inline [core] table in {str(path)!r}")

    rendered_value = json.dumps(name, ensure_ascii=False)
    if assignment_index is not None:
        line = lines[assignment_index]
        newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        body = line[:-len(newline)] if newline else line
        comment, _ = _comment_suffix(body, None)
        comment_at = len(body) - len(comment) if comment else len(body)
        comment = comment or ""
        before = body[:comment_at]
        prefix = re.match(r"^(\s*default_provider\s*=\s*)", before)
        assert prefix is not None
        tail = before[prefix.end():]
        trailing_space = tail[len(tail.rstrip(" \t")):]
        lines[assignment_index] = (
            prefix.group(1) + rendered_value + trailing_space + comment + newline
        )
        updated = "".join(lines)
    elif core_header is not None:
        line_ending = "\r\n" if lines[core_header].endswith("\r\n") else "\n"
        lines.insert(core_header + 1, f"default_provider = {rendered_value}{line_ending}")
        updated = "".join(lines)
    else:
        section = f"[core]\ndefault_provider = {rendered_value}\n"
        updated = _with_separator(raw, section)
    _write_text(path, updated)


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
            key: (
                "<redacted>"
                if str(key).casefold() not in {"api_key_env", "api_key_envs"}
                and any(
                    marker in str(key).casefold()
                    for marker in ("key", "token", "secret", "password")
                )
                else _redact(item)
            )
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
        _write_provider(path, name, provider)
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
        _write_provider(path, args.name, None)
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
        _write_default_provider(path, args.name)
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
