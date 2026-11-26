#!/usr/bin/env python3
"""T4: defects found in a package's code after it merged, from commit trailers and a register.

Reads git history (trailers `Fixes-pkg` and `Defect-id`) and a markdown register table.
Writes nothing except `--out`. Standard library only; the only subprocess is git.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTER = "wiki/status/post-merge-defects.md"
HEADER = ["id", "package", "found", "by", "fixed in", "note"]
SEP = "\x1f"
REC = "\x1e"


class InputError(Exception):
    """Invalid or unreadable input: exit 2."""


def _git(repo: Path, *args: str) -> tuple[int, str]:
    try:
        cp = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, check=False,
        )
    except OSError as exc:
        raise InputError(f"cannot run git: {exc}") from exc
    return cp.returncode, cp.stdout.decode("utf-8", errors="replace")


def _date(raw: str | None, name: str) -> dt.date | None:
    if raw is None:
        return None
    try:
        return dt.date.fromisoformat(raw)
    except ValueError as exc:
        raise InputError(f"{name} is not an ISO date: {raw!r}") from exc


def _in_window(day: dt.date, since: dt.date | None, until: dt.date | None) -> bool:
    return (since is None or day >= since) and (until is None or day <= until)


def _cells(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [c.strip() for c in text.split("|")]


def read_commits(
    repo: Path, base: str, since: dt.date | None, until: dt.date | None,
) -> dict[str, set[str]]:
    fmt = (
        f"{REC}%H{SEP}%h{SEP}%at{SEP}"
        f"%(trailers:key=Fixes-pkg,valueonly){SEP}"
        f"%(trailers:key=Defect-id,valueonly)"
    )
    rc, out = _git(repo, "log", f"--format={fmt}", "--end-of-options", base)
    if rc != 0:
        raise InputError(f"git log failed for base {base!r}")
    defects: dict[str, set[str]] = {}
    for rec in out.split(REC):
        if not rec.strip():
            continue
        parts = rec.split(SEP)
        if len(parts) < 5:
            continue
        sha, _short, epoch, pkgs_raw, ids_raw = parts[0], parts[1], parts[2], parts[3], parts[4]
        pkgs = [p.strip() for p in pkgs_raw.split("\n") if p.strip()]
        if not pkgs:
            continue
        try:
            day = dt.datetime.fromtimestamp(int(epoch), dt.UTC).date()
        except ValueError:
            continue
        if not _in_window(day, since, until):
            continue
        ids = [i.strip() for i in ids_raw.split("\n") if i.strip()]
        if ids:
            did = ids[0]
        else:
            rc2, short = _git(repo, "rev-parse", "--short=7", sha)
            did = f"commit:{short.strip()}"
        defects.setdefault(did, set()).update(pkgs)
    return defects


def read_register(
    path: Path, since: dt.date | None, until: dt.date | None, warnings: list[str],
) -> dict[str, str]:
    if not path.is_file():
        warnings.append("no register file")
        return {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise InputError(f"cannot read register {path}: {exc}") from exc
    start = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("|") and _cells(line) == HEADER:
            start = i
            break
    rows: dict[str, str] = {}
    if start is None:
        warnings.append("no register table")
        return rows
    for line in lines[start + 1:]:
        if not line.lstrip().startswith("|"):
            break
        cells = _cells(line)
        if cells and all(set(c) <= set("-: ") and c for c in cells):
            continue
        if len(cells) != len(HEADER):
            warnings.append(f"register row skipped (cell count {len(cells)}): {line.strip()}")
            continue
        rid, pkg, found = cells[0], cells[1], cells[2]
        try:
            day = dt.date.fromisoformat(found)
        except ValueError:
            warnings.append(f"register row skipped (bad date {found!r}): {line.strip()}")
            continue
        if not rid or not pkg:
            warnings.append(f"register row skipped (empty id or package): {line.strip()}")
            continue
        if not _in_window(day, since, until):
            continue
        rows.setdefault(rid, pkg)
    return rows


def build(args: argparse.Namespace) -> dict[str, Any]:
    repo = Path(args.repo)
    if not repo.is_dir():
        raise InputError(f"repo is not a directory: {repo}")
    rc, _ = _git(repo, "rev-parse", "--git-dir")
    if rc != 0:
        raise InputError(f"not a git repo: {repo}")
    rc, _ = _git(repo, "rev-parse", "--verify", "--quiet", f"{args.base}^{{commit}}")
    if rc != 0 or args.base.startswith("-"):
        raise InputError(f"unknown base branch: {args.base!r}")
    since = _date(args.since, "--since")
    until = _date(args.until, "--until")
    register = Path(args.register) if args.register else repo / DEFAULT_REGISTER
    if not register.is_absolute() and args.register:
        register = Path.cwd() / register
    warnings: list[str] = []
    commits = read_commits(repo, args.base, since, until)
    reg = read_register(register, since, until, warnings)

    only_commits: set[tuple[str, str]] = set()
    only_register: set[tuple[str, str]] = set()
    both: set[tuple[str, str]] = set()
    for did, pkgs in commits.items():
        if did in reg:
            continue
        for pkg in pkgs:
            only_commits.add((pkg, did))
    for did, pkg in reg.items():
        (both if did in commits else only_register).add((pkg, did))
    packages: dict[str, set[str]] = {}
    for pkg, did in only_commits | only_register | both:
        packages.setdefault(pkg, set()).add(did)
    return {
        "base": args.base,
        "window": {"since": args.since, "until": args.until},
        "packages": {
            p: {"defects": len(ids), "ids": sorted(ids)} for p, ids in sorted(packages.items())
        },
        "total": len(only_commits) + len(only_register) + len(both),
        "from_commits": len(only_commits),
        "from_register": len(only_register),
        "both": len(both),
        "warnings": warnings,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=str(REPO_ROOT))
    ap.add_argument("--base", default="fuse")
    ap.add_argument("--register", default=None)
    ap.add_argument("--since", default=None)
    ap.add_argument("--until", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    try:
        report = build(args)
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        try:
            Path(args.out).write_text(text, encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot write {args.out}: {exc}", file=sys.stderr)
            return 2
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
