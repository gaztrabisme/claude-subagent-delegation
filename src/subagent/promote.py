"""Promote one exact commit to a pinned, non-editable install.

The MCP server must serve code that was reviewed, not whatever the working
tree happens to hold. `promote` exports the named commit into
``<session_root>/installs/<sha>/`` with a plain (never editable) install, then
records the result in ``served.json`` so the workspace check and `list` can tell
which commit is running.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from subprocess import TimeoutExpired
from subprocess import run as _subprocess_run
from typing import Any
from urllib.parse import urlsplit

from .config import Settings
from .lane_status import LaneStatusStore

SERVED_FILE = "served.json"


class PromoteError(RuntimeError):
    """A promotion that cannot proceed; the CLI maps it to an exit code."""

    code = 1


class UnknownCommit(PromoteError):
    """The requested revision does not resolve in the repository."""

    code = 2


def _resolve(repo: Path, commit: str, runner: Any) -> str:
    result = runner(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
         "--end-of-options", f"{commit}^{{commit}}"],
        capture_output=True,
        text=True,
    )
    sha = (result.stdout or "").strip()
    if result.returncode != 0 or not sha:
        raise UnknownCommit(f"unknown commit {commit!r} in {repo}")
    return sha


def live_lanes(settings: Settings) -> list[str]:
    """Configured lanes that currently hold a live run lease."""
    store = LaneStatusStore(settings.session_root)
    return [name for name in settings.providers if store.active_count(name) > 0]


def missing_routes(settings: Settings) -> list[str]:
    """Configured non-local providers with no network route, in config order.

    A route is a non-empty ``network_allow`` or a ``base_url`` with a hostname,
    the same derivation as ``Agent._start_egress_proxy`` and the doctor's
    ``network_allow`` row.
    """
    missing = []
    for name, cfg in settings.providers.items():
        if cfg.local:
            continue
        if cfg.network_allow:
            continue
        if urlsplit(cfg.base_url or "").hostname:
            continue
        missing.append(name)
    return missing


def _checked(result: Any, action: str) -> None:
    if result.returncode != 0:
        detail = (getattr(result, "stderr", "") or "").strip()
        raise PromoteError(f"{action} failed ({result.returncode}): {detail}")


def _write_served(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(temporary, path)


def _install(
    repo: Path, sha: str, install_root: Path, runner: Any
) -> None:
    """Export `sha` and install it non-editably into a venv under `install_root`."""
    with tempfile.TemporaryDirectory(prefix="subagent-promote-") as temporary:
        source = Path(temporary) / "src"
        source.mkdir()
        archive = Path(temporary) / f"{sha}.tar"
        _checked(
            runner(
                ["git", "-C", str(repo), "archive", "--format=tar",
                 "--output", str(archive), sha],
                capture_output=True,
                text=True,
            ),
            "export",
        )
        _checked(
            runner(["tar", "-xf", str(archive), "-C", str(source)],
                   capture_output=True, text=True),
            "extract",
        )
        uv = shutil.which("uv") or "uv"
        venv = install_root / "venv"
        _checked(
            runner(
                # --clear makes a retry after a partial install start clean.
                [uv, "venv", "--clear", "--python", sys.executable, str(venv)],
                capture_output=True,
                text=True,
            ),
            "venv",
        )
        _checked(
            runner(
                [uv, "pip", "install", "--python", str(venv / "bin" / "python"),
                 str(source)],
                capture_output=True,
                text=True,
            ),
            "install",
        )


PROBE_HINT = (
    "; fix the lane, or pass --allow-failed-lane NAME or --skip-probe NAME"
)


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _probe_lane(name: str, runner: Any) -> tuple[bool, str]:
    """Run one doctor prompt for `name`; return (passed, failure detail)."""
    try:
        result = runner(
            [sys.executable, "-m", "subagent", "doctor",
             "--provider", name, "--prompt"],
            capture_output=True,
            text=True,
            timeout=400,
        )
    except TimeoutExpired as exc:
        return False, _first_line(str(exc)) or "timeout"
    if result.returncode == 0:
        return True, ""
    detail = _first_line(getattr(result, "stderr", "") or "")
    if not detail:
        detail = _first_line(getattr(result, "stdout", "") or "")
    if not detail:
        detail = f"exit {result.returncode}"
    return False, detail


def _probe_lanes(
    settings: Settings,
    runner: Any,
    skip_probe: Iterable[str],
    allow_failed: Iterable[str],
) -> None:
    """Probe each non-local lane once; refuse the promotion if one fails."""
    skip = set(skip_probe)
    allowed = set(allow_failed)
    failures = []
    for name, cfg in settings.providers.items():
        if cfg.local:
            continue
        if name in skip:
            print(f"warning: lane probe skipped for {name}")
            continue
        passed, detail = _probe_lane(name, runner)
        if passed:
            continue
        if name in allowed:
            print(f"warning: lane probe failed for {name}, allowed")
            continue
        failures.append(f"lane probe failed: {name} ({detail})")
    if failures:
        raise PromoteError("; ".join(failures) + PROBE_HINT)


def promote(
    repo: Path,
    commit: str,
    settings: Settings,
    *,
    force: bool = False,
    require_routes: bool = False,
    probe: bool = False,
    skip_probe: Iterable[str] = (),
    allow_failed: Iterable[str] = (),
    runner: Any = _subprocess_run,
) -> dict[str, Any]:
    """Install `commit` from `repo` and record it in ``served.json``."""
    repo = Path(repo).resolve()
    sha = _resolve(repo, commit, runner)
    skip_probe = tuple(skip_probe)
    allow_failed = tuple(allow_failed)
    for name in (*skip_probe, *allow_failed):
        if name not in settings.providers:
            raise PromoteError(f"unknown provider {name}")
    if require_routes:
        missing = missing_routes(settings)
        if missing:
            raise PromoteError(
                "no network route for " + ", ".join(missing)
                + "; add network_allow or set local = true"
            )
    install_root = Path(settings.session_root) / "installs" / sha
    existing = served(settings)
    if (
        existing is not None
        and existing.get("commit") == sha
        and existing.get("install_path") == str(install_root)
        and (install_root / "venv").is_dir()
    ):
        # Already installed as served: never touch a venv that may be in use.
        return existing
    live = live_lanes(settings)
    if live and not force:
        raise PromoteError(
            "live runs on " + ", ".join(live) + "; wait for them or pass --force"
        )
    if live:
        print(f"warning: live runs on {', '.join(live)}; promoting anyway")
    install_root.mkdir(parents=True, exist_ok=True)
    _install(repo, sha, install_root, runner)
    if probe:
        _probe_lanes(settings, runner, skip_probe, allow_failed)
    payload = {
        "commit": sha,
        "install_path": str(install_root),
        "python": str(install_root / "venv" / "bin" / "python"),
        "promoted_at": datetime.now(UTC).isoformat(),
    }
    _write_served(Path(settings.session_root) / SERVED_FILE, payload)
    return payload


def served(settings: Settings) -> dict[str, Any] | None:
    """The commit currently served, or None when nothing was promoted."""
    try:
        raw = (Path(settings.session_root) / SERVED_FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None
