"""Shared helpers for the G5 acceptance tests: throwaway git repos and a script runner."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


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


def git(repo: Path, *args: str, date: str | None = None) -> str:
    extra = {}
    if date:
        extra["GIT_COMMITTER_DATE"] = date
        extra["GIT_AUTHOR_DATE"] = date
    out = subprocess.run(
        ["git", "-C", str(repo), *args], env=_env(repo.parent, extra),
        check=True, capture_output=True, text=True,
    )
    return out.stdout


def make_repo(base: Path, name: str = "repo", branch: str = "fuse") -> Path:
    repo = base / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", branch)
    git(repo, "config", "user.name", "T")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "commit.gpgsign", "false")
    return repo


_n = [0]


def commit(repo: Path, subject: str = "work", trailers: list[str] | None = None,
           date: str = "2026-11-10T12:00:00Z", files: dict[str, str] | None = None) -> str:
    """Make a commit (touching a unique file unless files given); trailers are 'Key: value' lines."""
    if files is None:
        _n[0] += 1
        files = {f"f{_n[0]}.txt": str(_n[0])}
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    msg = subject
    if trailers:
        msg += "\n\nBody text.\n\n" + "\n".join(trailers)
    git(repo, "commit", "-q", "-m", msg, date=date)
    return git(repo, "rev-parse", "HEAD").strip()


def short(repo: Path, sha: str) -> str:
    return git(repo, "rev-parse", "--short=7", sha).strip()


def snapshot(*roots: Path) -> dict[str, tuple[int, int]]:
    """Every file under the roots (outside .git) with size and mtime_ns."""
    snap: dict[str, tuple[int, int]] = {}
    for root in roots:
        for p in root.rglob("*"):
            if p.is_file() and ".git" not in p.relative_to(root).parts:
                st = p.stat()
                snap[str(p)] = (st.st_size, st.st_mtime_ns)
    return snap


def run(script: str, args: list[str | Path], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    assert (SCRIPTS / script).is_file(), f"scripts/{script} is not built"
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPTS / script), *map(str, args)],
        cwd=str(cwd) if cwd else None, env=_env(cwd or Path("/")), capture_output=True, text=True,
    )


def run_json(script: str, args: list[str | Path], cwd: Path | None = None) -> dict[str, Any]:
    r = run(script, args, cwd)
    assert r.returncode == 0, f"exit {r.returncode}\nstderr: {r.stderr}"
    return json.loads(r.stdout)


def assert_out_contract(script: str, args: list[str | Path], out: Path, watch: list[Path]) -> None:
    """--out file and stdout are identical, indent=2, trailing newline; nothing else is written."""
    before = snapshot(*watch)
    r = run(script, [*args, "--out", out], cwd=watch[0])
    assert r.returncode == 0, r.stderr
    after = snapshot(*watch)
    created = {k for k in after if k not in before}
    assert created == {str(out)}
    assert {k: v for k, v in after.items() if k in before} == before
    text = out.read_text(encoding="utf-8")
    assert text == r.stdout
    assert text.endswith("}\n") and not text.endswith("\n\n")
    data = json.loads(text)
    assert text == json.dumps(data, indent=2, ensure_ascii=False) + "\n"
