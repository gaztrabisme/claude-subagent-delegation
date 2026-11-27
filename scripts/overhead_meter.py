#!/usr/bin/env python3
"""T12 overhead meter and trip-wire: one JSON report on how much work is machinery.

Counts non-merge commits per repo and window, records-only share, machinery versus
outside share, rulebook size, coordinator tokens (from a coord_meter.py report), owner
questions, and a trip-wire with a cut list. Reads git history only; writes nothing
except `--out`. Standard library only; the only subprocess is git.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPOS = REPO_ROOT / "wiki" / "status" / "t12-repos.json"
REGISTERS = "wiki/status/registers.md"
DECISIONS = "wiki/decisions.md"
METHOD_PREFIX = "## Method:"
owner_RE = re.compile(r"the owner, \d{4}-\d\d-\d\d.*[\"“]")
NO_READING = "no reading: no outside project listed"
DECISION_RE = re.compile(r"^## (S\d+)(?!\d)")
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


class InputError(Exception):
    """Invalid or unreadable input: exit 2."""


def _parse_ts(raw: Any) -> dt.datetime | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        when = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    return when


def _iso_z(when: dt.datetime) -> str:
    return when.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pct(part: int, whole: int) -> float | None:
    return round(100 * part / whole, 1) if whole else None


def _git(path: str, *args: str) -> tuple[int, str]:
    try:
        cp = subprocess.run(
            ["git", "-C", path, "-c", "core.quotepath=off", *args],
            capture_output=True, check=False,
        )
    except OSError:
        return 127, ""
    return cp.returncode, cp.stdout.decode("utf-8", errors="replace")


class Repo:
    def __init__(self, spec: dict[str, Any], base: Path) -> None:
        self.name: str = spec["name"]
        self.cls: str = spec["class"]
        self.branch: str = spec["branch"]
        records = spec.get("records", ["wiki/"])
        self.records: list[str] = [r for r in records if isinstance(r, str)]
        raw = Path(os.path.expanduser(spec["path"]))
        self.path = str(raw if raw.is_absolute() else base / raw)
        self.tip: str | None = None
        self.problem: str | None = None
        self._open()

    def _open(self) -> None:
        if not os.path.isdir(self.path):
            self.problem = f"repo {self.name}: path is not a directory: {self.path}"
            return
        rc, top = _git(self.path, "rev-parse", "--show-toplevel")
        if rc != 0 or os.path.realpath(top.strip()) != os.path.realpath(self.path):
            self.problem = f"repo {self.name}: not a git repo: {self.path}"
            return
        rc, sha = _git(self.path, "rev-parse", "--verify", "--quiet", f"{self.branch}^{{commit}}")
        if rc != 0 or not sha.strip():
            self.problem = f"repo {self.name}: branch not found: {self.branch}"
            return
        self.tip = sha.strip()

    def commits(self, start: dt.datetime, end: dt.datetime, *extra: str) -> list[dict[str, Any]]:
        """Commits reachable from the branch with committer date in [start, end)."""
        if self.tip is None:
            return []
        rc, text = _git(self.path, "log", *extra, "--format=%x1e%H%x1f%cI%x1f%B", self.tip, "--")
        if rc != 0:
            return []
        found = []
        for rec in text.split("\x1e"):
            if not rec.strip():
                continue
            sha, _, rest = rec.partition("\x1f")
            iso, _, msg = rest.partition("\x1f")
            when = _parse_ts(iso.strip())
            if when is not None and start <= when < end:
                found.append({"sha": sha.strip(), "when": when, "msg": msg})
        return found

    def changed_paths(self, sha: str) -> list[str]:
        _, text = _git(self.path, "diff-tree", "--no-commit-id", "--name-only", "-r",
                       "--root", "--no-renames", "-z", sha)
        return [p for p in text.split("\0") if p]

    def is_records_only(self, paths: list[str]) -> bool:
        return bool(paths) and all(any(p.startswith(r) for r in self.records) for p in paths)

    def diff(self, sha: str, *paths: str) -> str:
        args = ["diff-tree", "-p", "-U0", "--no-commit-id", "--root", "--no-renames", sha]
        if paths:
            args += ["--", *paths]
        return _git(self.path, *args)[1]

    def show(self, rev: str, path: str) -> str | None:
        rc, text = _git(self.path, "show", f"{rev}:{path}")
        return text if rc == 0 else None


def _load_repos(path: Path) -> tuple[list[Repo], list[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InputError(f"cannot read repos file {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("repos"), list):
        raise InputError("repos file must be an object with a 'repos' list")
    base = path.resolve().parent
    repos: list[Repo] = []
    seen: set[tuple[str, str]] = set()
    for spec in data["repos"]:
        if not isinstance(spec, dict):
            raise InputError("each repo must be an object")
        for key in ("name", "path", "branch", "class"):
            if not isinstance(spec.get(key), str) or not spec[key]:
                raise InputError(f"repo is missing string field '{key}'")
        if spec["class"] not in ("machinery", "outside"):
            raise InputError(f"repo {spec['name']}: class must be machinery or outside")
        if "records" in spec and not (
            isinstance(spec["records"], list) and all(isinstance(r, str) for r in spec["records"])
        ):
            raise InputError(f"repo {spec['name']}: records must be a list of strings")
        repo = Repo(spec, base)
        ident = (os.path.realpath(repo.path), repo.branch)
        if ident in seen or any(
            r.name == repo.name and os.path.realpath(r.path) == ident[0] for r in repos
        ):
            continue
        seen.add(ident)
        repos.append(repo)
    book = data.get("rulebook", [])
    if not isinstance(book, list) or not all(isinstance(b, str) for b in book):
        raise InputError("rulebook must be a list of strings")
    return repos, book


def _resolve(raw: str, base: Path) -> Path:
    p = Path(os.path.expanduser(raw))
    return p if p.is_absolute() else base / p


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _rulebook(
    book: list[str], base: Path, warnings: list[str]
) -> tuple[dict[str, Any], dict[str, str]]:
    files, words, methods = [], 0, 0
    texts: dict[str, str] = {}
    for raw in book:
        text = _read_text(_resolve(raw, base))
        if text is None:
            warnings.append(f"rulebook file missing or unreadable: {raw}")
            files.append({"path": raw, "words": None, "methods": None})
            continue
        texts[raw] = text
        w = len(text.split())
        m = sum(1 for ln in text.split("\n") if ln.startswith(METHOD_PREFIX))
        words += w
        methods += m
        files.append({"path": raw, "words": w, "methods": m})
    return {"words": words, "methods": methods, "files": files}, texts


def _coordination(meter: str | None, window: tuple[dt.datetime, dt.datetime],
                  merged: int, warnings: list[str]) -> dict[str, Any]:
    res: dict[str, Any] = {
        "meter_window": None, "coordinator_io_tokens": None, "claude_io_tokens": None,
        "coordinator_io_share": None, "merged_changes": merged,
        "coordinator_io_tokens_per_merged_change": None,
    }
    if meter is None:
        warnings.append("no --meter given: coordinator tokens unavailable")
        return res
    try:
        data = json.loads(Path(os.path.expanduser(meter)).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        warnings.append(f"meter unreadable: {exc}")
        return res
    if not isinstance(data, dict):
        warnings.append("meter is not a JSON object")
        return res
    win = data.get("window")
    if isinstance(win, dict):
        s, e = _parse_ts(win.get("start")), _parse_ts(win.get("end"))
        if s is not None and e is not None:
            res["meter_window"] = {"start": _iso_z(s), "end": _iso_z(e)}
            if (s, e) != window:
                warnings.append("meter window differs from the T12 window")
    roles = data.get("roles")
    if not isinstance(roles, dict) or not isinstance(roles.get("coordinator"), dict):
        warnings.append("meter has no coordinator role")
        return res
    totals: dict[str, int | float] = {}
    for rname, rec in roles.items():
        toks = rec.get("tokens") if isinstance(rec, dict) else None
        vals = [toks.get(k) if isinstance(toks, dict) else None for k in ("input", "output")]
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
            warnings.append(f"meter role {rname}: tokens unusable")
            return res
        totals[rname] = vals[0] + vals[1]
    coord = totals["coordinator"]
    claude = sum(totals.values())
    res["coordinator_io_tokens"] = coord
    res["claude_io_tokens"] = claude
    res["coordinator_io_share"] = round(100 * coord / claude, 1) if claude else None
    if merged:
        res["coordinator_io_tokens_per_merged_change"] = int(round(coord / merged))
    return res


def _section(lines: list[str], start: int) -> list[str]:
    """Lines from index `start` up to the next '## ' heading (exclusive)."""
    end = start + 1
    while end < len(lines) and not lines[end].startswith("## "):
        end += 1
    return lines[start:end]


def _waiting_bounds(lines: list[str]) -> tuple[int, int] | None:
    """Index range [start, end) of the '## Waiting on the owner' section."""
    for i, ln in enumerate(lines):
        if ln.startswith("## Waiting on the owner"):
            return i, i + len(_section(lines, i))
    return None


def _waiting_rows(text: str | None) -> list[str]:
    """Table data rows of the '## Waiting on the owner' section."""
    if text is None:
        return []
    lines = text.split("\n")
    bounds = _waiting_bounds(lines)
    if bounds is None:
        return []
    return [row for row in lines[bounds[0] + 1:bounds[1]] if _is_table_row(row)]


def _first_cell(row: str) -> str:
    return row.strip().strip("|").split("|", 1)[0].strip()


def _is_table_row(row: str) -> bool:
    """A data row: starts with '|', not the header, not a separator row."""
    if not row.startswith("|") or not row.strip("|-: \t"):
        return False
    return _first_cell(row).lower() != "id"


def _added_lines(diff: str) -> list[tuple[str, int | None, str]]:
    """(file, new-side line number, text) for each added diff line."""
    out = []
    cur, n = "", 0
    in_header = False
    for ln in diff.split("\n"):
        if in_header:
            if ln.startswith("+++ "):
                cur = ln[6:] if ln.startswith("+++ b/") else ""
            elif ln.startswith("@@"):
                in_header = False
                m = HUNK_RE.match(ln)
                n = int(m.group(1)) if m else 0
            elif ln.startswith("diff --git "):
                cur, n = "", 0
            continue
        if ln.startswith("diff --git "):
            in_header = True
            cur, n = "", 0
        elif ln.startswith("@@"):
            m = HUNK_RE.match(ln)
            n = int(m.group(1)) if m else 0
        elif ln.startswith("+"):
            out.append((cur, n, ln[1:]))
            n += 1
    return out


def _owner_questions(repos: list[Repo], start: dt.datetime, end: dt.datetime) -> dict[str, int]:
    rows_added = answers = 0
    for repo in repos:
        for c in repo.commits(start, end, "--no-merges"):
            sha = c["sha"]
            paths = repo.changed_paths(sha)
            if REGISTERS in paths:
                new = repo.show(sha, REGISTERS)
                old = repo.show(f"{sha}^", REGISTERS)
                if new is not None:
                    lines = new.split("\n")
                    bounds = _waiting_bounds(lines)
                    if bounds is not None:
                        old_ids = {_first_cell(r) for r in _waiting_rows(old)}
                        sec_start, sec_end = bounds
                        for _, num, text in _added_lines(repo.diff(sha, REGISTERS)):
                            if not num or not sec_start <= num - 1 < sec_end:
                                continue
                            if _is_table_row(text) and _first_cell(text) not in old_ids:
                                rows_added += 1
            if DECISIONS in paths:
                new = repo.show(sha, DECISIONS)
                if new is None:
                    continue
                lines = new.split("\n")
                old = repo.show(f"{sha}^", DECISIONS)
                old_ids = {m.group(1) for ln in (old or "").split("\n")
                           if (m := DECISION_RE.match(ln))}
                for _, num, text in _added_lines(repo.diff(sha, DECISIONS)):
                    m = DECISION_RE.match(text)
                    if m is None or m.group(1) in old_ids or not num or num > len(lines):
                        continue
                    if any(owner_RE.search(ln) for ln in _section(lines, num - 1)):
                        answers += 1
    return {"waiting_rows_added": rows_added, "owner_answers": answers,
            "total": rows_added + answers}


def _tripwire(threshold: float, cur: float | None, prev: float | None,
              repos: list[Repo], cur_commits: dict[str, list[dict[str, Any]]],
              records_only: dict[str, set[str]], book: list[str],
              texts: dict[str, str], reason: str | None = None,
              unreadable: bool = False) -> dict[str, Any]:
    tripped: bool | None = (
        None if reason or unreadable
        else cur is not None and prev is not None and cur > threshold and prev > threshold)
    cut: list[dict[str, Any]] = []
    if tripped:
        cands: list[dict[str, Any]] = []
        for raw in book:
            lines = texts.get(raw, "").split("\n")
            for i, ln in enumerate(lines):
                if ln.startswith(METHOD_PREFIX):
                    sec = _section(lines, i)
                    cands.append({"kind": "method", "name": ln[len(METHOD_PREFIX):].strip(),
                                  "file": raw, "cost": len(" ".join(sec).split()), "key": None})
        for repo in repos:
            costs: dict[str, int] = {}
            for c in cur_commits[repo.name]:
                if c["sha"] not in records_only[repo.name]:
                    continue
                for fname, _, text in _added_lines(repo.diff(c["sha"])):
                    if fname:
                        costs[fname] = costs.get(fname, 0) + len(text.split())
                for p in repo.changed_paths(c["sha"]):
                    costs.setdefault(p, 0)
            for fname, cost in costs.items():
                cands.append({"kind": "record", "name": f"{repo.name}:{fname}", "file": fname,
                              "cost": cost, "key": None})
        messages = [c["msg"].lower() for repo in repos for c in cur_commits[repo.name]
                    if c["sha"] not in records_only[repo.name]]
        for cand in cands:
            key = (cand["name"] if cand["kind"] == "method"
                   else os.path.splitext(os.path.basename(cand["file"]))[0]).lower()
            cand["effect"] = sum(1 for m in messages if key in m)
        cands.sort(key=lambda x: (x["effect"], -x["cost"], x["name"]))
        cut = [{k: x[k] for k in ("kind", "name", "file", "cost", "effect")} for x in cands[:3]]
    return {"threshold": threshold, "current": cur, "previous": prev, "tripped": tripped,
            "cut_list": cut, "reason": reason}


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    end = _parse_ts(args.now) if args.now else dt.datetime.now(dt.UTC)
    if end is None:
        raise InputError(f"invalid --now: {args.now}")
    start = _parse_ts(args.start) if args.start else end - dt.timedelta(days=args.days)
    if start is None:
        raise InputError(f"invalid --start: {args.start}")
    if start >= end:
        raise InputError("window start must be before its end")
    prev_start = start - (end - start)

    repos_path = Path(args.repos) if args.repos else DEFAULT_REPOS
    repos, book = _load_repos(repos_path)
    base = repos_path.resolve().parent
    warnings: list[str] = []
    for repo in repos:
        if repo.problem:
            warnings.append(repo.problem)
    no_outside = not any(r.cls == "outside" for r in repos)
    if no_outside:
        warnings.append("no outside repo listed")
        warnings.append(NO_READING)
    broken = [r.name for r in repos if r.problem]
    unreadable = bool(broken)
    reading_reason = (f"no reading: repo {broken[0]} unreadable" if unreadable
                      else NO_READING if no_outside else None)

    cur_commits: dict[str, list[dict[str, Any]]] = {}
    records_only: dict[str, set[str]] = {}
    per_repo: dict[str, dict[str, int]] = {}
    m_cur = o_cur = m_prev = o_prev = merged = 0
    for repo in repos:
        cur = repo.commits(start, end, "--no-merges")
        prev = repo.commits(prev_start, start, "--no-merges")
        cur_commits[repo.name] = cur
        records_only[repo.name] = {
            c["sha"] for c in cur if repo.is_records_only(repo.changed_paths(c["sha"]))
        }
        per_repo[repo.name] = {"total": len(cur), "records_only": len(records_only[repo.name])}
        if repo.cls == "machinery":
            m_cur += len(cur)
            m_prev += len(prev)
        else:
            o_cur += len(cur)
            o_prev += len(prev)
        merged += len(repo.commits(start, end, "--first-parent", "--merges"))

    total = sum(v["total"] for v in per_repo.values())
    rec = sum(v["records_only"] for v in per_repo.values())
    m_share = _pct(m_cur, m_cur + o_cur)
    o_share = round(100 - m_share, 1) if m_share is not None else None
    prev_share = _pct(m_prev, m_prev + o_prev)
    if unreadable:
        m_share = o_share = prev_share = None

    rulebook, texts = _rulebook(book, base, warnings)
    coordination = _coordination(args.meter, (start, end), merged, warnings)
    return {
        "window": {"start": _iso_z(start), "end": _iso_z(end)},
        "previous_window": {"start": _iso_z(prev_start), "end": _iso_z(start)},
        "commits": {"total": total, "records_only": rec,
                    "records_only_share": _pct(rec, total), "per_repo": per_repo},
        "machinery": {"machinery_commits": m_cur, "outside_commits": o_cur,
                      "machinery_share": m_share, "outside_share": o_share},
        "rulebook": rulebook,
        "coordination": coordination,
        "owner_questions": _owner_questions(repos, start, end),
        "tripwire": _tripwire(float(args.threshold), m_share, prev_share, repos, cur_commits,
                              records_only, book, texts,
                              reading_reason, unreadable),
        "warnings": warnings,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="T12 overhead meter and trip-wire")
    ap.add_argument("--repos", help="repos JSON (default wiki/status/t12-repos.json)")
    ap.add_argument("--now", help="window end, ISO 8601 (default: current UTC time)")
    ap.add_argument("--days", type=float, default=7, help="window length in days (default 7)")
    ap.add_argument("--start", help="window start, ISO 8601 (overrides --days)")
    ap.add_argument("--meter", help="coord_meter.py JSON report")
    ap.add_argument("--threshold", type=float, default=80, help="machinery share trip level")
    ap.add_argument("--out", help="also write the JSON here")
    args = ap.parse_args(argv)
    try:
        report = build_report(args)
    except InputError as exc:
        print(f"overhead_meter: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        try:
            Path(args.out).write_text(text, encoding="utf-8")
        except OSError as exc:
            print(f"overhead_meter: cannot write {args.out}: {exc}", file=sys.stderr)
            return 2
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
