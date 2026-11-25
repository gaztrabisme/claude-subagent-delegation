"""Version memory, conformance verification and operator-driven CLI updates."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .lane_state import LaneState
from .lane_status import LaneStatusStore

DEFAULT_TIMEOUT = 10.0
_VERSION = re.compile(r"\d+(?:\.\d+)+(?:[-+][A-Za-z0-9][A-Za-z0-9.-]*)?")
_DEFAULT_BINARIES = {
    "claude": "claude",
    "codex": "codex",
    "copilot": "copilot",
    "gemini": "gemini",
    "grok": "grok",
    "antigravity": "agy",
    "omp": "omp",
    "epi": "epi",
}
Runner = Callable[[list[str], float], tuple[int, str, str]]


def default_runner(argv: list[str], timeout: float = DEFAULT_TIMEOUT) -> tuple[int, str, str]:
    """Run argv without a shell, capturing output and bounding its duration."""
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def _harness(cfg: Any) -> Mapping[str, Any]:
    value = getattr(cfg, "extra", {}).get("harness", {})
    return value if isinstance(value, Mapping) else {}


def _argv(value: Any) -> list[str] | None:
    if not isinstance(value, (list, tuple)) or not value:
        return None
    if not all(isinstance(part, str) and part for part in value):
        return None
    return list(value)


def _binary(cfg: Any) -> str:
    return getattr(cfg, "binary", None) or _DEFAULT_BINARIES.get(
        getattr(cfg, "driver", ""), getattr(cfg, "driver", "")
    )


def _invoke(
    runner: Runner, argv: list[str], timeout: float = DEFAULT_TIMEOUT
) -> tuple[int, str, str] | None:
    try:
        result = runner(list(argv), timeout)
    except Exception:  # noqa: BLE001 - harness failures become a failed check
        return None
    if not isinstance(result, tuple) or len(result) != 3:
        return None
    code, stdout, stderr = result
    if isinstance(code, bool) or not isinstance(code, int):
        return None
    return code, str(stdout or ""), str(stderr or "")


def _version(text: str) -> str | None:
    match = _VERSION.search(text or "")
    return match.group(0) if match else None


def detect_version(cfg: Any, runner: Runner = default_runner) -> str | None:
    """Run a configured CLI's version command and return its first version token."""
    raw_args = _harness(cfg).get("version_args")
    args = (
        list(raw_args)
        if isinstance(raw_args, (list, tuple))
        and all(isinstance(arg, str) and arg for arg in raw_args)
        else ["--version"]
    )
    result = _invoke(runner, [_binary(cfg), *args])
    if result is None or result[0] != 0:
        return None
    return _version(result[1]) or _version(result[2])


def _when(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.astimezone()
    return current.astimezone(UTC)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(UTC)


def check_updates(
    settings: Any,
    runner: Runner = default_runner,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Check configured package sources, caching one result per lane interval."""
    current_time = _when(now)
    store = LaneStatusStore(settings.session_root, redact_keys=settings.leaked_keys)
    results: dict[str, dict[str, Any]] = {}
    for lane, cfg in settings.providers.items():
        harness_cfg = _harness(cfg)
        check_cmd = _argv(harness_cfg.get("update_check"))
        if check_cmd is None:
            continue
        if _contains_sudo(check_cmd):
            results[lane] = {
                "current": None,
                "available": None,
                "checked_at": current_time.isoformat(),
                "refused": True,
                "reason": "sudo is refused",
            }
            continue
        saved = store.harness_record(lane).get("harness_check")
        checked_at = _parse_time(saved.get("checked_at")) if isinstance(saved, dict) else None
        try:
            interval = max(0.0, float(harness_cfg.get("check_interval_hours", 24)))
        except (TypeError, ValueError):
            interval = 24.0
        if (
            isinstance(saved, dict)
            and checked_at is not None
            and current_time - checked_at < timedelta(hours=interval)
            and {"current", "available", "checked_at"} <= saved.keys()
        ):
            results[lane] = {
                "current": saved.get("current"),
                "available": saved.get("available"),
                "checked_at": saved["checked_at"],
            }
            continue

        version = detect_version(cfg, runner)
        command_result = _invoke(runner, check_cmd)
        available = None
        if command_result is not None and command_result[0] == 0:
            available = _version(command_result[1]) or _version(command_result[2])
        result = {
            "current": version,
            "available": available,
            "checked_at": current_time.isoformat(),
        }
        store.record_harness_check(lane, result, version=version)
        results[lane] = result
    return results


def status(settings: Any) -> list[dict[str, Any]]:
    """Return stored harness state for CLI display without probing binaries."""
    snapshot = LaneStatusStore(
        settings.session_root, redact_keys=settings.leaked_keys
    ).snapshot(settings, count_runs=False)
    memory = LaneState(settings.session_root)
    return [
        {
            "lane": lane,
            "harness_version": row.get("harness_version"),
            "harness_state": row.get("harness_state", "unknown"),
            "verified_version": memory.verified_version(lane),
        }
        for lane, row in snapshot.items()
    ]


def _translator(cfg: Any, lane: str) -> Any:
    from .providers import for_driver
    from .providers.base import Session

    driver = for_driver(cfg.driver)
    return driver.translator(Session(provider=lane))


def _event_type(event: Any) -> str:
    if not isinstance(event, dict):
        return "(invalid event)"
    return str(event.get("type") or event.get("event") or "(missing type)")


def _missing_expected(expected: Any, actual: Any, path: str = "") -> tuple[str, Any, Any] | None:
    """Find the first expected JSON value that is absent or differs."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return path or "event", expected, actual
        for key, value in expected.items():
            child_path = f"{path}.{key}" if path else str(key)
            if key not in actual:
                return child_path, value, "<missing>"
            found = _missing_expected(value, actual[key], child_path)
            if found is not None:
                return found
        return None
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return path or "events", expected, actual
        for index, value in enumerate(expected):
            child_path = f"{path}[{index}]"
            if index >= len(actual):
                return child_path, value, "<missing>"
            found = _missing_expected(value, actual[index], child_path)
            if found is not None:
                return found
        if len(actual) > len(expected):
            child_path = f"{path}[{len(expected)}]"
            return child_path, "<no additional items>", actual[len(expected)]
        return None
    if expected != actual:
        return path or "value", expected, actual
    return None


def _verify_fixtures(cfg: Any, lane: str, fixtures_dir: Path) -> list[str]:
    driver_dir = Path(fixtures_dir) / cfg.driver
    paths = sorted(driver_dir.glob("*.jsonl")) if driver_dir.is_dir() else []
    errors: list[str] = []
    if not paths:
        return [f"{cfg.driver}: no conformance fixtures found"]
    for fixture in paths:
        expected_path = fixture.with_suffix(".expected.json")
        try:
            inputs = [
                json.loads(line)
                for line in fixture.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            expected = json.loads(expected_path.read_text(encoding="utf-8"))
            if not isinstance(expected, list):
                raise ValueError("expected output must be a JSON array")
        except (OSError, ValueError, TypeError) as exc:
            errors.append(f"{fixture.stem}: invalid fixture: {exc}")
            continue

        try:
            translator = _translator(cfg, lane)
            from .providers import for_driver

            known_event_types = getattr(for_driver(cfg.driver), "known_event_types", None)
            if not known_event_types:
                errors.append(
                    f"{fixture.stem}: {cfg.driver} declares no known_event_types"
                )
                continue
            actual: list[dict[str, Any]] = []
            unknown: list[str] = []
            for event in inputs:
                if not isinstance(event, dict):
                    raise ValueError("fixture event must be a JSON object")
                event_type = _event_type(event)
                if event_type not in known_event_types:
                    unknown.append(event_type)
                    continue
                produced = [event] if translator is None else translator.feed(event)
                if not isinstance(produced, list):
                    raise ValueError(f"translator returned a non-list for {_event_type(event)}")
                actual.extend(produced)
            if unknown:
                errors.append(
                    f"{fixture.stem}: event type {unknown[0]} is not translated by {cfg.driver}"
                )
                continue
            difference = _missing_expected(expected, actual)
            if difference is not None:
                path, wanted, observed = difference
                event_index = None
                match = re.search(r"events?\[(\d+)\]|\[(\d+)\]", path)
                if match:
                    event_index = int(match.group(1) or match.group(2))
                if event_index is not None and event_index < len(expected):
                    event_type = _event_type(expected[event_index])
                else:
                    event_type = _event_type(expected[-1]) if expected else _event_type(inputs[-1])
                errors.append(
                    f"{fixture.stem}: drift for event type {event_type} at {path}; "
                    f"expected {wanted!r}, got {observed!r}"
                )
        except Exception as exc:  # noqa: BLE001 - a translator crash is conformance drift
            event_type = _event_type(inputs[-1]) if inputs else "(empty fixture)"
            errors.append(f"{fixture.stem}: drift for event type {event_type}: {exc}")
    return errors


def verify(
    lane: str,
    settings: Any,
    runner: Runner = default_runner,
    fixtures_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Run fixture translation and optional smoke checks for one configured lane."""
    cfg = settings.providers.get(lane)
    if cfg is None:
        return {"ok": False, "lane": lane, "errors": [f"unknown lane {lane!r}"]}
    store = LaneStatusStore(settings.session_root, redact_keys=settings.leaked_keys)
    smoke = _argv(_harness(cfg).get("smoke"))
    if smoke is not None and _contains_sudo(smoke):
        return {
            "ok": False,
            "refused": True,
            "lane": lane,
            "errors": ["sudo is refused in smoke command"],
        }
    version = detect_version(cfg, runner)
    store.record_harness_version(lane, version)
    errors: list[str] = []
    if version is None:
        errors.append(f"{lane}: could not detect harness version")
    conformance = (
        Path(fixtures_dir) if fixtures_dir is not None else
        Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "conformance"
    )
    errors.extend(_verify_fixtures(cfg, lane, conformance))
    if smoke is not None:
        result = _invoke(runner, smoke)
        if result is None or result[0] != 0:
            detail = "runner failed" if result is None else (result[2] or result[1]).strip()
            errors.append(f"{lane}: smoke command failed{(': ' + detail) if detail else ''}")
    ok = not errors
    if ok and version is not None:
        memory = LaneState(settings.session_root)
        memory.record_verified(lane, version)
        closure = memory.closed(lane)
        if closure is not None and closure.get("code") == "harness_unverified":
            memory.reopen(lane)
            store.record_probe(lane, True, detail="harness verification passed")
    return {"ok": ok, "lane": lane, "version": version, "errors": errors}


_SHELLS = frozenset({"bash", "sh", "dash", "zsh", "ksh", "ash"})
_SHELL_SEPARATORS = frozenset({";", "&", "&&", "|", "||", "(", ")", "\n"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", re.DOTALL)


def _is_sudo_word(value: str) -> bool:
    return Path(value).name.lower() == "sudo"


def _env_command(argv: list[str]) -> list[str]:
    """Return the command after env options and variable assignments."""
    index = 0
    while index < len(argv):
        part = argv[index]
        if part == "--":
            return argv[index + 1:]
        if part in ("-u", "--unset", "-C", "--chdir"):
            index += 2
            continue
        if part in ("-S", "--split-string"):
            if index + 1 >= len(argv):
                return []
            try:
                split = shlex.split(argv[index + 1])
            except ValueError:
                return []
            return [*split, *argv[index + 2:]]
        if part.startswith("-") or _ASSIGNMENT.match(part):
            index += 1
            continue
        return argv[index:]
    return []


def _shell_script(argv: list[str]) -> str | None:
    """Read the script passed to a POSIX shell's -c/--command option."""
    index = 1
    while index < len(argv):
        part = argv[index]
        if part == "--":
            return None
        if part == "--command":
            return argv[index + 1] if index + 1 < len(argv) else ""
        if part.startswith("--command="):
            return part.partition("=")[2]
        if part.startswith("-") and part != "-":
            if "c" in part[1:]:
                return argv[index + 1] if index + 1 < len(argv) else ""
            index += 1
            continue
        index += 1
    return None


def _script_commands(script: str) -> list[list[str]]:
    """Split shell text into command words while respecting quotes/comments."""
    lexer = shlex.shlex(script, posix=True, punctuation_chars=";&|()<>\n")
    lexer.whitespace = lexer.whitespace.replace("\n", "")
    lexer.whitespace_split = True
    commands: list[list[str]] = []
    current: list[str] = []
    for token in lexer:
        if token in _SHELL_SEPARATORS:
            if current:
                commands.append(current)
                current = []
        elif token in (">", ">>", "<", "<<", "<>", "<&", ">&"):
            # Redirection targets are not command words. They are harmless to
            # sudo detection, so retain them for the leading-word parser.
            current.append(token)
        else:
            current.append(token)
    if current:
        commands.append(current)
    return commands


def _command_word(words: list[str]) -> list[str]:
    """Skip shell assignments, control keywords and leading redirections."""
    index = 0
    control = {"!", "if", "then", "else", "elif", "while", "until", "do"}
    while index < len(words):
        word = words[index]
        if word in control or _ASSIGNMENT.match(word):
            index += 1
            continue
        if word in (">", ">>", "<", "<<", "<>", "<&", ">&"):
            index += 2
            continue
        return words[index:]
    return []


def _contains_sudo_argv(command: list[str]) -> bool:
    words = _command_word(command)
    if not words:
        return False
    executable = Path(words[0]).name.lower()
    if executable == "sudo" or _is_sudo_word(words[0]):
        return True
    if executable == "env":
        return _contains_sudo_argv(_env_command(words[1:]))
    if executable in _SHELLS:
        script = _shell_script(words)
        return _contains_sudo_script(script) if script is not None else False
    if executable in {"command", "exec", "nohup", "time"}:
        index = 1
        while index < len(words) and words[index].startswith("-"):
            index += 1
        return _contains_sudo_argv(words[index:])
    return False


def _contains_sudo_script(script: str) -> bool:
    try:
        return any(_contains_sudo_argv(words) for words in _script_commands(script))
    except ValueError:  # malformed shell quoting is refused if it names sudo
        return bool(re.search(r"(?<![\w.-])(?:[^\s;&|()<>]*/)?sudo(?=$|[\s;&|()<>])", script))


def _contains_sudo(command: list[str]) -> bool:
    """Whether argv invokes sudo directly or in a supported shell wrapper."""
    return _contains_sudo_argv(command)


def update(
    lane: str,
    settings: Any,
    runner: Runner = default_runner,
) -> dict[str, Any]:
    """Run an explicitly configured update, verify it and roll back on drift."""
    cfg = settings.providers.get(lane)
    if cfg is None:
        return {"ok": False, "lane": lane, "errors": [f"unknown lane {lane!r}"]}
    harness_cfg = _harness(cfg)
    update_cmd = _argv(harness_cfg.get("update_cmd"))
    rollback_cmd = _argv(harness_cfg.get("rollback_cmd"))
    if str(harness_cfg.get("kind", "")).lower() == "app":
        return {
            "ok": False,
            "refused": True,
            "lane": lane,
            "reason": "app updates stay with the owner",
        }
    if ((update_cmd is not None and _contains_sudo(update_cmd))
            or (rollback_cmd is not None and _contains_sudo(rollback_cmd))):
        return {"ok": False, "refused": True, "lane": lane, "reason": "sudo is refused"}
    if update_cmd is None:
        return {"ok": False, "lane": lane, "errors": ["no update_cmd is configured"]}

    store = LaneStatusStore(settings.session_root, redact_keys=settings.leaked_keys)
    previous_version = detect_version(cfg, runner)
    store.record_harness_previous(lane, previous_version)
    command_result = _invoke(runner, update_cmd)
    if command_result is None or command_result[0] != 0:
        message = "update command failed"
        if command_result is not None:
            detail = (command_result[2] or command_result[1]).strip()
            if detail:
                message += f": {detail}"
        return {
            "ok": False,
            "lane": lane,
            "previous_version": previous_version,
            "errors": [message],
        }

    verification = verify(lane, settings, runner)
    if verification["ok"]:
        return {
            "ok": True,
            "lane": lane,
            "previous_version": previous_version,
            "version": verification.get("version"),
            "verification": verification,
        }

    rollback_ok = False
    rollback_message: str | None = None
    if rollback_cmd is not None:
        if _contains_sudo(rollback_cmd):
            rollback_message = "rollback command refused because it contains sudo"
        else:
            rollback = _invoke(runner, rollback_cmd)
            rollback_ok = rollback is not None and rollback[0] == 0
            if not rollback_ok:
                detail = "" if rollback is None else (rollback[2] or rollback[1]).strip()
                rollback_message = "rollback command failed" + (f": {detail}" if detail else "")
    errors = [*verification.get("errors", [])]
    if rollback_message:
        errors.append(rollback_message)
    if not rollback_ok:
        memory = LaneState(settings.session_root)
        memory.close(
            lane,
            datetime.now().astimezone() + timedelta(hours=24),
            "harness_unverified",
            "; ".join(errors) or "harness verification failed and rollback was unavailable",
        )
    return {
        "ok": False,
        "lane": lane,
        "previous_version": previous_version,
        "rollback": "passed" if rollback_ok else "unavailable_or_failed",
        "verification": verification,
        "errors": errors,
    }
