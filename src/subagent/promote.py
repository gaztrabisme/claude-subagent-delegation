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
from datetime import UTC, datetime
from pathlib import Path
from subprocess import run as _subprocess_run
from typing import Any

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
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"],
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
                [uv, "venv", "--python", sys.executable, str(venv)],
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


def promote(
    repo: Path,
    commit: str,
    settings: Settings,
    *,
    force: bool = False,
    runner: Any = _subprocess_run,
) -> dict[str, Any]:
    """Install `commit` from `repo` and record it in ``served.json``."""
    repo = Path(repo).resolve()
    sha = _resolve(repo, commit, runner)
    live = live_lanes(settings)
    if live and not force:
        raise PromoteError(
            "live runs on " + ", ".join(live) + "; wait for them or pass --force"
        )
    if live:
        print(f"warning: live runs on {', '.join(live)}; promoting anyway")
    install_root = Path(settings.session_root) / "installs" / sha
    install_root.mkdir(parents=True, exist_ok=True)
    _install(repo, sha, install_root, runner)
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
