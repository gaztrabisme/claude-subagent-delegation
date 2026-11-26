#!/usr/bin/env python3
"""T5: how many harm-3 rules of the audit have a merged mechanism.

Reads the audit table from the playbook, a mechanism map (JSON) and git history of the
base branch. Writes nothing except `--out`. Standard library only; the only subprocess
is git.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AUDIT = "wiki/pilot-playbook-2026-11.md"
DEFAULT_MAP = "wiki/status/t5-mechanisms.json"
HEADER = ["Rank", "Rule (source)", "Breaks", "Evidence", "Harm"]


class InputError(Exception):
    """Invalid or unreadable input: exit 2."""


def _git(repo: Path, *args: str) -> int:
    try:
        cp = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    except OSError as exc:
        raise InputError(f"cannot run git: {exc}") from exc
    return cp.returncode


def _cells(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [c.strip() for c in text.split("|")]


def read_audit(path: Path, warnings: list[str]) -> list[tuple[str, str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise InputError(f"cannot read audit {path}: {exc}") from exc
    for i, line in enumerate(lines):
        if line.lstrip().startswith("|") and _cells(line)[: len(HEADER)] == HEADER:
            rows: list[tuple[str, str]] = []
            for row in lines[i + 1:]:
                if not row.lstrip().startswith("|"):
                    break
                cells = _cells(row)
                if len(cells) < len(HEADER) or all(set(c) <= set("-: ") for c in cells):
                    continue
                if cells[4] == "3":
                    rows.append((cells[0], cells[1]))
            return rows
    warnings.append("no audit table")
    return []


def read_map(path: Path, warnings: list[str]) -> dict[str, list[Any]]:
    if not path.is_file():
        warnings.append("no mechanism map")
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise InputError(f"cannot read mechanism map {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise InputError("mechanism map must be a JSON object")
    return data


def check(repo: Path, base: str, mech: dict[str, Any]) -> dict[str, Any]:
    failed: list[str] = []
    merge = str(mech.get("merge", ""))
    if merge.startswith("-") or not merge or _git(
        repo, "rev-parse", "--verify", "--quiet", f"{merge}^{{commit}}",
    ) != 0:
        failed.append(f"merge {merge} unknown")
    elif _git(repo, "merge-base", "--is-ancestor", merge, base) != 0:
        failed.append(f"merge {merge} not an ancestor of {base}")
    paths = mech.get("paths", [])
    for p in paths if isinstance(paths, list) else []:
        p = str(p)
        if not p or _git(repo, "cat-file", "-e", f"{base}:{p}") != 0:
            failed.append(f"missing path {p}")
    return {
        "mechanism": mech.get("mechanism"),
        "covers": mech.get("covers"),
        "merged": not failed,
        "failed": failed,
    }


def build(args: argparse.Namespace) -> dict[str, Any]:
    repo = Path(args.repo)
    if not repo.is_dir():
        raise InputError(f"repo is not a directory: {repo}")
    if _git(repo, "rev-parse", "--git-dir") != 0:
        raise InputError(f"not a git repo: {repo}")
    if args.base.startswith("-") or _git(
        repo, "rev-parse", "--verify", "--quiet", f"{args.base}^{{commit}}",
    ) != 0:
        raise InputError(f"unknown base branch: {args.base!r}")
    warnings: list[str] = []
    audit = Path(args.audit) if args.audit else repo / DEFAULT_AUDIT
    mapfile = Path(args.map) if args.map else repo / DEFAULT_MAP
    harm3 = read_audit(audit, warnings)
    mapping = read_map(mapfile, warnings)
    ranks = {r for r, _ in harm3}
    for key in mapping:
        if key not in ranks:
            warnings.append(f"map entry {key} is not a harm-3 row; ignored")
    rows = []
    counts = {"covered": 0, "part": 0, "none": 0}
    for rank, rule in harm3:
        entries = mapping.get(rank, [])
        if not isinstance(entries, list):
            raise InputError(f"map entry {rank} must be a list")
        mechs = []
        for e in entries:
            if not isinstance(e, dict):
                raise InputError(f"map entry {rank} holds a non-object mechanism")
            mechs.append(check(repo, args.base, e))
        merged = [m for m in mechs if m["merged"]]
        if any(m["covers"] == "full" for m in merged):
            status = "covered"
        elif merged:
            status = "part"
        else:
            status = "none"
        counts[status] += 1
        rows.append({"rank": rank, "rule": rule, "status": status, "mechanisms": mechs})
    return {
        "base": args.base,
        "harm3_rows": len(harm3),
        **counts,
        "rows": rows,
        "warnings": warnings,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=str(REPO_ROOT))
    ap.add_argument("--base", default="fuse")
    ap.add_argument("--audit", default=None)
    ap.add_argument("--map", default=None)
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
