"""Shared helpers for the T12 acceptance tests: tiny git repos and a subprocess runner."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "overhead_meter.py"

NOW = "2026-11-20T00:00:00Z"
START = "2026-11-13T00:00:00Z"   # NOW minus 7 days
PREV_START = "2026-11-06T00:00:00Z"

# commit dates: inside the current window, inside the previous window
CUR = "2026-11-15T12:00:00Z"
PREV = "2026-11-08T12:00:00Z"


def _env(home: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "LC_ALL": "C",
    }
    env.update(extra or {})
    return env


def git(repo: Path, *args: str, date: str | None = None, author_date: str | None = None) -> str:
    extra = {}
    if date:
        extra["GIT_COMMITTER_DATE"] = date
        extra["GIT_AUTHOR_DATE"] = author_date or date
    out = subprocess.run(
        ["git", "-C", str(repo), *args], env=_env(repo.parent, extra),
        check=True, capture_output=True, text=True,
    )
    return out.stdout


def make_repo(base: Path, name: str, branch: str = "main") -> Path:
    repo = base / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", branch)
    git(repo, "config", "user.name", "T")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "commit.gpgsign", "false")
    return repo


def commit(repo: Path, files: dict[str, str | None] | None, msg: str, date: str,
           author_date: str | None = None, allow_empty: bool = False) -> None:
    for rel, text in (files or {}).items():
        p = repo / rel
        if text is None:
            p.unlink()
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    args = ["commit", "-q", "-m", msg]
    if allow_empty:
        args.append("--allow-empty")
    git(repo, *args, date=date, author_date=author_date)


_pad_n = [0]


def pad(repo: Path, date: str, n: int = 1, msg: str = "chore pad") -> None:
    """n non-records commits (touch src/pad.txt) at the given date."""
    for _ in range(n):
        _pad_n[0] += 1
        commit(repo, {"src/pad.txt": f"pad {_pad_n[0]}\n"}, msg, date)


def merge_branch(repo: Path, name: str, files: dict[str, str | None], date: str,
                 msg: str = "side work", base: str = "main") -> None:
    """Commit on a side branch, then merge it into `base` with --no-ff at `date`."""
    git(repo, "checkout", "-q", "-b", name)
    commit(repo, files, msg, date)
    git(repo, "checkout", "-q", base)
    git(repo, "merge", "-q", "--no-ff", "-m", f"merge {name}", name, date=date)


def repos_file(base: Path, repos: list[dict[str, Any]], rulebook: list[str] | None = None,
               name: str = "repos.json") -> Path:
    data: dict[str, Any] = {"repos": repos}
    if rulebook is not None:
        data["rulebook"] = rulebook
    path = base / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def entry(name: str, path: Path | str, cls: str = "machinery", branch: str = "main",
          records: list[str] | None = None) -> dict[str, Any]:
    e: dict[str, Any] = {"name": name, "path": str(path), "branch": branch, "class": cls}
    if records is not None:
        e["records"] = records
    return e


def run(base: Path, *args: str, repos: Path | None = None, now: str | None = NOW,
        home: Path | None = None) -> subprocess.CompletedProcess:
    assert SCRIPT.exists(), f"{SCRIPT} does not exist yet"
    cmd = [sys.executable, "-I", str(SCRIPT)]
    if repos is not None:
        cmd += ["--repos", str(repos)]
    if now is not None:
        cmd += ["--now", now]
    cmd += list(args)
    return subprocess.run(cmd, cwd=base, env=_env(home or base), capture_output=True, text=True)


def out(base: Path, *args: str, repos: Path | None = None, now: str | None = NOW,
        home: Path | None = None) -> dict[str, Any]:
    cp = run(base, *args, repos=repos, now=now, home=home)
    assert cp.returncode == 0, cp.stderr
    return json.loads(cp.stdout)


def meter_file(base: Path, roles: dict[str, Any], start: str = START, end: str = NOW,
               name: str = "meter.json") -> Path:
    data = {"window": {"start": start, "end": end}, "roles": roles}
    path = base / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def role(i: Any, o: Any) -> dict[str, Any]:
    return {"tokens": {"input": i, "output": o}}
