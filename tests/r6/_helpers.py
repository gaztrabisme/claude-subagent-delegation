"""Shared offline helpers for the R6 acceptance tests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

TESTS_DIR = Path(__file__).resolve().parents[1]
FAKE_CLAUDE = TESTS_DIR / "fakes" / "fake_claude.py"
SCENARIOS = TESTS_DIR / "fakes" / "scenarios"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "R6 Test",
    "GIT_AUTHOR_EMAIL": "r6@example.test",
    "GIT_COMMITTER_NAME": "R6 Test",
    "GIT_COMMITTER_EMAIL": "r6@example.test",
}


def git(cwd: Path, *args: str) -> str:
    """Run a git command offline with deterministic author/committer env."""
    env = {**os.environ, **GIT_ENV}
    proc = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def init_git_repo(root: Path, files: dict[str, str] | None = None) -> str:
    """Initialize a git repo in `root`, write optional files, commit, and return HEAD sha."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init")
    for rel, content in (files or {"README.md": "init\n"}).items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "initial")
    return git(root, "rev-parse", "HEAD")


def commit_files(root: Path, files: dict[str, str], message: str = "update") -> str:
    """Write `files` in `root`, stage, commit, and return the new HEAD sha."""
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", message)
    return git(root, "rev-parse", "HEAD")


def read_trace(path: Path) -> list[dict[str, Any]]:
    """Read every JSON object from a JSONL trace file."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def fake_claude_provider(*, model: str = "glm-test", **extra: Any) -> dict[str, Any]:
    """A Claude-driver provider dict backed by `tests/fakes/fake_claude.py`."""
    return {
        "driver": "claude",
        "base_url": "https://api.z.ai/api/anthropic",
        "model": model,
        "api_key_env": "GLM_API_KEY",
        "vendor": "zai",
        "binary": str(FAKE_CLAUDE),
        **extra,
    }


@contextmanager
def short_socket_path():
    """Yield a short Unix socket path under /tmp (< 104 bytes on macOS) and clean it up."""
    sock_dir = Path(tempfile.mkdtemp(dir="/tmp", prefix="r6-"))
    os.chmod(sock_dir, 0o700)
    try:
        yield sock_dir / "a.sock"
    finally:
        shutil.rmtree(sock_dir, ignore_errors=True)
